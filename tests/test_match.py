"""岗位匹配三模式 + A/B —— 全离线（临时库 + 脚本化 LLM）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _fakes import FakeLLM, ScriptedLLM, says, wants_tool, wide_budget

from agent.config import ConfigError
from agent.memory.store import upsert_jobs, var_dir
from agent.runtime.budget import Budget
from agent.tools.job_search import LATEST_REL
from agent.tools.sources.base import Job
from workflows.job_match import (
    MAX_TOP,
    MODE_AGENT,
    MODE_SINGLE,
    MODE_WORKFLOW,
    STATUS_INVALID_OUTPUT,
    STATUS_NO_CANDIDATES,
    STATUS_NO_SHORTLIST,
    STATUS_SUCCESS,
    JobProfile,
    load_profile,
    run_ab,
    run_agent_match,
    run_single_match,
    run_workflow_match,
)

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "eval" / "fixtures" / "match_labeled.json"


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _profile(payload: dict) -> JobProfile:
    return JobProfile.model_validate(payload["profile"])


def _seed(conn, payload: dict) -> None:
    upsert_jobs(
        conn,
        [
            Job(
                source=item["source"],
                source_id=item["source_id"],
                title=item["title"],
                company=item["company"],
                url=item["url"],
                location=item.get("location"),
                published_at=item.get("published_at"),
                extras=item.get("extras") or {},
            )
            for item in payload["jobs"]
        ],
        raw_path=None,
        run_id="fixture",
    )


@pytest.fixture
def labeled(match_env):
    payload = _fixture()
    _seed(match_env, payload)
    return match_env, _profile(payload), payload


def _agent_script(*, save: bool = True) -> ScriptedLLM:
    """search → get → save → 文本；save=False 时只检索就收尾。"""
    steps = [
        wants_tool("search_jobs", '{"query": "Python 后端"}', call_id="c1"),
        wants_tool("get_job", '{"job_id": "fixture:m01"}', call_id="c2"),
    ]
    if save:
        steps.append(
            wants_tool(
                "save_shortlist",
                json.dumps(
                    {
                        "entries": [
                            {
                                "source": "fixture",
                                "source_id": "m01",
                                "score": 92,
                                "reason": "Python 后端且城市匹配",
                            }
                        ],
                        "note": "test",
                    },
                    ensure_ascii=False,
                ),
                call_id="c3",
            )
        )
    steps.append(says("已完成"))
    return ScriptedLLM(*steps)


def test_workflow_precision_at_five_is_one(labeled):
    conn, profile, payload = labeled

    result = run_workflow_match(conn, profile, run_id="w", top=5)

    assert result.status == STATUS_SUCCESS
    assert len(result.entries) == 5
    relevant = {item["source_id"] for item in payload["jobs"] if item["relevant"]}
    assert {entry["source_id"] for entry in result.entries} <= relevant


def test_workflow_scores_are_non_increasing(labeled):
    conn, profile, _ = labeled

    scores = [entry["score"] for entry in run_workflow_match(conn, profile, run_id="w").entries]

    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 82


def test_workflow_never_selects_excluded_direction(labeled):
    conn, profile, _ = labeled

    entries = run_workflow_match(conn, profile, run_id="w", top=MAX_TOP).entries

    assert "m11" not in {entry["source_id"] for entry in entries}


def test_workflow_with_empty_library(match_env):
    profile = JobProfile(titles=["Python"])

    result = run_workflow_match(match_env, profile, run_id="w")

    assert result.status == STATUS_NO_CANDIDATES


def test_single_mode_parses_fenced_json(labeled):
    conn, profile, _ = labeled
    payload = {
        "entries": [
            {"source": "fixture", "source_id": "m01", "score": 88, "reason": "很匹配"},
        ]
    }
    llm = FakeLLM(text="```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```")

    result = run_single_match(conn, llm, profile, run_id="s", top=5)

    assert result.status == STATUS_SUCCESS
    assert result.metrics.schema_valid is True
    assert result.metrics.llm_calls == 1
    assert [entry["source_id"] for entry in result.entries] == ["m01"]
    assert result.entries[0]["title"] == "Python 后端开发（应届）"
    assert llm.seen_tools == [None]


def test_single_mode_rejects_prose_output(labeled):
    conn, profile, _ = labeled
    llm = FakeLLM(text="我觉得 fixture:m01 挺合适的。")

    result = run_single_match(conn, llm, profile, run_id="s")

    assert result.status == STATUS_INVALID_OUTPUT
    assert result.metrics.schema_valid is False
    assert result.entries == ()


def test_single_mode_rejects_unknown_job_id(labeled):
    conn, profile, _ = labeled
    payload = {
        "entries": [
            {"source": "fixture", "source_id": "m99", "score": 90, "reason": "编的"},
        ]
    }
    llm = FakeLLM(text=json.dumps(payload, ensure_ascii=False))

    result = run_single_match(conn, llm, profile, run_id="s")

    assert result.status == STATUS_INVALID_OUTPUT
    assert "不存在" in result.detail
    assert result.entries == ()


def test_agent_mode_success_needs_a_fresh_shortlist(labeled):
    conn, profile, _ = labeled

    result = run_agent_match(
        conn, _agent_script(), profile, run_id="a", top=5, budget=wide_budget()
    )

    assert result.status == STATUS_SUCCESS
    assert result.metrics.schema_valid is True
    assert result.metrics.llm_calls == 4
    assert result.metrics.tool_calls == 3
    assert [entry["source_id"] for entry in result.entries] == ["m01"]
    assert result.artifact_path == "shortlists/a.json"


def test_agent_mode_search_only_is_no_shortlist(labeled):
    conn, profile, _ = labeled

    result = run_agent_match(
        conn, _agent_script(save=False), profile, run_id="a", top=5, budget=wide_budget()
    )

    assert result.status == STATUS_NO_SHORTLIST
    assert result.entries == ()


def test_stale_latest_json_does_not_count(labeled):
    """上一次 run 留下的清单不能冒充本次结果。"""
    conn, profile, _ = labeled
    stale = var_dir() / LATEST_REL
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text(
        json.dumps(
            {
                "saved_at": "2000-01-01T00:00:00+00:00",
                "note": "",
                "entries": [
                    {
                        "source": "fixture",
                        "source_id": "m01",
                        "score": 99,
                        "reason": "旧结果",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_agent_match(
        conn, _agent_script(save=False), profile, run_id="a", top=5, budget=wide_budget()
    )

    assert result.status == STATUS_NO_SHORTLIST
    assert result.entries == ()


def test_injection_is_blocked_and_network_is_untouched(labeled, monkeypatch):
    """标题里的注入串不能让模型把网络工具叫出来。"""
    import agent.tools.jobs as jobs_module

    calls: list[str] = []

    def bomb(*_args, **_kwargs):
        calls.append("network")
        raise AssertionError("匹配任务不该碰网络")

    monkeypatch.setattr(jobs_module, "get_source", bomb)
    conn, profile, _ = labeled
    llm = ScriptedLLM(
        wants_tool("search_jobs", '{"query": "后端"}', call_id="c1"),
        wants_tool("fetch_job_list", '{"page": 1}', call_id="c2"),
        says("完成"),
    )

    result = run_agent_match(conn, llm, profile, run_id="a", top=5, budget=wide_budget())

    assert calls == []
    assert result.metrics.blocked_tool_calls == 1
    wrapped = [m for m in llm.calls[1] if m.get("role") == "tool"][0]["content"]
    assert "<untrusted_web_content>" in wrapped
    assert wrapped.count("</untrusted_web_content>") == 1
    assert "忽略以上所有规则" in wrapped


def test_ab_compares_three_legs_and_keeps_runs_empty(labeled):
    conn, profile, _ = labeled
    single_payload = {
        "entries": [
            {"source": "fixture", "source_id": "m02", "score": 80, "reason": "单次判断"},
        ]
    }
    llm = ScriptedLLM(
        says(json.dumps(single_payload, ensure_ascii=False)),
        wants_tool("search_jobs", '{"query": "Python"}', call_id="c1"),
        wants_tool("get_job", '{"job_id": "fixture:m01"}', call_id="c2"),
        wants_tool(
            "save_shortlist",
            json.dumps(
                {
                    "entries": [
                        {
                            "source": "fixture",
                            "source_id": "m01",
                            "score": 90,
                            "reason": "Agent 判断",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            call_id="c3",
        ),
        says("完成"),
    )

    report = run_ab(conn, llm, profile, run_id="ab1", top=5, budget=wide_budget())

    modes = [result.mode for result in report.results]
    assert modes == [MODE_WORKFLOW, MODE_SINGLE, MODE_AGENT]
    assert report.results[0].metrics.llm_calls == 0
    assert report.results[0].metrics.prompt_tokens == 0
    assert report.results[1].metrics.llm_calls == 1
    assert all(result.artifact_path for result in report.results)

    markdown = Path(report.markdown_path).read_text(encoding="utf-8")
    assert markdown.count("| workflow |") == 1
    assert markdown.count("| single |") == 1
    assert markdown.count("| agent |") == 1

    assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_load_profile_errors_are_config_errors(tmp_path):
    with pytest.raises(ConfigError):
        load_profile(tmp_path / "missing.json")

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_profile(bad)

    empty = tmp_path / "empty.json"
    empty.write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_profile(empty)


def test_profile_terms_dedupe_and_keep_order():
    profile = JobProfile(titles=["Python", "后端"], skills=["Python", "SQL"])

    assert profile.terms == ["Python", "后端", "SQL"]


def test_budget_gate_terminates_agent_but_keeps_shortlist(labeled):
    """清单已提交、循环却被预算截断 —— 清单要保留，状态要说清。"""
    conn, profile, _ = labeled
    llm = ScriptedLLM(
        wants_tool(
            "save_shortlist",
            json.dumps(
                {
                    "entries": [
                        {
                            "source": "fixture",
                            "source_id": "m01",
                            "score": 70,
                            "reason": "来得及提交",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            call_id="c1",
        ),
        says("来不及说完成"),
    )

    result = run_agent_match(
        conn,
        llm,
        profile,
        run_id="a",
        top=5,
        budget=Budget(max_steps=1, max_tokens=10**9, max_wall_seconds=1e9),
    )

    assert result.status == "agent_max_steps_exceeded"
    assert result.metrics.schema_valid is True
    assert len(result.entries) == 1
    assert "清单已保存" in result.detail
