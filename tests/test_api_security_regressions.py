"""Request admission and session storage regressions with synthetic identities."""
from __future__ import annotations

import hashlib
import json
import re
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.agent import turn
from dataset_recommender.app import accounts, llm_quota, runtime_paths, webapp
from dataset_recommender.app.request_validation import (
    MAX_QUERY_CHARS, ParamValidationError, validate_iso_date,
)
from dataset_recommender.llm import llm_client
from dataset_recommender.retrieval import rerank


@pytest.fixture
def client(monkeypatch, tmp_path):
    accounts._reset_state_for_tests()
    webapp._rate_buckets.clear()
    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(webapp, "CONFIG_ROOT", tmp_path)
    monkeypatch.setenv("BIODATA_ACCOUNTS_FILE", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("BIODATA_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("BIODATA_LLM_QUOTA_FILE", str(tmp_path / "quota.json"))
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.setenv("BIODATA_LLM_DAILY_PER_USER", "1")
    monkeypatch.setenv("BIODATA_LLM_DAILY_GLOBAL", "100")
    monkeypatch.setenv("BIODATA_TRIAL_DAILY_PER_USER", "1")
    monkeypatch.setenv("BIODATA_TRIAL_DAILY_GLOBAL", "100")
    monkeypatch.setenv("BIODATA_LLM_QUOTA_EXEMPT", "")
    monkeypatch.setenv("BIODATA_SESSION_STORE", "")
    monkeypatch.setenv("LLM_PROVIDER", "openai-compatible")
    monkeypatch.setenv("LLM_API_KEY", "synthetic-server-key")
    monkeypatch.setenv("BIODATA_TRIAL_API_KEY", "synthetic-trial-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.example/v1")
    token = accounts.create_session(
        accounts.PublicUser("synthetic-alice", "alice"),
        sessions_path=tmp_path / "sessions.json")
    with TestClient(webapp.app, base_url="http://127.0.0.1") as instance:
        instance.cookies.set(accounts.SESSION_COOKIE, token)
        yield instance
    accounts._reset_state_for_tests()
    webapp._rate_buckets.clear()


@pytest.mark.parametrize("mock_flag", [False, True])
def test_rerank_without_polish_is_charged_and_stops_before_second_call(client, monkeypatch, mock_flag):
    calls = []

    def fake_call(prompt, config, *args, **kwargs):
        calls.append(config)
        return llm_client.LLMResult(text="[1,2]", attempted=True, succeeded=True,
                                    response_used=True, provider="trial", model="synthetic")

    monkeypatch.setattr(rerank, "call_llm", fake_call)
    payload = {"query": "human lung single cell", "provider": "trial", "use_llm": False,
               "polish": False, "mock_llm": mock_flag, "rerank": "llm", "top_k": 2}
    first = client.post("/api/recommend", json=payload)
    after_first = len(calls)
    second = client.post("/api/recommend", json=payload)
    assert (first.status_code, second.status_code) == (200, 429)
    assert after_first > 0 and len(calls) == after_first
    assert all(cfg.enable_llm and not cfg.mock_llm for cfg in calls)
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=True) == 1


@pytest.mark.parametrize("provider", ["trial", "openai-compatible"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("limit_kind", ["user", "global"])
def test_diagnose_admits_once_then_blocks_both_probes(client, monkeypatch, provider, enabled, limit_kind):
    calls = []
    monkeypatch.setattr(webapp, "healthcheck", lambda cfg: calls.append("health") or {})
    monkeypatch.setattr(webapp, "diagnose_network", lambda cfg: calls.append("network") or {})
    if limit_kind == "global":
        prefix = "BIODATA_TRIAL" if provider == "trial" else "BIODATA_LLM"
        monkeypatch.setenv(prefix + "_DAILY_PER_USER", "100")
        monkeypatch.setenv(prefix + "_DAILY_GLOBAL", "1")
    payload = {"provider": provider, "use_llm": enabled}
    first = client.post("/api/diagnose", json=payload)
    second = client.post("/api/diagnose", json=payload)
    assert (first.status_code, second.status_code) == (200, 429)
    assert calls == ["health", "network"]
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=provider == "trial") == 1


@pytest.mark.parametrize("payload", [
    {"provider": "mock", "use_llm": True},
    {"provider": "openai-compatible", "api_key": "synthetic-byok", "use_llm": True},
])
def test_diagnose_byok_and_mock_do_not_consume_server_quota(client, monkeypatch, payload):
    llm_quota.check_and_increment(webapp.PROJECT_ROOT, "alice", trial=False,
                                  per_user_limit=1, global_limit=100)
    monkeypatch.setattr(webapp, "healthcheck", lambda cfg: {})
    monkeypatch.setattr(webapp, "diagnose_network", lambda cfg: {})
    assert client.post("/api/diagnose", json=payload).status_code == 200
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=False) == 1


def _changing_resolver(monkeypatch, later):
    calls = []
    user = accounts.PublicUser("synthetic-alice", "alice")

    def resolve(token, **kwargs):
        calls.append(token)
        if len(calls) == 1:
            return user
        if later == "error":
            raise RuntimeError("synthetic session failure")
        return accounts.PublicUser("synthetic-bob", "bob") if later == "other" else None

    monkeypatch.setattr(accounts, "resolve_session", resolve)
    return calls, user


@pytest.mark.parametrize("later", ["expired", "error", "other"])
def test_workspace_keeps_the_middleware_identity(client, monkeypatch, later):
    calls, user = _changing_resolver(monkeypatch, later)
    response = client.post("/api/conversation-projects", json={"name": "Synthetic project"})
    assert response.status_code == 200
    assert len(calls) == 1
    monkeypatch.setattr(accounts, "resolve_session", lambda *args, **kwargs: user)
    assert len(client.get("/api/conversation-projects").json()["projects"]) == 1
    monkeypatch.setattr(accounts, "resolve_session", lambda *args, **kwargs: None)
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "0")
    assert client.get("/api/conversation-projects").json()["projects"] == []


def test_quota_uses_the_identity_admitted_by_middleware(client, monkeypatch):
    llm_quota.check_and_increment(webapp.PROJECT_ROOT, "alice", trial=False,
                                  per_user_limit=1, global_limit=100)
    calls, _ = _changing_resolver(monkeypatch, "expired")
    cfg = llm_client.LLMConfig(enable_llm=True, provider="openai-compatible", api_key="synthetic-key")
    monkeypatch.setattr(webapp, "load_llm_config", lambda **kwargs: cfg)
    executions = []
    monkeypatch.setattr(webapp.dream, "dream_from_conversations",
                        lambda *args, **kwargs: executions.append(True) or {"candidates": []})
    response = client.post("/api/dream", json={"conversations": [], "provider": "openai-compatible"})
    assert response.status_code == 429
    assert len(calls) == 1 and executions == []


def test_conversation_and_whoami_reuse_request_identity(client, monkeypatch):
    calls, user = _changing_resolver(monkeypatch, "expired")
    response = client.get("/api/account/whoami")
    assert response.json()["user"] == user.as_dict()
    assert len(calls) == 1
    calls, _ = _changing_resolver(monkeypatch, "expired")
    monkeypatch.setattr(turn, "route_turn", lambda text, **kw: {
        "route": "none", "query": "", "plan": None, "retrieval": None,
        "echo_zh": kw["principal"], "via": "rules"})
    response = client.post("/api/utterance", json={"utterance": "synthetic request"})
    assert response.status_code == 200 and response.json()["echo_zh"] == user.id
    assert len(calls) == 1


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_billable_get_rejects_fetch_metadata_without_origin(client, monkeypatch, site):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("cross-origin GET reached introduction execution")

    monkeypatch.setattr(webapp, "_locate_record_or_409", forbidden)
    response = client.get("/api/introduction?uid=synthetic&llm=1",
                          headers={"Sec-Fetch-Site": site, "Sec-Fetch-Mode": "no-cors"})
    assert response.status_code == 403 and calls == []


@pytest.mark.parametrize("explicit", [False, True])
def test_quota_path_cannot_replace_frozen_data(tmp_path, monkeypatch, explicit):
    database = tmp_path / "database"
    frozen = database / "base" / "catalog.json"
    frozen.parent.mkdir(parents=True)
    original = b'[{"dataset_name":"synthetic frozen record"}]'
    frozen.write_bytes(original)
    monkeypatch.setattr(runtime_paths, "repo_database_dir", lambda: database)
    monkeypatch.setenv("BIODATA_LLM_QUOTA_FILE", str(frozen))
    with pytest.raises(ValueError) as error:
        llm_quota.check_and_increment(tmp_path, "alice", trial=False,
                                     per_user_limit=1, global_limit=1,
                                     quota_path=frozen if explicit else None)
    assert getattr(error.value, "code", "") == "bad_store_path"
    assert frozen.read_bytes() == original


@pytest.mark.parametrize("value", ["２０２４-０１-０１", "٢٠٢٤-٠١-٠١", "2024-０1-01"])
def test_iso_date_requires_ascii_digits(value):
    with pytest.raises(ParamValidationError) as error:
        validate_iso_date(value, name="date_from")
    assert error.value.code == "bad_param"


@pytest.mark.parametrize("path", ["/api/recommend", "/api/feasibility", "/api/task-pack/preview", "/api/utterance"])
def test_mixed_unicode_date_window_is_rejected_before_execution(client, path):
    payload = {"query": "human lung", "date_from": "2025-01-01", "date_to": "２０２４-１２-３１"}
    if path == "/api/utterance":
        payload["utterance"] = "synthetic request"
    response = client.post(path, json=payload)
    assert response.status_code == 400
    assert response.headers.get("x-error-code") == "bad_param"


@pytest.mark.parametrize("path", ["/api/task-pack/preview", "/api/task-pack/build"])
@pytest.mark.parametrize("field", ["query", "query_effective"])
def test_task_pack_queries_are_bounded_before_retrieval(client, monkeypatch, path, field):
    touched = []

    class ForbiddenWorkflow:
        def __init__(self):
            touched.append(True)
            raise AssertionError("oversize query reached retrieval")

    monkeypatch.setattr(webapp, "DatasetRecommendationWorkflow", ForbiddenWorkflow)
    params = {"query": "human lung", field: "x" * (MAX_QUERY_CHARS + 1)}
    payload = params if path.endswith("preview") else {
        "plan_token": "synthetic", "snapshot_id": "synthetic", "content_digest": "synthetic",
        "selected_uids": ["synthetic"], "retrieval_params": params}
    response = client.post(path, json=payload)
    assert response.status_code == 400 and response.headers.get("x-error-code") == "bad_query"
    assert touched == []


def test_utterance_query_context_is_bounded_before_enqueue(client, monkeypatch):
    touched = []
    monkeypatch.setattr(turn, "route_turn", lambda *args, **kw: touched.append(True) or {
        "route": "none", "query": "", "plan": None, "retrieval": None, "via": "rules"})
    response = client.post("/api/utterance", json={"utterance": "synthetic request",
        "query": "x" * (MAX_QUERY_CHARS + 1), "background": True})
    assert response.status_code == 400 and response.headers.get("x-error-code") == "bad_query"
    assert touched == []


def test_sessions_store_only_digests_and_digest_is_not_a_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("BIODATA_SESSION_STORE", "")
    accounts._reset_state_for_tests()
    path = tmp_path / "sessions.json"
    user = accounts.PublicUser("synthetic-user", "alice")
    token = accounts.create_session(user, sessions_path=path)
    digest = hashlib.sha256(token.encode()).hexdigest()
    text = path.read_text(encoding="utf-8")
    assert token not in text and digest in text
    accounts._reset_state_for_tests()
    assert accounts.resolve_session(token, sessions_path=path) == user
    assert accounts.resolve_session(digest, sessions_path=path) is None
    accounts.destroy_session(digest, sessions_path=path)
    assert accounts.resolve_session(token, sessions_path=path) == user
    accounts.destroy_session(token, sessions_path=path)
    accounts._reset_state_for_tests()
    assert accounts.resolve_session(token, sessions_path=path) is None


def test_legacy_sessions_are_read_then_upgraded_on_write(tmp_path, monkeypatch):
    monkeypatch.setenv("BIODATA_SESSION_STORE", "")
    accounts._reset_state_for_tests()
    path = tmp_path / "sessions.json"
    token = "synthetic-legacy-token"
    legacy = {token: {"user_id": "synthetic-user", "username": "alice", "expires_at": time.time() + 3600}}
    path.write_text(json.dumps(legacy), encoding="utf-8")
    before = path.read_bytes()
    user = accounts.resolve_session(token, sessions_path=path)
    assert user == accounts.PublicUser("synthetic-user", "alice")
    assert path.read_bytes() == before
    accounts.create_session(user, sessions_path=path)
    assert token not in path.read_text(encoding="utf-8")
    accounts._reset_state_for_tests()
    assert accounts.resolve_session(token, sessions_path=path) == user
    assert accounts.resolve_session(hashlib.sha256(token.encode()).hexdigest(), sessions_path=path) is None


@pytest.mark.parametrize("path", ["/", "/dataset", "/static/index.html", "/static/dataset.html"])
def test_csp_hashes_the_emitted_importmap_without_restricting_connections(client, path):
    import base64
    response = client.get(path)
    assert response.status_code == 200
    policy = response.headers.get("content-security-policy", "")
    assert "script-src 'self'" in policy and "'unsafe-inline'" not in policy
    assert "connect-src" not in policy and "default-src" not in policy
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script\s*>", response.text, re.I | re.S)
    for script in (script for script in scripts if script.strip()):
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        assert f"'sha256-{digest}'" in policy
    assert "object-src 'none'" in policy and "base-uri 'self'" in policy


@pytest.mark.parametrize("path", ["/", "/dataset", "/static/index.html", "/static/dataset.html"])
def test_csp_survives_head_compression_and_cache_revalidation(client, path):
    response = client.get(path, headers={"Accept-Encoding": "gzip"})
    policy = response.headers["content-security-policy"]
    assert response.headers.get("content-encoding") == "gzip"
    head = client.head(path)
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-security-policy"] == policy
    if path.startswith("/static/"):
        cached = client.get(path, headers={"If-None-Match": response.headers["etag"]})
        assert cached.status_code == 304
        assert cached.headers["content-security-policy"] == policy


def test_csp_uses_the_actual_rendered_page_and_normalizes_html_newlines(client, monkeypatch):
    import base64
    script = '\n{"synthetic":true}\n'
    monkeypatch.setenv("BIODATA_SITE_FOOTER_HTML", '<script type="application/json">' + script + '</script>')
    response = client.get("/")
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert script in response.text and f"'sha256-{digest}'" in response.headers["content-security-policy"]
    assert webapp._content_security_policy("<script>\r\n1\r\n</script>") == \
           webapp._content_security_policy("<script>\n1\n</script>")


@pytest.mark.parametrize("payload", [
    {"provider": "trial", "mock_llm": True, "use_llm": False},
    {"provider": "openai-compatible", "mock_llm": True, "use_llm": True},
])
def test_diagnose_respects_the_effective_mock_configuration(client, monkeypatch, payload):
    configs = []
    monkeypatch.setattr(webapp, "healthcheck", lambda cfg: configs.append(cfg) or {})
    monkeypatch.setattr(webapp, "diagnose_network", lambda cfg: configs.append(cfg) or {})
    for _ in range(2):
        assert client.post("/api/diagnose", json=payload).status_code == 200
    assert len(configs) == 4 and all(cfg.mock_llm for cfg in configs)
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=payload["provider"] == "trial") == 0


def test_rerank_byok_uses_the_supplied_credential_without_server_charges(client, monkeypatch):
    calls = []

    def fake_call(prompt, config, *args, **kwargs):
        calls.append(config)
        return llm_client.LLMResult(text="[1,2]", attempted=True, succeeded=True,
            response_used=True, provider=config.provider, model="synthetic")

    monkeypatch.setattr(rerank, "call_llm", fake_call)
    payload = {"query": "human lung single cell", "provider": "openai-compatible",
        "api_key": "synthetic-byok", "use_llm": False, "polish": False, "rerank": "llm", "top_k": 2}
    for _ in range(2):
        assert client.post("/api/recommend", json=payload).status_code == 200
    assert len(calls) >= 2 and all(cfg.api_key == "synthetic-byok" for cfg in calls)
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=False) == 0


# ---------------------------------------------------------------------------
# /api/recommend 的 LLM 配额准入必须发生在「真实调用即将发出」时，
# 而不是「能力可用」（服务端有 key + auto_allow_llm）时。
# 这些用例走真实 workflow 与真实 strategy 分类器，只把 provider 请求换成计数替身
# （llm_client._call_chat_completions 是 rerank 与润色共用的唯一真实出口）。
# ---------------------------------------------------------------------------


def _count_provider_calls(monkeypatch):
    """provider 请求替身：记录每次真实出网调用，返回合法形态文本。

    rerank prompt 里带「包含 1 到 N」提示 → 返回完整编号排列（合法重排输出，不触发错误重问）；
    润色调用返回同一文本（会在反捏造校验处降级，但请求已真实发出，计数不受影响）。"""
    calls = []

    def fake_provider(prompt, config, provider_name, default_base_url):
        calls.append({"prompt": prompt, "config": config, "provider": provider_name})
        match = re.search(r"包含 1 到 (\d+)", prompt)
        text = json.dumps(list(range(1, int(match.group(1)) + 1))) if match else "[1]"
        return llm_client.LLMResult(text=text, attempted=True, succeeded=True,
                                    response_used=True, provider=provider_name, model=config.model)

    monkeypatch.setattr(llm_client, "_call_chat_completions", fake_provider)
    return calls


def _exhaust_user_quota():
    llm_quota.check_and_increment(webapp.PROJECT_ROOT, "alice", trial=False,
                                  per_user_limit=1, global_limit=100)


def _server_quota_usage():
    return llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=False)


def test_recommend_auto_precise_query_is_free_under_exhausted_quota(client, monkeypatch):
    """auto 分类器对精确查询派生 off/off（真实判定）：零真实 LLM 请求 → 不得预扣、不得 429。"""
    calls = _count_provider_calls(monkeypatch)
    _exhaust_user_quota()
    payload = {"query": "Xenium human lung", "provider": "openai-compatible",
               "strategy": "auto", "auto_allow_llm": True, "top_k": 5}
    response = client.post("/api/recommend", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["strategy"]["rerank_backend"] == "off"      # 真实分类器的派生结果
    assert body["strategy"]["tier"] == "precise"
    assert body["results"]                                   # 非 LLM 的确定性排序仍出结果
    assert calls == []                                       # 零真实 provider 请求
    assert _server_quota_usage() == 1                        # 已耗尽额度不增不减


def test_recommend_auto_mock_polish_is_free_under_exhausted_quota(client, monkeypatch):
    """auto + mock 润色：mock 不打 provider、不该烧服务端配额（即使额度已耗尽也应 200）。"""
    calls = _count_provider_calls(monkeypatch)
    _exhaust_user_quota()
    payload = {"query": "Xenium human lung", "provider": "openai-compatible",
               "strategy": "auto", "auto_allow_llm": True,
               "use_llm": True, "polish": True, "mock_llm": True, "top_k": 5}
    response = client.post("/api/recommend", json=payload)
    assert response.status_code == 200
    assert calls == []
    assert _server_quota_usage() == 1


def test_recommend_explicit_rerank_without_candidates_is_free_under_exhausted_quota(client, monkeypatch):
    """显式 rerank=llm 但零存活集：retriever 提前返回、rerank 块不执行 → 不得在无 items 时计费。"""
    calls = _count_provider_calls(monkeypatch)
    _exhaust_user_quota()
    payload = {"query": "Xenium mouse kidney", "provider": "openai-compatible",
               "rerank": "llm", "use_llm": False, "polish": False, "top_k": 5}
    response = client.post("/api/recommend", json=payload)
    assert response.status_code == 200
    assert response.json()["results"] == []
    assert calls == []
    assert _server_quota_usage() == 1


def test_recommend_polish_blocks_before_provider_call_under_exhausted_quota(client, monkeypatch):
    """显式润色会真实调用：额度耗尽时必须在任何 provider 请求之前 429。"""
    calls = _count_provider_calls(monkeypatch)
    _exhaust_user_quota()
    payload = {"query": "Xenium human lung", "provider": "openai-compatible",
               "use_llm": True, "polish": True, "top_k": 5}
    response = client.post("/api/recommend", json=payload)
    assert response.status_code == 429
    assert calls == []
    assert _server_quota_usage() == 1


def test_recommend_semantic_auto_rerank_charges_once_then_blocks_before_call(client, monkeypatch):
    """auto 真实派生 rerank=llm：准入放行一轮（一次真实调用/计一单位），耗尽后 429 零调用。

    查询取 complex 档（宽候选 + 多自由语义词）：无论本机本地语义后端是否可用，真实分类器
    都派生 rerank=llm（可用→混合两层；不可用→LLM 单层），用例不依赖模型工件在场。"""
    calls = _count_provider_calls(monkeypatch)
    payload = {"query": "mouse brain single cell", "provider": "openai-compatible",
               "strategy": "auto", "auto_allow_llm": True, "top_k": 5}
    first = client.post("/api/recommend", json=payload)
    assert first.status_code == 200
    assert first.json()["strategy"]["rerank_backend"] == "llm"
    assert len(calls) == 1                       # 真实 rerank 请求确实发生
    assert _server_quota_usage() == 1
    second = client.post("/api/recommend", json=payload)
    assert second.status_code == 429
    assert len(calls) == 1                       # 被拦在任何真实模型调用之前
    assert _server_quota_usage() == 1


def test_recommend_rerank_and_polish_count_as_one_round(client, monkeypatch):
    """同请求 rerank + polish 两个 stage 都真实发出，但只计一轮配额；下一请求 429。"""
    calls = _count_provider_calls(monkeypatch)
    payload = {"query": "mouse brain single cell", "provider": "openai-compatible",
               "strategy": "auto", "auto_allow_llm": True,
               "use_llm": True, "polish": True, "top_k": 2}
    first = client.post("/api/recommend", json=payload)
    assert first.status_code == 200
    assert len(calls) >= 2                       # rerank + polish 都真的调用了 provider
    assert _server_quota_usage() == 1            # 两 stage 只算一轮
    calls_before_second = len(calls)
    second = client.post("/api/recommend", json=payload)
    assert second.status_code == 429
    assert len(calls) == calls_before_second


def test_recommend_auto_byok_does_not_consume_server_quota(client, monkeypatch):
    """auto 选到 LLM rerank 但请求自带 key（BYOK）：真实调用发生、服务端配额不动。"""
    calls = _count_provider_calls(monkeypatch)
    payload = {"query": "mouse brain single cell", "provider": "openai-compatible",
               "api_key": "synthetic-byok", "strategy": "auto", "auto_allow_llm": True, "top_k": 2}
    for _ in range(2):
        assert client.post("/api/recommend", json=payload).status_code == 200
    assert len(calls) >= 2
    assert all(call["config"].api_key == "synthetic-byok" for call in calls)
    assert _server_quota_usage() == 0


def test_workflow_before_llm_hook_fires_once_before_real_calls(client, monkeypatch):
    """workflow 接缝：hook 在该轮第一次真实调用前触发、整轮只触发一次（rerank+polish 两 stage）。"""
    from dataset_recommender.app.workflow import DatasetRecommendationWorkflow, RecommendParams

    calls = _count_provider_calls(monkeypatch)
    seen = []
    workflow = DatasetRecommendationWorkflow()
    meta = workflow.run_with_meta(
        RecommendParams(query="human lung single cell", top_k=2,
                        use_llm=True, mock_llm=False, provider="openai-compatible",
                        strategy="auto", recall_available=False, llm_available=True),
        before_llm=lambda config: seen.append(config))
    assert meta.llm_called is True
    assert len(calls) >= 2 and len(seen) == 1
    assert seen[0].enable_llm and not seen[0].mock_llm and seen[0].api_key


@pytest.fixture
def replay_request_chain(client, monkeypatch, tmp_path):
    from dataset_recommender.app import replay_service, session_log, workflow
    from dataset_recommender.llm.config import DEFAULT_KEYWORD_MAPPING, Settings
    from dataset_recommender.retrieval.normalizer import DatasetRecord

    settings = Settings(tmp_path, tmp_path / "catalog", tmp_path / "out", 5,
                        True, False, DEFAULT_KEYWORD_MAPPING)
    settings.data_dir.mkdir()
    records = [DatasetRecord(
        dataset_name=f"Synthetic lung dataset {i}", species="human", tissue="lung",
        disease="normal", chemistry="Single Cell 3' v3", count="100", unit="cells",
        has_raw_data=True, url=f"https://catalog.example/datasets/{i}",
        source_file="synthetic.json", description="human lung single cell", raw={},
        platform_family="chromium", assay="gex", modality="single-cell",
    ) for i in range(3)]
    monkeypatch.setattr(webapp, "get_settings", lambda: settings)
    monkeypatch.setattr(workflow, "get_settings", lambda: settings)
    monkeypatch.setattr(workflow, "load_normalized_corpus", lambda *a, **kw: records)
    monkeypatch.setenv("BIODATA_SESSION_LOG", "on")
    replay_service._reset_for_tests()
    session_log._reset_for_tests()
    calls = []

    def provider_call(prompt, config, provider_name, default_base_url):
        calls.append(config)
        match = re.search(r"包含 1 到 (\d+)", prompt)
        text = json.dumps(list(range(1, int(match.group(1)) + 1))) if match else json.dumps({
            "motivation": None, "outputs": [{"title": "Synthetic result", "turn_refs": [1]}],
            "knowledge": [], "decisions": [], "edges": [],
        })
        return llm_client.LLMResult(text=text, attempted=True, succeeded=True,
                                   response_used=True, provider=provider_name, model=config.model)

    class InlineThread:
        def __init__(self, *, target, args, **kwargs):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(llm_client, "_call_chat_completions", provider_call)
    monkeypatch.setattr(replay_service, "threading", SimpleNamespace(Thread=InlineThread))
    yield calls, replay_service, session_log
    replay_service._reset_for_tests()
    session_log._reset_for_tests()


def _replay_recommend_payload(**overrides):
    return {"query": "human lung", "provider": "openai-compatible", "use_llm": True,
            "polish": False, "rerank": "off", "strategy": "fixed", "top_k": 2,
            "conv_id": "synthetic-replay", "replay_key": "synthetic-replay-key", **overrides}


@pytest.mark.parametrize("query,has_results", [("human lung", True), ("mouse kidney", False)])
def test_recommend_replay_draw_cannot_bypass_exhausted_quota(
        client, replay_request_chain, query, has_results):
    calls, replay, log = replay_request_chain
    _exhaust_user_quota()
    response = client.post("/api/recommend", json=_replay_recommend_payload(query=query))
    assert response.status_code == 200
    assert bool(response.json()["results"]) is has_results
    assert len(log.read_records(webapp.PROJECT_ROOT, "synthetic-alice", "synthetic-replay")) == 1
    assert calls == []
    assert _server_quota_usage() == 1
    assert replay.status(webapp.PROJECT_ROOT, "synthetic-alice", "synthetic-replay",
                         "synthetic-replay-key")["has_digest"] is False


@pytest.mark.parametrize("query,has_results", [("human lung", True), ("mouse kidney", False)])
def test_recommend_replay_draw_admits_one_round_and_preserves_later_rule_turns(
        client, replay_request_chain, query, has_results):
    calls, replay, log = replay_request_chain
    first = client.post("/api/recommend", json=_replay_recommend_payload(query=query))
    assert first.status_code == 200 and bool(first.json()["results"]) is has_results
    assert len(calls) == 1 and _server_quota_usage() == 1
    assert replay.points(webapp.PROJECT_ROOT, "synthetic-alice", "synthetic-replay",
                         "synthetic-replay-key")["digest"]["outputs"]
    second = client.post("/api/recommend", json=_replay_recommend_payload(query=query))
    assert second.status_code == 200
    assert len(calls) == 1 and _server_quota_usage() == 1
    assert len(log.read_records(webapp.PROJECT_ROOT, "synthetic-alice", "synthetic-replay")) == 2


@pytest.mark.parametrize("polish,expected_calls", [(False, 2), (True, 3)])
def test_recommend_main_llm_and_replay_draw_share_one_admission(
        client, replay_request_chain, polish, expected_calls):
    calls, replay, log = replay_request_chain
    payload = _replay_recommend_payload(rerank="llm", polish=polish)
    assert client.post("/api/recommend", json=payload).status_code == 200
    assert len(calls) == expected_calls and _server_quota_usage() == 1
    assert replay.status(webapp.PROJECT_ROOT, "synthetic-alice", "synthetic-replay",
                         "synthetic-replay-key")["has_digest"] is True
    assert client.post("/api/recommend", json=payload).status_code == 429
    assert len(calls) == expected_calls
    assert len(log.read_records(webapp.PROJECT_ROOT, "synthetic-alice", "synthetic-replay")) == 1


@pytest.mark.parametrize("overrides,reason", [
    ({"provider": "mock"}, "mock"), ({"mock_llm": True}, "mock"),
    ({"use_llm": False}, "no_key"), ({"provider": "trial"}, "trial"),
])
def test_recommend_nonworthy_replay_never_charges(client, replay_request_chain, overrides, reason):
    calls, replay, log = replay_request_chain
    assert client.post("/api/recommend", json=_replay_recommend_payload(**overrides)).status_code == 200
    assert calls == [] and _server_quota_usage() == 0
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=True) == 0
    assert replay.status(webapp.PROJECT_ROOT, "synthetic-alice", "synthetic-replay",
                         "synthetic-replay-key")["skip_reason"] == reason
    assert len(log.read_records(webapp.PROJECT_ROOT, "synthetic-alice", "synthetic-replay")) == 1


def test_recommend_replay_byok_keeps_effective_config_and_does_not_charge(
        client, replay_request_chain):
    calls, _, _ = replay_request_chain
    _exhaust_user_quota()
    for _ in range(2):
        assert client.post("/api/recommend", json=_replay_recommend_payload(
            api_key="synthetic-byok", model="synthetic-custom-model")).status_code == 200
    assert len(calls) == 2 and _server_quota_usage() == 1
    assert all(cfg.api_key == "synthetic-byok" and cfg.model == "synthetic-custom-model"
               and cfg.provider == "openai-compatible" for cfg in calls)
