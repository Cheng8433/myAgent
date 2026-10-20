"""CLI 的对外契约（M9）—— 退出码与两个新子命令的只读边界。

★ 退出码是给 cron / 任务计划程序读的唯一稳定信号，所以这里钉三件事：
  · 配置错误 → 2（不是未捕获异常的 1）：两种病该去的地方不同；
  · 已有实例 → 4（不是失败 1）：重试只会再撞一次锁；
  · status 只读：跑一次不抓取、不写心跳、不改库（除了幂等建表）。
"""

from __future__ import annotations

import app.main as cli
from agent.config import ConfigError
from app.lock import exclusive_lock


def test_config_error_becomes_exit_code_2(monkeypatch):
    def boom() -> int:
        raise ConfigError("缺少必填环境变量 DEMO")

    monkeypatch.setattr(cli, "cmd_report", boom)
    assert cli.main(["report"]) == cli.EXIT_CONFIG


def test_second_report_gets_busy_exit_code(monkeypatch, tmp_path):
    """锁被占着时 cmd_report 直接返回 4，且【没有】进到真正的 report。"""
    monkeypatch.setenv("VAR_DIR", str(tmp_path))
    called: list[int] = []

    def should_not_run() -> int:
        called.append(1)
        return 0

    monkeypatch.setattr(cli, "_report_locked", should_not_run)
    with exclusive_lock(tmp_path / "app.lock"):
        assert cli.cmd_report() == cli.EXIT_BUSY
    assert called == []


def test_hours_since_handles_bad_input():
    assert cli._hours_since(None) is None
    assert cli._hours_since("not-a-timestamp") is None


def test_status_is_read_only_and_survives_missing_login(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("VAR_DIR", str(tmp_path))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("JOB_SOURCE", "ncss")

    assert cli.main(["status"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "今日额度（东八区）：未使用" in out
    assert "心跳：还没有记录" in out
    assert "登录态：还没有" in out
    # ★ 只读：没有心跳文件被创建。
    assert not (tmp_path / "heartbeat.json").exists()
