"""Offline regressions for durable task recovery and completed tool receipts."""
from __future__ import annotations

import datetime
import importlib.util
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import fakeredis
import pytest
import redis
import rq
from fastapi import HTTPException
from fastapi.testclient import TestClient
from rq.serializers import JSONSerializer

from dataset_recommender.agent import run_control, turn
from dataset_recommender.app import accounts, conversation_runs as runs, durable_jobs, redis_store, rq_jobs, rq_worker, webapp
from dataset_recommender.retrieval import recall_api


@pytest.fixture(autouse=True)
def isolate_run_store(monkeypatch, tmp_path):
    monkeypatch.setenv("BIODATA_RUNS_DB", str(tmp_path / "runs.sqlite"))


@pytest.fixture
def broker(monkeypatch):
    server = fakeredis.FakeServer()
    text = fakeredis.FakeStrictRedis(server=server, decode_responses=True)
    binary = fakeredis.FakeStrictRedis(server=server, decode_responses=False)
    monkeypatch.setattr(redis_store, "_build_client", lambda: text)
    monkeypatch.setattr(redis_store, "_build_rq_client", lambda: binary)
    redis_store._reset_for_tests()
    yield text, binary
    redis_store._reset_for_tests()


@pytest.fixture
def local_job(monkeypatch, tmp_path):
    monkeypatch.setenv("BIODATA_JOBS_DB", str(tmp_path / "jobs.sqlite"))
    monkeypatch.delenv("BIODATA_JOB_BACKEND", raising=False)
    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    with webapp._CORPUS_SYNC_JOB_LOCK:
        webapp._CORPUS_SYNC_JOB.update(status="idle", started_at=None, finished_at=None, result=None, error=None)
    monkeypatch.setattr(webapp, "_CORPUS_SYNC_RECONCILED", False)
    yield tmp_path / "jobs.sqlite"
    with webapp._CORPUS_SYNC_JOB_LOCK:
        webapp._CORPUS_SYNC_JOB.update(status="idle", started_at=None, finished_at=None, result=None, error=None)


def _terminal(runtime, run_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        row = runtime.store.load("alice", run_id)
        if row["status"] in runs.TERMINAL:
            return row
        time.sleep(.01)
    raise AssertionError("completed execution remained in flight")


def test_enqueue_commit_with_lost_reply_keeps_the_live_job_claim(broker, monkeypatch):
    text, binary = broker
    queue_factory = rq.Queue
    submissions = []

    def queue(*args, **kwargs):
        instance = queue_factory(*args, **kwargs)
        enqueue = instance.enqueue

        def lost_reply(*args, **kwargs):
            job = enqueue(*args, **kwargs)
            submissions.append(job)
            job.set_status("started")
            job.started_at = datetime.datetime.now(datetime.timezone.utc)
            job.last_heartbeat = job.started_at
            job.save()
            raise redis.ConnectionError("synthetic committed enqueue reply loss")

        instance.enqueue = lost_reply
        return instance

    monkeypatch.setattr(rq, "Queue", queue)
    with pytest.raises(rq_jobs.JobBackendError):
        rq_jobs.start(None)
    assert text.hget(rq_jobs._STATUS_KEY, "status") == "running"
    assert rq_jobs.start(None)["status"] == "running"
    assert len(submissions) == 1
    monkeypatch.setattr(webapp, "_corpus_sync_execute", lambda sources: {"imported_total": 0, "operation_id": "synthetic-completed"})
    assert submissions[0].perform()["status"] == "done"
    assert rq_jobs.snapshot()["result"]["operation_id"] == "synthetic-completed"


def test_failed_enqueue_releases_only_after_confirmed_missing_and_grace(broker, monkeypatch):
    text, _ = broker
    queue_factory = rq.Queue

    def queue(*args, **kwargs):
        instance = queue_factory(*args, **kwargs)
        instance.enqueue = lambda *args, **kwargs: (_ for _ in ()).throw(redis.ConnectionError("synthetic outage"))
        return instance

    monkeypatch.setattr(rq, "Queue", queue)
    with pytest.raises(rq_jobs.JobBackendError):
        rq_jobs.start(None)
    assert rq_jobs.snapshot()["status"] == "running"
    text.hset(rq_jobs._STATUS_KEY, rq_jobs._CLAIMED_AT_FIELD, "0")
    assert rq_jobs.snapshot()["status"] == "failed"


def _independent_consumer(name):
    spec = importlib.util.spec_from_file_location("dataset_recommender.app." + name, Path(rq_jobs.__file__))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_invalidation_generation_is_not_destroyed_by_the_first_consumer(broker, monkeypatch):
    text, _ = broker
    monkeypatch.setattr(webapp, "_corpus_sync_execute", lambda sources: {"imported_total": 0})
    rq_jobs.corpus_sync_entry(None)
    first = _independent_consumer("synthetic_consumer_one")
    second = _independent_consumer("synthetic_consumer_two")
    assert first.consume_web_invalidate_flag() is True
    assert second.consume_web_invalidate_flag() is True
    assert first.consume_web_invalidate_flag() is False
    assert second.consume_web_invalidate_flag() is False
    assert text.hget(rq_jobs._STATUS_KEY, rq_jobs._FLAG_FIELD) != "0"


def test_plain_browsing_refreshes_worker_generation_without_status_poll(broker, monkeypatch):
    monkeypatch.setenv("BIODATA_JOB_BACKEND", "rq")
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "0")
    monkeypatch.setattr(webapp, "_corpus_sync_execute", lambda sources: {"imported_total": 0})
    rq_jobs.corpus_sync_entry(None)
    calls = []
    monkeypatch.setattr(webapp, "invalidate_external_cache", lambda: calls.append("corpus"))
    monkeypatch.setattr(recall_api, "invalidate_vectors", lambda: calls.append("vectors"))
    client = TestClient(webapp.app, base_url="http://127.0.0.1")
    assert client.get("/api/datasets", params={"limit": 1}).status_code == 200
    assert calls == ["corpus", "vectors"]
    assert client.get("/api/datasets", params={"limit": 1}).status_code == 200
    assert calls == ["corpus", "vectors"]


def test_queue_compensation_result_does_not_expire_before_acknowledgement(broker):
    text, binary = broker
    rq_jobs.start(None)
    job = rq.job.Job.fetch(text.hget(rq_jobs._STATUS_KEY, rq_jobs._JOB_ID_FIELD), connection=binary, serializer=JSONSerializer)
    assert job.result_ttl == -1


def test_queue_worker_and_fetch_use_json_without_pickle_fallback(broker, monkeypatch):
    text, binary = broker
    rq_jobs.start(["synthetic-source"])
    job = rq.job.Job.fetch(text.hget(rq_jobs._STATUS_KEY, rq_jobs._JOB_ID_FIELD), connection=binary, serializer=JSONSerializer)
    assert job.func_name == rq_jobs._ENTRY_PATH and job.args == [["synthetic-source"]]
    observed = []

    class Worker:
        def __init__(self, queues, connection, **kwargs):
            observed.append((queues[0].serializer, kwargs.get("serializer")))

        def work(self):
            return None

    monkeypatch.setattr(rq, "SimpleWorker", Worker)
    monkeypatch.setattr(rq, "Worker", Worker)
    rq_worker.main()
    assert observed[0][0] is JSONSerializer
    assert rq.serializers.resolve_serializer(observed[0][1]) is JSONSerializer


def test_sqlite_reconciliation_recovers_after_the_first_transient_failure(local_job, monkeypatch):
    durable_jobs.record_running(webapp._CORPUS_SYNC_JOB_KIND, "synthetic-previous-start", db_path=local_job)
    reconcile = durable_jobs.reconcile_interrupted
    calls = []

    def once(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise OSError("synthetic transient sqlite lock")
        return reconcile(*args, **kwargs)

    monkeypatch.setattr(durable_jobs, "reconcile_interrupted", once)
    webapp._corpus_sync_job_snapshot()
    assert webapp._corpus_sync_job_snapshot()["status"] == "failed"
    assert len(calls) == 2


def test_reconciliation_retry_cannot_interrupt_a_new_local_worker(local_job, monkeypatch):
    durable_jobs.record_running(webapp._CORPUS_SYNC_JOB_KIND, "synthetic-previous-start", db_path=local_job)
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(True)
        raise OSError("synthetic transient sqlite lock")

    monkeypatch.setattr(durable_jobs, "reconcile_interrupted", unavailable)
    webapp._corpus_sync_job_snapshot()
    monkeypatch.setattr(webapp, "_corpus_sync_job_run", lambda sources: None)
    assert webapp._corpus_sync_job_start(None)["status"] == "running"
    monkeypatch.setattr(durable_jobs, "reconcile_interrupted", lambda *a, **kw: pytest.fail("must not reconcile a live local task"))
    assert webapp._corpus_sync_job_snapshot()["status"] == "running"


def test_thread_start_failure_is_terminal_and_allows_a_new_attempt(local_job, monkeypatch):
    start = threading.Thread.start
    monkeypatch.setattr(threading.Thread, "start", lambda thread: (_ for _ in ()).throw(RuntimeError("synthetic thread limit")))
    with pytest.raises((HTTPException, RuntimeError)):
        webapp._corpus_sync_job_start(None)
    assert webapp._corpus_sync_job_snapshot()["status"] == "failed"
    assert durable_jobs.load(webapp._CORPUS_SYNC_JOB_KIND, db_path=local_job)["status"] == "failed"
    monkeypatch.setattr(threading.Thread, "start", start)
    monkeypatch.setattr(webapp, "_corpus_sync_execute", lambda sources: {"imported_total": 0})
    webapp._corpus_sync_job_start(None)
    deadline = time.monotonic() + 3
    while webapp._corpus_sync_job_snapshot()["status"] == "running" and time.monotonic() < deadline:
        time.sleep(.01)
    assert webapp._corpus_sync_job_snapshot()["status"] == "done"


@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
def test_completed_wrapped_write_retains_its_receipt_when_stopped(tmp_path, interruption):
    runtime = runs.ConversationRuntime(runs.RunStore(tmp_path))
    entered, release = threading.Event(), threading.Event()
    artifact = tmp_path / "synthetic-citations.bib"
    later = []
    receipt = {"verb": "cite.export", "ok": True, "result": {"filename": artifact.name}}

    def node(state, *, runtime):
        entered.set()
        assert release.wait(3)
        artifact.write_text("synthetic citation", encoding="utf-8")
        return {"steps": [receipt], "plan": {"steps": [receipt]}}

    def execute(emit):
        wrapped = run_control.cancellable_node(node)
        result = wrapped({}, runtime=SimpleNamespace(context=SimpleNamespace(cancel_check=run_control.current_check())))
        later.append("must not execute")
        return {"ok": True, **result}

    try:
        run = runtime.submit("alice", interruption, "fp", prepare=lambda: execute)
        assert entered.wait(2)
        if interruption == "cancel":
            runtime.cancel("alice", run["id"])
        else:
            runtime.max_seconds = -1
        release.set()
        final = _terminal(runtime, run["id"])
        assert artifact.exists() and later == []
        assert final["status"] == ("cancelled" if interruption == "cancel" else "failed")
        assert artifact.name in json.dumps(final["result"].get("completed_result"))
    finally:
        release.set()
        runtime.close()


@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
def test_oversized_interrupted_result_still_finishes_and_releases_capacity(tmp_path, interruption):
    runtime = runs.ConversationRuntime(runs.RunStore(tmp_path))
    entered, release = threading.Event(), threading.Event()
    receipt = {"verb": "cite.export", "ok": True, "result": {"filename": "synthetic-kept.bib"}}

    def execute(emit):
        entered.set()
        assert release.wait(3)
        return {"ok": True, "plan": {"steps": [receipt]}, "large_body": "x" * (8 * 1024 * 1024 + 1)}

    try:
        run = runtime.submit("alice", interruption, "fp", prepare=lambda: execute)
        assert entered.wait(2)
        if interruption == "cancel":
            runtime.cancel("alice", run["id"])
        else:
            runtime.max_seconds = -1
        release.set()
        final = _terminal(runtime, run["id"])
        assert final["status"] == ("cancelled" if interruption == "cancel" else "failed")
        assert "synthetic-kept.bib" in json.dumps(final["result"])
        assert "run_result_too_large" in json.dumps(final["result"])
        runtime.max_seconds = 900
        next_run = runtime.submit("alice", "next", "fp-next", prepare=lambda: lambda emit: {"ok": True})
        assert _terminal(runtime, next_run["id"])["status"] == "done"
    finally:
        release.set()
        runtime.close()


@pytest.mark.parametrize("request_id", [" synthetic-spaced ", "x" * 100])
def test_original_request_id_resolves_to_the_same_receipt(tmp_path, monkeypatch, request_id):
    runtime = runs.ConversationRuntime(runs.RunStore(tmp_path))
    monkeypatch.setattr(webapp, "_CONVERSATION_RUNTIME", runtime)
    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(webapp, "_account_gate_required", lambda: True)
    monkeypatch.setattr(accounts, "resolve_session", lambda *a, **kw: accounts.PublicUser("alice", "alice"))
    calls = []
    monkeypatch.setattr(turn, "route_turn", lambda text, **kw: calls.append(True) or {"route": "none", "query": "", "plan": None, "retrieval": None, "via": "rules"})
    client = TestClient(webapp.app, base_url="http://127.0.0.1")
    try:
        submitted = client.post("/api/utterance", json={"utterance": "synthetic request", "req_id": request_id, "background": True})
        assert submitted.status_code == 202
        run_id = submitted.json()["run"]["id"]
        _terminal(runtime, run_id)
        resolved = client.post("/api/runs/resolve", json={"req_id": request_id})
        assert resolved.status_code == 200 and resolved.json()["run"]["id"] == run_id
        assert calls == [True]
    finally:
        runtime.close()


@pytest.mark.parametrize("later", ["expired", "error"])
def test_run_execution_keeps_the_admitted_identity_snapshot(tmp_path, monkeypatch, later):
    runtime = runs.ConversationRuntime(runs.RunStore(tmp_path))
    monkeypatch.setattr(webapp, "_CONVERSATION_RUNTIME", runtime)
    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(webapp, "_account_gate_required", lambda: True)
    parsed = []
    written = []

    def resolve(*args, **kwargs):
        parsed.append(True)
        if len(parsed) == 1:
            return accounts.PublicUser("alice", "alice")
        if later == "error":
            raise RuntimeError("synthetic later resolution failure")
        return None

    def route(text, **kwargs):
        from dataset_recommender.corpus.patch_package import bind_patch_scope, current_patch_scope
        with bind_patch_scope(kwargs["principal"]):
            written.append(current_patch_scope())
        return {"route": "none", "query": "", "plan": None, "retrieval": None, "via": "rules"}

    monkeypatch.setattr(accounts, "resolve_session", resolve)
    monkeypatch.setattr(turn, "route_turn", route)
    client = TestClient(webapp.app, base_url="http://127.0.0.1")
    try:
        response = client.post("/api/utterance", json={"utterance": "synthetic request"})
        assert response.status_code == 200 and parsed == [True] and written == ["alice"]
    finally:
        runtime.close()


@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
def test_completed_node_receipt_survives_stop_before_the_graph_event(tmp_path, interruption):
    runtime = runs.ConversationRuntime(runs.RunStore(tmp_path))
    completed, release = threading.Event(), threading.Event()
    receipt = {"verb": "cite.export", "ok": True, "result": {"filename": "synthetic-event-window.bib"}}

    def node(state, *, runtime):
        return {"steps": [receipt], "plan": {"steps": [receipt]}}

    def execute(emit):
        run_control.cancellable_node(node)({}, runtime=SimpleNamespace(context=SimpleNamespace(cancel_check=run_control.current_check())))
        completed.set()
        assert release.wait(3)
        emit("step", {"node": "execute", "ok": True})
        raise AssertionError("next work must not run")

    try:
        run = runtime.submit("alice", "event-window", "fp", prepare=lambda: execute)
        assert completed.wait(2)
        if interruption == "cancel":
            runtime.cancel("alice", run["id"])
        else:
            runtime.max_seconds = -1
        release.set()
        final = _terminal(runtime, run["id"])
        assert final["status"] == ("cancelled" if interruption == "cancel" else "failed")
        assert "synthetic-event-window.bib" in json.dumps(final["result"]["completed_result"])
    finally:
        release.set()
        runtime.close()


def test_oversized_event_is_disclosed_without_losing_the_terminal_receipt(tmp_path):
    runtime = runs.ConversationRuntime(runs.RunStore(tmp_path))
    def execute(emit):
        emit("step", {"result": {"filename": "synthetic-event.bib"}, "large": "x" * 256001})
        return {"ok": True, "filename": "synthetic-event.bib"}
    try:
        run = runtime.submit("alice", "large-event", "fp", prepare=lambda: execute)
        final = _terminal(runtime, run["id"])
        assert final["status"] == "done" and final["result"]["filename"] == "synthetic-event.bib"
        event = runtime.store.events("alice", run["id"])[0]["data"]
        assert event["event_truncated"] and "synthetic-event.bib" in json.dumps(event)
    finally:
        runtime.close()


def test_invalidation_callback_failure_does_not_advance_its_cursor(broker, monkeypatch):
    monkeypatch.setattr(webapp, "_corpus_sync_execute", lambda sources: {"imported_total": 0})
    rq_jobs.corpus_sync_entry(None)
    consumer = rq_jobs.InvalidationConsumer()
    def fail():
        raise RuntimeError("synthetic cache refresh failure")
    with pytest.raises(RuntimeError):
        consumer.consume(fail)
    calls = []
    assert consumer.consume(lambda: calls.append(True)) is True
    assert consumer.consume(lambda: calls.append(True)) is False
    assert calls == [True]


def test_unacknowledged_json_result_is_persistent_then_reclaimed(broker, monkeypatch):
    text, binary = broker
    queue_factory = rq.Queue
    monkeypatch.setattr(rq, "Queue", lambda *a, **kw: queue_factory(*a, **{**kw, "is_async": False}))
    monkeypatch.setattr(webapp, "_corpus_sync_execute", lambda sources: {"imported_total": 0, "operation_id": "synthetic-durable"})
    compare = rq_jobs._compare_hash

    def unavailable(expected, mapping):
        if mapping.get("status") in {"done", "failed"}:
            raise redis.ConnectionError("synthetic terminal mirror outage")
        return compare(expected, mapping)

    monkeypatch.setattr(rq_jobs, "_compare_hash", unavailable)
    with pytest.raises(rq_jobs.JobBackendError):
        rq_jobs.start(None)
    job_id = text.hget(rq_jobs._STATUS_KEY, rq_jobs._JOB_ID_FIELD)
    job = rq.job.Job.fetch(job_id, connection=binary, serializer=JSONSerializer)
    assert job.get_status().value == "finished"
    assert job.result_ttl == -1 and binary.ttl(job.key) == -1
    assert job.return_value(refresh=True)["status"] == "done"
    monkeypatch.setattr(rq_jobs, "_compare_hash", compare)
    recovered = rq_jobs.snapshot()
    assert recovered["status"] == "done" and recovered["result"]["operation_id"] == "synthetic-durable"
    assert binary.exists(job.key) == 0


def test_legacy_pickle_job_is_rejected_without_executing_or_loading_pickle(broker, monkeypatch):
    import pickle
    _, binary = broker
    old_queue = rq.Queue("synthetic-legacy", connection=binary)
    legacy = old_queue.enqueue("builtins.sum", [1, 2])
    loads = []

    def forbidden(*args, **kwargs):
        loads.append(True)
        raise AssertionError("pickle fallback must not run")

    monkeypatch.setattr(pickle, "loads", forbidden)
    monkeypatch.setattr(rq.serializers.DefaultSerializer, "loads", forbidden)
    with pytest.raises((rq.exceptions.DeserializationError, UnicodeDecodeError, json.JSONDecodeError)):
        rq.job.Job.fetch(legacy.id, connection=binary, serializer=rq_jobs.RQ_SERIALIZER).perform()
    assert loads == []


def test_unserializable_result_discloses_summary_and_finishes(tmp_path):
    runtime = runs.ConversationRuntime(runs.RunStore(tmp_path))
    try:
        run = runtime.submit("alice", "invalid-result", "fp", prepare=lambda: lambda emit: {
            "ok": True, "plan": {"steps": [{"ok": True, "result": {"filename": "synthetic-summary.bib"}}]},
            "unsupported": object()})
        final = _terminal(runtime, run["id"])
        assert final["status"] == "failed"
        assert final["result"]["result_error_code"] == "run_result_not_serializable"
        assert "synthetic-summary.bib" in json.dumps(final["result"])
    finally:
        runtime.close()


@pytest.mark.parametrize("request_id", [" synthetic-old-spaced ", "x" * 100])
def test_existing_http_request_key_restores_without_rewriting_the_database(tmp_path, monkeypatch, request_id):
    import hashlib
    canonical = request_id.strip()
    if len(canonical) > 64:
        canonical = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:48]
    legacy_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    store = runs.RunStore(tmp_path)
    old, _ = store.claim("alice", "synthetic-old-placeholder", "fp", boot_id="synthetic-old-boot")
    store.transition("alice", old["id"], "done", {"ok": True, "filename": "synthetic-existing.bib"})
    with store.database(write=True) as connection:
        connection.execute("UPDATE conversation_runs SET request_key=? WHERE owner=? AND id=?",
                           (legacy_key, "alice", old["id"]))
    runtime = runs.ConversationRuntime(store)
    monkeypatch.setattr(webapp, "_CONVERSATION_RUNTIME", runtime)
    monkeypatch.setattr(webapp, "_account_gate_required", lambda: True)
    monkeypatch.setattr(accounts, "resolve_session", lambda *a, **kw: accounts.PublicUser("alice", "alice"))
    client = TestClient(webapp.app, base_url="http://127.0.0.1")
    try:
        response = client.post("/api/runs/resolve", json={"req_id": request_id})
        assert response.status_code == 200 and response.json()["run"]["id"] == old["id"]
        assert response.json()["run"]["result"]["filename"] == "synthetic-existing.bib"
        with store.database() as connection:
            saved = connection.execute("SELECT request_key FROM conversation_runs WHERE owner=? AND id=?",
                                       ("alice", old["id"])).fetchone()[0]
        assert saved == legacy_key
    finally:
        runtime.close()
