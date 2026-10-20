"""测试替身 —— 从 `scripts/m4b_verify.py` 【拷】过来的，不是 import。"""

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
    """假 Provider —— 三种脾气：正常答一句 / 永远点单 / 直接抛错。"""

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
        self.tool_name = tool_name
        self.calls: list[list[dict]] = []
        self.seen_tools: list[object] = []

    def chat(self, messages, tools=None):  # noqa: ANN001, ANN201 —— 假实现，形状对即可
        self.calls.append([dict(m) for m in messages])
        self.seen_tools.append(tools)
        if self.error is not None:
            raise self.error
        if self.always_wants:
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


class ScriptedLLM:
    """按剧本依次返回预置响应；剧本用完就报错，而不是默默重播。"""

    model = "scripted-model"

    def __init__(self, *responses: LLMResponse) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []
        self.seen_tools: list[object] = []

    def chat(self, messages, tools=None):  # noqa: ANN001, ANN201 —— 假实现，形状对即可
        self.calls.append([dict(m) for m in messages])
        self.seen_tools.append(tools)
        if not self._responses:
            raise AssertionError("ScriptedLLM 的剧本用完了 —— 模型多要了一轮")
        return self._responses.pop(0)


def wants_tool(name: str, arguments: str = "{}", *, call_id: str = "c1") -> LLMResponse:
    """一次「模型点名要调工具」的响应。"""
    return LLMResponse(
        content=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        prompt_tokens=100,
        completion_tokens=10,
        finish_reason="tool_calls",
    )


def says(text: str) -> LLMResponse:
    """一次「模型直接给出文字」的响应。"""
    return LLMResponse(
        content=text,
        tool_calls=[],
        prompt_tokens=100,
        completion_tokens=10,
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
