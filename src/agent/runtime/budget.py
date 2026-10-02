"""预算闸门 —— 编排层加固的第二块（M4a-4）。

解决的问题：ReAct 循环可能"步数没超、但钱烧光了"。max_steps 只兜得住
步数爆炸，兜不住"每步都很贵"，也兜不住"某一步工具卡住不返回"。
所以三道闸门各治一种病：
  · max_steps        —— 模型原地打转（已有 loop_detected，这是更外层的兜底）
  · max_tokens       —— 每步都合法但总成本失控（上下文被工具结果撑爆）
  · max_wall_seconds —— 工具卡住/网络慢，步数根本没涨，但时间在流

★ 为什么把三道闸放在【一个】函数里、而不是散成三个 if：
  因为它们必须按固定顺序判、只报【第一个】撞破的。散开写，顺序就没人管了，
  两处同时撞破时"报哪一道"变成偶然 —— 而偶然的东西测不出来。

★ 为什么 exceeded() 返回【闸门的名字】而不是 bool：
  bool 是信息的最小公倍数。loop.py 要把它翻成 RunResult.status，
  而下一条排查线索恰恰是"撞的是哪道闸" —— 撞 token 要去看上下文是不是
  被工具结果撑爆了，撞墙钟要去看哪个工具卡住了，两个方向的修法完全不同。
  返回 bool 就等于在最需要信息的地方把信息扔掉，而上游扔掉的，下游永远拿不回来。
  （对照 M1：ToolCall.arguments 保原始字符串而不解析，同一个道理。）

★ 为什么是【纯函数】而不是 PLAN.md:496 那个 `charge()` + `exhausted()`：
  loop.py 里【已经】在累加 prompt_tokens / completion_tokens 了（RunResult 要用）。
  如果 Budget 自己也记一份账，就有两本账 —— 而两本账迟早对不上，
  且"忘了 charge 某一处"是个不会报错的 bug（只是账少算了，没人发现）。
  所以这里只做判断题：把当前数字递进来，回答"撞没撞"。
  **账只有一本（loop.py 的局部变量），判断只在一处（这里）。**
  代价是 loop.py 要多传三个数 —— 用三个参数换掉一整类静默 bug，划算。

★ 已知的不对称（必须写在注释里，否则将来会被当成 bug）：
  token 用量【只有在调用返回之后】才知道。所以闸门只能在【每轮开头】判，
  这意味着超支最多会【超出一轮】—— 这是物理限制，不是漏判。
  和工具超时那句"我停止等待 ≠ 它停止了"是同一种诚实。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

GATE_MAX_STEPS = "max_steps"
GATE_MAX_TOKENS = "max_tokens"
GATE_WALL_CLOCK = "wall_clock"
"""闸门名。用常量而不是裸字符串：这几个名字会被 loop.py 拿去拼 status，
拼错了（"max_token"）不会报错，只会让下游的判定静默失效。"""


@dataclass(frozen=True)
class Budget:
    """一次 run 的三道上限。它只描述【限制】，不记录【用量】—— 见模块注释。"""

    max_steps: int = 20
    max_tokens: int = 200_000
    max_wall_seconds: float = 600.0

    @classmethod
    def from_env(cls) -> Budget:
        """从 .env 读三道闸。变量名与 .env.example 一致。

        三条纪律（都不是风格问题，各自对应一类静默 bug）：
        · 缺省值取自上面的类字段（cls.max_steps …），不再抄一遍数字 ——
          同一个默认值写两处的话，改了字段忘了字符串，就会出现
          "有 .env 时一套上限、没 .env 时另一套"，而且【不报错】。
        · 变量存在但为空（`AGENT_MAX_STEPS=`）时抛错，不静默退回默认值。
          一个被拼错的预算悄悄变成默认值，就是"少算的账本"。
        · 解析失败时把变量名写进消息：int() 自带的
          "invalid literal for int() with base 10: 'abc'"【没说是哪个变量】，
          配置错了却看不出是谁错了 —— M4a-1 那个"归因错位"的同一个病。

        ⚠️ 兜底必须写 cls.max_steps，不能写裸 max_steps：类属性不在方法的
           名字作用域里，而该行有赋值会让它被认成【局部变量】，变量缺失时
           抛 UnboundLocalError —— 且在有 .env 时【不报错】（or 左边为真，
           右边不求值），本机全绿、CI 上炸。实测确认。

        断言在 scripts/m4_verify.py 第 ⑦ 节。
        """
        load_dotenv()
        # ★ 兜底取 cls.max_steps，不能写裸 max_steps：
        #   类属性【不在】方法的名字作用域里；而这一行又有赋值，
        #   于是裸 max_steps 被认成【局部变量】—— 变量缺失时抛
        #   UnboundLocalError，而不是去取类字段。实测确认过。
        #   更阴的是它在有 .env 时【不报错】（or 左边为真，右边不求值），
        #   本机全绿、CI/生产上炸。
        #   用 `,` 传兜底而不是 `or`：变量存在但为空（AGENT_MAX_STEPS=）时
        #   直接抛错，而不是静默退回默认值 —— 那正是 TODO A 警告的"少算的账本"。
        try:
            max_steps = int(os.getenv("AGENT_MAX_STEPS", cls.max_steps))
        except ValueError as exc:
            raise ValueError("AGENT_MAX_STEPS 必须是整数") from exc
        try:
            max_tokens = int(os.getenv("AGENT_MAX_TOKENS", cls.max_tokens))
        except ValueError as exc:
            raise ValueError("AGENT_MAX_TOKENS 必须是整数") from exc
        try:
            max_wall_seconds = float(os.getenv("AGENT_WALL_CLOCK_SECONDS", cls.max_wall_seconds))
        except ValueError as exc:
            raise ValueError("AGENT_WALL_CLOCK_SECONDS 必须是浮点数") from exc

        return Budget(
            max_steps=max_steps,
            max_tokens=max_tokens,
            max_wall_seconds=max_wall_seconds,
        )

    def exceeded(self, *, steps: int, tokens: int, wall_seconds: float) -> str | None:
        """三道闸依次判，返回【第一个】撞破的闸门名；都没撞破返回 None。

        判据（三个参数都是【当前值】，不是增量 —— 判断题的输入）：
            steps        >= max_steps         → GATE_MAX_STEPS
            tokens       >= max_tokens        → GATE_MAX_TOKENS
            wall_seconds >= max_wall_seconds  → GATE_WALL_CLOCK

        ★ steps 传"【已经跑完】几轮"（loop.py 传的是 step - 1），
          tokens 传"prompt + completion 的累计值"。传错一个是 off-by-one，
          闸门就会早停或多跑一轮。
        ★ 顺序（steps → tokens → wall）是【契约的一部分】，不是随手排的：
          两道闸同时撞破时，返回哪个完全由顺序决定，而乱序 = 结果不可复现
          = 测不出也复现不了。想换顺序（比如 wall 放最前，因为"卡住"比"贵"
          更值得优先报）可以，但要在注释里写下理由。
          断言在 scripts/m4_verify.py 第 ⑤ 项 —— 两个方向都测了。
        """
        if steps >= self.max_steps:
            return GATE_MAX_STEPS
        if tokens >= self.max_tokens:
            return GATE_MAX_TOKENS
        if wall_seconds >= self.max_wall_seconds:
            return GATE_WALL_CLOCK
        return None
