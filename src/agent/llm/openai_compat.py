"""OpenAI 兼容协议的 Provider 实现。

DeepSeek / 通义 / 智谱 / Moonshot / 本地 vLLM·Ollama 都能用它，
因为它们都照着 OpenAI 的形状做（M1 已确认）。

为什么用 openai SDK，而不像 scripts/m1_probe.py 那样裸打 httpx？
  · SDK 处理了鉴权头、超时、错误类型化、流式 —— 这些是管道活，不值得重写
  · 但请记住：它内部就是您在 M1 亲眼看过的那一次 POST，没有魔法
  · 所以 SDK 换掉也不影响上层：上层只认 agent.llm.base 里的协议

 ⚠️ 本文件是抽象层里【唯一允许】import openai 的地方。
"""

import os
from typing import Any

from openai import OpenAI

from agent.llm.base import LLMResponse, ToolCall


class OpenAICompatProvider:
    """把 OpenAI 兼容接口的响应，翻译成本项目自己的 LLMResponse。"""

    def __init__(self) -> None:
        api_key = os.environ.get("LLM_API_KEY", "")
        if not api_key:
            raise RuntimeError("LLM_API_KEY 未设置。请 cp .env.example .env 并填好。")

        self._model = os.environ.get("LLM_MODEL", "deepseek-chat")

        self._client = OpenAI(
            base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1"),
            api_key=api_key,
            timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "60")),
            # ★ 关掉 SDK 自带的自动重试。
            #   理由：重试属于编排层（M4 统一管退避、幂等、预算）。
            #   如果让 SDK 偷偷重试，M4 的重试策略会被叠加放大，
            #   而且 trace 里看不到真实的失败次数。
            max_retries=0,
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """发一次请求，把 SDK 的响应对象翻译成我们的 LLMResponse。"""
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            **({"tools": tools} if tools else {}),
        )

        choice = resp.choices[0]
        msg = choice.message
        finish = choice.finish_reason

        content = msg.content
        tool_calls = [
            ToolCall(
                id=tc.id,  # 用上面的映射表
                name=tc.function.name,
                arguments=tc.function.arguments,
            )
            for tc in (msg.tool_calls or [])
        ]

        usage = resp.usage
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            finish_reason=finish,
        )
