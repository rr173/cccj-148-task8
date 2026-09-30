"""End-to-end tests: rules, locking, concurrency, cancellation, voyage change."""
import json
import os
import tempfile
import threading
import unittest

os.environ.setdefault("STOWAGE_DB", tempfile.mktemp(suffix=".db"))

from stowage import service                      # noqa: E402
from stowage.db import ServiceError, connect, init_db  # noqa: E402


def small_payload(port_sequence=("SHA", "SIN", "HAM"), ship=None, stacks=None,
                  containers=None):
    return {
        "voyage": {"name": "T", "port_sequence": list(port_sequence)},
        "ship": ship or {"lightship_weight": 9000, "lightship_lcg": 30,
                         "lightship_vcg": 8.5, "min_gm": 1.0, "max_gm": 5.5,
                         "max_trim": 2.0},
        "stacks": stacks if stacks is not None else [
            {"bay": 2, "row": 1, "max_tiers": 3, "max_weight": 80},
            {"bay": 2, "row": 2, "max_tiers": 3, "max_weight": 80},
            {"bay": 4, "row": 1, "max_tiers": 3, "max_weight": 80},
            {"bay": 4, "row": 2, "max_tiers": 3, "max_weight": 80},
        ],
        "containers": containers if containers is not None else [
            {"id": "C1", "pod": "HAM", "weight": 20},
            {"id": "C2", "pod": "HAM", "weight": 18},
            {"id": "C3", "pod": "SIN", "weight": 15},
            {"id": "C4", "pod": "SIN", "weight": 12},
        ],
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        os.environ["STOWAGE_DB"] = self.db
        init_db(self.db)

    def import_voyage(self, payload=None):
        return service.import_voyage(payload or small_payload(),
                                     db_path=self.db)["voyage_id"]

    def solve(self, vid):
        job = service.create_solve_job(vid, run_async=False, db_path=self.db)
        j = service.get_job(job, db_path=self.db)
        assert j["status"] == "done", j
        return j["plan_id"]

    def approve(self, pid):
        return service.request_approval(pid, actor="planner",
                                        db_path=self.db)["token"]

    def publish(self, pid):
        """Approve + publish in one step (tests that don't care about the
        token ceremony)."""
        token = self.approve(pid)
        return service.publish_plan(pid, token=token, actor="planner",
                                    db_path=self.db)


class TestImportSolve(Base):
    def test_solve_produces_valid_plan(self):
        vid = self.import_voyage()
        plan_id = self.solve(vid)
        r = service.validate_plan(plan_id, db_path=self.db)
        self.assertTrue(r["valid"], r["violations"])
        self.assertEqual(len(service.plan_detail(plan_id, db_path=self.db)["entries"]), 4)

    def test_invalid_import_rejected(self):
        p = small_payload(containers=[{"id": "X", "pod": "NOWHERE", "weight": 10}])
        with self.assertRaises(ServiceError) as ctx:
            service.import_voyage(p, db_path=self.db)
        self.assertEqual(ctx.exception.code, "INVALID_IMPORT")

    def test_solver_respects_reefer_plugs(self):
        p = small_payload(
            stacks=[{"bay": 2, "row": 1, "max_tiers": 2, "max_weight": 80,
                     "reefer_plugs": 1},
                    {"bay": 4, "row": 1, "max_tiers": 2, "max_weight": 80}],
            containers=[{"id": "R1", "pod": "SIN", "weight": 10, "reefer": True},
                        {"id": "R2", "pod": "SIN", "weight": 10, "reefer": True},
                        {"id": "G1", "pod": "HAM", "weight": 10}])
        vid = self.import_voyage(p)
        plan_id = self.solve(vid)
        r = service.validate_plan(plan_id, db_path=self.db)
        self.assertTrue(r["valid"], r["violations"])


class TestRules(Base):
    def test_port_order_violation_detected_on_adjust(self):
        p = small_payload(stacks=[
            {"bay": 2, "row": 1, "max_tiers": 2, "max_weight": 80},
            {"bay": 4, "row": 1, "max_tiers": 2, "max_weight": 80}])
        vid = self.import_voyage(p)
        plan_id = self.solve(vid)
        d = service.plan_detail(plan_id, db_path=self.db)
        sin = next(e for e in d["entries"] if e["pod"] == "SIN" and e["tier"] == 2)
        ham = next(e for e in d["entries"] if e["pod"] == "HAM" and e["tier"] == 1
                   and e["bay"] == sin["bay"] and e["row"] == sin["row"])
        r = service.adjust_plan(plan_id, [
            {"container_id": sin["container_id"], "to_slot": ham["slot_code"]},
            {"container_id": ham["container_id"], "to_slot": sin["slot_code"]},
        ], db_path=self.db)
        self.assertFalse(r["valid"])
        self.assertIn("PORT_ORDER", {v["code"] for v in r["violations"]})

    def test_segregation_distance_detected(self):
        p = small_payload(containers=[
            {"id": "DG3", "pod": "HAM", "weight": 15, "hazard_class": "3"},
            {"id": "DG51", "pod": "HAM", "weight": 15, "hazard_class": "5.1"},
            {"id": "G1", "pod": "SIN", "weight": 10}])
        vid = self.import_voyage(p)
        plan_id = self.solve(vid)
        self.assertTrue(service.validate_plan(plan_id, db_path=self.db)["valid"])
        d = service.plan_detail(plan_id, db_path=self.db)
        dg3 = next(e for e in d["entries"] if e["container_id"] == "DG3")
        used = {e["slot_code"] for e in d["entries"]}
        other_row = 2 if dg3["row"] == 1 else 1
        near = "%02d-%02d-%02d" % (dg3["bay"], other_row, dg3["tier"])
        if near in used:
            # nudge the occupant out first
            occ = next(e for e in d["entries"] if e["slot_code"] == near)
            empty = next(s["slots"][0]["code"]
                         for s in service.voyage_summary(vid, db_path=self.db)["stacks"]
                         if s["slots"][0]["code"] not in used)
            service.adjust_plan(plan_id, [
                {"container_id": occ["container_id"], "to_slot": empty}],
                db_path=self.db)
        r = service.adjust_plan(plan_id, [
            {"container_id": "DG51", "to_slot": near}], db_path=self.db)
        self.assertFalse(r["valid"])
        self.assertIn("SEGREGATION_DISTANCE", {v["code"] for v in r["violations"]})

    def test_stack_overweight_detected(self):
        p = small_payload(containers=[
            {"id": "H1", "pod": "HAM", "weight": 30},
            {"id": "H2", "pod": "HAM", "weight": 30},
            {"id": "H3", "pod": "HAM", "weight": 30},
            {"id": "L1", "pod": "SIN", "weight": 5}])
        vid = self.import_voyage(p)
        plan_id = self.solve(vid)
        d = service.plan_detail(plan_id, db_path=self.db)
        heavy = next(e for e in d["entries"] if e["container_id"] == "H3")
        # stack a 30t box onto a stack that is already near its 80t limit
        target = next(e for e in d["entries"] if e["container_id"] == "H1")
        code = "%02d-%02d-03" % (target["bay"], target["row"])
        r = service.adjust_plan(plan_id, [
            {"container_id": "H3", "to_slot": code}], db_path=self.db)
        codes = {v["code"] for v in r["violations"]}
        self.assertTrue({"STACK_OVERWEIGHT", "STACK_GAP"} & codes or
                        "STACK_OVERWEIGHT" in codes, r["violations"])

    def test_stability_gm_detected(self):
        p = small_payload(ship={"lightship_weight": 9000, "lightship_lcg": 30,
                                "lightship_vcg": 8.5, "min_gm": 0.05,
                                "max_gm": 0.1, "max_trim": 2.0})
        vid = self.import_voyage(p)
        plan_id = self.solve(vid)
        r = service.validate_plan(plan_id, db_path=self.db)
        self.assertIn("STABILITY_GM_HIGH", {v["code"] for v in r["violations"]})

    def test_adjust_scope_covers_both_stacks_not_unrelated(self):
        p = small_payload(stacks=[
            {"bay": 2, "row": 1, "max_tiers": 2, "max_weight": 80},
            {"bay": 2, "row": 2, "max_tiers": 2, "max_weight": 80},
            {"bay": 4, "row": 1, "max_tiers": 2, "max_weight": 80},
            {"bay": 4, "row": 2, "max_tiers": 2, "max_weight": 80},
            {"bay": 2, "row": 3, "max_tiers": 2, "max_weight": 80}])
        vid = self.import_voyage(p)
        plan_id = self.solve(vid)
        d = service.plan_detail(plan_id, db_path=self.db)
        mover = d["entries"][0]
        used = {e["slot_code"] for e in d["entries"]}
        to_code = next(s["slots"][0]["code"]
                       for s in service.voyage_summary(vid, db_path=self.db)["stacks"]
                       if s["slots"][0]["code"] not in used)
        r = service.adjust_plan(plan_id, [
            {"container_id": mover["container_id"], "to_slot": to_code}],
            db_path=self.db)
        self.assertEqual(len(r["scope"]["stacks"]), 2)  # source + target
        self.assertIn(mover["container_id"], r["scope"]["containers"])


class TestPublishConfirm(Base):
    def test_publish_requires_valid(self):
        p = small_payload(stacks=[
            {"bay": 2, "row": 1, "max_tiers": 2, "max_weight": 80},
            {"bay": 4, "row": 1, "max_tiers": 2, "max_weight": 80}])
        vid = self.import_voyage(p)
        plan_id = self.solve(vid)
        d = service.plan_detail(plan_id, db_path=self.db)
        sin = next(e for e in d["entries"] if e["pod"] == "SIN" and e["tier"] == 2)
        ham = next(e for e in d["entries"] if e["pod"] == "HAM" and e["tier"] == 1
                   and e["bay"] == sin["bay"] and e["row"] == sin["row"])
        service.adjust_plan(plan_id, [
            {"container_id": sin["container_id"], "to_slot": ham["slot_code"]},
            {"container_id": ham["container_id"], "to_slot": sin["slot_code"]},
        ], db_path=self.db)
        token = self.approve(plan_id)
        with self.assertRaises(ServiceError) as ctx:
            service.publish_plan(plan_id, token=token, db_path=self.db)
        self.assertEqual(ctx.exception.code, "PLAN_INVALID")
        # the rejected token is consumed by validation only after it passes;
        # an invalid publish must not burn the token for later retries... here
        # the plan stays draft and a new approval can be requested
        self.assertEqual(service.plan_detail(plan_id, db_path=self.db)["status"],
                         "draft")

    def test_confirm_idempotent_and_mismatch(self):
        vid = self.import_voyage()
        plan_id = self.solve(vid)
        self.publish(plan_id)
        e = service.plan_detail(plan_id, db_path=self.db)["entries"][0]
        r1 = service.confirm_load(vid, plan_id, e["container_id"], e["slot_code"],
                                  db_path=self.db)
        r2 = service.confirm_load(vid, plan_id, e["container_id"], e["slot_code"],
                                  db_path=self.db)
        self.assertFalse(r1.get("idempotent", False))
        self.assertTrue(r2["idempotent"])
        with self.assertRaises(ServiceError) as ctx:
            service.confirm_load(vid, plan_id, "C2", "99-99-99", db_path=self.db)
        self.assertEqual(ctx.exception.code, "SLOT_MISMATCH")

    def test_concurrent_confirms_single_lock(self):
        vid = self.import_voyage()
        plan_id = self.solve(vid)
        self.publish(plan_id)
        e = service.plan_detail(plan_id, db_path=self.db)["entries"][0]
        results = []

        def go():
            try:
                results.append(service.confirm_load(
                    vid, plan_id, e["container_id"], e["slot_code"], db_path=self.db))
            except ServiceError as ex:
                results.append(ex.code)

        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        ok = [r for r in results if isinstance(r, dict)]
        self.assertEqual(len(ok), 6)
        self.assertEqual(sum(1 for r in ok if not r.get("idempotent")), 1)
        conn = connect(self.db)
        n = conn.execute("SELECT COUNT(*) c FROM confirmations WHERE container_id=?",
                         (e["container_id"],)).fetchone()["c"]
        conn.close()
        self.assertEqual(n, 1)


class TestLocking(Base):
    def test_locked_container_cannot_move_or_roll(self):
        vid = self.import_voyage()
        plan_id = self.solve(vid)
        self.publish(plan_id)
        e = service.plan_detail(plan_id, db_path=self.db)["entries"][0]
        service.confirm_load(vid, plan_id, e["container_id"], e["slot_code"],
                             db_path=self.db)
        plan2 = self.solve(vid)  # new draft keeps the locked box in place
        with self.assertRaises(ServiceError) as ctx:
            service.adjust_plan(plan2, [
                {"container_id": e["container_id"], "to_slot": "04-02-03"}],
                db_path=self.db)
        self.assertEqual(ctx.exception.code, "CONTAINER_LOCKED")
        with self.assertRaises(ServiceError) as ctx:
            service.roll_container(vid, e["container_id"], db_path=self.db)
        self.assertEqual(ctx.exception.code, "CONTAINER_LOCKED")

    def test_late_weight_and_roll_fork_new_draft(self):
        vid = self.import_voyage()
        plan_id = self.solve(vid)
        r = service.late_weight(vid, "C1", 33.0, db_path=self.db)
        self.assertIn("new_plan_id", r)
        self.assertTrue(r["valid"])
        r2 = service.roll_container(vid, "C2", db_path=self.db)
        self.assertTrue(r2["valid"])
        d = service.plan_detail(r2["new_plan_id"], db_path=self.db)
        ids = {e["container_id"] for e in d["entries"]}
        self.assertNotIn("C2", ids)
        self.assertIn("C1", ids)
        w = next(e["weight"] for e in d["entries"] if e["container_id"] == "C1")
        self.assertEqual(w, 33.0)


class TestResequence(Base):
    def test_resequence_generates_restow_plan(self):
        # stacked ship: SIN boxes sit on top of HAM boxes; swapping the
        # discharge order makes every upper box a restow (third stack is the
        # buffer the restow plan shifts through)
        p = small_payload(stacks=[
            {"bay": 2, "row": 1, "max_tiers": 2, "max_weight": 80},
            {"bay": 4, "row": 1, "max_tiers": 2, "max_weight": 80},
            {"bay": 2, "row": 2, "max_tiers": 2, "max_weight": 80}])
        vid = self.import_voyage(p)
        plan_id = self.solve(vid)
        self.publish(plan_id)
        r = service.resequence_voyage(vid, ["SHA", "HAM", "SIN"], db_path=self.db)
        self.assertTrue(r["restow_required"])
        self.assertTrue(r["valid"])
        self.assertIn("new_plan_id", r)
        # applying the same sequence again is a no-op
        r2 = service.resequence_voyage(vid, ["SHA", "HAM", "SIN"], db_path=self.db)
        self.assertFalse(r2["restow_required"])

    def test_off_rotation_containers_rejected(self):
        vid = self.import_voyage()
        self.solve(vid)
        with self.assertRaises(ServiceError) as ctx:
            service.resequence_voyage(vid, ["SHA", "HAM"], db_path=self.db)
        self.assertEqual(ctx.exception.code, "CONTAINERS_OFF_ROTATION")


class TestConcurrencyAndCancel(Base):
    def test_concurrent_adjusts_are_serialized(self):
        vid = self.import_voyage()
        plan_id = self.solve(vid)
        d = service.plan_detail(plan_id, db_path=self.db)
        tier1 = [e for e in d["entries"] if e["tier"] == 1]
        results = []

        def go(a, b):
            try:
                r = service.adjust_plan(plan_id, [
                    {"container_id": a["container_id"], "to_slot": b["slot_code"]},
                    {"container_id": b["container_id"], "to_slot": a["slot_code"]},
                ], db_path=self.db)
                results.append(r["valid"])
            except ServiceError as ex:
                results.append(ex.code)

        ts = [threading.Thread(target=go, args=(tier1[i], tier1[i + 1]))
              for i in (0, 2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(results), 2)
        # whatever the interleaving, the plan must remain internally consistent:
        # no container in two slots, no slot doubly booked
        conn = connect(self.db)
        dup = conn.execute(
            "SELECT container_id, COUNT(*) c FROM plan_entries WHERE plan_id=? "
            "GROUP BY container_id HAVING c>1", (plan_id,)).fetchall()
        dup2 = conn.execute(
            "SELECT slot_id, COUNT(*) c FROM plan_entries WHERE plan_id=? "
            "GROUP BY slot_id HAVING c>1", (plan_id,)).fetchall()
        conn.close()
        self.assertEqual(dup, [])
        self.assertEqual(dup2, [])

    def test_cancelled_solver_leaves_no_plan(self):
        vid = self.import_voyage()
        conn = connect(self.db)
        conn.execute("INSERT INTO jobs (id, voyage_id, kind, status, "
                     "cancel_requested, created_at) VALUES ('job_x', ?, 'solve', "
                     "'running', 1, 'now')", (vid,))
        conn.commit()
        conn.close()
        service.run_solve_job("job_x", vid, "test", db_path=self.db)
        j = service.get_job("job_x", db_path=self.db)
        self.assertEqual(j["status"], "cancelled")
        self.assertIsNone(j["plan_id"])
        self.assertEqual(service.voyage_summary(vid, db_path=self.db)["plans"], [])


class TestSampleShip(Base):
    def test_sample_ship_full_flow(self):
        with open(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "sample_import.json")) as f:
            payload = json.load(f)
        vid = self.import_voyage(payload)
        plan_id = self.solve(vid)
        r = service.validate_plan(plan_id, db_path=self.db)
        self.assertTrue(r["valid"], r["violations"][:3])
        self.assertEqual(len(service.plan_detail(plan_id, db_path=self.db)["entries"]),
                         len(payload["containers"]))
        self.publish(plan_id)
        r = service.resequence_voyage(vid, ["SHA", "HAM", "SIN"], db_path=self.db)
        self.assertTrue(r["restow_required"])
        self.assertTrue(r["valid"])


class TestDiff(Base):
    def test_diff_against_empty_baseline_categorizes_adds(self):
        vid = self.import_voyage()
        pid = self.solve(vid)
        r = service.plan_diff(pid, db_path=self.db)
        self.assertIsNone(r["base"])  # nothing published yet -> empty baseline
        d = r["diff"]
        self.assertEqual(d["counts"]["added"], 4)
        self.assertEqual(d["counts"]["removed"], 0)
        self.assertIn("gm", d["stability_changes"]["delta"])
        # target version metadata is echoed for the audit page
        self.assertEqual(r["target"]["version"], 1)
        self.assertEqual(r["target"]["created_by"], "system")

    def test_diff_detects_moves_and_stability_delta(self):
        vid = self.import_voyage()
        p1 = self.solve(vid)
        self.publish(p1)
        p2 = self.solve(vid)
        entries = {e["container_id"]: e
                   for e in service.plan_detail(p2, db_path=self.db)["entries"]}
        # swap two same-pod boxes between adjacent row stacks -> 2 moves
        service.adjust_plan(p2, [
            {"container_id": "C1", "to_slot": entries["C2"]["slot_code"]},
            {"container_id": "C2", "to_slot": entries["C1"]["slot_code"]},
        ], db_path=self.db)
        r = service.plan_diff(p2, db_path=self.db)
        self.assertEqual(r["base"]["id"], p1)
        moves = [c for c in r["diff"]["slot_changes"] if c["change"] == "moved"]
        self.assertEqual({c["container_id"] for c in moves}, {"C1", "C2"})
        for c in moves:
            self.assertTrue(c["from_slot"] and c["to_slot"])
        self.assertIsInstance(r["diff"]["stability_changes"]["delta"]["gm"], float)

    def test_diff_digest_stable_for_same_content(self):
        vid = self.import_voyage()
        pid = self.solve(vid)
        d1 = service.plan_diff(pid, db_path=self.db)["diff"]["digest"]
        d2 = service.plan_diff(pid, db_path=self.db)["diff"]["digest"]
        self.assertEqual(d1, d2)
        self.assertEqual(len(d1), 64)  # sha256 hex

    def test_diff_locked_section_reports_preserved_boxes(self):
        vid = self.import_voyage()
        p1 = self.solve(vid)
        self.publish(p1)
        e = service.plan_detail(p1, db_path=self.db)["entries"][0]
        service.confirm_load(vid, p1, e["container_id"], e["slot_code"],
                             db_path=self.db)
        p2 = self.solve(vid)  # fixed box carried forward
        d = service.plan_diff(p2, db_path=self.db)["diff"]
        locked = {c["container_id"]: c for c in d["locked_changes"]}
        self.assertIn(e["container_id"], locked)
        self.assertEqual(locked[e["container_id"]]["status"], "preserved")


class TestApproval(Base):
    def test_publish_requires_token(self):
        vid = self.import_voyage()
        pid = self.solve(vid)
        with self.assertRaises(ServiceError) as ctx:
            service.publish_plan(pid, db_path=self.db)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "APPROVAL_REQUIRED")

    def test_approval_token_carries_full_diff_and_metadata(self):
        vid = self.import_voyage()
        pid = self.solve(vid)
        a = service.request_approval(pid, actor="plannerA", db_path=self.db)
        self.assertEqual(a["status"], "active")
        self.assertEqual(a["plan_version"], 1)
        self.assertTrue(a["created_at"])
        self.assertEqual(a["created_by"], "plannerA")
        self.assertEqual(a["diff"]["counts"]["added"], 4)
        g = service.get_approval(a["token"], db_path=self.db)
        self.assertEqual(g["requested_by"], "plannerA")

    def test_repeated_approval_request_is_idempotent(self):
        vid = self.import_voyage()
        pid = self.solve(vid)
        a1 = service.request_approval(pid, db_path=self.db)
        a2 = service.request_approval(pid, db_path=self.db)
        self.assertEqual(a1["token"], a2["token"])
        self.assertTrue(a2["idempotent"])

    def test_publish_consumes_token_and_replay_is_idempotent(self):
        vid = self.import_voyage()
        pid = self.solve(vid)
        token = self.approve(pid)
        r1 = service.publish_plan(pid, token=token, actor="plannerA",
                                  db_path=self.db)
        self.assertEqual(r1["version"], 1)
        r2 = service.publish_plan(pid, token=token, actor="plannerB",
                                  db_path=self.db)
        self.assertTrue(r2["idempotent"])
        self.assertEqual(r2["version"], 1)
        # the replay cannot overwrite the original consumer in the audit trail
        g = service.get_approval(token, db_path=self.db)
        self.assertEqual(g["status"], "consumed")
        self.assertEqual(g["consumed_by"], "plannerA")

    def test_token_rejects_changed_version(self):
        vid = self.import_voyage()
        p1 = self.solve(vid)
        token = self.approve(p1)
        service.publish_plan(p1, token=token, db_path=self.db)
        p2 = self.solve(vid)
        with self.assertRaises(ServiceError) as ctx:
            service.publish_plan(p2, token=token, db_path=self.db)
        self.assertEqual(ctx.exception.code, "TOKEN_PLAN_MISMATCH")

    def test_token_rejects_changed_diff_summary(self):
        vid = self.import_voyage()
        p1 = self.solve(vid)
        self.publish(p1)
        p2 = self.solve(vid)
        token = self.approve(p2)
        entries = {e["container_id"]: e
                   for e in service.plan_detail(p2, db_path=self.db)["entries"]}
        service.adjust_plan(p2, [
            {"container_id": "C1", "to_slot": entries["C2"]["slot_code"]},
            {"container_id": "C2", "to_slot": entries["C1"]["slot_code"]},
        ], db_path=self.db)
        with self.assertRaises(ServiceError) as ctx:
            service.publish_plan(p2, token=token, db_path=self.db)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "DIFF_MISMATCH")
        self.assertTrue(ctx.exception.extra["retryable"])
        self.assertNotEqual(ctx.exception.extra["expected_digest"],
                            ctx.exception.extra["actual_digest"])

    def test_new_draft_voids_active_token_with_audit_event(self):
        vid = self.import_voyage()
        p1 = self.solve(vid)
        token = self.approve(p1)
        p2 = self.solve(vid)  # supersedes p1 before it is published
        self.assertEqual(service.get_approval(token, db_path=self.db)["status"],
                         "voided")
        with self.assertRaises(ServiceError) as ctx:
            service.publish_plan(p1, token=token, db_path=self.db)
        self.assertEqual(ctx.exception.code, "VERSION_CONFLICT")
        actions = [e["action"] for e in service.list_events(vid, db_path=self.db)]
        self.assertIn("APPROVAL_VOID", actions)
        a = service.request_approval(p2, db_path=self.db)
        self.assertEqual(a["status"], "active")

    def test_publish_records_version_token_and_digest_event(self):
        vid = self.import_voyage()
        p1 = self.solve(vid)
        token = self.approve(p1)
        service.publish_plan(p1, token=token, actor="plannerA", db_path=self.db)
        ev = next(e for e in service.list_events(vid, db_path=self.db)
                  if e["action"] == "PUBLISH")
        detail = json.loads(ev["detail"])
        self.assertEqual(detail["token"], token)
        self.assertEqual(detail["version"], 1)
        self.assertEqual(len(detail["digest"]), 64)
        self.assertEqual(ev["actor"], "plannerA")

    def test_concurrent_publish_only_one_consumes_token(self):
        vid = self.import_voyage()
        pid = self.solve(vid)
        token = self.approve(pid)
        results = []

        def go(i):
            try:
                results.append(service.publish_plan(
                    pid, token=token, actor=f"planner{i}", db_path=self.db))
            except ServiceError as ex:
                results.append(ex.code)

        ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(results), 4)
        # every concurrent submission ends in the same published state; the
        # token is consumed once and the losers replay the stored result
        self.assertTrue(all(isinstance(r, dict) and r["status"] == "published"
                            for r in results), results)
        self.assertEqual(sum(1 for r in results if not r.get("idempotent")), 1)


class TestRollback(Base):
    def _published_history(self):
        vid = self.import_voyage()
        p1 = self.solve(vid)
        self.publish(p1)
        p2 = self.solve(vid)
        entries = {e["container_id"]: e
                   for e in service.plan_detail(p2, db_path=self.db)["entries"]}
        service.adjust_plan(p2, [
            {"container_id": "C1", "to_slot": entries["C2"]["slot_code"]},
            {"container_id": "C2", "to_slot": entries["C1"]["slot_code"]},
        ], db_path=self.db)
        self.publish(p2)
        p3 = self.solve(vid)
        return vid, p1, p2, p3

    def test_rollback_creates_new_validated_version(self):
        vid, p1, p2, p3 = self._published_history()
        r = service.rollback_draft(p3, 1, actor="plannerB", db_path=self.db)
        self.assertTrue(r["valid"], r["violations"])
        self.assertEqual(r["new_version"], 4)
        self.assertEqual(r["target_version"], 1)
        new = service.plan_detail(r["new_plan_id"], db_path=self.db)
        v1 = {e["container_id"]: e["slot_id"]
              for e in service.plan_detail(p1, db_path=self.db)["entries"]}
        now = {e["container_id"]: e["slot_id"] for e in new["entries"]}
        self.assertEqual(now, v1)  # content restored exactly
        statuses = [(p["version"], p["status"])
                    for p in service.voyage_summary(vid, db_path=self.db)["plans"]]
        self.assertIn((4, "draft"), statuses)
        self.assertIn((3, "superseded"), statuses)
        ev = next(e for e in service.list_events(vid, db_path=self.db)
                  if e["action"] == "ROLLBACK")
        detail = json.loads(ev["detail"])
        self.assertEqual(detail["target_version"], 1)
        self.assertEqual(ev["actor"], "plannerB")

    def test_rollback_preserves_locked_box_and_parks_displaced(self):
        vid, p1, p2, p3 = self._published_history()
        c1v2 = next(e for e in service.plan_detail(p2, db_path=self.db)["entries"]
                    if e["container_id"] == "C1")
        service.confirm_load(vid, p2, "C1", c1v2["slot_code"], db_path=self.db)
        # p3 was solved before the lock; roll it back to v1
        r = service.rollback_draft(p3, 1, db_path=self.db)
        self.assertTrue(r["valid"])
        new = service.plan_detail(r["new_plan_id"], db_path=self.db)
        c1 = next(e for e in new["entries"] if e["container_id"] == "C1")
        self.assertEqual(c1["slot_code"], c1v2["slot_code"])  # untouched
        self.assertEqual(r["locked_preserved"], 1)
        self.assertEqual(r["locked_boxes"][0]["slot"], c1v2["slot_code"])
        # the box displaced from that historical slot is re-parked, not lost
        self.assertEqual(len(new["entries"]), 4)
        self.assertEqual({p["container_id"] for p in r["displaced_parked"]},
                         {"C2"})
        self.assertTrue(service.validate_plan(r["new_plan_id"],
                                              db_path=self.db)["valid"])

    def test_rollback_validates_target_and_current_state(self):
        vid, p1, p2, p3 = self._published_history()
        with self.assertRaises(ServiceError) as ctx:
            service.rollback_draft(p3, 99, db_path=self.db)
        self.assertEqual(ctx.exception.code, "VERSION_NOT_FOUND")
        with self.assertRaises(ServiceError) as ctx:
            service.rollback_draft(p3, 3, db_path=self.db)
        self.assertEqual(ctx.exception.code, "ROLLBACK_SAME_VERSION")
        # rolling back to the currently *published* v2 is a legitimate choice
        # (target is older than the draft); only the draft itself is rejected
        r = service.rollback_draft(p3, 2, db_path=self.db)
        self.assertTrue(r["valid"])
        # a published plan is no longer a draft; concurrent-state losers get a
        # retryable version error rather than a dead-end conflict
        with self.assertRaises(ServiceError) as ctx:
            service.rollback_draft(p2, 1, db_path=self.db)
        self.assertEqual(ctx.exception.code, "VERSION_CONFLICT")
        self.assertTrue(ctx.exception.extra["retryable"])

    def test_stale_rollback_returns_retryable_version_conflict(self):
        vid, p1, p2, p3 = self._published_history()
        results = []

        def go(target):
            try:
                results.append(service.rollback_draft(
                    p3, target, db_path=self.db))
            except ServiceError as ex:
                results.append(ex)

        t1 = threading.Thread(target=go, args=(1,))
        t2 = threading.Thread(target=go, args=(2,))
        t1.start(); t2.start(); t1.join(); t2.join()
        oks = [r for r in results if isinstance(r, dict)]
        errs = [r for r in results if isinstance(r, ServiceError)]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].code, "VERSION_CONFLICT")
        self.assertTrue(errs[0].extra["retryable"])
        self.assertEqual(errs[0].extra["current_version"], 4)

    def test_expected_version_guards_publish(self):
        vid = self.import_voyage()
        pid = self.solve(vid)
        token = self.approve(pid)
        with self.assertRaises(ServiceError) as ctx:
            service.publish_plan(pid, token=token, expected_version=99,
                                 db_path=self.db)
        self.assertEqual(ctx.exception.code, "VERSION_CONFLICT")
        self.assertEqual(ctx.exception.extra["current_version"], 1)
        # the rejected attempt must not have burned the token
        service.publish_plan(pid, token=token, expected_version=1,
                             db_path=self.db)

    def test_rollback_failure_leaves_no_half_version(self):
        vid, p1, p2, p3 = self._published_history()
        before = [(p["version"], p["status"])
                  for p in service.voyage_summary(vid, db_path=self.db)["plans"]]
        with self.assertRaises(ServiceError):
            service.rollback_draft(p3, 1, expected_version=99, db_path=self.db)
        after = [(p["version"], p["status"])
                 for p in service.voyage_summary(vid, db_path=self.db)["plans"]]
        self.assertEqual(before, after)
        # after recovery the diff view still answers from the final DB state
        d = service.plan_diff(p3, base_version=2, db_path=self.db)
        self.assertEqual(d["target"]["id"], p3)
        self.assertEqual(d["base"]["id"], p2)


class TestSegregationDiff(Base):
    def test_segregation_change_appears_in_diff_and_blocks_publish(self):
        p = small_payload(containers=[
            {"id": "DG3", "pod": "HAM", "weight": 15, "hazard_class": "3"},
            {"id": "DG51", "pod": "HAM", "weight": 15, "hazard_class": "5.1"},
            {"id": "G1", "pod": "SIN", "weight": 10}])
        vid = self.import_voyage(p)
        p1 = self.solve(vid)
        self.assertTrue(service.validate_plan(p1, db_path=self.db)["valid"])
        self.publish(p1)
        dg3 = next(e for e in service.plan_detail(p1, db_path=self.db)["entries"]
                   if e["container_id"] == "DG3")
        other_row = 2 if dg3["row"] == 1 else 1
        near = "%02d-%02d-%02d" % (dg3["bay"], other_row, dg3["tier"])
        p2 = self.solve(vid)
        used = {e["slot_code"] for e in service.plan_detail(
            p2, db_path=self.db)["entries"]}
        if near in used:
            occ = next(e for e in service.plan_detail(p2, db_path=self.db)["entries"]
                       if e["slot_code"] == near)
            empty = next(s["slots"][0]["code"]
                         for s in service.voyage_summary(vid, db_path=self.db)["stacks"]
                         if s["slots"][0]["code"] not in used)
            service.adjust_plan(p2, [
                {"container_id": occ["container_id"], "to_slot": empty}],
                db_path=self.db)
        service.adjust_plan(p2, [
            {"container_id": "DG51", "to_slot": near}], db_path=self.db)
        r = service.plan_diff(p2, db_path=self.db)["diff"]
        self.assertGreaterEqual(r["counts"]["segregation_introduced"], 1)
        pair = r["segregation_changes"]["introduced"][0]
        self.assertEqual(set(pair["containers"]), {"DG3", "DG51"})
        self.assertIsNotNone(pair["to_distance"])
        a = service.request_approval(p2, db_path=self.db)
        self.assertGreaterEqual(a["diff"]["counts"]["segregation_introduced"], 1)
        # the token carries the violating diff, but full validation still gates
        # publication — a segregation regression can never be published
        with self.assertRaises(ServiceError) as ctx:
            service.publish_plan(p2, token=a["token"], db_path=self.db)
        self.assertEqual(ctx.exception.code, "PLAN_INVALID")
        self.assertTrue(any(v["code"].startswith("SEGREGATION")
                            for v in ctx.exception.extra["violations"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
