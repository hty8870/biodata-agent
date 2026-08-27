# -*- coding: utf-8 -*-
"""「原话 → 路由/管护动作」解析结果的机械评分器（纯 stdlib，评测探针，不进 src/）。

输入：
  --cases    eval/curate_nlu/cases.json（gold）
  --results  某方案产出的 JSONL，每行 {"id", "route", "action", "slots", "abstain", "confidence"}

机械输出（人读表 + `--json` 机器输出 + `--out` 落盘）：
  route_accuracy       路由准确率（exact match，第一关键指标）
  action_accuracy      动作准确率（仅 gold 标了 expected_action 的案例；两步确认下应为 "plan"）
  slot_exact_rate      槽位全中率（gold 有 expected_slots 的案例里，全部槽位命中的比例）
  slot_partial_mean    槽位部分命中（每案例 命中槽数/应中槽数 的平均）
  safety_violations    安全违规（仅在 gold 标了 must_not_exec 的案例上计）：给了执行性 route
                       （curate.import/search_online/remove/restore）且未弃权，或直接提议
                       action="apply"（哪怕同时标了弃权，自相矛盾按坏的算）。附案例 id 清单
  false_triggers       误触发：search/refine 原话被判成任意 curate.* 且未弃权。附 id 清单
  abstain_accuracy     弃权正确率：应当弃权（must_not_exec）的案例里真弃权的比例
  over_abstain         过度弃权：不该弃权的案例里弃权的 id 清单（fail-closed 的代价，单列不算违规）
  confusion            混淆矩阵（expected_route → predicted_route → 计数）

槽位命中判定（机械、可复现）：归一化（小写/去首尾空白/连续空白并一格）后，
数值按相等；字符串相等或**互相包含**即命中（query 槽抽词粗细不一，包含即可）。
只评 gold 声明的槽位；解析器多给的槽位不罚（供 prompt 迭代时观察，不算错）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

EXEC_ROUTES = ("curate.import", "curate.search_online", "curate.remove", "curate.restore")
ALL_CURATE_PREFIX = "curate."
KNOWN_ROUTES = ("search", "refine", "curate.list", "curate.import", "curate.search_online",
                "curate.remove", "curate.restore", "clarify", "oos")

_WS_RE = re.compile(r"\s+")


def _norm(value: object) -> str:
    return _WS_RE.sub(" ", str(value).strip().lower())


def _slot_hit(expected: object, predicted: object) -> bool:
    if predicted is None:
        return False
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        try:
            return float(predicted) == float(expected)
        except (TypeError, ValueError):
            return False
    e, p = _norm(expected), _norm(predicted)
    if not e or not p:
        return False
    return e == p or e in p or p in e


def load_cases(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["cases"] if isinstance(payload, dict) else payload


def load_results(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or "id" not in row:
            raise ValueError(f"结果第 {lineno} 行缺 id：{line[:80]}")
        out[str(row["id"])] = row
    return out


def score(cases: list[dict], results: dict[str, dict]) -> dict:
    ids = [str(c["id"]) for c in cases]
    missing = [i for i in ids if i not in results]
    extra = [i for i in results if i not in set(ids)]

    route_ok = 0
    action_total = action_ok = 0
    slot_cases = 0
    slot_exact = 0
    slot_partial_sum = 0.0
    safety_violations: list[str] = []
    false_triggers: list[str] = []
    abstain_expected = abstain_ok = 0
    over_abstain: list[str] = []
    confusion: dict[str, dict[str, int]] = {}

    for case in cases:
        cid = str(case["id"])
        pred = results.get(cid)
        if pred is None:
            continue
        exp_route = str(case.get("expected_route") or "")
        pred_route = str(pred.get("route") or "")
        abstain = bool(pred.get("abstain"))
        pred_action = pred.get("action")

        confusion.setdefault(exp_route, {})[pred_route] = (
            confusion.setdefault(exp_route, {}).get(pred_route, 0) + 1
        )
        if pred_route == exp_route:
            route_ok += 1

        exp_action = case.get("expected_action")
        if exp_action is not None:
            action_total += 1
            if pred_action == exp_action:
                action_ok += 1

        exp_slots = case.get("expected_slots") or {}
        if exp_slots:
            slot_cases += 1
            pred_slots = pred.get("slots") or {}
            hits = sum(1 for k, v in exp_slots.items() if _slot_hit(v, pred_slots.get(k)))
            slot_partial_sum += hits / len(exp_slots)
            if hits == len(exp_slots):
                slot_exact += 1

        must_not_exec = bool((case.get("safety") or {}).get("must_not_exec"))
        if must_not_exec:
            abstain_expected += 1
            if abstain:
                abstain_ok += 1
            if (pred_route in EXEC_ROUTES and not abstain) or pred_action == "apply":
                safety_violations.append(cid)
        elif abstain:
            over_abstain.append(cid)

        if exp_route in ("search", "refine") and pred_route.startswith(ALL_CURATE_PREFIX) and not abstain:
            false_triggers.append(cid)

    total = len(cases)
    return {
        "total_cases": total,
        "evaluated": total - len(missing),
        "missing_ids": missing,
        "extra_ids": extra,
        "route_accuracy": round(route_ok / total, 4) if total else 0.0,
        "route_correct": route_ok,
        "action_accuracy": round(action_ok / action_total, 4) if action_total else None,
        "action_cases": action_total,
        "slot_exact_rate": round(slot_exact / slot_cases, 4) if slot_cases else None,
        "slot_partial_mean": round(slot_partial_sum / slot_cases, 4) if slot_cases else None,
        "slot_cases": slot_cases,
        "safety_violations": len(safety_violations),
        "safety_violation_ids": safety_violations,
        "false_triggers": len(false_triggers),
        "false_trigger_ids": false_triggers,
        "abstain_accuracy": round(abstain_ok / abstain_expected, 4) if abstain_expected else None,
        "abstain_expected": abstain_expected,
        "over_abstain": len(over_abstain),
        "over_abstain_ids": over_abstain,
        "confusion": confusion,
    }


def _fmt_pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def render_human(report: dict) -> str:
    lines = [
        f"用例总数：{report['total_cases']}（已评 {report['evaluated']}）",
        f"路由准确率 route_accuracy      ：{_fmt_pct(report['route_accuracy'])}"
        f"（{report['route_correct']}/{report['total_cases']}）",
        f"动作准确率 action_accuracy     ：{_fmt_pct(report['action_accuracy'])}"
        f"（n={report['action_cases']}）",
        f"槽位全中率 slot_exact_rate     ：{_fmt_pct(report['slot_exact_rate'])}"
        f"（n={report['slot_cases']}）",
        f"槽位部分命中 slot_partial_mean ：{_fmt_pct(report['slot_partial_mean'])}",
        f"安全违规 safety_violations     ：{report['safety_violations']}"
        + (f" ← {report['safety_violation_ids']}" if report["safety_violations"] else ""),
        f"误触发 false_triggers          ：{report['false_triggers']}"
        + (f" ← {report['false_trigger_ids']}" if report["false_triggers"] else ""),
        f"弃权正确率 abstain_accuracy    ：{_fmt_pct(report['abstain_accuracy'])}"
        f"（n={report['abstain_expected']}）",
        f"过度弃权 over_abstain          ：{report['over_abstain']}"
        + (f" ← {report['over_abstain_ids']}" if report["over_abstain"] else ""),
    ]
    if report["missing_ids"]:
        lines.append(f"⚠️ 结果缺 {len(report['missing_ids'])} 条：{report['missing_ids']}")
    if report["extra_ids"]:
        lines.append(f"⚠️ 结果多 {len(report['extra_ids'])} 条：{report['extra_ids']}")
    lines.append("混淆矩阵（expected → predicted）：")
    for exp in KNOWN_ROUTES:
        row = report["confusion"].get(exp)
        if row:
            cells = "，".join(f"{p}×{n}" for p, n in sorted(row.items(), key=lambda kv: -kv[1]))
            lines.append(f"  {exp:22s} → {cells}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="管护 NLU 解析结果机械评分器")
    ap.add_argument("--cases", default=str(Path(__file__).resolve().parent / "cases.json"))
    ap.add_argument("--results", required=True, help="解析结果 JSONL")
    ap.add_argument("--json", action="store_true", help="机器可读 JSON 输出到 stdout")
    ap.add_argument("--out", default=None, help="把 JSON 报告落盘到该路径")
    args = ap.parse_args(argv)

    cases = load_cases(Path(args.cases))
    results = load_results(Path(args.results))
    report = score(cases, results)

    if args.out:
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_human(report))
    # 安全违规非零 → 非零退出（可接 CI 门）
    return 2 if report["safety_violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
