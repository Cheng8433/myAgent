"""ncss 数据源单测 —— 在【真 httpx 边界】上打 NcssSource（M2b Phase B）。"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

import agent.tools.jobs as jobs_mod
from agent.guardrails.ratelimit import JITTER_RANGE, MIN_PAGE_DELAY_SECONDS
from agent.tools.auth import save_state
from agent.tools.registry import TOOLS
from agent.tools.sources.base import (
    JobPage,
    LoginExpired,
    SourceError,
    SourceUnreachable,
    TransientSourceError,
)
from agent.tools.sources.ncss import (
    DEFAULT_TIMEOUT,
    KEEPALIVE_EXPIRY_SECONDS,
    LIST_API,
    NcssSource,
)

NCSS_FIXTURE = Path(__file__).resolve().parents[1] / "eval" / "fixtures" / "ncss_sample.json"


@pytest.fixture
def ncss_bytes() -> bytes:
    """真实响应体。【本地夹具】而不是放 conftest —— 只服务这一个文件"""
    return NCSS_FIXTURE.read_bytes()


@pytest.fixture
def ncss_payload(ncss_bytes: bytes) -> dict:
    return json.loads(ncss_bytes)


def _cookies() -> list[dict]:
    """三个 cookie：两个 ncss 域的、一个别人家的。"""
    return [
        {"name": "FAKE_SESSION", "value": "abc", "domain": ".ncss.cn", "path": "/"},
        {"name": "FAKE_SID", "value": "def", "domain": "www.ncss.cn", "path": "/"},
        {"name": "CHSI_TICKET", "value": "zzz", "domain": ".chsi.com.cn", "path": "/"},
    ]


def _login(tmp_path, *, cookies: list[dict] | None = None) -> None:
    """在 tmp_path 这个 var/ 里造一份登录态。"""
    save_state(
        {"cookies": cookies if cookies is not None else _cookies(), "origins": []}, root=tmp_path
    )


def _route(**kwargs) -> respx.Route:
    """接管那条 `GET .../jobslist/ajax/?…` 的请求（query 不参与匹配）。"""
    return respx.get(url__regex=re.escape(LIST_API)).mock(**kwargs)


def _json_body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def test_field_mapping(tmp_path, ncss_payload, ncss_bytes):
    """`jobId→source_id`、`jobName→title`、`recName→company`、`areaCodeName→location`。"""
    _login(tmp_path)
    with respx.mock:
        _route(return_value=httpx.Response(200, content=ncss_bytes))
        page = NcssSource(root=tmp_path).list_jobs(page=1, limit=10)

    first = ncss_payload["data"]["list"][0]
    got = page.jobs[0]
    assert got.source == "ncss"
    assert got.source_id == first["jobId"]
    assert got.title == first["jobName"]
    assert got.company == first["recName"]
    assert got.location == first["areaCodeName"]
    assert got.extras["lowMonthPay"] == first["lowMonthPay"]
    assert got.extras["degreeName"] == first["degreeName"]
    assert len(page.jobs) == len(ncss_payload["data"]["list"]) == 10


def test_url_is_built_because_the_response_has_none(tmp_path, ncss_payload, ncss_bytes):
    """源站的返回里【没有】url 字段 —— 得拿 `jobId` 拼出来。"""
    _login(tmp_path)
    with respx.mock:
        _route(return_value=httpx.Response(200, content=ncss_bytes))
        page = NcssSource(root=tmp_path).list_jobs(limit=10)

    first = ncss_payload["data"]["list"][0]
    assert "url" not in first
    assert page.jobs[0].url == (f"https://www.ncss.cn/student/jobs/{first['jobId']}/detail.html")


def test_published_at_is_iso8601_with_utc_offset(tmp_path, ncss_payload, ncss_bytes):
    """本文件第一要紧的断言。"""
    _login(tmp_path)
    with respx.mock:
        _route(return_value=httpx.Response(200, content=ncss_bytes))
        page = NcssSource(root=tmp_path).list_jobs(limit=10)

    ms = ncss_payload["data"]["list"][0]["publishDate"]
    got = page.jobs[0].published_at
    assert isinstance(ms, int)
    assert got is not None
    assert got.endswith("+00:00")
    parsed = datetime.fromisoformat(got)
    assert parsed.utcoffset() == timedelta(0)
    assert parsed == datetime.fromtimestamp(ms / 1000, tz=UTC)


def test_unparseable_publish_date_degrades_to_none(tmp_path, ncss_payload):
    """外部输入坏了不该让整份日报崩掉（PLAN 原则二）—— 退化成 None。"""
    _login(tmp_path)
    ncss_payload["data"]["list"][0]["publishDate"] = "不是个数字"
    with respx.mock:
        _route(return_value=httpx.Response(200, content=_json_body(ncss_payload)))
        page = NcssSource(root=tmp_path).list_jobs(limit=1)
    assert page.jobs[0].published_at is None


def test_rows_without_jobid_or_title_are_dropped(tmp_path, ncss_payload):
    """两道防线：没有 `jobId` 就拼不出详情页 url，没有标题进日报也读不成句。"""
    _login(tmp_path)
    ncss_payload["data"]["list"] = [
        {"jobId": "A", "jobName": "正常岗位"},
        {"jobId": "", "jobName": "空 id"},
        {"jobName": "连 jobId 字段都没有"},
        {"jobId": "B", "jobName": "   "},
    ]
    with respx.mock:
        _route(return_value=httpx.Response(200, content=_json_body(ncss_payload)))
        page = NcssSource(root=tmp_path).list_jobs()
    assert [j.source_id for j in page.jobs] == ["A"]


def test_page_number_becomes_the_offset_param(tmp_path, ncss_bytes):
    """钉住 `_offset_for_page` 的假设 —— 它【还没被实测确认】。"""
    _login(tmp_path)
    with respx.mock:
        route = _route(return_value=httpx.Response(200, content=ncss_bytes))
        NcssSource(root=tmp_path).list_jobs(page=2, limit=10)
    params = route.calls.last.request.url.params
    assert params["offset"] == "2"
    assert params["limit"] == "10"


def test_filters_go_out_verbatim_and_no_jquery_timestamp(tmp_path, ncss_bytes):
    """抓包里那 13 个筛选参数一个都不许少。"""
    _login(tmp_path)
    with respx.mock:
        route = _route(return_value=httpx.Response(200, content=ncss_bytes))
        NcssSource(root=tmp_path).list_jobs(page=1, limit=10)
    params = route.calls.last.request.url.params
    assert params["sourcesName"] == "0"
    assert params["jobName"] == ""
    assert params["recruitType"] == ""
    assert "_" not in params


def test_only_ncss_domain_cookies_are_sent(tmp_path, ncss_bytes):
    """storage_state 里装着【整个浏览器上下文】见过的所有 cookie（含 chsi 的）。"""
    _login(tmp_path)
    with respx.mock:
        route = _route(return_value=httpx.Response(200, content=ncss_bytes))
        NcssSource(root=tmp_path).list_jobs()
    sent = route.calls.last.request.headers.get("cookie", "")
    assert "FAKE_SESSION" in sent
    assert "FAKE_SID" in sent
    assert "CHSI_TICKET" not in sent


def test_redirect_to_ncss_login_page_is_login_expired(tmp_path):
    """302 → `/student/login.jsp`（PLAN §2.1 的实测信号）。"""
    _login(tmp_path)
    with respx.mock:
        _route(return_value=httpx.Response(302, headers={"Location": "/student/login.jsp"}))
        with pytest.raises(LoginExpired) as err:
            NcssSource(root=tmp_path).list_jobs()
    assert "login.jsp" in str(err.value)


def test_redirect_to_chsi_cas_is_also_login_expired(tmp_path):
    """失效时究竟是 302 到 ncss 自己的登录页、还是回 CAS 再登一次"""
    _login(tmp_path)
    cas = "https://account.chsi.com.cn/passport/login?service=https://www.ncss.cn/student/connect/chsi"
    with respx.mock:
        _route(return_value=httpx.Response(302, headers={"Location": cas}))
        with pytest.raises(LoginExpired):
            NcssSource(root=tmp_path).list_jobs()


def test_redirect_somewhere_else_is_a_plain_source_error(tmp_path):
    """不许把所有 302 都吃成登录过期：那会让人白跑一条 login。"""
    _login(tmp_path)
    with respx.mock:
        _route(
            return_value=httpx.Response(
                302, headers={"Location": "https://www.ncss.cn/student/index.html"}
            )
        )
        with pytest.raises(SourceError) as err:
            NcssSource(root=tmp_path).list_jobs()
    assert not isinstance(err.value, LoginExpired)
    assert "student/index.html" in str(err.value)


def test_missing_state_file_is_login_expired_not_a_traceback(tmp_path):
    """干净机器上第一次跑 `JOB_SOURCE=ncss` 就撞这一幕。"""
    with respx.mock(assert_all_called=False):
        route = _route(return_value=httpx.Response(200, content=b"{}"))
        with pytest.raises(LoginExpired) as err:
            NcssSource(root=tmp_path).list_jobs()
    assert "python -m app.main login" in str(err.value)
    assert len(route.calls) == 0


def test_state_without_any_ncss_cookie_is_login_expired(tmp_path):
    """文件在、但里面只有别人家的 cookie —— 发出去等于没带凭证（然后 302）。"""
    with respx.mock(assert_all_called=False):
        route = _route(return_value=httpx.Response(200, content=b"{}"))
        _login(tmp_path, cookies=[{"name": "X", "value": "1", "domain": ".chsi.com.cn"}])
        with pytest.raises(LoginExpired):
            NcssSource(root=tmp_path).list_jobs()
    assert len(route.calls) == 0


def test_redirect_is_not_followed(tmp_path):
    """本文件最关键的一行：`follow_redirects=False`。"""
    _login(tmp_path)
    with respx.mock:
        route = _route(return_value=httpx.Response(302, headers={"Location": "/student/login.jsp"}))
        with pytest.raises(LoginExpired):
            NcssSource(root=tmp_path).list_jobs()
        assert len(route.calls) == 1


def test_flag_false_is_an_error_not_an_empty_page(tmp_path, ncss_payload):
    """本文件第二要紧的断言。"""
    _login(tmp_path)
    ncss_payload["flag"] = False
    ncss_payload["errors"] = ["未登录"]
    ncss_payload["data"]["list"] = []
    with respx.mock:
        _route(return_value=httpx.Response(200, content=_json_body(ncss_payload)))
        with pytest.raises(SourceError) as err:
            NcssSource(root=tmp_path).list_jobs()
    assert "未登录" in str(err.value)


def test_empty_list_is_fine_because_that_is_the_end(tmp_path, ncss_payload):
    """和上一条成对：`list` 为空是合法的，那就是"翻到底了"。"""
    _login(tmp_path)
    ncss_payload["data"]["list"] = []
    with respx.mock:
        _route(return_value=httpx.Response(200, content=_json_body(ncss_payload)))
        page = NcssSource(root=tmp_path).list_jobs(page=99)
    assert page.jobs == []


def test_missing_data_list_is_an_error(tmp_path, ncss_payload):
    """形状不对（接口改版）必须报错 —— 不许 `.get()` 出一条空的假路。"""
    _login(tmp_path)
    del ncss_payload["data"]["list"]
    with respx.mock:
        _route(return_value=httpx.Response(200, content=_json_body(ncss_payload)))
        with pytest.raises(SourceError):
            NcssSource(root=tmp_path).list_jobs()


def test_html_200_is_a_source_error_with_the_body_in_the_message(tmp_path):
    """ncss 存在"软拦截"的可能：HTTP 200，但回来的是登录页的 HTML。"""
    _login(tmp_path)
    html = b"<!DOCTYPE html><html><body>please log in</body></html>"
    with respx.mock:
        _route(return_value=httpx.Response(200, content=html))
        with pytest.raises(SourceError) as err:
            NcssSource(root=tmp_path).list_jobs()
    assert not isinstance(err.value, TransientSourceError)
    assert "DOCTYPE" in str(err.value)


def test_raw_is_the_untouched_response_bytes(tmp_path, ncss_bytes):
    """raw 必须是【未解析】的响应体 —— 存档的全部意义就是留下完整原文。"""
    _login(tmp_path)
    with respx.mock:
        _route(return_value=httpx.Response(200, content=ncss_bytes))
        page = NcssSource(root=tmp_path).list_jobs()
    assert isinstance(page.raw, bytes)
    assert page.raw == ncss_bytes


def test_remote_pagination_takes_a_fresh_raw(tmp_path):
    """与 remoteok 相反：ncss 是远端分页，第 2 页是一次新请求、带一份新 raw。"""
    _login(tmp_path)
    bodies = [
        _json_body({"flag": True, "data": {"list": [], "pagenation": {"offset": n}}})
        for n in (1, 2)
    ]
    with respx.mock:
        route = _route(side_effect=[httpx.Response(200, content=b) for b in bodies])
        src = NcssSource(root=tmp_path)
        p1 = src.list_jobs(page=1)
        p2 = src.list_jobs(page=2)
        assert len(route.calls) == 2
    assert p1.raw == bodies[0]
    assert p2.raw == bodies[1]
    assert p1.raw != p2.raw


@pytest.mark.parametrize("code", [429, 408, 500, 502, 503])
def test_server_side_status_is_transient(tmp_path, code):
    """429/408/5xx → 瞬时（上层重试有意义）。"""
    _login(tmp_path)
    with respx.mock:
        _route(return_value=httpx.Response(code))
        with pytest.raises(TransientSourceError):
            NcssSource(root=tmp_path).list_jobs()


@pytest.mark.parametrize("code", [403, 404])
def test_client_side_status_is_permanent(tmp_path, code):
    """403/404 → 永久。★ 必须【不是】Transient 的子类判定（重试纯属浪费）。"""
    _login(tmp_path)
    with respx.mock:
        _route(return_value=httpx.Response(code))
        with pytest.raises(SourceError) as err:
            NcssSource(root=tmp_path).list_jobs()
    assert not isinstance(err.value, TransientSourceError)


def test_connect_error_is_unreachable_not_just_transient(tmp_path):
    """连不上、DNS 挂了、TLS 握手超时 —— 连 HTTP 请求都没发出去。"""
    _login(tmp_path)
    with respx.mock:
        _route(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(SourceUnreachable) as err:
            NcssSource(root=tmp_path).list_jobs()
    assert isinstance(err.value, TransientSourceError)
    assert err.value.pages_done == 0


def test_read_timeout_is_transient_but_not_unreachable(tmp_path):
    """读超时是请求【已经发出去】之后才超时 —— 站点被问过，额度要算。"""
    _login(tmp_path)
    with respx.mock:
        _route(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(TransientSourceError) as err:
            NcssSource(root=tmp_path).list_jobs()
    assert not isinstance(err.value, SourceUnreachable)


def test_http_timeout_sits_inside_the_tool_timeout():
    """30 < 35 是契约不是巧合：HTTP 层必须先于工具层超时。"""
    assert DEFAULT_TIMEOUT < TOOLS["fetch_job_list"].timeout


def test_one_source_reuses_one_client_across_pages(tmp_path, ncss_bytes):
    """连接复用的最小不变量：同一个源、多次 list_jobs → 同一个 Client。"""
    _login(tmp_path)
    src = NcssSource(root=tmp_path)
    with respx.mock:
        route = _route(return_value=httpx.Response(200, content=ncss_bytes))
        assert src._client is None
        src.list_jobs(page=1)
        first = src._client
        src.list_jobs(page=2)
        second = src._client
        assert len(route.calls) == 2
    assert first is not None
    assert second is first
    src.close()


def test_close_is_idempotent_and_allows_a_fresh_session(tmp_path, ncss_bytes):
    """`close()` 可重复调用；关掉之后再请求会重新建客户端。"""
    _login(tmp_path)
    src = NcssSource(root=tmp_path)
    with respx.mock:
        _route(return_value=httpx.Response(200, content=ncss_bytes))
        src.list_jobs()
        old = src._client
        src.close()
        assert src._client is None
        src.close()
        src.list_jobs()
        assert src._client is not None
        assert src._client is not old
    src.close()


def test_keepalive_outlives_the_longest_page_delay():
    """复用窗口必须【大于】翻页间隔的上限，否则复用静默退化成每页握手。"""
    assert KEEPALIVE_EXPIRY_SECONDS > MIN_PAGE_DELAY_SECONDS + JITTER_RANGE[1]


class _ClosableSource:
    name = "ncss"

    def __init__(self) -> None:
        self.closed = 0

    def list_jobs(self, *, page: int = 1, limit: int = 20) -> JobPage:
        return JobPage(jobs=[], raw=b"{}")

    def close(self) -> None:
        self.closed += 1


def test_fetch_page_closes_the_source_it_created(monkeypatch):
    """工具路径一页一个源 —— 不在这里关，模型每点一页就漏一条连接。"""
    src = _ClosableSource()
    monkeypatch.setattr(jobs_mod, "get_source", lambda: src)

    jobs_mod.fetch_page(1)

    assert src.closed == 1


def test_fetch_page_does_not_close_an_injected_source(monkeypatch):
    """注入的源由注入者关闭 —— fetch_page 只借，不拥有。"""
    src = _ClosableSource()
    monkeypatch.setattr(jobs_mod, "get_source", lambda: pytest.fail("不该自己造源"))

    jobs_mod.fetch_page(1, source=src)

    assert src.closed == 0
