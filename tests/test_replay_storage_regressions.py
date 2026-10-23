# -*- coding: utf-8 -*-
"""存储回归：断尾容错与 seq 真源、runtime 冻结守卫、复盘 key 绑定/损坏态、
手动整理 dirty 补跑、seen 与展示版本绑定、快照历史条目、sidechat 标注预算。

全部合成 tmp_path，零网络、零真实 userdata；store/session root 一律 tmp_path。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from dataset_recommender.app import runtime_paths as RP
from dataset_recommender.app import session_log as SL


@pytest.fixture(autouse=True)
def _session_log_on(monkeypatch):
    """离线验证 harness 把 BIODATA_SESSION_LOG 钉成 off 做隔离；本文件专测落痕/复盘链路，
    显式打开（需要验证开关语义的既有测试会自行 setenv off）。不放宽任何审计防线。"""
    monkeypatch.setenv("BIODATA_SESSION_LOG", "on")


# ---------------------------------------------------------------- session_log

def _records(root, principal="u1", conv="c1"):
    return SL.read_records(root, principal, conv)


def _truncated_tail() -> bytes:
    """未完成行 + 半个中文字符结尾（模拟断电/写失败把最后一个字符劈成半个字节）。"""
    return '{"seq": 2, "ts": 1, "kind": "chat", "payload": {"user": "断路"}}'.encode("utf-8")[:-4]


def test_read_tolerates_truncated_utf8_tail_and_keeps_history(tmp_path):
    """半个中文字符结尾（断电/写失败）只丢未完成的尾行；既有历史仍可读，坏文件不删。"""
    p = SL.log_path(tmp_path, "u1", "c1")
    p.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps({"seq": 1, "ts": 1, "kind": "search", "payload": {"query": "a"}},
                      ensure_ascii=False)
    p.write_bytes(good.encode("utf-8") + b"\n" + _truncated_tail())
    recs = _records(tmp_path)
    assert [r["seq"] for r in recs] == [1]
    assert p.read_bytes()                      # 不删整个坏文件/日志


def test_append_after_truncated_tail_resumes_and_keeps_old_bytes(tmp_path):
    """断尾后仍能追加（修前 strict 解码让初始化抛错、每次追加被吞），且不被粘进坏行。"""
    p = SL.log_path(tmp_path, "u1", "c1")
    p.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps({"seq": 1, "ts": 1, "kind": "search", "payload": {"query": "a"}},
                      ensure_ascii=False)
    p.write_bytes(good.encode("utf-8") + b"\n" + _truncated_tail())
    before = p.read_bytes()
    SL.append_turn(tmp_path, "u1", "c1", "chat", {"user": "新的一条"})
    after = p.read_bytes()
    assert after.startswith(before)            # 既有字节（含坏尾）原样保留
    recs = _records(tmp_path)
    assert [r["seq"] for r in recs] == [1, 2]  # seq 以已落数据为真源续编
    assert recs[1]["payload"]["user"] == "新的一条"


def test_append_after_half_written_json_line(tmp_path):
    """合法 UTF-8 但未换行的半截 JSON：新记录不得被拼进坏行而被一起丢掉。"""
    p = SL.log_path(tmp_path, "u1", "c1")
    p.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps({"seq": 1, "ts": 1, "kind": "search", "payload": {"query": "a"}},
                      ensure_ascii=False)
    p.write_bytes(good.encode("utf-8") + b"\n"
                  + b'{"seq": 2, "ts": 1, "kind": "chat", "paylo')
    SL.append_turn(tmp_path, "u1", "c1", "chat", {"user": "完整的新记录"})
    recs = _records(tmp_path)
    assert [r["seq"] for r in recs] == [1, 2]
    assert recs[1]["payload"]["user"] == "完整的新记录"


def test_failed_append_does_not_consume_seq(tmp_path, monkeypatch):
    """写盘失败不得先耗号；重启（清缓存）后 seq 仍唯一且递增（修前重启按行数恢复→重复 3）。"""
    real_redact = SL.redact
    SL.append_turn(tmp_path, "u1", "c1", "search", {"query": "a"})     # seq 1

    def boom(_line):
        raise RuntimeError("synthetic redact failure")

    monkeypatch.setattr(SL, "redact", boom)
    SL.append_turn(tmp_path, "u1", "c1", "search", {"query": "b"})     # 失败，不落盘
    monkeypatch.setattr(SL, "redact", real_redact)
    SL.append_turn(tmp_path, "u1", "c1", "search", {"query": "c"})     # seq 2

    SL._reset_for_tests()                                              # 模拟进程重启
    SL.append_turn(tmp_path, "u1", "c1", "search", {"query": "d"})     # seq 3
    seqs = [r["seq"] for r in _records(tmp_path)]
    assert seqs == [1, 2, 3] and len(seqs) == len(set(seqs))


# ---------------------------------------------------------------- runtime 冻结守卫

class _GuardError(Exception):
    """error_cls 约定 (code, message) 的最小替身。"""


def _make_dir_link(link: Path, target: Path) -> bool:
    """目录联接优先（无需管理员），失败退 symlink；都不行则 skip。"""
    target.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        out = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                             capture_output=True, text=True)
        if out.returncode == 0 and link.exists():
            return True
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return link.exists()


def test_runtime_guard_resolves_both_sides_for_symlinked_database(tmp_path, monkeypatch):
    """仓库 database 自身是目录链接时，指向其真实目录的运行时路径也必须被挡。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    real_db = tmp_path / "real-database"
    real_db.mkdir()
    link = repo / "database"
    if not _make_dir_link(link, real_db):
        pytest.skip("本机不允许创建目录链接")
    monkeypatch.setattr(RP, "_SOURCE_PROJECT_ROOT", repo)

    with pytest.raises(_GuardError):
        RP.assert_runtime_path(real_db / "runtime.json", _GuardError)
    with pytest.raises(_GuardError):
        RP.assert_runtime_path(link / "runtime.json", _GuardError)
    # 真正在仓库外的运行态路径仍放行（不误伤正常配置）
    outside = tmp_path / "outside" / "runtime.json"
    assert RP.assert_runtime_path(outside, _GuardError) == outside.resolve()


# ================================================================ Part B：复盘存储/服务

from fastapi.testclient import TestClient          # noqa: E402

from dataset_recommender.app import replay_service as SV   # noqa: E402
from dataset_recommender.app import replay_store as ST     # noqa: E402
from dataset_recommender.app import webapp                 # noqa: E402
from dataset_recommender.app.webapp import app             # noqa: E402

client = TestClient(app, base_url="http://127.0.0.1")

P, C, K = "u1", "cABC", "k-SECRET"
C1, K1 = "cTEST1-ab12", "k-TEST"

_DIGEST_JSON = (
    '{"motivation": {"id": "start", "title": "找肺癌数据", "detail": "d", "turn_refs": [1]},'
    '"outputs": [{"id": "o1", "title": "锁定 GSE131907", "detail": "d", "turn_refs": [1]}],'
    '"knowledge": [], "decisions": [],'
    '"edges": [{"from": "start", "to": "o1", "label": "驱动"}]}'
)
_DIGEST_JSON_V2 = (
    '{"motivation": {"id": "start", "title": "找肺癌数据", "detail": "d", "turn_refs": [1]},'
    '"outputs": [{"id": "o1", "title": "锁定 GSE131907", "detail": "d", "turn_refs": [1]},'
    '{"id": "o2", "title": "第二条成果", "detail": "d", "turn_refs": [2]}],'
    '"knowledge": [], "decisions": [],'
    '"edges": [{"from": "start", "to": "o1", "label": "驱动"}]}'
)


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


def _search_turn(root, cfg=None, principal=P, conv=C, key=K):
    SV.record_turn(root, principal, conv, key, "search",
                   {"query": "肺癌 单细胞", "total": 5, "status": "results", "top": ["A"]},
                   cfg if cfg is not None else _Cfg())


@pytest.fixture(autouse=True)
def _reset_draw_state():
    SV._reset_for_tests()
    SL._reset_for_tests()
    ST._reset_for_tests()
    yield
    SV._reset_for_tests()
    SL._reset_for_tests()
    ST._reset_for_tests()


@pytest.fixture
def replay_root(tmp_path, monkeypatch):
    """replay 端点的存储根重定向（这些端点不碰语料）。"""
    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    return tmp_path


# ---------------------------------------------------------------- backfill 配额闸

def test_backfill_charges_quota_only_when_a_draw_will_run(tmp_path, monkeypatch):
    monkeypatch.setattr(SV, "schedule_draw", lambda *a: None)
    calls = []

    def gate():
        calls.append(1)

    turns = [{"role": "user", "text": "找肺癌数据"}]
    assert SV.backfill(tmp_path, P, C, K, turns, _Cfg(), quota_gate=gate) == {"ok": True, "n": 1}
    assert calls == [1]
    # 已有记录 → 不再整理 → 不计费（不能为没有发生的 LLM 调用扣额度）
    assert SV.backfill(tmp_path, P, C, K, turns, _Cfg(), quota_gate=gate) == {"ok": True, "skipped": "has_records"}
    assert calls == [1]


def test_backfill_no_quota_charge_without_llm_eligibility(tmp_path, monkeypatch):
    monkeypatch.setattr(SV, "schedule_draw", lambda *a: None)
    calls = []
    turns = [{"role": "user", "text": "x"}]
    SV.backfill(tmp_path, P, C, K, turns, _Cfg(mock=True), quota_gate=lambda: calls.append(1))
    assert calls == []                     # mock 不产生真实调用，不该计费


def test_backfill_quota_exhausted_blocks_before_any_write(tmp_path, monkeypatch):
    class _Quota(Exception):
        pass

    def gate():
        raise _Quota("quota_exhausted")

    with pytest.raises(_Quota):
        SV.backfill(tmp_path, P, C, K, [{"role": "user", "text": "x"}], _Cfg(), quota_gate=gate)
    assert SL.read_records(tmp_path, P, C) == []                       # 痕迹未落（计费先于写入）
    data = ST.load(tmp_path, P, C)
    assert data["digest"] is None and data["threads"] == {}            # 也没有内容半成品


def test_backfill_429_leaves_no_key_and_allows_retry_with_another_key(tmp_path, monkeypatch):
    """新会话在配额闸被拒后不得绑定 key/占坑。

    旧顺序先 `_authorized(bind=True)`（立即 save key）再过 quota——429 也给新会话留下 key，
    换 key 重试被失败请求挡住。修后：过闸前只读，429 时 key 为空、无内容半成品；
    随后换 key 正常完成。
    """
    monkeypatch.setattr(SV, "schedule_draw", lambda *a: None)

    class _Quota(Exception):
        pass

    def gate():
        raise _Quota("quota_exhausted")

    turns = [{"role": "user", "text": "找肺癌数据"}]
    with pytest.raises(_Quota):
        SV.backfill(tmp_path, P, C, K, turns, _Cfg(), quota_gate=gate)
    data = ST.load(tmp_path, P, C)
    assert not str(data.get("key") or ""), "被拒请求不得绑定 key（占坑）"
    assert data["digest"] is None and data["threads"] == {}
    assert SL.read_records(tmp_path, P, C) == []
    # 换 key 合法重试：不被失败请求占坑，正常完成并绑定新 key
    assert SV.backfill(tmp_path, P, C, "another-key", turns, _Cfg()) == {"ok": True, "n": 1}
    assert str(ST.load(tmp_path, P, C).get("key") or "") == "another-key"


def test_backfill_route_wires_quota_gate(replay_root, monkeypatch):
    """HTTP 面：额度耗尽时 backfill 必须 429 且不落任何痕迹（修前该路由根本没闸）。"""
    from fastapi import HTTPException

    called = []

    def fake_gate(request, **kw):
        called.append(kw)
        raise HTTPException(status_code=429, detail="quota_exhausted")

    monkeypatch.setattr(webapp, "_gate_llm_quota", fake_gate)
    monkeypatch.setattr(webapp, "_replay_cfg_or_none", lambda payload: _Cfg())
    res = client.post("/api/replay/backfill", json={
        "conv_id": C1, "key": K1, "turns": [{"role": "user", "text": "x"}]})
    assert res.status_code == 429
    assert called and {"provider", "use_llm", "mock_llm", "api_key"} <= set(called[0])
    assert SL.read_records(replay_root, "anonymous", C1) == []


def test_backfill_route_skips_gate_when_no_draw(replay_root, monkeypatch):
    called = []
    monkeypatch.setattr(webapp, "_gate_llm_quota", lambda request, **kw: called.append(kw))
    monkeypatch.setattr(webapp, "_replay_cfg_or_none", lambda payload: _Cfg(mock=True))
    res = client.post("/api/replay/backfill", json={
        "conv_id": C1, "key": K1, "turns": [{"role": "user", "text": "x"}]})
    assert res.status_code == 200 and res.json() == {"ok": True, "n": 1}
    assert called == []


@pytest.mark.parametrize("mock", [False, True])
def test_backfill_persists_key_before_each_append(tmp_path, monkeypatch, mock):
    monkeypatch.setattr(SV, "schedule_draw", lambda *a: None)
    append = SL.append_turn
    observed = []

    def observe(root, principal, conv, *args):
        observed.append(ST.load(root, principal, conv)["key"])
        return append(root, principal, conv, *args)

    monkeypatch.setattr(SL, "append_turn", observe)
    turns = [{"role": "user", "text": "first"}, {"role": "assistant", "text": "second"}]
    assert SV.backfill(tmp_path, P, C, K, turns, _Cfg(mock=mock)) == {"ok": True, "n": 2}
    assert observed == [K, K]
    assert len(SL.read_records(tmp_path, P, C)) == 2


@pytest.mark.parametrize("mock", [False, True])
def test_backfill_final_save_fault_keeps_original_key_authorized(tmp_path, monkeypatch, mock):
    monkeypatch.setattr(SV, "schedule_draw", lambda *a: None)
    save = ST.save

    def fault_after_append(root, principal, conv, data):
        if SL.read_records(root, principal, conv):
            raise OSError("synthetic final save failure")
        return save(root, principal, conv, data)

    monkeypatch.setattr(ST, "save", fault_after_append)
    turns = [{"role": "user", "text": "preserved turn"}]
    with pytest.raises(OSError, match="final save failure"):
        SV.backfill(tmp_path, P, C, K, turns, _Cfg(mock=mock))
    assert len(SL.read_records(tmp_path, P, C)) == 1
    assert ST.load(tmp_path, P, C)["key"] == K
    assert SV.status(tmp_path, P, C, K)["n_turns"] == 1
    assert SV.status(tmp_path, P, C, "other-key") is None
    assert SV.backfill(tmp_path, P, C, K, turns, _Cfg(mock=mock)) == {
        "ok": True, "skipped": "has_records"}


def test_backfill_key_save_fault_never_appends_log(tmp_path, monkeypatch):
    monkeypatch.setattr(ST, "save", lambda *a: (_ for _ in ()).throw(OSError("key save failure")))
    with pytest.raises(OSError, match="key save failure"):
        SV.backfill(tmp_path, P, C, K, [{"role": "user", "text": "x"}], _Cfg())
    assert not ST.store_path(tmp_path, P, C).exists()
    assert not SL.log_path(tmp_path, P, C).exists()


@pytest.mark.parametrize("existing", [False, True])
def test_backfill_http_quota_refusal_preserves_store_key_and_log_bytes(replay_root, monkeypatch, existing):
    from dataset_recommender.app import accounts, llm_quota

    sessions = replay_root / "sessions.json"
    quota = replay_root / "quota.json"
    monkeypatch.setenv("BIODATA_SESSIONS_FILE", str(sessions))
    monkeypatch.setenv("BIODATA_LLM_QUOTA_FILE", str(quota))
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.setenv("BIODATA_LLM_DAILY_PER_USER", "1")
    monkeypatch.setenv("BIODATA_LLM_DAILY_GLOBAL", "100")
    monkeypatch.setenv("BIODATA_LLM_QUOTA_EXEMPT", "")
    monkeypatch.setenv("BIODATA_SESSION_STORE", "")
    cfg = _Cfg(provider="openai-compatible")
    monkeypatch.setattr(webapp, "_materialize_request_llm_config", lambda *args, **kw: cfg)
    monkeypatch.setattr(SV, "schedule_draw", lambda *a: pytest.fail("quota refusal must not schedule"))
    accounts._reset_state_for_tests()
    try:
        user = accounts.PublicUser("quota-id", "quota-user")
        http = TestClient(app, base_url="http://127.0.0.1")
        http.cookies.set(accounts.SESSION_COOKIE, accounts.create_session(user, sessions_path=sessions))
        llm_quota.check_and_increment(replay_root, user.username, trial=False,
                                     per_user_limit=1, global_limit=100)
        quota_before = quota.read_bytes()
        store_path = ST.store_path(replay_root, user.id, C1)
        log_path = SL.log_path(replay_root, user.id, C1)
        if existing:
            data = ST.load(replay_root, user.id, C1)
            data["key"], data["skip_reason"] = K1, "preserve"
            ST.save(replay_root, user.id, C1, data)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_bytes(b" \n")
        before = {p: p.read_bytes() if p.exists() else None for p in (store_path, log_path)}
        res = http.post("/api/replay/backfill", json={
            "conv_id": C1, "key": K1, "turns": [{"role": "user", "text": "x"}],
            "provider": "openai-compatible", "use_llm": True, "mock_llm": False})
        assert res.status_code == 429
        assert {p: p.read_bytes() if p.exists() else None for p in before} == before
        assert quota.read_bytes() == quota_before
        assert llm_quota.usage_snapshot(replay_root, user.username, trial=False) == 1
    finally:
        accounts._reset_state_for_tests()


# ---------------------------------------------------------------- key 绑定与失败态

def test_first_write_binds_key_and_others_are_rejected(tmp_path, monkeypatch):
    """新 conv 首写（chat）必须先绑定非空 key；之后他人任何 key 都拿不到。"""
    monkeypatch.setattr(SV, "call_llm", lambda p, c: _Result("回复"))
    out = SV.chat(tmp_path, P, C, K, "这是第一条", _Cfg())
    assert out["ok"] is True
    assert ST.load(tmp_path, P, C)["key"] == K
    assert SV.status(tmp_path, P, C, "k-ATTACKER") is None
    assert SV.points(tmp_path, P, C, "k-ATTACKER") is None
    assert SV.annotate(tmp_path, P, C, "k-ATTACKER", "o-0", "x") is None
    assert SV.take_snapshot(tmp_path, P, C, "k-ATTACKER") is None
    assert SV.chat(tmp_path, P, C, "k-ATTACKER", "x", _Cfg()) is None
    assert SV.status(tmp_path, P, C, K)["n_turns"] == 0        # 原 key 正常（chat 不落 session log）


def test_keyless_store_with_logs_is_not_claimable(tmp_path, monkeypatch):
    """已有痕迹但密钥状态缺失（旧版本遗留）→ 任何 key 不得读取/认领/整理。"""
    SL.append_turn(tmp_path, P, C, "chat", {"user": "受害者的痕迹", "reply": "x"})
    assert SV.status(tmp_path, P, C, "k-ATTACKER") is None
    assert SV.points(tmp_path, P, C, "k-ATTACKER") is None
    assert SV.chat(tmp_path, P, C, "k-ATTACKER", "x", _Cfg()) is None
    assert SV.manual_digest(tmp_path, P, C, "k-ATTACKER", _Cfg()) is None
    assert SV.backfill(tmp_path, P, C, "k-ATTACKER", [{"role": "user", "text": "x"}], _Cfg()) is None
    assert len(SL.read_records(tmp_path, P, C)) == 1           # 痕迹保原样，不删不读


def test_corrupt_key_state_with_logs_is_not_claimable(tmp_path):
    """密钥 JSON 损坏同样不得当新会话。"""
    SL.append_turn(tmp_path, P, C, "chat", {"user": "受害者的痕迹", "reply": "x"})
    p = ST.store_path(tmp_path, P, C)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{这不是 json", encoding="utf-8")
    assert SV.status(tmp_path, P, C, "k-ATTACKER") is None
    assert SV.chat(tmp_path, P, C, "k-ATTACKER", "x", _Cfg()) is None


def test_record_turn_persists_key_before_log(tmp_path, monkeypatch):
    """密钥先于日志落库：日志写失败也不留下「有痕迹无 key」的可认领态。"""

    def boom(*_a, **_k):
        raise RuntimeError("synthetic log failure")

    monkeypatch.setattr(SL, "append_turn", boom)
    SV.record_turn(tmp_path, P, C, K, "search", {"query": "x"}, _Cfg(mock=True))
    assert ST.load(tmp_path, P, C)["key"] == K
    assert SV.status(tmp_path, P, C, K) is not None


# ---------------------------------------------------------------- 手动整理补跑 dirty 轮次

def test_manual_digest_drains_turn_arriving_mid_flight(tmp_path, monkeypatch):
    """整理途中落痕的新轮次必须补跑（既有单飞 + dirty 机制），digest 覆盖到最新 seq。"""
    _search_turn(tmp_path, cfg=_Cfg(mock=True))                # seq 1，绑定 key
    prompts = []

    def fake_llm(prompt, cfg):
        prompts.append(prompt)
        if len(prompts) == 1:
            # 第一轮模型调用期间又落一轮；worthy cfg → schedule_draw 因 running 只置 dirty
            SV.record_turn(tmp_path, P, C, K, "chat", {"user": "新问题", "reply": "新回答"}, _Cfg())
        return _Result(_DIGEST_JSON)

    monkeypatch.setattr(SV, "call_llm", fake_llm)
    out = SV.manual_digest(tmp_path, P, C, K, _Cfg())
    assert out["ok"] is True and out["has_digest"] is True
    pts = SV.points(tmp_path, P, C, K)
    assert pts["digest"]["to"] == 2                            # 新轮次被补整理，而不是停在 1
    assert len(prompts) == 2
    assert SV.drawing(P, C) is False


# ---------------------------------------------------------------- seen 与展示版本绑定

def test_seen_binds_to_observed_version_and_keeps_new_points_unseen(tmp_path, monkeypatch):
    monkeypatch.setattr(SV, "call_llm", lambda p, c: _Result(_DIGEST_JSON))
    _search_turn(tmp_path, cfg=_Cfg(mock=True))
    SV.manual_digest(tmp_path, P, C, K, _Cfg())                # digest v1
    pts = SV.points(tmp_path, P, C, K)
    v1 = pts["digest_version"]
    assert isinstance(v1, int) and v1 >= 1 and pts["pids"] == ["o-0"]

    new_digest = json.loads(_DIGEST_JSON)
    new_digest["outputs"].append({"id": "o2", "title": "第二条成果", "detail": "d", "turn_refs": [2]})
    ST.save_digest(tmp_path, P, C, new_digest, from_seq=1, to_seq=2, model="m")   # 版本 2 落地
    pts2 = SV.points(tmp_path, P, C, K)
    assert pts2["digest_version"] == v1 + 1
    new_pid = [x for x in pts2["pids"] if x not in pts["pids"]][0]

    out = SV.mark_seen(tmp_path, P, C, K, observed_version=v1)  # 用户看到的还是 v1
    assert out["stale"] is True and out["marked"] == 0
    pts3 = SV.points(tmp_path, P, C, K)
    assert new_pid in pts3["unseen"]                            # 新成果不被吞
    # 用当前版本核销 → 全量核销；再验旧调用（不带版本）语义不变
    out2 = SV.mark_seen(tmp_path, P, C, K, observed_version=pts3["digest_version"])
    assert out2["stale"] is False and SV.points(tmp_path, P, C, K)["unseen"] == []


def test_seen_legacy_call_without_version_still_marks_all(tmp_path, monkeypatch):
    monkeypatch.setattr(SV, "call_llm", lambda p, c: _Result(_DIGEST_JSON))
    _search_turn(tmp_path, cfg=_Cfg(mock=True))
    SV.manual_digest(tmp_path, P, C, K, _Cfg())
    assert SV.mark_seen(tmp_path, P, C, K)["marked"] == 1      # 旧调用兼容：核销当前全部
    assert SV.points(tmp_path, P, C, K)["unseen"] == []


def test_seen_endpoint_accepts_observed_version(replay_root, monkeypatch):
    """HTTP additive：seen 接受 observed_version；旧 body（不带该字段）仍然可用。"""
    monkeypatch.setattr(SV, "schedule_draw", lambda *a: None)   # 版本推进由本测试显式触发
    responses = [_DIGEST_JSON, _DIGEST_JSON_V2]
    monkeypatch.setattr(SV, "call_llm", lambda p, c: _Result(responses.pop(0)))
    monkeypatch.setattr(webapp, "_replay_cfg_or_none", lambda payload: _Cfg())
    assert client.post("/api/replay/backfill", json={
        "conv_id": C1, "key": K1, "turns": [{"role": "user", "text": "x"}]}).status_code == 200
    assert client.post("/api/replay/digest", json={"conv_id": C1, "key": K1}).json()["has_digest"] is True
    pts = client.get(f"/api/replay/points?conv_id={C1}&key={K1}").json()
    v1, pids1 = pts["digest_version"], pts["pids"]
    assert isinstance(v1, int) and pids1 == ["o-0"]

    assert client.post("/api/replay/digest", json={"conv_id": C1, "key": K1}).status_code == 200
    pts2 = client.get(f"/api/replay/points?conv_id={C1}&key={K1}").json()
    assert pts2["digest_version"] == v1 + 1
    new_pid = [x for x in pts2["pids"] if x not in pids1][0]

    res = client.post("/api/replay/seen",
                      json={"conv_id": C1, "key": K1, "observed_version": v1})
    assert res.status_code == 200 and res.json()["stale"] is True
    pts3 = client.get(f"/api/replay/points?conv_id={C1}&key={K1}").json()
    assert new_pid in pts3["unseen"]
    assert client.post("/api/replay/seen", json={"conv_id": C1, "key": K1}).status_code == 200
    assert client.get(f"/api/replay/points?conv_id={C1}&key={K1}").json()["unseen"] == []


# ---------------------------------------------------------------- 快照历史条目

def test_snapshot_carries_point_payload_and_edges(tmp_path, monkeypatch):
    monkeypatch.setattr(SV, "call_llm", lambda p, c: _Result(_DIGEST_JSON))
    _search_turn(tmp_path, cfg=_Cfg(mock=True))
    SV.manual_digest(tmp_path, P, C, K, _Cfg())
    assert SV.annotate(tmp_path, P, C, K, "o-0", "标注一") is True
    assert SV.annotate(tmp_path, P, C, K, "start", "动机标注") is True
    snap = SV.take_snapshot(tmp_path, P, C, K)
    by_pid = {it["pid"]: it for it in snap["items"]}
    assert by_pid["o-0"]["kind"] == "outputs"
    assert by_pid["o-0"]["point"]["title"] == "锁定 GSE131907"   # 快照自带条目内容
    assert by_pid["start"]["kind"] == "motivation"
    assert {e["from"] for e in snap["edges"]} == {"start"}       # 捕获时的关系
    assert {e["to"] for e in snap["edges"]} == {"o-0"}

    # 重整理删掉 o-0：快照仍自带旧条目内容，且能继续追加标注（找回带标注的图）
    changed = {"motivation": {"id": "start", "title": "新的动机", "detail": "d", "turn_refs": [3]},
               "outputs": [], "knowledge": [], "decisions": [], "edges": []}
    ST.save_digest(tmp_path, P, C, changed, from_seq=1, to_seq=3, model="m")
    snap2 = ST.annotate_snapshot(tmp_path, P, C, snap["snap_id"], "o-0", "继续编辑")
    item = {it["pid"]: it for it in snap2["items"]}["o-0"]
    assert item["point"]["title"] == "锁定 GSE131907"
    assert "继续编辑" in item["notes"]


# ---------------------------------------------------------------- sidechat 标注预算

def test_chat_annotation_budget_is_bounded_and_disclosed(tmp_path, monkeypatch):
    _search_turn(tmp_path, cfg=_Cfg(mock=True))                 # 先绑定 key（首写语义）
    ST.save_digest(tmp_path, P, C, json.loads(_DIGEST_JSON), from_seq=1, to_seq=2, model="m")
    for _ in range(40):
        ST.append_thread(tmp_path, P, C, "o-0", "user", "标" * 2000)
    before = ST.load(tmp_path, P, C)["threads"]["o-0"]
    captured = {}

    def fake_llm(prompt, cfg):
        captured["prompt"] = prompt
        return _Result("回复")

    monkeypatch.setattr(SV, "call_llm", fake_llm)
    out = SV.chat(tmp_path, P, C, K, "短问题", _Cfg())
    assert out["ok"] is True
    prompt = captured["prompt"]
    assert len(prompt) <= SV._CHAT_INPUT_BUDGET + 300           # 总长受限
    assert "省略" in prompt                                     # 如实披露截断
    assert "标" * 50 in prompt                                  # 最新标注内容仍在
    assert ST.load(tmp_path, P, C)["threads"]["o-0"] == before  # 磁盘标注一字不动


def test_two_process_digest_coverage_cannot_regress_or_mutate_newer_state(tmp_path):
    import time
    root, old_ready, new_done, release = tmp_path, tmp_path / 'old-ready', tmp_path / 'new-done', tmp_path / 'old-release'
    data = ST._empty('coverage-race')
    data['key'] = 'synthetic-legacy-key'
    ST.save(root, 'coverage-owner', 'coverage-race', data)
    code = '''import sys,json,time
from pathlib import Path
from dataset_recommender.app import replay_store as s
root,ready,done,release=map(Path,sys.argv[1:5]);lane=sys.argv[5]
seq=1 if lane=='old' else 2
if lane=='old':
    ready.touch()
    while not release.exists():time.sleep(.01)
digest={'outputs':[{'title':lane,'detail':lane+' content','turn_refs':[seq]}],'knowledge':[],'decisions':[],'edges':[]}
result=s.save_digest(root,'coverage-owner','coverage-race',digest,from_seq=1,to_seq=seq,model='synthetic-model')
if lane=='new':done.touch()
print(json.dumps({'last_seq':result['last_seq'],'version':result['digest_version'],'title':result['digest']['outputs'][0]['title'],'input_mutated':'pid' in digest['outputs'][0]}))
'''
    def launch(lane):
        return subprocess.Popen([sys.executable, '-B', '-c', code, str(root), str(old_ready), str(new_done), str(release), lane], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def wait_file(path, process):
        deadline = time.monotonic() + 8
        while not path.exists() and time.monotonic() < deadline:
            assert process.poll() is None, process.communicate()[1].decode('utf-8', 'replace')
            time.sleep(.01)
        assert path.exists()
    old = launch('old')
    new = None
    try:
        wait_file(old_ready, old)
        new = launch('new')
        wait_file(new_done, new)
        output, error = new.communicate(timeout=8)
        assert new.returncode == 0, error.decode('utf-8', 'replace')
        newer = json.loads(output)
        assert newer['last_seq'] == 2 and newer['version'] == 1 and newer['title'] == 'new'
        ST.append_thread(root, 'coverage-owner', 'coverage-race', 'o-0', 'user', 'keep annotation')
        current = ST.load(root, 'coverage-owner', 'coverage-race')
        current['skip_reason'] = 'retain-current-metadata'
        ST.save(root, 'coverage-owner', 'coverage-race', current)
        path = ST.store_path(root, 'coverage-owner', 'coverage-race')
        before_bytes = path.read_bytes()
        before_state = ST.load(root, 'coverage-owner', 'coverage-race')
        before_unseen = ST.unseen(before_state)
        release.touch()
        output, error = old.communicate(timeout=8)
        assert old.returncode == 0, error.decode('utf-8', 'replace')
        late = json.loads(output)
        assert late['last_seq'] == 2 and late['version'] == 1 and late['title'] == 'new'
        assert late['input_mutated'] is False
        assert path.read_bytes() == before_bytes
        assert ST.load(root, 'coverage-owner', 'coverage-race') == before_state
        assert ST.unseen(ST.load(root, 'coverage-owner', 'coverage-race')) == before_unseen
        same_range = ST.save_digest(root, 'coverage-owner', 'coverage-race', {
            'outputs': [{'title': 'manual-new', 'detail': 'updated same coverage', 'turn_refs': [2]}],
            'knowledge': [], 'decisions': [], 'edges': []}, from_seq=1, to_seq=2, model='synthetic-model')
        assert same_range['last_seq'] == 2 and same_range['digest_version'] == 2
        assert same_range['digest']['outputs'][0]['title'] == 'manual-new'
        assert same_range['key'] == 'synthetic-legacy-key'
    finally:
        for process in (old, new):
            if process is not None and process.poll() is None:
                process.terminate(); process.wait(timeout=5)
