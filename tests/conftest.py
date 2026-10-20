"""pytest 共享夹具 —— M7 回归网的地基。"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.memory.db import connect, ensure_schema
from agent.observability.logs import shutdown_logging

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "eval" / "fixtures" / "remoteok_sample.json"
"""L2 回放与数据源用例共用的那份真实切片。"""


def _one_cert_pem() -> str:
    """从 certifi 的 bundle 里取【第一张】证书 —— 够 ssl 解析就行。"""
    import certifi

    bundle = Path(certifi.where()).read_text(encoding="utf-8")
    return bundle.split("-----END CERTIFICATE-----", 1)[0] + "-----END CERTIFICATE-----\n"


@pytest.fixture(autouse=True)
def _cheap_ca_bundle(monkeypatch, tmp_path):
    """让 httpx 别去解析那 270 KB 的 certifi CA bundle。"""
    pem = tmp_path / "one-cert.pem"
    pem.write_text(_one_cert_pem(), encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(pem))


@pytest.fixture(autouse=True)
def _quiet_logs():
    """每个用例前后都把日志关掉、回到静默。"""
    shutdown_logging()
    yield
    shutdown_logging()


@pytest.fixture(autouse=True)
def _pin_source(monkeypatch):
    """把 JOB_SOURCE 钉成 remoteok。"""
    monkeypatch.setenv("JOB_SOURCE", "remoteok")


@pytest.fixture
def conn(tmp_path):
    """一个建好表、跑完就关的临时库。"""
    connection = connect(tmp_path / "app.db")
    ensure_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def match_env(tmp_path, monkeypatch):
    """匹配用例的隔离环境：DB_PATH 与 VAR_DIR 都指向临时目录。

    工具在别的线程里自己 connect()，所以【必须】走环境变量，
    不能只把连接对象传进去。
    """
    db_path = tmp_path / "match.db"
    var_path = tmp_path / "var"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("VAR_DIR", str(var_path))
    connection = connect(db_path)
    ensure_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def fixture_bytes() -> bytes:
    """eval/fixtures 里那份真实响应体切片（data[0] 声明对象 + 29 条岗位）。"""
    return FIXTURE_PATH.read_bytes()
