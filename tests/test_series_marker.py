# -*- coding: utf-8 -*-
"""「同系列」标记 + 目录 uid 唯一性不变量门（2026-09-29）。

## 起因（用户报「LuCA 重复出现两张卡」）

推荐结果里 The single-cell lung cancer atlas (LuCA) 出现两张卡。诊断结论是**不是 bug**：
`database/external/cellxgene.json` 里是两条合法不同条目——core atlas（89.2 万细胞）与 extended
atlas（128.4 万细胞），同属一篇论文（`collection_doi` 10.1016/j.ccell.2022.10.008），全库 uid
扫描 0 重复、管线无重复引入。两张卡除标题后缀与样本量外逐字相同，观感像重复。

处置：**不藏任何合法条目**（按标题/DOI 去重等于把合法数据藏起来），改做通用「同系列」标记——
前端按**本响应 rows** 统计同一非空 `collection_doi` 的条数，≥2 时那些卡各带一枚「同系列 ×N」小标。

本文件把三件事钉住：

1. 不变量：一份推荐结果的每条卡片行 uid 不重复（真出现重复引入时立刻红，不靠肉眼比标题）；
2. 不变量：目录（base + external）里 uid 全局唯一——把本次诊断的扫描固化；
3. 接线：`collection_doi` 由生产者真发、cards.js 真消费（三道门都不执行一行 JS，漏接线只会
   表现为「用户看不见那枚小标」——**静默失败**，只能在静态门里钉死）。

刻意**不钉**具体外部库 uid / 顺序：`database/external/` 是 opt-in 外部库，允许增删。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from dataset_recommender.app.webapp import app

ROOT = Path(__file__).resolve().parents[1]

client = TestClient(app, base_url="http://127.0.0.1")


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 1. 推荐结果 uid 不重复

def _assert_unique_uids(rows: list, where: str) -> list[str]:
    uids = [str(r.get("dataset_uid") or "").strip() for r in rows]
    assert all(uids), f"{where}: 有卡片行缺 dataset_uid（{uids}）——前端卡片与 /api/files 都靠它定位"
    seen: dict[str, int] = {}
    for u in uids:
        seen[u] = seen.get(u, 0) + 1
    dup = sorted(u for u, n in seen.items() if n > 1)
    assert not dup, (
        f"{where}: 同一份结果里出现重复 uid {dup}——这是**真的重复引入**，不是「标题像」；"
        "前端「同系列 ×N」只解释同 collection 的不同条目，不能拿它掩盖真重复。"
    )
    return uids


#: 代表性 query/sources/facet_filters 组合（覆盖单库 / 多库 / 外部库 / 分面细化四条路径）。
_COMBOS = [
    {"query": "人类肺癌数据", "sources": ["10x Genomics"]},
    {"query": "人类肺癌免疫细胞", "sources": ["10x Genomics", "CELLxGENE Discover", "ArrayExpress"],
     "lenient_dims": []},
    {"query": "人类肺癌单细胞图谱", "sources": ["CELLxGENE Discover"]},   # 外部库路径
    {"query": "人类肺癌数据", "sources": ["10x Genomics"],
     "facet_filters": [{"dim": "species", "value": "homo sapiens"}]},
]


def test_recommend_results_never_repeat_a_uid() -> None:
    """每份推荐结果（含放宽预览 / 降级结果这些内嵌结果列表）里 uid 不重复。"""
    non_empty = 0
    for combo in _COMBOS:
        data = client.post("/api/recommend", json={**combo, "use_llm": False}).json()
        assert data.get("ok") is True, f"{combo} 未返回 ok：{data.get('detail')}"
        rows = data.get("results") or []
        if rows:
            non_empty += 1
        _assert_unique_uids(rows, f"{combo}")
        # 卡片行必须真带 collection_doi 键（值可为空串）——前端「同系列」标记的唯一输入。
        for i, row in enumerate(rows):
            assert "collection_doi" in row, f"{combo} results[{i}] 缺 collection_doi 键"
            assert isinstance(row["collection_doi"], str), f"{combo} results[{i}].collection_doi 非字符串"
        for opt in data.get("relaxation_options") or []:
            _assert_unique_uids(opt.get("results") or [], f"{combo} 放宽项 {opt.get('key')}")
        deg = data.get("degraded_search") or {}
        if deg.get("results"):
            _assert_unique_uids(deg["results"], f"{combo} 降级结果")
    assert non_empty, f"这些组合一个结果都没返回（{_COMBOS}）——不变量没被真正验证过"


def test_rows_from_retrieved_keeps_same_doi_siblings() -> None:
    """同一 collection_doi 的两条是**两份数据**：投影必须两条都留（不按 DOI 折叠、不去重）。

    这条同时是处置决策的钉子：日后若有人想「顺手按 DOI/标题去重」，这里当场红——
    去重会把 extended atlas 这类合法条目从结果里**藏掉**。
    """
    from dataset_recommender.app.recommend_rows import rows_from_retrieved

    payload = [
        {"dataset_name": "X -- core atlas", "dataset_uid": "u1", "collection_doi": "10.0000/x",
         "url": "https://example.org/1"},
        {"dataset_name": "X -- extended atlas", "dataset_uid": "u2", "collection_doi": "10.0000/x",
         "url": "https://example.org/2"},
        {"dataset_name": "Y", "dataset_uid": "u3", "url": "https://example.org/3"},
    ]
    rows = rows_from_retrieved(payload)
    assert [r["dataset_uid"] for r in rows] == ["u1", "u2", "u3"], "同 collection 的条目被折叠了"
    assert rows[0]["collection_doi"] == rows[1]["collection_doi"] == "10.0000/x"
    assert rows[2]["collection_doi"] == "", "缺 collection_doi 时应为空串（不是 None/缺键）"


# ---------------------------------------------------------------- 2. 目录 uid 全局唯一

def test_catalog_uids_are_unique_across_base_and_external() -> None:
    """`database/base/*.json` + `database/external/*.json` 的 dataset_uid 全局唯一。

    为什么 base 与 external 一起扫：两目录是**同一份装载语料**（`load_full_corpus` = base + external），
    同一个 uid 出现在两处 = 同一份数据在结果里出现两次——正是用户报的那类观感问题里唯一
    **真正**的 bug 形态（LuCA 那种是两条不同数据共用一个 collection DOI，不是这个）。

    经 `data_loader.load_raw_records` 读（装载口径的单一真源），不另写一套 JSON 解析：
    base 严格（缺一个文件就该红）、external 宽容（用户手放的坏文件不连累整库）。
    """
    from dataset_recommender.corpus.data_loader import load_raw_records

    base = load_raw_records(ROOT / "database" / "base")
    external = load_raw_records(ROOT / "database" / "external", lenient=True)
    assert base and external, "语料目录没装载出记录（路径/格式变了？）"

    where: dict[str, list[str]] = {}
    for r in [*base, *external]:
        uid = str(r.record.get("dataset_uid") or "").strip()
        assert uid, f"{r.source_file} 里有一条记录没有 dataset_uid"
        where.setdefault(uid, []).append(r.source_file)
    dup = {u: files for u, files in where.items() if len(files) > 1}
    assert not dup, f"dataset_uid 在语料里重复（同一份数据会出两张卡）：{dup}"


# ---------------------------------------------------------------- 3. 前端接线（静态钉）

def test_producer_and_frontend_wire_the_series_marker() -> None:
    """`collection_doi` 的「生产者 → 消费点」链在三处都在场：漏任何一处都静默（用户看不见小标）。"""
    producer = _read("src/dataset_recommender/app/recommend_rows.py")
    assert '"collection_doi"' in producer, "卡片行投影没发 collection_doi —— 前端拿不到输入"

    cards = _read("web/static/js/search/cards.js")
    assert "export function seriesCountsOf(" in cards, "缺「同系列」计数的唯一构造点 seriesCountsOf"
    assert "export function seriesBadgeHtml(" in cards, "缺「同系列」小标的唯一构造点 seriesBadgeHtml"
    assert "collection_doi" in cards, "cards.js 不读 collection_doi —— 小标没有输入"
    assert "${seriesBadge}${reachBadge}" in cards, "小标没进卡片徽章行（.badges）"
    assert "同系列" in cards, "小标文案不在场（用户可见的措辞丢了）"

    results = _read("web/static/js/search/results.js")
    assert "seriesCountsOf(items)" in results, "结果页没用本响应 rows 统计同系列条数"
    assert "buildCard(it, { rank: idx + 1, seriesCounts })" in results, (
        "结果页没把本响应的同系列计数传进 buildCard —— 小标永远不会出现"
    )

    css = _read("web/static/css/app.css")
    assert ".series {" in css, "小标的样式类 .series 未定义（会和其它 pill 撞样式/裸奔）"
