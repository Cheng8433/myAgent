"""单实例锁（M9）—— 第二个持有者必须被拒绝，且锁不靠"文件存在"判断。

★ 为什么这些断言值得存在：
  锁的失效模式是【僵尸锁】—— 进程死了文件还在，以后每天都以为"还在跑"。
  所以本文件不只测"能锁上"，还测"释放后能再锁"与"异常路径也释放"；
  判据是 OS 锁，不是文件删除（文件留着是正常的）。

★ 同进程二次加锁在 Windows 上也会被拒（实测 msvcrt 返回 errno 13），
  所以这里不需要真起两个进程。
"""

from __future__ import annotations

import pytest

from app.lock import LockBusy, exclusive_lock


def test_second_holder_is_refused(tmp_path):
    lock = tmp_path / "app.lock"
    with exclusive_lock(lock):
        with pytest.raises(LockBusy):
            with exclusive_lock(lock):
                pass


def test_lock_is_released_after_normal_exit(tmp_path):
    lock = tmp_path / "app.lock"
    with exclusive_lock(lock):
        pass
    # 释放后必须能再拿 —— 否则第二天就永远跑不了。
    with exclusive_lock(lock):
        pass


def test_lock_is_released_when_body_raises(tmp_path):
    lock = tmp_path / "app.lock"
    with pytest.raises(RuntimeError):
        with exclusive_lock(lock):
            raise RuntimeError("模拟抓取中途崩掉")
    with exclusive_lock(lock):
        pass


def test_lock_file_records_holder_without_being_the_criterion(tmp_path):
    """锁文件里写 pid/时间只是排障线索；锁本身由 OS 持有。"""
    lock = tmp_path / "nested" / "app.lock"
    with exclusive_lock(lock):
        # ★ 持有期间【不读】：Windows 的字节锁会挡住整段读取（实测 PermissionError）。
        pass
    text = lock.read_text(encoding="utf-8")
    assert "pid=" in text
    assert "at=" in text
    # 文件在退出后仍存在 —— 它从来不是"有没有锁"的判据。
    assert lock.exists()


def test_busy_message_tells_the_next_step(tmp_path):
    lock = tmp_path / "app.lock"
    with exclusive_lock(lock):
        with pytest.raises(LockBusy) as info:
            with exclusive_lock(lock):
                pass
    assert "已有另一个实例在运行" in str(info.value)
    assert "不重试" in str(info.value)
