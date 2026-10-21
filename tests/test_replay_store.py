# -*- coding: utf-8 -*-
"""replay_store 已知答案单测：落盘回环、key 闸、id→pid 翻译、标注/快照/角标语义。
全部用 tmp_path 作 project_root（存储落 tmp_path/.userdata/replay/），天然隔离。"""
from __future__ import annotations

from copy import deepcopy

from dataset_recommender.app import replay_store as ST  # noqa: E402

P, C = "u1", "cABC"


def _load(root):
    return ST.load(root, P, C)


def _llm_digest():
    return {
        "motivation": {"id": "start", "title": "找肺癌数据", "detail": "d", "turn_refs": [1]},
        "outputs": [{"id": "o1", "title": "锁定 GSE131907", "detail": "d", "turn_refs": [2]}],
        "knowledge": [{"id": "k1", "title": "10x", "detail": "d", "turn_refs": []}],
        "decisions": [{"id": "d1", "title": "放宽年份", "detail": "d", "turn_refs": [3]}],
        "edges": [{"from": "start", "to": "o1", "label": "驱动", "turn_refs": [1]},
                  {"from": "o1", "to": "d1", "label": "促成", "turn_refs": []},
                  {"from": "ghost", "to": "o1", "label": "丢"}],
    }


def test_load_empty_skeleton(tmp_path):
    data = _load(tmp_path)
    assert data["digest"] is None and data["threads"] == {} and data["key"] == ""
    assert data["seen_pids"] == [] and data["skip_reason"] == ""


def test_ensure_key_first_set_then_match_then_mismatch(tmp_path):
    data = _load(tmp_path)
    assert ST.ensure_key(data, "k-1") is True and data["key"] == "k-1"
    ST.save(tmp_path, P, C, data)
    data2 = _load(tmp_path)
    assert ST.ensure_key(data2, "k-1") is True
    assert ST.ensure_key(data2, "k-OTHER") is False


def test_save_digest_translates_ids_and_lints(tmp_path):
    data = ST.save_digest(tmp_path, P, C, _llm_digest(), from_seq=1, to_seq=3, model="m-x")
    dg = data["digest"]
    assert dg["motivation"]["pid"] == "start"
    assert dg["outputs"][0]["pid"] == "o-0"
    assert dg["knowledge"][0]["pid"] == "k-0"
    assert dg["decisions"][0]["pid"] == "d-0"
    assert [e["eid"] for e in dg["edges"]] == ["e-0", "e-1"]  # ghost 端点丢弃后重编号
    assert dg["edges"][1]["from"] == "o-0" and dg["edges"][1]["to"] == "d-0"
    assert data["last_seq"] == 3
    assert dg["model"] == "m-x" and dg["parse_failed"] is False
    assert dg["lint"] == ["1 个条目无因果边（孤岛：k-0）"]  # k-0 无因果边：lint 如实报出
    assert data["skip_reason"] == ""


def test_find_point_and_resolve_edge(tmp_path):
    ST.save_digest(tmp_path, P, C, _llm_digest(), from_seq=1, to_seq=3, model="m")
    data = _load(tmp_path)
    point, section = ST.find_point(data, "o-0")
    assert section == "outputs" and point["title"] == "锁定 GSE131907"
    edge, frm, to = ST.resolve_edge(data, "e-1")
    assert edge["label"] == "促成" and frm["pid"] == "o-0" and to["pid"] == "d-0"
    assert ST.find_point(data, "nope") == (None, "")


def test_threads_annotations_snapshot_cycle(tmp_path):
    ST.save_digest(tmp_path, P, C, _llm_digest(), from_seq=1, to_seq=3, model="m")
    ST.append_thread(tmp_path, P, C, "o-0", "user", "这条结论可靠吗")
    ST.append_thread(tmp_path, P, C, "o-0", "user", "样本量多少")
    ST.append_thread(tmp_path, P, C, "session", "user", "sidechat 不算标注")
    data = _load(tmp_path)
    composed = ST.compose_annotations(data)
    assert composed["count"] == 2  # session 线程不计
    assert composed["items"][0]["head"].startswith("成果「锁定 GSE131907」")
    snap = ST.take_snapshot(tmp_path, P, C)
    assert snap["count"] == 2 and "样本量多少" in snap["text"]
    data2 = _load(tmp_path)
    assert data2["threads"].get("o-0") is None  # 打包后图上标注清空
    assert data2["threads"]["session"]  # sidechat 不动
    # 空标注再打包 = no-op 不落新快照
    assert ST.take_snapshot(tmp_path, P, C)["count"] == 0
    assert len(_load(tmp_path)["snapshots"]) == 1
    # 快照内继续标注
    sid = snap["snap_id"]
    s2 = ST.annotate_snapshot(tmp_path, P, C, sid, "o-0", "追加疑问")
    assert s2["count"] == 3
    assert ST.annotate_snapshot(tmp_path, P, C, "s-nope", "o-0", "x") is None


def test_edge_only_snapshot_carries_the_edge_and_both_endpoints(tmp_path):
    """只标一条关系 → 快照必须自带这条关系与两端 point/kind 闭包。

    旧实现只把「两端都被标注」的关系放进 snap["edges"]：只标边的用户拿到的快照没有 edges、
    也没有可画节点，前端 snapshotDigest 返回 null → 找回时回退当前图（重整理删图后什么都画不出）。
    context 端点只是画布闭包：不计 count、不带 notes。"""
    ST.save_digest(tmp_path, P, C, _llm_digest(), from_seq=1, to_seq=3, model="m")
    ST.append_thread(tmp_path, P, C, "e-1", "user", "这条因果站不住")
    snap = ST.take_snapshot(tmp_path, P, C)
    assert snap["count"] == 1                                  # 标注只有那条关系
    assert [it["pid"] for it in snap["items"]] == ["e-1", "o-0", "d-0"]
    assert snap["items"][0]["kind"] == "edge"
    assert [it["notes"] for it in snap["items"][1:]] == [[], []]   # 端点闭包不是标注
    assert [it["kind"] for it in snap["items"][1:]] == ["outputs", "decisions"]
    assert snap["items"][1]["point"]["title"] == "锁定 GSE131907"
    assert "锁定 GSE131907" in snap["items"][1]["head"]
    assert [(e["eid"], e["from"], e["to"]) for e in snap["edges"]] == [("e-1", "o-0", "d-0")]
    assert snap["edges"][0]["label"] == "促成"
    # 续标 context 端点合法追加，count 只随真标注增加（闭包节点不是只读装饰）
    s2 = ST.annotate_snapshot(tmp_path, P, C, snap["snap_id"], "o-0", "补一条")
    assert s2["count"] == 2
    assert [it["pid"] for it in s2["items"]] == ["e-1", "o-0", "d-0"]


def test_edge_only_snapshot_still_complete_after_redigest_drops_the_graph(tmp_path):
    """标注关系之后一次重整理把整张图换掉——点「添加到对话」时闭包仍必须完整。

    条目自带数据取自 digest，digest 找不到时回退 point_history（既有机制）：
    旧关系与两端都还能定位，快照不依赖当前布局。"""
    ST.save_digest(tmp_path, P, C, _llm_digest(), from_seq=1, to_seq=3, model="m")
    ST.append_thread(tmp_path, P, C, "e-1", "user", "这条因果站不住")
    ST.save_digest(tmp_path, P, C, {
        "motivation": {"id": "start", "title": "换过的动机", "detail": "d", "turn_refs": [4]},
        "outputs": [{"id": "n1", "title": "新成果", "detail": "d", "turn_refs": [4]}],
        "knowledge": [], "decisions": [], "edges": [],
    }, from_seq=1, to_seq=4, model="m")
    current = _load(tmp_path)
    assert current["digest"]["edges"] == []                    # 当前图上这条关系已经没了
    snap = ST.take_snapshot(tmp_path, P, C)
    assert snap["count"] == 1 and [it["pid"] for it in snap["items"]] == ["e-1", "o-0", "d-0"]
    assert [(e["eid"], e["from"], e["to"], e["label"]) for e in snap["edges"]] == [("e-1", "o-0", "d-0", "促成")]
    assert snap["items"][1]["point"]["title"] == "锁定 GSE131907"


def test_unseen_and_mark_seen(tmp_path):
    ST.save_digest(tmp_path, P, C, _llm_digest(), from_seq=1, to_seq=3, model="m")
    un = ST.unseen(_load(tmp_path))
    assert un["worth"] == ["o-0", "d-0"]  # 成果+决策点
    assert un["knowledge"] == ["k-0"]
    ST.mark_seen(tmp_path, P, C)
    un2 = ST.unseen(_load(tmp_path))
    assert un2 == {"worth": [], "knowledge": []}


def test_redigest_reorder_and_insertion_preserve_point_and_edge_annotations(tmp_path):
    original = _llm_digest()
    ST.save_digest(tmp_path, P, C, original, from_seq=1, to_seq=3, model="m")
    ST.append_thread(tmp_path, P, C, "o-0", "user", "note about original")
    ST.append_thread(tmp_path, P, C, "e-1", "user", "note about relation")
    ST.mark_seen(tmp_path, P, C)
    revised = _llm_digest()
    revised["outputs"].insert(0, {"id": "new", "title": "新成果", "detail": "new", "turn_refs": [4]})
    revised["edges"].reverse()
    result = ST.save_digest(tmp_path, P, C, revised, from_seq=1, to_seq=4, model="m")
    assert result["digest"]["outputs"][1]["pid"] == "o-0"
    assert result["digest"]["outputs"][0]["pid"] != "o-0"
    assert ST.unseen(result)["worth"] == [result["digest"]["outputs"][0]["pid"]]
    composed = ST.compose_annotations(result)
    assert "锁定 GSE131907" in composed["items"][0]["head"]
    assert "促成" in composed["items"][1]["head"]


def test_legacy_ids_and_removed_points_keep_original_annotation_subject(tmp_path):
    initial = ST.save_digest(tmp_path, P, C, _llm_digest(), from_seq=1, to_seq=3, model="m")
    initial.pop("point_history")  # 模拟升级前的磁盘结构
    ST.save(tmp_path, P, C, initial)
    ST.append_thread(tmp_path, P, C, "o-0", "user", "原成果的标注")
    ST.append_thread(tmp_path, P, C, "start", "user", "原动机的标注")
    changed = _llm_digest()
    changed["outputs"] = []
    changed["motivation"]["title"] = "新的动机"
    ST.save_digest(tmp_path, P, C, changed, from_seq=1, to_seq=4, model="m")
    saved = _load(tmp_path)
    assert saved["digest"]["motivation"]["pid"] != "start"
    snapshot = ST.take_snapshot(tmp_path, P, C)
    assert "锁定 GSE131907" in snapshot["text"] and "找肺癌数据" in snapshot["text"]
    assert "新的动机" not in snapshot["text"]
    restored = ST.save_digest(tmp_path, P, C, deepcopy(_llm_digest()), from_seq=1, to_seq=5, model="m")
    assert restored["digest"]["outputs"][0]["pid"] == "o-0"
