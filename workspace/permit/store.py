"""岸桥作业许可 / 装船波次互锁 —— SQLite 持久层。

表分组：
  配载底账     hatches / hatch_conflicts / containers / plans / plan_entries
  波次         waves / wave_containers（含由堆叠顺序推导出的前置边）
  许可         permits / permit_hatches / permit_containers
  扫描         loading_scans（装船记录，按箱唯一）
  授权异常     auth_exceptions
  幂等         idem_records（波次发布/许可转交/扫描确认/异常处置）

所有多步写操作都在单个 BEGIN IMMEDIATE 事务内完成：进程在任何一步之间退出
都会被 SQLite 整体回滚；启动时 reconcile() 再做一次对账兜底，保证：
  - 同一箱不会同时属于两张有效许可（DB 级部分唯一索引）；
  - 同一舱区不会被不同岸桥在重叠时段占用（应用级冲突检查 + 冲突表）；
  - 装船记录与波次进度同源（progress 由 loading_scans 重算可修复）。
"""
from __future__ import annotations

import os
import sqlite3

DB_PATH = os.environ.get(
    "PERMIT_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "permit.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS hatches (
    code        TEXT PRIMARY KEY,                 -- 舱区编号，如 H1
    bay_from    INTEGER NOT NULL,
    bay_to      INTEGER NOT NULL,
    name        TEXT
);

CREATE TABLE IF NOT EXISTS hatch_conflicts (
    hatch_a     TEXT NOT NULL,                    -- 相邻冲突舱区（对称存储两行）
    hatch_b     TEXT NOT NULL,
    PRIMARY KEY (hatch_a, hatch_b)
);

CREATE TABLE IF NOT EXISTS containers (
    id          TEXT PRIMARY KEY,                 -- 箱号
    hatch_code  TEXT NOT NULL,
    stack_key   TEXT NOT NULL,                    -- 同一物理堆位标识（bay-row）
    stack_seq   INTEGER NOT NULL,                 -- 堆内自底向上序号：0=最底层，
                                                  -- 小数必须先装（在下面）
    tier_code   TEXT,
    FOREIGN KEY (hatch_code) REFERENCES hatches(code)
);
CREATE INDEX IF NOT EXISTS idx_c_stack ON containers(stack_key, stack_seq);

CREATE TABLE IF NOT EXISTS plans (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft',    -- draft | published
    created_at  TEXT NOT NULL,
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS plan_entries (
    plan_id       TEXT NOT NULL,
    container_id  TEXT NOT NULL,
    hatch_code    TEXT NOT NULL,
    PRIMARY KEY (plan_id, container_id)
);

CREATE TABLE IF NOT EXISTS waves (
    id          TEXT PRIMARY KEY,
    plan_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'released', -- released | in_progress | completed
    total       INTEGER NOT NULL DEFAULT 0,
    executed    INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS wave_containers (
    wave_id       TEXT NOT NULL,
    container_id  TEXT NOT NULL,
    hatch_code    TEXT NOT NULL,
    stack_key     TEXT NOT NULL,
    stack_seq     INTEGER NOT NULL,
    prereq_ids    TEXT NOT NULL DEFAULT '[]',     -- JSON：同堆下层且同波次的箱
    executed       INTEGER NOT NULL DEFAULT 0,
    executed_at    TEXT,
    scan_id        TEXT,                          -- 装船记录
    owner_permit   TEXT,                          -- 当前归属许可（执行后保留原归属）
    released       INTEGER NOT NULL DEFAULT 0,    -- 异常作废后脱离许可
    PRIMARY KEY (wave_id, container_id)
);
CREATE INDEX IF NOT EXISTS idx_wc_stack ON wave_containers(wave_id, stack_key, stack_seq);
-- 前置必须存在；SQLite 不强制数组内容，由服务层推导

CREATE TABLE IF NOT EXISTS permits (
    id            TEXT PRIMARY KEY,               -- 许可编号，如 PMT-0001
    wave_id       TEXT NOT NULL,
    crane_id      TEXT NOT NULL,
    revision      INTEGER NOT NULL DEFAULT 1,     -- 乐观锁：每次修改 +1
    status        TEXT NOT NULL DEFAULT 'active', -- active | suspended | revoked
    valid_from    TEXT NOT NULL,                  -- ISO8601，含时区
    valid_to      TEXT NOT NULL,
    created_by    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    suspend_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_permit_wave ON permits(wave_id);

CREATE TABLE IF NOT EXISTS permit_hatches (
    permit_id   TEXT NOT NULL,
    hatch_code  TEXT NOT NULL,
    PRIMARY KEY (permit_id, hatch_code)
);

CREATE TABLE IF NOT EXISTS permit_containers (
    permit_id     TEXT NOT NULL,
    wave_id       TEXT NOT NULL,
    container_id  TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'active',
                       -- active=未执行且归属本许可；executed=已装船（保留原归属）；
                       -- released=许可调整/撤销/作废后脱离本许可
    PRIMARY KEY (permit_id, wave_id, container_id)
);
-- 互锁核心（DB 级）：同一波次中尚未执行的箱，全库至多有一个 active 属主。
-- executed 行保留原归属、released 行已释放，二者都不参与唯一约束。
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_container_owner
    ON permit_containers(wave_id, container_id) WHERE state='active';

CREATE TABLE IF NOT EXISTS loading_scans (
    id            TEXT PRIMARY KEY,
    wave_id       TEXT NOT NULL,
    permit_id     TEXT NOT NULL,                   -- 扫描时携带并验证通过的许可
    permit_rev    INTEGER NOT NULL,                -- 扫描时的修订号快照
    container_id  TEXT NOT NULL,
    slot_code     TEXT,
    scanner_id    TEXT,
    scanned_at    TEXT NOT NULL,                   -- 扫描声称时间
    created_at    TEXT NOT NULL,
    UNIQUE (wave_id, container_id)                 -- 同箱只装一次（幂等）
);

CREATE TABLE IF NOT EXISTS auth_exceptions (
    id            TEXT PRIMARY KEY,
    wave_id       TEXT,
    permit_id     TEXT,                            -- 扫描引用的许可（可能已撤销）
    permit_rev    INTEGER,
    container_id  TEXT,
    scanner_id    TEXT,
    reason        TEXT NOT NULL,
        -- UNKNOWN_PERMIT | STALE_REVISION | PERMIT_SUSPENDED | PERMIT_REVOKED
        -- | PERMIT_EXPIRED | NOT_IN_WAVE | OUT_OF_HATCH | PERMIT_RELEASED
    detail        TEXT,
    status        TEXT NOT NULL DEFAULT 'open',   -- open | transferred | restored | voided
    resolved_permit_id TEXT,                       -- 转交/恢复后的目标许可
    resolved_by   TEXT,
    resolved_at   TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exc_status ON auth_exceptions(status);

CREATE TABLE IF NOT EXISTS idem_records (
    idem_key    TEXT PRIMARY KEY,                  -- 客户端幂等键
    scope       TEXT NOT NULL,                     -- wave_publish | permit_transfer | scan | exception
    request_hash TEXT NOT NULL,                    -- 同键不同体 -> 409
    status_code INTEGER NOT NULL,
    response    TEXT NOT NULL,                     -- JSON 原响应，重放
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    actor       TEXT,
    action      TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
"""


def connect(path=None) -> sqlite3.Connection:
    # autocommit + 显式 BEGIN IMMEDIATE：提交边界即崩溃边界，和既有子系统一致。
    conn = sqlite3.connect(path or DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db(path=None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


class PermitError(Exception):
    """业务规则失败；status 映射为 HTTP 状态码。"""

    def __init__(self, status, code, message, extra=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}
