"""单实例锁 —— M9 的第一件事（PLAN §M9 必备三件事之一）。"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

LOCK_FILE = "app.lock"
"""锁文件在 VAR_DIR 下的文件名（生产路径 var/app.lock，PLAN §M9 同名）。"""


class LockBusy(RuntimeError):
    """已有另一个实例持有锁 —— 本次直接退出，不是错误、也不该重试。"""


def _try_lock(handle: BinaryIO) -> None:
    """非阻塞地拿独占锁；拿不到抛 OSError（由调用方翻译成 LockBusy）。"""
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    """释放锁。★ 解锁前 seek(0)：锁的是哪个字节，就要从哪个字节解。"""
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_lock(path: str | Path) -> Iterator[Path]:
    """在 `path` 上拿独占锁；持有期间 yield 该路径，退出（含异常）时释放。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw_fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(raw_fd, "r+b", buffering=0)
    acquired = False
    try:
        try:
            _try_lock(handle)
            acquired = True
        except OSError as exc:
            raise LockBusy(
                f"已有另一个实例在运行（锁 {target}）。"
                "上一次还没结束；本次直接退出，不重试、不叠加。"
            ) from exc

        handle.seek(0)
        handle.truncate()
        meta = f"pid={os.getpid()} at={datetime.now(UTC).isoformat()}\n"
        handle.write(meta.encode("utf-8"))
        yield target
    finally:
        if acquired:
            try:
                _unlock(handle)
            except OSError:
                pass
        handle.close()
