"""退避重试 —— 编排层加固的第一块（M4a）。

为什么重试必须【集中在这一层】：
  · openai_compat.py 把 SDK 自带的 max_retries 关成了 0；
  · sources/ 两个实现也从不自己重试。
  两处都是刻意的。如果 SDK 偷偷重试 3 次、外层再重试 3 次，真实请求数
  是 9 次（放大平方），而 trace 里只看到 1 次失败 —— 既烧钱又查不出真相。

★ 能不能重试的判据，不是"失败了"，而是"失败了、且重发是安全的"。
  "安全" = 幂等：重发一次不会让副作用发生两遍。
  所以策略表是按 ToolSpec.side_effect 派发的，不是按异常类型派发 ——
  异常告诉我们"出了什么事"，side_effect 才告诉我们"重发安不安全"。
  两个问题，两张表，别混。

★ 这个模块【不许】import openai / httpx / playwright。
  它只认 agent.llm.base 与 tools.sources.base 里的异常类 ——
  那两个模块已经把厂商异常翻译成中立词汇了
  （httpx 是 M2 做到的，openai 是 M4 补上的）。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from agent.llm.base import LLMTransientError
from agent.tools.sources import TransientSourceError

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """一个操作的重试策略。"""

    attempts: int = 3
    """总次数（【含首次】）。attempts=1 表示"不重试"，不是"重试 1 次"。
    ★ 这个措辞容易歧义，所以在代码里一律叫 attempts 而不是 max_retries。"""

    base_delay: float = 1.0
    """首次退避的基数（秒）。之后按 2 的幂增长。"""

    cap: float = 30.0
    """单次退避的上限（秒）—— 防止 attempts 多时退避长到离谱。"""

    jitter: bool = True
    """是否加抖动。默认 True；关掉它的正当理由只有"调试时想看确定的序列"。"""

    def __post_init__(self) -> None:
        # ★ 让非法的策略在【构造时】就炸，而不是在 retry_call 里
        #   变成一条"重试完成，但未捕获到异常"的假消息。
        #   attempts=0 时 for 循环一次都不进，原代码于是抛 RuntimeError ——
        #   那句话在撒谎（异常没有丢，是策略写错了），把"配错了"伪装成
        #   "运行期意外"。校验放在能放的最早位置是这个项目一贯的做法
        #   （对照 registry：空描述的工具在 import 期就报错）。
        if self.attempts < 1:
            raise ValueError(f"attempts 至少为 1（attempts=1 表示不重试），收到 {self.attempts}")


DEFAULT_POLICY = RetryPolicy()
"""默认策略的【单例】。

★ 为什么不在函数签名里直接写 `policy: RetryPolicy = RetryPolicy()`：
  默认值是在【函数定义时】求值一次的，不是在每次调用时。
  也就是说所有调用方【共享同一个对象】。RetryPolicy 现在是 frozen 的，
  所以共享也无害 —— 但 ruff 的 B008 照样会拦，因为
  "默认值是个可变/可共享对象"这个形状本身就是坑：
  哪天有人给 RetryPolicy 去掉 frozen 再加个字段，共享对象就会变成
  一处改、处处变的幽灵 bug。把这个坑的形状消掉（提起单例），
  比依赖"它现在恰好是 frozen 的"更稳。
"""


DEFAULT_RETRYABLE: tuple[type[BaseException], ...] = (
    LLMTransientError,
    TransientSourceError,
)
"""默认"值得重试"的异常。

★ 注意这里【只列瞬时类】—— 「永久」类（LLMPermanentError / LoginExpired /
  普通 SourceError）不在其中，于是天然不会被重试。
  把"别重试"做成【默认行为】而不是【额外判断】，
  这样新加一个异常类时，漏掉它的后果是"不重试"（安全），
  而不是"盲目重试"（危险）。默认值该偏保守。
"""


def _backoff_base(attempt: int, policy: RetryPolicy) -> float:
    """第 attempt 次失败后的【基准】延迟（不含抖动）：base·2^(attempt-1)，夹 cap。

    ★ 单独拆出来，是为了让"基准"与"抖动"各有唯一出处 ——
      见 _jitter 的注释。基准是可以被【覆盖】的：429 时服务端给的
      Retry-After 就顶替它（但同样要过 _jitter）。
    """
    return min(policy.cap, policy.base_delay * 2 ** (attempt - 1))


def _jitter(value: float, policy: RetryPolicy, rand: Callable[[], float]) -> float:
    """把基准值抖成最终值。★ 全项目【只此一处】做抖动。

    用 full jitter（AWS 推荐）：取 [0, value) 上的均匀随机数。
    为什么必须抖：所有客户端都做【同步】退避时，会在同一时刻一起回来，
    形成第二波冲击（retry storm / 惊群）—— 服务端刚缓过来又被同一批打垮。
    抖动把"同步"打散成"错峰"。
    """
    return rand() * value if policy.jitter else value


# ★ 这里【刻意没有】一个公开的 backoff_delay()（基准 + 抖动那个版本）。
#   它曾经存在，而它的唯一调用方是探针脚本 —— 也就是说生产代码一行都不用它。
#   原因不是它写错了，是它插不进正确的流水线：
#       Retry-After 进来之后，顺序必须是  基准 → 夹 cap → 抖，
#   而"基准 + 抖动"这个形状中间没有留夹 cap 的缝，正好绕开了 M4a-3 那个 bug。
#   它留着的真正害处是【把测试脚手架伪装成消费者】——看起来有人在用，
#   于是没人敢删。ruff 抓不到这种函数：顶层函数在语法上就是公开 API。


def retry_call(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    retry_on: tuple[type[BaseException], ...] = DEFAULT_RETRYABLE,
    sleep: Callable[[float], None] | None = None,
    rand: Callable[[], float] | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """反复调用 fn，直到成功、撞上不该重试的错、或次数用尽。

    返回：fn 的返回值。
    抛出：最后一次失败的那个异常，【原样抛出，不包装】——
      理由：上层（loop.py）要把异常翻译成 {"ok": false, "hint": ...} 回灌给
      模型，多包一层它就得先剥壳，而剥壳代码是纯负担。

    sleep / on_retry / rand 可注入，是为了让测试【不必真的等】：
      真等 1+2+4 秒的测试会被所有人嫌弃，最后没人跑；
      而没人跑的测试，等于没有测试。on_retry 则是 M6 trace 的插口
      （"第 2 次重试，等了 2.3 秒"必须留在痕迹里，否则查不出限流）。
      rand 注入则是让"退了多久"这个断言可复现 —— 不注入就只能
      断言"睡过"，断言不了"睡对了"。

    ★ 为什么默认值是 None 而不是直接写 `= time.sleep`：
      默认值在【函数定义时】求值并绑定，一旦写成 `= time.sleep`，
      它就永远指向"导入那一刻的那个 sleep 函数对象"——
      于是 `patch("time.sleep")` 对这里【完全无效】（实测：照样真睡一秒）。
      而真正的调用方（jobs.py）不可能自己传 sleep，所以测试若想
      不真等，就只剩"patch 全局 time.sleep"这一条路 —— 那条路必须留着。
      写成 None、在调用时解析，patch 才有效。代价是多一个分支。

    ★ 三条判据，按顺序处理（retry_on 之外的异常压根不进 except，
      所以"永久错误不重试"是靠 except 子句本身表达的，不需要额外判断）：
        a) fn() 成功               → 立刻返回它的值。
        b) 抛 retry_on 之内的异常  → 记下，还有次数就退避后重试；
           次数用尽则原样抛【最后一个】。
        c) 抛 retry_on 之外的异常  → 从这里穿出去，原样上抛。
           这就是 LLMPermanentError / LoginExpired 的用处：
           401 重试三次毫无意义，"登录过期"重试三次只会被锁号。

    ★ 为什么不能在 except 里直接 raise（"一抛就重试"）：
      那样第一个瞬时异常就出去了，后面几次尝试根本不会发生 ——
      重试代码等于没写。所以异常只【记下】不抛，等次数真的用尽再抛，
      而且抛的是【最后一个】：报得准。（呼应：assert 那句只为类型收窄。）

    ★ Retry-After 覆盖的是【基准值】，不是最终值：
      · 先 min(base, policy.cap) 夹上限 —— 对方说 3600 秒不能照办，得有止损；
      · 再抖一次 —— 否则所有被限流的客户端会在同一秒一起回来，
        而这正是 Retry-After 最容易被用错的地方（照抄服务端的数字，
        全世界的客户端就整齐地同步了）。
      顺序不能反：先抖再夹，等于 cap 之内又变回同步。
    """
    sleeper = sleep if sleep is not None else time.sleep
    rng = rand if rand is not None else random.random

    last_exc: BaseException | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            return fn()
        except retry_on as exc:
            last_exc = exc
            if attempt == policy.attempts:
                break
            retry_after = getattr(exc, "retry_after", None)
            base = retry_after if retry_after is not None else _backoff_base(attempt, policy)
            delay = _jitter(min(base, policy.cap), policy, rng)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            sleeper(delay)

    assert last_exc is not None
    raise last_exc


POLICY_BY_SIDE_EFFECT: dict[str, RetryPolicy] = {
    # read：不改任何状态，重发多少次结果都一样 —— 放心重试。
    "read": RetryPolicy(attempts=3, base_delay=2.0, cap=10.0),
    # write：副作用会重复，但安全性来自【存储层的设计】而非这一行 ——
    #   M5 的 upsert_jobs 用 INSERT ... ON CONFLICT DO UPDATE，
    #   同一条数据重放 3 次仍然只有一行。改 upsert 的写法时，
    #   这个"3 次"会跟着失效 —— 依赖关系在这里，别忘了。
    "write": RetryPolicy(attempts=3, base_delay=2.0, cap=10.0),
    # external：发飞书。飞书 API 【没有幂等键】，重发一次就是两条消息
    #   推给全组。所以这里刻意 attempts=1（不重试）—— 不是漏写，是纪律：
    #   "没有幂等键就不许重试"。等 M5 的 pushes 表就位（本地记录
    #   "这条 run 的这个通知发过了吗"），才有资格按 PLAN.md:591 开到 2 次。
    "external": RetryPolicy(attempts=1),
}


def policy_for(side_effect: str) -> RetryPolicy:
    """按副作用等级取策略；未知等级保守处理 —— 不重试。

    ★ 为什么不给未知等级一个默认的 3 次：将来新加一个 side_effect 值时，
      静默地授予它"可重试"权限，比直接拒绝危险得多。
      这里宁可漏报（少重试一次）也不要误报（多发一条消息）。
    """
    return POLICY_BY_SIDE_EFFECT.get(side_effect, RetryPolicy(attempts=1))
