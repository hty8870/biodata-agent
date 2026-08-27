# -*- coding: utf-8 -*-
"""长程多步执行的**真 LLM + 真网络**端到端验证（2026-08-04，设计_长程多步执行_2026-08-04.md）。

两个病例（全部真跑，结果原样打印，成败如实报）：

A. 「检查ArrayExpress是否有更新，若有新的人类肺数据就联网搜来入库」
   期望：两步链 check_updates → search_online 真入库——tmp 根下出现新的外部库文件 +
   账本（tmp/.userdata/curate_net_ledger.jsonl）有 agent_exec 两行。
   （tmp 的 AE 快照刻意裁到前 5 条：让「线上最近 10 条」大概率呈现疑似新增，
   条件分支才会真走到第二步。decide 若判断条件不成立而 done，也如实打印——那是合法行为。）
B. 「检查10x是否有更新，若有则下载下来」（用户病例句）
   期望：steps = [check_updates 成功, search_online 失败（source_not_registered）]
   或 decide 判断无法执行而 done；report_zh 如实说明「10x 暂未接入联网入库」。

纪律：project_root 一律用 tmp 临时目录（database 相关文件临时拷贝/裁剪），**绝不污染真实库**；
LLM key 由 llm_client 自己从项目根 .env 读，本脚本不接触、不打印。
用法：./.venv/Scripts/python.exe scripts/sim_multi_step_exec.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.agent import agent_exec  # noqa: E402
from dataset_recommender.llm.llm_client import load_llm_config  # noqa: E402

CASE_A = "检查ArrayExpress是否有更新，若有新的人类肺数据就联网搜来入库"
#: A 的条件（「新的人类肺数据」）是否成立取决于线上最近条目的真实主题——不是我们能左右的。
#: A2 把条件改成「有新数据」（本地快照裁到 5 条后该条件几乎必然成立），用来验证两步链真入库。
CASE_A2 = "检查ArrayExpress是否有更新，若有新数据就把人类肺的数据联网搜来入库"
CASE_B = "检查10x是否有更新，若有则下载下来"


def make_tmp_root() -> Path:
    """tmp 项目根：只拷/裁 check_updates 要读的本地快照，其余目录留空（工具如实降级）。"""
    tmp = Path(tempfile.mkdtemp(prefix="biodata_loop_"))
    (tmp / "database" / "external").mkdir(parents=True)
    (tmp / "database" / "base").mkdir(parents=True)
    ae_path = ROOT / "database" / "external" / "arrayexpress.json"
    ae = json.loads(ae_path.read_text(encoding="utf-8"))
    records = list(ae.get("records") or [])
    ae["records"] = records[:5]           # 裁到 5 条：线上最近条目大概率全是「疑似新增」
    ae["record_count"] = len(ae["records"])
    (tmp / "database" / "external" / "arrayexpress.json").write_text(
        json.dumps(ae, ensure_ascii=False), encoding="utf-8")
    shutil.copy2(ROOT / "database" / "base" / "10x-Visium.json",
                 tmp / "database" / "base" / "10x-Visium.json")
    return tmp


def run_case(utterance: str, tmp: Path, config) -> dict:
    agent_exec._agent_project_root = lambda: tmp   # 图内工具/审计的项目根重定向到 tmp
    events = []

    def on_event(kind, entry):
        events.append(entry)
        print(f"  [trace] {entry['label_zh']} · ok={entry['ok']} · {entry['detail']}")

    plan, trace = agent_exec.plan_with_agent_events(
        utterance,
        has_results=False, result_total=0, config=config,
        retrieval=None, current_query="", current_filters=None,
        on_event=on_event,
    )
    return plan


def show_plan(plan: dict) -> None:
    print(f"  plan.verb = {plan.get('verb')}（{plan.get('verb_zh')}） source={plan.get('source')}")
    for i, s in enumerate(plan.get("steps") or [], 1):
        if s.get("ok"):
            r = s.get("result") or {}
            brief = {k: r.get(k) for k in ("record_count", "filename", "query", "source_label")
                     if r.get(k) is not None}
            if s.get("card_kind") == "check_updates":
                brief = {"sources": [
                    {k: e.get(k) for k in ("source", "mode", "local_count", "online_recent", "new_count")
                     if e.get(k) is not None}
                    for e in (r.get("sources") or [])
                ]}
            print(f"  step{i}: {s['verb']} ok=True {json.dumps(brief, ensure_ascii=False)[:400]}")
        else:
            print(f"  step{i}: {s['verb']} ok=False error={s.get('error')}")
    print(f"  report_zh = {plan.get('report_zh')}")


def ledger_rows(tmp: Path) -> list[dict]:
    path = tmp / ".userdata" / "curate_net_ledger.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def main() -> int:
    config = load_llm_config(project_root=ROOT)
    if not config.api_key:
        print("未配 LLM key（项目根 .env），真机验证无法跑。")
        return 2
    tmp = make_tmp_root()
    print(f"tmp project_root = {tmp}")
    ext_dir = tmp / "database" / "external"

    print(f"\n===== 病例 A：{CASE_A} =====")
    plan_a = run_case(CASE_A, tmp, config)
    show_plan(plan_a)
    new_files = sorted(p.name for p in ext_dir.glob("upload_*.json"))
    rows_a = ledger_rows(tmp)
    print(f"  外部库新文件 = {new_files}")
    print(f"  账本行（{len(rows_a)}）："
          + json.dumps([{k: r.get(k) for k in ('endpoint', 'ok', 'error')} for r in rows_a],
                       ensure_ascii=False))
    steps_a = plan_a.get("steps") or []
    a_ok = (
        len(steps_a) == 2
        and steps_a[0]["verb"] == "curate.check_updates" and steps_a[0]["ok"]
        and steps_a[1]["verb"] == "curate.search_online" and steps_a[1]["ok"]
        and bool(new_files)
        and any(r.get("endpoint") == "agent_exec:curate.search_online" and r.get("ok") for r in rows_a)
    )
    print(f"  => 病例A（原句，条件是否成立看线上真实条目）：{'两步链真入库 PASS' if a_ok else 'decide 判条件不成立而 done（合法行为，见上方如实输出）'}")
    if not a_ok:
        print(f"\n===== 病例 A2（条件必然成立的变体）：{CASE_A2} =====")
        plan_a2 = run_case(CASE_A2, tmp, config)
        show_plan(plan_a2)
        new_files = sorted(p.name for p in ext_dir.glob("upload_*.json"))
        rows_a = ledger_rows(tmp)
        print(f"  外部库新文件 = {new_files}")
        steps_a = plan_a2.get("steps") or []
        a_ok = (
            len(steps_a) == 2
            and steps_a[0]["verb"] == "curate.check_updates" and steps_a[0]["ok"]
            and steps_a[1]["verb"] == "curate.search_online" and steps_a[1]["ok"]
            and bool(new_files)
            and any(r.get("endpoint") == "agent_exec:curate.search_online" and r.get("ok")
                    for r in rows_a)
        )
        print(f"  => 病例A2（两步链真入库）：{'PASS' if a_ok else 'FAIL'}")

    print(f"\n===== 病例 B（用户病例句）：{CASE_B} =====")
    plan_b = run_case(CASE_B, tmp, config)
    show_plan(plan_b)
    steps_b = plan_b.get("steps") or []
    rows_b = ledger_rows(tmp)
    honest_gap = any(
        (not s.get("ok")) and "source_not_registered" in str(s.get("error") or "")
        for s in steps_b
    )
    report_b = str(plan_b.get("report_zh") or "")
    false_claim = agent_exec._report_contradicts_steps(report_b, steps_b)
    b_ok = (
        steps_b and steps_b[0]["verb"] == "curate.check_updates" and steps_b[0]["ok"]
        and (honest_gap or len(steps_b) == 1)   # 失败如实记 或 decide 判断做不到而 done
        and ("10x" in report_b)
        and not false_claim                      # 汇报绝不谎称做了没做的下载/入库
    )
    print(f"  => 病例B（如实降解：10x 暂未接入联网入库）：{'PASS' if b_ok else 'FAIL（见上方如实输出）'}")
    return 0 if (a_ok and b_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
