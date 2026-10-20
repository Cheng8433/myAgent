"""L3 语义评估 —— 只评【日报摘要】这一个 LLM 判断类产物。"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import respx

from agent.llm.base import LLMProvider, LLMResponse
from agent.runtime.budget import Budget
from agent.tools.sources.base import Job
from agent.tools.sources.remoteok import API_URL, RemoteOkSource
from eval import Check
from workflows.daily_report import _summarize
from workflows.report import render_report

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "remoteok_sample.json"

MIN_CHARS = 20
MAX_CHARS = 600

PRICE_TABLE: dict[str, tuple[float, float]] = {}
"""模型 → (输入元/1K tokens, 输出元/1K tokens)。**故意留空**。"""


def load_jobs() -> list[Job]:
    """今天的岗位 —— 走【真】RemoteOkSource（只有网络被 respx 换成了内存字节）。"""
    body = FIXTURE.read_bytes()
    with respx.mock:
        respx.get(API_URL).mock(return_value=httpx.Response(200, content=body))
        return RemoteOkSource().list_jobs(page=1, limit=200).jobs


_URL_RE = re.compile(r"https?://[^\s)\]<>\"'`]+")
_URL_TRAILING = ".,;:!?)）、。，！？】》"


def _urls_in(text: str) -> set[str]:
    return {u.rstrip(_URL_TRAILING) for u in _URL_RE.findall(text)}


def _data_rows(doc: str) -> list[str]:
    """报告里的岗位行（去掉表头与分隔行）。"""
    return [
        ln for ln in doc.splitlines() if ln.startswith("| ") and not ln.startswith(("| # ", "|---"))
    ]


def _summary_checks(answer: str, jobs: list[Job]) -> list[Check]:
    """四条性质断言。answer 是模型写的那段概括。"""
    text = answer.strip()

    fences_ok = text.count("```") % 2 == 0
    checks = [Check("schema_ok", bool(text) and fences_ok, "" if not text else "围栏不配对")]

    input_urls = {j.url for j in jobs if j.url}
    stray = _urls_in(text) - input_urls
    checks.append(
        Check("url_grounded", not stray, f"凭空出现的 URL：{sorted(stray)}" if stray else "")
    )

    checks.append(
        Check(
            "length_ok",
            MIN_CHARS <= len(text) <= MAX_CHARS,
            f"len={len(text)}（期望 {MIN_CHARS}~{MAX_CHARS}）",
        )
    )

    doc = render_report(jobs, day="2026-10-09", summary=text, run_id="eval", generated_at="eval")
    rows = _data_rows(doc)
    checks.append(Check("count_match", len(rows) == len(jobs), f"{len(rows)}/{len(jobs)}"))

    return checks


def evaluate(
    jobs: list[Job],
    provider: LLMProvider,
    *,
    budget: Budget | None = None,
) -> tuple[list[Check], int, int]:
    """跑一次真的摘要调用，返回 (四条断言, prompt_tokens, completion_tokens)。"""
    budget = budget or Budget(max_steps=4, max_tokens=10**9, max_wall_seconds=120.0)
    answer, _records, prompt_tokens, completion_tokens = _summarize(
        jobs, provider, run_id="eval", budget=budget
    )
    return _summary_checks(answer, jobs), prompt_tokens, completion_tokens


def cost_line(model: str, prompt_tokens: int, completion_tokens: int) -> str:
    """一行成本说明。单价表里没有这个模型时，只报 token、不编价格。"""
    total = prompt_tokens + completion_tokens
    price = PRICE_TABLE.get(model)
    if price is None:
        return f"本次评估消耗 {total:,} tokens（模型 {model} 不在单价表里 —— 只报 token，不编价格）"
    in_price, out_price = price
    cost = prompt_tokens / 1000 * in_price + completion_tokens / 1000 * out_price
    return f"本次评估消耗 {total:,} tokens ≈ ¥{cost:.4f}"


class _FakeSummarizer:
    """离线自测用：把一段写死的文本当成模型输出，不碰网络、不花钱。"""

    model = "fake-model"

    def __init__(self, text: str) -> None:
        self.text = text

    def chat(self, messages, tools=None):  # noqa: ANN001, ANN201 —— 假实现
        return LLMResponse(content=self.text, tool_calls=[], finish_reason="stop")


def _main() -> int:  # pragma: no cover —— 手工探针（离线，不需要 key）
    jobs = load_jobs()
    good = "今天这批岗位集中在工程与产品方向，多数是欧美公司的远程职位。"
    checks, _pt, _ct = evaluate(jobs, _FakeSummarizer(good))
    by_label = {c.label: c.ok for c in checks}
    print("好的摘要 —— 四条性质：", by_label)

    bad = "参考 https://remoteok.com/nonexistent-job-xyz 这条记录。"
    bad_ok = {c.label: c.ok for c in _summary_checks(bad, jobs)}
    print("幻觉链接的摘要 —— url_grounded：", bad_ok["url_grounded"], "（期望 False）")

    all_good = all(by_label.values()) and bad_ok["url_grounded"] is False
    return 0 if all_good else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
