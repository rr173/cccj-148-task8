"""Greedy candidate-plan solver.

Places containers one at a time (hardest first: earliest discharge port,
dangerous, reefer, heaviest) into the cheapest feasible slot. Feasibility is
checked incrementally with the same rules the validator uses, and the final
result is fully re-validated before it is committed by the service layer.

The solver honours `should_cancel()` between placements: raising
SolverCancelled leaves NO partial plan behind (the caller rolls back).
"""
from collections import defaultdict

from .domain import Structure, euclidean
from .rules import rule_for
from .validate import validate, stability_metrics


class SolverCancelled(Exception):
    pass


def solve(structure: Structure, fixed: dict, to_place: list,
          should_cancel=None) -> dict:
    """fixed: {container_id: slot_id} already locked; to_place: container ids.

    Returns {"assignment": {...}, "unplaced": [...]}.
    """
    assignment = dict(fixed)
    used_slots = set(assignment.values())
    stack_load = defaultdict(float)
    stack_tiers = defaultdict(set)
    stack_reefers = defaultdict(int)
    for cid, sid in assignment.items():
        slot = structure.slots[sid]
        c = structure.containers[cid]
        stack_load[slot.stack_id] += c.weight
        stack_tiers[slot.stack_id].add(slot.tier)
        if c.reefer:
            stack_reefers[slot.stack_id] += 1

    dg_placed = [(cid, sid) for cid, sid in assignment.items()
                 if structure.containers[cid].hazard_class]

    def order_of(cid):
        # hardest first: latest discharge port (must end up at the bottom),
        # dangerous, reefer, heaviest
        c = structure.containers[cid]
        return (-structure.port_rank(c.pod),
                0 if c.hazard_class else 1,
                0 if c.reefer else 1,
                -c.weight, c.id)

    def feasible(cid, slot):
        c = structure.containers[cid]
        stack = slot.stack
        if c.reefer and stack_reefers[stack.id] >= stack.reefer_plugs:
            return False
        if stack_load[stack.id] + c.weight > stack.max_weight + 1e-9:
            return False
        # gravity: every tier below must be occupied
        for t in range(1, slot.tier):
            if t not in stack_tiers[stack.id]:
                return False
        # port order against boxes already in this stack: a box discharged
        # later (higher rank) may never sit ON TOP of an earlier-port box
        my_rank = structure.port_rank(c.pod)
        for other_cid, other_sid in assignment.items():
            other_slot = structure.slots[other_sid]
            if other_slot.stack_id != stack.id:
                continue
            other_rank = structure.port_rank(structure.containers[other_cid].pod)
            if other_slot.tier < slot.tier and my_rank > other_rank:
                return False  # I'd sit on an earlier-port box
            if other_slot.tier > slot.tier and other_rank > my_rank:
                return False  # a later-port box already sits on me
        # segregation against dangerous boxes already placed
        if c.hazard_class:
            for oc, osid in dg_placed:
                rule = rule_for(c.hazard_class, structure.containers[oc].hazard_class)
                if rule is None:
                    continue
                kind, value = rule
                other_slot = structure.slots[osid]
                if kind == "min_distance" and euclidean(slot, other_slot) < value - 1e-9:
                    return False
                if kind == "not_same_stack" and slot.stack_id == other_slot.stack_id:
                    return False
                if kind == "not_same_hold" and slot.stack.hold_id == other_slot.stack.hold_id:
                    return False
        return True

    def cost(cid, slot):
        c = structure.containers[cid]
        # prefer: centre of gravity low & amidships, tight stacks
        centre_y = sum(s.y for s in structure.stacks.values()) / max(len(structure.stacks), 1)
        return (slot.z * 2.0
                + abs(slot.stack.y - centre_y) * 0.3
                + (slot.tier - 1) * 1.0
                - (len(stack_tiers[slot.stack_id])) * 0.2)

    slots_by_tier = sorted(structure.slots.values(), key=lambda s: (s.tier, s.code))
    unplaced = []
    steps = 0
    for cid in sorted(to_place, key=order_of):
        if should_cancel and steps % 25 == 0 and should_cancel():
            raise SolverCancelled()
        steps += 1
        best = None
        best_cost = None
        for slot in slots_by_tier:
            if slot.id in used_slots:
                continue
            if not feasible(cid, slot):
                continue
            cst = cost(cid, slot)
            if best_cost is None or cst < best_cost:
                best, best_cost = slot, cst
        if best is None:
            unplaced.append(cid)
            continue
        assignment[cid] = best.id
        used_slots.add(best.id)
        stack_load[best.stack_id] += structure.containers[cid].weight
        stack_tiers[best.stack_id].add(best.tier)
        if structure.containers[cid].reefer:
            stack_reefers[best.stack_id] += 1
        if structure.containers[cid].hazard_class:
            dg_placed.append((cid, best.id))

    # stability gate: if the result violates GM/trim, shed highest boxes
    for _ in range(len(assignment)):
        metrics = stability_metrics(structure, assignment)
        gm_ok = structure.min_gm - 1e-9 <= metrics["gm"] <= structure.max_gm + 1e-9
        if gm_ok and abs(metrics["trim"]) <= structure.max_trim + 1e-9:
            break
        movable = [(structure.slots[s].z, cid) for cid, s in assignment.items()
                   if cid not in fixed]
        if not movable:
            break
        movable.sort(reverse=True)
        _, drop = movable[0]
        unplaced.append(drop)
        del assignment[drop]

    # final full validation of everything we produced
    violations = validate(structure, assignment)
    return {"assignment": assignment, "unplaced": sorted(unplaced),
            "valid": len(violations) == 0, "violations": violations}
