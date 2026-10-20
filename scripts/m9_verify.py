"""M9 验收台 —— 部署的四块地基（锁 / 退出码 / 心跳 / 任务入口）。"""

from __future__ import annotations

import os
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import app.main as cli
from agent.config import ConfigError
from app.heartbeat import read_heartbeat, write_heartbeat
from app.lock import LockBusy, exclusive_lock

failures = 0


def check(label: str, got: object, want: object, note: str = "") -> None:
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"   {'OK ' if ok else 'BAD'} {label} → {got!r}   期望 {want!r}  {note}")


def _try_enter(lock: Path) -> str:
    """试第二把锁：返回 'busy' 或 'acquired'，不在这里断言。"""
    try:
        with exclusive_lock(lock):
            return "acquired"
    except LockBusy:
        return "busy"


def main() -> None:
    repo = Path(__file__).resolve().parent.parent

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmp_path = Path(tmp)

        print("\n═══ ① 单实例锁 ═══")
        lock = tmp_path / "var" / "app.lock"
        with exclusive_lock(lock):
            check("持有期间第二把锁被拒", _try_enter(lock), "busy")
        check("释放后可以重新加锁", _try_enter(lock), "acquired")

        try:
            with exclusive_lock(lock):
                raise RuntimeError("模拟抓取中途崩掉")
        except RuntimeError:
            pass
        check(
            "★ 异常路径也释放（否则第二天永远跑不了）",
            _try_enter(lock),
            "acquired",
        )

        with exclusive_lock(lock):
            pass
        text = lock.read_text(encoding="utf-8")
        check("锁文件留下 pid/时间作为事后线索", ("pid=" in text, "at=" in text), (True, True))
        check("锁文件存在≠锁存在", lock.exists(), True)

        print("\n═══ ② 配置错误是 2，不是 1 ═══")
        original_report = cli.cmd_report

        def boom() -> int:
            raise ConfigError("缺少必填环境变量 M9_DEMO（演示用）")

        cli.cmd_report = boom
        try:
            code = cli.main(["report"])
        finally:
            cli.cmd_report = original_report
        check("缺少配置 → 退出码 2", code, cli.EXIT_CONFIG)

        print("\n═══ ③ 心跳文件 ═══")
        beat_dir = tmp_path / "beat"
        payload = {
            "at": "2026-10-20T09:00:00+00:00",
            "run_id": "20261020T090000-abcd",
            "status": "success",
            "exit_code": 0,
        }
        rel = write_heartbeat(payload, root=beat_dir)
        check("写入返回相对路径", rel, "heartbeat.json")
        check("读回一致", read_heartbeat(root=beat_dir), payload)
        check("原子写不留临时文件", list(beat_dir.glob("*.tmp")), [])

        (beat_dir / "heartbeat.json").write_text("{ 这不是 json", encoding="utf-8")
        try:
            read_heartbeat(root=beat_dir)
            corrupted = "没报错"
        except ValueError:
            corrupted = "ValueError"
        check(
            "★ 损坏是错误，不是「没跑过」",
            corrupted,
            "ValueError",
            "把损坏吞成 None，等于在最需要线索时说一切正常",
        )

        print("\n═══ ④ status：只读，且缺东西也能好好说话 ═══")
        status_dir = tmp_path / "status"
        keys = ("VAR_DIR", "DB_PATH", "JOB_SOURCE")
        saved = {key: os.environ.get(key) for key in keys}
        os.environ.update(
            VAR_DIR=str(status_dir),
            DB_PATH=str(status_dir / "app.db"),
            JOB_SOURCE="ncss",
        )

        def run_status() -> tuple[int, str]:
            buf = StringIO()
            with redirect_stdout(buf):
                code = cli.cmd_status()
            return code, buf.getvalue()

        try:
            code, out = run_status()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        check("status 退出码", code, cli.EXIT_OK)
        check("今日额度未使用", "今日额度（东八区）：未使用" in out, True)
        check("没有心跳时照实说", "心跳：还没有记录" in out, True)
        check("没登录过时给出下一步", "登录态：还没有" in out, True)
        check(
            "★ status 不创建心跳（只读）",
            (status_dir / "heartbeat.json").exists(),
            False,
        )

        print("\n═══ ⑤ 部署件 ═══")
        runner = repo / "scripts" / "run_report.cmd"
        register = repo / "scripts" / "register_task.ps1"
        unregister = repo / "scripts" / "unregister_task.ps1"
        for path in (runner, register, unregister):
            check(f"{path.name} 存在", path.exists(), True)
        runner_text = runner.read_text(encoding="utf-8")
        check("入口跑的是 report", "-m app.main report" in runner_text, True)
        check("入口设了 UTF-8", "PYTHONUTF8=1" in runner_text, True)
        register_text = register.read_text(encoding="utf-8")
        check("注册脚本防叠加", "IgnoreNew" in register_text, True)
        check("注册脚本每天 08:00", '"08:00"' in register_text, True)

        print("\n═══ ⑥ report 撞锁 → 退出码 4 ═══")
        busy_dir = tmp_path / "busy"
        saved_var = os.environ.get("VAR_DIR")
        os.environ["VAR_DIR"] = str(busy_dir)
        entered: list[int] = []
        original_locked = cli._report_locked

        def should_not_enter() -> int:
            entered.append(1)
            return 0

        cli._report_locked = should_not_enter
        try:
            with exclusive_lock(busy_dir / "app.lock"):
                code = cli.cmd_report()
        finally:
            cli._report_locked = original_locked
            if saved_var is None:
                os.environ.pop("VAR_DIR", None)
            else:
                os.environ["VAR_DIR"] = saved_var
        check("撞锁 → 4", code, cli.EXIT_BUSY)
        check("★ 没有进到锁内逻辑", entered, [])

    print()
    if failures:
        print(f">>> {failures} 项不符。")
    else:
        print("✅ M9 通过：锁互斥且无僵尸、配置错误有独立退出码、心跳坏了不装没事、status 只读。")


if __name__ == "__main__":
    main()
