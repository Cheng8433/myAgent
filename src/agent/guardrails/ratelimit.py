"""抓取礼貌（PLAN §M8 第 7 条）—— 实名账号的硬下限，不是可调策略。"""

from __future__ import annotations

import random
from collections.abc import Callable

POLITE_SOURCES = frozenset({"ncss"})
"""需要按真人标准限速的数据源。新增源时必须在这里显式登记。"""

MIN_PAGE_DELAY_SECONDS = 3.0
"""PLAN 的硬下限：一次运行内翻页间隔 ≥3s。"""

JITTER_RANGE = (0.5, 2.0)
"""PLAN：`random.uniform(0.5, 2)`。最终间隔 = 3.0 + [0.5, 2.0]。"""


def page_delay(*, rand: Callable[..., float] | None = None) -> float:
    """两次翻页之间的等待秒数：硬下限 + 抖动。"""
    draw = rand if rand is not None else random.uniform
    return MIN_PAGE_DELAY_SECONDS + draw(*JITTER_RANGE)
