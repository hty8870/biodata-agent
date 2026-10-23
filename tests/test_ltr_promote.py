# -*- coding: utf-8 -*-
"""LTR 转正管线的端到端机械验收：accept / reject / rollback / status / REGISTRY。

**隔离**：一切读写都在 `tmp_path` 下的一套**合成项目根**里（合成语料 + 手工制品），
不碰真实 `models/ltr/`、真实语料与真实流水账；无网络、无 LLM、无真实训练
（制品是手工写权重的 RankNet MLP，用 `inv_pos` 的正负号把"好/坏排序"做成确定性的）。

**假体为什么可信**：门 1 的重载一致性、门 2 的 heldout 对比、门 3 的 OOD 冻结对照
都是**真实现**在假数据上跑（`heldout_metrics_of` / `ood_duel.compare_frozen`）；
只有门 4（召回开启的 784）用 `--frozen-report` 注入一份同形报告——那一门要 784 条真实
语料 + 真实嵌入器 + 54 题真跑，属于集成验收（见 docs/QUALITY_GATES.md），
不适合放进单测。故本文件的 accept 路径证明的是"门的判据与状态机"，不是"制品本身有多好"。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import ltr_promote  # noqa: E402
from train_ltr import DEFAULT_SEED, FEATURE_NAMES  # noqa: E402

INCUMBENT_TRAINED_AT = "2026-09-14T00:23:54+08:00"
CANDIDATE_TRAINED_AT = "2026-09-29T10:00:00+08:00"


# ---------------------------------------------------------------- 合成语料
def _cand(uid: str, pos: int, lex: float, vec: float, grade: int, sem: int, matched: dict) -> dict:
    return {"uid": uid, "pos": pos, "lex_score": lex, "vec_score": vec,
            "grade": grade, "llm_semantic": sem, "matched": matched}


def _row(query_id: str, *, ood_class: str | None = None) -> dict:
    """一条查询：三个候选，标注随 pos 递减 → **理想顺序 = 文件顺序**。"""
    row = {
        "query_id": query_id,
        "query": f"{query_id} 的查询文本",
        "has_raw_data_required": True,
        "candidates": [
            _cand("A", 1, 9.0, 0.9, 2, 3, {"species": True, "disease": True, "has_raw_data": True}),
            _cand("B", 2, 6.0, 0.5, 1, 2, {"species": True}),
            _cand("C", 3, 3.0, 0.1, 0, 0, {}),
        ],
    }
    if ood_class is not None:
        row["ood_class"] = ood_class
        row["constraints"] = {}
    return row


TRAIN_ROWS = [_row(f"train-{i}") for i in range(1, 4)]
HELDOUT_ROWS = [_row(f"heldout-{i}") for i in range(1, 3)]
#: OOD 的两半（`scripts/split_ood_evalonly.py` 拆出来的形状）：可训练层进 mix-in、只评测层
#: 当门 3 的评测集，两侧 query_id 不相交——合成夹具也照这个分工摆，别把评测行放进训练层。
TRAINABLE_ROWS = [_row(f"ood-trainable-{i}", ood_class="underspecified") for i in range(1, 3)]
EVALONLY_ROWS = [_row(f"ood-evalonly-{i}", ood_class="underspecified") for i in range(1, 3)]

TRAINABLE_REL = "research/ood_eval/synth_ood_trainable_xscore.jsonl"
EVALONLY_REL = "research/ood_eval/synth_ood_evalonly_xscore.jsonl"

#: 训练基准的查询数 = train 层 + mix-in 层：**可训练层**按 `MIX_IN_PATTERNS` 命中，
#: 会被 `merge_mix_in` 并进 TRAIN（只进 train、绝不碰 heldout）。攒量口径因此也含它；
#: 只评测层不在其中（它只服务门 3）。
BASIS_QUERIES = len(TRAIN_ROWS) + len(TRAINABLE_ROWS)


def _write_jsonl(path: Path, rows: "list[dict]") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """合成项目根：语料 + OOD 两半齐备，`models/ltr/` 由各测试自己钉现役制品。"""
    root = tmp_path / "proj"
    in_dir = root / "research" / "ltr_data"
    _write_jsonl(in_dir / "synth_train.jsonl", TRAIN_ROWS)
    _write_jsonl(in_dir / "synth_heldout.jsonl", HELDOUT_ROWS)
    _write_jsonl(root / TRAINABLE_REL, TRAINABLE_ROWS)
    _write_jsonl(root / EVALONLY_REL, EVALONLY_ROWS)
    return root


# ---------------------------------------------------------------- 手工制品
def _make_artifact(model_dir: Path, *, good: bool, trained_at: str) -> None:
    """手工 RankNet MLP：`good=True` → 分数 = +inv_pos（理想顺序）；False → −inv_pos（反序）。

    只写 `ltr_mlp.pt` + `ltr_spec.json`（`heldout_metrics` 先留空，见 `_build_artifact`）。
    """
    import torch

    import train_ltr

    model_dir.mkdir(parents=True, exist_ok=True)
    net = train_ltr._build_mlp(len(FEATURE_NAMES))
    with torch.no_grad():
        for parameter in net.parameters():
            parameter.zero_()
        net[0].weight[0, FEATURE_NAMES.index("inv_pos")] = 1.0   # h0[0] = inv_pos
        net[2].weight[0, 0] = 1.0                                # h1[0] = ReLU(inv_pos) = inv_pos
        net[4].weight[0, 0] = 1.0 if good else -1.0              # out = ± inv_pos
    torch.save({k: v.detach().clone() for k, v in net.state_dict().items()}, model_dir / "ltr_mlp.pt")
    _write_json(model_dir / ltr_promote.SPEC_NAME, {
        "feature_order": list(FEATURE_NAMES),
        "label_mode": "mix",
        "seed": train_ltr.DEFAULT_SEED,
        "feature_mean": [0.0] * len(FEATURE_NAMES),
        "feature_std": [1.0] * len(FEATURE_NAMES),
        "trained_at": trained_at,
        "train_counts": {"queries": len(TRAIN_ROWS), "pairs": 6},
        "heldout_metrics": None,          # 回填见 _build_artifact
        "model": {"type": "ranknet_mlp", "input_dim": len(FEATURE_NAMES), "hidden_layers": [32, 16],
                  "activation": "relu", "output_dim": 1, "loss": "pairwise_logistic"},
    })


def _build_artifact(model_dir: Path, *, good: bool, trained_at: str, root: Path) -> dict:
    """造一份制品，并用真代码在合成 heldout 上重算 → 回填 `heldout_metrics`。

    回填的意义：门 1 的一致性检查于是成为"真信号"——改坏制品而不改声明，门 1 当场红。
    """
    _make_artifact(model_dir, good=good, trained_at=trained_at)
    measured = ltr_promote.heldout_metrics_of(model_dir, root / "research" / "ltr_data",
                                              mode="mix", repo_root=root)
    spec = json.loads((model_dir / ltr_promote.SPEC_NAME).read_text(encoding="utf-8"))
    spec["heldout_metrics"] = {key: measured["metrics"][key] for key in ("nDCG@5", "nDCG@10", "MRR")}
    _write_json(model_dir / ltr_promote.SPEC_NAME, spec)
    return spec


def make_incumbent(root: Path, *, good: bool) -> dict:
    return _build_artifact(root / "models" / "ltr", good=good,
                           trained_at=INCUMBENT_TRAINED_AT, root=root)


def _candidate_meta(root: Path, candidate_dir: Path, *, version: str, spec: dict) -> dict:
    in_dir = root / "research" / "ltr_data"
    return {
        "schema_version": 1,
        "version": version,
        "created_at": ltr_promote.now_iso(),
        "trained_at": spec["trained_at"],
        "label_mode": spec["label_mode"],
        "seed": spec["seed"],
        "in_dir": str(in_dir),
        "mix_in": [],
        "train_command": ["python", "scripts/train_ltr.py"],
        "train_exit_code": 0,
        "selfcheck_max_delta": 0.0,
        "data_fingerprint": {
            "train": ltr_promote.data_fingerprint([in_dir / "synth_train.jsonl"], root),
            "heldout": ltr_promote.data_fingerprint([in_dir / "synth_heldout.jsonl"], root),
        },
        "basis": {"train_queries": len(TRAIN_ROWS), "train_pairs": 6,
                  "heldout_queries": len(HELDOUT_ROWS)},
        "train_counts": spec["train_counts"],
        "heldout_metrics": spec["heldout_metrics"],
        "artifact_sha256": ltr_promote.file_sha_map(
            [candidate_dir / name for name in ltr_promote.ARTIFACT_NAMES], candidate_dir),
        "git_sha": None,
    }


def make_candidate(root: Path, *, good: bool, version: str) -> Path:
    candidate_dir = root / "research" / "ltr_data" / "_candidates" / version
    spec = _build_artifact(candidate_dir, good=good, trained_at=CANDIDATE_TRAINED_AT, root=root)
    _write_json(candidate_dir / ltr_promote.CANDIDATE_META,
                _candidate_meta(root, candidate_dir, version=version, spec=spec))
    return candidate_dir


def _frozen_pass_report(tmp_path: Path) -> Path:
    """门 4 的同形报告（真跑一次的形状；见模块 docstring 的口径说明）。"""
    path = tmp_path / "_frozen_recall_report.json"
    _write_json(path, {"recall_backend": "dense", "metrics": {
        "n_queries": 54, "Top1_Accuracy_%": 97.7, "Top5_Hit_Rate_%": 97.7,
        "Constraint_Violation_%": 0.0, "FASTQ_Violation_%": 0.0, "NoResult_Correct": "10/10"}})
    return path


def _run(capsys, *argv) -> "tuple[int, str]":
    code = ltr_promote.main([str(a) for a in argv])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def _shas(ltr_dir: Path) -> dict:
    """制品目录的 {文件名: SHA-256}（键与根无关，便于跨目录逐位对比）。"""
    return {name: ltr_promote.sha256_file(ltr_dir / name) for name in ltr_promote.ARTIFACT_NAMES}


# ---------------------------------------------------------------- status
def test_status_counts_delta_and_threshold(project: Path, capsys, monkeypatch):
    """攒量数学：当前基准 − 现役 train_counts = 新增；阈值内/外与退出码。"""
    spec = make_incumbent(project, good=True)
    spec["train_counts"] = {"queries": 1, "pairs": 2}          # 人为压低 → 新增 2 查询
    _write_json(project / "models" / "ltr" / ltr_promote.SPEC_NAME, spec)

    monkeypatch.setattr(ltr_promote, "NEW_QUERIES_THRESHOLD", 5)
    code, out = _run(capsys, "--project-root", project, "status", "--require-ready")
    report = ltr_promote.status_report(ltr_promote.Paths(project), project / "research" / "ltr_data",
                                       seed=1, label="mix")
    assert report["basis"]["train_queries"] == BASIS_QUERIES
    assert report["delta"]["queries"] == BASIS_QUERIES - 1
    assert report["threshold"] == {"new_queries": 5, "gap": 5 - (BASIS_QUERIES - 1), "ready": False}
    assert "还差" in out and "NOT_READY" in out
    assert code == 1, "--require-ready 在未达阈值时必须非零退出（cron 判据）"

    monkeypatch.setattr(ltr_promote, "NEW_QUERIES_THRESHOLD", BASIS_QUERIES - 1)
    code, out = _run(capsys, "--project-root", project, "status", "--require-ready")
    assert "已达阈值" in out and "READY" in out and code == 0


def test_status_without_incumbent_artifact(tmp_path: Path, project: Path, capsys, monkeypatch):
    """无现役制品说明时不许崩：增量为 None、判 NOT_READY。"""
    monkeypatch.setattr(ltr_promote, "NEW_QUERIES_THRESHOLD", 1)
    code, out = _run(capsys, "--project-root", project, "status")
    assert "无法计算增量" in out and "NOT_READY" in out and code == 0


# ---------------------------------------------------------------- candidate（命令构造）
def test_basis_report_follows_the_actual_mix_ins(tmp_path: Path, project: Path):
    """基准与指纹必须跟着**实际传给候选的 mix-in** 走，而不是缺省模式。

    回归（2026-09-29 实测）：`candidate --mix-in X` 覆盖缺省层时，旧实现仍按
    `MIX_IN_PATTERNS` 记账，于是两份训练基准不同的候选会算出**同一个指纹** → 同一个版本号，
    `promote` 的"同版本拒绝"与"指纹变了才需要重训"两条判断同时失效，REGISTRY 里的
    基准计数也会写错（实测：1810 查询的训练被记成 1623）。
    """
    in_dir = project / "research" / "ltr_data"
    # 注意名字：`synth_train_wave*_xscore.jsonl` 现在**是**缺省层之一（波次进攒量口径），
    # 所以这里用一个缺省模式刻意不命中的名字，才能验"显式 --mix-in 覆盖缺省"。
    extra = in_dir / "synth_train_extra_20260930_xscore.jsonl"
    _write_jsonl(extra, [_row("extra-1"), _row("extra-2")])
    default_basis = ltr_promote.basis_report(ltr_promote.Paths(project), in_dir, seed=1, label="mix")
    # `candidate` 的实参形状：显式给出缺省层 + 新层（`--mix-in` 是替换语义，不是追加）。
    mix_ins = ltr_promote.Paths(project).mix_in_paths() + [extra]
    with_extra = ltr_promote.basis_report(ltr_promote.Paths(project), in_dir, seed=1, label="mix",
                                          mix_ins=mix_ins)
    assert with_extra["train_queries"] == default_basis["train_queries"] + 2
    assert with_extra["train_pairs"] >= default_basis["train_pairs"]
    assert with_extra["data_fingerprint"]["train"] != default_basis["data_fingerprint"]["train"]
    assert with_extra["data_fingerprint"]["heldout"] == default_basis["data_fingerprint"]["heldout"]


def test_candidate_dry_run_command_shape(project: Path, capsys):
    """`candidate` 的包装命令：训练入口、导出目录、mix-in 逐项展开、确定性旗标、--dry-run 不落盘。"""
    command = ltr_promote.train_command("py", project, project / "research" / "ltr_data",
                                        project / "out", label="mix", seed=7,
                                        mix_ins=[project / "research" / "ood_eval" / "x.jsonl"])
    assert command[:2] == ["py", str(project / "scripts" / "train_ltr.py")]
    assert "--export" in command and str(project / "out") in command
    assert "--deterministic" in command, "候选训练必须钉住数值环境（重训抖动与门禁容差同量级）"
    assert command[command.index("--mix-in") + 1] == str(project / "research" / "ood_eval" / "x.jsonl")
    assert command[command.index("--seed") + 1] == "7"
    # 配方旗标显式进命令行（审计链可见）；train_command 的兼容缺省 = f0
    assert command[command.index("--feature-set") + 1] == "f0"
    assert command[command.index("--eval-feature-set") + 1] == "f0"

    code, out = _run(capsys, "--project-root", project, "candidate", "--dry-run", "--ts", "T0")
    assert code == 0 and "train_ltr.py" in out
    assert not (project / "research" / "ltr_data" / "_candidates").exists(), "--dry-run 不许落盘"


def test_candidate_subcommand_stages_artifact_meta_and_journal(project: Path, capsys, monkeypatch):
    """`candidate` 的真路径（训练被替身接管）：_candidate.json 的每个字段 + 流水账事件。

    替身只做一件事——按 `--export <dir>` 落一份制品并回放训练器的自检行；于是这里验的是
    本管线自己的记账逻辑（版本号 = 训练时间戳+数据指纹短哈希、mix-in 计入基准、
    自检 Δ 从 stdout 解析、以及"candidate 事件进流水账"）。
    """
    real_run = ltr_promote.subprocess.run

    class _Done:
        returncode = 0
        stdout = ("  export: <dir> -> ltr_mlp.pt + ltr_spec.json (9 features: lex_norm, …)\n"
                  "  export self-check: max per-query |delta nDCG@10| = 3.05e-08\n")
        stderr = ""

    def fake_run(command, **kwargs):
        """只接管训练子进程（按 --export 认人）；其余 subprocess（如 git_sha）仍走真实现。"""
        if "--export" not in command:
            return real_run(command, **kwargs)
        out_dir = Path(command[command.index("--export") + 1])
        _make_artifact(out_dir, good=True, trained_at=CANDIDATE_TRAINED_AT)
        (out_dir / "_ltr_metrics.json").write_text(
            json.dumps({"generated_at": "2026-09-29T10:00:05+08:00"}), encoding="utf-8")
        sink = kwargs.get("stdout")
        if hasattr(sink, "write"):
            sink.write(_Done.stdout)
            sink.flush()
        return _Done()

    # 只替身 subprocess.run（本测试内全局生效，monkeypatch 结束时自动还原）
    monkeypatch.setattr(ltr_promote.subprocess, "run", fake_run)
    try:
        code, out = _run(capsys, "--project-root", project, "candidate", "--ts", "T1",
                         "--feature-set", "f0")  # 本测试验记账逻辑，钉住历史配方（f3 缺省有专测）
    finally:
        monkeypatch.setattr(ltr_promote.subprocess, "run", real_run)
    assert code == 0 and "候选已就位" in out

    candidate_dir = project / "research" / "ltr_data" / "_candidates" / "T1"
    meta = json.loads((candidate_dir / ltr_promote.CANDIDATE_META).read_text(encoding="utf-8"))
    assert meta["train_exit_code"] == 0 and meta["selfcheck_max_delta"] == 3.05e-08
    assert meta["version"].startswith("20260929T100000-") and len(meta["version"].split("-")[1]) == 8
    assert meta["artifact_sha256"] == _shas(candidate_dir)
    assert meta["basis"]["train_queries"] == BASIS_QUERIES, "mix-in 必须计入基准（攒量口径与训练口径同源）"
    assert meta["mix_in"] == [TRAINABLE_REL], "缺省 mix-in 只并可训练层；只评测层绝不进训练"
    assert meta["data_fingerprint"]["train"] == ltr_promote.data_fingerprint(
        [project / "research" / "ltr_data" / "synth_train.jsonl", project / TRAINABLE_REL], project)
    assert meta["trainer_metrics_generated_at"] == "2026-09-29T10:00:05+08:00"
    assert _Done.stdout in (candidate_dir / "_train_stdout.txt").read_text(encoding="utf-8")

    journal = (project / ".userdata" / ltr_promote.JOURNAL_NAME).read_text(encoding="utf-8")
    assert '"action": "candidate"' in journal and meta["version"] in journal


# ---------------------------------------------------------------- 门 3：OOD 评测集独立性
def _ood_row(query_id: str, *, swap_source: bool = False) -> dict:
    """一条 OOD 查询：A>B>C 的标注；`swap_source=True` 时给 C 一个足以翻身的词面分。

    候选制品只认 `inv_pos`（理想顺序）或 `inv_pos + 0.2·lex_norm`（C 挤到 B 前面），
    于是"仅这一条查询的 nDCG 掉了"可以被精确构造出来。
    """
    def cand(uid: str, pos: int, lex: float, grade: int, sem: int) -> dict:
        return {"uid": uid, "pos": pos, "lex_score": lex, "vec_score": None,
                "grade": grade, "llm_semantic": sem,
                "matched": {"species": True, "disease": True, "has_raw_data": True}}

    return {"query_id": query_id, "query": f"{query_id} 的查询文本", "ood_class": "underspecified",
            "constraints": {},
            "candidates": [cand("A", 1, 0.0, 2, 3), cand("B", 2, 0.0, 1, 2),
                           cand("C", 3, 10.0 if swap_source else 0.0, 0, 0)]}


def _make_ood_artifact(model_dir: Path, *, tiny_regression: bool) -> None:
    """手工 RankNet MLP：理想顺序 = inv_pos；`tiny_regression=True` 时输出 inv_pos + 0.2·lex_norm。

    后者的两臂在绝大多数查询上排序完全相同，只在 `swap_source` 的那一条上差一名——
    这是把 OOD margin 做成**极小负值**的手段（见下一个测试的边界断言）。
    """
    import torch

    import train_ltr

    model_dir.mkdir(parents=True, exist_ok=True)
    net = train_ltr._build_mlp(len(FEATURE_NAMES))
    with torch.no_grad():
        for parameter in net.parameters():
            parameter.zero_()
        net[0].weight[0, FEATURE_NAMES.index("inv_pos")] = 1.0   # h0[0] = inv_pos
        net[2].weight[0, 0] = 1.0
        net[4].weight[0, 0] = 1.0
        if tiny_regression:
            net[0].weight[1, FEATURE_NAMES.index("lex_norm")] = 0.2   # h0[1] = 0.2·lex_norm
            net[2].weight[1, 1] = 1.0
            net[4].weight[0, 1] = 1.0
    torch.save({k: v.detach().clone() for k, v in net.state_dict().items()}, model_dir / "ltr_mlp.pt")
    _write_json(model_dir / ltr_promote.SPEC_NAME, {
        "feature_order": list(FEATURE_NAMES),
        "label_mode": "mix",
        "seed": train_ltr.DEFAULT_SEED,
        "feature_mean": [0.0] * len(FEATURE_NAMES),
        "feature_std": [1.0] * len(FEATURE_NAMES),
        "trained_at": CANDIDATE_TRAINED_AT,
        "train_counts": {"queries": 1, "pairs": 1},
        "heldout_metrics": None,
        "model": {"type": "ranknet_mlp", "input_dim": len(FEATURE_NAMES), "hidden_layers": [32, 16],
                  "activation": "relu", "output_dim": 1, "loss": "pairwise_logistic"},
    })


def test_gate_ood_rejects_a_margin_that_rounds_to_negative_zero(tmp_path: Path) -> None:
    """回归（2026-09-29 定位）：门 3 曾拿**舍入到 4 位**的展示值做判据，−4e−5 变成 −0.0
    后与 `>= -0.0` 一比就放行——边界上的假 ACCEPT。判据改用未舍入 margin 后必须拦住。

    构造：1,300 条查询里只有一条排序退化（1 名之差，nDCG@10 掉 0.0558），
    于是 macro 差值 = −0.0558/1300 ≈ −4.3e−5——落在 (−5e−5, 0) 这个舍入陷阱里。
    """
    queries = 1300
    ood_path = tmp_path / "ood.jsonl"
    ood_path.write_text(
        "".join(json.dumps(_ood_row(f"ood-{i:04d}", swap_source=(i == 0)), ensure_ascii=False) + "\n"
                for i in range(queries)), encoding="utf-8")
    incumbent, candidate = tmp_path / "incumbent", tmp_path / "candidate"
    _make_ood_artifact(incumbent, tiny_regression=False)
    _make_ood_artifact(candidate, tiny_regression=True)

    raw = ltr_promote._ood_duel().compare_frozen(
        candidate_dir=candidate, incumbent_dir=incumbent, ood_path=ood_path)
    margin = float(raw["verdict"]["margin_raw"])
    assert raw["verdict"]["margin"] == -0.0, "前提：展示值必须被舍成 −0.0（否则这条测试不再覆盖边界）"
    assert float(raw["verdict"]["margin"]) >= -ltr_promote.OOD_MARGIN_TOL, \
        "前提：旧判据（舍入值 >= −0.0）本该放行——这正是要修的假 ACCEPT"
    assert -5e-5 < margin < 0.0, f"前提：未舍入 margin 必须落在舍入陷阱里，实测 {margin!r}"

    gate = ltr_promote.gate_ood(candidate, incumbent, ood_path)
    assert gate["passed"] is False, "未舍入 margin < 0 必须判 FAIL（fail-closed 让人来看配对胜负）"
    assert gate["tolerance"] == ltr_promote.OOD_MARGIN_TOL == 0.0
    assert gate["verdict"]["margin_raw"] < 0


def test_candidate_f3_is_the_default_recipe(project: Path, capsys):
    """2026-09-30 起缺省配方 = f3（生产存活池回放+共享构造函数；卫生决策，用户拍板「机制能跑就上」）：

    缺省 dry-run 的命令必须指到构建器的预合并变体、带 f3 旗标、无 mix-in（变体已含波次层）；
    数据缺失时 dry-run 提示会先构建而不真跑构建器；显式 `--feature-set f0` 回历史配方。
    """
    code, out = _run(capsys, "--project-root", project, "candidate", "--dry-run", "--ts", "TF3")
    assert code == 0
    assert "--feature-set f3" in out and "--eval-feature-set f3" in out, out
    assert str(ltr_promote.Paths(project).f3_train_dir) in out
    assert "--mix-in" not in out, "f3 变体已预合并，默认 mix-in 不得重复进"
    assert "build_f3_features.py" in out, "f3 数据缺失时 dry-run 必须预告构建步骤"

    code, out = _run(capsys, "--project-root", project, "candidate", "--dry-run", "--ts", "TF0",
                     "--feature-set", "f0")
    assert code == 0 and "--feature-set f0" in out
    assert str(project / "research" / "ltr_data") in out, "显式 f0 走历史数据目录"


def test_gate_follows_candidate_feature_set(project: Path, capsys, monkeypatch, tmp_path: Path):
    """门从候选 meta 继承 feature_set：heldout 重算（候选与现役两侧）与 OOD 对照都按候选配方。"""
    make_incumbent(project, good=True)
    candidate = make_candidate(project, good=True, version="v-f3")
    meta_path = candidate / ltr_promote.CANDIDATE_META
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["feature_set"] = "f3"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    captured: dict = {}

    def fake_heldout(model_dir, in_dir, *, mode, repo_root, feature_set="f0"):
        captured.setdefault("heldout_feature_sets", []).append(feature_set)
        captured["heldout_in_dir"] = str(in_dir)
        return {"metrics": {"nDCG@5": 0.9, "nDCG@10": 0.9, "MRR": 0.9},
                "declared": {"nDCG@5": 0.9, "nDCG@10": 0.9, "MRR": 0.9},
                "spec": json.loads((model_dir / ltr_promote.SPEC_NAME).read_text(encoding="utf-8")),
                "heldout_queries": 6, "ranked_queries": 6, "layers": {}}

    def fake_ood(candidate_dir, incumbent_dir, ood_path, *, feature_set="f0"):
        captured["ood"] = {"ood_path": str(ood_path), "feature_set": feature_set}
        return {"gate": "ood_duel", "passed": True, "tolerance": 0.0}

    monkeypatch.setattr(ltr_promote, "heldout_metrics_of", fake_heldout)
    monkeypatch.setattr(ltr_promote, "gate_ood", fake_ood)
    code, out = _run(capsys, "--project-root", project, "gate", str(candidate),
                     "--frozen-report", str(_frozen_pass_report(tmp_path)))
    assert code == 0, out
    assert captured["heldout_feature_sets"] == ["f3", "f3"], "候选与现役都按候选配方评测"
    assert captured["heldout_in_dir"] == str(Path(meta["in_dir"]).resolve()), \
        "heldout 评测面以候选 meta 的 in_dir 为准"
    assert captured["ood"] == {"ood_path": str(ltr_promote.Paths(project).f3_ood_path),
                               "feature_set": "f3"}, "f3 候选的门 3 用 f3 形态只评测集"


def test_compare_frozen_plumbs_feature_set(tmp_path: Path, monkeypatch):
    """门 3 的冻结对照把 feature_set 传进 candidate_features；f3 行缺叠加层必须抛错。"""
    ood_duel = ltr_promote._ood_duel()
    assert Path(ood_duel.__file__).resolve() == ROOT / "scripts" / "ood_duel.py", \
        "OOD 门必须使用公开 scripts/ood_duel.py 的唯一实现"
    seen = []
    real = ood_duel.candidate_features

    def spy(row, feature_set="f0"):
        seen.append(feature_set)
        return real(row, feature_set)

    monkeypatch.setattr(ood_duel, "candidate_features", spy)
    incumbent, candidate = tmp_path / "incumbent", tmp_path / "candidate"
    _make_ood_artifact(incumbent, tiny_regression=False)
    _make_ood_artifact(candidate, tiny_regression=False)
    ood_path = tmp_path / "ood.jsonl"
    ood_path.write_text(json.dumps(_ood_row("ood-0"), ensure_ascii=False) + "\n",
                        encoding="utf-8")
    # 夹具行没有 f3_feats：f3 评测会按契约抛错——抛错前先经过 spy，即证明 plumbing 到达
    with pytest.raises(ValueError, match="f3_feats"):
        ood_duel.compare_frozen(candidate_dir=candidate, incumbent_dir=incumbent,
                                ood_path=ood_path, feature_set="f3")
    assert seen and set(seen) == {"f3"}, "compare_frozen 必须把 feature_set 传到特征构造"

    def _scorer(feats):
        return [float(sum(r)) for r in feats]

    with pytest.raises(ValueError, match="f3_feats"):
        ood_duel.ltr_order(_ood_row("ood-y"), _scorer, "f3")
    # f0 路径不抛（历史行为不变）
    assert ood_duel.ltr_order(_ood_row("ood-y"), _scorer, "f0") is not None


def test_mix_in_patterns_hit_the_trainable_layer_only(project: Path) -> None:
    """门 3 的评测集绝不进训练 mix-in：可训练层命中，原全量与只评测层都不命中。"""
    ood_dir = project / "research" / "ood_eval"
    _write_jsonl(ood_dir / "synth_ood_label_input_xscore.jsonl",
                 [_row("legacy-full-1", ood_class="underspecified")])
    _write_jsonl(project / "research" / "ltr_data" / "synth_train_wave2_20260929_xscore.jsonl",
                 [_row("wave-2-1")])
    _write_jsonl(project / "research" / "ltr_data" / "synth_train_xscore.jsonl", [_row("exp-1")])
    hits = {p.relative_to(project).as_posix() for p in ltr_promote.Paths(project).mix_in_paths()}
    assert hits == {TRAINABLE_REL,
                    "research/ltr_data/synth_train_wave2_20260929_xscore.jsonl"}, \
        "缺省 mix-in = 可训练层 + 波次层，一个不多一个不少"

    for name in ("synth_ood_evalonly_xscore.jsonl",    # 只评测层：门 3 的评测集
                 "synth_ood_label_input_xscore.jsonl",  # 原全量：留档凭据，已不再被引用
                 "synth_train_xscore.jsonl"):           # 一次性实验导出（非波次层）
        assert f"research/ood_eval/{name}" not in hits and \
            f"research/ltr_data/{name}" not in hits, f"{name} 不得进训练 mix-in"


#: 改造前缺省训练基准的实测锚点（2026-09-29 实测：全量 OOD 交叉打分集 + wave2）：
#: 1,810 查询 / 49,999 对。它是"波次层进攒量口径"的对照点，下面的派生断言都从它出发。
LEGACY_BASIS_QUERIES = 1810
LEGACY_BASIS_PAIRS = 49999
#: 新缺省基准的实测对数（= 上面那份扣掉只评测层；**基准对变了就更新这里**，
#: 并回头复核门 2/门 3 的历史裁决是否还成立）。
DEFAULT_BASIS_PAIRS = 44965


def test_basis_counts_wave_layers_and_excludes_the_evalonly_layer() -> None:
    """攒量口径的两条硬约束，用真数据钉住：

    1. 波次层（`research/ltr_data/synth_train_wave*_xscore.jsonl`）必须计入基准——
       它曾不在口径里，于是 1810 查询的训练被记成 1623 查询，攒量阈值跟着算错；
    2. 门 3 的只评测层不得计入——它只服务评测。
    """
    in_dir = ROOT / "research" / "ltr_data"
    source = ROOT / "research" / "ood_eval" / "synth_ood_label_input_xscore.jsonl"
    evalonly = ROOT / "research" / "ood_eval" / "synth_ood_evalonly_xscore.jsonl"
    wave2 = in_dir / "synth_train_wave2_20260929_xscore.jsonl"
    if not (source.is_file() and evalonly.is_file() and wave2.is_file()):
        pytest.skip("研究数据层缺失（公开镜像不带 research/）——本条只在本仓跑")

    paths = ltr_promote.Paths(ROOT)
    legacy = ltr_promote.basis_report(paths, in_dir, seed=DEFAULT_SEED, label="mix",
                                      mix_ins=[source, wave2])
    assert (legacy["train_queries"], legacy["train_pairs"]) == (LEGACY_BASIS_QUERIES, LEGACY_BASIS_PAIRS), \
        "改造前的基准（全量 OOD + wave2）必须复现实测的 1810/49999"

    evalonly_queries = {json.loads(line)["query_id"]
                        for line in evalonly.read_text(encoding="utf-8").splitlines() if line.strip()}
    default = ltr_promote.basis_report(paths, in_dir, seed=DEFAULT_SEED, label="mix")
    assert default["train_queries"] == LEGACY_BASIS_QUERIES - len(evalonly_queries), \
        "缺省基准 = 改造前那份扣掉只评测层（评测集不参与训练）"
    assert default["train_pairs"] == DEFAULT_BASIS_PAIRS
    assert any("wave" in name for name in default["mix_in"]), \
        "波次层必须出现在攒量口径的 mix-in 记账里（否则又回到那份漏账）"
    assert not any("evalonly" in name for name in default["mix_in"])


# ---------------------------------------------------------------- reject 路径
def test_gate_hold_on_worse_candidate_leaves_incumbent_byte_identical(project: Path, capsys, tmp_path):
    """劣候选 → 门拦 → 现役逐位未动 → promote 拒绝（不接受口头结论）。"""
    make_incumbent(project, good=True)
    live = project / "models" / "ltr"
    before = _shas(live)
    candidate = make_candidate(project, good=False, version="bad-candidate")

    code, out = _run(capsys, "--project-root", project, "gate", candidate,
                     "--frozen-report", _frozen_pass_report(tmp_path))
    assert code == 1 and "HOLD" in out
    ledger = json.loads((candidate / ltr_promote.GATE_LEDGER).read_text(encoding="utf-8"))
    assert ledger["verdict"] == "HOLD"
    assert ledger["gates"]["heldout_no_regression"]["passed"] is False
    assert ledger["gates"]["heldout_no_regression"]["delta_nDCG@10"] < 0
    assert ledger["gates"]["ood_duel"]["passed"] is False
    assert ledger["gates"]["ood_duel"]["verdict"]["margin"] < 0

    assert _shas(live) == before, "门禁失败不得动现役一个字节"
    code, out = _run(capsys, "--project-root", project, "promote", candidate)
    assert code == 2 and "门禁未全过" in out

    journal = (project / ".userdata" / ltr_promote.JOURNAL_NAME).read_text(encoding="utf-8")
    assert '"action": "reject"' in journal, "拒绝事件必须进流水账（git 史只记转正）"
    assert _shas(live) == before


def test_promote_requires_gate_ledger(project: Path, capsys):
    """没有门禁账的候选目录一律拒收（fail-closed，不许口头结论）。"""
    make_incumbent(project, good=True)
    candidate = make_candidate(project, good=True, version="ungated")
    code, out = _run(capsys, "--project-root", project, "promote", candidate)
    assert code == 2 and "缺门禁账" in out


# ---------------------------------------------------------------- accept + rollback 路径
def test_accept_promote_then_rollback_restores_bytes(project: Path, capsys, tmp_path):
    """好候选 → 四门全过 → 转正（字节核验 + 归档 + REGISTRY）→ 回滚（字节恢复）。"""
    spec = make_incumbent(project, good=False)          # 现役是"反序"的差制品
    incumbent_version = ltr_promote.version_of_spec(spec)
    live = project / "models" / "ltr"
    before = _shas(live)
    candidate = make_candidate(project, good=True, version="good-candidate")
    candidate_shas = _shas(candidate)

    code, out = _run(capsys, "--project-root", project, "gate", candidate,
                     "--frozen-report", _frozen_pass_report(tmp_path))
    assert code == 0 and "ACCEPT" in out, out
    ledger = json.loads((candidate / ltr_promote.GATE_LEDGER).read_text(encoding="utf-8"))
    assert all(item["passed"] for item in ledger["gates"].values())
    assert ledger["gates"]["heldout_no_regression"]["candidate"]["nDCG@10"] == 1.0

    code, out = _run(capsys, "--project-root", project, "promote", candidate)
    assert code == 0 and "转正完成" in out
    # 就位字节 == 候选字节（逐位）
    installed = _shas(live)
    assert installed == candidate_shas
    # 前代归档保留原字节
    archived = live / "_archive" / incumbent_version
    assert _shas(archived) == before, "归档必须逐位等于被替代的现役"
    assert json.loads((archived / "_archive.json").read_text(encoding="utf-8"))["successor"] == "good-candidate"

    registry = json.loads((live / ltr_promote.REGISTRY_NAME).read_text(encoding="utf-8"))
    assert registry["active"] == "good-candidate"
    entries = {e["version"]: e for e in registry["versions"]}
    assert entries[incumbent_version]["status"] == "archived"
    assert entries["good-candidate"]["status"] == "active"
    assert entries["good-candidate"]["data_fingerprint"] == \
        ltr_promote.data_fingerprint([project / "research" / "ltr_data" / "synth_train.jsonl"], project)
    assert all(entries["good-candidate"]["gates"].values())
    assert entries["good-candidate"]["artifact_sha256"] == installed

    code, out = _run(capsys, "--project-root", project, "history")
    assert "REGISTRY" in out and "good-candidate" in out and "promote" in out

    # ---- 回滚：字节必须回到前代 ----
    code, out = _run(capsys, "--project-root", project, "rollback", incumbent_version)
    assert code == 0 and "回滚完成" in out
    assert _shas(live) == before, "回滚必须把现役字节还原成前代（逐位）"
    registry = json.loads((live / ltr_promote.REGISTRY_NAME).read_text(encoding="utf-8"))
    assert registry["active"] == incumbent_version
    entries = {e["version"]: e for e in registry["versions"]}
    assert entries[incumbent_version]["status"] == "active"
    assert entries["good-candidate"]["status"] == "rolled_back"
    assert entries["good-candidate"]["rolled_back_to"] == incumbent_version
    # 被替换下来的好候选也进了归档（回滚不是单向门）
    assert _shas(live / "_archive" / "good-candidate") == candidate_shas
    journal = (project / ".userdata" / ltr_promote.JOURNAL_NAME).read_text(encoding="utf-8")
    for action in ('"action": "gate"', '"action": "promote"', '"action": "rollback"'):
        assert action in journal, f"流水账缺事件：{action}"
    assert journal.count('"action": "gate"') == 1, "一次裁决只落一行（不是过/不过各写一套）"
    # 拒绝事件另测（test_gate_hold_…）；candidate 事件的落账见
    # test_candidate_subcommand_stages_artifact_meta_and_journal。


def test_promote_refuses_same_version(project: Path, capsys, tmp_path):
    """同版本（同 trained_at + 同数据指纹）拒绝转正——不给"重训一遍再转一次"留空转。"""
    make_incumbent(project, good=True)
    candidate = make_candidate(project, good=True, version="same-as-incumbent")
    candidate_shas = _shas(candidate)
    live = project / "models" / "ltr"
    # 让现役与候选字节相同（同内容 → 同版本语义），并把候选版本号设成现役版本号
    for name in ltr_promote.ARTIFACT_NAMES:
        (live / name).write_bytes((candidate / name).read_bytes())
    assert _shas(live) == candidate_shas
    meta = json.loads((candidate / ltr_promote.CANDIDATE_META).read_text(encoding="utf-8"))
    meta["version"] = ltr_promote.version_of_spec(
        json.loads((live / ltr_promote.SPEC_NAME).read_text(encoding="utf-8")))
    _write_json(candidate / ltr_promote.CANDIDATE_META, meta)

    code, out = _run(capsys, "--project-root", project, "gate", candidate,
                     "--frozen-report", _frozen_pass_report(tmp_path))
    assert code == 0, out
    code, out = _run(capsys, "--project-root", project, "promote", candidate)
    assert code == 2 and "同版本" in out


def test_rollback_unknown_version_reports_available(project: Path, capsys):
    make_incumbent(project, good=True)
    code, out = _run(capsys, "--project-root", project, "rollback", "no-such-version")
    assert code == 2 and "归档中无该版本" in out


@pytest.mark.parametrize("failure", ["tamper", "second_artifact", "registry", "journal"])
def test_failed_promotion_preserves_incumbent(project, capsys, tmp_path, monkeypatch, failure):
    make_incumbent(project, good=False)
    candidate = make_candidate(project, good=True, version="verified-candidate")
    code, out = _run(capsys, "--project-root", project, "gate", candidate,
                     "--frozen-report", _frozen_pass_report(tmp_path))
    assert code == 0, out
    paths = ltr_promote.Paths(project)
    targets = [paths.ltr_dir / name for name in ltr_promote.ARTIFACT_NAMES]
    targets += [paths.registry_path, paths.journal_path]
    before = {p: p.read_bytes() if p.exists() else None for p in targets}
    if failure == "tamper":
        (candidate / ltr_promote.WEIGHTS_NAME).write_bytes(b"changed")
    else:
        real_write, real_journal = ltr_promote.atomic_write_bytes, ltr_promote.append_journal
        failed = False

        def write(path, payload):
            nonlocal failed
            victim = (paths.ltr_dir / ltr_promote.SPEC_NAME if failure == "second_artifact"
                      else paths.registry_path)
            if failure != "journal" and path == victim and not failed:
                failed = True
                raise OSError("injected write failure")
            return real_write(path, payload)

        def journal(path, entry):
            real_journal(path, entry)
            raise OSError("injected append failure")

        monkeypatch.setattr(ltr_promote, "atomic_write_bytes", write)
        if failure == "journal":
            monkeypatch.setattr(ltr_promote, "append_journal", journal)
    code, out = _run(capsys, "--project-root", project, "promote", candidate)
    assert code == 2, out
    assert {p: p.read_bytes() if p.exists() else None for p in targets} == before


@pytest.mark.parametrize("mutation", ["empty", "missing", "truthy", "version", "candidate", "incumbent"])
def test_promote_requires_complete_gate_bound_to_artifacts(project, capsys, tmp_path, mutation):
    make_incumbent(project, good=False)
    candidate = make_candidate(project, good=True, version="bound-candidate")
    code, out = _run(capsys, "--project-root", project, "gate", candidate,
                     "--frozen-report", _frozen_pass_report(tmp_path))
    assert code == 0, out
    ledger_path = candidate / ltr_promote.GATE_LEDGER
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    gate = next(iter(ledger["gates"]))
    if mutation == "empty":
        ledger["gates"] = {}
    elif mutation == "missing":
        del ledger["gates"][gate]
    elif mutation == "truthy":
        ledger["gates"][gate]["passed"] = "true"
    elif mutation == "version":
        ledger["version"] = "another-candidate"
    elif mutation == "candidate":
        ledger["artifact_sha256"] = {"wrong": "digest"}
    else:
        (project / "models/ltr" / ltr_promote.WEIGHTS_NAME).write_bytes(b"new incumbent")
    _write_json(ledger_path, ledger)
    before = _shas(project / "models/ltr")
    code, out = _run(capsys, "--project-root", project, "promote", candidate)
    assert code == 2, out
    assert _shas(project / "models/ltr") == before


def _promoted_archive(project, tmp_path, capsys):
    spec = make_incumbent(project, good=False)
    version = ltr_promote.version_of_spec(spec)
    candidate = make_candidate(project, good=True, version="active-candidate")
    code, out = _run(capsys, "--project-root", project, "gate", candidate,
                     "--frozen-report", _frozen_pass_report(tmp_path))
    assert code == 0, out
    code, out = _run(capsys, "--project-root", project, "promote", candidate)
    assert code == 0, out
    paths = ltr_promote.Paths(project)
    return paths, version, paths.archive_dir / version


def _active_transaction_bytes(paths):
    targets = [paths.ltr_dir / name for name in ltr_promote.ARTIFACT_NAMES]
    targets += [paths.registry_path, paths.journal_path]
    return {p: p.read_bytes() if p.exists() else None for p in targets}


@pytest.mark.parametrize("damage", ["weights", "spec"])
def test_rollback_rejects_damaged_promoted_archive_without_recertifying(project, capsys, tmp_path, damage):
    paths, version, archive = _promoted_archive(project, tmp_path, capsys)
    if damage == "weights":
        (archive / ltr_promote.WEIGHTS_NAME).write_bytes(b"damaged archived bytes")
    else:
        spec_path = archive / ltr_promote.SPEC_NAME
        spec = ltr_promote.read_json(spec_path)
        spec["seed"] += 1
        _write_json(spec_path, spec)
    before = _active_transaction_bytes(paths)
    code, out = _run(capsys, "--project-root", project, "rollback", version)
    assert code == 2, out
    assert _active_transaction_bytes(paths) == before
    assert not (paths.archive_dir / "active-candidate").exists()
    assert "回滚完成" not in out


@pytest.mark.parametrize("legacy", ["registry-missing", "registry-null", "registry-entry-missing",
                                     "archive-missing", "archive-null", "archive-file-missing"])
def test_rollback_uses_remaining_complete_historical_evidence(project, capsys, tmp_path, legacy):
    paths, version, archive = _promoted_archive(project, tmp_path, capsys)
    expected = _shas(archive)
    registry = ltr_promote.load_registry(paths.registry_path)
    entry = ltr_promote.registry_entry(registry, version)
    archive_path = archive / "_archive.json"
    metadata = ltr_promote.read_json(archive_path)
    if legacy == "registry-missing":
        entry.pop("artifact_sha256")
    elif legacy == "registry-null":
        entry["artifact_sha256"] = None
    elif legacy == "registry-entry-missing":
        registry["versions"] = [e for e in registry["versions"] if e["version"] != version]
    elif legacy == "archive-missing":
        metadata.pop("artifact_sha256")
    elif legacy == "archive-null":
        metadata["artifact_sha256"] = None
    else:
        archive_path.unlink()
    ltr_promote.save_registry(paths.registry_path, registry)
    if legacy != "archive-file-missing":
        _write_json(archive_path, metadata)
    code, out = _run(capsys, "--project-root", project, "rollback", version)
    assert code == 0, out
    assert _shas(paths.ltr_dir) == expected
    restored = ltr_promote.registry_entry(ltr_promote.load_registry(paths.registry_path), version)
    assert restored["artifact_sha256"] == expected
    assert restored["data_fingerprint"] is None


@pytest.mark.parametrize("damage", ["both-missing", "both-null", "conflict", "registry-partial",
                                     "archive-partial", "registry-bad-value", "archive-bad-value",
                                     "registry-bad-json", "archive-bad-json", "archive-not-object"])
def test_rollback_refuses_incomplete_or_conflicting_historical_evidence(project, capsys, tmp_path, damage):
    paths, version, archive = _promoted_archive(project, tmp_path, capsys)
    registry = ltr_promote.load_registry(paths.registry_path)
    entry = ltr_promote.registry_entry(registry, version)
    archive_path = archive / "_archive.json"
    metadata = ltr_promote.read_json(archive_path)
    if damage == "both-missing":
        entry.pop("artifact_sha256")
        metadata.pop("artifact_sha256")
    elif damage == "both-null":
        entry["artifact_sha256"] = metadata["artifact_sha256"] = None
    elif damage == "conflict":
        metadata["artifact_sha256"][ltr_promote.WEIGHTS_NAME] = "0" * 64
    elif damage == "registry-partial":
        entry["artifact_sha256"].pop(ltr_promote.SPEC_NAME)
    elif damage == "archive-partial":
        metadata["artifact_sha256"].pop(ltr_promote.WEIGHTS_NAME)
    elif damage == "registry-bad-value":
        entry["artifact_sha256"][ltr_promote.WEIGHTS_NAME] = "not-a-sha256"
    elif damage == "archive-bad-value":
        metadata["artifact_sha256"] = []
    ltr_promote.save_registry(paths.registry_path, registry)
    _write_json(archive_path, metadata)
    if damage == "registry-bad-json":
        paths.registry_path.write_bytes(b"{broken registry")
    elif damage == "archive-bad-json":
        archive_path.write_bytes(b"{broken archive")
    elif damage == "archive-not-object":
        _write_json(archive_path, [])
    before = _active_transaction_bytes(paths)
    code, out = _run(capsys, "--project-root", project, "rollback", version)
    assert code == 2, out
    assert _active_transaction_bytes(paths) == before
    assert not (paths.archive_dir / "active-candidate").exists()
    assert "回滚完成" not in out


def test_rollback_revalidates_same_historical_hashes_at_install(project, capsys, tmp_path, monkeypatch):
    paths, version, archive = _promoted_archive(project, tmp_path, capsys)
    before = _active_transaction_bytes(paths)
    real_install = ltr_promote._install_artifact
    installed_expected = []
    historical = _shas(archive)

    def drift(src, dst, *, expected=None):
        if dst == paths.ltr_dir:
            installed_expected.append(expected)
        result = real_install(src, dst, expected=expected)
        if dst == paths.archive_dir / "active-candidate":
            (archive / ltr_promote.WEIGHTS_NAME).write_bytes(b"changed after preflight")
        return result

    monkeypatch.setattr(ltr_promote, "_install_artifact", drift)
    code, out = _run(capsys, "--project-root", project, "rollback", version)
    assert code == 2, out
    assert installed_expected == [historical]
    assert _active_transaction_bytes(paths) == before
    assert not list((paths.archive_dir / "active-candidate").glob("*"))


@pytest.mark.parametrize("restore_failures", [("journal",), ("journal", "registry"),
                                            ("journal", "weights"), ("journal", "registry", "weights", "spec")])
def test_authenticated_rollback_persistent_restore_faults_are_aggregated(
        project, capsys, tmp_path, monkeypatch, restore_failures):
    from types import SimpleNamespace

    paths, version, _archive = _promoted_archive(project, tmp_path, capsys)
    named = {"journal": paths.journal_path, "registry": paths.registry_path,
             "weights": paths.ltr_dir / ltr_promote.WEIGHTS_NAME,
             "spec": paths.ltr_dir / ltr_promote.SPEC_NAME}
    before = {p: p.read_bytes() for p in named.values()}
    untouched = paths.ltr_dir / ".unowned.recovery.tmp"
    untouched.write_bytes(b"unrelated scratch")
    real_write, real_journal = ltr_promote.atomic_write_bytes, ltr_promote.append_journal
    primary = OSError("synthetic primary journal failure")
    triggered, attempts = [], []

    def fail_journal(*args, **kwargs):
        real_journal(*args, **kwargs)
        triggered.append(True)
        raise primary

    def fault(path, raw):
        path = Path(path)
        if triggered:
            attempts.append(path)
            if path in {named[name] for name in restore_failures}:
                raise OSError("persistent rollback fault: " + path.name)
        return real_write(path, raw)

    monkeypatch.setattr(ltr_promote, "append_journal", fail_journal)
    monkeypatch.setattr(ltr_promote, "atomic_write_bytes", fault)
    with pytest.raises(ltr_promote.PromoteError) as caught:
        ltr_promote.cmd_rollback(SimpleNamespace(project_root=project, version=version))
    for name, path in named.items():
        assert path in attempts
        if name not in restore_failures:
            assert path.read_bytes() == before[path]
    assert "恢复不完整" in str(caught.value) and "已恢复" not in str(caught.value)
    causes, exc = [], caught.value
    while exc is not None:
        causes.append(exc)
        exc = exc.__cause__
    assert primary in causes
    for name in restore_failures:
        assert named[name].name in str(caught.value)
    assert untouched.read_bytes() == b"unrelated scratch"
    assert not list((paths.archive_dir / "active-candidate").glob("*"))


def test_failed_promotion_preserves_concurrent_gate_journal_once(project, capsys, tmp_path, monkeypatch):
    import threading
    from contextlib import contextmanager
    from dataset_recommender.corpus import fs_utils

    make_incumbent(project, good=False)
    first = make_candidate(project, good=True, version="first-candidate")
    second = make_candidate(project, good=True, version="second-candidate")
    report = _frozen_pass_report(tmp_path)
    code, out = _run(capsys, "--project-root", project, "gate", first, "--frozen-report", report)
    assert code == 0, out
    paths = ltr_promote.Paths(project)
    before = _active_transaction_bytes(paths)
    ready, done = threading.Event(), threading.Event()
    errors, results, readiness = [], [], []
    real_lock = fs_utils.file_critical_section
    real_append = ltr_promote.append_journal
    real_install = ltr_promote._install_artifact
    real_save = ltr_promote.save_registry

    @contextmanager
    def observe_request(path, **kwargs):
        if threading.current_thread().name == "concurrent-gate" and Path(path).name == JOURNAL_LOCK_NAME:
            readiness.append("journal-lock-request")
            ready.set()
        with real_lock(path, **kwargs):
            yield

    def observe_append(path, entry):
        result = real_append(path, entry)
        if threading.current_thread().name == "concurrent-gate":
            if not ready.is_set():
                readiness.append("unlocked-append-finished")
            ready.set()
        return result

    def gate_second():
        try:
            results.append(ltr_promote.main(["--project-root", str(project), "gate", str(second),
                                             "--frozen-report", str(report)]))
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=gate_second, name="concurrent-gate")
    started, failed = [], []

    def install(src, dst, *, expected=None):
        if dst == paths.ltr_dir and not started:
            started.append(True)
            thread.start()
            assert ready.wait(20)
        return real_install(src, dst, expected=expected)

    def save_once(path, registry):
        real_save(path, registry)
        if not failed:
            failed.append(True)
            raise OSError("synthetic first promotion registry fault")

    monkeypatch.setattr(fs_utils, "file_critical_section", observe_request)
    monkeypatch.setattr(ltr_promote, "append_journal", observe_append)
    monkeypatch.setattr(ltr_promote, "_install_artifact", install)
    monkeypatch.setattr(ltr_promote, "save_registry", save_once)
    try:
        code = ltr_promote.main(["--project-root", str(project), "promote", str(first)])
    finally:
        if started:
            thread.join(20)
    assert started and not thread.is_alive() and done.is_set() and not errors
    assert code == 2 and results == [0]
    ledger = ltr_promote.read_json(second / ltr_promote.GATE_LEDGER)
    assert ledger["verdict"] == "ACCEPT" and all(g["passed"] for g in ledger["gates"].values())
    journal = [json.loads(line) for line in paths.journal_path.read_text(encoding="utf-8").splitlines()]
    assert sum(e["action"] == "gate" and e["version"] == "second-candidate" for e in journal) == 1
    assert not any(e["action"] == "promote" and e["version"] == "first-candidate" for e in journal)
    assert paths.journal_path.read_bytes().startswith(before[paths.journal_path])
    assert {p: raw for p, raw in _active_transaction_bytes(paths).items() if p != paths.journal_path} == {
        p: raw for p, raw in before.items() if p != paths.journal_path}
    assert readiness == ["journal-lock-request"]


JOURNAL_LOCK_NAME = ltr_promote.JOURNAL_NAME + ".lock"


@pytest.mark.parametrize("command", ["promote", "rollback"])
def test_artifact_transaction_capture_append_and_restoration_hold_journal_os_lock(
        project, capsys, tmp_path, monkeypatch, command):
    from contextlib import contextmanager
    from dataset_recommender.corpus import fs_utils

    paths, version, _archive = _promoted_archive(project, tmp_path, capsys)
    if command == "promote":
        candidate = make_candidate(project, good=True, version="next-candidate")
        code, out = _run(capsys, "--project-root", project, "gate", candidate,
                         "--frozen-report", _frozen_pass_report(tmp_path))
        assert code == 0, out
        argument = candidate
    else:
        argument = version
    before = _active_transaction_bytes(paths)
    journal_lock = paths.journal_path.with_name(JOURNAL_LOCK_NAME)
    real_transaction = ltr_promote._files_transaction
    real_restore = ltr_promote._restore_files
    real_append = ltr_promote.append_journal
    phases, held = [], []

    def observe_lock(phase):
        phases.append(phase)
        try:
            handle = fs_utils.acquire_file_lock(journal_lock, wait=False)
        except fs_utils.FileLockBusy:
            held.append(True)
        else:
            fs_utils.release_file_lock(handle)
            held.append(False)

    @contextmanager
    def transaction(discover):
        if paths.journal_path in discover():
            observe_lock("capture")
        with real_transaction(discover):
            yield

    def restore(images):
        if paths.journal_path in images:
            observe_lock("restore")
        return real_restore(images)

    def fail_append(path, entry):
        real_append(path, entry)
        observe_lock("append")
        raise OSError("synthetic post-append fault")

    monkeypatch.setattr(ltr_promote, "_files_transaction", transaction)
    monkeypatch.setattr(ltr_promote, "_restore_files", restore)
    monkeypatch.setattr(ltr_promote, "append_journal", fail_append)
    code, out = _run(capsys, "--project-root", project, command, argument)
    assert code == 2, out
    assert phases == ["capture", "append", "restore"] and held == [True, True, True]
    assert _active_transaction_bytes(paths) == before


def test_gate_evaluation_leaves_journal_os_lock_free(project, capsys, tmp_path, monkeypatch):
    from dataset_recommender.corpus import fs_utils

    make_incumbent(project, good=False)
    candidate = make_candidate(project, good=True, version="short-journal-lock")
    paths = ltr_promote.Paths(project)
    real_metrics = ltr_promote.heldout_metrics_of
    evaluated = []

    def metrics(*args, **kwargs):
        handle = fs_utils.acquire_file_lock(paths.journal_path.with_name(JOURNAL_LOCK_NAME), wait=False)
        fs_utils.release_file_lock(handle)
        evaluated.append(True)
        return real_metrics(*args, **kwargs)

    monkeypatch.setattr(ltr_promote, "heldout_metrics_of", metrics)
    code, out = _run(capsys, "--project-root", project, "gate", candidate,
                     "--frozen-report", _frozen_pass_report(tmp_path))
    assert code == 0 and evaluated == [True, True], out


def test_append_journal_reuses_real_os_sidecar_lock(tmp_path, monkeypatch):
    import threading
    from dataset_recommender.corpus import fs_utils

    path = ltr_promote.Paths(tmp_path).journal_path
    lock_path = path.with_name(JOURNAL_LOCK_NAME)
    handle = fs_utils.acquire_file_lock(lock_path)
    real_acquire = fs_utils.acquire_file_lock
    requested, done = threading.Event(), threading.Event()
    observations, errors = [], []

    def observe(path, **kwargs):
        if Path(path) == lock_path:
            observations.append(done.is_set())
            requested.set()
        return real_acquire(path, **kwargs)

    def append():
        try:
            ltr_promote.append_journal(path, {"action": "gate", "version": "isolated-event"})
        except BaseException as exc:
            errors.append(exc)
        finally:
            done.set()

    monkeypatch.setattr(fs_utils, "acquire_file_lock", observe)
    thread = threading.Thread(target=append)
    thread.start()
    try:
        assert requested.wait(10)
        assert not path.exists() and observations == [False]
    finally:
        fs_utils.release_file_lock(handle)
        thread.join(10)
    assert not thread.is_alive() and not errors
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == "isolated-event"
