"""崩溃注入工作进程：在指定事务提交前 os._exit(91) 模拟掉电。

用法：
  python3 -m permit.tests.crash_worker <db_path> <point:C1|C2|C3|C4>

  C1  保存许可（含舱区/箱集合）提交前
  C2  许可转交、迁移未执行箱提交前
  C3  登记扫描结果 + 推进波次进度提交前
  C4  装船记录已落库、波次进度尚未推进（两阶段撕裂；PERMIT_CRASH_SCAN_SPLIT）
"""
import os
import sys

# 必须在 import permit 之前设置库路径
DB = sys.argv[1]
POINT = sys.argv[2]
os.environ["PERMIT_DB"] = DB

from permit import service  # noqa: E402
from permit.tests.fixture import make_fixture, t  # noqa: E402

make_fixture(DB)

if POINT == "C1":
    service.create_permit("W-1", {
        "crane_id": "QC-7", "hatches": ["H1"],
        "valid_from": t("12:30:00"), "valid_to": t("13:00:00"),
        "container_ids": []}, now=t("08:00:00"))
elif POINT == "C2":
    service.create_permit("W-1", {
        "id": "PC", "crane_id": "QC-3", "hatches": ["H1", "H2"],
        "valid_from": t("12:01:00"), "valid_to": t("13:00:00"),
        "container_ids": []}, now=t("08:00:00"))
    service.transfer_permit(
        "PA", {"target_permit_id": "PC", "expected_source_revision": 1},
        idem_key="tr-crash", now=t("12:10:00"))
elif POINT == "C3":
    service.confirm_scan(
        {"permit_id": "PA", "revision": 1, "container_id": "C-S1-0",
         "scanner_id": "HH-C"}, idem_key="scan-crash", now=t("09:00:00"))
elif POINT == "C4":
    os.environ["PERMIT_CRASH_SCAN_SPLIT"] = "1"
    os.environ["PERMIT_CRASH"] = "C4"
    service.confirm_scan(
        {"permit_id": "PA", "revision": 1, "container_id": "C-S1-0",
         "scanner_id": "HH-C"}, now=t("09:00:00"))
else:
    print(f"unknown point {POINT}")
    sys.exit(2)

print("worker survived (crash did not fire?)")
sys.exit(0)
