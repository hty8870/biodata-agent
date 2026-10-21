# -*- coding: utf-8 -*-
"""`ltr_features.ltr_feature_rows` 的 golden 逐位门 + 边缘已知答案。

为什么单列（2026-09-30 队列② R1）：9 维特征构造从 `vector_recall._ltr_fused_order`
抽成生产/离线共用单源。本文件钉死「**抽取逐位不变**」——`_reference_inline_rows` 是改造前
内联实现的**逐字复制**（只改函数名与签名），同一组合成池 + intent 下两个实现必须产出
**逐值相等**（`==`，不是 approx）的特征行；任何算术、顺序或取项口径漂移当场红。

边缘已知答案（lex_max=0 / vec 缺失 / 无约束查询 / raw 三态 / 空池 / 顺序旋转 / 未知名）
各自单列；其中 `dense[i] is None → has_vec=0、vec_norm=0` 是共享函数为离线回放显式定义的
语义（生产 dense 路径恒有分，参照实现里恒 1——两侧都在本文件钉住）。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval.ltr_features import ltr_feature_rows  # noqa: E402
from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402
from dataset_recommender.retrieval.retriever import RetrievedCandidate  # noqa: E402

FEATURE_ORDER = (
    "lex_norm", "vec_norm", "has_vec", "inv_pos",
    "matched_species", "matched_tissue", "matched_disease",
    "matched_has_raw_data", "raw_required",
)


# ---------------------------------------------------------------- golden 参照（改造前内联实现逐字复制）
def _reference_inline_rows(items, dense, intent, feature_order):
    """改造前 `vector_recall._ltr_fused_order` 的内联特征构造（2026-09-30 前版本逐字复制）。

    只作 golden 参照，生产代码不再走这里。参照实现里 dense 恒有分（生产 dense 路径语义）；
    无 vec 分（None）的情形是共享函数为离线回放新增的显式语义，由下方已知答案单独钉住。
    """
    if intent is None:
        return None
    from dataset_recommender.retrieval.retriever import constraint_satisfied  # 局部导入：同源风格

    lex_raw = [float(getattr(c, "score", 0.0) or 0.0) for c in items]
    lex_max = max(lex_raw) if lex_raw else 0.0
    hard = getattr(intent, "constraints", None) or {}
    soft = getattr(intent, "preferred_constraints", None) or {}
    raw_required = getattr(intent, "has_raw_data_required", None) is True

    def _matched(record, dim: str) -> float:
        terms = list(hard.get(dim) or []) + list(soft.get(dim) or [])
        if not terms:
            return 0.0
        return 1.0 if constraint_satisfied(record, dim, terms) else 0.0

    rows = []
    for i, cand in enumerate(items):
        record = cand.record
        feats = {
            "lex_norm": (lex_raw[i] / lex_max) if lex_max > 0 else 0.0,
            "vec_norm": float(dense[i]),
            "has_vec": 1.0,
            "inv_pos": 1.0 / (i + 1),
            "matched_species": _matched(record, "species"),
            "matched_tissue": _matched(record, "tissue"),
            "matched_disease": _matched(record, "disease"),
            "matched_has_raw_data": 1.0 if (raw_required and record.has_raw_data is True) else 0.0,
            "raw_required": 1.0 if raw_required else 0.0,
        }
        rows.append([feats[name] for name in feature_order])
    return rows


# ---------------------------------------------------------------- 夹具
def _rec(name: str, species: str = "", tissue: str = "", disease: str = "",
         has_raw_data=None) -> DatasetRecord:
    return DatasetRecord(
        dataset_name=name, species=species, tissue=tissue, disease=disease,
        chemistry="", count="", unit="", has_raw_data=has_raw_data, url="",
        source_file="x.json", description=f"desc of {name}", raw={},
    )


def _pool() -> "tuple[list[RetrievedCandidate], list[float]]":
    """固定合成池：刻意覆盖 0 分 / None 分 / 空字段 / raw 三态，让逐值断言真有区分度。"""
    items = [
        RetrievedCandidate(record=_rec("D0", "human", "breast", "breast cancer", True), score=10.0),
        RetrievedCandidate(record=_rec("D1", "mouse", "liver", "", False), score=6.0),
        RetrievedCandidate(record=_rec("D2", "human", "brain", "glioma", None), score=0.0),
        RetrievedCandidate(record=_rec("D3", "", "", "", True), score=None),
        RetrievedCandidate(record=_rec("D4", "human", "breast", "", False), score=3.5),
    ]
    dense = [0.5, 0.9, 0.1, 0.7, 0.3]
    return items, dense


def _intent(**kw) -> SimpleNamespace:
    base = {"constraints": {"species": ["human"], "disease": ["cancer"]},
            "preferred_constraints": {"tissue": ["breast"]},
            "has_raw_data_required": True}
    base.update(kw)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------- golden 逐位相等
def test_golden_rows_bit_equal_to_pre_refactor_inline():
    """逐位门：共享函数 == 改造前内联实现（逐值 ==，不是 approx）。"""
    items, dense = _pool()
    for intent in (
        _intent(),
        _intent(has_raw_data_required=None),
        _intent(has_raw_data_required=False),
        _intent(constraints={}, preferred_constraints={}),
        _intent(constraints={"tissue": ["brain"]}, preferred_constraints={"species": ["mouse"]}),
    ):
        assert ltr_feature_rows(items, dense, intent, FEATURE_ORDER) \
            == _reference_inline_rows(items, dense, intent, FEATURE_ORDER), \
            f"共享函数与改造前内联实现不一致（intent={intent!r}）"


def test_golden_rows_bit_equal_under_permuted_order():
    """feature_order 旋转/重排后仍逐位相等（证明两侧都严格按入参顺序组行）。"""
    items, dense = _pool()
    intent = _intent()
    rotated = FEATURE_ORDER[3:] + FEATURE_ORDER[:3]
    assert ltr_feature_rows(items, dense, intent, rotated) \
        == _reference_inline_rows(items, dense, intent, rotated)
    reversed_order = tuple(reversed(FEATURE_ORDER))
    assert ltr_feature_rows(items, dense, intent, reversed_order) \
        == _reference_inline_rows(items, dense, intent, reversed_order)


def test_golden_fallback_semantics_preserved():
    """回退语义逐位保留：intent None → None（与改造前相同）。"""
    items, dense = _pool()
    assert ltr_feature_rows(items, dense, None, FEATURE_ORDER) is None
    assert _reference_inline_rows(items, dense, None, FEATURE_ORDER) is None


# ---------------------------------------------------------------- 边缘已知答案（手工算好逐值比对）
def test_known_answer_lex_max_zero():
    """lex_max=0（全池 0 分/None 分）→ lex_norm 全 0，其余列照常。"""
    items = [RetrievedCandidate(record=_rec("A", "human"), score=0.0),
             RetrievedCandidate(record=_rec("B", "mouse"), score=None),
             RetrievedCandidate(record=_rec("C", "human"), score=0.0)]
    rows = ltr_feature_rows(items, [0.4, 0.2, 0.1], _intent(
        constraints={"species": ["human"]}, preferred_constraints={},
        has_raw_data_required=None), FEATURE_ORDER)
    col = {name: i for i, name in enumerate(FEATURE_ORDER)}
    assert [r[col["lex_norm"]] for r in rows] == [0.0, 0.0, 0.0]
    assert [r[col["inv_pos"]] for r in rows] == [1.0, 0.5, 1.0 / 3.0]
    assert [r[col["matched_species"]] for r in rows] == [1.0, 0.0, 1.0]
    assert [r[col["raw_required"]] for r in rows] == [0.0, 0.0, 0.0]


def test_known_answer_vec_missing():
    """dense 为 None 的候选 → has_vec=0、vec_norm=0（离线回放显式语义）；有分候选恒 1。"""
    items = [RetrievedCandidate(record=_rec("A", "human"), score=2.0),
             RetrievedCandidate(record=_rec("B", "mouse"), score=1.0)]
    rows = ltr_feature_rows(items, [0.25, None], _intent(
        constraints={}, preferred_constraints={}, has_raw_data_required=None), FEATURE_ORDER)
    col = {name: i for i, name in enumerate(FEATURE_ORDER)}
    assert rows[0][col["has_vec"]] == 1.0 and rows[0][col["vec_norm"]] == 0.25
    assert rows[1][col["has_vec"]] == 0.0 and rows[1][col["vec_norm"]] == 0.0


def test_known_answer_no_constraints():
    """无约束查询（hard/soft 全空、raw None）→ matched_* 与 raw 双槽全 0。"""
    items = [RetrievedCandidate(record=_rec("A", "human", "breast", "breast cancer", True), score=4.0)]
    rows = ltr_feature_rows(items, [0.6], _intent(
        constraints={}, preferred_constraints={}, has_raw_data_required=None), FEATURE_ORDER)
    assert rows == [[1.0, 0.6, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]]


def test_known_answer_preferred_only_and_raw_states():
    """仅软偏好命中也记 1；raw 三态：要求 raw 且记录 has_raw_data is True 才置 1。"""
    items = [
        RetrievedCandidate(record=_rec("A", "human", "breast", "", True), score=2.0),
        RetrievedCandidate(record=_rec("B", "mouse", "liver", "", None), score=1.0),
        RetrievedCandidate(record=_rec("C", "human", "brain", "", False), score=0.5),
    ]
    rows = ltr_feature_rows(items, [0.3, 0.2, 0.1], _intent(
        constraints={}, preferred_constraints={"tissue": ["breast"]},
        has_raw_data_required=True), FEATURE_ORDER)
    col = {name: i for i, name in enumerate(FEATURE_ORDER)}
    assert [r[col["matched_tissue"]] for r in rows] == [1.0, 0.0, 0.0]
    assert [r[col["matched_species"]] for r in rows] == [0.0, 0.0, 0.0], "无 species 项约束记 0"
    assert [r[col["matched_has_raw_data"]] for r in rows] == [1.0, 0.0, 0.0], \
        "has_raw_data 三态：None/False 都不是 True"
    assert [r[col["raw_required"]] for r in rows] == [1.0, 1.0, 1.0]


def test_known_answer_empty_pool():
    """空候选池 → 空行列表（不炸、lex_max 计算有守卫）。"""
    assert ltr_feature_rows([], [], _intent(), FEATURE_ORDER) == []


def test_unknown_feature_name_raises():
    """feature_order 含未知名 → KeyError（契约破裂响亮失败，绝不静默组错列）。"""
    items = [RetrievedCandidate(record=_rec("A", "human"), score=1.0)]
    with pytest.raises(KeyError):
        ltr_feature_rows(items, [0.5], _intent(), ("lex_norm", "no_such_feature"))
