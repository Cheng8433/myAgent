"""状态库的连接与建表 —— M5 的地基（PLAN.md §M5 / §1 架构里的 memory/db.py）。"""

from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

DB_PATH_ENV = "DB_PATH"
DEFAULT_DB_PATH = "var/app.db"
"""变量名与默认值都照抄 .env.example，不在代码里另起一套。"""


def new_run_id(*, now: datetime | None = None) -> str:
    """生成一次 run 的标识：UTC 时间戳 + 4 位随机十六进制，如 `20260930T143012-a1b2`。"""
    moment = now or datetime.now(UTC)
    return f"{moment.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(2)}"


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开（必要时创建）库文件，把三条 PRAGMA 设好，返回连接。"""
    load_dotenv()
    path = Path(db_path or os.getenv(DB_PATH_ENV, DEFAULT_DB_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys  = ON ")
    conn.execute("PRAGMA journal_mode  = WAL ")
    conn.execute("PRAGMA busy_timeout  = 5000 ")

    return conn


SCHEMA = """
  CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL,
    steps             INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    error             TEXT
  );

  CREATE TABLE IF NOT EXISTS steps (
    run_id            TEXT NOT NULL,
    seq               INTEGER NOT NULL,
    step              INTEGER NOT NULL,
    kind              TEXT NOT NULL,
    detail            TEXT NOT NULL DEFAULT '',
    ok                INTEGER,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    error             TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
  );

  CREATE TABLE IF NOT EXISTS jobs (
    source         TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    title          TEXT NOT NULL,
    company        TEXT NOT NULL DEFAULT '',
    url            TEXT NOT NULL DEFAULT '',
    location       TEXT,
    published_at   TEXT,
    extras         TEXT NOT NULL DEFAULT '{}',
    raw_path       TEXT,
    first_seen_run TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
  );
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """某张表现有的列名。PRAGMA 返回 (cid, name, ...)，所以取名用下标 [1]。"""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("steps", "latency_ms", "INTEGER NOT NULL DEFAULT 0"),
    ("steps", "error", "TEXT NOT NULL DEFAULT ''"),
)
"""历史库要补的列：(表, 列, 列定义)。新库已由 SCHEMA 建好，这里是幂等补丁。"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """把 SCHEMA 灌进库，再把历史库缺的列补上。幂等 —— 每次启动无脑调一次。"""
    conn.executescript(SCHEMA)
    for table, column, ddl in MIGRATIONS:
        if column not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    conn.commit()
