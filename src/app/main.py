"""命令行入口 —— `python -m app.main <子命令>`。

★ 为什么单独一层：进程级的东西（sys.argv / load_dotenv / sys.exit）
  留在这里，workflows/ 就还是一个能被测试直接调用的普通库 ——
  m4b_verify 调 run_daily_report，不经过这个文件。

★ 退出码是【对外接口】，不是随手 return 的数：
  它会被 cron / 任务计划程序 / CI 读走，是程序对外唯一稳定的信号 ——
  日志会改措辞、stdout 会加字段，退出码是契约。

★ cwd 必须是【仓库根】：.env 里的 DB_PATH=var/app.db 是相对路径，
  在别处跑（比如 cd src 再 python -m app.main）库就会落到别的文件夹去，
  而且不报错 —— 只是默默地在一个新库里从零开始。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    # 只给类型检查器看：_report_outcome 的签名要用它。
    # 运行时不需要 —— 那个 import 在 cmd_report 里惰性做（理由见那里）。
    from workflows.daily_report import DailyReport

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_LOGIN_EXPIRED = 3
"""0 成功 / 1 失败 / 3 登录过期 —— 照抄 PLAN.md §M2 已定的约定。

★ 用常量而不是散落的裸数字：`sys.exit(3)` 里的 3 是魔法数字，
  读代码的人得回去翻 PLAN 才知道它什么意思；常量名自带意思。

★ 3 现在【走得到】了（M2b 接线）：它由两条路产生 ——
  ① `report` 跑起来发现登录态过期（`run_daily_report` 返回 status="login_expired"）；
  ② `login` 自己失败（主人取消 / 超时 / 没登成）。
  两者退出码相同，理由：对 cron 来说它们是同一件事 ——
  "需要一个人去续期"，而不是"程序坏了"。
"""

NCSS_LOGIN_URL = "https://www.ncss.cn/student/login.jsp"
"""半自动登录的起点（PLAN §M2：有头浏览器 + 主人亲自过图形验证码）。

★ 为什么这些 ncss 专属常量先放在 CLI 这层：目前唯一的消费者就是 `cmd_login`。
  等 Phase B 写出 `sources/ncss.py`（那里才是"ncss 的一切"该住的地方 ——
  接口路径、分页参数、解析规则），这几个常量跟着搬过去。
  先在这儿建个"ncss 知识"的新家，反而是提前分家。"""

NCSS_LOGIN_HOST = "www.ncss.cn"
"""登录成功的【通用判据】之一：URL 回到这个域，且不在登录页上。

★ 为什么现在只能用"通用判据"而不是硬探针：PLAN §M0 还没做，我们**还不知道**
  登录后哪个接口是"必须登录才能访问"的（§2.1 只记到前缀 `/student/api/zeyuanhome/*`，
  确切的路径与分页参数在主人抓包之前是猜的）。把猜的端点写进成功判据，
  等于用一个可能错的东西去验收另一个东西 —— Phase B 换成硬探针
  （请求一次真职位接口、看是否仍 302），那时这条常量就退休了。"""

NCSS_COOKIE_DOMAIN = "ncss.cn"
"""筛 cookie 用的域名片段 —— ★ 刻意【不是】上面那个完整主机名。

cookie 的 domain 通常是【父域】且常带前导点（`.ncss.cn`），拿 `www.ncss.cn`
去比它一个都匹配不上 —— 于是"筛出来是空的"，而空 cookie 发出去的后果是
被 302 回登录页，看起来像"刚存的登录态就过期了"（归因指向完全错误的方向）。
用父域片段比，`.ncss.cn` 与 `www.ncss.cn` 都命中。
（确切的 cookie 域名要等 M0 抓包确认；用父域是个安全的【超集】——
 多带几个 ncss 域的 cookie 出去，比漏带一个导致 302 好。）"""

POLL_MS = 1000
"""轮询"登录完成没有"的间隔。1 秒：主人刚点完登录，最多多等一秒。
★ 用 Playwright 自己的 `page.wait_for_timeout` 而不是 `time.sleep`：
  前者在等待期间仍会让浏览器事件循环转起来（页面照样渲染），
  后者会把整个 sync 客户端按住 —— 表现是"浏览器窗口像假死了"。"""


def build_parser() -> argparse.ArgumentParser:
    """子命令表。

    ★ 三个子命令的**启动代价刻意不同**：report 要 LLM + 库 + 日志；
      board 只要库；login 只要 playwright（且 import 写在函数里）。
      每个子命令只付自己那一份（理由见 cmd_report 的 docstring）。
    """
    parser = argparse.ArgumentParser(prog="app.main", description="招聘信息日报 Agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("report", help="跑一次日报：抓取 → 入库 → 摘要 → 落盘")
    login = sub.add_parser(
        "login",
        help="半自动登录 ncss：开有头浏览器，主人亲自过验证码，登录态落盘（凭证只存在本机）",
    )
    # ★ 等待上限做成可配的，而不是硬编码：主人过验证码要多久（找手机、输错一次重来）
    #   是无法预知的，但它必须有上限 —— 否则无人值守时这一步会永远挂着。
    #   默认 600s 是"给人用"的量级；调小用于测试。
    login.add_argument(
        "--timeout", type=float, default=600.0, help="等主人完成登录的秒数（默认 600）"
    )
    board = sub.add_parser("board", help="看最近几天的成本与健康度（只读，不写任何文件）")
    # ★ 默认值写成整数 7 而不是 "7"：type=int 负责把用户敲的字符串转过来，
    #   而 default 不经过 type —— 写成 "7" 的话，不传 --days 时拿到的是字符串，
    #   `days < 1` 会当场 TypeError（字符串和整数没法比大小）。
    board.add_argument("--days", type=int, default=7, help="统计最近多少天（默认 7）")
    return parser


def _report_outcome(report: DailyReport) -> int:
    """把一次日报的结局讲清楚（stdout），并给出退出码。

    ★ import 写在这里而不是文件顶部：同 cmd_report 那条纪律 ——
      本函数只在日报跑完之后才会被调到，不必为别的子命令把 workflows 拉起来。

    ★ 比的是 STATUS_* 【常量本身】，不是它们【名字的字符串】：
      `STATUS_SUCCESS` 是变量（值是 "success"）；`"STATUS_SUCCESS"` 是一段
      文字（值就是 "STATUS_SUCCESS"）。写成后者，三个分支一个都进不去，
      全部掉进 else —— 而它看上去还挺像对的。
    """
    from agent.tools.auth import LOGIN_HINT
    from workflows.daily_report import (
        STATUS_FAILED,
        STATUS_LOGIN_EXPIRED,
        STATUS_NO_ITEMS,
        STATUS_SUCCESS,
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
    elif report.status == STATUS_NO_ITEMS:
        print("日报运行成功，但未抓取到任何岗位信息")
        return EXIT_OK
    elif report.status == STATUS_LOGIN_EXPIRED:
        # ★ 刻意【不】只打一句"失败"：这条消息是给人看的下一步指令。
        #   它必须包含【命令本身】（LOGIN_HINT 的唯一出处），
        #   而原因原文跟在后头 —— 主人要判断的是"要不要现在去续期"，
        #   不是"代码哪里错了"。
        print(f"登录态已过期，本次未抓到数据。请运行 `{LOGIN_HINT}` 续期后再跑。")
        print(f"（原因：{report.detail}）")
        return EXIT_LOGIN_EXPIRED
    # ★ 把 status 原文带进消息：只说"无效的返回值"，读日志的人还得回去翻代码
    #   才知道到底拿到了什么 —— 归因错位（同 M4a-1 那个病）。
    raise ValueError(f"DailyReport 返回了未知的 status：{report.status!r}")


def cmd_report() -> int:
    """跑一次日报，返回退出码。

    ★ 为什么 import 写在函数里而不是模块顶部：
      将来 `login` 子命令要 playwright、别的子命令可能要别的 ——
      顶部 import 会让"只跑 report"也把无关依赖全拉起来。
      惰性 import 让每个子命令只付自己的启动成本，
      和 sources/__init__.py 那句"用 remoteok 时不必 import ncss"同源。
      代价：这一段的依赖不在文件顶部，扫一眼看不全 —— 认了，
      因为它换来的是"启动成本只付自己的那一份"。
    """
    from dotenv import load_dotenv

    from agent.llm.openai_compat import OpenAICompatProvider
    from agent.memory.db import connect, ensure_schema, new_run_id
    from agent.observability.logs import LoggedProvider, configure_logging, shutdown_logging
    from agent.runtime.budget import Budget
    from workflows.daily_report import run_daily_report

    # ★ 先 load_dotenv 再碰任何 getenv：配置只在 .env 里，os.environ 里当然没有。
    #   （m3_verify 就在这上面栽过 —— .env 里明明有 key，"没 key 就跳过"那一支
    #     却永远成立，于是端到端那项永远静默跳过、末尾照样打 ✅。跳过必须是响的。）
    load_dotenv()

    # ★ 日志必须在 run_daily_report 【之前】接上：那之后打的业务事件
    #   （fetch_done / store_done / report_finished）才有个去处。
    #   级别走 .env 的 LOG_LEVEL（缺省 INFO）—— 配置只在 .env 里有一个出处，
    #   不在代码里另起一套（同 DB_PATH 那条纪律）。
    configure_logging(level=os.getenv("LOG_LEVEL", "INFO"))

    conn = connect()
    try:
        ensure_schema(conn)  # 幂等，每次启动无脑调一次
        report = run_daily_report(
            conn,
            # ★ 包一层：把每次 LLM 调用写进 var/logs/llm.log（**脱敏后**的 messages）。
            #   装饰器满足同一个 LLMProvider 协议，所以 workflows 一行都不用改 ——
            #   这正是 M1 那层抽象该兑现的红利（见 logs.py 的 LoggedProvider）。
            LoggedProvider(OpenAICompatProvider()),
            run_id=new_run_id(),
            budget=Budget.from_env(),
        )
    finally:
        # ★ finally 而不是"正常路径上 close"：中途抛异常时连接才不会漏掉。
        #   进程退出会回收 fd，但显式关掉是把"这里就是它的终点"写在代码上。
        conn.close()
        # ★ 日志句柄也要关，理由同上面那条，而且是 Windows 特有的：
        #   被打开着的文件删不掉，而清理失败抛的那条 PermissionError
        #   会盖住真正的失败（见 logs.shutdown_logging）。
        shutdown_logging()

    return _report_outcome(report)


def cmd_board(days: int = 7) -> int:
    """打印最近 `days` 天的成本与健康度，返回退出码。

    ★ 只读：它不跑日报、不写报告、也不落 trace —— 唯一可能的写入是
      ensure_schema（首次在新机器上跑时建空表），而那是幂等的。
      所以这里【不】configure_logging：看板没有业务事件要记，
      建了日志文件反而是"跑一次看板就多两个文件"的无谓副作用。
    """
    from dotenv import load_dotenv

    from agent.memory.db import connect, ensure_schema
    from agent.observability.board import cost_summary, recent_errors, render_board

    load_dotenv()

    conn = connect()
    try:
        # 幂等。调它的理由：库里一张表都没有时（刚 clone、还没跑过日报），
        # 不建表的话查询会抛 "no such table: runs" —— 而"还没跑过"明明是
        # 一个正常的、该被好好说出来的状态，不该长得像故障。
        ensure_schema(conn)
        daily = cost_summary(conn, days=days)
        errors = recent_errors(conn)
    finally:
        conn.close()

    print(render_board(daily, errors, days=days))
    return EXIT_OK


def _logged_in(url: str) -> bool:
    """URL 判据：算不算"登录成功、回到了 ncss"。

    ★ 抽成【纯函数】是为了能被单测（tests/test_login_expired.py）：
      判定逻辑一旦埋在 cmd_login 的轮询循环里，验收它就只能"真开一次浏览器
      手工试"—— 而那是本机最贵、最不稳定的一种验证方式。
      判据本身是纯字符串运算，没有理由它不能离线测。

    ★ 判据只认 host 与 path，不认 query：CAS 回跳时会带一堆 ticket 参数，
      按整串匹配等于把 ticket 也写进了契约。
    """
    parts = urlsplit(url)
    if parts.hostname != NCSS_LOGIN_HOST:
        return False
    return "login.jsp" not in parts.path


def cmd_login(timeout: float = 600.0) -> int:
    """半自动登录：开有头浏览器 → 主人自己过验证码 → 登录态落盘。返回退出码。

    ★ 为什么是【半自动】（PLAN §M2 已定，这里再记一遍理由）：
      ncss 走学信网 CAS，**有图形验证码**。验证码的本质是"证明你是人"——
      让主人这个真人来过，是尊重它而不是对抗它，所以它不会失效、不会被封号。
      工程上：登录只占系统 5% 的复杂度，却贡献 90% 的不稳定性，
      把它隔离成"一年做几次"的人工动作，剩下 95% 的自动化才真正可靠。
      ❌ 不接打码平台、不逆向验证码、不伪造设备指纹。

    ★ 这个函数【绝不出现账号密码】：主人自己在浏览器窗口里输。
      代码里连一个 password 变量都没有 —— 凭据从不经过我们的进程，
      所以"误提交凭据"这件事在结构上不可能发生（不只是"我们小心"）。

    ★ load_dotenv 必须在 save_state 【之前】：凭证落在 `VAR_DIR/auth/` 下，
      而 `VAR_DIR` 只在 .env 里。少了这一句，主人若把 VAR_DIR 改到了别处，
      这里会写到默认的 `var/`，而 `report` 去改过的那处找 —— 一个事实两个出处，
      且两边都不报错（同 memory/store.py 在 DB_PATH 上认的那笔账）。
    """
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
                # ★ 这是"环境没装好"，不是"登录失败"—— 分开报，下一步动作完全不同。
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
        # 轮询期间浏览器崩了/页面被导航没了 —— 同样是"没登成"，不是"程序坏了"。
        print(f"浏览器交互中断：{exc}")
        return EXIT_LOGIN_EXPIRED

    if state is None:
        print(f"未在 {timeout:.0f} 秒内检测到登录成功（窗口关掉了，或一直停在登录页）。")
        # ★ 失败时【不落盘】：半成品凭证比没有凭证更难排查 ——
        #   它会让下一次 report 走到"凭证存在但立刻 302"，看起来像"刚存的就过期了"。
        print("登录态【没有】保存。可以重跑一次本命令。")
        return EXIT_LOGIN_EXPIRED

    rel = save_state(state)
    names = sorted(cookies_from_state(state, domains=(NCSS_COOKIE_DOMAIN,)))
    print(f"登录态已保存：{rel}")
    # ★ 把 cookie 名打出来，不只是"给您看看"：这正是 PLAN §M0 第 2 步要记的
    #   "登录成功后哪个 Cookie 名下发了"。跑一次 login 就顺手把 M0 的那一格填了。
    shown = ", ".join(names) if names else "（一个都没有！）"
    print(f"捕获到的 {NCSS_COOKIE_DOMAIN} cookie（{len(names)} 个）：{shown}")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "report":
        return cmd_report()
    if args.command == "board":
        return cmd_board(args.days)
    if args.command == "login":
        return cmd_login(args.timeout)
    # argparse 的 required=True 保证走不到这里。写成显式 raise 而不是
    # return EXIT_OK：将来加了子命令却忘了接分支时，宁可当场炸，
    # 也不要静默"成功"。
    raise ValueError(f"未接线的子命令：{args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
