# -*- coding: utf-8 -*-
"""联网工具组去留裁决实测（2026-08-03 P1-B5）：通用搜索（DuckDuckGo，主力）vs 官方源适配器（对照）。

对每个源（10x / ENCODE / ArrayExpress）各跑 ≥20 次采样（典型查询 + corner case：空关键词、
生僻词、超长词、特殊字符、中英文混合），分别测「通用搜索」与「官方适配器」的成功率与延迟，
输出对比表（控制台 + JSON 落盘 协同/eval_net_tools_2026-08-03.json）。

裁决规则（任务书给定）：
  通用搜索成功率 ≥85% 且 p95 ≤15s → 可删适配器；否则两者并存。
  本机不可达某通道 → 如实记录错误形态，该结论本身即裁决依据（必然并存）。

**这是真联网脚本**（不是测试）：走 corpus_net 统一出口（限速 + 请求账本纪律不变），
账本记在仓库 .userdata/curate_net_ledger.jsonl（gitignore 的运行产物）。

用法：./.venv/Scripts/python.exe scripts/eval_net_tools.py [--per-source N]（默认 20+，含 corner case）
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.corpus import corpus_net  # noqa: E402

#: 每个源的典型查询（与该源的数据领域对齐，模拟真实管护问法）。
TYPICAL_QUERIES: dict[str, list[str]] = {
    "10x": ["visium lung", "chromium pbmc", "xenium breast", "visium hd mouse brain"],
    "encode": ["ChIP-seq human lung", "RNA-seq mouse brain", "ATAC-seq", "histone modification"],
    "arrayexpress": ["single cell lung", "human brain scrna", "mouse kidney", "covid pbmc"],
}

#: corner case 查询（所有源通用）：空关键词、生僻词、超长词、特殊字符、中英文混合。
CORNER_QUERIES = [
    "",
    "xyzzyqwkjv 生僻词不存在的",
    "a" * 300,
    "!@#$%^&*()_+-=[]{}|;:'\",.<>/?\\",
    "human lung 人类肺 single cell",
]


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


def _sample(channel: str, source: str, query: str, *, project_root: Path) -> dict:
    """跑一次采样：channel=generic（DDG 通用搜索，query 加 site: 约束对齐源）或 adapter（官方适配器）。

    通用搜索加 site: 约束的原因：裁决比的是「能不能靠通用搜索发现某源的数据」，
    裸关键词的 DDG 结果混着全网噪声，加 site: 才是与适配器等价的对比口径。"""
    if channel == "generic":
        site = {
            "10x": "site:10xgenomics.com",
            "encode": "site:encodeproject.org",
            "arrayexpress": "site:ebi.ac.uk",
        }[source]
        q = f"{query} {site}".strip()
        started = time.monotonic()
        res = corpus_net.search_online_source("ddg", q, limit=10, project_root=project_root)
    else:
        started = time.monotonic()
        res = corpus_net.search_online_source(source, query, limit=10, project_root=project_root)
    ms = (time.monotonic() - started) * 1000.0
    # 成功口径：ok=True 且真的拿到条目（no_results 也算一次失败——用户视角就是没搜到；
    # 但空关键词是刻意的 corner case，单独标注不计入成功率分母之外，见汇总）。
    n_items = len(res.get("items") or [])
    return {
        "channel": channel,
        "source": source,
        "query": query,
        "ok": bool(res.get("ok")) and n_items > 0,
        "raw_ok": bool(res.get("ok")),
        "items": n_items,
        "error": res.get("error") or "",
        "note_zh": res.get("note_zh") or "",
        "ms": round(ms, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="联网工具组去留裁决实测")
    parser.add_argument("--per-source", type=int, default=20,
                        help="每个源每通道的采样次数下限（默认 20，典型查询循环 + corner case）")
    args = parser.parse_args()

    sources = ["10x", "encode", "arrayexpress"]
    results: list[dict] = []
    for source in sources:
        queries = list(TYPICAL_QUERIES[source])
        plan: list[str] = []
        while len(plan) < max(args.per_source - len(CORNER_QUERIES), 0):
            plan.extend(queries)
        plan = plan[: max(args.per_source - len(CORNER_QUERIES), 0)] + CORNER_QUERIES
        for channel in ("generic", "adapter"):
            for q in plan:
                r = _sample(channel, source, q, project_root=ROOT)
                results.append(r)
                tag = "OK " if r["ok"] else "FAIL"
                print(f"[{tag}] {channel:7s} {source:12s} {r['ms']:8.0f}ms "
                      f"items={r['items']:2d} {r['error'] or '-':15s} q={q[:40]!r}", flush=True)

    # ---- 汇总：成功率分母剔除空关键词（那是参数校验用例，不是通道能力）----------------------
    summary: dict[str, dict] = {}
    for source in sources:
        for channel in ("generic", "adapter"):
            rows = [r for r in results if r["source"] == source and r["channel"] == channel]
            effective = [r for r in rows if r["query"] != ""]
            ok_rows = [r for r in effective if r["ok"]]
            lat = [r["ms"] for r in effective]
            errors: dict[str, int] = {}
            for r in effective:
                if not r["ok"]:
                    key = r["error"] or "empty_items"
                    errors[key] = errors.get(key, 0) + 1
            summary[f"{channel}:{source}"] = {
                "runs": len(effective),
                "success": len(ok_rows),
                "success_rate": round(len(ok_rows) / len(effective), 4) if effective else 0.0,
                "mean_ms": round(statistics.fmean(lat), 1) if lat else 0.0,
                "p50_ms": round(_percentile(lat, 50), 1),
                "p95_ms": round(_percentile(lat, 95), 1),
                "error_kinds": errors,
            }

    generic_rates = [v["success_rate"] for k, v in summary.items() if k.startswith("generic:")]
    generic_p95 = max((v["p95_ms"] for k, v in summary.items() if k.startswith("generic:")), default=0.0)
    generic_rate = statistics.fmean(generic_rates) if generic_rates else 0.0
    can_drop_adapters = generic_rate >= 0.85 and generic_p95 <= 15000.0
    verdict = (
        f"通用搜索平均成功率 {generic_rate:.1%}（门槛 85%），p95 {generic_p95:.0f}ms（门槛 15s）→ "
        + ("通用搜索达标，可考虑删适配器（仍需人工确认解析稳定性）。" if can_drop_adapters
           else "通用搜索未达标，裁决：通用搜索与官方适配器两者并存。")
    )

    print("\n===== 对比表 =====")
    print(f"{'通道:源':24s} {'次数':>4s} {'成功率':>8s} {'均值ms':>8s} {'p50ms':>8s} {'p95ms':>8s}  错误形态")
    for key, v in summary.items():
        print(f"{key:24s} {v['runs']:4d} {v['success_rate']:8.1%} {v['mean_ms']:8.0f} "
              f"{v['p50_ms']:8.0f} {v['p95_ms']:8.0f}  {json.dumps(v['error_kinds'], ensure_ascii=False)}")
    print("\n裁决：" + verdict)

    out = {
        "date": "2026-08-03",
        "per_source": args.per_source,
        "verdict_rule": "通用搜索成功率 ≥85% 且 p95 ≤15s → 可删适配器；否则并存",
        "summary": summary,
        "generic_avg_success_rate": round(generic_rate, 4),
        "generic_worst_p95_ms": generic_p95,
        "can_drop_adapters": can_drop_adapters,
        "verdict_zh": verdict,
        "raw": results,
    }
    out_path = ROOT / "协同" / "eval_net_tools_2026-08-03.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始数据已落盘：{out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
