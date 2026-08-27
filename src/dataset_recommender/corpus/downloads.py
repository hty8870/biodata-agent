# -*- coding: utf-8 -*-
"""真实文件下载直链 · 主线接入模块（零第三方依赖，优雅降级）。

按 `dataset_uid`（首选）或数据集页面 `url` 查该数据集的**真实文件下载直链**——替换掉表格里
原本的数据集页面链接。数据来自一次全库直链核验（767 数据集、15119 直链、逐条 Range-GET
校验 200/206、join 覆盖率 100%），已随包落地到 `data/download_links.by_uid.json`，**运行时不联网**。

优雅降级（与推荐器"永不崩"一致）：数据文件缺失/损坏 → 所有查询返回 None/空 → 调用方回退页面 url，
不报错、不阻塞。用 `BIODATA_DOWNLOAD_LINKS` 环境变量可覆盖数据文件位置。
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

_DEFAULT = str(Path(__file__).resolve().parents[1] / "data" / "download_links.by_uid.json")
_DATA_PATH = os.environ.get("BIODATA_DOWNLOAD_LINKS", _DEFAULT)


def _data_path() -> str:
    """数据文件路径：每次加载时现读环境变量，未设置时回落 _DATA_PATH（import 期快照，测试经
    monkeypatch 覆盖）。G-09：此前只在 import 期读一次，长驻进程内改环境变量
    不生效；现读后路径含进缓存键，换路径自然换缓存条目（同路径的内容热更新见模块 docstring）。"""
    return os.environ.get("BIODATA_DOWNLOAD_LINKS") or _DATA_PATH


@lru_cache(maxsize=4)
def _load_cached(path: str) -> "tuple[dict, dict]":
    """按路径加载 by_uid 映射 + 派生 by_url 映射。

    缓存纪律（cross-trace D6）：文件不存在 → 空表（稳定状态，缓存合法）；
    读了但失败（坏 JSON / IO / 顶层形状不符）→ 抛出由 _load 兜底——失败**不入缓存**，
    故障消除后同进程下次调用自动重试（此前失败被缓存到进程结束，须重启才恢复）。"""
    try:
        with open(path, encoding="utf-8") as f:
            by_uid = json.load(f)
    except FileNotFoundError:
        return {}, {}
    if not isinstance(by_uid, dict):
        raise ValueError("台账顶层不是 dict（文件损坏）")
    # 只保留值为 dict 的记录：即便文件被部分损坏（某条记录值退化成标量/列表），
    # 下游 getter 的 r.get(...) 也不会 AttributeError（守住"永不崩→降级"合同）。
    by_uid = {k: v for k, v in by_uid.items() if isinstance(v, dict)}
    by_url = {r.get("url"): r for r in by_uid.values() if r.get("url")}
    return by_uid, by_url


def _load() -> "tuple[dict, dict]":
    """加载数据（缓存键含路径：换环境变量即换缓存条目，G-09）；cache_clear 仪式保持可用。
    读盘失败 → 空表降级（"永不崩"合同不变），但不入缓存——下次调用自动重试（D6）。"""
    try:
        return _load_cached(_data_path())
    except Exception:
        return {}, {}


_load.cache_clear = _load_cached.cache_clear  # 测试既有失效仪式（cache_clear）不变


def get(uid_or_url: "str | None") -> "dict | None":
    """按 dataset_uid 或页面 url 取整条记录；查不到返回 None。"""
    if not uid_or_url:
        return None
    by_uid, by_url = _load()
    return by_uid.get(uid_or_url) or by_url.get(uid_or_url)


def primary_url(uid_or_url: "str | None") -> "str | None":
    """代表性主文件的真实下载直链（分析师最先要的那份）；查不到返回 None。"""
    r = get(uid_or_url)
    return r.get("primary_download_url") if r else None


def fastq_url(uid_or_url: "str | None") -> "str | None":
    """真实 FASTQ 原始 reads 直链（已排除 VDJ contig 输出）；无则 None。"""
    r = get(uid_or_url)
    return r.get("fastq_download_url") if r else None


def has_fastq(uid_or_url: "str | None") -> "bool | None":
    """是否存在真实 FASTQ reads（实测更正信号）；查不到返回 None。
    注意：与索引 has_raw_data 在 99 条上不一致（多为 Xenium 天然无 FASTQ）——采用前先量影响。"""
    r = get(uid_or_url)
    return r.get("has_raw_data_actual") if r else None


def files_for(uid_or_url: "str | None") -> list:
    """该数据集全部文件直链列表（可能为空 list）。"""
    r = get(uid_or_url)
    return r.get("files", []) if r else []


def all_real_urls(uid_or_url: "str | None") -> "set[str]":
    """该数据集所有真实下载直链集合。反捏造校验器白名单化时并入，避免真实直链被误判 unknown。"""
    return {f["download_url"] for f in files_for(uid_or_url) if isinstance(f, dict) and f.get("download_url")}


def file_count(uid_or_url: "str | None") -> int:
    """该数据集真实文件直链条数（0 表示无/降级——调用方据此决定是否显示「查看全部文件」入口）。"""
    return sum(1 for f in files_for(uid_or_url) if isinstance(f, dict) and f.get("download_url"))


def download_cell(uid_or_url: "str | None", fallback: str = "-") -> str:
    """Markdown 表格「下载链接」列：有真实直链则 [点击下载](url)，否则 fallback。"""
    u = primary_url(uid_or_url)
    return f"[点击下载]({u})" if u else fallback


def is_available() -> bool:
    """数据文件是否成功加载（供诊断/测试）。"""
    by_uid, _ = _load()
    return len(by_uid) > 0
