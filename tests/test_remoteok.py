"""数据源层单测 —— 在【真 httpx 边界】上打 RemoteOkSource。

★ 为什么用 respx 而不是自己写个假 httpx.get：
  verify 脚本（m2/m3/m4）都直接 patch `fetch_page` 或注入假 source —— 那是
  "跳过数据源层"测上层。于是 remoteok.py 自己（解析、筛选、切页、异常分类）
  从没被单独盯过，而它恰恰是那份"字段名映射错了也不报错"的代码。
  respx 拦在 httpx 的 transport 上：remoteok 里的 `httpx.get` 照常被调用，
  只把网络换成了内存里的一段响应体 —— 时间真实、调用链真实，只有字节是假的。

★ 用的是 eval/fixtures 那份【真实存档切片】（conftest 的 fixture_bytes），
  不是手搓的玩具 JSON：字段名、id 类型、date 形状都跟线上一致，
  于是"position→title 有没有映射对"这种断言才真的绑住了代码。
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from agent.tools.sources.base import SourceError, TransientSourceError
from agent.tools.sources.remoteok import API_URL, RemoteOkSource


def _mock_ok(body: bytes) -> respx.Route:
    """把 GET /api 换成一段内存响应体。调用方负责在 respx.mock 上下文里用它。"""
    return respx.get(API_URL).mock(return_value=httpx.Response(200, content=body))


def _real_rows(body: bytes) -> list[dict]:
    """fixture 里【真正的岗位】—— 按 remoteok 自己的判据筛（挡住声明对象）。"""
    return [r for r in json.loads(body) if r.get("position") and r.get("id")]


def test_declaration_object_skipped(fixture_bytes):
    """fixture 有 30 项，但 raw[0] 是声明对象 —— 岗位只能是 29 条。

    ★ 反过来断言：若解析漏了这道筛，这里会是 30，而数据里就混进一条
      title 取自 "legal" 的脏岗位，且没人会知道。
    """
    with respx.mock:
        _mock_ok(fixture_bytes)
        page = RemoteOkSource().list_jobs(page=1, limit=200)
    assert len(page.jobs) == 29 == len(_real_rows(fixture_bytes))


def test_field_mapping(fixture_bytes):
    """源站的字段名和我们不同：position→title、date→published_at、id→source_id。"""
    with respx.mock:
        _mock_ok(fixture_bytes)
        page = RemoteOkSource().list_jobs(page=1, limit=1)
    first = _real_rows(fixture_bytes)[0]
    got = page.jobs[0]
    assert got.source == "remoteok"
    assert got.source_id == str(first["id"])  # ★ id 归一化成 str（Job 的契约）
    assert got.title == first["position"].strip()
    assert got.url == first["url"].strip()
    assert got.published_at == first["date"]


def test_empty_title_dropped():
    """第二道防线：列表推导放行、但 strip 后是空标题的，映射循环里要丢弃。"""
    body = json.dumps(
        [
            {"last_updated": "x", "legal": "y"},  # 声明对象（第一道防线挡它）
            {"id": "1", "position": "正常岗位", "company": "A", "url": "https://x/1"},
            {"id": "2", "position": "   ", "company": "B", "url": "https://x/2"},  # 空标题
            {"id": "3", "company": "C", "url": "https://x/3"},  # 连 position 都没有
        ]
    ).encode()
    with respx.mock:
        _mock_ok(body)
        page = RemoteOkSource().list_jobs(page=1)
    assert [j.source_id for j in page.jobs] == ["1"]


def test_raw_is_untouched_response_bytes(fixture_bytes):
    """raw 必须是【未解析】的响应体 —— 存档的全部意义就是留下完整原文。"""
    with respx.mock:
        _mock_ok(fixture_bytes)
        page = RemoteOkSource().list_jobs(page=1, limit=5)
    assert isinstance(page.raw, bytes)
    assert page.raw == fixture_bytes


def test_local_pagination_shares_one_raw(fixture_bytes):
    """远端一次给全量、本地切片 —— 于是三页的 raw 是同一份字节。

    ★ 3 次 GET 拿回同一份 body：20 + 9 + 0（空页 = 翻到底）。
      这正是 L2 回放里"var/raw/ 只有一个文件"的成因，先在这里钉住它。
    """
    with respx.mock:
        _mock_ok(fixture_bytes)
        p1 = RemoteOkSource().list_jobs(page=1, limit=20)
        p2 = RemoteOkSource().list_jobs(page=2, limit=20)
        p3 = RemoteOkSource().list_jobs(page=3, limit=20)
    assert (len(p1.jobs), len(p2.jobs), len(p3.jobs)) == (20, 9, 0)
    assert p1.raw == p2.raw == p3.raw == fixture_bytes


@pytest.mark.parametrize("code", [429, 408, 500, 502, 503])
def test_server_side_status_is_transient(code):
    """429/408/5xx → 瞬时（上层重试有意义）。"""
    with respx.mock:
        respx.get(API_URL).mock(return_value=httpx.Response(code))
        with pytest.raises(TransientSourceError):
            RemoteOkSource().list_jobs()


@pytest.mark.parametrize("code", [403, 404])
def test_client_side_status_is_permanent(code):
    """403/404 → 永久（重试纯属浪费）。★ 必须【不是】Transient 的子类判定。"""
    with respx.mock:
        respx.get(API_URL).mock(return_value=httpx.Response(code))
        with pytest.raises(SourceError) as excinfo:
            RemoteOkSource().list_jobs()
    assert not isinstance(excinfo.value, TransientSourceError)


def test_transport_error_is_transient():
    """连接类故障（连不上、DNS 挂了）也是"值得再试一次"的一种。"""
    with respx.mock:
        respx.get(API_URL).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(TransientSourceError):
            RemoteOkSource().list_jobs()
