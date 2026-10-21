"""Project APIs derive ownership from the authenticated request."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.app import accounts, webapp


@pytest.fixture
def clients(monkeypatch, tmp_path):
    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(webapp, "_account_gate_required", lambda: True)
    users = {name: SimpleNamespace(id=name, username=name) for name in ("alice", "bob")}
    monkeypatch.setattr(accounts, "resolve_session", lambda token, **kw: users.get(token))
    result = {}
    for name in users:
        client = TestClient(webapp.app, base_url="http://127.0.0.1")
        client.cookies.set(accounts.SESSION_COOKIE, name)
        result[name] = client
    return result


def test_project_crud_and_conversation_move_are_owner_scoped(clients):
    a, b = clients["alice"], clients["bob"]
    created = a.post("/api/conversation-projects", json={"name": "Project A"})
    assert created.status_code == 200
    project_id = created.json()["project"]["id"]
    assert a.patch(f"/api/conversation-projects/{project_id}", json={"name": "Renamed"}).status_code == 200
    assert b.patch(f"/api/conversation-projects/{project_id}", json={"name": "Hijacked"}).status_code == 404
    assert a.put("/api/conversations/c1/project", json={"project_id": project_id}).status_code == 200
    assert b.put("/api/conversations/c1/project", json={"project_id": project_id}).status_code == 404
    assert a.get("/api/conversation-projects").json()["assignments"] == {"c1": project_id}
    assert b.get("/api/conversation-projects").json() == {"ok": True, "projects": [], "assignments": {}}
    assert a.put("/api/conversations/c1/project", json={"project_id": ""}).status_code == 200
    assert a.get("/api/conversation-projects").json()["assignments"]["c1"] == ""


def test_archive_restore_preserves_membership(clients):
    a = clients["alice"]
    project_id = a.post("/api/conversation-projects", json={"name": "A"}).json()["project"]["id"]
    a.put("/api/conversations/c1/project", json={"project_id": project_id})
    assert a.post(f"/api/conversation-projects/{project_id}/archive", json={}).json()["project"]["archived"]
    snapshot = a.get("/api/conversation-projects").json()
    assert snapshot["assignments"] == {"c1": project_id}
    assert snapshot["projects"][0]["archived"]
    assert not a.post(f"/api/conversation-projects/{project_id}/archive", json={"archived": False}).json()["project"]["archived"]


def test_project_api_rejects_anonymous_and_owner_payload_override(clients):
    anonymous = TestClient(webapp.app, base_url="http://127.0.0.1")
    assert anonymous.get("/api/conversation-projects").status_code == 401
    assert clients["alice"].post("/api/conversation-projects", json={"name": "A", "owner": "bob"}).status_code == 422


def test_color_create_and_partial_update_are_validated_and_account_owned(clients):
    a, b = clients["alice"], clients["bob"]
    project = a.post("/api/conversation-projects", json={"name": "A", "color": "#8B5CF6"}).json()["project"]
    assert project["color"] == "#8b5cf6"
    url = f"/api/conversation-projects/{project['id']}"
    changed = a.patch(url, json={"color": "#123ABC"})
    assert changed.status_code == 200
    assert changed.json()["project"]["name"] == "A" and changed.json()["project"]["color"] == "#123abc"
    assert a.patch(url, json={"name": "Renamed"}).json()["project"]["color"] == "#123abc"
    assert b.patch(url, json={"color": "#ffffff"}).status_code == 404
    assert a.patch(url, json={}).status_code == 422
    for color in ("red", "#abc", "#ffffff00", "url(example)", "#fff;display:none"):
        assert a.patch(url, json={"color": color}).status_code == 422
        assert a.post("/api/conversation-projects", json={"name": "Bad", "color": color}).status_code == 422
    assert a.get("/api/conversation-projects").json()["projects"][0]["color"] == "#123abc"


def test_reordering_requires_the_exact_owned_active_project_set(clients):
    a,b=clients["alice"],clients["bob"]
    ids=[a.post("/api/conversation-projects",json={"name": name}).json()["project"]["id"] for name in ("A","B")]
    bob_id=b.post("/api/conversation-projects",json={"name":"Other"}).json()["project"]["id"]
    url="/api/conversation-projects/reorder"
    response=a.post(url,json={"project_ids":ids[::-1]})
    assert response.status_code == 200
    assert [p["id"] for p in response.json()["projects"]] == ids[::-1]
    assert a.post(url,json={"project_ids":ids[:1]}).status_code == 409
    assert a.post(url,json={"project_ids":[ids[0],bob_id]}).status_code == 409
    assert a.post(url,json={"project_ids":[ids[0],ids[0]]}).status_code == 400
    assert b.get("/api/conversation-projects").json()["projects"][0]["id"] == bob_id
    assert [p["id"] for p in a.get("/api/conversation-projects").json()["projects"]] == ids[::-1]


def test_turn_captures_validated_project_including_stream_worker(clients, monkeypatch):
    from dataset_recommender.agent import turn
    from dataset_recommender.agent.learning_scope import current_project_id
    a, b = clients["alice"], clients["bob"]
    project_id = a.post("/api/conversation-projects", json={"name": "A"}).json()["project"]["id"]

    def route(text, **kwargs):
        return {"route": "none", "via": "rules", "echo_zh": current_project_id(),
                "query": "", "plan": None, "retrieval": None}

    monkeypatch.setattr(turn, "route_turn", route)
    payload = {"utterance": "synthetic request", "project_id": project_id, "conv_id": "c_project_test"}
    assert a.post("/api/utterance", json=payload).json()["echo_zh"] == project_id
    streamed = a.post("/api/utterance", json={**payload, "stream": True})
    assert streamed.status_code == 200 and project_id in streamed.text
    assert b.post("/api/utterance", json=payload).status_code == 404
