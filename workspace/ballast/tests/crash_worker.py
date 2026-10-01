"""崩溃注入工作进程：在指定点 os._exit(91) 模拟掉电。

用法：
  python3 -m ballast.tests.crash_worker <db_path> <point:C1|C2|C3|C4>

序列（固定令牌，便于重启后用同一令牌验证幂等回放）：
  C1 启动步骤 1（提交后崩溃）
  C2 启动步骤 1 -> 暂停（暂停提交后崩溃）
  C3 启动步骤 1 -> 一次足量反馈（反馈提交后崩溃）
  C4 启动步骤 1 -> 足量反馈 -> 确认（确认提交后崩溃）
"""
import json
import os
import sys

from ballast import fixture
from ballast.executor import Executor
from ballast.tests import fixture as tfx


def main():
    db, point = sys.argv[1], sys.argv[2]
    ex = Executor(db, crash=point)
    topo_payload = fixture.demo_topology()
    ex.save_topology(topo_payload, "crash")
    topo = fixture.topology_from_dict(topo_payload)
    vols = fixture.initial_volumes(topo)
    ex.init_levels(vols)
    ms = fixture.demo_milestones()
    ex.update_loading("voy_crash", ms[0].cargo, ms[0].expected_loaded_n)
    plan = ex.create_plan("voy_crash", ms, current_volumes=vols, max_step=250)
    pid = plan["plan_id"]

    print(json.dumps({"stage": "plan_ready", "plan_id": pid}), flush=True)

    ex.command("TOK-START-1", pid, 1, "start", "officer_a")
    print(json.dumps({"stage": "started"}), flush=True)
    if point == "C1":
        print(json.dumps({"stage": "should_have_crashed"}), flush=True)
        sys.exit(2)

    if point == "C2":
        ex.command("TOK-PAUSE-1", pid, 1, "pause", "officer_a")
        print(json.dumps({"stage": "should_have_crashed"}), flush=True)
        sys.exit(2)

    src, dst = tfx.start_levels(ex, pid, 1)
    step = plan["steps"][0]
    ex.feedback("TOK-FB-1", pid, 1, round(src - step["volume"], 3),
                round(dst + step["volume"], 3), 11.5)
    print(json.dumps({"stage": "feedback_done"}), flush=True)
    if point == "C3":
        print(json.dumps({"stage": "should_have_crashed"}), flush=True)
        sys.exit(2)

    ex.confirm("TOK-OK-1", pid, 1, "officer_a")
    print(json.dumps({"stage": "should_have_crashed"}), flush=True)
    sys.exit(2)


if __name__ == "__main__":
    main()
