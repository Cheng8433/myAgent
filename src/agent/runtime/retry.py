"""退避重试 —— 编排层加固的第一块（M4a）。"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from agent.llm.base import LLMTransientError
from agent.tools.sources import TransientSourceError

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """一个操作的重试策略。"""

    attempts: int = 3
    """总次数（【含首次】）。attempts=1 表示"不重试"，不是"重试 1 次"。"""

    base_delay: float = 1.0
    """首次退避的基数（秒）。之后按 2 的幂增长。"""

    cap: float = 30.0
    """单次退避的上限（秒）—— 防止 attempts 多时退避长到离谱。"""

    jitter: bool = True
    """是否加抖动。默认 True；关掉它的正当理由只有"调试时想看确定的序列"。"""

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError(f"attempts 至少为 1（attempts=1 表示不重试），收到 {self.attempts}")


DEFAULT_POLICY = RetryPolicy()
"""默认策略的【单例】。"""


DEFAULT_RETRYABLE: tuple[type[BaseException], ...] = (
    LLMTransientError,
    TransientSourceError,
)
"""默认"值得重试"的异常。"""


def _backoff_base(attempt: int, policy: RetryPolicy) -> float:
    """第 attempt 次失败后的【基准】延迟（不含抖动）：base·2^(attempt-1)，夹 cap。"""
    return min(policy.cap, policy.base_delay * 2 ** (attempt - 1))


def _jitter(value: float, policy: RetryPolicy, rand: Callable[[], float]) -> float:
    """把基准值抖成最终值。★ 全项目【只此一处】做抖动。"""
    return rand() * value if policy.jitter else value


def retry_call(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    retry_on: tuple[type[BaseException], ...] = DEFAULT_RETRYABLE,
    sleep: Callable[[float], None] | None = None,
    rand: Callable[[], float] | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """反复调用 fn，直到成功、撞上不该重试的错、或次数用尽。"""
    sleeper = sleep if sleep is not None else time.sleep
    rng = rand if rand is not None else random.random

    last_exc: BaseException | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            last_exc = exc
            if attempt == policy.attempts:
                break
            retry_after = getattr(exc, "retry_after", None)
            base = retry_after if retry_after is not None else _backoff_base(attempt, policy)
            delay = _jitter(min(base, policy.cap), policy, rng)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleeper(delay)

    assert last_exc is not None
    raise last_exc


POLICY_BY_SIDE_EFFECT: dict[str, RetryPolicy] = {
    "read": RetryPolicy(attempts=3, base_delay=2.0, cap=10.0),
    "write": RetryPolicy(attempts=3, base_delay=2.0, cap=10.0),
    "external": RetryPolicy(attempts=1),
}


def policy_for(side_effect: str) -> RetryPolicy:
    """按副作用等级取策略；未知等级保守处理 —— 不重试。"""
    return POLICY_BY_SIDE_EFFECT.get(side_effect, RetryPolicy(attempts=1))
