"""压载模块 SQLite 状态库。

表组：
  topology       大副录入的压载拓扑（舱/泵/阀/管路），保留历史版本
  plans          方案与修订（链式 revision_of），payload 含全部分阶段步骤
  steps          步骤物化视图（状态机推进都落在这张表）
  leases         泵/阀/互斥管段占用租约（资源级唯一索引保证抢占唯一性）
  commands       执行命令（按 token 幂等：同 token 回放，载荷变化拒绝）
  feedback       传感器反馈（按 token 幂等，部分流量可多次上报）
  tank_levels    各舱实测液位（物理真值，恢复的依据之一）
  alerts         偏差告警（流量不足、液位漂移、稳性越限等）
  events         只追加事件账本（恢复唯一状态的权威来源）
  checkpoints    方案级状态检查点（与事件同事务写入）
  loading_state  装船进度快照（来自已发布方案 + 确认记录/手工录入）

所有多步写入均在显式 BEGIN IMMEDIATE 事务中提交，提交边界即崩溃边界。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS topology (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id            TEXT PRIMARY KEY,
    topology_id   TEXT NOT NULL,
    voyage_id     TEXT,
    revision_no   INTEGER NOT NULL DEFAULT 1,
    revision_of   TEXT,                       -- 上一修订 id
    root_id       TEXT NOT NULL,              -- 首版 id（修订链根）
    status        TEXT NOT NULL,              -- active|superseded|completed|infeasible
    reason        TEXT,
    basis         TEXT NOT NULL,              -- JSON：生成时液位/装船/水量基准
    payload       TEXT NOT NULL,              -- JSON：phases/steps/目标稳性
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_root ON plans(root_id, revision_no);

CREATE TABLE IF NOT EXISTS steps (
    plan_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,             -- 全方案顺序号（跨 phase 连续）
    phase       INTEGER NOT NULL,
    source_tank TEXT NOT NULL,
    target_tank TEXT NOT NULL,
    volume      REAL NOT NULL,
    tolerance   REAL NOT NULL,
    pump        TEXT NOT NULL,
    resources   TEXT NOT NULL,                -- JSON 资源 id 列表（泵+阀）
    mutex_groups TEXT NOT NULL,               -- JSON 互斥组列表
    start_condition TEXT NOT NULL,            -- JSON 开始条件
    status      TEXT NOT NULL,                -- pending|ready|running|paused|confirmed|frozen
    transferred REAL NOT NULL DEFAULT 0,      -- 已核对转移量（源/目标取小）
    observed_src REAL,
    observed_dst REAL,
    dispatched_at TEXT,
    confirmed_at  TEXT,
    PRIMARY KEY (plan_id, seq)
);

CREATE TABLE IF NOT EXISTS leases (
    resource    TEXT NOT NULL,
    plan_id     TEXT NOT NULL,
    step_seq    INTEGER NOT NULL,
    operator_id TEXT NOT NULL,
    token       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'held', -- held|released
    acquired_at TEXT NOT NULL,
    released_at TEXT,
    PRIMARY KEY (resource, plan_id, step_seq, status)
);
-- 同一资源至多一条 held 租约（数据库级抢占仲裁）
CREATE UNIQUE INDEX IF NOT EXISTS idx_lease_held
    ON leases(resource) WHERE status='held';

CREATE TABLE IF NOT EXISTS commands (
    token       TEXT PRIMARY KEY,
    plan_id     TEXT NOT NULL,
    step_seq    INTEGER NOT NULL,
    command     TEXT NOT NULL,                -- start|pause|resume
    operator_id TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status      TEXT NOT NULL,                -- applied|rejected
    result      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
    token       TEXT PRIMARY KEY,
    plan_id     TEXT NOT NULL,
    step_seq    INTEGER NOT NULL,
    observed_src REAL NOT NULL,
    observed_dst REAL NOT NULL,
    flow_rate   REAL,
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tank_levels (
    tank        TEXT PRIMARY KEY,
    vol         REAL NOT NULL,
    updated_at  TEXT NOT NULL,
    source      TEXT NOT NULL                 -- init|flow:{token}|confirm:{token}
);

CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     TEXT,
    step_seq    INTEGER,
    kind        TEXT NOT NULL,
    severity    TEXT NOT NULL,                -- info|warning|critical
    message     TEXT NOT NULL,
    acked       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     TEXT,
    kind        TEXT NOT NULL,
    step_seq    INTEGER,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_plan ON events(plan_id, id);

CREATE TABLE IF NOT EXISTS checkpoints (
    plan_id     TEXT PRIMARY KEY,
    state       TEXT NOT NULL,                -- JSON：步骤状态/已确认量/租约摘要
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loading_state (
    voyage_id   TEXT PRIMARY KEY,
    loaded      TEXT NOT NULL,                -- JSON：已装船重量项
    total       INTEGER NOT NULL,
    loaded_n    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL,
    source      TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def init_db(path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def log_event(conn, kind, plan_id=None, step_seq=None, detail=None):
    conn.execute(
        "INSERT INTO events (plan_id,kind,step_seq,detail,created_at) "
        "VALUES (?,?,?,?,?)",
        (plan_id, kind, step_seq,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None,
         utc_now()))


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)
