"""ncss.cn 数据源 —— 项目的主靶子（PLAN.md §2.2）。

开发期替身是 remoteok（零登录、零对抗），上线换到这里：国家大学生就业服务平台，
背后是学信网 CAS，**要登录态**。上层的 `tools/jobs.py` 只认 `base.py` 的那份协议，
所以换源只改 `.env` 里一行 —— M1/M2 铺的那层抽象，在这里第一次真正兑现。

本文件与 `remoteok.py` 的三处不同，动手前先知道：

★ ① 要带 cookie，而且 cookie 是【现读现用】。
  `jobs.py` 的 `fetch_page` 是【每页】调一次 `get_source()`，所以数据源对象会被
  反复重建。本类因此刻意【无状态】：不持有 `Client`、不缓存 cookie，每次进
  `list_jobs` 都从盘上重读一遍。反过来写（构造时读一次、或持有一条长活连接）
  的后果是：每页漏一条连接，且"重读一次盘"被迫变成"必须重建对象" ——
  `jobs.py` 里那段"别每次重试都重建数据源"的警告就成真了。

★ ② `follow_redirects=False` 是本文件最关键的一个参数。
  登录态失效的信号是 302 → 登录页。若让它跟随重定向，httpx 会一路跟到登录页、
  拿回一个 HTTP 200 的 HTML，然后 `resp.json()` 抛 JSONDecodeError —— 于是
  "请去续期"（退出码 3）被翻译成"抓取失败，去看日志"（退出码 1），归因指向
  完全错误的方向。不跟随，那个 302 才会停在原处让我们看见。
  （和 M1 那条"SDK 对象直传"是同一种病：中间层给了一个方便的默认值，
    而它恰好把我们要的信息吃掉。）

★ ③ `publishDate` 是【毫秒整数】，不是 remoteok 那种现成的 ISO 字符串。
  必须转换，且必须转成【和 remoteok 同形状】的 ISO8601+UTC。不转的后果不是
  报错，是"两个源的数据在库里静默共存、格式不同、排序与按日期筛选全部失效"。

★ 数据源层【不碰磁盘】（base.py 契约 4）在这里的落点：本模块只【读】凭证
  （`auth.load_state`），从不写。服务端若在运行中轮换 cookie，我们丢弃 ——
  后果是中途失效被判成 `LoginExpired` → 退出码 3 请主人续期。
  这是【安全】的失败，不是一个静默错误。
"""

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
    TransientSourceError,
)

BASE_URL = "https://www.ncss.cn"

LIST_API = f"{BASE_URL}/student/jobs/jobslist/ajax/"
"""职位列表接口。★ 末尾那个斜杠是 URL 的一部分 —— 去掉会 404。

   `pagenation` 那个拼错的 key（少个 i）【没有被本模块读】，这不是漏了：
   `workflows/daily_report.py` 的 `fetch_all_pages` 靠"返回空页"收手，
   不需要知道总页数。照抄一个拼错的 key 进代码、却不消费它，就是又造一个
   "声明了但没人用"的空壳 —— 那个坑记在 `docs/SITE.md` §4.3，等真有人需要
   总页数时再读它（并且到那时要照抄那个错拼）。
"""

DETAIL_TEMPLATE = f"{BASE_URL}/student/jobs/{{job_id}}/detail.html"
"""职位详情页 —— 源站的返回里【没有】现成的 url 字段，得自己拼。
   模板来自 `docs/SITE.md` §1。同 remoteok 那排 `.get()` 的道理：
   字段名与拼法都是外部事实，集中成常量，改的时候只有一处。"""

COOKIE_DOMAINS = ("ncss.cn",)
"""只把 ncss 域的 cookie 发出去。

★ 用【父域片段】而不是完整主机名：storage_state 里两种写法都有
  （`.ncss.cn` 与 `www.ncss.cn`），拿完整主机名去比会一个都匹配不上 ——
  而后果（空 cookie → 302）看起来恰恰像"刚存的登录态就过期了"，
  归因指向完全错误的方向。断言在 `tests/test_auth.py`。
★ 顺带这也是 M8 脱敏要防的同一类事，只是从"发给谁"这一侧防：
  storage_state 里装着整个浏览器上下文见过的【所有】cookie（含 chsi、含埋点域），
  一古脑全发给 ncss，等于把别人家的凭据也交出去。"""

DEFAULT_TIMEOUT = 30.0
"""HTTP 层超时。★ 它必须【小于】工具层的 35s（`jobs.fetch_job_list` 的
   `@tool(..., timeout=35.0)`），这个大小关系是一条契约，不是巧合：
   让 HTTP 层先超时，抛出一个干净的 `TransientSourceError`（上层能重试、
   能记账）；反过来就是工具层先到 35s，那个线程被丢下不管 —— 上层只拿到
   "超时了"，拿不到 HTTP 层给的原因。护栏必须站在被护栏的东西【外面】。
   断言钉在 `tests/test_ncss.py::test_http_timeout_sits_inside_tool_timeout`。"""

USER_AGENT = "myAgent-learning/0.1"
REFERER = f"{BASE_URL}/student/jobs/index.html"
"""诚实署名 + 补上那页 XHR 本来就会带的标准请求头。

★ 这不是"绕过反爬"：不伪装 UA、不做 TLS 指纹、不接打码 —— 那是 PLAN §M2 的
  硬约定。只是别无谓地少带一个浏览器本来就会带的头。
  （"表明身份"这个习惯从 remoteok 起就有：UA 直接写着自己是谁。）"""

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
"""筛选参数，**照抄抓包，一个都不删**（`docs/SITE.md` §4）。

★ 为什么不"顺手删掉这些空参数"：值虽然是空串，但"传空串"与"压根不传这个键"
  是两件事 —— 服务端对【缺失】参数取什么默认值，我们没测过。
  `sourcesName=0` 更明显：它的值非空，删掉行为一定不同。
  抓包那一份就是唯一依据，别把我们的常识强加给源站。
  （同 §M0 那条纪律，也同 §4.3 里"照抄 `pagenation` 的错拼"。）

★ 抓包里那个 `_=1792149378213`（jQuery 的防缓存时间戳）【刻意不抄】：
  抄一个写死的旧时间戳，等于告诉对方与沿途缓存"我要的就是那份老东西"。
  如果将来真遇到缓存问题，正路是 `Cache-Control: no-cache`，
  而不是假装自己是 jQuery。
"""

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
"""源独有字段 → 全进 `Job.extras`（`Job` 只取两源【交集】，见 base.py）。
   对应 remoteok 的 tags / salary_min / salary_max。"""


def _offset_for_page(page: int) -> int:
    """页号 → 接口的 `offset` 参数。全项目【只此一处】做这个换算。

    ⚠️ **这个语义还没实测确认**（`docs/SITE.md` §4）：抓到的那次是
      `offset=1&limit=10` → 第 1 页，同一个响应里还写着 `limit=10`、`total=50`、
      `count=500`。而 `offset=1` 究竟是"第 1 页"还是"第 1 行"，
      **在第 1 页上长得一模一样** —— 点一次第 2 页才能分辨：
        · 发出去的是 `offset=2`  → 页码（下面这个实现）
        · 发出去的是 `offset=11` → 行偏移（改成 `(page-1)*limit + 1`）

      猜错的后果是【静默重复抓第 1 页】：不报错、日志里页数也对，
      只是每一页都是同一份数据 —— 日报会一本正经地少掉 90% 的岗位。
      所以刻意拆成一个具名函数：将来改，改这一行；
      `tests/test_ncss.py` 里有用例把这个假设钉在明面上。
    """
    return max(1, page)


def _iso_from_ms(value: Any) -> str | None:
    """`publishDate`（毫秒 epoch 整数）→ ISO8601 带 UTC 偏移。

    ★ 为什么要转：`base.py` 的 `Job` 契约是"ISO8601 字符串，统一带 UTC 偏移"，
      remoteok 那边源站直接就给这个形状（`2026-09-19T08:00:34+00:00`）。
      ncss 给的是毫秒整数。两个源在【同一张表】里共存，格式不一致既不会
      报错也不会入库失败 —— 只是排序和按日期筛选从此悄悄失效。
      （M5 在 `raw_path` 上认过一次同形状的账：能共存的不一致，最难查。）

    ★ 坏值返回 `None` 而不是抛异常：这是外部输入，少一个字段、格式变一变，
      不该让整份日报崩掉（PLAN 原则二，同 remoteok 那排 `.get()`）。
    """
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _redirect_error(location: str) -> SourceError:
    """302 的【落点】决定它是"登录过期"还是"另一种重定向"。

    ★ 判据是"落点里有没有 login"，不是"是不是 302"：302 也可能是
      http→https 之类的正常跳转，一律判成登录过期，会让人去跑一条没用的
      login，然后发现问题依旧 —— 比直接报错更费时间。

    ★ 为什么这一个判据对两种未知答案都成立：ncss 自己的登录页是
      `/student/login.jsp`，而 CAS 在 `account.chsi.com.cn/passport/login`
      —— 两个 URL 里都含 "login"。`docs/SITE.md` §5 里"失效时到底 302 到哪一个"
      还没实测确认，这个判据不必等它。落点原文一并写进消息，所以万一
      两个都不是，下一眼就能看到真答案。
    """
    shown = location or "(无 Location 头)"
    if "login" in location.lower():
        return LoginExpired(f"请求被重定向到登录页，登录态已失效：{shown}")
    return SourceError(f"意外的重定向（落点不是登录页，所以不是登录过期）：{shown}")


def _parse_json(resp: httpx.Response) -> dict[str, Any]:
    """解析响应体；解析不了就抛 `SourceError`，且带上原文开头。

    ★ 为什么要显式接住：ncss 存在一种"**软拦截**"可能 —— HTTP 200，
      但返回的是登录页的 HTML。那种情况下 `resp.json()` 抛的是
      `JSONDecodeError`，它的消息（"Expecting value: line 1 column 1"）
      完全没说清发生了什么，而真正的问题是"我拿到的是网页，不是数据"。
      把那 120 个字带上，一眼就能分辨是软拦截还是接口改版。
      （这也是为什么它不能是 `TransientSourceError`：重试三次拿到三份 HTML，
      纯浪费。）

    ★ 顶层不是对象也抛错，而不是返回空 dict：那种情况下后面的
      `payload.get("data")` 会一路 `.get()` 出空结果 → 日报安静地宣布
      "今天没有新岗位"。**宁可报错，也不要一句听起来完全正常的假话。**
    """
    try:
        payload = resp.json()
    except ValueError as exc:  # json.JSONDecodeError 是 ValueError 的子类
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
    """ncss 数据源。**无状态、可随时重建**（见模块顶部 ★①）。"""

    name = "ncss"

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT, root: str | Path | None = None) -> None:
        """`timeout` 默认见 `DEFAULT_TIMEOUT`（它必须小于工具层的 35s）。

        `root` 是给测试用的（临时 var/ 目录），生产路径走 `VAR_DIR` —— 同
        `archive_raw` / `write_report` 的开口方式：调用方一眼看得见它能换，
        不必知道去哪儿 patch。
        """
        self._timeout = timeout
        self._root = root

    def _session_cookies(self) -> dict[str, str]:
        """从盘上现读一次会话 cookie。缺文件 / 坏文件 / 没有 ncss 域的 cookie
        一律抛 `LoginExpired`。

        ★ 为什么把 `AuthMissingError` 在这里【翻成 `LoginExpired`】：
          `auth.py` 刻意让 `AuthMissingError` 不继承 `SourceError`，理由是
          "本机还没登录过"和"源站坏了"是两种病 —— 那个判断是对的，但它没说
          这两件事对【上层】该走哪条路。上层只有两条路：
            · `except LoginExpired` → 记 `login_expired` + 退出码 3 + 提示去跑 login
            · `except SourceError`  → 记 `failed` + 人得去翻日志
          "本机还没登录过"要的处置是【前者】。处置相同、退出码相同，
          唯一的差别是消息里那句话，所以让消息把它们分开：
          文件不在 vs 文件在但没有 ncss 的 cookie（登录没走完 / 存错了地方）。

        ★ 为什么翻译放在【数据源层】而不是 CLI：这一层是唯一同时知道
          "要 cookie"和"cookie 从磁盘来"的地方。放到 CLI 去，就得让
          `AuthMissingError` 一路穿过工具层、编排层不被误接 —— 那要靠三处
          `except` 都记得别碰它，而"记得"不是一种机制。
        """
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
        """拉一页职位，连同【原始响应体】一起返回。失败一律抛 `SourceError` 子类。

        ★ `raw` 是 `resp.content`（字节），而且必须在 `resp.json()` 【之前】取 ——
          解析一旦发生就丢信息，而存档存在的唯一理由就是"排查时还能看到完整原文"。
          （同 remoteok 那两行的顺序，那里丢的是那个声明对象。）

        ★ 分页由本方法负责，但这里是**远端分页**（不像 remoteok 是本地切片）：
          第 2 页是一次【新请求】，raw 是那一份响应 —— 别把第 1 页的缓存下来。
          `base.py` 的 `JobPage` 注释里写着这条。
        """
        cookies = self._session_cookies()
        params = {**_LIST_FILTERS, "offset": _offset_for_page(page), "limit": limit}

        try:
            resp = httpx.get(
                LIST_API,
                params=params,
                cookies=cookies,
                headers={"User-Agent": USER_AGENT, "Referer": REFERER},
                timeout=self._timeout,
                follow_redirects=False,  # ★ 见模块顶部 ★②，不是可选参数
            )
        except httpx.TransportError as exc:
            raise TransientSourceError(f"ncss 网络抖动，可以重试：{exc}") from exc
        except httpx.HTTPError as exc:
            raise SourceError(f"ncss 请求失败，不可重试：{exc}") from exc

        # ★ 顺序是契约：先判 3xx，再 raise_for_status。
        #   因为 follow_redirects=False 时 `raise_for_status()` 对 3xx 也会抛
        #   HTTPStatusError（httpx 只在"跟了重定向又跟不完"时才抛别的）——
        #   反过来的话，登录过期会被那个通用分支吸收成一句"HTTP 302"，
        #   退出码 3 就永远走不到了。
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

        body = resp.content  # ★ 先留原文，再动它（见本方法 docstring）
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
        # ★ 注意：`list` 是【空列表】是合法的 —— "翻到底了"就长这样，
        #   `fetch_all_pages` 正是靠它收手。所以这里只挡"形状不对"，
        #   不挡"内容是空的"：一个是接口坏了，一个是正常终点，混起来就再也
        #   分不出"抓完了"和"抓失败了"。

        jobs: list[Job] = []
        for row in rows:
            job_id = str(row.get("jobId") or "").strip()
            title = str(row.get("jobName") or "").strip()
            if not job_id or not title:
                # 第二道防线（同 remoteok）：没有 jobId 就拼不出详情页 url，
                # 没有标题的岗位进日报也读不成句。脏数据到此为止。
                continue
            jobs.append(
                Job(
                    source=self.name,
                    source_id=job_id,  # 源站本来就是 str（"BUr3PaTy3caCeABbpm7mar"）
                    title=title,
                    company=str(row.get("recName") or "").strip(),
                    url=DETAIL_TEMPLATE.format(job_id=job_id),
                    # ★ 值可能是"全国"而不是城市 —— 照抄，日报那边要能容忍它。
                    location=str(row.get("areaCodeName") or "").strip() or None,
                    published_at=_iso_from_ms(row.get("publishDate")),
                    extras={key: row.get(key) for key in _EXTRA_KEYS},
                )
            )
        return JobPage(jobs=jobs, raw=body)
