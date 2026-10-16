"""L2 流程回放 —— 用一份【提交进仓库】的真实切片，把整条日报主流程离线跑一遍。

和 L1（pytest）的分工：L1 盯【零件】（解析、去重、脱敏、渲染各自对不对），
L2 盯【串起来的那条线】（抓→存→摘要→落盘→落库，跑两次结果一致）。

★ 为什么 fixture 是 30 条（= 1 个声明对象 + 29 条岗位）而不是随手 12 条：
  `RemoteOkSource` 每次 GET 都拿【全量】、在本地按 `(page-1)*limit` 切片
  （limit=20）。12 条只有 1 页 —— "多页共用同一份 raw"这条性质根本断言不到。
  30 条 → 29 条岗位 → 3 次 GET → 20 + 9 + 0（空页收尾）→ **3 页共享同一份 raw**
  → `var/raw/` 里只有【一个】文件。这条断言是"内容寻址存档"的活证据：
  远端一次给全量，我们翻 5 页也只落一份盘。

★ 为什么在 httpx 边界用 respx、而不是 patch 掉 fetch_page：
  patch fetch_page 等于跳过整个数据源层 —— 那 L1 已经单独测过了。
  在 httpx 边界截住，走的是【真】remoteok 路径（真重试、真切片、真映射），
  只有网络换成内存里的一段字节。这是"回放"该有的样子。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import respx

from agent.llm.base import LLMResponse
from agent.memory.db import connect, ensure_schema, new_run_id
from agent.memory.store import var_dir
from agent.runtime.budget import Budget
from agent.tools.sources.remoteok import API_URL
from eval import Check
from workflows.daily_report import STATUS_SUCCESS, run_daily_report

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "remoteok_sample.json"


class _FakeLLM:
    """回放用的假 Provider —— 每次都说同一句话，于是"跑两次逐字节相同"可断言。

    ★ 只活在 eval 内部、且只此一处，故不放进 tests/_fakes —— 那会让
      tests 与 eval 互相 import。离线与真链路的边界，就划在这一行。
    """

    model = "fake-model"

    def __init__(self, text: str = "今天这批岗位集中在工程与产品方向。") -> None:
        self.text = text

    def chat(self, messages, tools=None):  # noqa: ANN001, ANN201 —— 假实现，形状对即可
        return LLMResponse(
            content=self.text,
            tool_calls=[],
            prompt_tokens=0,  # L2 全离线 → 不产生真实 token 成本
            completion_tokens=0,
            finish_reason="stop",
        )


def _budget() -> Budget:
    return Budget(max_steps=8, max_tokens=10**9, max_wall_seconds=60.0)


def _raw_files(root: Path) -> int:
    d = var_dir(root) / "raw"
    return len(list(d.glob("*.json"))) if d.exists() else 0


def _without_timestamp(doc: str) -> str:
    """去掉 `> 生成于 …` 那一行 —— 两次 run 的生成时刻天然不同。"""
    return "\n".join(line for line in doc.splitlines() if not line.startswith("> 生成于"))


def run_replay() -> list[Check]:
    """跑 L2，返回断言结果。全程离线、临时目录，不碰主人的 var/。"""
    checks: list[Check] = []
    body = FIXTURE.read_bytes()
    expected_jobs = 29  # 30 项切片减掉 1 个声明对象

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        conn = connect(root / "app.db")
        ensure_schema(conn)
        try:
            with respx.mock:
                respx.get(API_URL).mock(return_value=httpx.Response(200, content=body))
                rep1 = run_daily_report(
                    conn, _FakeLLM(), run_id=new_run_id(), budget=_budget(), root=root
                )
                rep2 = run_daily_report(
                    conn, _FakeLLM(), run_id=new_run_id(), budget=_budget(), root=root
                )

            checks.append(Check("status=success", rep1.status == STATUS_SUCCESS, rep1.status))
            checks.append(
                Check(
                    "声明对象被跳过（抓到 29 条而不是 30）",
                    rep1.total == expected_jobs,
                    f"total={rep1.total}",
                )
            )
            checks.append(
                Check(
                    "首次全部是新增",
                    (rep1.inserted, rep1.updated) == (expected_jobs, 0),
                    f"inserted={rep1.inserted} updated={rep1.updated}",
                )
            )
            checks.append(
                Check(
                    "新增 + 更新 == 输入条数",
                    rep1.inserted + rep1.updated == expected_jobs,
                    f"{rep1.inserted}+{rep1.updated}",
                )
            )
            checks.append(
                Check(
                    "★ 3 页共享同一份 raw → var/raw/ 只有 1 个文件",
                    _raw_files(root) == 1,
                    f"raw_files={_raw_files(root)}",
                )
            )

            # ── 幂等：同一天再跑一次 ──
            checks.append(
                Check(
                    "第二次全是刷新（inserted=0）",
                    (rep2.inserted, rep2.updated) == (0, expected_jobs),
                    f"inserted={rep2.inserted} updated={rep2.updated}",
                )
            )
            checks.append(Check("两次写的是同一个文件", rep2.report_path == rep1.report_path))

            doc1 = (var_dir(root) / rep1.report_path).read_text(encoding="utf-8")
            doc2 = (var_dir(root) / rep2.report_path).read_text(encoding="utf-8")
            checks.append(
                Check(
                    "★ 报告没被追加（去掉时间戳后逐字节相同）",
                    _without_timestamp(doc1) == _without_timestamp(doc2),
                )
            )
            checks.append(
                Check(
                    "★ 再跑一遍 raw 仍是 1 个（不计重复落盘）",
                    _raw_files(root) == 1,
                    f"raw_files={_raw_files(root)}",
                )
            )
            checks.append(
                Check(
                    "库里的岗位没翻倍",
                    conn.execute("select count(*) from jobs").fetchone()[0] == expected_jobs,
                )
            )
        finally:
            conn.close()

    return checks


def _main() -> int:  # pragma: no cover —— 手工探针
    checks = run_replay()
    failed = [c for c in checks if not c.ok]
    for c in checks:
        print(f"   {'OK ' if c.ok else 'BAD'} {c.label}" + (f"  [{c.note}]" if c.note else ""))
    print(f"\nL2：{len(checks) - len(failed)} passed / {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
