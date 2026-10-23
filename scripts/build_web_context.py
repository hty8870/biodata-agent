# -*- coding: utf-8 -*-
"""网页版镜像构建上下文打包器（手工发布用）。

职责边界：
- 只按 ``deploy/web/Dockerfile`` 的真实 ``COPY`` 源，从**干净的 Git 提交**里取 blob，
  生成服务器可直接解包的最小 tar.gz 与逐成员 SHA manifest；
- 不执行 docker/ssh/部署，也不读工作树内容（防止未提交、ignored 的用户上传混入镜像层）；
- 路径、秘密值与个人绝对路径校验复用 ``scripts/build_release.py`` 的既有实现，
  不另写第二套检测引擎。

消费点：手工发布在干净提交上运行本脚本；服务器核对 manifest 的 archive SHA 后解包到
新目录，再以既有 ``deploy/web/Dockerfile`` 构建镜像。镜像的版本/回退仍由镜像 tag 与
``deploy/web/deploy.sh`` 负责，本脚本不参与部署。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import shlex
import subprocess
import sys
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import build_release as release  # noqa: E402  （复用其路径/秘密/私有路径校验）

SCHEMA = "biodata-web-context/1"
DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCKERFILE = "deploy/web/Dockerfile"
VERSION_SOURCE = "src/dataset_recommender/app/webapp.py"
EXTRA_MEMBERS = (
    ".dockerignore",
    "scripts/smoke_web_image.py",
    "deploy/web/deploy.sh",
    "deploy/web/docker-compose.web.yml",
)
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")

# 纵深防御：即使未来 Dockerfile 误改成 COPY 整仓，这些路径也绝不允许进入镜像层。
# .env/缓存/构建产物等已由 build_release._excluded 覆盖，这里只补它没有列出的本机/研究目录。
FORBIDDEN_PARTS = frozenset({".git", ".userdata", ".claude", "models", "research", "工作笔记"})
FORBIDDEN_SUFFIXES = frozenset({".pem", ".key"})


class ContextError(RuntimeError):
    """构建上下文不满足发布合同。"""


@dataclass(frozen=True)
class GitEntry:
    relative: str
    mode: str
    sha: str


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            timeout=180,
        )
    except FileNotFoundError as exc:
        raise ContextError("git executable is required to build the web context") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContextError(f"git {' '.join(args)} failed: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise ContextError(f"git {' '.join(args)} failed: {detail[:500]}")
    return proc


def _validate_path(relative: str) -> None:
    try:
        release._validate_relative(relative)
    except release.ReleaseError as exc:
        raise ContextError(str(exc)) from exc


def _logical_lines(dockerfile: str):
    """合并 Dockerfile 续行；只用于识别 COPY，不解释其他指令。"""
    buffer = ""
    for raw in dockerfile.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        buffer = f"{buffer} {line}".strip() if buffer else line
        if buffer.endswith("\\"):
            buffer = buffer[:-1].rstrip()
            continue
        yield buffer
        buffer = ""
    if buffer:
        yield buffer


def parse_copy_sources(dockerfile: str) -> list[str]:
    """返回 Dockerfile 的 COPY 源（支持引号路径与 JSON 数组形式）。"""
    sources: list[str] = []
    for instruction in _logical_lines(dockerfile):
        match = re.match(r"^COPY\s+(.*)$", instruction, re.IGNORECASE)
        if not match:
            continue
        rest = match.group(1).strip()
        if rest.startswith("["):
            try:
                args = json.loads(rest)
            except json.JSONDecodeError as exc:
                raise ContextError(f"invalid JSON COPY instruction: {instruction}") from exc
            if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
                raise ContextError(f"invalid JSON COPY arguments: {instruction}")
        else:
            try:
                args = shlex.split(rest, posix=True)
            except ValueError as exc:
                raise ContextError(f"invalid COPY quoting: {instruction}") from exc
            if any(item.startswith("--") for item in args):
                raise ContextError(
                    "COPY flags are not supported by the web context builder; "
                    f"expand the exact sources instead: {instruction}"
                )
        if len(args) < 2:
            raise ContextError(f"COPY needs at least one source and a destination: {instruction}")
        for source in args[:-1]:
            source = source.strip().rstrip("/")
            if source in {"", "."}:
                raise ContextError(
                    "COPY of the whole repository is forbidden; list exact runtime sources"
                )
            if any(char in source for char in "*?["):
                raise ContextError(
                    f"COPY globs are not supported; list exact sources: {instruction}"
                )
            _validate_path(source)
            sources.append(source)
    if not sources:
        raise ContextError("Dockerfile has no COPY sources")
    return sorted(set(sources))


def _clean_commit(root: Path) -> str:
    status = _git(root, "status", "--porcelain", "--untracked-files=all").stdout
    if status.strip():
        sample = status.decode("utf-8", "replace").splitlines()[:5]
        raise ContextError(
            "working tree is dirty; commit or stash before building the web context "
            "(the context must come from a clean commit): " + "; ".join(sample)
        )
    commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}").stdout.decode("ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ContextError(f"HEAD is not a full commit id: {commit!r}")
    return commit


def _check_entry(mode: str, kind: str, sha: str, relative: str) -> GitEntry:
    _validate_path(relative)
    if kind != "blob" or mode not in {"100644", "100755"}:
        raise ContextError(
            f"COPY source is not a regular file: {relative} ({mode} {kind}); "
            "links, submodules and directories are not allowed as archive members"
        )
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise ContextError(f"unsupported Git object id for {relative}: {sha}")
    pure = PurePosixPath(relative)
    if any(part.casefold() in FORBIDDEN_PARTS for part in pure.parts):
        raise ContextError(f"forbidden runtime path in web context: {relative}")
    if relative.casefold().startswith(("database/trace/", "database/external/upload_")):
        raise ContextError(f"runtime output/user upload is forbidden in web context: {relative}")
    if pure.suffix.lower() in FORBIDDEN_SUFFIXES or pure.name.lower() in FORBIDDEN_SUFFIXES:
        raise ContextError(f"key-like file is forbidden in web context: {relative}")
    try:
        if release._excluded(relative):
            raise ContextError(f"excluded path would enter the web context: {relative}")
    except release.ReleaseError as exc:
        raise ContextError(str(exc)) from exc
    return GitEntry(relative=relative, mode=mode, sha=sha)


def _select_entries(root: Path, commit: str, paths: list[str]) -> dict[str, GitEntry]:
    proc = _git(root, "ls-tree", "-r", "-z", "--full-tree", commit, "--", *paths)
    entries: dict[str, GitEntry] = {}
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        meta, separator, path_bytes = record.partition(b"\t")
        if not separator:
            raise ContextError(f"unexpected git ls-tree record: {record[:80]!r}")
        try:
            relative = path_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContextError("Git path is not valid UTF-8; refusing an ambiguous archive name") from exc
        fields = meta.decode("ascii", "replace").split()
        if len(fields) != 3:
            raise ContextError(f"unexpected git ls-tree metadata: {meta[:80]!r}")
        entry = _check_entry(fields[0], fields[1], fields[2], relative)
        previous = entries.get(relative)
        if previous is not None and previous.sha != entry.sha:
            raise ContextError(f"duplicate path with different blobs: {relative}")
        entries[relative] = entry
    return entries


def _require_sources(entries: dict[str, GitEntry], sources: list[str], extras: tuple[str, ...]) -> None:
    paths = entries.keys()
    for source in sources:
        prefix = f"{source}/"
        if not any(path == source or path.startswith(prefix) for path in paths):
            raise ContextError(f"Dockerfile COPY source has no tracked files: {source}")
    for extra in extras:
        if extra not in entries:
            raise ContextError(f"required context file is not tracked at {extra}")


def _blob(root: Path, entry: GitEntry) -> bytes:
    payload = _git(root, "cat-file", "blob", entry.sha).stdout
    if len(payload) > release.MAX_RELEASE_FILE_BYTES:
        raise ContextError(f"web context file exceeds the per-file size limit: {entry.relative}")
    algorithm = "sha1" if len(entry.sha) == 40 else "sha256"
    actual = hashlib.new(algorithm, b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload)
    if actual.hexdigest() != entry.sha:
        raise ContextError(f"Git blob hash mismatch for {entry.relative}")
    return payload


def _scan_payload(payload: bytes, relative: str) -> None:
    """复用交付门的三类内容检查：秘密值、个人绝对路径、内部敏感词。"""
    try:
        release._reject_secret_value_bytes(payload, relative)
        release._reject_private_home_bytes(payload, relative)
        release._reject_release_forbidden_tokens_bytes(payload, relative)
    except release.ReleaseError as exc:
        raise ContextError(str(exc)) from exc
    except UnicodeError as exc:
        raise ContextError(f"web context text is not scannable: {relative}") from exc


def _product_version(entries: dict[str, GitEntry], root: Path) -> str:
    entry = entries.get(VERSION_SOURCE)
    if entry is None:
        raise ContextError(f"version anchor is missing from the web context: {VERSION_SOURCE}")
    try:
        return release._product_version_from_bytes(_blob(root, entry), VERSION_SOURCE)
    except release.ReleaseError as exc:
        raise ContextError(str(exc)) from exc


def _manifest_path(out: Path) -> Path:
    return out.with_name(f"{out.name}.manifest.json")


def _refuse_existing(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise ContextError(f"output already exists; refusing to overwrite: {path}")


def _write_archive(
    root: Path, entries: list[GitEntry], out: Path
) -> tuple[list[dict[str, object]], str, int]:
    tmp = out.with_name(f".{out.name}.{uuid.uuid4().hex}.tmp")
    members: list[dict[str, object]] = []
    total = 0
    try:
        with tmp.open("wb") as raw:
            # mtime=0 + 空文件名：同一提交重复打包得到逐字节相同的 archive，便于核 SHA。
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode="w",
                    format=tarfile.GNU_FORMAT,
                    encoding="utf-8",
                ) as tar:
                    for entry in entries:
                        payload = _blob(root, entry)
                        total += len(payload)
                        if total > release.MAX_RELEASE_TOTAL_BYTES:
                            raise ContextError("web context exceeds the total uncompressed size limit")
                        _scan_payload(payload, entry.relative)
                        info = tarfile.TarInfo(entry.relative)
                        info.size = len(payload)
                        info.mtime = 0
                        info.mode = 0o755 if entry.mode == "100755" else 0o644
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        info.type = tarfile.REGTYPE
                        tar.addfile(info, io.BytesIO(payload))
                        members.append(
                            {
                                "path": entry.relative,
                                "mode": entry.mode,
                                "bytes": len(payload),
                                "sha256": hashlib.sha256(payload).hexdigest(),
                            }
                        )
        size = tmp.stat().st_size
        if size > release.MAX_RELEASE_ARCHIVE_BYTES:
            raise ContextError("web context archive exceeds the archive size limit")
        digest = hashlib.sha256()
        with tmp.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        tmp.replace(out)
        return members, digest.hexdigest(), size
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def build_context(
    root: Path = DEFAULT_ROOT,
    out: Path | str | None = None,
    dockerfile: str = DEFAULT_DOCKERFILE,
) -> dict:
    """生成 tar.gz 与 manifest；返回写入的 manifest 内容。"""
    if out is None:
        raise ContextError("an output path is required")
    root = Path(root).resolve()
    out = Path(out)
    manifest_out = _manifest_path(out)
    if not root.is_dir():
        raise ContextError(f"project root does not exist: {root}")
    _validate_path(dockerfile)
    _refuse_existing(out)
    _refuse_existing(manifest_out)

    top = _git(root, "rev-parse", "--show-toplevel").stdout.decode("utf-8", "replace").strip()
    if Path(top).resolve() != root:
        raise ContextError("--root must be the Git top-level directory")

    commit = _clean_commit(root)
    dockerfile_entry = _select_entries(root, commit, [dockerfile]).get(dockerfile)
    if dockerfile_entry is None:
        raise ContextError(f"Dockerfile is not tracked: {dockerfile}")
    try:
        dockerfile_text = _blob(root, dockerfile_entry).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContextError(f"Dockerfile is not valid UTF-8: {dockerfile}") from exc
    sources = parse_copy_sources(dockerfile_text)
    extras = tuple(dict.fromkeys((dockerfile, *EXTRA_MEMBERS)))
    entries = _select_entries(root, commit, [*sources, *extras])
    _require_sources(entries, sources, extras)

    version = _product_version(entries, root)
    tag = f"{version}-g{commit[:8]}"
    if not TAG_RE.fullmatch(tag):
        raise ContextError(f"suggested image tag is invalid: {tag}")

    ordered = [entries[path] for path in sorted(entries)]
    out.parent.mkdir(parents=True, exist_ok=True)
    members, archive_sha, archive_size = _write_archive(root, ordered, out)
    try:
        manifest = {
            "schema": SCHEMA,
            "commit": commit,
            "version": version,
            "image_tag": tag,
            "dockerfile": dockerfile,
            "copy_sources": sources,
            "archive": {"file": out.name, "bytes": archive_size, "sha256": archive_sha},
            "members": members,
        }
        payload = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if len(payload) > release.MAX_RELEASE_MANIFEST_BYTES:
            raise ContextError("web context manifest exceeds the manifest size limit")
        tmp_manifest = manifest_out.with_name(f".{manifest_out.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp_manifest.write_bytes(payload)
            tmp_manifest.replace(manifest_out)
        except BaseException:
            tmp_manifest.unlink(missing_ok=True)
            raise
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_web_context.py",
        description=(
            "Build a minimal, clean web-image context tar.gz from the Dockerfile COPY sources "
            "of a clean Git commit, plus a per-member SHA manifest."
        ),
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Git top-level directory")
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="new .tar.gz path (must not exist; prefer outside the repository or a gitignored dir)",
    )
    parser.add_argument(
        "--dockerfile",
        default=DEFAULT_DOCKERFILE,
        help=f"tracked Dockerfile path relative to --root (default: {DEFAULT_DOCKERFILE})",
    )
    args = parser.parse_args(argv)
    try:
        manifest = build_context(args.root, args.out, args.dockerfile)
    except (ContextError, OSError) as exc:
        print(f"[web-context] ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"[web-context] commit  {manifest['commit']}")
    print(f"[web-context] version {manifest['version']}  tag {manifest['image_tag']}")
    print(f"[web-context] members {len(manifest['members'])}")
    print(
        f"[web-context] archive {manifest['archive']['file']} "
        f"{manifest['archive']['bytes']} bytes sha256={manifest['archive']['sha256']}"
    )
    print(f"[web-context] manifest {_manifest_path(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
