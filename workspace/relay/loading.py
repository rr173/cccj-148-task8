"""装船网关：把中继记录对接到现有装船确认入口。

现有入口 = stowage.service.confirm_load(voyage_id, plan_id, container, slot_code)
- UNIQUE(voyage_id, container_id) 幂等；槽位必须与已发布方案一致，否则
  抛 ServiceError(409, SLOT_MISMATCH / NOT_IN_PLAN / SLOT_OCCUPIED ...)。

中继器在它**之前**做跨终端争用判定（同箱只保留一个可执行结果），
在它**之后**把错误码翻译成 pending 原因，供操作台处置。
"""
from __future__ import annotations

from stowage import service
from stowage.db import ServiceError, connect

from . import protocol

protocol_valid_slot = protocol.valid_slot
protocol_valid_container = protocol.valid_container


class GatewayError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def published_plan(conn, voyage_id):
    """当前航次已发布方案（“计划更替”后它是新的一版）。"""
    return service._latest_published(conn, voyage_id)


def plan_slot_for(conn, plan_id, container_id):
    row = conn.execute(
        "SELECT s.code FROM plan_entries pe JOIN slots s ON s.id=pe.slot_id "
        "WHERE pe.plan_id=? AND pe.container_id=?",
        (plan_id, container_id)).fetchone()
    return row["code"] if row else None


def resolve(voyage_id, container_id, slot, db_path):
    """预检：这条扫描映射到当前航次是否可执行。

    返回 (plan_row, expected_slot, reason)：
      reason 非空表示不可执行（pending），不调用业务入口。
    """
    conn = connect(db_path)
    try:
        plan = published_plan(conn, voyage_id)
        if not plan:
            return None, None, "NO_PUBLISHED_PLAN"
        expected = plan_slot_for(conn, plan["id"], container_id)
        if expected is None:
            return plan, None, "NOT_IN_PLAN"
        if expected != slot:
            return plan, expected, "PLAN_CHANGED"
        return plan, expected, None
    finally:
        conn.close()


def confirm(voyage_id, plan_id, container_id, slot, actor, db_path):
    """调用现有装船确认入口。返回 dict（含 confirmation_id、是否幂等回放）。"""
    try:
        return service.confirm_load(voyage_id, plan_id, container_id, slot,
                                    actor=actor, db_path=db_path)
    except ServiceError as e:
        raise GatewayError(e.code, e.message)
