"""配置读取 —— 必填环境变量的【唯一】读法。"""

from __future__ import annotations

import os
from typing import TypeVar

T = TypeVar("T")


class ConfigError(ValueError):
    """配置读不到 / 读不懂 —— 程序不该带着它继续跑。"""


def _read_required(name: str) -> str:
    """读一个必填变量。缺失与空串分开报 —— 两种病，别混成一句。"""
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
    """读必填整数。解析失败时消息里带变量名，方便定位。"""
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
