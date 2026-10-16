"""Trace 落盘 —— 可观测的第一块（M6）。

一次 run 一个文件：`var/traces/run-<run_id>.jsonl`，一行一条留痕（JSON Lines）。
它回答的问题是【日志回答不了的那个】：不是"发生了什么"（日志已经说了
"日报失败"），而是"**为什么**失败"——失败了，那这一轮到底走到哪、慢在哪一步、
模型点了几单、哪一单崩了。PLAN §M6 的自测题问的就是这个：
「某天日报没生成，只给你 trace 文件，你能定位是登录挂还是 LLM 挂吗？」

★ 为什么是【一行一个 JSON】而不是一个大 JSON 数组：
  数组必须整个解析出来才看得见一行坏没坏 —— 而 trace 最常被读的时刻，
  恰恰是那次 run 半路死掉、文件可能没写完的时候。JSONL 是"坏了一行也只坏一行"，
  `tail -1` / `grep` / 逐行 json.loads 都能用（排查时你手上通常只有 shell）。

★ 为什么文件名带 run_id、而不是所有 run 追加进一个 trace.jsonl：
  一次 run 一个文件，才能"整段替换"（同 memory/store.py 里 steps 的理由）——
  重跑同一个 run_id 时是覆盖，不是把两份混在一起。混在一起的那种文件，
  读起来像是"这个 run 跑了两遍"，而真相是"跑了两个 run"。

★ 为什么写文件这件事【不放在 runtime/loop.py】：
  见 loop.py 顶部"M6"那段——一句话，循环自己写文件会污染测试的 var/，
  而给它加个"要不要写"的开关，就等于承认"忘了传就静默丢 trace"。
  落盘是编排层的事：它才是那个"一次 run 从哪里开始、到哪里结束"的知情人。

★ 与 steps 表的分工（同一批留痕的两个去处，不是两本账）：
  · steps 表 —— 结构化的、能用 SQL 聚合的那部分（步数、token、时延、错误代号）。
    看板与告警查它。
  · 本文件 —— 同一批留痕的【完整】快照，含只进 trace 的细枝末节（model /
    tool_calls / args_digest）。要看"那一轮模型具体点了什么"时读它。
  两者由同一个 StepRecord 序列化而来（单一出处），只是一个投影进库、
  一个全量落盘 —— 所以不存在"对不上"的可能。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent.memory.store import var_dir

if TYPE_CHECKING:
    # ★ 只给类型检查器看：本模块【不】在运行时依赖 runtime。
    #   StepRecord 只出现在函数签名里，而 `from __future__ import annotations`
    #   会把注解变成字符串 —— 运行时根本不会去求值它。
    #   （和 memory/store.py 对 StepRecord 的处理是同一招，理由也一样：
    #     别让"可观测"反过来把"被观测的人"拖进运行期依赖。）
    from agent.runtime.loop import StepRecord

TRACE_DIR = "traces"

# trace 每一行的【固定列】。extra 里的键不许撞上它们 —— 见 _line。
_CORE_KEYS = frozenset(
    {
        "ts",
        "run_id",
        "step",
        "kind",
        "detail",
        "ok",
        "prompt_tokens",
        "completion_tokens",
        "latency_ms",
    }
)


def trace_path(run_id: str, *, root: str | Path | None = None) -> Path:
    """这次 run 的 trace 文件位置：`<VAR_DIR>/traces/run-<run_id>.jsonl`。

    ★ 目录复用 memory/store.py 的 var_dir()，不另起一套环境变量 ——
      "运行时数据都在 var/ 下"是一个事实，只该有一个出处（同 report.py）。
    """
    return var_dir(root) / TRACE_DIR / f"run-{run_id}.jsonl"


def _line(run_id: str, record: StepRecord) -> dict[str, Any]:
    """把一条 StepRecord 摊成 trace 的一行。"""
    line: dict[str, Any] = {
        # ★ ts 用记录自己带的（产生它的那一层打的，见 StepRecord.ts）。
        #   留空时退回"落盘时刻"—— 那是次优的：批量落盘会让整批同刻，
        #   "哪一步慢、顺序如何"就糊了。但它【不是撒谎】：这条留痕确实在这一刻
        #   被写下。所以退回而不是抛错 —— 手搓的测试记录不该逼着填 ts。
        "ts": record.ts or datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "step": record.step,
        "kind": record.kind,
        "detail": record.detail,
        "ok": record.ok,
        "prompt_tokens": record.prompt_tokens,
        "completion_tokens": record.completion_tokens,
        "latency_ms": record.latency_ms,
    }
    # ★ error 为空【不写这个键】：空串与"没有这个字段"在 JSON 里是两件事，
    #   而下游 `if "error" in line` 才是想筛错误的人会写的判据。
    #   同 runs.error 写 NULL 不写 ""（memory/store.py）。
    if record.error:
        line["error"] = record.error

    # ★ 挡一道键冲突：extra 里手滑塞了 "step" 的话，静默覆盖会把真正的 step
    #   顶掉，而 trace 看上去还挺正常。宁可当场炸，也别让一个错位的键
    #   去冒充一个核心字段（本项目一贯：让错的那个响，别让它静默生效）。
    clash = _CORE_KEYS & record.extra.keys()
    if clash:
        raise ValueError(
            f"StepRecord.extra 出现了保留键 {sorted(clash)} —— "
            f"它们会覆盖 trace 的固定列。请换个名字。"
        )
    line.update(record.extra)
    return line


def write_trace(
    run_id: str,
    records: Sequence[StepRecord],
    *,
    root: str | Path | None = None,
) -> str:
    """把一次 run 的全部留痕写成 `var/traces/run-<run_id>.jsonl`，返回相对路径。

    参数只有两样：这次的 run_id，和按发生顺序排好的记录。编排层把
    【自己的步骤】和【子 run 的步骤】拼成同一个列表交进来 —— 一个文件讲完
    整个故事，正是上面"一次 run 一个文件"的意思。

    ★ 覆盖写、不是追加：同一个 run_id 重写就该是同一份文件。
      追加的话，重跑一次会让文件里躺着两遍全过程，读起来像"这个 run 跑了两趟"。
      （同 report.write_report 的覆盖写，同 store.record_run 的整段替换。）

    ★ 返回值是相对 var/ 的正斜杠路径：绝对路径进日志/进库，仓库一搬家就静默
      失效（同 archive_raw、write_report）。它会进 StepRecord.detail 和 stdout。

    ★ `open(..., encoding="utf-8")` 不可省：本机默认 GBK，
      一个中文参数摘要就当场 UnicodeEncodeError（README 那条纪律）。
      `ensure_ascii=False` 也是同一个目的的另一半 —— 让文件里的人话
      保持是人话，排查时不用去过一遍 \\uXXXX。
    """
    target = trace_path(run_id, root=root)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(_line(run_id, record), ensure_ascii=False) + "\n")
    return target.relative_to(var_dir(root)).as_posix()
