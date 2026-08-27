# -*- coding: utf-8 -*-
"""10x 平台信息补充（样本量 / 检测基因数 / 次要指标）· by-uid 旁挂账本（零第三方依赖，优雅降级）。

数据来自 2026-08 手工整理的《Visium-10x.xlsx》《Xenium-10x.xlsx》（10x 官方数据集页数值），
由 `scripts/build_sample_supplement.py` 生成到 `data/sample_supplement.by_uid.json`，
按 `dataset_uid`（首选）或数据集页面 `url` join，**运行时不联网**。

定位与 `downloads.py` 一致：只补缺、不覆盖——冻结 base 已有 `total_records` 的记录一律不动，
只有 base 缺失（卡片显示「未说明 Spots/Cells」）时才用本表回填；`gene_count` 是 base 没有的新字段。

优雅降级（与推荐器「永不崩」一致）：数据文件缺失/损坏 → 所有查询返回 None/空 → 调用方
按无补充处理，不报错、不阻塞。用 `BIODATA_SAMPLE_SUPPLEMENT` 环境变量可覆盖数据文件位置。
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

_DEFAULT = str(Path(__file__).resolve().parents[1] / "data" / "sample_supplement.by_uid.json")
_DATA_PATH = os.environ.get("BIODATA_SAMPLE_SUPPLEMENT", _DEFAULT)


def _data_path() -> str:
    """数据文件路径：每次加载时现读环境变量，未设置时回落 _DATA_PATH（import 期快照，测试经
    monkeypatch 覆盖）。G-09：此前只在 import 期读一次，长驻进程内改环境变量
    不生效；现读后路径含进缓存键，换路径自然换缓存条目。"""
    return os.environ.get("BIODATA_SAMPLE_SUPPLEMENT") or _DATA_PATH


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
        raise ValueError("补充表顶层不是 dict（文件损坏）")
    # 只保留值为 dict 的记录：即便文件部分损坏，下游 getter 的 r.get(...) 也不会 AttributeError。
    by_uid = {k: v for k, v in by_uid.items() if isinstance(v, dict)}
    by_url = {r.get("url"): r for r in by_uid.values() if r.get("url")}
    return by_uid, by_url


def _load() -> "tuple[dict, dict]":
    """加载补充表（缓存键含路径：换环境变量即换缓存条目，G-09）；cache_clear 仪式保持可用。
    读盘失败 → 空表降级（"永不崩"合同不变），但不入缓存——下次调用自动重试（D6）。"""
    try:
        return _load_cached(_data_path())
    except Exception:
        return {}, {}


_load.cache_clear = _load_cached.cache_clear  # 测试既有失效仪式（cache_clear）不变


def get(uid_or_url: "str | None") -> "dict | None":
    """按 dataset_uid 或页面 url 取整条补充记录；查不到返回 None。"""
    if not uid_or_url:
        return None
    by_uid, by_url = _load()
    return by_uid.get(uid_or_url) or by_url.get(uid_or_url)


def count_fill(uid_or_url: "str | None") -> str:
    """用于回填的样本量数字（纯数字字符串）；无补充返回 ''。调用方只在自身 count 缺失时使用。"""
    r = get(uid_or_url)
    return str(r.get("count") or "") if r else ""


def gene_count(uid_or_url: "str | None") -> str:
    """检测基因数（纯数字字符串）；无补充返回 ''。"""
    r = get(uid_or_url)
    return str(r.get("gene_count") or "") if r else ""


def count_note(uid_or_url: "str | None") -> str:
    """样本量口径说明（如多切片合计）；无则 ''。"""
    r = get(uid_or_url)
    return str(r.get("count_note") or "") if r else ""


def extra_facts(uid_or_url: "str | None") -> list:
    """次要补充事实行 [{"label","value"}]（测序深度/转录本中位数/空间分辨率/供体数等）；无则 []。"""
    r = get(uid_or_url)
    facts = r.get("extra_facts") if r else None
    if not isinstance(facts, list):
        return []
    return [f for f in facts if isinstance(f, dict) and f.get("label") and f.get("value")]


def is_available() -> bool:
    """数据文件是否成功加载（供诊断/测试）。"""
    by_uid, _ = _load()
    return len(by_uid) > 0
