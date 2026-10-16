"""OpenAI 兼容协议的 Provider 实现。

DeepSeek / 通义 / 智谱 / Moonshot / 本地 vLLM·Ollama 都能用它，
因为它们都照着 OpenAI 的形状做（M1 已确认）。

为什么用 openai SDK，而不像 scripts/m1_probe.py 那样裸打 httpx？
  · SDK 处理了鉴权头、超时、错误类型化、流式 —— 这些是管道活，不值得重写
  · 但请记住：它内部就是您在 M1 亲眼看过的那一次 POST，没有魔法
  · 所以 SDK 换掉也不影响上层：上层只认 agent.llm.base 里的协议

 ⚠️ 本文件是抽象层里【唯一允许】import openai 的地方。
   既然只有这里认识 openai，那么"认 openai 的异常类型"也必须收在这里 ——
   见下面的 _normalize()。上层拿到的永远是 LLMError 的子类。
"""

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

# 除 5xx 外，还该重试的 HTTP 状态码。用【状态码】分类，不枚举 SDK 类名 ——
# 理由：状态码是中立词汇（RFC 9110），SDK 类名是厂商词汇。
# 枚举类名的话，openai SDK 哪天重命名或加个子类，我们的策略会静悄悄地漏。
#   408 请求超时 / 409 冲突 / 429 限流
_EXTRA_RETRYABLE_STATUS = frozenset({408, 409, 429})


def _retry_after_seconds(exc: Exception) -> float | None:
    """从 Retry-After 响应头里抠出等待秒数。

    这个头有【两种】合法写法：
      · 秒数： "3"
      · HTTP 日期： "Wed, 21 Oct 2015 07:28:00 GMT"
    这里只处理第一种；日期形式返回 None，让上层回退到自己算的退避表。
    理由：为一个可选优化引入日期解析不划算 —— 回退是安全的，
    而多一段可能出错的解析代码是净负担。
    """
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
    """把 openai SDK 的异常，翻译成本项目的错误词汇表。

    ★ 这个函数是 M4 补上的那个洞：M1 归一化了【返回值】，漏了【异常】。
      没有它，runtime 层想判断"能不能重试"就得 import openai 认类名，
      抽象层在异常路径上就破了。

    分类顺序有意如此：先看有没有状态码（能拿到响应），再看没有的
    （连接根本没建立）。两者的重试含义不同 —— 前者能靠状态码精确判断，
    后者一律是瞬时故障。
    """
    # ① 连接层故障：客户端压根没拿到 HTTP 响应，一定是瞬时的。
    #   （APITimeoutError 是 APIConnectionError 的子类，这里一并覆盖。）
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return LLMTransientError(f"{type(exc).__name__}: {exc}")

    status = getattr(exc, "status_code", None)
    if status is not None:
        if status == 429:
            # 限流要单独成类：它带着"服务端要求等多久"这个额外信息，
            # 上层拿不到它就只能瞎猜退避时长。
            return LLMRateLimited(str(exc), retry_after=_retry_after_seconds(exc))
        if status >= 500 or status in _EXTRA_RETRYABLE_STATUS:
            return LLMTransientError(f"HTTP {status}: {exc}")
        # 其余 4xx：请求本身有问题（鉴权、参数、模型名），重发还是错。
        return LLMPermanentError(f"HTTP {status}: {exc}")

    # ② 没有状态码的其余情况（响应解析失败、SDK 内部错……）
    #   归为永久。理由：看不懂的错，重试大概率还是看不懂 ——
    #   与其烧钱重试，不如把它当作"需要人看"的信号抛出去。
    return LLMPermanentError(f"{type(exc).__name__}: {exc}")


class OpenAICompatProvider:
    """把 OpenAI 兼容接口的响应，翻译成本项目自己的 LLMResponse。

    ★ 四个 LLM_* 变量【都是必填】（2026-10-07 起）：KEY / MODEL / BASE_URL / TIMEOUT。
      代码里不留默认值 —— 尤其 base_url 与 model 必须成对出现：
      只改了 base_url 却忘了改 model，就会把 deepseek-chat 发给通义，
      报一个看不懂的错。让它缺了当场报，比发出一个注定失败的请求好。
      读法与报错见 agent.config（缺失 / 空串 / 解析失败分开报）。
    """

    def __init__(self) -> None:
        api_key = os.environ.get("LLM_API_KEY", "")
        if not api_key:
            # ★ 这一个不套 agent.config：.env.example 里 `LLM_API_KEY=` 【故意为空】
            #   （模板占位，等着你填）。所以它的"空"是意料之中的，
            #   消息该给出【下一步做什么】，而不是 config 那句"配错了"。
            raise ConfigError("LLM_API_KEY 未设置或为空。请 cp .env.example .env 并填好。")

        # ★ 公开属性而不是 self._model（M6 起）：它是 LLMProvider 协议的一部分
        #   （trace 要拿它归因成本），私有名会让协议那条声明变成一句空话。
        self.model = required_str("LLM_MODEL")

        self._client = OpenAI(
            base_url=required_str("LLM_BASE_URL"),
            api_key=api_key,
            timeout=required_float("LLM_TIMEOUT_SECONDS"),
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
        # ★ 为什么用显式 kwargs dict，而不写
        #     **({"tools": tools} if tools else {})
        #   三个理由，从硬到软：
        #   ① 类型检查器会被它绊倒（且报得莫名其妙）：`**` 解包意味着
        #      "值可能落到任何一个关键字参数上"，于是 Pylance 拿
        #      `list[dict[str, Any]]` 去核对 create() 的每个参数，
        #      最后指着 `audio` 说类型不对 —— 跟音频毫无关系。
        #      写成 `dict[str, Any]` 之后值是 Any，任何参数都收得下，
        #      不用 # type: ignore 就能干净（加 ignore 会让这一行
        #      从此对所有错误失明，包括将来真的那些）。
        #   ② 语意更准：tools 为空时【压根不传这个键】，而不是传 None。
        #      有些供应商把 `tools: null` 和"没这个字段"当两回事。
        #   ③ 一行里塞三个结构（条件表达式 + dict 字面量 + 解包）不好读。
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools

        try:
            resp = self._client.chat.completions.create(**kwargs)
        except OpenAIError as exc:
            # ★ 归一化后再抛，且用 `from exc` 保住原始调用链 ——
            #   上层只认 LLMError，但调试时 traceback 里仍能看到
            #   "这是由哪个 openai 异常引起的"。
            #   `raise ... from ...` 与裸 `raise` 的区别就在这里：
            #   前者是"换个类型重新抛出"，后者是"原样上抛"。
            raise _normalize(exc) from exc

        # ── 解析响应 ──
        # ★ 这两道检查是为了守住上面那条契约：「chat() 抛出的必须是 LLMError
        #   子类」。没有它们，下面那两行会漏出裸的 IndexError / AttributeError，
        #   一路穿到 runtime，被 loop.py 的 `except Exception` 兜成 tool_crashed
        #   回灌给模型 —— 于是一个【传输层】的"响应格式不对"，
        #   被报成了【工具层】的 bug，归因全错。
        #
        #   SDK 自己的类型标注就写着 usage 是 Optional[CompletionUsage]，
        #   也就是"我可能会给你 None"—— 这不是防御性编程，是照着契约接。
        if not resp.choices:
            raise LLMPermanentError(
                "响应里没有 choices —— 服务端返回了空结果（不是超时，重试无用）"
            )
        if resp.usage is None:
            # 刻意【不】默认成 0：M6 的成本归因全靠这两个数，
            # 静默填 0 会让账目悄悄少算，而少算的账本比没有账本更危险。
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
