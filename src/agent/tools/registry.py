"""工具注册表 —— 把 Python 函数翻译成模型看得懂的 JSON Schema。"""

import inspect
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel

SideEffect = Literal["read", "write", "external"]
"""工具的副作用等级：既决定并发与否，也决定重试策略。"""


class ToolSpec:
    """一个工具的完整说明书：函数本体 + 名字 + 描述 + 参数 schema。"""

    def __init__(
        self,
        fn: Callable[..., Any],
        args_model: type[BaseModel],
        *,
        side_effect: SideEffect = "read",
        timeout: float = 15.0,
        untrusted: bool = False,
    ) -> None:
        self.fn = fn
        self.name = fn.__name__
        self.description = inspect.cleandoc(fn.__doc__ or "").strip()
        self.args_model = args_model
        self.side_effect = side_effect
        self.timeout = timeout
        self.untrusted = untrusted
        """为 True 时，loop 会把执行结果包进不可信内容隔离区。"""

        if not self.description:
            raise ValueError(
                f"工具 {self.name} 没有 docstring。"
                "描述是模型决定要不要调它的唯一依据 —— 请写清楚「做什么、"
                "什么时候用、返回什么」。"
            )

    @property
    def parameters(self) -> dict[str, Any]:
        """由 Pydantic 模型【自动生成】JSON Schema，绝不手写。"""
        return self.args_model.model_json_schema()


TOOLS: dict[str, ToolSpec] = {}
"""全局注册表。M3 的循环、M8 的白名单都从这里取。"""


def tool(
    args_model: type[BaseModel],
    *,
    side_effect: SideEffect = "read",
    timeout: float = 15.0,
    untrusted: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把一个函数登记成工具。"""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        TOOLS[fn.__name__] = ToolSpec(
            fn,
            args_model,
            side_effect=side_effect,
            timeout=timeout,
            untrusted=untrusted,
        )
        return fn

    return deco


def openai_tools(names: list[str]) -> list[dict[str, Any]]:
    """把注册表里的工具，翻译成 chat() 的 tools 参数。"""
    return [
        {
            "type": "function",
            "function": {
                "name": TOOLS[n].name,
                "description": TOOLS[n].description,
                "parameters": TOOLS[n].parameters,
            },
        }
        for n in names
    ]
