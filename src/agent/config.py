"""配置读取 —— 必填环境变量的【唯一】读法。

★ 纪律：配置只从环境变量读，代码里【不留默认值】。
  同一个默认值写两处（代码 + .env.example）迟早分家，而分家【不报错】：
  模板会继续撒谎，你照着它配出来的是另一套上限。所以代码不写默认值，
  变量缺失就当场报错 —— 少算的账本比没有账本更危险。
  代价一并认下：.env 从"建议"升级成"必需"。

★ 为什么这段收在一个模块里、而不是各写一遍：
  budget.py 与 llm/openai_compat.py 都要读必填变量。两处各写一份校验，
  就是"同一个事实两个出处"——改一处忘一处，不报错。这正是本项目反复
  在还的那笔账（见 tools/jobs.py 的 _fetch_policy、runtime/retry.py 的 _jitter）。
  （PLAN §M8 的 config.py 就是这个位置；凭据那部分 M8 再往里收。）

★ 缺失、空串、解析失败【分开报】：它们是三种病，排错时要看的地方都不同。
  · 缺失   → "忘了配"      → 翻 .env.example
  · 空串   → "配成了空"    → 查那一行写错了什么
  · 格式错 → "写错了类型"  → 消息里带变量名与原始值
  混成一句"配置错误"，就等于在最需要信息的地方把信息扔掉
  （同 budget.exceeded() 返回闸门名而不是 bool）。
"""

from __future__ import annotations

import os
from typing import TypeVar

T = TypeVar("T")


class ConfigError(ValueError):
    """配置读不到 / 读不懂 —— 程序不该带着它继续跑。

    ★ 为什么继承 ValueError：这是白拿的分类能力。
      ① 上层想单独接走它时（PLAN §M9 的退出码 2 = 配置错误）能 `except ConfigError`；
      ② 而老的 `except ValueError`（如 scripts/m4_verify.py 第 ⑦ 节）
        照样接得住 —— 分类不该以打破既有调用方为代价。
    """


def _read_required(name: str) -> str:
    """读一个必填变量。缺失与空串分开报 —— 两种病，别混成一句。

    ★ 为什么区分 None 和 ""：`os.getenv("X")` 在 X 未设置时返回 None、
      在 `X=` 时返回 ""。前者是"忘了配"，后者是"配成了空"——
      要去的地方不同，所以消息必须分得开。
    """
    raw = os.getenv(name)
    if raw is None:
        raise ConfigError(f"缺少必填环境变量 {name}（配置模板见 .env.example）")
    if raw == "":
        raise ConfigError(f"环境变量 {name} 是空的 —— 空串不是「没配」，是「配错了」")
    return raw


def required_str(name: str) -> str:
    """读必填字符串。"""
    return _read_required(name)


def required_int(name: str) -> int:
    """读必填整数。解析失败的消息里带【变量名】—— int() 自带的
    "invalid literal for int() with base 10: 'abc'" 没说是谁错了。"""
    raw = _read_required(name)
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是整数，收到 {raw!r}") from exc


def required_float(name: str) -> float:
    """读必填浮点数。同 required_int。"""
    raw = _read_required(name)
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 必须是浮点数，收到 {raw!r}") from exc
