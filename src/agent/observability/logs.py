"""日志分层 —— 可观测的第二块（M6）。"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from agent.memory.store import var_dir

if TYPE_CHECKING:
    from agent.llm.base import LLMProvider, LLMResponse

APP_LOGGER = "agent"
"""业务日志的 logger 名。它的记录向上冒泡到 root，落 app.log。"""

LLM_LOGGER = "agent.llm"
"""LLM 日志的 logger 名。显式 propagate=False，只进 llm.log。"""

LOG_DIR = "logs"

log = structlog.get_logger(APP_LOGGER)
"""业务日志的入口。`log.info("report_done", status="success", inserted=5)`。"""

_llm_log = structlog.get_logger(LLM_LOGGER)


_MASK = "***"

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{6,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|password|secret)\b\"?\s*[:=]\s*\"?[^\s\"',}&]+"
    ),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(
        r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
        r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
    ),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
)

_SECRET_KEY_HINTS = ("key", "token", "password", "secret", "authorization", "cookie", "webhook")


def _redact_str(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_MASK, text)
    return text


def _is_secret_key(key: object) -> bool:
    """这个键名像不像"装着凭据的键"。"""
    return any(hint in str(key).lower() for hint in _SECRET_KEY_HINTS)


def redact(value: Any) -> Any:
    """递归地把明显的凭据换掉。dict / list / str 都吃，别的原样返回。"""
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
    """structlog 处理器：每一条日志出站前都过一遍 redact。"""
    return redact(event_dict)


def _silence() -> None:
    """把 structlog 设成什么都不写 —— 直到有人显式 configure_logging()。"""
    structlog.configure(
        logger_factory=structlog.ReturnLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def _file_handler(path: Path, formatter: logging.Formatter, level: int) -> logging.Handler:
    """一个往文件写的 handler。必须显式 encoding="utf-8"，否则中文在 Windows 落成 GBK。"""
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(formatter)
    handler.setLevel(level)
    return handler


def _is_our_logger(record: logging.LogRecord) -> bool:
    """app.log 只收我们自己 logger 树（`"agent"`）发出的记录。"""
    return record.name == APP_LOGGER or record.name.startswith(f"{APP_LOGGER}.")


def configure_logging(*, level: str = "INFO", root: str | Path | None = None) -> None:
    """把日志接到 `var/logs/{app,llm}.log` 上。幂等，可以重复调（会换掉旧 handler）。"""
    log_dir = var_dir(root) / LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):
        raise ValueError(f"未知的日志级别 {level!r}（见 .env 的 LOG_LEVEL）")

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            timestamper,
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
    app_handler = _file_handler(log_dir / "app.log", formatter, numeric_level)
    app_handler.addFilter(_is_our_logger)
    root_logger.addHandler(app_handler)

    llm_logger = logging.getLogger(LLM_LOGGER)
    for handler in list(llm_logger.handlers):
        llm_logger.removeHandler(handler)
        handler.close()
    llm_logger.setLevel(numeric_level)
    llm_logger.addHandler(_file_handler(log_dir / "llm.log", formatter, numeric_level))
    llm_logger.propagate = False


class LoggedProvider:
    """给任意 LLMProvider 套一层"把每次调用写进 llm.log"。"""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner

    @property
    def model(self) -> str:
        return self._inner.model

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
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
    """关掉所有日志文件句柄，然后回到静默。"""
    for name in ("", LLM_LOGGER):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
    _silence()


def _cli_smoke() -> None:  # pragma: no cover —— 手工探针，不进验收
    """手工探针：看一眼脱敏与两个日志文件是否各就各位。"""
    configure_logging(level="INFO")
    log.info("smoke", note="业务事件")
    _llm_log.info("llm_request", api_key="sk-abcdef123456", messages=[{"role": "user"}])
    print("wrote", var_dir() / LOG_DIR)
    sys.stdout.flush()


if __name__ == "__main__":  # pragma: no cover
    _cli_smoke()


_silence()
