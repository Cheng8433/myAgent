"""心跳 —— 「它今天到底跑没跑」（M9 必备三件事之二在【本机】的落点）。

★ PLAN §M9 要求"每天成功/失败都推一条极简消息（成功也推，因为「没消息」
  和「挂了」无法区分）"。外部推送（飞书）按主人决定暂缓 —— 所以本轮只做
  本机心跳文件 + `python -m app.main status`：主人打开电脑时能看到
  "最后一次运行是什么时候、什么结局"。
  ⚠️ 这是一笔【明确欠账】，不是"告警已经做了"：人不在电脑前时，
  本机心跳无法把消息送出去。PLAN 对账里照实写。

★ 为什么心跳不直接查 runs 表就够了：
  查表回答"库里最后一次 run 是什么"；心跳回答"这个进程最后一次【走到出口】
  是什么时候"。两者在异常退出时会分家：进程在抓取中途被 kill，runs 表里
  没有这次 run 的任何记录（finish 没被调到），心跳也停在【上一次】——
  于是"心跳时间很旧"本身就是线索：它表示这期间可能有过一次没留下痕迹的失败。
  数据库是主账本，心跳是给"最后一眼"用的钟面。

★ 心跳文件是【可以被删除的缓存级产物】：删了不会让任何逻辑出错，
  status 只说"还没有心跳记录"。所以写失败不该让日报失败 ——
  报告已经落盘，心跳只是给它配一块钟面。
"""

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
    """原子地写心跳，返回它相对 VAR_DIR 的 posix 路径。

    ★ 原子写（同 auth.save_state）：先写同目录临时文件再 os.replace。
      直接覆盖写的话，读到一半的 status 可能拿到半截 JSON —— 而心跳恰恰
      是"出问题时才会被翻出来看"的那份文件，它自己不能也变成问题。
    ★ 临时文件名带 pid：两个进程（理论上被别的东西绕过锁）同时写时，
      各自有各自的临时文件，不会互相覆盖一半。
    """
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
        # os.replace 成功后 tmp 已不存在；失败时清掉，别留垃圾。
        tmp.unlink(missing_ok=True)
    base = var_dir(root)
    try:
        return target.relative_to(base).as_posix()
    except ValueError:  # pragma: no cover —— heartbeat_path 永远在 base 下
        return target.as_posix()


def read_heartbeat(root: str | Path | None = None) -> dict | None:
    """读心跳；没有文件返回 None。文件损坏时抛 ValueError（含原文长度）。

    ★ 损坏不吞成 None：None 的意思是"从没跑过"，而损坏是"跑过，但记录坏了"
      —— 两种状态指向完全不同的下一步（重跑 vs 查磁盘/权限），
      混成一种就是归因错位（同 config.py 缺失/空串/格式错三分）。
    """
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
