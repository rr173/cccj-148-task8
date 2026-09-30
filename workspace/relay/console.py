"""操作台处置：对“待处理”记录可忽略、重新映射或重新调用确认。

同一 request_id 的处置请求只记一次：重复提交回放首次结果，
不二次调用业务入口、不产生第二条 dispositions。
"""
from __future__ import annotations

import json

from . import loading
from .store import (APPLIED, IGNORED, PENDING, connect, log_event, utc_now)


class ConsoleError(Exception):
    def __init__(self, http_status, code, message):
        super().__init__(message)
        self.http_status = http_status
        self.code = code


def dispose(recv, request_id, record_id, action, new_slot=None,
            operator_id="operator", note=None) -> dict:
    with recv.lock:
        conn = connect(recv.db_path)
        try:
            # 幂等：同一处置请求只记一次（只读，无需事务）
            prev = conn.execute(
                "SELECT * FROM dispositions WHERE request_id=?",
                (request_id,)).fetchone()
            if prev:
                return {"request_id": request_id, "idempotent": True,
                        **json.loads(prev["result"])}

            r = conn.execute("SELECT * FROM scan_records WHERE record_id=?",
                             (record_id,)).fetchone()
            if not r:
                raise ConsoleError(404, "RECORD_NOT_FOUND",
                                   f"未知记录 {record_id}")

            conn.execute("BEGIN IMMEDIATE")
            if action == "ignore":
                result = _do_ignore(conn, r, operator_id, note)
            elif action == "remap":
                result = _do_remap(conn, recv, r, new_slot, operator_id, note)
            elif action == "reconfirm":
                result = _do_reconfirm(conn, recv, r, operator_id, note)
            else:
                raise ConsoleError(400, "BAD_ACTION",
                                   "action 必须是 ignore|remap|reconfirm")

            conn.execute(
                "INSERT INTO dispositions (request_id,record_id,action,"
                "new_slot,note,operator_id,status,result,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (request_id, record_id, action, new_slot, note, operator_id,
                 result["outcome"], json.dumps(result, ensure_ascii=False),
                 utc_now()))
            conn.commit()
            return {"request_id": request_id, "idempotent": False, **result}
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()


def _do_ignore(conn, r, operator_id, note):
    if r["status"] not in (PENDING, APPLIED, IGNORED):
        raise ConsoleError(409, "NOT_DISPOSABLE",
                           f"记录状态 {r['status']} 不可处置")
    conn.execute("UPDATE scan_records SET status=? WHERE record_id=?",
                 (IGNORED, r["record_id"]))
    log_event(conn, r["session"], r["seq"], "RECORD_IGNORED",
              {"record_id": r["record_id"], "operator": operator_id,
               "note": note})
    return {"outcome": "applied", "record_id": r["record_id"],
            "status": IGNORED, "message": "已忽略，不再尝试装船确认"}


def _resolve_published(conn, recv, r):
    return loading.resolve(r["voyage_id"], r["container"], None,
                           recv.stowage_db)[:2]


def _do_remap(conn, recv, r, new_slot, operator_id, note):
    if r["status"] != PENDING:
        raise ConsoleError(409, "NOT_PENDING",
                           "只有待处理记录可以重新映射")
    if not new_slot:
        raise ConsoleError(400, "NEW_SLOT_REQUIRED", "remap 需要 new_slot")
    if not loading.protocol_valid_slot(new_slot):
        raise ConsoleError(422, "BAD_SLOT", f"非法槽位号 {new_slot}")

    plan, expected = _resolve_published(conn, recv, r)
    if not plan:
        raise ConsoleError(409, "NO_PUBLISHED_PLAN", "当前航次没有已发布方案")
    if expected is None:
        raise ConsoleError(409, "NOT_IN_PLAN",
                           "该箱不在当前已发布方案中，无法重映射")
    if expected != new_slot:
        raise ConsoleError(
            409, "REMAP_SLOT_MISMATCH",
            f"当前方案要求该箱在 {expected}，不能重映射到 {new_slot}；"
            "如确需偏置，请走配载系统改方案")

    try:
        res = loading.confirm(r["voyage_id"], plan["id"], r["container"],
                              new_slot, actor=f"console:{operator_id}",
                              db_path=recv.stowage_db)
    except loading.GatewayError as e:
        raise ConsoleError(409, e.code, str(e))

    conn.execute(
        "UPDATE scan_records SET status=?, plan_id=?, plan_version=?, "
        "resolved_slot=?, confirmation_id=?, reason=NULL, applied_at=? "
        "WHERE record_id=?",
        (APPLIED, plan["id"], plan["version"], new_slot,
         res["confirmation_id"], utc_now(), r["record_id"]))
    log_event(conn, r["session"], r["seq"], "RECORD_REMAPPED",
              {"record_id": r["record_id"], "from": r["slot"],
               "to": new_slot, "operator": operator_id, "note": note,
               "confirmation_id": res["confirmation_id"]})
    return {"outcome": "applied", "record_id": r["record_id"],
            "status": APPLIED, "resolved_slot": new_slot,
            "confirmation_id": res["confirmation_id"],
            "message": f"已重映射到 {new_slot} 并完成装船确认"}


def _do_reconfirm(conn, recv, r, operator_id, note):
    if r["status"] != PENDING:
        raise ConsoleError(409, "NOT_PENDING",
                           "只有待处理记录可以重新调用确认")
    plan, expected, reason = loading.resolve(
        r["voyage_id"], r["container"], r["slot"], recv.stowage_db)

    # 箱已在别处确认：现有装船入口保持幂等，重调仍命中旧确认
    if reason is None:
        already = conn.execute(
            "SELECT record_id, confirmation_id FROM scan_records "
            "WHERE voyage_id=? AND container=? AND status=? "
            "AND record_id<>?",
            (r["voyage_id"], r["container"], APPLIED,
             r["record_id"])).fetchone()
        try:
            res = loading.confirm(r["voyage_id"], plan["id"], r["container"],
                                  r["slot"], actor=f"console:{operator_id}",
                                  db_path=recv.stowage_db)
        except loading.GatewayError as e:
            return _stay_pending(conn, r, e.code, str(e), operator_id, note)
        # 业务入口幂等命中：说明该箱的装船确认已归属于别处（另一台终端或
        # 中继器之外），同一箱只能保留一项可执行结果，本记录保持待处理。
        if res.get("idempotent"):
            who = (already["record_id"] if already
                   else f"中继器之外({res['confirmation_id']})")
            return _stay_pending(
                conn, r, "CONFIRMED_ELSEWHERE",
                f"重调命中 {who} 的既有确认（{res['confirmation_id']}），"
                "同一箱只保留一项可执行结果",
                operator_id, note)
        if already:
            # 业务入口未幂等命中、中继侧却存在别的 applied（理论上不应到达）
            return _stay_pending(
                conn, r, "DUPLICATE_CONTAINER",
                f"{already['record_id']} 已持有可执行结果",
                operator_id, note)
        conn.execute(
            "UPDATE scan_records SET status=?, plan_id=?, plan_version=?, "
            "resolved_slot=?, confirmation_id=?, reason=NULL, applied_at=? "
            "WHERE record_id=?",
            (APPLIED, plan["id"], plan["version"], r["slot"],
             res["confirmation_id"], utc_now(), r["record_id"]))
        log_event(conn, r["session"], r["seq"], "RECORD_RECONFIRMED",
                  {"record_id": r["record_id"], "operator": operator_id,
                   "note": note, "confirmation_id": res["confirmation_id"]})
        return {"outcome": "applied", "record_id": r["record_id"],
                "status": APPLIED, "resolved_slot": r["slot"],
                "confirmation_id": res["confirmation_id"],
                "message": "重新调用确认成功"}

    return _stay_pending(
        conn, r, reason,
        f"重调条件仍不满足（{reason}；方案槽位 {expected}，上报 {r['slot']}）",
        operator_id, note)


def _stay_pending(conn, r, reason, message, operator_id, note):
    conn.execute("UPDATE scan_records SET reason=? WHERE record_id=?",
                 (reason, r["record_id"]))
    log_event(conn, r["session"], r["seq"], "RECORD_RECONFIRM_STAYED",
              {"record_id": r["record_id"], "operator": operator_id,
               "note": note, "reason": reason})
    return {"outcome": "noop", "record_id": r["record_id"],
            "status": PENDING, "reason": reason, "message": message}
