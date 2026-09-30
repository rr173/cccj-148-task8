"""Voyage-change restow (倒箱) planner.

Given a new port sequence, find which boxes become unloadable (a later-port
box stacked above them) and produce an explicit, ordered list of shift moves
using empty slots as buffers. Locked (already loaded) boxes can be shifted in
the plan — physically it is a restow operation — but the plan is validated
with the same segregation / weight / stability rules.
"""
from collections import defaultdict

from .domain import Structure, euclidean
from .rules import rule_for
from .validate import validate


def plan_resequence(structure: Structure, assignment: dict, new_sequence: list):
    """Returns {"moves": [...], "assignment": new_assignment} or
    {"infeasible": reason}. Each move: {container, from, to, reason}."""
    rank = {p: i for i, p in enumerate(new_sequence)}

    def rank_of(cid):
        return rank.get(structure.containers[cid].pod, len(new_sequence))

    # boxes stacked above a later-port box, per stack, top-down
    per_stack = defaultdict(list)
    for cid, sid in assignment.items():
        slot = structure.slots[sid]
        per_stack[slot.stack_id].append((slot.tier, cid))

    blocked = []
    for stack_id, items in per_stack.items():
        items.sort()
        ranks = [(rank_of(c), c) for _, c in items]
        for i in range(len(ranks)):
            for j in range(i + 1, len(ranks)):
                # upper box (j) discharged later than the box below it (i)
                if ranks[j][0] > ranks[i][0]:
                    blocked.append((items[j][0], ranks[j][1]))  # (tier, upper box)
    # shift upper (earlier-port) boxes first, highest tier first
    blocked.sort(reverse=True)
    to_shift = []
    seen = set()
    for _, cid in blocked:
        if cid not in seen:
            seen.add(cid)
            to_shift.append(cid)

    if not to_shift:
        return {"moves": [], "assignment": dict(assignment)}

    work = dict(assignment)
    used = set(work.values())
    load = defaultdict(float)
    tiers = defaultdict(set)
    reefers = defaultdict(int)
    for cid, sid in work.items():
        s = structure.slots[sid]
        c = structure.containers[cid]
        load[s.stack_id] += c.weight
        tiers[s.stack_id].add(s.tier)
        if c.reefer:
            reefers[s.stack_id] += 1

    dg_boxes = [(cid, sid) for cid, sid in work.items()
                if structure.containers[cid].hazard_class]

    def remove(cid):
        sid = work.pop(cid)
        s = structure.slots[sid]
        c = structure.containers[cid]
        used.discard(sid)
        load[s.stack_id] -= c.weight
        tiers[s.stack_id].discard(s.tier)
        if c.reefer:
            reefers[s.stack_id] -= 1
        if c.hazard_class:
            dg_boxes.remove((cid, sid))
        return sid

    def fits(cid, slot):
        c = structure.containers[cid]
        st = slot.stack
        if c.reefer and reefers[st.id] >= st.reefer_plugs:
            return False
        if load[st.id] + c.weight > st.max_weight + 1e-9:
            return False
        for t in range(1, slot.tier):
            if t not in tiers[st.id]:
                return False
        my_rank = rank_of(cid)
        for oc, osid in work.items():
            os_ = structure.slots[osid]
            if os_.stack_id != st.id:
                continue
            o_rank = rank_of(oc)
            if os_.tier < slot.tier and my_rank > o_rank:
                return False
            if os_.tier > slot.tier and o_rank > my_rank:
                return False
        if c.hazard_class:
            for oc, osid in dg_boxes:
                rule = rule_for(c.hazard_class, structure.containers[oc].hazard_class)
                if rule is None:
                    continue
                kind, value = rule
                os_ = structure.slots[osid]
                if kind == "min_distance" and euclidean(slot, os_) < value - 1e-9:
                    return False
                if kind == "not_same_stack" and slot.stack_id == os_.stack_id:
                    return False
                if kind == "not_same_hold" and slot.stack.hold_id == os_.stack.hold_id:
                    return False
        return True

    def place(cid, slot):
        work[cid] = slot.id
        used.add(slot.id)
        c = structure.containers[cid]
        load[slot.stack_id] += c.weight
        tiers[slot.stack_id].add(slot.tier)
        if c.reefer:
            reefers[slot.stack_id] += 1
        if c.hazard_class:
            dg_boxes.append((cid, slot.id))

    def free_slots():
        return sorted((s for s in structure.slots.values() if s.id not in used),
                      key=lambda s: (s.tier, s.code))

    moves = []
    for cid in to_shift:
        if cid not in work:
            continue
        origin = structure.slots[work[cid]]
        # final home: a slot that keeps the new port order legal
        home = next((s for s in free_slots() if fits(cid, s)), None)
        if home is None:
            return {"infeasible": f"no legal slot for {cid} under new port sequence"}
        if home.stack_id == origin.stack_id and home.tier == origin.tier:
            continue
        # if the home is buried under other boxes, shift them to a buffer first
        for oc, osid in list(work.items()):
            os_ = structure.slots[osid]
            if os_.stack_id == home.stack_id and os_.tier >= home.tier and oc != cid:
                buf = next((s for s in free_slots()
                            if s.stack_id != home.stack_id and fits(oc, s)), None)
                if buf is None:
                    return {"infeasible": f"no buffer slot to clear {oc} out of "
                                          f"stack for {cid}"}
                remove(oc)
                place(oc, buf)
                moves.append({"container": oc, "from": os_.code, "to": buf.code,
                              "reason": f"clear slot {home.code} (buffer)"})
        old_code = origin.code
        remove(cid)
        place(cid, home)
        moves.append({"container": cid, "from": old_code, "to": home.code,
                      "reason": "restow for new port sequence"})

    violations = validate(structure, work)
    if violations:
        return {"infeasible": "restow result failed validation",
                "violations": violations}
    return {"moves": moves, "assignment": work}
