"""真机 A/B 实验 —— 标注语料 + 真实 LLM，对比三种做法的质量与成本。

会花钱、会调模型：默认每模式跑 3 轮，只作定性演示，不构成统计结论。
用法：PYTHONUTF8=1 ./.venv/Scripts/python.exe scripts/match_ab.py --repeat 3
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from agent.config import ConfigError
from agent.memory.db import connect, ensure_schema, new_run_id
from agent.memory.store import var_dir, write_json_atomic
from agent.runtime.budget import Budget
from workflows import job_match as jm

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
"""让 `python scripts/match_ab.py` 也能 import 仓库根目录下的 eval 包。"""

from eval.match_ab import load_labeled, seed_labeled  # noqa: E402

MODES = (jm.MODE_WORKFLOW, jm.MODE_SINGLE, jm.MODE_AGENT)


def _ids(result) -> set[str]:  # noqa: ANN001 —— MatchResult
    return {entry["source_id"] for entry in result.entries}


def _summarize(mode: str, results: list, relevant: set[str], top: int) -> dict:  # noqa: ANN001
    n = len(results)
    precision = (
        sum(
            len(_ids(result) & relevant) / len(result.entries) if result.entries else 0.0
            for result in results
        )
        / n
    )
    recall = sum(len(_ids(result) & relevant) / len(relevant) for result in results) / n
    return {
        "mode": mode,
        "runs": n,
        "precision": precision,
        "recall": recall,
        "schema_rate": sum(1 for result in results if result.metrics.schema_valid) / n,
        "statuses": sorted({result.status for result in results}),
        "avg_llm_calls": sum(result.metrics.llm_calls for result in results) / n,
        "avg_tool_calls": sum(result.metrics.tool_calls for result in results) / n,
        "avg_tokens": sum(
            result.metrics.prompt_tokens + result.metrics.completion_tokens for result in results
        )
        / n,
        "avg_ms": round(sum(result.metrics.wall_ms for result in results) / n),
        "top": top,
    }


def _render(rows: list[dict], base: str) -> str:
    lines = [
        f"# 岗位匹配真机 A/B · {base}",
        "",
        "| 模式 | 轮数 | precision@k | recall@k | schema 合法率 | LLM 调用 | 工具调用 | "
        "平均 tokens | 平均耗时(ms) | 状态 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['mode']} | {row['runs']} | {row['precision']:.2f} | {row['recall']:.2f} | "
            f"{row['schema_rate']:.0%} | {row['avg_llm_calls']:.1f} | "
            f"{row['avg_tool_calls']:.1f} | {row['avg_tokens']:.0f} | {row['avg_ms']} | "
            f"{'、'.join(row['statuses'])} |"
        )
    lines += [
        "",
        "说明：语料为人工标注的小样本；轮数有限，只作定性演示。"
        "真实库没有标注，不能据此宣称线上质量。",
    ]
    return "\n".join(lines) + "\n"


def _run_once(conn, llm, profile, mode: str, base: str, index: int, top: int, budget: Budget):  # noqa: ANN001
    label = f"{base}-{mode}-r{index}"
    if mode == jm.MODE_WORKFLOW:
        return jm.run_workflow_match(conn, profile, run_id=label, top=top)
    if mode == jm.MODE_SINGLE:
        return jm.run_single_match(conn, llm, profile, run_id=label, top=top)
    return jm.run_agent_match(conn, llm, profile, run_id=label, top=top, budget=budget)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="岗位匹配真机 A/B 实验")
    parser.add_argument("--repeat", type=int, default=3, help="每种模式跑几轮（默认 3）")
    parser.add_argument("--top", type=int, default=5, help="每轮最多入选几条（默认 5）")
    args = parser.parse_args(argv)

    if args.repeat < 1 or not 1 <= args.top <= jm.MAX_TOP:
        print("--repeat 必须 ≥1，--top 必须在 1-20 之间。", file=sys.stderr)
        return 2

    from dotenv import load_dotenv

    load_dotenv()

    try:
        from agent.llm.openai_compat import OpenAICompatProvider

        llm = OpenAICompatProvider()
        budget = Budget.from_env()
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        print("请先在 .env 里填好 LLM_API_KEY / LLM_MODEL / AGENT_MAX_*。", file=sys.stderr)
        return 2

    payload = load_labeled()
    profile = jm.JobProfile.model_validate(payload["profile"])
    relevant = {item["source_id"] for item in payload["jobs"] if item["relevant"]}
    base = new_run_id()

    print(f"语料：{len(payload['jobs'])} 条岗位（其中相关 {len(relevant)} 条）")
    print(f"每模式 {args.repeat} 轮，最多入选 {args.top} 条；会真实调用模型。")

    # 只把库放到临时目录；artifact 仍落在 var/shortlists/。
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "match.db"
        os.environ["DB_PATH"] = str(db_path)
        conn = connect(db_path)
        try:
            ensure_schema(conn)
            seed_labeled(conn, payload)
            rows = []
            for mode in MODES:
                results = [
                    _run_once(conn, llm, profile, mode, base, index, args.top, budget)
                    for index in range(1, args.repeat + 1)
                ]
                rows.append(_summarize(mode, results, relevant, args.top))
                print(f"  {mode} 完成：{'、'.join(rows[-1]['statuses'])}")
        finally:
            conn.close()

    markdown = _render(rows, base)
    folder = var_dir() / "shortlists"
    folder.mkdir(parents=True, exist_ok=True)
    md_path = folder / f"ab-{base}.md"
    json_path = folder / f"ab-{base}.json"
    md_path.write_text(markdown, encoding="utf-8")
    write_json_atomic(
        json_path,
        {"run_id": base, "profile": profile.model_dump(), "rows": rows},
    )

    print()
    print(markdown)
    print(f"对比表: {md_path}")
    print(f"机器可读: {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
