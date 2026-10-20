"""状态写入 —— jobs / runs / steps 的读写（PLAN.md §1 架构里的 memory/store.py）。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv

from agent.tools.sources.base import Job, JobPage

if TYPE_CHECKING:
    from agent.runtime.loop import StepRecord

VAR_DIR_ENV = "VAR_DIR"
DEFAULT_VAR_DIR = "var"


def var_dir(root: str | Path | None = None) -> Path:
    """运行时数据的根目录（.env 里的 VAR_DIR，默认 var/）。"""
    load_dotenv()
    return Path(root or os.getenv(VAR_DIR_ENV, DEFAULT_VAR_DIR))


def archive_raw(raw: bytes, *, root: str | Path | None = None) -> str:
    """把原始响应体按【内容】存盘，返回它在 var/ 下的相对路径。"""
    digest = hashlib.sha1(raw).hexdigest()
    target = var_dir(root) / "raw" / f"{digest}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(raw)
    return target.relative_to(var_dir(root)).as_posix()


def upsert_jobs(
    conn: sqlite3.Connection,
    jobs: Sequence[Job],
    *,
    raw_path: str | None,
    run_id: str,
) -> dict[str, int]:
    """把一页岗位写进库，幂等。返回 {"inserted": n, "updated": m}。"""
    now = datetime.now(UTC).isoformat()
    before = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]

    SQL = """
        INSERT INTO jobs (
            source, source_id, title, company, url, location,
            published_at, extras, raw_path, first_seen_run, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, source_id) DO UPDATE SET
            title         = excluded.title,
            company       = excluded.company,
            url           = excluded.url,
            location      = excluded.location,
            published_at  = excluded.published_at,
            extras        = excluded.extras,
            raw_path      = excluded.raw_path,
            updated_at    = excluded.updated_at
        """
    for job in jobs:
        conn.execute(
            SQL,
            (
                job.source,
                job.source_id,
                job.title,
                job.company,
                job.url,
                job.location,
                job.published_at,
                json.dumps(job.extras, ensure_ascii=False),
                raw_path,
                run_id,
                now,
            ),
        )
    after = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
    conn.commit()

    inserted = after - before
    return {"inserted": inserted, "updated": len(jobs) - inserted}


def store_page(
    conn: sqlite3.Connection,
    page: JobPage,
    *,
    run_id: str,
    root: str | Path | None = None,
) -> dict[str, int]:
    """把【一次抓取】消费掉：原文存档 + 岗位入库。返回 upsert_jobs 那笔账。"""
    raw_path = archive_raw(page.raw, root=root)
    return upsert_jobs(conn, page.jobs, raw_path=raw_path, run_id=run_id)


_JOB_COLUMNS = (
    "source, source_id, title, company, url, location, published_at, extras, "
    "raw_path, first_seen_run, updated_at"
)


def _like(term: str) -> str:
    """把关键词洗成 LIKE 字面量：`%` / `_` / `\\` 都按普通字符匹配。"""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def query_jobs(
    conn: sqlite3.Connection,
    *,
    terms: Sequence[str] = (),
    location: str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """按关键词/城市/时间检索岗位。terms 之间 OR，单个词命中任一字段即可。"""
    if limit < 1:
        raise ValueError(f"limit 必须是正整数，收到 {limit!r}")

    clauses: list[str] = []
    params: list[Any] = []
    term_clauses: list[str] = []
    for term in terms:
        needle = _like(term)
        term_clauses.append(
            "(title LIKE ? ESCAPE '\\' OR company LIKE ? ESCAPE '\\' OR extras LIKE ? ESCAPE '\\')"
        )
        params.extend((needle, needle, needle))
    if term_clauses:
        clauses.append("(" + " OR ".join(term_clauses) + ")")
    if location:
        clauses.append("location LIKE ? ESCAPE '\\'")
        params.append(_like(location))
    if since:
        clauses.append("datetime(published_at) >= datetime(?)")
        params.append(since)

    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    sql = (
        f"SELECT {_JOB_COLUMNS} FROM jobs {where}"
        "ORDER BY published_at IS NULL, published_at DESC, source, source_id "
        "LIMIT ?"
    )
    rows = conn.execute(sql, (*params, limit)).fetchall()
    return [dict(row) for row in rows]


def get_job_row(
    conn: sqlite3.Connection,
    *,
    source: str,
    source_id: str,
) -> dict[str, Any] | None:
    """按主键读一条岗位；不存在返回 None。"""
    row = conn.execute(
        f"SELECT {_JOB_COLUMNS} FROM jobs WHERE source = ? AND source_id = ?",
        (source, source_id),
    ).fetchone()
    return dict(row) if row is not None else None


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """先写同目录临时文件再替换 —— 中途被杀不会留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def ran_since(
    conn: sqlite3.Connection,
    *,
    since: str,
    statuses: Sequence[str],
) -> bool:
    """`since` 之后有没有过这几种 status 的 run？—— "一天一次"的判据。"""
    placeholders = ", ".join(["?"] * len(statuses))
    sql = (
        "SELECT 1 FROM runs "
        "WHERE datetime(started_at) >= datetime(?) "
        f"AND status IN ({placeholders}) "
        "LIMIT 1"
    )
    return conn.execute(sql, (since, *statuses)).fetchone() is not None


def record_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    steps: Sequence[StepRecord],
    prompt_tokens: int,
    completion_tokens: int,
    started_at: str,
    error: str | None = None,
) -> None:
    """把一次 run 的结局写进 runs（一行）+ steps（一条 StepRecord 一行）。"""
    finished_at = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO runs (
            run_id, started_at, finished_at, status, steps,
            prompt_tokens, completion_tokens, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (run_id) DO UPDATE SET
            finished_at       = excluded.finished_at,
            status            = excluded.status,
            steps             = excluded.steps,
            prompt_tokens     = excluded.prompt_tokens,
            completion_tokens = excluded.completion_tokens,
            error             = excluded.error
        """,
        (
            run_id,
            started_at,
            finished_at,
            status,
            len(steps),
            prompt_tokens,
            completion_tokens,
            error,
        ),
    )

    conn.execute("DELETE FROM steps WHERE run_id = ?", (run_id,))
    conn.executemany(
        """
        INSERT INTO steps (
            run_id, seq, step, kind, detail, ok,
            prompt_tokens, completion_tokens, latency_ms, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                seq,
                record.step,
                record.kind,
                record.detail,
                record.ok,
                record.prompt_tokens,
                record.completion_tokens,
                record.latency_ms,
                record.error,
            )
            for seq, record in enumerate(steps)
        ],
    )

    conn.commit()
