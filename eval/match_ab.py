"""L2c · 岗位匹配三模式（离线）—— M10 的验收台。

全程用临时库 + 脚本化 LLM：不发网络请求、不读真实库、不花钱。
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import agent.tools.jobs as jobs_mod
from agent.llm.base import LLMResponse, ToolCall
from agent.memory.db import connect, ensure_schema
from agent.memory.store import upsert_jobs
from agent.runtime.budget import Budget
from agent.tools.sources.base import Job
from eval import Check
from workflows import job_match as jm

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "match_labeled.json"


def load_labeled() -> dict:
    """冻结语料：档案 + 12 条岗位 + relevant 标签。"""
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def seed_labeled(conn, payload: dict) -> None:
    """把冻结语料灌进（临时）库 —— 三种模式看的是同一批岗位。"""
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


class _ScriptedLLM:
    """按剧本回放；剧本用完直接报错。"""

    model = "fake-model"

    def __init__(self, *responses: LLMResponse) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None):  # noqa: ANN001, ANN201 —— 假实现
        self.calls.append([dict(m) for m in messages])
        if not self._responses:
            raise AssertionError("剧本用完了")
        return self._responses.pop(0)


def _wants(name: str, arguments: str, *, call_id: str = "c1") -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        prompt_tokens=10,
        completion_tokens=2,
        finish_reason="tool_calls",
    )


def _says(text: str) -> LLMResponse:
    return LLMResponse(
        content=text,
        tool_calls=[],
        prompt_tokens=10,
        completion_tokens=2,
        finish_reason="stop",
    )


def _save_args(source_id: str, score: int = 90) -> str:
    return json.dumps(
        {
            "entries": [
                {
                    "source": "fixture",
                    "source_id": source_id,
                    "score": score,
                    "reason": "离线验收用",
                }
            ]
        },
        ensure_ascii=False,
    )


@contextmanager
def _isolated_db():
    """临时 DB_PATH / VAR_DIR；退出时恢复原环境变量。"""
    names = ("DB_PATH", "VAR_DIR")
    saved = {name: os.environ.get(name) for name in names}
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "match.db"
        os.environ["DB_PATH"] = str(db_path)
        os.environ["VAR_DIR"] = str(Path(tmp) / "var")
        conn = connect(db_path)
        try:
            ensure_schema(conn)
            payload = load_labeled()
            seed_labeled(conn, payload)
            yield conn, payload
        finally:
            conn.close()
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def _precision(entries, relevant: set[str]) -> float:
    if not entries:
        return 0.0
    hits = sum(1 for entry in entries if entry["source_id"] in relevant)
    return hits / len(entries)


def run_match_checks() -> list[Check]:
    """跑岗位匹配的离线断言，返回结果。"""
    checks: list[Check] = []
    with _isolated_db() as (conn, payload):
        profile = jm.JobProfile.model_validate(payload["profile"])
        relevant = {item["source_id"] for item in payload["jobs"] if item["relevant"]}

        workflow = jm.run_workflow_match(conn, profile, run_id="ev-w", top=5)
        checks.append(
            Check(
                "workflow 的 precision@5 = 1.0",
                _precision(workflow.entries, relevant) == 1.0,
                f"实际 {_precision(workflow.entries, relevant):.2f}",
            )
        )
        checks.append(
            Check(
                "workflow 是 0 次 LLM、0 token",
                workflow.metrics.llm_calls == 0
                and workflow.metrics.prompt_tokens + workflow.metrics.completion_tokens == 0,
                f"llm_calls={workflow.metrics.llm_calls}",
            )
        )

        single = jm.run_single_match(
            conn,
            _ScriptedLLM(_says(_save_args("m01", 88))),
            profile,
            run_id="ev-s",
            top=5,
        )
        checks.append(
            Check(
                "single 恰 1 次 LLM 且 schema 合法",
                single.metrics.llm_calls == 1 and single.metrics.schema_valid,
                f"llm_calls={single.metrics.llm_calls} status={single.status}",
            )
        )

        invalid = jm.run_single_match(
            conn, _ScriptedLLM(_says("我觉得都不错")), profile, run_id="ev-bad", top=5
        )
        checks.append(
            Check(
                "single 的散文输出被判 invalid_output",
                invalid.status == jm.STATUS_INVALID_OUTPUT and not invalid.metrics.schema_valid,
                f"status={invalid.status}",
            )
        )

        agent = jm.run_agent_match(
            conn,
            _ScriptedLLM(
                _wants("search_jobs", '{"query": "Python 后端"}', call_id="c1"),
                _wants("get_job", '{"job_id": "fixture:m01"}', call_id="c2"),
                _wants("save_shortlist", _save_args("m01"), call_id="c3"),
                _says("完成"),
            ),
            profile,
            run_id="ev-a",
            top=5,
            budget=Budget(max_steps=20, max_tokens=10**9, max_wall_seconds=600.0),
        )
        checks.append(
            Check(
                "agent 走完检索 → 详情 → 提交，清单可复核",
                agent.status == jm.STATUS_SUCCESS
                and agent.metrics.tool_calls == 3
                and agent.metrics.schema_valid,
                f"status={agent.status} tools={agent.metrics.tool_calls}",
            )
        )

        no_list = jm.run_agent_match(
            conn,
            _ScriptedLLM(
                _wants("search_jobs", '{"query": "Python"}', call_id="c1"),
                _says("完成了"),
            ),
            profile,
            run_id="ev-nolist",
            top=5,
            budget=Budget(max_steps=20, max_tokens=10**9, max_wall_seconds=600.0),
        )
        checks.append(
            Check(
                "只检索不提交 → no_shortlist（不拿旧清单冒充）",
                no_list.status == jm.STATUS_NO_SHORTLIST and no_list.entries == (),
                f"status={no_list.status}",
            )
        )

        attempts: list[int] = []
        original = jobs_mod.get_source

        def _bomb():
            attempts.append(1)
            raise AssertionError("匹配任务不该碰网络")

        jobs_mod.get_source = _bomb
        try:
            injected = jm.run_agent_match(
                conn,
                _ScriptedLLM(
                    _wants("search_jobs", '{"query": "后端"}', call_id="c1"),
                    _wants("fetch_job_list", '{"page": 1}', call_id="c2"),
                    _says("完成"),
                ),
                profile,
                run_id="ev-inj",
                top=5,
                budget=Budget(max_steps=20, max_tokens=10**9, max_wall_seconds=600.0),
            )
        finally:
            jobs_mod.get_source = original
        checks.append(
            Check(
                "照注入串点 fetch_job_list → tool_not_allowed 且零网络",
                injected.metrics.blocked_tool_calls == 1 and attempts == [],
                f"blocked={injected.metrics.blocked_tool_calls} attempts={len(attempts)}",
            )
        )

        ab = jm.run_ab(
            conn,
            _ScriptedLLM(
                _says(_save_args("m02", 80)),
                _wants("search_jobs", '{"query": "Python"}', call_id="c1"),
                _wants("save_shortlist", _save_args("m01", 90), call_id="c2"),
                _says("完成"),
            ),
            profile,
            run_id="ev-ab",
            top=5,
            budget=Budget(max_steps=20, max_tokens=10**9, max_wall_seconds=600.0),
        )
        modes = [result.mode for result in ab.results]
        checks.append(
            Check(
                "A/B 三条腿都留下 artifact",
                modes == ["workflow", "single", "agent"]
                and all(result.artifact_path for result in ab.results),
                f"modes={modes}",
            )
        )
        markdown = Path(ab.markdown_path).read_text(encoding="utf-8")
        checks.append(
            Check(
                "A/B 表含三行（workflow / single / agent）",
                all(f"| {mode} |" in markdown for mode in modes),
            )
        )
        runs = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
        checks.append(Check("匹配不写 runs（不占日报额度、不污染看板）", runs == 0, f"runs={runs}"))

    return checks
