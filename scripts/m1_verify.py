"""M1 验收脚本 —— 用【我们自己的抽象】走完一轮 ReAct。

和 m1_probe.py 的区别：
  · m1_probe.py 是显微镜，裸 httpx 看原始协议形状（学习用）
  · 本脚本是验收台，只碰 agent.llm.base 的协议（产品用）
    它【不认识】openai SDK —— 这正是 M1 要证明的那件事。

运行（Windows 必须带 PYTHONUTF8=1，见 README）：
    PYTHONUTF8=1 ./.venv/Scripts/python.exe scripts/m1_verify.py

★ 断言是这门课的考点。哪条挂了，就是哪个契约没守住：
  想看懂它是怎么挂的，把断言换成 print 再跑一遍。
"""

import json

from dotenv import load_dotenv

from agent.llm.base import LLMResponse, ToolCall
from agent.llm.openai_compat import OpenAICompatProvider

# ★ load_dotenv() 必须在【实例化 Provider】之前。
#   import 只是加载类定义，Provider 是在 __init__ 里读环境变量的。
load_dotenv()


# ══════════════════════════════════════════════════════════
#  工具定义 —— 从 m1_probe.py 抄来的，M2 会给它们找个正式的家
# ══════════════════════════════════════════════════════════
TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市未来一天的天气与降水概率，用于判断是否需要带伞。",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "上海"}},
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "查询指定城市的当前本地时间与时区。",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "上海"}},
                "required": ["city"],
            },
        },
    },
]


# ── 假工具的真实实现（模型不会执行它们，是我们本地执行）─────
def get_weather(city: str) -> dict:
    return {"city": city, "rain": True, "temp_c": 18}


def get_time(city: str) -> dict:
    return {"city": city, "local_time": "2026-09-21T09:00:00+08:00"}


FAKE_IMPL = {"get_weather": get_weather, "get_time": get_time}


def main() -> None:
    provider = OpenAICompatProvider()
    question = "明天上海要带伞吗？"

    # ══════════════════════════════════════════════════════
    #  第 1 轮：模型应该点单
    # ══════════════════════════════════════════════════════
    print("═══ 第 1 轮 ═══")
    messages: list[dict] = [{"role": "user", "content": question}]
    resp1 = provider.chat(messages=messages, tools=TOOLS)

    # ── 考点 1：返回值【必须是我们的类型】，不能是 SDK 的对象 ──
    #   这是 Leaky Abstraction 的探针。哪天有人图省事
    #   在 openai_compat 里 `return resp`，这条立刻炸。
    assert isinstance(resp1, LLMResponse), (
        f"chat() 应返回 LLMResponse，实际是 {type(resp1).__module__}.{type(resp1).__name__}"
    )
    assert isinstance(resp1.tool_calls, list), "tool_calls 必须归一化成 list"
    assert all(isinstance(tc, ToolCall) for tc in resp1.tool_calls), (
        "tool_calls 里混进了非 ToolCall 对象 —— 抽象漏了"
    )

    # ── 考点 2：wants_tools 是循环的【唯一】终止判据 ──
    #   注意：这里刻意【不】看 resp1.content。content 可能是开场白
    #   （"我来查一下天气……"），非空不代表答完了。
    assert resp1.wants_tools is True, (
        "模型没点单？检查 TOOLS 的 description 够不够具体，或换个更明确的提问"
    )

    # ── 考点 3：arguments 保持【原始字符串】，Provider 不许替我们解析 ──
    #   理由见 base.py：解析失败是一种可以回灌给模型的错误，
    #   在这里解析会让异常逃逸出 Provider 层。
    print(f"  模型点了 {len(resp1.tool_calls)} 单：")
    for tc in resp1.tool_calls:
        assert isinstance(tc.arguments, str), (
            f"arguments 应是原始 str，实际 {type(tc.arguments).__name__} —— "
            "Provider 越界替上层做了解析"
        )
        assert tc.id, "tool_call_id 不能为空，回灌时要靠它配对"
        print(f"    · {tc.name}({tc.arguments})")
    print(f"  usage: prompt={resp1.prompt_tokens} completion={resp1.completion_tokens}")

    # ══════════════════════════════════════════════════════
    #  执行那一单，并把结果回灌
    #  ★ 注意这整段【没有 import openai】—— 我们用的是 ToolCall.id /
    #    .name / .arguments，形状刚好和裸协议的 call["function"][...] 对应，
    #    但这是我们的定义，不是 SDK 的。换供应商这行不用改。
    # ══════════════════════════════════════════════════════
    # 回灌第 [1] 条：assistant 消息必须【原样放回】，包括 tool_calls，
    # 否则第二轮模型不知道自己点过单。这里用 api 认的字段名重建。
    messages.append(
        {
            "role": "assistant",
            "content": resp1.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in resp1.tool_calls
            ],
        }
    )

    # 回灌第 [2..] 条：每单一条 role="tool"，content 必须是【字符串】
    for tc in resp1.tool_calls:
        if tc.name not in FAKE_IMPL:
            # 模型可能幻觉出不存在的工具名。M3 会正式处理这条路，
            # 现在先直接炸，让问题看得见。
            raise ValueError(f"模型点了一个不存在的工具：{tc.name}")
        args = json.loads(tc.arguments)  # ← 解析在这里做，不在 Provider
        result = FAKE_IMPL[tc.name](**args)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tc.id,  # ★ 必须和 assistant 里的 id 对上
                "content": json.dumps(result, ensure_ascii=False),
            }
        )

    # ══════════════════════════════════════════════════════
    #  第 2 轮：模型拿到工具结果，应该答完
    # ══════════════════════════════════════════════════════
    print("\n═══ 第 2 轮 ═══")
    # ★ 仍然带 tools：模型可能还想再点一单（比如再查 get_time）。
    #   "什么时候不用带 tools"由模型决定，不由我们猜。
    resp2 = provider.chat(messages=messages, tools=TOOLS)

    # ── 考点 4：终止。这是 M3 那个 while 循环的出口 ──
    assert resp2.wants_tools is False, (
        f"模型还想点单（{len(resp2.tool_calls)} 单），循环该继续而不是结束"
    )
    assert resp2.content, "循环终止了，但 content 是空的 —— 没有答案可交付"
    assert resp2.prompt_tokens > 0 and resp2.completion_tokens > 0, (
        "用量没回填。M6 的成本归因全靠这两个数，Provider 不许偷懒"
    )

    print("  最终回答:", resp2.content)
    print(
        f"  usage: prompt={resp2.prompt_tokens} completion={resp2.completion_tokens}"
        f"  finish_reason={resp2.finish_reason!r}"
    )

    print("\n✅ M1 通过：抽象层立住了，ReAct 的两端（点单 / 答完）都走得通。")


if __name__ == "__main__":
    main()
