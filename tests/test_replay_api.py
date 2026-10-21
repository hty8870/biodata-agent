# -*- coding: utf-8 -*-
"""`/api/replay/*` 端点 + utterance/recommend 落痕挂钩（**零网络**：route_turn 与 call_llm 一律替身）。

存储重定向两条路：
- 挂钩测试：monkeypatch `replay_service.record_turn` 把 project_root 换成 tmp_path
  （recommend 还要用真 PROJECT_ROOT 载语料，不能整换）；
- replay 端点测试：monkeypatch `webapp.PROJECT_ROOT`（这些端点不碰语料）。
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.agent import turn
from dataset_recommender.app import replay_service as SV
from dataset_recommender.app import session_log as SL
from dataset_recommender.app import webapp
from dataset_recommender.app.webapp import app

client = TestClient(app, base_url="http://127.0.0.1")

FAKE_RESULT = {
    "route": "none",
    "query": "",
    "plan": {"verb": "none", "source": "rule"},
    "echo_zh": "假回声",
    "retrieval": None,
    "via": "rule",
}

C1, K1 = "cTEST1-ab12", "k-TEST"


@pytest.fixture(autouse=True)
def _session_log_on(monkeypatch):
    """离线验证 harness 把 BIODATA_SESSION_LOG 钉成 off；本文件断言落痕链路，显式打开。"""
    monkeypatch.setenv("BIODATA_SESSION_LOG", "on")


@pytest.fixture(autouse=True)
def _clean_replay():
    SV._reset_for_tests()
    SL._reset_for_tests()
    yield
    SV._reset_for_tests()
    SL._reset_for_tests()


@pytest.fixture
def hook_root(tmp_path, monkeypatch):
    """挂钩的 record_turn 重定向到 tmp_path（真 PROJECT_ROOT 留给语料）。"""
    real = SV.record_turn
    monkeypatch.setattr(SV, "record_turn", lambda root, *a, **k: real(tmp_path, *a, **k))
    return tmp_path


@pytest.fixture
def replay_root(tmp_path, monkeypatch):
    """replay 端点的存储根重定向（这些端点不碰语料）。"""
    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def fake_llm(monkeypatch):
    """call_llm 替身 + _replay_cfg_or_none 替身（免 env 物化）。"""
    cfg = types.SimpleNamespace(mock_llm=False, enable_llm=True, api_key="sk-fake",
                                provider="deepseek", model="m-fake")
    monkeypatch.setattr(webapp, "_replay_cfg_or_none", lambda payload: cfg)
    monkeypatch.setattr(SV, "call_llm", lambda prompt, c: types.SimpleNamespace(
        text='{"motivation": {"id": "start", "title": "找肺癌数据", "detail": "d", "turn_refs": [1]},'
             '"outputs": [{"id": "o1", "title": "锁定 GSE131907", "detail": "d", "turn_refs": [1]}],'
             '"knowledge": [], "decisions": [], "edges": [{"from": "start", "to": "o1", "label": "驱动"}]}',
        succeeded=True))
    return cfg


# ---------------------------------------------------------------- 挂钩（utterance / recommend）

def test_utterance_hook_records_chat_turn(hook_root, monkeypatch):
    monkeypatch.setattr(turn, "route_turn", lambda text, **kw: dict(FAKE_RESULT))
    res = client.post("/api/utterance", json={
        "utterance": "你好", "agent": False, "conv_id": C1, "replay_key": K1})
    assert res.status_code == 200
    recs = SL.read_records(hook_root, "anonymous", C1)
    assert len(recs) == 1 and recs[0]["kind"] == "chat"
    assert recs[0]["payload"]["user"] == "你好" and recs[0]["payload"]["reply"] == "假回声"
    # 第二轮带错 key：主请求正常响应，复盘读写均拒绝且日志无副作用。
    res2 = client.post("/api/utterance", json={
        "utterance": "再说一句", "agent": False, "conv_id": C1, "replay_key": "k-WRONG"})
    assert res2.status_code == 200
    assert len(SL.read_records(hook_root, "anonymous", C1)) == 1


def test_utterance_no_conv_id_records_nothing(hook_root, monkeypatch):
    monkeypatch.setattr(turn, "route_turn", lambda text, **kw: dict(FAKE_RESULT))
    res = client.post("/api/utterance", json={"utterance": "你好", "agent": False})
    assert res.status_code == 200
    assert SL.read_records(hook_root, "anonymous", C1) == []


def _sse_frames(body_text):
    return [json.loads(line[len("data: "):]) for line in body_text.splitlines() if line.startswith("data: ")]


def test_utterance_stream_hook_records_chat_turn(hook_root, monkeypatch):
    """流式路径（前端 ubFetchStream 实际走的那条）的挂钩回归钉：2026-09-28 初版挂钩在
    流式 worker 作用域里引用了不存在的 request → NameError 撞上结构性防御、成功轮被
    误判成错误帧——非流式测试全绿没拦住。本钉专守流式：final 必须 ok 且落痕在。"""
    monkeypatch.setattr(turn, "route_turn", lambda text, **kw: dict(FAKE_RESULT))
    res = client.post("/api/utterance", json={
        "utterance": "你好", "agent": False, "stream": True, "conv_id": C1, "replay_key": K1})
    assert res.status_code == 200
    finals = [f for f in _sse_frames(res.text) if f.get("event") == "final"]
    assert finals and finals[0]["data"]["ok"] is True  # 成功轮不得被复盘误判成错误帧
    recs = SL.read_records(hook_root, "anonymous", C1)
    assert len(recs) == 1 and recs[0]["kind"] == "chat" and recs[0]["payload"]["user"] == "你好"


def test_utterance_stream_hook_failure_never_breaks_turn(hook_root, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("复盘炸了")
    monkeypatch.setattr(webapp, "_replay_record_utterance", boom)
    monkeypatch.setattr(turn, "route_turn", lambda text, **kw: dict(FAKE_RESULT))
    res = client.post("/api/utterance", json={
        "utterance": "你好", "agent": False, "stream": True, "conv_id": C1, "replay_key": K1})
    assert res.status_code == 200
    finals = [f for f in _sse_frames(res.text) if f.get("event") == "final"]
    assert finals and finals[0]["data"]["ok"] is True  # 挂钩炸了主路径也无损


def test_utterance_hook_failure_never_breaks_turn(hook_root, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("复盘炸了")
    monkeypatch.setattr(SV, "record_turn", boom)
    monkeypatch.setattr(turn, "route_turn", lambda text, **kw: dict(FAKE_RESULT))
    res = client.post("/api/utterance", json={
        "utterance": "你好", "agent": False, "conv_id": C1, "replay_key": K1})
    assert res.status_code == 200 and res.json()["ok"] is True  # fail-soft：主路径无损


def test_recommend_hook_records_search_turn(hook_root):
    res = client.post("/api/recommend", json={
        "query": "肺癌 单细胞", "sources": ["10x Genomics"], "use_llm": False,
        "conv_id": C1, "replay_key": K1})
    assert res.status_code == 200
    recs = SL.read_records(hook_root, "anonymous", C1)
    assert len(recs) == 1 and recs[0]["kind"] == "search"
    p = recs[0]["payload"]
    assert p["query"] == "肺癌 单细胞" and "total" in p and "status" in p and isinstance(p["top"], list)


# ---------------------------------------------------------------- 端点（status/points/digest/annotate/snapshot/seen/chat/backfill）

def test_status_empty_conv_is_empty_state(replay_root):
    res = client.get(f"/api/replay/status?conv_id={C1}&key={K1}")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True and body["n_turns"] == 0 and body["has_digest"] is False
    assert body["unseen"] == 0 and body["drawing"] is False


def test_backfill_then_full_window_cycle(replay_root, fake_llm):
    # 补建（用户显式动作）
    res = client.post("/api/replay/backfill", json={
        "conv_id": C1, "key": K1,
        "turns": [{"role": "user", "text": "找肺癌数据"}, {"role": "assistant", "text": "找到 3 个"}]})
    assert res.status_code == 200 and res.json() == {"ok": True, "n": 2}
    # 手动整理（fake LLM 真链）
    res = client.post("/api/replay/digest", json={"conv_id": C1, "key": K1})
    assert res.status_code == 200 and res.json()["has_digest"] is True
    # 窗口载荷
    res = client.get(f"/api/replay/points?conv_id={C1}&key={K1}")
    pts = res.json()
    assert pts["digest"]["outputs"][0]["title"] == "锁定 GSE131907"
    assert pts["unseen"] == ["o-0"]
    status = client.get(f"/api/replay/status?conv_id={C1}&key={K1}").json()
    assert status["unseen"] == len(pts["unseen"])   # 角标数字与图谱打标集同真源（同一函数同一口径）
    # 标注 → 快照（添加到对话）→ 快照内追加 → 已读
    assert client.post("/api/replay/annotate", json={
        "conv_id": C1, "key": K1, "pid": "o-0", "text": "可靠吗"}).json() == {"ok": True}
    snap = client.post("/api/replay/snapshot", json={"conv_id": C1, "key": K1}).json()
    assert snap["ok"] is True and snap["count"] == 1 and "可靠吗" in snap["text"]
    sid = snap["snap_id"]
    res = client.get(f"/api/replay/snapshots?conv_id={C1}&key={K1}")
    assert res.json()["snapshots"][0]["snap_id"] == sid
    res = client.post(f"/api/replay/snapshot/{sid}/annotate", json={
        "conv_id": C1, "key": K1, "pid": "o-0", "text": "追加"})
    assert res.json()["snapshot"]["count"] == 2
    seen = client.post("/api/replay/seen", json={"conv_id": C1, "key": K1})
    assert seen.status_code == 200 and seen.json()["ok"] is True   # 旧 body 仍可用（additive）
    assert seen.json()["marked"] == 1 and seen.json()["stale"] is False
    assert client.get(f"/api/replay/status?conv_id={C1}&key={K1}").json()["unseen"] == 0
    # sidechat（fake call_llm 恒回 digest JSON 文本，回复非空即链路通）
    res = client.post("/api/replay/chat", json={"conv_id": C1, "key": K1, "text": "这段对话干了啥"})
    assert res.json()["ok"] is True and res.json()["reply"]


def test_edge_only_snapshot_renders_in_the_real_layout_engine(replay_root, fake_llm, tmp_path):
    """端到端：只标一条关系 → 真实 /api/replay/snapshot 载荷 → node 真渲染引擎。

    载荷不手造：走真 HTTP 端点 → 真 replay_store → 真 JSON；node 侧跑真 snapshotDigest +
    真 computeLayout（布局模块整文件 vm 求值，不抄常量）。载荷缺闭包时两边同时红。
    驱动的 node 段在 tests/js/frontend_lifecycle_spec.mjs（同一个既有规格文件，未新增文件）。"""
    import os
    import shutil
    import subprocess

    root = Path(__file__).resolve().parents[1]
    node = os.environ.get("BIODATA_NODE") or shutil.which("node") or shutil.which("node.exe")
    if not node:
        pytest.skip("Node is unavailable")
    client.post("/api/replay/backfill", json={
        "conv_id": C1, "key": K1,
        "turns": [{"role": "user", "text": "找肺癌数据"}, {"role": "assistant", "text": "找到 3 个"}]})
    client.post("/api/replay/digest", json={"conv_id": C1, "key": K1})
    # 只标关系：图上没有任何节点标注
    res = client.post("/api/replay/annotate", json={
        "conv_id": C1, "key": K1, "pid": "e-0", "text": "这条因果站不住"})
    assert res.status_code == 200
    snap = client.post("/api/replay/snapshot", json={"conv_id": C1, "key": K1}).json()
    assert snap["count"] == 1
    assert [it["pid"] for it in snap["items"]] == ["e-0", "start", "o-0"]   # 关系 + 两端闭包
    assert [it["notes"] for it in snap["items"][1:]] == [[], []]
    assert snap["edges"][0]["eid"] == "e-0"

    payload = tmp_path / "replay-edge-only-snapshot.json"
    payload.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ, REPLAY_SNAPSHOT_PAYLOAD=str(payload))
    result = subprocess.run(
        [node, str(root / "tests" / "js" / "frontend_lifecycle_spec.mjs")], cwd=root,
        capture_output=True, text=True, encoding="utf-8", timeout=180, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REPLAY_SNAPSHOT_LAYOUT nodes=2 edges=1 count=1 items=3" in result.stdout, result.stdout[-2000:]


def test_key_mismatch_is_404(replay_root, fake_llm):
    client.post("/api/replay/backfill", json={
        "conv_id": C1, "key": K1, "turns": [{"role": "user", "text": "x"}]})
    assert client.get(f"/api/replay/status?conv_id={C1}&key=k-WRONG").status_code == 404
    assert client.get(f"/api/replay/points?conv_id={C1}&key=k-WRONG").status_code == 404
    assert client.post("/api/replay/annotate", json={
        "conv_id": C1, "key": "k-WRONG", "pid": "o-0", "text": "x"}).status_code == 404
    assert client.post("/api/replay/chat", json={
        "conv_id": C1, "key": "k-WRONG", "text": "x"}).status_code == 404


def test_annotate_unknown_point_404(replay_root, fake_llm):
    client.post("/api/replay/backfill", json={
        "conv_id": C1, "key": K1, "turns": [{"role": "user", "text": "x"}]})
    client.post("/api/replay/digest", json={"conv_id": C1, "key": K1})
    assert client.post("/api/replay/annotate", json={
        "conv_id": C1, "key": K1, "pid": "ghost", "text": "x"}).status_code == 404


def test_cross_origin_post_rejected(replay_root):
    res = client.post("/api/replay/annotate",
                      json={"conv_id": C1, "key": K1, "pid": "o-0", "text": "x"},
                      headers={"Origin": "http://evil.example.com"})
    assert res.status_code == 403


def test_digest_mock_skipped(replay_root, monkeypatch):
    # 不挂 fake_llm：mock 配置 → manual_digest 如实 skipped，不假装有图
    monkeypatch.setattr(webapp, "_replay_cfg_or_none",
                        lambda payload: types.SimpleNamespace(
                            mock_llm=True, enable_llm=False, api_key=None,
                            provider="mock", model="mock"))
    client.post("/api/replay/backfill", json={
        "conv_id": C1, "key": K1, "turns": [{"role": "user", "text": "x"}]})
    res = client.post("/api/replay/digest", json={"conv_id": C1, "key": K1})
    assert res.json() == {"ok": False, "skipped": "mock"}
