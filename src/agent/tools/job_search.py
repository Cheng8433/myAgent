"""岗位检索工具 —— 只读本地 jobs 库，不发网络请求。"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from agent.memory.db import connect, ensure_schema
from agent.memory.store import get_job_row, query_jobs, var_dir, write_json_atomic
from agent.tools.registry import tool

LATEST_REL = "shortlists/latest.json"
"""最近一次提交的清单（相对 var/）—— 工具唯一的固定落点。"""

MAX_LIMIT = 20
MAX_SHORTLIST = 20


def _clip(value: Any, limit: int) -> str:
    """把任意字段洗成定长字符串。"""
    return str(value if value is not None else "")[:limit]


def parse_extras(row: dict[str, Any]) -> dict[str, Any]:
    """jobs.extras 是 JSON 文本；坏了就当空字典，不让整条记录失踪。"""
    try:
        value = json.loads(row.get("extras") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def compact_row(row: dict[str, Any]) -> dict[str, Any]:
    """检索结果的一行：够模型判断，又不把上下文撑爆。"""
    extras = parse_extras(row)
    tags = extras.get("tags") or extras.get("recTags") or []
    if isinstance(tags, str):
        tags = [tags]
    return {
        "job_id": f"{row['source']}:{row['source_id']}",
        "source": row["source"],
        "source_id": row["source_id"],
        "title": _clip(row.get("title"), 200),
        "company": _clip(row.get("company"), 120),
        "location": _clip(row.get("location"), 60) or None,
        "published_at": row.get("published_at"),
        "tags": [_clip(tag, 30) for tag in list(tags)[:8]],
    }


def _compact_value(value: Any) -> Any:
    """extras 里的值：短的保留原样，长的截断。"""
    if isinstance(value, list):
        return [_clip(item, 40) for item in value[:8]]
    if isinstance(value, str):
        return _clip(value, 80)
    return value


def detail_row(row: dict[str, Any]) -> dict[str, Any]:
    """一条岗位的完整形状（含 url 与 extras），给 get_job 与清单回填用。"""
    item = compact_row(row)
    item["url"] = _clip(row.get("url"), 300)
    item["extras"] = {key: _compact_value(value) for key, value in parse_extras(row).items()}
    return item


def _db_error(exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "error": "db_error",
        "hint": (
            f"本地岗位库不可用（{type(exc).__name__}: {exc}）。先跑一次 python -m app.main report。"
        ),
    }


class SearchJobsArgs(BaseModel):
    query: str = Field("", max_length=200, description="关键词，空格分隔；任一命中即可")
    location: str | None = Field(None, max_length=60, description="城市/地区子串")
    since_days: int | None = Field(None, ge=1, le=365, description="只找最近 N 天发布的")
    limit: int = Field(10, ge=1, le=MAX_LIMIT, description="最多返回多少条")


@tool(SearchJobsArgs, side_effect="read", timeout=10.0, untrusted=True)
def search_jobs(
    query: str = "",
    location: str | None = None,
    since_days: int | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """按关键词检索本地岗位库；任一关键词命中标题/公司/标签即可。
    返回紧凑列表与 job_id（source:source_id）；看详情用 get_job。"""
    terms = [term for term in query.split() if term]
    since = None
    if since_days is not None:
        since = (datetime.now(UTC) - timedelta(days=since_days)).isoformat()

    conn = None
    try:
        conn = connect()
        ensure_schema(conn)
        rows = query_jobs(conn, terms=terms, location=location, since=since, limit=limit)
    except sqlite3.Error as exc:
        return {**_db_error(exc), "items": [], "count": 0}
    finally:
        if conn is not None:
            conn.close()

    items = [compact_row(row) for row in rows]
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "error": None,
        "hint": None if items else "没有命中的岗位。换个关键词，或先跑一次 report。",
    }


class GetJobArgs(BaseModel):
    job_id: str = Field(..., min_length=3, max_length=200, description="形如 source:source_id")


@tool(GetJobArgs, side_effect="read", timeout=10.0, untrusted=True)
def get_job(job_id: str) -> dict[str, Any]:
    """按 job_id（source:source_id）读取一条岗位的完整字段，
    含薪资、学历、标签等 extras；找不到时返回 ok=false。"""
    source, sep, source_id = job_id.partition(":")
    if not sep or not source or not source_id:
        return {
            "ok": False,
            "item": None,
            "error": "invalid_job_id",
            "hint": "job_id 必须形如 source:source_id，例如 ncss:12345",
        }

    conn = None
    try:
        conn = connect()
        ensure_schema(conn)
        row = get_job_row(conn, source=source, source_id=source_id)
    except sqlite3.Error as exc:
        return {**_db_error(exc), "item": None}
    finally:
        if conn is not None:
            conn.close()

    if row is None:
        return {
            "ok": False,
            "item": None,
            "error": "not_found",
            "hint": f"库里没有 {job_id}。先用 search_jobs 找到真实 id。",
        }
    return {"ok": True, "item": detail_row(row), "error": None, "hint": None}


class ShortlistEntryArgs(BaseModel):
    source: str = Field(..., min_length=1, max_length=40)
    source_id: str = Field(..., min_length=1, max_length=80)
    score: int = Field(..., ge=0, le=100)
    reason: str = Field(..., min_length=1, max_length=200)


class SaveShortlistArgs(BaseModel):
    entries: list[ShortlistEntryArgs] = Field(
        ...,
        min_length=1,
        max_length=MAX_SHORTLIST,
        description="按匹配度从高到低",
    )
    note: str = Field("", max_length=500, description="可选备注")


@tool(SaveShortlistArgs, side_effect="write", timeout=15.0)
def save_shortlist(entries: list[dict[str, Any]], note: str = "") -> dict[str, Any]:
    """提交最终入围清单（最多 20 条，score 0-100）。这是匹配任务的最后一步：
    不调用它等于没有结果；title/url 会从库里回填，只信 source/source_id。"""
    pairs = [(entry["source"], entry["source_id"]) for entry in entries]
    if len(set(pairs)) != len(pairs):
        return {
            "ok": False,
            "path": None,
            "count": 0,
            "error": "duplicate_job_ids",
            "hint": "同一岗位只能出现一次，请去重后再提交。",
        }

    conn = None
    try:
        conn = connect()
        ensure_schema(conn)
        rows = {pair: get_job_row(conn, source=pair[0], source_id=pair[1]) for pair in pairs}
    except sqlite3.Error as exc:
        return {**_db_error(exc), "path": None, "count": 0}
    finally:
        if conn is not None:
            conn.close()

    unknown = [f"{source}:{source_id}" for (source, source_id), row in rows.items() if row is None]
    if unknown:
        shown = "、".join(unknown[:5])
        more = f"（共 {len(unknown)} 条）" if len(unknown) > 5 else ""
        return {
            "ok": False,
            "path": None,
            "count": 0,
            "error": "unknown_job_ids",
            "hint": f"这些岗位不在库里：{shown}{more}。先用 search_jobs 找到真实 id。",
        }

    enriched = []
    for entry in entries:
        row = rows[(entry["source"], entry["source_id"])]
        item = detail_row(row)
        item["score"] = entry["score"]
        item["reason"] = entry["reason"]
        enriched.append(item)
    enriched.sort(key=lambda item: -item["score"])

    saved_at = datetime.now(UTC).isoformat()
    target = var_dir() / LATEST_REL
    try:
        write_json_atomic(target, {"saved_at": saved_at, "note": note, "entries": enriched})
    except OSError as exc:
        return {
            "ok": False,
            "path": None,
            "count": 0,
            "error": "artifact_error",
            "hint": f"清单落盘失败：{exc}",
        }

    return {
        "ok": True,
        "path": LATEST_REL,
        "count": len(enriched),
        "saved_at": saved_at,
        "error": None,
        "hint": None,
    }
