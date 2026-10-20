"""M2a 验收脚本 —— 验工具层的【两条路】，而不只是"能跑通"。"""

import agent.tools.jobs as jobs_mod
from agent.tools.jobs import fetch_job_list
from agent.tools.registry import TOOLS, openai_tools
from agent.tools.sources.base import JobPage, LoginExpired, SourceError

EXPECTED_ITEM_KEYS = {
    "source",
    "source_id",
    "title",
    "company",
    "url",
    "location",
    "published_at",
    "extras",
}

PAGE_LIMIT = 20


def check_schema() -> None:
    print("═══ 注册表与 Schema ═══")
    assert "fetch_job_list" in TOOLS, "工具没注册上 —— 忘了 import agent.tools.jobs？"

    spec = openai_tools(["fetch_job_list"])[0]["function"]
    assert spec["description"].strip(), "工具没有描述，模型永远不会调它"

    props = spec["parameters"]["properties"]
    assert "page" in props, "schema 里没有 page 参数"
    assert props["page"]["type"] == "integer"
    print("  工具已注册，schema 由 Pydantic 从 Field(ge=1, le=50) 自动生成 ✓")


def check_normal_path() -> None:
    print("\n═══ 路 A：正常路径 ═══")
    result = fetch_job_list(page=1)

    assert result["ok"] is True, f"拉取失败了：{result['error']} / {result['hint']}"
    assert result["error"] is None and result["hint"] is None, "成功时 error/hint 应为 None"
    assert result["count"] == len(result["items"]) >= 1, "count 和 items 对不上"

    for item in result["items"]:
        assert set(item) == EXPECTED_ITEM_KEYS, f"字段形状不对：{sorted(item)}"
        assert item["title"].strip(), f"混进了空标题的脏数据：{item}"
        assert item["source"] == "remoteok"
        assert isinstance(item["source_id"], str) and item["source_id"]
        assert item["source_id"] != "None", "id 缺失时被 str() 成了字符串 'None'"
        assert "description" not in item["extras"], "description 混进 extras 了"

    first = result["items"][0]
    print(f"  拿到 {result['count']} 条，字段形状全部合规 ✓")
    print(f"  样例：{first['title']} @ {first['company']}（{first['location']}）")


def check_pagination() -> None:
    print("\n═══ 路 A+：分页 ═══")
    page1 = fetch_job_list(page=1)
    page2 = fetch_job_list(page=2)

    ids1 = {i["source_id"] for i in page1["items"]}
    ids2 = {i["source_id"] for i in page2["items"]}
    assert ids1 and ids2, "某一页是空的，分页没生效"
    assert not (ids1 & ids2), f"两页有重叠，切片起点算错了：{sorted(ids1 & ids2)[:3]}"

    assert page1["count"] == PAGE_LIMIT, (
        f"第 1 页应有 {PAGE_LIMIT} 条，实际 {page1['count']} 条 —— "
        "多半是那个声明对象还留在切片里，被后面的 title 过滤器吃掉了"
    )
    assert page2["count"] == PAGE_LIMIT, f"第 2 页应有 {PAGE_LIMIT} 条，实际 {page2['count']} 条"
    print(f"  第 1 页 {len(ids1)} 条 / 第 2 页 {len(ids2)} 条，零重叠且页满 ✓")

    empty = fetch_job_list(page=50)
    assert empty["ok"] is True and empty["count"] == 0, "翻到底不该报错，应是 ok=True + 空列表"
    print("  翻到第 50 页：ok=True, count=0 ✓（空页 ≠ 出错）")


def check_policy_single_source() -> None:
    """考点：重试策略从工具【自己的声明】推导，不手抄（M4b 欠账的兑现点）。"""
    print("\n═══ 考点：重试策略的单一出处 ═══")
    spec = TOOLS["fetch_job_list"]
    original = spec.side_effect
    try:
        spec.side_effect = "external"
        assert jobs_mod._fetch_policy().attempts == 1, (
            "改了工具的声明，重试策略却没跟着变 —— 策略多半还是手抄的字符串字面量"
        )
        spec.side_effect = "read"
        assert jobs_mod._fetch_policy().attempts == 3, "改回 read 后策略没回来"
    finally:
        spec.side_effect = original
    print("  翻转 side_effect：external→1 次 / read→3 次，策略跟着声明走 ✓")


class _Boom:
    """一个只会抛异常的数据源替身，用来验证路 B。"""

    name = "boom"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def list_jobs(self, *, page: int = 1, limit: int = 20) -> JobPage:
        raise self._exc


def _patch_source(exc: Exception) -> None:
    """把 jobs 模块里的 get_source 换成"一调用就抛 exc"的替身。"""
    jobs_mod.get_source = lambda: _Boom(exc)


def check_failure_paths() -> None:
    print("\n═══ 路 B：失败路径（M2 核心考点）═══")
    original = jobs_mod.get_source
    try:
        _patch_source(SourceError("连不上 remoteok"))
        r = fetch_job_list(page=1)
        assert r["ok"] is False, "数据源抛了异常，工具却报告成功"
        assert r["error"] == "source_error", f"error 应是 source_error，实际 {r['error']!r}"
        assert "连不上" in (r["hint"] or ""), "hint 里没把原始原因带给模型"
        assert r["items"] == [] and r["count"] == 0, "失败时形状也要一致（items/count 要填）"
        print(f"  SourceError  → ok=False, error={r['error']!r} ✓")

        _patch_source(LoginExpired("302 → /student/login.jsp"))
        r = fetch_job_list(page=1)
        assert r["ok"] is False
        assert r["error"] == "login_expired", (
            f"error 应是 login_expired，实际 {r['error']!r} —— "
            "检查 except 分支的顺序：LoginExpired 是 SourceError 的【子类】，"
            "必须写在前面，否则永远轮不到它（被父类先接走了）"
        )
        assert "login" in (r["hint"] or ""), "hint 要告诉模型「去续期」，而不是让它重试"
        print(f"  LoginExpired → ok=False, error={r['error']!r} ✓")
    finally:
        jobs_mod.get_source = original


def main() -> None:
    check_schema()
    check_normal_path()
    check_pagination()
    check_policy_single_source()
    check_failure_paths()
    print("\n✅ M2a 通过：工具层的两条路都走得通，且模型在两路上都读得到有用的信息。")


if __name__ == "__main__":
    main()
