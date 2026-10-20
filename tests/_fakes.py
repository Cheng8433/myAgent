"""测试替身 —— 从 `scripts/m4b_verify.py` 【拷】过来的，不是 import。

★ 为什么是拷贝（有意重复）而不是 `from scripts.m4b_verify import FakeLLM`：
  ① `scripts/` 不是包、也不在 sys.path 上，import 它得先动 sys.path；
  ② 更要命的是那类脚本【没有】 `if __name__ == "__main__"` 守卫
     （2026-10-09 实测：m4_verify.py 全文没有这行）—— import 它 = 当场把
     整个探针跑一遍并 SystemExit。它是"一跑到底"的脚本，不是可复用的模块。
  两个消费者面向不同的问题：verify 脚本要"够真"（带叙述、一次性），
  pytest 要"够快够细"（无副作用、按用例挑）。所以各自留一份，
  改其中一份时记得另一份是【故意】不一样的。

★ 判据：这里只放【跨多个测试文件】用得到的东西。只给一个文件用的，
  留在那个文件里 —— 别让它变成"什么都往里塞"的杂物间。
"""

from __future__ import annotations

from agent.llm.base import LLMResponse, ToolCall
from agent.runtime.budget import Budget
from agent.tools.sources.base import Job


def job(
    sid: str,
    *,
    title: str | None = None,
    company: str = "某公司",
    url: str | None = None,
) -> Job:
    """造一条远程岗位。刻意带默认值 —— 测试想调的往往只有那一个字段。"""
    return Job(
        source="remoteok",
        source_id=sid,
        title=title if title is not None else f"岗位 {sid}",
        company=company,
        url=url if url is not None else f"https://example.test/{sid}",
        location="上海",
    )


class FakeLLM:
    """假 Provider —— 三种脾气：正常答一句 / 永远点单 / 直接抛错。

    ★ 它同样要遵守 Provider 契约第 3 条：回填真实 token 用量。
      填 0 的话，"runs 的 token = 子 run 的累计"那条断言就永远成立得毫无意义。

    ★ M6 起协议多了 `model`（trace 要拿它做成本归因），所以这里也得报一个。
    """

    model = "fake-model"

    PROMPT_TOKENS = 100
    COMPLETION_TOKENS = 10

    def __init__(
        self,
        *,
        text: str = "今天这批岗位集中在后端与数据方向。",
        error: Exception | None = None,
        always_wants: bool = False,
        tool_name: str = "no_such_tool",
    ) -> None:
        self.text = text
        self.error = error
        self.always_wants = always_wants
        # ★ 默认仍点一个【不存在】的名字（理由见 chat 里那段）；
        #   M8 的白名单用例要验证"存在但未授权"那条路，所以留一个注入口。
        self.tool_name = tool_name
        self.calls: list[list[dict]] = []
        self.seen_tools: list[object] = []

    def chat(self, messages, tools=None):  # noqa: ANN001, ANN201 —— 假实现，形状对即可
        self.calls.append([dict(m) for m in messages])
        self.seen_tools.append(tools)
        if self.error is not None:
            raise self.error
        if self.always_wants:
            # ★ 刻意点一个【不存在】的工具名：loop 的 Act 分支是按全局 TOOLS
            #   注册表查名字的（不是按传进去的 tool_names），点真名会真的发网络请求。
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c1", name=self.tool_name, arguments="{}")],
                prompt_tokens=self.PROMPT_TOKENS,
                completion_tokens=self.COMPLETION_TOKENS,
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content=self.text,
            tool_calls=[],
            prompt_tokens=self.PROMPT_TOKENS,
            completion_tokens=self.COMPLETION_TOKENS,
            finish_reason="stop",
        )


def wide_budget() -> Budget:
    """只留步数闸在远处，token / wall 放到够不到 —— 一次实验只动一个变量。"""
    return Budget(max_steps=20, max_tokens=10**9, max_wall_seconds=1e9)


def data_rows(doc: str) -> list[str]:
    """报告里的【岗位行】（去掉表头与分隔行）。用来数"每一条岗位都占了一行吗"。"""
    return [
        line
        for line in doc.splitlines()
        if line.startswith("| ") and not line.startswith("| # ") and not line.startswith("|---")
    ]


def without_timestamp(doc: str) -> str:
    """去掉 `> 生成于 …` 那一行 —— 两次 run 的生成时刻天然不同。"""
    return "\n".join(line for line in doc.splitlines() if not line.startswith("> 生成于"))
