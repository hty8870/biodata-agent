#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志 / 协作状态健康检查（BioData Agent）

三类受管对象，各有生命周期（详见 AGENTS.md §4「日志生命周期」）：

  1. 开发日志（开发日志归档/开发日志.md）—— **单一完整底稿**（2026-07-12 起：热日志 + 旧归档合并成
                       一份，不再分片，越靠上越新、永久保留）。本脚本只**报告**条目数，不再自动分片归档。
  2. 协同/认领/*.md —— 瞬时认领。合并后由属主即删；本脚本做**孤儿清扫**（git 里超 CLAIM_ORPHAN_DAYS
                       天没动的残留认领，默认只报告，加 --yes 才删）。_模板.md 永不动。
  3. 协同/交接/*.md —— 跨切移交。**从不按时间清**（防遗忘优先）；只在此列出「在途」数量。
  4. 阶段日志（阶段二产出/…）—— 粗粒度、稳定、自包含。**不回收**，且在 repo 外，脚本结构上够不着。

用法：
  py scripts/logrotate.py            # = --check：只报告，不改任何文件
  py scripts/logrotate.py --sweep-claims [--yes]   # 清扫陈旧孤儿认领（--yes 才真删）
  py scripts/logrotate.py --rotate   # 单文件模式下为**空操作**（仅提示：不再分片归档）

安全性：认领清扫默认 dry-run；交接目录只读不删；阶段日志完全不碰。所有写入 UTF-8 无 BOM、LF。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

# —— 可调阈值（改这里即可）——
LONG_LOG_NOTE = 250    # 开发日志条目数超过它 → 仅提示「文件较长，可考虑人工精简」，不做任何动作
CLAIM_ORPHAN_DAYS = 14 # 认领文件超过这么多天没在 git 里动过 = 孤儿候选

ROOT = Path(__file__).resolve().parent.parent      # 仓库根 = agent/
# 2026-07-12：开发日志合并为**单一底稿**（不再分热/归档两份），文件落在 开发日志归档/ 下。
DEVLOG = ROOT / "开发日志归档" / "开发日志.md"
CLAIMS_DIR = ROOT / "协同" / "认领"
HANDOFFS_DIR = ROOT / "协同" / "交接"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _split_entries(text: str) -> tuple[str, list[str]]:
    """把开发日志切成 (顶部 header, [条目块...])。条目 = 从一个 '## ' 行到下一个 '## ' 行前。"""
    lines = text.splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    if not starts:
        return text, []
    header = "".join(lines[: starts[0]])
    blocks: list[str] = []
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(lines)
        blocks.append("".join(lines[s:e]))
    return header, blocks


def _entry_date(block: str) -> str:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", block.splitlines()[0])
    return m.group(1) if m else "????-??-??"


def _git_last_commit_ts(rel: str) -> int | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "log", "-1", "--format=%ct", "--", rel],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        return int(out) if out else None
    except Exception:
        return None


def _claim_files() -> list[Path]:
    if not CLAIMS_DIR.is_dir():
        return []
    return sorted(p for p in CLAIMS_DIR.glob("*.md") if p.name != "_模板.md")


def _stale_claims() -> list[tuple[Path, float]]:
    now = time.time()
    out: list[tuple[Path, float]] = []
    for p in _claim_files():
        ts = _git_last_commit_ts(str(p.relative_to(ROOT)).replace("\\", "/"))
        if ts is None:
            continue  # 未入 git（新建未提交）→ 视为活动，不清
        age_days = (now - ts) / 86400
        if age_days > CLAIM_ORPHAN_DAYS:
            out.append((p, age_days))
    return out


def _handoff_files() -> list[Path]:
    if not HANDOFFS_DIR.is_dir():
        return []
    return sorted(p for p in HANDOFFS_DIR.glob("*.md") if p.name != "_模板.md")


def cmd_check() -> None:
    header, blocks = _split_entries(_read(DEVLOG)) if DEVLOG.exists() else ("", [])
    n = len(blocks)
    newest = _entry_date(blocks[0]) if blocks else "—"
    oldest = _entry_date(blocks[-1]) if blocks else "—"
    print("== 开发日志 (开发日志归档/开发日志.md · 单一底稿) ==")
    print(f"  条目数: {n}   ({oldest} … {newest}，越靠上越新，永久保留)")
    if n > LONG_LOG_NOTE:
        print(f"  → 文件较长（{n}>{LONG_LOG_NOTE} 条）：如需精简可人工处理；本脚本不再自动分片归档")
    else:
        print("  → 单文件模式：无需归档动作")

    claims = _claim_files()
    stale = _stale_claims()
    print("== 认领 (协同/认领/) ==")
    print(f"  活动认领: {len(claims)}   陈旧孤儿候选(>{CLAIM_ORPHAN_DAYS}天): {len(stale)}")
    for p, age in stale:
        print(f"    · {p.name}  ({age:.0f} 天未动) → --sweep-claims 可清")

    handoffs = _handoff_files()
    print("== 交接 (协同/交接/) ==")
    print(f"  在途移交: {len(handoffs)}   (交接从不按时间清；活做完由完成者删)")
    for p in handoffs:
        print(f"    · {p.name}")

    print("== 阶段日志 ==")
    print("  阶段二产出/开发日志_阶段二.md 等：粗粒度稳定日志 → 不回收、在 repo 外、本脚本不管辖")


def cmd_rotate() -> None:
    print("单文件模式：开发日志已合并为单一底稿（开发日志归档/开发日志.md），不再分片归档，--rotate 无操作。")
    print("如条目过多需精简，请人工判断后手动处理（保留完整叙事优先）。")


def cmd_sweep_claims(do_delete: bool) -> None:
    stale = _stale_claims()
    if not stale:
        print("无陈旧孤儿认领，无需清扫")
        return
    for p, age in stale:
        if do_delete:
            p.unlink()
            print(f"已删除孤儿认领: {p.name} ({age:.0f} 天未动)")
        else:
            print(f"[dry-run] 孤儿认领: {p.name} ({age:.0f} 天未动) —— 加 --yes 才真删")
    if not do_delete:
        print("（默认 dry-run；确认无误后加 --yes）")


def main() -> None:
    ap = argparse.ArgumentParser(description="BioData Agent 日志/协作状态健康检查")
    ap.add_argument("--rotate", action="store_true", help="单文件模式下为空操作（仅提示）")
    ap.add_argument("--sweep-claims", action="store_true", help="清扫陈旧孤儿认领")
    ap.add_argument("--yes", action="store_true", help="配合 --sweep-claims：真删（否则 dry-run）")
    args = ap.parse_args()

    if args.rotate:
        cmd_rotate()
    elif args.sweep_claims:
        cmd_sweep_claims(args.yes)
    else:
        cmd_check()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台中文不炸
    except Exception:
        pass
    main()
