"""pytest 共享夹具 —— M7 回归网的地基。

★ 这个目录【不加】 `__init__.py`：pytest 会把测试目录插进 sys.path，
  于是 `import _fakes` 直接可用（加了 `__init__.py` 反而要写成包内相对导入）。
  这是 pytest 的"rootdir + importmode"默认行为，不是巧合。

★ 这里只放【多个测试文件】共用的东西。夹具的判据同 _fakes：一处定义、
  多处消费；只服务一个文件的夹具留在那个文件里。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.memory.db import connect, ensure_schema
from agent.observability.logs import shutdown_logging

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "eval" / "fixtures" / "remoteok_sample.json"
"""L2 回放与数据源用例共用的那份真实切片。

★ 为什么 tests/ 读 eval/ 的数据文件不算"两个模块互相 import"：
  eval 与 tests 的分工是"不让它们 import 对方的【代码】"（各自有假 LLM），
  而这是一份【数据】。把它在这里再抄一份，就是"同一个事实两个出处"——
  改了一处忘了另一处，不报错，只是回放的数据和单测的数据悄悄走散。
"""


def _one_cert_pem() -> str:
    """从 certifi 的 bundle 里取【第一张】证书 —— 够 ssl 解析就行。"""
    import certifi

    bundle = Path(certifi.where()).read_text(encoding="utf-8")
    return bundle.split("-----END CERTIFICATE-----", 1)[0] + "-----END CERTIFICATE-----\n"


@pytest.fixture(autouse=True)
def _cheap_ca_bundle(monkeypatch, tmp_path):
    """让 httpx 别去解析那 270 KB 的 certifi CA bundle。

    ★ 本机实测：每次 `httpx.get` 建连都会重新 `ssl.create_default_context(
      cafile=certifi.where())`，而加载那个 bundle 要 1.3~2.4 秒（Windows + 杀软）。
      73 个用例里十几处建连，光这一项就吃掉 20 秒。

    ★ 这不是在放水 TLS：respx 已经在 httpx 的 transport 边界截住了【所有】
      请求，测试里根本不发生 TLS 握手。被关掉的只是"每次建连都重读一遍 CA"
      这笔与本项目无关的环境税 —— 换成一个只含一张证书的 PEM，
      Client 初始化从 1.3s 掉到 0.02s，且【不改变任何一条被断言的行为】。

    ★ 用官方支持的 `SSL_CERT_FILE` 环境变量（httpx._config 会读它），
      不去 patch httpx 的私有函数 —— 私有 API 换个版本就烂。
      用 monkeypatch 而不是直接改 os.environ：用例一结束就自动还原，
      免得污染同进程里跑 L3 真链路的 eval.run_eval。
    """
    pem = tmp_path / "one-cert.pem"
    pem.write_text(_one_cert_pem(), encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(pem))


@pytest.fixture(autouse=True)
def _quiet_logs():
    """每个用例前后都把日志关掉、回到静默。

    ★ 为什么 autouse：logs 的处理器是【模块级全局】的（structlog.configure
      是单例）。一个用例装了 handler 不清，下一个用例就接着用，而且
      Windows 上被打开着的句柄删不掉 —— tmp_path 的清理会抛 PermissionError，
      那条异常会盖住真正的断言失败。前后各放一次：前面保证干净起步，
      后面保证干净收场。
    """
    shutdown_logging()
    yield
    shutdown_logging()


@pytest.fixture(autouse=True)
def _pin_source(monkeypatch):
    """把 JOB_SOURCE 钉成 remoteok。

    ★ 为什么 autouse：主人的 .env 将来可能写着 JOB_SOURCE=ncss，
      于是"应该走 remoteok 真路径"的用例会静默走到另一条路上 ——
      那不是失败，是测错了对象（假绿）。钉住它，用例只依赖仓库里的代码。
    """
    monkeypatch.setenv("JOB_SOURCE", "remoteok")


@pytest.fixture
def conn(tmp_path):
    """一个建好表、跑完就关的临时库。

    ★ 为什么是 yield 夹具、不是 return：Windows 上 sqlite 连接不关，
      tmp_path 删不掉，清理异常会盖住断言失败（同 logs 那条）。所以
      用完必须 close —— yield 保证它在用例结束后一定被执行。
    """
    connection = connect(tmp_path / "app.db")
    ensure_schema(connection)
    yield connection
    connection.close()


@pytest.fixture
def fixture_bytes() -> bytes:
    """eval/fixtures 里那份真实响应体切片（data[0] 声明对象 + 29 条岗位）。"""
    return FIXTURE_PATH.read_bytes()
