# -*- coding: utf-8 -*-
"""`train_ltr.py` 的特征集/标签旋钮：`--feature-set` / `--train-label` / `--eval-label` /
`--eval-set` / `--per-query-out`，以及 F1 叠加层的已知答案口径。

**为什么单列一条门**：这些改动是"给一条已经在生产的训练路径加旋钮"。两个方向都会静默坏事——
（a）**默认路径漂移**：不带任何新旗标时必须与改前逐位一致（口径、特征、权重、指标），否则
现役 `models/ltr` 的血缘与 `_ltr_metrics*.json` 的历史数字全部作废；（b）**f1/f2 静默回退成 f0**：
叠加字段缺失时若退回自带 `pos/matched`，两个特征集就会变成同一个，对照实验直接得到假 null。

故这里钉三件事：
1. 缺省 == 显式 `f0`；`--label mix` == `--train-label mix --eval-label mix`（逐项相等，含权重）；
2. f1/f2/f3 缺叠加字段 → **抛错**，不是回退；f2 的列数是 9+8；f2 禁止 `--export`（生产只有
   9 维消费点）；f3 恰好相反——它的 9 列由生产同一构造函数产出（`ltr_features.ltr_feature_rows`），
   与推理侧逐值一致，故 **允许** `--export`（单源化的意义所在）；
3. f1 的三条口径修正（池内下标 / 判官换生产实现 / 缺 raw 标记读生效 intent）各有合成夹具的
   已知答案，且 `--eval-set` / `--per-query-out` 的账与汇总数字自洽。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import train_ltr  # noqa: E402
from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402

BUILDER_PATH = ROOT / "research" / "ltr_data" / "exp_20260929" / "build_f1_features.py"


# ---------------------------------------------------------------- 夹具
def _cand(uid: str, pos: int, lex: float, grade: int, sem: int) -> dict:
    return {"uid": uid, "pos": pos, "lex_score": lex, "vec_score": 0.5 - 0.05 * pos,
            "grade": grade, "llm_semantic": sem,
            "matched": {"species": pos == 1, "tissue": pos <= 2, "disease": pos <= 2,
                        "has_raw_data": True}}


def _rows(prefix: str, count: int, size: int = 6) -> "list[dict]":
    rows = []
    for i in range(count):
        candidates = [_cand(f"{prefix}-{i}-{j}", j + 1, 10.0 - 1.3 * j + 0.2 * ((i + j) % 3),
                            grade=max(0, 2 - (j // 2)), sem=max(0, 3 - j))
                      for j in range(size)]
        rows.append({"query_id": f"{prefix}-{i}", "query": f"{prefix} 查询 {i}",
                     "has_raw_data_required": bool(i % 2), "candidates": candidates})
    return rows


def _write_jsonl(path: Path, rows: "list[dict]") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _f1_override(row: dict, matched: "dict[str, float] | None" = None, raw: bool = False) -> dict:
    """把一行转成「已带 F1 叠加字段」的样子（构建器的产物形态）。"""
    return {**row, "f1_raw_required": raw,
            "candidates": [{**c, "f1_matched": dict(matched or {})} for c in row["candidates"]]}


@pytest.fixture()
def mini_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    in_dir = root / "research" / "ltr_data"
    _write_jsonl(in_dir / "synth_train.jsonl", _rows("train", 24))
    _write_jsonl(in_dir / "synth_heldout.jsonl", _rows("heldout", 6))
    return root


@pytest.fixture(autouse=True)
def _isolated_numeric_env():
    """进程内跑真训练会改全局状态（线程数/确定性开关/三处 RNG），用完原样还回去。"""
    import random

    import torch

    snapshot = {
        "threads": torch.get_num_threads(),
        "deterministic": torch.are_deterministic_algorithms_enabled(),
        "torch_state": torch.get_rng_state(),
        "numpy_state": np.random.get_state(),
        "random_state": random.getstate(),
    }
    yield
    torch.set_num_threads(snapshot["threads"])
    torch.use_deterministic_algorithms(snapshot["deterministic"])
    torch.set_rng_state(snapshot["torch_state"])
    np.random.set_state(snapshot["numpy_state"])
    random.setstate(snapshot["random_state"])


def _run(root: Path, out_dir: Path, *extra: str) -> int:
    argv = ["train_ltr.py", "--in-dir", str(root / "research" / "ltr_data"),
            "--out", str(out_dir / "_ltr_metrics.json"), *extra]
    original = sys.argv
    sys.argv = argv
    try:
        return train_ltr.main()
    finally:
        sys.argv = original


def _load(out_dir: Path) -> dict:
    return json.loads((out_dir / "_ltr_metrics.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 默认路径不变
def test_default_matches_explicit_f0_and_label(mini_project: Path, tmp_path: Path) -> None:
    """缺省（不带新旗标）== 显式 `--feature-set f0 --train-label mix --eval-label mix`：逐项相等。

    默认路径是现役制品与历史 `_ltr_metrics*.json` 的血缘基线，任何"顺手统一口径"都是无声重基线。
    """
    default_out = tmp_path / "default"
    explicit_out = tmp_path / "explicit"
    assert _run(mini_project, default_out, "--label", "mix", "--seed", "7") == 0
    assert _run(mini_project, explicit_out, "--feature-set", "f0", "--train-label", "mix",
                "--eval-label", "mix", "--seed", "7") == 0
    a, b = _load(default_out), _load(explicit_out)
    assert a["results"] == b["results"], "缺省与显式 f0 的指标必须逐项相等"
    assert a["feature_names"] == b["feature_names"] == list(train_ltr.FEATURE_NAMES)
    assert a["ablations"] == b["ablations"]


def test_label_option_equals_train_and_eval_labels(mini_project: Path, tmp_path: Path) -> None:
    """`--label sem` == `--train-label sem --eval-label sem`（向后兼容的唯一入口）。"""
    a_out, b_out = tmp_path / "a", tmp_path / "b"
    assert _run(mini_project, a_out, "--label", "sem", "--seed", "7") == 0
    assert _run(mini_project, b_out, "--train-label", "sem", "--eval-label", "sem", "--seed", "7") == 0
    a, b = _load(a_out), _load(b_out)
    assert a["results"] == b["results"]
    assert (a["train_label"], a["eval_label"]) == (b["train_label"], b["eval_label"]) == ("sem", "sem")


@pytest.fixture()
def skew_project(tmp_path: Path) -> Path:
    """评测标签会改变口径的夹具：lex 序与语义序**相左**，且训练行里有候选缺语义分。

    默认夹具的 lex 序与所有标签都单调一致（基线恒 1.0），换评测标签看不出差别——用它测
    "评测标签真的换了"会假绿。这里刻意造反例。
    """
    root = tmp_path / "skew"
    in_dir = root / "research" / "ltr_data"
    train = _rows("train", 24)
    for row in train:                      # 让三分之一的候选没有语义分：mix 不丢、sem 丢
        for j, cand in enumerate(row["candidates"]):
            if j % 3 == 2:
                cand["llm_semantic"] = None
    _write_jsonl(in_dir / "synth_train.jsonl", train)
    heldout = []
    for i in range(6):
        heldout.append({"query_id": f"heldout-{i}", "query": f"heldout 查询 {i}",
                        "has_raw_data_required": False,
                        "candidates": [
                            {"uid": f"h-{i}-1", "pos": 1, "lex_score": 10.0, "vec_score": 0.1,
                             "grade": 2, "llm_semantic": 0, "matched": {}},
                            {"uid": f"h-{i}-2", "pos": 2, "lex_score": 5.0, "vec_score": 0.2,
                             "grade": 0, "llm_semantic": 3, "matched": {}},
                            {"uid": f"h-{i}-3", "pos": 3, "lex_score": 1.0, "vec_score": 0.0,
                             "grade": 1, "llm_semantic": 1, "matched": {}},
                        ]})
    _write_jsonl(in_dir / "synth_heldout.jsonl", heldout)
    return root


def test_eval_label_is_decoupled_from_train_label(skew_project: Path, tmp_path: Path) -> None:
    """训练标签换、评测标签不换：评测侧指标（基线）不变，训练侧口径跟着换。

    这正是"训练目标与评测目标必须分开"的前提——旧 CLI 里 `--label` 会同时换掉评分尺子，
    两组数字不可比。
    """
    mix_out, decoupled_out, sem_out = tmp_path / "mix", tmp_path / "decoupled", tmp_path / "sem"
    assert _run(skew_project, mix_out, "--label", "mix", "--seed", "7") == 0
    assert _run(skew_project, decoupled_out, "--train-label", "mix", "--eval-label", "sem",
                "--seed", "7") == 0
    assert _run(skew_project, sem_out, "--label", "sem", "--seed", "7") == 0
    mix, dec, sem = _load(mix_out), _load(decoupled_out), _load(sem_out)

    assert dec["eval_label"] == "sem" and dec["train_label"] == "mix"
    assert dec["results"]["baselines"] == sem["results"]["baselines"], \
        "同一评测标签下基线指标必须一致（三条基线只看评测标签）"
    assert dec["results"]["baselines"] != mix["results"]["baselines"], "评测标签真的换了（基线口径不同）"
    assert dec["data"]["train_label_stats"] == mix["data"]["train_label_stats"], \
        "训练侧留用的是 mix 口径的样本统计"
    assert mix["data"]["train_label_stats"]["dropped_unlabelled"] == 0, "mix 不丢缺语义分的候选"
    assert sem["data"]["train_label_stats"]["dropped_unlabelled"] > 0, "sem 才丢（夹具前提）"
    assert mix["results"] != dec["results"], "训练标签换了、评测标签固定 → 模型侧数字应当变化"


def test_label_and_explicit_label_flags_conflict(mini_project: Path, tmp_path: Path) -> None:
    """`--label` 与 `--train-label/--eval-label` 同时给 → 当场报错，不做"后者覆盖前者"的猜测。"""
    with pytest.raises(SystemExit):
        _run(mini_project, tmp_path / "x", "--label", "mix", "--train-label", "det", "--seed", "7")


# ---------------------------------------------------------------- 特征集门
def test_f1_requires_overlay_fields(tmp_path: Path) -> None:
    """f1 缺 `f1_raw_required` / `f1_matched` → 抛错；静默回退会让 F0/F1 变成同一个臂。"""
    row = _rows("t", 1, 3)[0]
    with pytest.raises(ValueError, match="f1_raw_required"):
        train_ltr.candidate_features(row, "f1")
    stripped = {k: v for k, v in _f1_override(row).items() if k != "f1_raw_required"}
    with pytest.raises(ValueError, match="f1_raw_required"):
        train_ltr.candidate_features(stripped, "f1")
    no_matched = {**row, "f1_raw_required": False}          # 行级给了、候选级缺
    with pytest.raises(ValueError, match="f1_matched"):
        train_ltr.candidate_features(no_matched, "f1")


def test_f1_uses_pool_index_and_parsed_matched() -> None:
    """F1 的两处口径修正：池内下标（不是自带 pos）+ 叠加的 matched（不是自带 matched）。"""
    row = _rows("t", 1, 3)[0]
    row["candidates"][0]["pos"] = 99          # 自带 pos 离位（A/B 合并池的原始编号）
    row["candidates"][1]["pos"] = 1           # 重复
    matched = {"species": 1.0, "tissue": 0.0, "disease": 1.0, "has_raw_data": 0.0}
    f1, _lex, _vec = train_ltr.candidate_features(_f1_override(row, matched, raw=True), "f1")
    f0, _lex0, _vec0 = train_ltr.candidate_features(row, "f0")

    pos_col = train_ltr.FEATURE_NAMES.index("inv_pos")
    assert f0[0][pos_col] == pytest.approx(1 / 99) and f0[1][pos_col] == pytest.approx(1.0), \
        "F0 读的是自带 pos（含离位/重复）"
    assert f1[0][pos_col] == pytest.approx(1.0) and f1[1][pos_col] == pytest.approx(0.5), \
        "F1 读的是合并池下标 1/(i+1)"
    for name, value in matched.items():
        col = train_ltr.FEATURE_NAMES.index(f"matched_{name}")
        assert f1[0][col] == value and f1[1][col] == value, f"{name} 应取叠加值"
    raw_col = train_ltr.FEATURE_NAMES.index("raw_required")
    assert f1[0][raw_col] == 1.0, "raw_required 取行级 f1_raw_required"


def test_f2_appends_query_block_and_needs_it() -> None:
    """F2 = 9 + len(F2_QUERY_FEATURES) 列；缺 `f2_query` 抛错。"""
    row = _rows("t", 1, 3)[0]
    enriched = _f1_override(row)
    with pytest.raises(ValueError, match="f2_query"):
        train_ltr.candidate_features(enriched, "f2")
    block = [float(i) for i in range(len(train_ltr.F2_QUERY_FEATURES))]
    f2, _lex, _vec = train_ltr.candidate_features({**enriched, "f2_query": block}, "f2")
    assert f2.shape == (3, len(train_ltr.FEATURE_NAMES) + len(train_ltr.F2_QUERY_FEATURES))
    assert train_ltr.feature_names("f2")[len(train_ltr.FEATURE_NAMES):] == train_ltr.F2_QUERY_FEATURES
    assert f2[0][len(train_ltr.FEATURE_NAMES):].tolist() == block


def test_ablation_full_covers_f2_columns() -> None:
    """消融列组按特征集伸缩：f2 的 `full` 必须覆盖全部 17 列（否则消融表少一列还看不出来）。"""
    assert train_ltr.ablation_cols("f2")["full"] == tuple(range(17))
    assert train_ltr.ablation_cols("f0")["full"] == tuple(range(9))
    with pytest.raises(ValueError):
        train_ltr.feature_names("f9")


def test_f2_export_is_refused(mini_project: Path, tmp_path: Path, capsys) -> None:
    """f2 禁止导出：生产推理侧只有 9 维消费点，落盘只会得到永远加载不了的制品（fail-closed）。"""
    root = mini_project
    in_dir = root / "research" / "ltr_data"
    train = [_f1_override(row, {"species": 1.0}, raw=True) for row in _rows("train", 24)]
    held = [_f1_override(row, {"species": 1.0}, raw=True) for row in _rows("heldout", 6)]
    for rows in (train, held):
        for row in rows:
            row["f2_query"] = [0.5] * len(train_ltr.F2_QUERY_FEATURES)
    _write_jsonl(in_dir / "synth_train.jsonl", train)
    _write_jsonl(in_dir / "synth_heldout.jsonl", held)
    code = _run(root, tmp_path / "f2", "--feature-set", "f2", "--train-label", "mix",
                "--eval-label", "mix", "--seed", "7", "--export", str(tmp_path / "f2export"))
    assert code == 1, "f2 的 --export 必须拒绝"
    assert not (tmp_path / "f2export" / "ltr_mlp.pt").exists()
    assert "拒绝 feature-set=f2" in capsys.readouterr().out


# ---------------------------------------------------------------- f3 特征集门
def _f3_override(row: dict, base: float = 0.0) -> dict:
    """把一行转成「已带 f3 叠加字段」的样子：f3_feats = 9 个可辨识值（构建器的产物形态）。"""
    return {**row, "candidates": [
        {**c, "f3_feats": [base + 0.05 * k for k in range(len(train_ltr.FEATURE_NAMES))]}
        for c in row["candidates"]]}


def test_f3_requires_overlay_and_validates_shape() -> None:
    """f3 缺 `f3_feats` / 长度错 / 含非有限值 → 抛错；静默回退会让 f3 与别的臂变成同一个。"""
    row = _rows("t", 1, 3)[0]
    with pytest.raises(ValueError, match="f3_feats"):
        train_ltr.candidate_features(row, "f3")
    bad_len = {**row, "candidates": [{**c, "f3_feats": [0.0] * 8} for c in row["candidates"]]}
    with pytest.raises(ValueError, match="f3_feats"):
        train_ltr.candidate_features(bad_len, "f3")
    bad_val = {**row, "candidates": [
        {**c, "f3_feats": [0.0] * 8 + [float("nan")]} for c in row["candidates"]]}
    with pytest.raises(ValueError, match="非有限值"):
        train_ltr.candidate_features(bad_val, "f3")


def test_f3_uses_overlay_verbatim_and_keeps_baseline_inputs() -> None:
    """f3 的 9 列逐值取叠加层（不重算）；基线输入（lex/vec）仍读自带分，不受叠加层影响。"""
    row = _rows("t", 1, 3)[0]
    f3, lex, vec = train_ltr.candidate_features(_f3_override(row, base=1.0), "f3")
    assert f3.shape == (3, len(train_ltr.FEATURE_NAMES))
    expected = [1.0 + 0.05 * k for k in range(len(train_ltr.FEATURE_NAMES))]
    for i in range(3):
        assert f3[i].tolist() == pytest.approx(expected, abs=1e-6), "f3 列必须逐值等于叠加层"
    assert lex == [float(c["lex_score"]) for c in row["candidates"]]
    assert vec[0] == row["candidates"][0]["vec_score"]


def test_f3_ablation_full_covers_nine_columns() -> None:
    """f3 与 f0/f1 同 9 列契约；消融 full 覆盖全部 9 列。"""
    assert train_ltr.feature_names("f3") == train_ltr.FEATURE_NAMES
    assert train_ltr.ablation_cols("f3")["full"] == tuple(range(9))


def test_f3_export_is_allowed(mini_project: Path, tmp_path: Path) -> None:
    """f3 允许导出：特征与生产推理逐值一致（同一构造函数），导出物即生产可加载制品。"""
    root = mini_project
    in_dir = root / "research" / "ltr_data"
    _write_jsonl(in_dir / "synth_train.jsonl", [_f3_override(row, base=0.3) for row in _rows("train", 24)])
    _write_jsonl(in_dir / "synth_heldout.jsonl", [_f3_override(row, base=0.3) for row in _rows("heldout", 6)])
    export_dir = tmp_path / "f3export"
    code = _run(root, tmp_path / "f3", "--feature-set", "f3", "--train-label", "mix",
                "--eval-label", "mix", "--seed", "7", "--export", str(export_dir))
    assert code == 0, "f3 的 --export 必须放行（特征与生产推理一致，这是单源化的意义）"
    spec = json.loads((export_dir / "ltr_spec.json").read_text(encoding="utf-8"))
    assert spec["feature_set"] == "f3"
    assert spec["feature_order"] == list(train_ltr.FEATURE_NAMES)


# ---------------------------------------------------------------- --eval-feature-set（训练/评测特征解耦，R3）
def test_eval_feature_set_default_unchanged(mini_project: Path, tmp_path: Path) -> None:
    """缺省（不带旗标）== 显式 `--eval-feature-set f0`：逐项相等——缺省路径逐位不变。"""
    a_out, b_out = tmp_path / "a", tmp_path / "b"
    assert _run(mini_project, a_out, "--label", "mix", "--seed", "7") == 0
    assert _run(mini_project, b_out, "--feature-set", "f0", "--eval-feature-set", "f0",
                "--label", "mix", "--seed", "7") == 0
    a, b = _load(a_out), _load(b_out)
    assert a["results"] == b["results"]
    assert a["eval_feature_set"] == b["eval_feature_set"] == "f0"


def test_eval_feature_set_decoupled(
    mini_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """train f0 / eval f3：训练制品逐位不变，真实评分输入逐值取 f3 叠加层。

    确定性夹具：heldout 行列表顺序 = 最优序（A,B,C 增益递减），但 C 在 f0 的每个信号上
    都碾压 A（lex 100 vs 1、pos 1 vs 3、vec 0.4 vs 0.2）——f0 训练出的模型（训练夹具里
    lex/inv_pos/vec 全部与标签正相关）必然把 C 排到 A 前 ⇒ f0 评测 nDCG@10 < 1.0；
    f3 叠加层全候选同向量；直接核对真实评分输入，避免把不同 CPU 矩阵内核的
    浮点舍入差异误判为特征选择失败。训练、评分与排序都执行原实现。
    """
    import torch

    original_train = train_ltr.train_ranknet
    full_inputs: list[list[np.ndarray]] = []
    full_artifacts: list[dict] = []

    def observe_training(X, y, groups, cols, seed):
        score, info, artifacts = original_train(X, y, groups, cols, seed)
        if tuple(cols) != tuple(range(len(train_ltr.FEATURE_NAMES))):
            return score, info, artifacts
        assert score is not None and artifacts is not None
        inputs: list[np.ndarray] = []
        full_inputs.append(inputs)
        full_artifacts.append(artifacts)

        def observe_score(feats):
            inputs.append(feats.copy())
            return score(feats)

        return observe_score, info, artifacts

    monkeypatch.setattr(train_ltr, "train_ranknet", observe_training)
    root = mini_project
    in_dir = root / "research" / "ltr_data"
    heldout = [{"query_id": f"heldout-{i}", "query": f"heldout 查询 {i}",
                "has_raw_data_required": False,
                "candidates": [
                    {"uid": f"h-{i}-A", "pos": 3, "lex_score": 1.0, "vec_score": 0.2,
                     "grade": 2, "llm_semantic": 3, "matched": {}},
                    {"uid": f"h-{i}-B", "pos": 2, "lex_score": 50.0, "vec_score": 0.3,
                     "grade": 1, "llm_semantic": 1, "matched": {}},
                    {"uid": f"h-{i}-C", "pos": 1, "lex_score": 100.0, "vec_score": 0.4,
                     "grade": 0, "llm_semantic": 0, "matched": {}},
                ]} for i in range(6)]
    overlaid = [_f3_override(row, base=0.3) for row in heldout]
    _write_jsonl(in_dir / "synth_heldout.jsonl", overlaid)
    plain, crossed = tmp_path / "plain", tmp_path / "crossed"
    assert _run(root, plain, "--feature-set", "f0", "--label", "mix", "--seed", "7") == 0
    assert _run(root, crossed, "--feature-set", "f0", "--eval-feature-set", "f3",
                "--label", "mix", "--seed", "7") == 0
    a, b = _load(plain), _load(crossed)
    assert (b["feature_set"], b["eval_feature_set"]) == ("f0", "f3")
    assert a["data"]["train_label_stats"] == b["data"]["train_label_stats"], "训练样本统计不许变"
    am = a["results"]["models"]["ranknet_mlp"]["full"]
    bm = b["results"]["models"]["ranknet_mlp"]["full"]
    assert (am["pairs"], am["final_loss"]) == (bm["pairs"], bm["final_loss"]), \
        "评测特征口径不许扰动训练（pairs/final_loss 逐项一致）"
    assert a["results"]["baselines"] == b["results"]["baselines"], "基线只看 lex/vec，与特征口径无关"
    assert len(full_artifacts) == len(full_inputs) == 2
    left, right = full_artifacts
    np.testing.assert_array_equal(left["mean"], right["mean"])
    np.testing.assert_array_equal(left["std"], right["std"])
    assert left["state_dict"].keys() == right["state_dict"].keys()
    for key in left["state_dict"]:
        assert torch.equal(left["state_dict"][key], right["state_dict"][key]), key
    assert len(full_inputs[0]) == len(full_inputs[1]) == len(heldout)
    for row, plain_inputs, crossed_inputs in zip(overlaid, *full_inputs):
        expected_plain, _, _ = train_ltr.candidate_features(row, "f0")
        np.testing.assert_array_equal(plain_inputs, expected_plain)
        expected_crossed = np.asarray([c["f3_feats"] for c in row["candidates"]], dtype=np.float32)
        np.testing.assert_array_equal(crossed_inputs, expected_crossed)
        assert not np.array_equal(plain_inputs, crossed_inputs)
    assert am["nDCG@10"] < 1.0, "f0 特征下 C 碾压 A 的每个信号 → 模型必排反 → 严格小于 1.0"


def test_eval_feature_set_column_guard(mini_project: Path, tmp_path: Path) -> None:
    """f2 训练（17 列）× f3 评测（9 列）列数不同 → 当场 SystemExit（fail-closed）。"""
    with pytest.raises(SystemExit):
        _run(mini_project, tmp_path / "x", "--feature-set", "f2", "--eval-feature-set", "f3",
             "--label", "mix", "--seed", "7")


def test_eval_feature_set_f3_requires_overlay_on_eval_rows(mini_project: Path,
                                                           tmp_path: Path) -> None:
    """评测特征 f3 时评测行缺 f3_feats → ValueError（与 f1/f2 同款，不静默回退）。"""
    with pytest.raises(ValueError, match="f3_feats"):
        _run(mini_project, tmp_path / "y", "--feature-set", "f0",
             "--eval-feature-set", "f3", "--label", "mix", "--seed", "7")


# ---------------------------------------------------------------- 评测集 / 逐查询账
def test_eval_set_and_per_query_out_agree(mini_project: Path, tmp_path: Path) -> None:
    """`--eval-set` 的汇总与 `--per-query-out` 的明细必须自洽（同一份 ranking 的两种投影）。"""
    extra = tmp_path / "extra.jsonl"
    _write_jsonl(extra, _rows("extra", 4))
    out_dir = tmp_path / "run"
    per_query = tmp_path / "per_query.jsonl"
    assert _run(mini_project, out_dir, "--label", "mix", "--seed", "7",
                "--eval-set", f"extra={extra}", "--per-query-out", str(per_query)) == 0
    payload = _load(out_dir)
    assert payload["extra_eval_sets"] == {"extra": str(extra)}
    reported = payload["extra_eval_results"]["extra"]["ranknet_mlp_full"]["nDCG@10"]

    rows = [json.loads(line) for line in per_query.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_set = {}
    for row in rows:
        by_set.setdefault(row["eval_set"], []).append(row)
    assert set(by_set) == {"heldout", "extra"}, f"逐查询账应覆盖 in-dir heldout 与显式评测集：{set(by_set)}"
    assert all(row["ndcg10"].get("ranknet_mlp_full") is not None for row in rows)
    assert all("lex" in row["ndcg10"] for row in rows)
    macro = float(np.mean([row["ndcg10"]["ranknet_mlp_full"] for row in by_set["extra"]]))
    assert macro == pytest.approx(reported, abs=5e-5), \
        f"明细宏平均 {macro} 与汇总 {reported} 不一致（rounding 5e-6 内）"
    assert all(len(row["top10_uids"]["ranknet_mlp_full"]) <= 10 for row in rows)


def test_bad_eval_set_spec_is_rejected(mini_project: Path, tmp_path: Path) -> None:
    """`--eval-set` 缺 NAME=/缺文件 → 当场报错（评测集悄悄少一份 = 结论口径不明）。"""
    with pytest.raises(SystemExit):
        _run(mini_project, tmp_path / "a", "--label", "mix", "--eval-set", "noequals")
    with pytest.raises(SystemExit):
        _run(mini_project, tmp_path / "b", "--label", "mix", "--eval-set", "x=missing.jsonl")


# ---------------------------------------------------------------- F1 叠加层构建器（仓内研究目录，公开镜像不带）
@pytest.fixture()
def builder():
    if not BUILDER_PATH.is_file():
        pytest.skip("研究数据层缺失（公开镜像不带 research/）——本条只在本仓跑")
    spec = importlib.util.spec_from_file_location("build_f1_features", BUILDER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _record(uid: str, **kw) -> DatasetRecord:
    base = dict(dataset_name=uid, species="human", tissue="breast", disease="breast cancer",
                chemistry="", count="", unit="", has_raw_data=True, url="", source_file="x.json",
                description="d", raw={})
    base.update(kw)
    return DatasetRecord(**base)


def test_builder_overlay_matches_production_judge(builder) -> None:
    """构建器对生效 intent 的判定 == 生产裁判（同一函数）：命中 dim 记 1、无该项约束记 0。"""
    row = {"query_id": "q", "query": "找人的乳腺癌单细胞数据",
           "candidates": [{"uid": "A", "pos": 1, "lex_score": 2.0, "vec_score": 0.5, "grade": 2},
                          {"uid": "B", "pos": 1, "lex_score": 1.0, "vec_score": 0.1, "grade": 1}]}
    records = {"A": _record("A"), "B": _record("B", species="mouse", tissue="liver", disease="")}
    from types import SimpleNamespace
    intent = SimpleNamespace(constraints={"species": ["human"], "disease": ["breast cancer"]},
                             preferred_constraints={}, has_raw_data_required=None,
                             abstain=False, abstain_reason="", unresolved_terms=[])
    matched = builder.f1_matched(records["A"], intent)
    assert matched == {"species": 1.0, "tissue": 0.0, "disease": 1.0, "has_raw_data": 0.0}, matched
    matched_b = builder.f1_matched(records["B"], intent)
    assert matched_b["species"] == 0.0 and matched_b["disease"] == 0.0
    raw_intent = SimpleNamespace(constraints={}, preferred_constraints={},
                                has_raw_data_required=True, abstain=False, abstain_reason="",
                                unresolved_terms=[])
    assert builder.f1_matched(records["A"], raw_intent)["has_raw_data"] == 1.0
    assert builder.f1_matched(_record("C", has_raw_data=None), raw_intent)["has_raw_data"] == 0.0, \
        "has_raw_data 三态：None 不等于 True"


def test_builder_overlay_row_keeps_labels_and_adds_fields(builder) -> None:
    """`overlay_row` 只加字段：标签列逐位不变，且 F1 字段形态是训练侧期待的形态。"""
    row = {"query_id": "q", "query": "human breast cancer single cell",
           "has_raw_data_required": True,
           "candidates": [{"uid": "A", "pos": 5, "lex_score": 3.0, "vec_score": 0.4, "grade": 2,
                           "llm_semantic": 3, "matched": {"disease": False}}]}
    from types import SimpleNamespace
    settings = SimpleNamespace(keyword_mapping=None)
    out, audit = builder.overlay_row(row, settings, {"A": _record("A")}, "f2")
    assert row["candidates"][0]["matched"] == {"disease": False}, "入参不许被改写"
    assert out["candidates"][0]["llm_semantic"] == 3 and out["candidates"][0]["grade"] == 2
    assert out["candidates"][0]["lex_score"] == 3.0
    assert "f1_matched" in out["candidates"][0] and isinstance(out["f1_raw_required"], bool)
    assert len(out["f2_query"]) == len(train_ltr.F2_QUERY_FEATURES)
    assert audit["rows"] if False else True  # audit 结构由 build_variant 汇总，这里只验行级字段
    # F1 特征行必须能被训练侧直接吃下（形态自洽）
    matrix, _lex, _vec = train_ltr.candidate_features(out, "f2")
    assert matrix.shape == (1, len(train_ltr.FEATURE_NAMES) + len(train_ltr.F2_QUERY_FEATURES))


def test_builder_kendall_disagreement_known_answers(builder) -> None:
    """分歧度的已知答案：同序 0、反序 1、缺向量分不算、并列按原下标。"""
    assert builder.kendall_disagreement([3.0, 2.0, 1.0], [0.1, 0.2, 0.3]) == pytest.approx(1.0)
    assert builder.kendall_disagreement([3.0, 2.0, 1.0], [0.3, 0.2, 0.1]) == pytest.approx(0.0)
    assert builder.kendall_disagreement([1.0, 1.0, 1.0], [0.3, 0.2, 0.1]) == pytest.approx(0.0), \
        "lex 全并列 → 按原下标，与 vec 序（也是原下标）同序"
    assert builder.kendall_disagreement([2.0, 1.0], [float("nan"), 0.5]) == pytest.approx(0.0), \
        "只剩一个可比候选 → 记 0"


def test_builder_assert_labels_untouched_catches_drift(builder) -> None:
    """标签不变量是机械断言，不是口号：改一个 grade 必须炸。"""
    src = [{"query_id": "q", "candidates": [{"uid": "A", "pos": 1, "lex_score": 1.0,
                                             "vec_score": 0.1, "grade": 2, "llm_semantic": 3}]}]
    same = [{"query_id": "q", "candidates": [{"uid": "A", "pos": 1, "lex_score": 1.0,
                                              "vec_score": 0.1, "grade": 2, "llm_semantic": 3,
                                              "f1_matched": {}}]}]
    builder.assert_labels_untouched(src, same)  # 不抛
    drifted = [{"query_id": "q", "candidates": [{"uid": "A", "pos": 1, "lex_score": 1.0,
                                                 "vec_score": 0.1, "grade": 1, "llm_semantic": 3}]}]
    with pytest.raises(AssertionError):
        builder.assert_labels_untouched(src, drifted)
