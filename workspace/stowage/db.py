"""SQLite persistence layer.

All multi-step writes happen inside a single IMMEDIATE transaction so that
solver interruption / crashes never leave half-written plans, and concurrent
planners are serialized by the database write lock (optimistic version checks
on top of that return 409 to the loser).
"""
import os
import sqlite3

DB_PATH = os.environ.get("STOWAGE_DB", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stowage.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS voyages (
    id TEXT PRIMARY KEY,
    name TEXT,
    port_sequence TEXT NOT NULL,          -- JSON array of port codes, discharge order
    lightship_weight REAL NOT NULL,
    lightship_lcg REAL NOT NULL,
    lightship_vcg REAL NOT NULL,
    min_gm REAL NOT NULL,
    max_gm REAL NOT NULL,
    max_trim REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stacks (
    id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    bay INTEGER NOT NULL,
    row INTEGER NOT NULL,
    max_tiers INTEGER NOT NULL,
    max_weight REAL NOT NULL,
    on_deck INTEGER NOT NULL,
    reefer_plugs INTEGER NOT NULL DEFAULT 0,
    x REAL NOT NULL,                      -- metres, longitudinal (bow+)
    y REAL NOT NULL,                      -- metres, transverse (starboard+)
    z0 REAL NOT NULL,                     -- metres, vertical of tier 1 base
    hold_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS slots (
    id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    stack_id TEXT NOT NULL,
    tier INTEGER NOT NULL,
    code TEXT NOT NULL,                   -- human code e.g. 03-03-02
    z REAL NOT NULL,
    UNIQUE (voyage_id, stack_id, tier)
);

CREATE TABLE IF NOT EXISTS containers (
    id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    weight REAL NOT NULL,
    pod TEXT NOT NULL,                    -- port of discharge
    hazard_class TEXT,                    -- e.g. "3", "5.1", NULL = general cargo
    reefer INTEGER NOT NULL DEFAULT 0,
    length_ft INTEGER NOT NULL DEFAULT 40,
    arrived INTEGER NOT NULL DEFAULT 1,
    locked INTEGER NOT NULL DEFAULT 0,    -- physically loaded, position frozen
    discharged INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    version INTEGER NOT NULL,             -- per-voyage increasing
    status TEXT NOT NULL,                 -- draft | published | history | superseded
    valid INTEGER NOT NULL DEFAULT 0,     -- full validation result at build/publish time
    note TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (voyage_id, version)
);

CREATE TABLE IF NOT EXISTS plan_entries (
    plan_id TEXT NOT NULL,
    container_id TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    PRIMARY KEY (plan_id, container_id),
    UNIQUE (plan_id, slot_id)             -- a container can never occupy two slots,
);                                        -- and a slot never holds two containers

CREATE TABLE IF NOT EXISTS confirmations (
    id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    container_id TEXT NOT NULL,
    slot_id TEXT NOT NULL,
    actor TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (voyage_id, container_id)      -- idempotent: same box confirmed once
);

CREATE TABLE IF NOT EXISTS approval_tokens (
    id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,                -- draft the token was minted for
    plan_version INTEGER NOT NULL,       -- version snapshot at mint time
    digest TEXT NOT NULL,                -- SHA-256 of the canonical diff
    base_plan_id TEXT,                   -- published plan the diff was taken against
    status TEXT NOT NULL,                -- active | consumed | voided
    requested_by TEXT,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    consumed_by TEXT,
    result TEXT,                         -- JSON publish result (idempotency)
    void_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_approval_tokens_plan
    ON approval_tokens(plan_id, status);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,                 -- running | done | cancelled | failed
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    plan_id TEXT,
    error TEXT,
    stats TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    voyage_id TEXT NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
"""


def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path=None):
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


class ServiceError(Exception):
    """Business-rule failure; `status` maps to the HTTP status code."""

    def __init__(self, status, code, message, extra=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}
