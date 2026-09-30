#!/usr/bin/env python3
"""一键灌入演示数据：

  方案 D-001 已发布，12 个箱分布在 4 个舱区、4 个堆位（每堆 3 层），
  舱区 H1-H2、H2-H3、H3-H4 相邻冲突。
  波次 W-1（H1/H2，2 堆 6 箱）与 W-2（H3/H4，2 堆 6 箱）。
  许可：
    PMT-A  岸桥 QC-1，H1+H2，有效
    PMT-B  岸桥 QC-2，H3+H4，有效
    PMT-X  岸桥 QC-3，H4（演示暂停/恢复）

用法： python3 scripts/seed_permit.py [db_path]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from permit import service  # noqa: E402
from permit.store import connect, init_db  # noqa: E402

DAY = "2026-09-29T"


def iso(h1, h2):
    return f"{DAY}{h1}+08:00", f"{DAY}{h2}+08:00"


def reset(path):
    conn = connect(path)
    for t in ("events", "idem_records", "auth_exceptions", "loading_scans",
              "permit_containers", "permit_hatches", "permits",
              "wave_containers", "waves", "plan_entries", "plans",
              "containers", "hatch_conflicts", "hatches"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name='events'")
    conn.commit()
    conn.close()


def main():
    path = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else service._db_path()
    os.environ["PERMIT_DB"] = path
    init_db(path)
    reset(path)

    hatches = [{"code": f"H{i}", "bay_from": 10 + i * 4, "bay_to": 13 + i * 4,
                "name": f"{i}号舱"} for i in range(1, 5)]
    conflicts = [["H1", "H2"], ["H2", "H3"], ["H3", "H4"]]
    containers = []
    # 4 个堆位：S1->H1, S2->H2, S3->H3, S4->H4；每堆 3 层（0 底，2 顶）
    layout = {"S1": "H1", "S2": "H2", "S3": "H3", "S4": "H4"}
    for stack, hatch in layout.items():
        for seq in range(3):
            containers.append({
                "id": f"C-{stack}-{seq}", "hatch_code": hatch,
                "stack_key": stack, "stack_seq": seq,
                "tier_code": f"{stack}-T{seq}"})

    plan, _ = service.create_plan({
        "id": "D-001", "name": "演示航次配载方案",
        "hatches": hatches, "conflicts": conflicts,
        "containers": containers}, actor="planner")
    service.publish_plan("D-001", actor="planner")

    w1, _ = service.publish_wave("D-001", {
        "id": "W-1", "name": "第一装船波次（H1/H2）",
        "container_ids": [cid for cid, h in
                          [(c["id"], c["hatch_code"]) for c in containers]
                          if h in ("H1", "H2")]},
        actor="dispatcher", now=f"{DAY}08:00:00+08:00")
    w2, _ = service.publish_wave("D-001", {
        "id": "W-2", "name": "第二装船波次（H3/H4）",
        "container_ids": [cid for cid, h in
                          [(c["id"], c["hatch_code"]) for c in containers]
                          if h in ("H3", "H4")]},
        actor="dispatcher", now=f"{DAY}08:00:00+08:00")

    v1, v2 = iso("08:00:00", "12:00:00")   # W-1 上午
    v3, v4 = iso("13:00:00", "17:00:00")   # W-2 下午（与相邻的 W-1 错峰）
    service.create_permit("W-1", {
        "id": "PMT-A", "crane_id": "QC-1", "hatches": ["H1", "H2"],
        "valid_from": v1, "valid_to": v2}, actor="dispatcher",
        now=f"{DAY}08:00:00+08:00")
    service.create_permit("W-2", {
        "id": "PMT-B", "crane_id": "QC-2", "hatches": ["H3", "H4"],
        "valid_from": v3, "valid_to": v4,
        # H4 的箱留给错峰的另一台岸桥 PMT-X
        "container_ids": ["C-S3-0", "C-S3-1", "C-S3-2"]},
        actor="dispatcher", now=f"{DAY}08:00:00+08:00")
    service.create_permit("W-2", {
        "id": "PMT-X", "crane_id": "QC-3", "hatches": ["H4"],
        "valid_from": "2026-09-29T18:00:00+08:00",
        "valid_to": "2026-09-29T20:00:00+08:00"}, actor="dispatcher",
        now=f"{DAY}08:00:00+08:00")

    print(f"seeded demo data into {path}")
    print("  plan   D-001 (published), 12 boxes, hatches H1..H4, conflicts 1-2-3-4")
    print("  waves  W-1 (H1/H2, 6) 08-12 点；W-2 (H3/H4, 6) 13-17 点（错峰）")
    print("  permit PMT-A QC-1 H1+H2 08:00-12:00")
    print("  permit PMT-B QC-2 H3+H4 13:00-17:00")
    print("  permit PMT-X QC-3 H4    18:00-20:00（暂停/恢复演练）")
    print("  冲突演练：再给 QC-4 在 09:00-10:00 授 H2（与 PMT-A 同时段）会被拒")
    print("\n演练：")
    print("  curl -X POST localhost:8070/api/scans -H 'X-Now: 2026-09-29T09:00:00+08:00' \\")
    print("       -d '{\"permit_id\":\"PMT-A\",\"revision\":1,\"container_id\":\"C-S1-2\"}'")
    print("  -> 409 PREREQ_BLOCKED（C-S1-0/1 未装）; 先扫 C-S1-0 再扫 C-S1-1, C-S1-2")


if __name__ == "__main__":
    main()
