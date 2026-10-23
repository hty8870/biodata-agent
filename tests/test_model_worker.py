from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

from dataset_recommender.retrieval import model_worker as worker


def _ready(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text("{}", encoding="utf-8")
    (root / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"weights")


def test_ready_requires_config_tokenizer_and_nonempty_weight(tmp_path):
    root = tmp_path / "model"
    root.mkdir()
    assert not worker.model_files_ready(root)
    (root / "config.json").write_text("{}", encoding="utf-8")
    (root / "tokenizer.json").write_text("{}", encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"")
    assert not worker.model_files_ready(root)
    (root / "model.safetensors").write_bytes(b"ok")
    assert worker.model_files_ready(root)


def test_pair_validation_is_bounded():
    assert worker._valid_pairs([["q", "doc"]]) == [("q", "doc")]
    assert worker._valid_pairs([]) is None
    assert worker._valid_pairs([["q"]]) is None
    assert worker._valid_pairs([["q" * (worker.MAX_QUERY_CHARS + 1), "doc"]]) is None
    assert worker._valid_pairs([["q", "d"]] * (worker.MAX_PAIRS + 1)) is None


def test_download_prefers_modelscope_and_validates_real_files(monkeypatch, tmp_path):
    source = tmp_path / "cache"
    _ready(source)
    fake = ModuleType("modelscope")
    fake.snapshot_download = lambda model_id, ignore_file_pattern: str(source)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "modelscope", fake)
    target = tmp_path / "target"
    assert worker.download_model(target)
    assert worker.model_files_ready(target)


def test_download_falls_back_to_huggingface(monkeypatch, tmp_path):
    bad = ModuleType("modelscope")
    bad.snapshot_download = lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("offline"))  # type: ignore[attr-defined]
    hf = ModuleType("huggingface_hub")

    def download(*, repo_id, local_dir, ignore_patterns):
        _ready(Path(local_dir))

    hf.snapshot_download = download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "modelscope", bad)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hf)
    assert worker.download_model(tmp_path / "target")


# ---------- --embed 模式：嵌入请求边界 / 模型 id 路由 ----------

def test_texts_validation_is_bounded():
    assert worker._valid_texts(["q", "doc"]) == ["q", "doc"]
    assert worker._valid_texts([]) is None
    assert worker._valid_texts([1]) is None
    assert worker._valid_texts(["x" * (worker.MAX_EMBED_TEXT_CHARS + 1)]) is None
    assert worker._valid_texts(["ok"] * (worker.MAX_TEXTS + 1)) is None


def test_download_embed_uses_embed_model_id(monkeypatch, tmp_path):
    source = tmp_path / "cache"
    _ready(source)
    seen = {}
    fake = ModuleType("modelscope")

    def snap(model_id, ignore_file_pattern):
        seen["id"] = model_id
        return str(source)

    fake.snapshot_download = snap  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "modelscope", fake)
    assert worker.download_model(tmp_path / "target", model_id=worker.EMBED_MODEL_ID)
    assert seen["id"] == worker.EMBED_MODEL_ID


def test_main_embed_flag_routes_to_embed_model(monkeypatch, tmp_path):
    seen = {}

    def fake_download(target, model_id=worker.MODEL_ID):
        seen["id"] = model_id
        return True

    monkeypatch.setattr(worker, "download_model", fake_download)
    assert worker.main(["--download", str(tmp_path / "m"), "--embed"]) == 0
    assert seen["id"] == worker.EMBED_MODEL_ID
    assert worker.main(["--download", str(tmp_path / "m2")]) == 0
    assert seen["id"] == worker.MODEL_ID


def _files(root, payload):
    for name, raw in payload.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def _bytes(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def _download_payload(monkeypatch, payload):
    def download(staging, model_id):
        _files(staging, payload)
        return True
    monkeypatch.setattr(worker, "_download_via_mirrors", download)


def test_download_retires_old_managed_files_and_preserves_user_files(tmp_path, monkeypatch):
    import json
    target = tmp_path / "target"
    _files(target, {"config.json": b"old", "tokenizer.json": b"old",
                    "model.safetensors": b"damaged", "1_Pooling/config.json": b"old-pool",
                    "revision/model-00001-of-00002.safetensors": b"broken-shard",
                    "model.safetensors.index.json": b'{"weight_map":{"a":"missing.safetensors"}}',
                    "notes.txt": b"user notes", "user/report.json": b"user json"})
    fresh = {"config.json": b"{}", "tokenizer_config.json": b"{}", "pytorch_model.bin": b"new weights"}
    _download_payload(monkeypatch, fresh)
    assert worker.download_model(target)
    actual = _bytes(target)
    assert set(actual) == set(fresh) | {worker.COMPLETION_MARKER, "notes.txt", "user/report.json"}
    assert actual["notes.txt"] == b"user notes" and actual["user/report.json"] == b"user json"
    evidence = json.loads(actual[worker.COMPLETION_MARKER])
    assert set(evidence) == set(fresh)
    assert worker.verify_model_manifest(target, evidence)
    (target / "notes.txt").write_bytes(b"updated user notes")
    assert worker._completion_valid(target)
    monkeypatch.setattr(worker, "_download_via_mirrors", lambda *a: False)
    assert worker.download_model(target)


def test_download_retires_old_bin_when_new_sharded_safetensors_are_complete(tmp_path, monkeypatch):
    import json
    target = tmp_path / "target"
    _files(target, {"config.json": b"old", "tokenizer.json": b"old", "pytorch_model.bin": b"damaged",
                    "revision/pytorch_model.bin.index.json": b"old index", "notes.txt": b"keep"})
    fresh = {"config.json": b"{}", "tokenizer.json": b"{}",
             "model.safetensors.index.json": json.dumps({"weight_map": {
                 "a": "shards/model-00001.safetensors", "b": "shards/model-00002.safetensors"}}).encode(),
             "shards/model-00001.safetensors": b"one", "shards/model-00002.safetensors": b"two",
             "1_Pooling/config.json": b"{}"}
    _download_payload(monkeypatch, fresh)
    assert worker.download_model(target)
    assert set(_bytes(target)) == set(fresh) | {worker.COMPLETION_MARKER, "notes.txt"}
    assert worker._completion_valid(target)


def test_download_incomplete_shard_set_does_not_modify_original(tmp_path, monkeypatch):
    import json
    target = tmp_path / "target"
    _ready(target)
    worker._write_completion_marker(target)
    (target / "model.safetensors").write_bytes(b"damaged")
    before = _bytes(target)
    _download_payload(monkeypatch, {"config.json": b"{}", "tokenizer.json": b"{}",
        "model.safetensors.index.json": json.dumps({"weight_map": {
            "a": "shards/a.safetensors", "b": "shards/b.safetensors"}}).encode(),
        "shards/a.safetensors": b"one"})
    assert worker.download_model(target) is False
    assert _bytes(target) == before


def test_download_publication_and_marker_faults_restore_original(tmp_path, monkeypatch):
    import pytest
    target = tmp_path / "target"
    _ready(target)
    worker._write_completion_marker(target)
    (target / "model.safetensors").write_bytes(b"damaged")
    (target / "notes.txt").write_bytes(b"keep")
    before = _bytes(target)
    fresh = {"config.json": b"{}", "tokenizer.json": b"{}", "pytorch_model.bin": b"new"}
    _download_payload(monkeypatch, fresh)
    replace = worker.os.replace
    failures = []
    def fail_marker(source, dest):
        if Path(dest) == target / worker.COMPLETION_MARKER and not failures:
            failures.append(True)
            raise OSError("synthetic marker publication fault")
        return replace(source, dest)
    monkeypatch.setattr(worker.os, "replace", fail_marker)
    with pytest.raises(OSError, match="marker publication fault"):
        worker.download_model(target)
    assert _bytes(target) == before
    assert worker.download_model(target)
    assert worker._completion_valid(target) and not (target / "model.safetensors").exists()


def test_download_file_publication_fault_restores_old_bytes(tmp_path, monkeypatch):
    import pytest
    target = tmp_path / "target"
    _files(target, {"config.json": b"old config", "tokenizer.json": b"old tokenizer",
                    "model.safetensors": b"old weights", "notes.txt": b"keep"})
    before = _bytes(target)
    _download_payload(monkeypatch, {"config.json": b"{}", "tokenizer.json": b"{}", "pytorch_model.bin": b"new"})
    replace = worker.os.replace
    failures = []
    def fail_weight(source, dest):
        if Path(dest) == target / "pytorch_model.bin" and not failures:
            failures.append(True)
            raise OSError("synthetic file publication fault")
        return replace(source, dest)
    monkeypatch.setattr(worker.os, "replace", fail_weight)
    with pytest.raises(OSError, match="file publication fault"):
        worker.download_model(target)
    assert _bytes(target) == before


def test_failed_mirror_does_not_contaminate_successful_mirror(tmp_path, monkeypatch):
    source = tmp_path / "partial"
    _files(source, {"config.json": b"{}", "model.safetensors": b"partial weights"})
    ms = ModuleType("modelscope")
    ms.snapshot_download = lambda *a, **k: str(source)
    hf = ModuleType("huggingface_hub")
    def download(*, repo_id, local_dir, ignore_patterns):
        _files(Path(local_dir), {"config.json": b"{}", "tokenizer.json": b"{}", "pytorch_model.bin": b"new"})
    hf.snapshot_download = download
    monkeypatch.setitem(sys.modules, "modelscope", ms)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hf)
    target = tmp_path / "target"
    assert worker.download_model(target)
    assert not (target / "model.safetensors").exists()


def test_download_never_removes_unowned_staging_sibling(tmp_path, monkeypatch):
    sibling = tmp_path / "target.incoming"
    _files(sibling, {"notes.txt": b"someone else's data"})
    _download_payload(monkeypatch, {"config.json": b"{}", "tokenizer.json": b"{}", "pytorch_model.bin": b"new"})
    assert worker.download_model(tmp_path / "target")
    assert (sibling / "notes.txt").read_bytes() == b"someone else's data"


def test_publication_restore_failure_preserves_owned_backup_and_other_originals(tmp_path, monkeypatch):
    import pytest
    target = tmp_path / "target"
    old = {"config.json": b"old config", "tokenizer.json": b"old tokenizer",
           "model.safetensors": b"old weights", "notes.txt": b"keep"}
    _files(target, old)
    _download_payload(monkeypatch, {"config.json": b"{}", "tokenizer.json": b"{}", "pytorch_model.bin": b"new"})
    replace = worker.os.replace
    primary = OSError("synthetic publication failure")
    def fault(source, dest):
        if Path(dest) == target / "pytorch_model.bin":
            raise primary
        if Path(dest) == target / "config.json" and ".rollback-" in str(source):
            raise OSError("persistent config recovery failure")
        return replace(source, dest)
    monkeypatch.setattr(worker.os, "replace", fault)
    with pytest.raises(OSError, match="restore_incomplete") as caught:
        worker.download_model(target)
    assert caught.value.__cause__ is primary
    for name in ("tokenizer.json", "model.safetensors", "notes.txt"):
        assert (target / name).read_bytes() == old[name]
    backups = list(tmp_path.glob(".target.rollback-*"))
    assert len(backups) == 1 and (backups[0] / "config.json").read_bytes() == old["config.json"]
    assert not (target / worker.COMPLETION_MARKER).exists()
    assert not list(tmp_path.glob(".target.incoming-*"))


def test_malformed_shard_indexes_fail_closed_without_readiness_exception(tmp_path):
    root = tmp_path / "model"
    _ready(root)
    (root / "model.safetensors.index.json").write_bytes(b"[]")
    assert not worker.model_files_ready(root)
    assert not worker.verify_model_manifest(root, worker.model_file_manifest(root))


def test_download_overlapping_user_files_are_published_and_other_user_files_survive(tmp_path, monkeypatch):
    target = tmp_path / "target"
    _files(target, {"README.md": b"original readme", "user/README.md": b"original nested readme",
                    "notes.txt": b"unrelated notes", "config.json": b"old",
                    "tokenizer.json": b"old", "model.safetensors": b"damaged"})
    fresh = {"README.md": b"new readme", "user/README.md": b"new nested readme",
             "config.json": b"{}", "tokenizer.json": b"{}", "pytorch_model.bin": b"new"}
    _download_payload(monkeypatch, fresh)
    assert worker.download_model(target)
    actual = _bytes(target)
    assert actual["notes.txt"] == b"unrelated notes"
    assert all(actual[name] == raw for name, raw in fresh.items())
    assert not (target / "model.safetensors").exists()
    assert worker._completion_valid(target)


def test_download_without_marker_restores_overlapping_user_files_at_every_fault(tmp_path, monkeypatch):
    import pytest

    for phase in ("backup", "before_readme", "after_readme", "marker", "completion"):
        with monkeypatch.context() as patch:
            target = tmp_path / phase / "target"
            _files(target, {"README.md": b"original readme\x00\xff",
                            "user/README.md": b"original nested readme",
                            "notes.txt": b"unrelated notes", "config.json": b"old",
                            "tokenizer.json": b"old", "model.safetensors": b"damaged"})
            before = _bytes(target)
            assert not (target / worker.COMPLETION_MARKER).exists()
            fresh = {"README.md": b"new readme", "user/README.md": b"new nested readme",
                     "config.json": b"{}", "tokenizer.json": b"{}", "pytorch_model.bin": b"new"}
            _download_payload(patch, fresh)
            replace = worker.os.replace
            triggered = []
            primary = OSError("synthetic " + phase + " failure")

            def fault(source, dest):
                source, dest = Path(source), Path(dest)
                hit = ((phase == "backup" and source == target / "config.json")
                       or (phase == "before_readme" and ".incoming-" in str(source)
                           and dest == target / "README.md")
                       or (phase == "after_readme" and ".incoming-" in str(source)
                           and dest == target / "config.json")
                       or (phase == "marker" and dest == target / worker.COMPLETION_MARKER))
                if hit and not triggered:
                    triggered.append(True)
                    raise primary
                return replace(source, dest)

            patch.setattr(worker.os, "replace", fault)
            if phase == "completion":
                valid = worker._completion_valid
                patch.setattr(worker, "_completion_valid", lambda p: False if
                              (Path(p) / worker.COMPLETION_MARKER).exists() else valid(p))
            with pytest.raises(OSError) as caught:
                worker.download_model(target)
            if phase != "completion":
                assert caught.value is primary and triggered == [True]
            assert _bytes(target) == before, phase
            assert not list(target.parent.glob(".target.rollback-*"))
            assert not list(target.parent.glob(".target.incoming-*"))


def test_overlapping_user_file_restore_failure_aggregates_and_preserves_backup(tmp_path, monkeypatch):
    import pytest
    target = tmp_path / "target"
    old = {"README.md": b"original readme", "user/README.md": b"original nested",
           "config.json": b"old", "tokenizer.json": b"old", "model.safetensors": b"damaged",
           "notes.txt": b"unrelated notes"}
    _files(target, old)
    _download_payload(monkeypatch, {"README.md": b"new", "user/README.md": b"new nested",
        "config.json": b"{}", "tokenizer.json": b"{}", "pytorch_model.bin": b"new weights"})
    replace = worker.os.replace
    primary = OSError("synthetic marker failure")
    attempted = []

    def fault(source, dest):
        if Path(dest) == target / worker.COMPLETION_MARKER:
            raise primary
        if ".rollback-" in str(source):
            name = Path(dest).relative_to(target).as_posix()
            attempted.append(name)
            if name in {"README.md", "user/README.md"}:
                raise OSError("synthetic recovery failure")
        return replace(source, dest)

    monkeypatch.setattr(worker.os, "replace", fault)
    with pytest.raises(OSError, match="restore_incomplete") as caught:
        worker.download_model(target)
    assert caught.value.__cause__ is primary
    assert {"README.md", "user/README.md", "config.json", "tokenizer.json", "model.safetensors"} <= set(attempted)
    for name in ("config.json", "tokenizer.json", "model.safetensors", "notes.txt"):
        assert (target / name).read_bytes() == old[name]
    backup, = target.parent.glob(".target.rollback-*")
    for name in ("README.md", "user/README.md"):
        assert (backup / name).read_bytes() == old[name]
    assert not (target / worker.COMPLETION_MARKER).exists()
