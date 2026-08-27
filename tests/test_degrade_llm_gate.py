# -*- coding: utf-8 -*-
"""LLM 把关档：让 LLM 决定「这几个未收录词能不能忽略」（用户想法 1+2）。

用户的原话是「自动放宽 + 给 LLM 执行零返回的权力」。这里落成它的 **fail-closed 镜像**：
默认不降级，LLM 说可以才降级。

  · LLM 正常工作时，两种写法结果完全一样（同一个判断点、同一个判据）；
  · LLM 挂了 / 超时 / 输出解析不出来时，差别是决定性的——
    「默认降级 + 可否决」会返回 3473 条无关数据，「默认不降级 + 需批准」返回诚实的弃权。

本项目的产品底线（冻结评测 nr/adv 组：查无此物就如实说没有）要求后者。
本文件把这条不变量、以及三条路径（批准 / 否决 / LLM 缺席）钉死。
"""
import json

import pytest

from dataset_recommender.retrieval import rerank as R
from dataset_recommender.llm.config import get_settings
from dataset_recommender.corpus.corpus import known_source_values
from dataset_recommender.app.workflow import DatasetRecommendationWorkflow

S = get_settings()
ALL_SOURCES = known_source_values(S.data_dir, S.project_root)
QUERY = "人类膀胱造瘘的单细胞数据"     # 「造瘘」未收录 → 规则弃权 → 有降级建议


@pytest.fixture(scope="module")
def wf():
    return DatasetRecommendationWorkflow()


def _stub(monkeypatch, payload):
    monkeypatch.setattr(R, "_default_llm_call", lambda prompt, config: payload)


def test_approved_drop_returns_degraded_not_results(wf, monkeypatch):
    _stub(monkeypatch, json.dumps({"drop_ok": True, "reason": "造瘘只是术式修饰"}, ensure_ascii=False))
    res = wf.run_with_meta(query=QUERY, use_llm=False, sources=ALL_SOURCES, degrade_with_llm=True)
    # 关键：状态是 "degraded"，**不是** "results"。混进 results 就等于把降级结果冒充精确匹配。
    assert res.resolution_status == "degraded"
    assert res.retrieved_data and res.result_total > 0
    assert res.degraded_search["applied"] is True
    assert res.degraded_search["llm_verdict"] is True
    assert res.degraded_search["llm_reason"]
    assert "造瘘" in (res.fallback_reason or "")


def test_vetoed_drop_keeps_the_abstain(wf, monkeypatch):
    _stub(monkeypatch, json.dumps({"drop_ok": False, "reason": "膀胱是核心限定"}, ensure_ascii=False))
    res = wf.run_with_meta(query=QUERY, use_llm=False, sources=ALL_SOURCES, degrade_with_llm=True)
    assert res.resolution_status == "abstained"
    assert res.retrieved_data == [] and res.result_total == 0
    assert res.degraded_search["applied"] is False
    assert res.degraded_search["llm_verdict"] is False
    # 建议本身仍在（用户可以自己点），只是没有被自动应用
    assert res.degraded_search["count"] > 0


@pytest.mark.parametrize("payload", [None, "", "抱歉我不确定", "{}", '{"drop_ok": "yes"}', "[1,2,3]"])
def test_llm_absent_or_garbled_stays_closed(wf, monkeypatch, payload):
    """fail-closed 的全部含义：把关的人不在场、或说了句听不懂的话，一律不放行。"""
    _stub(monkeypatch, payload)
    res = wf.run_with_meta(query=QUERY, use_llm=False, sources=ALL_SOURCES, degrade_with_llm=True)
    assert res.resolution_status == "abstained", payload
    assert res.retrieved_data == []
    assert res.degraded_search["applied"] is False
    assert res.degraded_search["llm_verdict"] is not True


def test_off_by_default_never_calls_the_llm(wf, monkeypatch):
    """默认关：不传 degrade_with_llm 时连 LLM 通道都不该碰（离线可用是本项目的基本盘）。"""
    called = []
    monkeypatch.setattr(R, "_default_llm_call", lambda prompt, config: called.append(1) or "{}")
    res = wf.run_with_meta(query=QUERY, use_llm=False, sources=ALL_SOURCES)
    assert called == [], "默认档不应发生任何 LLM 调用"
    assert res.resolution_status == "abstained"
    assert "llm_verdict" not in (res.degraded_search or {})


# ---------- 纯函数层 ----------
def test_parse_drop_terms_response_is_tolerant():
    assert R.parse_drop_terms_response('{"drop_ok": true, "reason": "ok"}') == (True, "ok")
    assert R.parse_drop_terms_response('前言\n{"drop_ok": false, "reason": "不行"}\n后记') == (False, "不行")
    assert R.parse_drop_terms_response("完全不是 JSON") == (None, "")
    assert R.parse_drop_terms_response('{"drop_ok": 1}') == (None, "")   # 非 bool 不认


def test_judge_drop_terms_never_raises():
    def boom(_prompt):
        raise RuntimeError("network down")
    assert R.judge_drop_terms("q", ignored_terms=["x"], llm_call=boom) == (None, "")


def test_prompt_states_the_actual_stakes():
    """prompt 必须把「忽略后还剩什么条件」和「会命中多少条」都摆给 LLM——
    只问「能不能忽略这个词」而不给这两个数，LLM 没法判断『剩下的太宽泛』这一条判据。"""
    p = R.build_drop_terms_prompt("人类膀胱造瘘的单细胞数据", ["造瘘"], "物种=Human、组织=Bladder", 24)
    assert "造瘘" in p and "物种=Human" in p and "24" in p
    assert "drop_ok" in p
