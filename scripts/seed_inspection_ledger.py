# -*- coding: utf-8 -*-
"""活台账播种：把阶段二那次一次性 15,119 文件实测，升格成「在仓、可版本化、可 diff」的活台账首帧。

背景：真正跑完的存活实测（逐文件 Range-GET + size 一致性）落在 **git 仓库之外** 的
`阶段二产出/verification_progress.jsonl`（断点续跑真源，每行一个文件、带 md5/桶/状态/server_total/size_match）。
本脚本把它 **join** 进仓的文件级 manifest（`data/download_links.by_uid.json`），产出两件在仓工件：

  1. `data/inspection/snapshots/seed-<hash>.jsonl` —— **内容寻址、append-only** 的首帧快照
     （首行 `_meta`，其后每文件一行紧凑检查向量）。snapshot_id = 排序后逐行内容的 sha256 前 16 位：
     同一份实测重跑得同一个 id（可核验、可去重），两帧一 diff 即「存活变化报告」。
  2. `data/inspection/current.json` —— 运行时**物化视图**（by_uid → 逐文件最新状态），
     供 `inspection.py` 懒加载。事件日志(快照) + 物化视图(current) 是常规二元。

数据契约（v0，对齐 `docs/工作记录/下一阶段方向_能力化设计_2026-07-11.md` §5.5 四级阶梯）——逐文件检查向量：
  reachable : ok(HTTP 200/206) | dead(4xx/5xx) | unknown(网络错误/越界/未测)   —— 实测桶
  size      : match | mismatch | unknown   —— 服务器报告的总大小 vs 记录 bytes（integrity 的**廉价中间档**）
  integrity : 恒 unknown（md5 是 10x 声明值、**从未重算**；留待未来滚动抽样重算才升 verified/mismatch）
  load      : 恒 unknown（从未真加载）
  problem   := (reachable==dead) 或 (size==mismatch)   —— unknown 不算 problem（「不结论」≠「失效」）

诚实边界：这一步 **不重新实测**，只是把 2026-07-10 那帧洗干净、结构化、可 diff。真正的「保鲜」在 patrol_links.py。
硬约束：只读外部真源与在仓 manifest；**只写 `data/inspection/`**，绝不碰 `database/base/`、绝不改 manifest。

用法：
  py scripts/seed_inspection_ledger.py            # 生成首帧快照 + current.json
  py scripts/seed_inspection_ledger.py --dry-run  # 只打印统计，不落盘
环境变量（可选覆盖外部真源位置）：
  BIODATA_SEED_PROGRESS  默认 <workspace>/阶段二产出/verification_progress.jsonl
  BIODATA_SEED_LEDGER    默认 <workspace>/阶段二产出/verification_ledger.json（取权威快照时间戳）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

_AGENT_ROOT = Path(__file__).resolve().parent.parent          # agent/
_WORKSPACE = _AGENT_ROOT.parent                                # agent项目/
_MANIFEST = _AGENT_ROOT / "src" / "dataset_recommender" / "data" / "download_links.by_uid.json"
_OUT_DIR = _AGENT_ROOT / "src" / "dataset_recommender" / "data" / "inspection"
_SNAP_DIR = _OUT_DIR / "snapshots"

_SEED_PROGRESS = Path(os.environ.get(
    "BIODATA_SEED_PROGRESS", str(_WORKSPACE / "阶段二产出" / "verification_progress.jsonl")))
_SEED_LEDGER = Path(os.environ.get(
    "BIODATA_SEED_LEDGER", str(_WORKSPACE / "阶段二产出" / "verification_ledger.json")))

SCHEMA = "biodata-inspection/v0"


def norm(u: str) -> str:
    """折叠双斜杠伪迹、去 query/fragment、修转义——与阶段二 verify_all_links / progress.jsonl 同口径，
    保证 manifest 的 download_url 能与实测行的 url 精确 join。"""
    if not u or "://" not in u:
        return u or ""
    scheme, rest = u.split("://", 1)
    for sep in ("?", "#"):
        if sep in rest:
            rest = rest.split(sep, 1)[0]
    rest = re.sub(r"/{2,}", "/", rest)
    rest = rest.replace("\\u002F", "/").replace("\\/", "/")
    return f"{scheme}://{rest}"


def _reach_state(bucket: str | None) -> str:
    if bucket == "alive":
        return "ok"
    if bucket == "http_dead":
        return "dead"
    return "unknown"          # network_error（不结论）/ off_host / 缺行


def _size_state(server_total, rec_bytes) -> str:
    """从原始数字重判 size 档（不盲信 progress 里的 size_match 布尔——它把「无法解析」也记成 false，
    而「未知」不该被当「不一致」）。"""
    if server_total is None or rec_bytes is None:
        return "unknown"
    try:
        return "match" if int(server_total) == int(rec_bytes) else "mismatch"
    except (TypeError, ValueError):
        return "unknown"


def load_manifest_files() -> "list[tuple[str, str, str, int | None]]":
    """展开 manifest 的文件宇宙：(uid, norm_url, md5, rec_bytes)。只取有 download_url 的文件。"""
    by = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    out: list[tuple[str, str, str, int | None]] = []
    for uid, rec in by.items():
        if not isinstance(rec, dict):
            continue
        for f in rec.get("files", []) or []:
            if not isinstance(f, dict):
                continue
            url = norm(f.get("download_url", ""))
            if not url:
                continue
            out.append((uid, url, f.get("md5sum"), f.get("bytes")))
    return out


def load_probe_rows() -> "dict[str, dict]":
    """读外部实测真源 → key(uid\\turl) → 最新一行（同 key 后写覆盖，与阶段二聚合口径一致）。"""
    rows: dict[str, dict] = {}
    with _SEED_PROGRESS.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            k = r.get("key")
            if k:
                rows[k] = r
    return rows


def snapshot_date() -> str:
    """权威快照时间戳：取阶段二 ledger 的 snapshot_date（如 2026-07-10T20:11:47Z）；缺失则退到日期未知。"""
    try:
        led = json.loads(_SEED_LEDGER.read_text(encoding="utf-8"))
        sd = led.get("snapshot_date")
        if sd:
            return str(sd)
    except (OSError, json.JSONDecodeError):
        pass
    return "unknown"


def build(dry_run: bool = False) -> int:
    if not _SEED_PROGRESS.exists():
        print(f"[seed] 外部实测真源缺失：{_SEED_PROGRESS}\n"
              f"       这是一次性引导，需阶段二产出在场。可用 BIODATA_SEED_PROGRESS 指定其它位置。", file=sys.stderr)
        return 2
    if not _MANIFEST.exists():
        print(f"[seed] 文件级 manifest 缺失：{_MANIFEST}", file=sys.stderr)
        return 2

    files = load_manifest_files()
    probes = load_probe_rows()
    sd_iso = snapshot_date()
    sd_day = sd_iso[:10] if sd_iso != "unknown" else "unknown"

    snap_rows: list[dict] = []          # 紧凑快照行（内容寻址用）
    by_uid: dict[str, dict] = {}        # current.json 物化视图
    tally = {"files": 0, "reach_ok": 0, "reach_dead": 0, "reach_unknown": 0,
             "size_match": 0, "size_mismatch": 0, "size_unknown": 0, "problem": 0, "unprobed": 0}

    for uid, url, md5, rec_bytes in files:
        key = f"{uid}\t{url}"
        row = probes.get(key)
        if row is None:
            reach, http, srv, size = "unknown", None, None, "unknown"
            tally["unprobed"] += 1
        else:
            reach = _reach_state(row.get("bucket"))
            http = row.get("status")
            srv = row.get("server_total")
            size = _size_state(srv, rec_bytes)
        problem = (reach == "dead") or (size == "mismatch")

        tally["files"] += 1
        tally[f"reach_{reach}"] += 1
        tally[f"size_{size}"] += 1
        if problem:
            tally["problem"] += 1

        snap_rows.append({"k": key, "r": reach, "h": http, "s": size, "srv": srv})
        u = by_uid.setdefault(uid, {"lv": sd_day, "nf": 0, "np": 0, "f": {}})
        u["nf"] += 1
        if problem:
            u["np"] += 1
        # 逐文件紧凑向量：r=reachable, h=http, s=size, srv=server_total, v=last_verified；
        # problem/reason 由 inspection.py 在读取时派生，不落盘（省体积、单一真源）。
        u["f"][url] = {"r": reach, "h": http, "s": size, "srv": srv, "v": sd_day}

    # 内容寻址：排序后逐行关键字段拼串 → sha256 前 16 位。同份实测 → 同 id。
    canon = "\n".join(f'{r["k"]}|{r["r"]}|{r["h"]}|{r["s"]}|{r["srv"]}'
                      for r in sorted(snap_rows, key=lambda r: r["k"]))
    snap_id = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]

    meta = {
        "_meta": {
            "schema": SCHEMA, "snapshot_id": snap_id, "source": "seed",
            "snapshot_date": sd_iso, "generated_from": str(_SEED_PROGRESS.name),
            "n_files": tally["files"], "n_problem": tally["problem"], "tally": tally,
        }
    }

    print(f"[seed] snapshot_id={snap_id}  date={sd_iso}")
    print(f"[seed] 文件 {tally['files']} | reachable ok/dead/unknown="
          f"{tally['reach_ok']}/{tally['reach_dead']}/{tally['reach_unknown']} "
          f"| size match/mismatch/unknown={tally['size_match']}/{tally['size_mismatch']}/{tally['size_unknown']}")
    print(f"[seed] 问题文件(problem = dead 或 size-mismatch)：{tally['problem']}"
          + (f" | 未测 {tally['unprobed']}" if tally["unprobed"] else ""))

    if dry_run:
        print("[seed] --dry-run：不落盘。")
        return 0

    _SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snap_path = _SNAP_DIR / f"seed-{snap_id}.jsonl"
    with snap_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for r in sorted(snap_rows, key=lambda r: r["k"]):
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    current = {
        "schema": SCHEMA, "snapshot_id": snap_id, "snapshot_date": sd_iso,
        "source": "seed", "totals": tally, "by_uid": by_uid,
    }
    (_OUT_DIR / "current.json").write_text(
        json.dumps(current, ensure_ascii=False), encoding="utf-8")

    print(f"[seed] -> {snap_path.relative_to(_AGENT_ROOT)}")
    print(f"[seed] -> {(_OUT_DIR / 'current.json').relative_to(_AGENT_ROOT)}  (by_uid={len(by_uid)})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="活台账播种：阶段二 15k 实测 → 在仓首帧快照 + current.json")
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不落盘")
    args = ap.parse_args()
    return build(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
