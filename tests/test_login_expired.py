"""登录过期这条路 —— M2b Phase A 接线后最该被钉住的一组断言。"""

from __future__ import annotations

import pytest
from _fakes import FakeLLM, wide_budget

import agent.tools.jobs as jobs_mod
from agent.tools.jobs import fetch_job_list
from agent.tools.sources import LoginExpired, SourceError
from app.main import EXIT_FAILED, EXIT_LOGIN_EXPIRED, _logged_in, _report_outcome
from workflows.daily_report import (
    STATUS_FAILED,
    STATUS_LOGIN_EXPIRED,
    DailyReport,
    run_daily_report,
)


class _ExpiredSource:
    """只会说"登录过期了"的假数据源 —— 形状对得上 JobSource 协议。"""

    name = "ncss"

    def list_jobs(self, *, page: int = 1, limit: int = 20):
        raise LoginExpired("请求被 302 回 /student/login.jsp")


class _BrokenSource:
    """普通故障（非登录过期）—— 用来确认新分支【没有】把父类一并吞掉。"""

    name = "ncss"

    def list_jobs(self, *, page: int = 1, limit: int = 20):
        raise SourceError("HTTP 500：服务端异常")


def test_outcome_maps_login_expired_to_exit_3(capsys):
    """这条以前会当场 `raise ValueError`（主流程不认识这个 status）——"""
    code = _report_outcome(DailyReport(run_id="r1", status=STATUS_LOGIN_EXPIRED, detail="302"))
    out = capsys.readouterr().out
    assert code == EXIT_LOGIN_EXPIRED == 3
    assert "python -m app.main login" in out


def test_run_daily_report_records_login_expired(conn, tmp_path):
    """同时锁住 status 与 runs.error 两处留痕。"""
    rep = run_daily_report(
        conn,
        FakeLLM(),
        run_id="r1",
        budget=wide_budget(),
        root=tmp_path,
        source=_ExpiredSource(),
    )

    assert rep.status == STATUS_LOGIN_EXPIRED
    assert "302" in rep.detail
    row = conn.execute("SELECT status, error FROM runs WHERE run_id = ?", ("r1",)).fetchone()
    assert row["status"] == STATUS_LOGIN_EXPIRED
    assert row["error"] and "302" in row["error"]


def test_run_daily_report_still_reports_plain_source_error_as_failed(conn, tmp_path):
    """新分支不许把父类一并吞掉：普通故障仍然是 failed。"""
    rep = run_daily_report(
        conn,
        FakeLLM(),
        run_id="r2",
        budget=wide_budget(),
        root=tmp_path,
        source=_BrokenSource(),
    )

    assert rep.status == STATUS_FAILED


def test_outcome_maps_failed_to_exit_1(monkeypatch, capsys):
    """既有映射没被新分支改坏。"""
    code = _report_outcome(DailyReport(run_id="r3", status=STATUS_FAILED, detail="坏"))
    assert code == EXIT_FAILED == 1


def test_fetch_job_list_translates_login_expired(monkeypatch):
    """工具层把异常翻译成返回值，而不是让异常穿过模型边界。"""
    monkeypatch.setattr(jobs_mod, "get_source", lambda: _ExpiredSource())

    out = fetch_job_list()

    assert out["ok"] is False
    assert out["error"] == "login_expired"
    assert "python -m app.main login" in out["hint"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.ncss.cn/student/index.html", True),
        ("https://www.ncss.cn/student/jobs/index.html", True),
        ("https://www.ncss.cn/student/login.jsp", False),
        ("https://account.chsi.com.cn/passport/login?service=x", False),
        ("https://www.ncss.cn/student/login.jsp?from=index", False),
        ("about:blank", False),
        ("https://evil-ncss.cn/student/index.html", False),
    ],
)
def test_logged_in_judge(url, expected):
    """判据只认 host 与 path，不认 query：CAS 回跳会带一堆 ticket 参数。"""
    assert _logged_in(url) is expected
