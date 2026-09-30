"""核心业务场景：正常波次 / 前置阻塞 / 舱区冲突 / 旧修订 / 暂停恢复 /
并发转交 / 幂等重复请求。
"""
import threading

from permit import service

from permit.tests.fixture import PermitCase, t


class TestNormalWave(PermitCase):
    def test_stack_order_and_progress(self):
        w = service.wave_detail("W-1")
        self.assertEqual(set(w["workable"]),
                         {"C-S1-0", "C-S2-0"})  # 只有每个堆的底层可作业
        self.assertEqual(set(w["blocked"]),
                         {"C-S1-1", "C-S1-2", "C-S2-1", "C-S2-2"})
        results = self.load_stack("PA", 1, 1)
        self.assertTrue(all(r["outcome"] == "loaded" for r in results))
        self.assertEqual(results[-1]["progress"]["executed"], 3)
        w = service.wave_detail("W-1")
        self.assertEqual(w["status"], "in_progress")
        self.assertIsNotNone(w["started_at"])
        self.load_stack("PA", 1, 2, start="10:30:00")
        w = service.wave_detail("W-1")
        self.assertEqual((w["executed"], w["total"], w["status"]),
                         (6, 6, "completed"))
        self.assertIsNotNone(w["completed_at"])
        self.assert_invariants()

    def test_permit_workable_list_hides_prereq(self):
        p = service.permit_detail("PA")
        ids = {b["container_id"] for b in p["workable"]}
        self.assertEqual(ids, {"C-S1-0", "C-S2-0"})
        blocked = {b["container_id"]: b["blocked_by"] for b in p["blocked"]}
        self.assertEqual(blocked["C-S1-2"], ["C-S1-0", "C-S1-1"])

    def test_load_all_three_stacks_completes_wave(self):
        w1 = service.wave_detail("W-1")
        self.assertEqual(w1["status"], "released")
        self.assertIsNone(w1["started_at"])  # 未开始：没有装船记录


class TestPrerequisiteBlocking(PermitCase):
    def test_top_box_blocked(self):
        self.expect_error(409, "PREREQ_BLOCKED",
                          lambda: self.scan("PA", 1, "C-S1-2"))
        self.expect_error(409, "PREREQ_BLOCKED",
                          lambda: self.scan("PA", 1, "C-S2-1"))

    def test_no_scan_record_on_block(self):
        self.expect_error(409, "PREREQ_BLOCKED",
                          lambda: self.scan("PA", 1, "C-S1-2"))
        self.assertEqual(service.list_scans("W-1")["scans"], [])
        exc = [e for e in service.list_exceptions()["exceptions"]
               if e["reason"] == "PREREQ_BLOCKED"]
        self.assertEqual(exc, [])  # 前置阻塞不是授权异常
        w = service.wave_detail("W-1")
        self.assertEqual(w["status"], "released")  # 波次仍未开始

    def test_middle_becomes_workable_after_base(self):
        self.expect_error(409, "PREREQ_BLOCKED",
                          lambda: self.scan("PA", 1, "C-S1-1"))
        self.scan("PA", 1, "C-S1-0", now="09:00:00")
        self.scan("PA", 1, "C-S1-1", now="09:10:00")[0]
        self.scan("PA", 1, "C-S1-2", now="09:20:00")[0]
        self.assert_invariants()


class TestHatchConflict(PermitCase):
    def test_adjacent_hatches_same_window_rejected(self):
        e = self.expect_error(409, "HATCH_CONFLICT", lambda: service.create_permit(
            "W-1", {"crane_id": "QC-4", "hatches": ["H1"],
                    "valid_from": t("09:00:00"), "valid_to": t("10:00:00"),
                    "container_ids": []}, now=t("08:00:00")))
        self.assertTrue(e.extra["conflicts"])
        self.assertTrue(all("permit_id" in c for c in e.extra["conflicts"]))
        self.assertTrue(e.extra["occupancy"])  # 返回最新占用情况

    def test_adjacent_across_waves_also_locked(self):
        # W-1 占 H2（QC-1），QC-9 想在同一时段拿相邻的 H3 —— 即使新建波次也拒
        self.expect_error(409, "HATCH_CONFLICT", lambda: service.create_permit(
            "W-2", {"crane_id": "QC-9", "hatches": ["H3"],
                    "valid_from": t("09:00:00"), "valid_to": t("11:00:00"),
                    "container_ids": []}, now=t("08:30:00")))

    def test_non_overlapping_window_allowed(self):
        p, status = service.create_permit("W-1", {
            "crane_id": "QC-4", "hatches": ["H1"],
            "valid_from": t("12:30:00"), "valid_to": t("13:30:00")},
            now=t("08:00:00"))
        self.assertEqual(status, 201)
        self.assertEqual(p["hatches"], ["H1"])

    def test_same_crane_overlap_allowed(self):
        p, status = service.create_permit("W-1", {
            "crane_id": "QC-1", "hatches": ["H1"],
            "valid_from": t("09:00:00"), "valid_to": t("10:00:00"),
            "container_ids": []}, now=t("08:00:00"))
        self.assertEqual(status, 201)

    def test_adjust_into_conflict_rejected_and_returns_snapshot(self):
        e = self.expect_error(409, "HATCH_CONFLICT", lambda: service.update_permit(
            "PB", {"expected_revision": 1, "hatches": ["H2", "H3"],
                   "valid_from": t("09:00:00"), "valid_to": t("11:00:00")},
            now=t("08:30:00")))
        self.assertTrue(e.extra["occupancy"])


    def test_released_box_can_be_regranted(self):
        # PA 收缩到 H1：C-S2-* 变 released（行保留，不再是 active 属主）
        service.update_permit("PA", {"expected_revision": 1, "hatches": ["H1"]},
                              now=t("08:30:00"))
        w = service.wave_detail("W-1")
        owners = {b["container_id"]: b["owner_permit_id"]
                  for b in w["boxes"] if b["container_id"].startswith("C-S2")}
        self.assertEqual(set(owners.values()), {None})
        # 新许可在交接班窗口重新接管 H2 的箱
        p, _ = service.create_permit("W-1", {
            "id": "PC", "crane_id": "QC-3", "hatches": ["H2"],
            "valid_from": t("12:01:00"), "valid_to": t("13:00:00")},
            now=t("08:00:00"))
        self.assertEqual(p["active_count"], 3)
        # PC 先清空箱集合再加回：upsert 不得与保留的 released 行冲突
        service.update_permit("PC", {"expected_revision": 1,
                                     "container_ids": []}, now=t("12:05:00"))
        service.update_permit("PC", {"expected_revision": 2, "container_ids": [
            "C-S2-0", "C-S2-1", "C-S2-2"]}, now=t("12:06:00"))
        self.assertEqual(service.permit_detail("PC")["active_count"], 3)
        self.assert_invariants()


class TestStaleRevision(PermitCase):
    def _bump(self):
        service.update_permit("PA", {
            "expected_revision": 1, "valid_from": t("07:30:00")},
            now=t("09:00:00"))

    def test_scan_with_old_revision_goes_exception(self):
        self._bump()
        eid, reason = self.scan_exc("PA", 1, "C-S1-0")
        self.assertEqual(reason, "STALE_REVISION")
        exc = [e for e in service.list_exceptions("open")["exceptions"]
               if e["id"] == eid][0]
        self.assertEqual(exc["permit_rev"], 1)

    def test_concurrent_adjust_old_revision_cannot_overwrite(self):
        self._bump()  # r1 -> r2
        self.expect_error(409, "STALE_REVISION", lambda: service.update_permit(
            "PA", {"expected_revision": 1, "valid_to": t("11:30:00")},
            now=t("09:05:00")))
        p = service.permit_detail("PA")
        self.assertEqual(p["revision"], 2)  # 旧修订没有覆盖

    def test_restore_stale_exception_and_rescan(self):
        self._bump()
        eid, _ = self.scan_exc("PA", 1, "C-S1-0")
        r, _ = service.resolve_exception(eid, {"action": "restore"},
                                         idem_key="r1", now=t("09:10:00"))
        self.assertEqual(r["latest_revision"], 2)
        body, _ = self.scan("PA", 2, "C-S1-0", now="09:11:00")
        self.assertEqual(body["outcome"], "loaded")


class TestSuspendResume(PermitCase):
    def test_scan_while_suspended_exception_then_resume(self):
        service.suspend_permit("PA", {"reason": "检修"}, actor="d1")
        _, reason = self.scan_exc("PA", 2, "C-S1-0")
        self.assertEqual(reason, "PERMIT_SUSPENDED")
        self.expect_error(409, "BAD_PERMIT_STATE",
                          lambda: service.suspend_permit("PA", {}))
        p, _ = service.resume_permit("PA", {}, actor="d2")
        self.assertEqual(p["status"], "active")
        self.assertEqual(p["revision"], 3)
        body, _ = self.scan("PA", 3, "C-S1-0", now="09:00:00")
        self.assertEqual(body["outcome"], "loaded")

    def test_expired_permit_restore_requires_new_window(self):
        body, _ = self.scan("PA", 1, "C-S1-0", now="12:30:00")
        self.assertEqual(body["outcome"], "exception")
        eid = body["exception_id"]
        self.expect_error(400, "VALID_TO_REQUIRED", lambda: service.resolve_exception(
            eid, {"action": "restore"}, now=t("12:31:00")))
        r, _ = service.resolve_exception(
            eid, {"action": "restore", "valid_to": t("13:30:00")},
            now=t("12:31:00"), idem_key="rest-exp")
        self.assertEqual(r["outcome"], "restore")
        body, _ = self.scan("PA", 2, "C-S1-0", now="12:40:00")
        self.assertEqual(body["outcome"], "loaded")

    def test_revoked_permit_cannot_restore(self):
        service.revoke_permit("PA", {"reason": "取消"}, actor="d1")
        _, reason = self.scan_exc("PA", 2, "C-S1-0")
        self.assertEqual(reason, "PERMIT_REVOKED")
        exc = service.list_exceptions("open")["exceptions"][0]
        self.expect_error(409, "PERMIT_REVOKED", lambda: service.resolve_exception(
            exc["id"], {"action": "restore"}))

    def test_out_of_hatch_and_not_in_wave(self):
        # 用 PA（W-1）扫 W-2 的箱 -> NOT_IN_WAVE
        _, reason = self.scan_exc("PA", 1, "C-S3-0", now="09:00:00")
        self.assertEqual(reason, "NOT_IN_WAVE")
        # 箱被调走后原许可扫描 -> PERMIT_RELEASED / OUT_OF_HATCH
        service.update_permit("PA", {"expected_revision": 1, "hatches": ["H1"]},
                              now=t("08:30:00"))
        _, reason = self.scan_exc("PA", 2, "C-S2-0", now="09:00:00")
        self.assertIn(reason, ("PERMIT_RELEASED", "OUT_OF_HATCH"))


class TestTransfer(PermitCase):
    def test_full_transfer_keeps_executed_origin(self):
        # 先在 PA 装一个箱（保持 PA 归属）
        self.scan("PA", 1, "C-S1-0", now="09:00:00")
        # 目标许可 PC 放在 PA 收工(12:00) 与 PB 开工(13:00) 的交接班窗口：
        # 同一/相邻舱区在任何重叠时段都不能授给其他岸桥。
        service.create_permit("W-1", {
            "id": "PC", "crane_id": "QC-3", "hatches": ["H1", "H2"],
            "valid_from": t("12:01:00"), "valid_to": t("13:00:00")},
            now=t("08:00:00"))
        # 09:30 目标许可尚未生效 -> 拒绝
        self.expect_error(409, "TARGET_NOT_VALID_NOW",
                          lambda: service.transfer_permit(
                              "PA", {"target_permit_id": "PC"}, now=t("09:30:00")))
        # 12:10 交接班窗口转交：已执行箱留 PA，5 个未执行箱整体迁移
        r, _ = service.transfer_permit(
            "PA", {"target_permit_id": "PC", "expected_source_revision": 1},
            idem_key="tr1", now=t("12:10:00"))
        migrated = set(r["migrated"])
        self.assertNotIn("C-S1-0", migrated)          # 已执行箱不迁移
        self.assertIn("C-S2-0", migrated)
        self.assertEqual(len(migrated), 5)
        pc = service.permit_detail("PC")
        self.assertEqual(pc["executed_count"], 0)     # 已执行箱留在 PA
        self.assertEqual(pc["revision"], 2)
        pa = service.permit_detail("PA")
        self.assertEqual(pa["executed_count"], 1)
        self.assertEqual(pa["active_count"], 0)
        # 未执行箱现在由 PC（r2）扫描成功
        body, _ = self.scan("PC", 2, "C-S2-0", now="12:20:00")
        self.assertEqual(body["outcome"], "loaded")
        self.assert_invariants()

    def test_transfer_hatch_mismatch_rejected(self):
        service.create_permit("W-1", {
            "id": "PC", "crane_id": "QC-3", "hatches": ["H1"],
            "valid_from": t("12:01:00"), "valid_to": t("13:00:00")},
            now=t("08:00:00"))  # 只覆盖 H1；PA 还有 H2 的箱
        e = self.expect_error(409, "TRANSFER_HATCH_MISMATCH",
                              lambda: service.transfer_permit(
                                  "PA", {"target_permit_id": "PC"},
                                  now=t("12:10:00")))
        self.assertEqual(e.extra["missing_hatches"], ["H2"])

    def test_concurrent_transfers_only_one_wins(self):
        # 两名调度员同时把 PA 转交给同一目标 PC：IMMEDIATE 串行 + 乐观修订号，
        # 先到者把 PA 推到 r2，后到者携带的 r1 预期必须失败（旧修订不得覆盖）。
        service.create_permit("W-1", {
            "id": "PC", "crane_id": "QC-3", "hatches": ["H1", "H2"],
            "valid_from": t("12:01:00"), "valid_to": t("13:00:00"),
            "container_ids": []}, now=t("08:00:00"))
        errors, results = [], []

        def run(key):
            try:
                r, _ = service.transfer_permit(
                    "PA", {"target_permit_id": "PC",
                           "expected_source_revision": 1},
                    idem_key=key, now=t("12:10:00"))
                results.append(r["migrated"])
            except service.PermitError as e:
                errors.append((e.status, e.code))

        threads = [threading.Thread(target=run, args=("k-pc-1",)),
                   threading.Thread(target=run, args=("k-pc-2",))]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(len(results), 1, errors)
        self.assertEqual(errors, [(409, "STALE_REVISION")])
        self.assertEqual(len(results[0]), 6)
        detail = service.wave_detail("W-1")
        for b in detail["boxes"]:
            if not b["executed"]:
                self.assertEqual(b["owner_permit_id"], "PC")
        self.assert_invariants()


class TestIdempotency(PermitCase):
    def test_wave_publish_idempotent(self):
        payload = {"id": "W-9", "name": "幂等波次",
                   "container_ids": ["C-S3-0"]}
        a, st1 = service.publish_wave("D-1", dict(payload), idem_key="wk1",
                                      now=t("10:00:00"))
        b, st2 = service.publish_wave("D-1", dict(payload), idem_key="wk1",
                                      now=t("10:00:00"))
        self.assertEqual((a["id"], st1), ("W-9", 201))
        self.assertEqual(b["id"], "W-9")
        self.assertEqual(st2, 201)
        waves = [w["id"] for w in service.list_waves()["waves"]]
        self.assertEqual(waves.count("W-9"), 1)

    def test_same_key_different_body_rejected(self):
        service.publish_wave("D-1", {"id": "W-10", "container_ids": ["C-S3-0"]},
                             idem_key="wk2", now=t("10:00:00"))
        self.expect_error(409, "IDEM_KEY_REUSE", lambda: service.publish_wave(
            "D-1", {"id": "W-11", "container_ids": ["C-S3-1"]},
            idem_key="wk2", now=t("10:00:00")))

    def test_scan_idempotent_replays_loaded(self):
        a, _ = self.scan("PA", 1, "C-S1-0", now="09:00:00", key="sk1")
        b, _ = self.scan("PA", 1, "C-S1-0", now="09:00:00", key="sk1")
        self.assertEqual(a["scan_id"], b["scan_id"])
        scans = service.list_scans("W-1")["scans"]
        self.assertEqual(len(scans), 1)

    def test_natural_duplicate_scan_after_loaded(self):
        self.scan("PA", 1, "C-S1-0", now="09:00:00")
        body, _ = self.scan("PA", 1, "C-S1-0", now="09:05:00")
        self.assertEqual(body["outcome"], "loaded")
        self.assertTrue(body["duplicate"])

    def test_transfer_idempotent(self):
        service.create_permit("W-1", {
            "id": "PC", "crane_id": "QC-3", "hatches": ["H1", "H2"],
            "valid_from": t("12:01:00"), "valid_to": t("13:00:00"),
            "container_ids": []}, now=t("08:00:00"))
        payload = {"target_permit_id": "PC"}
        a, _ = service.transfer_permit("PA", dict(payload), idem_key="tk1",
                                       now=t("12:10:00"))
        b, _ = service.transfer_permit("PA", dict(payload), idem_key="tk1",
                                       now=t("12:10:00"))
        self.assertEqual(a["migrated"], b["migrated"])
        self.assert_invariants()

    def test_exception_resolve_idempotent(self):
        service.suspend_permit("PA", {"reason": "x"})
        body, _ = service.confirm_scan(
            {"permit_id": "PA", "revision": 2, "container_id": "C-S1-0"},
            now=t("09:00:00"))
        eid = body["exception_id"]
        a, _ = service.resolve_exception(eid, {"action": "void"},
                                         idem_key="vk1", now=t("09:01:00"))
        b, _ = service.resolve_exception(eid, {"action": "void"},
                                         idem_key="vk1", now=t("09:02:00"))
        self.assertEqual(a, b)
        self.expect_error(409, "EXCEPTION_CLOSED", lambda: service.resolve_exception(
            eid, {"action": "void"}, idem_key="vk2"))
