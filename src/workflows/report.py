"""日报的渲染与落盘 —— 这一层是【纯确定性代码】。

★ 为什么岗位表必须由代码渲染，而不是让模型写：
  PLAN §M0 的验收 B/C 要求"每条岗位都能点开链接"。交给模型写的话，
  它可能顺手合并同类项、漏掉一条、把 url 抄错一位 —— 而这些都不报错，
  只会让报告"看起来差不多"。表格由代码生成，三个字段由【构造】保证，
  而不是靠 Prompt 祈求模型守规矩。
  模型只做它真正擅长的那件事：用两三句话概括今天这批岗位。
  一句话：**能用代码确定的事，不要让模型去猜。**

★ 本模块【不】import runtime / llm / sqlite：它只认识 Job 和文件系统。
  渲染函数是纯函数（同样的输入 → 同样的字节），所以它能被离线测试断言到底。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.memory.store import var_dir
from agent.tools.sources.base import Job

CN_TZ = timezone(timedelta(hours=8))
"""东八区固定偏移（+08:00）。

★ 为什么不用 `ZoneInfo("Asia/Shanghai")`：本机 venv 未装 tzdata，
  实测抛 `ZoneInfoNotFoundError: No time zone found with key Asia/Shanghai`。
  而中国全境【没有夏令时】，固定 +08:00 是【诚实】而不是凑合 ——
  它表达的事实与实际永远一致。为一个恒定的偏移装 tzdata 依赖，不划算。
"""

_NO_SUMMARY = "_（本次没有生成摘要）_"
"""摘要缺席时的占位。

★ 写占位而不是留空：读报告的人要能分清
  「今天没有摘要」（→ 这个占位）和「渲染漏了一段」（→ 空白）。
  这也是本模块那条纪律的延续：诚实地说"没有"，别让读者去猜。
"""

_TEMPLATE = """# 招聘信息日报 · {day}

> 生成于 {generated_at} · run_id `{run_id}` · 共 {n} 条

{body}

## 岗位清单

{table}
"""

_MISSING_LINKS = "\n> ⚠️ 其中 {n} 条没有直接链接，已在表中用 `—` 标出。\n"


def report_day(*, now: datetime | None = None) -> str:
    """今天是几号（东八区），形如 "2026-10-04"。报告文件名用它。

    ★ now 可注入是为了让测试能断言"跨零点会翻页"这类边界 ——
      取时间的东西一旦写死成 datetime.now()，测试就只能断言"它非空"。
    """
    moment = now if now is not None else datetime.now(CN_TZ)
    if moment.tzinfo is None:
        # 传进来的裸 datetime 一律当【东八区】解释。
        # 不这么写的话，astimezone() 会按本机时区猜 —— 同一份代码在两台机器上
        # 得到差一天的文件名，而且不报错。
        moment = moment.replace(tzinfo=CN_TZ)
    return moment.astimezone(CN_TZ).strftime("%Y-%m-%d")


def report_path(day: str, *, root: str | Path | None = None) -> Path:
    """报告文件的位置：`<VAR_DIR>/reports/<day>.md`。

    ★ 目录复用 memory/store.py 的 var_dir()，不另起一套环境变量 ——
      "运行时数据都在 var/ 下"是一个事实，只该有一个出处。
    """
    return var_dir(root) / "reports" / f"{day}.md"


def write_report(markdown: str, *, day: str, root: str | Path | None = None) -> str:
    """把日报写到 `var/reports/<day>.md`，返回它【相对 var/】的路径。

    ★ 返回值是相对路径且 as_posix()：与 archive_raw 同一条纪律 ——
      绝对路径进日志/进库，仓库一搬家就静默失效；反斜杠漏进去，
      换台机器就读不出来。（这个返回值会进 StepRecord.detail 和 stdout。）

    ★ `write_text` 必须显式 `encoding="utf-8"`：本机默认编码是 GBK，
      不写的话一个中文标题就当场 UnicodeEncodeError
      （与需要 PYTHONUTF8=1 是同一个坑，只是这次它藏在库函数默认值里）。

    ★ `mkdir(exist_ok=True)` 天然幂等：不必先判断"目录在不在"——
      先判断后创建本身就是个竞态（两次 run 撞在一起时）。

    ★ 覆盖写而不是追加：同一天跑两次，报告应该是【最新那次】的样子。
      追加的话第二份会挂在第一份后面，而那正是"连跑两次结果一致"
      （PLAN §M0 验收）要拦的东西 —— 幂等不是"不写"，是"写出来的东西一样"。
    """
    target = report_path(day, root=root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    return target.relative_to(var_dir(root)).as_posix()


def _flat(text: str) -> str:
    """把一个字段洗成能安全放进【一行】表格的样子。

    ① `|` → `\\|`：不转义，【列】会被切断。
    ② `\\r\\n` / `\\r` / `\\n` → 一个空格：不压掉，【行】会被劈成两行。
       ★ 顺序要紧：必须先处理 `\\r\\n`。反过来的话，一个 Windows 折行
         会被换成【两个】空格 —— 不报错，但表格里凭空多出一道缝。
    这两种破坏 Markdown 都【不报错】，只会一声不响地把表渲染坏，
    所以只能靠这里兜住，兜不住也没有任何东西会提醒你。
    """
    return text.replace("|", "\\|").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def _render_table(jobs: Sequence[Job]) -> tuple[str, int]:
    """把岗位渲染成 Markdown 表格。返回 (表格文本, 缺链接的条数)。

    要达成的不变量（每条都由 scripts/m4b_verify.py 盯着）：
      ① 【每一条】job 都要占一行 —— 不许写 `if not job.url: continue`。
         日报的职责是反映【今天抓到了什么】，包括抓到的次品。
         悄悄跳过一条，读报告的人不会知道库里多了一个报告里查不到的岗位。
      ② url 为空 → 链接列写 `—`（一个全角破折号），不是空单元格。
         空单元格看起来像渲染 bug；`—` 看起来像"这条就是没有链接"。
         两者给读者的是完全不同的信号，而读者是拿它做决定的人。
      ③ title / company 里的 `|` 要转义成 `\\|`，换行要压成空格 ——
         否则一个岗位标题就能把整张表拆成两半，而 Markdown 不会报错，
         它只会一声不响地把这张表渲染坏。
      ④ 把缺链接的【条数】返回给调用方（它负责写文末那行脚注）——
         表格的职责只是表格；"要不要提数据质量"是文档层的事。

    表头（照抄，别改列名 —— 下游可能有人按列名解析）：
        | # | 岗位 | 公司 | 链接 |
        |---|------|------|------|
    行形如（`#` 从 1 开始）：
        | 1 | 后端工程师 | 某公司 | [打开](https://example.com/x) |

    提示：行首行尾都带 `|`；行与行之间用 "\\n" 拼起来（返回值末尾【不要】
    多加一个换行 —— 模板里已经留好了）。
    """
    # ★ 用 list + join，而不是在循环里 `message += ...`：拼接版每行都拖着一个
    #   尾随的 "\n"，最后一行也不例外 —— 返回值末尾就多出一个换行，模板里
    #   那个 {table} 后面便凭空多出一行空白。join 只在【行与行之间】放分隔符。
    rows: list[str] = ["| # | 岗位 | 公司 | 链接 |", "|---|------|------|------|"]
    lack_url = 0
    for seq, job in enumerate(jobs, start=1):
        # ★ 洗字符交给 _flat：`|` 切断列、换行劈开行，Markdown 两者都不报错。
        #   ★ 也别忘了 str 不可变：replace() 改的是【返回值】，必须用 `=` 接住。
        title = _flat(job.title)
        company = _flat(job.company)
        if job.url:
            link = f"[打开]({job.url})"
        else:
            # 缺链接【不是】"跳过这条"，而是"这条没有链接" —— 写一个全角 `—`。
            # 空单元格看起来像渲染 bug，`—` 看起来像一条事实。
            lack_url += 1
            link = "—"
        rows.append(f"| {seq} | {title} | {company} | {link} |")

    return "\n".join(rows), lack_url


def render_report(
    jobs: Sequence[Job],
    *,
    day: str,
    summary: str,
    run_id: str,
    generated_at: str,
) -> str:
    """把这一批岗位渲染成一份完整的 Markdown 日报，返回整篇正文。

    ★ 为什么 day 是一个【参数】、不在这里 datetime.now()：
      文件名（write_report）和标题（这里）说的必须是同一天。
      两边各取一次 now，就可能跨过午夜 —— 标题写着 10-04、文件却叫
      10-05.md，而两边都不报错。
      （M4b 计划的签名里没有 day，原想从 generated_at 截前 10 位 ——
        那是【另一个日期源】：generated_at 是 UTC，day 是东八区，
        晚上 8 点以后两者差一天。故签名加了 day，这条记在这儿别改回去。）

    ★ generated_at 由调用方传入，也是同一个理由：渲染函数要能被离线测试
      断言"它把这个时间写对了"；datetime.now() 藏在函数里的话，
      测试就只能断言"它非空"—— 那种断言等于没断言。

    格式见模块顶部的 _TEMPLATE（表头/摘要/清单的顺序已经定好）。
    """
    table, missing = _render_table(jobs)
    body = summary.strip() or _NO_SUMMARY
    doc = _TEMPLATE.format(
        day=day,
        generated_at=generated_at,
        run_id=run_id,
        n=len(jobs),
        body=body,
        table=table,
    )
    # ★ missing 为 0 时【不写】脚注：写一句"0 条缺链接"是纯噪音，
    #   它把一份健康的报告弄得像是出过问题。
    return doc + (_MISSING_LINKS.format(n=missing) if missing else "")
