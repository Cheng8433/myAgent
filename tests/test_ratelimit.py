"""限速与礼貌（M8 §7）—— 翻页节流 + 一天一次。

★ 两条规则各治一种病，所以分开断言：
  · 翻页节流管"一次 run【内部】"的请求间隔（治轰炸）；
  · 一天一次管"跨 run"的重复抓取（治同一个实名账号一天被请求多轮）。
  只做其中一条，另一条路上仍然可以打出同样的请求量。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _fakes import FakeLLM, wide_budget

from agent.guardrails.ratelimit import (
    JITTER_RANGE,
    MIN_PAGE_DELAY_SECONDS,
    POLITE_SOURCES,
    page_delay,
)
from agent.memory.store import record_run
from agent.tools.sources.base import Job, JobPage
from workflows import daily_report as dr
from workflows.daily_report import STATUS_ALREADY_RAN, STATUS_SUCCESS, run_daily_report
from workflows.report import CN_TZ, day_start_utc


class _Source:
    """只提供一个 .name 的假源；真正的抓取由 patch 掉的 fetch_page 承担。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def list_jobs(self, *, page: int = 1, limit: int = 20):  # pragma: no cover —— 不该被调到
        raise AssertionError("测试里 fetch_page 已被替换，不该走到数据源")


def _job(sid: str) -> Job:
    return Job(
        source="ncss",
        source_id=sid,
        title=f"岗位 {sid}",
        company="某公司",
        url=f"https://example.test/{sid}",
    )


def _fetch_script(script: dict[int, list[Job]]):
    calls: list[int] = []

    def fetch(page: int) -> JobPage:
        calls.append(page)
        return JobPage(jobs=script.get(page, []), raw=f'{{"page": {page}}}'.encode())

    return fetch, calls


def _seed_run(conn, status: str) -> None:
    record_run(
        conn,
        run_id=f"seed-{status}",
        status=status,
        steps=[],
        prompt_tokens=0,
        completion_tokens=0,
        started_at=datetime.now(UTC).isoformat(),
        error="种子" if status == "failed" else None,
    )


# ── ① 等待时长：硬下限 + 抖动 ────────────────────────────────────────────


def test_page_delay_is_floor_plus_jitter():
    low = page_delay(rand=lambda a, b: a)
    high = page_delay(rand=lambda a, b: b)
    assert low == MIN_PAGE_DELAY_SECONDS + JITTER_RANGE[0]
    assert high == MIN_PAGE_DELAY_SECONDS + JITTER_RANGE[1]


def test_page_delay_passes_the_plan_jitter_range():
    seen: list[tuple[float, float]] = []

    def rand(a: float, b: float) -> float:
        seen.append((a, b))
        return 0.0

    page_delay(rand=rand)
    assert seen == [(0.5, 2.0)]


# ── ② 翻页节流：只对 ncss、只从第 2 页起 ─────────────────────────────────


def test_polite_source_sleeps_between_pages(monkeypatch):
    fetch, calls = _fetch_script({1: [_job("1")], 2: [_job("2")], 3: []})
    monkeypatch.setattr(dr, "fetch_page", fetch)
    sleeps: list[float] = []

    pages = dr.fetch_all_pages(source=_Source("ncss"), sleep=sleeps.append)

    assert calls == [1, 2, 3]
    assert len(pages) == 2
    # ★ 次数 = 页数 - 1：第 1 页之前没有"上一个请求"要拉开距离。
    assert len(sleeps) == 2
    assert all(
        MIN_PAGE_DELAY_SECONDS < s <= MIN_PAGE_DELAY_SECONDS + JITTER_RANGE[1] for s in sleeps
    )


def test_dev_source_does_not_sleep(monkeypatch):
    fetch, calls = _fetch_script({1: [_job("1")], 2: []})
    monkeypatch.setattr(dr, "fetch_page", fetch)
    sleeps: list[float] = []

    dr.fetch_all_pages(source=_Source("remoteok"), sleep=sleeps.append)

    assert calls == [1, 2]
    assert sleeps == []  # remoteok 是替身：不为它拖慢每一次离线回放


def test_no_source_means_no_sleep(monkeypatch):
    """测试 / 离线脚本路径（m4b_verify 就是这条）：不传源就不睡。"""
    fetch, _ = _fetch_script({1: [_job("1")], 2: []})
    monkeypatch.setattr(dr, "fetch_page", fetch)
    sleeps: list[float] = []

    dr.fetch_all_pages(sleep=sleeps.append)

    assert sleeps == []


def test_polite_sources_is_explicit():
    """一个安全决定，不是默认值 —— 新增源时必须显式登记。"""
    assert POLITE_SOURCES == frozenset({"ncss"})


# ── ③ 一天一次：判据与例外 ───────────────────────────────────────────────


def test_second_run_same_day_is_skipped(monkeypatch, conn, tmp_path):
    fetch, calls = _fetch_script({1: [_job("1")], 2: []})
    monkeypatch.setattr(dr, "fetch_page", fetch)
    monkeypatch.setattr(dr.time, "sleep", lambda _s: None)

    first = run_daily_report(
        conn, FakeLLM(), run_id="first", budget=wide_budget(), root=tmp_path, source=_Source("ncss")
    )
    assert first.status == STATUS_SUCCESS
    assert calls == [1, 2]

    second = run_daily_report(
        conn,
        FakeLLM(),
        run_id="second",
        budget=wide_budget(),
        root=tmp_path,
        source=_Source("ncss"),
    )
    assert second.status == STATUS_ALREADY_RAN
    # ★ 关键：第二次一次请求都没发 —— 不是"抓完才发现重复"。
    assert calls == [1, 2]

    # 跳过不自我延续：第三次仍然会被【第一条 success】拦住，
    # 而不是被上一条 already_ran 拦住（两种情况下行为一样，但原因不能错）。
    third = run_daily_report(
        conn,
        FakeLLM(),
        run_id="third",
        budget=wide_budget(),
        root=tmp_path,
        source=_Source("ncss"),
    )
    assert third.status == STATUS_ALREADY_RAN
    assert calls == [1, 2]
    statuses = [r["status"] for r in conn.execute("select status from runs order by run_id")]
    assert statuses == [STATUS_SUCCESS, "already_ran", "already_ran"]


def test_login_expired_does_not_consume_the_daily_quota(monkeypatch, conn, tmp_path):
    """★ 登录过期一次请求都没发出去，而且主人续期后需要【立刻重跑】。

    把它算进额度，等于"今天登过期了，明天才能出报告"—— 惩罚的正是
    最需要重试的那一次。
    """
    _seed_run(conn, "login_expired")
    fetch, calls = _fetch_script({1: [_job("1")], 2: []})
    monkeypatch.setattr(dr, "fetch_page", fetch)
    monkeypatch.setattr(dr.time, "sleep", lambda _s: None)

    rep = run_daily_report(
        conn,
        FakeLLM(),
        run_id="after-login",
        budget=wide_budget(),
        root=tmp_path,
        source=_Source("ncss"),
    )

    assert rep.status == STATUS_SUCCESS
    assert calls == [1, 2]


def test_failed_run_counts_as_already_ran(monkeypatch, conn, tmp_path):
    """失败可能发生在抓取之后（入库 / 摘要），站点已经被请求过了。"""
    _seed_run(conn, "failed")
    fetch, calls = _fetch_script({1: [_job("1")], 2: []})
    monkeypatch.setattr(dr, "fetch_page", fetch)

    rep = run_daily_report(
        conn,
        FakeLLM(),
        run_id="after-fail",
        budget=wide_budget(),
        root=tmp_path,
        source=_Source("ncss"),
    )

    assert rep.status == STATUS_ALREADY_RAN
    assert calls == []


def test_dev_source_ignores_the_daily_quota(monkeypatch, conn, tmp_path):
    """remoteok 是替身：L2 回放要能在同一天连跑两次（幂等那条断言靠它）。"""
    _seed_run(conn, STATUS_SUCCESS)
    fetch, calls = _fetch_script({1: [_job("1")], 2: []})
    monkeypatch.setattr(dr, "fetch_page", fetch)

    rep = run_daily_report(
        conn,
        FakeLLM(),
        run_id="remoteok-again",
        budget=wide_budget(),
        root=tmp_path,
        source=_Source("remoteok"),
    )

    assert rep.status == STATUS_SUCCESS
    assert calls == [1, 2]


# ── ④ "一天"的边界按东八区算 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("local", "expected"),
    [
        (datetime(2026, 10, 20, 0, 30, tzinfo=CN_TZ), "2026-10-19T16:00:00+00:00"),
        (datetime(2026, 10, 20, 23, 30, tzinfo=CN_TZ), "2026-10-19T16:00:00+00:00"),
        (datetime(2026, 10, 19, 16, 0, tzinfo=UTC), "2026-10-19T16:00:00+00:00"),
    ],
)
def test_day_start_uses_china_midnight(local, expected):
    """UTC 零点是错的：早上 08:00 之前跑的那一次会被算进前一天。"""
    assert day_start_utc(now=local) == expected
