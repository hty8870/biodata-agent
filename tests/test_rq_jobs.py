# -*- coding: utf-8 -*-
"""RQ 消息队列后端（`rq_jobs`）的门。

用 fakeredis 顶掉共享单例（monkeypatch `redis_store._build_client` / `_build_rq_client`
两个 builder：状态 hash 走 decode 模式，RQ 队列走 bytes 模式——RQ job 数据是 zlib 二进制），
并用 `rq.Queue(..., is_async=False)` 把入队变成**同进程同步执行**——不需要真 worker，也就
不需要真的 Redis。两个 fake client 挂同一个 `FakeServer`，与生产同实例双连接一致。
覆盖：env 开关（含笔误 fail-closed）、快照形状与内存快照逐键一致、
start→done 往返、单飞附着、worker 侧异常收口、imported_total 决定是否重建向量、
重建失败如实 failed、跨进程失效旗标的置位/消费一次、Redis 故障如实 503 语义、
缺 rq 包时的惰性 import 报错。

注意：本文件的同步队列绕过 `Job.fetch()`，bytes 双通道已由夹具建模（队列连接是独立的
decode_responses=False fake），但真实 worker 进程时序不在本文件覆盖范围。
打桩 `webapp._corpus_sync_execute` 用 `raising=False`：它是 webapp 侧的
接线点，测试不该依赖它此刻已经存在。
"""
from __future__ import annotations

import sys
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fakeredis
import pytest
import redis
import rq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import dataset_recommender.app.webapp as webapp  # noqa: E402
from dataset_recommender.app import redis_store  # noqa: E402
from dataset_recommender.app import rq_jobs as R  # noqa: E402


@pytest.fixture()
def fake_redis(monkeypatch):
    """每个测试独立 fakeredis 服务（两个解码模式的 client 挂同一 FakeServer）；
    注入共享单例的两个 builder，测试结束复位（防跨测试串状态）。"""
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeStrictRedis(server=server, decode_responses=True)
    fake_bytes = fakeredis.FakeStrictRedis(server=server, decode_responses=False)
    monkeypatch.setattr(redis_store, "_build_client", lambda: fake)
    monkeypatch.setattr(redis_store, "_build_rq_client", lambda: fake_bytes)
    redis_store._reset_for_tests()
    yield fake
    redis_store._reset_for_tests()


@pytest.fixture()
def sync_queue(monkeypatch):
    """把 `rq.Queue` 换成同步队列：enqueue 即在本进程跑完执行体（无需 worker）。"""
    real_queue = rq.Queue

    def _make(name, connection=None, **kwargs):
        kwargs.setdefault("is_async", False)
        return real_queue(name, connection=connection, **kwargs)

    monkeypatch.setattr(rq, "Queue", _make)


def test_backend_active_default_rq_and_bad_value(monkeypatch):
    monkeypatch.delenv("BIODATA_JOB_BACKEND", raising=False)
    assert R.backend_active() is False
    monkeypatch.setenv("BIODATA_JOB_BACKEND", "   ")
    assert R.backend_active() is False
    monkeypatch.setenv("BIODATA_JOB_BACKEND", "RQ")
    assert R.backend_active() is True
    monkeypatch.setenv("BIODATA_JOB_BACKEND", "rabbit")
    with pytest.raises(R.JobBackendError) as ei:
        R.backend_active()
    assert ei.value.code == "bad_config"
    assert "BIODATA_JOB_BACKEND" in ei.value.message


def test_snapshot_idle_shape_matches_memory_job(fake_redis):
    snap = R.snapshot()
    assert snap == {"status": "idle", "started_at": None, "finished_at": None,
                    "result": None, "error": None}
    assert set(snap) == set(webapp._CORPUS_SYNC_JOB)


def test_start_runs_sync_and_roundtrips_result(fake_redis, sync_queue, monkeypatch):
    seen = []
    result = {"imported_total": 0, "sources": ["geo"], "detail": {"nested": [1, 2]}}
    monkeypatch.setattr(webapp, "_corpus_sync_execute",
                        lambda sources: seen.append(sources) or result, raising=False)
    monkeypatch.setattr(webapp, "_corpus_sync_rebuild_vectors", lambda: None)

    snap = R.start(["geo"])
    assert seen == [["geo"]]
    assert snap["status"] == "done"
    assert snap["result"] == result           # 紧凑 JSON 完整往返（含嵌套）
    assert snap["started_at"] and snap["finished_at"]
    assert snap["error"] is None
    assert set(snap) == set(webapp._CORPUS_SYNC_JOB)


def test_start_attaches_to_running_without_enqueue(fake_redis, monkeypatch):
    seen = []
    monkeypatch.setattr(webapp, "_corpus_sync_execute",
                        lambda sources: seen.append(sources) or {"imported_total": 0}, raising=False)
    original = R.start(["geo"])
    snap = R.start(["geo"])
    assert snap["status"] == "running"
    assert snap["started_at"] == original["started_at"]
    assert snap["finished_at"] is None
    assert seen == []                          # 没有新建任务：执行体一次都没被调


def test_concurrent_start_claims_once_and_uses_unique_job_ids(fake_redis, monkeypatch):
    barrier = threading.Barrier(2)
    compare = R._compare_hash
    def raced(expected, mapping):
        barrier.wait(3)
        return compare(expected, mapping)
    monkeypatch.setattr(R, "_compare_hash", raced)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(R.start, [["geo"], ["geo"]]))
    queue = rq.Queue(R._QUEUE_NAME, connection=redis_store.get_rq_client(), serializer=R.RQ_SERIALIZER)
    assert queue.count == 1
    first_id = queue.job_ids[0]
    assert all(s["status"] == "running" for s in outcomes)
    assert fake_redis.hget(R._STATUS_KEY, R._JOB_ID_FIELD) == first_id
    monkeypatch.setattr(R, "_compare_hash", compare)
    fake_redis.hset(R._STATUS_KEY, "status", "done")
    R.start(None)
    assert queue.count == 2 and len(set(queue.job_ids)) == 2


def test_entry_records_failure_and_swallows_exception(fake_redis, monkeypatch):
    def boom(sources):
        raise Exception("boom")

    monkeypatch.setattr(webapp, "_corpus_sync_execute", boom, raising=False)
    assert R.corpus_sync_entry(["geo"])["status"] == "failed"   # 返回真实终态，RQ 可用于补偿
    snap = R.snapshot()
    assert snap["status"] == "failed"
    assert "boom" in snap["error"]
    assert snap["finished_at"] is not None
    assert snap["result"] is None


@pytest.mark.parametrize("imported_total,expect_rebuild", [(3, True), (0, False)])
def test_rebuild_only_when_imported_total_positive(
        fake_redis, monkeypatch, imported_total, expect_rebuild):
    rebuilds = []
    monkeypatch.setattr(webapp, "_corpus_sync_execute",
                        lambda sources: {"imported_total": imported_total}, raising=False)
    monkeypatch.setattr(webapp, "_corpus_sync_rebuild_vectors",
                        lambda: rebuilds.append(1) or None)

    R.corpus_sync_entry(None)
    assert bool(rebuilds) is expect_rebuild
    assert R.snapshot()["status"] == "done"


def test_rebuild_failure_marks_failed_with_error(fake_redis, monkeypatch):
    monkeypatch.setattr(webapp, "_corpus_sync_execute",
                        lambda sources: {"imported_total": 1}, raising=False)
    monkeypatch.setattr(webapp, "_corpus_sync_rebuild_vectors",
                        lambda: "语料向量重建失败（退出码 1）")

    R.corpus_sync_entry(None)
    snap = R.snapshot()
    assert snap["status"] == "failed"
    assert snap["error"] == "语料向量重建失败（退出码 1）"
    assert snap["result"] == {"imported_total": 1}


def test_web_invalidate_flag_consumed_once(fake_redis, monkeypatch):
    monkeypatch.setattr(webapp, "_corpus_sync_execute",
                        lambda sources: {"imported_total": 0}, raising=False)
    monkeypatch.setattr(webapp, "_corpus_sync_rebuild_vectors", lambda: None)

    R.corpus_sync_entry(None)
    assert fake_redis.hget(R._STATUS_KEY, "needs_web_invalidate") == "1"
    assert R.consume_web_invalidate_flag() is True
    assert fake_redis.hget(R._STATUS_KEY, "needs_web_invalidate") == "1"
    assert fake_redis.hget(R._STATUS_KEY, R._GENERATION_FIELD)
    assert R.consume_web_invalidate_flag() is False


def test_snapshot_translates_redis_error(fake_redis, monkeypatch):
    def down(*args, **kwargs):
        raise redis.RedisError("connection down")

    monkeypatch.setattr(fake_redis, "hgetall", down)
    with pytest.raises(R.JobBackendError) as ei:
        R.snapshot()
    assert ei.value.code == "job_store_unavailable"


def test_start_without_rq_package(fake_redis, monkeypatch):
    monkeypatch.setitem(sys.modules, "rq", None)   # `import rq` → ImportError
    with pytest.raises(R.JobBackendError) as ei:
        R.start(None)
    assert ei.value.code == "job_backend_unavailable"
    assert "requirements-mq" in ei.value.message


@pytest.mark.parametrize("state", ["missing", "failed", "stopped"])
def test_snapshot_reconciles_orphaned_running_job(fake_redis, state):
    R.start(None)
    job_id = fake_redis.hget(R._STATUS_KEY, R._JOB_ID_FIELD)
    job = rq.job.Job.fetch(job_id, connection=redis_store.get_rq_client(), serializer=R.RQ_SERIALIZER)
    if state == "missing":
        job.delete()
        # 尚在入队宽限期时不误判；过期才收敛为中断。
        assert R.snapshot()["status"] == "running"
        fake_redis.hset(R._STATUS_KEY, R._CLAIMED_AT_FIELD, "0")
    else:
        job.set_status(state)
    snapshot = R.snapshot()
    assert snapshot["status"] == "failed" and "中断或缺失" in snapshot["error"]
    assert fake_redis.hget(R._STATUS_KEY, R._FLAG_FIELD) == "1"


@pytest.mark.parametrize("failed", [False, True])
def test_rq_result_repairs_lost_terminal_hash_write(fake_redis, sync_queue, monkeypatch, failed):
    def execute(sources):
        if failed:
            raise ValueError("synthetic sync failure")
        return {"imported_total": 0}
    monkeypatch.setattr(webapp, "_corpus_sync_execute", execute)
    compare = R._compare_hash
    failures = []
    def fail_first_terminal(expected, mapping):
        if mapping.get("status") in ("done", "failed") and not failures:
            failures.append(1)
            raise redis.ConnectionError("one lost terminal write")
        return compare(expected, mapping)
    monkeypatch.setattr(R, "_compare_hash", fail_first_terminal)
    snap = R.start(None)
    assert failures == [1]
    assert snap["status"] == ("failed" if failed else "done")
    assert snap["result"] == (None if failed else {"imported_total": 0})
    assert fake_redis.hget(R._STATUS_KEY, "status") == snap["status"]


def test_superseded_queued_job_does_not_execute_or_overwrite_new_owner(fake_redis, monkeypatch):
    R.start(None)
    old_id = fake_redis.hget(R._STATUS_KEY, R._JOB_ID_FIELD)
    old = rq.job.Job.fetch(old_id, connection=redis_store.get_rq_client(), serializer=R.RQ_SERIALIZER)
    fake_redis.hset(R._STATUS_KEY, "status", "failed")
    R.start(None)
    new_id = fake_redis.hget(R._STATUS_KEY, R._JOB_ID_FIELD)
    monkeypatch.setattr(webapp, "_corpus_sync_execute", lambda *a: pytest.fail("旧任务不得执行"))
    assert old.perform() is None
    assert R.snapshot()["status"] == "running"
    assert fake_redis.hget(R._STATUS_KEY, R._JOB_ID_FIELD) == new_id != old_id


def test_started_job_without_heartbeat_eventually_reconciles(fake_redis):
    R.start(None)
    job = rq.job.Job.fetch(fake_redis.hget(R._STATUS_KEY, R._JOB_ID_FIELD),
                           connection=redis_store.get_rq_client(), serializer=R.RQ_SERIALIZER)
    job.set_status("started")
    job.started_at = datetime.datetime.now(datetime.timezone.utc)
    job.last_heartbeat = job.started_at
    job.save()
    assert R.snapshot()["status"] == "running"
    job.last_heartbeat -= datetime.timedelta(seconds=job.timeout + 61)
    job.save()
    assert R.snapshot()["status"] == "failed"


def test_enqueued_timeout_covers_shared_vector_budget(fake_redis):
    from dataset_recommender.app import limits
    R.start(None)
    job = rq.job.Job.fetch(fake_redis.hget(R._STATUS_KEY, R._JOB_ID_FIELD),
                           connection=redis_store.get_rq_client(), serializer=R.RQ_SERIALIZER)
    assert job.timeout == limits.CORPUS_SYNC_JOB_TIMEOUT_SECONDS
    assert job.timeout > limits.CORPUS_SYNC_VECTOR_TIMEOUT_SECONDS > rq.Queue.DEFAULT_TIMEOUT
    assert webapp._CORPUS_SYNC_VECTOR_TIMEOUT_S == limits.CORPUS_SYNC_VECTOR_TIMEOUT_SECONDS
