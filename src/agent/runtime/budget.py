"""预算闸门 —— 编排层加固的第二块（M4a-4）。"""

from __future__ import annotations

from dataclasses import dataclass

from dotenv import load_dotenv

from agent.config import required_float, required_int

GATE_MAX_STEPS = "max_steps"
GATE_MAX_TOKENS = "max_tokens"
GATE_WALL_CLOCK = "wall_clock"
"""闸门名。loop.py 拿它拼 status，所以用常量而不是裸字符串。"""


@dataclass(frozen=True)
class Budget:
    """一次 run 的三道上限。它只描述【限制】，不记录【用量】—— 见模块注释。"""

    max_steps: int
    max_tokens: int
    max_wall_seconds: float

    @classmethod
    def from_env(cls) -> Budget:
        """从 .env 读三道闸。变量名与 .env.example 一致。"""
        load_dotenv()
        return Budget(
            max_steps=required_int("AGENT_MAX_STEPS"),
            max_tokens=required_int("AGENT_MAX_TOKENS"),
            max_wall_seconds=required_float("AGENT_WALL_CLOCK_SECONDS"),
        )

    def exceeded(self, *, steps: int, tokens: int, wall_seconds: float) -> str | None:
        """三道闸依次判，返回【第一个】撞破的闸门名；都没撞破返回 None。"""
        if steps >= self.max_steps:
            return GATE_MAX_STEPS
        if tokens >= self.max_tokens:
            return GATE_MAX_TOKENS
        if wall_seconds >= self.max_wall_seconds:
            return GATE_WALL_CLOCK
        return None
