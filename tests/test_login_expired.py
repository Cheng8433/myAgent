"""登录过期这条路 —— M2b Phase A 接线后最该被钉住的一组断言。

★ 为什么单独一个文件、而不是散进 test_loop / test_report：
  这条路的每一环都【只能靠类型分支】走通（`LoginExpired` 是 `SourceError`
  的子类，顺序写反了就静默降级）。把它单独成网，是为了让"接 ncss 之后"
  这条链的三层（源 → 工具 → 编排 → CLI 退出码）有【一个地方】可以整体看。
"""

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


# ── ① CLI：status → 退出码 ────────────────────────────────────────────────


def test_outcome_maps_login_expired_to_exit_3(capsys):
    """★ 这条以前会当场 `raise ValueError`（主流程不认识这个 status）——
    退出码 3 是 PLAN §M2 定的【对外契约】，cron 靠它分辨"要人续期"和"程序坏了"。"""
    code = _report_outcome(DailyReport(run_id="r1", status=STATUS_LOGIN_EXPIRED, detail="302"))
    out = capsys.readouterr().out
    assert code == EXIT_LOGIN_EXPIRED == 3
    assert "python -m app.main login" in out  # 消息里必须有【下一步命令】


# ── ② 编排层：假源抛 LoginExpired → runs 里记 login_expired ───────────────


def test_run_daily_report_records_login_expired(monkeypatch, conn, tmp_path):
    """★ 这条同时锁住两件事：

    ① 【except 的顺序】—— `LoginExpired` 必须排在 `except SourceError` 之前。
       它是子类，挪到后面这一支就永远进不来，而结果不是报错、是静默变成
       `failed`（归因错位）。这条断言就是那个顺序的钉子。
    ② 【runs.error 也要带上它】—— 看板的 `recent_errors` 是按
       `error IS NOT NULL` 筛的。漏了它，"登录态过期"这条**唯一能自愈**的记录
       就永远不出现在看板上，而那正是最该被一眼看见的一条。
    """
    monkeypatch.setattr(jobs_mod, "get_source", lambda: _ExpiredSource())

    rep = run_daily_report(conn, FakeLLM(), run_id="r1", budget=wide_budget(), root=tmp_path)

    assert rep.status == STATUS_LOGIN_EXPIRED
    assert "302" in rep.detail
    row = conn.execute("SELECT status, error FROM runs WHERE run_id = ?", ("r1",)).fetchone()
    assert row["status"] == STATUS_LOGIN_EXPIRED
    assert row["error"] and "302" in row["error"]


def test_run_daily_report_still_reports_plain_source_error_as_failed(monkeypatch, conn, tmp_path):
    """★ 新分支不许把父类一并吞掉：普通故障仍然是 failed。
    两条路的【处置不同】—— failed 要人去看日志，login_expired 只要跑一条命令。"""
    monkeypatch.setattr(jobs_mod, "get_source", lambda: _BrokenSource())

    rep = run_daily_report(conn, FakeLLM(), run_id="r2", budget=wide_budget(), root=tmp_path)

    assert rep.status == STATUS_FAILED


def test_outcome_maps_failed_to_exit_1(monkeypatch, capsys):
    """既有映射没被新分支改坏。"""
    code = _report_outcome(DailyReport(run_id="r3", status=STATUS_FAILED, detail="坏"))
    assert code == EXIT_FAILED == 1


# ── ③ 工具层：模型读到的是"去跑 login"，不是一句 source_error ─────────────


def test_fetch_job_list_translates_login_expired(monkeypatch):
    """★ 这一条是 M2 那条纪律的落点：工具【翻译成返回值】，
    因为返回值是模型唯一能读到的信息 —— 它得读到一个能照做的下一步。"""
    monkeypatch.setattr(jobs_mod, "get_source", lambda: _ExpiredSource())

    out = fetch_job_list()

    assert out["ok"] is False
    assert out["error"] == "login_expired"
    assert "python -m app.main login" in out["hint"]


# ── ④ cmd_login 的 URL 判据（纯函数，不用开浏览器就能验） ─────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.ncss.cn/student/index.html", True),  # 登录后回落到主页
        ("https://www.ncss.cn/student/jobs/index.html", True),
        ("https://www.ncss.cn/student/login.jsp", False),  # 还在登录页 = 没登成
        ("https://account.chsi.com.cn/passport/login?service=x", False),  # 停在 CAS
        ("https://www.ncss.cn/student/login.jsp?from=index", False),
        ("about:blank", False),  # 浏览器刚起来那一刻
        ("https://evil-ncss.cn/student/index.html", False),  # 只有 host 精确相等才算
    ],
)
def test_logged_in_judge(url, expected):
    """★ 判据只认 host 与 path，不认 query：CAS 回跳会带一堆 ticket 参数，
    按整串匹配等于把 ticket 也写进契约。"""
    assert _logged_in(url) is expected
