"""心跳 —— 「它今天到底跑没跑」（M9 必备三件事之二在【本机】的落点）。"""

from __future__ import annotations

import json
import os
from pathlib import Path

from agent.memory.store import var_dir

HEARTBEAT_FILE = "heartbeat.json"


def heartbeat_path(root: str | Path | None = None) -> Path:
    """心跳文件的位置：VAR_DIR/heartbeat.json（和锁、日志、报告同根）。"""
    return var_dir(root) / HEARTBEAT_FILE


def write_heartbeat(payload: dict, *, root: str | Path | None = None) -> str:
    """原子地写心跳，返回它相对 VAR_DIR 的 posix 路径。"""
    target = heartbeat_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    base = var_dir(root)
    try:
        return target.relative_to(base).as_posix()
    except ValueError:  # pragma: no cover —— heartbeat_path 永远在 base 下
        return target.as_posix()


def read_heartbeat(root: str | Path | None = None) -> dict | None:
    """读心跳；没有文件返回 None。文件损坏时抛 ValueError（含原文长度）。"""
    path = heartbeat_path(root)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"心跳文件损坏：{path}（{len(text)} 字符，{exc}）") from exc
    if not isinstance(data, dict):
        raise ValueError(f"心跳文件形状不对：{path} 顶层应为对象，收到 {type(data).__name__}")
    return data
