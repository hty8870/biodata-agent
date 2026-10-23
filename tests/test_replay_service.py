# -*- coding: utf-8 -*-
"""replay_service 编排单测：记 turn（含 key 闸与资格判定）、手动整理真链（monkeypatch LLM）、
角标/标注/快照/sidechat/补建。tmp_path 隔离；LLM 一律 monkeypatch，零网络。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from dataset_recommender.app import replay_service as SV  # noqa: E402
from dataset_recommender.app import session_log as SL  # noqa: E402

P, C, K = "u1", "cABC", "k-SECRET"


@pytest.fixture(autouse=True)
def _session_log_on(monkeypatch):
    """离线验证 harness 把 BIODATA_SESSION_LOG 钉成 off；本文件断言落痕/整理链路，显式打开。"""
    monkeypatch.setenv("BIODATA_SESSION_LOG", "on")


class _Cfg:
    """最小 LLMConfig 替身（字段与 dataclass 同名）。"""

    def __init__(self, *, mock=False, enable=True, key="sk-x", provider="deepseek", model="m-x"):
        self.mock_llm = mock
        self.enable_llm = enable
        self.api_key = key
        self.provider = provider
        self.model = model


class _Result:
    def __init__(self, text):
        self.text = text
        self.succeeded = text is not None


_DIGEST_JSON = (
    '{"motivation": {"id": "start", "title": "找肺癌数据", "detail": "d", "turn_refs": [1]},'
    '"outputs": [{"id": "o1", "title": "锁定 GSE131907", "detail": "d", "turn_refs": [1]}],'
    '"knowledge": [], "decisions": [],'
    '"edges": [{"from": "start", "to": "o1", "label": "驱动"}]}'
)


def _search_turn(root, cfg=None):
    SV.record_turn(root, P, C, K, "search",
                   {"query": "肺癌 单细胞", "total": 5, "status": "results", "top": ["A"]},
                   cfg if cfg is not None else _Cfg())


def setup_function():
    SV._reset_for_tests()
    SL._reset_for_tests()


def test_record_turn_writes_log_and_key(tmp_path):
    _search_turn(tmp_path, cfg=_Cfg(mock=True))  # mock：不自动整理但落痕
    assert len(SL.read_records(tmp_path, P, C)) == 1
    st = SV.status(tmp_path, P, C, K)
    assert st["n_turns"] == 1 and st["has_digest"] is False
    assert st["skip_reason"] == "mock"  # 跳过原因如实落库
    assert SV.status(tmp_path, P, C, "k-WRONG") is None  # key 不符 → None（端点 404）


def test_wrong_replay_key_cannot_append_or_schedule(tmp_path, monkeypatch):
    _search_turn(tmp_path, cfg=_Cfg(mock=True))
    before = SL.log_path(tmp_path, P, C).read_bytes()
    state_path = SV.replay_store.store_path(tmp_path, P, C)
    state_before = state_path.read_bytes()
    scheduled = []
    monkeypatch.setattr(SV, "schedule_draw", lambda *a: scheduled.append(a))
    SV.record_turn(tmp_path, P, C, "wrong", "chat", {"user": "injected"}, _Cfg())
    assert SL.log_path(tmp_path, P, C).read_bytes() == before
    assert state_path.read_bytes() == state_before
    assert not scheduled


def test_record_turn_serializes_read_modify_write_with_annotations(tmp_path, monkeypatch):
    _search_turn(tmp_path, cfg=_Cfg(mock=True))
    entered, release, annotation_started, annotation_done = (threading.Event() for _ in range(4))
    original = SV._draw_worthy
    def pause(cfg):
        entered.set()
        assert release.wait(3)
        return original(cfg)
    monkeypatch.setattr(SV, "_draw_worthy", pause)
    def annotate():
        annotation_started.set()
        SV.replay_store.append_thread(tmp_path, P, C, "session", "user", "saved note")
        annotation_done.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        record = pool.submit(_search_turn, tmp_path, _Cfg(mock=True))
        assert entered.wait(3)
        annotation = pool.submit(annotate)
        assert annotation_started.wait(3)
        try:
            assert not annotation_done.wait(0.1)
        finally:
            release.set()
        record.result(3)
        annotation.result(3)
    data = SV.replay_store.load(tmp_path, P, C)
    assert data["threads"]["session"][0]["content"] == "saved note"


def test_draw_worthy_table():
    assert SV._draw_worthy(None) == (False, "no_config")
    assert SV._draw_worthy(_Cfg(mock=True)) == (False, "mock")
    assert SV._draw_worthy(_Cfg(key="")) == (False, "no_key")
    assert SV._draw_worthy(_Cfg(enable=False)) == (False, "no_key")
    assert SV._draw_worthy(_Cfg(provider="trial")) == (False, "trial")
    assert SV._draw_worthy(_Cfg()) == (True, "")


def test_record_turn_schedules_background_draw(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(SV, "schedule_draw", lambda *a: called.append(a))
    _search_turn(tmp_path, cfg=_Cfg())
    assert len(called) == 1  # 有资格 → 调度后台整理


def test_record_turn_admits_draw_after_persisting_the_turn(tmp_path, monkeypatch):
    scheduled, admitted = [], []
    cfg = _Cfg()
    monkeypatch.setattr(SV, "schedule_draw", lambda *a: scheduled.append(a))

    def admit(config):
        assert len(SL.read_records(tmp_path, P, C)) == 1
        assert SV.status(tmp_path, P, C, K)["n_turns"] == 1
        assert scheduled == []
        admitted.append(config)

    SV.record_turn(tmp_path, P, C, K, "search", {"query": "synthetic"}, cfg, before_llm=admit)
    assert admitted == [cfg] and len(scheduled) == 1


def test_record_turn_denied_draw_preserves_the_turn_and_key(tmp_path, monkeypatch):
    scheduled = []
    monkeypatch.setattr(SV, "schedule_draw", lambda *a: scheduled.append(a))

    def deny(config):
        raise RuntimeError("synthetic quota refusal")

    SV.record_turn(tmp_path, P, C, K, "search", {"query": "synthetic"}, _Cfg(), before_llm=deny)
    assert scheduled == []
    assert len(SL.read_records(tmp_path, P, C)) == 1
    assert SV.status(tmp_path, P, C, K)["n_turns"] == 1
    assert SV.status(tmp_path, P, C, "wrong") is None


@pytest.mark.parametrize("cfg", [None, _Cfg(mock=True), _Cfg(key=""), _Cfg(provider="trial"), _Cfg(enable=False)])
def test_record_turn_nonworthy_draw_never_calls_admission(tmp_path, monkeypatch, cfg):
    admitted, scheduled = [], []
    monkeypatch.setattr(SV, "schedule_draw", lambda *a: scheduled.append(a))
    SV.record_turn(tmp_path, P, C, K, "search", {"query": "synthetic"}, cfg, before_llm=admitted.append)
    assert admitted == [] and scheduled == []
    assert len(SL.read_records(tmp_path, P, C)) == 1


def test_record_turn_without_records_does_not_admit_an_empty_draw(tmp_path, monkeypatch):
    monkeypatch.setenv("BIODATA_SESSION_LOG", "off")
    admitted, scheduled = [], []
    monkeypatch.setattr(SV, "schedule_draw", lambda *a: scheduled.append(a))
    SV.record_turn(tmp_path, P, C, K, "search", {"query": "synthetic"}, _Cfg(), before_llm=admitted.append)
    assert SL.read_records(tmp_path, P, C) == []
    assert admitted == [] and scheduled == []


def test_manual_digest_full_chain_with_fake_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(SV, "call_llm", lambda prompt, cfg: _Result(_DIGEST_JSON))
    _search_turn(tmp_path, cfg=_Cfg(mock=True))
    out = SV.manual_digest(tmp_path, P, C, K, _Cfg())
    assert out == {"ok": True, "has_digest": True}
    pts = SV.points(tmp_path, P, C, K)
    assert pts["digest"]["outputs"][0]["title"] == "锁定 GSE131907"
    assert pts["digest"]["motivation"]["pid"] == "start"
    assert pts["unseen"] == ["o-0"]  # 新成果 = 值得看
    assert pts["skip_reason"] == ""  # 成功整理清除跳过标记


def test_manual_digest_mock_and_trial_semantics(tmp_path, monkeypatch):
    monkeypatch.setattr(SV, "call_llm", lambda prompt, cfg: _Result(_DIGEST_JSON))
    _search_turn(tmp_path, cfg=_Cfg(provider="trial"))
    assert SV.status(tmp_path, P, C, K)["skip_reason"] == "trial"  # 自动整理跳过
    out = SV.manual_digest(tmp_path, P, C, K, _Cfg(provider="trial"))
    assert out["ok"] is True and out.get("has_digest") is True  # 手动不受 trial 限制
    assert SV.manual_digest(tmp_path, P, C, K, _Cfg(mock=True)) == {"ok": False, "skipped": "mock"}


def test_manual_digest_parse_failure_lands_parse_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(SV, "call_llm", lambda prompt, cfg: _Result("根本不是 JSON"))
    _search_turn(tmp_path, cfg=_Cfg(mock=True))
    out = SV.manual_digest(tmp_path, P, C, K, _Cfg())
    assert out["ok"] is True
    pts = SV.points(tmp_path, P, C, K)
    assert pts["digest"]["parse_failed"] is True  # 如实落失败档，不假装有图


def test_annotate_snapshot_seen_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(SV, "call_llm", lambda prompt, cfg: _Result(_DIGEST_JSON))
    _search_turn(tmp_path, cfg=_Cfg(mock=True))
    SV.manual_digest(tmp_path, P, C, K, _Cfg())
    assert SV.annotate(tmp_path, P, C, K, "o-0", "可靠吗") is True
    assert SV.annotate(tmp_path, P, C, K, "ghost", "x") is False  # 条目不存在
    assert SV.annotate(tmp_path, P, C, "k-WRONG", "o-0", "x") is None
    snap = SV.take_snapshot(tmp_path, P, C, K)
    assert snap["count"] == 1 and "可靠吗" in snap["text"]
    seen = SV.mark_seen(tmp_path, P, C, K)
    assert seen["ok"] is True and seen["marked"] == 1          # 旧调用（不带版本）＝核销当前全部
    assert SV.status(tmp_path, P, C, K)["unseen"] == 0


def test_chat_appends_session_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(SV, "call_llm", lambda prompt, cfg: _Result("这是对复盘的回答"))
    _search_turn(tmp_path, cfg=_Cfg(mock=True))
    out = SV.chat(tmp_path, P, C, K, "这段对话干了啥", _Cfg())
    assert out == {"ok": True, "reply": "这是对复盘的回答"}
    pts = SV.points(tmp_path, P, C, K)
    session = pts["threads"]["session"]
    assert [m["role"] for m in session] == ["user", "assistant"]
    assert SV.chat(tmp_path, P, C, "k-WRONG", "x", _Cfg()) is None
    out2 = SV.chat(tmp_path, P, C, K, "x", _Cfg(mock=True))
    assert out2["ok"] is False and "大模型" in out2["reply"]


def test_backfill_only_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(SV, "schedule_draw", lambda *a: None)
    turns = [{"role": "user", "text": "找肺癌数据"}, {"role": "assistant", "text": "找到 3 个"},
             {"role": "user", "text": ""}]  # 空文本跳过
    out = SV.backfill(tmp_path, P, C, K, turns, _Cfg())
    assert out == {"ok": True, "n": 2}
    recs = SL.read_records(tmp_path, P, C)
    assert [r["kind"] for r in recs] == ["backfill", "backfill"]
    out2 = SV.backfill(tmp_path, P, C, K, turns, _Cfg())
    assert out2 == {"ok": True, "skipped": "has_records"}  # 有记录后 fail-closed
    assert len(SL.read_records(tmp_path, P, C)) == 2
