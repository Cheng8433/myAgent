"""LLM Provider 抽象层 —— 与厂商无关的协议定义。"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    """模型点的一单。"""

    id: str
    """回灌 role="tool" 消息时，必须原样带回的配对凭证。"""

    name: str
    """工具名。模型可能幻觉出不存在的名字 —— 上层要能识别并容错。"""

    arguments: str
    """保持【原始字符串】，刻意不在这里 json.loads。"""


@dataclass(frozen=True)
class LLMResponse:
    """一次 LLM 调用的结果，已归一化为厂商无关的形状。"""

    content: str | None = None
    """模型的文字输出。"""

    tool_calls: list[ToolCall] = field(default_factory=list)
    """模型这一轮点的所有单。可能为空（= 它答完了），也可能是并行好几单。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    """真实用量。M6 的成本归因完全依赖这两个数 —— 不要偷懒不填。"""

    finish_reason: str = ""
    """原始终止原因：stop / tool_calls / length / content_filter。留作排查用。"""

    @property
    def wants_tools(self) -> bool:
        """这是 M3 循环的终止判据。"""
        return bool(self.tool_calls)


class LLMError(Exception):
    """LLM 层一切失败的基类 —— 与厂商无关的错误词汇表。"""


class LLMTransientError(LLMError):
    """瞬时故障：超时、连接中断、5xx。重试【有意义】。"""


class LLMPermanentError(LLMError):
    """永久故障：401 鉴权失败、400 请求体不合法、模型名不存在。"""


class LLMRateLimited(LLMTransientError):
    """429 限流：瞬时，但【必须听服务端的】，不能用自己算的退避表。"""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        """服务端要求的等待秒数；None = 对方没说，回退到自己的退避表。"""


class LLMProvider(Protocol):
    """所有 LLM 供应商实现都要满足的协议。"""

    model: str
    """这个 Provider 打的是哪个模型（如 "deepseek-chat"）。"""

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """发一次请求，拿一次结果。"""
        ...
