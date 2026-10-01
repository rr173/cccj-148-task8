"""压载拓扑领域模型：舱、泵组、阀门、管路连通关系。

大副录入的拓扑是一张无向图：
  节点 = 压载舱(T) / 泵组(P) / 阀门(V)
  边   = 管路，可声明 mutex_group（同组管路互斥）
一步调水 = 从源舱到目标舱的一条恰含一台泵的通路，
通路上的泵、阀、管路即该步所需资源。
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from stowage.db import ServiceError
from stowage.domain import new_id


@dataclass
class Tank:
    id: str
    name: str
    capacity: float
    min_level: float
    max_level: float
    level: float
    x: float
    y: float
    z: float
    density: float = 1.025

    @property
    def weight(self):
        return self.level * self.density


@dataclass
class Pump:
    id: str
    name: str
    rate: float


@dataclass
class Valve:
    id: str
    name: str


@dataclass
class Pipe:
    id: str
    name: str
    a_node: str
    b_node: str
    mutex_group: Optional[str] = None


@dataclass
class Topology:
    voyage_id: str
    tanks: Dict[str, Tank] = field(default_factory=dict)
    pumps: Dict[str, Pump] = field(default_factory=dict)
    valves: Dict[str, Valve] = field(default_factory=dict)
    pipes: Dict[str, Pipe] = field(default_factory=dict)

    def node_kind(self, node_id):
        if node_id in self.tanks:
            return "tank"
        if node_id in self.pumps:
            return "pump"
        if node_id in self.valves:
            return "valve"
        return None

    def adjacency(self):
        adj = {}
        for p in self.pipes.values():
            adj.setdefault(p.a_node, []).append((p.b_node, p.id))
            adj.setdefault(p.b_node, []).append((p.a_node, p.id))
        return adj


def parse_topology(voyage_id, payload) -> Topology:
    """校验并规范化大副录入的拓扑。任何一项非法都整体拒绝。"""
    errors = []
    topo = Topology(voyage_id=voyage_id)

    for t in payload.get("tanks") or []:
        try:
            name = str(t["name"]).strip()
            capacity = float(t["capacity"])
            level = float(t.get("level", 0.0))
            min_level = float(t.get("min_level", 0.0))
            max_level = float(t.get("max_level", capacity))
            if not name or capacity <= 0:
                raise ValueError("name/capacity")
            if not (0 <= min_level <= level <= max_level <= capacity):
                raise ValueError("levels out of range")
            topo.tanks[name] = Tank(
                id=name, name=name, capacity=capacity, min_level=min_level,
                max_level=max_level, level=level,
                x=float(t.get("x", 0.0)), y=float(t.get("y", 0.0)),
                z=float(t.get("z", 0.0)),
                density=float(t.get("density", 1.025)))
        except (KeyError, TypeError, ValueError) as e:
            errors.append(f"invalid tank {t!r}: {e}")

    for p in payload.get("pumps") or []:
        try:
            name = str(p["name"]).strip()
            rate = float(p["rate"])
            if not name or rate <= 0:
                raise ValueError("name/rate")
            topo.pumps[name] = Pump(id=name, name=name, rate=rate)
        except (KeyError, TypeError, ValueError) as e:
            errors.append(f"invalid pump {p!r}: {e}")

    for v in payload.get("valves") or []:
        try:
            name = str(v["name"]).strip()
            if not name:
                raise ValueError("name")
            topo.valves[name] = Valve(id=name, name=name)
        except (KeyError, TypeError, ValueError) as e:
            errors.append(f"invalid valve {v!r}: {e}")

    for p in payload.get("pipes") or []:
        try:
            name = str(p["name"]).strip()
            a, b = str(p["a"]), str(p["b"])
            if not name or a == b:
                raise ValueError("name/endpoints")
            topo.pipes[name] = Pipe(id=name, name=name, a_node=a, b_node=b,
                                    mutex_group=p.get("mutex_group") or None)
        except (KeyError, TypeError, ValueError) as e:
            errors.append(f"invalid pipe {p!r}: {e}")

    for pipe in topo.pipes.values():
        for node in (pipe.a_node, pipe.b_node):
            if topo.node_kind(node) is None:
                errors.append(f"pipe {pipe.name}: unknown node {node}")

    if not topo.tanks:
        errors.append("at least one tank is required")
    if errors:
        raise ServiceError(422, "INVALID_TOPOLOGY",
                           "ballast topology failed validation",
                           {"errors": errors})
    return topo


def find_path(topo: Topology, src_tank: str, dst_tank: str):
    """BFS 找一条源舱→目标舱、恰含一台泵、且不自相斥的通路。

    返回 {"pump": id, "valves": [...], "pipes": [...]} 或 None。
    同一步的管路若两根落在同一 mutex_group，等于该步自己与自己互斥，
    这样的通路不可用（换路或判不可达）。
    """
    from collections import deque

    adj = topo.adjacency()
    best = None
    queue = deque([(src_tank, [src_tank], [])])
    visited_depth = {src_tank: 0}
    while queue:
        node, nodes, pipe_ids = queue.popleft()
        if node == dst_tank:
            pumps = [n for n in nodes if n in topo.pumps]
            if len(pumps) != 1:
                continue  # 必须恰由一台泵驱动
            groups = [topo.pipes[pid].mutex_group for pid in pipe_ids]
            groups = [g for g in groups if g]
            if len(groups) != len(set(groups)):
                continue  # 通路内部互斥
            cand = {"pump": pumps[0],
                    "valves": [n for n in nodes if n in topo.valves],
                    "pipes": list(pipe_ids)}
            if best is None or len(cand["pipes"]) < len(best["pipes"]):
                best = cand
            continue
        for nxt, pid in adj.get(node, []):
            depth = len(nodes)
            if nxt in nodes:
                continue
            if nxt in topo.tanks and nxt != dst_tank:
                continue  # 不经过第三个舱
            if visited_depth.get(nxt, 1 << 30) <= depth:
                continue
            visited_depth[nxt] = depth
            queue.append((nxt, nodes + [nxt], pipe_ids + [pid]))
    return best


def resource_conflicts(steps) -> List[dict]:
    """计划级自检：任意两步若共享泵或互斥管路组，必须有先后确认关系
    （本模块方案严格串行，seq 即先后）；同一步内部不得自相斥。
    返回冲突列表，空列表 = 通过。"""
    problems = []
    for s in steps:
        groups = [g for g in s.get("mutex_groups", []) if g]
        if len(groups) != len(set(groups)):
            problems.append({"code": "STEP_SELF_MUTEX", "seq": s["seq"],
                             "message": f"step {s['seq']} uses two pipes of the "
                                        f"same mutex group"})
    return problems


def topo_to_dict(topo: Topology) -> dict:
    return {
        "voyage_id": topo.voyage_id,
        "tanks": [dict(name=t.name, capacity=t.capacity, min_level=t.min_level,
                       max_level=t.max_level, level=t.level, x=t.x, y=t.y,
                       z=t.z, density=t.density)
                  for t in sorted(topo.tanks.values(), key=lambda t: t.name)],
        "pumps": [dict(name=p.name, rate=p.rate)
                  for p in sorted(topo.pumps.values(), key=lambda p: p.name)],
        "valves": [dict(name=v.name)
                   for v in sorted(topo.valves.values(), key=lambda v: v.name)],
        "pipes": [dict(name=p.name, a=p.a_node, b=p.b_node,
                       mutex_group=p.mutex_group)
                  for p in sorted(topo.pipes.values(), key=lambda p: p.name)],
    }
