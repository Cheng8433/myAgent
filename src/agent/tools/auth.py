"""登录态基础设施 —— 凭证（Playwright storage_state）的落盘与读取。

本文件只处理【磁盘上的那份凭证】，一句话概括它的职责边界：
**谁登录、怎么登录，不归它管；"登录之后留下的东西放哪儿、怎么安全地放"归它管。**

★ 为什么单独一个模块，不塞进 `sources/ncss.py`：
  登录态不是"某个数据源"的私事。将来再接入第二个要登录的站点，或是把
  ncss 换成别的源，"放哪儿、怎么原子写、缺了怎么报"这三件事是同一件事。
  塞进 ncss.py 就得抄第二遍 —— 而抄出来的第二份不会报错，只会慢慢分家。

★ 为什么这里【没有】一个 @tool 装饰器（PLAN §M2 的工具清单里有个
  `ensure_session()`）：**登录态是基础设施，不是模型该做的决定。**
  模型能看到的只有工具返回值里那句 hint（`tools/jobs.py` 的
  `LoginExpired` 分支）。同一类纪律在本项目出现过三次：工具不碰 `run_id`
  （编排层的事）、Provider 不自己决定超时（上层的事）、数据源不碰磁盘
  （上层的事）—— 现在是第四次：**别把系统状态交给模型去维护。**
  给模型一个 ensure_session，等于给它一条能把自己锁在门外的路径。

★ 格式就用 Playwright 的 `storage_state` 【原样】，不自己发明一套：
  它就是一个 `{"cookies": [...], "origins": [...]}` 的 dict。自己发明格式
  在"哪天要改回浏览器运行时"的那一天会白写一遍转换层；而原生格式
  `context.add_cookies(state["cookies"])` 能直接喂回去。

★ 存的是 cookie，不含账号密码 —— 这一点是【结构上】成立的：
  Playwright 的 storage_state 里根本没有让你放 password 的格子，
  主人是在浏览器窗口里手工输入的（PLAN §M2 的硬约定，代码不碰凭据）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from agent.memory.store import var_dir

STATE_REL = "auth/ncss_state.json"
"""凭证在 var/ 下的相对路径。

★ 用相对路径常量而不是拼绝对路径：`var_dir()` 是"var/ 下有什么"的唯一出处
  （`observability/trace.py` / `workflows/report.py` / `observability/logs.py`
  都是这么复用的），换了 `VAR_DIR` 这里跟着走。
  `.gitignore` 里 `var/auth/` 与 `*.storage_state.json` 已经拦好了 —— 凭据
  永远不会进仓库，但主人仍该习惯性跑一次 `git status` 自查。
"""

LOGIN_HINT = "python -m app.main login"
"""续期命令。【只写一处】—— 消息里、提示里、文档里都引用这个名字，
  否则哪天子命令改了名，总有一处留在老名字上。"""


class AuthMissingError(RuntimeError):
    """本机还没有可用的登录态（文件不在 / 内容坏了 / 形状不对）。

    ★ 为什么不继承 `SourceError`：它表达的**不是"源站出问题了"**，
      而是"这台机器还没登录过"。两者的处置完全不同 ——
      前者要重试或降级，后者只要人去跑一次 login。
      混成一个类型，`except SourceError`（记 failed）就会把"请去登录"
      这个可执行的下一步，翻译成一句没有下文的"抓取失败"。
      所以它上抛到 CLI 层，由 `cmd_login` 之外的调用方翻成"去跑 login"。
    """


def state_path(*, root: str | Path | None = None) -> Path:
    """凭证文件的绝对路径：`<VAR_DIR>/auth/ncss_state.json`。"""
    return var_dir(root) / STATE_REL


def _relative_to_var(target: Path, *, root: str | Path | None) -> str:
    """转成【相对 var/ 的正斜杠】路径，同 `archive_raw` / `write_report` 的约定。

    ★ 绝对路径存进日志/库，仓库一搬家、换个 VAR_DIR 就静默失效 ——
      "文件明明在，就是找不到"，同 M5 在 raw_path 上认的那笔账。
    """
    return target.relative_to(var_dir(root)).as_posix()


def save_state(state: dict[str, Any], *, root: str | Path | None = None) -> str:
    """把 storage_state 原子地落盘，返回它【相对 var/】的路径。

    ★ 形状检查放在【写的时候】，不放在读的时候：
      一个形状不对的 state（比如把 `page.content()` 的 HTML 存了进去）
      Playwright 生成不出来、只会由改动引入。现在拦住，报错位置就是出错位置；
      等到某天日报里读它时再炸，报错位置在【另一个模块】，
      而中间隔着的那段时间里，磁盘上躺着一份看起来还挺正常的坏凭证。
      （同 M5c 那笔账：`run_id` 必填，宁可当场 TypeError。）

    ★ 原子写 = 同目录临时文件 + `os.replace`：
      `os.replace` 在 Windows 上也是原子的（同盘 rename）。直接 open("w")
      覆盖原文件的话，写到一半被 kill（或断电）就留下【半个 JSON】——
      下次读它得到 JSONDecodeError，而真正的问题是"没写完"，
      报错信息会把排查方向指向解析。临时文件写在【同目录】是要点：
      跨盘 rename 不原子，系统临时目录很可能在另一个盘上。
    """
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise ValueError(
            "storage_state 形状不对：期望 {'cookies': [...], ...}，"
            f"实际拿到 {type(state).__name__}。它应当来自 Playwright 的 context.storage_state()。"
        )

    target = state_path(root=root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(state, fp, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    finally:
        # ★ 失败路径上把临时文件清掉：留下它不会报错，只会让下一次
        #   `ls var/auth/` 里多一个来路不明的文件。
        if tmp.exists():
            tmp.unlink()

    return _relative_to_var(target, root=root)


def load_state(*, root: str | Path | None = None) -> dict[str, Any]:
    """读回 storage_state。文件不在 / 内容坏了，一律抛 `AuthMissingError`。

    ★ 三种情况【合并成同一个异常】：文件不在、JSON 坏了、形状不对。
      对调用方（"要续期了，去跑 login"）来说它们是同一件事；
      而对排障的人来说，区别写在【消息里】。这跟 `agent.config` 那条
      "缺失 / 空串 / 解析失败分开报"不矛盾：那里三种情况教人做的事不同
      （填一个 / 换一个 / 改格式），这里三种情况教人做的是同一件事。
    """
    target = state_path(root=root)
    if not target.exists():
        raise AuthMissingError(f"登录态不存在：{target}。请先运行 `{LOGIN_HINT}`。")
    try:
        with open(target, encoding="utf-8") as fp:
            state = json.load(fp)
    except json.JSONDecodeError as exc:
        raise AuthMissingError(
            f"登录态文件解析失败：{target}（{exc}）。文件可能没写完或不是 JSON。"
            f"请重新运行 `{LOGIN_HINT}` 覆盖它。"
        ) from exc
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise AuthMissingError(
            f"登录态文件形状不对：{target}（期望 {{'cookies': [...]}}）。"
            f"请重新运行 `{LOGIN_HINT}` 覆盖它。"
        )
    return state


def cookies_from_state(state: dict[str, Any], *, domains: tuple[str, ...]) -> dict[str, str]:
    """从 storage_state 里取出【指定域名】的 cookie，压成 `name -> value`。

    ★ 按域名筛是必须的，不是保险：storage_state 里装着【整个浏览器上下文】
      见过的所有 cookie（含 chsi 的、含统计/埋点域的）。一古脑全发给 ncss，
      等于把别的站的凭据也交出去 —— 那正是 M8 脱敏要防的同一类事情，
      只不过这里是从"发给谁"这一侧防。

    ★ 返回值是扁平的 `dict[str, str]`，因为这就是 `httpx.get(cookies=...)`
      要的形状：数据源拿到就能直接用，不用自己再拆一遍（拆错了不报错，
      只是"请求带着一个空 cookie 过去"，然后 302 —— 长得像登录过期）。
    """
    out: dict[str, str] = {}
    for cookie in state.get("cookies", []):
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain") or "")
        if not any(token in domain for token in domains):
            continue
        name = cookie.get("name")
        if not name:  # 无名 cookie 发出去也没用，跳过而不是记成空串
            continue
        out[str(name)] = str(cookie.get("value") or "")
    return out


def state_age_seconds(*, root: str | Path | None = None) -> float | None:
    """凭证文件距今多少秒；文件不存在返回 None。

    ★ 给 M5/M9 的"连续 N 天未成功就主动告警"留的口（PLAN §M2 的生命周期管理）：
      报"登录态是 11 天前的"比报"登录态过期了"早得多 —— 后者只能等它真失效。
      现在只提供读，不自动更新（自动更新属于 M5 那侧的账）。
    """
    target = state_path(root=root)
    if not target.exists():
        return None
    return time.time() - target.stat().st_mtime
