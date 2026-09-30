"""端到端 HTTP 测试：真实 ThreadingHTTPServer + 标准库 urllib 请求。

覆盖：时间轴/调度页 JSON、幂等头、旧修订 409 携带占用快照、
并发调整许可（两个真实 HTTP 连接）。
"""
import json
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from permit.server import Handler
from permit import service

from permit.tests.fixture import PermitCase, make_fixture, t


def _request(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class HttpCase(PermitCase):
    @classmethod
    def setUpClass(cls):
        pass

    def setUp(self):
        super().setUp()
        import os
        os.environ["PERMIT_DB"] = self.db
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def post(self, path, body, headers=None):
        return _request(self.base + path, "POST", body, headers)

    def get(self, path, headers=None):
        return _request(self.base + path, "GET", headers=headers)


class TestHttpBasic(HttpCase):
    def test_static_page(self):
        with urllib.request.urlopen(self.base + "/permit.html",
                                    timeout=10) as resp:
            html = resp.read().decode("utf-8")
        self.assertEqual(resp.status, 200)
        self.assertIn("装船波次", html)

    def test_board_and_schedule(self):
        st, body = self.get("/api/board", {"X-Now": t("09:00:00")})
        self.assertEqual(st, 200)
        self.assertEqual(len(body["waves"]), 2)
        sched = body["schedule"]
        pa = [p for p in sched["permits"] if p["id"] == "PA"][0]
        self.assertEqual(pa["time_state"], "current")
        pb = [p for p in sched["permits"] if p["id"] == "PB"][0]
        self.assertEqual(pb["time_state"], "future")

    def test_scan_flow_over_http(self):
        st, body = self.post("/api/scans", {
            "permit_id": "PA", "revision": 1, "container_id": "C-S1-0"},
            {"X-Now": t("09:00:00"), "Idempotency-Key": "scan-1"})
        self.assertEqual(st, 200)
        self.assertEqual(body["outcome"], "loaded")
        # 幂等头重放
        st2, body2 = self.post("/api/scans", {
            "permit_id": "PA", "revision": 1, "container_id": "C-S1-0"},
            {"X-Now": t("09:00:00"), "Idempotency-Key": "scan-1"})
        self.assertEqual(body2["scan_id"], body["scan_id"])
        # 前置阻塞
        st3, body3 = self.post("/api/scans", {
            "permit_id": "PA", "revision": 1, "container_id": "C-S2-2"},
            {"X-Now": t("09:00:00")})
        self.assertEqual(st3, 409)
        self.assertEqual(body3["error"]["code"], "PREREQ_BLOCKED")
        self.assertIn("C-S2-1", body3["error"]["blocked_by"])

    def test_old_revision_returns_exception(self):
        self.post("/api/permits/PA/adjust",
                  {"expected_revision": 1, "valid_from": t("07:00:00")},
                  {"X-Now": t("08:30:00")})
        st, body = self.post("/api/scans", {
            "permit_id": "PA", "revision": 1, "container_id": "C-S1-0"},
            {"X-Now": t("09:00:00")})
        self.assertEqual(st, 200)
        self.assertEqual(body["reason"], "STALE_REVISION")

    def test_hatch_conflict_returns_occupancy(self):
        st, body = self.post("/api/waves/W-1/permits", {
            "crane_id": "QC-9", "hatches": ["H2"],
            "valid_from": t("09:00:00"), "valid_to": t("10:00:00")},
            {"X-Now": t("08:00:00")})
        self.assertEqual(st, 409)
        self.assertEqual(body["error"]["code"], "HATCH_CONFLICT")
        self.assertTrue(body["error"]["occupancy"])
        self.assertTrue(any(c["permit_id"] == "PA"
                            for c in body["error"]["conflicts"]))

    def test_concurrent_adjust_loser_gets_409_and_latest(self):
        barrier = threading.Barrier(2)
        results = []

        def adjust(rev, new_from):
            barrier.wait()
            st, body = self.post("/api/permits/PA/adjust", {
                "expected_revision": rev, "valid_from": new_from},
                {"X-Now": t("08:45:00")})
            results.append((st, body))

        ths = [threading.Thread(target=adjust, args=(1, t("07:15:00"))),
               threading.Thread(target=adjust, args=(1, t("07:30:00")))]
        for th in ths:
            th.start()
        for th in ths:
            th.join()
        oks = [r for r in results if r[0] == 200]
        fails = [r for r in results if r[0] == 409]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(fails), 1)
        self.assertEqual(fails[0][1]["error"]["code"], "STALE_REVISION")
        self.assertEqual(fails[0][1]["error"]["latest_revision"], 2)
        st, p = self.get("/api/permits/PA")
        self.assertEqual(p["revision"], 2)  # 旧修订没有覆盖

    def test_transfer_and_exception_resolve(self):
        st, pc = self.post("/api/waves/W-1/permits", {
            "crane_id": "QC-3", "hatches": ["H1", "H2"],
            "valid_from": t("12:01:00"), "valid_to": t("13:00:00"),
            "container_ids": []}, {"X-Now": t("08:00:00")})
        self.assertEqual(st, 201, pc)
        target = pc["id"]
        st, body = self.post("/api/permits/PA/transfer",
                             {"target_permit_id": target},
                             {"Idempotency-Key": "tr-http",
                              "X-Now": t("12:10:00")})
        self.assertEqual(st, 200, body)
        self.assertEqual(len(body["migrated"]), 6)
        # 重复请求
        st2, body2 = self.post("/api/permits/PA/transfer",
                               {"target_permit_id": target},
                               {"Idempotency-Key": "tr-http",
                                "X-Now": t("12:10:00")})
        self.assertEqual(body2["migrated"], body["migrated"])
        st, v = self.get("/api/admin/verify")
        self.assertTrue(v["ok"], v)

    def test_health(self):
        st, body = self.get("/healthz")
        self.assertEqual((st, body["ok"]), (200, True))
