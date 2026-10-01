"""拓扑录入解析、示例压载网络、与配载库对接。

大副录入格式（JSON）：
{
  "ship": {"length":..,"beam":..,"lightship_w":..,...},
  "tanks": [{"id","name","x","y","z_bottom","z_top","length","width",
             "capacity","min_vol","max_vol","initial_vol"}],
  "pumps": [{"id","name","rate","suction_node","delivery_node"}],
  "valves": [{"id","node_a","node_b","open","mutex_group"}]
}
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Dict, List, Optional

from .domain import Pump, Ship, Tank, Topology, Valve, WeightItem
from .planner import Milestone


class TopologyError(Exception):
    pass


def topology_from_dict(payload: dict) -> Topology:
    ship_in = payload.get("ship") or {}
    ship = Ship(
        length=float(ship_in["length"]),
        beam=float(ship_in["beam"]),
        lightship_w=float(ship_in.get("lightship_w", 9000.0)),
        lightship_x=float(ship_in.get("lightship_x", 0.0)),
        lightship_y=float(ship_in.get("lightship_y", 0.0)),
        lightship_z=float(ship_in.get("lightship_z", 9.0)),
        max_heel_deg=float(ship_in.get("max_heel_deg", 3.0)),
        max_trim_m=float(ship_in.get("max_trim_m", 1.5)),
        min_draft=float(ship_in.get("min_draft", 3.0)),
        max_draft=float(ship_in.get("max_draft", 12.5)),
        max_bending_moment=float(ship_in.get("max_bending_moment", 8.0e5)),
        min_gm=float(ship_in.get("min_gm", 0.3)),
        max_gm=float(ship_in.get("max_gm", 25.0)),
    )
    tanks: Dict[str, Tank] = {}
    for t in payload.get("tanks", []):
        tk = Tank(
            id=str(t["id"]), name=str(t.get("name", t["id"])),
            x=float(t["x"]), y=float(t["y"]),
            z_bottom=float(t.get("z_bottom", 0.3)),
            z_top=float(t.get("z_top", 10.0)),
            length=float(t.get("length", 10.0)),
            width=float(t.get("width", 10.0)),
            capacity=float(t["capacity"]),
            min_vol=float(t.get("min_vol", 0.0)),
            max_vol=t.get("max_vol"),
            initial_vol=float(t.get("initial_vol", 0.0)),
        )
        if tk.id in tanks:
            raise TopologyError(f"重复舱号 {tk.id}")
        tanks[tk.id] = tk

    pumps: Dict[str, Pump] = {}
    for p in payload.get("pumps", []):
        pk = Pump(id=str(p["id"]), name=str(p.get("name", p["id"])),
                  rate=float(p["rate"]),
                  suction_node=str(p["suction_node"]),
                  delivery_node=str(p["delivery_node"]))
        if pk.id in pumps:
            raise TopologyError(f"重复泵号 {pk.id}")
        pumps[pk.id] = pk

    valves: Dict[str, Valve] = {}
    for v in payload.get("valves", []):
        vv = Valve(id=str(v["id"]), node_a=str(v["node_a"]),
                   node_b=str(v["node_b"]),
                   open=bool(v.get("open", True)),
                   mutex_group=v.get("mutex_group"))
        if vv.id in valves:
            raise TopologyError(f"重复阀号 {vv.id}")
        valves[vv.id] = vv

    if not tanks:
        raise TopologyError("至少需要一个压载舱")
    if not pumps:
        raise TopologyError("至少需要一台压载泵")
    return Topology(ship, tanks, pumps, valves)


def initial_volumes(topo: Topology) -> Dict[str, float]:
    return {t.id: t.initial_vol for t in topo.tanks.values()}


# --------------------------------------------------------------------- #
# 示例拓扑（六舱双泵、交叉总管互斥）
# --------------------------------------------------------------------- #
def demo_topology() -> dict:
    cap = 14 * 14 * 9.5
    specs = [("WBP", "左艏压载舱", 62, -11), ("EBP", "右艏压载舱", 62, 11),
             ("WBS", "左艉压载舱", -62, -11), ("EBS", "右艉压载舱", -62, 11),
             ("WBM", "左舯压载舱", 0, -12), ("EBM", "右舯压载舱", 0, 12)]
    tanks = [{"id": i, "name": n, "x": x, "y": y, "z_bottom": 0.5,
              "z_top": 10.0, "length": 14, "width": 14, "capacity": cap,
              "min_vol": 80, "max_vol": round(cap * 0.98, 2),
              "initial_vol": 700} for i, n, x, y in specs]
    valves = []
    for i, _, _, _ in specs:
        valves += [
            {"id": f"VS_{i}", "node_a": f"T:{i}", "node_b": "SUC"},
            {"id": f"VD_{i}", "node_a": f"T:{i}", "node_b": "DEL"},
            {"id": f"VS2_{i}", "node_a": f"T:{i}", "node_b": "SUC2"},
            {"id": f"VD2_{i}", "node_a": f"T:{i}", "node_b": "DEL2"},
        ]
    valves += [
        {"id": "VC_S", "node_a": "SUC", "node_b": "SUC2",
         "mutex_group": "CROSS"},
        {"id": "VC_D", "node_a": "DEL", "node_b": "DEL2",
         "mutex_group": "CROSS"},
    ]
    return {
        "ship": {"length": 180, "beam": 32, "lightship_w": 15000,
                 "lightship_x": -5, "lightship_z": 9,
                 "max_heel_deg": 2.0, "max_trim_m": 1.2,
                 "min_draft": 3.0, "max_draft": 12.0,
                 "max_bending_moment": 4.0e6, "min_gm": 0.3,
                 "max_gm": 25.0},
        "tanks": tanks,
        "pumps": [
            {"id": "P1", "name": "1号压载泵", "rate": 12,
             "suction_node": "SUC", "delivery_node": "DEL"},
            {"id": "P2", "name": "2号压载泵", "rate": 10,
             "suction_node": "SUC2", "delivery_node": "DEL2"},
        ],
        "valves": valves,
    }


def demo_milestones() -> List[Milestone]:
    cargo0 = [WeightItem(400, 50, -3, 15, "C01"),
              WeightItem(400, -50, -2, 15, "C02")]
    cargo1 = cargo0 + [WeightItem(500, 55, 4, 16, "C03"),
                       WeightItem(500, -30, 4, 16, "C04"),
                       WeightItem(400, 10, -3, 16, "C05")]
    cargo2 = cargo1 + [WeightItem(500, 20, 4, 16, "C06"),
                       WeightItem(400, -55, -1, 16, "C07")]
    return [Milestone("30%装船", cargo0, 2),
            Milestone("60%装船", cargo1, 5),
            Milestone("100%装船", cargo2, 7)]


# --------------------------------------------------------------------- #
# 对接配载库：已发布方案 + confirmations -> 实际装船重量项
# --------------------------------------------------------------------- #
def cargo_from_stowage(stowage_db: str, voyage_id: str,
                       only_confirmed: bool = True) -> List[WeightItem]:
    if not os.path.exists(stowage_db):
        raise TopologyError(f"配载库不存在: {stowage_db}")
    conn = sqlite3.connect(stowage_db)
    conn.row_factory = sqlite3.Row
    try:
        if only_confirmed:
            sql = """
            SELECT c.weight AS w, st.x AS x,
                   (CASE WHEN s.code LIKE '%-01-%' THEN -2.44
                         WHEN s.code LIKE '%-02-%' THEN 0
                         ELSE 2.44 END) AS y,
                   s.z AS z, c.id AS tag
            FROM confirmations cf
            JOIN containers c ON c.id = cf.container_id
            JOIN slots s ON s.id = cf.slot_id
            JOIN stacks st ON st.id = s.stack_id
            WHERE cf.voyage_id = ? AND c.discharged = 0
            """
            rows = conn.execute(sql, (voyage_id,)).fetchall()
        else:
            sql = """
            SELECT c.weight AS w, st.x AS x,
                   (CASE WHEN s.code LIKE '%-01-%' THEN -2.44
                         WHEN s.code LIKE '%-02-%' THEN 0
                         ELSE 2.44 END) AS y,
                   s.z AS z, c.id AS tag
            FROM plans p
            JOIN plan_entries pe ON pe.plan_id = p.id
            JOIN containers c ON c.id = pe.container_id
            JOIN slots s ON s.id = pe.slot_id
            JOIN stacks st ON st.id = s.stack_id
            WHERE p.voyage_id = ? AND p.status='published'
              AND c.discharged = 0
            """
            rows = conn.execute(sql, (voyage_id,)).fetchall()
        return [WeightItem(float(r["w"]), float(r["x"]), float(r["y"]),
                           float(r["z"]), r["tag"]) for r in rows]
    finally:
        conn.close()


def loading_counts(stowage_db: str, voyage_id: str) -> dict:
    conn = sqlite3.connect(stowage_db)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(
            "SELECT COUNT(*) c FROM plan_entries pe JOIN plans p ON p.id=pe.plan_id "
            "WHERE p.voyage_id=? AND p.status='published'", (voyage_id,)).fetchone()
        done = conn.execute(
            "SELECT COUNT(*) c FROM confirmations WHERE voyage_id=?",
            (voyage_id,)).fetchone()
        return {"total": total["c"], "loaded_n": done["c"]}
    finally:
        conn.close()


def milestones_from_stowage(stowage_db: str, voyage_id: str,
                            n_milestones: int = 3,
                            extra_cargo: Optional[List[WeightItem]] = None
                            ) -> List[Milestone]:
    """按已发布方案顺序（bay 从艉到艏）切成若干装船里程碑；
    已确认部分作为起点，其余均匀分段。"""
    conn = sqlite3.connect(stowage_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT c.weight AS w, st.x AS x,
                   (CASE WHEN s.code LIKE '%-01-%' THEN -2.44
                         WHEN s.code LIKE '%-02-%' THEN 0
                         ELSE 2.44 END) AS y,
                   s.z AS z, c.id AS tag,
                   cf.id AS confirmed
            FROM plans p
            JOIN plan_entries pe ON pe.plan_id = p.id
            JOIN containers c ON c.id = pe.container_id
            JOIN slots s ON s.id = pe.slot_id
            JOIN stacks st ON st.id = s.stack_id
            LEFT JOIN confirmations cf
              ON cf.container_id = c.id AND cf.voyage_id = p.voyage_id
            WHERE p.voyage_id=? AND p.status='published' AND c.discharged=0
            ORDER BY st.x DESC, s.code
        """, (voyage_id,)).fetchall()
    finally:
        conn.close()
    items = [WeightItem(float(r["w"]), float(r["x"]), float(r["y"]),
                        float(r["z"]), r["tag"])
             for r in rows]
    if not items:
        raise TopologyError(
            f"航次 {voyage_id} 没有已发布配载方案或方案中无箱位")
    conf_flags = [r["confirmed"] is not None for r in rows]
    n_done = sum(conf_flags)
    remaining = items[n_done:]
    if extra_cargo:
        remaining = remaining + list(extra_cargo)
    size = max(1, (len(remaining) + n_milestones - 1) // n_milestones)
    ms: List[Milestone] = []
    acc = items[:n_done]
    for k in range(0, len(remaining), size):
        chunk = remaining[k:k + size]
        acc = acc + chunk
        ms.append(Milestone(f"装船里程碑 {len(ms)+1}", list(acc),
                            n_done + k + len(chunk)))
    if not ms:
        ms = [Milestone("当前装船", items[:n_done], n_done)]
    return ms
