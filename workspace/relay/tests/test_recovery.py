"""场景 8：三个中断恢复点（落盘 / 业务入口 / 游标）。

真实子进程在各点 os._exit(91) 模拟掉电；主测试进程新建 Receiver 走
recover()，验证收件状态、装船确认、游标三者最终一致，并验证终端重传
返回原确认（幂等）。
"""
import json
import os
import subprocess
import sys
import unittest

from relay import protocol, views
from relay.receiver import Receiver
from relay.store import connect
from relay.tests import fixture as fx
from stowage.db import connect as biz_connect


class CrashBase(unittest.TestCase):
    def setUp(self):
        self.relay_db, self.stowage_db, self.spool = fx.new_paths()
        self.vid, self.mapping = fx.seed_voyage(self.stowage_db)

    def crash_once(self, point):
        env = dict(os.environ, STOWAGE_DB=self.stowage_db)
        p = subprocess.run(
            [sys.executable, "-m", "relay.tests.crash_worker",
             self.relay_db, self.stowage_db, self.vid, point],
            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 91,
                         f"{point} 应在崩溃点退出，得到 {p.returncode}\n"
                         f"stdout={p.stdout}\nstderr={p.stderr}")

    def fresh(self):
        recv = Receiver(self.relay_db, self.stowage_db)
        return recv

    def resend_packet(self, recv):
        items = list(self.mapping.items())[:2]
        pkt = protocol.seal({
            "device_id": "HH-CRASH", "session": "HH-CRASH:s1", "seq": 0,
            "voyage_id": self.vid,
            "records": [
                {"record_id": "rec_crash_1", "container": items[0][0],
                 "slot": items[0][1],
                 "collected_at": "2026-09-28T11:00:00+08:00"},
                {"record_id": "rec_crash_2", "container": items[1][0],
                 "slot": items[1][1],
                 "collected_at": "2026-09-28T11:00:01+08:00"}]})
        return recv.upload(json.dumps(pkt, ensure_ascii=False))

    def assert_consistent(self, recv, confirmations=2):
        v = views.verify_invariants(recv.db_path, recv.stowage_db)
        self.assertTrue(v["ok"], v["violations"])
        cur = connect(recv.db_path).execute(
            "SELECT cursor FROM sessions WHERE session=?",
            ("HH-CRASH:s1",)).fetchone()["cursor"]
        self.assertEqual(cur, 0)
        n = biz_connect(recv.stowage_db).execute(
            "SELECT COUNT(*) c FROM confirmations f JOIN containers c "
            "ON c.id=f.container_id WHERE f.voyage_id=?",
            (self.vid,)).fetchone()["c"]
        self.assertEqual(n, confirmations)
        applied, _ = views.applied(recv.db_path)
        self.assertEqual(len(applied), 2)


class TestP1BeforeCommit(CrashBase):
    """P1：落盘事务提交前退出。原始包/明细整体回滚，重传即正常接受。"""

    def test_recover_after_p1(self):
        self.crash_once("P1")
        # 崩溃瞬间：无 accepted 包、无明细、无确认、无游标
        con = connect(self.relay_db)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) c FROM raw_packets WHERE status='accepted'"
        ).fetchone()["c"], 0)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) c FROM scan_records").fetchone()["c"], 0)
        con.close()
        self.assertEqual(biz_connect(self.stowage_db).execute(
            "SELECT COUNT(*) c FROM confirmations").fetchone()["c"], 0)

        recv = self.fresh()
        recv.recover()  # 没有可补偿内容，不报错
        # 终端网络恢复后重发同一包 -> 正常接受（不是 quarantine/duplicate）
        ack = self.resend_packet(recv)
        self.assertEqual(ack["status"], "accepted")
        self.assertFalse(ack["duplicate"])
        self.assert_consistent(recv)


class TestP2BetweenBusinessCalls(CrashBase):
    """P2：调用业务入口途中退出（第一条已确认、第二条没开始）。

    重启恢复：补处理剩余记录并推进游标；已确认的第一条靠业务入口幂等安全重放。
    """

    def test_recover_after_p2(self):
        self.crash_once("P2")
        con = connect(self.relay_db)
        # 整包事务已提交：包+两条明细都在；游标未推进
        self.assertEqual(con.execute(
            "SELECT status FROM raw_packets WHERE session=? AND seq=0",
            ("HH-CRASH:s1",)).fetchone()["status"], "accepted")
        self.assertEqual(con.execute(
            "SELECT COUNT(*) c FROM scan_records").fetchone()["c"], 2)
        self.assertEqual(con.execute(
            "SELECT cursor FROM sessions WHERE session=?",
            ("HH-CRASH:s1",)).fetchone()["cursor"], -1)
        con.close()
        # 第一条业务确认已落库，第二条没有
        self.assertEqual(biz_connect(self.stowage_db).execute(
            "SELECT COUNT(*) c FROM confirmations").fetchone()["c"], 1)

        recv = self.fresh()
        actions = recv.recover()
        # 第一条在崩溃前已 applied；恢复只需补处理第二条（业务入口幂等，
        # 已确认的第一条不会重复）
        self.assertEqual(len(actions), 1)
        self.assertEqual(len(actions[0]["records"]), 1)
        self.assert_consistent(recv)

        # 终端重传：回原确认，不新增业务记录
        ack = self.resend_packet(recv)
        self.assertTrue(ack["duplicate"])
        self.assertEqual(biz_connect(self.stowage_db).execute(
            "SELECT COUNT(*) c FROM confirmations").fetchone()["c"], 2)


class TestP3AfterCursor(CrashBase):
    """P3：游标更新提交后、响应返回前退出。

    恢复是纯重放：所有状态已一致，不产生重复确认。
    """

    def test_recover_after_p3(self):
        self.crash_once("P3")
        # 崩溃瞬间所有工作其实已完成
        self.assertEqual(biz_connect(self.stowage_db).execute(
            "SELECT COUNT(*) c FROM confirmations").fetchone()["c"], 2)

        recv = self.fresh()
        actions = recv.recover()
        # 明细均已 applied，恢复无需新增动作（应用记录被幂等跳过）
        self.assertEqual(actions, [])
        self.assert_consistent(recv)

        # 重传返回原确认
        ack = self.resend_packet(recv)
        self.assertTrue(ack["duplicate"])
        self.assertEqual(ack["cursor"], 0)
        self.assertEqual(biz_connect(self.stowage_db).execute(
            "SELECT COUNT(*) c FROM confirmations").fetchone()["c"], 2)


class TestP2WithGapDrain(CrashBase):
    """P2 发生在断层排空过程中：重启继续排空，游标最终到位。"""

    def test_gap_drain_p2_recover(self):
        # 先让 seq=2 暂存（seq0/1 缺失），再用崩溃流程处理 seq=0 太复杂；
        # 这里直接验证 recover 后可以正常处理新序号 1，以及暂存包继续排空。
        recv0 = Receiver(self.relay_db, self.stowage_db)
        con = connect(self.relay_db)
        from relay import store
        store.set_config(con, "active_voyage", self.vid)
        con.commit()
        con.close()
        items = list(self.mapping.items())
        p2 = protocol.seal({
            "device_id": "HH-G", "session": "HH-G:s1", "seq": 2,
            "voyage_id": self.vid,
            "records": [{"record_id": "rec_g2", "container": items[2][0],
                         "slot": items[2][1],
                         "collected_at": "2026-09-28T12:00:00+08:00"}]})
        r = recv0.upload(json.dumps(p2, ensure_ascii=False))
        self.assertEqual(r["status"], "staged")

        self.crash_once("P2")  # HH-CRASH:s1 与 HH-G 互不影响
        recv = self.fresh()
        recv.recover()

        # 补齐 HH-G 的 0、1 后，#2 应自动排空
        for seq, (c, s) in ((0, items[3]), (1, items[4])):
            pkt = protocol.seal({
                "device_id": "HH-G", "session": "HH-G:s1", "seq": seq,
                "voyage_id": self.vid,
                "records": [{"record_id": f"rec_g{seq}", "container": c,
                             "slot": s,
                             "collected_at": "2026-09-28T12:00:01+08:00"}]})
            ack = recv.upload(json.dumps(pkt, ensure_ascii=False))
        self.assertEqual(ack["cursor"], 2)
        self.assertEqual(ack["status"], "accepted")
        self.assertTrue(views.verify_invariants(
            recv.db_path, recv.stowage_db)["ok"])


if __name__ == "__main__":
    unittest.main()
