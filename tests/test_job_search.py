"""本地岗位检索工具 —— 全离线（临时库），不发任何网络请求。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from agent.memory.store import upsert_jobs, var_dir
from agent.tools.job_search import LATEST_REL, get_job, save_shortlist, search_jobs
from agent.tools.sources.base import Job


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _job(
    sid: str,
    title: str,
    *,
    company: str = "某公司",
    location: str = "杭州",
    published: str | None = None,
    extras: dict | None = None,
) -> Job:
    return Job(
        source="fixture",
        source_id=sid,
        title=title,
        company=company,
        url=f"https://example.test/{sid}",
        location=location,
        published_at=published,
        extras=extras if extras is not None else {},
    )


@pytest.fixture
def seeded(match_env):
    upsert_jobs(
        match_env,
        [
            _job("1", "Python 后端", published=_days_ago(3), extras={"tags": ["Python", "SQL"]}),
            _job("2", "数据开发", company="云杉", published=_days_ago(0)),
            _job("3", "销售经理", published=None),
            _job("4", "后端 100% 远程", published=_days_ago(2)),
            _job("5", "后端_实习", published=_days_ago(4)),
            _job("6", "Python 工程师", location="上海", published=_days_ago(5)),
        ],
        raw_path=None,
        run_id="seed",
    )
    return match_env


def test_search_or_semantics_and_order(seeded):
    result = search_jobs(query="Python 云杉")

    assert result["ok"] is True
    assert [item["source_id"] for item in result["items"]] == ["2", "1", "6"]
    assert result["items"][0]["job_id"] == "fixture:2"


def test_search_location_filter(seeded):
    result = search_jobs(location="上海")

    assert [item["source_id"] for item in result["items"]] == ["6"]


def test_search_since_days_filter(seeded):
    result = search_jobs(since_days=1)

    assert [item["source_id"] for item in result["items"]] == ["2"]


def test_search_respects_limit(seeded):
    assert search_jobs(limit=2)["count"] == 2


def test_null_published_at_sorts_last(seeded):
    items = search_jobs()["items"]

    assert items[-1]["source_id"] == "3"
    assert items[0]["source_id"] == "2"


def test_percent_sign_is_a_literal(seeded):
    """`%` 若被当成通配符会命中一切 —— 转义后只该命中真的带 % 的那条。"""
    result = search_jobs(query="%")

    assert [item["source_id"] for item in result["items"]] == ["4"]


def test_underscore_is_a_literal(seeded):
    result = search_jobs(query="端_实")

    assert [item["source_id"] for item in result["items"]] == ["5"]


def test_search_on_empty_library(match_env):
    result = search_jobs()

    assert result["ok"] is True
    assert result["count"] == 0
    assert "没有命中" in result["hint"]


def test_db_error_is_classified_not_crashed(match_env, tmp_path, monkeypatch):
    not_a_file = tmp_path / "as_dir"
    not_a_file.mkdir()
    monkeypatch.setenv("DB_PATH", str(not_a_file))

    result = search_jobs()

    assert result["ok"] is False
    assert result["error"] == "db_error"
    assert "report" in result["hint"]


def test_get_job_returns_full_row(seeded):
    result = get_job("fixture:1")

    assert result["ok"] is True
    assert result["item"]["title"] == "Python 后端"
    assert result["item"]["url"] == "https://example.test/1"
    assert result["item"]["extras"]["tags"] == ["Python", "SQL"]


def test_get_job_not_found(seeded):
    result = get_job("fixture:999")

    assert result["ok"] is False
    assert result["error"] == "not_found"
    assert "search_jobs" in result["hint"]


def test_get_job_rejects_malformed_id(seeded):
    result = get_job("no-colon")

    assert result["ok"] is False
    assert result["error"] == "invalid_job_id"


def test_save_shortlist_backfills_from_db(seeded):
    result = save_shortlist(
        entries=[{"source": "fixture", "source_id": "1", "score": 90, "reason": "关键词全中"}],
        note="第一版",
    )

    assert result["ok"] is True
    assert result["path"] == LATEST_REL

    payload = json.loads((var_dir() / LATEST_REL).read_text(encoding="utf-8"))
    entry = payload["entries"][0]
    assert entry["title"] == "Python 后端"
    assert entry["url"] == "https://example.test/1"
    assert entry["score"] == 90
    assert entry["reason"] == "关键词全中"
    assert payload["note"] == "第一版"
    assert payload["saved_at"]


def test_save_shortlist_sorts_by_score(seeded):
    save_shortlist(
        entries=[
            {"source": "fixture", "source_id": "1", "score": 10, "reason": "低"},
            {"source": "fixture", "source_id": "2", "score": 80, "reason": "高"},
        ]
    )

    payload = json.loads((var_dir() / LATEST_REL).read_text(encoding="utf-8"))
    assert [entry["source_id"] for entry in payload["entries"]] == ["2", "1"]


def test_save_shortlist_rejects_unknown_id_without_writing(seeded):
    result = save_shortlist(
        entries=[{"source": "fixture", "source_id": "999", "score": 50, "reason": "查无此岗"}]
    )

    assert result["ok"] is False
    assert result["error"] == "unknown_job_ids"
    assert not (var_dir() / LATEST_REL).exists()


def test_save_shortlist_rejects_duplicates_without_writing(seeded):
    result = save_shortlist(
        entries=[
            {"source": "fixture", "source_id": "1", "score": 50, "reason": "a"},
            {"source": "fixture", "source_id": "1", "score": 40, "reason": "b"},
        ]
    )

    assert result["ok"] is False
    assert result["error"] == "duplicate_job_ids"
    assert not (var_dir() / LATEST_REL).exists()


def test_save_shortlist_leaves_no_temp_file(seeded):
    save_shortlist(entries=[{"source": "fixture", "source_id": "1", "score": 70, "reason": "还行"}])

    leftovers = list((var_dir() / "shortlists").glob("*.tmp"))
    assert leftovers == []


def test_tool_registration_marks_trust_levels():
    """检索结果来自网页数据（untrusted），本地写清单不是。"""
    from agent.tools.registry import TOOLS

    assert TOOLS["search_jobs"].side_effect == "read"
    assert TOOLS["search_jobs"].untrusted is True
    assert TOOLS["get_job"].untrusted is True
    assert TOOLS["save_shortlist"].side_effect == "write"
    assert TOOLS["save_shortlist"].untrusted is False
