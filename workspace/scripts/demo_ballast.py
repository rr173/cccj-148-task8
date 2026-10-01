"""压载模块一键演示：建拓扑 -> 生成方案 -> 逐步执行（反馈+确认）->
装船变化触发修订 -> 继续执行新修订。"""
import json
import os
import tempfile

from ballast import fixture
from ballast.executor import Executor, ExecError


def main():
    tmp = tempfile.mkdtemp(prefix="ballast_demo_")
    db = os.path.join(tmp, "ballast.db")
    ex = Executor(db)
    topo_payload = fixture.demo_topology()
    tid = ex.save_topology(topo_payload, "演示六舱双泵")
    topo = fixture.topology_from_dict(topo_payload)
    vols = fixture.initial_volumes(topo)
    ex.init_levels(vols)
    ms = fixture.demo_milestones()
    plan = ex.create_plan("voy_demo", ms, current_volumes=vols,
                          max_step=250)
    pid = plan["plan_id"]
    print(f"方案 {pid}（修订 1），共 {len(plan['steps'])} 步，"
          f"总水量 {plan['water_total']}")

    ex.update_loading("voy_demo", ms[0].cargo, ms[0].expected_loaded_n)

    def run_step(seq, tag, fraction=1.0):
        tok = f"t-{tag}-{seq}-start"
        r = ex.command(tok, pid, seq, "start", "officer_a")
        print(f"  start#{seq}: {r}")
        st = ex.get_plan_doc(pid)["steps"][seq - 1]
        # 模拟传感器：按目标体积读出反馈
        src0, dst0 = _start_levels(ex, pid, seq)
        q = st["volume"] * fraction
        r = ex.feedback(f"t-{tag}-{seq}-fb", pid, seq,
                        round(src0 - q, 3), round(dst0 + q, 3), 11.5)
        print(f"  feedback#{seq}: moved={r['transferred']} "
              f"within={r['within_tolerance']}")
        r = ex.confirm(f"t-{tag}-{seq}-ok", pid, seq, "officer_a")
        print(f"  confirm#{seq}: {r.get('status')} plan={r.get('plan_status')}")
    for seq in range(1, 4):
        run_step(seq, "r1")

    print("装船重量变化（临时甩箱后重载），生成修订 2 …")
    new_cargo = ms[1].cargo[:-1]  # 甩掉一箱
    ms2 = [fixture.Milestone("60%装船(修订)", new_cargo, len(new_cargo)),
           fixture.Milestone("100%装船(修订)", ms[2].cargo,
                             ms[2].expected_loaded_n)]
    ex.update_loading("voy_demo", new_cargo, len(new_cargo))
    rev = ex.revise(pid, ms2, reason="roll_container", operator_id="chief")
    print(f"新修订 {rev['plan_id']}（修订 {rev['revision_no']}），"
          f"{len(rev['steps'])} 步")
    # 旧修订启动请求应返回最新方案
    try:
        ex.command("t-old-start", pid, 4, "start", "officer_a")
        print("旧修订启动：未拒绝（异常）")
    except ExecError as e:
        print(f"旧修订启动: {e.code} latest={e.detail.get('latest_plan_id')}")
    # 重复令牌回放
    try:
        ex.command("t-old-start", pid, 4, "start", "officer_a")
    except ExecError as e:
        print(f"重复令牌回放: replay={e.detail.get('replay')}")
    print("演示完成。db =", db)


def _start_levels(ex, pid, seq):
    import sqlite3
    from ballast.store import connect
    conn = connect(ex.db_path)
    try:
        step = conn.execute("SELECT * FROM steps WHERE plan_id=? AND seq=?",
                            (pid, seq)).fetchone()
        src, dst = ex._step_start_levels(conn, step)
        return src, dst
    finally:
        conn.close()


if __name__ == "__main__":
    main()
