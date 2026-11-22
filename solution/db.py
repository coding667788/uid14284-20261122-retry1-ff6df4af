"""SQLite 持久层（仅标准库 sqlite3）。

所有写操作都在引擎开启的单个事务内完成；测试可通过 ``crash_before_commit``
在提交前主动抛出来模拟“进程提交前退出”。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id              TEXT PRIMARY KEY,
    current_generation      INTEGER NOT NULL,
    active_authenticator    TEXT NOT NULL,
    security_contact        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
    account_id      TEXT NOT NULL,
    generation      INTEGER NOT NULL,
    authenticator   TEXT NOT NULL,
    superseded      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, generation)
);

CREATE TABLE IF NOT EXISTS trustees (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trustee_id      TEXT NOT NULL,
    organization    TEXT NOT NULL,
    custody_domain  TEXT NOT NULL,
    valid_from      INTEGER NOT NULL,
    valid_until     INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    seq             INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS proofs (
    proof_id        TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL,
    trustee_id      TEXT NOT NULL,
    proof_type      TEXT NOT NULL,
    digest          TEXT NOT NULL,
    issued_at       INTEGER NOT NULL,
    valid_until     INTEGER NOT NULL,
    cost            INTEGER NOT NULL,
    revoked_at      INTEGER
);

CREATE TABLE IF NOT EXISTS recipes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id       TEXT NOT NULL,
    version         INTEGER NOT NULL,
    requirements    TEXT NOT NULL,
    active_from     INTEGER NOT NULL,
    UNIQUE (recipe_id, version)
);

CREATE TABLE IF NOT EXISTS requests (
    request_id      TEXT PRIMARY KEY,
    content_hash    TEXT NOT NULL,
    kind            TEXT NOT NULL,
    receipt_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    app_id                      TEXT PRIMARY KEY,
    account_id                  TEXT NOT NULL,
    generation                  INTEGER NOT NULL,
    request_id                  TEXT NOT NULL,
    recipe_id                   TEXT NOT NULL,
    recipe_version              INTEGER NOT NULL,
    new_authenticator_digest    TEXT NOT NULL,
    proof_ids_json              TEXT NOT NULL,
    freeze_until                INTEGER NOT NULL,
    status                      TEXT NOT NULL,
    created_at                  INTEGER NOT NULL,
    terminal_receipt_json       TEXT
);

CREATE TABLE IF NOT EXISTS reservations (
    proof_id    TEXT NOT NULL,
    app_id      TEXT NOT NULL,
    frozen_at   INTEGER NOT NULL,
    PRIMARY KEY (proof_id, app_id)
);

CREATE TABLE IF NOT EXISTS challenges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id      TEXT NOT NULL,
    challenger  TEXT NOT NULL,
    at          INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    request_id  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reservations_proof ON reservations(proof_id);
CREATE INDEX IF NOT EXISTS idx_apps_account ON applications(account_id);
CREATE INDEX IF NOT EXISTS idx_trustee_id ON trustees(trustee_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, *, crash_before_commit: bool = False):
    """显式 BEGIN/COMMIT；异常时 ROLLBACK。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        if crash_before_commit:
            conn.execute("ROLLBACK")
            raise RuntimeError("simulated crash before commit")
        conn.execute("COMMIT")
    except Exception:
        # 模拟崩溃已经回滚；其余异常同样确保回滚
        if not crash_before_commit:
            conn.execute("ROLLBACK")
        raise
