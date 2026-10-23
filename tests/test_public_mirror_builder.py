from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_public_mirror", ROOT / "scripts" / "build_public_mirror.py"
)
assert SPEC and SPEC.loader
mirror = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mirror)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _private_fixture(root: Path) -> str:
    paths = {
        ".deliveryignore": ".deliveryignore\n",
        "README.md": "# Public-safe source\n",
        "src/dataset_recommender/app/webapp.py": 'WEB_API_VERSION = "9.0.0"\n',
        "eval/evaluation-manifest.public.json": '{"audience":"public"}\n',
        "packaging/public-mirror/policy.json": json.dumps(
            {
                "schema_version": 1,
                "transform_version": "3",
                "private_repository": "hty8870/biodata-agent-private",
                "public_repository": "hty8870/biodata-agent",
                "mappings": {
                    "eval/evaluation-manifest.json": "eval/evaluation-manifest.public.json"
                },
                "generated_paths": ["public-mirror.json"],
                "forbidden_output_prefixes": ["private/"],
                "forbidden_text_fragments": ["private/"],
                "allowed_email_paths": [],
                "allowed_global_ip_prefixes": [],
                "allowed_global_ip_paths": [],
                "allowed_private_reference_paths": ["packaging/public-mirror/policy.json"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        "packaging/public-mirror/files.txt": (
            "README.md\n"
            "eval/evaluation-manifest.json\n"
            "packaging/public-mirror/files.txt\n"
            "packaging/public-mirror/policy.json\n"
            "src/dataset_recommender/app/webapp.py\n"
        ),
    }
    for relative, text in paths.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    _git(root, "init")
    _git(root, "config", "user.name", "fixture")
    _git(root, "config", "user.email", "fixture@example.com")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "fixture")
    return _git(root, "rev-parse", "HEAD")


def test_render_is_deterministic_and_applies_only_declared_mapping(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    commit = _private_fixture(root)

    first, first_manifest = mirror.render_tree(root, source_commit=commit, source_dirty=False)
    second, second_manifest = mirror.render_tree(root, source_commit=commit, source_dirty=False)

    assert first == second
    assert first_manifest == second_manifest
    assert first["README.md"] == b"# Public-safe source\n"
    assert first["eval/evaluation-manifest.json"] == b'{"audience":"public"}\n'
    assert "eval/evaluation-manifest.public.json" not in first
    assert first_manifest["source_private_commit"] == commit
    assert first_manifest["source_dirty"] is False
    assert first_manifest["file_count"] == 5


def test_exact_compare_rejects_extra_missing_and_changed_paths(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir()
    commit = _private_fixture(private)
    expected, _ = mirror.render_tree(private, source_commit=commit, source_dirty=False)
    public = tmp_path / "public"
    public.mkdir()
    mirror._write_tree(public, expected)
    _git(public, "init")
    _git(public, "config", "user.name", "fixture")
    _git(public, "config", "user.email", "fixture@example.com")
    _git(public, "add", "-A")
    _git(public, "commit", "-m", "fixture")
    assert mirror.compare_public(public, expected) == []

    (public / "README.md").write_text("changed\n", encoding="utf-8")
    (public / "extra.txt").write_text("extra\n", encoding="utf-8")
    _git(public, "add", "extra.txt")
    errors = mirror.compare_public(public, expected)
    assert "content:README.md" in errors
    assert "extra:extra.txt" in errors


@pytest.mark.parametrize(
    "payload, message",
    [
        ("contact real.person@live-domain.test\n", "undeclared email"),
        ("production endpoint http://8.8.8.8/v1\n", "live global IP"),
        ("see private/design.md\n", "private material"),
        ("path " + "C:" + "\\Users\\alice\\secret.txt\n", "user home path"),
    ],
)
def test_public_surface_scan_fails_closed(tmp_path: Path, payload: str, message: str) -> None:
    root = tmp_path / "private"
    root.mkdir()
    commit = _private_fixture(root)
    (root / "README.md").write_text(payload, encoding="utf-8")
    with pytest.raises(mirror.MirrorBuildError, match=message):
        mirror.render_tree(root, source_commit=commit, source_dirty=True)


def test_allowlist_rejects_noncanonical_or_unsorted_paths(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    _private_fixture(root)
    (root / "packaging/public-mirror/files.txt").write_text(
        "src/dataset_recommender/app/webapp.py\nREADME.md\n", encoding="utf-8"
    )
    with pytest.raises(mirror.MirrorBuildError, match="sorted and unique"):
        mirror.load_output_paths(root)


def test_new_tracked_path_must_be_public_or_explicitly_private(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    commit = _private_fixture(root)
    extra = root / "src" / "dataset_recommender" / "new_feature.py"
    extra.write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", extra.relative_to(root).as_posix())
    _git(root, "commit", "-m", "unclassified")
    with pytest.raises(mirror.MirrorBuildError, match="not classified"):
        mirror.render_tree(root, source_commit=commit, source_dirty=False)


@pytest.mark.parametrize("target_kind", ["private", "nested", "unknown", "wrong_origin"])
def test_apply_rejects_unidentified_or_overlapping_target(tmp_path, target_kind):
    private = tmp_path / "private"
    private.mkdir()
    head = _private_fixture(private)
    expected, _ = mirror.render_tree(private, source_commit=head, source_dirty=False)
    public = tmp_path / "public"
    public.mkdir()
    _git(public, "init")
    if target_kind == "private":
        public = private
    elif target_kind == "nested":
        public = private / "child"
        public.mkdir()
    elif target_kind == "wrong_origin":
        _git(public, "remote", "add", "origin", "https://github.com/hty8870/biodata-agent-private.git")
    before = (private / "README.md").read_bytes()
    with pytest.raises(mirror.MirrorBuildError):
        mirror.apply_public(public, expected, private_root=private)
    assert (private / "README.md").read_bytes() == before


@pytest.mark.parametrize("origin", ["https://github.com/hty8870/biodata-agent.git",
                                   "git@github.com:hty8870/biodata-agent.git", None])
def test_apply_accepts_matching_public_identity(tmp_path, origin):
    private = tmp_path / "private"
    private.mkdir()
    head = _private_fixture(private)
    expected, _ = mirror.render_tree(private, source_commit=head, source_dirty=False)
    public = tmp_path / "public"
    public.mkdir()
    _git(public, "init")
    if origin:
        _git(public, "remote", "add", "origin", origin)
    else:
        mirror._write_tree(public, expected)
        _git(public, "add", "-A")
        _git(public, "-c", "user.name=fixture", "-c", "user.email=fixture@example.com",
             "commit", "-m", "fixture")
    mirror.apply_public(public, expected, private_root=private)
    assert (public / "README.md").read_bytes() == expected["README.md"]


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_compare_refuses_nonregular_without_reading(tmp_path, monkeypatch, kind):
    target = tmp_path / "README.md"
    if kind == "directory":
        target.mkdir()
    else:
        target.write_bytes(b"expected")
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda p: p == target or original(p))
    monkeypatch.setattr(mirror, "_public_files", lambda _: {"README.md"})

    def no_read(path):
        pytest.fail("非普通文件不应进入内容读取")

    monkeypatch.setattr(Path, "read_bytes", no_read)
    assert mirror.compare_public(tmp_path, {"README.md": b"expected"}) == ["non-regular:README.md"]


@pytest.mark.parametrize("value", ["C:/out/x", "C:x", "a:stream", "../x", "a/../x",
    "a//x", "a/./x", ".", "a/", "a\\x", "CON.txt", "a/NUL", "a/x.", "a/x ",
    'a/"x', "a/<x", "a/>x", "a/x|y", "a/?x", "a/*x", "a/\x01x"])
def test_relative_rejects_nonportable_paths(value):
    with pytest.raises(mirror.MirrorBuildError):
        mirror._relative(value)


def test_forbidden_prefix_uses_portable_casefold(tmp_path):
    head = _private_fixture(tmp_path)
    policy_path = tmp_path / "packaging/public-mirror/policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["mappings"]["PRIVATE/secret.md"] = "README.md"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    files_path = tmp_path / "packaging/public-mirror/files.txt"
    entries = files_path.read_text(encoding="utf-8").splitlines() + ["PRIVATE/secret.md"]
    files_path.write_text("\n".join(sorted(entries)) + "\n", encoding="utf-8")
    with pytest.raises(mirror.MirrorBuildError, match="private-only"):
        mirror.render_tree(tmp_path, source_commit=head, source_dirty=True)


@pytest.mark.parametrize("kind", ["is_symlink", "is_junction"])
def test_parent_link_refused_for_read_and_write(tmp_path, monkeypatch, kind):
    parent = tmp_path / "linked"
    parent.mkdir()
    original = getattr(Path, kind, lambda p: False)
    monkeypatch.setattr(Path, kind, lambda p: p == parent or original(p), raising=False)
    with pytest.raises(mirror.MirrorBuildError, match="linked mirror parent"):
        mirror._write_tree(tmp_path, {"linked/file.txt": b"must not be written"})
    assert not (parent / "file.txt").exists()
    monkeypatch.setattr(mirror, "_public_files", lambda _: {"linked/file.txt"})
    assert mirror.compare_public(tmp_path, {"linked/file.txt": b""}) == ["unsafe:linked/file.txt"]


def test_apply_preflights_all_paths_before_mutation(tmp_path, monkeypatch):
    target = tmp_path / "old.txt"
    target.write_bytes(b"keep")
    monkeypatch.setattr(mirror, "_validate_public_target", lambda *a: None)
    monkeypatch.setattr(mirror, "_git", lambda *a: "")
    monkeypatch.setattr(mirror, "_public_files", lambda _: {"old.txt"})
    with pytest.raises(mirror.MirrorBuildError):
        mirror.apply_public(tmp_path, {"C:/outside.txt": b"new"})
    assert target.read_bytes() == b"keep"


def _public_fixture(root: Path, expected: dict[str, bytes]) -> None:
    root.mkdir()
    mirror._write_tree(root, expected)
    _git(root, "init")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", "-A")


def test_verify_rejects_old_staged_blob_even_when_worktree_matches(tmp_path):
    import verify_public_mirror as verifier

    private = tmp_path / "private"
    private.mkdir()
    head = _private_fixture(private)
    expected, _ = mirror.render_tree(private, source_commit=head, source_dirty=False)
    public = tmp_path / "public"
    _public_fixture(public, expected)
    target = public / "README.md"
    target.write_bytes(b"staged obsolete content\n")
    _git(public, "add", "README.md")
    target.write_bytes(expected["README.md"])
    assert "index-content:README.md" in mirror.compare_public(public, expected)
    with pytest.raises(verifier.MirrorError, match="index-content:README.md"):
        verifier.verify(private, public)
    _git(public, "add", "README.md")
    assert mirror.compare_public(public, expected) == []
    assert verifier.verify(private, public)["source_private_commit"] == head


def test_compare_rejects_same_content_symlink_in_index(tmp_path):
    # 无需 OS symlink 权限：工作树仍为同字节普通文件，待提交 mode 已是链接。
    public = tmp_path / "public"
    expected = {"README.md": b"same content\n"}
    _public_fixture(public, expected)
    oid = _git(public, "rev-parse", ":README.md")
    _git(public, "update-index", "--cacheinfo", f"120000,{oid},README.md")
    assert mirror.compare_public(public, expected) == ["index-mode:README.md"]


def test_compare_requires_new_generated_files_to_be_staged(tmp_path):
    public = tmp_path / "public"
    expected = {"README.md": b"same content\n"}
    _public_fixture(public, expected)
    expected["new.txt"] = b"generated\n"
    (public / "new.txt").write_bytes(expected["new.txt"])
    assert "missing:new.txt" in mirror.compare_public(public, expected)
    _git(public, "add", "new.txt")
    assert mirror.compare_public(public, expected) == []


def test_index_comparison_honors_declared_checkout_line_endings(tmp_path):
    public = tmp_path / "public"
    expected = {".gitattributes": b"*.txt text eol=crlf\n", "name with space.txt": b"text\r\n"}
    _public_fixture(public, expected)
    assert _git(public, "show", ":name with space.txt") == "text"
    assert mirror.compare_public(public, expected) == []
    (public / "name with space.txt").write_bytes(b"old\r\n")
    _git(public, "add", "name with space.txt")
    (public / "name with space.txt").write_bytes(expected["name with space.txt"])
    assert mirror.compare_public(public, expected) == ["index-content:name with space.txt"]
