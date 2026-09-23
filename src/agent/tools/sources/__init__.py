"""数据源工厂 —— 「切换只改 .env」这句话的落点。

调用方永远写 `get_source()`，不写 `RemoteOkSource()`。
这样 JOB_SOURCE 一改，上层代码一行不动。

★ 分支里做【惰性 import】：用 remoteok 时不必 import ncss，
  进而也不必 import playwright。依赖越晚引入，启动越轻、耦合越少。
"""

import os

from agent.tools.sources.base import Job, JobSource, LoginExpired, SourceError

__all__ = ["Job", "JobSource", "LoginExpired", "SourceError", "get_source"]


def get_source() -> JobSource:
    """按 .env 的 JOB_SOURCE 造一个数据源。"""
    name = os.environ.get("JOB_SOURCE", "remoteok").strip().lower()

    if name == "remoteok":
        from agent.tools.sources.remoteok import RemoteOkSource

        return RemoteOkSource()

    if name == "ncss":
        # 这个模块要到 M2b 才写。在写出来之前，跑它会 ImportError —— 这是预期的。
        from agent.tools.sources.ncss import NcssSource

        return NcssSource()

    raise ValueError(f"未知的 JOB_SOURCE：{name!r}（可选：remoteok / ncss）")
