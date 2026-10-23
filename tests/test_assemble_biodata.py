"""骨架新增文件与既有漂移保护，全部读写临时仓库。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import assemble_biodata as assembly


@pytest.fixture
def assembly_roots(tmp_path, monkeypatch):
    private, skeleton = tmp_path / "private", tmp_path / "skeleton"
    private.mkdir()
    (skeleton / ".git").mkdir(parents=True)
    for root in (private, skeleton):
        (root / "old.py").write_text("old", encoding="utf-8")
    (skeleton / "new.py").write_text("new", encoding="utf-8")
    assembly._write_assembly(private, skeleton_commit="a" * 40,
                             skeleton_files={"old.py": assembly._sha256_file(private / "old.py")})
    monkeypatch.setattr(assembly, "_skeleton_paths", lambda: ["old.py", "new.py"])
    monkeypatch.setattr(assembly, "_assembled_mappings", lambda: {})
    monkeypatch.setattr(assembly, "_skeleton_head_commit", lambda _: "b" * 40)
    return private, skeleton


def test_assemble_new_file_dry_run_then_apply_is_idempotent(assembly_roots, capsys):
    private, skeleton = assembly_roots
    before = (private / assembly.ASSEMBLY_PATH).read_bytes()
    args = dict(allow_drift=False, init_assembly=False)
    assert assembly.assemble(private, skeleton, write=False, **args) == 0
    assert "add: new.py" in capsys.readouterr().out
    assert not (private / "new.py").exists()
    assert (private / assembly.ASSEMBLY_PATH).read_bytes() == before
    assert assembly.assemble(private, skeleton, write=True, **args) == 0
    assert (private / "new.py").read_text() == "new"
    anchor = (private / assembly.ASSEMBLY_PATH).read_bytes()
    assert assembly.assemble(private, skeleton, write=True, **args) == 0
    assert (private / assembly.ASSEMBLY_PATH).read_bytes() == anchor


def test_assemble_refuses_removed_file_with_local_edits(assembly_roots, monkeypatch):
    """从新清单退役、但本地被改过的旧骨架文件：--write 前必须 refuse（旧实现直接删除）。

    漂移检查只遍历新 paths 时，removed=previous−paths 的文件当前哈希从不参与比对，
    手改内容会随删除与锚点刷新一起无声蒸发。
    """
    private, skeleton = assembly_roots
    (private / "old.py").write_text("local edit", encoding="utf-8")   # 本地改过的旧骨架文件
    monkeypatch.setattr(assembly, "_skeleton_paths", lambda: ["new.py"])  # 新清单里 old.py 已退役
    before = {p: p.read_bytes() for p in private.rglob("*") if p.is_file()}
    with pytest.raises(assembly.AssemblyError, match="装配快照不一致"):
        assembly.assemble(private, skeleton, write=True, allow_drift=False, init_assembly=False)
    assert (private / "old.py").read_text(encoding="utf-8") == "local edit"
    assert {p: p.read_bytes() for p in private.rglob("*") if p.is_file()} == before
    # 显式 --allow-skeleton-drift 仍是逃生口：确认后才允许退场
    assert assembly.assemble(private, skeleton, write=True, allow_drift=True, init_assembly=False) == 0
    assert not (private / "old.py").exists()


@pytest.mark.parametrize("mode", ["missing_old", "changed_old", "new_collision"])
def test_assemble_preserves_existing_file_guards(assembly_roots, mode):
    private, skeleton = assembly_roots
    if mode == "missing_old":
        (private / "old.py").unlink()
    elif mode == "changed_old":
        (private / "old.py").write_text("local edit")
    else:
        (private / "new.py").write_text("unrelated local file")
    before = {p: p.read_bytes() for p in private.rglob("*") if p.is_file()}
    with pytest.raises(assembly.AssemblyError):
        assembly.assemble(private, skeleton, write=True, allow_drift=False, init_assembly=False)
    assert {p: p.read_bytes() for p in private.rglob("*") if p.is_file()} == before
