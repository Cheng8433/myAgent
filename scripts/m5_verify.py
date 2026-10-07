"""M5 验收台 —— 状态层：建库 / 约束 / 幂等写入 / 内容寻址存档。

用法：PYTHONUTF8=1 ./.venv/Scripts/python.exe scripts/m5_verify.py

★ 为什么全部离线、全用临时目录：
  这一层要验的东西全是【约束】—— "插第二次会怎样"、"两源同 id 会怎样"。
  约束只有在被撞的时候才看得见，所以这里刻意去撞它。
  库文件和 var/ 都开在系统临时目录里，跑完自动清掉：
  不碰主人的 var/app.db（那里面有真实数据），也不留下垃圾。

★ 每条断言都配了"反面"：光证明"我这条主键拦得住重复"不够，
  还要证明"PLAN 初稿那条主键拦不住" —— 否则你不知道自己躲开了什么坑，
  改回去的时候也就不知道代价。这是这个项目一贯的做法：
  让代码招供，而不是让注释自说自话。

模块位置：src/agent/memory/{db.py, store.py}
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import tempfile
from pathlib import Path

from agent.memory.db import connect, ensure_schema, new_run_id
from agent.memory.store import archive_raw, record_run, store_page, upsert_jobs
from agent.runtime.loop import StepRecord
from agent.tools.sources.base import Job, JobPage

failures = 0


def check(label: str, got: object, want: object, note: str = "") -> None:
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"   {'OK ' if ok else 'BAD'} {label} → {got!r}   期望 {want!r}  {note}")


def _job(source_id: str, title: str, *, source: str = "remoteok") -> Job:
    return Job(
        source=source,
        source_id=source_id,
        title=title,
        company="某公司",
        url=f"https://example.test/{source}/{source_id}",
        extras={"tags": ["python"]},
    )


def _raise(fn) -> str:
    """跑 fn，返回 'IntegrityError' 或 '没抛'。用来断言【这条约束真的挡得住】。"""
    try:
        fn()
    except sqlite3.IntegrityError:
        return "IntegrityError"
    return "没抛"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # ══════════════════════════════════════════════════════════
        #  ① 建库：三张表 + 三条 PRAGMA 真的生效
        # ══════════════════════════════════════════════════════════
        print("\n═══ ① 建库：三张表 + 三条 PRAGMA ═══")
        conn = connect(tmp_path / "app.db")
        ensure_schema(conn)
        # ② 建表幂等 —— 每次启动都会无脑调一次，第二次绝不能炸。
        ensure_schema(conn)

        tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
        check("建出来的表", sorted(tables), ["jobs", "runs", "steps"], "pushes 刻意留给 M8")
        check("foreign_keys", conn.execute("pragma foreign_keys").fetchone()[0], 1, "连接级")
        check("journal_mode", conn.execute("pragma journal_mode").fetchone()[0], "wal", "库级")
        check("busy_timeout", conn.execute("pragma busy_timeout").fetchone()[0], 5000, "毫秒")

        # ══════════════════════════════════════════════════════════
        #  ② 两条主键真的拦得住（每条都带反面对照）
        # ══════════════════════════════════════════════════════════
        print("\n═══ ② 主键：拦得住重复吗（含反面对照）═══")
        conn.execute(
            "insert into runs (run_id, started_at, status) values (?, ?, ?)",
            ("r1", "2026-10-02T00:00:00+00:00", "success"),
        )
        row = ("r1", 0, 1, "llm", "stop", None, 10, 5)

        # ── 反面：PLAN 初稿给 steps 写的主键，含两列 NULL ──
        conn.execute(
            "create table legacy_steps (run_id text, step int, tool text, args text,"
            " primary key (run_id, step, tool, args))"
        )
        for _ in range(2):
            conn.execute("insert into legacy_steps values (?, ?, null, null)", ("r1", 1))
        legacy = conn.execute("select count(*) from legacy_steps").fetchone()[0]
        check(
            "反面：初稿主键 (run_id,step,tool,args)，同一行插两次",
            legacy,
            2,
            "★ 2 行 = 去重失效。SQLite 的主键不拒 NULL，而 llm 步的 tool/args 天然是 NULL",
        )

        # ── 正面：现役主键 (run_id, seq) ──
        conn.execute("insert into steps values (?, ?, ?, ?, ?, ?, ?, ?)", row)
        check(
            "正面：现役主键 (run_id,seq)，同一行插两次",
            _raise(lambda: conn.execute("insert into steps values (?,?,?,?,?,?,?,?)", row)),
            "IntegrityError",
        )

        print()
        # ── 外键：确保 ① 里那条 PRAGMA 不是摆设 ──
        check(
            "野 run_id（外键）",
            _raise(
                lambda: conn.execute(
                    "insert into steps values (?,?,?,?,?,?,?,?)",
                    ("查无此run", 0, 1, "llm", "stop", None, 0, 0),
                )
            ),
            "IntegrityError",
            "不打开 foreign_keys 的话它会照写不误",
        )

        print()
        # ── jobs 的复合主键：跨源共存 + 同源去重 ──
        upsert_jobs(conn, [_job("1", "remoteok 的 1 号")], raw_path=None, run_id="r1")
        inserted = upsert_jobs(
            conn, [_job("1", "ncss 的 1 号", source="ncss")], raw_path=None, run_id="r1"
        )
        check(
            "两个源同 id=1 共存", inserted, {"inserted": 1, "updated": 0}, "只按 job_id 会互相覆盖"
        )
        check(
            "同源同 id 再来一次",
            upsert_jobs(conn, [_job("1", "remoteok 的 1 号")], raw_path=None, run_id="r2"),
            {"inserted": 0, "updated": 1},
        )

        # ══════════════════════════════════════════════════════════
        #  ③ upsert 幂等：历史事实 vs 当前状态
        # ══════════════════════════════════════════════════════════
        print("\n═══ ③ upsert 幂等：第二次不该多出一行 ═══")
        page = [_job("10", "原始标题"), _job("11", "第二条")]
        check(
            "第 1 遍",
            upsert_jobs(conn, page, raw_path="raw/aa.json", run_id="r1"),
            {"inserted": 2, "updated": 0},
        )
        check(
            "第 2 遍（改了标题、换了 run 和 raw）",
            upsert_jobs(
                conn,
                [_job("10", "改过的标题"), _job("11", "第二条")],
                raw_path="raw/bb.json",
                run_id="r2",
            ),
            {"inserted": 0, "updated": 2},
            "返回 {inserted:2} 就说明退化成【先查后插】了",
        )

        check(
            "总行数（job 10/11 各一行）",
            conn.execute("select count(*) from jobs").fetchone()[0],
            4,
            "含前面两源那条",
        )
        got = conn.execute(
            "select first_seen_run, title, raw_path from jobs where source_id = '10'"
        ).fetchone()
        check(
            "★ first_seen_run 没被 r2 覆盖",
            got["first_seen_run"],
            "r1",
            "历史事实：第一次见到它是在哪次 run",
        )
        check("title 被刷新成新值", got["title"], "改过的标题", "当前状态：每次都刷")
        check("raw_path 被刷新成新值", got["raw_path"], "raw/bb.json", "当前状态：每次都刷")

        # 第三条新来的，账要能分开数
        check(
            "老两条 + 一新条",
            upsert_jobs(conn, [*page, _job("12", "新来的")], raw_path="raw/cc.json", run_id="r3"),
            {"inserted": 1, "updated": 2},
            "inserted 是留给日报说【今天新增几条】的",
        )

        # ══════════════════════════════════════════════════════════
        #  ④ 内容寻址：同一份字节只落一次盘
        # ══════════════════════════════════════════════════════════
        print("\n═══ ④ archive_raw：内容寻址 ═══")
        body = b'[{"position": "x"}]'
        p1 = archive_raw(body, root=tmp_path)
        f1 = tmp_path / p1
        mtime1 = f1.stat().st_mtime_ns
        p2 = archive_raw(body, root=tmp_path)
        check(
            "同一份内容两次调用",
            (p1 == p2, f1.stat().st_mtime_ns == mtime1),
            (True, True),
            "路径相同，且【没有重写】—— mtime 没变就是没重写",
        )
        check("文件名就是 sha1", Path(p1).name, f"{hashlib.sha1(body).hexdigest()}.json")
        check(
            "路径形状：相对 var/、正斜杠",
            (p1, Path(p1).is_absolute(), "\\" in p1),
            (f"raw/{hashlib.sha1(body).hexdigest()}.json", False, False),
            "绝对路径进库，仓库一搬家就全库失效",
        )

        p3 = archive_raw(b'[{"position": "y"}]', root=tmp_path)
        check("换一份内容", p3 != p1, True)
        check("磁盘上的文件数", len(list((tmp_path / "raw").iterdir())), 2, "同内容的没有存第二遍")

        # ══════════════════════════════════════════════════════════
        #  ⑤ new_run_id
        # ══════════════════════════════════════════════════════════
        print("\n═══ ⑤ new_run_id：同秒也不能撞 ═══")
        a, b = new_run_id(), new_run_id()
        check(
            "同一瞬间连开两个",
            a != b,
            True,
            "它同时是 runs 的主键、steps 的外键、jobs.first_seen_run —— 撞了就是静默覆盖",
        )
        check("格式 20260930T143012-a1b2", bool(re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{4}", a)), True)

        # ══════════════════════════════════════════════════════════
        #  ⑥ store_page：一次抓取被消费掉 —— 原文和记录必须成对出现
        # ══════════════════════════════════════════════════════════
        print("\n═══ ⑥ store_page：存档与入库是一件事的两半 ═══")
        page = JobPage(
            jobs=[_job("90", "第九十条")],
            raw=b'[{"position": "90"}]',
        )
        rid = new_run_id()
        got = store_page(conn, page, run_id=rid, root=tmp_path)
        check(
            "返回值就是 upsert 的账", got, {"inserted": 1, "updated": 0}, "store_page 不另造一份账"
        )

        row = conn.execute("select raw_path from jobs where source_id = '90'").fetchone()
        check("库里存的是相对路径", row["raw_path"].startswith("raw/"), True)
        check(
            "★ 那个路径【真的指得到文件】",
            (tmp_path / row["raw_path"]).read_bytes(),
            page.raw,
            "这才叫【两半是一件事】—— 库里的记录能找回它的原文",
        )
        check(
            "同一页再存一遍（新 run）",
            store_page(conn, page, run_id=new_run_id(), root=tmp_path),
            {"inserted": 0, "updated": 1},
            "raw 内容没变 → sha1 没变 → 磁盘上不多文件",
        )
        # ★ 这里数的是 3 不是 2：④ 已经写了 2 份（x / y），⑥ 的第一句又写进第 3 份。
        #   这一行真正要卡的是【再存一遍没有变成 4】—— 卡 2 是把自己的账算错了，
        #   断言就会在代码正确时变红（假红灯，和假绿灯一样费时间）。
        check(
            "磁盘上的 raw 文件数（同上一次一样）",
            len(list((tmp_path / "raw").iterdir())),
            3,
            "3 = ④ 的 2 份 + ⑥ 的第 3 份；再存一遍没多出来",
        )

        # ══════════════════════════════════════════════════════════
        #  ⑦ record_run：一次工作流的结局与留痕
        # ══════════════════════════════════════════════════════════
        print("\n═══ ⑦ record_run：结局与留痕 ═══")
        # ★ 这里【不再】造 RunResult：record_run 收显式字段 ——
        #   runs 表属于【工作流】，和 ReAct 子循环的 RunResult 不是一回事：
        #   子循环的词表里没有"列表为空"这种结局，而工作流有。
        steps = (
            StepRecord(
                step=1, kind="llm", detail="tool_calls", prompt_tokens=100, completion_tokens=10
            ),
            StepRecord(step=1, kind="tool", detail="fetch_job_list", ok=False),
            StepRecord(
                step=2, kind="llm", detail="tool_calls", prompt_tokens=120, completion_tokens=12
            ),
        )
        started = "2026-10-02T03:00:00+00:00"

        def write_run() -> None:
            """同一次 run、同样的参数写一遍。调两次就是幂等测试。"""
            record_run(
                conn,
                run_id=rid,
                status="success",
                steps=steps,
                prompt_tokens=220,
                completion_tokens=22,
                started_at=started,
            )

        write_run()
        r = conn.execute("select * from runs where run_id = ?", (rid,)).fetchone()
        check("runs.status", r["status"], "success", "工作流词表：success / failed / no_items")
        check("runs.steps = 记录条数", r["steps"], 3, "llm 与 tool 都算一步留痕")
        check("runs 的 token 是累计值", (r["prompt_tokens"], r["completion_tokens"]), (220, 22))
        check("runs.started_at 是历史事实", r["started_at"], started, "重跑不该篡改起点")

        rows = conn.execute(
            "select seq, step, kind, detail from steps where run_id = ? order by seq", (rid,)
        ).fetchall()
        check(
            "steps 的 seq 与 step 【不是同一回事】",
            [(x["seq"], x["step"]) for x in rows],
            [(0, 1), (1, 1), (2, 2)],
            "★ 一轮点 3 个工具 → 3 条记录共用 step=1；拿 step 当主键会当场撞",
        )

        # 幂等：整段替换，不是追加
        write_run()
        check(
            "重跑一遍（应整段替换）",
            conn.execute("select count(*) from steps where run_id = ?", (rid,)).fetchone()[0],
            3,
            "追加的话这里会变成 6",
        )

        # 反面：拿 step 当主键会怎样 —— 前两条记录的 step 都是 1，它们【共存于库里】
        check(
            "反面：step=1 的记录在库里占了",
            sum(1 for x in rows if x["step"] == 1),
            2,
            "★ 两条一步共用一个 step 是【正常的】—— 所以 step 当不了主键，seq 这列不是多余的",
        )

        conn.close()

    print()
    if failures:
        print(f">>> {failures} 项不符。")
    else:
        print("✅ M5 通过：建库、约束、幂等写入、内容寻址存档都守住了（全程离线）。")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
