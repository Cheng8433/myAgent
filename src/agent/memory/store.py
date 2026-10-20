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
from typing import TYPE_CHECKING

from dotenv import load_dotenv

from agent.tools.sources.base import Job, JobPage

if TYPE_CHECKING:
    # ★ 只给类型检查器看的 import：运行时【不】依赖 runtime。
    #   状态层不该反过来依赖编排层 —— 那样 memory ↔ runtime 就成环了。
    #   这里用得到 StepRecord 的只是函数签名，`from __future__ import annotations`
    #   会把注解变成字符串，运行时根本不会去求值它。
    #   （M4b 之前这里是 RunResult。改成 StepRecord 的原因见 record_run 的
    #     docstring：runs 表属于【工作流】，不该被 ReAct 子循环的词表绑住。）
    from agent.runtime.loop import StepRecord

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


def store_page(
    conn: sqlite3.Connection,
    page: JobPage,
    *,
    run_id: str,
    root: str | Path | None = None,
) -> dict[str, int]:
    """把【一次抓取】消费掉：原文存档 + 岗位入库。返回 upsert_jobs 那笔账。

    ★ 为什么这两件事必须收在一个函数里：
      它们是"这次抓取留下了什么"的两半。拆开写不会报错 —— 只会让库和原文对不上
      （库里有这条岗位，翻原文却找不到它是从哪儿解析出来的），而且没有任何提示。
      收在一处，就没有"忘了一半"这个可能。
      （这正是 PLAN.md §M4 主流程里 `ctx.call("upsert_jobs", items)` 那一步：
        由【编排层】调用，跑在主线程上 —— 所以 run_id 就在手边。
        工具层不碰这个：模型可以决定"去抓第 3 页"，但"这次抓取算哪次 run 的、
        要不要留档"不是它该管的事。工具是模型的手，编排是系统的手。）

    ★ 顺序（先存档、后入库）是被依赖关系定死的，不是风格：
      raw_path 要作为参数传给 upsert_jobs。反过来写就得先入库、再回填 raw_path ——
      中间那段时间库里挂着一个指不到东西的路径。

    ★ root 是给测试用的（临时目录），生产路径走 VAR_DIR，见 archive_raw。
    """
    raw_path = archive_raw(page.raw, root=root)
    return upsert_jobs(conn, page.jobs, raw_path=raw_path, run_id=run_id)


def ran_since(
    conn: sqlite3.Connection,
    *,
    since: str,
    statuses: Sequence[str],
) -> bool:
    """`since` 之后有没有过这几种 status 的 run？—— "一天一次"的判据。

    ★ 为什么这个函数住在 memory/ 而不是 workflows/：
      它只认识 runs 表（SQL + 参数），不认识"日报"、不认识 ncss。
      把它放进编排层，等于让"一天一次"这条策略和数据访问粘在一起，
      将来想用别的条件查 runs 就得再写一遍 SQL。分层判据：
      这里回答"库里有没有"，编排层回答"要不要因此跳过"。

    ★ `datetime()` 两边都包：runs.started_at 是 ISO8601（带 +00:00），
      而 since 也按 ISO 传进来。SQLite 的字符串比较在"日期格式不同"时
      会悄悄错一格 —— 用 datetime() 归一化成同一种形状再比，
      边界（正好 00:00:00）才不会因为多一个 'T' 或少一个时区后缀而判错。
      （M6 在日期比较上踩过同一条坑。）

    ★ SQL 由代码构造、值全走占位符（PLAN §M8 第 3 条）：
      statuses 的【个数】决定占位符个数，但每一个值仍然绑定在 `?` 上。
      直接拼 status 字符串会把"调用方传进来的词表"变成 SQL 的一部分，
      而这个函数的调用方是代码、也不是模型 —— 纪律仍然一样：
      **能参数化就参数化，别给下一个改动留一个可以偷懒的形状。**
    """
    placeholders = ", ".join(["?"] * len(statuses))
    sql = (
        "SELECT 1 FROM runs "
        "WHERE datetime(started_at) >= datetime(?) "
        f"AND status IN ({placeholders}) "
        "LIMIT 1"
    )
    return conn.execute(sql, (since, *statuses)).fetchone() is not None


def record_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    steps: Sequence[StepRecord],
    prompt_tokens: int,
    completion_tokens: int,
    started_at: str,
    error: str | None = None,
) -> None:
    """把一次 run 的结局写进 runs（一行）+ steps（一条 StepRecord 一行）。

    ★ 为什么收【显式字段】而不是一个 RunResult（M4b 改的，原来是后者）：
      `RunResult.status` 是 **ReAct 子循环**的封闭词表
      （success / loop_detected / max_steps_exceeded / …）；
      而 runs 表记的是**工作流**那一次 run
      （PLAN 的词表：success / failed / no_items，将来还有 login_expired）。
      两者不是一回事 —— 一次日报 run 里可能跑 0~2 次 ReAct 子 run，
      而且它有循环根本没有的结局（"列表为空"循环压根没被调用过）。
      硬把子循环的 RunResult 塞进 runs，等于在最需要准确的表上让类型撒谎。
      所以：**runs 表只由工作流写**，子 run 的结局不丢，它以一条
      `StepRecord(kind="subrun", detail=<子 run 的 status>)` 落进 steps。

    ★ 幂等靠"整段替换"：先按 run_id 删掉旧的 steps，再整批写回。
      为什么不像 jobs 那样 ON CONFLICT DO UPDATE 逐行处理 ——
      因为语义不同：jobs 是"这条岗位现在长什么样"（逐条刷新），
      而 steps 是"这次 run 一共发生了这些事"（一个整体）。整体就该整体替换，
      逐行 upsert 反而会在"这次比上次少了 3 步"时留下 3 条孤儿。

    ★ runs.error 从 M6 起【有值了】：调用方把失败原因原文递进来（见 error 参数）。
      在那之前它一直写 NULL。留空是诚实的（"还没有"），填 "" 是撒谎
      （"什么都没有"）—— 所以这里对"没给"与"给了空串"区别对待：
      没给就写 NULL，给了（哪怕空串）就照写。判据是 `is None`，不是真假。

    ★ steps 的 latency_ms / error 两列也是 M6 补的（列本身见 db.py 的 SCHEMA
      与 MIGRATIONS）。它们是"能用 SQL 聚合的那部分"—— 时延画曲线、
      错误代号筛选 —— 而 model / tool_calls / args_digest 那种"看一眼就够"的
      细节不进库，只进 trace（判据见 StepRecord 的 docstring）。

    断言在 scripts/m5_verify.py 的第 ⑦ 节、scripts/m6_verify.py —— 那儿就是反馈回路。
    """
    finished_at = datetime.now(UTC).isoformat()
    conn.execute(
        """
        INSERT INTO runs (
            run_id, started_at, finished_at, status, steps,
            prompt_tokens, completion_tokens, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (run_id) DO UPDATE SET
            finished_at       = excluded.finished_at,
            status            = excluded.status,
            steps             = excluded.steps,
            prompt_tokens     = excluded.prompt_tokens,
            completion_tokens = excluded.completion_tokens,
            error             = excluded.error
        """,
        (
            run_id,
            started_at,
            finished_at,
            status,
            len(steps),
            prompt_tokens,
            completion_tokens,
            error,
        ),
    )
    # ★ 这里【不】把 started_at 放进 DO UPDATE 的 SET：
    #   和 jobs.first_seen_run 是同一条纪律 —— "什么时候开的跑"是历史事实，
    #   第一次记下就不再改。重跑同一个 run_id 只该刷新结局，不该篡改起点。

    # ★ steps 用【整段替换】：先按 run_id 删掉旧的，再整批写回。
    #   为什么不像上面 runs 那样只靠 ON CONFLICT —— 语义不同：
    #   runs 是"一次 run 只有一行"，冲突就是同一行，DO UPDATE 天然正确；
    #   而 steps 是"这次 run 一共发生了这些事"，条数会变。若只逐行 upsert，
    #   这次比上次【少】几步时多出来的旧行没人删 —— 重跑一次，库里躺着两次
    #   run 的混合体，而且它看上去还挺完整。
    #
    # ★ DELETE 与下面的 INSERT 必须落在【同一个事务】里（提交统在末尾做）：
    #   分开提交的话，"删完、还没写"那个窗口里进程被 kill，steps 就空了，
    #   而 runs.steps 那个计数还写着 3 —— 库里躺着一个自相矛盾的状态。
    #
    # ★ seq 取 enumerate 的【下标】，不是 record.step：
    #   一轮里模型可能点 3 个工具，那 3 条留痕的 step 都是 1，
    #   而 steps 的主键是 (run_id, seq) —— 拿 step 当 seq 会当场 IntegrityError。
    #   step 回答"属于第几轮"，seq 回答"在这张表里排第几"，两个问题两把尺子。
    conn.execute("DELETE FROM steps WHERE run_id = ?", (run_id,))
    conn.executemany(
        """
        INSERT INTO steps (
            run_id, seq, step, kind, detail, ok,
            prompt_tokens, completion_tokens, latency_ms, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_id,
                seq,  # ← enumerate 的下标，从 0 开始；不是 record.step
                record.step,
                record.kind,
                record.detail,
                record.ok,  # bool | None 原样交出 → sqlite3 存成 1 / 0 / NULL
                record.prompt_tokens,  # 单轮值，别传累计值
                record.completion_tokens,
                record.latency_ms,
                record.error,
            )
            for seq, record in enumerate(steps)
        ],
    )

    conn.commit()
