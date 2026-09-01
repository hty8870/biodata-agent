from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_public_mirror", ROOT / "scripts" / "verify_public_mirror.py"
)
assert SPEC and SPEC.loader
mirror = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mirror)


def test_public_mirror_manifest_is_current_for_this_public_tree() -> None:
    manifest = mirror.load_manifest(ROOT)
    mirror.validate_manifest(ROOT, manifest)


def test_public_mirror_does_not_contain_private_surfaces() -> None:
    forbidden = (
        ".github/workflows/deploy-web.yml",
        "eval/eval_queries_holdout.json",
        "eval/eval_recall_graded.json",
        "eval/eval_rerank_graded.json",
        "协同",
        "开发日志归档",
    )
    assert not [relative for relative in forbidden if (ROOT / relative).exists()]


def test_public_evaluation_manifest_never_consumes_holdout() -> None:
    value = mirror._json_value(ROOT / "eval" / "evaluation-manifest.json")
    assert value["audience"] == "public"
    query_sets = value["entity_gap"]["query_sets"]
    assert query_sets
    assert not any("holdout" in str(name) for name in query_sets)
