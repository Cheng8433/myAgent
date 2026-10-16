"""成本与健康度看板 —— 可观测的第三块（M6）。

三层痕迹各回答一个问题（见 trace.py / logs.py 顶部）：
  · app.log  —— 发生了什么（流水账）
  · trace    —— 为什么（一次 run 的完整回放）
  · 本模块   —— 整体健康度（把 runs / steps 里**能用 SQL 聚合**的那部分摊开）

★ 为什么是"脚本汇总"而不是图表（PLAN §M6 的原话："非必需图表"）：
  图表解决的是"看得爽"，而这一步要解决的是"**看得出不对劲**"。
  一次 `python -m app.main board` 先回答两个运维问题 ——
  「最近花了多少 token」和「最近哪次挂了、为什么」——
  这两个问题用一行 SQL 和一个循环就能回答，不需要一个前端。

★ 为什么看板读【库】而不是读 trace 文件：
  因为库里的那两个列（runs.prompt_tokens / runs.steps）本来就是为聚合而生的
  （判据见 StepRecord 的 docstring：进库的那部分 = 能用 SQL 聚合的那部分）。
  读 trace 文件要对几十个 JSONL 求和 —— 那是把"已经在库里算好的账"
  再手工重算一遍，而重算就会和库里的数对不上（同一事实两个出处）。

★ 为什么 SQL 里要 `datetime(...)` 包一层，而不是拿 started_at 直接比字符串：
  started_at 是 `2026-10-07T12:00:00.123456+00:00`（带 T、带微秒、带偏移），
  而 `datetime('now','-7 days')` 返回 `2026-09-30 12:00:00`（空格分隔、无偏移）。
  两种格式的字符串比较，在"日期不同"时恰好蒙对（'1' > '0' 先分胜负），
  在"同一天不同时刻"时则靠 'T'(0x54) > ' '(0x20) 这个纯属巧合的字符序 ——
  也就是说它**看起来能跑**，边界那一次却会悄悄错一格。
  实测本机 SQLite 3.45.3 的 `datetime()` 能吃带偏移的 ISO8601（已验），
  所以两边都过一遍 datetime() 归一化到同一个形状再比 —— 不靠巧合。
"""

from __future__ import annotations

import sqlite3
from typing import Any

# 日聚合：一天的跑了几次、成几次败几次、烧了多少 token、平均几步。
# ★ 分组键用 date()，条件用 datetime() —— 前者只要"哪天"，后者要"到秒"的比较。
#   date()/datetime() 都会自己把 `+00:00` 偏移算进去（全库时间都是 UTC，
#   所以算完还是 UTC 的日期；将来真出现本地时间戳，这里也仍然是对的）。
_DAILY_SQL = """
    SELECT date(started_at)                        AS day,
           count(*)                                AS runs,
           sum(status = 'success')                 AS ok,
           sum(status = 'failed')                  AS failed,
           sum(status = 'no_items')                AS no_items,
           sum(prompt_tokens + completion_tokens)  AS tokens,
           avg(steps)                              AS avg_steps
      FROM runs
     WHERE datetime(started_at) > datetime('now', ?)
     GROUP BY day
     ORDER BY day
"""

# 最近的失败：只挑有 error 原文的那些（M6 起 runs.error 才有值）。
# ★ 为什么按 COALESCE 排序而不是 finished_at：finished_at 理论上可能是 NULL
#   （写了一半的 run），排到最后比排到最前更符合"最近发生了什么"的直觉。
_RECENT_ERRORS_SQL = """
    SELECT run_id, status, started_at, finished_at, error
      FROM runs
     WHERE error IS NOT NULL
     ORDER BY COALESCE(finished_at, started_at) DESC
     LIMIT ?
"""


def cost_summary(conn: sqlite3.Connection, *, days: int = 7) -> list[dict[str, Any]]:
    """最近 `days` 天的逐日汇总，按日期升序。返回 list[dict]。

    ★ `days` 在这里【校验】而不是让 SQL 去吞：它是命令行参数（外部输入），
      而 `datetime('now', '-0 days')` 会静静返回"只有今天之后"的结果 ——
      一个 0 或负数不会报错，只会让看板显得"最近什么都没跑"。
      边界上的输入就该在边界上拦住（同 config.py 的 required_str）。
    """
    if days < 1:
        raise ValueError(f"days 必须是正整数，收到 {days!r}")
    rows = conn.execute(_DAILY_SQL, (f"-{days} days",)).fetchall()
    # ★ 转成 dict 再交出去，而不是把 sqlite3.Row 漏给调用方：
    #   Row 只在连接活着时好用，且它的"取不到就 IndexError"是个陷阱。
    #   一个普通 dict 谁都能读、能 json.dumps、能断言相等。
    return [dict(row) for row in rows]


def recent_errors(conn: sqlite3.Connection, *, limit: int = 10) -> list[dict[str, Any]]:
    """最近 `limit` 条带错误原文的 run。健康度的另一半。"""
    if limit < 1:
        raise ValueError(f"limit 必须是正整数，收到 {limit!r}")
    rows = conn.execute(_RECENT_ERRORS_SQL, (limit,)).fetchall()
    return [dict(row) for row in rows]


def render_board(
    daily: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    *,
    days: int,
) -> str:
    """把两份汇总渲染成人读的一段文本（给 stdout）。

    ★ 返回值是【字符串】而不是直接 print：这样验收脚本能断言内容，
      而 app/main.py 负责"打印"这个副作用。同 report.py 的 render/write 分工。
    """
    lines: list[str] = [f"=== 最近 {days} 天 ==="]
    if not daily:
        # ★ 空的时候【说清楚是空】而不是打一个只有表头的表：
        #   "没有数据"和"表头下面没了"看起来一样，但前者是结论、后者像故障。
        lines.append("（这段时间没有任何 run）")
    else:
        lines.append(
            f"{'日期':<12}{'runs':>5}{'ok':>5}{'failed':>8}{'no_items':>10}{'tokens':>10}{'avg_steps':>11}"
        )
        total_runs = total_ok = total_failed = total_no_items = total_tokens = 0
        for row in daily:
            tokens = row["tokens"] or 0
            avg_steps = row["avg_steps"]
            avg_text = "—" if avg_steps is None else f"{avg_steps:.1f}"
            lines.append(
                f"{row['day']:<12}{row['runs']:>5}{row['ok']:>5}{row['failed']:>8}"
                f"{row['no_items']:>10}{tokens:>10}{avg_text:>11}"
            )
            total_runs += row["runs"]
            total_ok += row["ok"]
            total_failed += row["failed"]
            total_no_items += row["no_items"]
            total_tokens += tokens
        lines.append(
            f"{'合计':<12}{total_runs:>5}{total_ok:>5}{total_failed:>8}"
            f"{total_no_items:>10}{total_tokens:>10}{'':>11}"
        )

    lines.append("")
    lines.append("=== 最近的失败 ===")
    if not errors:
        lines.append("（没有带错误的 run —— 挺好）")
    else:
        for row in errors:
            when = row["finished_at"] or row["started_at"]
            lines.append(f"[{row['status']}] {when}  {row['run_id']}")
            lines.append(f"    {row['error']}")
    return "\n".join(lines)
