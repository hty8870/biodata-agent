#!/usr/bin/env python3
"""biodata 装配器：骨架仓（knowledge-agent-private checkout）→ 本私仓骨架区覆盖。

拓扑（2026-09-23 起）：
- `knowledge-agent-private` = 骨架真源/项目本体（骨架在这里进化）。
- 本仓（biodata-agent-private）= 骨架 + biodata 肉的**装配体产品仓**：肉区（biodata-pack
  profile 文件集）是本仓真源；骨架区（skeleton profile 文件集）是骨架仓的**只读快照**，
  只许经本装配器写入（防双写漂移闸见下）。

用法：
    python scripts/assemble_biodata.py --skeleton-root <骨架仓本地路径> [--dry-run]
    python scripts/assemble_biodata.py --skeleton-root ... --write        # 真正覆盖骨架区
    python scripts/assemble_biodata.py --skeleton-root ... --write --allow-skeleton-drift

漂移闸（fail-closed）：本仓骨架区当前内容必须等于 `packaging/assembly.json` 记录的
上次装配快照；不等 = 骨架区被手工改过 → 拒绝覆盖并逐文件列出差异（防覆盖丢手改）。
`--allow-skeleton-drift` 是显式逃生口（理由应写进提交信息）。

路径映射：骨架仓文件路径 = skeleton profile 的目的地路径；本仓对应路径经 assembled
profile 的 mappings 反解（如 `.gitignore` ↔ `.gitignore.public`、
`automation/quality-gates.json` ↔ `automation/quality-gates.public.json`）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_public_mirror as mirror  # noqa: E402

ASSEMBLY_PATH = "packaging/assembly.json"
ASSEMBLY_SCHEMA = 1


class AssemblyError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    """文件内容哈希：文本按行尾归一化（\r\n→\n）后哈希——两个仓的 checkout 行尾策略
    可能不同（core.autocrlf），裸字节会假漂移；二进制原样。"""
    payload = path.read_bytes()
    if b"\0" not in payload:
        payload = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(payload).hexdigest()


def _skeleton_paths() -> list[str]:
    """骨架区在**骨架仓**里的路径集（= skeleton profile 目的地清单）。"""
    return mirror.load_output_paths(ROOT, "skeleton")


def _assembled_mappings() -> dict[str, str]:
    return mirror.load_policy(ROOT, "assembled")["mappings"]


def _private_path(dest: str, mappings: dict[str, str]) -> str:
    return mappings.get(dest, dest)


def _hash_if_file(path: Path) -> str | None:
    """存在才哈希；不存在返回 None（退场文件的漂移核查用）。"""
    return _sha256_file(path) if path.is_file() else None


def _removed_drift(private_root: Path, removed: list[str], previous: dict[str, str],
                   mappings: dict[str, str]) -> list[str]:
    """新清单里已退役、但本仓对应文件被本地改过的骨架路径。

    漂移检查只遍历新 `paths` 时，这一类文件会在 --write 时被直接删除、锚点刷新，
    手改内容无声蒸发——退场同样必须先过 fail-closed 漂移闸（--allow-skeleton-drift 才放行）。
    """
    out: list[str] = []
    for dest in removed:
        current = _hash_if_file(private_root / PurePosixPath(_private_path(dest, mappings)).as_posix())
        if current is not None and previous.get(dest) is not None and current != previous[dest]:
            out.append(dest)
    return out


def _current_skeleton_hashes(private_root: Path, paths: list[str], mappings: dict[str, str], *,
                             allow_missing: set[str] | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    for dest in paths:
        p = private_root / PurePosixPath(_private_path(dest, mappings)).as_posix()
        if not p.is_file():
            if not p.exists() and dest in (allow_missing or set()):
                continue  # 只有旧锚点未声明的新文件可以尚未存在。
            raise AssemblyError(f"骨架区文件缺失（本仓不完整？）: {_private_path(dest, mappings)}")
        out[dest] = _sha256_file(p)
    return out


def _load_assembly(private_root: Path) -> dict:
    path = private_root / ASSEMBLY_PATH
    if not path.is_file():
        raise AssemblyError(
            f"缺少 {ASSEMBLY_PATH}——本仓尚未建立装配锚点。先用 --init-assembly 以当前骨架区为基准建立。"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssemblyError(f"无法读取 {ASSEMBLY_PATH}") from exc
    if data.get("schema_version") != ASSEMBLY_SCHEMA:
        raise AssemblyError(f"不支持的 {ASSEMBLY_PATH} schema")
    if not isinstance(data.get("skeleton_files"), dict):
        raise AssemblyError(f"{ASSEMBLY_PATH} 缺 skeleton_files")
    return data


def _write_assembly(private_root: Path, *, skeleton_commit: str, skeleton_files: dict[str, str]) -> None:
    payload = {
        "schema_version": ASSEMBLY_SCHEMA,
        "skeleton_repository": "hty8870/knowledge-agent-private",
        "skeleton_source_commit": skeleton_commit,
        "skeleton_tree_sha256": hashlib.sha256(
            json.dumps(skeleton_files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "skeleton_files": skeleton_files,
    }
    target = private_root / ASSEMBLY_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8", newline="\n")


def _skeleton_head_commit(skeleton_root: Path) -> str:
    raw = subprocess.run(
        ["git", "-c", f"safe.directory={skeleton_root}", "-C", str(skeleton_root), "rev-parse", "HEAD"],
        capture_output=True, timeout=60,
    )
    if raw.returncode:
        raise AssemblyError(f"无法读取骨架仓 HEAD: {skeleton_root}")
    return raw.stdout.decode("utf-8").strip()


def assemble(private_root: Path, skeleton_root: Path, *, write: bool, allow_drift: bool,
             init_assembly: bool) -> int:
    paths = _skeleton_paths()
    mappings = _assembled_mappings()
    if not (skeleton_root / ".git").exists():
        raise AssemblyError(f"--skeleton-root 不是 git checkout: {skeleton_root}")

    missing_src = [d for d in paths if not (skeleton_root / PurePosixPath(d).as_posix()).is_file()]
    if missing_src:
        raise AssemblyError("骨架仓缺文件: " + ", ".join(missing_src[:10]))

    anchor = None if init_assembly else _load_assembly(private_root)
    previous = anchor["skeleton_files"] if anchor else {}
    current = _current_skeleton_hashes(
        private_root, paths, mappings,
        allow_missing=(set(paths) - set(previous)) if not init_assembly else None)
    incoming = {d: _sha256_file(skeleton_root / PurePosixPath(d).as_posix()) for d in paths}

    if init_assembly:
        if (private_root / ASSEMBLY_PATH).exists():
            raise AssemblyError(f"{ASSEMBLY_PATH} 已存在，--init-assembly 只用于首次建立")
        _write_assembly(private_root, skeleton_commit=_skeleton_head_commit(skeleton_root),
                        skeleton_files=current)
        print(f"assembly anchor initialized: {len(current)} skeleton files recorded")
        return 0

    drift = sorted(
        d for d in paths
        if previous.get(d) is not None and previous[d] != current.get(d)
    ) + sorted(d for d in paths if d not in previous and d in current and current[d] != incoming[d])
    removed = sorted(set(previous) - set(paths))
    retired_drift = sorted(set(_removed_drift(private_root, removed, previous, mappings)))
    drift = sorted(set(drift) | set(retired_drift))
    if drift and not allow_drift:
        detail = "\n  - ".join(
            f"{d}（本次将从骨架区退场，但本地已修改）" if d in retired_drift else d
            for d in drift[:30]
        )
        raise AssemblyError(
            "本仓骨架区与上次装配快照不一致（疑似手工改过骨架区），拒绝覆盖/删除。"
            "逐文件差异见下；确认无误后用 --allow-skeleton-drift 显式覆盖：\n  - "
            + detail
        )

    to_update = sorted(d for d in paths if incoming[d] != current.get(d))
    print(f"skeleton: {anchor.get('skeleton_source_commit', '?')[:12]} → {_skeleton_head_commit(skeleton_root)[:12]}")
    print(f"skeleton area: {len(to_update)} file(s) to update, {len(removed)} to remove"
          + (f", drift overridden: {len(drift)}" if drift else ""))

    if not write:
        for d in to_update[:30]:
            action = "add" if d not in current else "update"
            print(f"  {action}: {_private_path(d, mappings)}")
        for d in removed[:10]:
            print(f"  remove: {_private_path(d, mappings)}")
        print("dry-run（未写盘；加 --write 才覆盖）")
        return 0

    for dest in to_update:
        src = skeleton_root / PurePosixPath(dest).as_posix()
        dst = private_root / PurePosixPath(_private_path(dest, mappings)).as_posix()
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    for dest in removed:
        dst = private_root / PurePosixPath(_private_path(dest, mappings)).as_posix()
        if dst.is_file():
            dst.unlink()

    new_current = _current_skeleton_hashes(private_root, paths, mappings)
    _write_assembly(private_root, skeleton_commit=_skeleton_head_commit(skeleton_root),
                    skeleton_files=new_current)
    print(f"assembled: {len(to_update)} updated, {len(removed)} removed; anchor {ASSEMBLY_PATH} refreshed")
    print("下一步：跑质量门 → 提交本仓 → build_public_mirror validate/apply 公开镜像")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把骨架仓内容装配进 biodata-agent-private 的骨架区")
    parser.add_argument("--skeleton-root", type=Path, required=True)
    parser.add_argument("--write", action="store_true", help="真正覆盖骨架区（缺省 dry-run 只验不写）")
    parser.add_argument("--allow-skeleton-drift", action="store_true",
                        help="显式允许覆盖「骨架区已被手工改动」的状态（理由写进提交信息）")
    parser.add_argument("--init-assembly", action="store_true",
                        help="首次建立装配锚点（以当前骨架区为基准；只建一次）")
    args = parser.parse_args(argv)
    try:
        return assemble(ROOT, args.skeleton_root.resolve(), write=bool(args.write),
                        allow_drift=bool(args.allow_skeleton_drift), init_assembly=bool(args.init_assembly))
    except AssemblyError as exc:
        print(f"assemble failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
