"""Conversation project ownership, membership and archival regressions."""
import pytest
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from dataset_recommender.app.workspace_store import DEFAULT_PROJECT_COLOR, WorkspaceError, WorkspaceStore


def test_project_and_membership_survive_reopening(tmp_path):
    store = WorkspaceStore(tmp_path)
    project = store.create_project("alice", "肺癌目录")
    store.assign_conversation("alice", "conversation_1", project["id"])
    reopened = WorkspaceStore(tmp_path)
    assert reopened.list_projects("alice")[0]["id"] == project["id"]
    assert reopened.assignments("alice") == {"conversation_1": project["id"]}
    assert reopened.list_projects("bob") == []


def test_other_account_cannot_read_rename_archive_or_assign_project(tmp_path):
    store = WorkspaceStore(tmp_path)
    project = store.create_project("alice", "A")
    operations = [lambda: store.get_project("bob", project["id"]),
                  lambda: store.rename_project("bob", project["id"], "B"),
                  lambda: store.archive_project("bob", project["id"]),
                  lambda: store.assign_conversation("bob", "c1", project["id"])]
    for operation in operations:
        with pytest.raises(WorkspaceError) as exc:
            operation()
        assert exc.value.code == "project_not_found"
    assert store.get_project("alice", project["id"])["name"] == "A"


def test_move_and_remove_only_change_the_selected_account(tmp_path):
    store = WorkspaceStore(tmp_path)
    project = store.create_project("alice", "A")
    store.assign_conversation("alice", "shared_conversation_id", project["id"])
    store.assign_conversation("bob", "shared_conversation_id", "")
    assert store.assignments("alice")["shared_conversation_id"] == project["id"]
    assert store.assignments("bob")["shared_conversation_id"] == ""
    store.assign_conversation("alice", "shared_conversation_id", "")
    assert store.assignments("alice")["shared_conversation_id"] == ""


def test_archiving_preserves_conversations_and_restore_recovers_group(tmp_path):
    store = WorkspaceStore(tmp_path)
    project = store.create_project("alice", "A")
    store.resolve_conversation("alice", "c1", project["id"], title="one")
    store.archive_project("alice", project["id"])
    assert store.list_projects("alice") == []
    assert store.list_projects("alice", include_archived=True)[0]["archived"]
    assert store.list_conversations("alice")[0]["project_id"] == project["id"]
    with pytest.raises(WorkspaceError):
        store.resolve_conversation("alice", "c1", project["id"])
    store.archive_project("alice", project["id"], archived=False)
    assert store.resolve_conversation("alice", "c1", project["id"]) == project["id"]


def test_turn_does_not_silently_override_conversation_membership(tmp_path):
    store = WorkspaceStore(tmp_path)
    first, second = [store.create_project("alice", name) for name in ("first", "second")]
    assert store.resolve_conversation("alice", "c1", first["id"]) == first["id"]
    with pytest.raises(WorkspaceError) as exc:
        store.resolve_conversation("alice", "c1", second["id"])
    assert exc.value.code == "conversation_project_changed"
    store.assign_conversation("alice", "c1", second["id"])
    assert store.resolve_conversation("alice", "c1") == second["id"]


def test_global_conversations_need_no_project(tmp_path):
    store = WorkspaceStore(tmp_path)
    assert store.resolve_conversation("alice", "c1", "", title="global") == ""
    assert store.list_conversations("alice", project_id="")[0]["title"] == "global"


@pytest.mark.parametrize("name", ["", " ", "x" * 81, "bad\nname", "invisible\u200b"])
def test_invalid_project_name_is_rejected(tmp_path, name):
    with pytest.raises(WorkspaceError) as exc:
        WorkspaceStore(tmp_path).create_project("alice", name)
    assert exc.value.code == "invalid_name"


@pytest.mark.parametrize("identifier", ["../other", "a/b", "a\\b", "x" * 81])
def test_invalid_membership_identifier_is_rejected(tmp_path, identifier):
    with pytest.raises(WorkspaceError):
        WorkspaceStore(tmp_path).assign_conversation("alice", identifier, "")


def test_color_and_partial_edits_preserve_identity_membership_and_archive(tmp_path):
    store = WorkspaceStore(tmp_path)
    project = store.create_project("alice", "A", color="#8B5CF6")
    assert project["color"] == "#8b5cf6"
    store.assign_conversation("alice", "c1", project["id"])
    updated = store.update_project("alice", project["id"], color="#123AbC")
    assert updated["name"] == "A" and updated["color"] == "#123abc"
    renamed = store.rename_project("alice", project["id"], "Renamed")
    assert renamed["id"] == project["id"] and renamed["color"] == "#123abc"
    store.archive_project("alice", project["id"])
    reopened = WorkspaceStore(tmp_path)
    assert reopened.get_project("alice", project["id"], active=False)["color"] == "#123abc"
    reopened.archive_project("alice", project["id"], archived=False)
    assert reopened.assignments("alice") == {"c1": project["id"]}
    with pytest.raises(WorkspaceError) as exc:
        reopened.update_project("bob", project["id"], color="#ffffff")
    assert exc.value.code == "project_not_found"
    assert reopened.get_project("alice", project["id"])["color"] == "#123abc"


@pytest.mark.parametrize("color", ["red", "#abc", "#ffffff00", "#fffffg", "#123456\n", "url(example)", "#fff;display:none", None])
def test_invalid_colors_are_rejected_without_changing_a_project(tmp_path, color):
    store = WorkspaceStore(tmp_path)
    with pytest.raises(WorkspaceError):
        store.create_project("alice", "A", color=color)
    assert store.list_projects("alice") == []


def test_old_database_upgrade_is_concurrent_and_keeps_old_sql_writes_compatible(tmp_path):
    path = tmp_path / "workspace.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE conversation_projects (
            owner TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY(owner,id))""")
        conn.execute("INSERT INTO conversation_projects(owner,id,name,created_at,updated_at) VALUES('alice','old','Before','t','t')")
    with ThreadPoolExecutor(max_workers=6) as pool:
        snapshots = list(pool.map(lambda _: WorkspaceStore(tmp_path, db_path=path).get_project("alice", "old"), range(12)))
    assert all(row["id"] == "old" and row["name"] == "Before" and row["color"] == DEFAULT_PROJECT_COLOR for row in snapshots)
    store = WorkspaceStore(tmp_path, db_path=path)
    store.update_project("alice", "old", color="#abcdef")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE conversation_projects SET name='Old client' WHERE owner='alice' AND id='old'")
        conn.execute("INSERT INTO conversation_projects(owner,id,name,created_at,updated_at) VALUES('alice','new','Legacy create','t','t')")
    assert store.get_project("alice", "old")["color"] == "#abcdef"
    assert store.get_project("alice", "new")["color"] == DEFAULT_PROJECT_COLOR


def test_project_order_persists_and_name_color_archive_do_not_reset_it(tmp_path):
    store = WorkspaceStore(tmp_path)
    projects = [store.create_project("alice", name) for name in ("A", "B", "C")]
    ids = [p["id"] for p in projects]
    assert [p["id"] for p in store.list_projects("alice")] == ids
    store.archive_project("alice", ids[1])
    store.reorder_projects("alice", [ids[2], ids[0]])
    store.update_project("alice", ids[2], name="Renamed", color="#abcdef")
    store.archive_project("alice", ids[1], archived=False)
    reopened = WorkspaceStore(tmp_path)
    assert [p["id"] for p in reopened.list_projects("alice")] == [ids[2], ids[1], ids[0]]
    new = reopened.create_project("alice", "D")
    assert reopened.list_projects("alice")[-1]["id"] == new["id"]
    for invalid in ([ids[0]], [ids[0], ids[0]], ["other_account"]):
        with pytest.raises(WorkspaceError):
            reopened.reorder_projects("alice", invalid)
    assert reopened.list_projects("alice")[-1]["id"] == new["id"]


def test_position_upgrade_preserves_legacy_display_order_per_account(tmp_path):
    path=tmp_path / "legacy-order.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE conversation_projects (
            owner TEXT NOT NULL,id TEXT NOT NULL,name TEXT NOT NULL,color TEXT NOT NULL DEFAULT '#0d9488',
            archived INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(owner,id))""")
        conn.executemany("INSERT INTO conversation_projects(owner,id,name,created_at,updated_at) VALUES(?,?,?,?,?)",
                         [("alice","a","A","t","1"),("alice","b","B","t","2"),("bob","c","C","t","3")])
    store=WorkspaceStore(tmp_path,db_path=path)
    assert [p["id"] for p in store.list_projects("alice")] == ["b","a"]
    assert store.list_projects("bob")[0]["position"] == 0
