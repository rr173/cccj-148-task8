"""压载水联动执行模块 HTTP API + 浏览器操作页（仅标准库）。

复用配载系统的全部路由（同一进程同一库），并挂接 /api/ballast/* 接口；
/ 指向压载操作页，/stowage 指向配载页。
"""
import inspect
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from stowage.db import ServiceError
from stowage.server import ROUTES as STOWAGE_ROUTES

from . import service
from .db import init_db

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "static")


def _json_body(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if not length:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ServiceError(400, "BAD_JSON", "request body is not valid JSON")


ROUTES = []


def route(method, pattern):
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
                       + "$")

    def deco(fn):
        ROUTES.append((method, regex, fn))
        return fn
    return deco


@route("GET", "/api/ballast/{vid}/topology")
def api_topology(_, m):
    return service.get_topology(m["vid"]), 200


@route("POST", "/api/ballast/{vid}/topology")
def api_set_topology(body, m):
    return service.save_topology(m["vid"], body,
                                 actor=body.get("actor", "chief")), 200


@route("POST", "/api/ballast/{vid}/plan")
def api_gen_plan(body, m):
    return service.generate_plan(m["vid"], actor=body.get("actor", "chief"),
                                 stages=body.get("stages", 3),
                                 reason=body.get("reason", "manual")), 201


@route("GET", "/api/ballast/{vid}/plan")
def api_get_plan(_, m):
    return service.get_plan(m["vid"]), 200


@route("GET", "/api/ballast/{vid}/revisions")
def api_revisions(_, m):
    return service.list_revisions(m["vid"]), 200


@route("POST", "/api/ballast/{vid}/recheck")
def api_recheck(body, m):
    return service.recheck(m["vid"], actor=body.get("actor", "officer"),
                           reason=body.get("reason", "manual")), 200


@route("POST", "/api/ballast/{vid}/exec/start")
def api_start(body, m):
    return service.start_step(m["vid"], token=body.get("token"),
                              seq=body.get("seq"),
                              revision=body.get("revision"),
                              actor=body.get("actor", "officer")), 200


@route("POST", "/api/ballast/{vid}/exec/pause")
def api_pause(body, m):
    return service.pause_step(m["vid"], token=body.get("token"),
                              actor=body.get("actor", "officer")), 200


@route("POST", "/api/ballast/{vid}/exec/resume")
def api_resume(body, m):
    return service.resume_step(m["vid"], token=body.get("token"),
                               actor=body.get("actor", "officer")), 200


@route("POST", "/api/ballast/{vid}/exec/confirm")
def api_confirm(body, m):
    return service.confirm_step(m["vid"], token=body.get("token"),
                                seq=body.get("seq"),
                                actor=body.get("actor", "officer")), 200


@route("POST", "/api/ballast/{vid}/seize")
def api_seize(body, m):
    return service.seize_pump(m["vid"], token=body.get("token"),
                              pump_id=body.get("pump_id"),
                              officer=body.get("officer", "officer")), 200


@route("POST", "/api/ballast/{vid}/release")
def api_release(body, m):
    return service.release_pump(m["vid"], token=body.get("token"),
                                pump_id=body.get("pump_id"),
                                officer=body.get("officer", "officer")), 200


@route("POST", "/api/ballast/{vid}/feedback")
def api_feedback(body, m):
    return service.feedback(m["vid"], levels=body.get("levels"),
                            flows=body.get("flows"),
                            actor=body.get("actor", "sensor")), 200


@route("GET", "/api/ballast/{vid}/stability")
def api_stability(_, m):
    return service.get_stability(m["vid"]), 200


@route("GET", "/api/ballast/{vid}/resources")
def api_resources(_, m):
    return service.get_resources(m["vid"]), 200


@route("GET", "/api/ballast/{vid}/alarms")
def api_alarms(_, m):
    return service.list_alarms(m["vid"]), 200


@route("POST", "/api/ballast/{vid}/alarms/{aid}/ack")
def api_alarm_ack(body, m):
    return service.ack_alarm(m["vid"], m["aid"],
                             actor=body.get("actor", "officer")), 200


@route("GET", "/api/ballast/{vid}/events")
def api_events(_, m):
    return service.list_events(m["vid"]), 200


@route("GET", "/api/ballast/{vid}/checkpoints")
def api_checkpoints(_, m):
    return service.list_checkpoints(m["vid"]), 200


@route("POST", "/api/ballast/recover")
def api_recover(body, _):
    return service.recover(vid=body.get("voyage_id")), 200


ALL_ROUTES = ROUTES + STOWAGE_ROUTES


class Handler(BaseHTTPRequestHandler):
    server_version = "BallastControl/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, payload, content_type="application/json"):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _dispatch(self, method):
        try:
            parsed, _, raw_query = self.path.partition("?")
            query = parse_qs(raw_query)
            if method == "GET" and parsed in ("/", "/ballast.html"):
                with open(os.path.join(STATIC_DIR, "ballast.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
                return
            if method == "GET" and parsed in ("/stowage", "/index.html"):
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
                return
            if method == "GET" and parsed == "/sample_import.json":
                sample = os.path.join(os.path.dirname(STATIC_DIR),
                                      "sample_import.json")
                with open(sample, "rb") as f:
                    self._send(200, f.read())
                return
            for m, regex, fn in ALL_ROUTES:
                if m != method:
                    continue
                match = regex.match(parsed)
                if match:
                    body = _json_body(self) if method == "POST" else {}
                    kwargs = match.groupdict()
                    args = (body, kwargs, query) \
                        if len(inspect.signature(fn).parameters) == 3 \
                        else (body, kwargs)
                    payload, status = fn(*args)
                    self._send(status, payload)
                    return
            self._send(404, {"error": {"code": "NOT_FOUND",
                                       "message": f"{method} {parsed}"}})
        except ServiceError as e:
            body = {"error": {"code": e.code, "message": e.message}}
            body["error"].update(e.extra)
            self._send(e.status, body)
        except Exception as e:  # pragma: no cover - defensive
            self._send(500, {"error": {"code": "INTERNAL", "message": str(e)}})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def main(host="0.0.0.0", port=8060):
    init_db()
    service.recover()  # 启动即按账本 + 最新液位恢复唯一状态
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"ballast control listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
