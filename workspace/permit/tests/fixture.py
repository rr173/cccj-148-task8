"""测试夹具：每个用例独立临时库 + 标准 4 舱区 / 4 堆位 / 两波次场景。

布局（堆内 stack_seq 0=最底层，必须先装）：
  S1->H1, S2->H2, S3->H3, S4->H4，各 3 个箱 C-Sk-0..2
  相邻冲突：H1-H2, H2-H3, H3-H4
  W-1 = H1/H2 六箱（08:00-12:00），W-2 = H3/H4 六箱（13:00-17:00，与 W-1 错峰）
"""
import os
import tempfile
import unittest

from permit import service
from permit.store import connect, init_db

T = "2026-09-29T"


def t(h):
    return f"{T}{h}+08:00"


def make_fixture(db_path):
    os.environ["PERMIT_DB"] = db_path
    init_db(db_path)
    hatches = [{"code": f"H{i}", "bay_from": 10 + i * 4, "bay_to": 13 + i * 4}
               for i in range(1, 4 + 1)]
    containers = []
    for k in range(1, 5):
        for seq in range(3):
            containers.append({"id": f"C-S{k}-{seq}", "hatch_code": f"H{k}",
                               "stack_key": f"S{k}", "stack_seq": seq,
                               "tier_code": f"S{k}-T{seq}"})
    service.create_plan({"id": "D-1", "name": "测试方案", "hatches": hatches,
                         "conflicts": [["H1", "H2"], ["H2", "H3"], ["H3", "H4"]],
                         "containers": containers}, actor="tester")
    service.publish_plan("D-1", actor="tester")
    service.publish_wave("D-1", {
        "id": "W-1", "name": "波次1",
        "container_ids": [f"C-S{k}-{s}" for k in (1, 2) for s in range(3)]},
        actor="tester", idem_key="seed-w1", now=t("08:00:00"))
    service.publish_wave("D-1", {
        "id": "W-2", "name": "波次2",
        "container_ids": [f"C-S{k}-{s}" for k in (3, 4) for s in range(3)]},
        actor="tester", idem_key="seed-w2", now=t("08:00:00"))
    service.create_permit("W-1", {
        "id": "PA", "crane_id": "QC-1", "hatches": ["H1", "H2"],
        "valid_from": t("08:00:00"), "valid_to": t("12:00:00")}, now=t("08:00:00"))
    service.create_permit("W-2", {
        "id": "PB", "crane_id": "QC-2", "hatches": ["H3", "H4"],
        "valid_from": t("13:00:00"), "valid_to": t("17:00:00")}, now=t("08:00:00"))
    return db_path


class PermitCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "permit_test.db")
        make_fixture(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    # -- 便捷封装 --
    def scan(self, permit, rev, cid, now="09:00:00", key=None, scanner="HH-01",
             slot="01-01-01"):
        return service.confirm_scan(
            {"permit_id": permit, "revision": rev, "container_id": cid,
             "slot_code": slot, "scanner_id": scanner}, idem_key=key, now=t(now))

    def scan_exc(self, permit, rev, cid, now="09:00:00"):
        body, status = service.confirm_scan(
            {"permit_id": permit, "revision": rev, "container_id": cid},
            now=t(now))
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "exception")
        return body["exception_id"], body["reason"]

    def expect_error(self, status, code, fn):
        try:
            fn()
        except service.PermitError as e:
            self.assertEqual((e.status, e.code), (status, code), e.message)
            return e
        self.fail(f"expected {status} {code}, no error raised")

    def load_stack(self, permit="PA", rev=1, stack=1, start="09:00:00"):
        """按底->顶顺序装完整堆，返回三次结果。"""
        out = []
        for seq in range(3):
            hh, mm = int(start[0:2]), int(start[3:5])
            mm += seq * 10
            hh += mm // 60
            mm %= 60
            out.append(self.scan(permit, rev, f"C-S{stack}-{seq}",
                                 now=f"{hh:02d}:{mm:02d}:00")[0])
        return out

    def assert_invariants(self):
        v = service.verify(self.db)
        self.assertTrue(v["ok"], v["violations"])
        conn = connect(self.db)
        try:
            # 未执行箱唯一 active 属主（DB 级索引也兜住）
            rows = conn.execute(
                "SELECT wave_id, container_id, COUNT(*) c FROM permit_containers "
                "WHERE state='active' GROUP BY 1,2 HAVING c>1").fetchall()
            self.assertEqual(rows, [])
        finally:
            conn.close()
