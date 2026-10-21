"""发表时间范围硬过滤（「时间维度」）。

约束：date_from/date_to 默认空串 → 官方评测/CLI 不传时整段 no-op，确定性门不受影响。
"""
import pytest

from dataset_recommender.llm.config import get_settings
from dataset_recommender.corpus.data_loader import load_raw_records
from dataset_recommender.retrieval.normalizer import normalize_records
from dataset_recommender.retrieval.query_parser import parse_query
from dataset_recommender.retrieval.retriever import DatasetRetriever


def _setup():
    s = get_settings()
    return normalize_records(load_raw_records(s.data_dir)), s


def _dates(cands):
    return [str(c.record.raw.get("published_date", "")) for c in cands]


def test_date_range_confines_results_to_window():
    recs, s = _setup()
    r = DatasetRetriever(top_k=10)
    intent = parse_query("人类", s.keyword_mapping)
    intent.date_from, intent.date_to = "2020-01-01", "2021-12-31"
    cands = r.retrieve(recs, intent, top_k=10)
    assert cands, "库内 2020–2021 应有人类数据"
    assert all("2020-01-01" <= d <= "2021-12-31" for d in _dates(cands))


def test_date_from_only_is_lower_bound():
    recs, s = _setup()
    r = DatasetRetriever(top_k=10)
    intent = parse_query("人类", s.keyword_mapping)
    intent.date_from = "2025-01-01"
    cands = r.retrieve(recs, intent, top_k=10)
    assert cands
    assert all(d >= "2025-01-01" for d in _dates(cands))


def test_nl_relative_dates_parsed_from_query():
    """中文/英文相对日期从查询里解析进 intent（此前会因残片「年以后」误弃权、date_from 为空）。"""
    i = parse_query("2022年以后的乳腺癌数据")
    assert i.date_from == "2022-01-01" and i.date_to == ""
    assert not i.abstain  # 关键：不再误弃权
    assert "breast cancer" in i.constraints.get("disease", []) or "invasive ductal carcinoma" in i.constraints.get("disease", [])

    r = parse_query("2020到2023年的人类数据")
    assert r.date_from == "2020-01-01" and r.date_to == "2023-12-31"

    b = parse_query("2023年以前的小鼠脑数据")
    assert b.date_to == "2022-12-31" and not b.abstain

    e = parse_query("human lung data since 2021")
    assert e.date_from == "2021-01-01"


def test_nl_date_survives_mcp_path_no_explicit_params():
    """workflow 未传 date 参数时（MCP/CLI 路径）应保留 NL 解析出的时间范围，而非清空。"""
    from dataset_recommender.app.workflow import DatasetRecommendationWorkflow
    wf = DatasetRecommendationWorkflow()
    res = wf.run_with_meta(query="2024年以后的人类数据", top_k=8, use_llm=False)
    # 命中的都应 >= 2024（若库里有 2024+ 的人类数据）
    dates = [str((c.get("published_date") or "")) for c in res.retrieved_data if c.get("published_date")]
    assert dates, "库内应有 2024+ 人类数据"
    assert all(d >= "2024-01-01" for d in dates)


def test_empty_date_range_is_noop():
    """不设时间范围 → 与不带该字段逐位一致（保护确定性 / 官方评测路径）。"""
    recs, s = _setup()
    r = DatasetRetriever(top_k=8)
    base = [c.record.dataset_name for c in r.retrieve(recs, parse_query("人类乳腺癌", s.keyword_mapping), top_k=8)]
    i1 = parse_query("人类乳腺癌", s.keyword_mapping)
    i1.date_from, i1.date_to = "", ""
    same = [c.record.dataset_name for c in r.retrieve(recs, i1, top_k=8)]
    assert base == same


@pytest.mark.parametrize("expression,lo,hi", [
    ("2024年12月", "2024-12-01", "2024-12-31"),
    ("2024年12月31日", "2024-12-31", "2024-12-31"),
    ("2024-12-31", "2024-12-31", "2024-12-31"),
    ("2024-02", "2024-02-01", "2024-02-29"),
    ("2023年2月", "2023-02-01", "2023-02-28"),
    ("2024年2月29日", "2024-02-29", "2024-02-29"),
    ("2024-01-15到2024年2月", "2024-01-15", "2024-02-29"),
    ("2024年12月以后", "2024-12-01", ""),
    ("before 2024-12-31", "", "2024-12-30"),
])
def test_parser_date_precision_and_spans(expression, lo, hi):
    from dataset_recommender.retrieval.query_parser import annotate_query_spans

    query = expression + " 人类肺数据"
    intent = parse_query(query)
    assert intent.parse_status == "executable"
    assert (intent.date_from, intent.date_to) == (lo, hi)
    assert [s["text"] for s in annotate_query_spans(query) if s["kind"] == "date"] == [expression]


@pytest.mark.parametrize("expression,inside,outside", [
    ("2024年12月", "2024-12-31T23:59:59Z", "2024-01-01"),
    ("2024年12月31日", "2024-12-31", "2024-12-30"),
    ("2024-12-31", "2024-12-31T12:00:00Z", "2025-01-01"),
])
def test_retrieval_honors_date_precision(expression, inside, outside):
    from dataset_recommender.retrieval.normalizer import normalize_dataset_record

    records = [normalize_dataset_record({
        "dataset_name": name, "species": "Human", "tissue": "Lung", "published_date": value,
    }, "synthetic") for name, value in (("inside", inside), ("outside", outside))]
    out = DatasetRetriever().retrieve(records, parse_query(expression + " 人类肺数据"))
    assert [c.record.dataset_name for c in out] == ["inside"]


def test_parser_date_precision_in_preferences_and_exclusions():
    intent = parse_query("人类 优先2024-02 不要小鼠")
    assert intent.parse_status == "executable"
    assert (intent.preferred_date_from, intent.preferred_date_to) == ("2024-02-01", "2024-02-29")
    assert intent.date_from == intent.date_to == ""
    assert intent.excluded_constraints["species"] == ["mouse"]


@pytest.mark.parametrize("expression", ["2023-02-29", "2024-13-01", "2024-00"])
def test_parser_invalid_iso_date_is_not_broadened(expression):
    assert parse_query(expression + " 人类").abstain_reason == "invalid_date"
