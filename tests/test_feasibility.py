# -*- coding: utf-8 -*-
"""可行性概览。诚实约束：只合计 Cells 数量，未知及不同单位不混加。"""
import sys
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.retrieval import feasibility  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402

_SURVIVORS = [
    {"species": "Homo sapiens", "platform_family": "Chromium", "source": "CELLxGENE Discover",
     "count": "10000", "unit": "cells", "download_url": "http://x", "published_date": "2021-05-01", "has_raw_data": False},
    {"species": "Homo sapiens", "platform_family": "", "source": "ArrayExpress",
     "count": "", "unit": "", "url": "http://y", "published_date": "2020-01-01"},   # AE：无细胞数
    {"species": "Mus musculus", "platform_family": "Chromium", "source": "10x Genomics",
     "count": "5000", "unit": "cells", "download_url": "http://z", "published_date": "2022-03-01", "has_raw_data": True},
]


def test_feasibility_only_sums_reported_cell_counts():
    r = feasibility.build_report(_SURVIVORS, 3, truncated=False)
    assert r["total_cells_lower_bound"] == 15000        # 只加有细胞数的两条
    assert r["datasets_with_cell_count"] == 2
    assert r["datasets_without_cell_count"] == 1        # AE 那条
    assert any("未纳入" in g and "ArrayExpress" in g for g in r["gaps"])   # 显式标注 AE 无细胞数
    assert "不代表独立细胞总量" in r["caveat"]


def test_distributions_and_rates():
    r = feasibility.build_report(_SURVIVORS, 3, truncated=False)
    sp = {x["value"]: x["count"] for x in r["species"]}
    assert sp["Homo sapiens"] == 2 and sp["Mus musculus"] == 1
    assert {x["value"] for x in r["sources"]} == {"CELLxGENE Discover", "ArrayExpress", "10x Genomics"}
    assert {x["value"] for x in r["years"]} == {"2020", "2021", "2022"}
    assert r["downloadable_count"] == 3
    assert r["fastq_count"] == 1


def test_truncation_flagged():
    r = feasibility.build_report(_SURVIVORS, 9000, truncated=True)
    assert r["truncated"] is True
    assert any("只统计了前" in g for g in r["gaps"])
    assert r["candidate_count"] == 9000 and r["aggregated_count"] == 3


def test_fastq_count_reads_the_structured_status_not_the_substring():
    """raw_data_status 是 dict，label/note 里天然带 "FASTQ" 字样——整字典转字符串
    找子串会把 no_fastq（明确没有）与 unknown（我们没查）全算成「含 FASTQ」。
    只有 code == has_fastq（或 has_raw_data is True）才计数；unknown≠不满足。"""
    status_note = ("目录级信号；unknown≠不满足；要下 FASTQ 定论请用 dataset_uid 调 get_file_manifest。")
    survivors = [
        {"source": "CELLxGENE Discover", "has_raw_data": True, "raw_data_status":
            {"code": "has_fastq", "label": "含原始数据", "warning": None, "authoritative": False, "note": status_note}},
        {"source": "CELLxGENE Discover", "has_raw_data": False, "raw_data_status":
            {"code": "no_fastq", "label": "不含原始数据", "warning": "w", "authoritative": False, "note": status_note}},
        {"source": "ArrayExpress", "has_raw_data": False, "raw_data_status":
            {"code": "unknown", "label": "未知", "warning": None, "authoritative": False, "note": status_note}},
        {"source": "10x Genomics", "has_raw_data": None, "raw_data_status":
            {"code": "unknown", "label": "未知", "warning": None, "authoritative": False, "note": status_note}},
        {"source": "10x Genomics", "has_raw_data": None},   # 老格式：字段缺失 → 不计（不猜）
    ]
    r = feasibility.build_report(survivors, len(survivors), truncated=False)
    assert r["fastq_count"] == 1, "只有 code=has_fastq 计 1；no_fastq/unknown 都不许被 note 里的 FASTQ 字样带进来"


def test_fastq_count_accepts_legacy_string_code():
    """老格式（字符串 code）照样按 code 判，不做子串。"""
    survivors = [{"source": "x", "raw_data_status": "has_fastq"},
                 {"source": "x", "raw_data_status": "no_fastq"},
                 {"source": "x", "raw_data_status": "no_fastq（无 FASTQ 文件）"}]
    r = feasibility.build_report(survivors, len(survivors), truncated=False)
    assert r["fastq_count"] == 1


def test_empty_survivors():
    r = feasibility.build_report([], 0, truncated=False)
    assert r["candidate_count"] == 0 and r["total_cells_lower_bound"] == 0
    assert r["species"] == [] and r["caveat"]


def test_api_feasibility_endpoint():
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.post("/api/feasibility", json={"query": "人类肺癌数据", "sources": ["10x Genomics"]}).json()
    assert r["ok"] and "report" in r
    rep = r["report"]
    for k in ("candidate_count", "total_cells_lower_bound", "species", "sources", "gaps", "caveat"):
        assert k in rep
    # 缺 query → 拒绝（空串被 pydantic 422，纯空白被端点 strip 后 400）
    assert client.post("/api/feasibility", json={"query": ""}).status_code == 422
    assert client.post("/api/feasibility", json={"query": "   "}).status_code == 400


def test_mcp_assess_feasibility_tool():
    from dataset_recommender.app import mcp_server as M
    import pytest
    from mcp.server.fastmcp.exceptions import ToolError

    out = M.assess_feasibility(query="人类肺癌数据", sources=["10x Genomics"])
    assert out["ok"] and "report" in out and "caveat" in out["report"]
    with pytest.raises(ToolError):
        M.assess_feasibility(query="   ")
    # 未知/拼错来源必须 fail-loud（bad_source），绝不静默过滤成空报告 → 假「无可复用数据」结论。
    with pytest.raises(ToolError, match="bad_source"):
        M.assess_feasibility(query="人类肺癌数据", sources=["NotARealSource"])


@pytest.mark.parametrize("count,expected", [
    ("100-200", 100), ("200–100", 100), ("1,000 至 2,000", 1000),
    ("1.5k", 1500), ("1.5K~2K", 1500), ("1.25m", 1250000),
    ("１２３", 123), ("١٢٣", 123), ("0", 0),
    ("²", None), ("1.5", None), ("1,00", None), ("-2", None),
    ("100-unknown", None), ("1-2-3", None), ("9" * 5000, None),
])
def test_feasibility_count_formats_do_not_concatenate_digits(count, expected):
    report = feasibility.build_report([{"count": count, "unit": "Cells"}], 1)
    assert report["total_cells_lower_bound"] == (expected or 0)
    assert report["datasets_with_cell_count"] == int(expected is not None)
    assert report["datasets_without_cell_count"] == int(expected is None)


def test_feasibility_different_units_are_never_added_as_cells():
    report = feasibility.build_report([
        {"sample_size": {"count": "100", "unit": "Cells"}},
        {"count": "500", "unit": "Spots"},
        {"count": "200", "unit": "Nuclei"},
        {"count": "300", "unit": ""},
    ], 4)
    assert report["total_cells_lower_bound"] == 100
    assert report["datasets_with_cell_count"] == 1
    assert report["datasets_without_cell_count"] == 3
    assert "不混加" in report["caveat"]
    assert "总细胞量下限" not in report["caveat"]


def test_feasibility_spots_only_has_no_cell_count():
    report = feasibility.build_report([{"count": "500", "unit": "Spots"}], 1)
    assert report["total_cells_lower_bound"] == 0
    assert report["datasets_with_cell_count"] == 0
    assert report["datasets_without_cell_count"] == 1
