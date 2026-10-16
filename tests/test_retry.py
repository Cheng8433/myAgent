"""重试单测 —— 策略【按副作用派发】，不是按异常类型。

★ 两个问题两张表：异常说"出了什么事"，side_effect 才说"重发安不安全"。
  所以这里既断言"read 重试 3 次 vs external 1 次"，也断言"永久错误一次都不重试"
  （后者靠 retry_on 的 except 子句表达，不需要额外分支 —— 那正是分类的价值）。
★ 抖动只落在【一处】、且顺序是 基准 → 夹 cap → 抖：断言 sleeper 收到的序列，
  而不是"睡过"。注入 rand/sleep 就是为了让"退了多少"可复现 ——
  不注入就只能断言"睡过"，那等于没断言。
"""

from __future__ import annotations

import pytest

from agent.llm.base import LLMPermanentError, LLMRateLimited, LLMTransientError
from agent.runtime.retry import RetryPolicy, policy_for, retry_call
from agent.tools.sources.base import TransientSourceError


def _always_raise(exc: BaseException):
    def fn():
        raise exc

    return fn


def _counting(exc: BaseException):
    """返回 (函数, 计数器)。函数每被调用一次，计数器 +1。"""
    box = {"n": 0}

    def fn():
        box["n"] += 1
        raise exc

    return fn, box


def test_policy_dispatch_by_side_effect():
    assert policy_for("read").attempts == 3
    assert policy_for("write").attempts == 3
    assert policy_for("external").attempts == 1  # 飞书没有幂等键 → 不许重试
    assert policy_for("nonsense").attempts == 1  # 未知等级 → 保守，不重试


def test_attempts_below_one_rejected_at_construction():
    """非法的策略在【构造时】就炸，而不是在 retry_call 里变成一条撒谎的消息。"""
    with pytest.raises(ValueError):
        RetryPolicy(attempts=0)


def test_transient_retries_three_times():
    fn, box = _counting(TransientSourceError("抖"))
    with pytest.raises(TransientSourceError):
        retry_call(fn, policy=RetryPolicy(attempts=3), sleep=lambda _s: None, rand=lambda: 0.0)
    assert box["n"] == 3


def test_external_never_retries():
    fn, box = _counting(TransientSourceError("抖"))
    with pytest.raises(TransientSourceError):
        retry_call(fn, policy=policy_for("external"), sleep=lambda _s: None, rand=lambda: 0.0)
    assert box["n"] == 1


def test_permanent_error_not_retried():
    fn, box = _counting(LLMPermanentError("401 鉴权失败"))
    with pytest.raises(LLMPermanentError):
        retry_call(fn, sleep=lambda _s: None, rand=lambda: 0.0)
    assert box["n"] == 1


def test_success_returns_immediately():
    assert retry_call(lambda: "值", policy=RetryPolicy(attempts=3)) == "值"


def test_backoff_schedule_and_single_jitter():
    """两次退避 = 1.0、2.0 的基准各抖一次 → ×0.5 得 0.5、1.0。"""
    slept: list[float] = []
    with pytest.raises(TransientSourceError):
        retry_call(
            _always_raise(TransientSourceError("x")),
            policy=RetryPolicy(attempts=3, base_delay=1.0, cap=10.0, jitter=True),
            sleep=slept.append,
            rand=lambda: 0.5,
        )
    assert slept == [0.5, 1.0]


def test_retry_after_is_clamped_then_jittered():
    """Retry-After 覆盖【基准值】：先夹 cap，再抖 —— 顺序反了就是把同步退避挪进 cap 里。"""
    slept: list[float] = []
    with pytest.raises(LLMRateLimited):
        retry_call(
            _always_raise(LLMRateLimited("429", retry_after=3600.0)),
            policy=RetryPolicy(attempts=2, cap=10.0, jitter=True),
            sleep=slept.append,
            rand=lambda: 1.0,
        )
    assert slept == [10.0]  # min(3600, 10) = 10，再 ×1.0


def test_transient_base_class_retries_llm_error():
    fn, box = _counting(LLMTransientError("超时"))
    with pytest.raises(LLMTransientError):
        retry_call(fn, policy=RetryPolicy(attempts=2), sleep=lambda _s: None, rand=lambda: 0.0)
    assert box["n"] == 2
