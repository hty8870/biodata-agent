# -*- coding: utf-8 -*-
"""RAG 工具组（rank / rerank）的常驻钉。

rank / rerank 常驻动词表与 LOOP_TOOLS 注册表；原环境开关与
OFF 逐位一致负向钉随代码一并摘除。

本文件钉：登记齐（动词表/豁免清单/注册表/返回契约/decide 面）、schema 形状、
display 布尔槽裁决、改写健全性检查三态、预算机械闸、联网归类（纯本地）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dataset_recommender.agent import action_plan as AP
from dataset_recommender.agent import agent_exec as AX
from dataset_recommender.agent import agent_schemas as SC


# ---------------------------------------------------------------- 登记齐

def test_verb_specs_registered():
    rank = AP.VERB_BY_NAME["rank"]
    rerank = AP.VERB_BY_NAME["rerank"]
    assert rank.kind == AP.EXEC and rank.slots == ("query", "display")
    assert rank.requires_results is False
    assert rerank.kind == AP.EXEC and rerank.slots == ("query", "reason", "display")
    assert rerank.requires_results is False
    # 尾部追加，旧动词一个不动；四工具追加在 route.request 之后。
    assert [s.verb for s in AP.VERB_SPECS[-4:]] == [
        "route.request", "compare.datasets", "compat.find", "fair.check"]
    assert "rank" in [s.verb for s in AP.VERB_SPECS] and "rerank" in [s.verb for s in AP.VERB_SPECS]
    # 刻意更新：curate.rollback 同为环内专属（回滚目标依赖本轮 steps
    # 实录的快照锚，前端单步 runner 没有这个现场）——补录豁免清单。
    # 刻意更新：compare.datasets / compat.find / fair.check 同为环内
    # 专属（默认对象依赖环内当前结果集现场）——补录豁免清单；cite.export 双通道不豁免。
    assert AP.FRONTEND_UNWIRED_EXEC_VERBS == ("search.rerun", "rank", "rerank",
                                              "curate.rollback",
                                              "compare.datasets", "compat.find",
                                              "fair.check")
    assert set(AP.FRONTEND_UNWIRED_EXEC_VERBS) <= set(AP.EXEC_VERBS)


def test_loop_tools_registered():
    for verb, card in (("rank", "rank"), ("rerank", "rerank")):
        spec = AX.LOOP_TOOLS[verb]
        assert spec["readonly"] is True
        assert spec["needs_context"] is True
        assert spec["card_kind"] == card
        assert callable(spec["run"]) and spec["decide_zh"]
    # decide 面：顺序表与注册表同集合（既有钉的延续）。
    assert set(AX._DECIDE_VERB_ORDER) == set(AX.LOOP_TOOLS)
    # 刻意更新：新四工具追加在 route.request 之后（顺序表尾部）。
    assert AX._DECIDE_VERB_ORDER[-5:] == (
        "route.request", "compare.datasets", "cite.export", "compat.find", "fair.check")
    names = [t["function"]["name"] for t in AX._DECIDE_TOOL_SPECS]
    assert "rank" in names and "rerank" in names
    assert AX._DECIDE_TOOL_NAME_TO_VERB["rank"] == "rank"
    assert AX._DECIDE_TOOL_NAME_TO_VERB["rerank"] == "rerank"
    # 返回契约登记。
    assert SC.LOOP_RESULT_MODELS["rank"] is SC.RankResult
    assert SC.LOOP_RESULT_MODELS["rerank"] is SC.RerankResult


def test_network_classification():
    """rank / rerank 跑本地管线不触网——联网暂停禁提面的**显式**归类钉
    （test_network_loop_tools_are_the_registry_minus_db_status 的对口）。
    刻意更新：curate.rollback 是本地文件操作（不触网），同归纯本地。
     刻意更新：compare/cite/compat/fair 全本地（结果处理不触网）。"""
    assert AX._NETWORK_LOOP_TOOLS == frozenset(
        set(AX.LOOP_TOOLS) - {"curate.db_status", "search.rerun", "rank", "rerank",
                              "route.request", "curate.rollback",
                              "compare.datasets", "cite.export",
                              "compat.find", "fair.check"})


# ---------------------------------------------------------------- schema 形状

def test_args_schema():
    rank_schema = SC.verb_parameters_schema(AP.VERB_BY_NAME["rank"])
    assert rank_schema["required"] == []  # 铁律：required 恒空
    assert rank_schema["properties"]["display"]["type"] == "boolean"
    assert "要检索的完整检索句" in rank_schema["properties"]["query"]["description"]
    rerank_schema = SC.verb_parameters_schema(AP.VERB_BY_NAME["rerank"])
    assert "原始" in rerank_schema["properties"]["query"]["description"]
    assert "优化检索词" in rerank_schema["properties"]["reason"]["description"]
    # search.rerun 的 query 描述逐字不变（既有钉的口径，本文件再钉一层防漂移）。
    rerun_schema = SC.verb_parameters_schema(AP.VERB_BY_NAME["search.rerun"])
    assert rerun_schema["properties"]["query"]["description"] == (
        "改写后的检索句：把当前查询换成规则更容易正确解析的说法，语义等价、"
        "不新增用户没表达的条件；当前没有可改的查询就不填。")


# ---------------------------------------------------------------- display 布尔槽裁决

def test_display_slot_adjudication():
    utter = "找找人类肺癌的数据"
    base = {"verb": "rank", "quoted": "找找人类肺癌的数据", "query": "human lung cancer"}
    plan = AP.build_plan_from_raw({**base, "display": True}, utter,
                                  has_results=False, result_total=0)
    assert plan["slots"].get("display") is True
    plan = AP.build_plan_from_raw({**base, "display": "TRUE"}, utter,
                                  has_results=False, result_total=0)
    assert plan["slots"].get("display") is True
    for bogus in (False, "false", "yes", 1, None):
        plan = AP.build_plan_from_raw({**base, "display": bogus}, utter,
                                      has_results=False, result_total=0)
        assert "display" not in plan["slots"], bogus


# ---------------------------------------------------------------- 改写健全性检查三态

class _FakeAnswer:
    def __init__(self, content):
        self.content = content


class _FakeModel:
    def __init__(self, content=None, exc=None):
        self._content, self._exc = content, exc

    def invoke(self, messages):
        if self._exc is not None:
            raise self._exc
        return _FakeAnswer(self._content)


def test_rewrite_query_sanity():
    # 模型缺席 → 退回原句，rewritten=False（如实标注，不静默）。
    assert AX._rewrite_query(None, "坏query") == ("坏query", False)
    # 正常改写 → 采纳。
    assert AX._rewrite_query(_FakeModel("mouse lung glioma"), "坏query") == (
        "mouse lung glioma", True)
    # 健谈模型多吐解释行 → 只取首行；包裹引号剥掉。
    assert AX._rewrite_query(_FakeModel('"mouse lung"\n因为……'), "坏query") == (
        "mouse lung", True)
    # 三态退回：空 / 与原句相同 / 超 200 字符 / 调用异常。
    assert AX._rewrite_query(_FakeModel(""), "坏query") == ("坏query", False)
    assert AX._rewrite_query(_FakeModel("坏query"), "坏query") == ("坏query", False)
    assert AX._rewrite_query(_FakeModel("x" * 201), "坏query") == ("坏query", False)
    assert AX._rewrite_query(_FakeModel(exc=RuntimeError("boom")), "坏query") == (
        "坏query", False)


def test_rewrite_query_records_usage():
    """rerank 的独立改写是真实 LLM 调用，给了 usage_sink
    就必须过 `_usage_record` 进账（末端聚合 plan.llm_usage）；读不到用量自然跳过。"""

    class _UsageModel:
        def invoke(self, messages):
            return SimpleNamespace(
                content="mouse lung",
                usage_metadata={"input_tokens": 8, "output_tokens": 2,
                                "input_token_details": {"cache_read": 3}})

    sink: list = []
    assert AX._rewrite_query(_UsageModel(), "坏query", usage_sink=sink) == (
        "mouse lung", True)
    assert sink == [{"node": "rerank_rewrite", "input": 8, "cache_read": 3, "output": 2}]
    # 无用量的替身：台账保持空（不伪造）。
    sink = []
    AX._rewrite_query(_FakeModel("mouse lung"), "坏query", usage_sink=sink)
    assert sink == []


# ---------------------------------------------------------------- 工具本体（管线替身）

def _fake_meta(total=7, rows=None, filters=None):
    # active_filters 用**生产真源形状**（workflow 投影字典的列表）——dict 形替身曾掩盖
    # 「dict() 强转列表必炸」的真 bug（run2 复盘）。
    return SimpleNamespace(
        result_total=total,
        active_filters=filters if filters is not None else [
            {"filter_id": "include:species", "polarity": "include", "dim": "species",
             "label": "物种", "values": ["Human"]},
        ],
        retrieved_data=rows if rows is not None else [
            {"dataset_name": f"DS{i}", "species": "Human", "tissue": "lung",
             "disease": "adenocarcinoma", "source": "GEO"} for i in range(5)
        ],
    )


@pytest.fixture
def fake_pipeline(monkeypatch):
    """替身标准管线：run_with_meta 返假 meta；recommend_payload 返最小同形 dict。"""
    import dataset_recommender.app.workflow as wf
    import dataset_recommender.app.recommend_rows as rr

    calls: list[dict] = []

    class _FakeFlow:
        def run_with_meta(self, p=None, **kwargs):
            # 生产调用点传 RecommendParams（位置参数）；兼容 kwargs 以防旧风格。
            calls.append(vars(p) if p is not None else kwargs)
            return _fake_meta()

    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _FakeFlow)
    monkeypatch.setattr(rr, "recommend_payload", lambda meta: {
        "ok": True, "result_total": meta.result_total,
        "query_constraints": meta.active_filters,
        "results": [{"dataset_name": "DS0", "species": "Human", "tissue": "lung",
                     "disease": "adenocarcinoma", "source": "GEO"}],
    })
    return AX, calls


def test_loop_rank_no_display_skips_payload(fake_pipeline):
    ax, calls = fake_pipeline
    out = ax._loop_rank({"query": "human lung cancer"}, None, {"search_sources": None})
    assert out["query"] == "human lung cancer"
    assert out["total"] == 7
    assert out["filters"] == [
        {"filter_id": "include:species", "polarity": "include", "dim": "species",
         "label": "物种", "values": ["Human"]},
    ]
    assert len(out["top"]) == 3 and out["top"][0]["dataset_name"] == "DS0"
    assert out["displayed"] is False and out["batch"] is None
    assert calls and calls[0]["query"] == "human lung cancer"
    assert calls[0]["use_llm"] is False
    assert "rerank_audit" not in calls[0]


def test_loop_rank_display_produces_batch(fake_pipeline):
    ax, calls = fake_pipeline
    out = ax._loop_rank({"query": "human lung cancer", "display": True}, None,
                        {"search_sources": None, "utterance": "找找肺癌的数据"})
    assert out["displayed"] is True
    batch = out["batch"]
    assert batch["kind"] == "rank"
    assert batch["label"] == "human lung cancer"
    # query_raw = 本轮用户原话（契约）——ctx 带 utterance
    # 时绝不许填成模型产出的 rank query；ctx 缺席（直调/测试）退回 query。
    assert batch["query_raw"] == "找找肺癌的数据"
    assert batch["query_effective"] == "human lung cancer"
    out2 = ax._loop_rank({"query": "human lung cancer", "display": True}, None,
                         {"search_sources": None})
    assert out2["batch"]["query_raw"] == "human lung cancer"
    assert batch["payload"]["result_total"] == 7
    # display=true 时 top digest 取自卡片行（与载荷同投影）。
    assert out["top"][0]["dataset_name"] == "DS0"
    # 返回契约形状闸：登记模型能接住真实返回。
    ax._LOOP_RESULT_MODELS["rank"].model_validate(out)


def test_loop_rank_empty_query_raises(fake_pipeline):
    ax, calls = fake_pipeline
    with pytest.raises(AX._SearchRerunParamError):
        ax._loop_rank({"query": "  "}, None, {})


def test_loop_rerank_rewritten(fake_pipeline, monkeypatch):
    ax, calls = fake_pipeline
    monkeypatch.setattr(ax, "_rewrite_query",
                        lambda model, q, **_: ("human lung adenocarcinoma", True))
    out = ax._loop_rerank({"query": "那个肺癌的数据", "display": True}, None,
                          {"chat_model": object(), "utterance": "那个肺癌的数据有没有"})
    assert out["original_query"] == "那个肺癌的数据"
    assert out["rewritten_query"] == "human lung adenocarcinoma"
    assert out["rewritten"] is True
    assert calls[0]["query"] == "human lung adenocarcinoma"
    batch = out["batch"]
    assert batch["kind"] == "rerank"
    assert batch["label"] == "human lung adenocarc"  # label = 生效的 rewritten_query（≤20 字截断）
    # query_raw = 本轮用户原话；原始坏 query 在结果顶层 original_query 键里。
    assert batch["query_raw"] == "那个肺癌的数据有没有"
    assert batch["query_effective"] == "human lung adenocarcinoma"
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)


def test_loop_rerank_fallback_to_original(fake_pipeline, monkeypatch):
    ax, calls = fake_pipeline
    monkeypatch.setattr(ax, "_rewrite_query", lambda model, q, **_: (q, False))
    out = ax._loop_rerank({"query": "坏query"}, None, {"chat_model": None})
    assert out["rewritten"] is False
    assert out["rewritten_query"] == "坏query"
    assert calls[0]["query"] == "坏query"


# ---------------------------------------------------------------- 结构化条件契约（设计约定）
#
# 环内 rank/rerank 只传 query/sources 曾丢失 facet/suppressed/lenient/date 结构化条件，
# 造成「同词重跑却放宽条件、uid 集合变化、弱批顶掉更优批」。修复后必须原样携带并 fail-closed。

def test_loop_rank_carries_structured_conditions(fake_pipeline):
    """（设计约定）：rank 把 facet/suppressed/lenient/date 原样带进管线——
    缺任何一项都是弱批顶掉好结果的回归。"""
    ax, calls = fake_pipeline
    ctx = {
        "search_sources": ["GEO"],
        "search_facet_filters": [{"dim": "species", "value": "homo sapiens"}],
        "search_suppressed_constraints": ["exclude:species"],
        "search_lenient_dims": ["disease"],
        "search_date_from": "2020-01-01",
        "search_date_to": "2024-12-31",
    }
    ax._loop_rank({"query": "human lung cancer", "display": True}, None, ctx)
    assert calls and calls[0]["facet_filters"] == [{"dim": "species", "value": "homo sapiens"}]
    assert calls[0]["suppressed_constraints"] == ["exclude:species"]
    assert calls[0]["lenient_dims"] == ["disease"]
    assert calls[0]["date_from"] == "2020-01-01"
    assert calls[0]["date_to"] == "2024-12-31"


def test_loop_rank_scope_kept_backfills_applied(fake_pipeline, monkeypatch):
    """（设计约定）：scope 完整保留时，payload 回填 applied_*（与 search.rerun 同义务）
    且显式日期出现在 interpretation 投影里（逐位相等才出批）。"""
    ax, calls = fake_pipeline
    monkeypatch.setattr(
        "dataset_recommender.app.recommend_rows.recommend_payload",
        lambda meta: {
            "ok": True, "result_total": meta.result_total, "query_constraints": meta.active_filters,
            "results": [{"dataset_name": "DS0", "species": "Human", "tissue": "lung",
                         "disease": "adenocarcinoma", "source": "GEO"}],
            "interpretation": {"intent": {"date_from": "2020-01-01", "date_to": "2024-12-31"}},
        },
    )
    ctx = {"search_sources": None, "search_date_from": "2020-01-01", "search_date_to": "2024-12-31",
           "search_facet_filters": [{"dim": "species", "value": "homo sapiens"}]}
    out = ax._loop_rank({"query": "human lung", "display": True}, None, ctx)
    assert out["batch"] is not None and out["batch"]["payload"]["ok"] is True
    assert out["batch"]["payload"]["applied_facets"] == [{"dim": "species", "value": "homo sapiens"}]
    assert out["batch"]["payload"]["applied_suppressed"] == []
    assert out["batch"]["payload"]["applied_lenient"] == []


def test_loop_rank_fail_closed_when_date_lost(fake_pipeline):
    """（设计约定）：显式日期在 interpretation 投影里带不出 → fail-closed——不回填 batch、
    如实带结构化标记，绝不放宽重跑顶掉已上屏的好结果。"""
    ax, calls = fake_pipeline  # fake recommend_payload 无 interpretation → 显式日期必然带不出
    ctx = {"search_sources": None, "search_date_from": "2020-01-01", "search_date_to": "2024-12-31"}
    out = ax._loop_rank({"query": "human lung", "display": True}, None, ctx)
    assert out["batch"] is None
    assert out["structured_context_lost"] is True
    assert out["disclosure_zh"]


def test_loop_rerank_carries_structured_conditions(fake_pipeline, monkeypatch):
    """（设计约定）：rerank 的改写只换检索句，结构化条件同样原样带进管线。"""
    ax, calls = fake_pipeline
    monkeypatch.setattr(ax, "_rewrite_query", lambda model, q, **_: ("human lung adenocarcinoma", True))
    ctx = {"search_sources": ["GEO"], "search_facet_filters": [{"dim": "tissue", "value": "lung"}],
           "search_lenient_dims": ["disease"], "search_date_from": "2021-01-01"}
    ax._loop_rerank({"query": "肺癌的数据", "display": True}, None, ctx)
    assert calls and calls[0]["facet_filters"] == [{"dim": "tissue", "value": "lung"}]
    assert calls[0]["lenient_dims"] == ["disease"]
    assert calls[0]["date_from"] == "2021-01-01"


def test_loop_rerank_fail_closed_when_date_lost(fake_pipeline, monkeypatch):
    """（设计约定）：rerank 同样 fail-closed——显式日期带不出则不出批。"""
    ax, calls = fake_pipeline
    monkeypatch.setattr(ax, "_rewrite_query", lambda model, q, **_: ("human lung", True))
    ctx = {"search_sources": None, "search_date_from": "2020-01-01"}
    out = ax._loop_rerank({"query": "肺癌", "display": True}, None, ctx)
    assert out["batch"] is None
    assert out["structured_context_lost"] is True


# ---------------------------------------------------------------- 预算机械闸

def _state_with_steps(verbs):
    return {"utterance": "找找人类肺癌的数据",
            "steps": [{"verb": v, "ok": True} for v in verbs]}


def test_rank_budget_gate():
    assert AX.MAX_RANK == 2
    state = _state_with_steps(["rank", "rank"])
    nxt, note, refused, violation = AX._adjudicate_decide_obj(
        {"verb": "rank", "quoted": "找找人类肺癌的数据", "query": "x"}, state)
    assert nxt is None and violation == ""
    assert "最多新检索" in refused and "预算已用完" in refused
    # 未用满 → 放行。
    nxt, *_ = AX._adjudicate_decide_obj(
        {"verb": "rank", "quoted": "找找人类肺癌的数据", "query": "human lung cancer"},
        _state_with_steps(["rank"]))
    assert nxt is not None and nxt["verb"] == "rank"


def test_rerank_budget_gate():
    assert AX.MAX_RERANK == 1
    state = _state_with_steps(["rerank"])
    nxt, note, refused, violation = AX._adjudicate_decide_obj(
        {"verb": "rerank", "quoted": "找找人类肺癌的数据", "query": "x"}, state)
    assert nxt is None and violation == ""
    assert "最多优化重检" in refused and "预算已用完" in refused


def test_budget_counters_count_failures_too():
    """提议过即消耗（不论成败）——防「失败换个说法再提」绕过上限空转。"""
    steps = [{"verb": "rank", "ok": False}, {"verb": "rank", "ok": True}]
    assert AX._rank_used(steps) == 2
    assert AX._rerank_used([{"verb": "rerank", "ok": False}]) == 1
    # 预算注入段存在且与 search.rerun 段同构（拼装进双壳的逻辑与既有段同一代码路径）。
    assert "新检索预算已用完" in AX._RANK_BUDGET_BLOCK_ZH
    assert "优化重检预算已用完" in AX._RERANK_BUDGET_BLOCK_ZH


# ---------------------------------------------------------------- 同批预算绕过

def test_batch_extras_count_against_rag_budget():
    """decide 侧：同批只读消费的第 2..N 个调用必须
    对「已执行 + 首步 + 已采纳同批步」的合成 steps 增量裁决——对原始 state 裁决时
    一枚 decide 回 3 个 rerank 会全过（MAX_RERANK=1 形同虚设）。"""
    state = {"utterance": "找找人类肺癌的数据", "steps": []}
    calls = [{"name": "rank", "args": {"query": q, "quoted": "找找人类肺癌的数据"}}
             for q in ("a", "b", "c")]
    accepted, dropped = AX._batch_readonly_extras(
        calls, {"verb": "rank", "query": "a", "quoted": "找找人类肺癌的数据"}, state)
    # MAX_RANK=2：首步占 1，同批至多再放行 1 个；第 3 个被预算闸机械剔除。
    assert [r["query"] for r in accepted] == ["b"] and dropped == 1
    # MAX_RERANK=1：首步已用满，同批 rerank 一个都不许过。
    calls = [{"name": "rerank", "args": {"query": q, "quoted": "找找人类肺癌的数据"}}
             for q in ("a", "b", "c")]
    accepted, dropped = AX._batch_readonly_extras(
        calls, {"verb": "rerank", "query": "a", "quoted": "找找人类肺癌的数据"}, state)
    assert accepted == [] and dropped == 2


def test_execute_batch_fuse_rechecks_rag_budget(monkeypatch):
    """execute 侧（与批内熔连同哲学）：主步/前序
    extra 真消耗预算后，后续 extra 执行前用当前实录重过预算闸——被剔的不执行、
    不记步、trace 如实留痕。"""
    monkeypatch.setattr(AX, "_audit_loop_tool", lambda *a, **k: None)
    monkeypatch.setitem(AX.LOOP_TOOLS["rank"], "run",
                        lambda slots, root, ctx=None: {
                            "query": str((slots or {}).get("query") or ""), "total": 1})
    runtime = SimpleNamespace(context=SimpleNamespace(on_progress=None, chat_model=None))
    state = {
        "utterance": "找找人类肺癌的数据", "plan": {"verb": "none"},
        "steps": [{"verb": "rank", "ok": True, "slots": {"query": "old"}}],
        "loop_plan": {"verb": "rank", "slots": {"query": "a"}},
        "loop_batch": [{"verb": "rank", "slots": {"query": "b"}}],
    }
    out = AX.execute(state, runtime=runtime)
    # 既有 1 + 主步 1 = MAX_RANK(2) 用满 → 同批 extra 熔断：只记主步。
    assert [s["slots"]["query"] for s in out["steps"]] == ["a"]
    assert any("批内熔断" in str(t.get("label_zh") or "") and "预算" in str(t.get("detail") or "")
               for t in out["trace"])


# ---------------------------------------------------------------- 返回契约形状闸

def test_result_models_shape():
    with pytest.raises(Exception):
        SC.RankResult.model_validate({"query": "x"})  # 缺 total
    ok = SC.RankResult.model_validate({"query": "x", "total": 3})
    assert ok.displayed is False and ok.batch is None and ok.top == []
    with pytest.raises(Exception):
        SC.RerankResult.model_validate({"original_query": "x", "total": 1})  # 缺 rewritten*
    ok = SC.RerankResult.model_validate({
        "original_query": "x", "rewritten_query": "y", "rewritten": True, "total": 1})
    assert ok.rewritten is True
