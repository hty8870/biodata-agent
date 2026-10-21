# -*- coding: utf-8 -*-
"""llm_cross_score 断点续跑 / 失败态 / 落盘清洁的离线契约测试。

**隔离**：全部在 `tmp_path` 上以合成 vec 行 + 假 gateway 运行，不真调任何服务商、不读真实
`research/ltr_data/`。为什么需要：旧实现把 `None`（重试耗尽的失败分）当作"已完成"写进产物，
resume 时永久跳过——失败与成功在文件里长得一样（都是键存在）。本文件用已知答案把两条语义钉死：
**整行完整才落盘**、**失败行下次重跑**，并且旧的部分行会被新的完整行收敛掉（不留遮蔽）。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location("llm_cross_score", ROOT / "scripts" / "llm_cross_score.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


xscore = _load_module()


class FakeGateway:
    """记录调用的假评委：可整模型失败；成功固定返回 score=2。"""

    def __init__(self, fail_models=()):
        self.calls: list[tuple[str, str]] = []
        self.fail_models = set(fail_models)

    def __call__(self, prompt, model, api_key, max_tokens, endpoint=None):
        self.calls.append((model, prompt))
        if model in self.fail_models:
            raise RuntimeError("judge unavailable")
        return '{"score": 2}'

    def models_called(self) -> list[str]:
        return [model for model, _prompt in self.calls]


def _rows() -> list[dict]:
    return [{
        "query_id": "q-1", "query": "10x 空间转录组", "rewrite_status": "ok",
        "candidates": [{"uid": "d1", "xscore": {}}, {"uid": "d2", "xscore": {}}],
    }]


def _write_vec(in_dir: Path, rows: list[dict]) -> None:
    in_dir.mkdir(parents=True, exist_ok=True)
    (in_dir / "synth_train_vec.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _lines(dst: Path) -> list[dict]:
    if not dst.exists():
        return []
    return [json.loads(line) for line in dst.read_text(encoding="utf-8").splitlines() if line.strip()]


def _args(models=None):
    return SimpleNamespace(models=models or ["m1", "m2"], limit=0, concurrency=2,
                           max_tokens=64, endpoint=None)


def _run(tmp_path: Path, gateway: FakeGateway, monkeypatch, *, engine="rowwise", args=None):
    monkeypatch.setattr(xscore, "call_gateway", gateway)
    monkeypatch.setattr(xscore.time, "sleep", lambda *_a: None)
    in_dir = tmp_path / "ltr"
    args = args or _args()
    runner = xscore.run_split if engine == "rowwise" else xscore.run_split_pipelined
    return runner("train", in_dir, {}, args, "fake-key")


def test_complete_rows_are_published_and_resumed_without_recall(tmp_path, monkeypatch):
    _write_vec(tmp_path / "ltr", _rows())
    gateway = FakeGateway()
    result = _run(tmp_path, gateway, monkeypatch)
    assert result == {"written": 1, "incomplete_rows": 0, "failures": {}}
    dst = tmp_path / "ltr" / "synth_train_xscore.jsonl"
    rows = _lines(dst)
    assert len(rows) == 1
    cand = rows[0]["candidates"][0]
    assert cand["xscore"] == {"m1": 2, "m2": 2}
    assert cand["label_model"] == "xscore:m1,m2" and cand["xscore_n"] == 2
    assert cand["llm_semantic"] == 2 and cand["llm_semantic_single"] is None
    assert "_written" not in dst.read_text(encoding="utf-8")     # 内部记账键不落盘

    calls_after_first = len(gateway.calls)
    resumed = _run(tmp_path, FakeGateway(), monkeypatch)
    assert resumed == {"written": 0, "incomplete_rows": 0, "failures": {}}
    assert len(_lines(dst)) == 1                                  # 幂等续跑不重复产出
    assert len(gateway.calls) == calls_after_first                 # 完整 pair 不再重打


def test_failed_judge_row_stays_unpublished_and_is_retried(tmp_path, monkeypatch):
    """None 不是完成态：整行不落盘、逐模型计数失败；评委恢复后下次重跑补齐。"""
    _write_vec(tmp_path / "ltr", _rows())
    dst = tmp_path / "ltr" / "synth_train_xscore.jsonl"
    down = FakeGateway(fail_models={"m2"})
    result = _run(tmp_path, down, monkeypatch)
    assert result["written"] == 0 and result["incomplete_rows"] == 1
    assert result["failures"] == {"m2": 2}                         # 两个候选各失败一次
    assert "m2" in down.models_called() and "m1" in down.models_called()
    assert _lines(dst) == []                                       # 不完整行不落盘

    up = FakeGateway()
    retried = _run(tmp_path, up, monkeypatch)
    assert retried == {"written": 1, "incomplete_rows": 0, "failures": {}}
    assert "m2" in up.models_called()                              # 失败评委真的被重试
    assert _lines(dst)[0]["candidates"][0]["xscore"] == {"m1": 2, "m2": 2}


def test_partial_line_is_reused_and_compacted_into_one_complete_row(tmp_path, monkeypatch):
    """旧版分批写下的部分行：已成功的评委不重打，缺失的补齐后收敛成一行（原子替换）。"""
    in_dir = tmp_path / "ltr"
    _write_vec(in_dir, _rows())
    dst = in_dir / "synth_train_xscore.jsonl"
    partial = _rows()[0]
    partial["candidates"][0]["xscore"] = {"m1": 2, "m2": None}
    partial["candidates"][1]["xscore"] = {"m1": 2, "m2": None}
    dst.write_text(json.dumps(partial, ensure_ascii=False) + "\n", encoding="utf-8")

    gateway = FakeGateway()
    result = _run(tmp_path, gateway, monkeypatch)
    assert result["written"] == 1
    assert gateway.models_called() == ["m2", "m2"]                 # 只补缺口，不重打 m1
    lines = _lines(dst)
    assert len(lines) == 1                                         # 旧部分行被收敛掉
    assert lines[0]["candidates"][0]["xscore"] == {"m1": 2, "m2": 2}


def test_pipelined_engine_has_same_publish_and_resume_semantics(tmp_path, monkeypatch):
    _write_vec(tmp_path / "ltr", _rows())
    down = FakeGateway(fail_models={"m2"})
    assert _run(tmp_path, down, monkeypatch, engine="pipelined")["written"] == 0
    assert _lines(tmp_path / "ltr" / "synth_train_xscore.jsonl") == []

    up = FakeGateway()
    result = _run(tmp_path, up, monkeypatch, engine="pipelined")
    assert result["written"] == 1
    text = (tmp_path / "ltr" / "synth_train_xscore.jsonl").read_text(encoding="utf-8")
    assert "_written" not in text and text.count("\n") == 1


@pytest.mark.parametrize("engine", ["rowwise", "pipelined"])
def test_done_candidate_is_backfilled_from_disk_before_skip(tmp_path, monkeypatch, engine):
    """一 candidate 盘上已完整、另一 candidate 有缺口时，先回填 prior 再判 skip。

    旧实现对 done 的 candidate 直接 continue（内存 xscore 仍是源行的空 dict）→ 补齐缺口后
    `_row_complete` 永远 False → 整行不发布，下次又把 d2 的 m2 重打一遍（反复付费）。"""
    in_dir = tmp_path / "ltr"
    _write_vec(in_dir, _rows())
    dst = in_dir / "synth_train_xscore.jsonl"
    partial = _rows()[0]
    partial["candidates"][0]["xscore"] = {"m1": 1, "m2": 3}       # d1 两评委都在（旧行已完整）
    partial["candidates"][1]["xscore"] = {"m1": 2, "m2": None}    # d2 只有 m1
    partial["_written"] = True                                    # 旧版内部记账键：随旧行一起退盘
    dst.write_text(json.dumps(partial, ensure_ascii=False) + "\n", encoding="utf-8")

    gateway = FakeGateway()
    result = _run(tmp_path, gateway, monkeypatch, engine=engine)
    assert result == {"written": 1, "incomplete_rows": 0, "failures": {}}
    assert gateway.models_called() == ["m2"]                      # 只补 d2 的缺口一次
    rows = _lines(dst)
    assert len(rows) == 1                                         # 部分旧行被完整新行收敛
    cands = {c["uid"]: c for c in rows[0]["candidates"]}
    assert cands["d1"]["xscore"] == {"m1": 1, "m2": 3}            # done candidate 的盘上分进内存并原样落盘
    assert cands["d2"]["xscore"] == {"m1": 2, "m2": 2}
    assert cands["d1"]["label_model"] == "xscore:m1,m2"           # 归因只含真实成功评委
    assert "_written" not in dst.read_text(encoding="utf-8")

    resumed = FakeGateway()
    assert _run(tmp_path, resumed, monkeypatch, engine=engine) == {
        "written": 0, "incomplete_rows": 0, "failures": {}}
    assert resumed.calls == []                                    # 完整行后再跑零调用


def test_label_model_names_only_successful_judges():
    assert xscore._label_model({"m1": 3, "m2": None, "m3": 1}, ["m1", "m2", "m3"]) == "xscore:m1,m3"
    assert xscore._label_model({}, ["m1", "m2"]) == "xscore:"
    assert xscore._is_int_score(2) is True and xscore._is_int_score(True) is False
    assert xscore._is_int_score(None) is False and xscore._is_int_score("2") is False


def test_load_progress_marks_only_full_pairs_done(tmp_path):
    dst = tmp_path / "synth_train_xscore.jsonl"
    dst.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in [
        {"query_id": "q-partial",
         "candidates": [{"uid": "d1", "xscore": {"m1": 2, "m2": None}}]},
        {"query_id": "q-full",
         "candidates": [{"uid": "d1", "xscore": {"m1": 3, "m2": 1}}]},
    ]), encoding="utf-8")
    done, prior, known = xscore.load_progress(dst, ["m1", "m2"])
    assert known == {"q-partial", "q-full"}
    assert xscore.pair_key("q-full", "d1") in done
    assert xscore.pair_key("q-partial", "d1") not in done          # 失败分不算完成
    assert prior[xscore.pair_key("q-partial", "d1")] == {"m1": 2}  # 成功分被保留复用


# ---------------------------------------------------------------------------
# 跨旧行的互补 partial 必须在零调用下收敛成唯一完整共识行
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("engine", ["rowwise", "pipelined"])
def test_complementary_partial_rows_converge_without_calls(tmp_path, monkeypatch, engine):
    """load_progress 跨旧行补全 prior（pair 判 done），但 run 层 todo 为空直接跳过——
    没有任何单行完整时，互补的两条 partial 旧行永远物化不出完整共识行。

    验收：零 gateway 调用也要写一条完整行（平均分/judge/spread 正确、无 _written），
    两条旧 partial 被收敛；第二次零写 bytes 不变；其他 qid 行原样保留。"""
    in_dir = tmp_path / "ltr"
    _write_vec(in_dir, _rows())
    dst = in_dir / "synth_train_xscore.jsonl"
    old1 = _rows()[0]
    old1["candidates"][0]["xscore"] = {"m1": 1, "m2": None}
    old1["candidates"][1]["xscore"] = {"m1": 2, "m2": None}
    old2 = _rows()[0]
    old2["candidates"][0]["xscore"] = {"m1": None, "m2": 3}
    old2["candidates"][1]["xscore"] = {"m1": None, "m2": 0}
    other = {"query_id": "q-other", "query": "别的查询",
             "candidates": [{"uid": "d9", "xscore": {"m1": 2, "m2": None}}]}
    dst.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in (old1, old2, other)),
                   encoding="utf-8")

    gateway = FakeGateway()
    result = _run(tmp_path, gateway, monkeypatch, engine=engine)
    assert gateway.calls == []                                   # 零 provider 调用
    assert result == {"written": 1, "incomplete_rows": 0, "failures": {}}
    rows = _lines(dst)
    assert [r["query_id"] for r in rows] == ["q-other", "q-1"]   # 唯一完整行；其他 qid 保留
    cands = {c["uid"]: c for c in rows[1]["candidates"]}
    assert cands["d1"]["xscore"] == {"m1": 1, "m2": 3}
    assert cands["d1"]["llm_semantic"] == 2                      # median(1,3)=2
    assert cands["d2"]["xscore"] == {"m1": 2, "m2": 0}
    assert cands["d2"]["llm_semantic"] == 1                      # median(0,2)=1
    for cand in cands.values():
        assert cand["label_model"] == "xscore:m1,m2"             # 只记真实成功评委
        assert cand["xscore_n"] == 2 and cand["xscore_spread"] == 2
    assert "_written" not in dst.read_text(encoding="utf-8")
    assert rows[0]["candidates"][0]["xscore"] == {"m1": 2, "m2": None}   # 其他 qid 内容不动

    before = dst.read_bytes()
    again = FakeGateway()
    assert _run(tmp_path, again, monkeypatch, engine=engine) == {
        "written": 0, "incomplete_rows": 0, "failures": {}}
    assert again.calls == []                                     # 已有唯一规范行：零调用零写
    assert dst.read_bytes() == before                            # 重跑 bytes 幂等


@pytest.mark.parametrize("engine", ["rowwise", "pipelined"])
def test_source_row_already_scored_is_materialized_without_calls(tmp_path, monkeypatch, engine):
    """源行已含全部成功评分、磁盘缺完整行：零调用物化；含 None 的缺口仍不发布。"""
    in_dir = tmp_path / "ltr"
    rows = _rows()
    rows[0]["candidates"][0]["xscore"] = {"m1": 3, "m2": 1}
    rows[0]["candidates"][1]["xscore"] = {"m1": 0, "m2": 2}
    _write_vec(in_dir, rows)
    dst = in_dir / "synth_train_xscore.jsonl"

    gateway = FakeGateway()
    result = _run(tmp_path, gateway, monkeypatch, engine=engine)
    assert gateway.calls == []
    assert result == {"written": 1, "incomplete_rows": 0, "failures": {}}
    cands = {c["uid"]: c for c in _lines(dst)[0]["candidates"]}
    assert cands["d1"]["xscore"] == {"m1": 3, "m2": 1} and cands["d1"]["llm_semantic"] == 2
    assert cands["d2"]["llm_semantic"] == 1

    # 源行带 None（未成功）且磁盘无分可补 → 不是完整行，走调用路径；评委仍失败则永不发布
    rows2 = _rows()
    rows2[0]["candidates"][0]["xscore"] = {"m1": 3, "m2": None}
    rows2[0]["candidates"][1]["xscore"] = {"m1": 0, "m2": 2}
    tmp2 = tmp_path / "case2"
    _write_vec(tmp2 / "ltr", rows2)
    down = FakeGateway(fail_models={"m2"})
    result2 = _run(tmp2, down, monkeypatch, engine=engine)
    assert result2["written"] == 0 and result2["incomplete_rows"] == 1
    assert _lines(tmp2 / "ltr" / "synth_train_xscore.jsonl") == []   # 失败 None 不发布
