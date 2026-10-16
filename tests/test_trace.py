"""trace 单测 —— 一行一条、保留键护栏、ts 的来源、覆盖写。

★ 保留键护栏（extra 撞固定列名 → ValueError）不是洁癖：静默覆盖会把真正的
  step 顶掉，而 trace 看上去还挺正常。这条雷是 m4b 真拦下过的（子 run 的 id
  想冒充整次 run 的 id），所以在这里留一条独立的回归。
"""

from __future__ import annotations

import json

import pytest

from agent.observability.trace import trace_path, write_trace
from agent.runtime.loop import StepRecord


def _lines(root, run_id):
    text = trace_path(run_id, root=root).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_extra_clashing_core_key_raises(tmp_path):
    with pytest.raises(ValueError):
        write_trace("r", [StepRecord(step=0, kind="llm", extra={"step": 9})], root=tmp_path)


def test_error_key_omitted_when_empty(tmp_path):
    """空串与"没有这个字段"是两件事：筛错误的判据是 `if "error" in line`。"""
    write_trace("r", [StepRecord(step=0, kind="llm", detail="stop")], root=tmp_path)
    assert "error" not in _lines(tmp_path, "r")[0]


def test_error_key_written_when_present(tmp_path):
    write_trace("r", [StepRecord(step=0, kind="tool", error="tool_timeout")], root=tmp_path)
    assert _lines(tmp_path, "r")[0]["error"] == "tool_timeout"


def test_ts_uses_records_own(tmp_path):
    ts = "2026-10-09T00:00:01.000000+00:00"
    write_trace("r", [StepRecord(step=0, kind="llm", ts=ts)], root=tmp_path)
    assert _lines(tmp_path, "r")[0]["ts"] == ts


def test_ts_falls_back_when_absent(tmp_path):
    """手搓的测试记录不该逼着填 ts —— 留空时退回落盘时刻（不抛）。"""
    write_trace("r", [StepRecord(step=0, kind="llm")], root=tmp_path)
    assert _lines(tmp_path, "r")[0]["ts"]


def test_extra_merged_into_line(tmp_path):
    write_trace(
        "r",
        [StepRecord(step=0, kind="llm", extra={"model": "deepseek-chat", "tool_calls": ["a"]})],
        root=tmp_path,
    )
    line = _lines(tmp_path, "r")[0]
    assert line["model"] == "deepseek-chat"
    assert line["tool_calls"] == ["a"]


def test_core_keys_present_on_every_line(tmp_path):
    core = {"ts", "run_id", "step", "kind", "detail", "ok", "prompt_tokens", "completion_tokens"}
    write_trace("r", [StepRecord(step=0, kind="llm")], root=tmp_path)
    assert core <= set(_lines(tmp_path, "r")[0])


def test_overwrite_not_append(tmp_path):
    two = [StepRecord(step=0, kind="llm"), StepRecord(step=0, kind="tool")]
    write_trace("r", two, root=tmp_path)
    write_trace("r", [StepRecord(step=0, kind="llm")], root=tmp_path)
    assert len(_lines(tmp_path, "r")) == 1  # 追加的话这里是 3，读起来像"跑了两趟"


def test_run_id_stamped_on_every_line(tmp_path):
    two = [StepRecord(step=0, kind="llm"), StepRecord(step=0, kind="tool")]
    write_trace("abc", two, root=tmp_path)
    assert {x["run_id"] for x in _lines(tmp_path, "abc")} == {"abc"}
