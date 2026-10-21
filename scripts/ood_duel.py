# -*- coding: utf-8 -*-
"""离线 OOD 排序对比：混入 OOD 数据重训的 LTR vs 纯向量序 vs 现役 LTR vs 机械门控融合。

用途：在分布外（OOD）查询上比较若干确定性/训练排序臂。默认模式按簇做 5 折、把 OOD 行
混入训练集重训 LTR，回答“混 OOD 数据训练能否在零泄漏的 held-out 折上胜过不交 OOD 标签
的对手”。`--compare-frozen DIR` 为冻结对照：两臂都是不再训练的制品，只比较候选制品与
现役制品在同一批 OOD 行上的 macro nDCG@10，并给出确定性基线作参照。

泄漏控制：cluster = (ood_class, 约束元组)；约束为空的类按 query_id 各自成簇。同簇永不
跨 train/eval。5 折按 ood_class 分层分配簇。门控臂只允许用生产可得信号（abstain /
parse.filters / query 文本）；constraints/excluded 是金标意图，禁止作为门控输入。

增益口径：线性 gain（grade 存在时 0.5*(grade/2)+0.5*(sem/3)，否则 sem/3）；nDCG@5/@10
宏平均。heldout（模板分布查询）作回归护栏。

用法：
    python scripts/ood_duel.py                       # 五折决斗
    python scripts/ood_duel.py --compare-frozen DIR  # 冻结对照：候选 DIR vs 现役（零重训）

`--compare-frozen` 把 DIR 的 LTR 制品作为 `ltr_candidate` 臂，与生产路径的 `ltr_old`
（现役）在同一批 OOD 行上做冻结对照——两臂都是不再训练的制品，故跳过五折重训与最终
全量重训。默认（不带旗标）行为不变。`--compare-frozen` 的缺省评测集是
`FROZEN_OOD_PATH`（只评测层，不进训练 mix-in），五折决斗仍读 `OOD_PATH`——两者刻意
分开，详见 `FROZEN_OOD_PATH` 的注释。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from train_ltr import (  # noqa: E402
    DEFAULT_IN_DIR,
    DEFAULT_SEED,
    FEATURE_NAMES,
    build_training_matrix,
    candidate_features,
    load_split,
    train_ranknet,
)

OOD_PATH = ROOT / "research" / "ood_eval" / "synth_ood_label_input_xscore.jsonl"

#: 冻结对照（`--compare-frozen`）的**只评测层**。
#: 与 `OOD_PATH` 分开是刻意的：五折决斗读的 `OOD_PATH` 是历史实验的数据（动它等于悄悄
#: 换掉一次既有实验），而冻结对照要比的是一批**不进训练 mix-in** 的行——两者由
#: `scripts/split_ood_evalonly.py` 从同一个源文件拆开，混用就回到“候选在自己见过的行上被裁决”。
FROZEN_OOD_PATH = ROOT / "research" / "ood_eval" / "synth_ood_evalonly_xscore.jsonl"
OUT_PATH = ROOT / "research" / "ood_eval" / "_ood_duel.json"
K_FOLDS = 5
FULL_COLS = tuple(range(len(FEATURE_NAMES)))

#: 门控规则常数（零训练、确定性、生产可部署）
GATE_ALPHA_LEX = 0.8
GATE_ALPHA_VEC = 0.2
GATE_ALPHA_MID = 0.5
ACCESSION_RE = re.compile(r"\b[A-Z]{1,4}\d{3,}\b|\d{5,}")


def load_ood_rows(path: Path | None = None) -> list[dict]:
    """读 OOD xscore 行并按 query_id 去重（保留候选更多的一行；同 id 双行是 A/B 复测）。"""
    source = path or OOD_PATH
    best: dict[str, dict] = {}
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        qid = row["query_id"]
        if qid not in best or len(row.get("candidates") or []) > len(best[qid].get("candidates") or []):
            best[qid] = row
    return list(best.values())


def cluster_key(row: dict) -> str:
    constraints = row.get("constraints") or {}
    if constraints:
        return f"{row['ood_class']}|{json.dumps(constraints, sort_keys=True, ensure_ascii=False)}"
    return f"{row['ood_class']}|{row['query_id']}"


def assign_folds(rows: list[dict], k: int) -> dict[str, int]:
    """簇级分层折分配：每类内按簇稳定排序后轮流发牌，保证各类在每折近似均衡。"""
    by_class: dict[str, list[str]] = defaultdict(list)
    seen: set[str] = set()
    for row in rows:
        key = cluster_key(row)
        if key in seen:
            continue
        seen.add(key)
        by_class[row["ood_class"]].append(key)
    fold_of: dict[str, int] = {}
    for cls in sorted(by_class):
        keys = sorted(by_class[cls])
        for i, key in enumerate(keys):
            fold_of[key] = i % k
    return fold_of


def gain(cand: dict) -> float | None:
    sem = cand.get("llm_semantic")
    if sem is None:
        return None
    sem_v = float(sem) / 3.0
    grade = cand.get("grade")
    if grade is None:
        return sem_v
    return 0.5 * (float(grade) / 2.0) + 0.5 * sem_v


def ndcg(order: list[int], gains: dict[int, float], k: int) -> float:
    dcg = sum(gains.get(idx, 0.0) / math.log2(r + 2) for r, idx in enumerate(order[:k]))
    ideal = sorted(gains.values(), reverse=True)
    idcg = sum(g / math.log2(r + 2) for r, g in enumerate(ideal[:k]))
    return dcg / idcg if idcg else 0.0


def _minmax(values: list[float]) -> list[float]:
    finite = [v for v in values if v is not None and math.isfinite(v)]
    if not finite:
        return [0.0] * len(values)
    low, high = min(finite), max(finite)
    if high - low < 1e-12:
        return [0.5 if (v is not None and math.isfinite(v)) else 0.0 for v in values]
    return [
        ((v - low) / (high - low)) if (v is not None and math.isfinite(v)) else 0.0
        for v in values
    ]


def gate_alpha(row: dict) -> float:
    """每查询词面权重：结构化（解析出过滤条件或编号直达）→ 词面主导；
    弃权/零过滤（口语、错字拼音、长啰嗦）→ 向量主导；其余居中。"""
    query = row.get("query") or ""
    filters = (row.get("parse") or {}).get("filters") or []
    if filters or ACCESSION_RE.search(query):
        return GATE_ALPHA_LEX
    if row.get("abstain") or not filters:
        return GATE_ALPHA_VEC
    return GATE_ALPHA_MID


def static_orders(row: dict) -> dict[str, list[int]]:
    cands = row.get("candidates") or []
    n = len(cands)
    lex = [float(c.get("lex_score") or 0.0) for c in cands]
    vec = [c.get("vec_score") for c in cands]
    pos_order = sorted(range(n), key=lambda i: (cands[i].get("pos") or 999, i))
    vec_order = sorted(range(n), key=lambda i: (-(vec[i] if vec[i] is not None else -2.0), i))
    lex_n = _minmax(lex)
    vec_n = _minmax(vec)
    lin = [0.5 * lex_n[i] + 0.5 * vec_n[i] for i in range(n)]
    lin_order = sorted(range(n), key=lambda i: (-lin[i], i))
    rrf: dict[int, float] = defaultdict(float)
    for r, i in enumerate(pos_order):
        rrf[i] += 1.0 / (60 + r + 1)
    for r, i in enumerate(vec_order):
        rrf[i] += 1.0 / (60 + r + 1)
    rrf_order = sorted(range(n), key=lambda i: (-rrf[i], i))
    a = gate_alpha(row)
    gated = [a * lex_n[i] + (1 - a) * vec_n[i] for i in range(n)]
    gate_alpha_order = sorted(range(n), key=lambda i: (-gated[i], i))
    gate_switch_order = pos_order if a >= GATE_ALPHA_MID else vec_order
    return {
        "pos": pos_order,
        "vec": vec_order,
        "linear": lin_order,
        "rrf": rrf_order,
        "gate_switch": gate_switch_order,
        "gate_alpha": gate_alpha_order,
    }


def ltr_order(row: dict, scorer, feature_set: str = "f0") -> list[int] | None:
    """LTR 打分排序。``feature_set`` 随候选配方（冻结对照从候选 meta 继承）：f3 = 读行的
    f3_feats 叠加层（生产存活池回放特征，与生产推理同一构造函数）。"""
    cands = row.get("candidates") or []
    if not cands:
        return None
    feats, _lex, _vec = candidate_features(row, feature_set)
    try:
        scores = scorer(feats.tolist())
    except Exception:
        return None
    return sorted(range(len(cands)), key=lambda i: (-scores[i], i))


def eval_rows(rows: list[dict], orders_by_qid: dict[str, dict[str, list[int]]]) -> dict:
    """宏平均 nDCG@5/@10 + per-class 分解 + 逐查询分数袋（供配对比较）。"""
    per_query: dict[str, dict[str, float]] = defaultdict(dict)
    per_cls: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        cands = row.get("candidates") or []
        gains = {i: g for i, c in enumerate(cands) if (g := gain(c))}
        if not gains:
            continue
        orders = orders_by_qid.get(row["query_id"]) or {}
        for name, order in orders.items():
            v10 = ndcg(order, gains, 10)
            per_query[name][row["query_id"]] = v10
            per_cls[row.get("ood_class") or "heldout"][name].append(v10)
    names = sorted(per_query)
    overall = {name: (statistics.mean(d.values()) if d else None) for name, d in per_query.items()}
    return {"overall10": overall, "per_query10": {k: dict(v) for k, v in per_query.items()},
            "per_cls10": {c: {n: round(statistics.mean(v), 4) for n, v in sorted(d.items()) if v}
                          for c, d in sorted(per_cls.items())}}


def train_scorer(rows: list[dict], seed: int):
    X, y, groups, stats = build_training_matrix(rows, "mix")
    if X.shape[0] == 0:
        return None, {"note": "no labelled candidates"}
    score_fn, info, _artifacts = train_ranknet(X, y, groups, FULL_COLS, seed)
    if score_fn is None:
        return None, info
    return (lambda feats_list: score_fn(np.asarray(feats_list, dtype=np.float32))), {**info, **stats}


def paired_stats(per_query: dict, left: str, right: str, macro: dict) -> dict:
    """两臂在共同查询上的配对统计（原值不取整，取整口径由调用方决定）。

    ltr_mix（五折决斗）与 ltr_candidate（冻结对照）共用这一份，避免第二套胜负计数。
    """
    common = sorted(set(per_query.get(left, {})) & set(per_query.get(right, {})))
    diffs = [per_query[left][q] - per_query[right][q] for q in common]
    wins = sum(1 for d in diffs if d > 1e-9)
    losses = sum(1 for d in diffs if d < -1e-9)
    return {
        "wins": wins, "losses": losses, "ties": len(diffs) - wins - losses, "n": len(diffs),
        "margin": (macro.get(left) or 0.0) - (macro.get(right) or 0.0),
        "mean": statistics.mean(diffs) if diffs else 0.0,
        "std": statistics.stdev(diffs) if len(diffs) > 1 else 0.0,
    }


def compare_frozen(candidate_dir: Path, *, incumbent_dir: "Path | None" = None,
                   ood_path: "Path | None" = None, feature_set: str = "f0") -> dict:
    """**冻结对照**（零重训）：候选制品 vs 现役制品在同一批 OOD 行上的 macro nDCG@10 对比。

    两臂都是不再训练的成品，故不需要任何折/训练——这正是 LTR 候选转正门禁要问的问题
    （"候选在 OOD 上是否不输现役"），而不是本脚本默认要问的"混 OOD 数据重训是否值得"。
    确定性对手臂（vec / 机械门控 / rrf / linear / 词面序）一并给出，用于判断这一版 LTR
    是否还打得过零训练的门控。

    incumbent_dir=None → 生产路径（`load_ltr_scorer()` 的缺省解析，即真正在服役的那份）。
    ood_path=None → `FROZEN_OOD_PATH`（只评测层：既不进缺省训练 mix-in，也不进本脚本的
    五折决斗——评测集与训练集物理分家是本门的成立条件）。
    """
    from dataset_recommender.retrieval.vector_recall import load_ltr_scorer

    rows = load_ood_rows(ood_path or FROZEN_OOD_PATH)
    ltr_old = load_ltr_scorer(incumbent_dir)
    ltr_candidate = load_ltr_scorer(candidate_dir)
    if ltr_old is None:
        raise RuntimeError(f"现役制品不可加载（{incumbent_dir or '生产路径'}）——冻结对照无从进行")
    if ltr_candidate is None:
        raise RuntimeError(f"候选制品不可加载（{candidate_dir}）——冻结对照无从进行")

    orders_by_qid: dict[str, dict[str, list[int]]] = {}
    for row in rows:
        arms = static_orders(row)
        for name, scorer in (("ltr_old", ltr_old), ("ltr_candidate", ltr_candidate)):
            order = ltr_order(row, scorer, feature_set)
            if order is not None:
                arms[name] = order
        orders_by_qid[row["query_id"]] = arms
    evaluation = eval_rows(rows, orders_by_qid)
    macro = evaluation["overall10"]
    reference = "ltr_old"
    if reference not in macro:
        mechanical = [n for n in sorted(macro) if not n.startswith("ltr_")]
        if not mechanical:
            raise RuntimeError("OOD 集上没有任何可比较的臂（全部无标签？）")
        reference = mechanical[0]
    stats = paired_stats(evaluation["per_query10"], "ltr_candidate", reference, macro)
    margin = stats["margin"]
    verdict = "win" if margin >= 0.02 else ("lose" if margin <= -0.02 else "tie")
    return {
        "mode": "frozen_compare",
        "n_ood": len(rows),
        "candidate_dir": str(candidate_dir),
        "incumbent_dir": str(incumbent_dir) if incumbent_dir else "production-default",
        "ood_path": str(ood_path or FROZEN_OOD_PATH),
        "overall10": {name: (round(value, 4) if value is not None else None) for name, value in macro.items()},
        "per_cls10": evaluation["per_cls10"],
        "verdict": {
            "best_opponent": reference,
            #: `margin` 只供人读（4 位小数）；**门禁必须用 `margin_raw`**——前者会把
            #: −4e−5 舍成 −0.0，与 `>= -0.0` 一比就放行（边界假 ACCEPT）。
            "margin": round(margin, 4),
            "margin_raw": margin,
            "verdict": verdict,
            "wins": stats["wins"], "losses": stats["losses"], "ties": stats["ties"],
            "paired_delta_mean": round(stats["mean"], 4),
            "paired_delta_std": round(stats["std"], 4),
            "n_paired": stats["n"],
        },
    }


def _run_compare_frozen(args) -> int:
    """`--compare-frozen DIR` 的落盘与打印（两种模式的产物名分开，避免互相覆盖）。"""
    payload = compare_frozen(args.compare_frozen.resolve(),
                            incumbent_dir=(args.incumbent_dir.resolve() if args.incumbent_dir else None),
                            ood_path=(args.ood_path.resolve() if args.ood_path else None))
    out = args.out or (ROOT / "research" / "ood_eval" / "_ood_frozen_compare.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"== 冻结对照（零重训，n={payload['n_ood']}） ==")
    for name in sorted(payload["overall10"], key=lambda n: -(payload["overall10"][n] or -1)):
        print(f"  {name:<16}{payload['overall10'][name]}")
    verdict = payload["verdict"]
    print(f"  候选 vs {verdict['best_opponent']}: margin {verdict['margin']:+.4f} → {verdict['verdict']}"
          f"（{verdict['wins']} 胜 {verdict['losses']} 负 {verdict['ties']} 平）")
    print(f"\nwrote {out}")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compare-frozen", type=Path, default=None, metavar="MODEL_DIR",
                        help="冻结对照模式：把 MODEL_DIR 的 LTR 制品作为 ltr_candidate 臂，与现役"
                             "做零重训对照（跳过五折与全量重训臂）；不带此旗标即历史行为")
    parser.add_argument("--incumbent-dir", type=Path, default=None,
                        help="冻结对照的现役制品目录（默认 = 生产加载路径）")
    parser.add_argument("--ood-path", type=Path, default=None,
                        help="OOD 数据文件（冻结对照默认 FROZEN_OOD_PATH=只评测层，五折决斗默认 OOD_PATH）")
    parser.add_argument("--out", type=Path, default=None, help="报告落点")
    args = parser.parse_args(argv)
    if args.compare_frozen is not None:
        return _run_compare_frozen(args)

    ood_rows = load_ood_rows(args.ood_path)
    train_rows, train_info = load_split(DEFAULT_IN_DIR, "train")
    held_rows, held_info = load_split(DEFAULT_IN_DIR, "heldout")
    print(f"ood={len(ood_rows)} train={len(train_rows)} heldout={len(held_rows)}")

    from dataset_recommender.retrieval.vector_recall import load_ltr_scorer
    ltr_old = load_ltr_scorer()
    print("ltr_old:", "loaded" if ltr_old else "UNAVAILABLE")

    fold_of = assign_folds(ood_rows, K_FOLDS)
    folds: dict[int, list[dict]] = defaultdict(list)
    for row in ood_rows:
        folds[fold_of[cluster_key(row)]].append(row)

    ood_orders: dict[str, dict[str, list[int]]] = {}
    held_orders: dict[str, dict[str, list[int]]] = {}
    fold_notes = []
    for fold in range(K_FOLDS):
        eval_rows_f = folds[fold]
        train_ood = [r for f in range(K_FOLDS) if f != fold for r in folds[f]]
        sc_mix, info_mix = train_scorer(train_rows + train_ood, DEFAULT_SEED + fold)
        sc_ood, info_ood = train_scorer(train_ood, DEFAULT_SEED + fold)
        fold_notes.append({
            "fold": fold,
            "eval_queries": len(eval_rows_f),
            "train_ood_queries": len(train_ood),
            "ltr_mix": info_mix,
            "ltr_ood": info_ood,
        })
        for row in eval_rows_f:
            orders = static_orders(row)
            if ltr_old is not None:
                o = ltr_order(row, ltr_old)
                if o is not None:
                    orders["ltr_old"] = o
            if sc_mix is not None:
                o = ltr_order(row, sc_mix)
                if o is not None:
                    orders["ltr_mix"] = o
            if sc_ood is not None:
                o = ltr_order(row, sc_ood)
                if o is not None:
                    orders["ltr_ood"] = o
            ood_orders[row["query_id"]] = orders
        print(f"fold {fold}: eval={len(eval_rows_f)} train_ood={len(train_ood)} "
              f"mix_pairs={info_mix.get('pairs')} ood_pairs={info_ood.get('pairs')}")

    # heldout 护栏：用全部 OOD + synth_train 训一版（模拟最终上线训练集），与旧制品对比
    sc_full, info_full = train_scorer(train_rows + ood_rows, DEFAULT_SEED)
    for row in held_rows:
        orders = static_orders(row)
        if ltr_old is not None:
            o = ltr_order(row, ltr_old)
            if o is not None:
                orders["ltr_old"] = o
        if sc_full is not None:
            o = ltr_order(row, sc_full)
            if o is not None:
                orders["ltr_mix_allood"] = o
        held_orders[row["query_id"]] = orders

    ood_eval = eval_rows(ood_rows, ood_orders)
    held_eval = eval_rows(held_rows, held_orders)

    names = sorted(ood_eval["overall10"])
    print("\n== OOD 决斗（nDCG@10 宏平均，5 折池化） ==")
    for name in names:
        print(f"  {name:<14}{ood_eval['overall10'][name]:.4f}")
    print("\n== heldout 护栏（nDCG@10） ==")
    for name in sorted(held_eval["overall10"]):
        v = held_eval["overall10"][name]
        print(f"  {name:<14}{v:.4f}" if v is not None else f"  {name:<14}n/a")

    # 裁决：ltr_mix vs 最强不交 OOD 标签的对手（vec/gate/ltr_old/rrf/linear/pos）
    opponents = [n for n in names if n not in ("ltr_mix", "ltr_ood")]
    pq = ood_eval["per_query10"]
    best_opp = max(opponents, key=lambda n: ood_eval["overall10"].get(n) or -1)
    stats = paired_stats(pq, "ltr_mix", best_opp, ood_eval["overall10"])
    wins, losses = stats["wins"], stats["losses"]
    margin = stats["margin"]
    verdict = "win" if margin >= 0.02 else ("lose" if margin <= -0.02 else "tie")
    guard = None
    if held_eval["overall10"].get("ltr_mix_allood") is not None and held_eval["overall10"].get("ltr_old") is not None:
        guard = round(held_eval["overall10"]["ltr_mix_allood"] - held_eval["overall10"]["ltr_old"], 4)

    print(f"\n== 裁决 ==")
    print(f"  最强对手: {best_opp} ({ood_eval['overall10'][best_opp]:.4f})")
    print(f"  ltr_mix margin: {margin:+.4f} → {verdict}（|margin|<0.02 判平）")
    print(f"  逐查询配对: {wins} 胜 {losses} 负 {stats['ties']} 平"
          f"（delta mean {stats['mean']:+.4f} ± {stats['std']:.4f}）")
    print(f"  heldout 护栏（ltr_mix_allood - ltr_old）: {guard}")

    payload = {
        "seed": DEFAULT_SEED,
        "k_folds": K_FOLDS,
        "n_ood": len(ood_rows),
        "n_train_template": len(train_rows),
        "n_heldout": len(held_rows),
        "train_layers": train_info["layers"],
        "heldout_layers": held_info["layers"],
        "folds": fold_notes,
        "ood": ood_eval,
        "heldout": held_eval,
        "verdict": {
            "best_opponent": best_opp,
            "margin": round(margin, 4),
            "verdict": verdict,
            "wins": wins,
            "losses": losses,
            "ties": stats["ties"],
            "paired_delta_mean": round(stats["mean"], 4),
            "paired_delta_std": round(stats["std"], 4),
            "heldout_guard": guard,
        },
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
