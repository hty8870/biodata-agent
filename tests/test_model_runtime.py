"""隔离模型运行时客户端单测：不真起子进程（fake _roundtrip），钉死两个子类的协议整形、
截断防御、畸形响应 fail-closed，以及共享基座的服务命令路由。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval import model_runtime as mr  # noqa: E402
from dataset_recommender.retrieval import model_worker as mw  # noqa: E402


def _fake_roundtrip(seen: dict, payload: dict):
    def roundtrip(self, request):
        seen.update(request)
        return {**payload, "id": request["id"], "ok": True}
    return roundtrip


# ---------- ExternalEmbedder ----------

def test_embedder_request_shape_and_12000_truncation(monkeypatch):
    seen = {}
    monkeypatch.setattr(mr._WorkerProcess, "_roundtrip",
                        _fake_roundtrip(seen, {"vectors": [[0.1, 0.2], [0.3, 0.4]]}))
    out = mr.ExternalEmbedder()(["短", "x" * 13000])
    assert seen["texts"][0] == "短"
    assert len(seen["texts"][1]) == 12000  # 与 recall_api / vector_index 同款截断
    assert out == [[0.1, 0.2], [0.3, 0.4]]


def test_embedder_vector_count_mismatch_raises(monkeypatch):
    monkeypatch.setattr(mr._WorkerProcess, "_roundtrip",
                        _fake_roundtrip({}, {"vectors": []}))
    with pytest.raises(RuntimeError):
        mr.ExternalEmbedder()(["a", "b"])


def test_embedder_non_list_vectors_raises(monkeypatch):
    monkeypatch.setattr(mr._WorkerProcess, "_roundtrip",
                        _fake_roundtrip({}, {"vectors": "broken"}))
    with pytest.raises(RuntimeError):
        mr.ExternalEmbedder()(["a"])


# ---------- ExternalCrossScorer（重构后行为逐位不变） ----------

def test_cross_scorer_request_shape(monkeypatch):
    seen = {}
    monkeypatch.setattr(mr._WorkerProcess, "_roundtrip",
                        _fake_roundtrip(seen, {"scores": [0.5]}))
    assert mr.ExternalCrossScorer()([("q", "d")]) == [0.5]
    assert seen["pairs"] == [["q", "d"]]


def test_cross_scorer_non_list_scores_raises(monkeypatch):
    monkeypatch.setattr(mr._WorkerProcess, "_roundtrip",
                        _fake_roundtrip({}, {"scores": "broken"}))
    with pytest.raises(RuntimeError):
        mr.ExternalCrossScorer()([("q", "d")])


# ---------- 服务命令路由（--embed 只挂在嵌入侧） ----------

def test_serve_commands(monkeypatch):
    monkeypatch.setattr(mr, "runtime_python", lambda paths=None: Path("py"))
    monkeypatch.setattr(mr, "worker_script", lambda paths=None: Path("worker.py"))
    monkeypatch.setattr(mr, "model_dir", lambda paths=None: Path("cross"))
    monkeypatch.setattr(mr, "embed_model_dir", lambda paths=None: Path("embed"))
    cross_cmd = mr.ExternalCrossScorer()._serve_command()
    embed_cmd = mr.ExternalEmbedder()._serve_command()
    assert cross_cmd[1:] == ["worker.py", "--serve", "cross"]
    assert embed_cmd[1:] == ["worker.py", "--serve", "embed", "--embed"]


def test_ready_gates_route_to_respective_checks(monkeypatch):
    monkeypatch.setattr(mr, "external_runtime_ready", lambda paths=None: True)
    monkeypatch.setattr(mr, "external_embed_ready", lambda paths=None: False)
    assert mr.ExternalCrossScorer()._ready() is True
    assert mr.ExternalEmbedder()._ready() is False


@pytest.fixture()
def fake_processes(tmp_path, monkeypatch):
    import io
    import json
    from dataclasses import replace
    from types import SimpleNamespace
    from dataset_recommender.retrieval import model_worker as mw
    paths = replace(mr.get_app_paths(), data_root=tmp_path, model_root=tmp_path / "models")
    target = mr.embed_model_dir(paths)
    target.mkdir(parents=True)
    (target / "config.json").write_bytes(b"{}")
    (target / "tokenizer.json").write_bytes(b"{}")
    def install(generation):
        (target / "pytorch_model.bin").write_bytes(str(generation).encode())
        mw._write_completion_marker(target)
    install(1)
    processes, controls = [], {}
    class Process:
        def __init__(self, command, **kwargs):
            self.generation = int((target / "pytorch_model.bin").read_bytes())
            self.stdout = io.StringIO('{"ready":true}\n')
            self.stderr = io.StringIO()
            self.terminated = False
            if controls.get("startup"):
                controls.pop("startup")()
            class Input(io.StringIO):
                def flush(inner):
                    request = json.loads(inner.getvalue().splitlines()[-1])
                    if controls.get("score"):
                        controls.pop("score")()
                    processes[-1].client.responses.put({"id": request["id"], "ok": True,
                        "vectors": [[float(processes[-1].generation)] for _ in request["texts"]]})
            self.stdin = Input()
            processes.append(self)
        def poll(self):
            return 0 if self.terminated else None
        def terminate(self):
            self.terminated = True
        def wait(self, timeout=None):
            return 0
    monkeypatch.setattr(mr.subprocess, "Popen", Process)
    monkeypatch.setattr(mr, "external_embed_ready", lambda paths=None: True)
    def client():
        embedder = mr.ExternalEmbedder(paths)
        embedder._start()
        processes[-1].client = embedder
        return embedder
    yield SimpleNamespace(client=client, paths=paths, processes=processes, controls=controls, install=install)
    for process in processes:
        process.terminate()


def test_external_actual_handle_generation_is_bound_and_warm_process_is_reused(fake_processes):
    state = fake_processes
    g1 = state.client()
    assert g1(["query"]) == [[1.0]] and g1(["again"]) == [[1.0]]
    assert len(state.processes) == 1
    state.install(2)
    with pytest.raises(RuntimeError, match="generation_changed"):
        g1(["stale handle"])
    assert state.processes[0].terminated and g1.process is None
    g2 = state.client()
    assert g2(["new handle"]) == [[2.0]] and len(state.processes) == 2
    assert g1.model_generation != g2.model_generation


def test_external_startup_generation_change_cannot_authenticate_a_stale_process(fake_processes):
    state = fake_processes
    state.controls["startup"] = lambda: state.install(2)
    with pytest.raises(RuntimeError, match="generation_changed"):
        state.client()
    assert state.processes[0].terminated
    g2 = state.client()
    assert g2(["query"]) == [[2.0]]


def test_external_late_old_response_is_rejected_on_generation_change(fake_processes):
    state = fake_processes
    g1 = state.client()
    state.controls["score"] = lambda: state.install(2)
    with pytest.raises(RuntimeError, match="generation_changed"):
        g1(["query"])
    assert state.processes[0].terminated
    assert state.client()(["query"]) == [[2.0]]


def test_late_reader_never_delivers_frames_into_replacement_process_queue(fake_processes):
    import queue
    state = fake_processes
    client = state.client()
    old_responses = client.responses
    client.responses = queue.Queue()
    client._reader(['{"id":"old","ok":true}\n'], old_responses)
    assert old_responses.get_nowait()["id"] == "old"
    assert client.responses.empty()


# ---------- 完成标记发布：Windows 句柄竞态的有界重试（实机 WinError 5 残余根因兜底） ----------

def _mini_model(tmp_path):
    target = tmp_path / "mini-model"
    target.mkdir()
    (target / "config.json").write_bytes(b"{}")
    (target / "tokenizer.json").write_bytes(b"{}")
    (target / "pytorch_model.bin").write_bytes(b"1")
    return target


def _flaky_replace(marker, failures):
    """返回 (注入包装, 计数)：对 marker 的替换前 `failures` 次抛 WinError 5，其余放行真替换。"""
    real_replace = os.replace
    calls = {"n": 0}

    def wrapped(src, dst):
        if os.fspath(dst) == os.fspath(marker) and calls["n"] < failures:
            calls["n"] += 1
            raise PermissionError(5, "拒绝访问（注入：句柄竞态）")
        return real_replace(src, dst)

    return wrapped, calls


def test_completion_marker_publish_retries_transient_permission_error(tmp_path, monkeypatch):
    """故障注入（瞬时 WinError 5）：有界重试消化后发布成功，marker 成为完整可校验证据。"""
    target = _mini_model(tmp_path)
    marker = mw._completion_marker_path(target)
    wrapped, calls = _flaky_replace(marker, failures=3)
    monkeypatch.setattr(mw.os, "replace", wrapped)
    mw._write_completion_marker(target)
    assert calls["n"] == 3, "瞬时 PermissionError 必须被有界重试消化"
    assert mw._completion_valid(target), "重试成功后 marker 必须是本次发布的完整证据"
    assert not list(target.glob(mw.COMPLETION_MARKER + ".*.tmp")), "成功后不得残留 tmp 文件"


def test_completion_marker_publish_is_fail_loud_and_keeps_old_marker(tmp_path, monkeypatch):
    """故障注入（持续 WinError 5）：重试有界、最终原样上抛（绝不伪成功）；旧 marker 字节逐字节保留。"""
    target = _mini_model(tmp_path)
    marker = mw._completion_marker_path(target)
    marker.write_bytes(b'{"old":"evidence"}\n')
    old_bytes = marker.read_bytes()
    wrapped, calls = _flaky_replace(marker, failures=mw._MARKER_PUBLISH_RETRIES + 10)
    monkeypatch.setattr(mw.os, "replace", wrapped)
    with pytest.raises(PermissionError):
        mw._write_completion_marker(target)
    assert calls["n"] == mw._MARKER_PUBLISH_RETRIES, "重试必须有界：第 N 次仍失败即上抛"
    assert marker.read_bytes() == old_bytes, "发布失败不得破坏旧 marker 字节"
    assert not list(target.glob(mw.COMPLETION_MARKER + ".*.tmp")), "失败路径不得残留 tmp 文件"


def test_external_generation_change_survives_transient_marker_publish_fault(fake_processes, monkeypatch):
    """代际冲突路径 + 瞬时 WinError 5 注入：重试消化故障后照常 RuntimeError generation_changed，
    新句柄读到新代际（原故障把本应 generation_changed 的测试掀成 PermissionError）。"""
    state = fake_processes
    marker = mw._completion_marker_path(mr.embed_model_dir(state.paths))
    wrapped, calls = _flaky_replace(marker, failures=2)
    monkeypatch.setattr(mw.os, "replace", wrapped)
    g1 = state.client()
    state.controls["score"] = lambda: state.install(2)
    with pytest.raises(RuntimeError, match="generation_changed"):
        g1(["query"])
    assert calls["n"] == 2 and state.processes[0].terminated
    assert state.client()(["query"]) == [[2.0]]
