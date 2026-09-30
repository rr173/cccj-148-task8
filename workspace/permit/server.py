"""岸桥许可 / 波次互锁 HTTP 服务（标准库）。

约定头：
  Idempotency-Key   波次发布 / 许可转交 / 扫描确认 / 异常处置的幂等键
  X-Actor           操作人（调度员 / 手持终端）
  X-Now             覆盖“当前时间”（ISO8601，含时区），用于演练与测试
"""
import inspect
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import service
from .store import PermitError, init_db

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "static")

ROUTES = []


def route(method, pattern):
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn):
        ROUTES.append((method, regex, fn))
        return fn
    return deco


# ---- 配载底账 ----------------------------------------------------------

@route("POST", "/api/plans")
def api_create_plan(body, _m, h):
    return service.create_plan(body, actor=h["actor"])


@route("GET", "/api/plans")
def api_plans(_b, _m, _h):
    return service.list_plans(), 200


@route("GET", "/api/plans/{pid}")
def api_plan(_b, m, _h):
    return service.plan_detail(m["pid"]), 200


@route("POST", "/api/plans/{pid}/publish")
def api_publish_plan(body, m, h):
    return service.publish_plan(m["pid"], actor=h["actor"])


# ---- 波次 --------------------------------------------------------------

@route("POST", "/api/plans/{pid}/waves")
def api_publish_wave(body, m, h):
    return service.publish_wave(m["pid"], body, actor=h["actor"],
                                idem_key=h["idem_key"], now=h["now"])


@route("GET", "/api/waves")
def api_waves(_b, _m, _h):
    return service.list_waves(), 200


@route("GET", "/api/waves/{wid}")
def api_wave(_b, m, _h):
    return service.wave_detail(m["wid"]), 200


# ---- 许可 --------------------------------------------------------------

@route("POST", "/api/waves/{wid}/permits")
def api_create_permit(body, m, h):
    return service.create_permit(m["wid"], body, actor=h["actor"], now=h["now"])


@route("GET", "/api/permits")
def api_permits(_b, _m, q):
    return service.list_permits(q.get("wave_id", [None])[0]), 200


@route("GET", "/api/permits/{pid}")
def api_permit(_b, m, _h):
    return service.permit_detail(m["pid"]), 200


@route("POST", "/api/permits/{pid}/adjust")
def api_adjust_permit(body, m, h):
    return service.update_permit(m["pid"], body, actor=h["actor"], now=h["now"])


@route("POST", "/api/permits/{pid}/suspend")
def api_suspend(body, m, h):
    return service.suspend_permit(m["pid"], body, actor=h["actor"])


@route("POST", "/api/permits/{pid}/resume")
def api_resume(body, m, h):
    return service.resume_permit(m["pid"], body, actor=h["actor"])


@route("POST", "/api/permits/{pid}/revoke")
def api_revoke(body, m, h):
    return service.revoke_permit(m["pid"], body, actor=h["actor"])


@route("POST", "/api/permits/{pid}/transfer")
def api_transfer(body, m, h):
    return service.transfer_permit(m["pid"], body, actor=h["actor"],
                                   idem_key=h["idem_key"], now=h["now"])


# ---- 扫描与授权异常 -----------------------------------------------------

@route("POST", "/api/scans")
def api_scan(body, _m, h):
    return service.confirm_scan(body, actor=h["actor"],
                                idem_key=h["idem_key"], now=h["now"])


@route("GET", "/api/scans")
def api_scans(_b, _m, q):
    return service.list_scans(q.get("wave_id", [None])[0]), 200


@route("GET", "/api/exceptions")
def api_exceptions(_b, _m, q):
    return service.list_exceptions(q.get("status", [None])[0]), 200


@route("POST", "/api/exceptions/{eid}/resolve")
def api_resolve(body, m, h):
    return service.resolve_exception(m["eid"], body, actor=h["actor"],
                                     idem_key=h["idem_key"], now=h["now"])


# ---- 调度页 / 管理 ------------------------------------------------------

@route("GET", "/api/schedule")
def api_schedule(_b, _m, h):
    return service.schedule(now=h["now"]), 200


@route("GET", "/api/board")
def api_board(_b, _m, h):
    return service.board(now=h["now"]), 200


@route("POST", "/api/admin/reconcile")
def api_reconcile(_b, _m, _h):
    return service.reconcile(), 200


@route("GET", "/api/admin/verify")
def api_verify(_b, _m, _h):
    return service.verify(), 200


@route("GET", "/healthz")
def api_health(_b, _m, _h):
    return {"ok": True}, 200


class Handler(BaseHTTPRequestHandler):
    server_version = "CranePermit/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, payload, content_type="application/json",
              extra_headers=None):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(payload)

    def _dispatch(self, method):
        try:
            parsed, _, raw_query = self.path.partition("?")
            query = parse_qs(raw_query)
            if method == "GET" and parsed in ("/permit", "/permit.html"):
                with open(os.path.join(STATIC_DIR, "permit.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
                return
            if method == "GET" and parsed == "/":
                with open(os.path.join(STATIC_DIR, "permit.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
                return
            for m, regex, fn in ROUTES:
                if m != method:
                    continue
                match = regex.match(parsed)
                if match:
                    body = self._json_body() if method == "POST" else {}
                    headers = {
                        "actor": self.headers.get("X-Actor", "dispatcher"),
                        "idem_key": self.headers.get("Idempotency-Key"),
                        "now": self.headers.get("X-Now"),
                    }
                    kwargs = match.groupdict()
                    params = inspect.signature(fn).parameters
                    args = (body, kwargs, headers) if len(params) == 3 \
                        else (body, kwargs, query)
                    payload, status = fn(*args)
                    self._send(status, payload)
                    return
            self._send(404, {"error": {"code": "NOT_FOUND",
                                       "message": f"{method} {parsed}"}})
        except PermitError as e:
            body = {"error": {"code": e.code, "message": e.message}}
            body["error"].update(e.extra)
            self._send(e.status, body)
        except Exception as e:  # pragma: no cover - defensive
            self._send(500, {"error": {"code": "INTERNAL", "message": str(e)}})

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise PermitError(400, "BAD_JSON", "请求体不是合法 JSON")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def main(host="0.0.0.0", port=8070, db_path=None):
    from .store import DB_PATH
    db_path = db_path or DB_PATH
    init_db(db_path)
    result = service.reconcile(db_path)  # 启动即对账，修复任何上次中断的撕裂
    if result["actions"]:
        print(f"startup reconcile: {len(result['actions'])} action(s): "
              f"{sorted({a['type'] for a in result['actions']})}", flush=True)
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"crane permit interlock listening on http://{host}:{port}", flush=True)
    print(f"dispatcher page: http://127.0.0.1:{port}/permit.html", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
