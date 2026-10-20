"""评估包（M7）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Check:
    """一条断言的结果。`ok=False` 时才值得写 note（说清期望什么、拿到什么）。"""

    label: str
    ok: bool
    note: str = ""
