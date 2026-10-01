"""压载水联动执行模块端到端测试。

覆盖：可行方案生成、目标稳性不可达、泵阀冲突、正常逐步执行、流量不足
暂停、装船变化后剩余步骤重算、旧修订与重复令牌、并发抢占、各中断点恢复。
"""
import json
import os
import tempfile
import threading
import unittest

os.environ.setdefault("STOWAGE_DB", tempfile.mktemp(suffix=".db"))

from stowage import service as stow              # noqa: E402
from stowage.db import ServiceError              # noqa: E402

from ballast import service                      # noqa: E402
from ballast.db import init_db                   # noqa: E402


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

def vessel_payload(containers=None):
    return {
        "voyage": {"name": "BAL-T", "port_sequence": ["SHA", "HAM"]},
        "ship": {"lightship_weight": 9000, "lightship_lcg": 30.5,
                 "lightship_vcg": 8.5, "min_gm": 0.5, "max_gm": 6.0,
                 "max_trim": 1.0},
        "stacks": [
            {"bay": 2, "row": 1, "max_tiers": 3, "max_weight": 500},
            {"bay": 2, "row": 2, "max_tiers": 3, "max_weight": 500},
            {"bay": 4, "row": 1, "max_tiers": 3, "max_weight": 500},
            {"bay": 4, "row": 2, "max_tiers": 3, "max_weight": 500},
        ],
        "containers": containers if containers is not None else [
            {"id": "C1", "pod": "HAM", "weight": 20},
            {"id": "C2", "pod": "HAM", "weight": 20},
            {"id": "C3", "pod": "HAM", "weight": 20},
            {"id": "C4", "pod": "HAM", "weight": 20},
        ],
    }


def topo_payload(heel_imbalance=True, with_pipes=True, config=None):
    """W1S 多 130m³ → 初始右倾 ~1.7°（限值 1.0°），必须生成纠正步骤。"""
    payload = {
        "tanks": [
            {"name": "W1P", "capacity": 200, "min_level": 5, "max_level": 190,
             "level": 20 if heel_imbalance else 85, "x": 30, "y": -10, "z": 4},
            {"name": "W1S", "capacity": 200, "min_level": 5, "max_level": 190,
             "level": 150 if heel_imbalance else 85, "x": 30, "y": 10, "z": 4},
            {"name": "FPT", "capacity": 200, "min_level": 5, "max_level": 190,
             "level": 100, "x": 55, "y": 0, "z": 2},
            {"name": "APT", "capacity": 200, "min_level": 5, "max_level": 190,
             "level": 100, "x": 5, "y": 0, "z": 2},
        ],
        "pumps": [{"name": "P1", "rate": 120}, {"name": "P2", "rate": 120}],
        "valves": [{"name": "V_W1P"}, {"name": "V_W1S"},
                   {"name": "V_FPT"}, {"name": "V_APT"}],
        "pipes": [],
        "config": config or {"max_heel_deg": 1.0, "max_draft": 12.0,
                             "min_draft": 1.0, "max_bm": 50.0, "tpc": 25.0,
                             "tolerance": 1.0, "drift_tol": 0.5,
                             "low_flow_ratio": 0.3},
    }
    if with_pipes:
        payload["pipes"] = [
            {"name": "suc_w1p", "a": "W1P", "b": "V_W1P"},
            {"name": "suc_w1s", "a": "W1S", "b": "V_W1S"},
            {"name": "suc_fpt", "a": "FPT", "b": "V_FPT"},
            {"name": "suc_apt", "a": "APT", "b": "V_APT"},
            {"name": "w1p_p1", "a": "V_W1P", "b": "P1"},
            {"name": "w1s_p1", "a": "V_W1S", "b": "P1"},
            {"name": "fpt_p2", "a": "V_FPT", "b": "P2"},
            {"name": "apt_p2", "a": "V_APT", "b": "P2"},
            {"name": "w1p_p2", "a": "V_W1P", "b": "P2"},
            {"name": "w1s_p2", "a": "V_W1S", "b": "P2"},
            {"name": "fpt_p1", "a": "V_FPT", "b": "P1"},
            {"name": "apt_p1", "a": "V_APT", "b": "P1"},
        ]
    return payload


class Base(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        os.environ["STOWAGE_DB"] = self.db
        init_db(self.db)
        service.clear_crash_points()
        self.vid = stow.import_voyage(vessel_payload(), db_path=self.db)[
            "voyage_id"]

    def tearDown(self):
        service.clear_crash_points()

    # -- stowage helpers ---------------------------------------------------- #
    def publish_stowage(self):
        job = stow.create_solve_job(self.vid, run_async=False, db_path=self.db)
        j = stow.get_job(job, db_path=self.db)
        assert j["status"] == "done", j
        plan_id = j["plan_id"]
        token = stow.request_approval(plan_id, db_path=self.db)["token"]
        stow.publish_plan(plan_id, token=token, db_path=self.db)
        return plan_id

    def confirm_all_cargo(self):
        """按已发布方案把全部箱确认装船（推动装船进度/阶段门）。"""
        plan = self.publish_stowage()
        detail = stow.plan_detail(plan, db_path=self.db)
        for e in detail["entries"]:
            stow.confirm_load(self.vid, plan, e["container_id"],
                              e["slot_code"], db_path=self.db)
        return plan

    # -- ballast helpers ---------------------------------------------------- #
    def set_topology(self, **kw):
        return service.save_topology(self.vid, topo_payload(**kw),
                                     db_path=self.db)

    def gen_plan(self, **kw):
        return service.generate_plan(self.vid, db_path=self.db, **kw)

    def levels(self):
        return {t["name"]: t["level"]
                for t in service.get_topology(self.vid, db_path=self.db)[
                    "tanks"]}

    def run_step(self, step, flow=120.0):
        """启动一步 → 反馈到位 → 确认。返回确认结果。"""
        plan = service.get_plan(self.vid, db_path=self.db)
        rev = plan["revision"]
        r = service.start_step(self.vid, token=f"tok-start-{step['seq']}",
                               seq=step["seq"], revision=rev, db_path=self.db)
        self.assertTrue(r.get("dispatched"), r)
        lv = self.levels()
        lv[step["source_tank"]] -= step["target_volume"]
        lv[step["target_tank"]] += step["target_volume"]
        service.feedback(self.vid, levels=lv,
                         flows={step["pump_id"]: flow}, db_path=self.db)
        return service.confirm_step(self.vid, token=f"tok-conf-{step['seq']}",
                                    seq=step["seq"], db_path=self.db)


# --------------------------------------------------------------------------- #
# 1. 可行方案生成
# --------------------------------------------------------------------------- #

class TestFeasiblePlan(Base):
    def test_plan_generation_and_invariants(self):
        self.publish_stowage()
        self.set_topology()
        # 初始状态确实横倾超限（否则不需要纠正步骤）
        stab = service.get_stability(self.vid, db_path=self.db)
        self.assertTrue(any(v["code"] == "HEEL"
                            for v in stab["violations"]), stab)

        plan = self.gen_plan(stages=3)
        steps = plan["steps"]
        self.assertTrue(steps, "应生成纠正横倾的步骤")
        total0 = plan["total_ballast"]

        # 每步字段齐全：源舱/目标舱/目标体积/允许误差/泵阀/开始条件
        for s in steps:
            for key in ("source_tank", "target_tank", "target_volume",
                        "tolerance", "pump_id", "valves", "pipes",
                        "start_condition"):
                self.assertIn(key, s, s)
            self.assertGreater(s["target_volume"], 0)
            self.assertGreater(s["tolerance"], 0)

        # 逐步仿真：水量守恒、不抽空、不越安全上限、终态稳性达标
        topo = service.get_topology(self.vid, db_path=self.db)
        bounds = {t["name"]: (t["min_level"], t["max_level"])
                  for t in topo["tanks"]}
        lv = {t["name"]: t["level"] for t in topo["tanks"]}
        for s in steps:
            lv[s["source_tank"]] -= s["target_volume"]
            lv[s["target_tank"]] += s["target_volume"]
            for name, v in lv.items():
                lo, hi = bounds[name]
                self.assertGreaterEqual(v, lo - 1e-6, (s, name, v))
                self.assertLessEqual(v, hi + 1e-6, (s, name, v))
            self.assertAlmostEqual(sum(lv.values()), total0, places=6)
        # 终态（全部货载上船）稳性必须达标
        self.confirm_all_cargo()
        for name, v in lv.items():
            service.feedback(self.vid, levels={name: v}, db_path=self.db)
        stab = service.get_stability(self.vid, db_path=self.db)
        self.assertEqual(stab["violations"], [], stab)

    def test_plan_requires_topology(self):
        with self.assertRaises(ServiceError) as ctx:
            self.gen_plan()
        self.assertEqual(ctx.exception.code, "NO_TOPOLOGY")

    def test_two_voyages_same_tank_names(self):
        """同一库内多个航次可以使用相同的舱/泵/阀/管命名。"""
        self.set_topology()
        vid2 = stow.import_voyage(
            vessel_payload(containers=[{"id": "Z1", "pod": "HAM",
                                        "weight": 20}]),
            db_path=self.db)["voyage_id"]
        service.save_topology(vid2, topo_payload(), db_path=self.db)
        t1 = service.get_topology(self.vid, db_path=self.db)
        t2 = service.get_topology(vid2, db_path=self.db)
        self.assertEqual(len(t1["tanks"]), len(t2["tanks"]))
        # 液位互不影响
        service.feedback(vid2, levels={"W1P": 50}, db_path=self.db)
        self.assertNotEqual(
            {t["name"]: t["level"] for t in
             service.get_topology(vid2, db_path=self.db)["tanks"]}["W1P"],
            {t["name"]: t["level"] for t in
             service.get_topology(self.vid, db_path=self.db)["tanks"]}["W1P"])


# --------------------------------------------------------------------------- #
# 2. 目标稳性不可达
# --------------------------------------------------------------------------- #

class TestInfeasible(Base):
    def test_unreachable_stability(self):
        self.publish_stowage()
        cfg = {"max_heel_deg": 1.0, "max_draft": 1.0,  # 吃水限值不可能达到
               "min_draft": 0.5, "max_bm": 50.0, "tpc": 25.0,
               "tolerance": 1.0, "drift_tol": 0.5, "low_flow_ratio": 0.3}
        self.set_topology(config=cfg)
        with self.assertRaises(ServiceError) as ctx:
            self.gen_plan()
        self.assertEqual(ctx.exception.code, "STABILITY_UNREACHABLE")
        self.assertTrue(ctx.exception.extra["violations"])
        # 失败已写账本，且没有留下任何修订
        self.assertEqual(service.get_plan(self.vid, db_path=self.db)[
                             "revision"], None)
        events = service.list_events(self.vid, db_path=self.db)["events"]
        self.assertTrue(any(e["action"] == "PLAN_INFEASIBLE"
                            for e in events))


# --------------------------------------------------------------------------- #
# 3. 泵阀冲突 / 连通缺失
# --------------------------------------------------------------------------- #

class TestPumpValveConflict(Base):
    def test_no_path_between_tanks(self):
        self.publish_stowage()
        self.set_topology(with_pipes=False)  # 有舱有泵无管路
        with self.assertRaises(ServiceError) as ctx:
            self.gen_plan()
        self.assertEqual(ctx.exception.code, "NO_PATH")

    def test_path_requires_exactly_one_pump(self):
        self.publish_stowage()
        topo = topo_payload()
        topo["pipes"] = [  # W1S 与 W1P 之间只隔着阀门，没有泵
            {"name": "suc_w1s", "a": "W1S", "b": "V_W1S"},
            {"name": "suc_w1p", "a": "W1P", "b": "V_W1P"},
            {"name": "direct", "a": "V_W1S", "b": "V_W1P"},
        ]
        service.save_topology(self.vid, topo, db_path=self.db)
        with self.assertRaises(ServiceError) as ctx:
            self.gen_plan()
        self.assertEqual(ctx.exception.code, "NO_PATH")

    def test_cannot_start_two_steps_at_once(self):
        self.publish_stowage()
        self.set_topology()
        plan = self.gen_plan()
        first, second = plan["steps"][0], plan["steps"][1] \
            if len(plan["steps"]) > 1 else None
        service.start_step(self.vid, token="t1", seq=first["seq"],
                           revision=plan["revision"], db_path=self.db)
        # 运行中再启动任何一步都被拒（串行纪律 + 泵独占）
        if second:
            with self.assertRaises(ServiceError):
                service.start_step(self.vid, token="t2", seq=second["seq"],
                                   revision=plan["revision"], db_path=self.db)


# --------------------------------------------------------------------------- #
# 4. 正常逐步执行
# --------------------------------------------------------------------------- #

class TestExecution(Base):
    def test_step_by_step_execution(self):
        plan_stow = self.confirm_all_cargo()  # 先装完，阶段门自然满足
        self.set_topology()
        plan = self.gen_plan()
        self.assertTrue(plan["steps"])
        total0 = plan["total_ballast"]

        # 顺序约束：第 2 步不能先于第 1 步确认前启动
        if len(plan["steps"]) > 1:
            with self.assertRaises(ServiceError) as ctx:
                service.start_step(self.vid, token="jump",
                                   seq=plan["steps"][1]["seq"],
                                   revision=plan["revision"], db_path=self.db)
            self.assertEqual(ctx.exception.code, "PREVIOUS_NOT_CONFIRMED")

        for step in service.get_plan(self.vid, db_path=self.db)["steps"]:
            # 运行中泵组被占用
            r = service.start_step(self.vid, token=f"s{step['seq']}",
                                   seq=step["seq"], revision=plan["revision"],
                                   db_path=self.db)
            res = service.get_resources(self.vid, db_path=self.db)
            pump = next(p for p in res["pumps"]
                        if p["pump_id"] == step["pump_id"])
            self.assertTrue(pump["occupied"])
            # 反馈到位并确认
            lv = self.levels()
            lv[step["source_tank"]] -= step["target_volume"]
            lv[step["target_tank"]] += step["target_volume"]
            service.feedback(self.vid, levels=lv,
                             flows={step["pump_id"]: 120}, db_path=self.db)
            r = service.confirm_step(self.vid, token=f"c{step['seq']}",
                                     seq=step["seq"], db_path=self.db)
            self.assertTrue(r["confirmed"], r)
            # 确认后泵组释放
            pump = next(p for p in service.get_resources(
                self.vid, db_path=self.db)["pumps"]
                if p["pump_id"] == step["pump_id"])
            self.assertFalse(pump["occupied"])
            # 全程水量守恒
            self.assertAlmostEqual(sum(self.levels().values()), total0,
                                   places=6)

        done = service.get_plan(self.vid, db_path=self.db)
        self.assertTrue(all(s["status"] == "confirmed"
                            for s in done["steps"]))
        stab = service.get_stability(self.vid, db_path=self.db)
        self.assertEqual(stab["violations"], [], stab)

    def test_confirm_requires_tolerance(self):
        self.confirm_all_cargo()
        self.set_topology()
        plan = self.gen_plan()
        step = plan["steps"][0]
        service.start_step(self.vid, token="s1", seq=step["seq"],
                           revision=plan["revision"], db_path=self.db)
        # 只转移了一小半 → 未达容差，不能确认
        lv = self.levels()
        part = step["target_volume"] * 0.4
        lv[step["source_tank"]] -= part
        lv[step["target_tank"]] += part
        service.feedback(self.vid, levels=lv, flows={step["pump_id"]: 120},
                         db_path=self.db)
        with self.assertRaises(ServiceError) as ctx:
            service.confirm_step(self.vid, token="c1", seq=step["seq"],
                                 db_path=self.db)
        self.assertEqual(ctx.exception.code, "TARGET_NOT_REACHED")


# --------------------------------------------------------------------------- #
# 5. 流量不足暂停
# --------------------------------------------------------------------------- #

class TestLowFlowPause(Base):
    def test_low_flow_auto_pause_then_resume(self):
        self.confirm_all_cargo()
        self.set_topology()
        plan = self.gen_plan()
        step = plan["steps"][0]
        service.start_step(self.vid, token="s1", seq=step["seq"],
                           revision=plan["revision"], db_path=self.db)
        # 流量不足（< 0.3*120=36）→ 自动暂停 + 告警 + 泵释放
        lv = self.levels()
        lv[step["source_tank"]] -= step["target_volume"] * 0.3
        lv[step["target_tank"]] += step["target_volume"] * 0.3
        r = service.feedback(self.vid, levels=lv,
                             flows={step["pump_id"]: 10}, db_path=self.db)
        self.assertTrue(r["paused"])
        alarms = service.list_alarms(self.vid, db_path=self.db)["alarms"]
        self.assertTrue(any(a["kind"] == "FLOW_LOW" for a in alarms))
        res = service.get_resources(self.vid, db_path=self.db)
        self.assertFalse(any(p["occupied"] for p in res["pumps"]))
        # 暂停中不能确认（量未达）
        with self.assertRaises(ServiceError):
            service.confirm_step(self.vid, token="c1", seq=step["seq"],
                                 db_path=self.db)
        # 流量恢复 → 继续 → 补量到位 → 确认
        service.resume_step(self.vid, token="r1", db_path=self.db)
        lv = self.levels()
        start = service.get_plan(self.vid, db_path=self.db)["steps"][0]
        lv[step["source_tank"]] = round(
            lv[step["source_tank"]] - step["target_volume"] * 0.7, 3)
        lv[step["target_tank"]] = round(
            lv[step["target_tank"]] + step["target_volume"] * 0.7, 3)
        service.feedback(self.vid, levels=lv, flows={step["pump_id"]: 120},
                         db_path=self.db)
        r = service.confirm_step(self.vid, token="c2", seq=step["seq"],
                                 db_path=self.db)
        self.assertTrue(r["confirmed"], r)
        self.assertAlmostEqual(r["transferred"], step["target_volume"],
                               delta=step["tolerance"])


# --------------------------------------------------------------------------- #
# 6. 装船变化 / 漂移 → 冻结未开始步骤并重算
# --------------------------------------------------------------------------- #

class TestReplan(Base):
    def test_loading_change_replans_remaining(self):
        # 较重的货载（4×200t）让方案除初始纠正外还有随装船阶段的调水步骤
        heavy = [{"id": f"H{i}", "pod": "HAM", "weight": 200}
                 for i in range(1, 5)]
        self.vid = stow.import_voyage(vessel_payload(containers=heavy),
                                      db_path=self.db)["voyage_id"]
        self.publish_stowage()
        self.set_topology()
        plan = self.gen_plan()
        self.assertGreaterEqual(len(plan["steps"]), 2, plan["steps"])
        rev1 = plan["revision"]
        # 先执行并确认第 1 步（已确认转移量必须保留）
        step1 = plan["steps"][0]
        self.run_step(step1)
        confirmed_lv = self.levels()

        # 装船重量剧变：全部待装箱晚到箱重 200t → 400t，剩余方案不再安全
        for cid in ("H1", "H2", "H3", "H4"):
            stow.late_weight(self.vid, cid, 400, db_path=self.db)
        r = service.recheck(self.vid, reason="loading_change", db_path=self.db)
        self.assertTrue(r["replanned"], r)
        self.assertGreater(r["revision"], rev1)

        revs = service.list_revisions(self.vid, db_path=self.db)["revisions"]
        old = next(x for x in revs if x["revision"] == rev1)
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["steps_by_status"].get("confirmed"), 1)
        self.assertGreater(old["confirmed_volume"], 0)
        self.assertGreaterEqual(old["steps_by_status"].get("frozen", 0), 1)
        new = next(x for x in revs if x["revision"] == r["revision"])
        self.assertEqual(new["status"], "active")

        # 新修订以当前实际液位为基线：已转移水量不丢、总量守恒
        self.assertEqual(self.levels(), confirmed_lv)
        self.assertAlmostEqual(r["total_ballast"], sum(confirmed_lv.values()),
                               places=6)
        # 新方案自身可行：仿真到底不越界且终态达标
        lv = dict(self.levels())
        topo = service.get_topology(self.vid, db_path=self.db)
        bounds = {t["name"]: (t["min_level"], t["max_level"])
                  for t in topo["tanks"]}
        for s in r["steps"]:
            lv[s["source_tank"]] -= s["target_volume"]
            lv[s["target_tank"]] += s["target_volume"]
            for name, v in lv.items():
                lo, hi = bounds[name]
                self.assertGreaterEqual(v, lo - 1e-6, (s, name))
                self.assertLessEqual(v, hi + 1e-6, (s, name))
        self.assertAlmostEqual(sum(lv.values()), sum(confirmed_lv.values()),
                               places=6)

    def test_safe_loading_change_keeps_plan(self):
        self.publish_stowage()
        self.set_topology()
        plan = self.gen_plan()
        stow.roll_container(self.vid, "C4", db_path=self.db)  # 甩掉一只 20t
        r = service.recheck(self.vid, reason="loading_change", db_path=self.db)
        self.assertFalse(r["replanned"], r)
        self.assertEqual(r["revision"], plan["revision"])

    def test_drift_triggers_replan(self):
        self.confirm_all_cargo()
        self.set_topology()
        plan = self.gen_plan()
        # 无步骤运行时源舱液位异常流失 → 剩余步骤会抽空 → 冻结重算
        lv = self.levels()
        src = max(plan["steps"], key=lambda s: s["target_volume"])[
            "source_tank"]
        lv[src] = 6.0
        r = service.feedback(self.vid, levels=lv, db_path=self.db)
        self.assertIsNotNone(r["replanned"], r)
        alarms = service.list_alarms(self.vid, db_path=self.db)["alarms"]
        self.assertTrue(any(a["kind"] == "DRIFT" for a in alarms))
        revs = service.list_revisions(self.vid, db_path=self.db)["revisions"]
        self.assertEqual(len(revs), 2)


# --------------------------------------------------------------------------- #
# 7. 旧修订与重复令牌
# --------------------------------------------------------------------------- #

class TestTokensAndRevisions(Base):
    def test_stale_revision_returns_latest_plan(self):
        self.publish_stowage()
        self.set_topology()
        p1 = self.gen_plan()
        for cid in ("C1", "C2", "C3", "C4"):
            stow.late_weight(self.vid, cid, 400, db_path=self.db)
        p2 = service.recheck(self.vid, db_path=self.db)
        self.assertTrue(p2["replanned"])
        r = service.start_step(self.vid, token="old-rev", seq=1,
                               revision=p1["revision"], db_path=self.db)
        self.assertTrue(r["revision_moved"])
        self.assertEqual(r["current_revision"], p2["revision"])
        self.assertTrue(r["steps"])

    def test_same_token_replays_first_result(self):
        self.confirm_all_cargo()
        self.set_topology()
        plan = self.gen_plan()
        step = plan["steps"][0]
        r1 = service.start_step(self.vid, token="tok-1", seq=step["seq"],
                                revision=plan["revision"], db_path=self.db)
        r2 = service.start_step(self.vid, token="tok-1", seq=step["seq"],
                                revision=plan["revision"], db_path=self.db)
        self.assertTrue(r2["idempotent"])
        self.assertEqual(r1, {k: v for k, v in r2.items()
                              if k != "idempotent"})
        # 只下发了一次：账本里 STEP_STARTED 只有一条
        events = service.list_events(self.vid, db_path=self.db)["events"]
        self.assertEqual(len([e for e in events
                              if e["action"] == "STEP_STARTED"]), 1)

    def test_token_payload_change_rejected(self):
        self.confirm_all_cargo()
        self.set_topology()
        plan = self.gen_plan()
        service.start_step(self.vid, token="tok-x", seq=1,
                           revision=plan["revision"], db_path=self.db)
        with self.assertRaises(ServiceError) as ctx:
            service.start_step(self.vid, token="tok-x", seq=2,
                               revision=plan["revision"], db_path=self.db)
        self.assertEqual(ctx.exception.code, "TOKEN_PAYLOAD_MISMATCH")


# --------------------------------------------------------------------------- #
# 8. 并发抢占
# --------------------------------------------------------------------------- #

class TestConcurrency(Base):
    def test_two_officers_one_pump(self):
        self.set_topology()
        results, errors = [], []

        def seize(officer, token):
            try:
                results.append(service.seize_pump(
                    self.vid, token=token, pump_id="P1", officer=officer,
                    db_path=self.db))
            except ServiceError as e:
                errors.append(e)

        threads = [threading.Thread(target=seize,
                                    args=(f"officer-{i}", f"tok-{i}"))
                   for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1, (results, errors))
        self.assertEqual(len(errors), 5)
        self.assertTrue(all(e.code == "PUMP_BUSY" for e in errors))
        # 占用者唯一
        res = service.get_resources(self.vid, db_path=self.db)
        p1 = next(p for p in res["pumps"] if p["pump_id"] == "P1")
        self.assertEqual(p1["held_by"], results[0]["officer"])

    def test_concurrent_start_same_step(self):
        self.confirm_all_cargo()
        self.set_topology()
        plan = self.gen_plan()
        step = plan["steps"][0]
        ok, failed = [], []

        def start(token):
            try:
                ok.append(service.start_step(
                    self.vid, token=token, seq=step["seq"],
                    revision=plan["revision"], db_path=self.db))
            except ServiceError as e:
                failed.append(e)

        threads = [threading.Thread(target=start, args=(f"t{i}",))
                   for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(ok), 1, (ok, failed))
        self.assertEqual(len(failed), 3)
        events = service.list_events(self.vid, db_path=self.db)["events"]
        self.assertEqual(len([e for e in events
                              if e["action"] == "STEP_STARTED"]), 1)


# --------------------------------------------------------------------------- #
# 9. 中断点恢复
# --------------------------------------------------------------------------- #

class TestRecovery(Base):
    def _prepare_running_step(self):
        self.confirm_all_cargo()
        self.set_topology()
        plan = self.gen_plan()
        step = plan["steps"][0]
        return plan, step

    def test_crash_after_seize(self):
        self.set_topology()
        service.simulate_crash_at("after_seize")
        with self.assertRaises(service.SimulatedCrash):
            service.seize_pump(self.vid, token="sz1", pump_id="P1",
                               officer="zhang", db_path=self.db)
        # 恢复：占用已落库且唯一；同令牌重试回放首次结果，不重复抢占
        rep = service.recover(self.vid, db_path=self.db)
        self.assertEqual(len(rep["recovered"]), 1)
        r = service.seize_pump(self.vid, token="sz1", pump_id="P1",
                               officer="zhang", db_path=self.db)
        self.assertTrue(r["idempotent"])
        res = service.get_resources(self.vid, db_path=self.db)
        p1 = next(p for p in res["pumps"] if p["pump_id"] == "P1")
        self.assertEqual(p1["held_by"], "zhang")
        # 另一名值班员抢同一泵组仍被拒
        with self.assertRaises(ServiceError) as ctx:
            service.seize_pump(self.vid, token="sz2", pump_id="P1",
                               officer="li", db_path=self.db)
        self.assertEqual(ctx.exception.code, "PUMP_BUSY")

    def test_crash_after_dispatch(self):
        plan, step = self._prepare_running_step()
        service.simulate_crash_at("after_dispatch")
        with self.assertRaises(service.SimulatedCrash):
            service.start_step(self.vid, token="st1", seq=step["seq"],
                               revision=plan["revision"], db_path=self.db)
        rep = service.recover(self.vid, db_path=self.db)["recovered"][0]
        self.assertEqual(rep["running_step"], step["seq"])
        # 客户端重试：同令牌回放，不重复下发
        r = service.start_step(self.vid, token="st1", seq=step["seq"],
                               revision=plan["revision"], db_path=self.db)
        self.assertTrue(r["idempotent"])
        self.assertTrue(r["dispatched"])
        events = service.list_events(self.vid, db_path=self.db)["events"]
        self.assertEqual(len([e for e in events
                              if e["action"] == "STEP_STARTED"]), 1)
        # 泵仍被该步独占，另起一步被拒
        if len(plan["steps"]) > 1:
            with self.assertRaises(ServiceError):
                service.start_step(self.vid, token="st2",
                                   seq=plan["steps"][1]["seq"],
                                   revision=plan["revision"], db_path=self.db)
        # 状态唯一：一个 running，一个泵占用
        state = rep["state"]
        self.assertEqual(len([s for s in state["steps"]
                              if s["status"] == "running"]), 1)
        self.assertEqual(len(state["seizures"]), 1)

    def test_crash_after_partial_flow(self):
        plan, step = self._prepare_running_step()
        service.start_step(self.vid, token="st1", seq=step["seq"],
                           revision=plan["revision"], db_path=self.db)
        lv = self.levels()
        lv[step["source_tank"]] -= step["target_volume"] * 0.5
        lv[step["target_tank"]] += step["target_volume"] * 0.5
        service.simulate_crash_at("after_partial_flow")
        with self.assertRaises(service.SimulatedCrash):
            service.feedback(self.vid, levels=lv,
                             flows={step["pump_id"]: 120}, db_path=self.db)
        # 恢复后已转移水量不丢（按最新液位重算）
        rep = service.recover(self.vid, db_path=self.db)["recovered"][0]
        self.assertAlmostEqual(rep["transferred_so_far"],
                               step["target_volume"] * 0.5, places=2)
        # 补齐剩余量后仍可确认
        lv = self.levels()
        lv[step["source_tank"]] -= step["target_volume"] * 0.5
        lv[step["target_tank"]] += step["target_volume"] * 0.5
        service.feedback(self.vid, levels=lv,
                         flows={step["pump_id"]: 120}, db_path=self.db)
        r = service.confirm_step(self.vid, token="c1", seq=step["seq"],
                                 db_path=self.db)
        self.assertTrue(r["confirmed"])

    def test_crash_after_confirm(self):
        plan, step = self._prepare_running_step()
        self.run_step_token = f"st-{step['seq']}"
        service.start_step(self.vid, token=self.run_step_token,
                           seq=step["seq"], revision=plan["revision"],
                           db_path=self.db)
        lv = self.levels()
        lv[step["source_tank"]] -= step["target_volume"]
        lv[step["target_tank"]] += step["target_volume"]
        service.feedback(self.vid, levels=lv,
                         flows={step["pump_id"]: 120}, db_path=self.db)
        service.simulate_crash_at("after_confirm")
        with self.assertRaises(service.SimulatedCrash):
            service.confirm_step(self.vid, token="cf1", seq=step["seq"],
                                 db_path=self.db)
        rep = service.recover(self.vid, db_path=self.db)["recovered"][0]
        self.assertIsNone(rep["running_step"])
        # 确认结果可回放，不会重复确认；泵已释放
        r = service.confirm_step(self.vid, token="cf1", seq=step["seq"],
                                 db_path=self.db)
        self.assertTrue(r["idempotent"])
        self.assertTrue(r["confirmed"])
        res = service.get_resources(self.vid, db_path=self.db)
        self.assertFalse(any(p["occupied"] for p in res["pumps"]))
        # 已确认转移量保留
        done = service.get_plan(self.vid, db_path=self.db)["steps"][0]
        self.assertEqual(done["status"], "confirmed")
        self.assertGreater(done["transferred"], 0)
        # 下一步可以正常推进
        steps = service.get_plan(self.vid, db_path=self.db)["steps"]
        if len(steps) > 1:
            r = service.start_step(self.vid, token="next",
                                   seq=steps[1]["seq"],
                                   revision=plan["revision"], db_path=self.db)
            self.assertTrue(r["dispatched"])


if __name__ == "__main__":
    unittest.main()
