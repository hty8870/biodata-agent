# -*- coding: utf-8 -*-
"""web 镜像构建上下文：只收干净提交中的 Dockerfile COPY 源，并复用既有交付校验。"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_web_context as ctx  # noqa: E402


FIXTURE_DOCKERFILE = """\
FROM scratch
COPY deploy/web/requirements-web.lock /tmp/requirements-web.lock
COPY src/ src/
COPY web/ web/
COPY database/ database/
COPY scripts/run_web.py scripts/run_web.py
COPY "使用教程/数据集上传/数据集上传规范.md" "使用教程/数据集上传/数据集上传规范.md"
COPY .agents/skills/biodata-dataset-discovery/ .agents/skills/biodata-dataset-discovery/
"""

EXPECTED_MEMBERS = {
    ".agents/skills/biodata-dataset-discovery/SKILL.md",
    ".dockerignore",
    "database/base/x.json",
    "deploy/web/Dockerfile",
    "deploy/web/deploy.sh",
    "deploy/web/docker-compose.web.yml",
    "deploy/web/requirements-web.lock",
    "scripts/run_web.py",
    "scripts/smoke_web_image.py",
    "src/app.py",
    "src/dataset_recommender/app/webapp.py",
    "web/static/index.html",
    "使用教程/数据集上传/数据集上传规范.md",
}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return proc


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _commit(root: Path, message: str = "fixture") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "Fixture")
    _write(root, "deploy/web/Dockerfile", FIXTURE_DOCKERFILE)
    _write(root, ".dockerignore", ".env\nmodels/\n")
    _write(root, ".gitignore", ".env\nmodels/\n.userdata/\nsrc/ignored.py\n")
    _write(root, "deploy/web/requirements-web.lock", "attrs==1.0\n")
    _write(root, "deploy/web/deploy.sh", "#!/usr/bin/env bash\nexit 0\n")
    _write(root, "deploy/web/docker-compose.web.yml", "services: {}\n")
    _write(
        root,
        "src/dataset_recommender/app/webapp.py",
        'WEB_API_VERSION = "1.2.3"\n',
    )
    _write(root, "src/app.py", "print('hi')\n")
    _write(root, "web/static/index.html", "<html></html>\n")
    _write(root, "database/base/x.json", "{}\n")
    _write(root, "scripts/run_web.py", "print('run')\n")
    _write(root, "scripts/build_corpus_vectors.py", "print('vectors')\n")
    _write(root, "scripts/smoke_web_image.py", "print('smoke')\n")
    _write(root, "使用教程/数据集上传/数据集上传规范.md", "# 规范\n")
    _write(root, ".agents/skills/biodata-dataset-discovery/SKILL.md", "# skill\n")
    _commit(root)
    return root


def _members(out: Path) -> dict[str, bytes]:
    with tarfile.open(out, "r:gz") as archive:
        result = {}
        for member in archive.getmembers():
            assert member.isfile(), member.name
            handle = archive.extractfile(member)
            assert handle is not None
            result[member.name] = handle.read()
        return result


def test_builds_only_copy_sources_from_the_clean_commit(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "ctx.tar.gz"
    manifest = ctx.build_context(repo, out)
    commit = _git(repo, "rev-parse", "HEAD").stdout.strip()

    assert manifest["commit"] == commit
    assert manifest["version"] == "1.2.3"
    assert manifest["image_tag"] == f"1.2.3-g{commit[:8]}"
    assert set(manifest["copy_sources"]) == {
        ".agents/skills/biodata-dataset-discovery",
        "database",
        "deploy/web/requirements-web.lock",
        "scripts/run_web.py",
        "src",
        "web",
        "使用教程/数据集上传/数据集上传规范.md",
    }
    assert {item["path"] for item in manifest["members"]} == EXPECTED_MEMBERS
    assert "scripts/build_corpus_vectors.py" not in {item["path"] for item in manifest["members"]}

    payloads = _members(out)
    assert set(payloads) == EXPECTED_MEMBERS
    assert [item["path"] for item in manifest["members"]] == sorted(EXPECTED_MEMBERS)
    for item in manifest["members"]:
        assert hashlib.sha256(payloads[item["path"]]).hexdigest() == item["sha256"]
        assert len(payloads[item["path"]]) == item["bytes"]
    assert manifest["archive"]["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert manifest["archive"]["bytes"] == out.stat().st_size
    written = json.loads(ctx._manifest_path(out).read_text(encoding="utf-8"))
    assert written == manifest


def test_manifest_does_not_leak_local_absolute_paths(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "ctx.tar.gz"
    ctx.build_context(repo, out)
    text = ctx._manifest_path(out).read_text(encoding="utf-8")
    assert str(tmp_path) not in text
    assert str(repo) not in text


def test_manifest_members_are_read_from_git_blobs(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "ctx.tar.gz"
    manifest = ctx.build_context(repo, out)
    payloads = _members(out)
    for item in manifest["members"]:
        raw = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "blob", f"HEAD:{item['path']}"],
            check=True,
            capture_output=True,
        ).stdout
        assert hashlib.sha256(raw).hexdigest() == item["sha256"]
        assert payloads[item["path"]] == raw


def test_same_commit_produces_byte_identical_archive(repo: Path, tmp_path: Path) -> None:
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    one = ctx.build_context(repo, first)
    two = ctx.build_context(repo, second)
    assert first.read_bytes() == second.read_bytes()
    assert one["archive"]["sha256"] == two["archive"]["sha256"]


def test_ignored_and_untracked_files_never_enter_the_context(repo: Path, tmp_path: Path) -> None:
    (repo / "src/ignored.py").write_text("ignored\n", encoding="utf-8")
    (repo / "models").mkdir()
    (repo / "models/big.bin").write_bytes(b"\0" * 32)
    (repo / ".userdata").mkdir()
    (repo / ".userdata/accounts.json").write_text("{}\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=local\n", encoding="utf-8")
    manifest = ctx.build_context(repo, tmp_path / "ctx.tar.gz")
    paths = {item["path"] for item in manifest["members"]}
    assert paths == EXPECTED_MEMBERS
    assert not any(path.endswith("ignored.py") for path in paths)


@pytest.mark.parametrize("relative", ["../escape.py", "/absolute.py", "C:/x.py", "src\\x.py", "src/./x.py"])
def test_unsafe_paths_are_rejected(relative: str) -> None:
    with pytest.raises(ctx.ContextError):
        ctx._validate_path(relative)


@pytest.mark.parametrize(
    ("mode", "kind"),
    [("120000", "blob"), ("160000", "commit")],
)
def test_links_and_submodules_are_rejected(mode: str, kind: str) -> None:
    with pytest.raises(ctx.ContextError, match="not a regular file"):
        ctx._check_entry(mode, kind, "a" * 40, "src/link.py")


def test_dirty_tracked_tree_is_rejected(repo: Path, tmp_path: Path) -> None:
    (repo / "src/app.py").write_text("changed\n", encoding="utf-8")
    out = tmp_path / "ctx.tar.gz"
    with pytest.raises(ctx.ContextError, match="dirty"):
        ctx.build_context(repo, out)
    assert not out.exists()


def test_untracked_file_makes_the_tree_dirty(repo: Path, tmp_path: Path) -> None:
    (repo / "src/untracked.py").write_text("new\n", encoding="utf-8")
    with pytest.raises(ctx.ContextError, match="dirty"):
        ctx.build_context(repo, tmp_path / "ctx.tar.gz")


def test_missing_copy_source_is_rejected(repo: Path, tmp_path: Path) -> None:
    dockerfile = (repo / "deploy/web/Dockerfile").read_text(encoding="utf-8")
    _write(repo, "deploy/web/Dockerfile", dockerfile + "COPY missing/ missing/\n")
    _commit(repo, "missing copy")
    with pytest.raises(ctx.ContextError, match="no tracked files"):
        ctx.build_context(repo, tmp_path / "ctx.tar.gz")


def test_missing_version_anchor_is_rejected(repo: Path, tmp_path: Path) -> None:
    _write(repo, "src/dataset_recommender/app/webapp.py", "VERSION = '1.2.3'\n")
    _commit(repo, "no version")
    with pytest.raises(ctx.ContextError, match="WEB_API_VERSION"):
        ctx.build_context(repo, tmp_path / "ctx.tar.gz")


def test_secret_like_value_is_rejected(repo: Path, tmp_path: Path) -> None:
    token = "gh" + "p_" + "A" * 36
    _write(repo, "src/leak.py", f"TOKEN = {token!r}\n")
    _commit(repo, "secret")
    with pytest.raises(ctx.ContextError, match="secret-like"):
        ctx.build_context(repo, tmp_path / "ctx.tar.gz")


def test_personal_home_path_is_rejected(repo: Path, tmp_path: Path) -> None:
    home = "C:" + "\\Users\\" + "alice" + "\\workspace"
    _write(repo, "src/path.py", f"ROOT = {home!r}\n")
    _commit(repo, "home path")
    with pytest.raises(ctx.ContextError, match="home path"):
        ctx.build_context(repo, tmp_path / "ctx.tar.gz")


def test_env_file_is_rejected_even_when_tracked(repo: Path, tmp_path: Path) -> None:
    _write(repo, "src/.env", "A=1\n")
    _git(repo, "add", "-f", "src/.env")
    _git(repo, "commit", "-m", "tracked env")
    with pytest.raises(ctx.ContextError, match="excluded path"):
        ctx.build_context(repo, tmp_path / "ctx.tar.gz")


def test_key_like_file_is_rejected(repo: Path, tmp_path: Path) -> None:
    _write(repo, "src/server.key", "not-a-real-key\n")
    _commit(repo, "key")
    with pytest.raises(ctx.ContextError, match="key-like"):
        ctx.build_context(repo, tmp_path / "ctx.tar.gz")


@pytest.mark.parametrize(
    ("relative", "message"),
    [
        ("database/external/upload_user.json", "user upload"),
        ("database/trace/events.jsonl", "runtime output"),
        ("src/.key", "key-like"),
    ],
)
def test_runtime_outputs_and_key_dotfiles_are_rejected(relative: str, message: str) -> None:
    with pytest.raises(ctx.ContextError, match=message):
        ctx._check_entry("100644", "blob", "a" * 40, relative)


def test_existing_output_and_manifest_are_not_overwritten(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "ctx.tar.gz"
    out.write_bytes(b"old")
    with pytest.raises(ctx.ContextError, match="already exists"):
        ctx.build_context(repo, out)
    assert out.read_bytes() == b"old"

    fresh = tmp_path / "fresh.tar.gz"
    manifest = tmp_path / "fresh.tar.gz.manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(ctx.ContextError, match="already exists"):
        ctx.build_context(repo, fresh)
    assert not fresh.exists()


def test_parse_copy_sources_supports_quotes_json_and_rejects_unsafe_forms() -> None:
    text = (
        'COPY "使用教程/数据集上传/规范 文件.md" "使用教程/数据集上传/规范 文件.md"\n'
        'COPY ["src/a b.py", "src/a b.py"]\n'
        "COPY src/ src/\n"
    )
    assert ctx.parse_copy_sources(text) == [
        "src",
        "src/a b.py",
        "使用教程/数据集上传/规范 文件.md",
    ]
    with pytest.raises(ctx.ContextError, match="whole repository"):
        ctx.parse_copy_sources("COPY . /app\n")
    with pytest.raises(ctx.ContextError, match="flags"):
        ctx.parse_copy_sources("COPY --chown=1:1 src/ src/\n")
    with pytest.raises(ctx.ContextError, match="globs"):
        ctx.parse_copy_sources("COPY src/*.py src/\n")
    with pytest.raises(ctx.ContextError, match="no COPY"):
        ctx.parse_copy_sources("FROM scratch\n")


def test_repository_dockerfile_copy_sources_are_runtime_assets() -> None:
    text = (ROOT / "deploy/web/Dockerfile").read_text(encoding="utf-8")
    sources = ctx.parse_copy_sources(text)
    for expected in (
        "src",
        "web",
        "database",
        "scripts/run_web.py",
        "scripts/build_corpus_vectors.py",
        "deploy/web/requirements-web.lock",
        "使用教程/数据集上传/数据集上传规范.md",
        "使用教程/MCP安装/agent接入提示词.md",
        "使用教程/MCP安装/在线接入提示词模板.md",
        ".agents/skills/biodata-dataset-discovery",
    ):
        assert expected in sources
    assert not any(source.startswith(("tests", "docs", "research")) for source in sources)


def test_cli_builds_context(repo: Path, tmp_path: Path, capsys) -> None:
    out = tmp_path / "ctx.tar.gz"
    assert ctx.main(["--root", str(repo), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "sha256=" in printed
    assert "members" in printed
    assert out.is_file()


def test_cli_reports_dirty_tree_as_nonzero(repo: Path, tmp_path: Path, capsys) -> None:
    (repo / "src/app.py").write_text("changed\n", encoding="utf-8")
    out = tmp_path / "ctx.tar.gz"
    assert ctx.main(["--root", str(repo), "--out", str(out)]) == 2
    assert "dirty" in capsys.readouterr().err
    assert not out.exists()
