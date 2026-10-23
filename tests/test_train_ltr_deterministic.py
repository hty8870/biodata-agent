# -*- coding: utf-8 -*-
"""`train_ltr.py --deterministic`：同数据同 seed 连训两次必须产出逐位相同的权重。

动机（2026-09-29 评审）：重训抖动的量级与门禁容差同阶——同一基准同一 seed 重训 6 次的
heldout nDCG@10 样本 SD ≈ 5.65e−4，而门 2 容差 1e-3、门 3 容差 0.0。只固定 seed 不够，
CPU 归约顺序随线程池变；`--deterministic` 把它钉成一条（单线程 + random/numpy/torch seed）。

用**小规模合成数据**在进程内连训两次（真实现、真导出、真重载），只验"可复现"这一点，
不验质量指标；`torch` 的全局设置由测试自己还原，避免污染同进程的其它用例。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import train_ltr  # noqa: E402


def _write_jsonl(path: Path, rows: "list[dict]") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


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


@pytest.fixture()
def mini_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    in_dir = root / "research" / "ltr_data"
    _write_jsonl(in_dir / "synth_train.jsonl", _rows("train", 24))
    _write_jsonl(in_dir / "synth_heldout.jsonl", _rows("heldout", 6))
    return root


@pytest.fixture(autouse=True)
def _isolated_numeric_env():
    """进程内跑真训练会改全局状态（线程数/确定性开关/三处 RNG），用完必须原样还回去。"""
    import random

    import numpy as np
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


def _train(root: Path, out_dir: Path, *, deterministic: bool) -> dict:
    """跑一次真训练（进程内），返回导出后的 state_dict。"""
    import torch

    weights_path = out_dir / "ltr_mlp.pt"
    argv = ["train_ltr.py", "--in-dir", str(root / "research" / "ltr_data"),
            "--label", "mix", "--seed", "7",
            "--export", str(out_dir), "--out", str(out_dir / "_ltr_metrics.json")]
    if deterministic:
        argv.append("--deterministic")
    original_argv = sys.argv
    sys.argv = argv
    try:
        assert train_ltr.main() == 0, f"训练失败：{argv}"
    finally:
        sys.argv = original_argv
    assert weights_path.is_file()
    return torch.load(weights_path, map_location="cpu", weights_only=True)


def _max_param_delta(left: dict, right: dict) -> float:
    import torch

    assert sorted(left) == sorted(right), "state_dict 的键必须一致"
    return max(float((left[key] - right[key]).abs().max()) for key in left)


def test_deterministic_mode_retrains_bit_identically(mini_project: Path, tmp_path: Path) -> None:
    """同数据同 seed 连训两次：全参数差恒 0，导出文件也逐字节相同。"""
    first = _train(mini_project, tmp_path / "run1", deterministic=True)
    second = _train(mini_project, tmp_path / "run2", deterministic=True)
    assert _max_param_delta(first, second) == 0.0, "确定性模式必须给出逐位相同的权重"
    assert (tmp_path / "run1" / "ltr_mlp.pt").read_bytes() == \
        (tmp_path / "run2" / "ltr_mlp.pt").read_bytes(), "导出文件本体也要同字节"


def test_deterministic_flag_records_its_settings(mini_project: Path, tmp_path: Path) -> None:
    """`--deterministic` 的生效证据落进 `_ltr_metrics.json`（血统可查），默认关闭时不出现。"""
    out_on = tmp_path / "on"
    _train(mini_project, out_on, deterministic=True)
    payload_on = json.loads((out_on / "_ltr_metrics.json").read_text(encoding="utf-8"))
    assert payload_on["deterministic"]["torch_threads"] == 1
    assert payload_on["deterministic"]["seed"] == 7

    out_off = tmp_path / "off"
    _train(mini_project, out_off, deterministic=False)
    payload_off = json.loads((out_off / "_ltr_metrics.json").read_text(encoding="utf-8"))
    assert payload_off["deterministic"] is None, "不带旗标时不碰线程数与全局 seed（standalone 行为不变）"


def test_enable_deterministic_mode_seeds_and_single_threads() -> None:
    """设置函数本身：单线程 + 三处 seed 同步；`use_deterministic_algorithms` 不支持的构建也不许致命。"""
    import random

    import numpy as np
    import torch

    settings = train_ltr.enable_deterministic_mode(1234)
    assert torch.get_num_threads() == 1
    assert settings["torch_threads"] == 1 and settings["seed"] == 1234
    assert random.random() == random.Random(1234).random(), "random 必须被同一 seed 重播"
    assert np.random.rand() == np.random.RandomState(1234).random_sample(), "numpy 同理"
    first_torch_draw = torch.rand(1).item()
    train_ltr.enable_deterministic_mode(1234)
    assert torch.rand(1).item() == first_torch_draw, "torch 流必须可重播（同 seed → 同首抽）"
