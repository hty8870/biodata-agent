"""rerank_audit 专项：解析容错、审核 sink、改写重搜择优、fail-open、冻结路径隔离。

覆盖三层：
- 纯函数：parse_order 逐位等价 / parse_audit_response 正常·退化·脏·越界 / _validated_rewrite 守卫。
- rerank_candidates：注入 llm_call → audit_ctx 写回 verdict/rewrite；audit_ctx=None → 纯重排不变；
  llm 无输出 → fail-open（attempted 保持 False、原序）。
- workflow 集成：改写采纳 / 判 OK 保持 / 改空退回 / 未开为 None / rerank=off 不触发（self-gating）。
- 空池/弃权档：空池独立审核（mode="empty"）已删（检索工具化 Phase 1）——
  空池救回改由 search.rerun 工具承担；钉「零命中恒 not_triggered」的新行为与
  rerank=off / auto 未授权 / clarification 三个「不触发」旧口径。
- 发现A 空转改写：ride-along 与空池两路的"原句+填充词"伪改写都被 _validated_rewrite 过滤、不采纳。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval import rerank as rr  # noqa: E402
from dataset_recommender.app.workflow import DatasetRecommendationWorkflow  # noqa: E402


# ----------------------------- 纯函数 -----------------------------

def _old_parse_order(text, n):
    order, seen = [], set()
    for token in re.findall(r"\d+", text or ""):
        try:
            one = int(token)
        except ValueError:
            continue
        z = one - 1
        if 0 <= z < n and z not in seen:
            seen.add(z); order.append(z)
    return order


@pytest.mark.parametrize("text,n", [
    ("[3,1,2]", 3), ("3 1 2 2 1", 3), ("", 5), ("[10, 2]", 3),
    ("乱 7 序 1 文 3 字 99", 5), (None, 4), ("[1,2,3,4,5]", 5),
])
def test_parse_order_byte_identical(text, n):
    assert rr.parse_order(text, n) == _old_parse_order(text, n)


def test_parse_audit_normal():
    o, v, rw = rr.parse_audit_response('{"order":[3,1,2],"keywords_ok":false,"rewrite":"人 脑 单细胞"}', 3)
    assert o == [2, 0, 1] and v is False and rw == "人 脑 单细胞"


def test_parse_audit_keywords_ok_no_rewrite():
    o, v, rw = rr.parse_audit_response('{"order":[1,2,3],"keywords_ok":true,"rewrite":""}', 3)
    assert v is True and rw == ""


def test_parse_audit_fenced_json():
    o, v, rw = rr.parse_audit_response('```json\n{"order":[2,1],"keywords_ok":false,"rewrite":"改写句"}\n```', 2)
    assert o == [1, 0] and rw == "改写句"


def test_parse_audit_degraded_to_order_only():
    # 完全不是 JSON → 退化用 parse_order 抽排列，审核字段留空（重排不因审核格式失效）
    o, v, rw = rr.parse_audit_response('我觉得顺序是 2, 1, 3', 3)
    assert o == [1, 0, 2] and v is None and rw == ""


def test_parse_audit_dirty_and_oob():
    assert rr.parse_audit_response('', 3) == ([], None, "")
    o, _, _ = rr.parse_audit_response('{"order":[9,2,2,1,0],"keywords_ok":false,"rewrite":"x"}', 3)
    assert o == [1, 0]   # 9/0越界丢、重复2丢


@pytest.mark.parametrize("blob", [
    '{"order":[1e400,1,2],"keywords_ok":false,"rewrite":"r"}',   # 1e400 → json.loads → inf
    '{"order":[Infinity,2,1],"keywords_ok":false,"rewrite":"r"}',  # Infinity 字面量 → inf
    '{"order":[-Infinity,1],"keywords_ok":true,"rewrite":""}',
])
def test_parse_audit_infinity_order_no_crash(blob):
    # 回归（验证）：order 含 inf（int(inf) 抛 OverflowError）绝不能穿透 fail-open 链。
    o, v, rw = rr.parse_audit_response(blob, 3)          # 不抛异常
    assert all(isinstance(i, int) and 0 <= i < 3 for i in o)  # inf 被丢，其余合法


def test_rerank_candidates_infinity_order_failopen():
    cands = _fake_candidates(3)
    ctx = {"keywords": "k", "vocab_hint": ""}
    call = lambda p: '{"order":[1e400,2,3,1],"keywords_ok":false,"rewrite":"人类 脑"}'
    out = rr.rerank_candidates("q", cands, backend="llm", config=None, llm_call=call, audit_ctx=ctx)  # 不抛
    assert ctx["attempted"] is True and ctx["rewrite"] == "人类 脑"
    assert len(out) == 3 and {c.record.dataset_name for c in out} == {"DS0", "DS1", "DS2"}


def test_validated_rewrite_guards():
    assert rr._validated_rewrite("  ", "q") == ""
    assert rr._validated_rewrite("q", "q") == ""
    assert rr._validated_rewrite("x" * 201, "q") == ""
    assert rr._validated_rewrite("  人 脑  ", "q") == "人 脑"


def test_validated_rewrite_noop_filler_rejected():
    # 发现A：LLM 改不进规则维度时憋出的"原句+填充词"伪改写 → 去填充词后同核 → 判空、不采纳。
    assert rr._validated_rewrite("人类免疫细胞的单细胞转录组数据", "人类免疫细胞的单细胞转录组") == ""
    assert rr._validated_rewrite("人类免疫细胞的单细胞转录组相关研究", "人类免疫细胞的单细胞转录组") == ""
    assert rr._validated_rewrite("我想找一些关于人类肺癌的数据", "人类肺癌") == ""
    # 真改写（换成规则词面/去掉未建模填充实义词）仍保留
    assert rr._validated_rewrite("小鼠 大脑 单细胞", "小鼠大脑发育的scRNA-seq") == "小鼠 大脑 单细胞"
    assert rr._validated_rewrite("乳腺癌 单细胞", "乳腺癌病人的肿瘤微环境单细胞图谱") == "乳腺癌 单细胞"


# ----------------------------- 空池独立审核档：已删（检索工具化 Phase 1）-----------------------------

def test_empty_pool_independent_audit_branch_removed():
    """空池独立审核档（rerank.audit_query_only 族四件） 随「检索工具化」删除——
    空池救回改由 search.rerun 工具承担（agent 显式调用 + 机械择优闸），审核不再脱离
    重排静默单发。本钉锁住这四件不再回到注册面（复活 = 有意决策，须同步改本钉与设计文档）。"""
    for name in ("audit_query_only", "build_query_audit_prompt",
                 "parse_query_audit_response", "_grade_query_audit_text"):
        assert not hasattr(rr, name), f"{name} 应随空池独立审核档一并删除"


def test_same_hard_filter_order_insensitive():
    # 验证 回归：约束是 OR 匹配、顺序无关；同一实体集换 alias 会翻转多值列表顺序 →
    # 必须按集合比，否则「人和小鼠」vs「人类和小鼠」被误判为"改变了结果"、误采纳空转改写。
    from dataset_recommender.app.workflow import _same_hard_filter

    def intent(**kw):
        base = dict(abstain=False, constraints={}, excluded_constraints={},
                    has_raw_data_required=None, date_from="", date_to="")
        base.update(kw)
        return SimpleNamespace(**base)

    a = intent(constraints={"species": ["mus musculus", "homo sapiens"]})
    b = intent(constraints={"species": ["homo sapiens", "mus musculus"]})  # 同集、逆序
    assert _same_hard_filter(a, b) is True
    c = intent(constraints={"species": ["homo sapiens"]})                  # 真不同（少一个）
    assert _same_hard_filter(a, c) is False
    # excluded 同集不同序 → 判同；raw/时间不同 → 判异
    d = intent(excluded_constraints={"disease": ["a", "b"]})
    e = intent(excluded_constraints={"disease": ["b", "a"]})
    assert _same_hard_filter(d, e) is True
    assert _same_hard_filter(intent(has_raw_data_required=True), intent(has_raw_data_required=False)) is False
    assert _same_hard_filter(intent(date_from="2020-01-01"), intent()) is False
    # 弃权态：都弃权 → 同；一弃权一执行 → 异
    assert _same_hard_filter(intent(abstain=True), intent(abstain=True)) is True
    assert _same_hard_filter(intent(abstain=True), a) is False


# ----------------------------- build_rerank_prompt -----------------------------

def _fake_candidates(n):
    out = []
    for i in range(n):
        rec = SimpleNamespace(
            dataset_name=f"DS{i}", species="human", tissue="brain", disease="-",
            platform_family="chromium", assay="gex", description="desc",
        )
        out.append(SimpleNamespace(record=rec))
    return out


def test_prompt_off_has_no_audit_markers():
    p = rr.build_rerank_prompt("查询", _fake_candidates(3))
    assert "keywords_ok" not in p and "审核" not in p and "JSON 整数数组" in p


def test_prompt_audit_has_keywords_and_contract():
    p = rr.build_rerank_prompt("查询", _fake_candidates(3), audit_keywords="species=Human", vocab_hint="物种：Human")
    assert "keywords_ok" in p and "species=Human" in p and "物种：Human" in p


# ----------------------------- rerank_candidates -----------------------------

def test_rerank_audit_ctx_populated():
    cands = _fake_candidates(3)
    ctx = {"keywords": "species=Human", "vocab_hint": "物种：Human"}
    call = lambda p: '{"order":[2,3,1],"keywords_ok":false,"rewrite":"人类 大脑"}'
    out = rr.rerank_candidates("q", cands, backend="llm", config=None, llm_call=call, audit_ctx=ctx)
    assert ctx["attempted"] is True
    assert ctx["verdict"] is False
    assert ctx["rewrite"] == "人类 大脑"
    assert [c.record.dataset_name for c in out] == ["DS1", "DS2", "DS0"]  # order 2,3,1 → idx1,2,0


def test_rerank_audit_none_is_plain_rerank():
    cands = _fake_candidates(3)
    call = lambda p: "[3,2,1]"
    out = rr.rerank_candidates("q", cands, backend="llm", config=None, llm_call=call, audit_ctx=None)
    assert [c.record.dataset_name for c in out] == ["DS2", "DS1", "DS0"]


def test_rerank_audit_failopen_no_llm_output():
    cands = _fake_candidates(3)
    ctx = {"keywords": "k", "vocab_hint": ""}
    out = rr.rerank_candidates("q", cands, backend="llm", config=None, llm_call=lambda p: None, audit_ctx=ctx)
    assert ctx["attempted"] is False and ctx["rewrite"] == ""      # 无输出 → 未审核
    assert [c.record.dataset_name for c in out] == ["DS0", "DS1", "DS2"]  # 原序


# ----------------------------- workflow 集成 -----------------------------

@pytest.fixture(scope="module")
def wf():
    return DatasetRecommendationWorkflow()


@pytest.fixture(scope="module")
def good_pair(wf):
    """探两个都能出结果、且结果集不同的查询，用于验证"改写切换了结果集"。"""
    probes = ["human blood", "mouse brain", "human liver", "小鼠 肝脏", "人类 大脑"]
    ok = [(q, len(wf.run_with_meta(query=q, use_llm=False).retrieved_data)) for q in probes]
    ok = [q for q, n in ok if n > 0]
    assert len(ok) >= 2, f"探测查询不足以覆盖测试：{ok}"
    return ok[0], ok[1]


def _names(res):
    return {d["dataset_name"] for d in res.retrieved_data}


def test_workflow_audit_off_returns_none(wf, good_pair):
    q, _ = good_pair
    res = wf.run_with_meta(query=q, use_llm=False, rerank_backend="llm")
    assert res.audit is None


def test_workflow_audit_rewrite_adopted(wf, good_pair, monkeypatch):
    q_orig, q_rw = good_pair
    orig = _names(wf.run_with_meta(query=q_orig, use_llm=False))
    rw = _names(wf.run_with_meta(query=q_rw, use_llm=False))

    def mock(prompt, config):
        if "keywords_ok" in prompt:
            return json.dumps({"order": [1], "keywords_ok": False, "rewrite": q_rw}, ensure_ascii=False)
        return "[1,2,3,4,5]"
    monkeypatch.setattr(rr, "_default_llm_call", mock)

    res = wf.run_with_meta(query=q_orig, use_llm=False, rerank_backend="llm", rerank_audit=True)
    assert res.audit["triggered"] is True
    assert res.audit["used"] is True
    assert res.audit["reason"] == "rewritten"
    assert res.audit["rewritten_query"] == q_rw
    assert _names(res) == rw and _names(res) != orig


def test_workflow_audit_keywords_ok_keeps_original(wf, good_pair, monkeypatch):
    q_orig, _ = good_pair
    orig = _names(wf.run_with_meta(query=q_orig, use_llm=False))

    def mock(prompt, config):
        if "keywords_ok" in prompt:
            return json.dumps({"order": [1], "keywords_ok": True, "rewrite": ""}, ensure_ascii=False)
        return "[1,2,3,4,5]"
    monkeypatch.setattr(rr, "_default_llm_call", mock)

    res = wf.run_with_meta(query=q_orig, use_llm=False, rerank_backend="llm", rerank_audit=True)
    assert res.audit["used"] is False
    assert res.audit["reason"] == "keywords_ok"
    assert _names(res) == orig


def test_workflow_audit_verdict_true_with_rewrite_ignored(wf, good_pair, monkeypatch):
    # 回归（验证）：LLM 自相矛盾地给 keywords_ok=true + 非空改写 → 信 verdict、不改写，
    # 决策对象保持自洽（verdict=True 不伴随 used=True/reason=rewritten）。
    q_orig, q_rw = good_pair
    orig = _names(wf.run_with_meta(query=q_orig, use_llm=False))

    def mock(prompt, config):
        if "keywords_ok" in prompt:
            return json.dumps({"order": [1], "keywords_ok": True, "rewrite": q_rw}, ensure_ascii=False)
        return "[1,2,3,4,5]"
    monkeypatch.setattr(rr, "_default_llm_call", mock)

    res = wf.run_with_meta(query=q_orig, use_llm=False, rerank_backend="llm", rerank_audit=True)
    assert res.audit["verdict"] is True
    assert res.audit["used"] is False
    assert res.audit["reason"] == "keywords_ok"
    assert res.audit["rewritten_query"] == ""
    assert _names(res) == orig


def test_workflow_audit_rewrite_empty_kept_original(wf, good_pair, monkeypatch):
    q_orig, _ = good_pair
    orig = _names(wf.run_with_meta(query=q_orig, use_llm=False))

    def mock(prompt, config):
        if "keywords_ok" in prompt:
            return json.dumps({"order": [1], "keywords_ok": False, "rewrite": "大象 火星 外星人数据"}, ensure_ascii=False)
        return "[1,2,3,4,5]"
    monkeypatch.setattr(rr, "_default_llm_call", mock)

    res = wf.run_with_meta(query=q_orig, use_llm=False, rerank_backend="llm", rerank_audit=True)
    assert res.audit["used"] is False
    assert res.audit["reason"] == "rewrite_empty_kept_original"
    assert _names(res) == orig


def test_workflow_audit_not_triggered_when_rerank_off(wf, good_pair, monkeypatch):
    q_orig, q_rw = good_pair
    orig = _names(wf.run_with_meta(query=q_orig, use_llm=False))

    def mock(prompt, config):
        return json.dumps({"order": [1], "keywords_ok": False, "rewrite": q_rw}, ensure_ascii=False)
    monkeypatch.setattr(rr, "_default_llm_call", mock)

    res = wf.run_with_meta(query=q_orig, use_llm=False, rerank_backend="off", rerank_audit=True)
    assert res.audit["triggered"] is False
    assert res.audit["reason"] == "not_triggered"
    assert _names(res) == orig


def test_workflow_audit_ride_along_noop_rewrite_filtered(wf, good_pair, monkeypatch):
    # 发现A（ride-along 路径，即真实复现的缺陷）：存活集非空时 LLM 给"原句+数据"空转改写 → 过滤、不采纳。
    q_orig, _ = good_pair
    orig = _names(wf.run_with_meta(query=q_orig, use_llm=False))

    def mock(prompt, config):
        if "keywords_ok" in prompt:
            return json.dumps({"order": [1], "keywords_ok": False, "rewrite": q_orig + "数据"}, ensure_ascii=False)
        return "[1,2,3,4,5]"
    monkeypatch.setattr(rr, "_default_llm_call", mock)

    res = wf.run_with_meta(query=q_orig, use_llm=False, rerank_backend="llm", rerank_audit=True)
    assert res.audit["mode"] == "rerank"
    assert res.audit["used"] is False
    assert res.audit["reason"] == "incomplete_no_rewrite"
    assert res.audit["rewritten_query"] == ""
    assert _names(res) == orig


def test_workflow_audit_rewrite_no_visible_change_not_adopted(wf, good_pair, monkeypatch):
    # 发现A 第二形态：改写换了写法（文本核不同，过得了空转过滤）但命中**同一批数据集** →
    # 结果对用户不可见地没变 → 不采纳、不打横幅（reason=rewrite_no_change_kept_original）。
    q_orig, _ = good_pair
    orig = _names(wf.run_with_meta(query=q_orig, use_llm=False))
    reversed_q = " ".join(reversed(q_orig.split()))
    if reversed_q == q_orig or _names(wf.run_with_meta(query=reversed_q, use_llm=False)) != orig:
        pytest.skip(f"该探测查询不满足『词序颠倒=同结果集』前提：{q_orig!r}")

    def mock(prompt, config):
        if "keywords_ok" in prompt:
            return json.dumps({"order": [1], "keywords_ok": False, "rewrite": reversed_q}, ensure_ascii=False)
        return "[1,2,3,4,5]"
    monkeypatch.setattr(rr, "_default_llm_call", mock)

    res = wf.run_with_meta(query=q_orig, use_llm=False, rerank_backend="llm", rerank_audit=True)
    assert res.audit["used"] is False
    assert res.audit["reason"] == "rewrite_no_change_kept_original"
    assert _names(res) == orig


# ----------------------------- workflow：空池 / 弃权档 -----------------------------
# 检索工具化 Phase 1：空池独立审核（发现B 扩展）已删——空池救回改由
# search.rerun 工具承担；本段钉「不再触发」的新行为与两个仍然成立的「不触发」旧口径。

@pytest.fixture(scope="module")
def empty_query(wf):
    """稳定产出 0 结果（no_match）的中文查询——用于空池档触发。"""
    q = "小鼠 胰腺癌"
    res = wf.run_with_meta(query=q, use_llm=False)
    assert len(res.retrieved_data) == 0 and res.resolution_status == "no_match", res.resolution_status
    return q


@pytest.fixture(scope="module")
def abstain_query(wf):
    """稳定规则弃权（abstained）的中文查询。

     夜换载荷：原载荷是「小鼠大脑发育的scRNA-seq」，它弃权的唯一原因是「发育」
    这个描述词不在词表里——而那正是本轮修掉的缺陷（发育已收进 FILLER_DOMAIN，该句现在
    正常返回小鼠大脑数据）。换成一个**结构上不可能被词表覆盖**的虚构疾病名，
    才是稳定的弃权载荷；它同时也是冻结评测 adv02 钉死的那类「查无此物」。
    """
    q = "霍格沃茨综合征的小鼠大脑数据"
    res = wf.run_with_meta(query=q, use_llm=False)
    assert len(res.retrieved_data) == 0 and res.resolution_status == "abstained", res.resolution_status
    return q


def _rewrite_mock(rewrite):
    """_default_llm_call 桩：审核类 prompt（含 keywords_ok）→ 给定改写 JSON；纯重排 prompt → 顺序数组。"""
    def mock(prompt, config):
        if "keywords_ok" in prompt:
            return json.dumps({"keywords_ok": False, "rewrite": rewrite}, ensure_ascii=False)
        return "[1,2,3,4,5]"
    return mock


def test_workflow_audit_empty_pool_no_longer_rescued(wf, empty_query, abstain_query, monkeypatch):
    """ 检索工具化 Phase 1：空池/弃权档的独立审核删除——空池救回改由
    search.rerun 工具承担。本钉锁：rerank_audit=True 零命中时，无论 fixed-rerank=llm
    还是 auto+授权 LLM，audit 都恒 not_triggered、mode 恒非 "empty"（不再脱离重排
    独立触发审核调用，也不再静默改写救回）。"""
    monkeypatch.setattr(rr, "_default_llm_call", _rewrite_mock("小鼠 大脑 单细胞"))
    for q in (empty_query, abstain_query):
        res = wf.run_with_meta(query=q, use_llm=False, rerank_backend="llm", rerank_audit=True)
        assert res.audit["triggered"] is False
        assert res.audit["reason"] == "not_triggered"
        assert res.audit["mode"] != "empty"
        res_auto = wf.run_with_meta(query=q, use_llm=False, strategy="auto",
                                    llm_available=True, rerank_audit=True)
        assert res_auto.audit["triggered"] is False
        assert res_auto.audit["reason"] == "not_triggered"
        assert res_auto.audit["mode"] != "empty"


def test_workflow_audit_empty_not_triggered_when_rerank_off(wf, empty_query, monkeypatch):
    # 空池但 rerank=off → 审核仍不触发（锁"LLM 重排子开关"心智）。
    monkeypatch.setattr(rr, "_default_llm_call", _rewrite_mock("小鼠 大脑 单细胞"))
    res = wf.run_with_meta(query=empty_query, use_llm=False, rerank_backend="off", rerank_audit=True)
    assert res.audit["triggered"] is False
    assert res.audit["reason"] == "not_triggered"
    assert res.audit["mode"] is None


def test_workflow_audit_empty_not_rescued_in_auto_when_llm_unavailable(wf, empty_query, monkeypatch):
    # 对偶：auto 但未授权 LLM（llm_available=False，含 MCP auto 默认）→ 不引 LLM、不救回。
    # 锁住上一轮"MCP auto 永不引 LLM"的保证，证明本次修复没有把它破坏。
    monkeypatch.setattr(rr, "_default_llm_call", _rewrite_mock("小鼠 大脑 单细胞"))
    res = wf.run_with_meta(query=empty_query, use_llm=False, strategy="auto", llm_available=False, rerank_audit=True)
    assert res.audit["triggered"] is False
    assert res.audit["reason"] == "not_triggered"
    assert res.audit["mode"] is None


def test_workflow_audit_clarification_not_triggered(wf, monkeypatch):
    # clarification_required 不介入（尊重澄清流程）：即便 rerank=llm + audit，也不独立审核。
    q = "不需要fastq的人类肺数据"
    base = wf.run_with_meta(query=q, use_llm=False)
    assert base.resolution_status == "clarification_required"
    monkeypatch.setattr(rr, "_default_llm_call", _rewrite_mock("人类 肺 单细胞"))
    res = wf.run_with_meta(query=q, use_llm=False, rerank_backend="llm", rerank_audit=True)
    assert res.audit["triggered"] is False and res.audit["mode"] is None
    assert res.audit["reason"] == "not_triggered"
    assert res.resolution_status == "clarification_required"
