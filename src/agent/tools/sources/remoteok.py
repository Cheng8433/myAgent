"""remoteok.com 数据源 —— 开发期替身（PLAN.md §2.2）。"""

import httpx

from agent.tools.sources.base import Job, JobPage, SourceError, TransientSourceError

API_URL = "https://remoteok.com/api"

USER_AGENT = "myAgent-learning/0.1"


class RemoteOkSource:
    name = "remoteok"

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def list_jobs(self, *, page: int = 1, limit: int = 20) -> JobPage:
        try:
            resp = httpx.get(
                API_URL,
                timeout=self._timeout,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.TransportError as exc:
            raise TransientSourceError(f"remoteok 网络抖动，可以重试：{exc}") from exc
        except httpx.HTTPStatusError as status_err:
            status_code = status_err.response.status_code
            if status_code in (429, 408) or 500 <= status_code < 600:
                raise TransientSourceError(
                    f"HTTP {status_code}：remoteok 服务器异常，可以重试"
                ) from status_err
            raise SourceError(
                f"HTTP {status_code}：remoteok 访问异常，不可重试：{status_err}"
            ) from status_err
        except httpx.HTTPError as error:
            raise SourceError(f"网络连接异常，请查看日志：{error}") from error
        body = resp.content
        payload = resp.json()
        rows = [r for r in payload if r.get("position") and r.get("id")]

        offset = (page - 1) * limit
        rows = rows[offset : offset + limit]
        jobs: list[Job] = []
        for row in rows:
            title = (row.get("position") or "").strip()
            if not title:
                continue
            location = (row.get("location") or "").strip() or None
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(row.get("id")),
                    title=title,
                    company=(row.get("company") or "").strip(),
                    url=(row.get("url") or "").strip(),
                    location=location,
                    published_at=row.get("date"),
                    extras={
                        "tags": row.get("tags"),
                        "salary_min": row.get("salary_min"),
                        "salary_max": row.get("salary_max"),
                    },
                )
            )
        return JobPage(jobs=jobs, raw=body)
