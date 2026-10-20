"""不可信内容隔离（PLAN §M8 第 4 条）—— 把网页数据包进一个模型看得懂的边界。"""

from __future__ import annotations

import re

DEFAULT_MAX_CHARS = 8000
"""字符上限管"单条太长"，与行数上限 `MAX_SUMMARY_LINES` 分工。"""

_CLOSING_TAG = re.compile(r"<\s*/\s*untrusted_web_content\s*>", re.IGNORECASE)
"""匹配【闭合标签】的所有常见变体：大小写任意、`< /` 与 `> ` 间允许空白。"""


def wrap_untrusted(text: str, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """把外部文本洗掉闭合标记、截断，再包进 `<untrusted_web_content>`。"""
    safe = _CLOSING_TAG.sub("", text)
    if len(safe) > max_chars:
        safe = safe[:max_chars]
    return f"<untrusted_web_content>\n{safe}\n</untrusted_web_content>"
