"""ReAct 循环单测 —— 离线（假 Provider），盯的是终止条件的【词表】。

★ 为什么只在这些点上断言：循环的形状（Reason→Act→回灌）已经被 m3_verify
  逐条走过一遍，这里不重复讲那个故事 —— 这里是"回归网"：把最容易改坏的
  几处（终止 status 的拼写、tool_calls 的"可遍历空 list"、幻觉工具名不崩）
  钉住，改动代码时它们是最先响的。
"""

from __future__ import annotations

from _fakes import FakeLLM, wide_budget

from agent.llm.base import LLMResponse
from agent.runtime.budget import Budget
from agent.runtime.loop import run_agent


def test_normal_round_returns_success():
    res = run_agent("任务", FakeLLM(text="答案"), run_id="r", tool_names=[], budget=wide_budget())
    assert (res.status, res.answer) == ("success", "答案")


def test_tool_calls_is_iterable_empty_list_not_none():
    """★ 回归护栏：不点单时 tool_calls 必须是【可遍历的空 list】而非 None。

    这一条不会被"循环跑通了"抓到 —— bool(None) 是 False，wants_tools 照样
    为假、循环照样成功返回；而下游任何 `for tc in resp.tool_calls` 会当场
    TypeError。所以只能靠这条直接盯着类型。
    """
    resp = LLMResponse(content="答完了")
    assert resp.tool_calls == []
    assert list(resp.tool_calls) == []
    assert resp.wants_tools is False


def test_max_steps_exceeded():
    res = run_agent(
        "t",
        FakeLLM(always_wants=True),
        run_id="r",
        tool_names=[],
        budget=Budget(max_steps=1, max_tokens=10**9, max_wall_seconds=1e9),
    )
    assert res.status == "max_steps_exceeded"


def test_max_tokens_exceeded():
    res = run_agent(
        "t",
        FakeLLM(always_wants=True),
        run_id="r",
        tool_names=[],
        budget=Budget(max_steps=99, max_tokens=1, max_wall_seconds=1e9),
    )
    assert res.status == "max_tokens_exceeded"


def test_wall_clock_exceeded():
    res = run_agent(
        "t",
        FakeLLM(always_wants=True),
        run_id="r",
        tool_names=[],
        budget=Budget(max_steps=99, max_tokens=10**9, max_wall_seconds=0.0),
    )
    assert res.status == "wall_clock_exceeded"


def test_loop_detected_on_repeated_signature():
    res = run_agent(
        "t",
        FakeLLM(always_wants=True),
        run_id="r",
        tool_names=[],
        budget=wide_budget(),
        repeat_limit=2,
    )
    assert res.status == "loop_detected"


def test_unknown_tool_fed_back_without_crashing():
    """幻觉出不存在的工具名是常态，不是异常 —— 要回灌一条 ok=false 让它自纠。"""
    res = run_agent(
        "t",
        FakeLLM(always_wants=True),
        run_id="r",
        tool_names=[],
        budget=wide_budget(),
        repeat_limit=2,
    )
    tool_recs = [r for r in res.steps if r.kind == "tool"]
    assert tool_recs, "至少要留一条 tool 留痕"
    assert tool_recs[0].error == "unknown_tool"
    assert tool_recs[0].ok is False


def test_registered_but_not_allowed_is_blocked(monkeypatch):
    """★ M8：`tool_names` 同时是【执行权限】，不再只是"发给模型的说明书"。

    这一条只能靠"探针有没有被真的调用"来验 —— 光看回灌了 tool_not_allowed
    还不够：那个分支写完却忘了把执行挪进 else，回灌照样是对的。
    """
    from pydantic import BaseModel

    from agent.tools.registry import TOOLS, ToolSpec

    class _NoArgs(BaseModel):
        pass

    executed: list[str] = []

    def probe_tool() -> dict:
        """只为本用例存在的探针工具；它一旦被执行，列表里就会多一条。"""
        executed.append("probe_tool")
        return {"ok": True}

    monkeypatch.setitem(TOOLS, "probe_tool", ToolSpec(probe_tool, _NoArgs))
    llm = FakeLLM(always_wants=True, tool_name="probe_tool")

    res = run_agent(
        "t",
        llm,
        run_id="r",
        tool_names=[],  # 注册表里有，但这次 run 没有授权
        budget=wide_budget(),
        repeat_limit=2,
    )

    tool_recs = [r for r in res.steps if r.kind == "tool"]
    assert tool_recs[0].error == "tool_not_allowed"
    assert tool_recs[0].ok is False
    assert executed == [], "未授权的工具绝不能被执行 —— 这就是白名单与广告的区别"

    # 可用清单只列【这次能用的】：列一个不能调的工具等于指错路。
    fed = [m for m in llm.calls[1] if m.get("role") == "tool"][0]
    assert "可用工具：[]" in fed["content"]


def test_status_vocabulary_is_closed():
    """循环的 status 是一个封闭词表 —— 改这里就等于改对外契约。"""
    seen = set()
    for budget in (
        Budget(max_steps=1, max_tokens=10**9, max_wall_seconds=1e9),
        Budget(max_steps=99, max_tokens=1, max_wall_seconds=1e9),
        Budget(max_steps=99, max_tokens=10**9, max_wall_seconds=0.0),
    ):
        seen.add(
            run_agent(
                "t", FakeLLM(always_wants=True), run_id="r", tool_names=[], budget=budget
            ).status
        )
    assert seen == {"max_steps_exceeded", "max_tokens_exceeded", "wall_clock_exceeded"}
