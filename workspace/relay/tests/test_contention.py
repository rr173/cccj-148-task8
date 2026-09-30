"""场景 5-6：双终端争用同一箱 / 箱已在别处确认 / 计划更替，以及处置与幂等。"""
import json
import unittest
import uuid

from relay import console, protocol, views
from relay.tests import fixture as fx
from stowage import service
from stowage.db import connect


class ContentionBase(unittest.TestCase):
    def setUp(self):
        self.relay_db, self.stowage_db, self.spool = fx.new_paths()
        self.vid, self.mapping = fx.seed_voyage(self.stowage_db)
        self.recv = fx.fresh_receiver(self.relay_db, self.stowage_db, self.vid)
        self.items = list(self.mapping.items())

    def pkt(self, device, seq, rid, container, slot, session=None):
        env = {
            "device_id": device,
            "session": session or f"{device}:s1",
            "seq": seq, "voyage_id": self.vid,
            "records": [{
                "record_id": rid, "container": container, "slot": slot,
                "collected_at": "2026-09-28T10:00:00+08:00"}]}
        return protocol.seal(env)

    def up(self, env):
        return self.recv.upload(json.dumps(env, ensure_ascii=False))

    def disp(self, rid, action, request_id=None, **kw):
        return console.dispose(self.recv,
                               request_id or f"req_{uuid.uuid4().hex[:8]}",
                               rid, action, **kw)

    def pending_of(self, container):
        return [r for r in views.pending(self.relay_db)
                if r["container"] == container]


class TestDualTerminalSameContainer(ContentionBase):
    """两台终端同时报告同一箱：只保留一项可执行结果，其余待处理。"""

    def test_second_report_pending_and_resolvable(self):
        c, s = self.items[0]
        a1 = self.up(self.pkt("HH-01", 0, "rec_a", c, s))
        a2 = self.up(self.pkt("HH-02", 0, "rec_b", c, s))
        self.assertEqual(a1["records"][0]["status"], "applied")
        self.assertEqual(a2["records"][0]["status"], "pending")
        self.assertEqual(a2["records"][0]["reason"], "DUPLICATE_CONTAINER")

        # 只有一项可执行结果
        applied = [r for r, _ in
                   [(x, None) for x in views.applied(self.relay_db)[0]]
                   if r["container"] == c]
        self.assertEqual(len(applied), 1)
        pend = self.pending_of(c)
        self.assertEqual(len(pend), 1)

        # 处置 1：忽略
        res = self.disp("rec_b", "ignore", note="重复扫描，忽略")
        self.assertEqual(res["status"], "ignored")
        self.assertEqual(self.pending_of(c), [])
        # 装船确认仍只有 1 条
        self.assertEqual(connect(self.stowage_db).execute(
            "SELECT COUNT(*) c FROM confirmations").fetchone()["c"], 1)
        self.assertTrue(
            views.verify_invariants(self.relay_db, self.stowage_db)["ok"])

    def test_reconfirm_stays_pending_for_contention(self):
        c, s = self.items[0]
        self.up(self.pkt("HH-01", 0, "rec_a", c, s))
        self.up(self.pkt("HH-02", 0, "rec_b", c, s))
        # 重调：业务入口幂等命中第一项 => 保持 pending
        res = self.disp("rec_b", "reconfirm")
        self.assertEqual(res["outcome"], "noop")
        self.assertEqual(res["status"], "pending")
        self.assertEqual(res["reason"], "CONFIRMED_ELSEWHERE")
        self.assertEqual(len(self.pending_of(c)), 1)


class TestConfirmedElsewhere(ContentionBase):
    """箱已在中继器之外确认：扫描进来直接待处理，原确认保留。"""

    def test_external_confirmation_yields_pending(self):
        c, s = self.items[1]
        plan = service._latest_published(connect(self.stowage_db), self.vid)
        # 模拟别处（现有入口）先确认
        service.confirm_load(self.vid, plan["id"], c, s, actor="crane-A",
                             db_path=self.stowage_db)
        a = self.up(self.pkt("HH-03", 0, "rec_x", c, s))
        self.assertEqual(a["records"][0]["status"], "pending")
        self.assertEqual(a["records"][0]["reason"], "CONFIRMED_ELSEWHERE")

        # 操作台忽略，原确认不动
        self.disp("rec_x", "ignore")
        row = connect(self.stowage_db).execute(
            "SELECT actor FROM confirmations f JOIN containers c2 "
            "ON c2.id=f.container_id WHERE c2.id=?", (c,)).fetchone()
        self.assertEqual(row["actor"], "crane-A")


class TestPlanChanged(ContentionBase):
    """断网期间计划更替：上报旧槽位 => pending(PLAN_CHANGED)，可重映射到新槽位。"""

    def test_swap_plan_then_remap(self):
        # 选 02-02 堆的 HAM 箱（与下层同港，对调后方案仍合法）
        inv = {s: c for c, s in self.mapping.items()}
        c3 = inv["02-02-02"]   # MSKU0000032，原槽位 02-02-02
        s3 = "02-02-02"

        # 计划更替：c3 改配到同堆同港的 02-02-01（合法新版本）
        new_plan, free_slot = fx.republish_move_container_to_valid_slot(
            self.stowage_db, self.vid, c3)
        new_map = fx.plan_slot_mapping(self.stowage_db, self.vid, new_plan)

        # 终端按旧槽位上报 c3
        a = self.up(self.pkt("HH-01", 0, "rec_c3_old", c3, s3))
        self.assertEqual(a["records"][0]["status"], "pending")
        self.assertEqual(a["records"][0]["reason"], "PLAN_CHANGED")

        # 错误重映射到无关槽位 => 拒绝
        bad = console.ConsoleError
        with self.assertRaises(bad) as cm:
            self.disp("rec_c3_old", "remap", new_slot=s3)
        self.assertEqual(cm.exception.code, "REMAP_SLOT_MISMATCH")

        # 重映射到当前方案槽位 => 应用
        target = new_map[c3]
        res = self.disp("rec_c3_old", "remap", new_slot=target)
        self.assertEqual(res["status"], "applied")
        self.assertEqual(res["resolved_slot"], target)

        # 业务表里 c3 的实际槽位 == 新方案槽位
        got = connect(self.stowage_db).execute(
            "SELECT s.code FROM confirmations f JOIN slots s ON s.id=f.slot_id "
            "JOIN containers c ON c.id=f.container_id WHERE c.id=?",
            (c3,)).fetchone()["code"]
        self.assertEqual(got, target)

        # 映射视图体现：扫描槽位 != 方案槽位，但已 resolved
        mp = views.voyage_mapping(self.relay_db, self.stowage_db, self.vid)
        row = [r for r in mp["records"] if r["container"] == c3][0]
        self.assertFalse(row["slot_matches_plan"])
        self.assertEqual(row["resolved_slot"], target)
        self.assertTrue(
            views.verify_invariants(self.relay_db, self.stowage_db)["ok"])


class TestDispositionIdempotent(ContentionBase):
    """场景 6：同一处置请求多次发送只记一次，回放首次结果。"""

    def test_same_request_id_records_once(self):
        c, s = self.items[0]
        self.up(self.pkt("HH-01", 0, "rec_a", c, s))
        self.up(self.pkt("HH-02", 0, "rec_b", c, s))

        rid = "req-xyz-001"
        r1 = self.disp("rec_b", "ignore", request_id=rid, note="首次")
        r2 = self.disp("rec_b", "ignore", request_id=rid, note="重复")
        r3 = self.disp("rec_b", "ignore", request_id=rid, note="再重复")
        self.assertFalse(r1["idempotent"])
        self.assertTrue(r2["idempotent"])
        self.assertTrue(r3["idempotent"])

        con = connect(self.relay_db)
        n = con.execute(
            "SELECT COUNT(*) c FROM dispositions WHERE request_id=?",
            (rid,)).fetchone()["c"]
        self.assertEqual(n, 1)
        # 事件日志也只有一次忽略
        ne = con.execute(
            "SELECT COUNT(*) c FROM relay_events WHERE kind='RECORD_IGNORED'")\
            .fetchone()["c"]
        self.assertEqual(ne, 1)
        con.close()

    def test_reconfirm_after_ignore_still_sees_business_owner(self):
        """原赢家被忽略（装船确认保留）后，待处理记录重调仍命中旧确认，
        保持 pending —— 同一箱只保留一项可执行结果。"""
        c, s = self.items[4]
        self.up(self.pkt("HH-01", 0, "rec_winner", c, s))
        self.up(self.pkt("HH-02", 0, "rec_loser", c, s))
        # 忽略赢家
        self.disp("rec_winner", "ignore", request_id="ignore-win")
        # 业务表里该箱仍有确认（忽略不撤销装船）。loser 重调命中旧确认
        res = self.disp("rec_loser", "reconfirm", request_id="rec-los")
        self.assertEqual(res["outcome"], "noop")
        self.assertEqual(res["reason"], "CONFIRMED_ELSEWHERE")
        # 同一请求再发：幂等回放（两次返回体语义一致）
        again = console.dispose(self.recv, "rec-los", "rec_loser",
                                "reconfirm")
        self.assertTrue(again["idempotent"])
        self.assertEqual(again["reason"], "CONFIRMED_ELSEWHERE")

    def test_reconfirm_succeeds_when_plan_matches_again(self):
        """计划换版使旧槽位记录 pending；计划再换回原槽位后重调成功。"""
        inv = {s: c for c, s in self.mapping.items()}
        c3 = inv["02-02-02"]
        s3 = "02-02-02"
        # 换版：c3 槽位变为同堆同港的另一槽
        _, other = fx.republish_move_container_to_valid_slot(
            self.stowage_db, self.vid, c3)
        self.assertNotEqual(other, s3)
        # 终端按原槽位上报 => PLAN_CHANGED
        a = self.up(self.pkt("HH-07", 0, "rec_old", c3, s3))
        self.assertEqual(a["records"][0]["reason"], "PLAN_CHANGED")
        # 重调：条件不满足，保持 pending
        r1 = self.disp("rec_old", "reconfirm", request_id="rc1")
        self.assertEqual(r1["outcome"], "noop")
        # 再次换版换回原槽位
        fx.republish_relocate(self.stowage_db, self.vid, c3, s3)
        cur = service._latest_published(connect(self.stowage_db), self.vid)
        self.assertEqual(fx.plan_slot_mapping(self.stowage_db, self.vid,
                                              cur["id"])[c3], s3)
        # 重调成功
        r2 = self.disp("rec_old", "reconfirm", request_id="rc2")
        self.assertEqual(r2["outcome"], "applied")
        self.assertEqual(r2["confirmation_id"][:4], "cnf_")
        self.assertEqual(len(self.pending_of(c3)), 0)


class TestRemapValidation(ContentionBase):
    def test_remap_requires_pending(self):
        c, s = self.items[5]
        self.up(self.pkt("HH-01", 0, "rec_a", c, s))
        with self.assertRaises(console.ConsoleError) as cm:
            self.disp("rec_a", "remap", new_slot=s)
        self.assertEqual(cm.exception.code, "NOT_PENDING")

    def test_remap_bad_slot(self):
        c, s = self.items[6]
        c2, s2 = self.items[7]
        self.up(self.pkt("HH-01", 0, "rec_win", c, s))
        self.up(self.pkt("HH-02", 0, "rec_lose", c, s))
        with self.assertRaises(console.ConsoleError) as cm:
            self.disp("rec_lose", "remap", new_slot="nope")
        self.assertEqual(cm.exception.code, 422) if False else \
            self.assertEqual(cm.exception.http_status, 422)


if __name__ == "__main__":
    unittest.main()
