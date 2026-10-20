"""日报的渲染与落盘 —— 这一层是【纯确定性代码】。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from agent.memory.store import var_dir
from agent.tools.sources.base import Job

CN_TZ = timezone(timedelta(hours=8))
"""东八区固定偏移（+08:00）。"""

_NO_SUMMARY = "_（本次没有生成摘要）_"
"""摘要缺席时的占位。"""

_TEMPLATE = """# 招聘信息日报 · {day}

> 生成于 {generated_at} · run_id `{run_id}` · 共 {n} 条

{body}

## 岗位清单

{table}
"""

_MISSING_LINKS = "\n> ⚠️ 其中 {n} 条没有直接链接，已在表中用 `—` 标出。\n"


def report_day(*, now: datetime | None = None) -> str:
    """今天是几号（东八区），形如 "2026-10-04"。报告文件名用它。"""
    moment = now if now is not None else datetime.now(CN_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CN_TZ)
    return moment.astimezone(CN_TZ).strftime("%Y-%m-%d")


def day_start_utc(*, now: datetime | None = None) -> str:
    """东八区"今天 00:00"换算成 UTC ISO8601 —— "一天一次"的时间边界。"""
    moment = now if now is not None else datetime.now(CN_TZ)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=CN_TZ)
    local_midnight = moment.astimezone(CN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(UTC).isoformat()


def report_path(day: str, *, root: str | Path | None = None) -> Path:
    """报告文件的位置：`<VAR_DIR>/reports/<day>.md`。"""
    return var_dir(root) / "reports" / f"{day}.md"


def write_report(markdown: str, *, day: str, root: str | Path | None = None) -> str:
    """把日报写到 `var/reports/<day>.md`，返回它【相对 var/】的路径。"""
    target = report_path(day, root=root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    return target.relative_to(var_dir(root)).as_posix()


def _flat(text: str) -> str:
    """把一个字段洗成能安全放进【一行】表格的样子。"""
    return text.replace("|", "\\|").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def _render_table(jobs: Sequence[Job]) -> tuple[str, int]:
    """把岗位渲染成 Markdown 表格。返回 (表格文本, 缺链接的条数)。"""
    rows: list[str] = ["| # | 岗位 | 公司 | 链接 |", "|---|------|------|------|"]
    lack_url = 0
    for seq, job in enumerate(jobs, start=1):
        title = _flat(job.title)
        company = _flat(job.company)
        if job.url:
            link = f"[打开]({job.url})"
        else:
            lack_url += 1
            link = "—"
        rows.append(f"| {seq} | {title} | {company} | {link} |")

    return "\n".join(rows), lack_url


def render_report(
    jobs: Sequence[Job],
    *,
    day: str,
    summary: str,
    run_id: str,
    generated_at: str,
) -> str:
    """把这一批岗位渲染成一份完整的 Markdown 日报，返回整篇正文。"""
    table, missing = _render_table(jobs)
    body = summary.strip() or _NO_SUMMARY
    doc = _TEMPLATE.format(
        day=day,
        generated_at=generated_at,
        run_id=run_id,
        n=len(jobs),
        body=body,
        table=table,
    )
    return doc + (_MISSING_LINKS.format(n=missing) if missing else "")
