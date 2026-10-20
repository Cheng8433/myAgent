"""预算单测 —— 三闸的【固定顺序】与 from_env 的三路。"""

from __future__ import annotations

import pytest

from agent.runtime import budget as budget_mod
from agent.runtime.budget import (
    GATE_MAX_STEPS,
    GATE_MAX_TOKENS,
    GATE_WALL_CLOCK,
    Budget,
)


def test_all_three_hit_reports_first():
    b = Budget(max_steps=1, max_tokens=1, max_wall_seconds=0.0)
    assert b.exceeded(steps=5, tokens=999, wall_seconds=99.0) == GATE_MAX_STEPS


def test_tokens_wins_over_wall():
    b = Budget(max_steps=999, max_tokens=1, max_wall_seconds=0.0)
    assert b.exceeded(steps=0, tokens=999, wall_seconds=99.0) == GATE_MAX_TOKENS


def test_wall_alone():
    b = Budget(max_steps=999, max_tokens=10**9, max_wall_seconds=0.0)
    assert b.exceeded(steps=0, tokens=0, wall_seconds=0.0) == GATE_WALL_CLOCK


def test_boundary_is_inclusive():
    """判据是 >=，不是 >：踩在上限上就算撞破（否则会多跑一轮）。"""
    hit = Budget(max_steps=1, max_tokens=10**9, max_wall_seconds=1e9)
    assert hit.exceeded(steps=1, tokens=0, wall_seconds=0.0) == GATE_MAX_STEPS
    ok = Budget(max_steps=2, max_tokens=10**9, max_wall_seconds=1e9)
    assert ok.exceeded(steps=1, tokens=0, wall_seconds=0.0) is None


def test_no_gate_hit():
    b = Budget(max_steps=5, max_tokens=100, max_wall_seconds=10.0)
    assert b.exceeded(steps=4, tokens=99, wall_seconds=9.999) is None


def _no_dotenv(monkeypatch):
    """顶掉 load_dotenv —— 否则主人 .env 里的值会盖住"变量缺失"那条路。"""
    monkeypatch.setattr(budget_mod, "load_dotenv", lambda: None)


def test_from_env_missing_raises(monkeypatch):
    _no_dotenv(monkeypatch)
    for name in ("AGENT_MAX_STEPS", "AGENT_MAX_TOKENS", "AGENT_WALL_CLOCK_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError) as excinfo:
        Budget.from_env()
    assert "AGENT_MAX_STEPS" in str(excinfo.value)


def test_from_env_reads_all(monkeypatch):
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("AGENT_MAX_STEPS", "7")
    monkeypatch.setenv("AGENT_MAX_TOKENS", "1234")
    monkeypatch.setenv("AGENT_WALL_CLOCK_SECONDS", "2.5")
    b = Budget.from_env()
    assert (b.max_steps, b.max_tokens, b.max_wall_seconds) == (7, 1234, 2.5)


def test_from_env_empty_string_raises(monkeypatch):
    """空串不是"没配"，是"配错了" —— 静默回退就是"少算的账本"。"""
    _no_dotenv(monkeypatch)
    monkeypatch.setenv("AGENT_MAX_STEPS", "")
    monkeypatch.setenv("AGENT_MAX_TOKENS", "1")
    monkeypatch.setenv("AGENT_WALL_CLOCK_SECONDS", "1")
    with pytest.raises(ValueError) as excinfo:
        Budget.from_env()
    assert "AGENT_MAX_STEPS" in str(excinfo.value)
