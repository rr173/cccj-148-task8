"""场景 9：中断恢复（真实子进程 os._exit(91) 掉电）。

覆盖需求中四个关键保存边界：
  C1 保存许可 / C2 迁移未执行箱 / C3 登记扫描并推进波次 —— 提交前掉电，
     整体回滚，重试幂等成功；
  C4 装船记录已存在但波次仍显示未开始的撕裂 —— 启动 reconcile 修复，
     随后扫描靠“已装船”幂等回放。

每个场景恢复后都校验三条不变量：
  ① 同一箱不会同时属于两张有效许可；
  ② 同一舱区不会被重叠占用；
  ③ 装船记录与波次进度一致（不存在“有记录但波次未开始”）。
"""
import os
import subprocess
import sys
import tempfile
import unittest

from permit import service
from permit.store import connect
from permit.tests.fixture import make_fixture, t


class CrashBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "permit_crash.db")
        os.environ["PERMIT_DB"] = self.db  # 主进程对账/重试也走这个库

    def tearDown(self):
        self.tmp.cleanup()

    def crash_once(self, point):
        env = dict(os.environ, PERMIT_CRASH=point)
        env.pop("PERMIT_DB", None)
        p = subprocess.run(
            [sys.executable, "-m", "permit.tests.crash_worker", self.db, point],
            capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(
            p.returncode, 91,
            f"{point} 应在崩溃点以 91 退出，得 {p.returncode}\n"
            f"stdout={p.stdout}\nstderr={p.stderr}")


class TestC1PermitSave(CrashBase):
    """C1：保存许可事务提交前退出 -> 许可/舱区/箱归属整体回滚。"""

    def test_rollback_and_retry(self):
        self.crash_once("C1")
        conn = connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM permits WHERE crane_id='QC-7'"
        ).fetchone()["c"], 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM permit_hatches WHERE permit_id IN "
            "(SELECT id FROM permits WHERE crane_id='QC-7')").fetchone()["c"], 0)
        conn.close()
        # 崩溃发生在提交前：夹具的 PA/PB 都已提交，QC-7 授权整体回滚，
        # 同一窗口/舱区可以重新授予
        os.environ["PERMIT_DB"] = self.db
        p, st = service.create_permit("W-1", {
            "crane_id": "QC-7", "hatches": ["H1"],
            "valid_from": t("12:30:00"), "valid_to": t("13:00:00"),
            "container_ids": []}, now=t("08:00:00"))
        self.assertEqual(st, 201)
        self.assertTrue(service.verify(self.db)["ok"])


class TestC2Transfer(CrashBase):
    """C2：迁移未执行箱提交前退出 -> 没有箱被双归属，重试迁移成功。"""

    def test_no_double_owner_and_idempotent_retry(self):
        self.crash_once("C2")
        conn = connect(self.db)
        # 目标许可 PC 已在 C2 之前……不，创建 PC 与转交是两个独立已提交事务，
        # PC 存在，但转交整体回滚，六个箱仍归 PA
        pc = conn.execute("SELECT COUNT(*) c FROM permits WHERE id='PC'"
                          ).fetchone()["c"]
        self.assertEqual(pc, 1)
        dup = conn.execute(
            "SELECT wave_id, container_id, COUNT(*) c FROM permit_containers "
            "WHERE state='active' GROUP BY 1,2 HAVING c>1").fetchall()
        self.assertEqual(dup, [])
        pa_boxes = conn.execute(
            "SELECT COUNT(*) c FROM permit_containers pc JOIN permits p "
            "ON p.id=pc.permit_id WHERE p.id='PA' AND pc.state='active'"
        ).fetchone()["c"]
        self.assertEqual(pa_boxes, 6)
        conn.close()

        # 以同一幂等键重试转交
        r, st = service.transfer_permit(
            "PA", {"target_permit_id": "PC", "expected_source_revision": 1},
            idem_key="tr-crash", now=t("12:10:00"))
        self.assertEqual(st, 200)
        self.assertEqual(len(r["migrated"]), 6)
        self.assertTrue(service.verify(self.db)["ok"])


class TestC3Scan(CrashBase):
    """C3：登记扫描结果/推进波次进度提交前退出 -> 无装船记录、波次未开始；
    同一幂等键重试成功且只装一次。"""

    def test_rollback_scan_then_idempotent_retry(self):
        self.crash_once("C3")
        conn = connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM loading_scans").fetchone()["c"], 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM idem_records WHERE idem_key='scan-crash'"
        ).fetchone()["c"], 0)
        w = conn.execute("SELECT * FROM waves WHERE id='W-1'").fetchone()
        self.assertEqual((w["status"], w["executed"], w["started_at"]),
                         ("released", 0, None))
        self.assertEqual(conn.execute(
            "SELECT executed FROM wave_containers WHERE container_id='C-S1-0'"
        ).fetchone()["executed"], 0)
        conn.close()

        body, st = service.confirm_scan(
            {"permit_id": "PA", "revision": 1, "container_id": "C-S1-0",
             "scanner_id": "HH-C"}, idem_key="scan-crash", now=t("09:00:00"))
        self.assertEqual(st, 200)
        self.assertEqual(body["outcome"], "loaded")
        scans = service.list_scans("W-1")["scans"]
        self.assertEqual(len(scans), 1)
        w = service.wave_detail("W-1")
        self.assertEqual((w["status"], w["executed"]), ("in_progress", 1))
        self.assertTrue(service.verify(self.db)["ok"])


class TestC4TornScanReconcile(CrashBase):
    """C4：装船记录已存在，但波次仍显示未开始（两阶段撕裂）。

    启动 reconcile 以装船记录为事实源修复 executed/progress/status；
    终端重扫该箱得到幂等回放，不会重复装船。
    """

    def test_reconcile_repairs_progress(self):
        self.crash_once("C4")
        conn = connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) c FROM loading_scans").fetchone()["c"], 1)
        w = conn.execute("SELECT * FROM waves WHERE id='W-1'").fetchone()
        self.assertEqual(w["status"], "released")       # 撕裂：有记录但未开始
        self.assertIsNone(w["started_at"])
        conn.close()
        # 自检必须能发现
        v = service.verify(self.db)
        self.assertFalse(v["ok"])
        codes = {x["code"] for x in v["violations"]}
        self.assertIn("WAVE_NOT_STARTED_WITH_SCANS", codes)

        # 启动恢复（与 permit.server.main 调用同一个入口）
        rec = service.reconcile(self.db, now=t("09:01:00"))
        actions = {a["type"] for a in rec["actions"]}
        self.assertIn("mark_executed", actions)
        self.assertIn("wave_progress_fixed", actions)
        self.assertTrue(rec["verify"]["ok"])

        w = service.wave_detail("W-1")
        self.assertEqual((w["status"], w["executed"]), ("in_progress", 1))
        self.assertIsNotNone(w["started_at"])

        # 终端重扫 -> 已装船幂等回放，不产生第二条记录
        body, st = service.confirm_scan(
            {"permit_id": "PA", "revision": 1, "container_id": "C-S1-0"},
            now=t("09:02:00"))
        self.assertEqual(st, 200)
        self.assertEqual(body["outcome"], "loaded")
        self.assertTrue(body["duplicate"])
        self.assertEqual(len(service.list_scans("W-1")["scans"]), 1)
        self.assertTrue(service.verify(self.db)["ok"])
