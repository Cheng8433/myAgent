"""脱敏单测 —— 出站口的最后一道，盯的是"计数 vs 凭据"这条分界。

★ 这里最重要的一条是【回归护栏】：`prompt_tokens` / `completion_tokens`
  是 M6 成本归因最该看清的两个数，而它们的键名里含子串 "token" ——
  2026-10-09 实测被键名启发式误伤成 "***"，而且【静默】替换（日志看上去一切正常）。
  修法是"认值的类型，不猜键的名字"：凭证在 JSON 里一定是字符串，计数一定是数字。
  这一条断言就是把那次事故钉在原地。
"""

from __future__ import annotations

from agent.observability.logs import redact


def test_token_counts_survive():
    """计数是数字 → 不许被当成凭据盖掉（那颗实测雷的回归护栏）。"""
    assert redact({"prompt_tokens": 842, "completion_tokens": 61}) == {
        "prompt_tokens": 842,
        "completion_tokens": 61,
    }


def test_max_tokens_survives():
    assert redact({"max_tokens": 4096}) == {"max_tokens": 4096}


def test_secret_key_with_non_string_value_kept():
    """键名像凭据、但值是数字 → 不动。判据是类型，不是名字。"""
    assert redact({"token": 123}) == {"token": 123}


def test_api_key_masked_by_key_name():
    assert redact({"api_key": "sk-abcdef123456"}) == {"api_key": "***"}


def test_authorization_key_masked():
    assert redact({"Authorization": "Bearer abcdef123456"}) == {"Authorization": "***"}


def test_bearer_masked_by_value_shape():
    """键名没命中，靠【值的形状】兜住 —— 两条路都要走。"""
    assert redact({"note": "Authorization: Bearer abcdef"}) == {"note": "Authorization: ***"}


def test_sk_pattern_masked_inside_string():
    assert redact("key=sk-abcdef123456") == "key=***"


def test_nested_dict_masked():
    """llm.log 里 messages 是个大 dict —— 只盖顶层等于没盖。"""
    assert redact({"messages": [{"role": "user", "api_key": "sk-nested12345"}]}) == {
        "messages": [{"role": "user", "api_key": "***"}]
    }


def test_plain_values_untouched():
    assert redact({"role": "user", "n": 1, "ok": True, "nothing": None}) == {
        "role": "user",
        "n": 1,
        "ok": True,
        "nothing": None,
    }
