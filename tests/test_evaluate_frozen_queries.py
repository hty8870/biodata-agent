"""冻结评测输入与显式重基线同步，不修改真实评测材料。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import evaluate_recommendation as ev
import promote_uploads as pu


def test_shipped_frozen_query_identity():
    contract = ev.validate_frozen_queries(ev.DEFAULT_QUERIES, ev.FROZEN_QUERY_CONTRACT)
    assert contract["counts"] == {"total": 54, "scored": 44, "no_result": 10}


@pytest.mark.parametrize("change", ["remove", "label", "id", "duplicate"])
def test_frozen_query_mutation_is_rejected(tmp_path, change):
    data = json.loads(ev.DEFAULT_QUERIES.read_text(encoding="utf-8"))
    rows = data["queries"]
    if change == "remove":
        data["queries"] = [q for q in rows if q["id"] != "nr03"]
    elif change == "label":
        rows[0]["must_match"] = {}
    elif change == "id":
        rows[0]["id"] = "changed"
    else:
        rows[0]["id"] = rows[1]["id"]
    path = tmp_path / "queries.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        ev.validate_frozen_queries(path, ev.FROZEN_QUERY_CONTRACT)


def test_frozen_fingerprint_normalizes_checkout_line_endings(tmp_path):
    path = tmp_path / "queries.json"
    raw = ev.DEFAULT_QUERIES.read_bytes().replace(b"\r\n", b"\n")
    path.write_bytes(raw.replace(b"\n", b"\r\n"))
    ev.validate_frozen_queries(path, ev.FROZEN_QUERY_CONTRACT)


def test_frozen_metrics_reject_smaller_denominator():
    contract = json.loads(ev.FROZEN_QUERY_CONTRACT.read_text(encoding="utf-8"))
    metrics = {"Top1_Accuracy_%": 97.7, "Top5_Hit_Rate_%": 97.7,
               "Constraint_Violation_%": 0, "FASTQ_Violation_%": 0,
               "NoResult_Correct": "9/9", "n_queries": 53}
    assert ev.print_metrics({"metrics": metrics})  # 通用评测允许不同题数。
    assert not ev.print_metrics({"metrics": metrics}, query_contract=contract)


def test_default_cli_checks_identity_before_loading_pipeline(tmp_path, monkeypatch):
    path = tmp_path / "queries.json"
    path.write_text('{"queries": []}', encoding="utf-8")
    monkeypatch.setattr(ev, "DEFAULT_QUERIES", path)
    monkeypatch.setattr(ev, "_ensure_cli_utf8_stdout", lambda: None)
    monkeypatch.setattr(ev, "load_pipeline", lambda: pytest.fail("pipeline must not run"))
    monkeypatch.setattr(sys, "argv", ["evaluate_recommendation.py"])
    with pytest.raises(SystemExit) as exc:
        ev.main()
    assert exc.value.code == 2


def test_controlled_refresh_only_updates_contract(tmp_path, monkeypatch):
    (tmp_path / "eval").mkdir()
    (tmp_path / "automation").mkdir()
    query_path = tmp_path / "eval/eval_queries.json"
    query_path.write_text('{"queries": [{"id": "test", "query": "example"}]}', encoding="utf-8")
    original = query_path.read_bytes()
    monkeypatch.setattr(pu, "promote_uploads", lambda *_: pytest.fail("no base writes allowed"))
    argv = ["--project-root", str(tmp_path), "--refresh-frozen-queries"]
    with pytest.raises(SystemExit):
        pu.main(argv)
    assert not (tmp_path / "automation/frozen-queries.json").exists()
    assert pu.main(argv + ["--rebaseline-reason", "Authorized fixture revision"]) == 0
    contract = ev.validate_frozen_queries(query_path, tmp_path / "automation/frozen-queries.json")
    assert contract["counts"] == {"total": 1, "scored": 1, "no_result": 0}
    assert query_path.read_bytes() == original
    assert not (tmp_path / "database").exists()
