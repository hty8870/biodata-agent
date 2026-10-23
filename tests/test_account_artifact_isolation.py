"""Account boundaries for request replay and generated citation files."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.app import accounts, webapp
from dataset_recommender.app.runtime_paths import account_data_dir_for
from dataset_recommender.agent import agent_exec
from dataset_recommender.content import reuse_pack
from dataset_recommender.corpus.patch_package import bind_patch_scope


@pytest.fixture
def clients(monkeypatch, tmp_path):
    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(webapp, "_account_gate_required", lambda: False)
    users = {name: SimpleNamespace(id=name, username=name) for name in ("account_A", "account_B")}
    monkeypatch.setattr(accounts, "resolve_session", lambda token, **kw: users.get(token))
    with webapp._UTT_IDEM_LOCK:
        webapp._UTT_IDEM.clear()
    result = {}
    for name in users:
        client = TestClient(webapp.app, base_url="http://127.0.0.1")
        client.cookies.set(accounts.SESSION_COOKIE, name)
        result[name] = client
    yield result
    with webapp._UTT_IDEM_LOCK:
        webapp._UTT_IDEM.clear()


def test_same_request_id_is_independent_between_accounts(clients, monkeypatch):
    from dataset_recommender.agent import turn

    calls = []

    def route(text, **kwargs):
        owner = kwargs["principal"]
        calls.append(owner)
        return {"route": "none", "via": "rules", "echo_zh": owner, "query": "",
                "refine_plan": None, "plan": None, "retrieval": None, "needs_agent": False}

    monkeypatch.setattr(turn, "route_turn", route)
    payload = {"utterance": "synthetic identical request", "req_id": "shared-test-id"}
    a = clients["account_A"].post("/api/utterance", json=payload)
    b = clients["account_B"].post("/api/utterance", json=payload)
    replay = clients["account_A"].post("/api/utterance", json=payload)
    assert a.status_code == b.status_code == replay.status_code == 200
    assert a.json()["echo_zh"] == replay.json()["echo_zh"] == "account_A"
    assert b.json()["echo_zh"] == "account_B"
    assert calls == ["account_A", "account_B"]


def test_citation_download_checks_account_ownership(clients, tmp_path):
    owned = account_data_dir_for(tmp_path, ".userdata/citations", "account_A")
    owned.mkdir(parents=True)
    (owned / "sample.ris").write_bytes(b"synthetic account A citation")
    own = clients["account_A"].get("/api/citations/download", params={"f": "sample.ris"})
    other = clients["account_B"].get("/api/citations/download", params={"f": "sample.ris"})
    assert own.status_code == 200
    assert own.content == b"synthetic account A citation"
    assert other.status_code == 404


def test_same_second_exports_keep_both_results(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_exec, "_full_corpus", lambda root: [])
    monkeypatch.setattr(agent_exec.time, "strftime", lambda fmt: "20990101-000000")
    monkeypatch.setattr(reuse_pack, "build_pack_for_uids", lambda uids, records: {"uids": uids})
    monkeypatch.setattr(reuse_pack, "to_ris", lambda pack: ",".join(pack["uids"]))
    monkeypatch.setattr(reuse_pack, "to_bibtex", lambda pack: ",".join(pack["uids"]))
    with bind_patch_scope("account_A"):
        first = agent_exec._loop_cite_export({"uids": ["SYNTHETIC_A"]}, tmp_path)
        second = agent_exec._loop_cite_export({"uids": ["SYNTHETIC_B"]}, tmp_path)
    assert first["files"][0]["filename"] != second["files"][0]["filename"]
    assert Path(first["out_dir"]).name == "u_account_A"
    assert (Path(first["out_dir"]) / first["files"][0]["filename"]).read_text() == "SYNTHETIC_A"
    assert (Path(second["out_dir"]) / second["files"][0]["filename"]).read_text() == "SYNTHETIC_B"


@pytest.mark.parametrize("owner", ["../other", "/absolute", "a/b", "a\\b"])
def test_account_artifact_paths_reject_unsafe_owners(tmp_path, owner):
    with pytest.raises(ValueError):
        account_data_dir_for(tmp_path, ".userdata/citations", owner)
