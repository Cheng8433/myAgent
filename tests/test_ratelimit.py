"""限速与礼貌（M8 §7）—— 翻页节流 + 一天一次。"""

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
from agent.tools.sources.base import Job, JobPage, SourceUnreachable
from workflows import daily_report as dr
from workflows.daily_report import (
    STATUS_ALREADY_RAN,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_UNREACHABLE,
    run_daily_report,
)
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

    def fetch(page: int, *, source=None) -> JobPage:
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


def test_polite_source_sleeps_between_pages(monkeypatch):
    fetch, calls = _fetch_script({1: [_job("1")], 2: [_job("2")], 3: []})
    monkeypatch.setattr(dr, "fetch_page", fetch)
    sleeps: list[float] = []

    pages = dr.fetch_all_pages(source=_Source("ncss"), sleep=sleeps.append)

    assert calls == [1, 2, 3]
    assert len(pages) == 2
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
    assert sleeps == []


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


def test_the_same_source_is_handed_to_every_page(monkeypatch):
    """整轮一条连接的前提：每一页拿到的都是【同一个源对象】。"""
    seen: list[object] = []
    fetch, _ = _fetch_script({1: [_job("1")], 2: []})

    def recording(page, *, source=None):
        seen.append(source)
        return fetch(page, source=source)

    monkeypatch.setattr(dr, "fetch_page", recording)
    src = _Source("ncss")

    dr.fetch_all_pages(source=src, sleep=lambda _s: None)

    assert seen == [src, src]


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
    assert calls == [1, 2]

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
    """登录过期一次请求都没发出去，而且主人续期后需要【立刻重跑】。"""
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


def _unreachable_on(page_to_fail: int):
    """第 page_to_fail 页抛 SourceUnreachable，其余页正常。"""
    calls: list[int] = []

    def fetch(page: int, *, source=None) -> JobPage:
        calls.append(page)
        if page == page_to_fail:
            raise SourceUnreachable("ncss 连不上，未发出 HTTP 请求：boom")
        return JobPage(jobs=[_job(str(page))], raw=b'{"ok": true}')

    return fetch, calls


def test_first_page_unreachable_does_not_consume_the_daily_quota(monkeypatch, conn, tmp_path):
    """第一页就连不上：一个 HTTP 请求都没发出去，站点没被查询过。"""
    fetch, calls = _unreachable_on(1)
    monkeypatch.setattr(dr, "fetch_page", fetch)

    first = run_daily_report(
        conn,
        FakeLLM(),
        run_id="unreachable",
        budget=wide_budget(),
        root=tmp_path,
        source=_Source("ncss"),
    )
    assert first.status == STATUS_UNREACHABLE
    assert calls == [1]

    fetch_ok, calls_ok = _fetch_script({1: [_job("1")], 2: []})
    monkeypatch.setattr(dr, "fetch_page", fetch_ok)
    monkeypatch.setattr(dr.time, "sleep", lambda _s: None)
    again = run_daily_report(
        conn,
        FakeLLM(),
        run_id="after-unreachable",
        budget=wide_budget(),
        root=tmp_path,
        source=_Source("ncss"),
    )
    assert again.status == STATUS_SUCCESS
    assert calls_ok == [1, 2]


def test_unreachable_after_a_successful_page_still_counts_as_failed(monkeypatch, conn, tmp_path):
    """抓过第 1 页之后才断网 —— 站点已经被请求过，这次仍记 failed、仍占额度。"""
    fetch, calls = _unreachable_on(2)
    monkeypatch.setattr(dr, "fetch_page", fetch)
    monkeypatch.setattr(dr.time, "sleep", lambda _s: None)

    first = run_daily_report(
        conn,
        FakeLLM(),
        run_id="unreachable-page2",
        budget=wide_budget(),
        root=tmp_path,
        source=_Source("ncss"),
    )
    assert first.status == STATUS_FAILED
    assert calls == [1, 2]
    assert "前 1 页已抓取" in first.detail
    assert "第 2 页不可达" in first.detail

    fetch_ok, calls_ok = _fetch_script({1: [_job("1")], 2: []})
    monkeypatch.setattr(dr, "fetch_page", fetch_ok)
    again = run_daily_report(
        conn,
        FakeLLM(),
        run_id="after-page2-fail",
        budget=wide_budget(),
        root=tmp_path,
        source=_Source("ncss"),
    )
    assert again.status == STATUS_ALREADY_RAN
    assert calls_ok == []


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


class _ClosableSource(_Source):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def _run(conn, root, *, source=None, run_id="owned"):
    return run_daily_report(
        conn,
        FakeLLM(),
        run_id=run_id,
        budget=wide_budget(),
        root=root,
        source=source,
    )


def test_workflow_closes_the_source_it_created(monkeypatch, conn, tmp_path):
    """整轮复用一条连接的前提是：编排层造的源，编排层关。"""
    src = _ClosableSource("ncss")
    monkeypatch.setattr(dr, "get_source", lambda: src)
    fetch, calls = _fetch_script({1: [_job("1")], 2: []})
    monkeypatch.setattr(dr, "fetch_page", fetch)
    monkeypatch.setattr(dr.time, "sleep", lambda _s: None)

    rep = _run(conn, tmp_path)

    assert rep.status == STATUS_SUCCESS
    assert calls == [1, 2]
    assert src.closed == 1


def test_workflow_does_not_close_an_injected_source(monkeypatch, conn, tmp_path):
    """注入的源由调用方关闭 —— 编排层只借，不拥有。"""
    src = _ClosableSource("ncss")
    fetch, calls = _fetch_script({1: [_job("1")], 2: []})
    monkeypatch.setattr(dr, "fetch_page", fetch)
    monkeypatch.setattr(dr.time, "sleep", lambda _s: None)

    rep = _run(conn, tmp_path, source=src, run_id="injected")

    assert rep.status == STATUS_SUCCESS
    assert calls == [1, 2]
    assert src.closed == 0


def test_workflow_closes_owned_source_even_when_skipping(monkeypatch, conn, tmp_path):
    """already_ran 是早退支 —— 早退也不能漏掉 close。"""
    _seed_run(conn, STATUS_SUCCESS)
    src = _ClosableSource("ncss")
    monkeypatch.setattr(dr, "get_source", lambda: src)

    rep = _run(conn, tmp_path, run_id="skip")

    assert rep.status == STATUS_ALREADY_RAN
    assert src.closed == 1
