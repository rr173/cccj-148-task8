"""接收服务。

收件顺序保证（按设备会话）：
  相同(会话,序号,内容)再到  -> 回原确认（duplicate），不重复写明细
  相同序号、不同内容        -> 隔离队列（quarantine），不动游标
  序号 > 游标+1             -> 暂存（staged），不推进游标；断层补齐后连续处理
  校验和错 / 任一条记录非法 -> 整包拒绝（rejected），绝不只写部分记录
  序号 == 游标+1            -> 落原始包 + 写全部明细 + 逐条调装船入口 + 推进游标

崩溃点（env RELAY_CRASH = P1|P2|P3，每个点只触发一次）：
  P1 落盘事务提交前（原始包/明细已写、未提交）
  P2 调用业务入口途中（整包事务已提交，部分箱已确认）
  P3 游标更新提交后（响应还没发回终端）
恢复只依赖 relay_events + 各业务表，重启后收件状态/装船确认/游标一致。

所有写入都是显式 BEGIN IMMEDIATE 事务（store.connect 默认 autocommit），
三个崩溃点与 COMMIT 的位置在代码中一目了然。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading

from . import loading, protocol, store
from .store import (APPLIED, PENDING, PK_ACCEPTED, PK_QUARANTINE,
                    PK_REJECTED, PK_STAGED, connect, log_event, utc_now)


class RecvError(Exception):
    def __init__(self, http_status, code, message):
        super().__init__(message)
        self.http_status = http_status
        self.code = code


class Receiver:
    def __init__(self, db_path: str, stowage_db: str, crash: str | None = None):
        self.db_path = db_path
        self.stowage_db = stowage_db
        store.init_db(db_path)
        self.lock = threading.RLock()   # 收件/处置串行，争用判定顺序确定
        self.crash = crash or os.environ.get("RELAY_CRASH")
        self._crashed: set[str] = set()

    # ------------------------------------------------------------------ #
    def _maybe_crash(self, point, session, seq):
        if self.crash == point and point not in self._crashed:
            self._crashed.add(point)
            try:
                os.write(2, f"[RELAY] 注入崩溃 {point} @ {session}#{seq}\n"
                         .encode())
                try:
                    os.fsync(2)
                except OSError:
                    pass
            except OSError:
                pass
            os._exit(91)  # 立即退出，不执行 finally / atexit

    # ------------------------------------------------------------------ #
    def upload(self, raw_text: str) -> dict:
        with self.lock:
            return self._upload(raw_text)

    def _autocommit_save(self, session, seq, raw_sha, raw_text, code, message):
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO raw_packets (session,seq,sha256,payload,status,"
                "error_code,message,received_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(session,seq,sha256) DO UPDATE SET "
                "error_code=excluded.error_code, message=excluded.message",
                (session or "?", seq if seq is not None else -1, raw_sha,
                 raw_text, PK_REJECTED, code, message, utc_now()))
            conn.commit()
        finally:
            conn.close()

    def _get_or_create_session(self, conn, session, device_id):
        row = conn.execute("SELECT * FROM sessions WHERE session=?",
                           (session,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO sessions (session,device_id,cursor,created_at) "
                "VALUES (?,?,-1,?)", (session, device_id, utc_now()))
            row = conn.execute("SELECT * FROM sessions WHERE session=?",
                               (session,)).fetchone()
        return row

    def _cursor(self, conn, session):
        row = conn.execute("SELECT cursor FROM sessions WHERE session=?",
                           (session,)).fetchone()
        return row["cursor"] if row else -1

    def _active_voyage(self, conn):
        return store.get_config(conn, "active_voyage")

    # ------------------------------------------------------------------ #
    def _upload(self, raw_text: str) -> dict:
        raw_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        try:
            env = json.loads(raw_text)
        except (ValueError, TypeError):
            self._autocommit_save(None, None, raw_sha, raw_text,
                                  "BAD_JSON", "不是合法 JSON")
            raise RecvError(400, "BAD_JSON", "请求体不是合法 JSON")

        try:
            protocol.validate_shape(env)
        except protocol.PacketError as e:
            meta = env if isinstance(env, dict) else {}
            self._autocommit_save(meta.get("session"), meta.get("seq"),
                                  raw_sha, raw_text, e.code, str(e))
            raise RecvError(400, e.code, str(e))

        session, seq = env["session"], env["seq"]

        if not protocol.verify_checksum(env):
            self._autocommit_save(session, seq, raw_sha, raw_text,
                                  "CHECKSUM_MISMATCH", "校验和不符")
            raise RecvError(422, "CHECKSUM_MISMATCH",
                            "校验和不符，整包拒绝（未写入任何记录）")
        try:
            protocol.validate_records(env)
        except protocol.RecordError as e:
            self._autocommit_save(session, seq, raw_sha, raw_text,
                                  "INVALID_RECORD", str(e))
            raise RecvError(422, "INVALID_RECORD",
                            f"第 {e.index} 条记录非法：{e}；整包拒绝")

        content_sha = env["checksum"]
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = self._get_or_create_session(conn, session, env["device_id"])
            cursor = cur["cursor"]

            # 相同(会话,序号,内容)：回放原确认
            row = conn.execute(
                "SELECT * FROM raw_packets WHERE session=? AND seq=? "
                "AND sha256=?", (session, seq, content_sha)).fetchone()
            if row:
                conn.commit()
                return self._replay_ack(row)

            # 同序号不同内容 => 隔离（被拒包不占序号）
            other = conn.execute(
                "SELECT 1 FROM raw_packets WHERE session=? AND seq=? "
                "AND status!=?", (session, seq, PK_REJECTED)).fetchone()
            if other or seq <= cursor:
                ack = self._quarantine(conn, env, content_sha)
                conn.commit()
                return ack

            # 断层：暂存原件
            if seq > cursor + 1:
                ack = self._stage(conn, env, content_sha, cursor)
                conn.commit()
                return ack

            # 连续序号：落盘（事务 A），P1 在提交前
            voyage_id = env.get("voyage_id") or self._active_voyage(conn)
            if not voyage_id:
                conn.rollback()
                raise RecvError(409, "NO_ACTIVE_VOYAGE",
                                "包未带 voyage_id 且未配置当前航次")
            self._persist_accepted(conn, env, content_sha, voyage_id)
            self._maybe_crash("P1", session, seq)
            conn.commit()  # ---- P1 之后：原始包+全部明细原子落盘 ----
        finally:
            conn.close()

        # 逐条调用现有装船确认入口（独立事务；P2 在中途）
        outcomes = self._apply_packet_records(session, seq,
                                              crash_point="P2")
        self._advance_cursor(session, seq)  # P3 在其事务提交后
        self._drain(session)
        conn = connect(self.db_path)
        try:
            return {"session": session, "seq": seq,
                    "cursor": self._cursor(conn, session),
                    "status": "accepted", "duplicate": False,
                    "records": outcomes}
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    def _replay_ack(self, row):
        conn = connect(self.db_path)
        try:
            if row["status"] == PK_QUARANTINE:
                return {"session": row["session"], "seq": row["seq"],
                        "status": "quarantine", "duplicate": True,
                        "reason": row["message"]}
            cursor = self._cursor(conn, row["session"])
            if row["status"] == PK_STAGED:
                return {"session": row["session"], "seq": row["seq"],
                        "status": "staged", "duplicate": True,
                        "cursor": cursor,
                        "missing": list(range(cursor + 1, row["seq"]))}
            records = conn.execute(
                "SELECT record_id,container,slot,status,reason,"
                "confirmation_id FROM scan_records WHERE session=? AND seq=? "
                "ORDER BY rowid", (row["session"], row["seq"])).fetchall()
            return {"session": row["session"], "seq": row["seq"],
                    "cursor": cursor, "status": "accepted",
                    "duplicate": True, "records": [dict(r) for r in records]}
        finally:
            conn.close()

    def _quarantine(self, conn, env, content_sha):
        msg = f"同一会话序号 {env['seq']} 已存在不同内容的包"
        conn.execute(
            "INSERT INTO raw_packets (session,seq,sha256,payload,status,"
            "error_code,message,received_at) VALUES (?,?,?,?,?,?,?,?)",
            (env["session"], env["seq"], content_sha,
             json.dumps(env, ensure_ascii=False), PK_QUARANTINE,
             "SEQ_CONTENT_CONFLICT", msg, utc_now()))
        log_event(conn, env["session"], env["seq"], "PACKET_QUARANTINED")
        return {"session": env["session"], "seq": env["seq"],
                "status": "quarantine", "reason": msg}

    def _stage(self, conn, env, content_sha, cursor):
        conn.execute(
            "INSERT INTO raw_packets (session,seq,sha256,payload,status,"
            "message,received_at) VALUES (?,?,?,?,?,?,?)",
            (env["session"], env["seq"], content_sha,
             json.dumps(env, ensure_ascii=False), PK_STAGED,
             f"等待缺失序号（当前游标 {cursor}）", utc_now()))
        log_event(conn, env["session"], env["seq"], "PACKET_STAGED",
                 {"cursor": cursor})
        return {"session": env["session"], "seq": env["seq"],
                "status": "staged", "cursor": cursor,
                "missing": list(range(cursor + 1, env["seq"]))}

    def _persist_accepted(self, conn, env, content_sha, voyage_id):
        session, seq = env["session"], env["seq"]
        conn.execute(
            "INSERT INTO raw_packets (session,seq,sha256,payload,status,"
            "received_at) VALUES (?,?,?,?,?,?)",
            (session, seq, content_sha, json.dumps(env, ensure_ascii=False),
             PK_ACCEPTED, utc_now()))
        log_event(conn, session, seq, "PACKET_ACCEPTED",
                 {"voyage_id": voyage_id})
        for r in env["records"]:
            conn.execute(
                "INSERT OR IGNORE INTO scan_records "
                "(record_id,session,seq,device_id,voyage_id,container,slot,"
                "collected_at,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["record_id"], session, seq, env["device_id"], voyage_id,
                 r["container"], r["slot"], r["collected_at"], "received",
                 utc_now()))

    # ------------------------------------------------------------------ #
    def _apply_packet_records(self, session, seq, crash_point=None):
        conn = connect(self.db_path)
        outcomes = []
        crashed = False
        try:
            rows = conn.execute(
                "SELECT * FROM scan_records WHERE session=? AND seq=? "
                "AND status='received' ORDER BY rowid",
                (session, seq)).fetchall()
            for idx, r in enumerate(rows):
                conn.execute("BEGIN IMMEDIATE")
                outcome = self._apply_one(conn, r)
                conn.commit()
                outcomes.append(outcome)
                if crash_point and idx == 0 and not crashed:
                    crashed = True
                    # P2：至少一条业务调用已提交，在“调用之间”退出
                    self._maybe_crash(crash_point, session, seq)
            return outcomes
        finally:
            conn.close()

    def _apply_one(self, conn, r) -> dict:
        rid = r["record_id"]
        owner = conn.execute(
            "SELECT record_id,session,device_id,status FROM scan_records "
            "WHERE voyage_id=? AND container=? AND status IN (?,?) "
            "AND record_id<>? ORDER BY rowid LIMIT 1",
            (r["voyage_id"], r["container"], APPLIED, PENDING, rid)).fetchone()
        if owner:
            return self._mark_pending(
                conn, r, "DUPLICATE_CONTAINER",
                f"该箱已有来自 {owner['device_id']}（{owner['session']}）的"
                f"{owner['status']} 结果")

        try:
            plan, expected, reason = loading.resolve(
                r["voyage_id"], r["container"], r["slot"], self.stowage_db)
        except loading.GatewayError as e:
            return self._mark_pending(conn, r, e.code, str(e))

        if reason == "NO_PUBLISHED_PLAN":
            return self._mark_pending(conn, r, reason, "当前航次没有已发布方案")
        if reason in ("NOT_IN_PLAN", "PLAN_CHANGED"):
            msg = ("箱不在当前已发布方案中" if reason == "NOT_IN_PLAN"
                   else f"计划已更替：方案槽位应为 {expected}，上报 {r['slot']}")
            return self._mark_pending(conn, r, reason, msg)

        try:
            res = loading.confirm(r["voyage_id"], plan["id"], r["container"],
                                  r["slot"], actor=f"relay:{r['device_id']}",
                                  db_path=self.stowage_db)
        except loading.GatewayError as e:
            return self._mark_pending(conn, r, e.code, str(e))

        if res.get("idempotent"):
            return self._mark_pending(
                conn, r, "CONFIRMED_ELSEWHERE",
                f"该箱在中继器之外已被确认（{res['confirmation_id']}）")

        conn.execute(
            "UPDATE scan_records SET status=?,plan_id=?,plan_version=?,"
            "resolved_slot=?,confirmation_id=?,reason=NULL,applied_at=? "
            "WHERE record_id=?",
            (APPLIED, plan["id"], plan["version"], r["slot"],
             res["confirmation_id"], utc_now(), rid))
        log_event(conn, r["session"], r["seq"], "RECORD_APPLIED",
                 {"record_id": rid, "container": r["container"],
                  "slot": r["slot"], "confirmation_id": res["confirmation_id"]})
        return {"record_id": rid, "container": r["container"],
                "slot": r["slot"], "status": APPLIED,
                "confirmation_id": res["confirmation_id"]}

    def _mark_pending(self, conn, r, reason, message):
        conn.execute("UPDATE scan_records SET status=?,reason=? WHERE record_id=?",
                     (PENDING, reason, r["record_id"]))
        log_event(conn, r["session"], r["seq"], "RECORD_PENDING",
                 {"record_id": r["record_id"], "container": r["container"],
                  "reason": reason, "message": message})
        return {"record_id": r["record_id"], "container": r["container"],
                "slot": r["slot"], "status": PENDING, "reason": reason,
                "message": message}

    # ------------------------------------------------------------------ #
    def _advance_cursor(self, session, seq):
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE sessions SET cursor=? WHERE session=?",
                         (seq, session))
            log_event(conn, session, seq, "CURSOR_ADVANCED", {"cursor": seq})
            conn.commit()  # ---- P3：游标已落盘 ----
            self._maybe_crash("P3", session, seq)
        finally:
            conn.close()

    def _drain(self, session):
        while True:
            conn = connect(self.db_path)
            try:
                cursor = self._cursor(conn, session)
                row = conn.execute(
                    "SELECT * FROM raw_packets WHERE session=? AND seq=? "
                    "AND status=?", (session, cursor + 1, PK_STAGED)).fetchone()
                if not row:
                    return
                env = json.loads(row["payload"])
                voyage_id = env.get("voyage_id") or self._active_voyage(conn)
            finally:
                conn.close()

            conn = connect(self.db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("UPDATE raw_packets SET status=? WHERE id=?",
                             (PK_ACCEPTED, row["id"]))
                log_event(conn, session, env["seq"], "STAGED_PROMOTED")
                if not voyage_id:
                    conn.rollback()
                    raise RecvError(409, "NO_ACTIVE_VOYAGE",
                                    "暂存包没有可用的当前航次")
                for rec in env["records"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO scan_records "
                        "(record_id,session,seq,device_id,voyage_id,container,"
                        "slot,collected_at,status,created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (rec["record_id"], session, env["seq"],
                         env["device_id"], voyage_id, rec["container"],
                         rec["slot"], rec["collected_at"], "received",
                         utc_now()))
                self._maybe_crash("P1", session, env["seq"])
                conn.commit()
            finally:
                conn.close()

            self._apply_packet_records(session, env["seq"], crash_point="P2")
            self._advance_cursor(session, env["seq"])

    # ------------------------------------------------------------------ #
    def recover(self) -> list[dict]:
        """从事件日志恢复：把每个会话推到 包/确认/游标 一致状态。"""
        actions = []
        with self.lock:
            conn = connect(self.db_path)
            try:
                sessions = [r["session"] for r in conn.execute(
                    "SELECT DISTINCT session FROM sessions")]
            finally:
                conn.close()

            for session in sessions:
                while True:
                    conn = connect(self.db_path)
                    try:
                        cursor = self._cursor(conn, session)
                        nxt = cursor + 1
                        row = conn.execute(
                            "SELECT * FROM raw_packets WHERE session=? "
                            "AND seq=? AND status=?",
                            (session, nxt, PK_ACCEPTED)).fetchone()
                        if not row:
                            break
                        env = json.loads(row["payload"])
                        voyage_id = env.get("voyage_id") or \
                            self._active_voyage(conn)
                    finally:
                        conn.close()

                    # 补明细（P1 提交前死 => 整体回滚，根本走不到这里；
                    # 走到这里说明已提交，INSERT OR IGNORE 幂等）
                    conn = connect(self.db_path)
                    try:
                        conn.execute("BEGIN IMMEDIATE")
                        restored = 0
                        for rec in env["records"]:
                            cur = conn.execute(
                                "INSERT OR IGNORE INTO scan_records "
                                "(record_id,session,seq,device_id,voyage_id,"
                                "container,slot,collected_at,status,created_at)"
                                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (rec["record_id"], session, nxt,
                                 env["device_id"], voyage_id, rec["container"],
                                 rec["slot"], rec["collected_at"], "received",
                                 utc_now()))
                            restored += cur.rowcount
                        if restored:
                            log_event(conn, session, nxt,
                                      "RECOVERY_RECORDS_RESTORED",
                                      {"restored": restored})
                        conn.commit()
                    finally:
                        conn.close()

                    outcomes = self._apply_packet_records(session, nxt)
                    self._advance_cursor(session, nxt)
                    actions.append({"session": session, "seq": nxt,
                                    "records": outcomes})

                self._drain(session)
        return actions
