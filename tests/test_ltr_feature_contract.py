# -*- coding: utf-8 -*-
"""三向特征契约：训练侧特征名 == 制品 feature_order == 推理构造器键集合。

**为什么单列一条门**：这三处历史上是三份手抄镜像——`train_ltr.FEATURE_NAMES`、
`models/ltr/ltr_spec.json` 的 `feature_order`、推理侧特征构造（2026-09-30 队列② R1 起
单源化为 `retrieval/ltr_features.ltr_feature_rows`，此前内联在
`vector_recall._ltr_fused_order`）里的特征字典。任何一处改名（或顺序调换）都会让推理侧
组不出向量：字典取不到键 → `KeyError` → 生产路径**静默 fail-soft 回退**（线上表现成
"LTR 悄悄不生效"，trace 里只有一行 `ltr_status="fallback"`），而三门质量门全绿——因为
冻结评测根本不开召回。本测试把这种 desync 从"线上静默降级"变成"门期响亮失败"。

三层断言，从最咬人到最便宜：

1. **值层（同构输入逐位相等）**：同一组逻辑候选，训练侧 `candidate_features()` 产出的
   9 维行 == 推理侧 `_ltr_fused_order()` 喂给打分器的 9 维行。这一条同时钉死**名字**与
   **顺序**：任何错位都会让某个维度的值落到别的列上，数值断言当场红。
2. **绑定层（spec 顺序真的被遵守）**：把伪 scorer 的 `ltr_feature_order` 旋转一位，
   喂进去的行必须恰好是原行的同一位旋转——证明推理侧确实按 spec 声明的顺序组织向量，
   而不是按自己的字典顺序（否则改 spec 顺序就成了无声 no-op）。
3. **契约层 + 源码层**：现役制品 spec 的 `feature_order` 与 `FEATURE_NAMES` 逐位相同；
   并用 AST 取出推理侧特征字典的键集合与 `FEATURE_NAMES` 集合比对（漏项/多写一项直接红）。

另一处**已知且有意**的不对称（本测试显式钉住，防止有人"顺手对齐"）：`has_vec` 在训练侧是
"该候选是否有向量分"（无向量时为 0）；单源函数 `ltr_feature_rows` 把它定义为
"`dense[i] is None` → 0"——生产 dense 召回走到融合那一步时每个候选都已经算出余弦
（恒非 None），故推理侧 has_vec 恒为 1，与改造前内联实现逐位一致；离线回放（f3）允许
缺向量分的候选进入，才走 0 的那一支。值层夹具只用带向量分的候选。
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from train_ltr import FEATURE_NAMES, candidate_features  # noqa: E402
from dataset_recommender.retrieval import vector_recall  # noqa: E402
from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402
from dataset_recommender.retrieval.retriever import RetrievedCandidate  # noqa: E402

SPEC_PATH = ROOT / "models" / "ltr" / "ltr_spec.json"

#: 同构夹具的逻辑输入：两个候选，lex 分 10/5（故 max=10）、pos 1/2、向量分 0.5/0.2，
#: 第一个候选命中 species+disease 且自带 raw，第二个候选什么都不命中。
_TRAIN_ROW = {
    "query_id": "q-fixture",
    "has_raw_data_required": True,
    "candidates": [
        {"uid": "A", "pos": 1, "lex_score": 10.0, "vec_score": 0.5, "grade": 2, "llm_semantic": 3,
         "matched": {"species": True, "tissue": False, "disease": True, "has_raw_data": True}},
        {"uid": "B", "pos": 2, "lex_score": 5.0, "vec_score": 0.2, "grade": 0, "llm_semantic": 1,
         "matched": {}},
    ],
}

#: 期望的 9 维行（顺序 = FEATURE_NAMES）。
_EXPECTED_ROWS = [
    [1.0, 0.5, 1.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0],
    [0.5, 0.2, 1.0, 0.5, 0.0, 0.0, 0.0, 0.0, 1.0],
]


def _record(uid: str) -> DatasetRecord:
    """与 _TRAIN_ROW 同构的记录：A 命中 human/breast cancer 且有 raw，B 都不命中。"""
    if uid == "A":
        return DatasetRecord(dataset_name="A", species="human", tissue="breast", disease="breast cancer",
                             chemistry="", count="", unit="", has_raw_data=True, url="", source_file="x.json",
                             description="A desc", raw={})
    return DatasetRecord(dataset_name="B", species="mouse", tissue="liver", disease="",
                         chemistry="", count="", unit="", has_raw_data=False, url="", source_file="x.json",
                         description="B desc", raw={})


def _intent() -> SimpleNamespace:
    """与 _TRAIN_ROW 同构的解析意图：硬约束 species=human、disease=cancer，要求 raw。"""
    return SimpleNamespace(constraints={"species": ["human"], "disease": ["cancer"]},
                           preferred_constraints={}, has_raw_data_required=True)


def _inference_rows(feature_order: "list[str]") -> "list[list[float]]":
    """跑一遍推理构造器，捕获它真正喂给打分器的那几行。"""
    captured: dict = {}

    def scorer(batch):
        captured["rows"] = [list(map(float, row)) for row in batch]
        return [0.0] * len(batch)

    scorer.ltr_feature_order = feature_order
    items = [RetrievedCandidate(record=_record("A"), score=10.0),
             RetrievedCandidate(record=_record("B"), score=5.0)]
    original = vector_recall.load_ltr_scorer
    vector_recall.load_ltr_scorer = lambda *a, **k: scorer  # type: ignore[assignment]
    try:
        order = vector_recall._ltr_fused_order(items, [0.5, 0.2], _intent(), {})
    finally:
        vector_recall.load_ltr_scorer = original  # type: ignore[assignment]
    assert order is not None, "推理构造器返回 None（视作不可用）——夹具本身不成立"
    return captured["rows"]


def test_trainer_features_match_expected_row_order():
    """训练侧：9 列的值-位置对应关系（本测试其余断言的公共基准）。"""
    matrix, _lex, _vec = candidate_features(_TRAIN_ROW)
    assert matrix.shape == (2, len(FEATURE_NAMES))
    assert np.allclose(matrix.tolist(), _EXPECTED_ROWS, atol=1e-6), (
        f"训练侧特征序与 FEATURE_NAMES 不符：{matrix.tolist()}"
    )


def test_inference_constructor_row_equals_trainer_row():
    """值层：推理侧喂给打分器的 9 维行 == 训练侧同构输入的 9 维行（逐位）。"""
    trainer_rows = candidate_features(_TRAIN_ROW)[0].tolist()
    inference_rows = _inference_rows(list(FEATURE_NAMES))
    assert np.allclose(inference_rows, trainer_rows, atol=1e-6), (
        "推理侧与训练侧的特征向量不一致——名字或顺序已经 desync（生产上会静默回退）\n"
        f"  trainer  : {trainer_rows}\n  inference: {inference_rows}"
    )


def test_inference_follows_spec_declared_order():
    """绑定层：spec 顺序旋转一位 → 喂进去的行必须恰好是同一位旋转。"""
    base = _inference_rows(list(FEATURE_NAMES))
    rotated_names = list(FEATURE_NAMES[1:]) + [FEATURE_NAMES[0]]
    rotated = _inference_rows(rotated_names)
    expected = [row[1:] + row[:1] for row in base]
    assert np.allclose(rotated, expected, atol=1e-12), (
        "推理侧没有按 ltr_spec 声明的 feature_order 组向量——按该顺序旋转入参应当只旋转输出行的位次"
    )


def test_live_artifact_feature_order_matches_trainer():
    """契约层：现役制品的 feature_order 与训练侧 FEATURE_NAMES 逐位相同。"""
    if not SPEC_PATH.is_file():
        pytest.skip("现役制品不在本仓库（骨架/公开镜像无 models/ltr）——契约由值层与源码层断言覆盖")
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    assert list(spec["feature_order"]) == list(FEATURE_NAMES), (
        f"models/ltr/ltr_spec.json 的 feature_order 与训练侧契约不符：{spec['feature_order']}"
    )
    assert int(spec["model"]["input_dim"]) == len(FEATURE_NAMES)
    assert len(spec["feature_mean"]) == len(spec["feature_std"]) == len(FEATURE_NAMES)


def test_inference_feature_dict_keys_match_trainer():
    """源码层：推理侧特征字典的键集合 == FEATURE_NAMES 集合（AST 机械门，漏项/改名直接红）。

    构造点 2026-09-30 起单源化为 `ltr_features.ltr_feature_rows`（自
    `vector_recall._ltr_fused_order` 抽离，行为逐位不变，golden 见 test_ltr_features.py）——
    构造点再搬家时本门需同步指向。
    """
    source = (ROOT / "src" / "dataset_recommender" / "retrieval" / "ltr_features.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    keys: "set[str]" | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "ltr_feature_rows":
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assign) and isinstance(inner.value, ast.Dict):
                if any(isinstance(t, ast.Name) and t.id == "feats" for t in inner.targets):
                    keys = {k.value for k in inner.value.keys if isinstance(k, ast.Constant)}
    assert keys is not None, "没在 ltr_feature_rows 里找到特征字典——推理侧的构造点变了，本门需同步"
    assert keys == set(FEATURE_NAMES), (
        f"推理侧特征字典键集合与训练侧契约不符：多={sorted(keys - set(FEATURE_NAMES))} "
        f"少={sorted(set(FEATURE_NAMES) - keys)}"
    )


def test_has_vec_asymmetry_is_explicit():
    """有意的语义钉住：训练侧缺向量分 → has_vec=0；推理侧（dense 融合期必有向量）恒为 1。

    单源函数把这条统一为 `dense[i] is None → has_vec=0`：生产 dense 路径恒有分（恒走 1 支），
    离线回放允许缺分（才走 0 支）。这一条不是为了"修"它，而是把口径写进测试：
    谁要动其中一侧，必须同时回答另一侧怎么办。
    """
    row = {"query_id": "q", "candidates": [{"pos": 1, "lex_score": 1.0, "vec_score": None,
                                            "matched": {}, "grade": 0}]}
    matrix, _lex, vec = candidate_features(row)
    has_vec_index = FEATURE_NAMES.index("has_vec")
    assert matrix[0][has_vec_index] == 0.0 and np.isnan(vec[0]), "训练侧缺向量分应记 has_vec=0 且 NaN"
    assert _inference_rows(list(FEATURE_NAMES))[0][has_vec_index] == 1.0, "推理侧 has_vec 应恒为 1"
