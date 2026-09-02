from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_public_mirror as builder  # noqa: E402


COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class MirrorError(RuntimeError):
    pass


def load_manifest(public_root: Path) -> dict[str, Any]:
    path = public_root / builder.MANIFEST_PATH
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MirrorError(f"invalid public mirror manifest: {path}") from exc
    if not isinstance(value, dict):
        raise MirrorError("public mirror manifest must be an object")
    return value


def validate_manifest(public_root: Path, manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 2 or manifest.get("transform_version") != "3":
        raise MirrorError("unsupported public mirror manifest schema/transform")
    if manifest.get("private_repository") != "hty8870/biodata-agent-private":
        raise MirrorError("unexpected private repository identity")
    if manifest.get("public_repository") != "hty8870/biodata-agent":
        raise MirrorError("unexpected public repository identity")
    commit = manifest.get("source_private_commit")
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise MirrorError("source_private_commit must be a full commit hash")
    if manifest.get("source_dirty") is not False:
        raise MirrorError("certified public mirror must come from a clean private commit")
    for key in ("policy_sha256", "tree_sha256"):
        value = manifest.get(key)
        if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
            raise MirrorError(f"{key} must be SHA-256")
    if not isinstance(manifest.get("file_count"), int) or manifest["file_count"] <= 0:
        raise MirrorError("file_count must be positive")
    try:
        public_version = builder._runtime_version(public_root)
    except builder.MirrorBuildError as exc:
        raise MirrorError(str(exc)) from exc
    if manifest.get("runtime_version") != public_version:
        raise MirrorError("public mirror runtime_version is stale")


def verify(private_root: Path, public_root: Path) -> dict[str, Any]:
    manifest = load_manifest(public_root)
    validate_manifest(public_root, manifest)
    try:
        private_head, private_dirty = builder._source_state(private_root, allow_dirty=False)
    except builder.MirrorBuildError as exc:
        raise MirrorError(str(exc)) from exc
    if private_dirty or private_head != manifest["source_private_commit"]:
        raise MirrorError(
            f"public mirror source is stale: manifest={manifest['source_private_commit']} private={private_head}"
        )
    try:
        expected, expected_manifest = builder.render_tree(
            private_root,
            source_commit=private_head,
            source_dirty=False,
        )
        errors = builder.compare_public(public_root, expected)
    except builder.MirrorBuildError as exc:
        raise MirrorError(str(exc)) from exc
    if errors:
        raise MirrorError("exact public tree drift: " + ", ".join(errors[:20]))
    if expected_manifest != manifest:
        raise MirrorError("public manifest does not equal deterministic generator output")
    return expected_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="verify the exact private-to-public generated tree")
    parser.add_argument("--public-root", type=Path, default=Path.cwd())
    parser.add_argument("--private-root", type=Path)
    parser.add_argument("--manifest-only", action="store_true")
    args = parser.parse_args(argv)
    public_root = args.public_root.resolve()
    try:
        if args.manifest_only:
            manifest = load_manifest(public_root)
            validate_manifest(public_root, manifest)
            print(
                f"public mirror manifest valid: files={manifest['file_count']} "
                f"tree={manifest['tree_sha256']}"
            )
            return 0
        if args.private_root is None:
            raise MirrorError("--private-root is required unless --manifest-only is used")
        manifest = verify(args.private_root.resolve(), public_root)
    except MirrorError as exc:
        print(f"public mirror verification failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"public mirror exact: files={manifest['file_count']} "
        f"tree={manifest['tree_sha256']} source={manifest['source_private_commit']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
