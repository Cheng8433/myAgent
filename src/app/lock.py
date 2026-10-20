"""单实例锁 —— M9 的第一件事（PLAN §M9 必备三件事之一）。

★ 为什么必须锁：定时任务和人手跑会撞车，而撞车的后果不是"多花点时间"——
  两个进程同时翻页 = 对同一个实名账号的请求量翻倍，同时写同一个 SQLite
  文件 = 后一个撞锁失败（或者更糟：两个 run 都往 jobs 里写同一批岗位）。
  「一天一次」管的是【跨天】的频率，管不住同一天里两条并发的 run。

★ 为什么用 OS 级文件锁，而不是"锁文件存在就当有人跑"：
  后者的经典故障是【僵尸锁】——进程被 kill（任务计划程序超时、断电、
  任务管理器结束进程），锁文件留在磁盘上，此后每一天的定时任务都以为
  "上一次还没跑完"，静静地退出，而没有任何东西坏掉、也没有任何东西跑。
  这个故障最恶毒的地方是它【看起来完全正常】。
  OS 锁（Windows 的 _locking / POSIX 的 flock）的所有权属于进程：
  进程活着锁就在，进程死了锁由内核释放 —— 不存在"过期判断"这件事。
  锁文件本身留着没关系，它只是锁的载体，不是锁本身。

★ 为什么不自己写 PID + 时间戳做过期判断：
  那需要回答"这个 PID 现在还活着吗"——Windows 上没有安全的 os.kill(pid, 0)
  （Python 会把 signal 0 变成 TerminateProcess），得用 ctypes 问内核；
  而 PID 还会被系统复用。用现成的 OS 锁，这一整类问题都不存在。

★ 锁的范围是【整个 report 进程】：从建库到写心跳。读写库、抓取、摘要、
  落盘都在里面。login 不加锁 —— 它不碰库、不抓岗位，而且主人可能一边
  等登录一边手跑别的；把它也锁上只会制造"为什么我不能登录"的困惑。

★ msvcrt 的字节偏移是按【文件位置】算的：加锁和解锁前都要 seek(0)。
  不 seek 的话，解锁会从写元数据后的位置开始，抛 OSError 而不是静默 ——
  但那会让一次正常的 report 在收尾时炸掉，属于"锁本身制造故障"。
"""

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

        # LK_NBLCK：非阻塞独占锁第 1 个字节；已被占用时抛 OSError(errno=13)。
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
    """在 `path` 上拿独占锁；持有期间 yield 该路径，退出（含异常）时释放。

    ★ 拿不到锁时抛 LockBusy。消息里【不】附锁文件内容 ——
      实测（Windows）：字节锁会把整段读取也挡掉，持有期间别人
      `read_text()` 会拿 PermissionError。于是 pid 记录只在两个时候有用：
      ① 释放之后（事后想知道上一次是谁），② 不是"读不到就猜"。
      判据永远是 OS 锁本身，文件内容只是事后线索。
    ★ 正常退出、抛异常退出、被 KeyboardInterrupt —— finally 都会解锁。
      进程被 kill 时 finally 不执行，但那正是 OS 锁要兜的场景：内核回收。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # r+b 要求文件已存在，所以先 O_CREAT 打开再包成文件对象。
    # buffering=0：元数据写进去立刻可见，不给"看到半截"留窗口。
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

        # 写排障线索（不是锁的判据）。先截断，免得旧 PID 接着新 PID。
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
                # 解锁失败不该盖住真正要抛的异常（同 logs.shutdown_logging 的纪律）。
                pass
        handle.close()
