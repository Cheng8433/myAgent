"""工具注册表 —— 把 Python 函数翻译成模型看得懂的 JSON Schema。

为什么要有这一层（PLAN.md §M2）：
  tool calling 协议里的 "tools" 字段，本质是【写给模型的说明书】。
  手写那份 JSON Schema 是重复劳动，而且会和函数签名失同步 ——
  改了参数忘了改 schema，模型就会传来一个不存在的参数名。
  所以：以【函数签名 + Pydantic 模型】为唯一真相，schema 由它生成。

⚠️ 但 schema 只是【提示】，不是护栏。
   minimum / maximum / enum 这些约束，模型可能无视，服务端也常常不校验。
   真正的校验必须在函数体里做 —— 见 jobs.py 里那行 `page = max(1, page)`。

纪律：本模块【不 import 任何厂商 SDK】。
  openai_tools() 只是产出那个通用形状的 dict，谁都能用。
"""

import inspect
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel

SideEffect = Literal["read", "write", "external"]
"""工具的副作用等级。M3 用它决定并发与否，M8 用它做权限白名单：
read 可并发、可重试；write / external 要串行、要幂等判断。"""


class ToolSpec:
    """一个工具的完整说明书：函数本体 + 名字 + 描述 + 参数 schema。"""

    def __init__(
        self,
        fn: Callable[..., Any],
        args_model: type[BaseModel],
        *,
        side_effect: SideEffect = "read",
        timeout: float = 15.0,
    ) -> None:
        self.fn = fn
        self.name = fn.__name__
        # ★ 描述就来自 docstring，且【不是给人看的注释】—— 它是模型判断
        #   "该不该调这个工具"的唯一依据。cleandoc 去掉缩进，省掉无谓的 token。
        self.description = inspect.cleandoc(fn.__doc__ or "").strip()
        self.args_model = args_model
        self.side_effect = side_effect
        self.timeout = timeout

        # ★ 空描述的工具是【废工具】。在 import 期就炸掉，
        #   比等到线上才发现"模型从来不调它"要好得多。
        if not self.description:
            raise ValueError(
                f"工具 {self.name} 没有 docstring。"
                "描述是模型决定要不要调它的唯一依据 —— 请写清楚「做什么、"
                "什么时候用、返回什么」。"
            )

    @property
    def parameters(self) -> dict[str, Any]:
        """★ 由 Pydantic 模型【自动生成】JSON Schema，绝不手写。

        model_json_schema() 会顺带产出 "title" / "$defs" 这类 Pydantic 自己的
        字段。主流厂商（含 DeepSeek）都容忍多余字段；若哪家报错，只改这一处
        即可裁剪，不必动任何工具定义 —— 这就是把生成收在一处的好处。
        """
        return self.args_model.model_json_schema()


TOOLS: dict[str, ToolSpec] = {}
"""全局注册表。M3 的循环、M8 的白名单都从这里取。

⚠️ 它是靠 `@tool` 装饰器在 import 时填充的。所以想让它有内容，
   必须先 import 定义工具的那个模块（见 scripts/m2_verify.py 顶部）。
"""


def tool(
    args_model: type[BaseModel],
    *,
    side_effect: SideEffect = "read",
    timeout: float = 15.0,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """把一个函数登记成工具。

    刻意用显式关键字参数，而不是 PLAN.md 初稿里的 `**kw` ——
    这样 IDE 能补全、传错参数名会立刻报错，而不是悄悄塞进 ToolSpec。
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        TOOLS[fn.__name__] = ToolSpec(fn, args_model, side_effect=side_effect, timeout=timeout)
        return fn  # ★ 返回原函数：它依然可以被直接调用（写测试时很有用）

    return deco


def openai_tools(names: list[str]) -> list[dict[str, Any]]:
    """把注册表里的工具，翻译成 chat() 的 tools 参数。

    ★ 这正是 M1 抽象层派上用场的地方：这里产出的是【协议形状】的 dict，
      不依赖 openai SDK —— 它只是恰好跟 OpenAI 的形状一致（事实标准）。
      哪天要接 Anthropic 的 tool_use，只需在这里多一个分支。
    """
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
