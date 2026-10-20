"""心跳文件（M9）—— 原子写、能读回、坏文件不伪装成"没跑过"。"""

from __future__ import annotations

import json

import pytest

from app.heartbeat import heartbeat_path, read_heartbeat, write_heartbeat


def test_roundtrip(tmp_path):
    payload = {
        "at": "2026-10-20T09:00:00+00:00",
        "run_id": "20261020T090000-abcd",
        "status": "success",
        "exit_code": 0,
        "report_path": "reports/2026-10-20.md",
    }
    rel = write_heartbeat(payload, root=tmp_path)

    assert rel == "heartbeat.json"
    assert read_heartbeat(root=tmp_path) == payload
    assert heartbeat_path(tmp_path) == tmp_path / "heartbeat.json"


def test_missing_file_means_never_ran(tmp_path):
    assert read_heartbeat(root=tmp_path) is None


def test_corrupt_file_is_an_error_not_an_absence(tmp_path):
    (tmp_path / "heartbeat.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError) as info:
        read_heartbeat(root=tmp_path)
    assert "损坏" in str(info.value)


def test_non_object_is_an_error(tmp_path):
    (tmp_path / "heartbeat.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ValueError):
        read_heartbeat(root=tmp_path)


def test_atomic_write_leaves_no_temp_files(tmp_path):
    write_heartbeat({"at": "x"}, root=tmp_path)
    assert list(tmp_path.glob("*.tmp")) == []
    assert (tmp_path / "heartbeat.json").exists()


def test_overwrite_replaces_previous_heartbeat(tmp_path):
    write_heartbeat({"status": "failed"}, root=tmp_path)
    write_heartbeat({"status": "success"}, root=tmp_path)
    assert read_heartbeat(root=tmp_path) == {"status": "success"}
