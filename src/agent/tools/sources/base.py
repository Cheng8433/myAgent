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


class JobSource(Protocol):
    """所有数据源实现都要满足的协议（鸭子类型，无需继承）。"""

    name: str
    """源标识，要能对上 Job.source。"""

    def list_jobs(self, *, page: int = 1, limit: int = 20) -> list[Job]:
        """拉一页招聘信息。

        契约（三条，和 llm/base.py 的 Provider 是同一套纪律）：
        1. **不重试**。重试属于编排层（M4），否则退避策略会散落各层。
        2. **不吞异常**，但要做【类型归一化】：底层异常一律包成 SourceError 子类。
        3. **只做数据，不做判断**。不要把"这条值不值得投"之类的业务逻辑写进来。

        分页由本方法负责：远端可能一次给全量，本地切片即可。
        """
        ...
