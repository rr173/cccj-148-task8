"""场景 1-4、7：连续上传 / 相同包再传 / 篡改包与非法记录 / 断层补齐 / 隔离。"""
import json
import unittest

from relay import protocol, views
from relay.tests import fixture as fx


class Base(unittest.TestCase):
    def setUp(self):
        self.relay_db, self.stowage_db, self.spool = fx.new_paths()
        self.vid, self.mapping = fx.seed_voyage(self.stowage_db)
        self.recv = fx.fresh_receiver(self.relay_db, self.stowage_db, self.vid)
        self.items = list(self.mapping.items())

    def upload_env(self, spool_or_env):
        if hasattr(spool_or_env, "read_packet"):
            raise ValueError("need env")
        return self.recv.upload(json.dumps(spool_or_env, ensure_ascii=False))

    def mkspool(self, device="HH-01", session=None):
        sp = fx.make_spool(self.spool, device)
        sp.start_session(session or f"{device}:s1")
        return sp

    def make_packet(self, device, session, seq, records, voyage=True):
        return protocol.seal({
            "device_id": device, "session": session, "seq": seq,
            "voyage_id": self.vid if voyage else None,
            "records": records})

    def rec(self, container, slot, rid=None):
        import uuid
        return {"record_id": rid or f"rec_{uuid.uuid4().hex[:12]}",
                "container": container, "slot": slot,
                "collected_at": "2026-09-28T09:00:00+08:00"}

    def assertOk(self):
        v = views.verify_invariants(self.relay_db, self.stowage_db)
        self.assertTrue(v["ok"], v["violations"])


class TestContinuousUpload(Base):
    """场景 1：正常连续上传，游标推进、全部装船确认。"""

    def test_three_packets_in_order(self):
        sp = self.mkspool()
        # 段0：2箱；段1：2箱；段2：1箱
        for batch in (self.items[0:2], self.items[2:4], self.items[4:5]):
            for c, s in batch:
                sp.append_scan(c, s)
            sp.seal(self.vid)

        acks = []
        for seq in range(3):
            acks.append(self.upload_env(sp.read_packet(seq)))

        self.assertEqual([a["cursor"] for a in acks], [0, 1, 2])
        self.assertTrue(all(r["status"] == "applied"
                            for a in acks for r in a["records"]))
        overview = views.sessions_overview(self.relay_db)[0]
        self.assertEqual(overview["cursor"], 2)
        self.assertEqual(overview["next_expected"], 3)
        applied, _ = views.applied(self.relay_db)
        self.assertEqual(len(applied), 5)
        # 现有装船确认表确实有 5 条，且 container 被锁定
        from stowage.db import connect
        n = connect(self.stowage_db).execute(
            "SELECT COUNT(*) c FROM confirmations").fetchone()["c"]
        self.assertEqual(n, 5)
        self.assertOk()


class TestDuplicateReplay(Base):
    """场景 2：相同包再次上传，返回原确认，不产生重复确认。"""

    def test_same_packet_returns_original_ack(self):
        sp = self.mkspool()
        for c, s in self.items[:2]:
            sp.append_scan(c, s)
        sp.seal(self.vid)
        env = sp.read_packet(0)

        a1 = self.upload_env(env)
        self.assertFalse(a1["duplicate"])
        a2 = self.upload_env(env)
        a3 = self.upload_env(env)
        self.assertTrue(a2["duplicate"])
        self.assertTrue(a3["duplicate"])
        # 回放的是原确认（confirmation_id 一致）
        self.assertEqual([r["confirmation_id"] for r in a1["records"]],
                         [r["confirmation_id"] for r in a2["records"]])
        from stowage.db import connect
        n = connect(self.stowage_db).execute(
            "SELECT COUNT(*) c FROM confirmations").fetchone()["c"]
        self.assertEqual(n, 2)
        self.assertOk()


class TestTamperAndInvalid(Base):
    """场景 3：篡改包（校验和错）与记录非法均整包拒绝，绝不写入部分记录。"""

    def test_checksum_tamper_rejected_entirely(self):
        c1, s1 = self.items[0]
        good = self.make_packet("HH-01", "HH-01:s1", 0,
                                [self.rec(c1, s1)])
        a = self.upload_env(good)
        self.assertEqual(a["status"], "accepted")

        # 篡改：换箱号但不重算 checksum
        bad = json.loads(json.dumps(good))
        bad["records"][0]["container"] = self.items[1][0]
        # 同会话同序号不同内容 + 坏校验和 => 优先按校验和整包拒绝
        with self.assertRaises(Exception) as cm:
            self.upload_env(bad)
        self.assertEqual(cm.exception.code, "CHECKSUM_MISMATCH")

        # 没有明细落库（good 只有 1 条 applied）
        applied, _ = views.applied(self.relay_db)
        self.assertEqual(len(applied), 1)
        rej = views.rejected(self.relay_db)
        self.assertTrue(any(r["error_code"] == "CHECKSUM_MISMATCH"
                            for r in rej))

        # 合法重传（正确内容+正确校验和）仍可被接受 —— 被拒包不占序号
        good2 = self.make_packet(
            "HH-02", "HH-02:s1", 0,
            [self.rec(self.items[2][0], self.items[2][1])])
        # 用同一会话再发一个序号 1 的新合法包验证不阻断后续
        good3 = self.make_packet(
            "HH-01", "HH-01:s1", 1,
            [self.rec(self.items[3][0], self.items[3][1])])
        self.assertEqual(self.upload_env(good3)["status"], "accepted")
        self.assertOk()

    def test_invalid_record_rejected_entirely(self):
        c1, s1 = self.items[0]
        c2, s2 = self.items[1]
        # 包内两条，第二条槽位非法；重封后校验和正确
        pkt = self.make_packet("HH-09", "HH-09:s1", 0,
                               [self.rec(c1, s1, "rec_a"),
                                self.rec(c2, "NOT-A-SLOT", "rec_b")])
        with self.assertRaises(Exception) as cm:
            self.upload_env(pkt)
        self.assertEqual(cm.exception.code, "INVALID_RECORD")
        # 连“合法的第一条”也不得写入
        self.assertEqual(views.pending(self.relay_db), [])
        applied, _ = views.applied(self.relay_db)
        self.assertEqual(applied, [])
        from stowage.db import connect
        self.assertEqual(connect(self.stowage_db).execute(
            "SELECT COUNT(*) c FROM confirmations").fetchone()["c"], 0)
        self.assertOk()

    def test_bad_container_number_rejected(self):
        pkt = self.make_packet("HH-09", "HH-09:s2", 0,
                               [self.rec("MSKU0000012", "02-01-01")])
        with self.assertRaises(Exception) as cm:
            self.upload_env(pkt)
        self.assertEqual(cm.exception.code, "INVALID_RECORD")


class TestGapFill(Base):
    """场景 4：序号断层暂存不推进；断层补齐后连续处理（暂存自动排空）。"""

    def test_gap_then_fill_drains(self):
        sess = "HH-01:gap"
        p0 = self.make_packet("HH-01", sess, 0,
                              [self.rec(*self.items[0])])
        p2 = self.make_packet("HH-01", sess, 2,
                              [self.rec(*self.items[1])])
        p1 = self.make_packet("HH-01", sess, 1,
                              [self.rec(*self.items[2])])

        self.assertEqual(self.upload_env(p0)["cursor"], 0)
        # 2 先到：暂存，游标不动
        staged = self.upload_env(p2)
        self.assertEqual(staged["status"], "staged")
        self.assertEqual(staged["missing"], [1])
        overview = views.sessions_overview(self.relay_db)[0]
        self.assertEqual(overview["cursor"], 0)
        self.assertEqual(len(overview["staged"]), 1)

        # p2 重发：回放 staged 结论
        again = self.upload_env(p2)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["status"], "staged")

        # 1 补齐：游标连推到 2，暂存包自动应用
        ack1 = self.upload_env(p1)
        self.assertEqual(ack1["cursor"], 2)  # drain 已把 #2 一并推进
        overview = views.sessions_overview(self.relay_db)[0]
        self.assertEqual(overview["staged"], [])
        applied, _ = views.applied(self.relay_db)
        self.assertEqual(len(applied), 3)
        self.assertOk()


class TestQuarantine(Base):
    """场景补充：相同会话序号、不同内容 -> 隔离；原确认不变。"""

    def test_same_seq_different_content_quarantine(self):
        sess = "HH-01:q"
        good = self.make_packet("HH-01", sess, 0,
                                [self.rec(self.items[0][0],
                                          self.items[0][1], "rec_orig")])
        self.assertEqual(self.upload_env(good)["status"], "accepted")

        evil = self.make_packet("HH-01", sess, 0,
                                [self.rec(self.items[1][0],
                                          self.items[1][1], "rec_other")])
        q = self.upload_env(evil)
        self.assertEqual(q["status"], "quarantine")

        # 原确认原样可回放
        replay = self.upload_env(good)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(replay["status"], "accepted")
        # 隔离重放
        replay_q = self.upload_env(evil)
        self.assertEqual(replay_q["status"], "quarantine")

        quar = views.quarantine(self.relay_db)
        self.assertEqual(len(quar), 1)
        self.assertEqual(quar[0]["seq"], 0)
        # 游标没被隔离包推动
        self.assertEqual(views.sessions_overview(self.relay_db)[0]["cursor"], 0)
        self.assertOk()


if __name__ == "__main__":
    unittest.main()
