"""命令行入口 —— `python -m app.main <子命令>`。"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from workflows.daily_report import DailyReport

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_LOGIN_EXPIRED = 3
EXIT_BUSY = 4
"""0 成功 / 1 失败 / 2 配置错误 / 3 登录过期 / 4 已有实例在跑。"""

NCSS_LOGIN_URL = "https://www.ncss.cn/student/login.jsp"
"""半自动登录的起点（PLAN §M2：有头浏览器 + 主人亲自过图形验证码）。"""

NCSS_LOGIN_HOST = "www.ncss.cn"
"""登录成功的【通用判据】之一：URL 回到这个域，且不在登录页上。"""

NCSS_COOKIE_DOMAIN = "ncss.cn"
"""筛 cookie 用的域名片段 —— ★ 刻意【不是】上面那个完整主机名。"""

POLL_MS = 1000
"""轮询"登录完成没有"的间隔。1 秒：主人刚点完登录，最多多等一秒。"""


def build_parser() -> argparse.ArgumentParser:
    """子命令表。"""
    parser = argparse.ArgumentParser(prog="app.main", description="招聘信息日报 Agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("report", help="跑一次日报：抓取 → 入库 → 摘要 → 落盘")
    login = sub.add_parser(
        "login",
        help="半自动登录 ncss：开有头浏览器，主人亲自过验证码，登录态落盘（凭证只存在本机）",
    )
    login.add_argument(
        "--timeout", type=float, default=600.0, help="等主人完成登录的秒数（默认 600）"
    )
    sub.add_parser(
        "status",
        help="看最后一次运行的心跳、今日额度与登录态年龄（只读，不抓取）",
    )
    board = sub.add_parser("board", help="看最近几天的成本与健康度（只读，不写任何文件）")
    board.add_argument("--days", type=int, default=7, help="统计最近多少天（默认 7）")
    match = sub.add_parser("match", help="岗位匹配：本地库检索 → 排序 → 清单（可做三模式 A/B）")
    match.add_argument("--profile", required=True, help="求职档案 JSON 的路径")
    match.add_argument(
        "--mode",
        choices=["agent", "workflow", "single", "ab"],
        default="agent",
        help="agent=ReAct 循环（默认）/ workflow=纯规则 / single=单次 LLM / ab=三种并排",
    )
    match.add_argument("--top", type=int, default=10, help="最多入选多少条（1-20，默认 10）")
    return parser


def _report_outcome(report: DailyReport) -> int:
    """把一次日报的结局讲清楚（stdout），并给出退出码。"""
    from agent.tools.auth import LOGIN_HINT
    from workflows.daily_report import (
        STATUS_ALREADY_RAN,
        STATUS_FAILED,
        STATUS_LOGIN_EXPIRED,
        STATUS_NO_ITEMS,
        STATUS_SUCCESS,
        STATUS_UNREACHABLE,
    )

    if report.status == STATUS_SUCCESS:
        print(
            f"报告落在: {report.report_path}，"
            f"新增 {report.inserted} 条岗位信息，更新 {report.updated} 条岗位信息"
        )
        return EXIT_OK
    elif report.status == STATUS_FAILED:
        print(f"日报运行失败，失败原因：{report.detail}")
        return EXIT_FAILED
    elif report.status == STATUS_UNREACHABLE:
        print("日报未运行：连不上数据源（未发出 HTTP 请求，不占今日额度）。")
        print(f"网络恢复后可直接重跑。原因：{report.detail}")
        return EXIT_FAILED
    elif report.status == STATUS_NO_ITEMS:
        print("日报运行成功，但未抓取到任何岗位信息")
        return EXIT_OK
    elif report.status == STATUS_ALREADY_RAN:
        print(f"本次未抓取：{report.detail}。")
        return EXIT_OK
    elif report.status == STATUS_LOGIN_EXPIRED:
        print(f"登录态已过期，本次未抓到数据。请运行 `{LOGIN_HINT}` 续期后再跑。")
        print(f"（原因：{report.detail}）")
        return EXIT_LOGIN_EXPIRED
    raise ValueError(f"DailyReport 返回了未知的 status：{report.status!r}")


def cmd_report() -> int:
    """跑一次日报，返回退出码。"""
    from dotenv import load_dotenv

    load_dotenv()

    from agent.memory.store import var_dir
    from app.lock import LOCK_FILE, LockBusy, exclusive_lock

    try:
        with exclusive_lock(var_dir() / LOCK_FILE):
            return _report_locked()
    except LockBusy as exc:
        print(f"本次未启动：{exc}")
        print("上一次日报还没结束，本次直接退出（不重试、不叠加）。")
        return EXIT_BUSY


def _report_locked() -> int:
    """锁内的一次完整 report：建库 → 跑流程 → 讲结局 → 写心跳。"""
    from agent.llm.openai_compat import OpenAICompatProvider
    from agent.memory.db import connect, ensure_schema, new_run_id
    from agent.observability.logs import LoggedProvider, configure_logging, shutdown_logging
    from agent.runtime.budget import Budget
    from workflows.daily_report import run_daily_report

    configure_logging(level=os.getenv("LOG_LEVEL", "INFO"))

    conn = connect()
    try:
        ensure_schema(conn)
        report = run_daily_report(
            conn,
            LoggedProvider(OpenAICompatProvider()),
            run_id=new_run_id(),
            budget=Budget.from_env(),
        )
        code = _report_outcome(report)
        _write_heartbeat(report, code)
        return code
    finally:
        conn.close()
        shutdown_logging()


def _write_heartbeat(report: DailyReport, exit_code: int) -> None:
    """把这一次的结局写进 var/heartbeat.json（成功/失败都写）。"""
    from datetime import UTC, datetime

    from app.heartbeat import write_heartbeat

    payload = {
        "at": datetime.now(UTC).isoformat(),
        "run_id": report.run_id,
        "status": report.status,
        "exit_code": exit_code,
        "detail": report.detail,
        "report_path": report.report_path,
        "source": os.getenv("JOB_SOURCE", "remoteok").strip().lower(),
    }
    try:
        rel = write_heartbeat(payload)
    except OSError as exc:
        print(f"⚠️ 心跳写入失败（日报本身已完成）：{exc}")
        return
    from agent.observability.logs import log

    log.info("heartbeat_written", path=rel, status=report.status, exit_code=exit_code)


def cmd_board(days: int = 7) -> int:
    """打印最近 `days` 天的成本与健康度，返回退出码。"""
    from dotenv import load_dotenv

    from agent.memory.db import connect, ensure_schema
    from agent.observability.board import cost_summary, recent_errors, render_board

    load_dotenv()

    conn = connect()
    try:
        ensure_schema(conn)
        daily = cost_summary(conn, days=days)
        errors = recent_errors(conn)
    finally:
        conn.close()

    print(render_board(daily, errors, days=days))
    return EXIT_OK


def _hours_since(iso: str | None) -> float | None:
    """ISO 时间戳距现在多少小时；解析不了返回 None。"""
    from datetime import UTC, datetime

    if not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - moment).total_seconds() / 3600


def cmd_status() -> int:
    """回答"它最近跑没跑、今天还能不能跑、登录态多旧了" —— 只读，不抓取。"""
    from dotenv import load_dotenv

    load_dotenv()

    from agent.memory.db import connect, ensure_schema
    from agent.memory.store import ran_since
    from agent.tools.auth import LOGIN_HINT, state_age_seconds, state_path
    from app.heartbeat import heartbeat_path, read_heartbeat
    from workflows.daily_report import DAILY_CAP_STATUSES
    from workflows.report import day_start_utc

    source = os.getenv("JOB_SOURCE", "remoteok").strip().lower()
    print("== myAgent 状态 ==")
    print(f"数据源：{source}")

    conn = connect()
    try:
        ensure_schema(conn)
        today_used = ran_since(conn, since=day_start_utc(), statuses=DAILY_CAP_STATUSES)
    finally:
        conn.close()

    if source == "ncss":
        print(f"今日额度（东八区）：{'已使用' if today_used else '未使用'}")
    else:
        print("今日额度：开发期替身源不受「一天一次」限制")

    beat_path = heartbeat_path()
    try:
        beat = read_heartbeat()
    except (OSError, ValueError) as exc:
        print(f"心跳：⚠️ 读不出来 —— {exc}")
    else:
        if beat is None:
            print(f"心跳：还没有记录（{beat_path} 不存在，说明本机还没跑完过一次 report）")
        else:
            age = _hours_since(beat.get("at"))
            age_text = f"{age:.1f} 小时前" if age is not None else "时间未知"
            print(
                f"心跳：{beat.get('at')}  status={beat.get('status')}  "
                f"exit={beat.get('exit_code')}  ({age_text})"
            )
            if beat.get("report_path"):
                print(f"报告：{beat.get('report_path')}")
            if beat.get("detail"):
                print(f"说明：{beat.get('detail')}")

    p = state_path()
    if p.exists():
        hours = state_age_seconds() / 3600
        print(
            f"登录态：已保存 {hours:.1f} 小时（{p}）。"
            "服务端寿命未知；过期时 report 会以退出码 3 提醒续期。"
        )
    else:
        print(f"登录态：还没有（本机没登录过；需要时运行 `{LOGIN_HINT}`）")
    return EXIT_OK


def _logged_in(url: str) -> bool:
    """URL 判据：算不算"登录成功、回到了 ncss"。"""
    parts = urlsplit(url)
    if parts.hostname != NCSS_LOGIN_HOST:
        return False
    return "login.jsp" not in parts.path


def cmd_login(timeout: float = 600.0) -> int:
    """半自动登录：开有头浏览器 → 主人自己过验证码 → 登录态落盘。返回退出码。"""
    from dotenv import load_dotenv

    from agent.tools.auth import cookies_from_state, save_state

    load_dotenv()

    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    print("即将打开一个浏览器窗口。请在窗口里完成登录（含图形验证码）。")
    print("账号密码由您自己输入 —— 本程序不读取、不保存它们，窗口关闭后凭据只留在本机。")

    state: dict | None = None
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=False)
            except PlaywrightError as exc:
                print(f"浏览器内核起不来：{exc}")
                print("请先跑一次：python -m playwright install chromium")
                return EXIT_FAILED

            context = browser.new_context()
            page = context.new_page()
            page.goto(NCSS_LOGIN_URL)

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if page.is_closed():
                    print("浏览器窗口被关掉了。")
                    break
                if _logged_in(page.url):
                    state = context.storage_state()
                    break
                page.wait_for_timeout(POLL_MS)

            browser.close()
    except PlaywrightError as exc:
        print(f"浏览器交互中断：{exc}")
        return EXIT_LOGIN_EXPIRED

    if state is None:
        print(f"未在 {timeout:.0f} 秒内检测到登录成功（窗口关掉了，或一直停在登录页）。")
        print("登录态【没有】保存。可以重跑一次本命令。")
        return EXIT_LOGIN_EXPIRED

    rel = save_state(state)
    names = sorted(cookies_from_state(state, domains=(NCSS_COOKIE_DOMAIN,)))
    print(f"登录态已保存：{rel}")
    shown = ", ".join(names) if names else "（一个都没有！）"
    print(f"捕获到的 {NCSS_COOKIE_DOMAIN} cookie（{len(names)} 个）：{shown}")
    return EXIT_OK


def _match_outcome(result) -> int:
    """讲清一次匹配的结局；质量类结局不混成"程序坏了"。"""
    from workflows import job_match as jm

    if result.status == jm.STATUS_SUCCESS:
        print(f"匹配完成（{result.mode}）：{len(result.entries)} 条入选")
        for item in result.entries:
            print(f"  [{item['score']:>3}] {item['title']} @ {item['company']} — {item['reason']}")
            if item.get("url"):
                print(f"        {item['url']}")
    elif result.status in (jm.STATUS_NO_CANDIDATES, jm.STATUS_NO_SHORTLIST):
        print(f"没有产出清单（{result.mode}）：{result.detail}")
    elif result.status == jm.STATUS_INVALID_OUTPUT:
        print(f"模型输出不合格（{result.mode}）：{result.detail}")
    else:
        print(f"匹配失败（{result.mode}，{result.status}）：{result.detail}")

    metrics = result.metrics
    print(
        f"账本：LLM {metrics.llm_calls} 次 / 工具 {metrics.tool_calls} 次 / "
        f"tokens {metrics.prompt_tokens + metrics.completion_tokens} / {metrics.wall_ms} ms"
    )
    if result.artifact_path:
        print(f"清单落在: {result.artifact_path}")
    return EXIT_OK if result.status in jm.QUALITY_STATUSES else EXIT_FAILED


def _ab_outcome(report) -> int:
    """打印三模式并排结果；任一模式非正常终止则退出码 1。"""
    from workflows import job_match as jm

    print(f"A/B 完成（{report.run_id}）：")
    for result in report.results:
        metrics = result.metrics
        print(
            f"  {result.mode:<8} {result.status:<24} 入选 {len(result.entries):>2}  "
            f"LLM {metrics.llm_calls}  工具 {metrics.tool_calls}  {metrics.wall_ms} ms"
        )
    if report.markdown_path:
        print(f"对比表: {report.markdown_path}")
    if report.json_path:
        print(f"机器可读: {report.json_path}")
    normal = all(result.status in jm.QUALITY_STATUSES for result in report.results)
    return EXIT_OK if normal else EXIT_FAILED


def cmd_match(profile_path: str, mode: str, top: int) -> int:
    """岗位匹配：读档案 → 跑指定模式 → 讲结局。返回退出码。"""
    from dotenv import load_dotenv

    load_dotenv()

    from agent.memory.db import connect, ensure_schema, new_run_id
    from agent.observability.logs import LoggedProvider, configure_logging, shutdown_logging
    from agent.runtime.budget import Budget
    from workflows import job_match as jm

    if not 1 <= top <= jm.MAX_TOP:
        print(f"--top 必须在 1-{jm.MAX_TOP} 之间，收到 {top}。", file=sys.stderr)
        return EXIT_CONFIG

    profile = jm.load_profile(profile_path)
    configure_logging(level=os.getenv("LOG_LEVEL", "INFO"))

    llm = None
    if mode != jm.MODE_WORKFLOW:
        from agent.llm.openai_compat import OpenAICompatProvider

        llm = LoggedProvider(OpenAICompatProvider())

    run_id = new_run_id()
    conn = connect()
    try:
        ensure_schema(conn)
        if mode == jm.MODE_AB:
            return _ab_outcome(
                jm.run_ab(conn, llm, profile, run_id=run_id, top=top, budget=Budget.from_env())
            )
        if mode == jm.MODE_WORKFLOW:
            return _match_outcome(jm.run_workflow_match(conn, profile, run_id=run_id, top=top))
        if mode == jm.MODE_SINGLE:
            return _match_outcome(jm.run_single_match(conn, llm, profile, run_id=run_id, top=top))
        return _match_outcome(
            jm.run_agent_match(conn, llm, profile, run_id=run_id, top=top, budget=Budget.from_env())
        )
    finally:
        conn.close()
        shutdown_logging()


def main(argv: list[str] | None = None) -> int:
    """子命令分发 + 配置错误的统一出口（退出码 2）。"""
    from agent.config import ConfigError

    args = build_parser().parse_args(argv)
    try:
        if args.command == "report":
            return cmd_report()
        if args.command == "status":
            return cmd_status()
        if args.command == "board":
            return cmd_board(args.days)
        if args.command == "login":
            return cmd_login(args.timeout)
        if args.command == "match":
            return cmd_match(args.profile, args.mode, args.top)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    raise ValueError(f"未接线的子命令：{args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
