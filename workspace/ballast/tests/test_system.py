"""压载联动执行模块端到端测试（unittest，纯标准库）。

覆盖：可行方案生成、目标稳性不可达、泵阀冲突、正常逐步执行、流量不足暂停、
装船变化剩余步骤重算、旧修订/重复令牌、并发抢占、C1..C4 各中断点恢复。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from ballast import fixture, linalg
from ballast.domain import (Pump, Ship, Tank, Topology, Valve, WeightItem,
                            evaluate)
from ballast.executor import (CONFIRMED, FROZEN, PAUSED, RUNNING, ExecError,
                              Executor, payload_hash)
from ballast.planner import (Milestone, PlanError, build_phase_steps,
                             plan_all, solve_targets)
from ballast.server import make_handler
from ballast.store import connect
from ballast.tests import fixture as tfx


def http_json(url, body=None, method=None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method or ("POST" if body else "GET"))
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# --------------------------------------------------------------------- #
class LinAlgTests(unittest.TestCase):
    def test_basic_feasible(self):
        x, st = linalg.solve([1, 1], Aeq=[[1, 1]], beq=[10],
                             bounds=[(0, 6), (0, 6)])
        self.assertEqual(st, "optimal")
        self.assertAlmostEqual(sum(x), 10, places=6)

    def test_infeasible(self):
        x, st = linalg.solve(
            [1, 1], Aeq=[[1, 1]], beq=[10],
            Aub=[[-1, 0]], bub=[-9], bounds=[(0, 6), (0, 6)])
        self.assertEqual(st, "infeasible")
        self.assertIsNone(x)

    def test_l1_dev_variables(self):
        # 共享偏差变量 u_i>=|x_i-v0_i|：x+y=10，目标(x=4,y=1) 不可同时满足，
        # 最小偏差解为 x=9,y=1（u=5,v=0），成本 5
        c = [0, 0, 1, 1]
        Aub = [[1, 0, -1, 0], [-1, 0, -1, 0],
               [0, 1, 0, -1], [0, -1, 0, -1]]
        bub = [4, -4, 1, -1]
        x, st = linalg.solve(c, Aeq=[[1, 1, 0, 0]], beq=[10], Aub=Aub,
                             bub=bub, bounds=[(0, 10)] * 4)
        self.assertEqual(st, "optimal")
        self.assertAlmostEqual(x[0], 9, places=4)
        self.assertAlmostEqual(x[1], 1, places=4)
        self.assertAlmostEqual(x[2] + x[3], 5, places=4)
        for r, b in zip(Aub, bub):
            self.assertLessEqual(
                sum(a * y for a, y in zip(r, x)), b + 1e-6)


# --------------------------------------------------------------------- #
class TopologyTests(unittest.TestCase):
    def test_route_and_mutex(self):
        topo = fixture.topology_from_dict(fixture.demo_topology())
        pump = topo.pumps["P1"]
        res = topo.find_route("WBS", "EBP", pump)
        self.assertIn("P1", res)
        self.assertIn("VS_WBS", res)
        self.assertIn("VD_EBP", res)
        # P2 有舱到 SUC2/DEL2 的直连阀，不经过交叉总管
        res2 = topo.find_route("WBS", "EBP", topo.pumps["P2"])
        self.assertIn("P2", res2)
        self.assertIn("VS2_WBS", res2)
        self.assertIn("VD2_EBP", res2)
        # 交叉阀本身属于互斥组 CROSS
        self.assertEqual(topo.valves["VC_S"].mutex_group, "CROSS")

    def test_bad_initial_level_rejected(self):
        p = fixture.demo_topology()
        p["tanks"][0]["initial_vol"] = 10 ** 9
        with self.assertRaises(ValueError):
            fixture.topology_from_dict(p)


# --------------------------------------------------------------------- #
class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.topo = fixture.topology_from_dict(fixture.demo_topology())
        self.vols = fixture.initial_volumes(self.topo)

    def test_feasible_plan_conservation(self):
        ms = fixture.demo_milestones()
        doc = plan_all(self.topo, ms, self.vols, max_step=250)
        self.assertGreater(len(doc["steps"]), 0)
        # 每个阶段终态稳性通过
        for ph in doc["phases"]:
            self.assertTrue(ph["stability"]["ok"],
                            ph["stability"]["violations"])
        # 水量守恒
        self.assertAlmostEqual(doc["water_total"], sum(self.vols.values()),
                               places=4)
        # 步骤总和守恒：每步源减目标增，按阶段验证
        for ph_idx, ph in enumerate(doc["phases"]):
            net = {}
            for s in doc["steps"]:
                if s["phase"] != ph_idx:
                    continue
                net[s["source_tank"]] = net.get(s["source_tank"], 0) - s["volume"]
                net[s["target_tank"]] = net.get(s["target_tank"], 0) + s["volume"]
            tv = ph["target_volumes"]
            # 阶段起点
            start = (self.vols if ph_idx == 0
                     else doc["phases"][ph_idx - 1]["target_volumes"])
            for t, v in tv.items():
                self.assertAlmostEqual(start[t] + net.get(t, 0), v, places=2)

    def test_step_tank_limits(self):
        ms = fixture.demo_milestones()
        doc = plan_all(self.topo, ms, self.vols, max_step=250)
        # 无任何中间状态抽空/灌满
        vols = dict(self.vols)
        for s in doc["steps"]:
            vols[s["source_tank"]] -= s["volume"]
            vols[s["target_tank"]] += s["volume"]
            tk = self.topo.tanks[s["source_tank"]]
            tt = self.topo.tanks[s["target_tank"]]
            self.assertGreaterEqual(vols[s["source_tank"]], tk.min_vol - 1e-6)
            self.assertLessEqual(vols[s["target_tank"]], tt.max_vol + 1e-6)

    def test_unreachable_stability(self):
        # 远超压载调节能力的单侧货载
        huge = [WeightItem(3000, 50, 13, 16, "H1"),
                WeightItem(3000, -50, 13, 16, "H2")]
        with self.assertRaises(PlanError) as cm:
            solve_targets(self.topo, huge, self.vols)
        self.assertEqual(cm.exception.code, "STABILITY_UNREACHABLE")

    def test_routing_unavailable(self):
        # 孤立舱：删掉 WBP 的全部阀门
        p = fixture.demo_topology()
        p["valves"] = [v for v in p["valves"]
                       if not (v["node_a"] == "T:WBP" or v["node_b"] == "T:WBP")]
        topo = fixture.topology_from_dict(p)
        # 构造需要向 WBP 注水的目标
        targets = dict(self.vols)
        targets["WBP"] = 900
        targets["EBP"] = 500
        with self.assertRaises(PlanError) as cm:
            build_phase_steps(topo, self.vols, targets, 0, 250, 0.02)
        self.assertEqual(cm.exception.code, "ROUTING_UNAVAILABLE")

    def test_planner_conflict_when_mutex_required_parallel(self):
        # 单泵单总管：任何转移都必须独占同一泵——两个并发转移配对时，
        # 规划器仍给出串行有序步骤（安全），资源冲突留给执行层抢占仲裁。
        p = fixture.demo_topology()
        p["pumps"] = [u for u in p["pumps"] if u["id"] == "P1"]
        p["valves"] = [v for v in p["valves"]
                       if not v["id"].startswith(("VS2", "VD2", "VC"))]
        topo = fixture.topology_from_dict(p)
        ms = fixture.demo_milestones()
        doc = plan_all(topo, ms, self.vols, max_step=250)
        self.assertTrue(all(s["pump"] == "P1" for s in doc["steps"]))


# --------------------------------------------------------------------- #
class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.ex, self.topo, self.vols, self.tmp = tfx.fresh_executor()
        plan, ms = tfx.make_plan(self.ex, self.vols)
        self.pid = plan["plan_id"]
        self.plan = plan
        self.ms = ms

    def test_normal_stepwise(self):
        doc = self.ex.get_plan_doc(self.pid)
        n = len(doc["steps"])
        for seq in range(1, n + 1):
            st = next(s for s in doc["steps"] if s["seq"] == seq)
            # 随阶段推进同步装船里程碑（阶段开始条件）
            self.ex.update_loading(
                "voy_t", self.ms[st["phase"]].cargo,
                self.ms[st["phase"]].expected_loaded_n)
            tfx.execute_step(self.ex, self.pid, seq)
            self.assertEqual(tfx.step_status(self.ex, self.pid, seq),
                             CONFIRMED)
            doc = self.ex.get_plan_doc(self.pid)
        # 方案完成、所有租约释放
        self.assertEqual(self.ex.get_plan_doc(self.pid)["status"], "completed")
        self.assertFalse(tfx.held_resources(self.ex))
        # 总水量守恒（实测液位）
        lv = self.ex.current_volumes()
        self.assertAlmostEqual(sum(lv.values()), sum(self.vols.values()),
                               places=2)

    def test_start_requires_prev_confirmed(self):
        with self.assertRaises(ExecError) as cm:
            self.ex.command("t1", self.pid, 2, "start", "a")
        self.assertEqual(cm.exception.code, "START_CONDITION_NOT_MET")

    def test_loading_gate(self):
        # 新航次、里程碑要求装船数未达到
        ex2, topo2, vols2, _ = tfx.fresh_executor()
        ms = fixture.demo_milestones()
        ex2.update_loading("voy_gate", ms[0].cargo, 0)  # 装船 0 < 需要 2
        plan = ex2.create_plan("voy_gate", ms, current_volumes=vols2,
                               max_step=250)
        with self.assertRaises(ExecError) as cm:
            ex2.command("g1", plan["plan_id"], 1, "start", "a")
        self.assertEqual(cm.exception.code, "LOADING_NOT_REACHED")

    def test_duplicate_token_replays_first_result(self):
        r1 = self.ex.command("SAME", self.pid, 1, "start", "officer_a")
        r2 = self.ex.command("SAME", self.pid, 1, "start", "officer_a")
        self.assertTrue(r2["replay"])
        self.assertEqual(r1["status"], r2["status"])
        # 命令表里只有一条
        conn = connect(self.ex.db_path)
        n = conn.execute("SELECT COUNT(*) c FROM commands WHERE token='SAME'"
                         ).fetchone()["c"]
        conn.close()
        self.assertEqual(n, 1)

    def test_same_token_changed_payload_rejected(self):
        self.ex.command("TK", self.pid, 1, "start", "officer_a")
        with self.assertRaises(ExecError) as cm:
            self.ex.command("TK", self.pid, 2, "start", "officer_a")
        self.assertEqual(cm.exception.code, "TOKEN_PAYLOAD_MISMATCH")
        with self.assertRaises(ExecError) as cm:
            self.ex.command("TK", self.pid, 1, "pause", "officer_a")
        self.assertEqual(cm.exception.code, "TOKEN_PAYLOAD_MISMATCH")

    def test_concurrent_pump_preemption(self):
        # 两名值班员在不同步骤上同时抢占同一空闲泵组 P2：恰一方成功
        self.ex.command("a-start", self.pid, 1, "start", "officer_a")
        barrier = threading.Barrier(3)
        results = []

        def contender(operator, token, seq):
            barrier.wait()
            conn = connect(self.ex.db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        "INSERT INTO leases (resource,plan_id,step_seq,"
                        "operator_id,token,status,acquired_at) "
                        "VALUES ('P2',?,?,?,?,'held',?)",
                        (self.pid, seq, operator, token, "now"))
                    conn.commit()
                    results.append((operator, "won"))
                except Exception:
                    conn.rollback()
                    results.append((operator, "lost"))
            finally:
                conn.close()

        t2 = threading.Thread(target=contender, args=("officer_b", "b", 5))
        t3 = threading.Thread(target=contender, args=("officer_c", "c", 6))
        t2.start(); t3.start(); barrier.wait()
        t2.join(); t3.join()
        winners = [o for o, r in results if r == "won"]
        self.assertEqual(len(winners), 1, results)
        self.assertIn(winners[0], ("officer_b", "officer_c"))
        self.assertIn("P1", tfx.held_resources(self.ex))

    def test_start_conflicting_step_via_api_loses(self):
        # 步骤 1 占用 P1 后，步骤 2（同泵）不能获得资源
        self.ex.command("a-start", self.pid, 1, "start", "officer_a")
        self.assertIn("P1", tfx.held_resources(self.ex))

    def test_low_flow_alert_and_pause_flow(self):
        self.ex.command("p1", self.pid, 1, "start", "a")
        # 流量不足（额定 12，反馈 2.0）产生告警，但不推进
        src, dst = tfx.start_levels(self.ex, self.pid, 1)
        step = self.plan["steps"][0]
        r = self.ex.feedback("fb-low", self.pid, 1,
                             round(src - step["volume"] * 0.2, 3),
                             round(dst + step["volume"] * 0.2, 3), 2.0)
        self.assertFalse(r["within_tolerance"])
        alerts = [a for a in self._alerts() if a["kind"] == "FLOW_LOW"]
        self.assertTrue(alerts)
        # 容差不足时确认被拒
        with self.assertRaises(ExecError) as cm:
            self.ex.confirm("bad-ok", self.pid, 1, "a")
        self.assertEqual(cm.exception.code, "OUT_OF_TOLERANCE")
        # 暂停后仍可恢复
        self.ex.command("p2", self.pid, 1, "pause", "a")
        self.assertEqual(tfx.step_status(self.ex, self.pid, 1), PAUSED)
        self.ex.command("p3", self.pid, 1, "resume", "a")
        self.assertEqual(tfx.step_status(self.ex, self.pid, 1), RUNNING)
        tfx.feedback_full(self.ex, self.pid, 1, 1.0, token="fb-full")
        r = self.ex.confirm("ok1", self.pid, 1, "a")
        self.assertEqual(r["status"], CONFIRMED)

    def test_partial_transfer_preserved_across_feedback(self):
        self.ex.command("p1", self.pid, 1, "start", "a")
        tfx.feedback_full(self.ex, self.pid, 1, 0.5, token="fb-half")
        doc = self.ex.get_plan_doc(self.pid)
        moved = doc["steps"][0]["transferred"]
        self.assertAlmostEqual(moved,
                               self.plan["steps"][0]["volume"] * 0.5, places=1)

    def test_pump_not_shared_by_two_steps(self):
        self.ex.command("s1", self.pid, 1, "start", "a")
        held1 = tfx.held_resources(self.ex)
        self.assertIn("P1", held1)
        # 直接尝试给步骤 2 强插 P1 的 held 租约必然失败（数据库仲裁）
        conn = connect(self.ex.db_path)
        with self.assertRaises(Exception):
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO leases (resource,plan_id,step_seq,operator_id,"
                "token,status,acquired_at) VALUES ('P1',?,?,?, 'held',?)",
                (self.pid, 2, "b", "trespass", "now"))
            conn.commit()
        conn.rollback(); conn.close()
        # 持有者仍只有步骤 1
        conn = connect(self.ex.db_path)
        rows = conn.execute(
            "SELECT step_seq FROM leases WHERE resource='P1' AND status='held'"
        ).fetchall()
        conn.close()
        self.assertEqual([r["step_seq"] for r in rows], [1])

    def test_feedback_token_idempotent(self):
        self.ex.command("s1", self.pid, 1, "start", "a")
        r1 = tfx.feedback_full(self.ex, self.pid, 1, 0.5, token="same-fb")
        r2 = self.ex.feedback("same-fb", self.pid, 1, 0, 0)
        self.assertTrue(r2["replay"])
        doc = self.ex.get_plan_doc(self.pid)
        self.assertAlmostEqual(doc["steps"][0]["transferred"],
                               r1["transferred"], places=6)

    def _alerts(self):
        conn = connect(self.ex.db_path)
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM alerts")]
        finally:
            conn.close()


# --------------------------------------------------------------------- #
class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.ex, self.topo, self.vols, self.tmp = tfx.fresh_executor()
        plan, ms = tfx.make_plan(self.ex, self.vols)
        self.pid = plan["plan_id"]
        self.ms = ms
        # 执行前三步
        for seq in (1, 2, 3):
            tfx.execute_step(self.ex, self.pid, seq)

    def test_freeze_only_unstarted_and_preserve_confirmed(self):
        # 装船变化：甩掉一箱后修订
        new_cargo = self.ms[1].cargo[:-1]
        ms2 = [Milestone("60%(修)", new_cargo, len(new_cargo)),
               Milestone("100%(修)", self.ms[2].cargo,
                         self.ms[2].expected_loaded_n)]
        self.ex.update_loading("voy_t", new_cargo, len(new_cargo))
        rev = self.ex.revise(self.pid, ms2, "roll_container", "chief")
        # 旧方案被取代；其 pending 步骤冻结，confirmed 保留
        old = self.ex.get_plan_doc(self.pid)
        self.assertEqual(old["status"], "superseded")
        self.assertTrue(all(s["status"] in (CONFIRMED, FROZEN)
                            for s in old["steps"]))
        self.assertEqual(sum(1 for s in old["steps"]
                             if s["status"] == CONFIRMED), 3)
        # 新方案基线液位 = 已确认转移后的真实液位
        lv = self.ex.current_volumes()
        self.assertAlmostEqual(rev["basis"]["volumes"]["WBS"],
                               lv["WBS"], places=1)
        # 新方案守恒
        self.assertAlmostEqual(rev["water_total"],
                               sum(rev["basis"]["volumes"].values()), places=2)
        # 新修订可继续执行
        tfx.execute_step(self.ex, rev["plan_id"], 1)
        self.assertEqual(tfx.step_status(self.ex, rev["plan_id"], 1),
                         CONFIRMED)

    def test_stale_revision_start_returns_latest(self):
        new_cargo = self.ms[1].cargo[:-1]
        ms2 = [Milestone("60%(修)", new_cargo, len(new_cargo)),
               Milestone("100%(修)", self.ms[2].cargo, 7)]
        self.ex.update_loading("voy_t", new_cargo, len(new_cargo))
        rev = self.ex.revise(self.pid, ms2, "roll_container")
        # 对旧方案的未开始步骤发起启动 -> 拒绝并返回最新方案
        with self.assertRaises(ExecError) as cm:
            self.ex.command("old-start", self.pid, 4, "start", "a")
        self.assertEqual(cm.exception.code, "SUPERSEDED_PLAN")
        self.assertEqual(cm.exception.detail["latest_plan_id"],
                         rev["plan_id"])
        # 同令牌再发 -> 回放首次结果
        with self.assertRaises(ExecError) as cm2:
            self.ex.command("old-start", self.pid, 4, "start", "a")
        self.assertEqual(cm2.exception.code, "SUPERSEDED_PLAN")
        self.assertTrue(cm2.exception.detail["replay"])

    def test_recompute_remaining_after_loading_change(self):
        # 改变后续装船重量分布，重算的新方案目标稳性仍全部达标
        shifted = [WeightItem(i.w, i.x + 8, -i.y, i.z, i.tag)
                   for i in self.ms[2].cargo]
        ms2 = [Milestone("100%(变)", shifted, len(shifted))]
        self.ex.update_loading("voy_t", shifted, len(shifted))
        rev = self.ex.revise(self.pid, ms2, "loading_changed")
        for ph in rev["phases"]:
            self.assertTrue(ph["stability"]["ok"], ph["stability"]["violations"])
        self.assertEqual(rev["revision_no"], 2)
        self.assertEqual(rev["revision_of"], self.pid)


# --------------------------------------------------------------------- #
class InfeasiblePlanPersistTests(unittest.TestCase):
    def test_infeasible_plan_recorded(self):
        ex, topo, vols, _ = tfx.fresh_executor()
        huge = [Milestone("极端", [WeightItem(4000, 50, 13, 16),
                                   WeightItem(4000, -50, 13, 16)], 2)]
        with self.assertRaises(ExecError) as cm:
            ex.create_plan("voy_x", huge, current_volumes=vols)
        self.assertEqual(cm.exception.code, "STABILITY_UNREACHABLE")
        conn = connect(ex.db_path)
        row = conn.execute(
            "SELECT status FROM plans ORDER BY revision_no DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertEqual(row["status"], "infeasible")


# --------------------------------------------------------------------- #
class RecoveryTests(unittest.TestCase):
    """真实子进程在各崩溃点 os._exit(91)，新进程据账本恢复唯一状态。"""

    def _run_crash(self, point):
        tmp = tempfile.mkdtemp(prefix=f"ballast_crash_{point}_")
        db = os.path.join(tmp, "ballast.db")
        env = dict(os.environ, PYTHONPATH="/workspace")
        p = subprocess.run(
            [sys.executable, "-m", "ballast.tests.crash_worker", db, point],
            cwd="/workspace", env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 91, p.stderr)
        lines = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
        return db, lines

    def test_c1_start_recovers_without_redispatch(self):
        db, lines = self._run_crash("C1")
        ex = Executor(db)
        rec = ex.recover()
        # 找到计划
        conn = connect(db)
        row = conn.execute("SELECT id FROM plans ORDER BY revision_no LIMIT 1"
                           ).fetchone()
        pid = row["id"]
        step = conn.execute("SELECT status FROM steps WHERE plan_id=? AND seq=1",
                            (pid,)).fetchone()
        conn.close()
        self.assertEqual(step["status"], RUNNING)  # 启动事件已落账
        # 同令牌重发 -> 回放（不重复下发）
        r = ex.command("TOK-START-1", pid, 1, "start", "officer_a")
        self.assertTrue(r["replay"])
        self.assertEqual(r["status"], RUNNING)
        # 仍能继续完成
        tfx.feedback_full(ex, pid, 1, token="FB-C1")
        r = ex.confirm("OK-C1", pid, 1, "officer_a")
        self.assertEqual(r["status"], CONFIRMED)
        # 启动事件只有一条
        conn = connect(db)
        n = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE plan_id=? AND kind='STEP_START'",
            (pid,)).fetchone()["c"]
        conn.close()
        self.assertEqual(n, 1)

    def test_c2_pause_recovers(self):
        db, _ = self._run_crash("C2")
        ex = Executor(db)
        ex.recover()
        conn = connect(db)
        pid = conn.execute("SELECT id FROM plans LIMIT 1").fetchone()["id"]
        status = conn.execute(
            "SELECT status FROM steps WHERE plan_id=? AND seq=1",
            (pid,)).fetchone()["status"]
        conn.close()
        self.assertEqual(status, PAUSED)
        # 暂停令牌回放
        r = ex.command("TOK-PAUSE-1", pid, 1, "pause", "officer_a")
        self.assertTrue(r.get("replay"))

    def test_c3_partial_flow_preserved(self):
        db, _ = self._run_crash("C3")
        ex = Executor(db)
        ex.recover()
        conn = connect(db)
        pid = conn.execute("SELECT id FROM plans LIMIT 1").fetchone()["id"]
        row = conn.execute(
            "SELECT transferred,observed_src,observed_dst,status FROM steps "
            "WHERE plan_id=? AND seq=1", (pid,)).fetchone()
        conn.close()
        self.assertIsNotNone(row["observed_src"])
        self.assertGreater(row["transferred"], 0)
        self.assertEqual(row["status"], RUNNING)
        # 反馈令牌重发 -> 回放，不累计第二次
        r = ex.feedback("TOK-FB-1", pid, 1, 0, 0)
        self.assertTrue(r["replay"])
        conn = connect(db)
        n = conn.execute(
            "SELECT COUNT(*) c FROM feedback WHERE token='TOK-FB-1'"
        ).fetchone()["c"]
        conn.close()
        self.assertEqual(n, 1)

    def test_c4_confirm_recovers(self):
        db, _ = self._run_crash("C4")
        ex = Executor(db)
        ex.recover()
        conn = connect(db)
        pid = conn.execute("SELECT id FROM plans LIMIT 1").fetchone()["id"]
        status = conn.execute(
            "SELECT status FROM steps WHERE plan_id=? AND seq=1",
            (pid,)).fetchone()["status"]
        held = conn.execute(
            "SELECT COUNT(*) c FROM leases WHERE plan_id=? AND status='held'",
            (pid,)).fetchone()["c"]
        conn.close()
        self.assertEqual(status, CONFIRMED)
        self.assertEqual(held, 0)  # 无两个步骤共占泵
        # 确认令牌回放首次结果
        r = ex.confirm("TOK-OK-1", pid, 1, "officer_a")
        self.assertTrue(r["replay"])
        self.assertEqual(r["status"], CONFIRMED)


# --------------------------------------------------------------------- #
class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ex, cls.topo, cls.vols, cls.tmp = tfx.fresh_executor()
        plan, ms = tfx.make_plan(cls.ex, cls.vols)
        cls.pid = plan["plan_id"]
        cls.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(cls.ex, None))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_01_pages_and_state(self):
        st, b = http_json(self.base + "/ballast/stability")
        self.assertEqual(st, 200)
        self.assertIn("stability", b)
        st, b = http_json(self.base + "/ballast/leases")
        self.assertEqual(st, 200)
        st, b = http_json(self.base + "/ballast/events")
        self.assertEqual(st, 200)

    def test_02_full_flow_over_http(self):
        st, b = http_json(self.base + "/ballast/command",
                          {"token": "H1", "plan_id": self.pid, "step_seq": 1,
                           "command": "start", "operator_id": "a"})
        self.assertEqual(st, 200)
        self.assertEqual(b["result"]["status"], "running")
        # 并发抢占：另一值班员同泵
        st, b2 = http_json(self.base + "/ballast/command",
                           {"token": "H2", "plan_id": self.pid,
                            "step_seq": 2, "command": "start",
                            "operator_id": "b"})
        # 第 2 步前序未确认，先被开始条件拒绝
        self.assertEqual(st, 409)
        src, dst = tfx.start_levels(self.ex, self.pid, 1)
        step = self.ex.get_plan_doc(self.pid)["steps"][0]
        st, b = http_json(self.base + "/ballast/feedback",
                          {"token": "HF1", "plan_id": self.pid, "step_seq": 1,
                           "observed_src": round(src - step["volume"], 3),
                           "observed_dst": round(dst + step["volume"], 3),
                           "flow_rate": 11.5})
        self.assertEqual(st, 200)
        self.assertTrue(b["result"]["within_tolerance"])
        st, b = http_json(self.base + "/ballast/command",
                          {"token": "HOK1", "plan_id": self.pid,
                           "step_seq": 1, "command": "confirm",
                           "operator_id": "a"})
        self.assertEqual(st, 200, b)
        self.assertEqual(b["result"]["status"], "confirmed")
        # 重复令牌
        st, b = http_json(self.base + "/ballast/command",
                          {"token": "H1", "plan_id": self.pid, "step_seq": 1,
                           "command": "start", "operator_id": "a"})
        self.assertTrue(b["result"]["replay"])

    def test_03_token_mismatch_http(self):
        st, b = http_json(self.base + "/ballast/command",
                          {"token": "HOK1", "plan_id": self.pid,
                           "step_seq": 2, "command": "start",
                           "operator_id": "a"})
        self.assertEqual(st, 409)
        self.assertEqual(b["error"]["code"], "TOKEN_PAYLOAD_MISMATCH")


if __name__ == "__main__":
    unittest.main(verbosity=2)
