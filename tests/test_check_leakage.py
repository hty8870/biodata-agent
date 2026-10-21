# -*- coding: utf-8 -*-
"""Leakage guard for generated query waves: known answers, verdicts, and the
self-test path the release runbook depends on.

**隔离**：全部读写都在 `tmp_path` 下的合成项目根里（合成评测集 + 合成语料 + 合成波），
不读真实 `eval/`、不读真实 `research/`、无网络、无 LLM。真实判交报告由 CLI 在流水线里产出。

为什么这类守卫需要单测：一个恒返回 PASS 的守卫与一个正确的守卫在真实跑批里长得一模一样
（都只打印"没撞车"）。本文件用**已知答案的正反例**把守卫钉死：逐字复制、仅标点/全角差异的
复制都必须被抓；同一维度上的干净行必须留下。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import check_leakage  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def _roots(tmp_path: Path) -> "tuple[Path, Path]":
    """Synthetic project root + a wave with one known-good and one known-bad row."""
    root = tmp_path / "root"
    _write_jsonl(root / "eval" / "eval_queries.json",
                 [{"query": "人类肺组织的单细胞数据，要能下载"}, {"query": "小鼠海马单细胞数据"}])
    _write_jsonl(root / "research" / "ltr_data" / "synth_train.jsonl",
                 [{"query": "人类心脏的单细胞数据"}])
    wave = tmp_path / "wave.jsonl"
    _write_jsonl(wave, [
        {"qid": "w-000", "query": "人类肺组织的单细胞数据，要能下载"},
        {"qid": "w-001", "query": "人类肺组织　的单细胞数据，要能下载！！！"},
        {"qid": "w-002", "query": "找点小鼠海马的转录组数据"},
        {"qid": "w-003", "query": "人类心脏的单细胞数据"},
    ])
    return root, wave


# ---------------------------------------------------------------- 归一化
def test_normalize_folds_width_case_and_punctuation() -> None:
    assert check_leakage.normalize("Fei  Xian-Wei：Hua！") == check_leakage.normalize("feixianweihua")
    assert check_leakage.normalize("ＧＳＥ１２３") == check_leakage.normalize("gse123")
    assert check_leakage.normalize("") == ""
    # 不同内容不得被折叠成同一个键（防止归一化过头把所有东西都判成撞车）
    assert check_leakage.normalize("肺癌数据") != check_leakage.normalize("肝癌数据")


def test_query_keys_config_covers_utterance_and_keeps_priority_order() -> None:
    """`utterance` 是 query 键配置的一部分（agent live cases 的用户原话字段），不是特例分支。"""
    assert "utterance" in check_leakage.QUERY_KEYS
    assert check_leakage.query_of({"utterance": "  找点数据  "}) == "找点数据"
    assert check_leakage.query_of({"query": "优先", "utterance": "次要"}) == "优先"


def test_reference_paths_are_literal_not_globs() -> None:
    """Wave layers live in the same directory as the training corpus; a glob such as
    ``synth_train*.jsonl`` would make a checked wave a duplicate of itself."""
    for _name, relative, kind in check_leakage.REFERENCE_SETS:
        assert "*" not in relative and "?" not in relative, relative
        assert kind in ("eval", "train")


# ---------------------------------------------------------------- 裁决
def test_verbatim_and_normalised_copies_are_fatal(tmp_path: Path) -> None:
    root, wave = _roots(tmp_path)
    report, kept = check_leakage.check(wave, root)
    caught = {item["qid"] for item in report["eval_collisions"]}
    assert caught == {"w-000", "w-001"}
    assert report["verdict"] == "FAIL"
    assert [row["qid"] for row in kept] == ["w-002"]


def test_train_collision_is_dropped_without_failing(tmp_path: Path) -> None:
    root, wave = _roots(tmp_path)
    report, kept = check_leakage.check(wave, root)
    assert [item["qid"] for item in report["train_duplicates"]] == ["w-003"]
    assert "w-003" not in {row["qid"] for row in kept}
    assert report["eval_collisions"]  # 该波整体仍然失败（撞评测集）


def test_clean_wave_passes_and_keeps_every_row(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _write_jsonl(root / "eval" / "eval_queries.json", [{"query": "人类肺组织的单细胞数据"}])
    wave = tmp_path / "clean.jsonl"
    _write_jsonl(wave, [{"qid": "c-0", "query": "找点小鼠海马的转录组数据"},
                        {"qid": "c-1", "query": "有没有青蛙的分类数据"}])
    report, kept = check_leakage.check(wave, root)
    assert report["verdict"] == "PASS"
    assert [row["qid"] for row in kept] == ["c-0", "c-1"]


def test_row_without_comparable_text_is_never_kept(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "eval").mkdir(parents=True)
    wave = tmp_path / "wave.jsonl"
    _write_jsonl(wave, [{"qid": "bad", "query": "   "}, {"qid": "ok", "query": "青蛙的数据"}])
    _report, kept = check_leakage.check(wave, root)
    assert [row["qid"] for row in kept] == ["ok"]


def test_missing_reference_set_is_reported(tmp_path: Path) -> None:
    """A mistyped root must not make the guard vacuously green."""
    empty = tmp_path / "nowhere"
    empty.mkdir()
    wave = tmp_path / "wave.jsonl"
    _write_jsonl(wave, [{"qid": "w-0", "query": "青蛙的数据"}])
    report, _kept = check_leakage.check(wave, empty)
    assert report["reference_sets"]
    assert all(not ref["exists"] for ref in report["reference_sets"])


def test_selftest_known_answers_pass() -> None:
    """The CLI's own known-answer battery (used as the release-runbook gate)."""
    assert check_leakage.selftest() == 0


# ---------------------------------------------------------------- CLI
def test_cli_writes_report_and_filtered_wave(tmp_path: Path) -> None:
    root, wave = _roots(tmp_path)
    report_path = tmp_path / "report.json"
    filtered = tmp_path / "filtered.jsonl"
    code = check_leakage.main(["--wave", str(wave), "--project-root", str(root),
                               "--report", str(report_path), "--filtered", str(filtered)])
    assert code == 1  # 撞评测集 -> 非零退出（fail-closed）
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["verdict"] == "FAIL"
    kept = [json.loads(line) for line in filtered.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [row["qid"] for row in kept] == ["w-002"]


def test_cli_rejects_missing_wave_argument() -> None:
    with pytest.raises(SystemExit):
        check_leakage.main([])


# ------------------------------------------- 真实导出形态：漂亮 JSON / 行尾逗号 / 破行
def _pretty_eval_file(path: Path, rows: list[dict]) -> None:
    """写真实导出形态：对象包装 + 数组每个元素占一行（除最后一个元素外每行都以逗号结尾）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = ",\n".join("    " + json.dumps(row, ensure_ascii=False) for row in rows)
    path.write_text("{\n  \"queries\": [\n" + body + "\n  ]\n}\n", encoding="utf-8")


def test_pretty_printed_eval_sets_count_and_match_every_query(tmp_path: Path) -> None:
    """漂亮打印的评测集必须整份参与泄漏判定：逐行解析只会看到没有尾逗号的最后一行，
    导致非末条 query 的撞车被静默放行。54/50/dev 三个真实规模各钉一次计数。"""
    root = tmp_path / "root"
    middle = {"id": "sp30", "query": "人类肺组织的单细胞数据，要能下载"}
    last = {"id": "sp54", "query": "小鼠海马单细胞数据"}
    _pretty_eval_file(root / "eval" / "eval_queries.json",
                      [{"id": f"sp{i:02d}", "query": f"合成查询 {i}"} for i in range(1, 53)]
                      + [middle, last])
    _pretty_eval_file(root / "eval" / "eval_queries_holdout.json",
                      [{"id": f"h{i:02d}", "query": f"holdout {i}"} for i in range(1, 51)])
    _pretty_eval_file(root / "eval" / "eval_queries_dev.json",
                      [{"id": f"d{i:02d}", "query": f"dev {i}"} for i in range(1, 56)])
    wave = tmp_path / "wave.jsonl"
    _write_jsonl(wave, [
        {"qid": "w-mid", "query": middle["query"]},
        {"qid": "w-last", "query": last["query"]},
        {"qid": "w-clean", "query": "青蛙的分类数据"},
    ])
    report, kept = check_leakage.check(wave, root)
    by_name = {ref["name"]: ref for ref in report["reference_sets"]}
    assert by_name["eval_queries"]["rows"] == 54
    assert by_name["eval_queries_holdout"]["rows"] == 50
    assert by_name["eval_queries_dev"]["rows"] == 55
    caught = {item["qid"] for item in report["eval_collisions"]}
    assert caught == {"w-mid", "w-last"}          # 非末条（第 53 条）也必须被抓
    assert report["verdict"] == "FAIL"
    assert [row["qid"] for row in kept] == ["w-clean"]


def test_jsonl_reference_with_trailing_commas_counts_every_row(tmp_path: Path) -> None:
    """行尾逗号的 JSONL（分批导出/手工拼接的中间形态）也要整份读入，不能只留末行。"""
    root = tmp_path / "root"
    ref = root / "eval" / "agent_live_cases_v1.jsonl"
    ref.parent.mkdir(parents=True)
    ref.write_text('{"query": "第一条"},\n{"query": "第二条"},\n{"query": "第三条"}\n',
                   encoding="utf-8")
    wave = tmp_path / "wave.jsonl"
    _write_jsonl(wave, [{"qid": "w-1", "query": "第一条"}, {"qid": "w-clean", "query": "干净"}])
    report, _kept = check_leakage.check(wave, root)
    ref_info = next(ref for ref in report["reference_sets"] if ref["name"] == "agent_live_cases_v1")
    assert ref_info["rows"] == 3 and ref_info["unique"] == 3
    assert [item["qid"] for item in report["eval_collisions"]] == ["w-1"]


def test_utterance_rows_join_the_reference_index(tmp_path: Path) -> None:
    """agent live cases 的 utterance 行必须进参考索引：此前 rows 读入但 unique=0，
    索引为空 → 撞车判定对该套完全失效（绿报告是空判）。"""
    root = tmp_path / "root"
    _write_jsonl(root / "eval" / "agent_live_cases_v1.jsonl", [
        {"id": "a01", "utterance": "库里现在有多少条数据"},
        {"id": "a02", "utterance": "帮我看看 ENCODE 有没有更新"},
        {"id": "a03", "utterance": "换一批推荐"},
    ])
    wave = tmp_path / "wave.jsonl"
    _write_jsonl(wave, [{"qid": "w-mid", "query": "帮我看看 ENCODE 有没有更新"},
                        {"qid": "w-clean", "query": "人类肺组织的单细胞数据"}])
    report, kept = check_leakage.check(wave, root)
    ref_info = next(ref for ref in report["reference_sets"] if ref["name"] == "agent_live_cases_v1")
    assert ref_info["rows"] == 3 and ref_info["unique"] == 3
    assert [item["qid"] for item in report["eval_collisions"]] == ["w-mid"]
    assert [row["qid"] for row in kept] == ["w-clean"]


def test_reference_with_rows_but_no_query_text_fails_closed(tmp_path: Path) -> None:
    """参考集在、行也在、却一条可比对 query 都提不出来：必须 fail-closed，而不是绿空报告。"""
    root = tmp_path / "root"
    _write_jsonl(root / "eval" / "agent_live_cases_v1.jsonl",
                 [{"id": "a01", "note": "字段名漂移，query 键一个都不在"}])
    wave = tmp_path / "wave.jsonl"
    _write_jsonl(wave, [{"qid": "w-clean", "query": "青蛙的数据"}])
    with pytest.raises(ValueError, match="提取不到任何可比对 query"):
        check_leakage.check(wave, root)
    report_path = tmp_path / "report.json"
    assert check_leakage.main(["--wave", str(wave), "--project-root", str(root),
                               "--report", str(report_path)]) == 2
    assert not report_path.exists()


def test_malformed_reference_line_fails_closed_not_silently_green(tmp_path: Path) -> None:
    """参考集里解析不了的行不允许被跳过：半个索引的 PASS 是假绿，必须 fail-closed。"""
    root = tmp_path / "root"
    ref = root / "eval" / "agent_live_cases_v1.jsonl"
    ref.parent.mkdir(parents=True)
    ref.write_text('{"query": "第一条"}\n{ 这一行不是 JSON\n', encoding="utf-8")
    wave = tmp_path / "wave.jsonl"
    _write_jsonl(wave, [{"qid": "w-clean", "query": "干净"}])
    with pytest.raises(ValueError, match="参考集拒绝静默降级"):
        check_leakage.check(wave, root)

    report_path = tmp_path / "report.json"
    code = check_leakage.main(["--wave", str(wave), "--project-root", str(root),
                               "--report", str(report_path)])
    assert code == 2
    assert not report_path.exists()               # 不给判定，也不落盘假 PASS 报告


def test_torn_wave_line_is_tolerated_and_never_keeps_junk(tmp_path: Path) -> None:
    """波文件允许崩溃破行（torn line）：该行丢弃，正常行照常判定/保留。"""
    root = tmp_path / "root"
    _write_jsonl(root / "eval" / "eval_queries.json", [{"query": "人类肺组织的单细胞数据"}])
    wave = tmp_path / "wave.jsonl"
    wave.write_text('{"qid": "w-ok", "query": "青蛙的数据"}\n{"qid": "w-torn", "query": "半行\n',
                    encoding="utf-8")
    report, kept = check_leakage.check(wave, root)
    assert [row["qid"] for row in kept] == ["w-ok"]
    assert report["verdict"] == "PASS"
