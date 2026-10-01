"""压载联动执行模块 HTTP 服务（Python 标准库）。

路由：
  GET  /                           浏览器操作页
  POST /ballast/topology           录入/更新压载拓扑
  GET  /ballast/topology           当前拓扑（舱/泵/阀/连通路径）
  GET  /ballast/topology/{id}
  POST /ballast/levels             初始化/校准各舱液位
  GET  /ballast/levels
  POST /ballast/plans              生成方案（里程碑装船时间线）
  POST /ballast/plans/from-stowage 从配载库已发布方案生成时间线
  GET  /ballast/plans/{id}         方案时间线（步骤状态+稳性）
  GET  /ballast/plans/root/{rid}   修订链最新方案
  GET  /ballast/revisions          修订历史（按 root 链）
  POST /ballast/plans/{id}/revise  冻结未开始步骤并生成新修订
  POST /ballast/command            start/pause/resume/confirm（token 幂等）
  POST /ballast/feedback           传感器液位/流量反馈（token 幂等）
  GET  /ballast/stability          实时稳性（当前实测液位+装船）
  GET  /ballast/leases             泵阀占用
  GET  /ballast/alerts             偏差告警
  POST /ballast/alerts/{id}/ack
  GET  /ballast/events             事件账本
  GET  /ballast/checkpoints
  POST /ballast/loading            录入/同步装船进度
  POST /ballast/recover            手动触发崩溃恢复
  GET  /healthz
"""
from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import fixture
from .domain import WeightItem, evaluate
from .executor import ExecError, Executor
from .planner import Milestone


def _items_to_cargo(items):
    return [WeightItem(float(it["w"]), float(it.get("x", 0.0)),
                       float(it.get("y", 0.0)), float(it.get("z", 10.0)),
                       str(it.get("tag", ""))) for it in items]


def _milestones_from_body(body):
    out = []
    for ms in body.get("milestones", []):
        out.append(Milestone(
            label=str(ms.get("label", f"M{len(out)}")),
            cargo=_items_to_cargo(ms.get("cargo", [])),
            expected_loaded_n=int(ms.get("expected_loaded_n", 0))))
    return out


def make_handler(ex: Executor, stowage_db: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BallastControl/1.0"

        def log_message(self, fmt, *args):
            pass

        def _send(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _console(self):
            path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "static", "ballast.html")
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

        def _body(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else ""
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except ValueError:
                raise _Http(400, "BAD_JSON", "请求体不是合法 JSON")

        # ---- GET ----------------------------------------------------- #
        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path in ("/", "/ballast"):
                    return self._console()
                if path == "/healthz":
                    return self._send(200, {"ok": True})
                if path == "/ballast/topology":
                    return self._send(200, {"topologies": _list_topologies(ex)})
                if path == "/ballast/levels":
                    return self._send(200, {"levels": ex.current_volumes()})
                if path == "/ballast/leases":
                    return self._send(200, {"leases": _leases(ex)})
                if path == "/ballast/alerts":
                    return self._send(200, {"alerts": _alerts(ex)})
                if path == "/ballast/events":
                    return self._send(200, {"events": _events(ex)})
                if path == "/ballast/checkpoints":
                    return self._send(200, {"checkpoints": _checkpoints(ex)})
                if path == "/ballast/revisions":
                    return self._send(200, {"revisions": _revisions(ex)})
                if path == "/ballast/stability":
                    return self._send(200, _stability(ex))
                m = re.match(r"^/ballast/topology/([^/]+)$", path)
                if m:
                    topo, payload, tid = ex.load_topology(m.group(1))
                    return self._send(200, {"topology_id": tid,
                                            "payload": payload})
                m = re.match(r"^/ballast/plans/root/([^/]+)$", path)
                if m:
                    doc = ex.latest_for_root(m.group(1))
                    if doc is None:
                        return self._send(404, {"error": {"code": "NOT_FOUND"}})
                    return self._send(200, {"plan": doc})
                m = re.match(r"^/ballast/plans/([^/]+)$", path)
                if m:
                    return self._send(200, {"plan": ex.get_plan_doc(m.group(1))})
                self._send(404, {"error": {"code": "NOT_FOUND", "path": path}})
            except _Http as e:
                self._send(e.status, {"error": {"code": e.code,
                                                "message": e.msg}})
            except ExecError as e:
                self._send(e.http_status,
                           {"error": {"code": e.code, "message": str(e),
                                      **e.detail}})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": {"code": "INTERNAL",
                                           "message": str(e)}})

        # ---- POST ---------------------------------------------------- #
        def do_POST(self):
            path = self.path.split("?")[0]
            try:
                body = self._body()
                if path == "/ballast/topology":
                    tid = ex.save_topology(body.get("topology", body),
                                           name=body.get("name", "default"))
                    return self._send(200, {"topology_id": tid})
                if path == "/ballast/levels":
                    ex.init_levels(body["levels"], source=body.get("source",
                                                                   "init"))
                    return self._send(200, {"ok": True})
                if path == "/ballast/plans":
                    doc = ex.create_plan(
                        body.get("voyage_id"),
                        _milestones_from_body(body),
                        current_volumes=body.get("current_volumes"),
                        topology_id=body.get("topology_id"),
                        tolerance_pct=float(body.get("tolerance_pct", 0.02)),
                        max_step=float(body.get("max_step", 300.0)),
                        reason=body.get("reason", "initial"))
                    return self._send(200, {"plan": doc})
                if path == "/ballast/plans/from-stowage":
                    if not stowage_db:
                        raise _Http(400, "NO_STOWAGE_DB",
                                    "服务未配置配载库路径")
                    vid = body["voyage_id"]
                    ms = fixture.milestones_from_stowage(
                        stowage_db, vid,
                        n_milestones=int(body.get("n_milestones", 3)))
                    if not ex.current_volumes():
                        topo, _, _ = ex.load_topology(body.get("topology_id"))
                        ex.init_levels(fixture.initial_volumes(topo))
                    doc = ex.create_plan(vid, ms,
                                         topology_id=body.get("topology_id"))
                    return self._send(200, {"plan": doc})
                if path == "/ballast/command":
                    cmd = body.get("command")
                    if cmd == "confirm":
                        result = ex.confirm(body["token"], body["plan_id"],
                                            int(body["step_seq"]),
                                            body.get("operator_id", "officer"))
                    else:
                        result = ex.command(body["token"], body["plan_id"],
                                            int(body["step_seq"]), cmd,
                                            body.get("operator_id", "officer"))
                    status = result.pop("http_status", 200)
                    return self._send(status, {"result": result})
                if path == "/ballast/feedback":
                    result = ex.feedback(
                        body["token"], body["plan_id"], int(body["step_seq"]),
                        float(body["observed_src"]),
                        float(body["observed_dst"]),
                        body.get("flow_rate"))
                    return self._send(200, {"result": result})
                m = re.match(r"^/ballast/plans/([^/]+)/revise$", path)
                if m:
                    doc = ex.revise(
                        m.group(1), _milestones_from_body(body),
                        reason=body.get("reason", "loading_changed"),
                        operator_id=body.get("operator_id", "chief"),
                        measured_volumes=body.get("measured_volumes"))
                    return self._send(200, {"plan": doc})
                if path == "/ballast/loading":
                    ex.update_loading(body["voyage_id"],
                                      _items_to_cargo(body.get("loaded", [])),
                                      int(body.get("loaded_n", 0)),
                                      source=body.get("source", "manual"))
                    return self._send(200, {"ok": True})
                m = re.match(r"^/ballast/alerts/(\d+)/ack$", path)
                if m:
                    _ack_alert(ex, int(m.group(1)))
                    return self._send(200, {"ok": True})
                if path == "/ballast/recover":
                    return self._send(200, {"recovered": ex.recover()})
                self._send(404, {"error": {"code": "NOT_FOUND", "path": path}})
            except _Http as e:
                self._send(e.status, {"error": {"code": e.code,
                                                "message": e.msg}})
            except ExecError as e:
                self._send(e.http_status,
                           {"error": {"code": e.code, "message": str(e),
                                      **e.detail}})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": {"code": "INTERNAL",
                                           "message": str(e)}})

    return Handler


class _Http(Exception):
    def __init__(self, status, code, msg):
        self.status, self.code, self.msg = status, code, msg


# ---- 视图查询 -------------------------------------------------------- #
def _conn(ex):
    from .store import connect
    return connect(ex.db_path)


def _list_topologies(ex):
    conn = _conn(ex)
    try:
        return [{"id": r["id"], "name": r["name"], "active": bool(r["is_active"]),
                 "created_at": r["created_at"]}
                for r in conn.execute(
                    "SELECT id,name,is_active,created_at FROM topology "
                    "ORDER BY created_at DESC")]
    finally:
        conn.close()


def _leases(ex):
    conn = _conn(ex)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT resource,plan_id,step_seq,operator_id,status,acquired_at,"
            "released_at FROM leases ORDER BY status DESC, resource")]
    finally:
        conn.close()


def _alerts(ex):
    conn = _conn(ex)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT 300")]
    finally:
        conn.close()


def _ack_alert(ex, alert_id):
    conn = _conn(ex)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE alerts SET acked=1 WHERE id=?", (alert_id,))
        conn.commit()
    finally:
        conn.close()


def _events(ex):
    conn = _conn(ex)
    try:
        return [{"id": r["id"], "plan_id": r["plan_id"], "kind": r["kind"],
                 "step_seq": r["step_seq"], "detail":
                     json.loads(r["detail"]) if r["detail"] else None,
                 "created_at": r["created_at"]}
                for r in conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT 400")]
    finally:
        conn.close()


def _checkpoints(ex):
    conn = _conn(ex)
    try:
        return [{"plan_id": r["plan_id"],
                 "state": json.loads(r["state"]), "updated_at": r["updated_at"]}
                for r in conn.execute(
                    "SELECT * FROM checkpoints ORDER BY updated_at DESC")]
    finally:
        conn.close()


def _revisions(ex):
    conn = _conn(ex)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id,root_id,revision_of,revision_no,status,reason,"
            "voyage_id,created_at FROM plans ORDER BY created_at DESC LIMIT 200")]
    finally:
        conn.close()


def _stability(ex):
    topo, _, tid = ex.load_topology()
    conn = _conn(ex)
    try:
        vols = {r["tank"]: r["vol"] for r in conn.execute(
            "SELECT tank,vol FROM tank_levels")}
        cargo = []
        for r in conn.execute("SELECT loaded FROM loading_state"):
            cargo += _items_to_cargo(json.loads(r["loaded"]))
    finally:
        conn.close()
    if not vols:
        vols = {t.id: t.initial_vol for t in topo.tanks.values()}
    rep = evaluate(topo, cargo, vols)
    from .planner import _rep_dict
    return {"topology_id": tid, "volumes": vols,
            "stability": _rep_dict(rep)}


def serve(ex: Executor, port: int = 8091, stowage_db: str | None = None):
    httpd = ThreadingHTTPServer(("127.0.0.1", port),
                                make_handler(ex, stowage_db))
    print(f"[ballast] 压载联动服务监听 http://127.0.0.1:{port} "
          f"(db={ex.db_path}, stowage={stowage_db})")
    httpd.serve_forever()
