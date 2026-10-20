"""L2 流程回放 —— 用一份【提交进仓库】的真实切片，把整条日报主流程离线跑一遍。"""

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
    """回放用的假 Provider —— 每次都说同一句话，于是"跑两次逐字节相同"可断言。"""

    model = "fake-model"

    def __init__(self, text: str = "今天这批岗位集中在工程与产品方向。") -> None:
        self.text = text

    def chat(self, messages, tools=None):  # noqa: ANN001, ANN201 —— 假实现，形状对即可
        return LLMResponse(
            content=self.text,
            tool_calls=[],
            prompt_tokens=0,
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
    expected_jobs = 29

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
