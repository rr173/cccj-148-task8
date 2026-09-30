"""Service layer: all state-changing operations, each atomic.

Concurrency model
-----------------
* Every multi-step write runs in one IMMEDIATE transaction -> a crash or a
  cancelled solver leaves nothing half-written.
* SQLite serializes writers; optimistic checks (plan still draft? container
  still unlocked?) run inside the same transaction, so two planners cannot
  both win. The loser gets 409.
* UNIQUE(plan_id, slot_id) and UNIQUE(plan_id, container_id) make "one box in
  two places" impossible at the database level.
* confirmations has UNIQUE(voyage_id, container_id): a repeated confirm is a
  no-op returning the original record (idempotent).
"""
import json
import threading
import time
from datetime import datetime, timezone

from .db import ServiceError, connect
from .diff import compute_diff
from .domain import build_structure, new_id, parse_import
from .rehandle import plan_resequence
from .solver import SolverCancelled, solve
from .validate import stability_metrics, validate


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _row(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()


def _rows(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def _get_voyage(conn, voyage_id):
    v = _row(conn, "SELECT * FROM voyages WHERE id=?", (voyage_id,))
    if not v:
        raise ServiceError(404, "VOYAGE_NOT_FOUND", f"voyage {voyage_id} not found")
    return v


def get_structure(conn, voyage_id):
    v = _get_voyage(conn, voyage_id)
    stacks = _rows(conn, "SELECT * FROM stacks WHERE voyage_id=?", (voyage_id,))
    slots = _rows(conn, "SELECT * FROM slots WHERE voyage_id=?", (voyage_id,))
    containers = _rows(conn, "SELECT * FROM containers WHERE voyage_id=?", (voyage_id,))
    return build_structure(v, stacks, slots, containers)


def _plan_assignment(conn, plan_id):
    return {r["container_id"]: r["slot_id"]
            for r in _rows(conn, "SELECT container_id, slot_id FROM plan_entries "
                                 "WHERE plan_id=?", (plan_id,))}


def _get_plan(conn, plan_id):
    p = _row(conn, "SELECT * FROM plans WHERE id=?", (plan_id,))
    if not p:
        raise ServiceError(404, "PLAN_NOT_FOUND", f"plan {plan_id} not found")
    return p


def _next_version(conn, voyage_id):
    r = _row(conn, "SELECT COALESCE(MAX(version),0)+1 AS v FROM plans WHERE voyage_id=?",
             (voyage_id,))
    return r["v"]


def _create_plan(conn, voyage_id, assignment, valid, note, actor, status="draft"):
    """Insert a plan + entries; supersedes previous drafts. Caller holds txn."""
    if status == "draft":
        conn.execute("UPDATE plans SET status='superseded' WHERE voyage_id=? "
                     "AND status='draft'", (voyage_id,))
        # any approval minted against the old draft can no longer be trusted:
        # void it so a stale token can never publish a newer plan by mistake
        old = _rows(conn, "SELECT id, requested_by FROM approval_tokens "
                          "WHERE voyage_id=? AND status='active'", (voyage_id,))
        if old:
            conn.execute("UPDATE approval_tokens SET status='voided', "
                         "void_reason='draft superseded by new version' "
                         "WHERE voyage_id=? AND status='active'", (voyage_id,))
            for t in old:
                _log(conn, voyage_id, actor, "APPROVAL_VOID",
                     {"token": t["id"], "reason": "draft superseded"})
    plan_id = new_id("pln")
    conn.execute(
        "INSERT INTO plans (id, voyage_id, version, status, valid, note, "
        "created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (plan_id, voyage_id, _next_version(conn, voyage_id), status,
         1 if valid else 0, note, actor, now()))
    for cid, sid in assignment.items():
        conn.execute("INSERT INTO plan_entries (plan_id, container_id, slot_id) "
                     "VALUES (?,?,?)", (plan_id, cid, sid))
    return plan_id


def _log(conn, voyage_id, actor, action, detail=None):
    conn.execute("INSERT INTO events (voyage_id, actor, action, detail, created_at) "
                 "VALUES (?,?,?,?,?)",
                 (voyage_id, actor, action, json.dumps(detail or {}), now()))


def _latest_plan(conn, voyage_id):
    """The current working plan: newest version among draft/published."""
    return _row(conn, "SELECT * FROM plans WHERE voyage_id=? AND status IN "
                      "('published','draft') ORDER BY version DESC LIMIT 1",
                (voyage_id,))


def _latest_published(conn, voyage_id):
    return _row(conn, "SELECT * FROM plans WHERE voyage_id=? AND status="
                      "'published' ORDER BY version DESC LIMIT 1",
                (voyage_id,))


def _locked_ids(conn, voyage_id):
    return {r["id"] for r in _rows(
        conn, "SELECT id FROM containers WHERE voyage_id=? AND locked=1 "
              "AND discharged=0", (voyage_id,))}


def _plan_meta(p):
    return {"id": p["id"], "version": p["version"], "status": p["status"],
            "valid": bool(p["valid"]), "created_by": p["created_by"],
            "created_at": p["created_at"], "note": p["note"]}


def _build_diff(conn, voyage_id, target_plan, base_plan=None, structure=None):
    if structure is None:
        structure = get_structure(conn, voyage_id)
    target_assignment = _plan_assignment(conn, target_plan["id"])
    base_assignment = {}
    if base_plan is None:
        base_plan = _latest_published(conn, voyage_id)
    if base_plan is not None:
        base_assignment = _plan_assignment(conn, base_plan["id"])
    diff = compute_diff(structure, base_assignment, target_assignment,
                        locked_ids=_locked_ids(conn, voyage_id))
    return diff, base_plan


def _version_conflict(message, **extra):
    return ServiceError(409, "VERSION_CONFLICT", message,
                        {"retryable": True, **extra})


# --------------------------------------------------------------------------- #
# import
# --------------------------------------------------------------------------- #

def import_voyage(payload, actor="system", db_path=None):
    data = parse_import(payload)
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        vid = new_id("voy")
        ship = data["ship"]
        conn.execute(
            "INSERT INTO voyages (id, name, port_sequence, lightship_weight, "
            "lightship_lcg, lightship_vcg, min_gm, max_gm, max_trim, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (vid, data["voyage"]["name"], json.dumps(data["voyage"]["port_sequence"]),
             ship["lightship_weight"], ship["lightship_lcg"], ship["lightship_vcg"],
             ship["min_gm"], ship["max_gm"], ship["max_trim"], now()))
        for s in data["stacks"]:
            conn.execute(
                "INSERT INTO stacks (id, voyage_id, bay, row, max_tiers, max_weight, "
                "on_deck, reefer_plugs, x, y, z0, hold_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (s["id"], vid, s["bay"], s["row"], s["max_tiers"], s["max_weight"],
                 1 if s["on_deck"] else 0, s["reefer_plugs"], s["x"], s["y"], s["z0"],
                 s["hold_id"]))
        for s in data["slots"]:
            conn.execute(
                "INSERT INTO slots (id, voyage_id, stack_id, tier, code, z) "
                "VALUES (?,?,?,?,?,?)",
                (s["id"], vid, s["stack_id"], s["tier"], s["code"], s["z"]))
        for c in data["containers"]:
            conn.execute(
                "INSERT INTO containers (id, voyage_id, weight, pod, hazard_class, "
                "reefer, length_ft, arrived) VALUES (?,?,?,?,?,?,?,?)",
                (c["id"], vid, c["weight"], c["pod"], c["hazard_class"],
                 1 if c["reefer"] else 0, c["length_ft"], 1 if c["arrived"] else 0))
        _log(conn, vid, actor, "IMPORT",
             {"stacks": len(data["stacks"]), "containers": len(data["containers"])})
        conn.commit()
        return {"voyage_id": vid, "stacks": len(data["stacks"]),
                "slots": len(data["slots"]), "containers": len(data["containers"])}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# solver job
# --------------------------------------------------------------------------- #

def create_solve_job(voyage_id, actor="system", db_path=None, run_async=True):
    conn = connect(db_path)
    try:
        _get_voyage(conn, voyage_id)
        job_id = new_id("job")
        conn.execute("INSERT INTO jobs (id, voyage_id, kind, status, created_at) "
                     "VALUES (?,?,?,?,?)", (job_id, voyage_id, "solve", "running", now()))
        conn.commit()
    finally:
        conn.close()
    if run_async:
        t = threading.Thread(target=run_solve_job, args=(job_id, voyage_id, actor, db_path),
                             daemon=True)
        t.start()
    else:
        run_solve_job(job_id, voyage_id, actor, db_path)
    return job_id


def run_solve_job(job_id, voyage_id, actor, db_path=None):
    conn = connect(db_path)
    started = time.time()
    try:
        structure = get_structure(conn, voyage_id)
        # locked boxes keep their confirmed physical slots
        fixed = {}
        locked = _rows(conn, "SELECT c.id, f.slot_id FROM containers c "
                             "JOIN confirmations f ON f.container_id=c.id "
                             "WHERE c.voyage_id=? AND c.locked=1 AND c.discharged=0",
                       (voyage_id,))
        for r in locked:
            fixed[r["id"]] = r["slot_id"]
        to_place = [c.id for c in structure.containers.values()
                    if c.arrived and not c.locked and not c.discharged]

        def should_cancel():
            r = _row(conn, "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,))
            return bool(r and r["cancel_requested"])

        try:
            result = solve(structure, fixed, to_place, should_cancel=should_cancel)
        except SolverCancelled:
            conn.execute("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=?",
                         (now(), job_id))
            conn.commit()
            _log(conn, voyage_id, actor, "SOLVE_CANCELLED", {"job": job_id})
            conn.commit()
            return

        assignment = result["assignment"]
        conn.execute("BEGIN IMMEDIATE")
        jr = _row(conn, "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,))
        if jr and jr["cancel_requested"]:
            conn.rollback()
            conn.execute("UPDATE jobs SET status='cancelled', finished_at=? WHERE id=?",
                         (now(), job_id))
            conn.commit()
            return
        plan_id = _create_plan(conn, voyage_id, assignment, result["valid"],
                               note=f"solver job {job_id}; unplaced: "
                                    f"{len(result['unplaced'])}", actor=actor)
        stats = {"placed": len(assignment), "unplaced": result["unplaced"],
                 "valid": result["valid"],
                 "violations": len(result["violations"]),
                 "duration_s": round(time.time() - started, 3)}
        conn.execute("UPDATE jobs SET status='done', plan_id=?, stats=?, finished_at=? "
                     "WHERE id=?", (plan_id, json.dumps(stats), now(), job_id))
        _log(conn, voyage_id, actor, "SOLVE_DONE", {"job": job_id, "plan": plan_id,
                                                    **stats})
        conn.commit()
    except Exception as exc:  # never leave a zombie job
        conn.rollback()
        conn.execute("UPDATE jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                     (str(exc), now(), job_id))
        conn.commit()
    finally:
        conn.close()


def cancel_job(job_id, db_path=None):
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        j = _row(conn, "SELECT * FROM jobs WHERE id=?", (job_id,))
        if not j:
            raise ServiceError(404, "JOB_NOT_FOUND", f"job {job_id} not found")
        if j["status"] != "running":
            raise ServiceError(409, "JOB_NOT_RUNNING",
                               f"job is {j['status']}, cannot cancel")
        conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
        conn.commit()
        return {"job_id": job_id, "cancel_requested": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_job(job_id, db_path=None):
    conn = connect(db_path)
    try:
        j = _row(conn, "SELECT * FROM jobs WHERE id=?", (job_id,))
        if not j:
            raise ServiceError(404, "JOB_NOT_FOUND", f"job {job_id} not found")
        out = dict(j)
        if out.get("stats"):
            out["stats"] = json.loads(out["stats"])
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# plans: read / adjust / validate / publish
# --------------------------------------------------------------------------- #

def plan_detail(plan_id, db_path=None):
    conn = connect(db_path)
    try:
        p = _get_plan(conn, plan_id)
        structure = get_structure(conn, p["voyage_id"])
        assignment = _plan_assignment(conn, plan_id)
        entries = []
        for cid, sid in assignment.items():
            c = structure.containers.get(cid)
            slot = structure.slots.get(sid)
            if not c or not slot:
                continue
            entries.append({
                "container_id": cid, "slot_id": sid, "slot_code": slot.code,
                "bay": slot.stack.bay, "row": slot.stack.row, "tier": slot.tier,
                "on_deck": slot.stack.on_deck, "weight": c.weight, "pod": c.pod,
                "hazard_class": c.hazard_class, "reefer": c.reefer,
                "locked": c.locked,
            })
        metrics = stability_metrics(structure, assignment)
        tok = _row(conn, "SELECT id, status, digest, requested_by, created_at, "
                         "consumed_at FROM approval_tokens WHERE plan_id=? "
                         "ORDER BY created_at DESC LIMIT 1", (plan_id,))
        approval = None
        if tok:
            approval = {"token": tok["id"], "status": tok["status"],
                        "digest": tok["digest"], "requested_by": tok["requested_by"],
                        "created_at": tok["created_at"],
                        "consumed_at": tok["consumed_at"]}
        return {"id": p["id"], "voyage_id": p["voyage_id"], "version": p["version"],
                "status": p["status"], "valid": bool(p["valid"]), "note": p["note"],
                "created_by": p["created_by"], "created_at": p["created_at"],
                "entries": sorted(entries, key=lambda e: e["slot_code"]),
                "metrics": metrics, "approval": approval}
    finally:
        conn.close()


def validate_plan(plan_id, db_path=None):
    conn = connect(db_path)
    try:
        p = _get_plan(conn, plan_id)
        structure = get_structure(conn, p["voyage_id"])
        assignment = _plan_assignment(conn, plan_id)
        violations = validate(structure, assignment)
        return {"plan_id": plan_id, "valid": len(violations) == 0,
                "violations": violations,
                "metrics": stability_metrics(structure, assignment)}
    finally:
        conn.close()


def adjust_plan(plan_id, moves, actor="planner", db_path=None):
    """Apply manual moves to a DRAFT plan, then re-validate everything the
    moves can affect: both source and target stacks, every moved box, its
    segregation against all other dangerous boxes, and global stability."""
    if not moves:
        raise ServiceError(422, "EMPTY_MOVES", "no moves supplied")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        p = _get_plan(conn, plan_id)
        if p["status"] != "draft":
            raise ServiceError(409, "PLAN_NOT_DRAFT",
                               f"plan is {p['status']}; only drafts can be adjusted")
        structure = get_structure(conn, p["voyage_id"])
        assignment = _plan_assignment(conn, plan_id)

        by_code = {s.code: s for s in structure.slots.values()}
        targets = {}
        for m in moves:
            cid = m.get("container_id")
            code = m.get("to_slot")
            if cid not in structure.containers:
                raise ServiceError(404, "CONTAINER_NOT_FOUND", f"unknown container {cid}")
            if structure.containers[cid].locked:
                raise ServiceError(409, "CONTAINER_LOCKED",
                                   f"{cid} is loaded/locked and cannot be replanned")
            if code not in by_code:
                raise ServiceError(404, "SLOT_NOT_FOUND", f"unknown slot {code}")
            if code in targets:
                raise ServiceError(422, "DUPLICATE_TARGET",
                                   f"two moves target slot {code}")
            targets[code] = cid

        moved = [m["container_id"] for m in moves]
        # a target slot occupied by a box that is not itself moving -> conflict
        incoming = {cid: by_code[code].id for code, cid in targets.items()}
        for cid, sid in assignment.items():
            if cid in incoming:
                continue
            if sid in incoming.values():
                raise ServiceError(409, "SLOT_OCCUPIED",
                                   f"target slot occupied by {cid}, which is not "
                                   f"part of this adjustment")

        old_stacks = {structure.slots[assignment[c]].stack_id
                      for c in moved if c in assignment}
        new_assignment = dict(assignment)
        for cid, sid in incoming.items():
            new_assignment[cid] = sid
        new_stacks = {structure.slots[sid].stack_id for sid in incoming.values()}

        scope = {"containers": set(moved),
                 "stacks": old_stacks | new_stacks,
                 "pairs_with": set(moved)}
        violations = validate(structure, new_assignment, scope=scope)

        # delete-then-insert so simultaneous swaps never transiently violate
        # UNIQUE(plan_id, slot_id)
        for cid in moved:
            conn.execute("DELETE FROM plan_entries WHERE plan_id=? AND "
                         "container_id=?", (plan_id, cid))
        for cid, sid in incoming.items():
            conn.execute("INSERT INTO plan_entries (plan_id, container_id, slot_id) "
                         "VALUES (?,?,?)", (plan_id, cid, sid))
        conn.execute("UPDATE plans SET valid=? WHERE id=?",
                     (0 if violations else 1, plan_id))
        _log(conn, p["voyage_id"], actor, "ADJUST",
             {"plan": plan_id, "moves": moves, "valid": not violations})
        conn.commit()
        return {"plan_id": plan_id, "valid": len(violations) == 0,
                "violations": violations,
                "scope": {"containers": sorted(scope["containers"]),
                          "stacks": sorted(scope["stacks"])}}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def request_approval(plan_id, actor="planner", db_path=None):
    """Mint a one-time approval token bound to (draft version, diff digest).

    Re-requesting for the same unchanged draft returns the still-active token
    (idempotent). If the draft moved (newer version or different diff content),
    any older active token is voided and a fresh one is issued.
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        p = _get_plan(conn, plan_id)
        if p["status"] == "superseded":
            latest = _latest_plan(conn, p["voyage_id"])
            raise _version_conflict(
                f"plan v{p['version']} was superseded by a newer draft; "
                "request approval for the current draft",
                current_version=latest["version"] if latest else None)
        if p["status"] != "draft":
            latest = _latest_plan(conn, p["voyage_id"])
            raise _version_conflict(
                f"plan v{p['version']} is {p['status']}; another planner "
                "changed it while your approval request was waiting",
                current_version=latest["version"] if latest else None,
                current_plan_id=latest["id"] if latest else None,
                current_status=p["status"])
        structure = get_structure(conn, p["voyage_id"])
        diff, base = _build_diff(conn, p["voyage_id"], p, structure=structure)
        base_meta = _plan_meta(base) if base else None

        # idempotent: unchanged draft + unchanged diff -> hand back the token
        tok = _row(conn, "SELECT * FROM approval_tokens WHERE plan_id=? AND "
                         "status='active' ORDER BY created_at DESC LIMIT 1",
                   (plan_id,))
        if tok and tok["plan_version"] == p["version"] \
                and tok["digest"] == diff["digest"]:
            conn.rollback()
            return {"token": tok["id"], "status": "active",
                    "plan_id": plan_id, "plan_version": p["version"],
                    "digest": diff["digest"], "base": base_meta,
                    "diff": diff, "created_by": tok["requested_by"],
                    "created_at": tok["created_at"], "idempotent": True}

        if tok:  # draft content changed under the old token
            conn.execute("UPDATE approval_tokens SET status='voided', "
                         "void_reason='draft changed before approval' WHERE id=?",
                         (tok["id"],))
            _log(conn, p["voyage_id"], actor, "APPROVAL_VOID",
                 {"token": tok["id"], "plan": plan_id,
                  "reason": "draft changed before approval"})

        token_id = new_id("apr")
        conn.execute(
            "INSERT INTO approval_tokens (id, voyage_id, plan_id, plan_version, "
            "digest, base_plan_id, status, requested_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (token_id, p["voyage_id"], plan_id, p["version"], diff["digest"],
             base["id"] if base else None, "active", actor, now()))
        _log(conn, p["voyage_id"], actor, "APPROVAL_REQUEST",
             {"token": token_id, "plan": plan_id, "version": p["version"],
              "digest": diff["digest"], "base_version": base_meta["version"]
              if base_meta else None, "counts": diff["counts"]})
        conn.commit()
        return {"token": token_id, "status": "active", "plan_id": plan_id,
                "plan_version": p["version"], "digest": diff["digest"],
                "base": base_meta, "diff": diff, "created_by": actor,
                "created_at": now(), "idempotent": False}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_approval(token_id, db_path=None):
    conn = connect(db_path)
    try:
        t = _row(conn, "SELECT * FROM approval_tokens WHERE id=?", (token_id,))
        if not t:
            raise ServiceError(404, "TOKEN_NOT_FOUND",
                               f"approval token {token_id} not found")
        p = _get_plan(conn, t["plan_id"])
        out = {"token": t["id"], "status": t["status"], "voyage_id": t["voyage_id"],
               "plan_id": t["plan_id"], "plan_version": t["plan_version"],
               "digest": t["digest"], "requested_by": t["requested_by"],
               "created_by": t["requested_by"],
               "created_at": t["created_at"], "consumed_at": t["consumed_at"],
               "consumed_by": t["consumed_by"], "void_reason": t["void_reason"],
               "current_plan_status": p["status"],
               "current_plan_version": p["version"]}
        if t["result"]:
            out["result"] = json.loads(t["result"])
        return out
    finally:
        conn.close()


def list_approvals(voyage_id, db_path=None):
    conn = connect(db_path)
    try:
        _get_voyage(conn, voyage_id)
        rows = _rows(conn, "SELECT * FROM approval_tokens WHERE voyage_id=? "
                           "ORDER BY created_at DESC LIMIT 100", (voyage_id,))
        return [{"token": r["id"], "status": r["status"], "plan_id": r["plan_id"],
                 "plan_version": r["plan_version"], "digest": r["digest"],
                 "created_by": r["requested_by"], "created_at": r["created_at"],
                 "consumed_at": r["consumed_at"], "consumed_by": r["consumed_by"],
                 "void_reason": r["void_reason"]} for r in rows]
    finally:
        conn.close()


def publish_plan(plan_id, token=None, actor="planner", expected_version=None,
                 db_path=None):
    """Publish a draft. A one-time approval token is mandatory; it is consumed
    inside this transaction. The diff digest is recomputed from current rows,
    so any draft mutation after approval is rejected. Re-submitting the same
    (already consumed) token replays the original publish result (idempotent)."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        p = _get_plan(conn, plan_id)

        if p["status"] == "published" and not token:
            # back-compat: publishing the already-published plan is idempotent
            conn.rollback()
            return {"plan_id": plan_id, "status": "published",
                    "version": p["version"], "idempotent": True}

        if not token:
            raise ServiceError(409, "APPROVAL_REQUIRED",
                               "publishing requires a one-time approval token; "
                               "POST /api/plans/{id}/approvals first")
        t = _row(conn, "SELECT * FROM approval_tokens WHERE id=?", (token,))
        if not t:
            raise ServiceError(404, "TOKEN_NOT_FOUND",
                               f"approval token {token} not found")
        if t["plan_id"] != plan_id:
            raise ServiceError(409, "TOKEN_PLAN_MISMATCH",
                               "token was issued for a different plan")
        # a stale version must never overwrite a newer draft: a voided token
        # against a superseded plan is reported as a retryable version error
        if p["status"] == "superseded":
            latest = _latest_plan(conn, p["voyage_id"])
            raise _version_conflict(
                f"draft v{p['version']} was superseded; approve and publish "
                f"the current draft v{latest['version']}",
                current_version=latest["version"],
                current_plan_id=latest["id"],
                token_status=t["status"])
        if t["status"] == "consumed":
            # idempotent replay: same token, same publish outcome
            conn.rollback()
            result = json.loads(t["result"]) if t["result"] else \
                {"plan_id": plan_id, "status": "published"}
            return {**result, "idempotent": True, "token": token}
        if t["status"] == "voided":
            raise ServiceError(409, "TOKEN_VOIDED",
                               "token is no longer valid: "
                               + (t["void_reason"] or "voided"),
                               {"retryable": True})
        if p["status"] == "published":
            raise ServiceError(409, "PLAN_ALREADY_PUBLISHED",
                               "a newer version was published meanwhile; request "
                               "approval for the current draft",
                               {"retryable": True, "current_version": p["version"]})
        if p["status"] != "draft":
            raise _version_conflict(
                f"plan is {p['status']}; the draft this token approved no "
                "longer exists", current_plan_status=p["status"])
        if expected_version is not None and int(expected_version) != p["version"]:
            raise _version_conflict(
                f"expected draft v{expected_version} but found v{p['version']}",
                expected_version=expected_version, current_version=p["version"])
        if t["plan_version"] != p["version"]:
            raise _version_conflict(
                f"token approved v{t['plan_version']} but draft is now "
                f"v{p['version']}", expected_version=t["plan_version"],
                current_version=p["version"])

        # recompute the exact diff the token was bound to
        base_plan = _row(conn, "SELECT * FROM plans WHERE id=?",
                         (t["base_plan_id"],)) if t["base_plan_id"] else None
        diff, _ = _build_diff(conn, p["voyage_id"], p, base_plan=base_plan)
        if diff["digest"] != t["digest"]:
            raise ServiceError(
                409, "DIFF_MISMATCH",
                "draft changed after approval: diff digest no longer matches "
                "the token; request a new approval",
                {"retryable": True, "expected_digest": t["digest"],
                 "actual_digest": diff["digest"]})

        structure = get_structure(conn, p["voyage_id"])
        assignment = _plan_assignment(conn, plan_id)
        violations = validate(structure, assignment)
        if violations:
            raise ServiceError(422, "PLAN_INVALID",
                               "plan has violations and cannot be published",
                               {"violations": violations})
        conn.execute("UPDATE plans SET status='history' WHERE voyage_id=? "
                     "AND status='published'", (p["voyage_id"],))
        conn.execute("UPDATE plans SET status='published', valid=1 WHERE id=?",
                     (plan_id,))
        result = {"plan_id": plan_id, "status": "published",
                  "version": p["version"]}
        conn.execute("UPDATE approval_tokens SET status='consumed', "
                     "consumed_at=?, consumed_by=?, result=? WHERE id=?",
                     (now(), actor, json.dumps(result), token))
        _log(conn, p["voyage_id"], actor, "PUBLISH",
             {"plan": plan_id, "version": p["version"], "token": token,
              "digest": t["digest"], "counts": diff["counts"]})
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rollback_draft(plan_id, target_version, actor="planner", expected_version=None,
                   db_path=None):
    """Roll an unpublished draft back to a historical version.

    Locked / confirmed boxes keep their physical slots: the historical
    assignment is taken as the starting point, confirmed slots are overlaid,
    and displaced unlocked boxes are parked in free slots. The resulting draft
    passes a full-vessel revalidation; a brand-new version and a ROLLBACK event
    are produced. Everything is one IMMEDIATE transaction — a failure leaves
    the old draft and every version exactly as they were.
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        p = _get_plan(conn, plan_id)
        if p["status"] == "superseded":
            latest = _latest_plan(conn, p["voyage_id"])
            raise _version_conflict(
                f"draft v{p['version']} was superseded; roll back the current "
                f"draft v{latest['version']}",
                current_version=latest["version"], current_plan_id=latest["id"])
        if expected_version is not None and int(expected_version) != p["version"]:
            raise _version_conflict(
                f"expected draft v{expected_version} but found v{p['version']}",
                expected_version=expected_version, current_version=p["version"])
        if p["status"] != "draft":
            # The write lock was just acquired: a non-draft state here means a
            # concurrent publish won the race — the client must retry against
            # the new current draft, so this stays a retryable version error.
            latest = _latest_plan(conn, p["voyage_id"])
            raise _version_conflict(
                f"plan v{p['version']} is {p['status']}; another planner "
                "changed it while your rollback was waiting",
                current_version=latest["version"] if latest else None,
                current_plan_id=latest["id"] if latest else None,
                current_status=p["status"])
        target = _row(conn, "SELECT * FROM plans WHERE voyage_id=? AND version=?",
                      (p["voyage_id"], int(target_version)))
        if not target:
            raise ServiceError(404, "VERSION_NOT_FOUND",
                               f"version v{target_version} not found")
        if target["id"] == plan_id:
            raise ServiceError(422, "ROLLBACK_SAME_VERSION",
                               "target version is the current draft")
        if target["version"] >= p["version"]:
            raise ServiceError(422, "ROLLBACK_TARGET_NOT_HISTORY",
                               f"v{target['version']} is not older than the "
                               f"current draft v{p['version']}")

        voyage_id = p["voyage_id"]
        structure = get_structure(conn, voyage_id)
        history = _plan_assignment(conn, target["id"])
        # arrived boxes only: rolled (un-arrived) boxes stay out
        history = {cid: sid for cid, sid in history.items()
                   if cid in structure.containers
                   and structure.containers[cid].arrived
                   and sid in structure.slots}

        # confirmed physical positions (discharged boxes are no longer aboard)
        fixed = {r["container_id"]: r["slot_id"]
                 for r in _rows(
                     conn, "SELECT c.id AS container_id, f.slot_id AS slot_id "
                           "FROM confirmations f JOIN containers c "
                           "ON c.id=f.container_id WHERE f.voyage_id=? "
                           "AND c.locked=1 AND c.discharged=0", (voyage_id,))
                 if r["slot_id"] in structure.slots}

        # 1) start from history, drop boxes displaced by a confirmed slot
        used_fixed = set(fixed.values())
        work = {cid: sid for cid, sid in history.items()
                if sid not in used_fixed}
        displaced = sorted(cid for cid, sid in history.items()
                           if sid in used_fixed and cid not in fixed)
        # 2) overlay the fixed positions (injective: one confirmation per box,
        #    one box per slot) — a fixed box missing from history is re-added
        for cid, sid in fixed.items():
            work[cid] = sid
            if cid in displaced:
                displaced.remove(cid)
        # 3) park every displaced unlocked box in a legal free slot
        #    (gravity-aware: lowest feasible tier; final word is full validate)
        parked = []
        for cid in displaced:
            c = structure.containers[cid]
            occupied_stacks = {}
            for oc, osid in work.items():
                osl = structure.slots[osid]
                occupied_stacks.setdefault(osl.stack_id, {})[osl.tier] = oc
            free = sorted((s for s in structure.slots.values()
                           if s.id not in set(work.values())),
                          key=lambda s: (s.z, s.code))
            home = None
            for slot in free:
                tiers = occupied_stacks.get(slot.stack_id, {})
                if any(t not in tiers for t in range(1, slot.tier)):
                    continue  # would float
                stack_w = sum(structure.containers[oc].weight
                              for oc in tiers.values())
                if stack_w + c.weight > slot.stack.max_weight + 1e-9:
                    continue
                if c.reefer:
                    reefers = sum(1 for oc in tiers.values()
                                  if structure.containers[oc].reefer)
                    if reefers >= slot.stack.reefer_plugs:
                        continue
                home = slot
                break
            if home is None:
                raise ServiceError(422, "ROLLBACK_NO_BUFFER",
                                   f"cannot park displaced container {cid}: no "
                                   "feasible free slot while preserving locked "
                                   "boxes", {"container_id": cid})
            work[cid] = home.id
            parked.append({"container_id": cid,
                           "from_slot": structure.slots[history[cid]].code,
                           "to_slot": home.code})

        # full-vessel validation of the rolled-back stowage
        violations = validate(structure, work)
        new_plan_id = _create_plan(
            conn, voyage_id, work, len(violations) == 0,
            f"rollback to v{target['version']} (locked boxes preserved)", actor)
        new_version = _row(conn, "SELECT version FROM plans WHERE id=?",
                           (new_plan_id,))["version"]
        _log(conn, voyage_id, actor, "ROLLBACK",
             {"from_plan": plan_id, "from_version": p["version"],
              "target_version": target["version"], "new_plan": new_plan_id,
              "new_version": new_version,
              "locked_preserved": len(fixed), "displaced_parked": len(parked),
              "valid": not violations})
        conn.commit()
        return {"new_plan_id": new_plan_id, "new_version": new_version,
                "target_version": target["version"],
                "valid": len(violations) == 0, "violations": violations,
                "locked_preserved": len(fixed),
                "locked_boxes": [{"container_id": cid,
                                  "slot": structure.slots[sid].code}
                                 for cid, sid in sorted(fixed.items())],
                "displaced_parked": parked}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def plan_diff(plan_id, base_version=None, db_path=None):
    """Categorized diff of any draft (or plan) vs. the latest published plan,
    or vs. an explicitly named historical version."""
    conn = connect(db_path)
    try:
        target = _get_plan(conn, plan_id)
        base = None
        if base_version is not None:
            base = _row(conn, "SELECT * FROM plans WHERE voyage_id=? AND version=?",
                        (target["voyage_id"], int(base_version)))
            if not base:
                raise ServiceError(404, "VERSION_NOT_FOUND",
                                   f"base version v{base_version} not found")
        diff, base = _build_diff(conn, target["voyage_id"], target, base_plan=base)
        return {"voyage_id": target["voyage_id"],
                "target": _plan_meta(target),
                "base": _plan_meta(base) if base else None,
                "diff": diff}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# loading confirmation / discharge
# --------------------------------------------------------------------------- #

def confirm_load(voyage_id, plan_id, container_id, slot_code, actor="operator",
                 db_path=None):
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get_voyage(conn, voyage_id)
        c = _row(conn, "SELECT * FROM containers WHERE id=? AND voyage_id=?",
                 (container_id, voyage_id))
        if not c:
            raise ServiceError(404, "CONTAINER_NOT_FOUND",
                               f"unknown container {container_id}")
        # idempotent: a repeated confirm of an already-loaded box returns the
        # original record instead of double-booking anything
        existing = _row(conn, "SELECT * FROM confirmations WHERE voyage_id=? "
                              "AND container_id=?", (voyage_id, container_id))
        if existing:
            conn.rollback()
            return {"confirmation_id": existing["id"], "container_id": container_id,
                    "slot_id": existing["slot_id"], "locked": True,
                    "idempotent": True}
        if c["discharged"]:
            raise ServiceError(409, "ALREADY_DISCHARGED",
                               f"{container_id} was already discharged")
        p = _get_plan(conn, plan_id)
        if p["voyage_id"] != voyage_id or p["status"] != "published":
            raise ServiceError(409, "PLAN_NOT_PUBLISHED",
                               "loading must confirm against the published plan")
        entry = _row(conn, "SELECT * FROM plan_entries WHERE plan_id=? "
                           "AND container_id=?", (plan_id, container_id))
        if not entry:
            raise ServiceError(409, "NOT_IN_PLAN",
                               f"{container_id} is not in the published plan")
        slot = _row(conn, "SELECT * FROM slots WHERE id=?", (entry["slot_id"],))
        if slot["code"] != slot_code:
            raise ServiceError(409, "SLOT_MISMATCH",
                               f"plan says {slot['code']}, crane reports {slot_code}")
        clash = _row(conn, "SELECT f.id FROM confirmations f JOIN containers c2 "
                           "ON c2.id=f.container_id WHERE f.voyage_id=? AND "
                           "f.slot_id=? AND c2.discharged=0",
                     (voyage_id, entry["slot_id"]))
        if clash:
            raise ServiceError(409, "SLOT_OCCUPIED",
                               f"slot {slot_code} already holds a loaded container")
        cid = new_id("cnf")
        conn.execute("INSERT INTO confirmations (id, voyage_id, plan_id, "
                     "container_id, slot_id, actor, created_at) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (cid, voyage_id, plan_id, container_id, entry["slot_id"],
                      actor, now()))
        conn.execute("UPDATE containers SET locked=1 WHERE id=?", (container_id,))
        _log(conn, voyage_id, actor, "CONFIRM_LOAD",
             {"container": container_id, "slot": slot_code, "plan": plan_id})
        conn.commit()
        return {"confirmation_id": cid, "container_id": container_id,
                "slot_id": entry["slot_id"], "locked": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def discharge_container(voyage_id, container_id, actor="operator", db_path=None):
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        c = _row(conn, "SELECT * FROM containers WHERE id=? AND voyage_id=?",
                 (container_id, voyage_id))
        if not c:
            raise ServiceError(404, "CONTAINER_NOT_FOUND",
                               f"unknown container {container_id}")
        conn.execute("UPDATE containers SET discharged=1, locked=0 WHERE id=?",
                     (container_id,))
        _log(conn, voyage_id, actor, "DISCHARGE", {"container": container_id})
        conn.commit()
        return {"container_id": container_id, "discharged": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# late weight / roll / resequence
# --------------------------------------------------------------------------- #

def _fork_plan_with(conn, voyage_id, assignment, valid, note, actor):
    return _create_plan(conn, voyage_id, assignment, valid, note, actor)


def late_weight(voyage_id, container_id, new_weight, actor="planner", db_path=None):
    """Late weight update. Only the not-yet-locked part of the plan may be
    re-arranged; locked boxes stay where the crane put them."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get_voyage(conn, voyage_id)
        c = _row(conn, "SELECT * FROM containers WHERE id=? AND voyage_id=?",
                 (container_id, voyage_id))
        if not c:
            raise ServiceError(404, "CONTAINER_NOT_FOUND",
                               f"unknown container {container_id}")
        conn.execute("UPDATE containers SET weight=? WHERE id=?",
                     (float(new_weight), container_id))
        structure = get_structure(conn, voyage_id)
        latest = _latest_plan(conn, voyage_id)
        result = {"container_id": container_id, "new_weight": float(new_weight),
                  "locked": bool(c["locked"])}
        if latest:
            assignment = _plan_assignment(conn, latest["id"])
            violations = validate(structure, assignment)
            if not c["locked"]:
                # unlocked: fork a fresh draft so the change is re-validated
                # and can be re-solved; locked entries are carried over as-is
                plan_id = _fork_plan_with(
                    conn, voyage_id, assignment, len(violations) == 0,
                    f"late weight update {container_id} -> {new_weight}t", actor)
                result["new_plan_id"] = plan_id
            result["valid"] = len(violations) == 0
            result["violations"] = violations
        _log(conn, voyage_id, actor, "LATE_WEIGHT",
             {"container": container_id, "weight": new_weight,
              "locked": bool(c["locked"])})
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def roll_container(voyage_id, container_id, actor="planner", db_path=None):
    """临时甩箱: remove a box from the voyage. Locked boxes cannot be rolled —
    they are physically on board."""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get_voyage(conn, voyage_id)
        c = _row(conn, "SELECT * FROM containers WHERE id=? AND voyage_id=?",
                 (container_id, voyage_id))
        if not c:
            raise ServiceError(404, "CONTAINER_NOT_FOUND",
                               f"unknown container {container_id}")
        if c["locked"]:
            raise ServiceError(409, "CONTAINER_LOCKED",
                               f"{container_id} is already loaded; cannot roll")
        conn.execute("UPDATE containers SET arrived=0 WHERE id=?", (container_id,))
        latest = _latest_plan(conn, voyage_id)
        result = {"container_id": container_id, "rolled": True}
        if latest:
            assignment = _plan_assignment(conn, latest["id"])
            assignment.pop(container_id, None)
            structure = get_structure(conn, voyage_id)
            violations = validate(structure, assignment)
            plan_id = _fork_plan_with(conn, voyage_id, assignment,
                                      len(violations) == 0,
                                      f"rolled {container_id}", actor)
            result["new_plan_id"] = plan_id
            result["valid"] = len(violations) == 0
            result["violations"] = violations
        _log(conn, voyage_id, actor, "ROLL", {"container": container_id})
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def resequence_voyage(voyage_id, new_sequence, actor="planner", db_path=None):
    """Mid-voyage port rotation change. Checks whether the current stowage can
    be discharged in the new order; if not, emits an explicit restow (倒箱)
    plan and a new draft. Locked boxes keep their slots unless the restow
    itself must move them (physically a restow operation)."""
    if not new_sequence:
        raise ServiceError(422, "EMPTY_SEQUENCE", "new port sequence is empty")
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _get_voyage(conn, voyage_id)
        structure = get_structure(conn, voyage_id)
        off_rotation = [c.id for c in structure.containers.values()
                        if not c.discharged and c.pod not in new_sequence]
        if off_rotation:
            raise ServiceError(422, "CONTAINERS_OFF_ROTATION",
                               "these containers' pods are not in the new "
                               "sequence; roll them first",
                               {"containers": sorted(off_rotation)})
        latest = _latest_plan(conn, voyage_id)
        if not latest:
            raise ServiceError(409, "NO_PLAN", "no plan to resequence")
        assignment = _plan_assignment(conn, latest["id"])
        outcome = plan_resequence(structure, assignment, new_sequence)
        if "infeasible" in outcome:
            raise ServiceError(422, "RESEQUENCE_INFEASIBLE", outcome["infeasible"],
                               {"violations": outcome.get("violations", [])})
        conn.execute("UPDATE voyages SET port_sequence=? WHERE id=?",
                     (json.dumps(new_sequence), voyage_id))
        result = {"voyage_id": voyage_id, "new_sequence": new_sequence,
                  "moves": outcome["moves"], "restow_required": bool(outcome["moves"])}
        if outcome["moves"]:
            violations = validate(structure, outcome["assignment"])
            plan_id = _fork_plan_with(conn, voyage_id, outcome["assignment"],
                                      len(violations) == 0,
                                      f"resequence -> {new_sequence}", actor)
            result["new_plan_id"] = plan_id
            result["valid"] = len(violations) == 0
        _log(conn, voyage_id, actor, "RESEQUENCE",
             {"sequence": new_sequence, "moves": len(outcome["moves"])})
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# queries for UI / API
# --------------------------------------------------------------------------- #

def voyage_summary(voyage_id, db_path=None):
    conn = connect(db_path)
    try:
        v = _get_voyage(conn, voyage_id)
        structure = get_structure(conn, voyage_id)
        plans = _rows(conn, "SELECT id, version, status, valid, note, created_by, "
                            "created_at FROM plans WHERE voyage_id=? "
                            "ORDER BY version DESC", (voyage_id,))
        stacks_out = []
        for st in structure.stacks.values():
            stacks_out.append({
                "id": st.id, "bay": st.bay, "row": st.row,
                "max_tiers": st.max_tiers, "max_weight": st.max_weight,
                "on_deck": st.on_deck, "reefer_plugs": st.reefer_plugs,
                "x": st.x, "y": st.y, "hold_id": st.hold_id,
                "slots": [{"id": s.id, "code": s.code, "tier": s.tier}
                          for s in sorted(
                              (sl for sl in structure.slots.values()
                               if sl.stack_id == st.id), key=lambda s: s.tier)],
            })
        containers_out = [dict(id=c.id, weight=c.weight, pod=c.pod,
                               hazard_class=c.hazard_class, reefer=c.reefer,
                               arrived=c.arrived, locked=c.locked,
                               discharged=c.discharged)
                          for c in structure.containers.values()]
        return {"id": v["id"], "name": v["name"],
                "port_sequence": json.loads(v["port_sequence"]),
                "limits": {"min_gm": v["min_gm"], "max_gm": v["max_gm"],
                           "max_trim": v["max_trim"]},
                "stacks": sorted(stacks_out, key=lambda s: (s["bay"], s["row"],
                                                            s["on_deck"])),
                "containers": sorted(containers_out, key=lambda c: c["id"]),
                "plans": [dict(p) for p in plans]}
    finally:
        conn.close()


def list_events(voyage_id, limit=100, db_path=None):
    conn = connect(db_path)
    try:
        _get_voyage(conn, voyage_id)
        rows = _rows(conn, "SELECT * FROM events WHERE voyage_id=? "
                           "ORDER BY id DESC LIMIT ?", (voyage_id, limit))
        return [dict(r) for r in rows]
    finally:
        conn.close()
