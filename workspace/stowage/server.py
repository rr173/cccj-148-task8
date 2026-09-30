"""HTTP API + static UI, stdlib only (no external dependencies)."""
import json
import os
import re
import inspect
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import service
from .db import ServiceError, init_db

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "static")


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
    regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn):
        ROUTES.append((method, regex, fn))
        return fn
    return deco


@route("POST", "/api/import")
def api_import(body, _):
    return service.import_voyage(body, actor=body.get("actor", "planner")), 201


@route("GET", "/api/voyages/{vid}")
def api_voyage(_, m):
    return service.voyage_summary(m["vid"]), 200


@route("GET", "/api/voyages/{vid}/events")
def api_events(_, m):
    return service.list_events(m["vid"]), 200


@route("GET", "/api/voyages/{vid}/versions")
def api_versions(_, m):
    summary = service.voyage_summary(m["vid"])
    return {"voyage_id": m["vid"], "versions": summary["plans"]}, 200


@route("POST", "/api/voyages/{vid}/solve")
def api_solve(body, m):
    job_id = service.create_solve_job(m["vid"], actor=body.get("actor", "planner"))
    return {"job_id": job_id}, 202


@route("POST", "/api/voyages/{vid}/resequence")
def api_resequence(body, m):
    return service.resequence_voyage(m["vid"], body.get("new_sequence"),
                                     actor=body.get("actor", "planner")), 200


@route("POST", "/api/voyages/{vid}/late-weight")
def api_late_weight(body, m):
    return service.late_weight(m["vid"], body.get("container_id"),
                               body.get("weight"),
                               actor=body.get("actor", "planner")), 200


@route("POST", "/api/voyages/{vid}/roll")
def api_roll(body, m):
    return service.roll_container(m["vid"], body.get("container_id"),
                                  actor=body.get("actor", "planner")), 200


@route("POST", "/api/voyages/{vid}/discharge")
def api_discharge(body, m):
    return service.discharge_container(m["vid"], body.get("container_id"),
                                       actor=body.get("actor", "operator")), 200


@route("POST", "/api/voyages/{vid}/confirm")
def api_confirm(body, m):
    return service.confirm_load(m["vid"], body.get("plan_id"),
                                body.get("container_id"), body.get("slot_code"),
                                actor=body.get("actor", "operator")), 200


@route("GET", "/api/jobs/{jid}")
def api_job(_, m):
    return service.get_job(m["jid"]), 200


@route("POST", "/api/jobs/{jid}/cancel")
def api_job_cancel(_, m):
    return service.cancel_job(m["jid"]), 200


@route("GET", "/api/plans/{pid}")
def api_plan(_, m):
    return service.plan_detail(m["pid"]), 200


@route("POST", "/api/plans/{pid}/adjust")
def api_adjust(body, m):
    return service.adjust_plan(m["pid"], body.get("moves", []),
                               actor=body.get("actor", "planner")), 200


@route("POST", "/api/plans/{pid}/validate")
def api_validate(_, m):
    return service.validate_plan(m["pid"]), 200


@route("POST", "/api/plans/{pid}/publish")
def api_publish(body, m):
    return service.publish_plan(
        m["pid"], token=body.get("token"), actor=body.get("actor", "planner"),
        expected_version=body.get("expected_version")), 200


@route("GET", "/api/plans/{pid}/diff")
def api_plan_diff(_, m, q):
    base_version = q.get("base_version", [None])[0]
    return service.plan_diff(m["pid"], base_version=base_version), 200


@route("POST", "/api/plans/{pid}/approvals")
def api_request_approval(body, m):
    return service.request_approval(m["pid"], actor=body.get("actor", "planner")), 201


@route("POST", "/api/plans/{pid}/rollback")
def api_rollback(body, m):
    return service.rollback_draft(
        m["pid"], body.get("target_version"), actor=body.get("actor", "planner"),
        expected_version=body.get("expected_version")), 200


@route("GET", "/api/approvals/{token}")
def api_approval(_, m, q):
    return service.get_approval(m["token"]), 200


@route("GET", "/api/voyages/{vid}/approvals")
def api_approvals(_, m, q):
    return {"voyage_id": m["vid"],
            "approvals": service.list_approvals(m["vid"])}, 200


class Handler(BaseHTTPRequestHandler):
    server_version = "StowagePlanner/1.0"

    def log_message(self, fmt, *args):  # quieter logs
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
            if method == "GET" and parsed in ("/", "/index.html"):
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
                return
            if method == "GET" and parsed == "/sample_import.json":
                sample = os.path.join(os.path.dirname(STATIC_DIR), "sample_import.json")
                with open(sample, "rb") as f:
                    self._send(200, f.read())
                return
            for m, regex, fn in ROUTES:
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


def main(host="0.0.0.0", port=8080):
    init_db()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"stowage planner listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
