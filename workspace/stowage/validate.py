"""Constraint validation engine.

validate(structure, assignment, scope) -> list of violations.

assignment: {container_id: slot_id}
scope: None = full plan; or {
    "containers": set of container ids to check individually,
    "stacks":     set of stack ids whose stack rules are re-checked,
    "pairs_with": container ids whose segregation against ALL other
                  dangerous boxes is re-checked,
}
Stability (GM / trim) is always computed globally — a local move can break
global equilibrium, so "only check the moved box" is never sufficient.
"""
from collections import defaultdict
from typing import Dict, List, Optional

from .domain import Structure, euclidean
from .rules import rule_for


def _violation(code, message, containers=None, slots=None, stacks=None, extra=None):
    v = {"code": code, "message": message,
         "containers": sorted(containers or []), "slots": sorted(slots or []),
         "stacks": sorted(stacks or [])}
    if extra:
        v["detail"] = extra
    return v


def validate(structure: Structure, assignment: Dict[str, str],
             scope: Optional[dict] = None) -> List[dict]:
    violations = []
    slot_of = dict(assignment)
    cont_of_slot = {}
    for cid, sid in slot_of.items():
        if sid in cont_of_slot:
            violations.append(_violation(
                "SLOT_DOUBLE_BOOKED",
                f"slot {structure.slots[sid].code} holds two containers",
                containers=[cont_of_slot[sid], cid], slots=[sid]))
        cont_of_slot[sid] = cid
    for cid in slot_of:
        if cid not in structure.containers:
            violations.append(_violation("UNKNOWN_CONTAINER", f"unknown container {cid}",
                                         containers=[cid]))

    check_all_containers = scope is None
    containers_scope = set() if scope is None else set(scope.get("containers", []))
    stacks_scope = set() if scope is None else set(scope.get("stacks", []))
    pairs_scope = set() if scope is None else set(scope.get("pairs_with", []))

    # ---- stack rules: weight limit, no gaps, reefer plugs -----------------
    per_stack = defaultdict(list)  # stack_id -> [(tier, cid)]
    for cid, sid in slot_of.items():
        slot = structure.slots.get(sid)
        if slot is None:
            violations.append(_violation("UNKNOWN_SLOT", f"unknown slot {sid}",
                                         containers=[cid], slots=[sid]))
            continue
        per_stack[slot.stack_id].append((slot.tier, cid))

    for stack_id, items in per_stack.items():
        if not check_all_containers and stack_id not in stacks_scope:
            continue
        stack = structure.stacks[stack_id]
        items.sort()
        total_w = sum(structure.containers[c].weight for _, c in items
                      if c in structure.containers)
        if total_w > stack.max_weight + 1e-9:
            violations.append(_violation(
                "STACK_OVERWEIGHT",
                f"stack bay{stack.bay} row{stack.row} carries {total_w:.1f}t > "
                f"limit {stack.max_weight:.1f}t",
                containers=[c for _, c in items], stacks=[stack_id]))
        tiers = sorted(t for t, _ in items)
        for expected, actual in enumerate(tiers, start=1):
            if actual != expected:
                violations.append(_violation(
                    "STACK_GAP",
                    f"stack bay{stack.bay} row{stack.row} has a floating container "
                    f"at tier {actual} (gap below)",
                    containers=[c for t, c in items if t == actual],
                    stacks=[stack_id]))
                break
        if stack.reefer_plugs >= 0:
            reefers = [c for _, c in items
                       if structure.containers.get(c) and structure.containers[c].reefer]
            if len(reefers) > stack.reefer_plugs:
                violations.append(_violation(
                    "REEFER_NO_PLUG",
                    f"stack bay{stack.bay} row{stack.row} has {len(reefers)} reefer "
                    f"containers but {stack.reefer_plugs} plugs",
                    containers=reefers, stacks=[stack_id]))

    # ---- port order: nothing for an earlier port may sit on top of a box
    #      bound for a later port (would force a restow at the earlier port)
    for stack_id, items in per_stack.items():
        items.sort()
        ranks = [(structure.port_rank(structure.containers[c].pod), c)
                 for _, c in items if c in structure.containers]
        involved = {c for _, c in ranks}
        if not check_all_containers and not (involved & containers_scope) \
                and stack_id not in stacks_scope:
            continue
        for i in range(len(ranks)):
            for j in range(i + 1, len(ranks)):
                lower_rank, lower_c = ranks[i]
                upper_rank, upper_c = ranks[j]
                if upper_rank > lower_rank:
                    violations.append(_violation(
                        "PORT_ORDER",
                        f"container {upper_c} (discharged later) sits on top of "
                        f"{lower_c} (discharged earlier) -> restow required",
                        containers=[lower_c, upper_c], stacks=[stack_id]))

    # ---- dangerous goods segregation --------------------------------------
    dg = [(cid, sid) for cid, sid in slot_of.items()
          if cid in structure.containers and structure.containers[cid].hazard_class]
    for i in range(len(dg)):
        for j in range(i + 1, len(dg)):
            ca, sa = dg[i]
            cb, sb = dg[j]
            if not check_all_containers and not ({ca, cb} & pairs_scope):
                continue
            ha = structure.containers[ca].hazard_class
            hb = structure.containers[cb].hazard_class
            rule = rule_for(ha, hb)
            if rule is None:
                continue
            kind, value = rule
            slot_a, slot_b = structure.slots[sa], structure.slots[sb]
            if kind == "min_distance":
                d = euclidean(slot_a, slot_b)
                if d < value - 1e-9:
                    violations.append(_violation(
                        "SEGREGATION_DISTANCE",
                        f"{ca} (class {ha}) and {cb} (class {hb}) are {d:.1f}m "
                        f"apart, required >= {value:.1f}m",
                        containers=[ca, cb], slots=[sa, sb]))
            elif kind == "not_same_stack":
                if slot_a.stack_id == slot_b.stack_id:
                    violations.append(_violation(
                        "SEGREGATION_SAME_STACK",
                        f"{ca} (class {ha}) and {cb} (class {hb}) may not share "
                        f"a stack", containers=[ca, cb], slots=[sa, sb]))
            elif kind == "not_same_hold":
                if slot_a.stack.hold_id == slot_b.stack.hold_id:
                    violations.append(_violation(
                        "SEGREGATION_SAME_HOLD",
                        f"{ca} (class {ha}) and {cb} (class {hb}) may not share "
                        f"hold {slot_a.stack.hold_id}",
                        containers=[ca, cb], slots=[sa, sb]))

    # ---- stability: always global ------------------------------------------
    stab = stability_metrics(structure, slot_of)
    if stab["gm"] < structure.min_gm - 1e-9:
        violations.append(_violation(
            "STABILITY_GM_LOW",
            f"GM {stab['gm']:.2f}m below minimum {structure.min_gm:.2f}m "
            f"(vessel tender / top-heavy)",
            extra=stab))
    if stab["gm"] > structure.max_gm + 1e-9:
        violations.append(_violation(
            "STABILITY_GM_HIGH",
            f"GM {stab['gm']:.2f}m above maximum {structure.max_gm:.2f}m "
            f"(vessel overly stiff, excessive accelerations)",
            extra=stab))
    if abs(stab["trim"]) > structure.max_trim + 1e-9:
        violations.append(_violation(
            "STABILITY_TRIM",
            f"trim {stab['trim']:.2f}m exceeds +/-{structure.max_trim:.2f}m",
            extra=stab))
    return violations


def stability_metrics(structure: Structure, assignment: Dict[str, str]) -> dict:
    """Simplified vertical/longitudinal balance.

    GM_proxy = KM - KG where KM is a beam-derived constant; trim from the
    longitudinal moment arm. Lightship included.
    """
    km = 13.0
    ship_len = max((s.x for s in structure.stacks.values()), default=100.0) + BAY_FALLBACK
    mid = ship_len / 2.0
    total_w = structure.lightship_weight
    moment_v = structure.lightship_weight * structure.lightship_vcg
    moment_l = structure.lightship_weight * (structure.lightship_lcg - mid)
    for cid, sid in assignment.items():
        c = structure.containers.get(cid)
        slot = structure.slots.get(sid)
        if c is None or slot is None:
            continue
        total_w += c.weight
        moment_v += c.weight * slot.z
        moment_l += c.weight * (slot.stack.x - mid)
    kg = moment_v / total_w if total_w else 0.0
    lcg = mid + (moment_l / total_w if total_w else 0.0)
    # trim approximated: moment imbalance / (displacement * unit trim arm)
    trim = (lcg - mid) * total_w / max(total_w * ship_len / 100.0, 1.0)
    return {"gm": km - kg, "kg": kg, "lcg": lcg, "trim": trim,
            "displacement": total_w}


BAY_FALLBACK = 12.2
