"""状态库的连接与建表 —— M5 的地基（PLAN.md §M5 / §1 架构里的 memory/db.py）。

★ 第一件事：持久化状态 ≠ 把 messages 存起来。
  存 messages 是「把过程抄一遍」，它回答不了任何一个运维问题。真正要存的只有两样：

    · 进度      —— 跑到哪一步了（runs / steps）。用来断点续跑、算成本。
    · 幂等凭证  —— 这件事【做过了】（pushes，M8 才写）。

  判据很好记：**重启之后你要问自己的那个问题，决定了要存什么。**
  「上次跑到哪了？」→ runs/steps；「这条岗位推过了吗？」→ pushes。
  问不出来的状态就别存 —— 存了也没人读（这正是 M4a 那条「声明了但没人消费」）。

★ 第二件事：写入必须幂等，这不是优化，是前提。
  M4a 给 side_effect="write" 的工具开了 3 次重试（见 runtime/retry.py 的策略表）。
  那意味着 **同一个写入被执行多次是常态，不是意外**。
  「先查后插」（SELECT 完再 INSERT）在这种前提下必然翻车：两次重试之间就是
  经典的竞态，而崩在中间还会留下半个状态。所以本库只认一种写法：
  `INSERT ... ON CONFLICT ... DO UPDATE` —— 一条原子语句，跑 1 遍和跑 10 遍同结果。
  **幂等是重试的地基**：没有它，M4a 那 3 次重试就是在往库里灌重复数据。

★ 第三件事：schema 里有两个【实测踩出来的】坑，都不是风格问题。
  1) SQLite 的 PRIMARY KEY 【不拒 NULL】。
     实测：往 `PRIMARY KEY (run_id, step, tool, args)` 的表里插两条
     ('r', 1, NULL, NULL)，两条都进去了，去重当场失效。
     而 PLAN.md 初稿给 steps 写的就是这个主键 —— 因为 llm 步的 tool/args
     天然是 NULL（llm 不调工具，没有工具名）。改用 (run_id, seq)。
  2) 岗位的主键不能只用站点侧的 id。remoteok 和 ncss 各自从 1 开始编号，
     两条【不同源】的岗位会撞成同一行、互相覆盖。
     必须 (source, source_id) —— sources/base.py 把 source 和 source_id
     分成两列，理由就在这里。

★ 第四件事：连接是有线程归属的，别做成全局单例。
  实测：在主线程建的连接，拿到别的线程里用 → `ProgrammingError: SQLite objects
  created in a thread can only be used in that same thread`。
  而 loop.py 的 `_call_tool` 是【在工作线程里】执行工具的（那是超时的代价），
  于是 M5b 的 upsert_jobs 跑在哪个线程、连接就得在哪个线程建。
  解法到 M5b 再定（每次操作建连 / threading.local），但纪律现在就立：
  **连接是短命对象。** 顺带说一句，`check_same_thread=False` 能绕过这个报错，
  但它是把安全网拆掉去换方便 —— 真正的多线程共享连接要另外一套做法，不要图省事。

⚠️ 纪律：本模块只管【连接与建表】，不认识 Job、也不认识 RunResult。
   业务读写归 store.py，Job 的归一化归 tools/sources/base.py。
   分层的收益在 M6 兑现：trace 和看板直接查库，谁也不用 import 谁的业务类型。
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

DB_PATH_ENV = "DB_PATH"
DEFAULT_DB_PATH = "var/app.db"
"""变量名与默认值都照抄 .env.example，不在代码里另起一套。

★ 为什么默认值写成 "var/app.db" 而不是拼 VAR_DIR + 文件名：
  两个来源拼一个路径，就会出现「改了 VAR_DIR 却发现库还在老地方」
  （因为 .env 里 DB_PATH 是显式的，它说了算）—— 同一件事有两个出处，
  必然有一个是假的。让 DB_PATH 一个人说了算。
"""


def new_run_id(*, now: datetime | None = None) -> str:
    """生成一次 run 的标识：UTC 时间戳 + 4 位随机十六进制，如 `20260930T143012-a1b2`。

    ★ 为什么不能只用时间戳：
      它是 runs.run_id 的主键，也是 var/raw/<run_id>/ 的目录名。同一秒起两次
      run（跑测试、手动重跑、定时任务和手跑撞车）就会撞 —— 而主键撞了不报错，
      是**静默覆盖**：上一次 run 的记录被这一次顶掉，于是「上次为什么失败」
      这条最重要的线索，恰好被这次覆盖弄丢了。
    ★ 为什么是 4 位十六进制（65536 种）而不是 uuid4 全长：
      它要进目录名、要进日志、要被人手动抄进命令里。可读可抄比极限防撞更重要 ——
      只有同秒并发才需要它，4 位足够；真撞了概率是 1/65536，后果也只是覆盖。
    ★ 为什么用 UTC 而不是本地时间：
      全库时间统一 UTC（runs.started_at / jobs.published_at 都是），
      run_id 跟着走，免得同一份数据里出现两种时区还得猜。
    """
    moment = now or datetime.now(UTC)
    return f"{moment.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(2)}"


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开（必要时创建）库文件，把三条 PRAGMA 设好，返回连接。

    ★ 三条 PRAGMA 不是可选项，各有各的病要治：
      1) foreign_keys = ON   —— SQLite 默认【关】外键。不打开的话，steps.run_id
         指向一个不存在的 run 也照写不误，脏数据静默累积。
         ★ 它是【连接级】的：不是写进库文件的属性，每开一个新连接都要重设。
           这条最容易忘 —— 因为"上一次明明设过"。
      2) journal_mode = WAL  —— 读写不互斥。M6 的看板在跑的时候日报还能写，
         否则"一边写一边读"直接 database is locked。
         ★ 它是【库级】的：设一次就留在库里，新连接自动继承。和上一条正好相反，
           所以两条不能用同一种记忆方式记。
         ⚠️ 实测的坑：WAL 不能在事务里改。改不动时它【不报错】，只是静静返回
            当前值（'delete'）。而 Python 的 sqlite3 在第一条 INSERT/UPDATE/
            DELETE 之前就会开隐式写事务 —— 所以「先写一批数据，再顺手设个 WAL」
            是静默失效的。**建连时第一时间设，赶在任何 DML 之前。**
      3) busy_timeout        —— 撞锁时等一会儿再放弃，而不是当场抛 OperationalError。
         （实测：一个没提交的写事务足以让另一个连接连 PRAGMA 都改不动。）
         进程可能被 kill 在写着的时候，等几秒远比"日报因为一次锁冲突整份废掉"便宜。

    ★ row_factory = sqlite3.Row：让 `row["run_id"]` 能按列名取。
      否则只能按下标 row[3] —— 而 schema 一改下标全错，且不报错，只是取错列。

    ★ 本函数【不 commit】任何东西：谁开事务谁负责（见 ensure_schema）。
      把 commit 藏进建连函数，会让调用方以为自己开着事务，实际早就提交了。

    ★ 线程归属见模块注释第四条：这个连接只能在【建它的那个线程】里用。
    （断言待补进 scripts/m5_verify.py —— M5d 那一节。）
    """
    load_dotenv()
    path = Path(db_path or os.getenv(DB_PATH_ENV, DEFAULT_DB_PATH))
    # var/ 可能还不存在（刚 git clone 下来就是这样），而 sqlite3.connect 不会建目录。
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # ★ 顺序也是契约的一部分：三行必须在【任何 DML 之前】。
    #   这个函数目前只有建连、没有 DML，所以顺序天然安全 ——
    #   但谁要是哪天在 return 之前插了一句 INSERT，journal_mode 会静静失效

    conn.execute("PRAGMA foreign_keys  = ON ")
    conn.execute("PRAGMA journal_mode  = WAL ")
    conn.execute("PRAGMA busy_timeout  = 5000 ")

    return conn


SCHEMA = """
  CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL,
    steps             INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    error             TEXT
  );

  CREATE TABLE IF NOT EXISTS steps (
    run_id            TEXT NOT NULL,
    seq               INTEGER NOT NULL,
    step              INTEGER NOT NULL,
    kind              TEXT NOT NULL,
    detail            TEXT NOT NULL DEFAULT '',
    ok                INTEGER,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
  );

  CREATE TABLE IF NOT EXISTS jobs (
    source         TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    title          TEXT NOT NULL,
    company        TEXT NOT NULL DEFAULT '',
    url            TEXT NOT NULL DEFAULT '',
    location       TEXT,
    published_at   TEXT,
    extras         TEXT NOT NULL DEFAULT '{}',
    raw_path       TEXT,
    first_seen_run TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
  );
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """把 SCHEMA 灌进库。幂等 —— 每次启动都可以无脑调一次。

    ★ 用 executescript 而不是 execute：前者能一次跑多条语句（含注释与分号），
      后者只认一条。
      代价是 executescript 会先隐式 COMMIT 掉当前事务 —— 所以它【只适合建表】
      这种"本来就不需要和别的操作同一个事务"的场合，别拿它包业务写入。
    """
    conn.executescript(SCHEMA)
    conn.commit()
