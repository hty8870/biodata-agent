from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any


SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
VERSION_RE = re.compile(r'^WEB_API_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', re.MULTILINE)


class MirrorError(RuntimeError):
    pass


class _PublicPythonSurface(ast.NodeTransformer):
    """Remove text that the public mirror may deliberately rewrite.

    Runtime structure, constants, branches, calls, and defaults remain in the
    tree. Only docstrings and Pydantic/OpenAPI title/description keyword values
    are normalized.
    """

    @staticmethod
    def _strip_docstring(node: Any) -> Any:
        if (
            getattr(node, "body", None)
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body = node.body[1:]
        return node

    def visit_Module(self, node: ast.Module) -> ast.AST:
        self.generic_visit(node)
        return self._strip_docstring(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        return self._strip_docstring(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        self.generic_visit(node)
        return self._strip_docstring(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.AST:
        self.generic_visit(node)
        return self._strip_docstring(node)

    def visit_keyword(self, node: ast.keyword) -> ast.AST:
        self.generic_visit(node)
        if node.arg in {"description", "title"}:
            node.value = ast.Constant("<public-text>")
        return node


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _python_surface(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise MirrorError(f"cannot parse Python mirror surface: {path}") from exc
    normalized = _PublicPythonSurface().visit(tree)
    return hashlib.sha256(ast.dump(normalized, include_attributes=False).encode()).hexdigest()


def _json_value(path: Path, *, drop_comment: bool = False) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MirrorError(f"cannot parse JSON mirror surface: {path}") from exc
    if drop_comment and isinstance(value, dict):
        value = dict(value)
        value.pop("_comment", None)
    return value


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise MirrorError(f"git {' '.join(args)} failed for {root}")
    return result.stdout.strip()


def _version(root: Path) -> str:
    text = (root / "src/dataset_recommender/app/webapp.py").read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if not match:
        raise MirrorError(f"WEB_API_VERSION missing in {root}")
    return match.group(1)


def load_manifest(public_root: Path) -> dict[str, Any]:
    path = public_root / "public-mirror.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MirrorError(f"invalid public mirror manifest: {path}") from exc
    if not isinstance(value, dict):
        raise MirrorError("public mirror manifest must be an object")
    return value


def validate_manifest(public_root: Path, manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise MirrorError("unsupported public mirror schema")
    if manifest.get("private_repository") != "hty8870/biodata-agent-private":
        raise MirrorError("unexpected private repository identity")
    if manifest.get("public_repository") != "hty8870/biodata-agent":
        raise MirrorError("unexpected public repository identity")
    commit = manifest.get("source_private_commit")
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise MirrorError("source_private_commit must be a full commit hash")
    if manifest.get("runtime_version") != _version(public_root):
        raise MirrorError("public mirror runtime_version is stale")
    pairs = manifest.get("reviewed_file_pairs")
    if not isinstance(pairs, list) or not pairs:
        raise MirrorError("reviewed_file_pairs must be non-empty")
    for item in pairs:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise MirrorError("invalid reviewed file-pair entry")
        for key in ("private_sha256", "public_sha256"):
            value = item.get(key)
            if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
                raise MirrorError(f"invalid {key} for {item.get('path')}")


def verify(private_root: Path, public_root: Path, manifest: dict[str, Any], *, allow_dirty: bool) -> dict[str, int]:
    validate_manifest(public_root, manifest)
    private_head = _git(private_root, "rev-parse", "HEAD")
    if private_head != manifest["source_private_commit"]:
        raise MirrorError(
            f"public mirror is stale: records {manifest['source_private_commit']}, private HEAD is {private_head}"
        )
    if not allow_dirty and _git(private_root, "status", "--porcelain"):
        raise MirrorError("private source is dirty; commit it before certifying the mirror")
    if _version(private_root) != manifest["runtime_version"]:
        raise MirrorError("private and public runtime versions differ")

    private_py = {
        path.relative_to(private_root).as_posix(): path
        for path in (private_root / "src/dataset_recommender").rglob("*.py")
    }
    public_py = {
        path.relative_to(public_root).as_posix(): path
        for path in (public_root / "src/dataset_recommender").rglob("*.py")
    }
    if private_py.keys() != public_py.keys():
        raise MirrorError("private/public Python file sets differ")
    python_checked = 0
    for relative in sorted(private_py):
        if _python_surface(private_py[relative]) != _python_surface(public_py[relative]):
            raise MirrorError(f"Python runtime surface drift: {relative}")
        python_checked += 1

    private_snapshots = {
        path.relative_to(private_root).as_posix(): path
        for folder in (private_root / "database/base", private_root / "database/external")
        for path in folder.glob("*.json")
    }
    public_snapshots = {
        path.relative_to(public_root).as_posix(): path
        for folder in (public_root / "database/base", public_root / "database/external")
        for path in folder.glob("*.json")
    }
    if private_snapshots.keys() != public_snapshots.keys():
        raise MirrorError("private/public metadata snapshot sets differ")
    for relative in sorted(private_snapshots):
        if _json_value(private_snapshots[relative]) != _json_value(public_snapshots[relative]):
            raise MirrorError(f"metadata snapshot drift: {relative}")

    eval_files = (
        "eval/eval_queries.json",
        "eval/eval_queries_dev.json",
        "eval/eval_queries_public_validation.json",
    )
    for relative in eval_files:
        if _json_value(private_root / relative, drop_comment=True) != _json_value(
            public_root / relative, drop_comment=True
        ):
            raise MirrorError(f"public evaluation surface drift: {relative}")

    exact_files = (
        "requirements/requirements-ci.lock",
        "services/telemetry-receiver/requirements.lock",
        "database/SOURCES.yml",
    )
    for relative in exact_files:
        if _sha256(private_root / relative) != _sha256(public_root / relative):
            raise MirrorError(f"exact mirror file drift: {relative}")

    for item in manifest["reviewed_file_pairs"]:
        relative = item["path"]
        if _sha256(private_root / relative) != item["private_sha256"]:
            raise MirrorError(f"reviewed private file changed: {relative}")
        if _sha256(public_root / relative) != item["public_sha256"]:
            raise MirrorError(f"reviewed public file changed: {relative}")

    forbidden_public = (
        ".github/workflows/deploy-web.yml",
        "eval/eval_queries_holdout.json",
        "eval/eval_recall_graded.json",
        "eval/eval_rerank_graded.json",
        "协同",
        "开发日志归档",
    )
    leaked = [relative for relative in forbidden_public if (public_root / relative).exists()]
    if leaked:
        raise MirrorError("private-only paths exist in public mirror: " + ", ".join(leaked))

    public_eval = _json_value(public_root / "eval/evaluation-manifest.json")
    query_sets = ((public_eval.get("entity_gap") or {}).get("query_sets") or [])
    if public_eval.get("audience") != "public" or any("holdout" in str(name) for name in query_sets):
        raise MirrorError("public evaluation manifest exposes or consumes a holdout")
    return {
        "python_files": python_checked,
        "metadata_snapshots": len(private_snapshots),
        "evaluation_files": len(eval_files),
        "reviewed_pairs": len(manifest["reviewed_file_pairs"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="verify the private-to-public mirror contract")
    parser.add_argument("--public-root", type=Path, default=Path.cwd())
    parser.add_argument("--private-root", type=Path)
    parser.add_argument("--manifest-only", action="store_true")
    parser.add_argument("--allow-private-dirty", action="store_true")
    args = parser.parse_args(argv)
    public_root = args.public_root.resolve()
    try:
        manifest = load_manifest(public_root)
        if args.manifest_only:
            validate_manifest(public_root, manifest)
            print("public mirror manifest: valid")
            return 0
        if args.private_root is None:
            raise MirrorError("--private-root is required unless --manifest-only is used")
        counts = verify(args.private_root.resolve(), public_root, manifest, allow_dirty=args.allow_private_dirty)
    except MirrorError as exc:
        print(f"public mirror verification failed: {exc}")
        return 1
    print("public mirror verified: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
