"""三层评估的汇总入口 —— `python -m eval.run_eval`（从仓库根目录跑）。

输出格式照 PLAN §M7（见 docs/PLAN.md）：

    === L1 单元测试 === 73 passed / 0 failed
    === L2 流程回放 === 9 passed / 0 failed  (fixture: remoteok_sample)
    === L3 语义评估 ===
      report_summary: schema_ok=100% url_grounded=100% length_ok=100% count_match=100%
    === Token 成本 === 本次评估消耗 1,234 tokens

★ 退出码是【对外契约】（同 app/main.py）：
    0 = 全绿（含 L3 真的跑过了）
    1 = 有断言失败
    2 = L3 被【诚实跳过】（没配 LLM_API_KEY）—— 绝不把"没验证"混进绿灯。
  为什么单立一个 2：CI/cron 只看退出码。把"没跑 L3"和"L3 通过了"都报 0，
  等于让"我们从来没验过模型输出"这件事永远看不见 —— 那正是本项目最恨的假绿。
"""

from __future__ import annotations

import os
import sys

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_SKIPPED = 2


class _Counter:
    """pytest 插件：数通过 / 失败。

    ★ 三阶段（setup / call / teardown）都要数：fixture 崩了是 setup 阶段的
      failed，跳过了 call —— 只看 call 的话，一个崩在 fixture 里的用例
      会被算成"没通过也没失败"，而总数看着还对。数错了比数不出更危险。
    """

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
    # `-p no:cacheprovider`：评估不该在仓库里留下 .pytest_cache 这种副产物。
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

    # ── L1：单元回归网 ──
    passed, failed = _run_l1()
    print(f"=== L1 单元测试 === {passed} passed / {failed} failed")

    # ── L2：流程回放（离线、确定性） ──
    # ★ 惰性 import（挪到 main 里、且放在 L1 之后）：replay/semantic 会 import
    #   respx，而 respx 在 pytest 启动【之前】就进 sys.modules 的话，pytest 的
    #   断言重写就没法给它打补丁（PytestAssertRewriteWarning）。晚一步 import
    #   就没这条噪音 —— 同 app/main.py 那句"每个子命令只付自己的启动成本"。
    from eval import injection, replay

    l2 = replay.run_replay()
    l2_failed = _report_checks(l2)
    print(
        f"=== L2 流程回放 === {len(l2) - l2_failed} passed / {l2_failed} failed"
        "  (fixture: remoteok_sample)"
    )

    # ── L2b：注入防线（离线） ──
    # ★ 单立一节而不是塞进 L2：它验的不是"日报流程对不对"，而是
    #   "网页数据能不能越界变成指令" —— 两条问题，各有一个计数。
    l2b = injection.run_injection_checks()
    l2b_failed = _report_checks(l2b)
    print(f"=== L2b 注入防线（离线）=== {len(l2b) - l2b_failed} passed / {l2b_failed} failed")

    model = os.getenv("LLM_MODEL", "?")
    if not os.getenv("LLM_API_KEY"):
        print("=== L3 语义评估 === SKIPPED —— 未设 LLM_API_KEY，本次【未验证】模型输出")
        print("=== Token 成本 === 本次评估消耗 0 tokens（L3 未运行）")
        # ★ 即便 L1/L2 全绿，也返回 2：跳过不是通过。
        return EXIT_SKIPPED if not (failed or l2_failed or l2b_failed) else EXIT_FAIL

    # ── L3：语义评估（真模型） ──
    from agent.llm.openai_compat import OpenAICompatProvider
    from eval import semantic

    jobs = semantic.load_jobs()
    checks, prompt_tokens, completion_tokens = semantic.evaluate(jobs, OpenAICompatProvider())
    print("=== L3 语义评估 ===")
    print("  report_summary: " + " ".join(f"{c.label}={'100%' if c.ok else '0%'}" for c in checks))
    l3_failed = _report_checks(checks)
    print(f"=== Token 成本 === {semantic.cost_line(model, prompt_tokens, completion_tokens)}")

    return EXIT_FAIL if (failed or l2_failed or l2b_failed or l3_failed) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
