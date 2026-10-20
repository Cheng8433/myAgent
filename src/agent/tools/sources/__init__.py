"""数据源工厂 —— 「切换只改 .env」这句话的落点。"""

import os

from agent.tools.sources.base import (
    Job,
    JobSource,
    LoginExpired,
    SourceError,
    SourceUnreachable,
    TransientSourceError,
    close_source,
)

__all__ = [
    "Job",
    "JobSource",
    "LoginExpired",
    "SourceError",
    "SourceUnreachable",
    "TransientSourceError",
    "close_source",
    "get_source",
]


def get_source() -> JobSource:
    """按 .env 的 JOB_SOURCE 造一个数据源。"""
    name = os.environ.get("JOB_SOURCE", "remoteok").strip().lower()

    if name == "remoteok":
        from agent.tools.sources.remoteok import RemoteOkSource

        return RemoteOkSource()

    if name == "ncss":
        from agent.tools.sources.ncss import NcssSource

        return NcssSource()

    raise ValueError(f"未知的 JOB_SOURCE：{name!r}（可选：remoteok / ncss）")
