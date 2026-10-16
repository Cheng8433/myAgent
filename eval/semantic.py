"""L3 语义评估 —— 只评【日报摘要】这一个 LLM 判断类产物。

★ 为什么不能断言字符串相等：LLM 输出不稳定（代码里没设 temperature，
  2026-10-07 实测同一份输入两次跑出的摘要不同）。所以断言的是**可验证的性质**，
  不是"答得对不对"。四条性质（前三条真的绑住模型，第四条绑的是渲染器）：

  ① schema_ok    合法 Markdown：非空、代码围栏成对
  ② url_grounded ★ 摘要里出现的每个 URL 都必须在输入里存在 —— 防幻觉。
                 PLAN §M7 点名的那一条，面试加分项：模型没有"外面的知识"
                 可以用来编链接，能编出来的链接一定是它从输入里抄错或凭空造的。
  ③ length_ok    长度在范围内（2~3 句话该有的长度，不是一篇作文也不是空话）
  ④ count_match  渲染出的报告表行数 == 输入条数

★ 为什么 ④ 是"渲染出的行数"，而不是 PLAN 原文的"提及的岗位数量 == 输入数量"：
  `_summary_prompt` 明令模型"只输出概括本身，不要罗列岗位、不要输出清单"，
  于是"提及数量"根本无法从输出里测（测出来恒为 0）。指标换成由【渲染器】
  保证的那条 —— 它同样守着"报告里每条岗位都在"，却是确定性的、不靠模型守规矩。
  （能确定的事交给代码，不确定的事才交给评估 —— 和 §M0"表格由代码渲染"一脉。）

★ 无 key 时【诚实跳过】：不跑、不假装通过、退出码 2（同 m3_verify 的姿态）。
  "没验证"必须是一句响的话，不能混进绿灯里。
"""

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
"""模型 → (输入元/1K tokens, 输出元/1K tokens)。**故意留空**。

★ 编一个价格就是报一个假的成本 —— 而"少算的账本比没有账本更危险"。
  要报价格时，从供应商的定价页抄两个数填进来（键用 .env 里 LLM_MODEL 的值），
  例如：  "deepseek-chat": (0.002, 0.008)   ← 这两个数是举例，别照抄。
  留空的后果只是"只报 token、不报钱"，比"报一个假的钱"诚实得多。
"""


def load_jobs() -> list[Job]:
    """今天的岗位 —— 走【真】RemoteOkSource（只有网络被 respx 换成了内存字节）。

    ★ 不复用 replay 抓到的 jobs：L2 与 L3 各自独立、互不 import，
      免得"改一层要动另一层"。代价是 fixture 被解析两遍 —— 便宜、且明确。
    """
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
    """跑一次真的摘要调用，返回 (四条断言, prompt_tokens, completion_tokens)。

    ★ 走的是生产同一条路：`workflows.daily_report._summarize`。计费就从它的
      返回值取 —— 不另起一套"估算"，否则评估报的成本和线上对不上。
    """
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

    # 反面：塞一个不存在的链接，证明 url_grounded 真的会响（否则它是条假断言）。
    bad = "参考 https://remoteok.com/nonexistent-job-xyz 这条记录。"
    bad_ok = {c.label: c.ok for c in _summary_checks(bad, jobs)}
    print("幻觉链接的摘要 —— url_grounded：", bad_ok["url_grounded"], "（期望 False）")

    all_good = all(by_label.values()) and bad_ok["url_grounded"] is False
    return 0 if all_good else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
