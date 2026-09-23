"""remoteok.com 数据源 —— 开发期替身（PLAN.md §2.2）。

零登录、零对抗、零风险，M1~M3 用它快速迭代；M4 换 ncss 时上层无需改动。

2026-09-20 本机实测（已确认，不必再猜）：
  · 响应约 627 KB，顶层是 list，长度 100
  · ★ 但 raw[0] 【不是职位】，是 {"last_updated":..., "legal":...} 的声明对象
    → 真实职位只有 99 条。不跳过它，数据里就混进一条脏数据。
  · raw[1] 的字段：apply_url, company, company_logo, date, description,
    epoch, id, location, logo, position, salary_max, salary_min, slug, tags, url
  · id 是【字符串】"1137408"；date 已是带 UTC 偏移的 ISO8601
  · location 有时是城市名（"Cincinnati"），有时是空串

★ 声明对象一共挡了两道：上面这行列表推导按【内容】筛（而不是硬编码 raw[1:]，
  这样对方哪天多塞一个声明对象也不会漏），下面映射循环里的 `if not title` 是第二道。
两道都在是有意的纵深防御 —— 但每改一道，记得确认另一道不依赖它的副作用。
"""

import httpx

from agent.tools.sources.base import Job, SourceError

API_URL = "https://remoteok.com/api"

# 带一个诚实的 User-Agent：说明自己是谁、干什么用的。
# PLAN.md 原则三/§2.2 都提到，remoteok 的 ToS 要署名回链 ——
# 个人自用不外发可以先不管，但"表明身份"这个习惯要养成。
USER_AGENT = "myAgent-learning/0.1"


class RemoteOkSource:
    name = "remoteok"

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def list_jobs(self, *, page: int = 1, limit: int = 20) -> list[Job]:
        try:
            resp = httpx.get(
                API_URL,
                timeout=self._timeout,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            # ★ 异常归一化：httpx 的类型不许漏到上层（契约第 2 条）。
            #   包成 SourceError 之后，上层不必 import httpx 就能处理失败。
            raise SourceError(f"remoteok 请求失败：{exc}") from exc

        raw = resp.json()
        rows = [r for r in raw if r.get("position") and r.get("id")]

        # 完成分页
        offset = (page - 1) * limit
        rows = rows[offset : offset + limit]
        # 源站的字段名和我们不同（position → title，date → published_at），下面逐项映射。
        # 取值一律走 .get()：这是外部输入，少一个字段不该让整份日报崩掉（PLAN.md 原则二）。
        jobs: list[Job] = []
        for row in rows:
            title = (row.get("position") or "").strip()
            # 第二道防线：万一上面的列表推导被改动，这里仍能挡住空标题的脏数据。
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
        return jobs
