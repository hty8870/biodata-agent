"""Bounded, owner-qualified execution, durable replay and truthful cancellation."""
import threading
import time

import pytest

from dataset_recommender.agent.run_control import RunCancelled, cancellable_node
from dataset_recommender.app.conversation_runs import ConversationRuntime, RunError, RunStore, TERMINAL


@pytest.fixture(autouse=True)
def isolate_run_database(monkeypatch, tmp_path):
    monkeypatch.setenv("BIODATA_RUNS_DB", str(tmp_path / "runs.sqlite"))


def completed(runtime, owner, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = runtime.store.load(owner, run_id)
        if row["status"] in TERMINAL:
            return row
        time.sleep(.01)
    raise AssertionError("synthetic task did not finish")


def test_completed_replay_survives_reopening_without_reexecuting(tmp_path):
    store = RunStore(tmp_path)
    calls = []
    runtime = ConversationRuntime(store)
    def prepare():
        calls.append("prepared")
        return lambda emit: {"ok": True, "value": "owned result"}
    run = runtime.submit("alice", "request1", "fp", prepare=prepare)
    assert completed(runtime, "alice", run["id"])["result"]["value"] == "owned result"
    runtime.close()
    reopened = ConversationRuntime(RunStore(tmp_path))
    replay = reopened.submit("alice", "request1", "fp", prepare=prepare)
    assert replay["id"] == run["id"] and replay["status"] == "done"
    assert calls == ["prepared"]
    reopened.close()


def test_owner_and_fingerprint_boundaries(tmp_path):
    runtime = ConversationRuntime(RunStore(tmp_path))
    prepare = lambda: (lambda emit: {"ok": True})
    first = runtime.submit("alice", "shared", "fp", prepare=prepare)
    second = runtime.submit("bob", "shared", "fp", prepare=prepare)
    assert first["id"] != second["id"]
    for operation in (lambda: runtime.store.load("bob", first["id"]),
                      lambda: runtime.store.events("bob", first["id"]),
                      lambda: runtime.cancel("bob", first["id"])):
        with pytest.raises(RunError) as exc:
            operation()
        assert exc.value.code == "run_not_found"
    with pytest.raises(RunError) as exc:
        runtime.submit("alice", "shared", "different", prepare=prepare)
    assert exc.value.code == "run_request_conflict"
    runtime.close()


def test_owner_capacity_does_not_block_another_owner(tmp_path):
    release, started = threading.Event(), threading.Event()
    runtime = ConversationRuntime(RunStore(tmp_path), workers=2, max_per_owner=1, max_inflight=2)
    def execute(emit):
        started.set(); assert release.wait(5); return {"ok": True}
    first = runtime.submit("alice", "a", "fp", prepare=lambda: execute)
    assert started.wait(2)
    with pytest.raises(RunError) as exc:
        runtime.submit("alice", "b", "fp", prepare=lambda: execute)
    assert exc.value.code == "run_capacity"
    other = runtime.submit("bob", "c", "fp", prepare=lambda: (lambda emit: {"ok": True}))
    assert completed(runtime, "bob", other["id"])["status"] == "done"
    release.set(); completed(runtime, "alice", first["id"]); runtime.close()


def test_running_cancellation_waits_for_a_safe_boundary(tmp_path):
    release, started = threading.Event(), threading.Event()
    runtime = ConversationRuntime(RunStore(tmp_path))
    def execute(emit):
        emit("step", {"known_completed": True})
        started.set(); assert release.wait(5)
        emit("step", {"must_not_be_added": True})
        return {"ok": True}
    run = runtime.submit("alice", "a", "fp", prepare=lambda: execute)
    assert started.wait(2)
    assert runtime.cancel("alice", run["id"])["status"] == "cancelling"
    release.set()
    result = completed(runtime, "alice", run["id"])
    assert result["status"] == "cancelled" and result["result"]["may_have_side_effects"]
    assert runtime.store.events("alice", run["id"])[0]["data"] == {"known_completed": True}
    assert len(runtime.store.events("alice", run["id"])) == 1
    runtime.close()


def test_queued_cancel_does_not_execute(tmp_path):
    release, started = threading.Event(), threading.Event(); calls = []
    runtime = ConversationRuntime(RunStore(tmp_path), workers=1, max_per_owner=2)
    def slow(emit):
        started.set(); assert release.wait(5); return {"ok": True}
    first = runtime.submit("alice", "a", "fp", prepare=lambda: slow)
    assert started.wait(2)
    second = runtime.submit("alice", "b", "fp", prepare=lambda: (lambda emit: calls.append("executed") or {"ok": True}))
    cancelled = runtime.cancel("alice", second["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["result"]["may_have_side_effects"] is False
    release.set(); completed(runtime, "alice", first["id"]); runtime.close()
    assert calls == []


def test_restart_reports_interruption_instead_of_replaying(tmp_path):
    store = RunStore(tmp_path)
    row, _ = store.claim("alice", "old", "fp", boot_id="old_process")
    runtime = ConversationRuntime(store)
    replay = runtime.submit("alice", "old", "fp", prepare=lambda: pytest.fail("interrupted write must not replay"))
    assert replay["id"] == row["id"] and replay["status"] == "interrupted"
    assert replay["result"]["may_have_side_effects"]
    runtime.close()


def test_events_and_request_receipts_exclude_credentials(tmp_path):
    runtime = ConversationRuntime(RunStore(tmp_path))
    fake = "sk" + "-" + "A" * 24
    def execute(emit):
        emit("step", {"api_key": fake, "note": fake})
        return {"ok": True, "authorization": fake}
    row = runtime.submit("alice", fake, "fp", prepare=lambda: execute,
                         request={"api_key": fake, "text": fake})
    final = completed(runtime, "alice", row["id"])
    assert "api_key" not in final["request"]
    assert fake not in str(final) and fake not in str(runtime.store.events("alice", row["id"]))
    assert runtime.cancel("alice", row["id"])["status"] == "done"
    runtime.close()


def test_graph_node_cancellation_prevents_the_node_call():
    from types import SimpleNamespace
    calls = []
    def check(): raise RunCancelled()
    wrapped = cancellable_node(lambda state, runtime: calls.append("ran"))
    with pytest.raises(RunCancelled):
        wrapped({}, runtime=SimpleNamespace(context=SimpleNamespace(cancel_check=check)))
    assert calls == []


def test_deadline_reports_timeout_and_retains_completed_event(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from dataset_recommender.app import conversation_runs

    clock = [0.0]
    runtime_time = SimpleNamespace(**vars(time))
    runtime_time.monotonic = lambda: clock[0]
    monkeypatch.setattr(conversation_runs, "time", runtime_time)
    runtime = ConversationRuntime(RunStore(tmp_path), max_seconds=.03)
    def execute(emit):
        emit("step", {"known_completed": True})
        clock[0] = .05
        emit("step", {"must_not_execute": True})
        return {"ok": True}
    try:
        row = runtime.submit("alice", "timeout", "fp", prepare=lambda: execute)
        final = completed(runtime, "alice", row["id"])
        assert final["status"] == "failed" and final["result"]["code"] == "run_timeout"
        assert runtime.store.events("alice", row["id"])[0]["data"] == {"known_completed": True}
        assert len(runtime.store.events("alice", row["id"])) == 1
    finally:
        runtime.close()

def test_failed_admission_releases_the_request_without_replaying_a_failed_receipt(tmp_path):
    from dataset_recommender.app.conversation_runs import RunError
    runtime = ConversationRuntime(RunStore(tmp_path), max_inflight=1, max_per_owner=1)
    def reject():
        raise ValueError("synthetic configuration rejection")
    for _ in range(2):
        with pytest.raises(ValueError):
            runtime.submit("alice", "retryable", "fp", prepare=reject)
        with pytest.raises(RunError) as missing:
            runtime.store.find_request("alice", "retryable")
        assert missing.value.code == "run_not_found"
    calls = []
    row = runtime.submit("alice", "retryable", "fp", prepare=lambda: lambda emit: calls.append("executed") or {"ok": True})
    assert completed(runtime, "alice", row["id"])["status"] == "done"
    replay = runtime.submit("alice", "retryable", "fp", prepare=reject)
    assert replay["id"] == row["id"] and calls == ["executed"]
    runtime.close()
