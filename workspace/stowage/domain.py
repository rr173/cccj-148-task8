"""Domain model: ship structure, containers, geometry helpers.

Coordinates (metres) are derived from the bay/row/tier grid so segregation
distances can be computed in 3D:
  x = longitudinal (bow positive), y = transverse (starboard positive),
  z = vertical (keel baseline positive).
"""
import math
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

BAY_LEN_40 = 12.2          # metres per 40ft bay
BAY_LEN_20 = 6.1
ROW_WIDTH = 2.44
TIER_HEIGHT = 2.6
DECK_BASE_Z = 11.0         # tiers >= 82 (on deck) start here
HOLD_SPAN_BAYS = 4         # bays per hold, for "same hold" segregation rules


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def slot_code(bay, row, tier):
    return f"{bay:02d}-{row:02d}-{tier:02d}"


@dataclass
class Stack:
    id: str
    bay: int
    row: int
    max_tiers: int
    max_weight: float
    on_deck: bool
    reefer_plugs: int
    x: float
    y: float
    z0: float
    hold_id: int


@dataclass
class Slot:
    id: str
    stack_id: str
    tier: int
    code: str
    z: float
    stack: Stack = field(repr=False, default=None)


@dataclass
class Container:
    id: str
    weight: float
    pod: str
    hazard_class: Optional[str] = None
    reefer: bool = False
    length_ft: int = 40
    arrived: bool = True
    locked: bool = False
    discharged: bool = False


@dataclass
class Structure:
    voyage_id: str
    port_sequence: List[str]
    stacks: Dict[str, Stack]
    slots: Dict[str, Slot]
    containers: Dict[str, Container]
    lightship_weight: float
    lightship_lcg: float
    lightship_vcg: float
    min_gm: float
    max_gm: float
    max_trim: float

    def port_rank(self, pod):
        """Lower rank = discharged earlier. Unknown ports rank last."""
        try:
            return self.port_sequence.index(pod)
        except ValueError:
            return len(self.port_sequence)


def _bay_length(bay):
    return BAY_LEN_20 if bay % 2 == 1 else BAY_LEN_40


def build_structure(voyage_row, stack_rows, slot_rows, container_rows) -> Structure:
    stacks = {}
    for r in stack_rows:
        stacks[r["id"]] = Stack(
            id=r["id"], bay=r["bay"], row=r["row"], max_tiers=r["max_tiers"],
            max_weight=r["max_weight"], on_deck=bool(r["on_deck"]),
            reefer_plugs=r["reefer_plugs"], x=r["x"], y=r["y"], z0=r["z0"],
            hold_id=r["hold_id"],
        )
    slots = {}
    for r in slot_rows:
        st = stacks[r["stack_id"]]
        slots[r["id"]] = Slot(id=r["id"], stack_id=r["stack_id"], tier=r["tier"],
                              code=r["code"], z=r["z"], stack=st)
    containers = {}
    for r in container_rows:
        containers[r["id"]] = Container(
            id=r["id"], weight=r["weight"], pod=r["pod"],
            hazard_class=r["hazard_class"], reefer=bool(r["reefer"]),
            length_ft=r["length_ft"], arrived=bool(r["arrived"]),
            locked=bool(r["locked"]), discharged=bool(r["discharged"]),
        )
    return Structure(
        voyage_id=voyage_row["id"],
        port_sequence=list(__import__("json").loads(voyage_row["port_sequence"])),
        stacks=stacks, slots=slots, containers=containers,
        lightship_weight=voyage_row["lightship_weight"],
        lightship_lcg=voyage_row["lightship_lcg"],
        lightship_vcg=voyage_row["lightship_vcg"],
        min_gm=voyage_row["min_gm"],
        max_gm=voyage_row["max_gm"], max_trim=voyage_row["max_trim"],
    )


def parse_import(payload: dict) -> dict:
    """Validate and normalize an import payload into rows ready to insert."""
    errors = []
    voyage_in = payload.get("voyage") or {}
    ship_in = payload.get("ship") or {}
    stacks_in = payload.get("stacks") or []
    containers_in = payload.get("containers") or []

    port_sequence = voyage_in.get("port_sequence") or []
    if not port_sequence:
        errors.append("voyage.port_sequence is required")

    ship = {
        "lightship_weight": float(ship_in.get("lightship_weight", 9000.0)),
        "lightship_lcg": float(ship_in.get("lightship_lcg", 80.0)),
        "lightship_vcg": float(ship_in.get("lightship_vcg", 11.0)),
        "min_gm": float(ship_in.get("min_gm", 0.5)),
        "max_gm": float(ship_in.get("max_gm", 1.5)),
        "max_trim": float(ship_in.get("max_trim", 2.0)),
    }

    stacks, slots = [], []
    seen_stack_keys = set()
    for s in stacks_in:
        try:
            bay = int(s["bay"]); row = int(s["row"])
            max_tiers = int(s.get("max_tiers", 4))
            max_weight = float(s.get("max_weight", 100.0))
            on_deck = bool(s.get("on_deck", False))
            plugs = int(s.get("reefer_plugs", 0))
        except (KeyError, TypeError, ValueError):
            errors.append(f"invalid stack entry: {s!r}")
            continue
        if (bay, row, on_deck) in seen_stack_keys:
            errors.append(f"duplicate stack bay={bay} row={row} on_deck={on_deck}")
            continue
        seen_stack_keys.add((bay, row, on_deck))
        x = float(s["x"]) if "x" in s else bay * (BAY_LEN_20 if bay % 2 == 1 else BAY_LEN_40)
        y = float(s["y"]) if "y" in s else (row - 1) * ROW_WIDTH
        z0 = float(s.get("z0", DECK_BASE_Z if on_deck else 0.5))
        hold_id = int(s.get("hold_id", (bay - 1) // HOLD_SPAN_BAYS + 1))
        sid = new_id("stk")
        stacks.append({
            "id": sid, "bay": bay, "row": row, "max_tiers": max_tiers,
            "max_weight": max_weight, "on_deck": on_deck, "reefer_plugs": plugs,
            "x": x, "y": y, "z0": z0, "hold_id": hold_id,
        })
        for tier in range(1, max_tiers + 1):
            # hold tiers: 02,04,... style low numbers; deck tiers: 82,84,...
            code_tier = tier if not on_deck else 80 + 2 * tier
            slots.append({
                "id": new_id("slt"), "stack_id": sid, "tier": tier,
                "code": slot_code(bay, row, code_tier),
                "z": z0 + (tier - 1) * TIER_HEIGHT + TIER_HEIGHT / 2,
            })

    containers = []
    seen_ids = set()
    for c in containers_in:
        cid = str(c.get("id", "")).strip()
        if not cid:
            errors.append(f"container missing id: {c!r}")
            continue
        if cid in seen_ids:
            errors.append(f"duplicate container id {cid}")
            continue
        seen_ids.add(cid)
        pod = str(c.get("pod", "")).strip()
        if pod not in port_sequence:
            errors.append(f"container {cid}: pod {pod!r} not in port_sequence")
            continue
        try:
            weight = float(c.get("weight", 0))
            if weight <= 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"container {cid}: invalid weight")
            continue
        hazard = c.get("hazard_class")
        hazard = str(hazard).strip() if hazard not in (None, "", []) else None
        containers.append({
            "id": cid, "weight": weight, "pod": pod, "hazard_class": hazard,
            "reefer": bool(c.get("reefer", False)),
            "length_ft": int(c.get("length_ft", 40)),
            "arrived": bool(c.get("arrived", True)),
        })

    if not stacks:
        errors.append("at least one stack is required")
    if errors:
        from .db import ServiceError
        raise ServiceError(422, "INVALID_IMPORT", "import payload failed validation",
                           {"errors": errors})
    return {
        "voyage": {"name": voyage_in.get("name", "voyage"), "port_sequence": port_sequence},
        "ship": ship, "stacks": stacks, "slots": slots, "containers": containers,
    }


def euclidean(a: Slot, b: Slot) -> float:
    return math.sqrt((a.stack.x - b.stack.x) ** 2 +
                     (a.stack.y - b.stack.y) ** 2 +
                     (a.z - b.z) ** 2)


def manhattan(a: Slot, b: Slot) -> float:
    return (abs(a.stack.x - b.stack.x) + abs(a.stack.y - b.stack.y) +
            abs(a.z - b.z))
