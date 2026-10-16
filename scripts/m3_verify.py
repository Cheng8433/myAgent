"""M3 验收脚本 —— 用【假 LLM】把 ReAct 循环逼到每一条边界上。

为什么不用真模型来测：
  真模型的"发挥"会让断言时灵时不灵。我们要验的是【循环的逻辑】，
  不是模型的脾气。假 Provider 每次返回剧本里写好的响应，结果完全确定 ——
  而且它还会把每次收到的 messages 记下来，供我们检查"到底回灌了什么"。

运行：
    PYTHONUTF8=1 ./.venv/Scripts/python.exe scripts/m3_verify.py
"""

import json
import os
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel

import agent.tools.jobs  # noqa: F401  ← 触发真实工具注册
from agent.llm.base import LLMResponse, ToolCall
from agent.memory.db import new_run_id
from agent.runtime.budget import Budget
from agent.runtime.loop import _assistant_message, run_agent
from agent.tools.registry import TOOLS, tool


def budget_of(max_steps: int) -> Budget:
    """只卡步数，把另外两道闸放到够不到的地方。

    ★ M3 验的是【循环逻辑】，不是预算。token / wall 闸要是在这里生效，
      测出来的就不是"循环该不该停"，而是"预算配得够不够大" ——
      一个实验一次只动一个变量。
      （#21 给 run_agent 换签名时漏改了这里，8 个调用点全 TypeError；
       跑新脚本 m4_verify.py 是发现不了的，因为它传的是 budget=。）
    """
    return Budget(max_steps=max_steps, max_tokens=10**9, max_wall_seconds=1e9)


# ══════════════════════════════════════════════════════════════
#  测试替身
# ══════════════════════════════════════════════════════════════


class ScriptedLLM:
    """照着剧本走的假 Provider。第 n 次 chat() 返回第 n 条预设响应。"""

    # M6 起协议多了 `model`（trace 拿它做成本归因）；假 Provider 也要报一个。
    model = "scripted-fake"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._script = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None):
        self.calls.append([dict(m) for m in messages])
        if not self._script:
            raise AssertionError(f"剧本用完了，循环却还要第 {len(self.calls)} 轮 —— 说明它没停下来")
        return self._script.pop(0)


# ★ 假 Provider 也必须遵守 LLMProvider 契约第 3 条：回填真实 token 用量。
#   这里填固定的非零值，循环那头才验得出"有没有在累加"。
#   （早先这俩函数忘了填，默认 0 —— 于是断言在冤枉循环，而不是循环真的漏了累加。）
FAKE_PROMPT_TOKENS = 100
FAKE_COMPLETION_TOKENS = 10


def wants(*pairs: tuple[str, str], content: str | None = None) -> LLMResponse:
    """造一条"要调工具"的响应。pairs = [(工具名, 参数JSON字符串), ...]"""
    return LLMResponse(
        content=content,
        tool_calls=[
            ToolCall(id=f"call_{i}", name=name, arguments=args)
            for i, (name, args) in enumerate(pairs)
        ],
        prompt_tokens=FAKE_PROMPT_TOKENS,
        completion_tokens=FAKE_COMPLETION_TOKENS,
        finish_reason="tool_calls",
    )


def says(text: str) -> LLMResponse:
    """造一条"答完了"的响应。"""
    return LLMResponse(
        content=text,
        tool_calls=[],
        prompt_tokens=FAKE_PROMPT_TOKENS,
        completion_tokens=FAKE_COMPLETION_TOKENS,
        finish_reason="stop",
    )


class _NoArgs(BaseModel):
    pass


class _PageArgs(BaseModel):
    page: int = 1


EXECUTED: list[str] = []


def register_fake(
    name: str,
    result: dict,
    *,
    args_model: type[BaseModel] = _NoArgs,
    raises: Exception | None = None,
) -> None:
    """注册一个假工具，并把每次执行记进 EXECUTED（用来数它被调了几次）。"""

    def fake(**kwargs: Any) -> dict:
        EXECUTED.append(name)
        if raises is not None:
            raise raises
        return result

    fake.__name__ = name
    fake.__doc__ = f"测试用假工具 {name}。"  # 空描述会被 ToolSpec 直接拒绝
    tool(args_model=args_model)(fake)  # type: ignore[call-arg]


def last_tool_message(llm: ScriptedLLM, round_index: int = 1) -> dict:
    """取出第 round_index 轮请求里回灌给模型的 tool 消息内容。"""
    messages = llm.calls[round_index]
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs, f"第 {round_index} 轮没有回灌任何 tool 消息 —— 模型会原地打转"
    return json.loads(tool_msgs[-1]["content"])


# ══════════════════════════════════════════════════════════════
#  检查项
# ══════════════════════════════════════════════════════════════


def check_assistant_message_shape() -> None:
    print("═══ 1. _assistant_message 的协议形状 ═══")
    msg = _assistant_message(
        LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="c1", name="t", arguments="{}")],
            finish_reason="tool_calls",
        )
    )
    assert msg["role"] == "assistant"
    assert msg["content"] == "", f"content 为 None 时必须归一化成 ''，实际 {msg['content']!r}"

    # ★ 多余字段：DeepSeek 容忍，但别家可能 400。
    #   M1 那句"换供应商只改 .env"要靠这种纪律才守得住。
    assert set(msg) == {"role", "content", "tool_calls"}, (
        f"assistant 消息里混进了非协议字段：{sorted(set(msg) - {'role', 'content', 'tool_calls'})}"
    )
    call = msg["tool_calls"][0]
    assert set(call) == {"id", "type", "function"}, f"tool_call 形状不对：{sorted(call)}"
    assert call["type"] == "function"
    assert set(call["function"]) == {"name", "arguments"}, (
        f"function 形状不对：{sorted(call['function'])}"
    )
    print("  形状合规、content 已归一化、无多余字段 ✓")


def check_happy_path() -> None:
    print("\n═══ 2. 正常一轮即答 ═══")
    EXECUTED.clear()
    register_fake("fake_ok", {"ok": True, "items": ["a", "b"], "count": 2})
    llm = ScriptedLLM([wants(("fake_ok", "{}")), says("做完了")])

    result = run_agent(
        "随便干点什么", llm, run_id=new_run_id(), tool_names=["fake_ok"], budget=budget_of(4)
    )

    assert result.status == "success", f"应正常结束，实际 {result.status}"
    assert result.answer == "做完了"
    # ★ 卡【具体数字】而不是 > 0：跑了 2 轮，每轮假 Provider 报 100+10，
    #   结果就必须是 200 / 20。卡住数字才抓得到"少累加一轮"或
    #   "把单步用量和累计用量写反"这类偏移。
    assert len(llm.calls) == 2, f"应问模型 2 次，实际 {len(llm.calls)} 次"
    expected_prompt = FAKE_PROMPT_TOKENS * len(llm.calls)
    expected_completion = FAKE_COMPLETION_TOKENS * len(llm.calls)
    assert (result.prompt_tokens, result.completion_tokens) == (
        expected_prompt,
        expected_completion,
    ), (
        f"用量应为 {expected_prompt}/{expected_completion}，实际 "
        f"{result.prompt_tokens}/{result.completion_tokens} —— 没累加，或累加了错的东西"
    )
    # ★ 单步留痕必须是【这一轮】的用量，不是累计值 —— 否则 M6 的成本归因
    #   会把第 2 步读成"这一步就烧了 200 token"，画出来的曲线全是假的。
    #   （这里卡的是"每一条都等于单轮值"，而不是"末条等于累计值" ——
    #     后者恰恰是在给 bug 开绿灯，抚子第一版就写错了。）
    llm_steps = [s for s in result.steps if s.kind == "llm"]
    assert llm_steps, "没有留下 llm 步的痕迹"
    assert all(s.prompt_tokens == FAKE_PROMPT_TOKENS for s in llm_steps), (
        "StepRecord.prompt_tokens 应是单轮值"
        f"（每步都该是 {FAKE_PROMPT_TOKENS}），实际 {[s.prompt_tokens for s in llm_steps]}"
    )
    assert all(s.completion_tokens == FAKE_COMPLETION_TOKENS for s in llm_steps), (
        "StepRecord.completion_tokens 应是单轮值"
        f"（每步都该是 {FAKE_COMPLETION_TOKENS}），实际 "
        f"{[s.completion_tokens for s in llm_steps]}"
    )

    # ★ 考点：成功路径上工具必须【恰好执行一次】。
    #   多执行一次 = 一次抓取变两次、一条飞书变两条（write 工具会出真事故）。
    assert EXECUTED.count("fake_ok") == 1, (
        f"工具被执行了 {EXECUTED.count('fake_ok')} 次，应恰好 1 次"
    )

    fed = last_tool_message(llm)
    assert fed["ok"] is True
    # ★ 考点：成功时工具自己的返回值要【原样透传】，循环不该再套一层壳。
    #   套了壳的话，真实数据会藏进 result["result"] 里，模型要多绕一层才读得到。
    assert "items" in fed, f"工具返回值被套了壳，模型看到的是 {sorted(fed)}"

    # tool_call_id 必须和点单时的一一对应
    tool_msgs = [m for m in llm.calls[1] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1, f"只点了 1 单却回灌了 {len(tool_msgs)} 条"
    assert tool_msgs[0]["tool_call_id"] == "call_0", "tool_call_id 对不上，真接口会 400"
    print("  status=success，只执行 1 次、原样透传、id 对应、用量已累加 ✓")


def check_unknown_tool() -> None:
    print("\n═══ 3. 模型幻觉出不存在的工具名 ═══")
    register_fake("fake_ok", {"ok": True})
    llm = ScriptedLLM([wants(("查天气的tool", "{}")), says("好的")])

    result = run_agent(
        "随便", llm, run_id=new_run_id(), tool_names=["fake_ok"], budget=budget_of(4)
    )

    assert result.status == "success", "幻觉工具名不该让整个 run 崩掉"
    fed = last_tool_message(llm)
    assert fed["error"] == "unknown_tool", f"应回灌 unknown_tool，实际 {fed.get('error')!r}"
    assert "fake_ok" in fed["hint"], "hint 里要列出可用工具，模型才知道改成什么"
    print("  回灌 unknown_tool + 可用清单，模型有机会自我纠正 ✓")


def check_invalid_args() -> None:
    print("\n═══ 4. 参数不合法（M1「保原始字符串」的兑现点）═══")
    EXECUTED.clear()
    register_fake("fake_page", {"ok": True}, args_model=_PageArgs)
    llm = ScriptedLLM([wants(("fake_page", "{这不是JSON")), says("好的")])

    result = run_agent(
        "随便", llm, run_id=new_run_id(), tool_names=["fake_page"], budget=budget_of(4)
    )

    assert result.status == "success", "参数不合法不该让整个 run 崩掉"
    fed = last_tool_message(llm)
    assert fed["error"] == "invalid_args", f"应回灌 invalid_args，实际 {fed.get('error')!r}"
    assert "fake_page" not in EXECUTED, "参数校验失败的调用【不应】被执行"
    print("  解析失败被翻译成 invalid_args 回灌，且工具未被误执行 ✓")


def check_tool_crash() -> None:
    print("\n═══ 5. 工具抛出【意料之外】的异常（except Exception 的意义）═══")
    EXECUTED.clear()
    register_fake("fake_boom", {"ok": True}, raises=RuntimeError("数据库连接断了"))
    llm = ScriptedLLM([wants(("fake_boom", "{}")), says("好的")])

    result = run_agent(
        "随便", llm, run_id=new_run_id(), tool_names=["fake_boom"], budget=budget_of(4)
    )

    assert result.status == "success", (
        f"单个工具崩溃不该让整个 run 死掉（实际 {result.status}）—— 整轮失败就什么都查不出来了"
    )
    fed = last_tool_message(llm)
    assert fed.get("ok") is False, f"工具崩了却报告 {fed!r} —— 异常被咽下去了"
    assert "RuntimeError" in str(fed.get("hint", "")), (
        "hint 里要带上异常类型和原文，模型和日志才看得见发生了什么"
    )
    print("  工具崩溃被翻译成 ok=False 回灌，run 继续 ✓")


def check_loop_detection() -> None:
    print("\n═══ 6. 原地打转 → loop_detected（而不是烧光 token）═══")
    register_fake("fake_fail", {"ok": False, "error": "boom", "hint": "没救了"})
    # 剧本给足 10 轮；如果循环没打断，它会一直问到剧本用完
    llm = ScriptedLLM([wants(("fake_fail", "{}")) for _ in range(10)])

    result = run_agent(
        "随便",
        llm,
        run_id=new_run_id(),
        tool_names=["fake_fail"],
        budget=budget_of(15),
        repeat_limit=2,
    )

    assert result.status == "loop_detected", (
        f"应触发死循环检测，实际 {result.status} —— 循环在拿钱空转"
    )
    assert len(llm.calls) == 2, (
        f"repeat_limit=2 表示「连续 2 次一模一样」才停，应恰好问 2 轮，实际 {len(llm.calls)} 轮"
    )
    print(f"  连续 {len(llm.calls)} 轮同参数同工具 → 第 {len(llm.calls)} 轮打断 ✓")


def check_repeat_limit_is_configurable() -> None:
    """repeat_limit=3 就该校在第 3 轮 —— 抓"把 2 写死在 if 里"。"""
    print("\n═══ 6b. repeat_limit 是可调的参数，不是写死的 2 ═══")
    register_fake("fake_fail", {"ok": False, "error": "boom", "hint": "没救了"})
    llm = ScriptedLLM([wants(("fake_fail", "{}")) for _ in range(10)])

    result = run_agent(
        "随便",
        llm,
        run_id=new_run_id(),
        tool_names=["fake_fail"],
        budget=budget_of(15),
        repeat_limit=3,
    )

    assert result.status == "loop_detected", f"应触发死循环检测，实际 {result.status}"
    # ★ 这里卡 3 不卡 2：写死 2 的实现在上一项照样过，只有参数一变才露馅。
    assert len(llm.calls) == 3, (
        f"repeat_limit=3 应在第 3 轮打断，实际问了 {len(llm.calls)} 轮 —— "
        "是不是把 repeat_limit 写死成了 2？"
    )
    print(f"  repeat_limit=3 → 第 {len(llm.calls)} 轮打断，参数确实在生效 ✓")


def check_max_steps() -> None:
    print("\n═══ 7. 参数每轮不同 → 走 max_steps（不能误判成死循环）═══")
    register_fake("fake_page", {"ok": True}, args_model=_PageArgs)
    # 每轮参数都不同 —— 这是【合法】的连续调用（比如翻页），不该被当成打转
    llm = ScriptedLLM([wants(("fake_page", json.dumps({"page": i}))) for i in range(1, 11)])

    result = run_agent(
        "随便",
        llm,
        run_id=new_run_id(),
        tool_names=["fake_page"],
        budget=budget_of(3),
        repeat_limit=2,
    )

    assert result.status == "max_steps_exceeded", (
        f"应撞上步数上限，实际 {result.status} —— 是不是把「连续相同」写成了「累计」？"
    )
    assert len(llm.calls) == 3, f"max_steps=3 应该只问 3 轮，实际 {len(llm.calls)} 轮"
    print("  翻页式调用没被误判，第 3 轮撞上限正常退出 ✓")


def check_real_tool() -> bool:
    """真实链路只有这一项验得到。返回 True 表示【真的验过了】。

    ★ 这里曾经写的是 `print("跳过：没有 LLM_API_KEY")` + return —— 而且脚本
      压根没调 load_dotenv()，于是 .env 里明明有 key，这一项也永远"跳过"，
      末尾照样打 ✅。两处毛病叠在一起就成了【假绿灯】：绿的是"其余 7 项"，
      读的人却以为是"整条链路"。跳过必须是响的，不是哑的。
    """
    print("\n═══ 8. 真实工具体验（走真模型，验真链路）═══")
    if not os.environ.get("LLM_API_KEY"):
        print("  ✗ 未验证：环境里没有 LLM_API_KEY（.env 里有的话，检查 load_dotenv 调了没）")
        return False

    from agent.llm.openai_compat import OpenAICompatProvider

    llm = OpenAICompatProvider()
    assert "fetch_job_list" in TOOLS
    result = run_agent(
        "拉取招聘列表第 1 页，只列 3 个公司名，不要展开。",
        llm,
        run_id=new_run_id(),
        tool_names=["fetch_job_list"],
        budget=budget_of(6),
    )
    print(f"  status={result.status}  步数={len(result.steps)}")
    print(f"  token={result.prompt_tokens}+{result.completion_tokens}")
    print(f"  回答：{result.answer[:160]}")
    assert result.status == "success", f"真实链路没走通：{result.status}"
    print("  ✓ 真模型 + 真工具 + 真循环，端到端走通")
    return True


def main() -> None:
    # ★ 先 load_dotenv：上面那个"没 key 就跳过"的分支，之前因为缺这一行
    #   而永远成立 —— 环境变量只在 .env 里，os.environ 里当然没有。
    load_dotenv()

    check_assistant_message_shape()
    check_happy_path()
    check_unknown_tool()
    check_invalid_args()
    check_tool_crash()
    check_loop_detection()
    check_repeat_limit_is_configurable()
    check_max_steps()
    if not check_real_tool():
        # ★ 退出码 2 而不是 0：没验过 ≠ 通过。让 CI / 一眼扫过的人都看得见。
        print("\n⚠️ 前 7 项通过，但【端到端链路未验证】—— 这不是全绿。")
        raise SystemExit(2)
    print("\n✅ M3 通过：循环的出口、回灌、三种终止条件、以及真实链路都守住了。")


if __name__ == "__main__":
    main()
