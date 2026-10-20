"""ncss.cn 数据源 —— 项目的主靶子（PLAN.md §2.2）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from agent.tools.auth import LOGIN_HINT, AuthMissingError, cookies_from_state, load_state
from agent.tools.sources.base import (
    Job,
    JobPage,
    LoginExpired,
    SourceError,
    SourceUnreachable,
    TransientSourceError,
)

BASE_URL = "https://www.ncss.cn"

LIST_API = f"{BASE_URL}/student/jobs/jobslist/ajax/"
"""职位列表接口。★ 末尾那个斜杠是 URL 的一部分 —— 去掉会 404。"""

DETAIL_TEMPLATE = f"{BASE_URL}/student/jobs/{{job_id}}/detail.html"
"""职位详情页 —— 源站的返回里【没有】现成的 url 字段，得自己拼。"""

COOKIE_DOMAINS = ("ncss.cn",)
"""只带 ncss 域的 cookie；按父域片段筛，`.ncss.cn` 与 `www.ncss.cn` 都命中。"""

DEFAULT_TIMEOUT = 30.0
"""HTTP 层超时。★ 必须小于工具层的 35s，让 HTTP 先超时并抛出可重试的异常。"""

KEEPALIVE_EXPIRY_SECONDS = 30.0
"""连接空闲多久后丢弃。★ 必须大于翻页间隔上限（3 + 2 = 5s），否则每页都重建连接。"""

USER_AGENT = "myAgent-learning/0.1"
REFERER = f"{BASE_URL}/student/jobs/index.html"
"""诚实署名 + 补上那页 XHR 本来就会带的标准请求头。"""

_LIST_FILTERS: dict[str, str] = {
    "jobType": "",
    "areaCode": "",
    "jobName": "",
    "monthPay": "",
    "industrySectors": "",
    "property": "",
    "categoryCode": "",
    "memberLevel": "",
    "recruitType": "",
    "keyUnits": "",
    "degreeCode": "",
    "sourcesName": "0",
    "sourcesType": "",
}
"""筛选参数，**照抄抓包，一个都不删**（`docs/SITE.md` §4）。"""

_EXTRA_KEYS = (
    "lowMonthPay",
    "highMonthPay",
    "degreeName",
    "recProperty",
    "recScale",
    "recTags",
    "major",
    "headCount",
)
"""源独有字段 → 全进 `Job.extras`（`Job` 只取两源【交集】，见 base.py）。"""


def _offset_for_page(page: int) -> int:
    """页号 → 接口的 `offset` 参数。全项目【只此一处】做这个换算。"""
    return max(1, page)


def _iso_from_ms(value: Any) -> str | None:
    """`publishDate`（毫秒 epoch 整数）→ ISO8601 带 UTC 偏移。"""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _redirect_error(location: str) -> SourceError:
    """302 的【落点】决定它是"登录过期"还是"另一种重定向"。"""
    shown = location or "(无 Location 头)"
    if "login" in location.lower():
        return LoginExpired(f"请求被重定向到登录页，登录态已失效：{shown}")
    return SourceError(f"意外的重定向（落点不是登录页，所以不是登录过期）：{shown}")


def _parse_json(resp: httpx.Response) -> dict[str, Any]:
    """解析响应体；解析不了就抛 `SourceError`，且带上原文开头。"""
    try:
        payload = resp.json()
    except ValueError as exc:
        head = " ".join(resp.text[:120].split())
        raise SourceError(
            f"返回的不是 JSON（前 120 字：{head!r}）—— 可能被软拦截到登录页，或接口改版了。"
        ) from exc
    if not isinstance(payload, dict):
        raise SourceError(
            f"返回的 JSON 顶层不是对象，而是 {type(payload).__name__} —— 接口可能改版了。"
        )
    return payload


class NcssSource:
    """ncss 数据源。懒建一条长活连接，用完请 `close()`。"""

    name = "ncss"

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT, root: str | Path | None = None) -> None:
        """`timeout` 默认见 `DEFAULT_TIMEOUT`（它必须小于工具层的 35s）。"""
        self._timeout = timeout
        self._root = root
        self._client: httpx.Client | None = None

    def _client_or_create(self) -> httpx.Client:
        """返回本次会话的客户端；第一次调用时才建连读凭证。"""
        if self._client is None:
            self._client = httpx.Client(
                cookies=self._session_cookies(),
                headers={"User-Agent": USER_AGENT, "Referer": REFERER},
                timeout=self._timeout,
                follow_redirects=False,
                limits=httpx.Limits(
                    max_connections=1,
                    max_keepalive_connections=1,
                    keepalive_expiry=KEEPALIVE_EXPIRY_SECONDS,
                ),
            )
        return self._client

    def close(self) -> None:
        """关闭连接（幂等）。谁创建谁关闭，注入的源由调用方关。"""
        if self._client is not None:
            self._client.close()
            self._client = None

    def _session_cookies(self) -> dict[str, str]:
        """建连接时从盘上读一次会话 cookie。缺文件 / 坏文件 / 没有 ncss 域的 cookie"""
        try:
            state = load_state(root=self._root)
        except AuthMissingError as exc:
            raise LoginExpired(f"本机还没有可用的 ncss 登录态（{exc}）") from exc

        cookies = cookies_from_state(state, domains=COOKIE_DOMAINS)
        if not cookies:
            raise LoginExpired(
                f"登录态文件里没有 {COOKIE_DOMAINS[0]} 域的 cookie —— 登录可能没走完，"
                f"或凭证被存到了别处。请重新运行 `{LOGIN_HINT}`。"
            )
        return cookies

    def list_jobs(self, *, page: int = 1, limit: int = 20) -> JobPage:
        """拉一页职位，连同【原始响应体】一起返回。失败一律抛 `SourceError` 子类。"""
        params = {**_LIST_FILTERS, "offset": _offset_for_page(page), "limit": limit}

        try:
            resp = self._client_or_create().get(LIST_API, params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise SourceUnreachable(f"ncss 连不上，未发出 HTTP 请求：{exc}") from exc
        except httpx.TransportError as exc:
            raise TransientSourceError(f"ncss 网络抖动，可以重试：{exc}") from exc
        except httpx.HTTPError as exc:
            raise SourceError(f"ncss 请求失败，不可重试：{exc}") from exc

        # 3xx 必须先于 raise_for_status 判：跟随重定向关闭时它也会对 3xx 抛异常。
        if resp.is_redirect:
            raise _redirect_error(resp.headers.get("location", ""))

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as status_err:
            code = status_err.response.status_code
            if code in (429, 408) or 500 <= code < 600:
                raise TransientSourceError(
                    f"HTTP {code}：ncss 服务器异常，可以重试"
                ) from status_err
            raise SourceError(f"HTTP {code}：ncss 访问异常，不可重试：{status_err}") from status_err

        body = resp.content
        payload = _parse_json(resp)

        if payload.get("flag") is not True:
            raise SourceError(
                f"ncss 报告业务失败（flag={payload.get('flag')!r}，"
                f"errors={payload.get('errors')!r}）—— HTTP 是成功的，服务端说这次不成功。"
                "★ 宁可变红也不把它读成「没有岗位」：读成空的，日报会安静地宣布"
                "「今天没有新职位」，而那是一句听起来完全正常的假话。"
                "⚠️ `flag` 何时为 false 我们没实测过（见 docs/SITE.md §4.3）；"
                "已知风险是分页越界时也可能返回 false —— 那会让抓完最后一页后的"
                "那一次请求失败。若真发生，先看上面 errors 说了什么再决定怎么改。"
            )

        data = payload.get("data")
        rows = data.get("list") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise SourceError(
                f"响应里没有 data.list（拿到 {type(rows).__name__}）—— 接口可能改版了。"
            )

        jobs: list[Job] = []
        for row in rows:
            job_id = str(row.get("jobId") or "").strip()
            title = str(row.get("jobName") or "").strip()
            if not job_id or not title:
                continue
            jobs.append(
                Job(
                    source=self.name,
                    source_id=job_id,
                    title=title,
                    company=str(row.get("recName") or "").strip(),
                    url=DETAIL_TEMPLATE.format(job_id=job_id),
                    location=str(row.get("areaCodeName") or "").strip() or None,
                    published_at=_iso_from_ms(row.get("publishDate")),
                    extras={key: row.get(key) for key in _EXTRA_KEYS},
                )
            )
        return JobPage(jobs=jobs, raw=body)
