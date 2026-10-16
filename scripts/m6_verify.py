"""M6 验收台 —— 可观测层：trace 落盘 / 步骤新列 / 老库迁移 / 脱敏 / 看板。

用法：PYTHONUTF8=1 ./.venv/Scripts/python.exe scripts/m6_verify.py

★ 为什么全程离线、全用临时目录：
  M6 三块东西有一个共同的坏脾气 ——【平时看不见】。trace 是写文件、
  脱敏是改字符串、迁移只在"老库"上才发生。这三件事都不会在"跑通了"的
  那一刻报错：trace 少一个字段、脱敏多盖一个数字、迁移没补上列，
  全都是【静默】的。所以这一台不验"跑得通"，只验"形状对不对"，
  而且全在系统临时目录里做：不碰主人的 var/app.db（那里面有真实数据），
  跑完自动清。

★ 每一节钉的都是「只有理解了才写得对」的那一条，不是"没抛异常"：
  ① 钉的是 ts 由【产生留痕的那一层】打 —— 批量落盘不许把整批抹成同一刻；
  ② 钉的是"进库的"和"只进 trace 的"这两类字段的分界（能用 SQL 聚合吗）；
  ③ 钉的是 `CREATE TABLE IF NOT EXISTS` 补不了列 —— 那正是迁移存在的理由；
  ④ 钉的是 prompt_tokens / completion_tokens 这类【计数】不许被当成凭据盖掉
     （这颗雷是 2026-10-09 实测抓到的：键名含子串 "token" 被误伤成 "***"）；
  ⑤ 钉的是日期比较要过 datetime() 归一化 —— 直接比字符串会在边界悄悄错一格。

模块位置：src/agent/observability/{trace,logs,board}.py、
         src/agent/memory/{db,store}.py、src/agent/runtime/loop.py 的 StepRecord
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent.llm.base import LLMResponse, LLMTransientError
from agent.memory.db import (
    MIGRATIONS,
    SCHEMA,
    _columns,
    connect,
    ensure_schema,
    new_run_id,
)
from agent.memory.store import record_run, var_dir
from agent.observability.board import cost_summary, recent_errors, render_board
from agent.observability.logs import (
    LoggedProvider,
    configure_logging,
    log,
    redact,
    shutdown_logging,
)
from agent.observability.trace import trace_path, write_trace
from agent.runtime.loop import StepRecord

failures = 0


def check(label: str, got: object, want: object, note: str = "") -> None:
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"   {'OK ' if ok else 'BAD'} {label} → {got!r}   期望 {want!r}  {note}")


def _raises(fn) -> str:
    """跑 fn，返回异常类名；没抛就返回 '没抛'。把"抛了什么"本身当成断言值。"""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 —— 这里就是要拿异常类型当结果
        return type(exc).__name__
    return "没抛"


def _read_trace(root: Path, rel: str) -> list[dict]:
    """把一份 trace 文件按行 json.loads —— 这本身就是"JSONL"要兑现的能力。"""
    text = (var_dir(root) / rel).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


class _Fake:
    """写日志那一节用的假 Provider：报一个 model，能按需要抛错。"""

    model = "fake-model"

    def __init__(self, *, boom: bool) -> None:
        self.boom = boom

    def chat(self, messages, tools=None):
        if self.boom:
            raise LLMTransientError("连接超时")
        return LLMResponse(
            content="你好",
            finish_reason="stop",
            prompt_tokens=842,
            completion_tokens=61,
        )


def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)

        # ══════════════════════════════════════════════════════════
        #  ① trace：一次 run 一个文件、一行一条、按发生顺序
        # ══════════════════════════════════════════════════════════
        print("\n═══ ① trace：一次 run 一个文件、一行一条 ═══")
        t_root = tmp_path / "t"
        t_root.mkdir()
        rid = new_run_id()
        ts1 = "2026-10-07T12:00:01.231000+00:00"
        ts2 = "2026-10-07T12:00:03.100000+00:00"
        ts3 = "2026-10-07T12:00:05.900000+00:00"
        records = [
            StepRecord(
                step=0,
                kind="llm",
                detail="tool_calls",
                prompt_tokens=842,
                completion_tokens=61,
                latency_ms=1840,
                ts=ts1,
                extra={"model": "deepseek-chat", "tool_calls": ["fetch_job_list"]},
            ),
            StepRecord(
                step=0,
                kind="tool",
                detail="fetch_job_list",
                ok=True,
                latency_ms=1850,
                ts=ts2,
                extra={"args_digest": "a1b2c3d4e5f6"},
            ),
            StepRecord(
                step=0,
                kind="subrun",
                detail="llm_error:LLMTransientError",
                ok=False,
                latency_ms=0,
                error="llm_error",
                ts=ts3,
                extra={"sub_run_id": f"{rid}-summary"},
            ),
        ]
        rel = write_trace(rid, records, root=t_root)

        check(
            "路径形状：相对 var/、正斜杠",
            (rel, Path(rel).is_absolute(), "\\" in rel),
            (f"traces/run-{rid}.jsonl", False, False),
            "绝对路径进日志/进库，仓库一搬家就静默失效",
        )
        check(
            "trace_path 与 write_trace 是同一个出处",
            trace_path(rid, root=t_root).name,
            Path(rel).name,
        )

        lines = _read_trace(t_root, rel)
        core_keys = {"ts", "run_id", "step", "kind", "detail", "ok"}
        check("行数 = 记录数", len(lines), 3, "每步都记，不管成功失败")
        check("每行的固定列都在", all(core_keys <= set(x) for x in lines), True)
        check(
            "★ ts 用的是记录【自己带的】(批量落盘没抹成同一刻)",
            [x["ts"] for x in lines],
            [ts1, ts2, ts3],
            "落盘时才 now() 的话这三行会是同一个时间戳 —— 顺序与耗时全糊了",
        )
        check("run_id 逐行一致", {x["run_id"] for x in lines}, {rid})
        check(
            "extra 并进了那一行（llm 的 model / tool_calls）",
            (lines[0]["model"], lines[0]["tool_calls"]),
            ("deepseek-chat", ["fetch_job_list"]),
        )
        check("extra 并进了那一行（tool 的 args_digest）", lines[1]["args_digest"], "a1b2c3d4e5f6")
        check(
            "error 为空【不】写这个键",
            ("error" in lines[0], "error" in lines[1]),
            (False, False),
            '空串与「没有这个字段」是两件事，筛错误的判据是 if "error" in line',
        )
        check("error 有值才写", lines[2]["error"], "llm_error")

        # ts 留空 → 退回落盘时刻（不抛）
        write_trace(rid, [StepRecord(step=0, kind="llm", detail="stop")], root=t_root)
        check("ts 留空时退回落盘时刻（不抛）", bool(_read_trace(t_root, rel)[0]["ts"]), True)

        # 覆盖写，不是追加
        write_trace(rid, records, root=t_root)
        write_trace(rid, records[:1], root=t_root)
        check(
            "★ 同 run_id 再写一遍是【覆盖】",
            len(_read_trace(t_root, rel)),
            1,
            "追加的话这里会是 4，读起来像「这个 run 跑了两趟」",
        )

        # 一次 run 一个文件
        write_trace(new_run_id(), records, root=t_root)
        check(
            "多次 run → 多个文件（不混在一起）",
            len(list((var_dir(t_root) / "traces").glob("*.jsonl"))),
            2,
        )

        # 键冲突护栏：extra 里混进固定列的名字
        check(
            "★ extra 塞了保留键 → 当场炸",
            _raises(
                lambda: write_trace(
                    new_run_id(), [StepRecord(step=0, kind="llm", extra={"step": 9})], root=t_root
                )
            ),
            "ValueError",
            "静默覆盖会把真正的 step 顶掉，而 trace 看上去还挺正常 —— 这是 m4b 真拦下过的雷",
        )

        # ══════════════════════════════════════════════════════════
        #  ② steps 的两列：进库的是"能用 SQL 聚合的"，其余只进 trace
        # ══════════════════════════════════════════════════════════
        print("\n═══ ② steps 新列：进库的 vs 只进 trace 的 ═══")
        conn = connect(tmp_path / "s" / "app.db")
        ensure_schema(conn)
        rid2 = new_run_id()
        run_steps = (
            StepRecord(
                step=1,
                kind="llm",
                detail="tool_calls",
                prompt_tokens=100,
                completion_tokens=10,
                latency_ms=1840,
                ts="2026-10-02T03:00:01+00:00",
                extra={"model": "deepseek-chat"},
            ),
            StepRecord(
                step=1,
                kind="tool",
                detail="fetch_job_list",
                ok=False,
                latency_ms=1850,
                error="tool_timeout",
                ts="2026-10-02T03:00:02+00:00",
                extra={"args_digest": "deadbeefcafe"},
            ),
        )
        record_run(
            conn,
            run_id=rid2,
            status="failed",
            steps=run_steps,
            prompt_tokens=220,
            completion_tokens=22,
            started_at="2026-10-02T03:00:00+00:00",
            error="SourceError: HTTP 403",
        )
        rows = conn.execute(
            "select seq, latency_ms, error from steps where run_id = ? order by seq", (rid2,)
        ).fetchall()
        check(
            "steps 的 latency_ms / error 落对了",
            [(r["seq"], r["latency_ms"], r["error"]) for r in rows],
            [(0, 1840, ""), (1, 1850, "tool_timeout")],
            "没有错误的步写空串，不是 NULL —— 这一列是【代号】，空就是「这步没事」",
        )
        check(
            "failed 才把原因原文写进 runs.error",
            conn.execute("select error from runs where run_id = ?", (rid2,)).fetchone()["error"],
            "SourceError: HTTP 403",
        )

        rid3 = new_run_id()
        record_run(
            conn,
            run_id=rid3,
            status="success",
            steps=run_steps,
            prompt_tokens=220,
            completion_tokens=22,
            started_at="2026-10-02T03:00:00+00:00",
        )
        check(
            "★ 没给 error 时 runs.error 是 NULL 而不是空串",
            conn.execute("select error from runs where run_id = ?", (rid3,)).fetchone()["error"],
            None,
            '留空是诚实的「还没有」，填 "" 是撒谎「什么都没有」',
        )

        step_cols = _columns(conn, "steps")
        check(
            "只进 trace 的字段【不】进库",
            ("ts" in step_cols, "model" in step_cols, "extra" in step_cols),
            (False, False, False),
            "铺成列的话，一半的列对任何一行都是 NULL —— 那正是「声明了但没人消费」",
        )

        # ══════════════════════════════════════════════════════════
        #  ③ 迁移：老库的 steps 少两列
        # ══════════════════════════════════════════════════════════
        print("\n═══ ③ 迁移：给【已存在】的表补列 ═══")
        check(
            "迁移与建表说的是一套话",
            all(column in SCHEMA for _table, column, _ddl in MIGRATIONS),
            True,
            "迁移补的列必须在 SCHEMA 里也有，否则新库和老库会长成两个样子",
        )

        m_conn = connect(tmp_path / "m" / "app.db")
        ensure_schema(m_conn)
        # 造一个"老库"：把 M6 新加的两列删掉，回到 M5 的形状。
        # （用 DROP COLUMN 而不是手写一份旧 SCHEMA —— 手写的副本迟早会跟真的走散。）
        m_conn.execute("ALTER TABLE steps DROP COLUMN latency_ms")
        m_conn.execute("ALTER TABLE steps DROP COLUMN error")
        check(
            "老库确实少了两列",
            {"latency_ms", "error"} & _columns(m_conn, "steps"),
            set(),
        )
        m_conn.execute(
            "insert into runs (run_id, started_at, status) values (?, ?, ?)",
            ("old", "2026-09-01T00:00:00+00:00", "success"),
        )
        m_conn.execute(
            "insert into steps (run_id, seq, step, kind, detail, ok, prompt_tokens,"
            " completion_tokens) values ('old', 0, 1, 'llm', 'stop', NULL, 5, 1)"
        )
        m_conn.commit()

        check(
            "反面：老库上按新 schema 写 → 报错",
            _raises(
                lambda: m_conn.execute(
                    "insert into steps (run_id, seq, step, kind, latency_ms)"
                    " values ('old', 1, 1, 'llm', 100)"
                )
            ),
            "OperationalError",
            "CREATE TABLE IF NOT EXISTS 对已存在的表是空操作 —— 这就是迁移存在的全部理由",
        )

        ensure_schema(m_conn)
        check(
            "补齐后的列",
            {"latency_ms", "error"} <= _columns(m_conn, "steps"),
            True,
        )
        old_row = m_conn.execute("select latency_ms, error from steps where seq = 0").fetchone()
        check(
            "★ 老行还在，新列取默认值",
            (old_row["latency_ms"], old_row["error"]),
            (0, ""),
            "ADD COLUMN 的 DEFAULT 会回填到老行上 —— 不许因为补列把老数据弄丢",
        )
        check("ensure_schema 幂等", _raises(lambda: ensure_schema(m_conn)), "没抛")
        m_conn.close()

        # ══════════════════════════════════════════════════════════
        #  ④ 看板：聚合，以及那个"别拿字符串比日期"的坑
        # ══════════════════════════════════════════════════════════
        print("\n═══ ④ 看板：聚合与日期窗口 ═══")
        b_conn = connect(tmp_path / "b" / "app.db")
        ensure_schema(b_conn)
        now = datetime.now(UTC)

        def add_run(name: str, days_ago: int, status: str, pt: int, ct: int, err=None) -> None:
            when = (now - timedelta(days=days_ago)).isoformat()
            b_conn.execute(
                "insert into runs (run_id, started_at, finished_at, status, steps,"
                " prompt_tokens, completion_tokens, error) values (?,?,?,?,?,?,?,?)",
                (name, when, when, status, 2, pt, ct, err),
            )

        add_run("today", 0, "success", 100, 20)
        add_run("six_days", 6, "failed", 40, 10, err="SourceError: HTTP 403")
        add_run("ten_days", 10, "success", 900, 99)  # 应被 7 天窗口排除
        b_conn.commit()

        daily = cost_summary(b_conn, days=7)
        check(
            "★ 窗口内的天数（10 天前那条被排除）",
            len(daily),
            2,
            "靠 datetime() 归一化比较，不是拿两种格式的字符串硬比大小",
        )
        check("窗口内 token 合计", sum(r["tokens"] for r in daily), 100 + 20 + 40 + 10)
        check("窗口内 runs 合计", sum(r["runs"] for r in daily), 2)
        check(
            "ok / failed 分得开",
            (sum(r["ok"] for r in daily), sum(r["failed"] for r in daily)),
            (1, 1),
        )

        errs = recent_errors(b_conn)
        check("最近的失败只挑【有 error 原文】的", [e["run_id"] for e in errs], ["six_days"])
        check("边界：days=0 当场报", _raises(lambda: cost_summary(b_conn, days=0)), "ValueError")
        check("边界：limit=0 当场报", _raises(lambda: recent_errors(b_conn, limit=0)), "ValueError")

        empty_conn = connect(tmp_path / "b" / "empty.db")
        ensure_schema(empty_conn)
        check("空库不炸，只是空", cost_summary(empty_conn, days=7), [])
        empty_conn.close()

        board_txt = render_board(daily, errs, days=7)
        check("看板含合计行", board_txt.count("合计"), 1)
        check("看板含失败原文", "SourceError: HTTP 403" in board_txt, True)
        check(
            "空的时候【说清楚是空】",
            "（这段时间没有任何 run）" in render_board([], [], days=7),
            True,
            "「没有数据」和「表头下面没了」看起来一样，但前者是结论、后者像故障",
        )
        b_conn.close()

        # ══════════════════════════════════════════════════════════
        #  ⑤ 脱敏：计数不许被当成凭据（实测抓到的雷）
        # ══════════════════════════════════════════════════════════
        print("\n═══ ⑤ 脱敏：计数 vs 凭据 ═══")
        check(
            "★ token 计数保住（键名含 token 但值是数字）",
            redact({"prompt_tokens": 842, "completion_tokens": 61}),
            {"prompt_tokens": 842, "completion_tokens": 61},
            "这一颗雷是 2026-10-09 实测抓到的：子串匹配把两个计数盖成了 ***",
        )
        check("同族的 max_tokens 也保住", redact({"max_tokens": 4096}), {"max_tokens": 4096})
        check("api_key 盖掉", redact({"api_key": "sk-abcdef123456"}), {"api_key": "***"})
        check(
            "Authorization 头盖掉（键名命中）",
            redact({"Authorization": "Bearer abcdef123456"}),
            {"Authorization": "***"},
        )
        check(
            "值里的 Bearer 盖掉（键名没命中，靠值的形状）",
            redact({"note": "Authorization: Bearer abcdef"}),
            {"note": "Authorization: ***"},
        )
        check(
            "嵌套 dict 的内层一样盖",
            redact({"messages": [{"role": "user", "api_key": "sk-nested12345"}]}),
            {"messages": [{"role": "user", "api_key": "***"}]},
            "llm.log 里 messages 是个大 dict —— 只盖顶层等于没盖",
        )

        # ══════════════════════════════════════════════════════════
        #  ⑥ 日志分层：两个文件，且不互相掺
        # ══════════════════════════════════════════════════════════
        print("\n═══ ⑥ 日志分层：app.log 与 llm.log ═══")
        l_root = tmp_path / "l"
        l_root.mkdir()
        configure_logging(level="INFO", root=l_root)
        check(
            "未知日志级别当场报（不静默退回）",
            _raises(lambda: configure_logging(level="INFOO", root=l_root)),
            "ValueError",
            "配置写错却悄悄用默认级别，是最难查的那种「配置没生效」",
        )

        log.info("report_finished", inserted=5)
        LoggedProvider(_Fake(boom=False)).chat(
            [{"role": "user", "content": "hi", "api_key": "sk-leak123456"}]
        )
        try:
            LoggedProvider(_Fake(boom=True)).chat([{"role": "user", "content": "hi"}])
        except LLMTransientError:
            pass  # 日志层【不吞】异常，原样抛回 —— 这里接住只为了继续验

        # 冒充第三方库：httpx 在每次请求后打的 INFO（GET / POST 都是它）。
        # 它会顺着 root 冒进来 —— 但它不是我们从 "agent" 树发的，
        # app.log 的白名单 filter 该把它挡在门外（见 logs._is_our_logger）。
        # 同时挂个临时 handler 到 root，证明"记录确实发出了、是 filter 挡的"，
        # 而不是"根本没打出来"（否则这条断言就是假绿）。
        seen: list[str] = []

        class _Probe(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                seen.append(record.name)

        probe = _Probe()
        root_logger = logging.getLogger()
        root_logger.addHandler(probe)
        logging.getLogger("httpx").info('HTTP Request: GET https://example.com "HTTP/1.1 200 OK"')
        root_logger.removeHandler(probe)

        shutdown_logging()  # 关句柄，否则 Windows 上临时目录删不掉

        app_txt = (var_dir(l_root) / "logs" / "app.log").read_text(encoding="utf-8")
        llm_txt = (var_dir(l_root) / "logs" / "llm.log").read_text(encoding="utf-8")
        check("业务事件在 app.log", "report_finished" in app_txt, True)
        check(
            "★ 第三方 INFO 确实冒到了 root（证明是 filter 挡的，不是没发出）",
            "httpx" in seen,
            True,
        )
        check(
            "★ 但它【不】落进 app.log",
            "example.com" in app_txt,
            False,
            "app.log 只收 agent 树的记录 —— httpx 每请求的 INFO 是噪声，不是业务事件",
        )
        check(
            "★ LLM 事件【不】落进 app.log",
            "llm_request" in app_txt,
            False,
            "propagate=False 的全部意义：同一条只落一处，否则两个文件必有一个是过期的",
        )
        check("LLM 事件在 llm.log", "llm_response" in llm_txt, True)
        counts = (
            llm_txt.count("llm_request"),
            llm_txt.count("llm_response"),
            llm_txt.count("llm_error"),
        )
        check(
            "失败那一次只留 request + error，没有 response",
            counts,
            (2, 1, 1),
            "raise 把控制流带出函数 —— 异常时那句 llm_response 根本走不到",
        )
        check(
            "★ token 计数在文件里是【数字】",
            '"prompt_tokens": 842' in llm_txt,
            True,
            "这一条就是⑤那颗雷的【回归护栏】：回到子串匹配，它会当场变红",
        )
        check("★ 密钥在文件里是星号", '"api_key": "***"' in llm_txt, True)
        check("llm.log 里没有裸密钥", "sk-leak123456" in llm_txt, False)

    print()
    if failures:
        print(f">>> {failures} 项不符。")
    else:
        print("✅ M6 通过：trace 形状、步骤新列、老库迁移、脱敏、看板都守住了（全程离线）。")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
