"""Trace 落盘 —— 可观测的第一块（M6）。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent.memory.store import var_dir

if TYPE_CHECKING:
    from agent.runtime.loop import StepRecord

TRACE_DIR = "traces"

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
    """这次 run 的 trace 文件位置：`<VAR_DIR>/traces/run-<run_id>.jsonl`。"""
    return var_dir(root) / TRACE_DIR / f"run-{run_id}.jsonl"


def _line(run_id: str, record: StepRecord) -> dict[str, Any]:
    """把一条 StepRecord 摊成 trace 的一行。"""
    line: dict[str, Any] = {
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
    if record.error:
        line["error"] = record.error

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
    """把一次 run 的全部留痕写成 `var/traces/run-<run_id>.jsonl`，返回相对路径。"""
    target = trace_path(run_id, root=root)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(_line(run_id, record), ensure_ascii=False) + "\n")
    return target.relative_to(var_dir(root)).as_posix()
