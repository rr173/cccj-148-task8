"""真实 HTTP socket 端到端：启动接收服务，用边缘模拟器上传，走操作台 API。"""
import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from relay import protocol, views
from relay.server import make_handler
from relay.receiver import Receiver
from relay.segment import DeviceSpool
from relay.tests import fixture as fx


def http(method, url, body=None):
    if body is None:
        data = None
    elif isinstance(body, (bytes, str)):
        data = body.encode() if isinstance(body, str) else body
    else:
        data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


class TestHttpFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.relay_db, cls.stowage_db, cls.spool = fx.new_paths()
        cls.vid, cls.mapping = fx.seed_voyage(cls.stowage_db)
        cls.recv = fx.fresh_receiver(cls.relay_db, cls.stowage_db, cls.vid)
        cls.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(cls.recv))
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        fx.wait_server(cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_full_flow_over_http(self):
        st, b = http("GET", self.base + "/healthz")
        self.assertEqual((st, b["ok"]), (200, True))

        sp = DeviceSpool(self.spool, "HH-WEB")
        sp.start_session("HH-WEB:s1")
        (c1, s1), (c2, s2) = list(self.mapping.items())[0], \
            list(self.mapping.items())[1]
        sp.append_scan(c1, s1)
        sp.append_scan(c2, s2)
        sp.seal(self.vid)
        env = sp.read_packet(0)

        # 上传（直接发送预序列化文本，模拟终端真实字节流）
        raw_text = json.dumps(env, ensure_ascii=False)
        st, b = http("POST", self.base + "/relay/upload", raw_text)
        self.assertEqual(st, 200)
        self.assertEqual(b["ack"]["status"], "accepted")

        # 相同包 -> 200 duplicate
        st, b = http("POST", self.base + "/relay/upload", raw_text)
        self.assertEqual(st, 200)
        self.assertTrue(b["ack"]["duplicate"])

        # 篡改 -> 422
        evil = json.loads(json.dumps(env))
        evil["records"][0]["container"] = "ZZZZ0000000"
        st, b = http("POST", self.base + "/relay/upload",
                     json.dumps(evil, ensure_ascii=False))
        self.assertEqual(st, 422)
        self.assertEqual(b["error"]["code"], "CHECKSUM_MISMATCH")

        # 视图
        st, b = http("GET", self.base + "/relay/sessions")
        self.assertEqual(b["sessions"][0]["cursor"], 0)
        st, b = http("GET", self.base + "/relay/rejected")
        self.assertEqual(len(b["rejected"]), 1)

        # 处置一台重复终端（先制造 pending）
        c = c1
        pkt = protocol.seal({"device_id": "HH-W2", "session": "HH-W2:s1",
                             "seq": 0, "voyage_id": self.vid,
                             "records": [{"record_id": "rec_web_dup",
                                          "container": c, "slot": s1,
                                          "collected_at":
                                          "2026-09-28T09:00:00+08:00"}]})
        st, b = http("POST", self.base + "/relay/upload",
                     json.dumps(pkt, ensure_ascii=False))
        self.assertEqual(b["ack"]["records"][0]["status"], "pending")

        body = {"request_id": "web-req-1", "record_id": "rec_web_dup",
                "action": "ignore", "operator_id": "web"}
        st1, b1 = http("POST", self.base + "/relay/dispose", body)
        st2, b2 = http("POST", self.base + "/relay/dispose", body)
        self.assertFalse(b1["disposition"]["idempotent"])
        self.assertTrue(b2["disposition"]["idempotent"])

        # verify
        st, b = http("GET", self.base + "/relay/verify")
        self.assertTrue(b["ok"], b["violations"])

        # 操作台页面
        with urllib.request.urlopen(self.base + "/console") as r:
            self.assertIn("船岸", r.read().decode())


if __name__ == "__main__":
    unittest.main()
