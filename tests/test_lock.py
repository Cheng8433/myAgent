"""单实例锁（M9）—— 第二个持有者必须被拒绝，且锁不靠"文件存在"判断。"""

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
        pass
    text = lock.read_text(encoding="utf-8")
    assert "pid=" in text
    assert "at=" in text
    assert lock.exists()


def test_busy_message_tells_the_next_step(tmp_path):
    lock = tmp_path / "app.lock"
    with exclusive_lock(lock):
        with pytest.raises(LockBusy) as info:
            with exclusive_lock(lock):
                pass
    assert "已有另一个实例在运行" in str(info.value)
    assert "不重试" in str(info.value)
