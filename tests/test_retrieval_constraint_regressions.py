# -*- coding: utf-8 -*-
"""约束回归：raw 排除第三态、英文否定列表、来源偏好范围、日期交集、
猜测 raw=False 三态、显式 LLM 开关。合成 query/记录；workflow 用本地真实语料（零网络）。"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dataset_recommender.app.workflow import DatasetRecommendationWorkflow  # noqa: E402
from dataset_recommender.retrieval import normalizer as NM  # noqa: E402
from dataset_recommender.retrieval import query_parser as QP  # noqa: E402
from dataset_recommender.retrieval.normalizer import (  # noqa: E402
    DatasetRecord,
    normalize_dataset_record,
)
from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402
from dataset_recommender.retrieval.retriever import passes_hard_filter  # noqa: E402
from dataset_recommender.retrieval.search_request import resolve_search_request  # noqa: E402

TODAY = date(2026, 10, 3)


def _record(*, has_raw: bool | None, source: str, uid: str = "") -> DatasetRecord:
    raw = {"dataset_name": "synthetic", "species": "human", "source": source,
           "has_raw_data": has_raw, "dataset_uid": uid,
           "published_date": "2024-06-01", "url": "https://example.org/x"}
    return normalize_dataset_record(raw, f"{source}.json")


def _intent(**kw):
    base = {"parse_status": "executable"}
    base.update(kw)
    from dataset_recommender.retrieval.query_parser import QueryIntent
    return QueryIntent(original_query="synthetic", **base)


# ---------------------------------------------------------------- raw 排除必须落成第三态

@pytest.mark.parametrize("query", [
    "人类数据，FASTQ的不要",
    "human data without-fastq",
    "不要 FASTQ 的人类数据",
    "人类数据，FASTQ 除外",
])
def test_fastq_negative_forms_set_raw_false(query):
    intent = parse_query(query)
    assert intent.parse_status == "executable", (query, intent.clarification_reason)
    assert intent.has_raw_data_required is False, query


def test_fastq_exclusion_filters_out_fastq_records():
    intent = parse_query("人类数据，FASTQ的不要")
    fastq = _record(has_raw=True, source="10x Genomics", uid="10x-1")
    plain = _record(has_raw=False, source="10x Genomics", uid="10x-2")
    assert passes_hard_filter(fastq, intent) is False      # 含 FASTQ 必须被挡
    assert passes_hard_filter(plain, intent) is True
    # 反向：要 FASTQ 时仍只留 True（原语义不回归）
    want = parse_query("要 FASTQ 的人类数据")
    assert want.has_raw_data_required is True
    assert passes_hard_filter(fastq, want) is True and passes_hard_filter(plain, want) is False


# ---------------------------------------------------------------- 英文否定列表整条绑定

def test_english_negative_list_binds_every_member():
    intent = parse_query("human data without lymphoma or melanoma")
    excluded = {t for targets in intent.excluded_constraints.values() for t in targets}
    assert {"lymphoma", "melanoma"} <= excluded, intent.excluded_constraints
    positive = {t for targets in intent.constraints.values() for t in targets}
    assert "melanoma" not in positive, intent.constraints


def test_english_affirmative_list_still_binds_both():
    intent = parse_query("human data with lymphoma or melanoma")
    positive = {t for targets in intent.constraints.values() for t in targets}
    assert {"lymphoma", "melanoma"} <= positive, intent.constraints


# ---------------------------------------------------- 实体列表连接词的可变空白与词边界

def _record_disease(disease: str, uid: str = "") -> DatasetRecord:
    raw = {"dataset_name": "synthetic", "species": "human", "source": "synthetic",
           "has_raw_data": False, "dataset_uid": uid, "disease": disease,
           "published_date": "2024-06-01", "url": "https://example.org/x"}
    return normalize_dataset_record(raw, "synthetic.json")


@pytest.mark.parametrize("query", [
    "human data without lymphoma  or melanoma",       # 双空格
    "human data without lymphoma\tor\tmelanoma",      # tab
    "human data without lymphoma \t or  melanoma",    # 混合空白
    "human data without lymphoma  and melanoma",      # and 双空格
])
def test_english_negative_list_tolerates_variable_whitespace(query):
    """词表里 " or "/" and " 写的是单空格字面形；用户敲出双空格/tab 时后项曾掉进正向
    内核（melanoma 变「必须包含」）——静默反向比弃权更糟。连接词改为实体间 gap 匹配：
    任意空白 + 词表连接词 + 英文词边界，坐标仍落在原串上（不做全句空白归一）。"""
    intent = parse_query(query)
    assert intent.parse_status == "executable", (query, intent.clarification_reason)
    excluded = {t for targets in intent.excluded_constraints.values() for t in targets}
    assert {"lymphoma", "melanoma"} <= excluded, intent.excluded_constraints
    positive = {t for targets in intent.constraints.values() for t in targets}
    assert "melanoma" not in positive, intent.constraints


def test_variable_whitespace_exclusion_filters_both_diseases():
    """真实 hard filter：双空格排除列表里两个 disease 都必须被挡，普通 disease 照常放行。"""
    intent = parse_query("human data without lymphoma  or melanoma")
    assert passes_hard_filter(_record_disease("melanoma"), intent) is False
    assert passes_hard_filter(_record_disease("lymphoma"), intent) is False
    assert passes_hard_filter(_record_disease("lung cancer"), intent) is True


def test_english_negative_list_single_space_unchanged():
    """控制组：单空格本来就是绿的（既有钉子），新 gap 匹配不得改写它。"""
    intent = parse_query("human data without lymphoma or melanoma")
    excluded = {t for targets in intent.excluded_constraints.values() for t in targets}
    assert {"lymphoma", "melanoma"} <= excluded, intent.excluded_constraints


def test_reverse_english_list_binds_before_chinese_suffix():
    """反向消费者同机制：英文列表 + 中文排除后缀（『lymphoma or melanoma除外』），
    修前反向只绑到 melanoma、lymphoma 落进正向。"""
    intent = parse_query("lymphoma or melanoma除外")
    assert intent.parse_status == "executable", intent.clarification_reason
    excluded = {t for targets in intent.excluded_constraints.values() for t in targets}
    assert {"lymphoma", "melanoma"} <= excluded, intent.excluded_constraints
    positive = {t for targets in intent.constraints.values() for t in targets}
    assert "lymphoma" not in positive, intent.constraints


def test_reverse_chinese_suffix_list_unchanged():
    """反向中文排除后缀原行为护栏（本就绿的通道，不被新匹配改写）。"""
    intent = parse_query("淋巴瘤和黑色素瘤的不要")
    assert intent.parse_status == "executable", intent.clarification_reason
    excluded = {t for targets in intent.excluded_constraints.values() for t in targets}
    assert {"lymphoma", "melanoma"} <= excluded, intent.excluded_constraints


def test_list_connector_gap_matcher_boundaries():
    """唯一 gap 匹配函数（正/反向共用）的单元钉：可变空白命中、orange/androgen 词边界
    不吞、句末悬空 "or" 不吃（吃了会把弃权静默变成部分排除）。坐标基于原串。"""
    # 正向：pos = 左侧实体结束坐标；返回 (词表 token, 连接词后坐标)
    assert QP._match_list_connector("x or y", 1) == (" or ", 4)
    assert QP._match_list_connector("x  or  y", 1) == (" or ", 5)
    assert QP._match_list_connector("x \t or \t y", 1) == (" or ", 6)
    assert QP._match_list_connector("x和y", 1) == ("和", 2)
    assert QP._match_list_connector("x 、y", 1) == ("、", 3)
    assert QP._match_list_connector("x orange y", 1) == ("", 1)
    assert QP._match_list_connector("x androgen y", 1) == ("", 1)
    assert QP._match_list_connector("x or", 1) == ("", 1)          # 悬空连接词不消费
    # 反向：pos = 右侧实体 start 向左跳过空白后的坐标；返回 (词表 token, gap 起点坐标)
    assert QP._match_list_connector("x or y", 4, left=True) == (" or ", 1)
    assert QP._match_list_connector("x  or  y", 5, left=True) == (" or ", 1)
    assert QP._match_list_connector("x和y", 2, left=True) == ("和", 1)
    assert QP._match_list_connector("senior y", 6, left=True) == ("", 6)
    assert QP._match_list_connector("foo androgen y", 12, left=True) == ("", 12)


# ---------------------------------------------------------------- 偏好不得越过显式来源范围

def test_preference_does_not_expand_explicit_source_scope():
    r = DatasetRecommendationWorkflow().run_with_meta(
        query="来自10x的人类数据，优先CELLxGENE",
        sources=["10x Genomics"],
        auto_parse_sources=True,
        use_llm=False,
    )
    assert r.interpretation["effective_sources"] == ["10x Genomics"]
    intent = r.interpretation["intent"]
    assert intent["preferred_sources"] == []                       # 范围外偏好不得挂 chip
    assert "CELLxGENE Discover" in r.unused_query_terms            # 走既有「未生效」回显
    rows = json.dumps(r.retrieved_data or [], ensure_ascii=False).lower()
    assert "cellxgene" not in rows                                 # 实际也没装载到范围外记录


def test_preference_still_expands_default_pool():
    """反向：没有范围约束时「优先 X」仍按设计扩容（否则偏好是结构性 no-op）。"""
    resolution = resolve_search_request(
        "人类肺数据，优先CELLxGENE", None, ["10x Genomics", "CELLxGENE Discover"],
        auto_parse_sources=True)
    assert resolution.preferred_sources == ["CELLxGENE Discover"]


# ---------------------------------------------------------------- 同句日期取交集

def test_relative_date_intersects_same_sentence_absolute_bound():
    df, dt, _spans = QP._parse_dates("近5年、2024年之前的人类数据", today=TODAY)
    assert df == "2021-10-03"
    assert dt == "2023-12-31"          # 修前：丢掉上界，给出 today


def test_bare_relative_date_window_unchanged():
    df, dt, _spans = QP._parse_dates("近5年的人类数据", today=TODAY)
    assert (df, dt) == ("2021-10-03", "2026-10-03")


def test_absolute_only_parsing_unchanged():
    df, dt, _spans = QP._parse_dates("2024年之前的人类数据", today=TODAY)
    assert (df, dt) == ("", "2023-12-31")


# ---------------------------------------------------- 同句多个日期表达收全后一次交集

def _record_published(published: str) -> DatasetRecord:
    raw = {"dataset_name": "synthetic", "species": "human", "source": "synthetic",
           "has_raw_data": False, "dataset_uid": "", "published_date": published,
           "url": "https://example.org/x"}
    return normalize_dataset_record(raw, "synthetic.json")


def test_multiple_upper_bounds_take_tighter():
    """同类多个上界（『2025年之前、2024年之前』）修前只留首个 → 2024 记录照样返回。"""
    df, dt, _spans = QP._parse_dates("2025年之前、2024年之前的人类数据", today=TODAY)
    assert (df, dt) == ("", "2023-12-31")
    intent = parse_query("2025年之前、2024年之前的人类数据", today=TODAY)
    assert intent.parse_status == "executable", intent.clarification_reason
    assert passes_hard_filter(_record_published("2024-01-01"), intent) is False
    assert passes_hard_filter(_record_published("2023-12-31"), intent) is True


def test_multiple_lower_bounds_take_tighter():
    """同类多个下界取 max；修前第二个『以后』连 span 都没消费、残差门弃权。"""
    intent = parse_query("2020年以后、2022年以后的人类数据", today=TODAY)
    assert intent.parse_status == "executable", intent.clarification_reason
    assert (intent.date_from, intent.date_to) == ("2022-01-01", "")
    assert passes_hard_filter(_record_published("2021-12-31"), intent) is False
    assert passes_hard_filter(_record_published("2022-01-01"), intent) is True   # after/since 含边界


def test_two_ranges_and_before_bound_intersect():
    """两个完整区间 + before 上界三段一次交集；区间端点不得被并列年份歧义门误判。"""
    intent = parse_query("2020到2023年、2021到2024年、2022年之前的人类数据", today=TODAY)
    assert intent.parse_status == "executable", intent.clarification_reason
    assert (intent.date_from, intent.date_to) == ("2021-01-01", "2021-12-31")


def test_multiple_relative_windows_intersect():
    """近5年 ∩ 近2年 → 更紧的窗口（修前只留首个相对表达）。"""
    df, dt, _spans = QP._parse_dates("近5年、近2年的人类数据", today=TODAY)
    assert (df, dt) == ("2024-10-03", "2026-10-03")


def test_month_day_precision_kept_in_intersection():
    intent = parse_query("2024年3月15日以后、2024年6月之前的人类数据", today=TODAY)
    assert (intent.date_from, intent.date_to) == ("2024-03-15", "2024-05-31")


def test_inverted_intersection_rejects_every_record_not_swapped():
    """空交集（下界>上界）原样落下：不 swap、不清空成无限窗——任何记录都拒。"""
    intent = parse_query("2024年之前、2025年以后的人类数据", today=TODAY)
    assert intent.parse_status == "executable", intent.clarification_reason
    assert (intent.date_from, intent.date_to) == ("2025-01-01", "2023-12-31")
    for pub in ("2020-01-01", "2024-06-01", "2026-01-01", ""):
        assert passes_hard_filter(_record_published(pub), intent) is False, pub


@pytest.mark.parametrize("query", [
    "2025年之前、2024年之前的人类数据",
    "2024年之前、2025年之前的人类数据",
])
def test_bound_order_permutation_same_result(query):
    """表达顺序置换 → 同一交集（修前首命中即定，顺序不同结果不同）。"""
    df, dt, _spans = QP._parse_dates(query, today=TODAY)
    assert (df, dt) == ("", "2023-12-31")


def test_adjacent_range_endpoints_are_not_bare_multi_year():
    """两个明确区间的相邻端点（『2020到2021年和2022到2023年』）不是裸并列年——
    歧义门只拦未被完整 range/boundary 消费的并列年；此处交集为空 → 任何记录都拒。"""
    intent = parse_query("2020到2021年和2022到2023年的人类数据", today=TODAY)
    assert intent.parse_status == "executable", intent.clarification_reason
    assert intent.date_from > intent.date_to, "两个不相交区间交集为空（不 swap、不放宽）"
    assert passes_hard_filter(_record_published("2021-06-01"), intent) is False


def test_bare_multi_year_still_abstains():
    """旧歧义保留：裸并列年（未被任何完整表达消费）照旧弃权。"""
    intent = parse_query("2020年和2022年的人类数据", today=TODAY)
    assert intent.parse_status == "abstained" and intent.abstain_reason == "ambiguous_multi_year"


def test_reversed_single_range_still_normalized():
    """单个倒置区间照旧规范化（sorted endpoints），不受收集机制影响。"""
    df, dt, _spans = QP._parse_dates("2023到2020年的人类数据", today=TODAY)
    assert (df, dt) == ("2020-01-01", "2023-12-31")


@pytest.mark.parametrize("query", [
    "2020年到2025年、2022年以后、2023年和2024年的人类数据",   # 合法端点对在前，裸并列在后
    "2023年和2024年、2020年到2025年、2022年以后的人类数据",   # 裸并列在前（修前即弃权，控制）
])
def test_bare_multi_year_caught_regardless_of_order(query):
    """歧义门必须顺序无关：首个命中是合法区间/上下界端点对而获豁免后，
    后续的裸并列年（『2023年和2024年』）不得漏检——修前只 search 首个命中，
    裸并列前移才弃权、后置则放行成 executable（同一句话两种结果）。"""
    intent = parse_query(query, today=TODAY)
    assert intent.parse_status == "abstained", (query, intent.date_from, intent.date_to)
    assert intent.abstain_reason == "ambiguous_multi_year", query


def test_legal_endpoint_pair_then_bare_pair_abstains():
    """前合法后裸：『2020到2025年和2023年和2024年』——2025 是合法区间端点（获豁免），
    2023/2024 是裸并列（必须仍拦）。"""
    intent = parse_query("2020到2025年和2023年和2024年的人类数据", today=TODAY)
    assert intent.parse_status == "abstained" and intent.abstain_reason == "ambiguous_multi_year"


def test_overlapping_legal_and_bare_pair_control():
    """相邻重叠 Pair 控制：区间端点紧邻裸年（『2020到2025年、2023年和2024年』）——
    修前修后都必须弃权，blank 机制不得把该拦的放走。"""
    intent = parse_query("2020到2025年、2023年和2024年的人类数据", today=TODAY)
    assert intent.parse_status == "abstained" and intent.abstain_reason == "ambiguous_multi_year"


@pytest.mark.parametrize("query", [
    "2023年和2023年和2024年的人类数据",        # 同年 pair 在前，异年 pair 共享第二个 2023
    "2023年和2023年和2024年和2024年的人类数据",  # 4 年重复链：异年 pair 夹在两对同年之间
    "2024年和2024年和2023年和2023年的人类数据",  # 同上换序
])
def test_overlapping_bare_year_pairs_caught(query):
    """同年重复本身不歧义，但从同年 pair **第二组**开始的下一 pair（2023,2024）必须仍被
    检查——非重叠遍历会把整个同年 pair 消耗掉，共享端点的异年裸并列随之漏检
    （修前这三条都 executable，换序的异年在前却弃权：同一语义两种结果）。"""
    intent = parse_query(query, today=TODAY)
    assert intent.parse_status == "abstained", (query, intent.date_from, intent.date_to)
    assert intent.abstain_reason == "ambiguous_multi_year", query


@pytest.mark.parametrize("query", [
    "2024年和2023年和2023年的人类数据",       # 换序：异年 pair 在前（修前即弃权）
    "2023年和2023年和2023年和2024年的人类数据",  # 4 年链末尾异年（非重叠遍历也能到，修前即弃权）
])
def test_different_year_pair_reachable_before_still_abstains(query):
    """控制：异年 pair 在非重叠遍历下本就可达的语序，修前修后都必须弃权。"""
    intent = parse_query(query, today=TODAY)
    assert intent.parse_status == "abstained" and intent.abstain_reason == "ambiguous_multi_year", query


@pytest.mark.parametrize("query", [
    "2023年和2023年和2023年的人类数据",   # 全同年重复链
    "2020年和2020年的人类数据",           # 同年 pair 基础形
])
def test_same_year_repeat_is_not_ambiguous(query):
    """控制：同年重复不是歧义（恰好这几年语义唯一）——照常 executable，不得被
    重叠推进误判。"""
    intent = parse_query(query, today=TODAY)
    assert intent.parse_status == "executable", (query, intent.clarification_reason)



# ---------------------------------------------------------------- 猜测 raw=False ≠ 已确认无 FASTQ

def test_guessed_false_is_unknown_for_filter_and_facets():
    guessed = _record(has_raw=False, source="ArrayExpress")        # 抓取占位，未逐条核验
    assert NM.effective_has_raw_data(guessed) is None
    not_fastq = _intent(has_raw_data_required=False)
    assert passes_hard_filter(guessed, not_fastq) is False         # 不许当「已确认没有」
    want_fastq = _intent(has_raw_data_required=True)
    assert passes_hard_filter(guessed, want_fastq) is False


def test_evidenced_false_still_counts_as_no_fastq():
    evidenced = _record(has_raw=False, source="CELLxGENE Discover")
    assert NM.effective_has_raw_data(evidenced) is False
    assert passes_hard_filter(evidenced, _intent(has_raw_data_required=False)) is True
    assert passes_hard_filter(evidenced, _intent(has_raw_data_required=True)) is False


# ---------------------------------------------------------------- 显式 LLM 开关不被自动化旗标压掉

def test_explicit_polish_survives_automation_capability_flag(monkeypatch):
    wf = DatasetRecommendationWorkflow()
    built: list[dict] = []
    monkeypatch.setattr(wf, "_effective_llm_config",
                        lambda **kw: (built.append(kw), None)[1])
    wf.run_with_meta(query="人类肺组织数据", sources=["10x Genomics"],
                     use_llm=True, llm_available=False, rerank_backend="off")
    assert built and built[-1].get("use_llm") is True


def test_explicit_rerank_backend_survives_automation_capability_flag(monkeypatch):
    wf = DatasetRecommendationWorkflow()
    built: list[dict] = []
    monkeypatch.setattr(wf, "_effective_llm_config",
                        lambda **kw: (built.append(kw), None)[1])
    wf.run_with_meta(query="人类肺组织数据", sources=["10x Genomics"],
                     use_llm=False, llm_available=False, rerank_backend="llm")
    assert built, "显式 rerank_backend='llm' 不得被 llm_available=False 压掉"


def test_preloop_deterministic_path_still_skips_llm_config(monkeypatch):
    """反向护栏：pre-loop 确定性管线（llm_available=False + 默认 rerank/use_llm 关）
    仍不得构造任何 LLM 配置（不读 env）。"""
    wf = DatasetRecommendationWorkflow()
    built: list[dict] = []
    monkeypatch.setattr(wf, "_effective_llm_config",
                        lambda **kw: (built.append(kw), None)[1])
    wf.run_with_meta(query="人类肺组织数据", sources=["10x Genomics"],
                     use_llm=False, llm_available=False, rerank_backend="off")
    assert built == []
