# -*- coding: utf-8 -*-
"""replay_digest 纯逻辑已知答案单测：浓缩形状与截断、parse_digest 必过/必败、lint 六条。"""
from __future__ import annotations

from dataset_recommender.app import replay_digest as D  # noqa: E402


def _rec(seq, kind, **payload):
    return {"seq": seq, "ts": 1.0, "kind": kind, "payload": payload}


# ---------------------------------------------------------------- 浓缩

def test_condense_search_shape():
    text = D.condense_record(_rec(3, "search", query="肺癌 单细胞", total=42, status="results",
                                  top=["数据集甲", "数据集乙"]))
    assert "回合 #3 用户（检索）: 肺癌 单细胞" in text
    assert "命中 42 条（results）" in text
    assert "数据集甲 ｜ 数据集乙" in text


def test_condense_chat_steps_and_fail_mark():
    text = D.condense_record(_rec(2, "chat", user="对比一下", reply="结论如下",
                                  steps=[{"verb_zh": "对比两个数据集", "ok": True},
                                         {"verb_zh": "导出引文", "ok": False}]))
    assert "回合 #2 用户: 对比一下" in text
    assert "步骤· 对比两个数据集" in text
    assert "步骤✗ 导出引文" in text
    assert "助手: 结论如下" in text


def test_condense_backfill_roles():
    assert "回合 #1 用户:" in D.condense_record(_rec(1, "backfill", role="user", text="你好"))
    assert "回合 #2 助手:" in D.condense_record(_rec(2, "backfill", role="assistant", text="在的"))


def test_condense_records_cap_keeps_head_and_tail():
    recs = [_rec(i, "chat", user="长" * 400, reply="长" * 400, steps=[]) for i in range(1, 41)]
    text = D.condense_records(recs, cap=6000)
    assert "回合 #1 " in text and "回合 #40 " in text
    assert "中间" in text and "省略" in text
    assert "回合 #20 用户" not in text


def test_seq_range():
    assert D.seq_range([_rec(3, "chat"), _rec(7, "chat")]) == (3, 7)
    assert D.seq_range([]) == (0, 0)


# ---------------------------------------------------------------- parse_digest

_CLEAN = """{"motivation": {"id": "start", "title": "找肺癌数据", "detail": "复用公开数据", "turn_refs": [1]},
"outputs": [{"id": "o1", "title": "锁定 GSE131907", "detail": "58 例肺癌", "turn_refs": [2, "3"]}],
"knowledge": [{"id": "k1", "title": "10x 平台", "detail": " droplet 单细胞", "turn_refs": []}],
"decisions": [{"id": "d1", "title": "放宽年份", "detail": "2018 后零命中", "turn_refs": [4]}],
"edges": [{"from": "start", "to": "o1", "label": "驱动", "turn_refs": [1]},
          {"from": "k1", "to": "d1", "label": "促成"},
          {"from": "ghost", "to": "o1", "label": "丢"},
          {"from": "o1", "to": "o1", "label": "自环"}]}"""


def test_parse_clean():
    out = D.parse_digest(_CLEAN)
    assert out is not None
    assert out["motivation"]["id"] == "start"
    assert out["outputs"][0]["turn_refs"] == [2, 3]  # 数字串归一为 int
    assert len(out["edges"]) == 2  # 未知端点与自环丢弃


def test_parse_tolerates_prose_and_fence():
    out = D.parse_digest("好的，结果如下：\n```json\n" + _CLEAN + "\n```\n以上。")
    assert out is not None and len(out["outputs"]) == 1


def test_parse_garbage_returns_none():
    assert D.parse_digest("完全不是 JSON") is None
    assert D.parse_digest("") is None
    assert D.parse_digest(None) is None
    assert D.parse_digest('{"outputs": [], "knowledge": [], "decisions": []}') is None  # 三类全空+无动机=失败


def test_parse_motivation_only_is_valid():
    """单轮对话的常态：模型只整理出动机、三类全空——这是有效 digest（图从动机开始长），
    不得判 parse_failed（2026-09-28 实测：首轮即如此，判失败会误报「这次没整理出来」）。"""
    out = D.parse_digest('{"motivation": {"id": "start", "title": "检索肺癌数据", "detail": "d", "turn_refs": [1]},'
                         '"outputs": [], "knowledge": [], "decisions": [], "edges": []}')
    assert out is not None
    assert out["motivation"]["title"] == "检索肺癌数据"
    assert out["outputs"] == [] and out["edges"] == []


def test_parse_bad_items_filtered_and_ids_synthesized():
    out = D.parse_digest('{"outputs": [{"title": "无id条目", "detail": "x"}, "坏项", {"id": 5}],'
                         ' "knowledge": [], "decisions": [], "edges": []}')
    assert out is not None
    assert len(out["outputs"]) == 2  # "坏项"（非 dict）被滤；数字 id 归一为字符串保留
    assert [it["id"] for it in out["outputs"]] == ["o1", "5"]


# ---------------------------------------------------------------- lint_digest

def _translated():
    """模拟 save_digest 翻译后的 digest（pid 形态）。"""
    return {
        "motivation": {"pid": "start", "title": "找肺癌数据", "turn_refs": [1]},
        "outputs": [{"pid": "o-0", "title": "锁定 GSE131907", "turn_refs": [2]}],
        "knowledge": [{"pid": "k-0", "title": "10x 平台", "turn_refs": [1]}],
        "decisions": [{"pid": "d-0", "title": "放宽年份", "turn_refs": [4]}],
        "edges": [{"from": "start", "to": "o-0", "label": "驱动", "turn_refs": [1]},
                  {"from": "k-0", "to": "d-0", "label": "促成", "turn_refs": []}],
    }


def test_lint_clean():
    assert D.lint_digest(_translated(), 1, 4) == []


def test_lint_out_of_range_refs():
    dg = _translated()
    dg["outputs"][0]["turn_refs"] = [2, 99]
    warns = D.lint_digest(dg, 1, 4)
    assert any("越界" in w and "#99" in w for w in warns)


def test_lint_duplicate_titles():
    dg = _translated()
    dg["knowledge"][0]["title"] = "锁定 GSE131907"
    warns = D.lint_digest(dg, 1, 4)
    assert any("标题重复" in w for w in warns)


def test_lint_backward_edge():
    dg = _translated()
    dg["edges"] = [{"from": "o-0", "to": "start", "label": "回看", "turn_refs": []}]
    warns = D.lint_digest(dg, 1, 4)
    assert any("时间逆向" in w for w in warns)


def test_lint_orphans_and_zero_edges():
    dg = _translated()
    dg["edges"] = []
    warns = D.lint_digest(dg, 1, 4)
    assert any("孤岛" in w for w in warns)
    assert any("没有任何因果边" in w for w in warns)


def test_lint_accepts_stable_motivation_id_after_regeneration():
    dg = _translated()
    dg["motivation"]["pid"] = "m-1"
    dg["edges"] = []
    warnings = D.lint_digest(dg, 1, 4)
    assert not any("孤岛：m-1" in w or "m-1、" in w for w in warnings)
