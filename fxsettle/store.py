"""SQLite 存储层：建表与连接管理。"""

import sqlite3

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_groups (
    version INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rates (
    version INTEGER NOT NULL REFERENCES rate_groups(version),
    currency TEXT NOT NULL,
    price TEXT NOT NULL,
    PRIMARY KEY (version, currency)
);
CREATE TABLE IF NOT EXISTS accounts (
    kind TEXT NOT NULL CHECK (kind IN ('customer', 'reserve', 'fee')),
    currency TEXT NOT NULL,
    balance_atoms TEXT NOT NULL,
    PRIMARY KEY (kind, currency)
);
CREATE TABLE IF NOT EXISTS requests (
    key TEXT PRIMARY KEY,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    amount_atoms TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'rejected')),
    reason TEXT,
    receipt_id INTEGER
);
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    rate_version INTEGER NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    principal_atoms TEXT NOT NULL,
    fee_atoms TEXT NOT NULL,
    credited_atoms TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id),
    kind TEXT NOT NULL,
    currency TEXT NOT NULL,
    delta_atoms TEXT NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开（必要时创建）数据库并确保表结构存在。"""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    return conn
