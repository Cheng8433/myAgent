"""状态层单测 —— 幂等写入、内容寻址存档、主键的两列。"""

from __future__ import annotations

from _fakes import job

from agent.memory.store import archive_raw, store_page, upsert_jobs, var_dir
from agent.tools.sources.base import Job, JobPage


def test_upsert_is_idempotent(conn):
    jobs = [job("1"), job("2")]
    first = upsert_jobs(conn, jobs, raw_path="raw/x.json", run_id="r1")
    second = upsert_jobs(conn, jobs, raw_path="raw/x.json", run_id="r2")
    assert first == {"inserted": 2, "updated": 0}
    assert second == {"inserted": 0, "updated": 2}
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 2


def test_first_seen_run_is_history(conn):
    """first_seen_run 一旦写下就不再改 —— 它的意思是"我第一次见到它是在哪次 run"。"""
    upsert_jobs(conn, [job("1")], raw_path=None, run_id="r1")
    upsert_jobs(conn, [job("1")], raw_path=None, run_id="r2")
    row = conn.execute("select first_seen_run from jobs where source_id='1'").fetchone()
    assert row["first_seen_run"] == "r1"


def test_current_state_is_refreshed(conn):
    """与 first_seen_run 相对的另一类列：岗位信息每次见到都要刷新。"""
    upsert_jobs(conn, [job("1", title="旧标题")], raw_path=None, run_id="r1")
    upsert_jobs(conn, [job("1", title="新标题")], raw_path=None, run_id="r2")
    row = conn.execute("select title, first_seen_run from jobs where source_id='1'").fetchone()
    assert (row["title"], row["first_seen_run"]) == ("新标题", "r1")


def test_two_sources_coexist(conn):
    """主键是 (source, source_id) —— 两个源各自从 1 开始编号，只按 id 会互相覆盖。"""
    a = Job(source="remoteok", source_id="1", title="甲", company="", url="")
    b = Job(source="ncss", source_id="1", title="乙", company="", url="")
    upsert_jobs(conn, [a], raw_path=None, run_id="r")
    upsert_jobs(conn, [b], raw_path=None, run_id="r")
    rows = conn.execute("select source, title from jobs order by source").fetchall()
    assert [(r["source"], r["title"]) for r in rows] == [("ncss", "乙"), ("remoteok", "甲")]


def test_same_source_same_id_dedupes_into_one_row(conn):
    upsert_jobs(conn, [job("1")], raw_path=None, run_id="r")
    upsert_jobs(conn, [job("1")], raw_path=None, run_id="r")
    assert conn.execute("select count(*) from jobs").fetchone()[0] == 1


def test_archive_raw_is_content_addressed(tmp_path):
    """同一份字节 → 同一个 sha1 文件名 → 只落一次盘。"""
    raw = b'{"a": 1}'
    rel1 = archive_raw(raw, root=tmp_path)
    rel2 = archive_raw(raw, root=tmp_path)
    assert rel1 == rel2
    assert rel1.startswith("raw/") and rel1.endswith(".json")
    assert len(list((var_dir(tmp_path) / "raw").glob("*.json"))) == 1


def test_archive_raw_distinct_content_distinct_files(tmp_path):
    archive_raw(b'{"a": 1}', root=tmp_path)
    archive_raw(b'{"a": 2}', root=tmp_path)
    assert len(list((var_dir(tmp_path) / "raw").glob("*.json"))) == 2


def test_archive_raw_returns_relative_posix_path(tmp_path):
    """库里存的是"逻辑路径"：相对 var/、正斜杠 —— 换台机器也读得出来。"""
    rel = archive_raw(b"x", root=tmp_path)
    assert "\\" not in rel
    assert not rel.startswith("/") and ":" not in rel
    assert (var_dir(tmp_path) / rel).is_file()


def test_store_page_archives_then_upserts(conn, tmp_path):
    """store_page = 存档 + 入库，合成一个函数（拆开写会让库和原文对不上）。"""
    raw = b'{"hello": "\xe4\xb8\xad\xe6\x96\x87"}'
    page = JobPage(jobs=[job("1")], raw=raw)
    counts = store_page(conn, page, run_id="r1", root=tmp_path)
    assert counts == {"inserted": 1, "updated": 0}
    raw_path = conn.execute("select raw_path from jobs").fetchone()["raw_path"]
    assert (var_dir(tmp_path) / raw_path).read_bytes() == raw
