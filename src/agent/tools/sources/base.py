"""招聘数据源抽象 —— 和 agent/llm/base.py 是同一套套路。

存在的理由（PLAN.md §2.2）：
  开发期用 remoteok（零登录、零对抗），上线用 ncss（要学信网登录态）。
  上层 tools/jobs.py 只认这里的协议，所以换站点时上层一行不改 ——
  只改 .env 里的 JOB_SOURCE。这与 M1 的 Provider 抽象是同一个设计。

⚠️ 纪律：本模块禁止 import httpx / playwright 或任何具体实现。
   一旦 import 了，这层抽象就是假的 —— 和 base.py 同一个道理。
"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Job:
    """一条招聘信息的【归一化】形状。

    ★ 归一化的关键决策：字段只取两个源的【交集】。
      取并集的话，ncss 有的「学历要求」remoteok 没有，就变成一堆永远为
      None 的字段，上层到处要写 `if x is not None`，得不偿失。
      各源独有的字段，统一塞进 extras。
    """

    source: str
    """来源标识："remoteok" / "ncss"。入库去重时和 source_id 一起做主键。"""

    source_id: str
    """源站自己的主键（remoteok 是 id，ncss 是 jobId）。

    ★ 归一化成 str 而不是保留 int：两个源的类型未必一致，
      统一成 str 最省事，也方便和 SQLite 的 TEXT 列对上。
    """

    title: str
    company: str
    url: str
    """能直接点开的职位页。remoteok 实测 url 与 apply_url 相同，取 url。"""

    location: str | None = None
    published_at: str | None = None
    """ISO8601 字符串，统一带 UTC 偏移
    （remoteok 实测：2026-09-19T08:00:34+00:00）。
    ★ 统一存 UTC，展示时再转 Asia/Shanghai —— 见 PLAN.md §M2 常见坑。"""

    extras: dict[str, Any] = field(default_factory=dict)
    """源独有的字段（remoteok: tags / salary_min / salary_max）。

    ⚠️ 刻意【不含职位描述全文】：remoteok 单条描述约 6KB，一页 20 条就是
       120KB，整包塞进上下文会直接把 token 预算烧穿（M6 会算这笔账）。
       需要全文时走 fetch_job_detail 单独取。
    """


@dataclass(frozen=True)
class JobPage:
    """一页招聘信息 —— 连同【产出它的原始响应体】一起交出来。

    ★ 为什么 raw 要挤进返回值，而不是另开一个 `source.last_raw` 属性：
      属性版本靠"你得记得紧跟在 list_jobs() 之后读"这种口头约定。忘了读不会报错，
      原文就那么静静丢了 —— 而要查"这条岗位当时抓到的原文长什么样"的时刻，
      恰恰就是最需要它的时刻。
      放进返回值里，这件事在【类型上】就没有出错的可能：拿到 jobs 就拿到了 raw。
      （和 M1「arguments 不许在 Provider 里解析」是同一原则的两面：
        那边是"不该替我做的别做"，这边是"该给我的请一起给我"。）

    ★ 为什么 raw 是 bytes 而不是解析好的 dict：
      存原文的全部意义就是"排查时还能看到完整原文"，任何一次解析都会丢信息
      （声明对象、多出来的字段、字段顺序……）。解析是我们的事，存档只管存档。

    ★ 为什么不把 raw 塞进每个 Job 里：
      一页 20 条共享【同一份】响应体。塞进去就是抄 20 份 ——
      remoteok 这一页约 627 KB，抄 20 份就是 12 MB，纯浪费磁盘。
      （存档走内容寻址，同一份 sha1 只落一次，见 memory/store.py。）
    """

    jobs: list[Job]
    raw: bytes
    """这次抓取拿到的原始响应体。上层负责把它写进 var/raw/，数据源层不碰磁盘。"""


class SourceError(Exception):
    """数据源层的一切失败。

    ★ 为什么要这个基类：httpx / playwright 的异常类型【不许漏到上层】。
      不包一层的话，上层就得 import httpx 才能 except —— 抽象又漏了。
      这和 M1 那个 "SDK 对象直传" 是同一类错误，只是发生在异常路径上。
    """


class LoginExpired(SourceError):
    """登录态失效。

    ★ 这是 ncss 的【实测信号】（PLAN.md §2.1）：请求返回 302 → /student/login.jsp。

      单独立一个类型，是因为它需要的处置和别人【不同】——
      不是重试，而是告警 + 人工续期 + 优雅退出（退出码 3）。
      上层靠 `except LoginExpired` 把它从普通错误里挑出来。
    """


class TransientSourceError(SourceError):
    """【瞬时】故障：超时、连接中断、5xx。

    ★ 单独立这个类型，是为了让重试层能分辨"值得再试一次"和"再试也是白搭"。

      现在 remoteok.py 把所有 httpx.HTTPError 囫囵包成一个 SourceError ——
      于是 404（页面/接口没了，永久）和超时（网络抖了，瞬时）被同一个
      类型吸收。重试层拿到一个 SourceError，**无法判断该不该重试**，
      只能二选一：全都重试（404 也重试 3 次，浪费 7 秒、多打 3 个请求，
      对被抓的站点很不礼貌），或全都不重试（网络抖一下整份日报就废了）。

      两个都不对，所以要把"瞬时不瞬时"这个信息，从抛出的那一刻就带上。

    ★ 为什么不给 SourceError 加一个 `retryable: bool` 字段，而用子类？
      因为 `except` 是按类型匹配的。子类让"哪些错误值得重试"变成一个
      **可以写在 except 子句里的问题**（`except TransientSourceError`），
      而字段需要先捕获、再读值、再分支 —— 多三步，且忘了一步就静默走错。
    """


class JobSource(Protocol):
    """所有数据源实现都要满足的协议（鸭子类型，无需继承）。"""

    name: str
    """源标识，要能对上 Job.source。"""

    def list_jobs(self, *, page: int = 1, limit: int = 20) -> JobPage:
        """拉一页招聘信息，连同原始响应体一起返回。

        契约（四条，和 llm/base.py 的 Provider 是同一套纪律）：
        1. **不重试**。重试属于编排层（M4），否则退避策略会散落各层。
        2. **不吞异常**，但要做【类型归一化】：底层异常一律包成 SourceError 子类。
        3. **只做数据，不做判断**。不要把"这条值不值得投"之类的业务逻辑写进来。
        4. **不碰磁盘**。raw 原样交出去，存哪儿、怎么命名是上层的事 ——
           数据源一旦知道 var/raw 的存在，换源、写测试都得跟着伺候文件系统。

        分页由本方法负责：远端可能一次给全量，本地切片即可。
        ★ 连带后果：这一页的 raw 是【整份响应体】，不是切片。
          同一页翻到第 2 页，raw 是【又一次请求】的响应 —— 别把第一页的缓存下来。
          远端一次给全量时，翻页=同一份 raw 的 sha1 相同，内容寻址自然只落一次盘。
        """
        ...
