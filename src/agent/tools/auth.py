"""登录态基础设施 —— 凭证（Playwright storage_state）的落盘与读取。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from agent.memory.store import var_dir

STATE_REL = "auth/ncss_state.json"
"""凭证在 var/ 下的相对路径。"""

LOGIN_HINT = "python -m app.main login"
"""续期命令的唯一出处：消息、提示、文档都引用它，不各写一遍。"""


class AuthMissingError(RuntimeError):
    """本机还没有可用的登录态（文件不在 / 内容坏了 / 形状不对）。"""


def state_path(*, root: str | Path | None = None) -> Path:
    """凭证文件的绝对路径：`<VAR_DIR>/auth/ncss_state.json`。"""
    return var_dir(root) / STATE_REL


def _relative_to_var(target: Path, *, root: str | Path | None) -> str:
    """转成【相对 var/ 的正斜杠】路径，同 `archive_raw` / `write_report` 的约定。"""
    return target.relative_to(var_dir(root)).as_posix()


def save_state(state: dict[str, Any], *, root: str | Path | None = None) -> str:
    """把 storage_state 原子地落盘，返回它【相对 var/】的路径。"""
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise ValueError(
            "storage_state 形状不对：期望 {'cookies': [...], ...}，"
            f"实际拿到 {type(state).__name__}。它应当来自 Playwright 的 context.storage_state()。"
        )

    target = state_path(root=root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(state, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()

    return _relative_to_var(target, root=root)


def load_state(*, root: str | Path | None = None) -> dict[str, Any]:
    """读回 storage_state。文件不在 / 内容坏了，一律抛 `AuthMissingError`。"""
    target = state_path(root=root)
    if not target.exists():
        raise AuthMissingError(f"登录态不存在：{target}。请先运行 `{LOGIN_HINT}`。")
    try:
        with open(target, encoding="utf-8") as fp:
            state = json.load(fp)
    except json.JSONDecodeError as exc:
        raise AuthMissingError(
            f"登录态文件解析失败：{target}（{exc}）。文件可能没写完或不是 JSON。"
            f"请重新运行 `{LOGIN_HINT}` 覆盖它。"
        ) from exc
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise AuthMissingError(
            f"登录态文件形状不对：{target}（期望 {{'cookies': [...]}}）。"
            f"请重新运行 `{LOGIN_HINT}` 覆盖它。"
        )
    return state


def cookies_from_state(state: dict[str, Any], *, domains: tuple[str, ...]) -> dict[str, str]:
    """从 storage_state 里取出【指定域名】的 cookie，压成 `name -> value`。"""
    out: dict[str, str] = {}
    for cookie in state.get("cookies", []):
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "")
        if not any(token in domain for token in domains):
            continue
        name = cookie.get("name")
        if not name:
            continue
        out[str(name)] = str(cookie.get("value") or "")
    return out


def state_age_seconds(*, root: str | Path | None = None) -> float | None:
    """凭证文件距今多少秒；文件不存在返回 None。"""
    target = state_path(root=root)
    if not target.exists():
        return None
    return time.time() - target.stat().st_mtime
