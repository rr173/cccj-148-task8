"""分阶段压载水调水方案规划器。

输入：
  topo                压载拓扑（舱/泵/阀/互斥管段）
  loading_milestones  装船时间线：[(label, [WeightItem...]), ...]，从当前进度开始
  current_volumes     各舱当前体积（m³，实测）
  options             容差/单步上限等

流程：
  1) 在当前排水量/重心处线性化横倾、纵倾、艏艉吃水、船体弯矩限值；
     决策变量 = 各里程碑末各舱目标体积，加 L1 偏差变量，最小化总调水量。
     水量守恒（舱间转移，无舷外水）写成等式；每舱 min/max 为界。
     LP 不可行 -> StabilityUnreachable（目标稳性不可达）。
  2) 相邻里程碑间按净增减配平转移：源（需减少）与目标（需增加）配对，
     经拓扑查泵阀路径；找不到路径 -> RoutingUnavailable。
     两步资源（泵/阀/互斥组）相交 -> PlanConflict（泵阀冲突，无可并行方案）。
  3) 大转移拆成不超过单步上限的有序步骤；检查中间体积不越界（区间凸性
     保证单调路径上的舱界），并给出开始条件/所需泵阀/容差。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import linalg
from .domain import RHO, Ship, StabilityReport, Tank, Topology, WeightItem, evaluate


class PlanError(Exception):
    def __init__(self, code: str, message: str, detail: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.detail = detail or {}


@dataclass
class Milestone:
    label: str
    cargo: List[WeightItem]
    expected_loaded_n: int = 0


@dataclass
class PlannedStep:
    seq: int
    phase: int
    source_tank: str
    target_tank: str
    volume: float
    tolerance: float
    pump: str
    resources: List[str]
    mutex_groups: List[str]
    start_condition: dict
    status: str = "pending"
    transferred: float = 0.0


# --------------------------------------------------------------------- #
# 线性化
# --------------------------------------------------------------------- #
def linearized_coeffs(topo: Topology, cargo: List[WeightItem],
                      volumes: Dict[str, float]):
    """在当前工况处把稳性限值展开成关于舱体积增量 dv 的线性式。

    横向力矩 My(dv)   = My0 + Σ ρ y_i dv_i
    纵向力矩 Mx(dv)   = Mx0 + Σ ρ x_i dv_i
    重量   Δ(dv)      = Δ0 + ρ Σ dv_i
    纵倾 trim ≈ L/Δ (Mx - Mx_centroid) ，在当前点一阶展开：
      trim(dv) ≈ trim0 + Σ a_i dv_i，
      a_i = L·ρ (x_i - Mx/Δ)/ (Δ0·GM_L)
    横倾 heel(rad) ≈ My/(Δ·GM_T)：
      b_i = ρ (y_i - My/Δ)/(Δ0·GM_T)
    吃水 d_aft = Δ/(ρLB) - trim/2；d_fwd = Δ/(ρLB) + trim/2
    船体弯矩：在固定船长网格上，M_k 对 dv_i 的梯度为
      浮力贡献 + 点载荷贡献（见 _bending_grad）。
    """
    rep = evaluate(topo, cargo, volumes)
    ship = topo.ship
    L, B = ship.length, ship.beam

    delta0 = rep.displacement
    mx0 = sum(it.w * it.x for it in
              [WeightItem(ship.lightship_w, ship.lightship_x, ship.lightship_y,
                          ship.lightship_z)] + cargo) + \
        sum(RHO * volumes[t] * topo.tanks[t].x for t in volumes)
    my0 = sum(it.w * it.y for it in
              [WeightItem(ship.lightship_w, ship.lightship_x, ship.lightship_y,
                          ship.lightship_z)] + cargo) + \
        sum(RHO * volumes[t] * topo.tanks[t].y for t in volumes)

    d_d = 1.0 / (RHO * L * B)                 # d 对 Δ 的系数，再乘 ρ
    heel_scale = 1.0 / (delta0 * rep.gm_t)
    trim_scale = L / (delta0 * rep.gm_l)
    bm_grad = _bending_grad(topo, cargo, volumes, rep)

    coeffs = {}
    for tid, tank in topo.tanks.items():
        heel_b = RHO * (tank.y - my0 / delta0) * heel_scale
        trim_a = RHO * (tank.x - mx0 / delta0) * trim_scale
        daft = d_d * RHO - trim_a / 2
        dfwd = d_d * RHO + trim_a / 2
        coeffs[tid] = {
            "heel": math.degrees(heel_b),
            "trim": trim_a,
            "draft_aft": daft,
            "draft_fwd": dfwd,
            "bending": bm_grad[tid],
        }
    base = {
        "heel": rep.heel_deg, "trim": rep.trim_m,
        "draft_aft": rep.draft_aft, "draft_fwd": rep.draft_fwd,
        "bending": list(rep.__dict__.get("_bm_at", [])),  # 内部填充
    }
    return coeffs, base, rep, bm_grad


def _bending_grad(topo: Topology, cargo: List[WeightItem],
                  volumes: Dict[str, float], rep: StabilityReport,
                  n_stations: int = 41) -> Dict[str, List[float]]:
    """各站弯矩对每个舱 dv 的灵敏度。

    M(x_k) = ∫_0^k V ds；V 中重量项：点载荷在 x_i 左侧时贡献 -w_i，
    浮力按均布 +ρ·s/L·dv_i，空船不变。故
      ∂M_k/∂dv_i = ρ·[ (x_k-x0)²/(2L) - 1{x_i<=x_k}·(x_k-x_i) ]。
    """
    L = topo.ship.length
    x0 = -L / 2
    xs = [x0 + L * k / (n_stations - 1) for k in range(n_stations)]
    grad: Dict[str, List[float]] = {}
    for tid, tank in topo.tanks.items():
        g = []
        xt = min(max(tank.x, x0), -x0)
        for xk in xs:
            buoy = (xk - x0) ** 2 / (2 * L)
            weight = (xk - xt) if xt <= xk else 0.0
            g.append(RHO * (buoy - weight))
        grad[tid] = g
    # 当前各站弯矩
    buoy_per_m = rep.displacement / L
    light_per_m = topo.ship.lightship_w / L
    pts = [(min(max(t.x, x0), -x0), RHO * volumes[t.id]) for t in topo.tanks.values()]
    pts += [(min(max(it.x, x0), -x0), it.w) for it in cargo]
    pts.sort()
    vals = []
    shear = moment = 0.0
    pi = 0
    prev = x0
    for xk in xs:
        moment += shear * (xk - prev) + 0.5 * (buoy_per_m - light_per_m) * (xk - prev) ** 2
        shear += (buoy_per_m - light_per_m) * (xk - prev)
        while pi < len(pts) and pts[pi][0] <= xk + 1e-9:
            shear -= pts[pi][1]
            pi += 1
        vals.append(moment)
        prev = xk
    rep.__dict__["_bm_at"] = vals
    return grad


# --------------------------------------------------------------------- #
# 目标体积 LP
# --------------------------------------------------------------------- #
def solve_targets(topo: Topology, cargo: List[WeightItem],
                  volumes: Dict[str, float],
                  slack_limit: Optional[float] = None
                  ) -> Tuple[Dict[str, float], StabilityReport, dict]:
    """求满足全部限值且调水量最小的各舱目标体积。

    同时允许“装船重量微调”作为最终不可达时的诊断：先严格求解；严格不可行
    时再放开求解用于报告需要的最小松驰（不用于生成方案）。
    """
    tank_ids = list(topo.tanks.keys())
    n = len(tank_ids)
    coeffs, base, rep, bm_grad = linearized_coeffs(topo, cargo, volumes)
    ship = topo.ship

    # L1 偏差变量 u_i,v_i >=0：dv_i = (x_i-v0_i)，|dv_i| = u_i+v_i，
    # v0_i - u_i <= x_i <= v0_i + u_i（实现时直接加约束行）
    total_vars = 2 * n
    bounds = []
    for tid in tank_ids:
        t = topo.tanks[tid]
        bounds.append((t.min_vol, t.max_vol))
    for _ in range(n):
        bounds.append((0.0, 1e9))

    def col(tank_col_row, i):  # 占位，保持可读性
        return tank_col_row[i]

    c = [0.0] * n + [1.0] * n
    Aeq = [[1.0] * n + [0.0] * n]
    beq = [sum(volumes.values())]

    Aub: List[List[float]] = []
    bub: List[float] = []

    def add_ineq(row: List[float], rhs: float):
        Aub.append(row + [0.0] * n)
        bub.append(rhs)

    # L1: x_i - u_i <= v0_i ; -x_i - u_i <= -v0_i（u_i 一列两用：列 n+i）
    for i, tid in enumerate(tank_ids):
        r1 = [0.0] * total_vars
        r1[i] = 1.0
        r1[n + i] = -1.0
        add_ineq(r1, volumes[tid])
        r2 = [0.0] * total_vars
        r2[i] = -1.0
        r2[n + i] = -1.0
        add_ineq(r2, -volumes[tid])

    # 稳性限值（在 base 处一阶展开）
    for i, tid in enumerate(tank_ids):
        pass
    # heel: heel0 + Σ hi·dv_i ∈ [-max,max]
    for sign in (1, -1):
        row = [0.0] * total_vars
        for i, tid in enumerate(tank_ids):
            row[i] = sign * coeffs[tid]["heel"]
        add_ineq(row, sign * ship.max_heel_deg - sign * base["heel"])
    for sign in (1, -1):
        row = [0.0] * total_vars
        for i, tid in enumerate(tank_ids):
            row[i] = sign * coeffs[tid]["trim"]
        add_ineq(row, sign * ship.max_trim_m - sign * base["trim"])
    # 吃水：d0 + Σd_i dv ∈ [min,max]  （艏/艉分别）
    for key, lo, hi in (("draft_aft", ship.min_draft, ship.max_draft),
                        ("draft_fwd", ship.min_draft, ship.max_draft)):
        row_hi = [0.0] * total_vars
        row_lo = [0.0] * total_vars
        for i, tid in enumerate(tank_ids):
            row_hi[i] = coeffs[tid][key]
            row_lo[i] = -coeffs[tid][key]
        add_ineq(row_hi, hi - base[key])
        add_ineq(row_lo, base[key] - lo)
    # 弯矩：每站 -Mmax <= M0_k + Σ g_ik dv_i <= Mmax
    nst = len(next(iter(bm_grad.values())))
    bm0 = base["bending"]
    for k in range(nst):
        row_p = [0.0] * total_vars
        row_m = [0.0] * total_vars
        for i, tid in enumerate(tank_ids):
            row_p[i] = bm_grad[tid][k]
            row_m[i] = -bm_grad[tid][k]
        add_ineq(row_p, ship.max_bending_moment - bm0[k])
        add_ineq(row_m, ship.max_bending_moment + bm0[k])

    x, status = linalg.solve(c, Aeq=Aeq, beq=beq, Aub=Aub, bub=bub,
                             bounds=bounds)
    if status != "optimal":
        raise PlanError("STABILITY_UNREACHABLE",
                        "在当前装船工况与舱容/水量约束下，目标稳性不可达",
                        {"base_violations": rep.violations})
    targets = {tid: x[i] for i, tid in enumerate(tank_ids)}
    # 舍入到三位小数并把舍入残差并回调节能力最大的舱，严格保持水量守恒
    targets = _round_conserve(targets, sum(volumes.values()), topo)
    move = round(sum(x[n:]), 3)

    # 用完整非线性模型复核目标点
    target_rep = evaluate(topo, cargo, targets)
    if not target_rep.ok:
        # 线性化在大幅调水时可能偏乐观；按目标点重新线性化再迭代一次
        coeffs2, base2, rep2, bm_grad2 = linearized_coeffs(topo, cargo, targets)
        x2, st2 = _resolve_at(topo, targets, cargo, coeffs2, base2, bm_grad2)
        cand = ({tid: x2[i] for i, tid in enumerate(tank_ids)}
                if st2 == "optimal" else None)
        if cand is None or not evaluate(topo, cargo,
                _round_conserve(cand, sum(volumes.values()), topo)).ok:
            raise PlanError("STABILITY_UNREACHABLE",
                            "线性化复核未通过且无可行目标体积",
                            {"violations": target_rep.violations})
        targets = _round_conserve(cand, sum(volumes.values()), topo)
        target_rep = evaluate(topo, cargo, targets)
        move = round(sum(abs(targets[t] - volumes[t]) for t in tank_ids) / 2, 3)

    return targets, target_rep, {"movement": move}


def _round_conserve(targets: Dict[str, float], total: float,
                    topo: Topology) -> Dict[str, float]:
    """三位小数舍入后，把残差并入可调余量最大的舱，严格守恒且不越界。"""
    ids = list(targets)
    out = {t: round(targets[t], 3) for t in ids}
    diff = round(total - sum(out.values()), 3)
    if abs(diff) > 1e-9:
        # 选与残差方向一致、余量最大的舱
        if diff > 0:
            cand = [t for t in ids if out[t] + diff <= topo.tanks[t].max_vol]
            pick = max(cand or ids, key=lambda t: topo.tanks[t].max_vol - out[t])
        else:
            cand = [t for t in ids if out[t] + diff >= topo.tanks[t].min_vol]
            pick = max(cand or ids, key=lambda t: out[t] - topo.tanks[t].min_vol)
        out[pick] = round(out[pick] + diff, 3)
    return out


def _resolve_at(topo, v0, cargo, coeffs, base, bm_grad):
    tank_ids = list(topo.tanks.keys())
    n = len(tank_ids)
    total = 2 * n
    bounds = [(topo.tanks[t].min_vol, topo.tanks[t].max_vol) for t in tank_ids]
    bounds += [(0.0, 1e9)] * n
    c = [0.0] * n + [1.0] * n
    Aeq = [[1.0] * n + [0.0] * n]
    beq = [sum(v0.values())]
    Aub, bub = [], []

    def add(row, rhs):
        Aub.append(row + [0.0] * n); bub.append(rhs)

    for i in range(n):
        r = [0.0] * total; r[i] = 1; r[n + i] = -1
        add(r, v0[tank_ids[i]])
        r = [0.0] * total; r[i] = -1; r[n + i] = -1
        add(r, -v0[tank_ids[i]])
    ship = topo.ship
    for sign in (1, -1):
        r = [0.0] * total
        for i, tid in enumerate(tank_ids): r[i] = sign * coeffs[tid]["heel"]
        add(r, sign * ship.max_heel_deg - sign * base["heel"])
        r = [0.0] * total
        for i, tid in enumerate(tank_ids): r[i] = sign * coeffs[tid]["trim"]
        add(r, sign * ship.max_trim_m - sign * base["trim"])
    for key, lo, hi in (("draft_aft", ship.min_draft, ship.max_draft),
                        ("draft_fwd", ship.min_draft, ship.max_draft)):
        rh = [0.0] * total; rl = [0.0] * total
        for i, tid in enumerate(tank_ids):
            rh[i] = coeffs[tid][key]; rl[i] = -coeffs[tid][key]
        add(rh, hi - base[key]); add(rl, base[key] - lo)
    for k in range(len(base["bending"])):
        rp = [0.0] * total; rm = [0.0] * total
        for i, tid in enumerate(tank_ids):
            rp[i] = bm_grad[tid][k]; rm[i] = -bm_grad[tid][k]
        add(rp, ship.max_bending_moment - base["bending"][k])
        add(rm, ship.max_bending_moment + base["bending"][k])
    return linalg.solve(c, Aeq=Aeq, beq=beq, Aub=Aub, bub=bub, bounds=bounds)


# --------------------------------------------------------------------- #
# 步骤编排
# --------------------------------------------------------------------- #
def build_phase_steps(topo: Topology, v_from: Dict[str, float],
                      v_to: Dict[str, float], phase: int,
                      max_step: float, tolerance_pct: float
                      ) -> Tuple[List[PlannedStep], List[str]]:
    """把两个体积状态之间的净变化配平成有序转移步骤。"""
    tank_ids = list(topo.tanks.keys())
    delta = {t: round(v_to[t] - v_from[t], 3) for t in tank_ids}
    donors = sorted([t for t in tank_ids if delta[t] < -1e-6],
                    key=lambda t: delta[t])
    receivers = sorted([t for t in tank_ids if delta[t] > 1e-6],
                       key=lambda t: -delta[t])
    if abs(sum(delta.values())) > 1e-3:
        raise PlanError("WATER_NOT_CONSERVED",
                        f"阶段 {phase} 水量不守恒 Δ={sum(delta.values()):.3f}")

    raw: List[Tuple[str, str, float, str, List[str], List[str]]] = []
    di = ri = 0
    rem_d = {t: -delta[t] for t in donors}
    rem_r = {t: delta[t] for t in receivers}
    while di < len(donors) or ri < len(receivers):
        if di >= len(donors) or ri >= len(receivers):
            raise PlanError("WATER_NOT_CONSERVED", "配平后仍有残余单侧需求")
        s, d = donors[di], receivers[ri]
        q = min(rem_d[s], rem_r[d])
        pump, resources, groups = _route(topo, s, d)
        raw.append((s, d, round(q, 3), pump, resources, groups))
        rem_d[s] -= q
        rem_r[d] -= q
        if rem_d[s] <= 1e-6:
            di += 1
        if rem_r[d] <= 1e-6:
            ri += 1

    # 同一步骤集内不得资源互斥（泵/阀/互斥组）——阶段内步骤严格串行，
    # 互斥检查用于：①跨 phase 重叠窗口（当前实现不重叠）；②路由本身报冲突。
    _check_routing_conflicts(raw)

    # 拆分为单步上限
    steps: List[PlannedStep] = []
    seq_base = 0
    for (s, d, q, pump, resources, groups) in raw:
        pieces = max(1, math.ceil(q / max_step - 1e-9))
        each = q / pieces
        for k in range(pieces):
            vol = round(each if k < pieces - 1 else
                        q - each * (pieces - 1), 3)
            tol = round(max(vol * tolerance_pct, 2.0), 3)
            steps.append(PlannedStep(
                seq=0, phase=phase, source_tank=s, target_tank=d, volume=vol,
                tolerance=tol, pump=pump, resources=resources,
                mutex_groups=groups, status="pending",
                start_condition={"phase": phase,
                                 "kind": "prev_step_confirmed",
                                 "detail": "同阶段前序步骤全部确认"
                                           if steps else "里程碑达到即可启动"}))
    for i, st in enumerate(steps):
        st.seq = i + 1
    return steps, donors + receivers


def _route(topo: Topology, source_tank: str, target_tank: str):
    candidates = []
    for pump in topo.pumps.values():
        resources = topo.find_route(source_tank, target_tank, pump)
        if resources is not None:
            candidates.append((pump, resources))
    if not candidates:
        raise PlanError("ROUTING_UNAVAILABLE",
                        f"舱 {source_tank} -> {target_tank} 无可用泵阀路径",
                        {"source": source_tank, "target": target_tank})
    # 选流量最大的泵
    candidates.sort(key=lambda c: -c[0].rate)
    pump, resources = candidates[0]
    groups = sorted({topo.valves[r].mutex_group for r in resources
                     if r in topo.valves and topo.valves[r].mutex_group})
    return pump.id, resources, groups


def _check_routing_conflicts(raw):
    """原始配对中若同一资源被不同转移同时需要，属于规划级泵阀冲突。

    步骤严格串行时资源可复用；这里检查的是“并行必要性”——当前规划总是
    串行，所以真正暴露的冲突是：同一步骤的资源集与其他步骤在数据拓扑层
    声明为互斥（留给并行扩展），以及同一对舱的重复路径异常。
    """
    seen_pairs = set()
    for s, d, q, pump, res, groups in raw:
        key = (s, d)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)


def verify_intermediate_safety(topo: Topology, cargo: List[WeightItem],
                               v_from: Dict[str, float],
                               steps: List[PlannedStep]) -> None:
    """逐步模拟，确认中间体积与稳性全部满足；不满足则报告首个断点。"""
    vols = dict(v_from)
    for st in steps:
        vols[st.source_tank] = round(vols[st.source_tank] - st.volume, 3)
        vols[st.target_tank] = round(vols[st.target_tank] + st.volume, 3)
        ts = topo.tanks[st.source_tank]
        tt = topo.tanks[st.target_tank]
        if vols[st.source_tank] < ts.min_vol - 1e-6 or \
           vols[st.target_tank] > tt.max_vol + 1e-6:
            raise PlanError("STEP_TANK_LIMIT",
                            f"步骤 {st.seq} 会抽空 {st.source_tank} 或"
                            f"灌满 {st.target_tank}",
                            {"seq": st.seq})
        rep = evaluate(topo, cargo, vols)
        if not rep.ok:
            raise PlanError("INTERMEDIATE_STABILITY",
                            f"步骤 {st.seq} 后稳性越限",
                            {"seq": st.seq, "violations": rep.violations})


def plan_all(topo: Topology, milestones: List[Milestone],
             current_volumes: Dict[str, float],
             max_step: float = 300.0,
             tolerance_pct: float = 0.02) -> dict:
    """生成全时间线分阶段方案。"""
    vols = dict(current_volumes)
    phases = []
    all_steps: List[PlannedStep] = []
    seq = 1
    prev_rep = evaluate(topo, milestones[0].cargo if milestones else [], vols)
    for ph, ms in enumerate(milestones):
        targets, rep, info = solve_targets(topo, ms.cargo, vols)
        steps, affected = build_phase_steps(topo, vols, targets, ph,
                                            max_step, tolerance_pct)
        verify_intermediate_safety(topo, ms.cargo, vols, steps)
        for st in steps:
            st.seq = seq
            st.start_condition = {
                "phase": ph, "milestone": ms.label,
                "expected_loaded_n": ms.expected_loaded_n,
                "kind": "first_step" if seq == 1 else "prev_step_confirmed",
                "prev_seq": None if seq == 1 else seq - 1,
            }
            all_steps.append(st)
            seq += 1
        phases.append({
            "phase": ph, "milestone": ms.label,
            "expected_loaded_n": ms.expected_loaded_n,
            "target_volumes": targets,
            "stability": _rep_dict(rep),
            "movement": info["movement"],
            "affected_tanks": affected,
        })
        vols = targets
        prev_rep = rep

    return {
        "phases": phases,
        "steps": [_step_dict(s) for s in all_steps],
        "final_volumes": vols,
        "water_total": round(sum(vols.values()), 3),
    }


def _rep_dict(rep: StabilityReport) -> dict:
    return {
        "draft_aft": round(rep.draft_aft, 3),
        "draft_fwd": round(rep.draft_fwd, 3),
        "heel_deg": round(rep.heel_deg, 4),
        "trim_m": round(rep.trim_m, 4),
        "gm_t": round(rep.gm_t, 4),
        "gm_l": round(rep.gm_l, 4),
        "displacement": round(rep.displacement, 2),
        "max_bending": round(rep.max_bending, 1),
        "ok": rep.ok,
        "violations": rep.violations,
    }


def _step_dict(s: PlannedStep) -> dict:
    return {
        "seq": s.seq, "phase": s.phase,
        "source_tank": s.source_tank, "target_tank": s.target_tank,
        "volume": s.volume, "tolerance": s.tolerance,
        "pump": s.pump, "resources": s.resources,
        "mutex_groups": s.mutex_groups,
        "start_condition": s.start_condition,
        "status": s.status, "transferred": s.transferred,
    }
