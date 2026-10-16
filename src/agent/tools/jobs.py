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

from agent.runtime.retry import RetryPolicy, policy_for, retry_call
from agent.tools.registry import TOOLS, tool
from agent.tools.sources import LoginExpired, SourceError, get_source
from agent.tools.sources.base import JobPage, JobSource


class FetchListArgs(BaseModel):
    page: int = Field(1, ge=1, le=50, description="页码，从 1 开始")


def _fetch_policy() -> RetryPolicy:
    """取【取列表】这个操作的重试策略 —— 从工具自己的声明推导，不手抄。

    ★ 为什么不直接写 policy_for("read")：那是同一个事实（"取列表是只读的"）
      的第二个出处。装饰器写 read、这里写 read —— 哪天有人把 fetch_job_list
      改成 side_effect="write"，两处会当场分家，而分家【不报错】：
      工具自称 write、重试却按 read 走。（M4b 记下的欠账，这次还清。）

    ★ 为什么必须是【函数】、不能提成模块级常量：
      常量在 import 期求值，而本函数定义在 fetch_job_list【之前】——
      那一刻装饰器还没跑、TOOLS 里还是空的，当场 KeyError。
      函数在【调用时】才读 TOOLS，那时模块已 import 完、注册表已填好。
      （本项目第三次撞"求值时机"这个主题：retry.py 的 sleep=None、
        budget.py 的 cls.max_steps 是同一个坑的另外两张脸。）

    断言在 scripts/m2_verify.py 的「单一出处」一节。
    """
    return policy_for(TOOLS["fetch_job_list"].side_effect)


def fetch_page(page: int = 1, *, source: JobSource | None = None) -> JobPage:
    """取【一页】招聘信息，连同原始响应体一起返回。异常原样上抛。

    ★ 它存在的理由是"两个消费者，一个出处"：
      · 编排层（workflows/daily_report.py）要 `JobPage.raw` 才能落存档 + 入库；
      · 工具层（下面的 fetch_job_list）要 items 回灌给模型。
      而"调数据源 + 重试策略"这件事【必须只有一处】—— 否则编排层得自己
      再写一遍 `retry_call(..., policy_for("read"))`，两套退避参数迟早漂移，
      而漂移不会报错。

    ★ 与 fetch_job_list 的分工，是 M2 那条"数据源抛异常 / 工具层翻译"的翻版：
      · 本函数：异常【原样上抛】（SourceError / LoginExpired / TransientSourceError）。
        编排层必须靠类型分支 —— "登录过期 → 退出码 3"和"网络抖了 → 记 failed"
        是两种处置；一旦翻译成 {"ok": false}，就再也分不出来了。
      · fetch_job_list：把异常【翻译成返回值】。因为工具的返回值是
        模型唯一能读到的信息（见文件顶部那段）。

    ★ source 是给测试用的注入口（假数据源），生产路径走 get_source()。
      开口放在参数上而不是"让测试去 patch 模块全局名"，理由同 archive_raw 的 root：
      调用方一眼看得见它可以换，不必知道去哪儿 patch。

    ★ page 在这里夹到 >= 1 —— 不在 fetch_job_list 里夹。这里是所有调用的
      必经之地，护栏放在必经之地才拦得住每一次。
    """
    src = source or get_source()
    # ★ get_source() 必须在 lambda 【外面】。写进 lambda 里有两处害：
    #   ① 每次重试都重建一个数据源（ncss 那种带会话的对象会白白重建）；
    #   ② get_source() 自己的异常（比如 JOB_SOURCE 拼错）也会被算进
    #      "可重试的瞬时故障" —— 而它其实是配置错误，重试三次纯属浪费。
    #
    # ★ 重试策略从工具【自己的声明】推导（见上面的 _fetch_policy）——
    #   这里不再手抄 "read"。它曾经是 policy_for("read")，同一个事实的第二个
    #   出处：改了装饰器的 side_effect，这一行不会跟着改，而两处分家【不报错】。
    #   M4b 记下的这笔欠账，在这次还清。
    return retry_call(lambda: src.list_jobs(page=max(1, page)), policy=_fetch_policy())


@tool(FetchListArgs, side_effect="read", timeout=35.0)
def fetch_job_list(page: int = 1) -> dict[str, Any]:
    """拉取招聘信息【列表】的指定页。

    返回 {"ok": bool, "items": [...], "count": int, "error": str|None, "hint": str|None}。
    每项含 source / source_id / title / company / url / location / published_at / extras。
    注意：只返回摘要字段，【不含职位描述全文】—— 需要全文请用 fetch_job_detail。

    什么时候用它：想知道"最近有哪些职位"时。翻页从 page=1 开始，
    返回的 items 为空说明已经翻到底了（这不是错误）。
    """
    try:
        # ★ 上面 Field(ge=1, le=50) 只是写给模型看的【提示】，不是护栏 ——
        #   模型完全可能传来 page=0 或 page=-3，服务端也不一定校验。
        #   真正的校验（page 夹到 >= 1）在 fetch_page 里，那是所有调用的必经之地。
        job_page = fetch_page(page)
        # Job 是我们自己的对象，必须转成原生 dict 才能回灌给模型。
        # （和 M1「arguments 别急着解析」方向相反，但理由不冲突：
        #   那边是「别替上层做决定」，这边是「必须变成文本才能喂给模型」。）
        items = [asdict(job) for job in job_page.jobs]

        # ⚠️ job_page.raw 在【这一层】仍然没有去处，这不是漏了 ——
        #   本函数的返回值直接进 messages，627 KB 的原文进去就是当场烧穿
        #   上下文预算（M6 会算这笔账）。
        #   它的归属是【编排层】：daily_report 调 fetch_page 拿 raw，
        #   交给 store_page 落 var/raw/ + 写回 jobs.raw_path。
        #   所以它不是一个"忘了接"的悬空钩子，是一个"排好队等着"的接口 ——
        #   到 M4b，它等到人了。

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
            # ★ 措辞覆盖两种成因（过期 / 本机还没登录过）—— 这句话是【模型】
            #   唯一读到的信息，说"已过期"而实际是"从没登录过"，模型就会
            #   按"续一次期"来理解。处置虽然相同，但少一句误导是一句。
            "hint": (
                "登录态不可用（已过期，或本机还没有登录过），"
                f"请运行 python -m app.main login 手动续期。（原因：{exc}）"
            ),
        }
    except SourceError as exc:
        return {
            "ok": False,
            "items": [],
            "count": 0,
            "error": "source_error",
            "hint": str(exc),
        }
