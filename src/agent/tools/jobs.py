"""第一个真工具 —— 把「数据源」包成「模型能调的函数」。"""

from dataclasses import asdict
from typing import Any

from pydantic import BaseModel, Field

from agent.runtime.retry import RetryPolicy, policy_for, retry_call
from agent.tools.registry import TOOLS, tool
from agent.tools.sources import LoginExpired, SourceError, close_source, get_source
from agent.tools.sources.base import JobPage, JobSource


class FetchListArgs(BaseModel):
    page: int = Field(1, ge=1, le=50, description="页码，从 1 开始")


def _fetch_policy() -> RetryPolicy:
    """取【取列表】这个操作的重试策略 —— 从工具自己的声明推导，不手抄。"""
    return policy_for(TOOLS["fetch_job_list"].side_effect)


def fetch_page(page: int = 1, *, source: JobSource | None = None) -> JobPage:
    """取一页招聘信息，连同原始响应体一起返回。异常原样上抛；source 供测试注入。"""
    src = source or get_source()
    try:
        return retry_call(lambda: src.list_jobs(page=max(1, page)), policy=_fetch_policy())
    finally:
        if source is None:
            close_source(src)


@tool(FetchListArgs, side_effect="read", timeout=35.0)
def fetch_job_list(page: int = 1) -> dict[str, Any]:
    """拉取招聘信息列表的指定页；items 为空表示已翻到底。
    返回 {"ok", "items", "count", "error", "hint"}，每项不含职位描述全文。"""
    try:
        job_page = fetch_page(page)
        items = [asdict(job) for job in job_page.jobs]

        return {
            "ok": True,
            "items": items,
            "count": len(items),
            "error": None,
            "hint": None,
        }

    except LoginExpired as exc:
        return {
            "ok": False,
            "items": [],
            "count": 0,
            "error": "login_expired",
            "hint": (
                "登录态不可用（已过期，或本机还没有登录过），"
                f"请运行 python -m app.main login 手动续期。（原因：{exc}）"
            ),
        }
    except SourceError as exc:
        return {
            "ok": False,
            "items": [],
            "count": 0,
            "error": "source_error",
            "hint": str(exc),
        }
