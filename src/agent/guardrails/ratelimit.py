"""抓取礼貌（PLAN §M8 第 7 条）—— 实名账号的硬下限，不是可调策略。

★ 为什么这些数【硬编码】、不做成环境变量：
  PLAN 原文就是"频率上限硬编码在代码里，不靠配置"。可配的护栏不是护栏 ——
  一个能被改成 0.1 秒的间隔，和没有这条规则只差一次手滑。
  `.env.example` 里原先那对 `HTTP_MIN_DELAY_SECONDS` / `HTTP_MAX_DELAY_SECONDS`
  【从未被任何代码读过】，M8 把它们删了：声明了但没人消费的配置项，
  比缺一个配置项更糟 —— 它让人以为"改了就会生效"。
  （同 HARD_STEP_CEILING / MAX_PAGES / "一天只跑一次"的纪律。）

★ 为什么是 `POLITE_SOURCES` 白名单而不是"给所有源都睡"：
  remoteok 是开发期替身，零登录、零账号风险；为它加 3 秒等待只会拖慢
  L2 回放与所有验收台。ncss 是主人的**实名账号**，标准必须更严。
  "对谁礼貌"是一个需要显式回答的问题，不该靠"默认全睡"含糊过去。

★ 抖动（jitter）的作用不是"更随机所以更安全"，而是**避免固定节奏**：
  每次恰好 3.0 秒的请求间隔，本身就是一个人工指纹。
"""

from __future__ import annotations

import random
from collections.abc import Callable

POLITE_SOURCES = frozenset({"ncss"})
"""需要按真人标准限速的数据源。新增源时在这里显式登记 —— 漏登记是
【不睡】，不是报错，所以这个集合的注释要写清楚它是一道安全决定。"""

MIN_PAGE_DELAY_SECONDS = 3.0
"""PLAN 的硬下限：一次运行内翻页间隔 ≥3s。"""

JITTER_RANGE = (0.5, 2.0)
"""PLAN：`random.uniform(0.5, 2)`。最终间隔 = 3.0 + [0.5, 2.0]。"""


def page_delay(*, rand: Callable[..., float] | None = None) -> float:
    """两次翻页之间的等待秒数：硬下限 + 抖动。

    ★ `rand=None` 在【调用时】解析成 `random.uniform`，不在 def 时绑定：
      这是 retry.py 里 `sleep=None` 那条纪律的同一个坑 —— 默认值写成
      `rand=random.uniform` 的话，将来想换随机源（测试、可复现实验）
      就得去改调用方，而不是传一个参数。
    """
    draw = rand if rand is not None else random.uniform
    return MIN_PAGE_DELAY_SECONDS + draw(*JITTER_RANGE)
