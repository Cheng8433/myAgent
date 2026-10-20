"""招聘数据源抽象 —— 和 agent/llm/base.py 是同一套套路。"""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Job:
    """一条招聘信息的【归一化】形状。"""

    source: str
    """来源标识："remoteok" / "ncss"。入库去重时和 source_id 一起做主键。"""

    source_id: str
    """源站自己的主键（remoteok 是 id，ncss 是 jobId）。"""

    title: str
    company: str
    url: str
    """能直接点开的职位页。remoteok 实测 url 与 apply_url 相同，取 url。"""

    location: str | None = None
    published_at: str | None = None
    """ISO8601 字符串，统一带 UTC 偏移"""

    extras: dict[str, Any] = field(default_factory=dict)
    """源独有的字段（remoteok: tags / salary_min / salary_max）。"""


@dataclass(frozen=True)
class JobPage:
    """一页招聘信息 —— 连同【产出它的原始响应体】一起交出来。"""

    jobs: list[Job]
    raw: bytes
    """这次抓取拿到的原始响应体。上层负责把它写进 var/raw/，数据源层不碰磁盘。"""


class SourceError(Exception):
    """数据源层的一切失败。"""


class LoginExpired(SourceError):
    """登录态不可用 —— 过期了，或者本机压根还没登录过。"""


class TransientSourceError(SourceError):
    """【瞬时】故障：超时、连接中断、5xx。"""


class SourceUnreachable(TransientSourceError):
    """站点【根本没被查询到】—— DNS / 连接 / TLS 握手就失败了，一个 HTTP 响应都没拿到。"""

    pages_done: int = 0
    """在它之前【已经成功抓完】的页数。数据源层总是留 0，由翻页层补。"""


class JobSource(Protocol):
    """所有数据源实现都要满足的协议（鸭子类型，无需继承）。"""

    name: str
    """源标识，要能对上 Job.source。"""

    def list_jobs(self, *, page: int = 1, limit: int = 20) -> JobPage:
        """拉一页招聘信息，连同原始响应体一起返回。"""
        ...


def close_source(source: JobSource) -> None:
    """关闭一个数据源；没有 `close()` 的实现是 no-op。"""
    close = getattr(source, "close", None)
    if close is not None:
        close()
