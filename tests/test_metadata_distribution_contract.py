from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import make_delivery as delivery  # noqa: E402
import sanitize_metadata_contacts as contacts  # noqa: E402


def _snapshots(root: Path = ROOT) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for folder in (root / "database" / "base", root / "database" / "external")
        for path in folder.glob("*.json")
    }


def test_every_snapshot_has_a_rights_manifest_entry() -> None:
    manifest = (ROOT / "database" / "SOURCES.yml").read_text(encoding="utf-8")
    declared = set(re.findall(r"(?m)^\s+snapshot:\s+([^\s#]+)\s*$", manifest))
    assert declared == _snapshots()
    assert "project code, not third-party records" in manifest
    assert "contact_data_policy:" in manifest


def test_live_metadata_contains_no_contact_email() -> None:
    assert contacts.scan(contacts.snapshot_paths()) == []


def test_contact_redaction_is_atomic_and_does_not_log_values(tmp_path: Path) -> None:
    database = tmp_path / "database" / "external"
    database.mkdir(parents=True)
    snapshot = database / "sample.json"
    snapshot.write_text('[{"description":"contact person@example.invalid"}]\n', encoding="utf-8")

    assert contacts.scan([snapshot], root=tmp_path) == [("database/external/sample.json", 1)]
    assert contacts.redact_file(snapshot) == 1
    assert contacts.scan([snapshot], root=tmp_path) == []
    assert "[contact removed]" in snapshot.read_text(encoding="utf-8")


def test_delivery_gate_reports_only_contact_location(tmp_path: Path) -> None:
    database = tmp_path / "database" / "base"
    database.mkdir(parents=True)
    snapshot = database / "sample.json"
    snapshot.write_text('[{"description":"person@example.invalid"}]\n', encoding="utf-8")

    assert delivery.scan_metadata_contacts([snapshot], root=tmp_path) == [
        {"path": "database/base/sample.json", "line": 1}
    ]
