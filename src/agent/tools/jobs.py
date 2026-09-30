"""第一个真工具 —— 把「数据源」包成「模型能调的函数」。

层级关系（记住这三行，M3 的循环会反复走这条路）：

    模型 --tool_call--> jobs.fetch_job_list --调用--> sources.remoteok.list_jobs
                          ↑ 工具层                     ↑ 数据源层
                            把异常翻译成                 失败就抛
                            {"ok": false, ...}          SourceError

★ 两层对失败的处理【故意不一样】，这是 M2 的核心考点：
  · 数据源层：失败就【抛异常】—— 像 M1 的 Provider 契约，"不吞异常"。
  · 工具层：把异常【翻译成返回值】{"ok": false, "error": ..., "hint": ...}

  为什么？因为【工具的返回值是模型唯一能读到的信息】。
  异常只有主人和日志看得见，模型看不见 —— 它就没法判断下一步该干嘛，
  只能盲试或者死循环。把错误写成 hint，模型才可能做出正确的决定。

  （对照 M1 那条设计：`arguments` 保持原始字符串不许在 Provider 里解析，
    理由也是"让错误能被回灌给模型"。同一个原则，换了个位置。）
"""

from dataclasses import asdict
from typing import Any

from pydantic import BaseModel, Field

from agent.runtime.retry import policy_for, retry_call
from agent.tools.registry import tool
from agent.tools.sources import LoginExpired, SourceError, get_source


class FetchListArgs(BaseModel):
    page: int = Field(1, ge=1, le=50, description="页码，从 1 开始")


@tool(FetchListArgs, side_effect="read", timeout=35.0)
def fetch_job_list(page: int = 1) -> dict[str, Any]:
    """拉取招聘信息【列表】的指定页。

    返回 {"ok": bool, "items": [...], "count": int, "error": str|None, "hint": str|None}。
    每项含 source / source_id / title / company / url / location / published_at / extras。
    注意：只返回摘要字段，【不含职位描述全文】—— 需要全文请用 fetch_job_detail。

    什么时候用它：想知道"最近有哪些职位"时。翻页从 page=1 开始，
    返回的 items 为空说明已经翻到底了（这不是错误）。
    """
    # ★ 这一行就是"schema 不是护栏"的实证：
    #   上面 Field(ge=1, le=50) 只是写给模型看的【提示】，
    #   模型完全可能传来 page=0 或 page=-3，服务端也不一定校验。
    #   真正的校验必须在这里。
    try:
        page = max(1, page)
        source = get_source()
        # ★ 重试必须放在【这里】，不能放在 loop.py。位置的道理是 M4a 的核心：
        #   异常一旦被下面那个 except 翻译成返回值，"重试"这件事就不存在了 ——
        #   loop.py 看到的是 {"ok": false}，retry_call 永远等不到异常。
        #   实测过：ConnectTimeout 时 httpx.get 只被调用 1 次，
        #   而返回的 hint 自己还写着"可以重试"。
        #   （同一条纪律的另一半：超时可以包在外面，因为它不需要异常。）
        #
        # ⚠️ 已知重复：side_effect="read" 在上面的装饰器上声明了一次，这里又写了第二次。
        #   同一个事实有两个出处 —— 想彻底消掉，得把"吃异常"这件事上移到 @tool
        #   装饰器上（它同时知道 side_effect 和 timeout）。
        #   现在不做：一次只动一个变量。这是记在账上的债，不是忘了。
        jobs = retry_call(
            lambda: source.list_jobs(page=page),
            policy=policy_for("read"),
        )
        # Job 是我们自己的对象，必须转成原生 dict 才能回灌给模型。
        # （和 M1「arguments 别急着解析」方向相反，但理由不冲突：
        #   那边是「别替上层做决定」，这边是「必须变成文本才能喂给模型」。）
        items = [asdict(job) for job in jobs]

        # 三个返回值的形状刻意保持一致（失败时也填 items/count），
        # 这样模型不必区分"键不存在"和"值为空"两种情况，提示词才好写。
        return {
            "ok": True,
            "items": items,
            "count": len(items),
            "error": None,
            "hint": None,
        }

    # ★ LoginExpired 必须排在 SourceError 前面：它是子类，
    #   顺序反了会被父类先接走，"去续期"这条 hint 就永远轮不到。
    except LoginExpired as exc:
        return {
            "ok": False,
            "items": [],
            "count": 0,
            "error": "login_expired",
            "hint": f"登录态已过期，请运行 python -m app.main login 手动续期。（原因：{exc}）",
        }
    except SourceError as exc:
        return {
            "ok": False,
            "items": [],
            "count": 0,
            "error": "source_error",
            "hint": str(exc),
        }
