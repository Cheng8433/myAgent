"""状态写入 —— jobs / runs / steps 的读写（PLAN.md §1 架构里的 memory/store.py）。

本模块只有一个中心思想：**每一次写入都必须幂等。**

★ 为什么这不是"顺手加的优化"，而是【前提】：
  M4a 给 side_effect="write" 的工具开了 3 次重试。那意味着
  **同一个写入被执行多次是常态，不是意外** —— 网络抖一下、进程被 kill 在
  一半、模型重试一次，都会让同一批数据到达这里不止一遍。
  于是「先 SELECT 看有没有、再 INSERT」这种写法必然翻车：
  两次重试之间就是经典的竞态，崩在中间还会留下半个状态。
  唯一站得住的写法是 `INSERT ... ON CONFLICT ... DO UPDATE` ——
  一条原子语句，跑 1 遍和跑 10 遍结果相同。

★ 幂等的 **Why** 补一句：它买到的不是"省一次写入"，
  而是「**可以放心重试**」这个能力。没有它，M4a 的那 3 次重试
  就是在往库里灌重复行 —— 重试机制本身反而成了 bug 的来源。

★ 表里其实住着【两类列】，写 upsert 时千万别一视同仁：
  · **历史事实**：一旦写下就不再改 —— `first_seen_run`。
    它的意思是"我第一次见到这条岗位是在哪次 run"，改掉它 = 篡改历史。
  · **当前状态**：每次见到都要刷新 —— title / company / url / location /
    published_at / extras / raw_path / updated_at。岗位信息会变（薪资改了、
    链接换了），这里存的是"现在长什么样"。
  分不清这两类，就回答不了"这条岗位的薪资是什么时候变的"——
  因为最新的值把第一次的值盖掉了，中间发生了什么没人记得。
  一句话：**ON CONFLICT DO UPDATE 的 SET 子句就是这条分界线的落地点。**
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from agent.tools.sources.base import Job

VAR_DIR_ENV = "VAR_DIR"
DEFAULT_VAR_DIR = "var"


def var_dir(root: str | Path | None = None) -> Path:
    """运行时数据的根目录（.env 里的 VAR_DIR，默认 var/）。"""
    load_dotenv()
    return Path(root or os.getenv(VAR_DIR_ENV, DEFAULT_VAR_DIR))


def archive_raw(raw: bytes, *, root: str | Path | None = None) -> str:
    """把原始响应体按【内容】存盘，返回它在 var/ 下的相对路径。

    路径形如 `raw/<sha1>.json` —— 文件名就是内容的 sha1，这叫【内容寻址】。

    ★ 为什么不按 PLAN.md 初稿写成 `raw/<run_id>/<sha1>.json`：
      因为那两段在互相拆台 —— 路径里已经有 sha1 了，再加一层 run_id，
      等于「同一份字节，每天抓一次就多存一份」。实测过：remoteok 第 1 页和
      第 2 页的 raw 是**同一份 564 KB**（远端一次给全量、本地切片），
      按 run 分目录的话，翻 5 页就是同一份存 5 遍；再乘上每天一次 run，
      一年下来是几百 MB 的纯重复。
      而这个目录本来想回答的问题 ——「这份原文是哪次 run 抓的」——
      库里已经答得更好：jobs.first_seen_run + 这次 run 的 started_at。
      **同一个事实有两个出处，早晚会有一个是假的**，所以路径里不再重复记它。
      （代价要一起认下：不能再用 `rm -rf var/raw/<run_id>` 清某次 run 的存档，
        得改成"删掉没有被任何 jobs.raw_path 引用的文件"。这笔账记在这里。）

    ★ 返回值是【相对 var/ 的路径】而不是绝对路径：
      绝对路径写进库里，仓库一搬家、换个 VAR_DIR，全库的 raw_path 全部失效 ——
      而且是静默失效（文件明明在，就是找不到）。读取方一律 `var_dir() / raw_path`。

    ★ 已经存在就不重写：内容寻址下"同名"必然"同内容"，重写纯属浪费 ——
      而且重写会让 mtime 变新，反而掩盖了"这份存档是几天前抓的"这个线索。

    ★ 目录创建用 exist_ok=True：它天然幂等，不用先判断"目录在不在"
      （先判断后创建本身就是个竞态）。
    """
    digest = hashlib.sha1(raw).hexdigest()
    target = var_dir(root) / "raw" / f"{digest}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(raw)
    # as_posix() 而不是 str()：库里存的是"逻辑路径"，
    # 别让 Windows 的反斜杠漏进去 —— 换台机器就读不出来了。
    return target.relative_to(var_dir(root)).as_posix()


def upsert_jobs(
    conn: sqlite3.Connection,
    jobs: Sequence[Job],
    *,
    raw_path: str | None,
    run_id: str,
) -> dict[str, int]:
    """把一页岗位写进库，幂等。返回 {"inserted": n, "updated": m}。

    参数：
      raw_path —— 这一页【共同的】原文存档路径（见 archive_raw）。
                  一页 20 条共享同一份响应体，所以它属于"这一页"而不是"每条"。
      run_id   —— 只用在 first_seen_run 上：新岗位才写它，老岗位不动。

    返回值的 inserted / updated 是给日报用的（"今天新增 5 条"）。
    ★ 怎么分开数：在**同一个事务里**前后各数一次总行数，差值就是新增。
      简单、显然正确、任何 SQLite 版本都能用。
      （另一条路是 `RETURNING first_seen_run` 再和 run_id 比 —— SQLite 3.35+
      才有，本机实测是 3.45.3。两条都行，选前者是因为它不需要读者知道版本。）

    ★ 事务边界：本函数自己 commit。理由见 db.py 里那句"谁开事务谁负责"——
      一页 20 条必须**同生共死**：批量写到第 13 条崩了，前面 12 条要么全在、
      要么全不在。留一半在库里，下次重试就会把 inserted 数错。
    """
    now = datetime.now(UTC).isoformat()
    before = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]

    SQL = """
        INSERT INTO jobs (
            source, source_id, title, company, url, location,
            published_at, extras, raw_path, first_seen_run, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, source_id) DO UPDATE SET
            title         = excluded.title,
            company       = excluded.company,
            url           = excluded.url,
            location      = excluded.location,
            published_at  = excluded.published_at,
            extras        = excluded.extras,
            raw_path      = excluded.raw_path,
            updated_at    = excluded.updated_at
        """
    for job in jobs:
        conn.execute(
            SQL,
            (
                job.source,
                job.source_id,
                job.title,
                job.company,
                job.url,
                # 下面三个可以是 None：NULL 在这里是【有意义的】，不是缺失。
                job.location,
                job.published_at,
                # Job.extras 是 dict，列是 TEXT —— 必须序列化。ensure_ascii=False
                # 是为了让库里直接能读懂中文，排查时不用过一遍解码。
                json.dumps(job.extras, ensure_ascii=False),
                raw_path,
                run_id,
                now,
            ),
        )
    # ★ 计数必须在 commit 【之前】—— 这样 before / after 才真的在同一个事务里，
    #   数字不会被并发的写入者搅浑。（出处：docstring 里那句"在同一个事务里"，
    #   之前把 commit 放在了前面，注释和代码对不上，是我的疏漏。）
    after = conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
    conn.commit()

    inserted = after - before
    return {"inserted": inserted, "updated": len(jobs) - inserted}
