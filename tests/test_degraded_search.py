# -*- coding: utf-8 -*-
"""未收录词降级建议（想法 0 的诚实形态）专项。

用户提的是「纯规则模式下不要因为不存在满足全部关键词的数据集就零返回」。方向对，
但**不能自动降级**——实测（全库 5665 条）：

    「2022 年之后发表」去掉「发表」   →   3 条心衰数据          救回，很好
    「人类膀胱癌 snATAC」去掉「膀胱」  →  11 条，没有一条是膀胱    无关
    「翼龙的单细胞数据」去掉「翼龙」   → 3473 条                 灾难
    「霍格沃茨综合征的人类数据」去掉   → 3623 条                 灾难

后两行正是冻结评测 nr01–nr04 / adv01 / adv02 / adv09 钉死的产品底线：查无此物就如实说没有。
所以落地成**只算不应用**的一个可点选项，条数 + 「忽略后实际生效的条件」一并回传，
由用户自己判断这次忽略值不值。本文件钉死的正是「只算不应用」这条不变量。
"""
import pytest

from dataset_recommender.llm.config import get_settings
from dataset_recommender.corpus.corpus import known_source_values
from dataset_recommender.retrieval.query_parser import parse_query
from dataset_recommender.app.workflow import (DatasetRecommendationWorkflow,
                                              build_degraded_search, strip_terms)

CAT = get_settings().keyword_mapping
S = get_settings()
ALL_SOURCES = known_source_values(S.data_dir, S.project_root)


@pytest.fixture(scope="module")
def wf():
    return DatasetRecommendationWorkflow()


# ---------- 解析层：未收录词现在是结构化字段，不用去 abstain_detail 里抠字符串 ----------
def test_unresolved_terms_is_structured():
    it = parse_query("人类膀胱造瘘的单细胞数据", CAT)
    assert it.abstain and it.abstain_reason == "unresolved_term"
    assert it.unresolved_terms == ["造瘘"], it.unresolved_terms
    # 文字版仍在（前端弃权文案还在用），但结构化字段才是上层的真源
    assert "造瘘" in it.abstain_detail


@pytest.mark.parametrize("query", ["人类肺组织的单细胞数据", "不需要fastq", "最好是 Xenium 的黑色素瘤数据"])
def test_unresolved_terms_empty_on_other_states(query):
    """只有 unresolved_term 弃权才有值；可执行 / 澄清 / 其它弃权理由恒空。"""
    assert parse_query(query, CAT).unresolved_terms == []


# ---------- strip_terms：大小写不敏感、长词先挖 ----------
def test_strip_terms_is_case_insensitive():
    assert strip_terms("Human XYZZY lung", ["xyzzy"]).split() == ["Human", "lung"]
    assert strip_terms("人类肺数据", []) == "人类肺数据"


# ---------- 降级建议本身 ----------
def test_degraded_option_reports_count_and_surviving_conditions(wf):
    res = wf.run_with_meta(query="人类膀胱造瘘的单细胞数据", use_llm=False, sources=ALL_SOURCES)
    assert res.resolution_status == "abstained"
    deg = res.degraded_search
    assert deg is not None
    assert deg["ignored_terms"] == ["造瘘"]
    assert deg["count"] > 0
    assert len(deg["results"]) > 0
    labels = {f["label"] for f in deg["active_filters"]}
    # 关键：不能只给条数。用户看不到「忽略之后到底在筛什么」就无法判断这批结果值不值。
    assert {"物种", "组织"} <= labels, deg["active_filters"]


def test_degraded_option_is_never_auto_applied(wf):
    """底线：有降级建议 ≠ 已经降级。返回体仍然是弃权、零结果。"""
    res = wf.run_with_meta(query="人类膀胱造瘘的单细胞数据", use_llm=False, sources=ALL_SOURCES)
    assert res.degraded_search is not None
    assert res.resolution_status == "abstained"
    assert res.retrieved_data == []
    assert res.result_total == 0


@pytest.mark.parametrize("query,ignored", [
    ("翼龙的单细胞数据", "翼龙"),
    ("霍格沃茨综合征的人类数据", "霍格沃茨综合征"),
])
def test_adversarial_queries_still_return_nothing_but_show_the_price(wf, query, ignored):
    """冻结评测 adv01/adv02 的那两句：仍然零结果（底线不动），
    但降级建议里的条数大得离谱、生效条件只剩一条——这本身就是最诚实的劝退信号，
    不需要任何阈值去替用户做判断。"""
    res = wf.run_with_meta(query=query, use_llm=False, sources=ALL_SOURCES)
    assert res.resolution_status == "abstained" and res.retrieved_data == []
    deg = res.degraded_search
    assert deg and deg["ignored_terms"] == [ignored]
    assert deg["count"] > 1000                      # 忽略之后是「半个库」
    assert len(deg["active_filters"]) == 1          # 只剩一条条件，一眼看出没意义


def test_no_option_when_nothing_survives(wf):
    """硬闸：忽略之后一个条件都不剩 → 不给选项。那已经不是检索，是把整个库倒出来。"""
    res = wf.run_with_meta(query="霍格沃茨综合征的数据", use_llm=False, sources=ALL_SOURCES)
    if res.resolution_status == "abstained":
        assert res.degraded_search is None, res.degraded_search


def test_no_option_for_executable_or_empty_intersection(wf):
    """可执行、或「能执行但空交集」的查询都不该有降级建议——后者归引导式放宽管。"""
    ok = wf.run_with_meta(query="人类肺组织的单细胞数据", use_llm=False, sources=ALL_SOURCES)
    assert ok.resolution_status == "results" and ok.degraded_search is None
    # 「斑马鱼的乳腺癌数据」：两个词都认识、只是库里没有这个组合 → 空交集，归引导式放宽管
    # （注意别拿冻结评测的 nr02「斑马鱼的黑色素瘤数据」当载荷：那条的「无结果」前提是
    #  base-only 767 条，全库里它是有 1 条的）。
    empty = wf.run_with_meta(query="斑马鱼的乳腺癌数据", use_llm=False, sources=ALL_SOURCES)
    assert empty.resolution_status == "no_match" and empty.degraded_search is None
    assert empty.relaxation_options, "空交集仍应给出引导式放宽项"


def test_helper_returns_none_for_non_unresolved_abstain():
    """直接调：非 unresolved_term 的弃权不产生降级建议。

    `prepare` 传一个会炸的桩：正确实现应当在**调用 prepare 之前**就返回 None。

    2026-07-25 换了载荷：原来用「最好是 Xenium 的黑色素瘤数据」，而 hedge 现在按软偏好照做
    （与「优先 Xenium…」同解，55 条），那句已经不再弃权。换成「优先不要 X」——
    系统表达不了软性排除（做出来就是硬排除），这条 fail-closed 是写明的红线、不会再变。
    """
    intent = parse_query("优先不要小鼠的肺数据", CAT)
    assert intent.abstain and intent.abstain_reason != "unresolved_term"

    def _boom(_q):
        raise AssertionError("非 unresolved_term 弃权不该触发任何检索")

    assert build_degraded_search(intent, _boom) is None


# ---------- 数字必须与「点下去真跑的那次」同口径（2026-07-22 夜对抗评审 4 条同根发现）----------
def test_degraded_count_and_filters_honour_explicit_date_range(wf):
    """用户设了时间范围时，降级芯片的条数和「实际在筛的条件」必须把这个窗算进去。

    修前：`build_degraded_search` 自己 `parse_query` + `retrieve`，界面上设的时间范围
    整个丢失 —— 芯片写 3473 条，真实执行 807 条（虚高 4.3 倍），预览的 5 张卡一张都不在窗内。
    数字骗人是这一层唯一不能犯的错。
    """
    res = wf.run_with_meta(query="翼龙的单细胞数据", use_llm=False, sources=ALL_SOURCES,
                           date_from="2020-01-01", date_to="2021-12-31")
    deg = res.degraded_search
    assert deg is not None, "这句应当给出降级建议"
    labels = {f["label"] for f in deg["active_filters"]}
    assert "发表时间" in labels, f"降级建议漏掉了用户设的时间范围：{deg['active_filters']}"
    # 真的按它给的降级句跑一次，条数必须对得上
    truth = wf.run_with_meta(query=deg["query"], use_llm=False, sources=ALL_SOURCES,
                             date_from="2020-01-01", date_to="2021-12-31")
    assert deg["count"] == truth.result_total, (
        f"降级建议说 {deg['count']} 条，点下去实际 {truth.result_total} 条")


def test_degraded_count_honours_facet_filters(wf):
    """同一条不变量对分面成立：芯片说 N 条，点开必须就是 N 条。"""
    facets = [{"dim": "source", "value": "ArrayExpress"}]
    res = wf.run_with_meta(query="翼龙的单细胞数据", use_llm=False, sources=ALL_SOURCES,
                           facet_filters=facets)
    deg = res.degraded_search
    if deg is None:
        pytest.skip("该分面下没有降级建议")
    truth = wf.run_with_meta(query=deg["query"], use_llm=False, sources=ALL_SOURCES,
                             facet_filters=facets)
    assert deg["count"] == truth.result_total, (
        f"分面生效时降级建议说 {deg['count']} 条，实际 {truth.result_total} 条")


def test_degraded_suggestion_does_not_resurrect_suppressed_constraints(wf):
    """用户刚在「已命中」里删掉的条件，不该在降级建议的「实际在筛的条件」里复活。"""
    base = wf.run_with_meta(query="人类膀胱造瘘的单细胞数据", use_llm=False, sources=ALL_SOURCES)
    assert base.degraded_search and {"物种", "组织"} <= {
        f["label"] for f in base.degraded_search["active_filters"]}
    sup = wf.run_with_meta(query="人类膀胱造瘘的单细胞数据", use_llm=False, sources=ALL_SOURCES,
                           suppressed_constraints=["tissue"])
    if sup.degraded_search is None:
        return          # 忽略后没条件了 → 硬闸生效，也是正确行为
    labels = {f["label"] for f in sup.degraded_search["active_filters"]}
    assert "组织" not in labels, f"被用户删掉的条件在降级建议里复活了：{labels}"


# ---------- 结构性隔离：冻结评测不经编排层 ----------
def test_frozen_eval_path_never_sees_degraded_search():
    """官方评测走 parse_query + retriever.retrieve，不碰 workflow。
    这里用「解析器本身不产出任何降级结果」把这条隔离钉死：新字段只是词表，不改弃权行为。"""
    it = parse_query("翼龙的单细胞数据", CAT)
    assert it.abstain is True and it.parse_status == "abstained"
    assert it.constraints == {} and it.has_raw_data_required is None
