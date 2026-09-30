"""接收端 SQLite 状态库。

表分四组：
  会话/游标     sessions
  原始包        raw_packets（永远保留原件：接受/重放/隔离/暂存/拒绝都落盘）
  收件明细      scan_records（记录如何映射到当前航次、是否已调装船确认）
  操作台        dispositions（按 request_id 幂等，同一处置只记一次）
  恢复日志      relay_events（只追加；重启后据此把系统推回一致状态）

所有状态推进都以 relay_events 为 WAL：先写意图再推进，重启时按日志补偿。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session     TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL,
    cursor      INTEGER NOT NULL DEFAULT -1,  -- 已连续确认的最大序号（下一个期望 = cursor+1；首包序号为 0）
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_packets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    payload      TEXT NOT NULL,                 -- 原始 JSON 文本，一字节不改
    status       TEXT NOT NULL,                 -- accepted|staged|duplicate|quarantine|rejected
    error_code   TEXT,
    message      TEXT,
    received_at  TEXT NOT NULL,
    UNIQUE (session, seq, sha256)
);
CREATE INDEX IF NOT EXISTS idx_raw_session_seq ON raw_packets(session, seq);

CREATE TABLE IF NOT EXISTS scan_records (
    record_id        TEXT PRIMARY KEY,          -- 终端生成的全局记录 ID
    session          TEXT NOT NULL,
    seq              INTEGER NOT NULL,
    device_id        TEXT NOT NULL,
    voyage_id        TEXT NOT NULL,
    container        TEXT NOT NULL,
    slot             TEXT NOT NULL,             -- 终端上报的实际槽位
    collected_at     TEXT NOT NULL,
    plan_id          TEXT,                      -- 应用时解析到的已发布方案
    plan_version     INTEGER,
    resolved_slot    TEXT,                      -- 处置后实际确认的槽位（可能 != slot）
    status           TEXT NOT NULL,             -- received|applied|pending|ignored
    reason           TEXT,                      -- pending 原因
    confirmation_id  TEXT,                      -- 现有装船确认入口返回的确认 ID
    applied_at       TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE (session, record_id)
);
CREATE INDEX IF NOT EXISTS idx_scan_voyage_container
    ON scan_records(voyage_id, container);
CREATE INDEX IF NOT EXISTS idx_scan_session_seq
    ON scan_records(session, seq);

CREATE TABLE IF NOT EXISTS dispositions (
    request_id   TEXT PRIMARY KEY,              -- 操作台生成的幂等键
    record_id    TEXT NOT NULL,
    action       TEXT NOT NULL,                 -- ignore|remap|reconfirm
    new_slot     TEXT,
    note         TEXT,
    operator_id  TEXT,
    status       TEXT NOT NULL,                 -- applied|noop|rejected
    result       TEXT,                          -- JSON 结果（重放时原样返回）
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relay_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ev_session_seq ON relay_events(session, seq);

CREATE TABLE IF NOT EXISTS config (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    # isolation_level=None => autocommit；所有多步写入由调用方显式
    # BEGIN IMMEDIATE / COMMIT 控制，崩溃点的提交边界清晰可见。
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


# 记录终态
RECEIVED = "received"
APPLIED = "applied"
PENDING = "pending"
IGNORED = "ignored"

# 原始包终态
PK_ACCEPTED = "accepted"
PK_STAGED = "staged"
PK_DUPLICATE = "duplicate"
PK_QUARANTINE = "quarantine"
PK_REJECTED = "rejected"


def log_event(conn, session, seq, kind, detail=None):
    conn.execute(
        "INSERT INTO relay_events (session, seq, kind, detail, created_at) "
        "VALUES (?,?,?,?,?)",
        (session, seq, kind,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None,
         utc_now()))


def get_config(conn, key, default=None):
    row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(conn, key, value):
    conn.execute("INSERT INTO config(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))
