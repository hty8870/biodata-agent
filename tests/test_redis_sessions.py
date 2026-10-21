# -*- coding: utf-8 -*-
"""Redis 会话后端测试（`accounts` 的 opt-in 后端，用 fakeredis 注入，禁真网络）。

覆盖两类保证：
1. **默认形态不依赖 Redis**——env 未设时走内存 + JSON 摘要快照，且绝不触碰 Redis；
2. **Redis 后端语义与文件后端逐条对齐**——往返、TTL、未知/过期/畸形条目、fail-open 与
   fail-closed 边界、env 笔误 fail-closed、测试复位清理单例。
"""
from __future__ import annotations

import json
import asyncio
import threading
import sys
import time
from pathlib import Path

import fakeredis
import pytest
import redis

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.app import accounts as A  # noqa: E402
from dataset_recommender.app import redis_store  # noqa: E402

_DAY = 24 * 3600
_KEY = "biodata:sess:{}"


@pytest.fixture(autouse=True)
def _isolate_accounts_state():
    """每个测试前后复位 accounts 与 redis_store 的单例（跨测试不串状态）。"""
    A._reset_state_for_tests()
    yield
    A._reset_state_for_tests()


@pytest.fixture()
def fake_redis(monkeypatch):
    """把共享建连接缝换成 fakeredis（不连真 Redis）。"""
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(redis_store, "_build_client", lambda: client)
    return client


@pytest.fixture()
def redis_mode(monkeypatch):
    monkeypatch.setenv("BIODATA_SESSION_STORE", "redis")


def test_default_env_uses_file_backend_and_never_touches_redis(tmp_path, monkeypatch):
    """env 未设 → 默认内存 + JSON 快照路径；Redis 建连绝不被触发。"""
    monkeypatch.delenv("BIODATA_SESSION_STORE", raising=False)
    monkeypatch.delenv("BIODATA_REDIS_URL", raising=False)

    def _must_not_build():
        raise AssertionError("默认形态不应建 Redis 连接")

    monkeypatch.setattr(redis_store, "_build_client", _must_not_build)

    user = A.PublicUser(id="u-default", username="alice")
    sessions = tmp_path / "sessions.json"
    token = A.create_session(user, sessions_path=sessions)
    assert A.resolve_session(token, sessions_path=sessions) == user
    assert sessions.exists()   # 落盘快照仍在（默认路径逐字节不变）


def test_redis_roundtrip_persists_json_and_ttl(fake_redis, redis_mode):
    user = A.PublicUser(id="u2", username="bob")
    token = A.create_session(user)
    assert A.resolve_session(token) == user

    from dataset_recommender.app.redis_sessions import RedisSessionStore
    key = RedisSessionStore._key(token)
    assert fake_redis.exists(key) == 1
    ttl = fake_redis.ttl(key)
    assert 0 < ttl <= 30 * _DAY
    payload = json.loads(fake_redis.get(key))
    assert payload["user_id"] == "u2"
    assert payload["username"] == "bob"
    assert payload["expires_at"] > time.time()


def test_unknown_token_and_destroy(fake_redis, redis_mode):
    assert A.resolve_session("never-issued") is None

    user = A.PublicUser(id="u3", username="carol")
    token = A.create_session(user)
    assert A.resolve_session(token) == user

    A.destroy_session(token)
    assert A.resolve_session(token) is None
    from dataset_recommender.app.redis_sessions import RedisSessionStore
    assert fake_redis.exists(_KEY.format(token)) == 0
    assert fake_redis.exists(RedisSessionStore._key(token)) == 0


def test_expired_entry_is_deleted(fake_redis, redis_mode):
    token = "expired-token"
    key = _KEY.format(token)
    fake_redis.set(key, json.dumps({"user_id": "u4", "username": "dave", "expires_at": time.time() - 10}))
    assert A.resolve_session(token) is None
    assert fake_redis.exists(key) == 0


def test_malformed_entry_is_deleted(fake_redis, redis_mode):
    token = "malformed-token"
    key = _KEY.format(token)

    fake_redis.set(key, "{not json at all")
    assert A.resolve_session(token) is None
    assert fake_redis.exists(key) == 0

    fake_redis.set(key, json.dumps({"username": "missing-user-id"}))   # 结构不符同样丢弃
    assert A.resolve_session(token) is None
    assert fake_redis.exists(key) == 0


class _FailingClient:
    """任何命令都抛 redis.RedisError 的假客户端（模拟 Redis 抖动）。"""

    def set(self, *args, **kwargs):
        raise redis.RedisError("boom")

    def get(self, *args, **kwargs):
        raise redis.RedisError("boom")

    def delete(self, *args, **kwargs):
        raise redis.RedisError("boom")


def test_command_failure_create_fails_closed_resolve_fails_open(monkeypatch, redis_mode):
    monkeypatch.setattr(redis_store, "_build_client", lambda: _FailingClient())

    with pytest.raises(A.AccountError) as excinfo:
        A.create_session(A.PublicUser(id="u6", username="erin"))
    assert excinfo.value.code == "session_store_unavailable"

    assert A.resolve_session("any-token") is None   # fail-open：登录态丢了就重新登录
    with pytest.raises(A.AccountError, match="会话撤销暂时失败"):
        A.destroy_session("any-token")


def test_missing_redis_package_semantics(monkeypatch, redis_mode):
    """缺包时创建/撤销均如实报错；resolve 仍按未登录处理。"""
    def _missing():
        raise redis_store.RedisUnavailable("redis_package_missing", "未安装 redis 依赖包。")

    monkeypatch.setattr(redis_store, "_build_client", _missing)

    with pytest.raises(A.AccountError) as excinfo:
        A.create_session(A.PublicUser(id="u7", username="frank"))
    assert excinfo.value.code == "redis_package_missing"

    assert A.resolve_session("any-token") is None
    with pytest.raises(A.AccountError) as excinfo:
        A.destroy_session("any-token")
    assert excinfo.value.code == "redis_package_missing"


def test_logout_delete_failure_is_503_and_keeps_cookie_for_retry(fake_redis, redis_mode, monkeypatch):
    from fastapi.testclient import TestClient
    from dataset_recommender.app.webapp import app
    token = A.create_session(A.PublicUser("test-account", "alice"))
    client = TestClient(app, base_url="http://127.0.0.1")
    client.cookies.set(A.SESSION_COOKIE, token)
    def fail(*a, **kw):
        raise redis.ConnectionError("synthetic failure")
    with monkeypatch.context() as m:
        m.setattr(fake_redis, "delete", fail)
        response = client.post("/api/account/logout")
        assert response.status_code == 503
        assert "set-cookie" not in response.headers
        assert A.resolve_session(token) is not None
    assert client.post("/api/account/logout").status_code == 200
    assert A.resolve_session(token) is None


def test_slow_session_resolution_does_not_block_async_middleware(monkeypatch, redis_mode):
    from starlette.requests import Request
    from dataset_recommender.app import webapp
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    def slow(*a, **kw):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(3), "事件循环应能在同步查询等待期间继续调度"
        return None
    monkeypatch.setattr(A, "resolve_session", slow)
    monkeypatch.setattr(webapp, "_account_gate_required", lambda: False)
    request = Request({"type": "http", "method": "GET", "path": "/api/test",
                       "headers": [], "query_string": b"", "scheme": "http"})
    async def exercise():
        async def next_call(req):
            return "ok"
        async def progress():
            for _ in range(300):
                if entered.is_set():
                    release.set()
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("会话查询未启动")
        results = await asyncio.gather(webapp._account_gate(request, next_call), progress())
        assert results[0] == "ok"
    asyncio.run(exercise())


def test_typo_session_store_env_fails_closed(monkeypatch):
    monkeypatch.setenv("BIODATA_SESSION_STORE", "rediss")   # 拼错：不许静默降级到内存后端
    with pytest.raises(A.AccountError) as excinfo:
        A.create_session(A.PublicUser(id="u8", username="gina"))
    assert excinfo.value.code == "bad_config"


def test_client_factory_injection_seam():
    """构造参数 `client_factory` 直接注入客户端（不经共享单例）——MQ 后端同款接缝。"""
    from dataset_recommender.app import redis_sessions

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    store = redis_sessions.RedisSessionStore(
        ttl_seconds=60, token_bytes=8, client_factory=lambda: client,
    )
    user = A.PublicUser(id="u-factory", username="frank")
    token = store.create(user)
    assert store.resolve(token)["user_id"] == "u-factory"
    store.destroy(token)
    assert store.resolve(token) is None


def test_reset_state_clears_client_singleton(monkeypatch, redis_mode):
    builds = []

    def _factory():
        builds.append(1)
        return fakeredis.FakeStrictRedis(decode_responses=True)

    monkeypatch.setattr(redis_store, "_build_client", _factory)

    user = A.PublicUser(id="u9", username="henry")
    A.create_session(user)
    A.create_session(user)
    assert len(builds) == 1   # 单例复用：两次写入只建一次连接

    A._reset_state_for_tests()
    A.create_session(user)
    assert len(builds) == 2   # 复位后重新建连（测试隔离生效）


def test_redis_persists_only_token_digest_and_digest_cannot_authenticate(fake_redis, redis_mode):
    import hashlib
    token = A.create_session(A.PublicUser("synthetic-user", "alice"))
    digest = hashlib.sha256(token.encode()).hexdigest()
    keys = list(fake_redis.scan_iter("biodata:sess:*"))
    assert keys == ["biodata:sess:sha256:" + digest]
    assert token not in json.dumps(keys)
    assert A.resolve_session(digest) is None
    assert A.resolve_session("sha256:" + digest) is None
    A.destroy_session("sha256:" + digest)
    assert A.resolve_session(token) == A.PublicUser("synthetic-user", "alice")


def test_redis_legacy_session_migrates_without_extending_expiry(fake_redis, redis_mode):
    import hashlib
    token = "synthetic-legacy-token"
    legacy_key = _KEY.format(token)
    expires = time.time() + 90
    fake_redis.set(legacy_key, json.dumps({"user_id": "synthetic-user", "username": "alice",
                                         "expires_at": expires}), ex=90)
    assert A.resolve_session(token) == A.PublicUser("synthetic-user", "alice")
    digest_key = "biodata:sess:sha256:" + hashlib.sha256(token.encode()).hexdigest()
    assert fake_redis.exists(legacy_key) == 0
    assert 0 < fake_redis.ttl(digest_key) <= 90
    assert json.loads(fake_redis.get(digest_key))["expires_at"] == expires
    A.destroy_session(token)
    assert A.resolve_session(token) is None


def test_redis_legacy_migration_preserves_shorter_redis_ttl(fake_redis, redis_mode):
    from dataset_recommender.app.redis_sessions import RedisSessionStore
    token = "synthetic-short-ttl"
    fake_redis.set(_KEY.format(token), json.dumps({"user_id": "synthetic-user", "username": "alice",
        "expires_at": time.time() + 90}), px=1500)
    before = fake_redis.pttl(_KEY.format(token))
    assert A.resolve_session(token) == A.PublicUser("synthetic-user", "alice")
    assert 0 < fake_redis.pttl(RedisSessionStore._key(token)) <= before


@pytest.mark.parametrize("concurrent_action", ["migrate", "revoke"])
def test_redis_legacy_migration_races_do_not_lose_or_restore_session(
        fake_redis, redis_mode, monkeypatch, concurrent_action):
    from dataset_recommender.app.redis_sessions import RedisSessionStore
    token = "synthetic-racing-legacy"
    user = A.PublicUser("synthetic-user", "alice")
    fake_redis.set(_KEY.format(token), json.dumps({"user_id": user.id, "username": user.username,
        "expires_at": time.time() + 90}), ex=90)
    pipeline_factory = fake_redis.pipeline
    injected = []

    def pipeline(*args, **kwargs):
        pipe = pipeline_factory(*args, **kwargs)
        execute = pipe.execute

        def interleave():
            if not injected:
                injected.append(True)
                if concurrent_action == "migrate":
                    assert A.resolve_session(token) == user
                else:
                    A.destroy_session(token)
            return execute()

        pipe.execute = interleave
        return pipe

    monkeypatch.setattr(fake_redis, "pipeline", pipeline)
    assert A.resolve_session(token) == (user if concurrent_action == "migrate" else None)
    assert fake_redis.exists(_KEY.format(token)) == 0
    assert fake_redis.exists(RedisSessionStore._key(token)) == (1 if concurrent_action == "migrate" else 0)
