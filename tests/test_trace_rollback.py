# -*- coding: utf-8 -*-
"""trace.snapshot + trace.rollback：快照 diff/回退正确性/fail-closed/dry-run/幂等/CLI。

夹具在 tmp 项目根下造 `database/external/` 文件——快照 watch 目录恒为它（base 红线）。"""
from __future__ import annotations

import json

import pytest

from dataset_recommender.agent.trace.rollback import apply_rollback, main, plan_rollback
from dataset_recommender.agent.trace.snapshot import SnapshotError, SnapshotStore


@pytest.fixture()
def proj(tmp_path):
    ext = tmp_path / "database" / "external"
    ext.mkdir(parents=True)
    (ext / "keep.json").write_text('{"records": [{"id": 1}]}', encoding="utf-8")
    return tmp_path


def _ext(proj):
    return proj / "database" / "external"


def _manifest_rows(proj):
    path = proj / ".userdata" / "recycle" / "manifest.jsonl"
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_capture_finalize_diff_created(proj):
    store = SnapshotStore(proj)
    sid = store.capture("curate.search_online")
    (_ext(proj) / "upload_20260817_x_curate_arrayexpress.json").write_text(
        '{"records": [{"id": 2}]}', encoding="utf-8")
    diff = store.finalize(sid)
    assert [e["name"] for e in diff["created"]] == ["upload_20260817_x_curate_arrayexpress.json"]
    assert diff["modified"] == [] and diff["deleted"] == []
    meta = store.load(sid)
    assert meta["finalized"] is True and meta["preimage_missing"] == []
    assert meta["diff"]["created"][0]["sha256"]


def test_finalize_attribution_excludes_concurrent_created_files(proj):
    """capture—finalize 窗口里他人写入的文件不因全目录差分被记成 created——
    哈希匹配挡不住「归属错误」；本步只许回滚自己自报的产物。"""
    store = SnapshotStore(proj)
    sid = store.capture("curate.search_online")
    mine = _ext(proj) / "upload_mine.json"
    theirs = _ext(proj) / "upload_theirs.json"
    mine.write_text('{"records": [{"id": 1}]}', encoding="utf-8")
    theirs.write_text('{"records": [{"id": 2}]}', encoding="utf-8")   # 并发请求的写入
    diff = store.finalize(sid, written=["upload_mine.json"])
    assert [e["name"] for e in diff["created"]] == ["upload_mine.json"]
    assert diff["unattributed_created"] == ["upload_theirs.json"]
    result = apply_rollback(proj, sid)
    assert [e["name"] for e in result["applied"]["recycled"]] == ["upload_mine.json"]
    assert theirs.is_file(), "他人文件必须原样保留"
    assert theirs.read_text(encoding="utf-8") == '{"records": [{"id": 2}]}'


def test_finalize_without_written_keeps_legacy_full_diff(proj):
    """written 缺省 = 旧行为逐位不变（不设清单的动词仍走全目录差分）。"""
    store = SnapshotStore(proj)
    sid = store.capture("curate.search_online")
    (_ext(proj) / "upload_other.json").write_text('{"records": [{"id": 3}]}',
                                                  encoding="utf-8")
    diff = store.finalize(sid)
    assert [e["name"] for e in diff["created"]] == ["upload_other.json"]
    assert "unattributed_created" not in diff
    assert store.load(sid)["diff"]["created"][0]["name"] == "upload_other.json"


def test_finalize_mutation_receipt_filters_all_three_lists(proj):
    """收据覆盖 created/modified/deleted 三类——capture—finalize
    窗口内他人的改写/删除只进 `unattributed_*` 留痕，不进回滚执行清单。"""
    store = SnapshotStore(proj)
    victim = _ext(proj) / "keep.json"
    doomed = _ext(proj) / "doomed.json"
    doomed.write_text('{"records": []}', encoding="utf-8")
    sid = store.capture("curate.search_online")
    (_ext(proj) / "upload_mine.json").write_text('{"records": [{"id": 1}]}', encoding="utf-8")
    victim.write_text('{"records": [{"id": 2}]}', encoding="utf-8")   # 并发他人改写
    doomed.unlink()                                                    # 并发他人删除
    diff = store.finalize(sid, mutation={"created": ["upload_mine.json"],
                                         "modified": [], "deleted": []})
    assert [e["name"] for e in diff["created"]] == ["upload_mine.json"]
    assert diff["modified"] == [] and diff["deleted"] == []
    assert diff["unattributed_modified"] == ["keep.json"]
    assert diff["unattributed_deleted"] == ["doomed.json"]
    result = apply_rollback(proj, sid)
    assert [e["name"] for e in result["applied"]["recycled"]] == ["upload_mine.json"]
    assert victim.read_text(encoding="utf-8") == '{"records": [{"id": 2}]}', "他人改写不被回写"
    assert not doomed.exists(), "他人删除不被「恢复」"

    # 空收据（失败步）：一切变化都不归本步；回滚零动作
    sid2 = store.capture("curate.sync_updates")
    other = _ext(proj) / "upload_other.json"
    other.write_text('{"records": [{"id": 3}]}', encoding="utf-8")
    diff2 = store.finalize(sid2, mutation={"created": [], "modified": [], "deleted": []})
    assert diff2["created"] == [] and diff2["unattributed_created"] == ["upload_other.json"]
    res2 = apply_rollback(proj, sid2)
    assert res2["applied"]["recycled"] == [] and other.is_file(), "失败步窗口不得回滚他人文件"


def test_rollback_any_target_conflict_blocks_all_writes(proj):
    """全量 preflight CAS：多目标里任一已非写后状态 → 整体零修改，
    连无冲突目标也不先写；dry-run 计划同样不留执行项。"""
    store = SnapshotStore(proj)
    a = _ext(proj) / "upload_a.json"
    b = _ext(proj) / "upload_b.json"
    sid = store.capture("curate.search_online")
    a.write_text('{"records": [{"id": 1}]}', encoding="utf-8")
    b.write_text('{"records": [{"id": 2}]}', encoding="utf-8")
    store.finalize(sid, mutation={"created": ["upload_a.json", "upload_b.json"],
                                  "modified": [], "deleted": []})
    b.write_text('{"records": [{"id": 2}, {"id": 3}]}', encoding="utf-8")   # 后续编辑
    plan = plan_rollback(proj, sid)
    assert [c["name"] for c in plan["conflicts"]] == ["upload_b.json"]
    assert plan["conflicts"][0]["code"] == "state_conflict"
    assert plan["recycle_created"] == [] and plan["restore_bytes"] == [], \
        "阻塞时计划不留执行项（dry-run 与 apply 同口径）"
    res = apply_rollback(proj, sid)
    assert res["conflict"] is True and res["applied"]["recycled"] == []
    assert a.is_file() and b.is_file(), "连无冲突目标也不先写"
    assert not (proj / ".userdata" / "recycle").exists()


def test_rollback_missing_after_state_is_untrusted(proj):
    """缺写后指纹（旧格式/不完整快照）明确不可信、需人工——零修改，不隐式 overwrite。"""
    store = SnapshotStore(proj)
    sid = store.capture("curate.search_online")
    (_ext(proj) / "upload_x.json").write_text('{"records": [{"id": 9}]}', encoding="utf-8")
    store.finalize(sid, mutation={"created": ["upload_x.json"], "modified": [], "deleted": []})
    meta = store.load(sid)
    meta["diff"]["created"][0].pop("sha256")   # 模拟旧格式：缺写后指纹
    store.save_meta(sid, meta)
    plan = plan_rollback(proj, sid)
    assert [c["code"] for c in plan["conflicts"]] == ["missing_after_state"]
    res = apply_rollback(proj, sid)
    assert res["conflict"] is True and res["applied"]["recycled"] == []
    assert (_ext(proj) / "upload_x.json").is_file(), "不可信快照不得动任何文件"


def test_rollback_created_file_goes_to_recycle_not_deleted(proj):
    store = SnapshotStore(proj)
    sid = store.capture("curate.search_online")
    new_file = _ext(proj) / "upload_a.json"
    new_file.write_text('{"records": [{"id": 9}]}', encoding="utf-8")
    store.finalize(sid)
    # dry-run：只出计划，零副作用
    plan = plan_rollback(proj, sid)
    assert plan["recycle_created"] == ["upload_a.json"]
    assert new_file.is_file() and not (proj / ".userdata").exists()
    # apply：移入回收站（移动而非删除），manifest 留行，meta 记回退
    result = apply_rollback(proj, sid)
    assert [e["name"] for e in result["applied"]["recycled"]] == ["upload_a.json"]
    assert not new_file.is_file()
    recycle = proj / ".userdata" / "recycle"
    moved = list(recycle.glob("*_upload_a.json"))
    assert len(moved) == 1 and moved[0].read_text(encoding="utf-8") == '{"records": [{"id": 9}]}'
    rows = _manifest_rows(proj)
    assert rows and rows[0]["action"] == "trace_rollback"
    assert rows[0]["original_path"] == "database/external/upload_a.json"
    assert rows[0]["snapshot_id"] == sid
    assert store.load(sid)["rolled_back_at"]
    # 幂等：二次执行 → created 已不存在，如实 skipped，不再动
    result2 = apply_rollback(proj, sid)
    assert result2["applied"]["recycled"] == []
    assert any(s["name"] == "upload_a.json" for s in result2["skipped"])


def test_rollback_modified_with_preimage_restores_bytes(proj):
    store = SnapshotStore(proj)
    original = (_ext(proj) / "keep.json").read_bytes()
    sid = store.capture("curate.restore", preimage_paths=["keep.json"])
    (_ext(proj) / "keep.json").write_text('{"records": []}', encoding="utf-8")
    store.finalize(sid)
    plan = plan_rollback(proj, sid)
    assert plan["restore_bytes"] == ["keep.json"]
    result = apply_rollback(proj, sid)
    assert [e["name"] for e in result["applied"]["restored"]] == ["keep.json"]
    assert (_ext(proj) / "keep.json").read_bytes() == original


def test_rollback_deleted_with_preimage_restores_file(proj):
    store = SnapshotStore(proj)
    sid = store.capture("curate.remove", preimage_paths=["keep.json"])
    (_ext(proj) / "keep.json").unlink()
    diff = store.finalize(sid)
    assert [e["name"] for e in diff["deleted"]] == ["keep.json"]
    result = apply_rollback(proj, sid)
    assert [e["name"] for e in result["applied"]["restored"]] == ["keep.json"]
    assert (_ext(proj) / "keep.json").is_file()


def test_rollback_modified_without_preimage_fail_closed(proj):
    store = SnapshotStore(proj)
    sid = store.capture("curate.search_online")      # 没点名 preimage
    (_ext(proj) / "keep.json").write_text('{"records": [{"id": 3}]}', encoding="utf-8")
    store.finalize(sid)
    assert store.load(sid)["preimage_missing"] == ["keep.json"]
    plan = plan_rollback(proj, sid)
    assert [u["name"] for u in plan["unrestorable"]] == ["keep.json"]
    result = apply_rollback(proj, sid)
    assert result["applied"]["restored"] == []       # 拒动：宁可少退不毁数据
    assert (_ext(proj) / "keep.json").read_text(encoding="utf-8") == '{"records": [{"id": 3}]}'


def test_rollback_requires_finalized_snapshot(proj):
    store = SnapshotStore(proj)
    sid = store.capture("curate.search_online")
    with pytest.raises(SnapshotError):
        plan_rollback(proj, sid)
    with pytest.raises(SnapshotError):      # apply 同闸：未 finalize 绝不 silently 空退
        apply_rollback(proj, sid)


def test_rollback_unknown_snapshot_honest_error(proj):
    with pytest.raises(SnapshotError):
        plan_rollback(proj, "ghost")


def test_snapshot_list_and_bad_meta(proj):
    store = SnapshotStore(proj)
    sid = store.capture("curate.sync_updates")
    rows = store.list_snapshots()
    assert rows and rows[0]["snapshot_id"] == sid and rows[0]["verb"] == "curate.sync_updates"
    bad = store.dir / "broken"
    bad.mkdir()
    (bad / "meta.json").write_text("{bad", encoding="utf-8")
    rows = store.list_snapshots()
    assert any(r.get("error") for r in rows if r["snapshot_id"] == "broken")


def test_capture_rejects_unsafe_preimage_name(proj):
    store = SnapshotStore(proj)
    with pytest.raises(SnapshotError):
        store.capture("curate.remove", preimage_paths=["../base/10x-Visium.json"])
    with pytest.raises(SnapshotError):
        store.capture("curate.remove", preimage_paths=["ghost.json"])


def test_cli_dry_run_and_apply(proj, capsys):
    store = SnapshotStore(proj)
    sid = store.capture("curate.search_online")
    (_ext(proj) / "upload_b.json").write_text('{"records": []}', encoding="utf-8")
    store.finalize(sid)
    rc = main(["--root", str(proj), "--snapshot", sid])
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["recycle_created"] == ["upload_b.json"]
    assert (_ext(proj) / "upload_b.json").is_file()   # dry-run 零副作用
    rc = main(["--root", str(proj), "--snapshot", sid, "--apply"])
    assert rc == 0
    assert not (_ext(proj) / "upload_b.json").is_file()
    rc = main(["--root", str(proj), "--snapshot", "ghost"])
    assert rc == 2
    assert "回退失败" in capsys.readouterr().err


def test_cli_apply_with_unrestorable_exits_nonzero(proj, capsys):
    store = SnapshotStore(proj)
    sid = store.capture("curate.search_online")
    (_ext(proj) / "keep.json").write_text('{"records": [{"id": 4}]}', encoding="utf-8")
    store.finalize(sid)
    rc = main(["--root", str(proj), "--snapshot", sid, "--apply"])
    assert rc == 1                                     # 有 fail-closed 拒动项：如实非零
    assert "无 preimage" in capsys.readouterr().err


@pytest.mark.parametrize("created", [False, True])
def test_rollback_refuses_edits_after_snapshot(proj, created, capsys):
    store = SnapshotStore(proj)
    name = "upload_new.json" if created else "keep.json"
    sid = store.capture("curate.restore", preimage_paths=[] if created else [name])
    target = _ext(proj) / name
    target.write_bytes(b'after-operation')
    store.finalize(sid)
    target.write_bytes(b'later-user-edit')
    assert main(["--root", str(proj), "--snapshot", sid, "--apply"]) == 1
    assert target.read_bytes() == b'later-user-edit'
    assert "rolled_back_at" not in store.load(sid)
    result = json.loads(capsys.readouterr().out)
    assert result["skipped"][0]["code"] == "state_conflict"


def test_rollback_checks_version_after_acquiring_write_lock(proj, monkeypatch):
    from contextlib import contextmanager
    from dataset_recommender.agent.trace import rollback
    store = SnapshotStore(proj)
    sid = store.capture("curate.restore", preimage_paths=["keep.json"])
    target = _ext(proj) / "keep.json"
    target.write_bytes(b'after-operation')
    store.finalize(sid)
    @contextmanager
    def lock(root):
        target.write_bytes(b'changed-while-waiting')
        yield
    monkeypatch.setattr(rollback, "ingest_critical_section", lock)
    result = apply_rollback(proj, sid)
    assert not result["applied"]["restored"]
    assert target.read_bytes() == b'changed-while-waiting'


def test_rollback_mixed_targets_one_conflict_has_zero_data_changes(proj, monkeypatch):
    from dataset_recommender.agent.trace import rollback

    store = SnapshotStore(proj)
    ext = _ext(proj)
    doomed = ext / "upload_deleted.json"
    doomed.write_bytes(b"before-deletion")
    sid = store.capture("curate.sync_updates", preimage_paths=["keep.json", doomed.name])
    (ext / "keep.json").write_bytes(b"my-modification")
    doomed.unlink()
    created = ext / "upload_created.json"
    created.write_bytes(b"my-created")
    store.finalize(sid, mutation={"created": [created.name], "modified": ["keep.json"],
                                  "deleted": [doomed.name]})
    created.write_bytes(b"later-user-version")
    before = {p.name: p.read_bytes() for p in ext.iterdir() if p.is_file()}
    published = []
    monkeypatch.setattr(rollback, "publish_corpus_write", lambda root: published.append(root))
    plan = plan_rollback(proj, sid)
    assert plan["recycle_created"] == [] and plan["restore_bytes"] == []
    assert [c["name"] for c in plan["conflicts"]] == [created.name]
    result = apply_rollback(proj, sid)
    assert result["conflict"] and result["applied"] == {"recycled": [], "restored": []}
    assert {p.name: p.read_bytes() for p in ext.iterdir() if p.is_file()} == before
    assert not doomed.exists() and not (proj / ".userdata" / "recycle").exists()
    assert published == [] and not result["rolled_back"]


def _rollback_mutation(root, mode, created):
    from dataset_recommender.corpus import patch_package as pp

    store = SnapshotStore(root)
    if mode == "patch":
        account = "acct_rollback"
        if not created:
            pp.block_uids(root, account, ["seed"])
        target = pp.patch_path_for(root, account)
        before = target.read_bytes() if target.exists() else None
        with pp.bind_patch_scope(account):
            sid = store.capture("curate.remove")
            with pp.collect_patch_mutations() as receipts:
                pp.block_uids(root, account, ["mine"])
            store.finalize(sid, mutation={"patch": receipts})
        lock_path = target.with_suffix(".lock")
    else:
        target = _ext(root) / ("upload_new.json" if created else "keep.json")
        before = target.read_bytes() if target.exists() else None
        sid = store.capture("curate.restore", preimage_paths=[] if created else [target.name])
        target.write_bytes(b"operation-after")
        store.finalize(sid)
        lock_path = root / ".userdata" / "upload_ingest.lock"
    return store, sid, target, before, lock_path


@pytest.mark.parametrize("mode", ["directory", "patch"])
@pytest.mark.parametrize("created", [False, True])
def test_concurrent_rollback_keeps_completion_and_saves_under_os_lock(proj, monkeypatch, mode, created):
    import threading
    from contextlib import contextmanager
    from dataset_recommender.agent.trace import rollback
    from dataset_recommender.corpus import fs_utils, patch_package as pp

    store, sid, target, before, lock_path = _rollback_mutation(proj, mode, created)
    requested, release = threading.Event(), threading.Event()
    real_lock = pp.patch_critical_section if mode == "patch" else rollback.ingest_critical_section
    real_save = SnapshotStore.save_meta
    saved_under_lock, results, errors = [], {}, []

    @contextmanager
    def ordered_lock(*args):
        if threading.current_thread().name == "rollback-second":
            requested.set()
            assert release.wait(10)
        with real_lock(*args):
            yield

    def observe_save(self, snapshot_id, meta):
        try:
            handle = fs_utils.acquire_file_lock(lock_path, wait=False)
        except fs_utils.FileLockBusy:
            saved_under_lock.append(True)
        else:
            fs_utils.release_file_lock(handle)
            saved_under_lock.append(False)
        return real_save(self, snapshot_id, meta)

    def apply(label):
        try:
            results[label] = apply_rollback(proj, sid)
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(pp if mode == "patch" else rollback,
                        "patch_critical_section" if mode == "patch" else "ingest_critical_section",
                        ordered_lock)
    monkeypatch.setattr(SnapshotStore, "save_meta", observe_save)
    second = threading.Thread(target=apply, args=("second",), name="rollback-second")
    first = threading.Thread(target=apply, args=("first",), name="rollback-first")
    second.start()
    try:
        assert requested.wait(10)
        first.start()
        first.join(10)
        assert not first.is_alive() and not errors
        completed = store.load(sid)["rolled_back_at"]
    finally:
        release.set()
        second.join(10)
    assert not second.is_alive() and not errors
    assert results["first"]["rolled_back"] and results["second"]["rolled_back"]
    assert results["second"]["applied"] == {"recycled": [], "restored": []}
    assert store.load(sid)["rolled_back_at"] == completed
    assert (target.read_bytes() if target.exists() else None) == before
    assert saved_under_lock == [True, True]


@pytest.mark.parametrize("mode", ["directory", "patch"])
@pytest.mark.parametrize("change", ["finalized", "mode", "scope"])
def test_rollback_rechecks_meta_after_routing_lock(proj, monkeypatch, mode, change):
    from contextlib import contextmanager
    from dataset_recommender.agent.trace import rollback
    from dataset_recommender.corpus import patch_package as pp

    store, sid, target, _before, _lock_path = _rollback_mutation(proj, mode, False)
    original = target.read_bytes()
    real_lock = pp.patch_critical_section if mode == "patch" else rollback.ingest_critical_section
    changed_meta = []

    @contextmanager
    def changed_route(*args):
        meta = store.load(sid)
        meta[change] = {"finalized": False, "mode": "changed-mode", "scope": "other_account"}[change]
        store.save_meta(sid, meta)
        changed_meta.append((store.dir / sid / "meta.json").read_bytes())
        with real_lock(*args):
            yield

    monkeypatch.setattr(pp if mode == "patch" else rollback,
                        "patch_critical_section" if mode == "patch" else "ingest_critical_section",
                        changed_route)
    with pytest.raises(SnapshotError):
        apply_rollback(proj, sid)
    assert target.read_bytes() == original
    assert (store.dir / sid / "meta.json").read_bytes() == changed_meta[0]
    assert not (proj / ".userdata" / "recycle").exists()
