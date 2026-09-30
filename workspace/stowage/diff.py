"""Auditable plan-diff engine.

Computes the difference between two plan versions (any draft vs. the latest
published version, or any two historical versions) and categorises every
change for the approval page:

* ``slot_changes``        – per-container slot added / removed / moved
* ``segregation_changes`` – dangerous-goods pairs whose IMDG-style compliance
                            or distance changed (introduced / resolved)
* ``stability_changes``   – GM / KG / LCG / trim / displacement deltas
* ``locked_changes``      – physically loaded (confirmed) boxes: preserved /
                            moved / added / missing — rollback must leave the
                            first category as the only outcome

The ``digest`` is a SHA-256 over a *canonical* encoding of the summary, so an
approval token is cryptographically bound to one (draft version, diff content)
pair. Recomputing the digest at publish time detects any draft mutation
(including same-row in-place adjustments that do not bump the version number).
"""
import hashlib
import json
from typing import Dict, Iterable, Optional

from .domain import Structure, euclidean
from .rules import rule_for
from .validate import stability_metrics

DIGEST_ROUND = 4   # decimal places for floats inside the digest


def _r(x):
    """Round for the digest; normalise -0.0 so recomputation is stable."""
    v = round(float(x), DIGEST_ROUND)
    return 0.0 if v == 0 else v


def _slot_code(structure: Structure, slot_id: Optional[str]):
    if slot_id is None:
        return None
    slot = structure.slots.get(slot_id)
    return slot.code if slot else None


def _pair_state(structure: Structure, ca: str, cb: str, sa_id: str, sb_id: str):
    """Rule + measured distance + violation flag for one DG pair."""
    ha = structure.containers[ca].hazard_class
    hb = structure.containers[cb].hazard_class
    rule = rule_for(ha, hb)
    if rule is None:
        return None  # same class: no segregation requirement
    kind, required = rule
    sa, sb = structure.slots[sa_id], structure.slots[sb_id]
    distance = euclidean(sa, sb)
    if kind == "min_distance":
        violated = distance < required - 1e-9
    elif kind == "not_same_stack":
        violated = sa.stack_id == sb.stack_id
    else:  # not_same_hold
        violated = sa.stack.hold_id == sb.stack.hold_id
    return {"kind": kind, "required": required, "distance": distance,
            "violated": bool(violated)}


def _dg_pair_states(structure: Structure, assignment: Dict[str, str]) -> dict:
    dg = sorted(cid for cid, sid in assignment.items()
                if cid in structure.containers
                and structure.containers[cid].hazard_class
                and sid in structure.slots)
    states = {}
    for i in range(len(dg)):
        for j in range(i + 1, len(dg)):
            a, b = dg[i], dg[j]
            st = _pair_state(structure, a, b, assignment[a], assignment[b])
            if st is not None:
                states[(a, b)] = st
    return states


def _pair_record(structure, a, b, base_st, target_st, slot_of_b, slot_of_t):
    return {
        "containers": [a, b],
        "classes": {a: structure.containers[a].hazard_class,
                    b: structure.containers[b].hazard_class},
        "rule": {"kind": (target_st or base_st)["kind"],
                 "required": (target_st or base_st)["required"]},
        "from_distance": None if base_st is None else _r(base_st["distance"]),
        "to_distance": None if target_st is None else _r(target_st["distance"]),
        "from_slots": {a: _slot_code(structure, slot_of_b.get(a)),
                       b: _slot_code(structure, slot_of_b.get(b))},
        "to_slots": {a: _slot_code(structure, slot_of_t.get(a)),
                     b: _slot_code(structure, slot_of_t.get(b))},
    }


def compute_diff(structure: Structure, base_assignment: Dict[str, str],
                 target_assignment: Dict[str, str],
                 locked_ids: Iterable[str] = ()) -> dict:
    """Return the full categorized diff plus a tamper-evident digest.

    ``base_assignment=None/{}`` means the empty baseline (first publish)."""
    slot_of_b = dict(base_assignment or {})
    slot_of_t = dict(target_assignment or {})
    locked = set(locked_ids)

    # ---- 1) per-slot changes ----------------------------------------------
    slot_changes = []
    for cid in sorted(set(slot_of_b) | set(slot_of_t)):
        sb, st = slot_of_b.get(cid), slot_of_t.get(cid)
        if sb == st:
            continue
        change = "moved" if (sb and st) else ("added" if st else "removed")
        c = structure.containers.get(cid)
        slot_changes.append({
            "container_id": cid, "change": change,
            "pod": c.pod if c else None,
            "hazard_class": c.hazard_class if c else None,
            "weight": c.weight if c else None,
            "locked": cid in locked,
            "from_slot": _slot_code(structure, sb),
            "to_slot": _slot_code(structure, st),
            "from_slot_id": sb, "to_slot_id": st,
        })

    # ---- 2) dangerous-goods segregation -----------------------------------
    states_b = _dg_pair_states(structure, slot_of_b)
    states_t = _dg_pair_states(structure, slot_of_t)
    introduced, resolved, distance_changes = [], [], []
    for pair in sorted(set(states_b) | set(states_t)):
        sb_st, st_st = states_b.get(pair), states_t.get(pair)
        a, b = pair
        rec = _pair_record(structure, a, b, sb_st, st_st, slot_of_b, slot_of_t)
        if sb_st is None and st_st is not None and st_st["violated"]:
            introduced.append(rec)
        elif st_st is None and sb_st is not None and sb_st["violated"]:
            resolved.append(rec)
        elif sb_st is not None and st_st is not None:
            if st_st["violated"] and not sb_st["violated"]:
                introduced.append(rec)
            elif sb_st["violated"] and not st_st["violated"]:
                resolved.append(rec)
            if abs(st_st["distance"] - sb_st["distance"]) > 1e-9:
                distance_changes.append(
                    {**rec, "compliant": not st_st["violated"]})

    # ---- 3) stability metrics ---------------------------------------------
    metrics_b = stability_metrics(structure, slot_of_b)
    metrics_t = stability_metrics(structure, slot_of_t)
    keys = ("gm", "kg", "lcg", "trim", "displacement")
    stability_changes = {
        "from": {k: _r(metrics_b[k]) for k in keys},
        "to": {k: _r(metrics_t[k]) for k in keys},
        "delta": {k: _r(metrics_t[k] - metrics_b[k]) for k in keys},
        "limits": {"min_gm": structure.min_gm, "max_gm": structure.max_gm,
                   "max_trim": structure.max_trim},
        "gm_ok_before": structure.min_gm - 1e-9 <= metrics_b["gm"]
                        <= structure.max_gm + 1e-9,
        "gm_ok_after": structure.min_gm - 1e-9 <= metrics_t["gm"]
                       <= structure.max_gm + 1e-9,
        "trim_ok_before": abs(metrics_b["trim"]) <= structure.max_trim + 1e-9,
        "trim_ok_after": abs(metrics_t["trim"]) <= structure.max_trim + 1e-9,
    }

    # ---- 4) locked (confirmed) boxes --------------------------------------
    locked_changes = []
    for cid in sorted(locked):
        sb, st = slot_of_b.get(cid), slot_of_t.get(cid)
        if sb == st and st is not None:
            status = "preserved"
        elif sb and st:
            status = "moved"
        elif st:
            status = "added"
        else:
            status = "missing"  # locked box lost by the target plan
        locked_changes.append({
            "container_id": cid, "status": status,
            "from_slot": _slot_code(structure, sb),
            "to_slot": _slot_code(structure, st),
        })

    # ---- canonical summary + digest ---------------------------------------
    summary = {
        "slots": [[c["container_id"], c["from_slot"], c["to_slot"]]
                  for c in slot_changes],
        "segregation": {
            "introduced": [[r["containers"], r["rule"]["kind"],
                            r["rule"]["required"],
                            r["from_distance"], r["to_distance"]]
                           for r in introduced],
            "resolved": [[r["containers"], r["rule"]["kind"],
                          r["rule"]["required"],
                          r["from_distance"], r["to_distance"]]
                         for r in resolved],
        },
        "stability": {k: [stability_changes["from"][k],
                          stability_changes["to"][k]] for k in keys},
        "locked": [[c["container_id"], c["from_slot"], c["to_slot"],
                    c["status"]] for c in locked_changes],
    }
    canonical = json.dumps(summary, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    counts = {
        "slot_changes": len(slot_changes),
        "added": sum(1 for c in slot_changes if c["change"] == "added"),
        "removed": sum(1 for c in slot_changes if c["change"] == "removed"),
        "moved": sum(1 for c in slot_changes if c["change"] == "moved"),
        "segregation_introduced": len(introduced),
        "segregation_resolved": len(resolved),
        "segregation_distance_changed": len(distance_changes),
        "locked_preserved": sum(1 for c in locked_changes
                                if c["status"] == "preserved"),
        "locked_moved": sum(1 for c in locked_changes
                            if c["status"] == "moved"),
        "locked_missing": sum(1 for c in locked_changes
                              if c["status"] == "missing"),
    }
    return {
        "slot_changes": slot_changes,
        "segregation_changes": {"introduced": introduced, "resolved": resolved,
                                "distance_changes": distance_changes},
        "stability_changes": stability_changes,
        "locked_changes": locked_changes,
        "summary": summary, "digest": digest, "counts": counts,
    }
