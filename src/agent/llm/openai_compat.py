"""OpenAI 兼容协议的 Provider 实现。"""

import os
from typing import Any

from openai import APIConnectionError, APITimeoutError, OpenAI, OpenAIError

from agent.config import ConfigError, required_float, required_str
from agent.llm.base import (
    LLMError,
    LLMPermanentError,
    LLMRateLimited,
    LLMResponse,
    LLMTransientError,
    ToolCall,
)

_EXTRA_RETRYABLE_STATUS = frozenset({408, 409, 429})


def _retry_after_seconds(exc: Exception) -> float | None:
    """从 Retry-After 响应头里抠出等待秒数。"""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _normalize(exc: Exception) -> LLMError:
    """把 openai SDK 的异常，翻译成本项目的错误词汇表。"""
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return LLMTransientError(f"{type(exc).__name__}: {exc}")

    status = getattr(exc, "status_code", None)
    if status is not None:
        if status == 429:
            return LLMRateLimited(str(exc), retry_after=_retry_after_seconds(exc))
        if status >= 500 or status in _EXTRA_RETRYABLE_STATUS:
            return LLMTransientError(f"HTTP {status}: {exc}")
        return LLMPermanentError(f"HTTP {status}: {exc}")

    return LLMPermanentError(f"{type(exc).__name__}: {exc}")


class OpenAICompatProvider:
    """把 OpenAI 兼容接口的响应，翻译成本项目自己的 LLMResponse。"""

    def __init__(self) -> None:
        api_key = os.environ.get("LLM_API_KEY", "")
        if not api_key:
            raise ConfigError("LLM_API_KEY 未设置或为空。请 cp .env.example .env 并填好。")

        self.model = required_str("LLM_MODEL")

        # 关掉 SDK 自带重试，统一由外层 retry_call 管。
        self._client = OpenAI(
            base_url=required_str("LLM_BASE_URL"),
            api_key=api_key,
            timeout=required_float("LLM_TIMEOUT_SECONDS"),
            max_retries=0,
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """发一次请求，把 SDK 的响应对象翻译成我们的 LLMResponse。"""
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools

        try:
            resp = self._client.chat.completions.create(**kwargs)
        except OpenAIError as exc:
            raise _normalize(exc) from exc

        if not resp.choices:
            raise LLMPermanentError(
                "响应里没有 choices —— 服务端返回了空结果（不是超时，重试无用）"
            )
        if resp.usage is None:
            raise LLMPermanentError(
                "响应里没有 usage —— 无法归因 token 成本（M6 依赖它）。"
                "若该供应商确实不回填用量，应在此显式记录，而不是填 0 蒙混过去。"
            )

        choice = resp.choices[0]
        msg = choice.message
        finish = choice.finish_reason

        content = msg.content
        tool_calls = [
            ToolCall(
                id=tc.id,
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
