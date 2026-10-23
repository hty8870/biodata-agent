"""Durable conversation transport remains responsive and owner-scoped."""
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.agent import turn
from dataset_recommender.app import accounts, webapp
from dataset_recommender.app.conversation_runs import ConversationRuntime, RunStore, TERMINAL


@pytest.fixture
def environment(monkeypatch, tmp_path):
    monkeypatch.setenv("BIODATA_RUNS_DB", str(tmp_path / "runs.sqlite"))
    runtime = ConversationRuntime(RunStore(tmp_path), workers=2, max_per_owner=1)
    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(webapp, "_CONVERSATION_RUNTIME", runtime)
    monkeypatch.setattr(webapp, "_account_gate_required", lambda: True)
    users = {name: SimpleNamespace(id=name, username=name) for name in ("alice", "bob")}
    monkeypatch.setattr(accounts, "resolve_session", lambda token, **kw: users.get(token))
    clients = {}
    for name in users:
        client = TestClient(webapp.app, base_url="http://127.0.0.1")
        client.cookies.set(accounts.SESSION_COOKIE, name)
        clients[name] = client
    yield clients, runtime
    runtime.close()


def wait_result(client, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        body = client.get(f"/api/runs/{run_id}").json()["run"]
        if body["status"] in TERMINAL:
            return body
        time.sleep(.01)
    raise AssertionError("task did not finish")


def reply(owner):
    return {"route": "none", "query": "", "plan": None, "echo_zh": owner,
            "retrieval": None, "via": "rules"}


def test_submission_does_not_wait_and_replay_cost_is_charged_once(environment, monkeypatch):
    clients, runtime = environment
    started, release = threading.Event(), threading.Event()
    costs, executions = [], []
    monkeypatch.setattr(webapp, "_gate_llm_quota", lambda *args, **kwargs: costs.append("charged"))
    def route(text, **kwargs):
        executions.append(kwargs["principal"]); started.set()
        assert release.wait(5)
        kwargs["on_event"]("step", {"label_zh": "synthetic completed step"})
        return reply(kwargs["principal"])
    monkeypatch.setattr(turn, "route_turn", route)
    payload = {"utterance": "synthetic request", "req_id": "shared", "background": True}
    first = clients["alice"].post("/api/utterance", json=payload)
    assert first.status_code == 202 and started.wait(2)
    run_id = first.json()["run"]["id"]
    assert clients["bob"].get("/api/health").status_code == 200
    replay = clients["alice"].post("/api/utterance", json=payload)
    assert replay.status_code == 202 and replay.json()["run"]["id"] == run_id
    assert clients["bob"].get(f"/api/runs/{run_id}").status_code == 404
    assert clients["bob"].post(f"/api/runs/{run_id}/cancel").status_code == 404
    release.set()
    final = wait_result(clients["alice"], run_id)
    assert final["result"]["echo_zh"] == "alice"
    assert costs == ["charged"] and executions == ["alice"]
    events = clients["alice"].get(f"/api/runs/{run_id}/events").json()["events"]
    assert events[0]["seq"] == 1 and events[0]["event"] == "step"


def test_capacity_response_does_not_charge_or_block_other_account(environment, monkeypatch):
    clients, runtime = environment
    started, release = threading.Event(), threading.Event()
    costs = []
    monkeypatch.setattr(webapp, "_gate_llm_quota", lambda *args, **kw: costs.append("charge"))
    def route(text, **kwargs):
        if kwargs["principal"] == "alice":
            started.set(); assert release.wait(5)
        return reply(kwargs["principal"])
    monkeypatch.setattr(turn, "route_turn", route)
    first = clients["alice"].post("/api/utterance", json={"utterance": "one", "background": True, "req_id": "a"})
    assert started.wait(2)
    denied = clients["alice"].post("/api/utterance", json={"utterance": "two", "background": True, "req_id": "b"})
    assert denied.status_code == 503 and denied.headers["x-error-code"] == "run_capacity"
    other = clients["bob"].post("/api/utterance", json={"utterance": "one", "background": True, "req_id": "a"})
    assert other.status_code == 202
    assert wait_result(clients["bob"], other.json()["run"]["id"])["status"] == "done"
    assert len(costs) == 2
    release.set(); wait_result(clients["alice"], first.json()["run"]["id"])


def test_legacy_json_uses_the_same_bounded_executor(environment, monkeypatch):
    clients, runtime = environment
    started, release = threading.Event(), threading.Event()
    costs, result = [], []
    monkeypatch.setattr(webapp, "_gate_llm_quota", lambda *args, **kw: costs.append("charge"))
    def route(text, **kwargs):
        if kwargs["principal"] == "alice":
            started.set(); assert release.wait(5)
        return reply(kwargs["principal"])
    monkeypatch.setattr(turn, "route_turn", route)
    first = threading.Thread(target=lambda: result.append(clients["alice"].post(
        "/api/utterance", json={"utterance": "one", "req_id": "legacy"})))
    first.start(); assert started.wait(2)
    rejected = clients["alice"].post("/api/utterance", json={"utterance": "two", "req_id": "different"})
    assert rejected.status_code == 503
    other = clients["bob"].post("/api/utterance", json={"utterance": "one", "req_id": "legacy"})
    assert other.status_code == 200 and other.json()["echo_zh"] == "bob"
    release.set(); first.join(5)
    assert result[0].status_code == 200 and costs == ["charge", "charge"]


def test_switching_transports_reuses_the_same_receipt(environment, monkeypatch):
    clients, runtime = environment
    calls = []
    monkeypatch.setattr(turn, "route_turn", lambda text, **kw: calls.append(text) or reply(kw["principal"]))
    payload = {"utterance": "one", "req_id": "transport_switch"}
    first = clients["alice"].post("/api/utterance", json={**payload, "background": True})
    row = wait_result(clients["alice"], first.json()["run"]["id"])
    second = clients["alice"].post("/api/utterance", json=payload)
    third = clients["alice"].post("/api/utterance", json={**payload, "stream": True})
    assert second.json() == row["result"]
    assert third.status_code == 200 and "alice" in third.text
    assert calls == ["one"]
