"""成本与健康度看板 —— 可观测的第三块（M6）。"""

from __future__ import annotations

import sqlite3
from typing import Any

_DAILY_SQL = """
    SELECT date(started_at)                        AS day,
           count(*)                                AS runs,
           sum(status = 'success')                 AS ok,
           sum(status = 'failed')                  AS failed,
           sum(status = 'no_items')                AS no_items,
           sum(prompt_tokens + completion_tokens)  AS tokens,
           avg(steps)                              AS avg_steps
      FROM runs
     WHERE datetime(started_at) > datetime('now', ?)
     GROUP BY day
     ORDER BY day
"""

_RECENT_ERRORS_SQL = """
    SELECT run_id, status, started_at, finished_at, error
      FROM runs
     WHERE error IS NOT NULL
     ORDER BY COALESCE(finished_at, started_at) DESC
     LIMIT ?
"""


def cost_summary(conn: sqlite3.Connection, *, days: int = 7) -> list[dict[str, Any]]:
    """最近 `days` 天的逐日汇总，按日期升序。返回 list[dict]。"""
    if days < 1:
        raise ValueError(f"days 必须是正整数，收到 {days!r}")
    rows = conn.execute(_DAILY_SQL, (f"-{days} days",)).fetchall()
    return [dict(row) for row in rows]


def recent_errors(conn: sqlite3.Connection, *, limit: int = 10) -> list[dict[str, Any]]:
    """最近 `limit` 条带错误原文的 run。健康度的另一半。"""
    if limit < 1:
        raise ValueError(f"limit 必须是正整数，收到 {limit!r}")
    rows = conn.execute(_RECENT_ERRORS_SQL, (limit,)).fetchall()
    return [dict(row) for row in rows]


def render_board(
    daily: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    *,
    days: int,
) -> str:
    """把两份汇总渲染成人读的一段文本（给 stdout）。"""
    lines: list[str] = [f"=== 最近 {days} 天 ==="]
    if not daily:
        lines.append("（这段时间没有任何 run）")
    else:
        lines.append(
            f"{'日期':<12}{'runs':>5}{'ok':>5}{'failed':>8}{'no_items':>10}{'tokens':>10}{'avg_steps':>11}"
        )
        total_runs = total_ok = total_failed = total_no_items = total_tokens = 0
        for row in daily:
            tokens = row["tokens"] or 0
            avg_steps = row["avg_steps"]
            avg_text = "—" if avg_steps is None else f"{avg_steps:.1f}"
            lines.append(
                f"{row['day']:<12}{row['runs']:>5}{row['ok']:>5}{row['failed']:>8}"
                f"{row['no_items']:>10}{tokens:>10}{avg_text:>11}"
            )
            total_runs += row["runs"]
            total_ok += row["ok"]
            total_failed += row["failed"]
            total_no_items += row["no_items"]
            total_tokens += tokens
        lines.append(
            f"{'合计':<12}{total_runs:>5}{total_ok:>5}{total_failed:>8}"
            f"{total_no_items:>10}{total_tokens:>10}{'':>11}"
        )

    lines.append("")
    lines.append("=== 最近的失败 ===")
    if not errors:
        lines.append("（没有带错误的 run —— 挺好）")
    else:
        for row in errors:
            when = row["finished_at"] or row["started_at"]
            lines.append(f"[{row['status']}] {when}  {row['run_id']}")
            lines.append(f"    {row['error']}")
    return "\n".join(lines)
