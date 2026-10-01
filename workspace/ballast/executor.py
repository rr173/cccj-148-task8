"""执行引擎：顺序步骤状态机、幂等命令、泵阀抢占、容差核对、修订与崩溃恢复。

步骤状态：pending -> ready -> running <-> paused -> confirmed
                                    \-> frozen（修订时未开始步骤冻结）

命令令牌（token）：
  相同 token + 相同载荷（plan_id/step_seq/command/operator 规范化哈希）
    -> 回放首次结果（applied/rejected 都回放，HTTP 状态也固化在 result 中）
  相同 token + 不同载荷 -> 409 TOKEN_PAYLOAD_MISMATCH
  旧修订（plan 已 superseded）的启动请求 -> 303 语义，返回最新方案信息
  抢占：leases 部分唯一索引保证同一资源只有一条 held 租约；
        两名值班员并发抢占同一泵组，数据库仲裁恰好一方成功。

事件账本（只追加）是恢复的权威来源；每次状态推进先写事件再改状态，
崩溃注入点 BALLAST_CRASH = C1..C4 全部位于“事务提交后、响应前”。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Dict, List, Optional

from . import planner, store
from .domain import Topology, WeightItem, evaluate
from .planner import Milestone, PlanError, plan_all
from .store import connect, dumps, log_event, utc_now

# ---- 步骤状态 -------------------------------------------------------- #
PENDING, READY, RUNNING, PAUSED, CONFIRMED, FROZEN = \
    "pending", "ready", "running", "paused", "confirmed", "frozen"
ACTIVE = "active"
SUPERSEDED = "superseded"
COMPLETED = "completed"


class ExecError(Exception):
    def __init__(self, http_status: int, code: str, message: str,
                 detail: Optional[dict] = None):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.detail = detail or {}


def payload_hash(d: dict) -> str:
    return hashlib.sha256(dumps(d).encode("utf-8")).hexdigest()[:16]


class Executor:
    def __init__(self, db_path: str, crash: Optional[str] = None):
        self.db_path = db_path
        store.init_db(db_path)
        self.lock = threading.RLock()
        self.crash = crash or os.environ.get("BALLAST_CRASH")
        self._crashed: set = set()

    # ------------------------------------------------------------------ #
    def _maybe_crash(self, point: str, plan_id: str, seq: Optional[int] = None):
        if self.crash == point and point not in self._crashed:
            self._crashed.add(point)
            try:
                os.write(2, f"[BALLAST] 注入崩溃 {point} @ {plan_id}#{seq}\n".encode())
                os.fsync(2)
            except OSError:
                pass
            os._exit(91)

    # ---- 拓扑 --------------------------------------------------------- #
    def save_topology(self, topo_payload: dict, name: str = "default",
                      activate: bool = True) -> str:
        from .fixture import topology_from_dict  # 避免循环依赖
        topo = topology_from_dict(topo_payload)   # 校验
        tid = f"top_{hashlib.sha256(dumps(topo_payload).encode()).hexdigest()[:12]}"
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT id FROM topology WHERE id=?", (tid,)).fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO topology (id,name,payload,is_active,created_at) "
                    "VALUES (?,?,?,0,?)",
                    (tid, name, dumps(topo_payload), utc_now()))
                log_event(conn, "TOPOLOGY_CREATED", None, None,
                         {"topology_id": tid, "name": name})
            if activate:
                conn.execute("UPDATE topology SET is_active=0")
                conn.execute("UPDATE topology SET is_active=1 WHERE id=?", (tid,))
            conn.commit()
        finally:
            conn.close()
        return tid

    def active_topology_id(self, conn) -> Optional[str]:
        row = conn.execute(
            "SELECT id FROM topology WHERE is_active=1 ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None

    def load_topology(self, topology_id: Optional[str] = None
                      ) -> tuple[Topology, dict, str]:
        from .fixture import topology_from_dict
        conn = connect(self.db_path)
        try:
            if topology_id is None:
                topology_id = self.active_topology_id(conn)
            if topology_id is None:
                raise ExecError(404, "NO_TOPOLOGY", "尚未录入压载拓扑")
            row = conn.execute("SELECT payload FROM topology WHERE id=?",
                               (topology_id,)).fetchone()
            if not row:
                raise ExecError(404, "TOPOLOGY_NOT_FOUND", topology_id)
            payload = json.loads(row["payload"])
        finally:
            conn.close()
        return topology_from_dict(payload), payload, topology_id

    # ---- 方案生成 ----------------------------------------------------- #
    def create_plan(self, voyage_id: Optional[str], milestones: List[Milestone],
                    current_volumes: Optional[Dict[str, float]] = None,
                    topology_id: Optional[str] = None,
                    tolerance_pct: float = 0.02,
                    max_step: float = 300.0,
                    reason: str = "initial") -> dict:
        with self.lock:
            topo, payload, topology_id = self.load_topology(topology_id)
            vols = current_volumes or self.current_volumes()
            for tid, v in vols.items():
                if tid not in topo.tanks:
                    raise ExecError(422, "UNKNOWN_TANK", f"未知舱 {tid}")
                t = topo.tanks[tid]
                if not t.min_vol - 1e-6 <= v <= t.max_vol + 1e-6:
                    raise ExecError(422, "LEVEL_OUT_OF_RANGE",
                                    f"舱 {tid} 初始液位 {v} 越界",
                                    {"tank": tid, "vol": v,
                                     "min": t.min_vol, "max": t.max_vol})
            try:
                plan_doc = plan_all(topo, milestones, vols,
                                    max_step=max_step,
                                    tolerance_pct=tolerance_pct)
            except PlanError as e:
                conn = connect(self.db_path)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    pid = f"pln_{os.urandom(6).hex()}"
                    conn.execute(
                        "INSERT INTO plans (id,topology_id,voyage_id,revision_no,"
                        "revision_of,root_id,status,reason,basis,payload,created_at) "
                        "VALUES (?,?,?,1,NULL,?,'infeasible',?,?,?,?)",
                        (pid, topology_id, voyage_id, pid, e.code,
                         dumps({"volumes": vols, "milestones": _ms_brief(milestones)}),
                         dumps({"error": e.code, "detail": e.detail}), utc_now()))
                    log_event(conn, "PLAN_INFEASIBLE", pid, None,
                              {"code": e.code, "detail": e.detail})
                    conn.commit()
                finally:
                    conn.close()
                raise ExecError(422, e.code, str(e),
                                {"plan_id": pid, **e.detail})

            return self._persist_plan(topo, payload, topology_id, voyage_id,
                                      milestones, vols, plan_doc, reason,
                                      revision_of=None, root_id=None)

    def _persist_plan(self, topo, payload, topology_id, voyage_id, milestones,
                      vols, plan_doc, reason, revision_of, root_id) -> dict:
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            pid = f"pln_{os.urandom(6).hex()}"
            if root_id is None:
                root_id = pid
            rev_no = 1
            if revision_of:
                r = conn.execute(
                    "SELECT revision_no,root_id FROM plans WHERE id=?",
                    (revision_of,)).fetchone()
                if not r:
                    raise ExecError(404, "PLAN_NOT_FOUND", revision_of)
                root_id = r["root_id"]
                rev_no = r["revision_no"] + 1
            conn.execute(
                "INSERT INTO plans (id,topology_id,voyage_id,revision_no,"
                "revision_of,root_id,status,reason,basis,payload,created_at) "
                "VALUES (?,?,?,?,?,?, 'active',?,?,?,?)",
                (pid, topology_id, voyage_id, rev_no, revision_of, root_id,
                 reason,
                 dumps({"volumes": vols,
                        "milestones": _ms_brief(milestones)}),
                 dumps(plan_doc), utc_now()))
            for s in plan_doc["steps"]:
                conn.execute(
                    "INSERT INTO steps (plan_id,seq,phase,source_tank,target_tank,"
                    "volume,tolerance,pump,resources,mutex_groups,start_condition,"
                    "status,transferred) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'pending',0)",
                    (pid, s["seq"], s["phase"], s["source_tank"],
                     s["target_tank"], s["volume"], s["tolerance"], s["pump"],
                     dumps(s["resources"]), dumps(s["mutex_groups"]),
                     dumps(s["start_condition"])))
            conn.execute(
                "INSERT INTO checkpoints (plan_id,state,updated_at) VALUES (?,?,?)",
                (pid, dumps({"steps": {str(s["seq"]): {"status": "pending",
                                                       "transferred": 0.0}
                                       for s in plan_doc["steps"]}}), utc_now()))
            log_event(conn, "PLAN_CREATED", pid, None,
                     {"revision_no": rev_no, "revision_of": revision_of,
                      "steps": len(plan_doc["steps"]),
                      "water_total": plan_doc["water_total"]})
            conn.commit()
        finally:
            conn.close()
        return {"plan_id": pid, "revision_no": rev_no, "root_id": root_id,
                "revision_of": revision_of, "status": "active",
                "reason": reason,
                "basis": {"volumes": vols,
                          "milestones": _ms_brief(milestones)},
                **plan_doc}

    # ---- 液位真值 ----------------------------------------------------- #
    def current_volumes(self) -> Dict[str, float]:
        conn = connect(self.db_path)
        try:
            return {r["tank"]: r["vol"]
                    for r in conn.execute(
                        "SELECT tank,vol FROM tank_levels")}
        finally:
            conn.close()

    def init_levels(self, volumes: Dict[str, float], source: str = "init"):
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for t, v in volumes.items():
                conn.execute(
                    "INSERT INTO tank_levels (tank,vol,updated_at,source) "
                    "VALUES (?,?,?,?) ON CONFLICT(tank) DO UPDATE SET "
                    "vol=excluded.vol,updated_at=excluded.updated_at,"
                    "source=excluded.source",
                    (t, float(v), utc_now(), source))
            log_event(conn, "LEVELS_INIT", None, None, {"volumes": volumes})
            conn.commit()
        finally:
            conn.close()

    # ---- 查询 --------------------------------------------------------- #
    def get_plan_doc(self, plan_id: str) -> dict:
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT * FROM plans WHERE id=?",
                               (plan_id,)).fetchone()
            if not row:
                raise ExecError(404, "PLAN_NOT_FOUND", plan_id)
            steps = [self._step_row(r) for r in conn.execute(
                "SELECT * FROM steps WHERE plan_id=? ORDER BY seq", (plan_id,))]
            return self._plan_view(conn, row, steps)
        finally:
            conn.close()

    def latest_for_root(self, root_id: str) -> Optional[dict]:
        conn = connect(self.db_path)
        try:
            r = conn.execute(
                "SELECT id FROM plans WHERE root_id=? ORDER BY revision_no DESC "
                "LIMIT 1", (root_id,)).fetchone()
        finally:
            conn.close()
        return self.get_plan_doc(r["id"]) if r else None

    def _plan_view(self, conn, row, steps) -> dict:
        payload = json.loads(row["payload"])
        return {
            "plan_id": row["id"], "topology_id": row["topology_id"],
            "voyage_id": row["voyage_id"], "revision_no": row["revision_no"],
            "revision_of": row["revision_of"], "root_id": row["root_id"],
            "status": row["status"], "reason": row["reason"],
            "basis": json.loads(row["basis"]),
            "phases": payload.get("phases", []),
            "water_total": payload.get("water_total"),
            "steps": steps,
        }

    @staticmethod
    def _step_row(r) -> dict:
        return {
            "seq": r["seq"], "phase": r["phase"],
            "source_tank": r["source_tank"], "target_tank": r["target_tank"],
            "volume": r["volume"], "tolerance": r["tolerance"],
            "pump": r["pump"], "resources": json.loads(r["resources"]),
            "mutex_groups": json.loads(r["mutex_groups"]),
            "start_condition": json.loads(r["start_condition"]),
            "status": r["status"], "transferred": r["transferred"],
            "observed_src": r["observed_src"], "observed_dst": r["observed_dst"],
            "dispatched_at": r["dispatched_at"], "confirmed_at": r["confirmed_at"],
        }

    # ---- 命令：启动 / 暂停 / 恢复 ------------------------------------ #
    def command(self, token: str, plan_id: str, step_seq: int, command: str,
                operator_id: str) -> dict:
        with self.lock:
            return self._command(token, plan_id, step_seq, command, operator_id)

    def _command(self, token, plan_id, step_seq, command, operator_id):
        if not token:
            raise ExecError(400, "NO_TOKEN", "执行命令必须携带令牌")
        if command not in ("start", "pause", "resume"):
            raise ExecError(422, "BAD_COMMAND", command)
        body = {"plan_id": plan_id, "step_seq": step_seq,
                "command": command, "operator_id": operator_id}
        bh = payload_hash(body)

        conn = connect(self.db_path)
        try:
            # 令牌幂等：先查后插，唯一索引兜底。回放时保留首次的接受/拒绝结论。
            existing = conn.execute(
                "SELECT * FROM commands WHERE token=?", (token,)).fetchone()
            if existing:
                if existing["payload_hash"] != bh:
                    raise ExecError(409, "TOKEN_PAYLOAD_MISMATCH",
                                    "同一执行令牌绑定了不同载荷",
                                    {"original_plan_id": existing["plan_id"],
                                     "original_seq": existing["step_seq"]})
                result = json.loads(existing["result"])
                if existing["status"] == "rejected":
                    code = result.get("rejected") or result.get("error", {}).get(
                        "code") or "REJECTED"
                    raise ExecError(result.get("http_status", 409), code,
                                    "首次请求已被拒绝（令牌回放）",
                                    {**{k: v for k, v in result.items()
                                        if k != "http_status"},
                                     "replay": True})
                result["replay"] = True
                return result

            row = conn.execute("SELECT * FROM plans WHERE id=?",
                               (plan_id,)).fetchone()
            if not row:
                self._record_command(conn, token, body, bh, "rejected",
                                     {"http_status": 404,
                                      "error": {"code": "PLAN_NOT_FOUND"}})
                conn.commit()
                raise ExecError(404, "PLAN_NOT_FOUND", plan_id)

            # 旧修订：启动请求返回最新方案（幂等固化）
            if row["status"] == SUPERSEDED and command == "start":
                latest = conn.execute(
                    "SELECT id,revision_no FROM plans WHERE root_id=? "
                    "ORDER BY revision_no DESC LIMIT 1",
                    (row["root_id"],)).fetchone()
                result = {"http_status": 409, "ok": False,
                          "rejected": "SUPERSEDED_PLAN",
                          "latest_plan_id": latest["id"],
                          "latest_revision_no": latest["revision_no"]}
                self._record_command(conn, token, body, bh, "rejected", result)
                log_event(conn, "STALE_REVISION", plan_id, step_seq,
                          {"token": token, "latest": latest["id"]})
                conn.commit()
                raise ExecError(409, "SUPERSEDED_PLAN",
                                "该修订已被取代，请按最新方案执行",
                                {k: v for k, v in result.items()
                                 if k != "http_status"})

            step = conn.execute(
                "SELECT * FROM steps WHERE plan_id=? AND seq=?",
                (plan_id, step_seq)).fetchone()
            if not step:
                self._record_command(conn, token, body, bh, "rejected",
                                     {"http_status": 404,
                                      "error": {"code": "STEP_NOT_FOUND"}})
                conn.commit()
                raise ExecError(404, "STEP_NOT_FOUND", str(step_seq))

            return self._apply_command(conn, row, step, command,
                                       operator_id, token, body, bh)
        finally:
            conn.close()

    def _reject(self, conn, token, body, bh, code, http_status, result,
                plan_id=None, seq=None, event=None):
        """登记一条幂等的拒绝命令并抛出（由上层在提交后捕获转 HTTP）。"""
        result = {"http_status": http_status, **result}
        self._record_command(conn, token, body, bh, "rejected", result)
        if event:
            log_event(conn, event, plan_id, seq,
                     {"reason": code, "token": token})
        conn.commit()
        raise ExecError(http_status, code, result.get("message", code),
                        {k: v for k, v in result.items()
                         if k not in ("http_status", "message")})

    def _apply_command(self, conn, plan_row, step, command, operator_id,
                       token, body, bh):
        plan_id = plan_row["id"]
        seq = step["seq"]
        status = step["status"]

        if command == "start":
            if status == CONFIRMED:
                result = {"ok": True, "status": CONFIRMED, "note": "步骤已确认"}
                self._record_command(conn, token, body, bh, "applied", result)
                conn.commit()
                return dict(result)
            if status in (RUNNING, PAUSED):
                result = {"ok": True, "status": status, "note": "步骤已在执行"}
                self._record_command(conn, token, body, bh, "applied", result)
                conn.commit()
                return dict(result)
            if status == FROZEN:
                self._reject(conn, token, body, bh, "STEP_FROZEN", 409,
                             {"ok": False}, plan_id, seq)
            # 开始条件：同方案前序步骤必须已确认
            prev = conn.execute(
                "SELECT status FROM steps WHERE plan_id=? AND seq=?",
                (plan_id, seq - 1)).fetchone()
            if prev and prev["status"] != CONFIRMED:
                self._reject(conn, token, body, bh,
                             "START_CONDITION_NOT_MET", 409,
                             {"ok": False, "prev_seq": seq - 1,
                              "prev_status": prev["status"]},
                             plan_id, seq, event="START_DENIED")

            # 里程碑装船进度条件（expected_loaded_n 已达成）
            cond = json.loads(step["start_condition"])
            need_n = cond.get("expected_loaded_n") or 0
            loaded_n = self._loaded_n(conn, plan_row["voyage_id"])
            if loaded_n < need_n:
                self._reject(conn, token, body, bh, "LOADING_NOT_REACHED", 409,
                             {"ok": False, "need_loaded_n": need_n,
                              "actual_loaded_n": loaded_n},
                             plan_id, seq, event="START_DENIED")

            resources = json.loads(step["resources"])
            groups = json.loads(step["mutex_groups"])
            lock_resources = resources + [f"GROUP:{g}" for g in groups]
            # 抢占：逐条插入部分唯一索引；任一条冲突即整体失败回滚
            try:
                conn.execute("SAVEPOINT sp_acquire")
                for res in lock_resources:
                    conn.execute(
                        "INSERT INTO leases (resource,plan_id,step_seq,"
                        "operator_id,token,status,acquired_at) "
                        "VALUES (?,?,?,?,?, 'held',?)",
                        (res, plan_id, seq, operator_id, token, utc_now()))
            except Exception:
                conn.execute("ROLLBACK TO SAVEPOINT sp_acquire")
                holder = self._lease_holder(conn, lock_resources)
                log_event(conn, "PREEMPT_LOST", plan_id, seq,
                         {"resources": lock_resources, "holder": holder,
                          "operator": operator_id})
                self._reject(conn, token, body, bh, "RESOURCE_BUSY", 409,
                             {"ok": False, "resources": lock_resources,
                              "holder": holder}, plan_id, seq)

            conn.execute(
                "UPDATE steps SET status='running', dispatched_at=? "
                "WHERE plan_id=? AND seq=?", (utc_now(), plan_id, seq))
            self._record_command(conn, token, body, bh, "applied",
                                 {"ok": True, "status": RUNNING})
            log_event(conn, "STEP_START", plan_id, seq,
                     {"token": token, "operator": operator_id,
                      "resources": lock_resources})
            self._checkpoint(conn, plan_id)
            conn.commit()
            # 崩溃点 C1：下发启动提交后、响应前
            self._maybe_crash("C1", plan_id, seq)
            return {"ok": True, "status": RUNNING}

        if command == "pause":
            if status != RUNNING:
                self._reject(conn, token, body, bh, "NOT_RUNNING", 409,
                             {"ok": False, "status": status})
            conn.execute("UPDATE steps SET status='paused' WHERE plan_id=? AND seq=?",
                         (plan_id, seq))
            self._record_command(conn, token, body, bh, "applied",
                                 {"ok": True, "status": PAUSED})
            log_event(conn, "STEP_PAUSE", plan_id, seq, {"token": token})
            self._checkpoint(conn, plan_id)
            conn.commit()
            # 崩溃点 C2：暂停命令提交后、响应前
            self._maybe_crash("C2", plan_id, seq)
            return {"ok": True, "status": PAUSED}

        # resume
        if status != PAUSED:
            self._reject(conn, token, body, bh, "NOT_PAUSED", 409,
                         {"ok": False, "status": status})
        conn.execute("UPDATE steps SET status='running' WHERE plan_id=? AND seq=?",
                     (plan_id, seq))
        self._record_command(conn, token, body, bh, "applied",
                             {"ok": True, "status": RUNNING})
        log_event(conn, "STEP_RESUME", plan_id, seq, {"token": token})
        self._checkpoint(conn, plan_id)
        conn.commit()
        return {"ok": True, "status": RUNNING}

    # ---- 传感器反馈 --------------------------------------------------- #
    def feedback(self, token: str, plan_id: str, step_seq: int,
                 observed_src: float, observed_dst: float,
                 flow_rate: Optional[float] = None) -> dict:
        """记录一次液位/流量反馈。仅 running/paused 步骤接受。

        按 min(源减少, 目标增加) 累计已核对转移量；反馈本身不推进步骤，
        由 confirm 在容差与稳性双通过后推进。
        """
        with self.lock:
            conn = connect(self.db_path)
            try:
                dup = conn.execute("SELECT * FROM feedback WHERE token=?",
                                   (token,)).fetchone()
                if dup:
                    return {"ok": True, "replay": True,
                            "plan_id": dup["plan_id"], "step_seq": dup["step_seq"]}
                step = conn.execute(
                    "SELECT * FROM steps WHERE plan_id=? AND seq=?",
                    (plan_id, step_seq)).fetchone()
                if not step:
                    raise ExecError(404, "STEP_NOT_FOUND", str(step_seq))
                if step["status"] not in (RUNNING, PAUSED):
                    raise ExecError(409, "FEEDBACK_BAD_STATE",
                                    f"步骤状态 {step['status']} 不接受反馈",
                                    {"status": step["status"]})
                topo, _, _ = self.load_topology(
                    self._plan_topology(conn, plan_id))
                tsrc, tdst = topo.tanks[step["source_tank"]], \
                    topo.tanks[step["target_tank"]]
                if not tsrc.min_vol - 1e-6 <= observed_src <= tsrc.max_vol + 1e-6 or \
                   not tdst.min_vol - 1e-6 <= observed_dst <= tdst.max_vol + 1e-6:
                    raise ExecError(422, "LEVEL_OUT_OF_RANGE", "反馈液位越出舱界")

                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO feedback (token,plan_id,step_seq,observed_src,"
                    "observed_dst,flow_rate,detail,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (token, plan_id, step_seq, observed_src, observed_dst,
                     flow_rate, dumps({"operator_feed": True}), utc_now()))
                transferred = self._reconcile(conn, topo, step,
                                              observed_src, observed_dst)
                conn.execute(
                    "UPDATE steps SET observed_src=?,observed_dst=?,transferred=? "
                    "WHERE plan_id=? AND seq=?",
                    (observed_src, observed_dst, transferred,
                     plan_id, step_seq))
                # 流量不足告警（有反馈但增量低于额定 25%）
                alert = None
                if flow_rate is not None:
                    pump = topo.pumps[step["pump"]]
                    if flow_rate < pump.rate * 0.25:
                        alert = self._alert(
                            conn, plan_id, step_seq, "FLOW_LOW", "warning",
                            f"流量 {flow_rate} 低于额定 {pump.rate*0.25}")
                log_event(conn, "FLOW_FEEDBACK", plan_id, step_seq,
                         {"token": token, "src": observed_src,
                          "dst": observed_dst, "rate": flow_rate,
                          "transferred": transferred})
                self._checkpoint(conn, plan_id)
                conn.commit()
                # 崩溃点 C3：收到部分流量提交后
                self._maybe_crash("C3", plan_id, step_seq)
                return {"ok": True, "transferred": transferred,
                        "target": step["volume"],
                        "tolerance": step["tolerance"],
                        "within_tolerance":
                            abs(transferred - step["volume"]) <= step["tolerance"],
                        "alert": alert}
            finally:
                conn.close()

    def _reconcile(self, conn, topo: Topology, step, src_vol, dst_vol) -> float:
        """以步骤开始时的舱位为基准（从账本反推），核对双向转移量取小值。"""
        start_src, start_dst = self._step_start_levels(conn, step)
        out = start_src - src_vol
        inn = dst_vol - start_dst
        moved = max(0.0, min(out, inn))
        return round(moved, 3)

    def _step_start_levels(self, conn, step) -> tuple[float, float]:
        """从事件账本恢复该步骤开始瞬间的源/目标舱体积。"""
        plan_id = step["plan_id"]
        seq = step["seq"]
        # 该方案全部已确认步骤累计量 + 本步前的基线
        basis = conn.execute("SELECT basis FROM plans WHERE id=?",
                             (plan_id,)).fetchone()
        vols = dict(json.loads(basis["basis"])["volumes"])
        rows = conn.execute(
            "SELECT * FROM steps WHERE plan_id=? AND seq<? AND status='confirmed' "
            "ORDER BY seq", (plan_id, seq)).fetchall()
        for r in rows:
            q = r["transferred"]
            vols[r["source_tank"]] = vols.get(r["source_tank"], 0) - q
            vols[r["target_tank"]] = vols.get(r["target_tank"], 0) + q
        return vols[step["source_tank"]], vols[step["target_tank"]]

    # ---- 确认 --------------------------------------------------------- #
    def confirm(self, token: str, plan_id: str, step_seq: int,
                operator_id: str) -> dict:
        with self.lock:
            conn = connect(self.db_path)
            try:
                existing = conn.execute(
                    "SELECT * FROM commands WHERE token=?", (token,)).fetchone()
                body = {"plan_id": plan_id, "step_seq": step_seq,
                        "command": "confirm", "operator_id": operator_id}
                bh = payload_hash(body)
                if existing:
                    if existing["payload_hash"] != bh:
                        raise ExecError(409, "TOKEN_PAYLOAD_MISMATCH",
                                        "同一确认令牌绑定了不同载荷")
                    result = json.loads(existing["result"])
                    if existing["status"] == "rejected":
                        code = result.get("rejected") or "REJECTED"
                        raise ExecError(result.get("http_status", 409), code,
                                        "首次确认已被拒绝（令牌回放）",
                                        {**{k: v for k, v in result.items()
                                            if k != "http_status"},
                                         "replay": True})
                    result["replay"] = True
                    return result

                step = conn.execute(
                    "SELECT * FROM steps WHERE plan_id=? AND seq=?",
                    (plan_id, step_seq)).fetchone()
                if not step:
                    raise ExecError(404, "STEP_NOT_FOUND", str(step_seq))
                if step["status"] == CONFIRMED:
                    result = {"ok": True, "status": CONFIRMED}
                    self._record_command(conn, token, body, bh, "applied",
                                         result)
                    conn.commit()
                    result["replay"] = True
                    return result
                if step["status"] not in (RUNNING, PAUSED):
                    raise ExecError(409, "CONFIRM_BAD_STATE",
                                    f"步骤状态 {step['status']} 不可确认",
                                    {"status": step["status"]})

                topo, _, _ = self.load_topology(self._plan_topology(conn, plan_id))
                transferred = step["transferred"]
                if step["observed_src"] is None:
                    raise ExecError(409, "NO_FEEDBACK", "尚无传感器反馈，无法核对")
                if abs(transferred - step["volume"]) > step["tolerance"]:
                    conn.execute("BEGIN IMMEDIATE")
                    alert = self._alert(
                        conn, plan_id, step_seq, "TRANSFER_OFF_TOLERANCE",
                        "critical",
                        f"已转移 {transferred} 与目标 {step['volume']} "
                        f"差 {abs(transferred-step['volume'])} > {step['tolerance']}")
                    result = {"ok": False, "rejected": "OUT_OF_TOLERANCE",
                              "transferred": transferred,
                              "tolerance": step["tolerance"], "alert": alert}
                    self._record_command(conn, token, body, bh, "rejected",
                                         {"http_status": 409, **result})
                    log_event(conn, "CONFIRM_DENIED", plan_id, step_seq,
                             {"reason": "tolerance", "transferred": transferred})
                    conn.commit()
                    raise ExecError(409, "OUT_OF_TOLERANCE",
                                    "转移量超出容差，不能确认", result)

                # 稳性重新计算（按确认后实测液位 + 当前装船）
                vols = self._volumes_after(conn, topo, plan_id, step)
                cargo = self._current_cargo(conn, plan_id)
                rep = evaluate(topo, cargo, vols)
                if not rep.ok:
                    conn.execute("BEGIN IMMEDIATE")
                    alert = self._alert(
                        conn, plan_id, step_seq, "STABILITY_FAIL", "critical",
                        f"确认时稳性复核不通过: "
                        f"{[v['kind'] for v in rep.violations]}")
                    result = {"ok": False, "rejected": "STABILITY_FAIL",
                              "violations": rep.violations, "alert": alert}
                    self._record_command(conn, token, body, bh, "rejected",
                                         {"http_status": 409, **result})
                    log_event(conn, "CONFIRM_DENIED", plan_id, step_seq,
                             {"reason": "stability",
                              "violations": rep.violations})
                    conn.commit()
                    raise ExecError(409, "STABILITY_FAIL",
                                    "确认时稳性复核不通过", result)

                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE steps SET status='confirmed',confirmed_at=?, "
                    "transferred=? WHERE plan_id=? AND seq=?",
                    (utc_now(), transferred, plan_id, step_seq))
                # 释放资源
                conn.execute(
                    "UPDATE leases SET status='released',released_at=? "
                    "WHERE plan_id=? AND step_seq=? AND status='held'",
                    (utc_now(), plan_id, step_seq))
                # 提交实测液位为真值
                for t, v in vols.items():
                    conn.execute(
                        "INSERT INTO tank_levels (tank,vol,updated_at,source) "
                        "VALUES (?,?,?,?) ON CONFLICT(tank) DO UPDATE SET "
                        "vol=excluded.vol,updated_at=excluded.updated_at,"
                        "source=excluded.source",
                        (t, v, utc_now(), f"confirm:{token}"))
                # 后续步骤 ready
                nxt = conn.execute(
                    "SELECT seq FROM steps WHERE plan_id=? AND seq=?",
                    (plan_id, step_seq + 1)).fetchone()
                prow = conn.execute("SELECT status FROM plans WHERE id=?",
                                    (plan_id,)).fetchone()
                plan_status = prow["status"]
                if plan_status == ACTIVE and not nxt:
                    plan_status = COMPLETED
                conn.execute("UPDATE plans SET status=? WHERE id=?",
                             (plan_status, plan_id))
                result = {"ok": True, "status": CONFIRMED,
                          "transferred": transferred,
                          "stability": planner._rep_dict(rep),
                          "plan_status": plan_status}
                self._record_command(conn, token, body, bh, "applied", result)
                log_event(conn, "STEP_CONFIRMED", plan_id, step_seq,
                         {"token": token, "transferred": transferred,
                          "stability": planner._rep_dict(rep),
                          "plan_status": plan_status})
                self._checkpoint(conn, plan_id)
                conn.commit()
                # 崩溃点 C4：确认步骤提交后
                self._maybe_crash("C4", plan_id, step_seq)
                return result
            finally:
                conn.close()

    def _volumes_after(self, conn, topo, plan_id, step) -> Dict[str, float]:
        """确认该步后全船液位：已确认步骤 + 当前步实测。"""
        basis = conn.execute("SELECT basis FROM plans WHERE id=?",
                             (plan_id,)).fetchone()
        vols = dict(json.loads(basis["basis"])["volumes"])
        rows = conn.execute(
            "SELECT * FROM steps WHERE plan_id=? AND seq<=? AND status='confirmed'",
            (plan_id, step["seq"])).fetchall()
        for r in rows:
            q = r["transferred"]
            vols[r["source_tank"]] -= q
            vols[r["target_tank"]] += q
        # 当前步尚未 confirmed，用实测量
        cur = conn.execute(
            "SELECT * FROM steps WHERE plan_id=? AND seq=?",
            (plan_id, step["seq"])).fetchone()
        if cur and cur["status"] != CONFIRMED and cur["observed_src"] is not None:
            start_src, start_dst = self._step_start_levels(conn, cur)
            vols[cur["source_tank"]] = cur["observed_src"]
            vols[cur["target_tank"]] = cur["observed_dst"]
        return {t: round(v, 3) for t, v in vols.items()}

    # ---- 修订：装船变化 / 甩箱 / 液位漂移 ---------------------------- #
    def revise(self, root_or_plan_id: str, milestones: List[Milestone],
               reason: str, operator_id: str = "chief",
               measured_volumes: Optional[Dict[str, float]] = None,
               freeze_only: bool = True) -> dict:
        """冻结尚未开始的步骤并生成新修订；已确认转移量必须保留。

        新方案基线 = 当前实测液位（含运行中步骤的已转移量），
        里程碑从当前装船进度重新给出。
        """
        with self.lock:
            conn = connect(self.db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT * FROM plans WHERE id=?",
                    (root_or_plan_id,)).fetchone()
                if not row:
                    raise ExecError(404, "PLAN_NOT_FOUND", root_or_plan_id)
                root_id = row["root_id"]
                cur = conn.execute(
                    "SELECT * FROM plans WHERE root_id=? ORDER BY revision_no DESC "
                    "LIMIT 1", (root_id,)).fetchone()
                if cur["status"] == COMPLETED:
                    raise ExecError(409, "PLAN_COMPLETED", "方案已全部完成，无需修订")

                # 冻结未开始（pending）步骤；running/paused 保持，confirmed 保留
                frozen = conn.execute(
                    "UPDATE steps SET status='frozen' WHERE plan_id=? "
                    "AND status='pending'", (cur["id"],)).rowcount
                conn.execute("UPDATE plans SET status=? WHERE id=?",
                             (SUPERSEDED, cur["id"]))
                log_event(conn, "REVISION_FREEZE", cur["id"], None,
                         {"frozen": frozen, "reason": reason,
                          "operator": operator_id})
                conn.commit()
            finally:
                conn.close()

            topo, payload, topology_id = self.load_topology(cur["topology_id"])
            vols = measured_volumes or self.current_volumes()
            # 运行中步骤的已转移量并入基线（实测液位优先；未上报则按 transferred）
            vols = self._effective_volumes(cur["id"], topo, vols)

            try:
                plan_doc = plan_all(topo, milestones, vols)
            except PlanError as e:
                conn = connect(self.db_path)
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    pid = f"pln_{os.urandom(6).hex()}"
                    conn.execute(
                        "INSERT INTO plans (id,topology_id,voyage_id,revision_no,"
                        "revision_of,root_id,status,reason,basis,payload,created_at) "
                        "VALUES (?,?,?,?,?,?, 'infeasible',?,?,?,?)",
                        (pid, cur["topology_id"], cur["voyage_id"],
                         cur["revision_no"] + 1, cur["id"], root_id,
                         f"{reason}:{e.code}",
                         dumps({"volumes": vols,
                                "milestones": _ms_brief(milestones)}),
                         dumps({"error": e.code, "detail": e.detail}),
                         utc_now()))
                    log_event(conn, "REVISION_INFEASIBLE", pid, None,
                             {"code": e.code, "reason": reason})
                    conn.commit()
                finally:
                    conn.close()
                raise ExecError(422, e.code, str(e), {"plan_id": pid, **e.detail})

            return self._persist_plan(topo, payload, cur["topology_id"],
                                      cur["voyage_id"], milestones, vols,
                                      plan_doc, reason,
                                      revision_of=cur["id"], root_id=root_id)

    def _effective_volumes(self, plan_id: str, topo: Topology,
                           measured: Dict[str, float]) -> Dict[str, float]:
        """以实测液位为准；缺失舱按方案基线+已确认量推算。"""
        conn = connect(self.db_path)
        try:
            basis = conn.execute("SELECT basis FROM plans WHERE id=?",
                                 (plan_id,)).fetchone()
            vols = dict(json.loads(basis["basis"])["volumes"])
            for r in conn.execute(
                    "SELECT * FROM steps WHERE plan_id=? AND status='confirmed'",
                    (plan_id,)):
                vols[r["source_tank"]] -= r["transferred"]
                vols[r["target_tank"]] += r["transferred"]
            run = conn.execute(
                "SELECT * FROM steps WHERE plan_id=? AND status IN "
                "('running','paused')", (plan_id,)).fetchone()
            if run and run["observed_src"] is not None:
                vols[run["source_tank"]] = run["observed_src"]
                vols[run["target_tank"]] = run["observed_dst"]
            for t in topo.tanks:
                if t in measured:
                    vols[t] = measured[t]
            return {t: round(v, 3) for t, v in vols.items()}
        finally:
            conn.close()

    # ---- 恢复 --------------------------------------------------------- #
    def recover(self) -> dict:
        """从事件账本 + 检查点重建唯一执行状态。

        规则（事件是权威来源）：
          STEP_START 之后无 STEP_CONFIRMED/STEP_PAUSE，且无 STEP_PAUSE
            -> 步骤保持 running，租约保持 held（不重复下发：启动事件已存在）；
          存在 held 租约但步骤已 confirmed -> 释放租约（C4 崩溃的补偿）；
          running/paused 步骤的液位以最后一次 FLOW_FEEDBACK 为准；
          所有命令/反馈按 token 幂等，恢复不会产生重复事件。
        """
        with self.lock:
            conn = connect(self.db_path)
            actions: List[dict] = []
            try:
                conn.execute("BEGIN IMMEDIATE")
                plans = conn.execute(
                    "SELECT id,status FROM plans WHERE status IN "
                    "('active','superseded')").fetchall()
                for p in plans:
                    pid = p["id"]
                    steps = conn.execute(
                        "SELECT seq,status FROM steps WHERE plan_id=?",
                        (pid,)).fetchall()
                    for s in steps:
                        if s["status"] in (RUNNING, PAUSED):
                            # 租约是否仍在
                            held = conn.execute(
                                "SELECT COUNT(*) c FROM leases WHERE plan_id=? "
                                "AND step_seq=? AND status='held'",
                                (pid, s["seq"])).fetchone()["c"]
                            n_res = conn.execute(
                                "SELECT COUNT(*) c FROM leases WHERE plan_id=? "
                                "AND step_seq=?", (pid, s["seq"])).fetchone()["c"]
                            if not held and n_res:
                                # C1 之后租约丢失（不应发生），重建占用
                                step = conn.execute(
                                    "SELECT resources,mutex_groups,pump FROM steps "
                                    "WHERE plan_id=? AND seq=?",
                                    (pid, s["seq"])).fetchone()
                                resources = json.loads(step["resources"]) + \
                                    [f"GROUP:{g}" for g in
                                     json.loads(step["mutex_groups"])]
                                ev = conn.execute(
                                    "SELECT detail FROM events WHERE plan_id=? "
                                    "AND step_seq=? AND kind='STEP_START' "
                                    "ORDER BY id DESC LIMIT 1",
                                    (pid, s["seq"])).fetchone()
                                det = json.loads(ev["detail"]) if ev else {}
                                for res in resources:
                                    conn.execute(
                                        "INSERT OR IGNORE INTO leases "
                                        "(resource,plan_id,step_seq,operator_id,"
                                        "token,status,acquired_at) "
                                        "VALUES (?,?,?,?,?, 'held',?)",
                                        (res, pid, s["seq"],
                                         det.get("operator", "recover"),
                                         det.get("token", "recover"), utc_now()))
                                actions.append({"action": "LEASE_REBUILT",
                                                "plan_id": pid, "seq": s["seq"]})
                            else:
                                actions.append({"action": "RUNNING_RESTORED",
                                                "plan_id": pid, "seq": s["seq"],
                                                "status": s["status"]})
                    # 已确认但租约未释放（C4 在释放后/响应前崩溃的兜底）
                    stale = conn.execute(
                        "SELECT DISTINCT step_seq FROM leases WHERE plan_id=? "
                        "AND status='held'", (pid,)).fetchall()
                    for z in stale:
                        st = conn.execute(
                            "SELECT status FROM steps WHERE plan_id=? AND seq=?",
                            (pid, z["step_seq"])).fetchone()
                        if st and st["status"] == CONFIRMED:
                            conn.execute(
                                "UPDATE leases SET status='released',released_at=? "
                                "WHERE plan_id=? AND step_seq=? AND status='held'",
                                (utc_now(), pid, z["step_seq"]))
                            actions.append({"action": "LEASE_RELEASED",
                                            "plan_id": pid, "seq": z["step_seq"]})
                    self._checkpoint(conn, pid)
                log_event(conn, "RECOVERY", None, None, {"actions": actions})
                conn.commit()
            finally:
                conn.close()
            return {"actions": actions}

    # ---- 辅助 --------------------------------------------------------- #
    def _record_command(self, conn, token, body, bh, status, result):
        conn.execute(
            "INSERT INTO commands (token,plan_id,step_seq,command,operator_id,"
            "payload_hash,status,result,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (token, body["plan_id"], body["step_seq"], body["command"],
             body["operator_id"], bh, status, dumps(result), utc_now()))

    def _alert(self, conn, plan_id, seq, kind, severity, message):
        conn.execute(
            "INSERT INTO alerts (plan_id,step_seq,kind,severity,message,created_at)"
            " VALUES (?,?,?,?,?,?)",
            (plan_id, seq, kind, severity, message, utc_now()))
        return {"kind": kind, "severity": severity, "message": message}

    def _checkpoint(self, conn, plan_id):
        rows = conn.execute(
            "SELECT seq,status,transferred FROM steps WHERE plan_id=? ORDER BY seq",
            (plan_id,)).fetchall()
        state = {"steps": {str(r["seq"]): {"status": r["status"],
                                           "transferred": r["transferred"]}
                           for r in rows},
                 "held": [{"resource": r["resource"], "seq": r["step_seq"]}
                          for r in conn.execute(
                              "SELECT resource,step_seq FROM leases WHERE "
                              "plan_id=? AND status='held'", (plan_id,))]}
        conn.execute(
            "INSERT INTO checkpoints (plan_id,state,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(plan_id) DO UPDATE SET state=excluded.state,"
            "updated_at=excluded.updated_at",
            (plan_id, dumps(state), utc_now()))

    def _lease_holder(self, conn, resources) -> Optional[dict]:
        for res in resources:
            r = conn.execute(
                "SELECT operator_id,plan_id,step_seq FROM leases WHERE resource=? "
                "AND status='held'", (res,)).fetchone()
            if r:
                return {"resource": res, "operator_id": r["operator_id"],
                        "plan_id": r["plan_id"], "step_seq": r["step_seq"]}
        return None

    def _plan_topology(self, conn, plan_id) -> str:
        r = conn.execute("SELECT topology_id FROM plans WHERE id=?",
                         (plan_id,)).fetchone()
        return r["topology_id"]

    def _loaded_n(self, conn, voyage_id) -> int:
        if not voyage_id:
            return 10 ** 9  # 未关联航次时里程碑条件视为已满足
        r = conn.execute("SELECT loaded_n FROM loading_state WHERE voyage_id=?",
                         (voyage_id,)).fetchone()
        return r["loaded_n"] if r else 0

    def _current_cargo(self, conn, plan_id) -> List[WeightItem]:
        r = conn.execute("SELECT voyage_id FROM plans WHERE id=?",
                         (plan_id,)).fetchone()
        if not r or not r["voyage_id"]:
            return []
        lr = conn.execute("SELECT loaded FROM loading_state WHERE voyage_id=?",
                          (r["voyage_id"],)).fetchone()
        if not lr:
            return []
        return [WeightItem(**it) if isinstance(it, dict) else it
                for it in json.loads(lr["loaded"])]

    def update_loading(self, voyage_id: str, items: List[WeightItem],
                       loaded_n: int, source: str = "manual"):
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            payload = [{"w": it.w, "x": it.x, "y": it.y, "z": it.z,
                        "tag": it.tag} for it in items]
            total = sum(it.w for it in items)
            conn.execute(
                "INSERT INTO loading_state (voyage_id,loaded,total,loaded_n,"
                "updated_at,source) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(voyage_id) DO UPDATE SET loaded=excluded.loaded,"
                "total=excluded.total,loaded_n=excluded.loaded_n,"
                "updated_at=excluded.updated_at,source=excluded.source",
                (voyage_id, dumps(payload), total, loaded_n, utc_now(), source))
            log_event(conn, "LOADING_UPDATED", None, None,
                     {"voyage_id": voyage_id, "loaded_n": loaded_n,
                      "total": total, "source": source})
            conn.commit()
        finally:
            conn.close()


def _ms_brief(milestones: List[Milestone]) -> list:
    return [{"label": m.label, "n_items": len(m.cargo),
             "expected_loaded_n": m.expected_loaded_n,
             "weight": round(sum(i.w for i in m.cargo), 2)}
            for m in milestones]
