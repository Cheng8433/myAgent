"""评估包（M7）。

★ 为什么这个文件【必须】存在（哪怕只是一行）：让 `python -m eval.run_eval`
  的模块解析【确定】。没有 `__init__.py` 时 `eval` 是一个 PEP 420
  命名空间包 —— 它可以被 site-packages 里任何一个同名的包影子掉，于是
  "跑起来的其实是别人的代码"，而这是一种极难查的失败（不报错，只是行为不对）。

★ 顺手在这里放一个 L2 / L3 共用的结果类型：两层产出同一形状的断言结果，
  run_eval 才能统一计数。放这里而不是让两层各定一份 —— 又一处"同一个事实
  两个出处"。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Check:
    """一条断言的结果。`ok=False` 时才值得写 note（说清期望什么、拿到什么）。"""

    label: str
    ok: bool
    note: str = ""
