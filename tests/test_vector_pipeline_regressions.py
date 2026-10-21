# -*- coding: utf-8 -*-
"""管线回归：全隔离——
假嵌入器/假 worker 请求/假下载源，不加载真实模型、不联网、不碰真实 models。

钉死：嵌入器冷并发单飞；子范围视图不 prune/不写档；字节预算切块与无 id 失败 fail-fast；
兜底复用统一嵌入器；worker 下载 temporary+校验+atomic publish+完成标记；legacy manifest 不 recert READY；
训练侧 query representation 与生产同形、candidate_text 唯一真源；LTR 探针在候选作用域内取证。
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dataset_recommender.retrieval import model_runtime as mr  # noqa: E402
from dataset_recommender.retrieval import model_worker as mw  # noqa: E402
from dataset_recommender.retrieval import vector_index as vi  # noqa: E402
from dataset_recommender.retrieval import vector_recall as vr  # noqa: E402
from dataset_recommender.retrieval.candidate_text import candidate_text as true_candidate_text  # noqa: E402
from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402


def _rec(name: str, uid: str = "") -> DatasetRecord:
    return DatasetRecord(
        dataset_name=name, species="human", tissue="breast", disease="breast cancer",
        chemistry="", count="", unit="", has_raw_data=True, url="", source_file="x.json",
        description=f"desc of {name}", raw={"dataset_uid": uid} if uid else {},
    )


class _StubEmbedder:
    def __init__(self, dims: int = vi.EMBED_DIMENSIONS):
        self.dims = dims

    def __call__(self, texts):
        out = []
        for t in texts:
            digest = hashlib.sha256(str(t).encode("utf-8")).digest()
            out.append([digest[i % len(digest)] / 255.0 for i in range(self.dims)])
        return out


@pytest.fixture()
def idx_file(tmp_path, monkeypatch):
    path = tmp_path / "vector_index" / "corpus_vectors.test.json.gz"
    monkeypatch.setattr(vi, "index_path", lambda paths=None: path)
    vi.reset_caches_for_test()
    yield path
    vi.reset_caches_for_test()


# ---------------------------------------------------------------- 嵌入器冷并发单飞

def test_local_embedder_is_single_flight_under_cold_concurrency(monkeypatch):
    vi.reset_caches_for_test()
    built: list = []
    barrier = threading.Barrier(4)
    real_embedder = _StubEmbedder()

    def slow_load():
        built.append(1)
        time.sleep(0.05)                 # 冷加载窗口：并发者应等锁而不是各建一份
        return real_embedder

    monkeypatch.setattr(vi, "load_embedder", slow_load)
    outs: list = []

    def worker():
        barrier.wait(2)
        outs.append(vi.get_embedder())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(built) == 1                       # 工厂仅一次
    assert len(outs) == 4 and len({id(o) for o in outs}) == 1   # 四路复用同一客户端
    vi.reset_caches_for_test()


def test_embedder_failure_is_not_cached(monkeypatch):
    vi.reset_caches_for_test()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return _StubEmbedder() if calls["n"] > 1 else None

    monkeypatch.setattr(vi, "load_embedder", flaky)
    assert vi.get_embedder() is None             # 失败不缓存
    assert vi.get_embedder() is not None         # 稍后可用即可生效
    vi.reset_caches_for_test()


# ---------------------------------------------------------------- 子范围视图不 prune

def test_view_scope_does_not_prune_or_write_index(idx_file):
    enc = _StubEmbedder()
    vi.ensure_index([_rec("A", "uid-a"), _rec("B", "uid-b")], embedder=enc)
    before = idx_file.read_bytes()

    view = vi.ensure_index([_rec("A", "uid-a")], embedder=enc, scope="view")

    assert {"uid-a", "uid-b"} <= set(view)       # 视图含索引里其它 uid
    assert idx_file.read_bytes() == before       # 未写档（子集不是 prune 依据）
    with gzip.open(idx_file, "rt", encoding="utf-8") as fh:
        assert "uid-b" in json.load(fh)["vectors"]


def test_full_scope_still_prunes_removed_uid(idx_file):
    enc = _StubEmbedder()
    vi.ensure_index([_rec("A", "uid-a"), _rec("B", "uid-b")], embedder=enc)
    store = vi.ensure_index([_rec("A", "uid-a")], embedder=enc)
    assert "uid-b" not in store                  # 全库维护路径语义不变


# ---------------------------------------------------------------- 字节预算 + fail-fast

def test_external_embedder_chunks_by_byte_budget(monkeypatch):
    emb = mr.ExternalEmbedder()
    calls: list[dict] = []

    def fake_roundtrip(request):
        calls.append(request)
        return {"id": request["id"], "ok": True,
                "vectors": [[0.0] * vi.EMBED_DIMENSIONS for _ in request["texts"]]}

    monkeypatch.setattr(emb, "_roundtrip", fake_roundtrip)
    big = "汉" * 8000
    out = emb([big] * 512)                       # 条数合法（≤512）但总字节 ≈12.3 MB
    assert len(out) == 512
    assert len(calls) > 1                        # 已按同一字节预算切块
    for request in calls:
        assert request["id"]                       # 每块都带相关 ID
        assert len(json.dumps(request, ensure_ascii=False).encode("utf-8")) <= mw.MAX_REQUEST_BYTES


def test_worker_request_rejection_fails_fast_without_waiting_timeout(monkeypatch):
    emb = mr.ExternalEmbedder()
    monkeypatch.setattr(emb, "_start", lambda: None)
    emb.process = SimpleNamespace(stdin=io.StringIO(), poll=lambda: None)
    emb.responses.put({"id": "", "ok": False, "error": "request_too_large"})
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="request_rejected"):
        emb._roundtrip({"id": "abc123", "texts": ["x"]})
    assert time.monotonic() - started < 5        # 不空等 SCORE_TIMEOUT_S


def test_oversized_single_item_fails_before_sending():
    with pytest.raises(ValueError, match="too_large"):
        mr._split_by_budget(["汉" * 5_000_000])


# ---------------------------------------------------------------- 兜底复用统一嵌入器

def test_semantic_fallback_uses_unified_embedder(monkeypatch):
    used: list = []

    monkeypatch.setattr(vr, "load_embedder", lambda *_a, **_k: None)   # 模拟冻结包：主进程无模型
    monkeypatch.setattr(vi, "get_embedder",
                        lambda: (used.append(True), _StubEmbedder())[1])
    monkeypatch.setattr(vi, "ensure_index", lambda *a, **k: {})
    assert vr.semantic_fallback_recall([], "查询") is None
    assert used == [True]


# ---------------------------------------------------------------- 完成证据与原子发布

def _fake_download(monkeypatch, *, payload: "dict[str, bytes]"):
    """把 modelscope.snapshot_download 换成「写到一个假源目录」的替身（不联网）。"""
    source = Path(tempfile_dir()) / "fake-source"
    source.mkdir(parents=True, exist_ok=True)
    for name, blob in payload.items():
        (source / name).write_bytes(blob)

    module = SimpleNamespace(snapshot_download=lambda *_a, **_k: str(source))
    monkeypatch.setitem(sys.modules, "modelscope", module)
    return source


_TMP_HOLDER: dict = {}


def tempfile_dir() -> str:
    if "dir" not in _TMP_HOLDER:
        import tempfile
        _TMP_HOLDER["dir"] = tempfile.mkdtemp(prefix="b05b-worker-")
    return _TMP_HOLDER["dir"]


def test_download_model_publishes_atomically_with_completion_marker(monkeypatch, tmp_path):
    target = tmp_path / "model"
    _fake_download(monkeypatch, payload={"config.json": b"{}", "tokenizer.json": b"{}",
                                         "model.safetensors": b"weights"})
    assert mw.download_model(target, model_id="fake/model") is True
    assert (target / "model.safetensors").read_bytes() == b"weights"
    assert mw._completion_valid(target)                       # 完成标记与实物一致
    assert not (tmp_path / "model.incoming").exists()         # staging 已清


def test_truncated_weights_without_marker_are_not_treated_as_ready(monkeypatch, tmp_path):
    target = tmp_path / "model"
    target.mkdir(parents=True)
    (target / "config.json").write_text("{}", encoding="utf-8")
    (target / "tokenizer.json").write_text("{}", encoding="utf-8")
    (target / "model.safetensors").write_bytes(b"x")          # 截断残留、非空
    assert mw.model_files_ready(target)                       # 旧口径会认为「就绪」
    assert mw._completion_valid(target) is False              # 新口径：无完成证据

    _fake_download(monkeypatch, payload={"config.json": b"{}", "tokenizer.json": b"{}",
                                         "model.safetensors": b"weights"})
    assert mw.download_model(target, model_id="fake/model") is True
    assert (target / "model.safetensors").read_bytes() == b"weights"   # 已重下载修复
    assert mw._completion_valid(target)


def test_tampered_weight_invalidates_completion(monkeypatch, tmp_path):
    target = tmp_path / "model"
    _fake_download(monkeypatch, payload={"config.json": b"{}", "tokenizer.json": b"{}",
                                         "model.safetensors": b"weights"})
    assert mw.download_model(target, model_id="fake/model") is True
    (target / "model.safetensors").write_bytes(b"corrupt-bytes")  # 事后被改（尺寸也不同）
    assert mw._completion_valid(target) is False


def test_legacy_manifest_without_size_list_is_not_ready(monkeypatch, tmp_path):
    from dataset_recommender.app.runtime_paths import AppPaths
    paths = AppPaths(
        install_root=tmp_path, resource_root=tmp_path, data_root=tmp_path,
        config_root=tmp_path, shipped_base_dir=tmp_path, shipped_external_dir=tmp_path,
        user_external_dir=tmp_path, userdata_dir=tmp_path / ".userdata",
        model_root=tmp_path / "models", log_root=tmp_path / "logs",
        trace_root=tmp_path / "trace", export_root=tmp_path / "exports",
        run_root=tmp_path / "run", runtime_mode="frozen",
    )
    cross = mr.model_dir(paths)
    cross.mkdir(parents=True)
    (cross / "config.json").write_text("{}", encoding="utf-8")
    (cross / "tokenizer.json").write_text("{}", encoding="utf-8")
    (cross / "model.safetensors").write_bytes(b"weights")
    manifest = {"schema": mr.READY_SCHEMA, "model_id": mw.MODEL_ID, "runtime_bytes": 1,
                "model_bytes": 7}                              # 旧版：没有 model_files 尺寸清单
    mr.ready_manifest_path(paths).parent.mkdir(parents=True, exist_ok=True)
    mr.ready_manifest_path(paths).write_text(json.dumps(manifest), encoding="utf-8")
    assert mr._manifest_files_intact(manifest, paths) is False     # 不再存在性认证


# ---------------------------------------------------------------- 训练-产线同形与唯一真源

def test_augment_uses_production_query_text_and_true_candidate_text():
    import augment_vec_scores as av

    row = {"query": "人类肺癌数据", "query_nl": "（改写）human lung cancer", "rewrite_status": "ok"}
    assert av.production_query_text(row) == "人类肺癌数据"       # 原句，不扩写（与生产 dense 路径同形）
    assert av.candidate_text is true_candidate_text            # 本地副本已退役

    import harvest_ood_candidates as hv
    assert hv.candidate_text is true_candidate_text            # 消费者同样指向唯一真源


def test_production_dense_path_embeds_raw_query(monkeypatch):
    """产线取证：dense/vector 注入嵌入器路径嵌入的是原句（parity 的另一半）。"""
    recorded: list = []

    class _Recorder:
        def __call__(self, texts):
            recorded.extend(texts)
            return [[0.1] * vi.EMBED_DIMENSIONS for _ in texts]

    from dataset_recommender.retrieval.query_parser import parse_query
    from dataset_recommender.retrieval.retriever import RetrievedCandidate

    intent = parse_query("人类肺癌数据")
    cands = [RetrievedCandidate(record=_rec("A", "uid-a"), score=1.0)]
    vr.recall_rerank("人类肺癌数据", cands, backend="vector", embedder=_Recorder(), intent=intent)
    assert recorded and recorded[0] == "人类肺癌数据"


def test_warm_view_does_not_wait_for_cold_build(idx_file):
    a, b = _rec("A", "a"), _rec("B", "b")
    vi.ensure_index([a], embedder=_StubEmbedder())
    entered, release, warm_done = threading.Event(), threading.Event(), threading.Event()
    outcomes = []
    def slow(texts):
        entered.set()
        assert release.wait(5)
        return _StubEmbedder()(texts)
    cold = threading.Thread(target=lambda: vi.ensure_index([a, b], embedder=slow))
    warm = threading.Thread(target=lambda: (outcomes.append(vi.ensure_index([a], embedder=_StubEmbedder(), scope="view")), warm_done.set()))
    cold.start()
    try:
        assert entered.wait(2)
        warm.start()
        assert warm_done.wait(.5), "warm snapshot reader waited behind encode"
        assert outcomes[0]["a"]["h"]
    finally:
        release.set()
        cold.join(5)
        if warm.ident:
            warm.join(5)


def test_old_build_cannot_overwrite_newer_scope(idx_file):
    entered, release = threading.Event(), threading.Event()
    old = [_rec("old", "old")]
    new = [_rec("new", "new")]
    out = []
    def slow(texts):
        entered.set()
        assert release.wait(5)
        return _StubEmbedder()(texts)
    worker = threading.Thread(target=lambda: out.append(vi.ensure_index(old, embedder=slow)))
    worker.start()
    try:
        assert entered.wait(2)
        # register the newer full request without waiting indefinitely for the old build
        newest = threading.Thread(target=lambda: vi.ensure_index(new, embedder=_StubEmbedder()))
        newest.start()
        time.sleep(.05)
    finally:
        release.set()
        worker.join(5)
        newest.join(5)
    with gzip.open(idx_file, "rt", encoding="utf-8") as handle:
        assert set(json.load(handle)["vectors"]) == {"new"}
    assert out == [None]


def test_disk_replacement_during_build_is_not_overwritten(idx_file):
    entered, release = threading.Event(), threading.Event()
    out = []
    def slow(texts):
        entered.set()
        assert release.wait(5)
        return _StubEmbedder()(texts)
    thread = threading.Thread(target=lambda: out.append(vi.ensure_index([_rec("A", "a")], embedder=slow)))
    thread.start()
    try:
        assert entered.wait(2)
        vi._write_index(idx_file, {"external": {"h": "external", "v": [1.0] * vi.EMBED_DIMENSIONS}})
        before = idx_file.read_bytes()
    finally:
        release.set()
        thread.join(5)
    assert idx_file.read_bytes() == before and out == [None]


def test_same_size_weight_corruption_invalidates_both_evidence_consumers(tmp_path):
    from dataset_recommender.app import model_installer as installer
    target = tmp_path / "weights"
    target.mkdir()
    for name, blob in {"config.json": b"{}", "tokenizer.json": b"{}", "model.safetensors": b"weights"}.items():
        (target / name).write_bytes(blob)
    mw._write_completion_marker(target)
    evidence = mw.model_file_manifest(target)
    assert installer._weights_trusted(target, {"model_files": evidence}, "model_files")
    (target / "model.safetensors").write_bytes(b"damaged")
    assert not mw._completion_valid(target)
    assert not installer._weights_trusted(target, {"model_files": evidence}, "model_files")
    assert not mr._files_intact(evidence, target)


def _record_archive_history(lp, archived):
    lp.write_json_atomic(archived / "_archive.json", {
        "artifact_sha256": lp.file_sha_map([archived / name for name in lp.ARTIFACT_NAMES], archived),
    })


def _exception_causes(exception):
    while exception is not None:
        yield exception
        exception = exception.__cause__


@pytest.mark.parametrize("failpoint", ["ltr_mlp.pt", "ltr_spec.json", "registry", "journal", "archive-weights", "archive-spec", "archive-meta"])
def test_rollback_restores_all_transaction_bytes_after_failure(tmp_path, monkeypatch, failpoint):
    import ltr_promote as lp
    paths = lp.Paths(tmp_path)
    paths.ltr_dir.mkdir(parents=True)
    archived = paths.archive_dir / "old"
    archived.mkdir(parents=True)
    for directory, marker in ((paths.ltr_dir, "current"), (archived, "old")):
        (directory / lp.WEIGHTS_NAME).write_bytes(marker.encode())
        (directory / lp.SPEC_NAME).write_text(json.dumps({"trained_at": marker, "train_counts": {}}), encoding="utf-8")
    _record_archive_history(lp, archived)
    lp.save_registry(paths.registry_path, {"schema_version": 1, "active": "current", "versions": [
        {"version": "current", "status": "active"}, {"version": "old", "status": "archived"}]})
    paths.journal_path.parent.mkdir()
    paths.journal_path.write_bytes(b'{"existing":true}\n')
    targets = [paths.ltr_dir / name for name in lp.ARTIFACT_NAMES] + [paths.registry_path, paths.journal_path]
    before = {path: path.read_bytes() for path in targets}
    failed = []
    primary = OSError("synthetic post-write fault: " + failpoint)
    if failpoint in lp.ARTIFACT_NAMES or failpoint.startswith("archive-"):
        write = lp.atomic_write_bytes
        archive_names = {"archive-weights": lp.WEIGHTS_NAME, "archive-spec": lp.SPEC_NAME, "archive-meta": "_archive.json"}
        target = paths.archive_dir / "current" / archive_names[failpoint] if failpoint in archive_names else paths.ltr_dir / failpoint
        def fault(path, raw):
            result = write(path, raw)
            if Path(path) == target and not failed:
                failed.append(True)
                raise primary
            return result
        monkeypatch.setattr(lp, "atomic_write_bytes", fault)
    else:
        name = "save_registry" if failpoint == "registry" else "append_journal"
        real = getattr(lp, name)
        def fault(*args, **kwargs):
            real(*args, **kwargs)
            failed.append(True)
            raise primary
        monkeypatch.setattr(lp, name, fault)
    with pytest.raises((lp.PromoteError, OSError)) as caught:
        lp.cmd_rollback(SimpleNamespace(project_root=tmp_path, version="old"))
    assert failed == [True]
    assert primary in list(_exception_causes(caught.value))
    assert {path: path.read_bytes() for path in targets} == before


@pytest.mark.parametrize("change", ["model", "corpus", "records"])
def test_changed_build_generation_never_commits(idx_file, monkeypatch, change):
    from dataset_recommender.corpus import corpus
    records = [_rec("A", "a")]
    entered, release = threading.Event(), threading.Event()
    outcomes = []
    def slow(texts):
        entered.set()
        assert release.wait(5)
        return _StubEmbedder()(texts)
    thread = threading.Thread(target=lambda: outcomes.append(vi.ensure_index(records, embedder=slow)))
    thread.start()
    try:
        assert entered.wait(2)
        if change == "model":
            monkeypatch.setattr(vi, "DEFAULT_EMBEDDING_MODEL", "synthetic-new-generation")
        elif change == "corpus":
            corpus.invalidate_external_cache()
        else:
            records[0].description = "changed while building"
    finally:
        release.set()
        thread.join(5)
    assert outcomes == [None] and not idx_file.exists()


def test_per_path_build_single_flight_and_late_batch_failure(idx_file, monkeypatch):
    monkeypatch.setattr(vi, "MAX_TEXTS", 1)
    entered, release = threading.Event(), threading.Event()
    calls, results = [], []
    records = [_rec("A", "a"), _rec("B", "b")]
    def slow(texts):
        calls.append(list(texts))
        entered.set()
        assert release.wait(5)
        return _StubEmbedder()(texts)
    threads = [threading.Thread(target=lambda: results.append(vi.ensure_index(records, embedder=slow))) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert entered.wait(2)
    release.set()
    for thread in threads:
        thread.join(5)
    assert len(calls) == 2 and len(results) == 2 and all(set(result) == {"a", "b"} for result in results)
    before = idx_file.read_bytes()
    fail_calls = []
    def later_failure(texts):
        fail_calls.append(True)
        return _StubEmbedder()(texts) if len(fail_calls) == 1 else []
    assert vi.ensure_index([_rec("C", "c"), _rec("D", "d")], embedder=later_failure) is None
    assert idx_file.read_bytes() == before


def test_batch_cosine_is_bitwise_legacy_and_never_caches_mutable_norms(monkeypatch):
    import math
    import random
    import struct
    randomizer = random.Random(781)
    query = [randomizer.uniform(-1, 1) for _ in range(384)]
    docs = [[randomizer.uniform(-1, 1) for _ in range(384)] for _ in range(31)] + [[0.0] * 384]
    def legacy(document):
        dot = sum(x * y for x, y in zip(query, document))
        na = math.sqrt(sum(x * x for x in query))
        nb = math.sqrt(sum(y * y for y in document))
        return 0.0 if na <= 0 or nb <= 0 else dot / (na * nb)
    expected = [legacy(document) for document in docs]
    snapshots = [vr.VectorSnapshot(document) for document in docs]
    valid = vr.valid_vector
    queried = []
    def checked(values, dimensions=None):
        if values is query:
            queried.append(True)
        return valid(values, dimensions)
    monkeypatch.setattr(vr, "valid_vector", checked)
    actual = vr.batch_cosine(query, snapshots)
    assert queried == [True]
    assert [struct.pack("d", score) for score in actual] == [struct.pack("d", score) for score in expected]
    with pytest.raises(AttributeError):
        snapshots[0].norm = 123.0
    assert not hasattr(snapshots[0], "__dict__")
    first = vr.batch_cosine(query, docs)
    docs[0][0] += 1
    assert vr.batch_cosine(query, docs)[0] != first[0]
    with pytest.raises(ValueError):
        vr.batch_cosine(query, [[float("nan")] * 384])
    assert vr.batch_cosine(query, [[1.0]], failsoft=True) == [None]


def _warm_verify_sha_rehash_budget(target):
    """暖验证的精确 SHA 重读预算：逐文件用真实 _weight_sig 接缝量化。

    暖缓存命中仍会对每个文件调用一次 _weight_sig：Windows 上有可信 USN token
    时零重读；token 缺失时生产逻辑退化为 ("sha256", …) 完整重读——每个对应文件
    恰好一次 _weight_sha。非 Windows 的 ctime 分支不携带 token、天然零重读。
    断言按此预算精确匹配，不允许 >= 或任意次数的松弛。
    """
    budget = 0
    for path in mw._model_files(target).values():
        signature = mw._weight_sig(path)
        if len(signature) > 5 and signature[-1][0] == "sha256":
            budget += 1
    return budget


def test_weight_sha_validation_cache_detects_same_size_changes(tmp_path, monkeypatch):
    target = tmp_path / "healthy"
    target.mkdir()
    for name, raw in {"config.json": b"{}", "tokenizer.json": b"{}", "model.safetensors": b"weights"}.items():
        (target / name).write_bytes(raw)
    evidence = mw.model_file_manifest(target)
    budget = _warm_verify_sha_rehash_budget(target)
    original = mw._weight_sha
    calls = []
    def hash_file(path):
        calls.append(Path(path).name)
        return original(path)
    monkeypatch.setattr(mw, "_weight_sha", hash_file)
    assert mw.verify_model_manifest(target, evidence)
    count = len(calls)
    assert mw.verify_model_manifest(target, evidence) and len(calls) == count + budget
    (target / "model.safetensors").write_bytes(b"damaged")
    assert not mw.verify_model_manifest(target, evidence)
    assert not mw.verify_model_manifest(target, {"config.json": 2, "tokenizer.json": 2, "model.safetensors": 7})


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file-change token seam")
def test_windows_forced_no_token_warm_verify_rehashes_every_file(tmp_path, monkeypatch):
    """OS token 接缝全缺失时，暖验证必须对每个对应文件完整重读一次 SHA——
    钉死逐文件精确次数，不把退化当“性能绿”。"""
    monkeypatch.setattr(mw, "_windows_file_change_token", lambda path: None)
    target = tmp_path / "healthy"
    target.mkdir()
    for name, raw in {"config.json": b"{}", "tokenizer.json": b"{}", "model.safetensors": b"weights"}.items():
        (target / name).write_bytes(raw)
    evidence = mw.model_file_manifest(target)
    budget = _warm_verify_sha_rehash_budget(target)
    assert budget == len(evidence)
    original = mw._weight_sha
    calls = []
    def hash_file(path):
        calls.append(Path(path).name)
        return original(path)
    monkeypatch.setattr(mw, "_weight_sha", hash_file)
    assert mw.verify_model_manifest(target, evidence)
    count = len(calls)
    assert mw.verify_model_manifest(target, evidence)
    assert len(calls) == count + len(evidence)
    (target / "model.safetensors").write_bytes(b"damaged")
    assert not mw.verify_model_manifest(target, evidence)
    assert not mw.verify_model_manifest(target, {"config.json": 2, "tokenizer.json": 2, "model.safetensors": 7})


def test_cross_process_publication_lock_is_enforced(idx_file):
    from dataset_recommender.corpus.fs_utils import acquire_file_lock, release_file_lock
    handle = acquire_file_lock(idx_file.with_name(idx_file.name + ".lock"), wait=False)
    try:
        assert vi.ensure_index([_rec("A", "a")], embedder=_StubEmbedder()) is None
        assert not idx_file.exists()
    finally:
        release_file_lock(handle)
    assert vi.ensure_index([_rec("A", "a")], embedder=_StubEmbedder())


def test_failed_downloader_preserves_untrusted_directory_without_certifying_it(tmp_path, monkeypatch):
    from dataset_recommender.app import model_installer
    target = tmp_path / "download"
    target.mkdir()
    for name, raw in {"config.json": b"{}", "tokenizer.json": b"{}", "model.safetensors": b"weights", "notes.txt": b"keep"}.items():
        (target / name).write_bytes(raw)
    mw._write_completion_marker(target)
    evidence = mw.model_file_manifest(target)
    (target / "model.safetensors").write_bytes(b"damaged")
    model_installer._discard_untrusted_weights(target, {"model_files": evidence}, "model_files")
    before = {path.name: path.read_bytes() for path in target.iterdir()}
    monkeypatch.setattr(mw, "_download_via_mirrors", lambda *a, **kw: False)
    assert not mw.download_model(target)
    assert {path.name: path.read_bytes() for path in target.iterdir()} == before
    assert not mw._completion_valid(target)
    assert not target.with_name(target.name + ".incoming").exists()


def test_corpus_signature_is_rechecked_after_serialization(idx_file, monkeypatch):
    record = _rec("A", "a")
    dump = vi.json.dump
    def changed(payload, handle, *args, **kwargs):
        result = dump(payload, handle, *args, **kwargs)
        record.description = "changed after serialized staging"
        return result
    monkeypatch.setattr(vi.json, "dump", changed)
    assert vi.ensure_index([record], embedder=_StubEmbedder()) is None
    assert not idx_file.exists()


@pytest.fixture()
def native_generations(tmp_path, monkeypatch, idx_file):
    from dataclasses import replace
    paths = replace(mr.get_app_paths(), data_root=tmp_path, model_root=tmp_path / "models",
                    userdata_dir=tmp_path / ".userdata")
    target = mr.embed_model_dir(paths)
    target.mkdir(parents=True)
    (target / "config.json").write_bytes(b"{}")
    (target / "tokenizer.json").write_bytes(b"{}")
    loaded, encoded = [], []
    controls = {}
    def install(generation, marker=True):
        (target / "pytorch_model.bin").write_bytes(str(generation).encode())
        if marker:
            mw._write_completion_marker(target)
    install(1)
    class Native:
        def __init__(self, root, **kwargs):
            assert Path(root) == target and kwargs["local_files_only"] is True
            self.generation = int((target / "pytorch_model.bin").read_bytes())
            loaded.append(self.generation)
            if controls.get("loading"):
                controls.pop("loading")()
        def encode(self, texts, **kwargs):
            encoded.append((self.generation, list(texts)))
            if controls.get("encoding"):
                controls["encoding"](self.generation)
            return [[float(self.generation)] * vi.EMBED_DIMENSIONS for _ in texts]
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Native))
    monkeypatch.setattr(vr, "_setup_determinism", lambda: None)
    monkeypatch.setattr(vr, "get_app_paths", lambda: paths)
    monkeypatch.setattr(vi, "get_app_paths", lambda: paths)
    monkeypatch.setattr(mr, "get_app_paths", lambda: paths)
    monkeypatch.setattr(vi, "_load_corpus", lambda paths=None: [_rec("A", "a")])
    vr._EMBEDDER_CACHE.clear()
    vr._DOC_VECTOR_CACHE.clear()
    yield SimpleNamespace(paths=paths, target=target, install=install, loaded=loaded,
                          encoded=encoded, controls=controls, index=idx_file)
    vr._EMBEDDER_CACHE.clear()
    vr._DOC_VECTOR_CACHE.clear()


@pytest.mark.parametrize("marker", [True, False])
def test_native_positive_caches_and_actual_index_vectors_change_generation(native_generations, marker):
    state = native_generations
    records = [_rec("A", "a")]
    g1 = vi.get_embedder()
    assert vr.load_embedder() is g1 and vi.ensure_index(records)["a"]["v"][0] == 1
    vr._dense_vectors_cached(g1, None, "query", ["document"])
    calls = len(state.encoded)
    assert vi.get_embedder() is g1 and vr.load_embedder() is g1
    assert vi.ensure_index(records)["a"]["v"][0] == 1 and len(state.encoded) == calls
    state.install(2, marker=marker)
    g2 = vi.get_embedder()
    assert g2 is not g1 and vr.load_embedder() is g2
    assert g2(["actual encoder"])[0][0] == 2
    assert vi.ensure_index(records)["a"]["v"][0] == 2
    assert vr._dense_vectors_cached(g2, None, "query", ["document"])[1][0] == 2
    with gzip.open(state.index, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["meta"]["model_state"] == json.dumps(vi._model_state(state.paths))
    assert payload["vectors"]["a"]["v"][0] == 2 and state.loaded == [1, 2]


def test_native_changed_while_loading_is_not_cached_or_labeled_as_new(native_generations):
    state = native_generations
    state.controls["loading"] = lambda: state.install(2)
    assert vr.load_embedder() is None
    assert not vr._EMBEDDER_CACHE
    assert vi.get_embedder()(["actual encoder"])[0][0] == 2
    assert state.loaded == [1, 2]


def test_native_handle_cannot_capture_a_newer_generation_after_acquisition(native_generations, monkeypatch):
    state = native_generations
    g1 = vi.get_embedder()
    def load(paths=None):
        state.install(2)
        return [_rec("A", "a")]
    monkeypatch.setattr(vi, "_load_corpus", load)
    assert vi.ensure_index() is None and not state.index.exists()
    assert vi.get_embedder() is not g1
    assert vi.ensure_index([_rec("A", "a")])["a"]["v"][0] == 2


def test_native_late_old_build_never_publishes_after_reinstallation(native_generations):
    state = native_generations
    entered, release = threading.Event(), threading.Event()
    results = []
    def pause(generation):
        if generation == 1:
            entered.set()
            assert release.wait(5)
    state.controls["encoding"] = pause
    old = threading.Thread(target=lambda: results.append(vi.ensure_index([_rec("A", "a")])))
    old.start()
    try:
        assert entered.wait(2)
        state.install(2)
        g2 = vi.get_embedder()
        assert g2(["new handle"])[0][0] == 2
    finally:
        release.set()
        old.join(5)
    assert not old.is_alive() and results == [None] and not state.index.exists()
    assert vi.ensure_index([_rec("A", "a")])["a"]["v"][0] == 2


def test_native_warm_query_rejects_generation_change_mid_encode(native_generations):
    state = native_generations
    record = _rec("A", "a")
    assert vi.ensure_index([record])
    state.controls["encoding"] = lambda generation: state.install(2) if generation == 1 else None
    assert vi.index_dense_vectors("query", [record]) is None
    assert vi.index_dense_vectors("query", [record])[0][0] == 2


def test_external_fallback_cache_changes_generation_without_native_dependency(native_generations, monkeypatch):
    state = native_generations
    monkeypatch.setattr(vi, "load_embedder", lambda: None)
    monkeypatch.setattr(mr, "external_embed_ready", lambda paths=None: True)
    clients, closed = [], []
    def external():
        generation = int((state.target / "pytorch_model.bin").read_bytes())
        class Encoder:
            def __call__(self, texts):
                return [[float(generation)] * vi.EMBED_DIMENSIONS for _ in texts]
            def close(self):
                closed.append(generation)
        enc = Encoder()
        clients.append(enc)
        return enc
    monkeypatch.setattr(mr, "external_embedder", external)
    g1 = vi.get_embedder()
    assert vi.get_embedder() is g1
    state.install(2)
    g2 = vi.get_embedder()
    assert g2 is not g1 and g2(["query"])[0][0] == 2
    assert len(clients) == 2 and closed == [1] and not state.loaded


def _rollback_project(tmp_path):
    import ltr_promote as lp
    paths = lp.Paths(tmp_path)
    archived = paths.archive_dir / "old"
    for directory, marker in ((paths.ltr_dir, "current"), (archived, "old")):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / lp.WEIGHTS_NAME).write_bytes(marker.encode())
        (directory / lp.SPEC_NAME).write_text(json.dumps({"trained_at": marker, "train_counts": {}}), encoding="utf-8")
    _record_archive_history(lp, archived)
    lp.save_registry(paths.registry_path, {"schema_version": 1, "active": "current", "versions": [
        {"version": "current", "status": "active"}, {"version": "old", "status": "archived"}]})
    paths.journal_path.parent.mkdir()
    paths.journal_path.write_bytes(b'{"existing":true}\n')
    return lp, paths


@pytest.mark.parametrize("restore_failures", [("journal",), ("journal", "registry"), ("journal", "weights"),
                                            ("journal", "registry", "weights", "spec")])
def test_persistent_transaction_restore_faults_aggregate_and_do_not_stop_other_files(tmp_path, monkeypatch, restore_failures):
    lp, paths = _rollback_project(tmp_path)
    named = {"journal": paths.journal_path, "registry": paths.registry_path,
             "weights": paths.ltr_dir / lp.WEIGHTS_NAME, "spec": paths.ltr_dir / lp.SPEC_NAME}
    before = {path: path.read_bytes() for path in named.values()}
    untouched = paths.ltr_dir / ".unowned.recovery.tmp"
    untouched.write_bytes(b"unrelated scratch")
    real_write, journal = lp.atomic_write_bytes, lp.append_journal
    attempts = []
    primary = OSError("synthetic primary journal failure")
    triggered, persistent_injected = [], []
    def fail_journal(*args, **kwargs):
        journal(*args, **kwargs)
        triggered.append(True)
        raise primary
    def fault(path, raw):
        path = Path(path)
        if triggered:
            attempts.append(path)
            if path in {named[name] for name in restore_failures}:
                persistent_injected.append(path)
                raise OSError("persistent rollback fault: " + path.name)
        return real_write(path, raw)
    monkeypatch.setattr(lp, "append_journal", fail_journal)
    monkeypatch.setattr(lp, "atomic_write_bytes", fault)
    with pytest.raises(lp.PromoteError) as caught:
        lp.cmd_rollback(SimpleNamespace(project_root=tmp_path, version="old"))
    assert triggered == [True]
    assert set(persistent_injected) == {named[name] for name in restore_failures}
    assert len(persistent_injected) == len(restore_failures)
    assert caught.value.primary_error is primary
    for name, path in named.items():
        assert path in attempts
        if name not in restore_failures:
            assert path.read_bytes() == before[path]
    assert "恢复不完整" in str(caught.value) and "已恢复" not in str(caught.value)
    causes, exc = [], caught.value
    while exc is not None:
        causes.append(exc)
        exc = exc.__cause__
    assert primary in causes
    for name in restore_failures:
        assert named[name].name in str(caught.value)
    assert untouched.read_bytes() == b"unrelated scratch"
    assert not any((paths.archive_dir / "current").glob("*"))


def test_transaction_discovery_failure_does_not_prevent_restoring_known_paths(tmp_path, monkeypatch):
    import ltr_promote as lp
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"original a")
    b.write_bytes(b"original b")
    calls = []
    primary = OSError("primary mutation fault")
    def discover():
        calls.append(True)
        if len(calls) > 1:
            raise OSError("discovery fault")
        return [a, b]
    with pytest.raises(lp.PromoteError, match="恢复不完整") as caught:
        with lp._files_transaction(discover):
            a.write_bytes(b"changed a")
            b.write_bytes(b"changed b")
            raise primary
    assert a.read_bytes() == b"original a" and b.read_bytes() == b"original b"
    assert caught.value.__cause__ is primary


def test_native_cross_encoder_positive_cache_uses_the_same_generation_contract(native_generations, monkeypatch):
    state = native_generations
    target = mr.model_dir(state.paths)
    target.mkdir(parents=True)
    (target / "config.json").write_bytes(b"{}")
    (target / "tokenizer.json").write_bytes(b"{}")
    loaded = []
    def install(generation):
        (target / "pytorch_model.bin").write_bytes(str(generation).encode())
        mw._write_completion_marker(target)
    class NativeCross:
        def __init__(self, root, **kwargs):
            assert Path(root) == target and kwargs["local_files_only"]
            self.generation = int((target / "pytorch_model.bin").read_bytes())
            loaded.append(self.generation)
        def predict(self, pairs, **kwargs):
            return [float(self.generation)] * len(pairs)
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(CrossEncoder=NativeCross))
    vr._CROSS_CACHE.clear()
    try:
        install(1)
        g1 = vr.load_cross_encoder()
        assert g1([("q", "doc")]) == [1.0] and vr.load_cross_encoder() is g1
        install(2)
        g2 = vr.load_cross_encoder()
        assert g2 is not g1 and g2([("q", "doc")]) == [2.0] and loaded == [1, 2]
    finally:
        vr._CROSS_CACHE.clear()


def test_real_atomic_restore_fault_cleans_only_owned_temporary_files(tmp_path, monkeypatch):
    from dataset_recommender.app import runtime_paths
    lp, paths = _rollback_project(tmp_path)
    named = [paths.ltr_dir / name for name in lp.ARTIFACT_NAMES] + [paths.registry_path]
    before = {path: path.read_bytes() for path in named}
    scratch = paths.journal_path.with_name(".unowned.journal.restore.tmp")
    scratch.write_bytes(b"unrelated scratch")
    replace, journal = runtime_paths.os.replace, lp.append_journal
    triggered, temps = [], []
    primary = OSError("primary journal fault")
    def fail_journal(*args, **kwargs):
        journal(*args, **kwargs)
        triggered.append(True)
        raise primary
    def fault(source, dest):
        if triggered and Path(dest) == paths.journal_path:
            temps.append(Path(source))
            raise PermissionError("persistent journal replace fault")
        return replace(source, dest)
    monkeypatch.setattr(lp, "append_journal", fail_journal)
    monkeypatch.setattr(runtime_paths.os, "replace", fault)
    with pytest.raises(lp.PromoteError, match="恢复不完整") as caught:
        lp.cmd_rollback(SimpleNamespace(project_root=tmp_path, version="old"))
    assert triggered == [True]
    assert primary in list(_exception_causes(caught.value))
    assert caught.value.primary_error is primary
    assert {path: path.read_bytes() for path in named} == before
    assert len(temps) == 4 and all(not path.exists() for path in temps)
    assert scratch.read_bytes() == b"unrelated scratch"


def _integrity_model_7(target):
    target.mkdir()
    for name, raw in {"config.json": b'{"a":0}', "tokenizer.json": b'{"a":0}', "model.safetensors": b"weights"}.items():
        assert len(raw) == 7
        (target / name).write_bytes(raw)
    return target


def _freeze_model_stats(monkeypatch, target):
    original = Path.stat
    frozen = {path: path.stat() for path in target.iterdir()}
    monkeypatch.setattr(Path, "stat", lambda path, *args, **kwargs: frozen[path] if path in frozen else original(path, *args, **kwargs))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file-change token")
def test_windows_same_metadata_corruption_invalidates_manifest_and_generation(tmp_path, monkeypatch):
    target = _integrity_model_7(tmp_path / "same-stat")
    _freeze_model_stats(monkeypatch, target)
    evidence = mw.model_file_manifest(target)
    generation = mw.model_generation(target)
    budget = _warm_verify_sha_rehash_budget(target)
    original = mw._weight_sha
    calls = []
    def sha(path):
        calls.append(Path(path).name)
        return original(path)
    monkeypatch.setattr(mw, "_weight_sha", sha)
    assert mw.verify_model_manifest(target, evidence)
    count = len(calls)
    assert mw.verify_model_manifest(target, evidence) and len(calls) == count + budget
    (target / "model.safetensors").write_bytes(b"damaged")
    assert original(target / "model.safetensors") != evidence["model.safetensors"]["sha256"]
    assert not mw.verify_model_manifest(target, evidence)
    assert mw.model_generation(target) != generation


def _simulated_usn_token(path):
    """确定性可信 token（等价 USN 保证：文件字节一变 token 必变），不碰 kernel32。"""
    digest = hashlib.sha256(Path(path).read_bytes()).digest()
    return ("usn", 3, (1 << 80) | int.from_bytes(digest[:8], "little") | 1,
            int.from_bytes(digest[8:16], "little") | 1)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file-change token seam")
@pytest.mark.parametrize("capability", ["all-supported", "no-supported", "mixed"])
def test_warm_signature_rehash_budget_is_exact_by_token_capability(tmp_path, monkeypatch, capability):
    """能力三分类的暖重读预算精确契约：全有 token → 暖 0 次；全无 → 每文件一次；
    混合 → 恰好为无 token 文件数。冻 stat 的同大小改写仍必须被拒绝且代际变化。"""
    target = _integrity_model_7(tmp_path / ("cap-" + capability))
    untokened = {"mixed": {"model.safetensors"}}.get(capability, set())

    def seam(path):
        if capability == "no-supported" or Path(path).name in untokened:
            return None
        return _simulated_usn_token(path)

    monkeypatch.setattr(mw, "_windows_file_change_token", seam)
    _freeze_model_stats(monkeypatch, target)
    evidence = mw.model_file_manifest(target)
    generation = mw.model_generation(target)
    budget = _warm_verify_sha_rehash_budget(target)
    assert budget == {"all-supported": 0, "no-supported": 3, "mixed": 1}[capability]
    original = mw._weight_sha
    calls = []
    def sha(path):
        calls.append(Path(path).name)
        return original(path)
    monkeypatch.setattr(mw, "_weight_sha", sha)
    assert mw.verify_model_manifest(target, evidence)
    count = len(calls)
    assert mw.verify_model_manifest(target, evidence)
    assert len(calls) == count + budget
    (target / "model.safetensors").write_bytes(b"damaged")   # mixed 恰好损坏无 token 的文件
    assert not mw.verify_model_manifest(target, evidence)
    assert mw.model_generation(target) != generation


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows same-size writes")
def test_windows_native_same_size_corruption_never_reuses_sha_success(tmp_path):
    for number in range(300):
        target = _integrity_model_7(tmp_path / ("native-%d" % number))
        evidence = mw.model_file_manifest(target)
        assert mw.verify_model_manifest(target, evidence)
        assert mw.verify_model_manifest(target, evidence)
        (target / "model.safetensors").write_bytes(b"damaged")
        assert not mw.verify_model_manifest(target, evidence), number


def _usn_record(major=3, reference=None, usn=789):
    import struct
    header, reference_size, usn_offset = (60, 8, 24) if major == 2 else (76, 16, 40)
    reference = reference if reference is not None else (1 << 80) + 123 if major == 3 else 123
    raw = bytearray((header + 2 + 7) // 8 * 8)
    struct.pack_into("<IHH", raw, 0, len(raw), major, 0)
    raw[8:8 + reference_size] = reference.to_bytes(reference_size, "little")
    struct.pack_into("<q", raw, usn_offset, usn)
    struct.pack_into("<HH", raw, header - 4, 2, header)
    raw[header:header + 2] = b"x\0"
    return bytes(raw)


@pytest.mark.parametrize("major", [2, 3])
def test_file_usn_token_preserves_complete_file_reference(major):
    reference = (1 << 80) + 123 if major == 3 else (1 << 60) + 123
    assert mw._parse_usn_record(_usn_record(major, reference)) == ("usn", major, reference, 789)


@pytest.mark.parametrize("fault", ["empty", "header", "truncated", "short-v2", "oversized", "unknown", "minor",
                                  "length-small", "length-large", "unaligned", "reference-zero", "usn-zero",
                                  "usn-negative", "name-empty", "name-odd", "name-offset-small", "name-offset-odd", "name-overrun"])
def test_file_usn_token_rejects_untrusted_records(fault):
    import struct
    raw = bytearray(_usn_record())
    if fault == "empty":
        raw = bytearray()
    elif fault == "header":
        raw = raw[:7]
    elif fault == "truncated":
        raw = raw[:-1]
    elif fault == "short-v2":
        raw = bytearray(56)
        struct.pack_into("<IHH", raw, 0, len(raw), 2, 0)
    elif fault == "oversized":
        raw = bytearray(4104)
        struct.pack_into("<IHH", raw, 0, len(raw), 3, 0)
    elif fault == "unknown":
        struct.pack_into("<H", raw, 4, 4)
    elif fault == "minor":
        struct.pack_into("<H", raw, 6, 1)
    elif fault.startswith("length-"):
        struct.pack_into("<I", raw, 0, len(raw) + (8 if fault == "length-large" else -8))
    elif fault == "unaligned":
        raw.append(0)
        struct.pack_into("<I", raw, 0, len(raw))
    elif fault == "reference-zero":
        raw[8:24] = bytes(16)
    elif fault.startswith("usn-"):
        struct.pack_into("<q", raw, 40, -1 if fault == "usn-negative" else 0)
    elif fault.startswith("name-offset-"):
        struct.pack_into("<H", raw, 74, 8 if fault == "name-offset-small" else 77)
    else:
        struct.pack_into("<H", raw, 72, {"name-empty": 0, "name-odd": 1, "name-overrun": 20}[fault])
    assert mw._parse_usn_record(bytes(raw)) is None


class _FileUsnApi:
    def __init__(self, fault=None):
        import itertools
        self.fault = fault
        self.handles = itertools.count(1)
        self.opened, self.closed = [], []

    def CreateFileW(self, *args):
        import ctypes
        assert args[1:4] == (0x80000000, 7, None) and args[4:] == (3, 0, None)
        if self.fault == "open-error":
            raise OSError("synthetic open error")
        if self.fault == "open-invalid":
            return ctypes.c_void_p(-1).value
        if self.fault == "open-null":
            return None
        handle = next(self.handles)
        self.opened.append(handle)
        return handle

    def DeviceIoControl(self, handle, control, versions, input_size, output, output_size, returned, overlap):
        import ctypes
        assert control == 0x900EB and input_size == 4 and tuple(versions) == (2, 3)
        assert output_size == 4096 and overlap is None
        if self.fault == "read-error":
            raise OSError("synthetic ioctl error")
        if self.fault == "read-false":
            return False
        raw = _usn_record(reference=(1 << 80) + handle, usn=handle * 5)
        if self.fault == "unknown":
            raw = raw[:4] + b"\4\0" + raw[6:]
        ctypes.memmove(output, raw, len(raw))
        returned._obj.value = 4097 if self.fault == "oversized-return" else 7 if self.fault == "short-return" else len(raw)
        return True

    def CloseHandle(self, handle):
        self.closed.append(handle)
        if self.fault == "close-error":
            raise OSError("synthetic close error")
        return self.fault != "close-false"


@pytest.mark.parametrize("fault", [None, "open-error", "open-invalid", "open-null", "read-error", "read-false",
                                  "unknown", "short-return", "oversized-return", "close-false", "close-error"])
def test_file_usn_query_releases_every_owned_handle(tmp_path, monkeypatch, fault):
    path = tmp_path / "synthetic.bin"
    path.write_bytes(b"weights")
    api = _FileUsnApi(fault)
    monkeypatch.setattr(mw, "_windows_file_api", lambda: api)
    result = mw._windows_file_change_token(path)
    assert result == (("usn", 3, (1 << 80) + 1, 5) if fault is None else None)
    assert api.closed == api.opened


def test_file_usn_query_unavailable_is_not_a_trusted_token(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.bin"
    path.write_bytes(b"weights")
    def unavailable():
        raise OSError("synthetic unavailable file API")
    monkeypatch.setattr(mw, "_windows_file_api", unavailable)
    assert mw._windows_file_change_token(path) is None


def test_file_usn_query_has_per_call_buffers_and_handles(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / "synthetic.bin"
    path.write_bytes(b"weights")
    api = _FileUsnApi()
    monkeypatch.setattr(mw, "_windows_file_api", lambda: api)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(mw._windows_file_change_token, [path] * 32))
    assert set(results) == {("usn", 3, (1 << 80) + handle, handle * 5) for handle in api.opened}
    assert len(results) == len(api.opened) == len(api.closed) == 32
    assert sorted(api.closed) == sorted(api.opened)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows token fallback")
@pytest.mark.parametrize("token_failure", ["missing", "io-error", "unknown-record"])
def test_windows_missing_change_token_uses_full_sha_for_warm_signatures(tmp_path, monkeypatch, token_failure):
    target = _integrity_model_7(tmp_path / "fallback")
    _freeze_model_stats(monkeypatch, target)
    if token_failure == "missing":
        monkeypatch.setattr(mw, "_windows_file_change_token", lambda path: None)
    else:
        api = _FileUsnApi("read-error" if token_failure == "io-error" else "unknown")
        monkeypatch.setattr(mw, "_windows_file_api", lambda: api)
    evidence = mw.model_file_manifest(target)
    generation = mw.model_generation(target)
    original = mw._weight_sha
    calls = []
    def sha(path):
        calls.append(Path(path).name)
        return original(path)
    monkeypatch.setattr(mw, "_weight_sha", sha)
    assert mw.verify_model_manifest(target, evidence)
    assert len(calls) == 9
    assert mw.verify_model_manifest(target, evidence)
    assert len(calls) == 12
    (target / "model.safetensors").write_bytes(b"damaged")
    assert not mw.verify_model_manifest(target, evidence)
    assert mw.model_generation(target) != generation
    assert not mr._files_intact(evidence, target)
    from dataset_recommender.app import model_installer
    assert not model_installer._weights_trusted(target, {"model_files": evidence}, "model_files")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows token fallback")
def test_windows_missing_change_token_invalidates_index_and_encoder_generations(tmp_path, monkeypatch, idx_file):
    from dataclasses import replace
    paths = replace(mr.get_app_paths(), model_root=tmp_path / "model-root")
    target = mr.embed_model_dir(paths)
    target.parent.mkdir(parents=True)
    _integrity_model_7(target)
    _freeze_model_stats(monkeypatch, target)
    monkeypatch.setattr(mw, "_windows_file_change_token", lambda path: None)
    loaded = []
    class Native:
        def __init__(self, root, **kwargs):
            assert Path(root) == target and kwargs["local_files_only"]
            self.value = (target / "model.safetensors").read_bytes()
            loaded.append(self.value)
        def encode(self, texts, **kwargs):
            return [[1.0 if self.value == b"weights" else 2.0] * vi.EMBED_DIMENSIONS for _ in texts]
        def predict(self, pairs, **kwargs):
            return [1.0 if self.value == b"weights" else 2.0 for _ in pairs]
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=Native, CrossEncoder=Native))
    monkeypatch.setattr(vr, "_setup_determinism", lambda: None)
    vr._EMBEDDER_CACHE.clear()
    vr._CROSS_CACHE.clear()
    try:
        state = vi._model_state(paths)
        client = mr.ExternalEmbedder(paths)
        generation = client._generation()
        old = vr.load_embedder(target)
        cross = vr.load_cross_encoder(target)
        assert vr.load_embedder(target) is old and vr.load_cross_encoder(target) is cross
        records = [_rec("A", "a")]
        assert vi.ensure_index(records, paths=paths, embedder=old)["a"]["v"][0] == 1.0
        (target / "model.safetensors").write_bytes(b"damaged")
        assert vi._model_state(paths) != state and client._generation() != generation
        new = vr.load_embedder(target)
        newer_cross = vr.load_cross_encoder(target)
        assert new is not old and new(["synthetic"])[0][0] == 2.0
        assert newer_cross is not cross and newer_cross([("q", "d")]) == [2.0]
        assert vi.ensure_index(records, paths=paths, embedder=new)["a"]["v"][0] == 2.0
        assert loaded == [b"weights", b"weights", b"damaged", b"damaged"]
    finally:
        vr._EMBEDDER_CACHE.clear()
        vr._CROSS_CACHE.clear()


def test_model_worker_integrity_is_available_as_standalone_data_script(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("standalone_model_worker", mw.__file__)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    target = _integrity_model_7(tmp_path / "standalone")
    evidence = worker.model_file_manifest(target)
    assert worker.verify_model_manifest(target, evidence)
    (target / "model.safetensors").write_bytes(b"damaged")
    assert not worker.verify_model_manifest(target, evidence)


def test_file_usn_query_accepts_v2_records(tmp_path, monkeypatch):
    import ctypes
    path = tmp_path / "synthetic.bin"
    path.write_bytes(b"weights")
    api = _FileUsnApi()
    def read(handle, control, versions, input_size, output, output_size, returned, overlap):
        raw = _usn_record(2, reference=(1 << 60) + handle, usn=456)
        ctypes.memmove(output, raw, len(raw))
        returned._obj.value = len(raw)
        return True
    api.DeviceIoControl = read
    monkeypatch.setattr(mw, "_windows_file_api", lambda: api)
    assert mw._windows_file_change_token(path) == ("usn", 2, (1 << 60) + 1, 456)
    assert api.closed == api.opened == [1]
