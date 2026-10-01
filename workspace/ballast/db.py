"""压载模块 SQLite 持久层。

与配载系统（stowage）共用同一个数据库文件：本模块只写 ballast_* 表，
已发布配载方案（plans/plan_entries）与装船确认（confirmations）从
stowage 的表里只读取得。

一致性模型与配载系统相同：
  * 所有多步写入都在单个 BEGIN IMMEDIATE 事务里完成，崩溃不留半成品；
  * SQLite 串行化写者，乐观检查（步骤仍是 pending？泵仍空闲？修订号仍最新？）
    在同一事务内执行，负者收到 409；
  * 部分唯一索引 UNIQUE(voyage_id, pump_id) WHERE released=0 让
    「同一泵组同时被两名值班员抢占」在数据库层只有一方成功；
  * ballast_ledger 只追加，ballast_checkpoints 在每次状态跃迁后落快照，
    重启恢复只依赖账本 + 最新液位。
"""
import os
import sqlite3

from stowage.db import DB_PATH, SCHEMA as STOWAGE_SCHEMA, ServiceError  # noqa: F401

SCHEMA = """
CREATE TABLE IF NOT EXISTS ballast_config (
    voyage_id TEXT PRIMARY KEY,
    max_heel_deg REAL NOT NULL DEFAULT 3.0,    -- 横倾限值（度）
    max_draft REAL NOT NULL DEFAULT 12.0,      -- 最大平均吃水 m
    min_draft REAL NOT NULL DEFAULT 2.0,       -- 最小平均吃水 m
    max_bm REAL NOT NULL DEFAULT 8.0,          -- 船体梁弯矩代理限值（舱体受力）
    tpc REAL NOT NULL DEFAULT 25.0,            -- 每厘米吃水吨数（吃水近似）
    tolerance REAL NOT NULL DEFAULT 2.0,       -- 调水目标默认允许误差 m3
    drift_tol REAL NOT NULL DEFAULT 1.0,       -- 液位漂移告警阈值 m3
    low_flow_ratio REAL NOT NULL DEFAULT 0.3   -- 流量不足判定（占泵额定比）
);

CREATE TABLE IF NOT EXISTS ballast_tanks (
    id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    name TEXT NOT NULL,
    capacity REAL NOT NULL,          -- 舱容 m3
    min_level REAL NOT NULL,         -- 抽空保护下限 m3
    max_level REAL NOT NULL,         -- 安全上限 m3（舱体受力）
    level REAL NOT NULL,             -- 当前液位 m3（最新传感器值）
    x REAL NOT NULL,                 -- 纵向 m（艏+）
    y REAL NOT NULL,                 -- 横向 m（右舷+）
    z REAL NOT NULL,                 -- 垂向 m
    density REAL NOT NULL DEFAULT 1.025,
    UNIQUE (voyage_id, name)
);

CREATE TABLE IF NOT EXISTS ballast_pumps (
    id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    name TEXT NOT NULL,
    rate REAL NOT NULL,              -- 额定流量 m3/h
    UNIQUE (voyage_id, name)
);

CREATE TABLE IF NOT EXISTS ballast_valves (
    id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    name TEXT NOT NULL,
    UNIQUE (voyage_id, name)
);

CREATE TABLE IF NOT EXISTS ballast_pipes (
    id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    name TEXT NOT NULL,
    a_node TEXT NOT NULL,            -- 端点节点 id（舱/泵/阀）
    b_node TEXT NOT NULL,
    mutex_group TEXT,                -- 同组管路互斥，不能被两步同时占用
    UNIQUE (voyage_id, name)
);

CREATE TABLE IF NOT EXISTS ballast_plans (
    id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    revision INTEGER NOT NULL,       -- 每航次递增的修订号
    status TEXT NOT NULL,            -- active | superseded
    base_plan_id TEXT,               -- 生成时依据的已发布配载方案
    reason TEXT,                     -- initial | loading_change | drift | manual
    note TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (voyage_id, revision)
);

CREATE TABLE IF NOT EXISTS ballast_steps (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    voyage_id TEXT NOT NULL,
    seq INTEGER NOT NULL,            -- 修订内顺序号
    stage INTEGER NOT NULL,          -- 所属装船阶段
    source_tank TEXT NOT NULL,
    target_tank TEXT NOT NULL,
    target_volume REAL NOT NULL,     -- 目标转移量 m3
    tolerance REAL NOT NULL,         -- 允许误差 m3
    pump_id TEXT NOT NULL,
    valves TEXT NOT NULL,            -- JSON: 阀门 id 列表
    pipes TEXT NOT NULL,             -- JSON: 管路 id 列表
    start_condition TEXT NOT NULL,   -- JSON: 开始条件
    status TEXT NOT NULL,            -- pending|running|paused|confirmed|frozen
    src_start REAL,                  -- 启动时源舱液位（核对基准）
    dst_start REAL,                  -- 启动时目标舱液位
    transferred REAL NOT NULL DEFAULT 0,  -- 已确认转移量 m3
    started_at TEXT,
    confirmed_at TEXT,
    UNIQUE (plan_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_ballast_steps_voyage
    ON ballast_steps(voyage_id, status);

CREATE TABLE IF NOT EXISTS ballast_tokens (
    token TEXT PRIMARY KEY,          -- 执行令牌（幂等键）
    voyage_id TEXT NOT NULL,
    action TEXT NOT NULL,            -- start|pause|resume|confirm|seize|release
    payload_hash TEXT NOT NULL,      -- 载荷规范化后的 SHA-256
    payload TEXT NOT NULL,           -- 规范化 JSON 载荷
    result TEXT NOT NULL,            -- 首次执行结果（重放用）
    actor TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ballast_seizures (
    id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    pump_id TEXT NOT NULL,
    officer TEXT NOT NULL,
    step_id TEXT,                    -- 由步骤持有时为步骤 id，否则为人工抢占
    released INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    released_at TEXT
);
-- 同一航次同一泵组最多一条未释放占用：并发抢占只有一方成功
CREATE UNIQUE INDEX IF NOT EXISTS uq_ballast_seizure_active
    ON ballast_seizures(voyage_id, pump_id) WHERE released=0;

CREATE TABLE IF NOT EXISTS ballast_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voyage_id TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    detail TEXT,                     -- JSON
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ballast_ledger_voyage
    ON ballast_ledger(voyage_id, id);

CREATE TABLE IF NOT EXISTS ballast_checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voyage_id TEXT NOT NULL,
    state TEXT NOT NULL,             -- JSON 执行状态快照
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ballast_alarms (
    id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    kind TEXT NOT NULL,              -- DEVIATION|DRIFT|FLOW_LOW|STABILITY|WATER_UNBALANCED
    message TEXT NOT NULL,
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- open | acked
    created_at TEXT NOT NULL
);
"""


def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path=None):
    """建齐 stowage 与 ballast 全部表（可独立使用，也可叠加在现有库上）。"""
    conn = connect(path)
    try:
        conn.executescript(STOWAGE_SCHEMA)
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
