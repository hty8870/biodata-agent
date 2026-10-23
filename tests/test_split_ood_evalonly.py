# -*- coding: utf-8 -*-
"""OOD 交叉打分集的 trainable/evalonly 拆分（`scripts/split_ood_evalonly.py`）。

门 3 的评测集必须与训练 mix-in 物理分家，而拆分本身必须是**可复现的一刀**：
两侧 query_id 不相交、重跑逐字节一致、比例落在容差内、行文本与源文件逐字节相同。
前半段用合成行（公开镜像也能跑），后半段用真数据核对落盘的两份派生文件。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import split_ood_evalonly as splitter  # noqa: E402


def _lines(rows: "list[dict]") -> "list[str]":
    return [json.dumps(row, ensure_ascii=False) for row in rows]


def _rows(count: int) -> "list[dict]":
    return [{"query_id": f"q-{i:03d}", "query": f"查询 {i}", "ood_class": "underspecified",
             "candidates": [{"uid": "A", "pos": 1, "grade": 2, "llm_semantic": 3}]}
            for i in range(count)]


# ---------------------------------------------------------------- 分桶与拆分
def test_split_is_deterministic_and_lands_in_ratio_band() -> None:
    """同一输入两次拆分逐字节一致；比例落在 0.7 ± 0.05（n=400 时二项 SD≈2.3pp）。"""
    lines = _lines(_rows(400))
    first = splitter.split_lines(lines)
    second = splitter.split_lines(lines)
    assert first == second, "拆分必须可复现（重跑同字节）"
    trainable, evalonly = first
    assert len(trainable) + len(evalonly) == len(lines), "不许丢行"
    ratio = len(trainable) / len(lines)
    assert 0.65 <= ratio <= 0.75, f"可训练层占比越界：{ratio}"
    assert set(splitter.query_ids_of(trainable)).isdisjoint(splitter.query_ids_of(evalonly))


def test_same_query_id_never_straddles_the_split() -> None:
    """同一 query_id 的多行（A/B 复测）必须整组落同一侧——行级随机会漏成两侧共享查询。"""
    lines = _lines(_rows(60)) + _lines([
        {"query_id": "q-007", "query": "复测", "ood_class": "underspecified", "candidates": []},
        {"query_id": "Q-007 ", "query": "复测（大小写/空白差异）", "ood_class": "underspecified",
         "candidates": []},
    ])
    trainable, evalonly = splitter.split_lines(lines)
    train_ids, eval_ids = splitter.query_ids_of(trainable), splitter.query_ids_of(evalonly)
    assert "q-007" not in (train_ids & eval_ids)
    assert "q-007" in train_ids | eval_ids
    assert len(trainable) + len(evalonly) == len(lines)


def test_split_refuses_a_broken_source_line() -> None:
    """撕裂尾行必须炸：静默丢行等于评测集悄悄缩水。"""
    with pytest.raises(ValueError, match="不是合法 JSON"):
        splitter.split_lines(['{"query_id": "q-1"}', '{"query_id": "q-2"'])
    with pytest.raises(ValueError, match="缺 query_id"):
        splitter.split_lines(['{"query": "没有 id"}'])


def test_check_mode_detects_drift(tmp_path: Path, capsys) -> None:
    """`--check` 只在两份派生文件与源文件重算结果逐字节一致时放行。"""
    source = tmp_path / "src.jsonl"
    source.write_text("".join(line + "\n" for line in _lines(_rows(40))), encoding="utf-8")
    assert splitter.main(["--in", str(source), "--out-dir", str(tmp_path)]) == 0
    assert splitter.main(["--in", str(source), "--out-dir", str(tmp_path), "--check"]) == 0
    capsys.readouterr()

    trainable = tmp_path / splitter.TRAINABLE_NAME
    tampered = trainable.read_text(encoding="utf-8").splitlines()
    tampered[0] = json.dumps({"query_id": "hand-edited"}, ensure_ascii=False)
    trainable.write_text("".join(line + "\n" for line in tampered), encoding="utf-8")
    capsys.readouterr()
    assert splitter.main(["--in", str(source), "--out-dir", str(tmp_path), "--check"]) == 1
    assert "不一致" in capsys.readouterr().err


# ---------------------------------------------------------------- 真数据：落盘的拆分
def test_shipped_split_matches_its_source() -> None:
    """仓里那两份派生文件必须就是源文件按 seed 20260929 拆出来的那一刀。"""
    ood_dir = ROOT / "research" / "ood_eval"
    source = ood_dir / "synth_ood_label_input_xscore.jsonl"
    trainable_path = ood_dir / splitter.TRAINABLE_NAME
    evalonly_path = ood_dir / splitter.EVALONLY_NAME
    if not source.is_file():
        pytest.skip("研究数据层缺失（公开镜像不带 research/）——本条只在本仓跑")
    assert trainable_path.is_file() and evalonly_path.is_file(), "两份派生文件必须 tracked 在仓里"

    source_lines = [line for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    trainable = [line for line in trainable_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    evalonly = [line for line in evalonly_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert splitter.split_lines(source_lines) == (trainable, evalonly), \
        "落盘的拆分与源文件重算结果不一致（有人手改过派生文件？）"

    train_ids, eval_ids = splitter.query_ids_of(trainable), splitter.query_ids_of(evalonly)
    assert train_ids & eval_ids == set(), "两侧 query_id 必须不相交"
    assert train_ids | eval_ids == splitter.query_ids_of(source_lines), "两侧合起来不许少查询"
    ratio = len(trainable) / len(source_lines)
    assert 0.60 <= ratio <= 0.80, f"实测行占比 {ratio:.4f} 明显偏离 ~0.7"
    # 逐行搬运的强断言（字节级）：输出行是源行的子集且不重复、换行口径也是 LF，
    # 不重新序列化 = schema 与源文件同一份；CRLF 漂移在这一条上必须露头。
    source_bytes = set(source.read_bytes().split(b"\n"))
    for path in (trainable_path, evalonly_path):
        payload = path.read_bytes()
        assert b"\r\n" not in payload, f"{path.name} 出现 CRLF（源文件是 LF）"
        assert all(line in source_bytes for line in payload.split(b"\n") if line)
    for line in (trainable[0], evalonly[0]):
        assert sorted(json.loads(line)) == sorted(json.loads(source_lines[0])), \
            "两侧行的顶层字段集必须与源文件一致"
