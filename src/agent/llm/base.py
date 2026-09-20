"""LLM Provider 抽象层 —— 与厂商无关的协议定义。

存在的理由（PLAN.md §M1）：不同厂商的 tool calling 协议形状不同
（OpenAI/DeepSeek/通义 是 `tool_calls`，Anthropic 是 `tool_use` 内容块），
而且细节差异更多（并行工具调用、tool_choice 支持度、arguments 的类型）。

上层代码（runtime / workflows）只依赖这里定义的协议，
换供应商时只改 .env 里的 LLM_BASE_URL，一行业务代码都不用动。

⚠️ 纪律：本模块【禁止】import openai / httpx 或任何厂商 SDK。
   一旦 import 了，这层抽象就是假的 —— 因为它开始依赖某个具体厂商。
"""

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
    """★ 保持【原始字符串】，刻意不在这里 json.loads。

    理由：解析失败是一种【可回灌给模型的错误】。
    如果在这里解析，异常会逃逸出 Provider 层，上层就失去了
    「告诉模型"你的参数不合法，请重试"」的机会（M3 的 4c 分支）。
    """


@dataclass(frozen=True)
class LLMResponse:
    """一次 LLM 调用的结果，已归一化为厂商无关的形状。"""

    content: str | None = None
    """模型的文字输出。

    ⚠️ 陷阱（M1 实测）：content 非空【不代表】模型回答了问题。
    它可能只是一句开场白（"I'll check the weather..."）。
    判断"该不该结束"请用 wants_tools，不要看 content。
    """

    tool_calls: list[ToolCall] = field(default_factory=list)
    """模型这一轮点的所有单。可能为空（= 它答完了），也可能是并行好几单。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    """真实用量。M6 的成本归因完全依赖这两个数 —— 不要偷懒不填。"""

    finish_reason: str = ""
    """原始终止原因：stop / tool_calls / length / content_filter。留作排查用。"""

    @property
    def wants_tools(self) -> bool:
        """★ 这是 M3 循环的终止判据。

        有 tool_calls → 模型还要行动，循环继续；
        没有          → 模型答完了，循环结束。

        把它做成属性而不是让调用方自己写 `if resp.tool_calls:`，
        是为了让"唯一正确的判据"只有一个出口 —— 少一个写错的地方。
        """
        return bool(self.tool_calls)


class LLMProvider(Protocol):
    """所有 LLM 供应商实现都要满足的协议。

    刻意用 Protocol 而不是 ABC：实现类无需继承，只要形状对就行
    （鸭子类型）——这样测试时塞一个假 Provider 极其方便。
    """

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """发一次请求，拿一次结果。

        契约（三条，都很重要）：
        1. **不重试**（超时除外）。重试属于编排层，M4 统一管 ——
            否则重试会散落在各层，无法统一退避策略与幂等判断。
        2. **不吞异常**。失败就抛，让上层决定是重试、降级还是告警。
        3. **必须回填真实 token 用量**。M6 成本归因依赖它。

        注意：`timeout` 不在这里做参数，由实现类从配置读取
        （见 .env 的 LLM_TIMEOUT_SECONDS）。
        """
        ...
