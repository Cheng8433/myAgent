"""M4a 验收台 —— 编排加固层：预算闸门 / 工具超时 / 退避重试。

用法：PYTHONUTF8=1 ./.venv/Scripts/python.exe scripts/m4_verify.py

★ 为什么这个脚本非存在不可：M4a 的三块（budget / timeout / retry）都是
  "平时看不见"的代码 —— 没撞上限时它们一行都不执行。而看不见的东西
  只能靠【把它逼到边界】来验证。所以这里全用假 Provider / 假时钟：
  不花钱、不联网、可复现，但真的走 loop.py 的每一行。

★ 时间也是外部依赖。测墙钟闸门若真 sleep，测出来的就不是"闸门对不对"，
  而是"这台机器的 sleep 精度多少"（Windows 上 sleep(0.02) 可能睡 15~30ms）。
  所以这里注入假时钟：假 Provider 每回答一轮就把时钟推一格 —— 墙钟的
  推进变成确定的，断言才能卡具体数字。
"""

from __future__ import annotations

import json
import os
import time
from unittest.mock import patch

import httpx
from pydantic import BaseModel

# ★ 必须 import 才会注册：@tool 装饰器靠 import 时的副作用往 TOOLS 里填。
#   少了这一行，openai_tools(["fetch_job_list"]) 直接 KeyError ——
#   报错位置在 registry.py，跟"忘了写 import"看起来毫无关系。
import agent.tools.jobs  # noqa: F401
from agent.llm.base import (
    LLMError,
    LLMRateLimited,
    LLMResponse,
    LLMTransientError,
    ToolCall,
)
from agent.memory.db import new_run_id
from agent.runtime.budget import Budget
from agent.runtime.loop import run_agent
from agent.runtime.retry import RetryPolicy, policy_for, retry_call
from agent.tools.registry import TOOLS, tool
from agent.tools.sources.base import SourceError, TransientSourceError
from agent.tools.sources.remoteok import RemoteOkSource

# 故意用一个【不存在】的工具名：工具层会回 unknown_tool，
# 于是既不联网也不碰真实数据源，但循环该走的每一行都走到了。
PROBE_TOOL = "probe_tool"
# 每轮参数都不同 —— 否则会先撞上 repeat_limit（loop_detected），
# 测出来的就不是预算闸门了。
PROBE_ARGS = '{{"page": {n}}}'

failures = 0


def check(label: str, got: object, want: object, note: str = "") -> None:
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"   {'OK ' if ok else 'BAD'} {label} → {got!r}   期望 {want!r}  {note}")


class FakeClock:
    """假时钟：只在被要求时才前进，于是"过了多久"完全可控。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class RoundScript:
    """假 Provider：台词是按轮次的。

    rounds=2 表示"前 2 轮还要工具，第 3 轮答完"；None 表示永远要工具。

    ★ 契约第 3 条（回填真实 token 用量）必须照做 —— 假 Provider 也要守契约，
      否则测的是假货的行为，不是真货的行为（M3 就在这栽过一次）。
    ★ M6 起契约还多了 `model`（trace 拿它做成本归因），所以这里也得报一个。
    """

    model = "roundscript-fake"

    def __init__(
        self,
        *,
        rounds: int | None = None,
        prompt_tokens: int = 100,
        completion_tokens: int = 10,
        clock: FakeClock | None = None,
        clock_advance: float = 0.0,
    ) -> None:
        self.calls = 0
        self._rounds = rounds
        self._pt = prompt_tokens
        self._ct = completion_tokens
        self._clock = clock
        self._advance = clock_advance

    def chat(self, messages, tools=None) -> LLMResponse:
        self.calls += 1
        if self._clock is not None:
            self._clock.now += self._advance

        if self._rounds is not None and self.calls > self._rounds:
            return LLMResponse(
                content="做完了",
                tool_calls=[],
                prompt_tokens=self._pt,
                completion_tokens=self._ct,
                finish_reason="stop",
            )
        return LLMResponse(
            content="我再来查一下",
            tool_calls=[
                ToolCall(
                    id=f"call_{self.calls}",
                    name=PROBE_TOOL,
                    arguments=PROBE_ARGS.format(n=self.calls),
                )
            ],
            prompt_tokens=self._pt,
            completion_tokens=self._ct,
            finish_reason="tool_calls",
        )


def run_with(llm: RoundScript, budget: Budget, *, clock: FakeClock | None = None):
    """跑一次 run_agent，必要时用假时钟顶掉 time.monotonic。"""
    if clock is None:
        return run_agent(
            "测试任务", llm, run_id=new_run_id(), tool_names=["fetch_job_list"], budget=budget
        )
    with patch("time.monotonic", new=clock):
        return run_agent(
            "测试任务", llm, run_id=new_run_id(), tool_names=["fetch_job_list"], budget=budget
        )


print("═══ 预算闸门（budget.py + loop.py 的接线）")

print("\n① 宽松预算不该误伤：模型第 2 轮答完就该正常结束")
llm = RoundScript(rounds=1)
res = run_with(llm, Budget(max_steps=20, max_tokens=200_000, max_wall_seconds=600.0))
check("status", res.status, "success", "闸门没撞到就不许插手")
check("模型被问了几次", llm.calls, 2)

print("\n② 步数闸门：max_steps=3 应该【允许跑满 3 轮】，第 4 轮前才停")
llm = RoundScript()
res = run_with(llm, Budget(max_steps=3, max_tokens=10**9, max_wall_seconds=10**9))
check("status", res.status, "max_steps_exceeded", "词汇表要一致")
check("实际跑了几轮", llm.calls, 3, "max_steps=3 → 允许 3 轮")

print("\n③ token 闸门：每轮 110，上限 250 → 第 4 轮开头发现 330 已超")
llm = RoundScript(prompt_tokens=100, completion_tokens=10)
res = run_with(llm, Budget(max_steps=99, max_tokens=250, max_wall_seconds=10**9))
check("status", res.status, "max_tokens_exceeded")
check("实际跑了几轮", llm.calls, 3, "110→220→330，第三轮跑完已超")
check("累计 token 如实汇总", res.prompt_tokens + res.completion_tokens, 330)

print("\n④ 墙钟闸门：每轮推时钟 0.1s，上限 0.15s → 第 3 轮开头发现 0.2 已超")
clock = FakeClock()
llm = RoundScript(clock=clock, clock_advance=0.1)
res = run_with(llm, Budget(max_steps=99, max_tokens=10**9, max_wall_seconds=0.15), clock=clock)
check("status", res.status, "wall_clock_exceeded")
check("实际跑了几轮", llm.calls, 2)

print("\n⑤ 两道闸同时撞破时，报哪一道【由固定顺序决定】")
# ★ 用例必须让两道闸在【同一次检查】里都成立，否则测的是"谁先撞"而不是"谁优先"。
#   第一次写这组用例时我按错误的轮次凑的数，于是它恰好在旧行为下通过、
#   在修好之后变红 —— 这是"断言编码了错语义"的经典形状。
#   第 2 轮开头：已跑完 1 轮、累计 110 token → max_steps=1 与 max_tokens=1 同时成立。
llm = RoundScript()
res = run_with(llm, Budget(max_steps=1, max_tokens=1, max_wall_seconds=10**9))
check("steps 与 tokens 同时撞", res.status, "max_steps_exceeded", "steps 在 tokens 之前")
check("实际跑了几轮", llm.calls, 1)

# 再把顺序反过来验一次：tokens 与 wall 同时撞 → 该报 tokens（tokens 在 wall 之前）。
# 只有两个方向都测过，才能排除"其实只有一个闸在起效"这种可能。
clock = FakeClock()
llm = RoundScript(clock=clock, clock_advance=0.1)
res = run_with(
    llm,
    Budget(max_steps=99, max_tokens=1, max_wall_seconds=0.05),
    clock=clock,
)
check("tokens 与 wall 同时撞", res.status, "max_tokens_exceeded", "tokens 在 wall 之前")

print("\n⑥ status 词汇表：循环报出来的每一个 status 都必须是文档里的那几种")
SEEN = {
    "success",
    "loop_detected",
    "max_steps_exceeded",
    "max_tokens_exceeded",
    "wall_clock_exceeded",
}
observed = set()
# ★ 三个字段现在【必填】（2026-10-07 起没有代码默认值），所以每处都写全。
for b in (
    Budget(max_steps=1, max_tokens=10**9, max_wall_seconds=10**9),
    Budget(max_steps=99, max_tokens=1, max_wall_seconds=10**9),
    Budget(max_steps=99, max_tokens=10**9, max_wall_seconds=0.0),
):
    observed.add(run_with(RoundScript(), b).status)
bogus = observed - SEEN
check("多出来的 status", bogus or "（无）", "（无）", "既有 max_steps_exceeded，别造出第二个写法")

print("\n⑦ from_env：三道闸【都是必填】，缺一个就抛错")
# ★ 这一节补的是"声明了但没人消费"的另一面：from_env() 自从写出来就没有
#   任何断言在盯它 —— 而没有断言 = 改坏了也不会有人知道。
#   假时钟在这里帮不上忙，要假的是【环境】：用 patch 把 load_dotenv 顶掉，
#   否则 .env 里的值会盖住"变量缺失"那条路 —— 正是它掩盖过一个真 bug
#   （裸 max_steps 引出的 UnboundLocalError，有 .env 时不报错）。
no_dotenv = patch("agent.runtime.budget.load_dotenv")

# ★ 2026-10-07 起这条断言【反过来】了：从前断言的是"缺失 → 取类字段默认值"，
#   而那正是被废掉的行为（同一个默认值两个住处 → 模板迟早撒谎）。
#   现在的要求是"缺失 → 当场报错"。断言跟着实现走，不是反过来。
with no_dotenv, patch.dict(os.environ, {}, clear=True):
    try:
        Budget.from_env()
    except ValueError as exc:
        check(
            "三个变量都缺失 → 抛错（不再有代码兜底）",
            "AGENT_MAX_STEPS" in str(exc),
            True,
            "静默用一个代码里的数，就是「少算的账本」—— 缺失必须响",
        )
    else:
        check("三个变量都缺失 → 抛错（不再有代码兜底）", "没炸", "ValueError")

with (
    no_dotenv,
    patch.dict(
        os.environ,
        {"AGENT_MAX_STEPS": "7", "AGENT_MAX_TOKENS": "1234", "AGENT_WALL_CLOCK_SECONDS": "2.5"},
    ),
):
    b = Budget.from_env()
    check("三个变量都在 → 照读", (b.max_steps, b.max_tokens, b.max_wall_seconds), (7, 1234, 2.5))

with no_dotenv, patch.dict(os.environ, {"AGENT_MAX_STEPS": ""}):
    try:
        Budget.from_env()
    except ValueError as exc:
        # ★ 附带卡住消息里有没有变量名：int("") 自带的报错【不说是哪个变量】，
        #   配置错了却看不出谁错了，就是 M4a-1 那个归因错位。
        check(
            "变量为空串 → 抛错、不静默回退",
            "AGENT_MAX_STEPS" in str(exc),
            True,
            "空串悄悄退回默认值 = 少算的账本",
        )
    else:
        check("变量为空串 → 抛错、不静默回退", "没炸", "ValueError")

# ══════════════════════════════════════════════════════════════
#  工具超时 + 退避重试
# ══════════════════════════════════════════════════════════════

print("\n═══ 工具超时（spec.timeout 终于被消费）")


class Scripted:
    """照着剧本走的假 Provider。第 n 次 chat() 返回第 n 条响应。"""

    # M6 起协议多了 `model`（trace 拿它做成本归因）；假 Provider 也要报一个。
    model = "scripted-fake"

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._script = list(responses)
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        return self._script.pop(0)


def wants(name: str, args: str = "{}") -> LLMResponse:
    return LLMResponse(
        content="我调一下",
        tool_calls=[ToolCall(id="call_0", name=name, arguments=args)],
        prompt_tokens=100,
        completion_tokens=10,
        finish_reason="tool_calls",
    )


def says(text: str = "好了") -> LLMResponse:
    return LLMResponse(
        content=text, tool_calls=[], prompt_tokens=100, completion_tokens=10, finish_reason="stop"
    )


class _SlowArgs(BaseModel):
    seconds: float = 5.0


@tool(_SlowArgs, side_effect="read", timeout=0.1)
def fake_slow(seconds: float = 5.0) -> dict:
    """故意睡很久的假工具 —— 专门用来验 spec.timeout 真的会截断。"""
    time.sleep(seconds)
    return {"ok": True, "晚了": seconds}


llm = Scripted([wants("fake_slow", '{"seconds": 0.6}'), says()])
t0 = time.monotonic()
res = run_with(llm, Budget(max_steps=4, max_tokens=10**9, max_wall_seconds=10**9))
elapsed = time.monotonic() - t0

# ★ 这条断言是【抓 `with ThreadPoolExecutor(...)` 那个坑】的：
#   写成 with 的话，__exit__ 会等到 0.6s 睡完才返回，于是耗时 ≈0.6s。
#   卡在 0.5s 以下，才说明"我们真的没等它"。
check("整个 run 的耗时 < 0.5s", elapsed < 0.5, True, f"实测 {elapsed:.3f}s，工具要睡 0.6s")
fed = json.loads(llm.calls[1][-1]["content"])
check("回灌的 error", fed.get("error"), "tool_timeout")
check("hint 里带上工具名与秒数", "fake_slow" in fed["hint"] and "0.1" in fed["hint"], True)

print("\n═══ 退避重试（落在工具层，因为它必须赶在异常被翻译之前）")

fetch = TOOLS["fetch_job_list"].fn
REQ = httpx.Request("GET", "https://remoteok.com/api")


def call_fetch(fake):
    """跑一次真工具，返回 (httpx.get 调用次数, 真正睡过的秒数列表)。

    ★ patch("time.sleep") 对这里【能生效】，是因为 retry_call 的默认值写成
      None、在调用时解析。若写成 `= time.sleep`，默认值在 def 时就被绑死，
      patch 完全无效 —— 这个脚本就会真睡 2+4 秒。
    """
    hits: list[int] = []

    def wrapped(*a, **k):
        hits.append(1)
        return fake()

    with patch("httpx.get", wrapped), patch("time.sleep") as sleeper:
        out = fetch(page=1)
    return len(hits), [round(c.args[0], 3) for c in sleeper.call_args_list], out


def raise_timeout():
    raise httpx.ConnectTimeout("连不上")


n, slept, out = call_fetch(raise_timeout)
check("瞬时故障：尝试次数", n, 3, "POLICY_BY_SIDE_EFFECT['read'].attempts")
check("瞬时故障：真的退避了", len(slept), 2, "3 次尝试 = 2 次退避")
check("瞬时故障：最终仍如实报告失败", out["error"], "source_error")

n, slept, out = call_fetch(lambda: httpx.Response(404, request=REQ))
check("永久故障：尝试次数", n, 1, "404 重发还是 404，一次都不多")
check("永久故障：不睡", slept, [])

print("\n═══ 策略表的纪律：没有幂等键就不许重试")
check("external（发飞书）", policy_for("external").attempts, 1, "重发一次 = 两条消息")
check("未知 side_effect", policy_for("胡说").attempts, 1, "宁可漏报，不许误报")
check("read", policy_for("read").attempts, 3)

print("\n═══ 退避表的形状：指数增长、夹 cap、抖动只落一次")
# rand 注入成常量后，"睡了多久"就等于"抖动前算出的基准"——
# 于是这个数字可以【卡具体值】，不必放宽成区间。放宽了就抓不到"抖了两次"。
# （吸收自 scripts/_probe_retry.py，那个临时探针已删。）


def slept(policy: RetryPolicy, exc: BaseException, rand=None) -> list[float]:
    out: list[float] = []
    try:
        retry_call(
            lambda: (_ for _ in ()).throw(exc),
            policy=policy,
            sleep=out.append,
            rand=rand or (lambda: 1.0),
        )
    except LLMError:
        pass
    return out


check(
    "第 1/2/3 次失败后的基准",
    slept(RetryPolicy(attempts=4, base_delay=1.0, cap=30.0), LLMTransientError("抖")),
    [1.0, 2.0, 4.0],
    "2 的幂",
)
check(
    "cap=4 时夹住不涨",
    slept(RetryPolicy(attempts=6, base_delay=1.0, cap=4.0), LLMTransientError("抖")),
    [1.0, 2.0, 4.0, 4.0, 4.0],
)
check(
    "Retry-After=100 但 cap=30",
    slept(
        RetryPolicy(attempts=2, base_delay=2.0, cap=30.0), LLMRateLimited("429", retry_after=100.0)
    ),
    [30.0],
    "先夹上限：对方说 3600s 也不能照办",
)
# ★ 抖动只允许落一次。两次的话 30 · 0.5 · 0.5 = 7.5 —— 这个数就是它的指纹。
check(
    "rand 恒 0.5、Retry-After=100、cap=30",
    slept(
        RetryPolicy(attempts=2, base_delay=1.0, cap=30.0),
        LLMRateLimited("429", retry_after=100.0),
        rand=lambda: 0.5,
    ),
    [15.0],
    "得 7.5 就是抖了两次",
)
check(
    "jitter=False 时 rand 被无视",
    slept(
        RetryPolicy(attempts=3, base_delay=1.0, cap=30.0, jitter=False),
        LLMTransientError("抖"),
        rand=lambda: 0.1,
    ),
    [1.0, 2.0],
    "调试开关",
)
try:
    RetryPolicy(attempts=0)
except ValueError as exc:
    check("attempts=0 构造期就炸", type(exc).__name__, "ValueError", "不是运行期")
else:
    check("attempts=0 构造期就炸", "没炸", "ValueError")

print("\n═══ 数据源把失败分成【瞬时】与【永久】（吸收自 scripts/m4_probe_source.py）")
# 上层能重试的前提，是数据源先把这两类分开 —— 分不开的话，
# 策略表再正确也没用（它只能看见一个笼统的 SourceError）。
#
# 这里直接调 RemoteOkSource.list_jobs()，【刻意不经过 jobs.py】——
# 要走 jobs.py 的话异常会被它翻译成 {"ok": false}，我们就看不见分类了。
# 被测对象是"数据源抛哪一类"，所以调用点必须贴着数据源。


def _raise(exc: BaseException):
    """造一个"一调用就抛"的假 httpx.get（模拟网络层故障）。"""

    def f(*a, **k):
        raise exc

    return f


def _status(code: int):
    """造一个"返回该状态码响应"的假 httpx.get。

    ★ 返回响应而不是直接抛：raise_for_status() 是 remoteok.py 自己调的，
      我们要验的正是"它在那行上把它归成了哪一类"。
      直接抛 HTTPStatusError 就把被测代码那一步跳过去了。
    """

    def f(*a, **k):
        return httpx.Response(code, request=REQ)

    return f


SRC_CASES = [
    ("连接超时    ", _raise(httpx.ConnectTimeout("连不上")), TransientSourceError),
    ("连接被拒    ", _raise(httpx.ConnectError("refused")), TransientSourceError),
    ("读超时      ", _raise(httpx.ReadTimeout("读不动")), TransientSourceError),
    ("500 服务端崩", _status(500), TransientSourceError),
    ("503 暂不可用", _status(503), TransientSourceError),
    ("429 被限流  ", _status(429), TransientSourceError),
    ("408 请求超时", _status(408), TransientSourceError),
    ("404 页面没了", _status(404), SourceError),
    ("403 不给看  ", _status(403), SourceError),
    ("重定向环    ", _raise(httpx.TooManyRedirects("转圈")), SourceError),
]
for label, fake, expected in SRC_CASES:
    with patch("httpx.get", fake):
        try:
            RemoteOkSource().list_jobs()
        except BaseException as exc:  # noqa: BLE001 —— 探针就要抓一切
            # ★ 卡【精确类型】而不是 isinstance：
            #   "永久"的用例若抛了 TransientSourceError（SourceError 的子类），
            #   isinstance(SourceError) 也会过 —— 那就把 bug 放过去了。
            check(label, type(exc) is expected, True, f"期望 {expected.__name__}")
        else:
            check(label, "居然没抛", f"期望 {expected.__name__}", "数据源契约第 2 条：不吞异常")

print()
if failures:
    print(f">>> {failures} 项不符。")
else:
    print("全部通过（离线、确定性；真实链路由 scripts/m3_verify.py 的第 8 项负责）。")
raise SystemExit(1 if failures else 0)
