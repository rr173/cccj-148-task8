"""实船稳性 / 受力近似模型。

在配载系统的简化模型（KG/GM、纵倾）上扩展压载模块需要的量：
  横倾 heel   —— 由横向力矩与 GM 近似：tanθ = M_t / (Δ·GM)
  纵倾 trim   —— 与 stowage.validate 同一公式，保证两模块口径一致
  吃水 draft  —— Δ / (TPC·100)，艏艉吃水 = 平均吃水 ± trim/2，吃水差 = trim
  舱体受力    —— 船体梁弯矩代理 bm = Σw·|x−mid| / Δ，以及每舱 [min,max] 液位

所有计算都是确定性的纯函数，便于规划器反复求值与测试断言。
"""
import math

KM = 13.0          # 与 stowage.validate 保持一致的常数
BAY_FALLBACK = 12.2

DEFAULT_CONFIG = {
    "max_heel_deg": 3.0,
    "max_draft": 12.0,
    "min_draft": 2.0,
    "max_bm": 8.0,
    "tpc": 25.0,
    "tolerance": 2.0,
    "drift_tol": 1.0,
    "low_flow_ratio": 0.3,
}


class Ship:
    """船舶静力学上下文：空船参数 + 船长/船中 + 稳性限值。"""

    def __init__(self, lightship_weight, lightship_lcg, lightship_vcg,
                 ship_len, min_gm, max_gm, max_trim, config=None):
        self.lightship_weight = lightship_weight
        self.lightship_lcg = lightship_lcg
        self.lightship_vcg = lightship_vcg
        self.ship_len = ship_len
        self.mid = ship_len / 2.0
        self.min_gm = min_gm
        self.max_gm = max_gm
        self.max_trim = max_trim
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(config or {})
        self.config = cfg


def metrics(ship: Ship, cargo_items, ballast_items):
    """cargo_items / ballast_items: (weight, x, y, z) 四元组列表。"""
    total_w = ship.lightship_weight
    mv = ship.lightship_weight * ship.lightship_vcg
    ml = ship.lightship_weight * (ship.lightship_lcg - ship.mid)
    mt = 0.0                       # 横向力矩（右舷为正）
    bm = 0.0                       # 船体梁弯矩代理
    for w, x, y, z in list(cargo_items) + list(ballast_items):
        if w <= 0:
            continue
        total_w += w
        mv += w * z
        ml += w * (x - ship.mid)
        mt += w * y
        bm += w * abs(x - ship.mid)
    kg = mv / total_w if total_w else 0.0
    gm = KM - kg
    lcg = ship.mid + (ml / total_w if total_w else 0.0)
    tcg = mt / total_w if total_w else 0.0
    trim = (lcg - ship.mid) * total_w / max(total_w * ship.ship_len / 100.0, 1.0)
    heel = 90.0 if gm <= 0 else math.degrees(
        math.atan2(mt, total_w * gm))
    draft = total_w / (ship.config["tpc"] * 100.0)
    return {
        "displacement": round(total_w, 3),
        "kg": round(kg, 4),
        "gm": round(gm, 4),
        "lcg": round(lcg, 4),
        "tcg": round(tcg, 4),
        "trim": round(trim, 4),
        "heel_deg": round(heel, 4),
        "draft_mean": round(draft, 4),
        "draft_fwd": round(draft - trim / 2.0, 4),
        "draft_aft": round(draft + trim / 2.0, 4),
        "draft_diff": round(trim, 4),
        "bm_ratio": round(bm / total_w if total_w else 0.0, 4),
    }


def violations(ship: Ship, m) -> list:
    """指标 → 超限列表（空 = 安全）。规划器与执行确认共用同一判据。"""
    out = []
    cfg = ship.config
    if m["gm"] < ship.min_gm - 1e-9:
        out.append({"code": "GM_LOW", "limit": ship.min_gm,
                    "value": m["gm"],
                    "message": f"GM {m['gm']:.2f}m 低于下限 {ship.min_gm:.2f}m"})
    if m["gm"] > ship.max_gm + 1e-9:
        out.append({"code": "GM_HIGH", "limit": ship.max_gm,
                    "value": m["gm"],
                    "message": f"GM {m['gm']:.2f}m 高于上限 {ship.max_gm:.2f}m"})
    if abs(m["heel_deg"]) > cfg["max_heel_deg"] + 1e-9:
        out.append({"code": "HEEL", "limit": cfg["max_heel_deg"],
                    "value": m["heel_deg"],
                    "message": f"横倾 {m['heel_deg']:.2f}° 超限 "
                               f"±{cfg['max_heel_deg']:.2f}°"})
    if abs(m["trim"]) > ship.max_trim + 1e-9:
        out.append({"code": "TRIM", "limit": ship.max_trim,
                    "value": m["trim"],
                    "message": f"纵倾 {m['trim']:.2f}m 超限 "
                               f"±{ship.max_trim:.2f}m"})
    if m["draft_mean"] > cfg["max_draft"] + 1e-9:
        out.append({"code": "DRAFT_HIGH", "limit": cfg["max_draft"],
                    "value": m["draft_mean"],
                    "message": f"平均吃水 {m['draft_mean']:.2f}m 超过 "
                               f"{cfg['max_draft']:.2f}m"})
    if m["draft_mean"] < cfg["min_draft"] - 1e-9:
        out.append({"code": "DRAFT_LOW", "limit": cfg["min_draft"],
                    "value": m["draft_mean"],
                    "message": f"平均吃水 {m['draft_mean']:.2f}m 低于 "
                               f"{cfg['min_draft']:.2f}m"})
    if m["bm_ratio"] > cfg["max_bm"] + 1e-9:
        out.append({"code": "HULL_STRESS", "limit": cfg["max_bm"],
                    "value": m["bm_ratio"],
                    "message": f"船体受力代理 {m['bm_ratio']:.2f} 超限 "
                               f"{cfg['max_bm']:.2f}"})
    return out


def violation_score(ship: Ship, m) -> float:
    """超限程度的连续量度，供规划器贪心下降；0 = 全部达标。"""
    score = 0.0
    cfg = ship.config
    score += max(0.0, ship.min_gm - m["gm"]) ** 2 * 100
    score += max(0.0, m["gm"] - ship.max_gm) ** 2 * 100
    score += max(0.0, abs(m["heel_deg"]) - cfg["max_heel_deg"]) ** 2
    score += max(0.0, abs(m["trim"]) - ship.max_trim) ** 2 * 10
    score += max(0.0, m["draft_mean"] - cfg["max_draft"]) ** 2 * 10
    score += max(0.0, cfg["min_draft"] - m["draft_mean"]) ** 2 * 10
    score += max(0.0, m["bm_ratio"] - cfg["max_bm"]) ** 2 * 10
    return score
