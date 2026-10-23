#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把发布候选包 + PDF 装配成全平台（Windows/macOS/Linux）命令行交付包。

定位：zip 版安装包——三平台通用、命令行/脚本启动、前端依赖浏览器
（Windows 图形化 webview 安装版走 Inno exe，不在本装配范围）。
解压后点一个入口脚本就全流程启动：Windows 双击 打开前端.bat、
macOS 双击 打开前端.command、Linux 终端 sh 打开前端.sh。

用法：
  <python> packaging/delivery-posix/assemble.py <rc_zip> <pdf> [-o <out_dir>] [--date <YYYY-MM-DD>]

输入：
  rc_zip  scripts/build_release.py build 产出的发布候选包（zip，含 allowlist 源码树）。
  pdf     「BioData Agent 使用说明书.pdf」的任意路径（输出时保留其原名）。

输出（out_dir 默认仓库根的 dist/）：
  biodata-agent-<YYYY-MM-DD>/
    ├─ 从这里开始.txt                三平台说明
    ├─ 打开前端.bat                  Windows 双击入口
    ├─ 打开前端.sh                   Linux/通用入口（sh 前端根定位 + exec bash 引擎）
    ├─ 打开前端.command              macOS 双击入口（内容同 .sh）
    ├─ BioData Agent 使用说明书.pdf  原样拷贝
    └─ biodata-agent/                RC zip 解出的源码树（exec 位保留）
  以及同名的 .zip（用 python zipfile 手动控制 external_attr，保证 .sh/.command 的 exec 位）。

设计：纯标准库、确定性。exec 位不从磁盘 mode 读（Windows 磁盘 mode 不可靠），而是：
  * 顶层入口脚本 .sh/.command 一律 0o100755，.bat/文本/PDF 一律 0o100644；
  * 从 RC 解出的文件复用其 zip 条目自带的 mode（build_release 已把可执行条目写成 0o100755）。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import build_release  # noqa: E402
DELIVERY_DIR_NAME_PREFIX = "biodata-agent-"
VERSION = 1

#: 交付目录所有权指纹：只在成功装配时写入，记录**完整产物树**（相对路径 -> sha256）与
#: PDF 名单。重建前必须逐路径/逐摘要递归对账——顶层白名单不够：src/web/database 子目录里
#: 用户新增、字节改动或额外 PDF 都会被 rmtree 一起带走。任何多出/改动/缺失/链接 → 默认 refuse。
#: schema 2 = 完整树清单（v1 的顶层白名单版本不再接受重建；无指纹一律 refuse）。
OWNER_MARKER = ".delivery-owner.json"
OWNER_GENERATOR = "packaging/delivery-posix/assemble.py"
OWNER_SCHEMA = 2
# 顶层文件的字节 mode（不依赖磁盘 mode，Windows 下磁盘 mode 不可靠）。
TOP_LEVEL_MODES = {
    "从这里开始.txt": 0o100644,
    "打开前端.bat": 0o100644,
    "打开前端.sh": 0o100755,
    "打开前端.command": 0o100755,
}
# dispatch by suffix：PDF 以及其他顶层拷贝统一 644。
TOP_LEVEL_DEFAULT_MODE = 0o100644

# RC zip 里这些条目不进交付（构建记录/内部工件，不需要随客户源码树外发）。
RC_EXCLUDED = frozenset({"release-manifest.json"})

FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


def _zip_info(relative: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(relative, date_time=FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (mode & 0xFFFF) << 16
    info.flag_bits |= 0x800
    return info


def _mode_from_zipinfo(info: zipfile.ZipInfo, default: int = 0o100644) -> int:
    mode = (info.external_attr >> 16) & 0xFFFF
    if mode == 0:
        return default
    return mode


def _rc_entry_modes(rc_zip: Path) -> dict[str, int]:
    """读 RC zip，返回「zip 内相对路径 -> 字节 mode」（缺失给 0o100644）。"""
    modes: dict[str, int] = {}
    with zipfile.ZipFile(rc_zip, "r") as bundle:
        for info in bundle.infolist():
            if info.is_dir():
                continue
            # 转正斜杠，统一纯相对路径。
            relative = info.filename.replace("\\", "/")
            if relative in RC_EXCLUDED:
                continue
            modes[relative] = _mode_from_zipinfo(info)
    return modes


def _safe_join(base: Path, relative: str) -> Path:
    """共用的落点检查：规范相对路径、无链接组件、最终包含关系。"""
    pure = build_release._validate_relative(relative)
    base = base.resolve()
    target = base
    for part in pure.parts:
        target = target / part
        if build_release._is_link_like(target):
            raise ValueError(f"linked delivery path: {relative!r}")
    resolved = target.resolve()
    if resolved == base or not resolved.is_relative_to(base):
        raise ValueError(f"delivery path escapes output directory: {relative!r}")
    return resolved


def _tree_manifest(root: Path) -> "dict[str, str] | None":
    """递归产物树清单：相对 POSIX 路径 -> sha256（跳过根上的所有权指纹自身）。

    任何符号链接/junction、非普通文件或不可读 -> None（不跟随、不归档，调用方 refuse）。"""
    files: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames):
            if build_release._is_link_like(Path(dirpath) / name):
                return None
        for name in filenames:
            if name == OWNER_MARKER and Path(dirpath) == root:
                continue
            path = Path(dirpath) / name
            if build_release._is_link_like(path) or not path.is_file():
                return None
            try:
                payload = path.read_bytes()
            except OSError:
                return None
            files[path.relative_to(root).as_posix()] = hashlib.sha256(payload).hexdigest()
    return files


def _publish_archive(tmp_archive: Path, archive: Path) -> None:
    """原子发布 zip：落点若是链接/junction 先拒（不覆盖他人目标）；异常只清自己的临时档。"""
    try:
        if build_release._is_link_like(archive):
            raise ValueError(f"refusing to replace linked archive path: {archive}")
        os.replace(tmp_archive, archive)
    finally:
        try:
            tmp_archive.unlink(missing_ok=True)
        except OSError:
            pass


def _assert_owned_or_absent(delivery_dir: Path, date_str: str) -> None:
    """再次装配前的**完整树**所有权核验：对不上就 refuse（绝不 rmtree 用户内容）。

    对账 = 路径集合逐项相等 + 逐文件摘要相同 + 树内无任何符号链接/junction，
    PDF 以上次记录的名单为准（额外 PDF = 用户内容）。只有真正未被改动过的旧产物可重建。
    """
    if not delivery_dir.exists():
        return
    marker = delivery_dir / OWNER_MARKER
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if (not isinstance(data, dict) or data.get("generator") != OWNER_GENERATOR
            or data.get("schema_version") != OWNER_SCHEMA):
        raise ValueError(
            f"refusing to delete unowned delivery directory: {delivery_dir}"
            f"（缺 {OWNER_MARKER} 完整所有权指纹，可能是用户目录或含输入材料/旧版产物）。"
            "请改用新的输出目录，或人工确认后手动清理。")
    recorded = data.get("files")
    if not isinstance(recorded, dict) or not recorded:
        raise ValueError(f"refusing to delete {delivery_dir}：所有权指纹没有产物树清单")
    pdf_name = str(data.get("pdf") or "")
    if pdf_name and (Path(pdf_name).name != pdf_name or "/" in pdf_name):
        raise ValueError(f"refusing to delete {delivery_dir}：指纹里的 PDF 名单非法")
    actual = _tree_manifest(delivery_dir)
    if actual is None:
        raise ValueError(
            f"refusing to delete {delivery_dir}：树内存在符号链接/junction 或不可读文件，"
            "不能当作 owned 产物重建（危险清理默认 refuse）。")
    extra = sorted(set(actual) - set(recorded))
    missing = sorted(set(recorded) - set(actual))
    changed = sorted(k for k in set(actual) & set(recorded) if actual[k] != recorded[k])
    if extra or missing or changed:
        raise ValueError(
            f"refusing to delete {delivery_dir}：产物树与所有权指纹不一致"
            f"（新增 {extra[:3]} / 缺失 {missing[:3]} / 改动 {changed[:3]}）——"
            "存在用户内容或手工改动，危险清理默认 refuse。请改用新的输出目录或人工清理。")


def build_delivery(
    rc_zip: Path,
    pdf: Path,
    out_dir: Path,
    date_str: str,
) -> Path:
    """在 out_dir 下装配并压缩 mac/Linux 交付包，返回 zip 路径。"""
    # 严格 ISO 日历日期：fromisoformat 也接受紧凑日期等别名，必须回写等于原串。
    date = datetime.date.fromisoformat(date_str)
    if date.isoformat() != date_str:
        raise ValueError("delivery date must be YYYY-MM-DD")
    rc_zip = rc_zip.resolve()
    pdf = pdf.resolve()
    out_dir = out_dir.resolve()
    if not rc_zip.is_file():
        raise FileNotFoundError(f"RC zip not found: {rc_zip}")
    if not pdf.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf}")

    # 验证独立 CLI 的真实输入，必须先于删除或创建任何输出。
    build_release.verify_release(rc_zip)

    delivery_name = f"{DELIVERY_DIR_NAME_PREFIX}{date_str}"
    delivery_dir = _safe_join(out_dir, delivery_name)
    archive = _safe_join(out_dir, f"{delivery_name}.zip")
    for input_path in (rc_zip, pdf):
        if input_path == delivery_dir or input_path.is_relative_to(delivery_dir):
            raise ValueError(f"input is inside delivery directory: {input_path}")
    # 所有权核验先于任何删除：同日同目录重跑只能清理**本装配器**上一次的产物。
    _assert_owned_or_absent(delivery_dir, date_str)
    if delivery_dir.exists():
        shutil.rmtree(delivery_dir)
    delivery_dir.mkdir(parents=True)

    # 1) 顶层说明 + 入口脚本 + PDF（入口脚本 起在仓库内经 launchers/ 维护；
    #    交付包内仍落在顶层，布局不变）。
    readme = Path(__file__).with_name("从这里开始.txt")
    shutil.copyfile(readme, delivery_dir / "从这里开始.txt")
    shutil.copyfile(REPO_ROOT / "launchers" / "打开前端.bat", delivery_dir / "打开前端.bat")
    shutil.copyfile(REPO_ROOT / "launchers" / "打开前端.sh", delivery_dir / "打开前端.sh")
    shutil.copyfile(REPO_ROOT / "launchers" / "打开前端.command", delivery_dir / "打开前端.command")
    shutil.copyfile(pdf, delivery_dir / pdf.name)

    # 2) 解出源码树（复用 RC 里的 mode，保留 .sh/.command 的 exec 位）。
    source_dir = delivery_dir / "biodata-agent"
    source_dir.mkdir()
    modes = _rc_entry_modes(rc_zip)
    with zipfile.ZipFile(rc_zip, "r") as bundle:
        for info in bundle.infolist():
            if info.is_dir():
                continue
            relative = info.filename.replace("\\", "/")
            if relative in RC_EXCLUDED:
                continue
            target = _safe_join(source_dir, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle.read(info.filename))
            mode = modes.get(relative, 0o100644)
            # 尽量落盘 exec 位（POSIX 有效；Windows 尽力而为）。
            try:
                target.chmod(mode & 0o7777)
            except OSError:
                pass

    # 2b) 产物树完成：写入完整所有权指纹（PDF 名单 + 全树摘要），再打 zip。
    (delivery_dir / OWNER_MARKER).write_text(
        json.dumps({"schema_version": OWNER_SCHEMA, "generator": OWNER_GENERATOR,
                    "date": date_str, "pdf": pdf.name, "files": _tree_manifest(delivery_dir)},
                   ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")

    # 3) 打成 exec 位保留的 zip；先写临时文件再原子替换（读侧永不见半截归档）。
    # 独占唯一临时档（mkstemp：不覆盖任何现存同名 tmp，也不跟随既有链接）——
    # 旧实现用固定 `archive.zip.tmp`：若该名被预先做成链接/普通文件，写入会截断他人目标。
    fd, tmp_name = tempfile.mkstemp(dir=str(archive.parent), prefix=archive.name + ".",
                                    suffix=".tmp")
    os.close(fd)
    tmp_archive = Path(tmp_name)
    with zipfile.ZipFile(tmp_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zip_out:
        # 顶层文件（mode 由 TOP_LEVEL_MODES / 默认决定）。
        for name in (
            "从这里开始.txt",
            "打开前端.bat",
            "打开前端.sh",
            "打开前端.command",
            pdf.name,
        ):
            path = delivery_dir / name
            mode = TOP_LEVEL_MODES.get(name, TOP_LEVEL_DEFAULT_MODE)
            zip_out.writestr(_zip_info(name, mode), path.read_bytes())
        # 源码树（mode 复用 RC 记录；目录条目写 0o40755）。
        for relative in sorted(modes):
            path = _safe_join(source_dir, relative)
            if not path.is_file():
                continue
            zip_out.writestr(
                _zip_info(f"biodata-agent/{relative}", modes[relative]),
                path.read_bytes(),
            )
    _publish_archive(tmp_archive, archive)
    return archive


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Assemble the all-platform (Windows/macOS/Linux) command-line delivery package.")
    parser.add_argument("rc_zip", type=Path, help="path to the release candidate zip")
    parser.add_argument("pdf", type=Path, help="path to 'BioData Agent 使用说明书.pdf'")
    parser.add_argument("-o", "--out-dir", type=Path, default=REPO_ROOT / "dist")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    date_str = args.date if args.date is not None else datetime.date.today().isoformat()
    try:
        archive = build_delivery(args.rc_zip, args.pdf, args.out_dir, date_str)
    except (OSError, ValueError, zipfile.BadZipFile, KeyError, build_release.ReleaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
