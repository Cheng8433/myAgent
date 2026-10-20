"""三层评估的汇总入口 —— `python -m eval.run_eval`（从仓库根目录跑）。"""

from __future__ import annotations

import os
import sys

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIPPED = 2


class _Counter:
    """pytest 插件：数通过 / 失败。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def pytest_runtest_logreport(self, report) -> None:  # noqa: ANN001 —— pytest 钩子
        if report.failed:
            self.failed += 1
        elif report.when == "call" and report.passed:
            self.passed += 1


def _run_l1() -> tuple[int, int]:
    """跑 L1（pytest），返回 (passed, failed)。"""
    import pytest

    counter = _Counter()
    pytest.main(["tests", "-p", "no:cacheprovider"], plugins=[counter])
    return counter.passed, counter.failed


def _report_checks(checks) -> int:  # noqa: ANN001 —— list[eval.Check]
    """打印没通过的断言（绿的不打），返回失败条数。"""
    failed = 0
    for c in checks:
        if not c.ok:
            failed += 1
            print(f"    BAD {c.label}  {c.note}")
    return failed


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()

    passed, failed = _run_l1()
    print(f"=== L1 单元测试 === {passed} passed / {failed} failed")

    from eval import injection, match_ab, replay

    l2 = replay.run_replay()
    l2_failed = _report_checks(l2)
    print(
        f"=== L2 流程回放 === {len(l2) - l2_failed} passed / {l2_failed} failed"
        "  (fixture: remoteok_sample)"
    )

    l2b = injection.run_injection_checks()
    l2b_failed = _report_checks(l2b)
    print(f"=== L2b 注入防线（离线）=== {len(l2b) - l2b_failed} passed / {l2b_failed} failed")

    l2c = match_ab.run_match_checks()
    l2c_failed = _report_checks(l2c)
    print(f"=== L2c 岗位匹配（离线）=== {len(l2c) - l2c_failed} passed / {l2c_failed} failed")

    model = os.getenv("LLM_MODEL", "?")
    baseline_failed = failed or l2_failed or l2b_failed or l2c_failed
    if not os.getenv("LLM_API_KEY"):
        print("=== L3 语义评估 === SKIPPED —— 未设 LLM_API_KEY，本次【未验证】模型输出")
        print("=== Token 成本 === 本次评估消耗 0 tokens（L3 未运行）")
        return EXIT_SKIPPED if not baseline_failed else EXIT_FAIL

    from agent.llm.openai_compat import OpenAICompatProvider
    from eval import semantic

    jobs = semantic.load_jobs()
    checks, prompt_tokens, completion_tokens = semantic.evaluate(jobs, OpenAICompatProvider())
    print("=== L3 语义评估 ===")
    print("  report_summary: " + " ".join(f"{c.label}={'100%' if c.ok else '0%'}" for c in checks))
    l3_failed = _report_checks(checks)
    print(f"=== Token 成本 === {semantic.cost_line(model, prompt_tokens, completion_tokens)}")

    return EXIT_FAIL if (baseline_failed or l3_failed) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
