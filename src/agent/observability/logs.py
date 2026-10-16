"""日志分层 —— 可观测的第二块（M6）。

PLAN §M6 把日志分成三层，各有各的读者，所以各有各的去处：

| 层       | 内容                          | 去处                    |
|----------|-------------------------------|-------------------------|
| 业务日志 | 岗位数、新增、推送结果        | `var/logs/app.log`(JSON) |
| LLM 日志 | 完整 messages（**脱敏后**）   | `var/logs/llm.log`       |
| Trace    | 每步一条留痕                  | `var/traces/`（见 trace.py）|

★ 为什么业务日志与 LLM 日志要分成两个文件，而不是都扔进 app.log：
  体积差着两个数量级。一条业务事件是"今天新增 5 条"；一次 LLM 调用是
  6 KB 的岗位描述 + 系统提示词。混在一个文件里，你想看"这周哪天失败了"
  就得从几百 MB 的提示词里翻 —— 而能一眼翻到的日志，才是会被看的日志。
  （同一个道理让 trace 也不进库：`steps` 表存的是能聚合的那部分。）

★ 并且 app.log 只收我们自己的记录：handler 上挂了一道白名单 filter
  （`_is_our_logger`），把 httpx / openai 等第三方库每次请求打的 INFO
  挡在门外 —— 它们不是业务事件，见该函数的 docstring。

★ 为什么是 JSON Lines 而不是"人读着舒服"的文本：
  日志的读者有两个——排查时的你（要能 grep），和将来的看板（要能聚合）。
  写成 `[ts] INFO 今天新增 5 条` 那种，看板就得写正则去抠数字，而正则
  会在某次措辞改动后悄悄失配（不报错、只是少统计一批）。JSON 一行一个事件，
  两边都满足。

★ 默认【静默】，要日志得显式 configure_logging()。理由同 standard library
  给库配 NullHandler 的那条老规矩：**库不该往调用方的 stdout 说话**。
  不这么做的话，任何一个 import 了 workflows 的测试脚本都会被日志淹掉，
  然后大家学会"忽略日志输出"——而那正是日志最没用的状态。
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from agent.memory.store import var_dir

if TYPE_CHECKING:
    # 只给类型检查器看：本模块运行时【不】依赖 llm 的具体实现，
    # 只看协议（LLMProvider）。`from __future__ import annotations` 让注解
    # 变成字符串，所以这里不必在运行期 import。
    from agent.llm.base import LLMProvider, LLMResponse

APP_LOGGER = "agent"
"""业务日志的 logger 名。它的记录向上冒泡到 root，落 app.log。"""

LLM_LOGGER = "agent.llm"
"""LLM 日志的 logger 名。它被显式设成 propagate=False ——
否则同一条记录会【同时】进 llm.log 和 app.log，而"同一份内容两个文件"
就是本项目最恨的那件事：两个出处迟早有一个是过期的。"""

LOG_DIR = "logs"

log = structlog.get_logger(APP_LOGGER)
"""业务日志的入口。`log.info("report_done", status="success", inserted=5)`。
★ 事件名用【英文标识符】而不是一句中文：它是给人筛的键（`grep report_done`），
  不是给人读的句子。想读得顺，把参数带上就顺了。"""

_llm_log = structlog.get_logger(LLM_LOGGER)


# ══════════════════════════════════════════════════════════════
#  脱敏
# ══════════════════════════════════════════════════════════════

_MASK = "***"

_SECRET_PATTERNS = (
    # OpenAI 系的密钥长这样；这是最常见的一种漏法（有人把 key 贴进 prompt 调试）。
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    # Authorization: Bearer xxx
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{6,}"),
    # api_key=... / token: "..." / password="..."（JSON 与 query string 两种写法都吃）
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|password|secret)\b\"?\s*[:=]\s*\"?[^\s\"',}&]+"
    ),
)

_SECRET_KEY_HINTS = ("key", "token", "password", "secret", "authorization", "cookie", "webhook")


def _redact_str(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_MASK, text)
    return text


def _is_secret_key(key: object) -> bool:
    """这个键名像不像"装着凭据的键"。

    ★ 单独抽出来是为了让 redact() 里那一行长条件读得懂，也为了让
      "键名怎么判"这件事只有一个出处（将来 M8 扩词表只改这里）。
      注意它只管【名字像不像】，"值是不是字符串"由调用点判 —— 两条判据
      分开，是因为它们回答的是不同的问题（见 redact 的 ★）。
    """
    return any(hint in str(key).lower() for hint in _SECRET_KEY_HINTS)


def redact(value: Any) -> Any:
    """递归地把明显的凭据换掉。dict / list / str 都吃，别的原样返回。

    ★ 这是 **M6 的最小实现**，目的只有一个：别让 llm.log 把密钥写下盘。
      写完的日志是【落在磁盘上的】——把 sk-xxx 写进 `var/logs/llm.log`
      和写在代码里的区别，只在于攻击者要不要多 gitignore 一次。
      M8 会把它扩成出站口的统一 formatter（手机号 / 邮箱 / 身份证 / Cookie…），
      但"先做小的"好过"等到 M8 才做"：聊天记录里混进一个 Bearer 头，
      是每天都在发生的事。

    ★ 按【键名】和按【值的形状】两条路都过一遍：
      前者抓 `{"api_key": "..."}`，后者抓 `"Authorization: Bearer ..."` 这种
      键名已经在前缀里的写法。只做一条会漏掉另一半。

    ★ 键名命中时，还要【值确实是字符串】才整块盖掉 —— 判据是值的类型，
      不是拿正则再去猜键名。这一条是实测抓出来的事故补上的：
      `prompt_tokens` / `completion_tokens` 里含子串 "token"，被键名启发式
      误当成凭据，双双盖成 "***" —— 而那两个数恰是 M6 成本归因最该看清的
      东西，且是【静默】替换，日志看上去一切正常（见 m6_verify 的脱敏考点）。
      凭证在 JSON 里一定是字符串，计数一定是数字，所以：
      认类型是判据，猜名字是碰运气。
    """
    if isinstance(value, dict):
        return {
            k: (_MASK if isinstance(v, str) and _is_secret_key(k) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _redact_str(value)
    return value


def _redact_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog 处理器：每一条日志出站前都过一遍 redact。

    ★ 把它做成【处理器】而不是让每个调用点自己调 redact()，是这一整块的关键：
      处理器在管线上，调用点不在。写给日志的人可以忘（而且一定会忘），
      而管线不会忘 —— 这就是 PLAN §M8 那句"用 redact() 作为 formatter，
      保证「忘了脱敏」不可能发生"的落地。脱敏在【出站口】，不在入站口。
    """
    return redact(event_dict)


# ══════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════


def _silence() -> None:
    """把 structlog 设成什么都不写 —— 直到有人显式 configure_logging()。

    ★ 这一步是【import 期】做的，看起来像个副作用，但它是上面那条
      "库不该往调用方的 stdout 说话"的落地：不做的话，structlog 的默认
      配置会往 stdout 打 `event=... level=...`，于是每个 import 了
      workflows 的测试脚本都被淹。宁可默认哑，也不要默认吵。
    """
    structlog.configure(
        logger_factory=structlog.ReturnLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def _file_handler(path: Path, formatter: logging.Formatter, level: int) -> logging.Handler:
    """一个往文件写的 handler。★ 必须显式 encoding="utf-8"：
    Windows 默认 GBK，一条中文日志就当场 UnicodeEncodeError ——
    而那会发生在【记录日志的那一行】，把真正的故障盖掉。"""
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(formatter)
    handler.setLevel(level)
    return handler


def _is_our_logger(record: logging.LogRecord) -> bool:
    """app.log 只收我们自己 logger 树（`"agent"`）发出的记录。

    ★ 为什么按【谁说的】筛，而不是把 httpx 的级别调高：调级别是打地鼠 ——
      挡了 httpx 还有 openai / httpcore / urllib3。而 app.log 的读者（README 的
      日志分层表）要的是"我们干了什么"；第三方库每次请求都打的 INFO
      （`HTTP Request: GET https://remoteok.com/api "HTTP/1.1 200 OK"`）是库的
      内部实现噪声，不是业务事件 —— 实测其来源是 httpx 的 `_client` logger
      （GET 与 POST 都由它打）。
    ★ 不会因此漏掉故障：请求失败已经作为【带类型的异常】进了我们自己的留痕
      （fetch_failed / llm_error / runs.error），比一行 INFO 清楚得多。
    ★ 挡在【handler】上，不挡在 logger 上：只影响 app.log 这一个出口，
      不去改第三方 logger 的全局级别（别动别人的设置）。
      （注：`Handler.filter` 自 Python 3.2 起接受普通可调用对象，不必是 Filter 实例。）

    ★ 认下的账（明知故丢 —— 别以为这道 filter 滴水不漏）：被它拦下的记录是
      **静默消失**的，不进任何文件（root 有 handler ⇒ `found>0` ⇒ 连
      `logging.lastResort` 都不会接住）。**失败**那一路没丢：连接类故障与 4xx/5xx
      都已翻成带类型的异常进了我们自己的留痕（fetch_failed / runs.error）。
      丢的是**成功**请求的状态码（200 / HTTP 版本 / reason phrase）—— 没人别处记它。
      判定为「库的内部细节」而非「业务事实」：查看"站点还在正常响应吗"时，
      trace 里 fetch 留痕的 pages/jobs 与 raw 存档的变化已经够用。
    """
    return record.name == APP_LOGGER or record.name.startswith(f"{APP_LOGGER}.")


def configure_logging(*, level: str = "INFO", root: str | Path | None = None) -> None:
    """把日志接到 `var/logs/{app,llm}.log` 上。幂等，可以重复调（会换掉旧 handler）。

    ★ 为什么用 structlog 官方的 "绕道 stdlib logging" 配方，而不是
      structlog 自己的 PrintLogger(file=...)：
      因为【两个文件】。structlog 的 logger_factory 是全局单例，一次只能指向
      一个输出；要靠 stdlib 的 logger 层级（"agent"→app.log、
      "agent.llm"→llm.log 且不冒泡）才分得开两个文件。
      代价是多绕一层，收益是 llm.log 不会跟 app.log 混在一起。

    ★ 会先关掉旧 handler 再装新的：测试与重复调用都靠这一点。
      不关的话文件句柄会越攒越多，而 Windows 上被占着的文件删不掉
      （临时目录清理会失败，并且那条 PermissionError 会盖住真正的断言失败）。
    """
    log_dir = var_dir(root) / LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):  # 名字写错了（"INFOO"）→ 当场报，不静默退回
        raise ValueError(f"未知的日志级别 {level!r}（见 .env 的 LOG_LEVEL）")

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            timestamper,
            # ★ 脱敏放在最靠近出站的位置：前面所有处理器塞进去的东西
            #   （包括调用点自己带的 kv）都已经被摊进 event_dict 了，
            #   所以这一步能一次盖住全部 —— 放在链首则会漏掉后面的字段。
            _redact_processor,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[timestamper],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
    )

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()
    root_logger.setLevel(numeric_level)
    # ★ app.log 挂一道【白名单】filter：只收 "agent" 树的记录（见 _is_our_logger）。
    #   不加这道的话，httpx / openai 每次请求的 INFO 会顺着 root 冒进来 ——
    #   它们不是"agent"发的，所以拿不到 add_log_level / add_logger_name，
    #   进文件的行会【唯独缺 level / logger 两个键】，一眼就看出是外来的。
    app_handler = _file_handler(log_dir / "app.log", formatter, numeric_level)
    app_handler.addFilter(_is_our_logger)
    root_logger.addHandler(app_handler)

    llm_logger = logging.getLogger(LLM_LOGGER)
    for handler in list(llm_logger.handlers):
        llm_logger.removeHandler(handler)
        handler.close()
    llm_logger.setLevel(numeric_level)
    llm_logger.addHandler(_file_handler(log_dir / "llm.log", formatter, numeric_level))
    # ★ propagate=False 是这一段的全部要害：不关掉的话，"agent.llm" 的记录
    #   会顺着层级冒到 root，于是同一条【也】进了 app.log —— 那份内容就有
    #   两个出处了（正是 LOG_DIR 那段表想避免的）。
    llm_logger.propagate = False


# ══════════════════════════════════════════════════════════════
#  LLM 日志：包在 Provider 外面的装饰器
# ══════════════════════════════════════════════════════════════


class LoggedProvider:
    """给任意 LLMProvider 套一层"把每次调用写进 llm.log"。

    ★ 为什么是【装饰器】而不是在 runtime/loop.py 里打日志：
      Provider 是"一次请求"的天然单位，它手上有完整的 messages 与真实的
      用量；而循环只有"这一轮我拼了什么"。把日志放进循环，等于让循环
      去猜 Provider 的事。装饰器满足同一个协议（LLMProvider），
      所以 app/main.py 里 `LoggedProvider(OpenAICompatProvider())` 一包即可，
      上层一行不改 —— 这正是 M1 那层抽象该兑现的红利。

    ★ 它【不】吞异常：记完 error 日志后原样 raise。
      日志层的职责是"留下痕迹"，不是"改变控制流"——一个顺手把异常吃掉的
      日志装饰器，是排查噩梦（现象消失，原因无从查）。
    """

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner

    @property
    def model(self) -> str:
        # 协议要求（见 llm/base.py）：装饰器也得报出里层的身份，
        # 否则它换掉之后 trace 里的 model 就没了。
        return self._inner.model

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        # 工具【只记名字】，不记 schema：schema 是静态的（由代码生成），
        # 每次调用都抄一遍纯属浪费体积，而"这一轮带了哪些工具"才是变化的那个事实。
        #
        # ★ 这里【不】手动调 redact()：脱敏是日志管线上的处理器（见 _redact_processor），
        #   不是调用点的责任。在这一层再调一次，就等于给"两个地方管同一件事"
        #   开了个头 —— 而那个头的结局一向是其中一个过期。
        tool_names = [t.get("function", {}).get("name", "?") for t in (tools or [])]
        _llm_log.info(
            "llm_request",
            model=self.model,
            message_count=len(messages),
            tool_names=tool_names,
            messages=messages,
        )
        try:
            resp = self._inner.chat(messages=messages, tools=tools)
        except Exception as exc:  # noqa: BLE001 —— 只为记一笔，随即原样抛回
            _llm_log.warning(
                "llm_error",
                model=self.model,
                error=type(exc).__name__,
                detail=str(exc),
            )
            raise
        _llm_log.info(
            "llm_response",
            model=self.model,
            finish_reason=resp.finish_reason,
            prompt_tokens=resp.prompt_tokens,
            completion_tokens=resp.completion_tokens,
            tool_calls=[c.name for c in resp.tool_calls],
            content=resp.content or "",
        )
        return resp


def shutdown_logging() -> None:
    """关掉所有日志文件句柄，然后回到静默。

    ★ 存在的理由是 Windows：**文件被打开着就删不掉**。测试与验收脚本都在
      临时目录里跑，不关句柄的话退出时 `TemporaryDirectory` 的清理会抛
      PermissionError —— 而那条清理异常会【盖住】真正的断言失败，
      让人对着一个无关的错误查半天（memory 那边关连接是同一个理由）。
    """
    for name in ("", LLM_LOGGER):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
    _silence()


def _cli_smoke() -> None:  # pragma: no cover —— 手工探针，不进验收
    """手动看一眼脱敏与两个文件是否各就各位：
    PYTHONUTF8=1 ./.venv/Scripts/python.exe -m agent.observability.logs
    """
    configure_logging(level="INFO")
    log.info("smoke", note="业务事件")
    _llm_log.info("llm_request", api_key="sk-abcdef123456", messages=[{"role": "user"}])
    print("wrote", var_dir() / LOG_DIR)
    sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    _cli_smoke()


_silence()
