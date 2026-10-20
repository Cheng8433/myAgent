"""登录态基础设施的回归网（M2b Phase A）。"""

from __future__ import annotations

import json

import pytest

from agent.tools.auth import (
    STATE_REL,
    AuthMissingError,
    cookies_from_state,
    load_state,
    save_state,
    state_age_seconds,
    state_path,
)


def _state() -> dict:
    """一份形状【照实测】的 storage_state（名字与域名取自 2026-10-16 真机快照）。"""
    return {
        "cookies": [
            {"name": "SESSION", "value": "abc", "domain": "www.ncss.cn", "path": "/student/"},
            {"name": "cna", "value": "def", "domain": ".ncss.cn", "path": "/"},
            {
                "name": "JSESSIONID",
                "value": "zzz",
                "domain": "account.chsi.com.cn",
                "path": "/passport",
            },
            {"name": "", "value": "无名", "domain": ".ncss.cn", "path": "/"},
        ],
        "origins": [],
    }


def test_save_then_load_roundtrip(tmp_path):
    saved = _state()
    rel = save_state(saved, root=tmp_path)
    assert rel == STATE_REL
    assert load_state(root=tmp_path) == saved


def test_saved_path_is_relative_posix(tmp_path):
    """路径形状是契约：绝对路径进日志/库，仓库一搬家就静默失效（M5 那笔账）。"""
    rel = save_state(_state(), root=tmp_path)
    assert rel == "auth/ncss_state.json"
    assert "\\" not in rel
    assert ":" not in rel and not rel.startswith("/")


def test_state_path_sits_under_var_dir(tmp_path):
    assert state_path(root=tmp_path) == tmp_path / "auth" / "ncss_state.json"


def test_atomic_write_leaves_no_tmp_file(tmp_path):
    """临时文件在同目录生出来，成功后必须【已经不在】了。"""
    save_state(_state(), root=tmp_path)
    assert list((tmp_path / "auth").iterdir()) == [tmp_path / "auth" / "ncss_state.json"]


def test_overwrite_replaces_instead_of_appending(tmp_path):
    """再存一次是【覆盖】：凭证换了（主人重新登录）不该留下两份。"""
    save_state(_state(), root=tmp_path)
    save_state(
        {"cookies": [{"name": "NEW", "value": "1", "domain": ".ncss.cn"}], "origins": []},
        root=tmp_path,
    )
    assert load_state(root=tmp_path)["cookies"][0]["name"] == "NEW"


def test_chinese_survives_roundtrip(tmp_path):
    '`ensure_ascii=False` + `encoding="utf-8"`：否则中文被转成 \\uXXXX（看不出来）'
    save_state(
        {"cookies": [{"name": "备注", "value": "已登录", "domain": ".ncss.cn"}], "origins": []},
        root=tmp_path,
    )
    assert load_state(root=tmp_path)["cookies"][0]["value"] == "已登录"


def test_missing_state_raises_with_next_step(tmp_path):
    """报错消息必须包含【下一步命令】—— 这个异常的处置动作只有一个。"""
    with pytest.raises(AuthMissingError) as err:
        load_state(root=tmp_path)
    assert "python -m app.main login" in str(err.value)


def test_corrupt_state_raises_not_jsondecode(tmp_path):
    """坏文件被翻译成 AuthMissingError（而不是漏出 json 的异常类型）。"""
    target = state_path(root=tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ 写到一半被 kill", encoding="utf-8")
    with pytest.raises(AuthMissingError) as err:
        load_state(root=tmp_path)
    assert "解析失败" in str(err.value)


def test_wrong_shape_raises(tmp_path):
    """形状不对（比如把 HTML 存了进来）也要走同一个异常类型。"""
    target = state_path(root=tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"cookies": "不是个列表"}), encoding="utf-8")
    with pytest.raises(AuthMissingError):
        load_state(root=tmp_path)


def test_save_rejects_wrong_shape_before_writing(tmp_path):
    """形状检查在【写的时候】：坏凭证根本进不了盘，而不是等某天读它时才炸。"""
    with pytest.raises(ValueError):
        save_state({"nope": 1}, root=tmp_path)
    assert not state_path(root=tmp_path).exists()


def test_cookies_filtered_by_domain():
    """别的域的 cookie 不许带出去（那是把别人家的凭据也交出去）。"""
    cookies = cookies_from_state(_state(), domains=("ncss.cn",))
    assert cookies == {"SESSION": "abc", "cna": "def"}


def test_parent_domain_token_matches_subdomains():
    """`.ncss.cn` 与 `www.ncss.cn` 都要命中父域片段 ——"""
    assert set(cookies_from_state(_state(), domains=("ncss.cn",))) == {"SESSION", "cna"}


def test_state_holds_no_credentials(tmp_path):
    """钉住一条【结构性】承诺：落盘的只有 cookie，没有账号密码。"""
    save_state(_state(), root=tmp_path)
    raw = state_path(root=tmp_path).read_text(encoding="utf-8").lower()
    assert "password" not in raw
    assert "username" not in raw


def test_state_age(tmp_path):
    assert state_age_seconds(root=tmp_path) is None
    save_state(_state(), root=tmp_path)
    age = state_age_seconds(root=tmp_path)
    assert age is not None and 0 <= age < 60
