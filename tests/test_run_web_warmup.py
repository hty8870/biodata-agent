# -*- coding: utf-8 -*-
"""`run_web.warm_web_recall` 契约：Web 启动期预热本地语义重排模型的四态行为。

全程 mock `vector_recall` 的可用性/预热函数——**绝不加载真实的 ~2GB cross-encoder**，
故本测在无模型的 CI 与有模型的本机都稳定、快速、确定。

回归意图：预热逻辑必须(1) 尊重 BIODATA_SKIP_RECALL_WARM 开发逃生阀；(2) 模型不可用时
秒过、绝不下载；(3) 可用则真的把它加载进缓存；(4) 任何加载失败都不阻断开服。

追加（of1）：`--open` 自动开浏览器的接线契约——缺省不开（历史行为逐字节一致）、
传参时「挂守候线程 → 预热 → 开服」顺序钉死；助手线程「端口可连即开页、超时如实提示」两态。
"""
import socket
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _p in (PROJECT_ROOT / "scripts", PROJECT_ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import run_web  # noqa: E402
import dataset_recommender.retrieval.vector_recall as VR  # noqa: E402


@pytest.fixture
def spy(monkeypatch):
    """把 vector_recall 的可用性/预热替换成可编程的间谍，记录调用参数。"""
    calls = {"available": [], "warm": []}

    def make(available_ret, warm_ret):
        def _available(backend):
            calls["available"].append(backend)
            return available_ret

        def _warm(backend):
            calls["warm"].append(backend)
            if isinstance(warm_ret, Exception):
                raise warm_ret
            return warm_ret

        monkeypatch.setattr(VR, "recall_backend_local_available", _available)
        monkeypatch.setattr(VR, "warm_recall_backend", _warm)
        return calls

    return make


def _quiet(*_a, **_k):
    return None


def test_skip_env_disables_warm(monkeypatch, spy):
    calls = spy(available_ret=True, warm_ret=True)
    monkeypatch.setenv("BIODATA_SKIP_RECALL_WARM", "1")
    assert run_web.warm_web_recall(log=_quiet) == "disabled"
    # 逃生阀开启时连可用性都不该探测，更不该加载。
    assert calls["available"] == [] and calls["warm"] == []


def test_unavailable_model_is_cheap_noop(monkeypatch, spy):
    monkeypatch.delenv("BIODATA_SKIP_RECALL_WARM", raising=False)
    calls = spy(available_ret=False, warm_ret=True)
    assert run_web.warm_web_recall(log=_quiet) == "unavailable"
    assert calls["available"] == ["cross_encoder"]
    assert calls["warm"] == [], "模型不可用时绝不能调用加载（避免下载/阻塞）"


def test_available_model_is_warmed(monkeypatch, spy):
    monkeypatch.delenv("BIODATA_SKIP_RECALL_WARM", raising=False)
    calls = spy(available_ret=True, warm_ret=True)
    assert run_web.warm_web_recall(log=_quiet) == "warmed"
    assert calls["warm"] == ["cross_encoder"]


def test_warm_failure_does_not_block_startup(monkeypatch, spy):
    monkeypatch.delenv("BIODATA_SKIP_RECALL_WARM", raising=False)
    spy(available_ret=True, warm_ret=False)
    assert run_web.warm_web_recall(log=_quiet) == "failed"


def test_warm_exception_is_swallowed(monkeypatch, spy):
    monkeypatch.delenv("BIODATA_SKIP_RECALL_WARM", raising=False)
    spy(available_ret=True, warm_ret=RuntimeError("boom"))
    # 绝不向上抛：开服不能被模型加载异常阻断。
    assert run_web.warm_web_recall(log=_quiet) == "failed"


def test_main_calls_warm_before_serving(monkeypatch):
    """main() 必须在 uvicorn.run 之前预热——否则首个请求仍现加载。

    不设 sys.argv（pytest 自身参数在场）：parse_known_args 必须忽略未知参数且缺省不开浏览器。"""
    order = []
    serve_kwargs = {}
    monkeypatch.setattr(run_web, "open_browser_when_ready", lambda h, p: order.append("open"))
    monkeypatch.setattr(run_web, "warm_web_recall", lambda *a, **k: order.append("warm") or "warmed")
    def serve(*args, **kwargs):
        serve_kwargs.update(kwargs)
        order.append("serve")
    monkeypatch.setattr(run_web.uvicorn, "run", serve)
    run_web.main()
    assert order == ["warm", "serve"]
    assert serve_kwargs["workers"] == 1


def test_main_open_flag_starts_watcher_before_warm(monkeypatch):
    """--open 时必须先挂浏览器守候线程（端口未通前轮询）再预热再开服——顺序反了浏览器会撞连接拒绝。"""
    order = []
    monkeypatch.setattr(run_web, "open_browser_when_ready", lambda h, p: order.append(("open", h, p)))
    monkeypatch.setattr(run_web, "warm_web_recall", lambda *a, **k: order.append("warm") or "warmed")
    monkeypatch.setattr(run_web.uvicorn, "run", lambda *a, **k: order.append("serve"))
    monkeypatch.setattr(sys, "argv", ["run_web.py", "--open"])
    run_web.main()
    assert order == [("open", "127.0.0.1", 7860), "warm", "serve"]


def test_open_browser_when_ready_opens_once_port_listens(monkeypatch):
    """端口可连即开页：URL 必须是实际 host:port，且只开一次。"""
    opened = []
    monkeypatch.setattr(run_web.webbrowser, "open", lambda url: opened.append(url) or True)
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        thread = run_web.open_browser_when_ready("127.0.0.1", port, poll_s=0.02, log=_quiet)
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert opened == [f"http://127.0.0.1:{port}"]


def test_open_browser_when_ready_timeout_is_honest(monkeypatch):
    """超时不开页、不抛异常，只如实提示手动地址——开服绝不能被浏览器线程拖挂。"""
    opened = []
    logs = []
    monkeypatch.setattr(run_web.webbrowser, "open", lambda url: opened.append(url) or True)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()
    thread = run_web.open_browser_when_ready(
        "127.0.0.1", dead_port, timeout_s=0.2, poll_s=0.05, log=logs.append
    )
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert opened == []
    assert any("手动访问" in m for m in logs)
