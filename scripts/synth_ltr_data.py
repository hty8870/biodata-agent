# -*- coding: utf-8 -*-
"""Synthesize pointwise LTR (learning-to-rank) data from the frozen catalog.

Deterministic, offline, zero-LLM: sample constraint combinations from the
controlled vocabulary, render natural-language queries from a template bank,
run the real retrieval pipeline, and grade every returned candidate by
constraint satisfaction (labels are known by construction).

Design notes:
- Hard constraints go through the parser as usual; one dimension is phrased
  with a soft-preference hedge (e.g. "最好…") so survivors vary in how many
  sampled constraints they satisfy — that variance is what the ranker learns
  from. Grades: 2 = all sampled constraints satisfied, 1 = exactly one
  violated, 0 = two or more violated.
- Candidate judging reuses the frozen benchmark's external judge
  (``evaluate_recommendation.constraint_satisfied``) so labels share the
  benchmark's semantics.
- Queries colliding verbatim with the frozen evaluation set are dropped.
- Train/held-out split is by constraint-combination cluster so no
  combination leaks across the boundary.

Usage:
  python scripts/synth_ltr_data.py [--target-queries 1400] [--seed 20260907]
      [--out-dir research/ltr_data] [--limit N]
"""
from __future__ import annotations

import argparse
import io
import json
import random
import sys
import time
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))
sys.path.insert(0, str(AGENT_ROOT / "scripts"))

from evaluate_recommendation import constraint_satisfied, load_pipeline, recommend  # noqa: E402
from dataset_recommender.retrieval.query_parser import active_filters, parse_query  # noqa: E402
from dataset_recommender.retrieval.vocabulary import CATALOG  # noqa: E402

DEFAULT_SEED = 20260907
DEFAULT_TARGET = 1400
HELDOUT_MIN_QUERIES = 150
HELDOUT_RATIO = 0.15
TOP_K = 10
#: Minimum corpus records matching all constraints for a cluster to be usable.
MIN_SUPPORT_FULL = 1

CORE_DIMS = ("species", "tissue", "disease")

#: Query templates; {body} is the space-joined constraint phrases, {soft} a
#: hedged soft-preference clause, {raw} a raw-data requirement clause.
TEMPLATES = (
    "{body}",
    "找{body}的数据集",
    "给我一些{body}相关的数据",
    "有没有{body}的公开数据",
    "想做{body}方面的研究，有什么数据集可用",
    "{body}单细胞数据",
    "请推荐{body}数据集",
)
SOFT_CLAUSES = ("，最好{phrase}", "，优先{phrase}", "，{phrase}的更好")
RAW_CLAUSES = ("，需要包含 FASTQ 原始数据", "，要能下载到原始数据", "，必须有 fastq")


def _is_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def dim_entries(dim: str) -> list[dict]:
    """Controlled-vocabulary entries for a dimension: canonical value, a
    query-renderable phrase (longest CJK alias, else the canonical display),
    and judge terms (canonical + non-CJK aliases, lowercased)."""
    out: list[dict] = []
    for entry in CATALOG.get(dim, []) or []:
        display = str(entry.get("display") or "").strip()
        if not display:
            continue
        aliases = [str(a).strip() for a in entry.get("aliases", []) if str(a).strip()]
        cjk = sorted((a for a in aliases if _is_cjk(a)), key=len, reverse=True)
        judge = sorted({display.lower(), *(a.lower() for a in aliases if not _is_cjk(a))})
        out.append({"value": display, "phrase": cjk[0] if cjk else display, "judge": judge})
    return sorted(out, key=lambda e: e["value"])


#: How many query phrasings may reuse the same constraint cluster. Clusters stay
#: the split unit for the held-out set, so extra phrasings never leak across.
PHRASINGS_PER_CLUSTER = 6

#: Sampling patterns: (hard dims, soft dim or None, require raw data).
PATTERNS = (
    (("disease",), "tissue", False),
    (("disease",), "tissue", True),
    (("species", "disease"), "tissue", False),
    (("species", "disease"), "tissue", True),
    (("tissue", "disease"), "species", False),
    (("species", "tissue"), None, True),
    (("disease",), None, True),
    (("species", "tissue", "disease"), None, False),
)


def build_query(parts: list[str], soft_phrase: str | None, need_raw: bool, rng: random.Random) -> str:
    body = " ".join(parts)
    text = rng.choice(TEMPLATES).format(body=body)
    if soft_phrase:
        text += rng.choice(SOFT_CLAUSES).format(phrase=soft_phrase)
    if need_raw:
        text += rng.choice(RAW_CLAUSES)
    return text


def hard_dim_set(intent: object) -> set[str]:
    dims = set()
    for f in active_filters(intent):
        if f.get("polarity") != "include":
            continue
        fid = str(f.get("filter_id") or "")
        if fid.startswith("include:"):
            dims.add(fid.split(":", 1)[1])
        elif fid == "raw:required":
            dims.add("has_raw_data")
    return dims


def grade_candidate(record: object, constraints: dict[str, list]) -> tuple[int, dict[str, bool]]:
    matched = {dim: constraint_satisfied(record, dim, terms) for dim, terms in constraints.items()}
    violated = sum(1 for ok in matched.values() if not ok)
    return (2 if violated == 0 else 1 if violated == 1 else 0), matched


def synthesize(target_queries: int, seed: int, out_dir: Path, limit: int | None) -> dict:
    settings, records = load_pipeline()
    entries = {dim: dim_entries(dim) for dim in CORE_DIMS}

    # Precompute per-entry matching record sets once; combo support is then a
    # set intersection instead of rescanning the corpus per attempt.
    match_sets: dict[tuple[str, str], frozenset] = {}
    for dim, dim_entries_list in entries.items():
        for e in dim_entries_list:
            match_sets[(dim, e["value"])] = frozenset(
                i for i, r in enumerate(records) if constraint_satisfied(r, dim, e["judge"]))
    raw_set = frozenset(i for i, r in enumerate(records) if getattr(r, "has_raw_data", None) is True)
    bench_queries = set()
    bench_path = AGENT_ROOT / "eval" / "eval_queries.json"
    if bench_path.exists():
        payload = json.loads(bench_path.read_text(encoding="utf-8"))
        bench_queries = {str(q.get("query") or "").strip() for q in payload.get("queries", [])}

    rng = random.Random(seed)
    seen_queries: set[str] = set()
    cluster_counts: dict[str, int] = {}
    dropped = {"parse": 0, "support": 0, "bench_dup": 0, "query_dup": 0}
    specs: list[dict] = []

    attempts = 0
    max_attempts = max(target_queries * 400, 200_000)
    while len(specs) < target_queries and attempts < max_attempts:
        attempts += 1
        hard_dims, soft_dim, need_raw = PATTERNS[rng.randrange(len(PATTERNS))]
        picked = {dim: entries[dim][rng.randrange(len(entries[dim]))] for dim in hard_dims}
        soft_entry = entries[soft_dim][rng.randrange(len(entries[soft_dim]))] if soft_dim else None
        constraints: dict[str, object] = {dim: e["judge"] for dim, e in picked.items()}
        if soft_entry:
            constraints[soft_dim] = soft_entry["judge"]
        if need_raw:
            constraints["has_raw_data"] = True  # judge expects a bool for this key

        cluster = "|".join(f"{d}={picked[d]['value']}" for d in sorted(picked))
        if soft_entry:
            cluster += f"|soft:{soft_dim}={soft_entry['value']}"
        if need_raw:
            cluster += "|raw"
        if cluster_counts.get(cluster, 0) >= PHRASINGS_PER_CLUSTER:
            continue

        sets = [match_sets[(dim, picked[dim]["value"])] for dim in hard_dims]
        if soft_entry:
            sets.append(match_sets[(soft_dim, soft_entry["value"])])
        if need_raw:
            sets.append(raw_set)
        support = len(sets[0].intersection(*sets[1:])) if sets else 0
        if support < MIN_SUPPORT_FULL:
            dropped["support"] += 1
            continue

        query = build_query([picked[d]["phrase"] for d in hard_dims],
                            soft_entry["phrase"] if soft_entry else None, need_raw, rng)
        if query in seen_queries:
            dropped["query_dup"] += 1
            continue
        if query in bench_queries:
            dropped["bench_dup"] += 1
            continue

        intent = parse_query(query, settings.keyword_mapping)
        parsed_hard = hard_dim_set(intent)
        expected_hard = set(hard_dims) | ({"has_raw_data"} if need_raw else set())
        if getattr(intent, "parse_status", "") != "executable" or not expected_hard <= parsed_hard:
            dropped["parse"] += 1
            continue

        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
        seen_queries.add(query)
        specs.append({"query": query, "cluster": cluster, "constraints": constraints,
                      "hard": sorted(expected_hard), "support": support})

    if limit:
        specs = specs[:limit]

    rows: list[dict] = []
    for i, spec in enumerate(specs, 1):
        candidates = recommend(spec["query"], records, settings, top_k=TOP_K)
        if not candidates:
            dropped["support"] += 1
            continue
        rows.append({
            "query_id": f"syn{i:05d}",
            "query": spec["query"],
            "cluster_id": spec["cluster"],
            "constraints": {d: (t if isinstance(t, bool) else (t[0] if len(t) == 1 else t))
                            for d, t in spec["constraints"].items()},
            "hard_dims": spec["hard"],
            "support": spec["support"],
            "candidates": [
                (lambda grade, matched: {
                    "uid": str(c.record.raw.get("dataset_uid") or "") if isinstance(c.record.raw, dict) else "",
                    "pos": pos,
                    "lex_score": float(getattr(c, "score", 0.0) or 0.0),
                    "grade": grade,
                    "matched": matched,
                })(*grade_candidate(c.record, spec["constraints"]))
                for pos, c in enumerate(candidates, 1)
            ],
        })

    # Cluster-disjoint split: whole clusters to held-out until enough queries.
    clusters = sorted({r["cluster_id"] for r in rows})
    rng.shuffle(clusters)
    heldout_target = max(HELDOUT_MIN_QUERIES, int(round(len(rows) * HELDOUT_RATIO)))
    heldout_clusters: set[str] = set()
    heldout_count = 0
    for cl in clusters:
        if heldout_count >= heldout_target:
            break
        heldout_clusters.add(cl)
        heldout_count += sum(1 for r in rows if r["cluster_id"] == cl)
    for r in rows:
        r["split"] = "heldout" if r["cluster_id"] in heldout_clusters else "train"

    out_dir.mkdir(parents=True, exist_ok=True)
    for split in ("train", "heldout"):
        part = [r for r in rows if r["split"] == split]
        with (out_dir / f"synth_{split}.jsonl").open("w", encoding="utf-8") as fh:
            for r in part:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    grades = [c["grade"] for r in rows for c in r["candidates"]]
    stats = {
        "seed": seed,
        "corpus_size": len(records),
        "queries": len(rows),
        "pairs": len(grades),
        "grade_distribution": {str(g): grades.count(g) for g in (0, 1, 2)},
        "train_queries": sum(1 for r in rows if r["split"] == "train"),
        "heldout_queries": sum(1 for r in rows if r["split"] == "heldout"),
        "heldout_clusters": len(heldout_clusters),
        "cluster_leakage": 0,  # disjoint by construction; assertion below guards
        "dropped": dropped,
        "attempts": attempts,
    }
    train_clusters = {r["cluster_id"] for r in rows if r["split"] == "train"}
    assert not (train_clusters & heldout_clusters), "cluster leakage between splits"
    (out_dir / "_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


def main() -> None:
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target-queries", type=int, default=DEFAULT_TARGET)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--out-dir", type=Path, default=AGENT_ROOT / "research" / "ltr_data")
    ap.add_argument("--limit", type=int, default=None, help="debug: cap synthesized queries")
    args = ap.parse_args()

    started = time.time()
    stats = synthesize(args.target_queries, args.seed, args.out_dir, args.limit)
    stats["elapsed_s"] = round(time.time() - started, 1)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
