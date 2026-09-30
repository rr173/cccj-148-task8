"""接收端 HTTP 服务（Python 标准库）。

路由：
  POST /relay/upload              段包上传
  GET  /relay/sessions            会话/游标概览
  GET  /relay/pending             待处理
  GET  /relay/gaps                断层
  GET  /relay/quarantine          隔离
  GET  /relay/applied             已应用
  GET  /relay/rejected            整包拒绝台账
  GET  /relay/events              事件日志
  GET  /relay/voyages/<vid>/map   扫描→当前航次映射
  POST /relay/dispose             操作台处置（request_id 幂等）
  POST /relay/config              设置当前航次 {"active_voyage": ...}
  POST /relay/recover             手动触发恢复
  GET  /relay/verify              一致性自检
  GET  /healthz
"""
from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import console, store, views
from .receiver import Receiver, RecvError


def make_handler(recv: Receiver):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RelayReceiver/1.0"

        def log_message(self, fmt, *args):  # 安静一点
            pass

        def _send(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_console(self):
            path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "static", "relay.html")
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                return self._send(404, {"error": {"code": "NO_CONSOLE"}})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else ""
            return raw

        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path in ("/", "/console", "/relay"):
                    return self._serve_console()
                if path == "/healthz":
                    return self._send(200, {"ok": True})
                if path == "/relay/sessions":
                    return self._send(200, {"sessions": views.sessions_overview(
                        recv.db_path)})
                if path == "/relay/pending":
                    return self._send(200, {"pending": views.pending(
                        recv.db_path)})
                if path == "/relay/gaps":
                    return self._send(200, {"gaps": views.gaps(recv.db_path)})
                if path == "/relay/quarantine":
                    return self._send(200, {"quarantine": views.quarantine(
                        recv.db_path)})
                if path == "/relay/applied":
                    rows, ignored = views.applied(recv.db_path)
                    return self._send(200, {"applied": rows,
                                            "ignored_count": ignored})
                if path == "/relay/rejected":
                    return self._send(200, {"rejected": views.rejected(
                        recv.db_path)})
                if path == "/relay/events":
                    return self._send(200, {"events": views.events(
                        recv.db_path, limit=300)})
                m = re.match(r"^/relay/voyages/([^/]+)/map$", path)
                if m:
                    return self._send(200, views.voyage_mapping(
                        recv.db_path, recv.stowage_db, m.group(1)))
                if path == "/relay/verify":
                    return self._send(200, views.verify_invariants(
                        recv.db_path, recv.stowage_db))
                self._send(404, {"error": {"code": "NOT_FOUND",
                                           "message": path}})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": {"code": "INTERNAL",
                                           "message": str(e)}})

        def do_POST(self):
            path = self.path.split("?")[0]
            raw = self._read_json()
            try:
                if path == "/relay/upload":
                    try:
                        ack = recv.upload(raw)
                    except RecvError as e:
                        return self._send(e.http_status,
                                          {"error": {"code": e.code,
                                                     "message": str(e)}})
                    status = 200
                    if ack.get("status") == "quarantine":
                        status = 409
                    return self._send(status, {"ack": ack})

                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    return self._send(400, {"error": {"code": "BAD_JSON",
                                                      "message": "非法 JSON"}})

                if path == "/relay/dispose":
                    try:
                        result = console.dispose(
                            recv,
                            request_id=body.get("request_id"),
                            record_id=body.get("record_id"),
                            action=body.get("action"),
                            new_slot=body.get("new_slot"),
                            operator_id=body.get("operator_id", "operator"),
                            note=body.get("note"))
                    except console.ConsoleError as e:
                        return self._send(e.http_status,
                                          {"error": {"code": e.code,
                                                     "message": str(e)}})
                    code = 200 if result.get("outcome") != "noop" else 409
                    return self._send(code, {"disposition": result})

                if path == "/relay/config":
                    conn = store.connect(recv.db_path)
                    try:
                        for k in ("active_voyage",):
                            if k in body:
                                store.set_config(conn, k, body[k])
                        conn.commit()
                    finally:
                        conn.close()
                    return self._send(200, {"ok": True, "config": body})

                if path == "/relay/recover":
                    actions = recv.recover()
                    return self._send(200, {"recovered": actions,
                                            "verify": views.verify_invariants(
                                                recv.db_path,
                                                recv.stowage_db)})
                self._send(404, {"error": {"code": "NOT_FOUND"}})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": {"code": "INTERNAL",
                                           "message": str(e)}})

    return Handler


def serve(recv: Receiver, port: int = 8090):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(recv))
    print(f"[relay] 接收服务监听 http://127.0.0.1:{port} "
          f"(db={recv.db_path}, stowage={recv.stowage_db})")
    httpd.serve_forever()
