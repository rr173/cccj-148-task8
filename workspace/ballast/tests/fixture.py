"""测试夹具：内存/临时文件 Executor + 示例拓扑/里程碑 + 执行辅助。"""
from __future__ import annotations

import json
import os
import tempfile

from ballast import fixture
from ballast.domain import WeightItem
from ballast.executor import Executor
from ballast.planner import Milestone
from ballast.store import connect


def fresh_executor(crash: str | None = None):
    tmp = tempfile.mkdtemp(prefix="ballast_test_")
    db = os.path.join(tmp, "ballast.db")
    ex = Executor(db, crash=crash)
    topo_payload = fixture.demo_topology()
    ex.save_topology(topo_payload, "test")
    topo = fixture.topology_from_dict(topo_payload)
    vols = fixture.initial_volumes(topo)
    ex.init_levels(vols)
    return ex, topo, vols, tmp


def make_plan(ex, vols, voyage="voy_t"):
    ms = fixture.demo_milestones()
    ex.update_loading(voyage, ms[0].cargo, ms[0].expected_loaded_n)
    plan = ex.create_plan(voyage, ms, current_volumes=vols, max_step=250)
    return plan, ms


def start_levels(ex, plan_id, seq):
    conn = connect(ex.db_path)
    try:
        step = conn.execute("SELECT * FROM steps WHERE plan_id=? AND seq=?",
                            (plan_id, seq)).fetchone()
        return ex._step_start_levels(conn, step)
    finally:
        conn.close()


def feedback_full(ex, plan_id, seq, fraction=1.0, rate=11.5,
                  token=None):
    step = next(s for s in ex.get_plan_doc(plan_id)["steps"]
                if s["seq"] == seq)
    src, dst = start_levels(ex, plan_id, seq)
    q = step["volume"] * fraction
    return ex.feedback(token or f"fb-{plan_id[-4:]}-{seq}", plan_id, seq,
                       round(src - q, 3), round(dst + q, 3), rate)


def execute_step(ex, plan_id, seq, operator="officer_a", fraction=1.0,
                 start_token=None, fb_token=None, ok_token=None):
    tag = f"{plan_id[-4:]}-{seq}-{os.urandom(3).hex()}"
    r1 = ex.command(start_token or f"st-{tag}",
                    plan_id, seq, "start", operator)
    r2 = feedback_full(ex, plan_id, seq, fraction=fraction,
                       token=fb_token or f"fb-{tag}")
    r3 = ex.confirm(ok_token or f"ok-{tag}",
                    plan_id, seq, operator)
    return r1, r2, r3


def run_plan_to_end(ex, plan_id, stop_before=None):
    doc = ex.get_plan_doc(plan_id)
    for s in doc["steps"]:
        if stop_before is not None and s["seq"] >= stop_before:
            break
        if s["status"] == "pending":
            execute_step(ex, plan_id, s["seq"])


def all_leases(ex):
    conn = connect(ex.db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM leases ORDER BY resource")]
    finally:
        conn.close()


def held_resources(ex):
    return {l["resource"] for l in all_leases(ex) if l["status"] == "held"}


def step_status(ex, plan_id, seq):
    return next(s["status"] for s in ex.get_plan_doc(plan_id)["steps"]
                if s["seq"] == seq)
