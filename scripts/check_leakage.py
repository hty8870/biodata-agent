# -*- coding: utf-8 -*-
"""Deterministic leakage guard for a newly generated query wave.

Why this exists: a query wave is *training* data, but the same corpus also holds
the frozen benchmarks, the held-out split and the OOD duel set that decide
whether a retrained artifact may be promoted. A single query that appears in both
sides turns that decision into memorisation, and no quality gate can see it --
the metrics just look better. Text comparison is cheap, exhaustive and needs no
model, so it runs *before* any labelling budget is spent.

Matching is by normalised text (NFKC, case folding, punctuation/whitespace
removal), so a copy that only differs by spacing, full-width characters or
punctuation is still caught. The normalisation is deliberately aggressive:
for a guard, a false positive costs one query, a false negative costs the
validity of the whole wave.

Verdict semantics:

- collision with an **evaluation** set -> fatal (exit code 1); the wave must not
  be used as training data until the offending queries are removed;
- collision with the **incumbent training corpus** or another wave row -> not
  fatal, but the row is dropped from the filtered wave: a repeat carries no new
  pairwise information and only reweights the loss surface.

Every reference set is optional: a missing path is reported as ``absent`` rather
than silently ignored, so a mistyped root cannot make the guard vacuously green.

Usage:
  python scripts/check_leakage.py --wave <wave.jsonl> --report <report.json> \
      --filtered <wave.clean.jsonl>
  python scripts/check_leakage.py --selftest
"""
from __future__ import annotations

import argparse

import json
import sys
import tempfile
import unicodedata
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]

#: Reference corpora, as (name, project-relative path, kind). ``kind`` decides the
#: verdict: ``eval`` collisions are fatal, ``train`` collisions are deduplicated.
#: Paths are literal (no globs) on purpose: a glob such as ``synth_train*.jsonl``
#: would also match the wave layer this guard is checking, making the wave a
#: duplicate of itself.
REFERENCE_SETS: tuple[tuple[str, str, str], ...] = (
    ("eval_queries", "eval/eval_queries.json", "eval"),
    ("eval_queries_holdout", "eval/eval_queries_holdout.json", "eval"),
    ("eval_queries_dev", "eval/eval_queries_dev.json", "eval"),
    ("agent_live_cases_v1", "eval/agent_live_cases_v1.jsonl", "eval"),
    ("agent_live_cases_aug", "eval/agent_live_cases_aug.jsonl", "eval"),
    ("ood_queries", "research/ood_eval/ood_queries.jsonl", "eval"),
    ("ood_duel_rows", "research/ood_eval/synth_ood_label_input_xscore.jsonl", "eval"),
    ("synth_heldout", "research/ltr_data/synth_heldout.jsonl", "eval"),
    ("synth_heldout_nl", "research/ltr_data/synth_heldout_nl.jsonl", "eval"),
    ("synth_heldout_labeled", "research/ltr_data/synth_heldout_labeled.jsonl", "eval"),
    ("synth_heldout_vec", "research/ltr_data/synth_heldout_vec.jsonl", "eval"),
    ("synth_heldout_xscore", "research/ltr_data/synth_heldout_xscore.jsonl", "eval"),
    ("synth_train", "research/ltr_data/synth_train.jsonl", "train"),
    ("synth_train_nl", "research/ltr_data/synth_train_nl.jsonl", "train"),
    ("synth_train_labeled", "research/ltr_data/synth_train_labeled.jsonl", "train"),
    ("synth_train_vec", "research/ltr_data/synth_train_vec.jsonl", "train"),
    ("synth_train_xscore", "research/ltr_data/synth_train_xscore.jsonl", "train"),
)

#: Query-bearing keys, tried in order. Reference files are heterogeneous (plain
#: benchmark entries, synthesised rows, OOD rows, agent live cases whose user
#: text lives in ``utterance``), so the guard reads whichever key is present
#: instead of requiring one shape. This is the single knob for a new key name;
#: a reference corpus that yields no comparable query at all fails closed below.
QUERY_KEYS = ("query", "query_nl", "text", "question", "prompt", "utterance")


def normalize(text: str) -> str:
    """Fold a query to its comparison form: NFKC, case-folded, alphanumerics only.

    Punctuation and whitespace are dropped rather than collapsed, so that
    ``fei xian wei hua`` and ``feixianweihua`` (a real pinyin-vs-hanzi-adjacent
    pair in this corpus) compare equal.
    """
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


#: Object keys that hold the row list in the reference files, tried before the
#: first-list fallback so a file with leading metadata lists cannot shadow them.
_ROW_LIST_KEYS = ("queries", "rows", "items", "records", "data")


def _rows_from_list(value: "object") -> "list[dict]":
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _rows_from_document(payload: "object") -> "list[dict]":
    """Whole decoded JSON document -> rows.

    A wrapper object is unwrapped by a known row key first (``queries`` …), then by
    its first list value (the historical behaviour); a lone row object and a
    ``{id: row}`` mapping are also accepted so a document-shaped file cannot
    silently read as zero rows.
    """
    if isinstance(payload, list):
        return _rows_from_list(payload)
    if isinstance(payload, dict):
        for key in _ROW_LIST_KEYS:
            if isinstance(payload.get(key), list):
                return _rows_from_list(payload.get(key))
        for value in payload.values():
            if isinstance(value, list):
                return _rows_from_list(value)
        if query_of(payload):
            return [payload]
        return [value for value in payload.values() if isinstance(value, dict) and query_of(value)]
    return []


def _rows_from_line(payload: "object") -> "list[dict]":
    """One JSONL line -> rows: a dict line is a row, an array line is expanded."""
    if isinstance(payload, dict):
        return [payload]
    return _rows_from_list(payload)


def read_rows(path: Path, *, strict: bool = False) -> "list[dict]":
    """Read a JSON array / object or JSON lines without silently dropping rows.

    Whole-text JSON is tried **first**: a pretty-printed array has a trailing
    comma on every element line, so line-first parsing would see only the last
    element (the one without a comma) and silently collapse the file to a single
    row. JSONL falls back to per-line decoding and tolerates a torn line (a
    crash-partial write) plus a trailing comma on an element line.

    ``strict=True`` (reference sets) fails closed: any non-empty line that
    cannot be decoded raises, because a reference corpus that silently shrinks
    turns the leakage guard into a vacuous pass. Wave files stay tolerant.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    stripped = text.strip()
    if stripped:
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            return _rows_from_document(payload)
    rows: "list[dict]" = []
    malformed: "list[int]" = []
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        attempts = (line, line[:-1].rstrip() if line.endswith(",") else None)
        payload = None
        for attempt in attempts:
            if not attempt:
                continue
            try:
                payload = json.loads(attempt)
            except json.JSONDecodeError:
                continue
            break
        if payload is None:
            malformed.append(number)
            continue
        rows.extend(_rows_from_line(payload))
    if strict and malformed:
        preview = ", ".join(str(number) for number in malformed[:5])
        raise ValueError(
            f"{path}: {len(malformed)} 行无法解析（第 {preview} 行）——参考集拒绝静默降级"
        )
    return rows


def query_of(row: dict) -> str:
    for key in QUERY_KEYS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _index(rows: "list[dict]") -> "dict[str, str]":
    """normalised query -> original text, for one reference file's rows."""
    out: dict[str, str] = {}
    for row in rows:
        text = query_of(row)
        key = normalize(text)
        if key:
            out.setdefault(key, text)
    return out


def load_references(root: Path) -> "list[dict]":
    refs = []
    for name, relative, kind in REFERENCE_SETS:
        path = root / relative
        entry = {"name": name, "path": relative, "kind": kind, "exists": path.is_file(),
                 "rows": 0, "unique": 0, "index": {}}
        if entry["exists"]:
            # 参考集 strict：解析不了的行使索引静默缺失 = 泄漏门可能放行，必须 fail-closed。
            rows = read_rows(path, strict=True)
            index = _index(rows)
            if not index:
                # 文件在、行也在，却一条可比对 query 都提不出来（字段名没被 QUERY_KEYS 覆盖 /
                # 全是空文本）：此时该参考集对撞车判定完全无效，PASS 是假绿。必须 fail-closed，
                # 而不是给一份"零参考"的绿报告。允许无 query 的只有波数据（read_rows strict=False）。
                raise ValueError(
                    f"{path}: 参考集存在但提取不到任何可比对 query（行数 {len(rows)}）"
                    f"——请核对 QUERY_KEYS 是否覆盖该文件的字段名"
                )
            entry["index"] = index
            entry["rows"] = len(rows)
            entry["unique"] = len(index)
        refs.append(entry)
    return refs


def check(wave_path: Path, root: Path) -> "tuple[dict, list[dict]]":
    """Compare a wave against every reference set; return (report, kept rows)."""
    wave_rows = read_rows(wave_path)
    refs = load_references(root)

    # A single lookup table, keyed by normalised text. Evaluation sets are
    # inserted first so that a query living in both an eval set and the training
    # corpus is reported as the fatal collision, not the harmless duplicate.
    collisions: dict[str, tuple[dict, str]] = {}
    for ref in sorted(refs, key=lambda item: 0 if item["kind"] == "eval" else 1):
        for key, text in ref["index"].items():
            collisions.setdefault(key, (ref, text))

    seen_wave: dict[str, int] = {}
    eval_collisions: list[dict] = []
    train_duplicates: list[dict] = []
    intra_duplicates: list[dict] = []
    kept: list[dict] = []

    for position, row in enumerate(wave_rows, 1):
        qid = str(row.get("qid") or row.get("query_id") or f"row{position}")
        text = query_of(row)
        key = normalize(text)
        dropped = False
        if not key:
            dropped = True  # no comparable query text: never silently kept
        elif key in collisions:
            ref, ref_text = collisions[key]
            record = {"row": position, "qid": qid, "query": text,
                      "set": ref["name"], "set_kind": ref["kind"], "reference_query": ref_text}
            if ref["kind"] == "eval":
                eval_collisions.append(record)
            else:
                train_duplicates.append(record)
            dropped = True
        elif key in seen_wave:
            intra_duplicates.append({"row": position, "qid": qid, "query": text,
                                     "first_row": seen_wave[key]})
            dropped = True
        else:
            seen_wave[key] = position
        if not dropped:
            kept.append(row)

    report = {
        "schema_version": 1,
        "wave": {"path": wave_path.name, "rows": len(wave_rows),
                 "unique_normalized": len(seen_wave)},
        "reference_sets": [
            {"name": ref["name"], "path": ref["path"], "kind": ref["kind"],
             "exists": ref["exists"], "rows": ref["rows"], "unique": ref["unique"]}
            for ref in refs
        ],
        "eval_collisions": eval_collisions,
        "train_duplicates": train_duplicates,
        "intra_duplicates": intra_duplicates,
        "rows_kept": len(kept),
        "verdict": "FAIL" if eval_collisions else "PASS",
    }
    return report, kept


def write_outputs(report: dict, kept: "list[dict]", report_path: "Path | None",
                  filtered_path: "Path | None") -> None:
    # 显式 LF：JSONL 产物要与管线其余层字节可比（Windows 下 text 模式默认 CRLF）。
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8", newline="\n")
    if filtered_path is not None:
        filtered_path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in kept)
        filtered_path.write_text(body, encoding="utf-8", newline="\n")


def print_report(report: dict) -> None:
    wave = report["wave"]
    print(f"查询波 {wave['path']}：{wave['rows']} 行，归一化后 {wave['unique_normalized']} 条唯一")
    for ref in report["reference_sets"]:
        mark = " " if ref["exists"] else "?"
        print(f"  [{mark}] {ref['name']:<24}{ref['kind']:<6}{ref['rows']:>6} 行"
              f"（唯一 {ref['unique']}）{'  ← 缺失' if not ref['exists'] else ''}")
    print(f"  评测集撞车：{len(report['eval_collisions'])}  "
          f"现役训练重复：{len(report['train_duplicates'])}  "
          f"波内重复：{len(report['intra_duplicates'])}")
    for item in report["eval_collisions"][:20]:
        print(f"    ! [{item['set']}] {item['qid']}: {item['query'][:60]}")
    for item in report["train_duplicates"][:10]:
        print(f"    - [{item['set']}] {item['qid']}: {item['query'][:60]}")
    print(f"  保留 {report['rows_kept']} 行 · verdict: {report['verdict']}")


# ---------------------------------------------------------------------------- 自测
# 已知答案自测：故意混入「逐字复制」与「仅标点/全角不同」两种评测查询，外加一条现役
# train 重复与一条波内重复；护栏必须抓住全部，且干净行必须留下。没有这条自测，
# 一个恒返回 PASS 的护栏与一个正确的护栏在真实跑批里长得一模一样。
SELFTEST_EVAL = [
    {"query": "人类肺组织的单细胞数据，要能下载"},
    {"query": "小鼠海马单细胞数据"},
]
SELFTEST_TRAIN = [
    {"query": "人类心脏的单细胞数据"},
]
SELFTEST_WAVE = [
    {"qid": "w-000", "query": "人类肺组织的单细胞数据，要能下载"},          # 逐字撞评测
    {"qid": "w-001", "query": "人类肺组织的单细胞数据，要能下载！！！ "},     # 归一化后撞评测
    {"qid": "w-002", "query": "找点小鼠海马的转录组数据"},                  # 干净
    {"qid": "w-003", "query": "找点小鼠海马的转录组数据！"},                # 波内重复
    {"qid": "w-004", "query": "人类心脏的单细胞数据"},                      # 撞现役 train
    {"qid": "w-005", "query": "有没有青蛙的分类数据"},                      # 干净
]


def selftest() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "eval").mkdir(parents=True)
        (root / "research" / "ltr_data").mkdir(parents=True)
        (root / "eval" / "eval_queries.json").write_text(
            json.dumps(SELFTEST_EVAL, ensure_ascii=False), encoding="utf-8")
        (root / "research" / "ltr_data" / "synth_train.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in SELFTEST_TRAIN),
            encoding="utf-8")
        wave = root / "wave.jsonl"
        wave.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in SELFTEST_WAVE),
                        encoding="utf-8")

        report, kept = check(wave, root)
        caught = {item["qid"] for item in report["eval_collisions"]}
        checks = [
            ("逐字撞评测被抓", caught == {"w-000", "w-001"}),
            ("现役 train 重复被抓", [i["qid"] for i in report["train_duplicates"]] == ["w-004"]),
            ("波内重复被抓", [i["qid"] for i in report["intra_duplicates"]] == ["w-003"]),
            ("裁决为 FAIL", report["verdict"] == "FAIL"),
            ("干净行留下", [row["qid"] for row in kept] == ["w-002", "w-005"]),
        ]

        clean = root / "wave_clean.jsonl"
        clean.write_text("".join(json.dumps(SELFTEST_WAVE[i], ensure_ascii=False) + "\n"
                                 for i in (2, 5)), encoding="utf-8")
        report2, kept2 = check(clean, root)
        checks.append(("干净波裁决为 PASS", report2["verdict"] == "PASS"))
        checks.append(("干净波全保留", len(kept2) == 2))

        missing = root / "absent_root"
        missing.mkdir()
        report3, _ = check(wave, missing)
        checks.append(("缺失参考集如实报告",
                       all(not ref["exists"] for ref in report3["reference_sets"])))

    ok = all(passed for _name, passed in checks)
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("selftest: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wave", type=Path, help="待检查的查询波（JSONL）")
    parser.add_argument("--project-root", type=Path, default=AGENT_ROOT,
                        help="参考集所在的项目根（默认本仓库）")
    parser.add_argument("--report", type=Path, help="判交报告落盘路径（JSON）")
    parser.add_argument("--filtered", type=Path, help="剔除重复后的查询波落盘路径")
    parser.add_argument("--selftest", action="store_true", help="跑已知答案自测并退出")
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.wave:
        parser.error("--wave 或 --selftest 必选其一")
    # 重设编码而不是换掉 sys.stdout 对象：包一层 TextIOWrapper 会在它被回收时
    # 连带关掉底层 buffer（在 pytest 等捕获环境里表现为 "I/O operation on closed file"）。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # pragma: no cover - stdout may not be reconfigurable
        pass

    wave = args.wave if args.wave.is_absolute() else Path.cwd() / args.wave
    if not wave.is_file():
        print(f"查询波不存在：{wave}", file=sys.stderr)
        return 2
    try:
        report, kept = check(wave, args.project_root.resolve())
    except (ValueError, OSError) as exc:
        # 参考集读不全就不给判定：半截索引的 PASS 是假绿。
        print(f"参考集读取失败（fail-closed）：{exc}", file=sys.stderr)
        return 2
    write_outputs(report, kept, args.report, args.filtered)
    print_report(report)
    return 1 if report["verdict"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
