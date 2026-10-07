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
import sys
from typing import TYPE_CHECKING

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

★ 3 现在【走不到】：remoteok 无登录态，不会抛 LoginExpired。
  它先放在这里，是因为它属于【对外契约】（PLAN §M2 与调用方约好的），
  不是"声明了没人读"的空钩子 —— M2b 接 ncss 之后它就活了。
"""


def build_parser() -> argparse.ArgumentParser:
    """子命令表。

    ⤵ 将来会有第 2 个子命令 `login`（M2b 的 Playwright 半自动登录）——
      插在下面，紧跟着 report。tools/jobs.py 给模型的 hint 里已经写着
      `python -m app.main login`，接口先留着，实现到 M2b 补。
    """
    parser = argparse.ArgumentParser(prog="app.main", description="招聘信息日报 Agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("report", help="跑一次日报：抓取 → 入库 → 摘要 → 落盘")
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
    from workflows.daily_report import STATUS_FAILED, STATUS_NO_ITEMS, STATUS_SUCCESS

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
    from agent.runtime.budget import Budget
    from workflows.daily_report import run_daily_report

    # ★ 先 load_dotenv 再碰任何 getenv：配置只在 .env 里，os.environ 里当然没有。
    #   （m3_verify 就在这上面栽过 —— .env 里明明有 key，"没 key 就跳过"那一支
    #     却永远成立，于是端到端那项永远静默跳过、末尾照样打 ✅。跳过必须是响的。）
    load_dotenv()

    conn = connect()
    try:
        ensure_schema(conn)  # 幂等，每次启动无脑调一次
        report = run_daily_report(
            conn,
            OpenAICompatProvider(),
            run_id=new_run_id(),
            budget=Budget.from_env(),
        )
    finally:
        # ★ finally 而不是"正常路径上 close"：中途抛异常时连接才不会漏掉。
        #   进程退出会回收 fd，但显式关掉是把"这里就是它的终点"写在代码上。
        conn.close()

    return _report_outcome(report)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "report":
        return cmd_report()
    # argparse 的 required=True 保证走不到这里。写成显式 raise 而不是
    # return EXIT_OK：将来加了子命令却忘了接分支时，宁可当场炸，
    # 也不要静默"成功"。
    raise ValueError(f"未接线的子命令：{args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
