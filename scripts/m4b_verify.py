"""M4b 验收台 —— 确定性主流程：翻页 / 两阶段 / 渲染 / 落盘 / 落库。

用法：PYTHONUTF8=1 ./.venv/Scripts/python.exe scripts/m4b_verify.py

★ 为什么全程离线、假数据源 + 假 LLM + 临时目录：
  M4b 要验的是【编排的形状】，不是模型或站点的脾气 ——
  "抓第 3 页失败时库动没动"这个问题，答案必须每次都一样。
  假 fetch_page 每次返回剧本里写好的页，还记下自己被问过哪些页码，
  于是"循环有没有在该停的地方停"变成一个可断言的事实，而不是感觉。
  库文件与 var/ 都开在系统临时目录里：不碰主人的 var/app.db，跑完自动清。

★ 每一节的断言都尽量钉【一条只能靠理解才写得对的东西】，而不是"跑通了"：
  例如 ③ 钉的是"失败 ⇒ 零写入"（两阶段的可执行定义），
  ⑤ 钉的是"没链接的岗位也占一行"（抓 if not job.url: continue 的写法），
  ⑥ 钉的是"一个标题就能拆坏整张表"（Markdown 不报错，所以只能靠断言）。

模块位置：src/workflows/{daily_report.py, report.py}、src/app/main.py
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent.llm.base import LLMResponse, LLMTransientError, ToolCall
from agent.memory.db import connect, ensure_schema, new_run_id
from agent.memory.store import var_dir
from agent.runtime.budget import Budget
from agent.tools.sources.base import Job, JobPage, SourceError
from app.main import EXIT_FAILED, EXIT_OK, _report_outcome
from workflows import daily_report as dr
from workflows.daily_report import (
    STATUS_FAILED,
    STATUS_NO_ITEMS,
    STATUS_SUCCESS,
    DailyReport,
    run_daily_report,
)
from workflows.report import _render_table, report_day

failures = 0


def check(label: str, got: object, want: object, note: str = "") -> None:
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"   {'OK ' if ok else 'BAD'} {label} → {got!r}   期望 {want!r}  {note}")


# ══════════════════════════════════════════════════════════════
#  测试替身
# ══════════════════════════════════════════════════════════════


def _job(
    sid: str,
    *,
    title: str | None = None,
    company: str = "某公司",
    url: str | None = None,
) -> Job:
    return Job(
        source="remoteok",
        source_id=sid,
        title=title if title is not None else f"岗位 {sid}",
        company=company,
        url=url if url is not None else f"https://example.test/{sid}",
        location="上海",
    )


def fetch_script(
    pages: dict[int, list[Job]], *, fail_on: int | None = None, error: Exception | None = None
):
    """造一个假 fetch_page。返回 (函数, 被请求过的页码列表)。

    ★ 那个页码列表就是本节的判据：翻页循环有没有在该停的地方停，
      看的是它【问过哪些页】，而不是它最后返回了什么。
    """
    called: list[int] = []

    def fetch(page: int) -> JobPage:
        called.append(page)
        if fail_on is not None and page == fail_on:
            raise error
        return JobPage(jobs=pages.get(page, []), raw=f'{{"page": {page}}}'.encode())

    return fetch, called


class FakeLLM:
    """假 Provider —— 三种脾气：正常答一句 / 永远点单 / 直接抛错。

    ★ 它同样要遵守 Provider 契约第 3 条：回填真实 token 用量。
      填 0 的话，⑤ 里那条"runs 的 token = 子 run 的累计"就永远成立得毫无意义。

    ★ M6 起协议多了 `model`（trace 要拿它做成本归因），所以这里也得报一个。
      写死一个假的就行 —— 这台验收台关心的是"这个字段有没有被带下去"，
      不是"模型名对不对"（那要真链路才验得了）。
    """

    model = "fake-model"

    PROMPT_TOKENS = 100
    COMPLETION_TOKENS = 10

    def __init__(
        self,
        *,
        text: str = "今天这批岗位集中在后端与数据方向。",
        error=None,
        always_wants: bool = False,
    ):
        self.text = text
        self.error = error
        self.always_wants = always_wants
        self.calls: list[list[dict]] = []
        self.seen_tools: list[object] = []

    def chat(self, messages, tools=None):
        self.calls.append([dict(m) for m in messages])
        self.seen_tools.append(tools)
        if self.error is not None:
            raise self.error
        if self.always_wants:
            # ★ 刻意点一个【不存在】的工具名：loop 的 Act 分支是按全局 TOOLS
            #   注册表查名字的（不是按传进去的 tool_names），点真名会真的发网络请求。
            return LLMResponse(
                content=None,
                tool_calls=[ToolCall(id="c1", name="no_such_tool", arguments="{}")],
                prompt_tokens=self.PROMPT_TOKENS,
                completion_tokens=self.COMPLETION_TOKENS,
                finish_reason="tool_calls",
            )
        return LLMResponse(
            content=self.text,
            tool_calls=[],
            prompt_tokens=self.PROMPT_TOKENS,
            completion_tokens=self.COMPLETION_TOKENS,
            finish_reason="stop",
        )


def wide_budget() -> Budget:
    """只留步数闸在远处，token / wall 放到够不到 —— 一次实验只动一个变量。"""
    return Budget(max_steps=20, max_tokens=10**9, max_wall_seconds=1e9)


def data_rows(doc: str) -> list[str]:
    """报告里的【岗位行】（去掉表头与分隔行）。用来数"每一条岗位都占了一行吗"。"""
    return [
        line
        for line in doc.splitlines()
        if line.startswith("| ") and not line.startswith("| # ") and not line.startswith("|---")
    ]


def without_timestamp(doc: str) -> str:
    """去掉 `> 生成于 …` 那一行 —— 两次 run 的生成时刻天然不同。"""
    return "\n".join(line for line in doc.splitlines() if not line.startswith("> 生成于"))


def read_report(root: Path, rel: str) -> str:
    return (var_dir(root) / rel).read_text(encoding="utf-8")


def reports_on_disk(root: Path) -> list[str]:
    """var/reports/ 下已落盘的文件名。用来看"该写的时候写了没、不该写的时候写了没"。"""
    d = var_dir(root) / "reports"
    return sorted(p.name for p in d.glob("*.md")) if d.exists() else []


def main() -> None:
    opened: list[sqlite3.Connection] = []

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)

        def fresh(name: str):
            """一节一个干净的库 + 干净的 var/ —— 免得上一节的 raw 文件跑来做客。"""
            root = tmp_path / name
            root.mkdir()
            conn = connect(root / "app.db")
            ensure_schema(conn)
            opened.append(conn)  # ★ 记下来统一关：Windows 上连接没关，临时目录删不掉
            return conn, root

        # ══════════════════════════════════════════════════════════
        #  ① 翻页：空页就是【翻到底了】
        # ══════════════════════════════════════════════════════════
        print("\n═══ ① 翻页：空页是唯一正常的出口 ═══")
        full = {
            1: [_job(str(i)) for i in range(20)],
            2: [_job(str(i)) for i in range(20, 40)],
            3: [],
        }
        fetch, called = fetch_script(full)
        with patch.object(dr, "fetch_page", fetch):
            pages = dr.fetch_all_pages()
        check("被请求过的页码", called, [1, 2, 3], "★ 判据写成 != None 的话这里会一直问到第 50 页")
        check("返回的页数", len(pages), 2, "空页【不】收进列表 —— 它的意思是「没有更多了」")
        check("每页的条数", [len(p.jobs) for p in pages], [20, 20])

        # ══════════════════════════════════════════════════════════
        #  ② 护栏：远端永远不返回空页
        # ══════════════════════════════════════════════════════════
        print("\n═══ ② 护栏：远端永远给得出东西 ═══")
        endless = {i: [_job(str(i))] for i in range(1, 11)}
        fetch, called = fetch_script(endless)
        with patch.object(dr, "fetch_page", fetch):
            pages = dr.fetch_all_pages(max_pages=3)
        check("被请求过的页码", called, [1, 2, 3], "★ 没有这道护栏，分页参数一失效就是无限抓")
        check("返回的页数", len(pages), 3, "第二个出口：抓满 max_pages 就收手")

        # ══════════════════════════════════════════════════════════
        #  ③ 两阶段：抓取失败 ⇒ 库一行都没动
        # ══════════════════════════════════════════════════════════
        print("\n═══ ③ 两阶段：抓取中途失败，库动没动 ═══")
        conn, root = fresh("c")
        boom = SourceError("HTTP 403：remoteok 访问异常，不可重试")
        fetch, called = fetch_script({1: [_job("1")]}, fail_on=2, error=boom)
        with patch.object(dr, "fetch_page", fetch):
            rep = run_daily_report(
                conn, FakeLLM(), run_id=new_run_id(), budget=wide_budget(), root=root
            )
        check("status", rep.status, STATUS_FAILED)
        check("被请求过的页码", called, [1, 2], "第 1 页明明抓成功了，但……")
        check(
            "★ detail 带得回【原始】错误消息",
            boom.args[0] in rep.detail,
            True,
            "吞掉原文再抛一句通用话术，排查时线索就没了",
        )
        check(
            "★ jobs 表行数",
            conn.execute("select count(*) from jobs").fetchone()[0],
            0,
            "这就是两阶段的可执行定义：失败 ⇒ 零写入，failed 这个 status 才没有歧义",
        )
        check("raw 目录里没有存档", (var_dir(root) / "raw").exists(), False)
        check("没有报告文件", reports_on_disk(root), [])
        check(
            "runs 里留了痕",
            conn.execute("select status from runs").fetchone()["status"],
            STATUS_FAILED,
            "三个出口都要留痕，而 failed 是最不能漏的那条",
        )

        # ══════════════════════════════════════════════════════════
        #  ④ 判空：抓到了，只是没有岗位
        # ══════════════════════════════════════════════════════════
        print("\n═══ ④ 判空：抓取成功但一条都没有 ═══")
        conn, root = fresh("d")
        fetch, called = fetch_script({1: []})
        with patch.object(dr, "fetch_page", fetch):
            rep = run_daily_report(
                conn, FakeLLM(), run_id=new_run_id(), budget=wide_budget(), root=root
            )
        check("status", rep.status, STATUS_NO_ITEMS, "它和「抓取失败」是两回事，日志里不能长得一样")
        check("jobs 表行数", conn.execute("select count(*) from jobs").fetchone()[0], 0)
        check("没写报告", reports_on_disk(root), [])
        check(
            "runs 里词表是 no_items（不是 failed）",
            conn.execute("select status from runs").fetchone()["status"],
            STATUS_NO_ITEMS,
        )

        # ══════════════════════════════════════════════════════════
        #  ⑤ 全流程：抓 → 存 → 摘要 → 落盘 → 落库
        # ══════════════════════════════════════════════════════════
        print("\n═══ ⑤ 全流程：一次成功的日报 ═══")
        conn, root = fresh("e")
        batch = [
            _job("1", title="后端工程师", company="甲公司"),
            _job("2", title="数据工程师", company="乙公司"),
            _job("3", title="没有链接的岗位", company="丙公司", url=""),
        ]
        fetch, _ = fetch_script({1: batch, 2: []})
        llm = FakeLLM(text="今天这批岗位集中在后端与数据方向，都在上海。")
        with patch.object(dr, "fetch_page", fetch):
            rep = run_daily_report(conn, llm, run_id=new_run_id(), budget=wide_budget(), root=root)
        check("status", rep.status, STATUS_SUCCESS)
        check("inserted / updated / total", (rep.inserted, rep.updated, rep.total), (3, 0, 3))
        check("★ 报告路径是相对 var/ 的正斜杠", rep.report_path, f"reports/{report_day()}.md")

        doc = read_report(root, rep.report_path)
        check("报告非空", len(doc) > 0, True)
        check("摘要被写进正文", llm.text in doc, True)
        check(
            "★ 岗位行数 = 抓到的条数",
            len(data_rows(doc)),
            3,
            "含那条没链接的 —— 这就是 `if not job.url: continue` 会踩坏的地方",
        )
        check("缺链接写全角破折号", "| — |" in doc, True)
        check("文末脚注报了缺链接的条数", "其中 1 条没有直接链接" in doc, True)

        check(
            "runs.status",
            conn.execute("select status from runs").fetchone()["status"],
            STATUS_SUCCESS,
        )
        kinds = [r["kind"] for r in conn.execute("select kind from steps order by seq")]
        check(
            "steps 的 kind 组成",
            sorted(set(kinds)),
            ["llm", "subrun", "workflow"],
            "3 条 workflow（fetch/store/report）+ 1 条 llm + 1 条 subrun",
        )
        check(
            "steps 的 seq 从 0 连续",
            [r["seq"] for r in conn.execute("select seq from steps order by seq")],
            [0, 1, 2, 3, 4],
        )
        wf = [
            r["detail"]
            for r in conn.execute("select detail from steps where kind='workflow' order by seq")
        ]
        check(
            "工作流那三条记的是阶段名",
            wf,
            ["fetch:1页/3条", "store:+3/~0", f"report:{rep.report_path}"],
        )

        # ★ M6 对账第 3 条那笔债的回归护栏：编排层那三条 workflow 留痕要各自在
        #   【创建的这一刻】打 ts，而不是留空、退回落盘时刻。真实 run 抓到过退回
        #   的样子：fetch/store 的 ts 落在 run 收尾的 .580，反而晚于它们之后才发生的
        #   llm（.569）—— 按 ts 排序会得到一个物理上不可能的顺序。所以这里断言两件：
        #   ① workflow 行都带着自己的 ts；② 整份文件的 ts 随行序单调不减
        #      （行序 = records 的构建顺序 = 真实发生顺序）。
        trace_lines = [
            json.loads(ln)
            for ln in (var_dir(root) / "traces" / f"run-{rep.run_id}.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if ln.strip()
        ]
        wf_lines = [x for x in trace_lines if x["kind"] == "workflow"]
        check(
            "★ workflow 留痕各自带 ts（不退回落盘时刻）",
            all(x["ts"] for x in wf_lines),
            True,
            "退回落盘时刻的话三条同刻，回放的时间轴就糊了",
        )
        check(
            "★ 每条也带 latency_ms（不是默认 0）",
            all(isinstance(x["latency_ms"], int) and x["latency_ms"] >= 0 for x in wf_lines),
            True,
            "ts 给顺序、latency 给时长；少哪个都答不了 PLAN 自测题的「慢在哪一步」",
        )
        check(
            "★ 文件的 ts 随行序单调不减（回放顺序 = 真实顺序）",
            [x["ts"] for x in trace_lines] == sorted(x["ts"] for x in trace_lines),
            True,
            "之前 fetch/store 的 ts 晚于 llm —— 按 ts 排会得到一个物理上不可能的顺序",
        )

        tok = conn.execute("select prompt_tokens, completion_tokens from runs").fetchone()
        check(
            "runs 的 token = 子 run 的【累计值】",
            (tok["prompt_tokens"], tok["completion_tokens"]),
            (FakeLLM.PROMPT_TOKENS, FakeLLM.COMPLETION_TOKENS),
        )
        sub = conn.execute(
            "select detail, prompt_tokens, completion_tokens from steps where kind='subrun'"
        ).fetchone()
        check(
            "★ subrun 那条刻意带 0 token",
            (sub["detail"], sub["prompt_tokens"], sub["completion_tokens"]),
            ("success", 0, 0),
            "填累计值的话，任何 SUM(steps.prompt_tokens) 都会多算一遍",
        )
        check(
            "★ 摘要那一趟没带工具（白名单为空）",
            llm.seen_tools,
            [[]],
            "空列表 = 请求里根本没有 tools 键 —— 模型只能写，不能跑去多抓一页",
        )

        # ══════════════════════════════════════════════════════════
        #  ⑥ 渲染：一个标题就能拆坏整张表
        # ══════════════════════════════════════════════════════════
        print("\n═══ ⑥ 渲染：脏字符 ═══")
        dirty = [
            _job("1", title="带竖线的|岗位", company="甲\n公司"),
            _job("2", title="带\r\n换行的岗位", company="乙公司"),
        ]
        table, missing = _render_table(dirty)
        check("missing", missing, 0)
        check("`|` 被转义成 \\|", "\\|" in table, True, "不转义 → 列被切断；Markdown 一声不响")
        check(
            "行数 = 表头 + 分隔 + 2 行", len(table.splitlines()), 4, "换行不压掉 → 一行被劈成两行"
        )
        check("字段里的换行压成了空格", "带 换行的岗位" in table, True)
        check(
            "★ 返回值末尾不多一个换行",
            table.endswith("\n"),
            False,
            "模板里 {table} 之后已经留了一个",
        )
        check("空列表也给出表头", len(_render_table([])[0].splitlines()), 2)
        check("空列表的缺链接计数", _render_table([])[1], 0)

        # ══════════════════════════════════════════════════════════
        #  ⑦ 落盘幂等：再跑一次，写出来的东西一样
        # ══════════════════════════════════════════════════════════
        print("\n═══ ⑦ 落盘幂等：同一天跑两次 ═══")
        # ★ 这里能断言"逐字节相同"，靠的是【假 LLM 每次说同一句话】。
        #   真链路上摘要那段文字每次都不同（模型非确定性，代码里没设 temperature）
        #   —— 2026-10-07 实测：表格逐字节相同，只有摘要那行变了。
        #   所以"幂等"的准确范围是：**表格、计数、库里的行**，不含模型写的那段话。
        #   这不是本节在放水，而是"离线验收台只验确定性层"的本来面目 ——
        #   模型的不确定性归 M7 的评估管，不该混进这里。
        fetch, _ = fetch_script({1: batch, 2: []})
        with patch.object(dr, "fetch_page", fetch):
            rep2 = run_daily_report(
                conn,
                FakeLLM(text="今天这批岗位集中在后端与数据方向，都在上海。"),
                run_id=new_run_id(),
                budget=wide_budget(),
                root=root,
            )
        check("第二次 inserted（都见过了）", rep2.inserted, 0)
        check("第二次 updated", rep2.updated, 3)
        check("两次写的是同一个文件", rep2.report_path, rep.report_path)

        doc2 = read_report(root, rep2.report_path)
        check(
            "★ 报告没有被【追加】",
            len(data_rows(doc2)),
            3,
            "追加的话这里是 6 —— 那正是「连跑两次结果一致」要拦的东西",
        )
        check(
            "★ 去掉时间戳那一行，两次内容完全相同",
            without_timestamp(doc2) == without_timestamp(doc),
            True,
            "生成的时刻天然不同，但它不该影响别的字节",
        )
        check(
            "库里的岗位也没翻倍",
            conn.execute("select count(*) from jobs").fetchone()[0],
            3,
            "upsert 是刷新，不是插入",
        )
        check(
            "first_seen_run 仍指向第一次",
            conn.execute("select first_seen_run from jobs where source_id='1'").fetchone()[
                "first_seen_run"
            ],
            rep.run_id,
            "历史事实：换了 run 也不许改写",
        )

        # ══════════════════════════════════════════════════════════
        #  ⑧ 摘要降级：子 run 没跑完，日报照样出来
        # ══════════════════════════════════════════════════════════
        print("\n═══ ⑧ 摘要降级：子 run 撞上预算 ═══")
        conn, root = fresh("h")
        fetch, _ = fetch_script({1: [_job("1")], 2: []})
        with patch.object(dr, "fetch_page", fetch):
            rep = run_daily_report(
                conn,
                FakeLLM(always_wants=True),
                run_id=new_run_id(),
                budget=Budget(max_steps=1, max_tokens=10**9, max_wall_seconds=1e9),
                root=root,
            )
        check("日报仍然算成功", rep.status, STATUS_SUCCESS, "少一段话 ≠ 没有日报")
        doc = read_report(root, rep.report_path)
        check(
            "正文里是【占位符】而不是空白",
            "_（本次没有生成摘要）_" in doc,
            True,
            "读报告的人要能分清「今天没摘要」和「渲染漏了一段」",
        )
        check(
            "★ 子 run 的结局留了痕",
            conn.execute("select detail from steps where kind='subrun'").fetchone()["detail"],
            "max_steps_exceeded",
            "咽下去不吭声才是那个经典 bug：明天少了段话，没人知道是谁的锅",
        )
        check(
            "runs 的 token 仍然是真实的累计值",
            conn.execute("select prompt_tokens from runs").fetchone()["prompt_tokens"],
            FakeLLM.PROMPT_TOKENS,
            "没答完也是花过钱的，这笔账不能不算",
        )

        # ══════════════════════════════════════════════════════════
        #  ⑨ 摘要趟抛错：日报不该跟着陪葬
        # ══════════════════════════════════════════════════════════
        print("\n═══ ⑨ 摘要趟抛错（LLM 挂了）═══")
        conn, root = fresh("i")
        fetch, _ = fetch_script({1: [_job("1")], 2: []})
        with patch.object(dr, "fetch_page", fetch):
            try:
                rep = run_daily_report(
                    conn,
                    FakeLLM(error=LLMTransientError("连接超时")),
                    run_id=new_run_id(),
                    budget=wide_budget(),
                    root=root,
                )
            except Exception as exc:  # noqa: BLE001 —— 就是要把"它炸了"抓成一条断言
                rep = exc
        check(
            "★ 摘要趟抛错时日报仍然产出",
            type(rep).__name__,
            "DailyReport",
            f"实际：{type(rep).__name__}: {rep}" if isinstance(rep, Exception) else "",
        )
        check(
            "报告仍然落盘",
            reports_on_disk(root),
            [f"{report_day()}.md"],
            "数据都已经入库了，报告不能凭空消失 —— 那就是「库动了、账上没有」",
        )
        doc = read_report(root, f"reports/{report_day()}.md") if reports_on_disk(root) else ""
        check("正文里是占位符，不是空白", "_（本次没有生成摘要）_" in doc, True)
        sub = conn.execute("select detail from steps where kind='subrun'").fetchone()
        check(
            "★ 失败原因留了痕",
            str(sub["detail"] if sub else None).startswith("llm_error:"),
            True,
            "咽下去不吭声才是那个经典 bug：明天少了段话，没人知道是谁的锅",
        )
        check(
            "runs 里也有这次的记录",
            conn.execute("select count(*) from runs").fetchone()[0],
            1,
            "连 runs 都没有 = 这次 run 在账本上根本不存在",
        )

        # ══════════════════════════════════════════════════════════
        #  ⑩ 退出码：对外的契约
        # ══════════════════════════════════════════════════════════
        print("\n═══ ⑩ 退出码 ═══")
        quiet = io.StringIO()
        with contextlib.redirect_stdout(quiet):
            codes = [
                _report_outcome(DailyReport(run_id="r", status=STATUS_SUCCESS)),
                _report_outcome(DailyReport(run_id="r", status=STATUS_FAILED)),
                _report_outcome(DailyReport(run_id="r", status=STATUS_NO_ITEMS)),
            ]
            try:
                _report_outcome(DailyReport(run_id="r", status="某个没见过的"))
                unknown = "没抛"
            except ValueError as exc:
                unknown = f"ValueError: {exc}"
        check(
            "success / failed / no_items 的退出码",
            codes,
            [EXIT_OK, EXIT_FAILED, EXIT_OK],
            "PLAN §M2 定的契约",
        )
        check(
            "未知 status",
            unknown.startswith("ValueError"),
            True,
            "★ 宁可当场炸，也别静默当成功；消息里还要带上拿到的是什么",
        )

        # ★ 关掉所有连接再退出 with：Windows 上文件被占用时删不掉临时目录，
        #   那条 PermissionError 会盖住真正的失败信息 —— 比失败本身更费时间。
        for c in opened:
            c.close()

    print()
    if failures:
        print(f">>> {failures} 项不符。")
    else:
        print("✅ M4b 通过：翻页、两阶段、渲染、落盘幂等、落库词表都守住了（全程离线）。")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
