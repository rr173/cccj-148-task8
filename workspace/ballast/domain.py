"""领域模型：压载舱 / 泵阀管路拓扑 / 矩形驳船近似稳性。

坐标（米）：x 纵向（船舯为 0，艏向为正），y 横向（右舷为正），z 垂向（基线向上）。

稳性为小角度矩形驳船近似（接口与真实静水力曲线可平滑替换）：
  吃水       d = Δ/(ρLB)（Δ=排水量质量）
  纵倾       trim = L * Mx/(Δ·GM_L)，GM_L = d/2 + L²/(12d) - KG
  横倾       heel = My/(Δ·GM_T)，GM_T = d/2 + B²/(12d) - KG - 自由液面修正
  船体梁     浮力按船长均布，与各重量点积分得剪力/弯矩，取 |BM| 最大值
限值：横倾角、纵倾差、艏艉吃水、最大船体弯矩，以及每舱 min/max 安全体积
（含压载泵吸空余量与舱顶安全余量，即“不抽空、不灌过安全上限”）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

RHO = 1.025  # 海水密度 t/m³

# 步骤资源互斥组：同一互斥管段上的两步互斥（资源集合相交即互斥）
# 阀门/泵自身也作为资源；交叉总管 CROSS 是共享互斥段。


@dataclass
class Tank:
    id: str
    name: str
    x: float                 # 舱中心纵向位置 m（舯=0，艏+）
    y: float                 # 舱中心横向位置 m（右舷+）
    z_bottom: float          # 舱底 z
    z_top: float             # 舱顶 z
    length: float            # 舱长 m（纵向，自由液面 I 用）
    width: float             # 舱宽 m（横向）
    capacity: float          # 几何容量 m³
    min_vol: float = 0.0     # 安全下限（泵吸空余量），低于即“抽空”
    max_vol: Optional[float] = None  # 安全上限，默认 0.98*capacity
    initial_vol: float = 0.0

    def __post_init__(self):
        if self.max_vol is None:
            self.max_vol = round(self.capacity * 0.98, 3)
        if not (self.min_vol <= self.initial_vol <= self.max_vol):
            raise ValueError(f"舱 {self.id} 初始液位 {self.initial_vol} 越界 "
                             f"[{self.min_vol}, {self.max_vol}]")

    @property
    def area(self) -> float:
        return self.length * self.width

    def level(self, vol: float) -> float:
        return self.z_bottom + vol / self.area

    def vcg(self, vol: float) -> float:
        return self.z_bottom + (vol / self.area) / 2.0

    def free_surface_i(self) -> float:
        """自由液面横向惯性矩 I = l·b³/12。"""
        return self.length * self.width ** 3 / 12.0


@dataclass
class Pump:
    id: str
    name: str
    rate: float              # 额定流量 m³/min
    suction_node: str
    delivery_node: str


@dataclass
class Valve:
    id: str
    node_a: str
    node_b: str
    open: bool = True
    mutex_group: Optional[str] = None   # 所在互斥管段（如交叉总管）


@dataclass
class Ship:
    length: float            # LBP m
    beam: float              # 型宽 m
    lightship_w: float       # 空船质量 t
    lightship_x: float = 0.0
    lightship_y: float = 0.0
    lightship_z: float = 8.0
    max_heel_deg: float = 3.0
    max_trim_m: float = 1.5
    min_draft: float = 3.0
    max_draft: float = 12.5
    max_bending_moment: float = 8.0e5   # t·m
    min_gm: float = 0.3
    max_gm: float = 5.0


@dataclass
class Topology:
    ship: Ship
    tanks: Dict[str, Tank]
    pumps: Dict[str, Pump]
    valves: Dict[str, Valve]

    # ---- 管路图（无向）：节点 -> {邻居: (资源id, 互斥组)} -------------- #
    def graph(self, only_open: bool = True) -> Dict[str, Dict[str, Tuple[str, Optional[str]]]]:
        g: Dict[str, Dict[str, Tuple[str, Optional[str]]]] = {}
        for v in self.valves.values():
            if only_open and not v.open:
                continue
            g.setdefault(v.node_a, {})[v.node_b] = (v.id, v.mutex_group)
            g.setdefault(v.node_b, {})[v.node_a] = (v.id, v.mutex_group)
        return g

    def find_route(self, source_tank: str, target_tank: str, pump: Pump
                   ) -> Optional[List[str]]:
        """返回该泵路径上需要开启的全部资源（泵 + 阀门），不可达返回 None。

        源舱 -> 泵吸口、泵排口 -> 目标舱两段各自经阀门无向连通。
        """
        g = self.graph()
        src = self._tank_node(source_tank)
        dst = self._tank_node(target_tank)
        if src is None or dst is None:
            return None
        v1 = self._reachable_valves(g, src, pump.suction_node)
        v2 = self._reachable_valves(g, pump.delivery_node, dst)
        if v1 is None or v2 is None:
            return None
        return [pump.id] + v1 + v2

    @staticmethod
    def _reachable_valves(g, start, goal) -> Optional[List[str]]:
        """BFS，返回 start->goal 路径上的阀门资源 id 列表。"""
        if start == goal:
            return []
        prev = {start: None}
        used_res: Dict[str, Optional[str]] = {}
        q = [start]
        while q:
            n = q.pop(0)
            for nb, (res, grp) in g.get(n, {}).items():
                if nb in prev:
                    continue
                prev[nb] = n
                used_res[nb] = res
                if nb == goal:
                    path = []
                    cur = nb
                    while prev[cur] is not None:
                        path.append(used_res[cur])
                        cur = prev[cur]
                    return list(reversed(path))
                q.append(nb)
        return None

    def _tank_node(self, tank_id: str) -> Optional[str]:
        """压载舱挂接点约定为节点 'T:<tank_id>'。"""
        if tank_id in self.tanks:
            return f"T:{tank_id}"
        return None

    def resource_conflict(self, res_a: List[str], res_b: List[str]) -> Optional[str]:
        """两组资源是否互斥：资源 id（泵/阀）相交，或互斥组相交。"""
        ga = {self._resource_group(r) for r in res_a}
        gb = {self._resource_group(r) for r in res_b}
        overlap = ga & gb
        overlap.discard(None)
        if set(res_a) & set(res_b) or overlap:
            return sorted(set(res_a) & set(res_b))[0] if set(res_a) & set(res_b) \
                else sorted(overlap)[0]
        return None

    def _resource_group(self, res: str) -> Optional[str]:
        v = self.valves.get(res)
        return v.mutex_group if v else None


# --------------------------------------------------------------------- #
@dataclass
class WeightItem:
    w: float
    x: float
    y: float = 0.0
    z: float = 10.0
    tag: str = ""


@dataclass
class StabilityReport:
    draft_aft: float
    draft_fwd: float
    heel_deg: float
    trim_m: float
    gm_t: float
    gm_l: float
    displacement: float
    max_bending: float
    tank_violations: List[dict] = field(default_factory=list)
    violations: List[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def evaluate(topo: Topology, cargo: List[WeightItem],
             volumes: Dict[str, float]) -> StabilityReport:
    """计算当前重量状态下的完整稳性并核对全部限值。"""
    ship = topo.ship
    items: List[WeightItem] = [WeightItem(ship.lightship_w, ship.lightship_x,
                                          ship.lightship_y, ship.lightship_z,
                                          "lightship")]
    items += list(cargo)
    tank_violations: List[dict] = []
    fsc_sum = 0.0
    for tid, tank in topo.tanks.items():
        v = volumes.get(tid, 0.0)
        if v < tank.min_vol - 1e-6:
            tank_violations.append({"tank": tid, "kind": "BELOW_MIN",
                                    "vol": v, "limit": tank.min_vol})
        if v > tank.max_vol + 1e-6:
            tank_violations.append({"tank": tid, "kind": "OVER_MAX",
                                    "vol": v, "limit": tank.max_vol})
        if v > 1e-9:
            items.append(WeightItem(v * RHO, tank.x, tank.y, tank.vcg(v), tid))
            # 未满舱自由液面（满舱/空舱无自由液面效应）
            if v < tank.max_vol - 1e-6 and v > tank.min_vol + 1e-6:
                fsc_sum += RHO * tank.free_surface_i()

    delta = sum(i.w for i in items)
    mx = sum(i.w * i.x for i in items)
    my = sum(i.w * i.y for i in items)
    mz = sum(i.w * i.z for i in items)
    kg = mz / delta

    L, B = ship.length, ship.beam
    d = delta / (RHO * L * B)
    bm_l = L ** 2 / (12 * d)
    bm_t = B ** 2 / (12 * d)
    gm_l = d / 2 + bm_l - kg
    gm_t_eff = d / 2 + bm_t - kg - fsc_sum / delta

    trim = L * mx / (delta * gm_l) if gm_l > 0 else math.copysign(99.0, mx)
    d_aft = d - trim / 2
    d_fwd = d + trim / 2
    heel = math.degrees(my / (delta * gm_t_eff)) if gm_t_eff > 0 else 99.0

    max_bm = _max_bending_moment(ship, items, delta, L)

    violations = list(tank_violations)
    def bad(kind, value, limit, **extra):
        violations.append({"kind": kind, "value": round(value, 4),
                           "limit": limit, **extra})

    if abs(heel) > ship.max_heel_deg:
        bad("HEEL", heel, ship.max_heel_deg)
    if abs(trim) > ship.max_trim_m:
        bad("TRIM", trim, ship.max_trim_m)
    if d_aft < ship.min_draft or d_fwd < ship.min_draft:
        bad("DRAFT_LOW", min(d_aft, d_fwd), ship.min_draft)
    if d_aft > ship.max_draft or d_fwd > ship.max_draft:
        bad("DRAFT_HIGH", max(d_aft, d_fwd), ship.max_draft)
    if abs(max_bm) > ship.max_bending_moment:
        bad("BENDING", max_bm, ship.max_bending_moment)
    if gm_t_eff < ship.min_gm:
        bad("GM_LOW", gm_t_eff, ship.min_gm)
    if gm_t_eff > ship.max_gm:
        bad("GM_HIGH", gm_t_eff, ship.max_gm)

    return StabilityReport(
        draft_aft=d_aft, draft_fwd=d_fwd, heel_deg=heel, trim_m=trim,
        gm_t=gm_t_eff, gm_l=gm_l, displacement=delta, max_bending=max_bm,
        tank_violations=tank_violations, violations=violations)


def _max_bending_moment(ship: Ship, items: List[WeightItem],
                        delta: float, L: float) -> float:
    """浮力沿船长均布；重量含空船均布 + 各点载荷，扫一遍求最大弯矩。"""
    x0, x1 = -L / 2, L / 2
    buoy_per_m = delta / L
    light_per_m = ship.lightship_w / L
    pts = [(x0, 0.0, 0.0)]
    for it in items:
        if it.tag == "lightship":
            continue
        pts.append((min(max(it.x, x0), x1), it.w, 0.0))
    pts.sort(key=lambda p: p[0])
    shear = 0.0
    moment = 0.0
    max_bm = 0.0
    prev_x = x0
    for x, point_w, _ in pts[1:]:
        dx = x - prev_x
        # 均布段精确积分：M(x)=M0+V0·dx+0.5·q·dx²，V(x)=V0+q·dx
        moment += shear * dx + 0.5 * (buoy_per_m - light_per_m) * dx ** 2
        shear += (buoy_per_m - light_per_m) * dx
        shear -= point_w
        max_bm = max(max_bm, abs(moment))
        prev_x = x
    return max_bm


def total_water(volumes: Dict[str, float]) -> float:
    return sum(volumes.values())
