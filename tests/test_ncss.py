"""ncss 数据源单测 —— 在【真 httpx 边界】上打 NcssSource（M2b Phase B）。

★ 与 `test_remoteok.py` 的两点不同，先看清：

  ① **每个用例都要先造一个假登录态**。ncss 的 `list_jobs` 第一步就是读盘上的
     cookie，所以先 `save_state(...)` 到 `tmp_path`。顺带这件事本身也是一条
     可断言的用例 —— "干净机器上第一次跑 JOB_SOURCE=ncss"就撞在那一幕上。

  ② **302 是主角**。这个源最要紧的一行是 `follow_redirects=False`，它唯一的
     用途就是让"登录失效"以一个【看得见的 302】的形式出现。所以下面有一整个
     小节都在打 302 的各种落点。

★ 数据用的是 `eval/fixtures/ncss_sample.json` —— 主人真机抓到的那一份，
  原样落盘（10 条全留着）。字段名、类型、`pagenation` 的错拼都与线上一致，
  于是"毫秒转没转对""url 拼没拼对"这种断言才真的绑住了代码。
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

import agent.tools.jobs  # noqa: F401  ← 只为让 @tool 跑起来，下面要用 TOOLS
from agent.tools.auth import save_state
from agent.tools.registry import TOOLS
from agent.tools.sources.base import LoginExpired, SourceError, TransientSourceError
from agent.tools.sources.ncss import DEFAULT_TIMEOUT, LIST_API, NcssSource

NCSS_FIXTURE = Path(__file__).resolve().parents[1] / "eval" / "fixtures" / "ncss_sample.json"


# ── 夹具与帮手 ─────────────────────────────────────────────────────────────


@pytest.fixture
def ncss_bytes() -> bytes:
    """真实响应体。【本地夹具】而不是放 conftest —— 只服务这一个文件
    （conftest 的 docstring 定过这条规矩：共用的才上去）。"""
    return NCSS_FIXTURE.read_bytes()


@pytest.fixture
def ncss_payload(ncss_bytes: bytes) -> dict:
    return json.loads(ncss_bytes)


def _cookies() -> list[dict]:
    """三个 cookie：两个 ncss 域的、一个别人家的。

    ★ 名字仍然刻意是假的（`FAKE_*`），但理由换了。旧理由（"真名还没确定"）
      已在 2026-10-16 失效：实测真名是 `SESSION`、且 `JSESSIONID` 属于 CAS 域
      （见 `docs/SITE.md` §3.2）。新理由更硬：
      本用例验的是【按域名筛选】这条机制，它**不该依赖任何一个具体的 cookie 名** ——
      把线上真名写死进来，ncss 哪天换个名字，测试就会红，而那是个**假警报**；
      更糟的是，它还会诱使人去"把测试改对"，而不是去看真问题。
      （想看真名长什么样，看 `tests/test_auth.py::_state` —— 那里照实测写了真名。）
    """
    return [
        {"name": "FAKE_SESSION", "value": "abc", "domain": ".ncss.cn", "path": "/"},
        {"name": "FAKE_SID", "value": "def", "domain": "www.ncss.cn", "path": "/"},
        # ★ 别的域：不该出现在发出去的 Cookie 头里（见对应的用例）。
        {"name": "CHSI_TICKET", "value": "zzz", "domain": ".chsi.com.cn", "path": "/"},
    ]


def _login(tmp_path, *, cookies: list[dict] | None = None) -> None:
    """在 tmp_path 这个 var/ 里造一份登录态。"""
    save_state(
        {"cookies": cookies if cookies is not None else _cookies(), "origins": []}, root=tmp_path
    )


def _route(**kwargs) -> respx.Route:
    """接管那条 `GET .../jobslist/ajax/?…` 的请求（query 不参与匹配）。

    ★ 用 `url__regex` 前缀匹配，而不是 `respx.get(LIST_API)`：后者把【完整
      URL】当模式，而我们发出去的请求带着一串 query —— 能不能匹配上取决于
      respx 那个版本怎么比较 query。前缀正则不依赖那个细节。
    """
    return respx.get(url__regex=re.escape(LIST_API)).mock(**kwargs)


def _json_body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


# ── ① 字段映射（源站叫什么 → Job 叫什么）──────────────────────────────────


def test_field_mapping(tmp_path, ncss_payload, ncss_bytes):
    """`jobId→source_id`、`jobName→title`、`recName→company`、`areaCodeName→location`。"""
    _login(tmp_path)
    with respx.mock:
        _route(return_value=httpx.Response(200, content=ncss_bytes))
        page = NcssSource(root=tmp_path).list_jobs(page=1, limit=10)

    first = ncss_payload["data"]["list"][0]
    got = page.jobs[0]
    assert got.source == "ncss"
    assert got.source_id == first["jobId"]  # ★ 源站本来就是 str，不用再转
    assert got.title == first["jobName"]
    assert got.company == first["recName"]
    assert got.location == first["areaCodeName"]
    assert got.extras["lowMonthPay"] == first["lowMonthPay"]
    assert got.extras["degreeName"] == first["degreeName"]
    assert len(page.jobs) == len(ncss_payload["data"]["list"]) == 10


def test_url_is_built_because_the_response_has_none(tmp_path, ncss_payload, ncss_bytes):
    """★ 源站的返回里【没有】url 字段 —— 得拿 `jobId` 拼出来。

    remoteok 那边是现成的 `url` 字段。两个源"长得像、实际不一样"，
    照抄 remoteok 的映射会得到 `url=None`（不报错，只是日报里每条都点不开）。
    所以这里先钉住"返回里没有 url"这个事实，再钉住拼出来的形状。
    """
    _login(tmp_path)
    with respx.mock:
        _route(return_value=httpx.Response(200, content=ncss_bytes))
        page = NcssSource(root=tmp_path).list_jobs(limit=10)

    first = ncss_payload["data"]["list"][0]
    assert "url" not in first
    assert page.jobs[0].url == (f"https://www.ncss.cn/student/jobs/{first['jobId']}/detail.html")


def test_published_at_is_iso8601_with_utc_offset(tmp_path, ncss_payload, ncss_bytes):
    """★ 本文件第一要紧的断言。

    源站给的是**毫秒整数**（如 `1791796047304`），而 `base.py` 的 `Job` 契约是
    "ISO8601 字符串，统一带 UTC 偏移" —— remoteok 那边源站【直接】就给这个形状。
    不转的后果不是报错：两个源的数据在同一张表里共存、格式不同、谁也不吭声，
    只是排序与按日期筛选从此悄悄失效。（M5 在 `raw_path` 上认过同形状的账。）

    ★ 断言按【形状】而不是硬编码那串时间：形状一致才是契约，
      具体是哪一秒是数据。`remoteok` 的形状是 `2026-09-19T08:00:34+00:00`。
    """
    _login(tmp_path)
    with respx.mock:
        _route(return_value=httpx.Response(200, content=ncss_bytes))
        page = NcssSource(root=tmp_path).list_jobs(limit=10)

    ms = ncss_payload["data"]["list"][0]["publishDate"]
    got = page.jobs[0].published_at
    assert isinstance(ms, int)  # ① 源站确实是毫秒整数
    assert got is not None
    assert got.endswith("+00:00")  # ② 和 remoteok 同形状
    parsed = datetime.fromisoformat(got)  # ③ 标准解析器吃得下
    assert parsed.utcoffset() == timedelta(0)
    assert parsed == datetime.fromtimestamp(ms / 1000, tz=UTC)  # ④ 值也没算错


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


# ── ② 请求形状（分页 / 筛选参数 / 不抄 jQuery 时间戳）─────────────────────


def test_page_number_becomes_the_offset_param(tmp_path, ncss_bytes):
    """★ 钉住 `_offset_for_page` 的假设 —— 它【还没被实测确认】。

    抓到的只有第 1 页（`offset=1&limit=10`），而"页码"与"起始行号"
    在第 1 页上长得【一模一样】。这个用例把当前的假设写在明面上：
    改 `_offset_for_page` 时它会红 —— 那是提醒"顺便确认 docs/SITE.md §4
    那一格填了没有"，不是提醒"你改错了"。
    """
    _login(tmp_path)
    with respx.mock:
        route = _route(return_value=httpx.Response(200, content=ncss_bytes))
        NcssSource(root=tmp_path).list_jobs(page=2, limit=10)
    params = route.calls.last.request.url.params
    assert params["offset"] == "2"
    assert params["limit"] == "10"


def test_filters_go_out_verbatim_and_no_jquery_timestamp(tmp_path, ncss_bytes):
    """★ 抓包里那 13 个筛选参数一个都不许少 —— "传空串"与"压根不传这个键"
    是两件事，服务端对缺失参数取什么默认值我们没测过（`sourcesName=0` 更明显：
    它的值非空，删掉行为一定不同）。

    ★ 抓包里那个 `_=1792149378213` 必须【不在】：那是 jQuery 的防缓存伪装。
      抄一个写死的旧时间戳，等于告诉对方"我要的就是那份老东西"。
    """
    _login(tmp_path)
    with respx.mock:
        route = _route(return_value=httpx.Response(200, content=ncss_bytes))
        NcssSource(root=tmp_path).list_jobs(page=1, limit=10)
    params = route.calls.last.request.url.params
    assert params["sourcesName"] == "0"
    assert params["jobName"] == ""  # 空串也要在
    assert params["recruitType"] == ""
    assert "_" not in params


def test_only_ncss_domain_cookies_are_sent(tmp_path, ncss_bytes):
    """★ storage_state 里装着【整个浏览器上下文】见过的所有 cookie（含 chsi 的）。
    一古脑全发出去，等于把别人家的凭据也交给 ncss —— M8 脱敏防的是同一类事，
    只是这里的防线在"发给谁"那一侧。"""
    _login(tmp_path)
    with respx.mock:
        route = _route(return_value=httpx.Response(200, content=ncss_bytes))
        NcssSource(root=tmp_path).list_jobs()
    sent = route.calls.last.request.headers.get("cookie", "")
    assert "FAKE_SESSION" in sent
    assert "FAKE_SID" in sent  # `.ncss.cn` 与 `www.ncss.cn` 都要命中
    assert "CHSI_TICKET" not in sent


# ── ③ 登录态失效：四种进法，同一条出路（退出码 3）──────────────────────────


def test_redirect_to_ncss_login_page_is_login_expired(tmp_path):
    """302 → `/student/login.jsp`（PLAN §2.1 的实测信号）。"""
    _login(tmp_path)
    with respx.mock:
        _route(return_value=httpx.Response(302, headers={"Location": "/student/login.jsp"}))
        with pytest.raises(LoginExpired) as err:
            NcssSource(root=tmp_path).list_jobs()
    assert "login.jsp" in str(err.value)


def test_redirect_to_chsi_cas_is_also_login_expired(tmp_path):
    """★ 失效时究竟是 302 到 ncss 自己的登录页、还是回 CAS 再登一次
    （`docs/SITE.md` §5 那一格还没实测确认）—— 判据是"落点里含 login"，
    对【两种答案都成立】，所以不必等那一格填完才敢写代码。"""
    _login(tmp_path)
    cas = "https://account.chsi.com.cn/passport/login?service=https://www.ncss.cn/student/connect/chsi"
    with respx.mock:
        _route(return_value=httpx.Response(302, headers={"Location": cas}))
        with pytest.raises(LoginExpired):
            NcssSource(root=tmp_path).list_jobs()


def test_redirect_somewhere_else_is_a_plain_source_error(tmp_path):
    """★ 不许把【所有】302 都吃成"登录过期"：那会让人去跑一条没用的 login，
    然后发现问题依旧 —— 比直接报错更费时间。落点里没有 login 就如实报。

    断言里带上落点原文，是因为"万一两种已知落点都不是"，
    下一眼就该从这条消息里看到真答案。"""
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
    """★ 干净机器上第一次跑 `JOB_SOURCE=ncss` 就撞这一幕。

    `AuthMissingError` 是 `RuntimeError`（`auth.py` 故意不继承 `SourceError`）。
    若不在数据源层翻译，它会一路穿过工具层与编排层的 `except` 子句，
    最后在 CLI 上炸成一页栈 —— 而它要的处置清清楚楚只有一条：去跑 login。
    所以消息里必须有那条命令，且【一次请求都不发】（没凭证发什么都是浪费）。
    """
    with respx.mock(assert_all_called=False):
        route = _route(return_value=httpx.Response(200, content=b"{}"))
        with pytest.raises(LoginExpired) as err:
            NcssSource(root=tmp_path).list_jobs()
    assert "python -m app.main login" in str(err.value)
    assert len(route.calls) == 0


def test_state_without_any_ncss_cookie_is_login_expired(tmp_path):
    """文件在、但里面只有别人家的 cookie —— 发出去等于没带凭证（然后 302）。
    提前判掉，消息才说得清是"登录没走完/存错了地方"，而不是含糊的"过期了"。"""
    with respx.mock(assert_all_called=False):
        route = _route(return_value=httpx.Response(200, content=b"{}"))
        _login(tmp_path, cookies=[{"name": "X", "value": "1", "domain": ".chsi.com.cn"}])
        with pytest.raises(LoginExpired):
            NcssSource(root=tmp_path).list_jobs()
    assert len(route.calls) == 0


def test_redirect_is_not_followed(tmp_path):
    """★ 本文件最关键的一行：`follow_redirects=False`。

    若它被打开，httpx 会跟到登录页、拿回一个 **HTTP 200 的 HTML**，然后
    `resp.json()` 抛 JSONDecodeError —— 于是"请去续期"（退出码 3）被翻译成
    "抓取失败，去看日志"（退出码 1），归因指向完全错误的方向。

    这条断言是【行为性】的：302 之后只发生过一次请求（没跟）。
    """
    _login(tmp_path)
    with respx.mock:
        route = _route(return_value=httpx.Response(302, headers={"Location": "/student/login.jsp"}))
        with pytest.raises(LoginExpired):
            NcssSource(root=tmp_path).list_jobs()
        # ★ 断言必须在 with 里面：respx 退出时会 reset 掉路由，`route.calls` 也随之清空。
        assert len(route.calls) == 1


# ── ④ 把"失败"和"空的"分开（最容易长成假绿灯的地方）─────────────────────


def test_flag_false_is_an_error_not_an_empty_page(tmp_path, ncss_payload):
    """★★ 本文件第二要紧的断言。

    `flag` 为 false 时若按"没有岗位"处理，日报会**安静地宣布**
    「今天没有新职位」—— 一句听起来完全正常的假话。
    宁可整条 run 变红（人看得见、成本看板上 `recent_errors` 有记录），
    也不要一个假的成功。（同 M5 那条：少算的账本比没有账本更危险。）
    """
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
    """★ 和上一条成对。`list` 是**空列表**是合法的 —— "翻到底了"就长这样，
    `fetch_all_pages` 正是靠它收手。所以这里只挡"形状不对"，不挡"内容是空的"：
    混成一条，就再也分不出"抓完了"和"抓失败了"。"""
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
    """★ ncss 存在"软拦截"的可能：HTTP 200，但回来的是登录页的 HTML。

    裸的 `resp.json()` 抛 `JSONDecodeError`，消息是
    `Expecting value: line 1 column 1` —— 完全没说清发生了什么。翻译成
    `SourceError` 并把原文开头带上，一眼就能分辨"软拦截"还是"接口改版"。
    也必须是【永久】错：重试三次拿到三份 HTML，纯浪费（对源站也不礼貌）。
    """
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
    """★ 与 remoteok 【相反】：这边是**远端分页** —— 第 2 页是一次新请求，
    raw 是那一份响应，不是本地切片出来的同一份字节。

    （remoteok 那条用例断言"三页 raw 全等"，两条断言合起来把
      "远端一次给全量"和"远端真分页"这两个世界分开了。）"""
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
        assert len(route.calls) == 2  # ★ 断言在 with 里面（退出时 respx 会 reset）
    assert p1.raw == bodies[0]
    assert p2.raw == bodies[1]
    assert p1.raw != p2.raw


# ── ⑤ 异常分类（同 remoteok 的那张表，一个字不改）────────────────────────


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


def test_transport_error_is_transient(tmp_path):
    """连不上、DNS 挂了 —— 也是"值得再试一次"的一种。"""
    _login(tmp_path)
    with respx.mock:
        _route(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(TransientSourceError):
            NcssSource(root=tmp_path).list_jobs()


def test_http_timeout_sits_inside_the_tool_timeout():
    """★ 30 < 35 是【契约】不是巧合（见 `ncss.DEFAULT_TIMEOUT` 的注释）：
    让 HTTP 层先超时，抛一个干净的 `TransientSourceError`（上层能重试、能记账）；
    反过来就是工具层先到 35s、那个线程被丢下不管 —— 上层只拿到"超时了"，
    拿不到 HTTP 层给的原因。护栏必须站在被护栏的东西【外面】。
    """
    assert DEFAULT_TIMEOUT < TOOLS["fetch_job_list"].timeout
