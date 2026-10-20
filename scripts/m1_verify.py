"""M1 验收脚本 —— 用【我们自己的抽象】走完一轮 ReAct。"""

import json

from dotenv import load_dotenv

from agent.llm.base import LLMResponse, ToolCall
from agent.llm.openai_compat import OpenAICompatProvider

load_dotenv()


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


def get_weather(city: str) -> dict:
    return {"city": city, "rain": True, "temp_c": 18}


def get_time(city: str) -> dict:
    return {"city": city, "local_time": "2026-09-21T09:00:00+08:00"}


FAKE_IMPL = {"get_weather": get_weather, "get_time": get_time}


def main() -> None:
    provider = OpenAICompatProvider()
    question = "明天上海要带伞吗？"

    print("═══ 第 1 轮 ═══")
    messages: list[dict] = [{"role": "user", "content": question}]
    resp1 = provider.chat(messages=messages, tools=TOOLS)

    assert isinstance(resp1, LLMResponse), (
        f"chat() 应返回 LLMResponse，实际是 {type(resp1).__module__}.{type(resp1).__name__}"
    )
    assert isinstance(resp1.tool_calls, list), "tool_calls 必须归一化成 list"
    assert all(isinstance(tc, ToolCall) for tc in resp1.tool_calls), (
        "tool_calls 里混进了非 ToolCall 对象 —— 抽象漏了"
    )

    assert resp1.wants_tools is True, (
        "模型没点单？检查 TOOLS 的 description 够不够具体，或换个更明确的提问"
    )

    print(f"  模型点了 {len(resp1.tool_calls)} 单：")
    for tc in resp1.tool_calls:
        assert isinstance(tc.arguments, str), (
            f"arguments 应是原始 str，实际 {type(tc.arguments).__name__} —— "
            "Provider 越界替上层做了解析"
        )
        assert tc.id, "tool_call_id 不能为空，回灌时要靠它配对"
        print(f"    · {tc.name}({tc.arguments})")
    print(f"  usage: prompt={resp1.prompt_tokens} completion={resp1.completion_tokens}")

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

    for tc in resp1.tool_calls:
        if tc.name not in FAKE_IMPL:
            raise ValueError(f"模型点了一个不存在的工具：{tc.name}")
        args = json.loads(tc.arguments)
        result = FAKE_IMPL[tc.name](**args)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )

    print("\n═══ 第 2 轮 ═══")
    resp2 = provider.chat(messages=messages, tools=TOOLS)

    assert resp2.wants_tools is False, (
        f"模型还想点单（{len(resp2.tool_calls)} 单），循环该继续而不是结束"
    )
    assert resp2.content, "循环终止了，但 content 是空的 —— 没有答案可交付"

    assert isinstance(resp2.tool_calls, list), (
        f"tool_calls 应是 list（可以为空），实际 {type(resp2.tool_calls).__name__}"
    )
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
