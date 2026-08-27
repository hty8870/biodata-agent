# -*- coding: utf-8 -*-
"""两件事的专项门：

1. **空查询不再命中全库**：只剩填充词 / 执行词（打包 / 下载脚本 / 前20条）/ 纯数字的查询，
   解析器判 `abstained`（reason="empty_query"），检索器返回空——不把整个库当结果倒出去。
   真实检索查询（有物种 / 组织 / 疾病 / 技术 / 时间等）逐条仍 executable，冻结 767 不受影响。

2. **执行侧（下载/打包/导出）关键词命中的 LLM 核对**（action_audit，opt-in、fail-open、只报不代劳）：
   prompt/parse 容错、fail-open、workflow 触发/门禁、规则漏认时 missed_by_rule。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval import rerank as rr  # noqa: E402
from dataset_recommender.retrieval.query_parser import parse_query, detect_action_markers  # noqa: E402
from dataset_recommender.app.workflow import DatasetRecommendationWorkflow  # noqa: E402


# ============================ 1. 空查询弃权（不命中全库） ============================

@pytest.mark.parametrize("q", [
    "打包前20条", "帮我打包", "下载脚本", "导出引文", "打包", "帮我打包前20条", "把这些打包",
])
def test_bare_action_command_abstains(q):
    """光秃秃的执行指令（有打包/下载/导出、没检索目标）→ 弃权，绝不命中全库。"""
    it = parse_query(q)
    assert it.abstain is True, f"{q!r} 应弃权、不命中全库"
    assert it.abstain_reason == "action_only", f"{q!r} 弃权原因应为 action_only，实为 {it.abstain_reason}"
    assert it.parse_status == "abstained"


@pytest.mark.parametrize("q", [
    # 真实检索意图
    "人类肺癌单细胞", "有FASTQ的人类乳腺癌", "小鼠脑", "2020年以来的数据", "肺癌免疫细胞",
    "Visium 人脑", "10x 的乳腺癌", "打包人类肺数据",
    # 宽泛/无建模词的查询：**不**落纯执行指令弃权——按既有设计返回宽结果 + 提醒（无执行词）。
    "我想要一些数据", "细胞", "免疫细胞", "T细胞", "衰老相关", "前20条", "帮我",
])
def test_non_action_queries_are_not_action_only_abstain(q):
    it = parse_query(q)
    assert not (it.abstain and it.abstain_reason == "action_only"), \
        f"{q!r} 不是纯执行指令，不该按 action_only 弃权（实为 {it.abstain_reason}）"


def test_empty_query_returns_no_results_not_whole_corpus():
    """端到端：空查询走 workflow → resolution_status=abstained、result_total=0（而非全库）。"""
    wf = DatasetRecommendationWorkflow()
    res = wf.run_with_meta(query="打包前20条", top_k=10, use_llm=False)
    assert res.resolution_status == "abstained"
    assert res.result_total == 0
    assert len(res.retrieved_data) == 0
    # 执行说法仍被认到（供 actionHint 指路），只是检索本身弃权。
    assert res.action_markers == ["打包"]


# ============================ 2. action_audit 纯函数 ============================

def test_action_audit_parse_normal():
    ok = '{"is_action": true, "markers": ["存成压缩包下载"], "reason": "要打包并下载"}'
    is_action, markers, reason = rr.parse_action_audit_response(ok)
    assert is_action is True and markers == ["存成压缩包下载"] and reason == "要打包并下载"


def test_action_audit_parse_not_action():
    is_action, markers, reason = rr.parse_action_audit_response('{"is_action": false, "markers": []}')
    assert is_action is False and markers == []


def test_action_audit_parse_fenced_and_noisy():
    is_action, markers, _ = rr.parse_action_audit_response('噪声 {"is_action": true, "markers": ["下载"]} 尾巴')
    assert is_action is True and markers == ["下载"]


@pytest.mark.parametrize("text", ["", None, "完全不是 JSON", "{不合法}"])
def test_action_audit_parse_fail_open(text):
    is_action, markers, reason = rr.parse_action_audit_response(text)
    assert is_action is None and markers == [] and reason == ""


def test_audit_action_markers_injected_call():
    out = rr.audit_action_markers(
        "把这几个数据存下来", rule_markers=[],
        llm_call=lambda p: '{"is_action": true, "markers": ["把这几个数据存下来"], "reason": "要存档下载"}',
    )
    assert out == (True, ["把这几个数据存下来"], "要存档下载")


def test_audit_action_markers_fail_open_on_none():
    assert rr.audit_action_markers("随便", rule_markers=[], llm_call=lambda p: None) == (None, [], "")


def test_audit_action_markers_never_raises():
    def boom(_p):
        raise RuntimeError("network down")
    assert rr.audit_action_markers("x", rule_markers=[], llm_call=boom) == (None, [], "")


def test_action_audit_prompt_mentions_rule_markers():
    p = rr.build_action_audit_prompt("帮我打包前20条", ["打包"])
    assert "打包" in p and "is_action" in p and "markers" in p


# ============================ 3. workflow 集成 + 门禁 ============================

@pytest.fixture()
def wf():
    return DatasetRecommendationWorkflow()


def test_workflow_action_audit_default_none(wf):
    """默认不传 action_audit → meta.action_audit 恒 None（冻结路径零影响）。"""
    res = wf.run_with_meta(query="人类肺癌单细胞", top_k=5, use_llm=False)
    assert res.action_audit is None


def test_workflow_action_audit_mock_blocked(wf):
    """mock 一律不核对（mock 忽略 prompt、对执行侧判断无意义）。"""
    res = wf.run_with_meta(query="人类肺数据打包", top_k=5, use_llm=True, mock_llm=True,
                           provider="mock", action_audit=True)
    assert res.action_audit is None


def test_workflow_action_audit_no_key_fail_open(wf, monkeypatch):
    """无 key → fail-open、不发注定失败的网络调用 → None（monkeypatch 出无 key config，env 无关）。"""
    monkeypatch.setattr(DatasetRecommendationWorkflow, "_effective_llm_config",
                        lambda self, **kw: SimpleNamespace(api_key="", enable_llm=False, mock_llm=False))
    res = wf.run_with_meta(query="人类肺数据打包", top_k=5, use_llm=False,
                           provider="openai-compatible", action_audit=True)
    assert res.action_audit is None


def test_workflow_action_audit_triggered_and_missed_by_rule(wf, monkeypatch):
    """真实 key（monkeypatch 出一份带 key 的 config）+ 规则漏认的下载说法 →
    action_audit 触发、llm_is_action=True、missed_by_rule=True（规则一个都没认到）。"""
    monkeypatch.setattr(DatasetRecommendationWorkflow, "_effective_llm_config",
                        lambda self, **kw: SimpleNamespace(api_key="k", enable_llm=False, mock_llm=False))
    monkeypatch.setattr(rr, "_default_llm_call",
                        lambda p, c: '{"is_action": true, "markers": ["存成一个压缩包"], "reason": "要打包下载"}')
    q = "帮我把人类肺的数据存成一个压缩包"
    assert detect_action_markers(q) == []   # 规则裸词匹配漏认（没有『打包/下载』字面）
    res = wf.run_with_meta(query=q, top_k=5, use_llm=False, provider="openai-compatible", action_audit=True)
    aa = res.action_audit
    assert aa is not None and aa["triggered"] is True
    assert aa["llm_is_action"] is True and aa["llm_markers"] == ["存成一个压缩包"]
    assert aa["rule_markers"] == [] and aa["missed_by_rule"] is True
    assert aa["reason"] == "要打包下载"


def test_workflow_action_audit_agree_when_rule_hits(wf, monkeypatch):
    """规则认到（打包）+ LLM 也判 is_action → agree=True、missed_by_rule=False。"""
    monkeypatch.setattr(DatasetRecommendationWorkflow, "_effective_llm_config",
                        lambda self, **kw: SimpleNamespace(api_key="k", enable_llm=False, mock_llm=False))
    monkeypatch.setattr(rr, "_default_llm_call",
                        lambda p, c: '{"is_action": true, "markers": ["打包"], "reason": "明确要打包"}')
    res = wf.run_with_meta(query="人类肺数据，帮我打包前20条", top_k=5,
                           use_llm=False, provider="openai-compatible", action_audit=True)
    aa = res.action_audit
    assert aa["triggered"] is True and aa["llm_is_action"] is True
    assert "打包" in aa["rule_markers"] and aa["missed_by_rule"] is False and aa["agree"] is True


def test_workflow_action_audit_llm_no_verdict_marks_not_triggered(wf, monkeypatch):
    """LLM 返回解析不出（如 mock 策展表）→ triggered=False、fail-open。"""
    monkeypatch.setattr(DatasetRecommendationWorkflow, "_effective_llm_config",
                        lambda self, **kw: SimpleNamespace(api_key="k", enable_llm=False, mock_llm=False))
    monkeypatch.setattr(rr, "_default_llm_call", lambda p, c: "一段不是 JSON 的话")
    res = wf.run_with_meta(query="人类肺数据打包", top_k=5, use_llm=False,
                           provider="openai-compatible", action_audit=True)
    aa = res.action_audit
    assert aa is not None and aa["triggered"] is False and aa["llm_is_action"] is None


# ============================ 4. API 契约 ============================

def test_api_recommend_accepts_action_audit_and_echoes_key():
    from fastapi.testclient import TestClient
    from dataset_recommender.app.webapp import app
    client = TestClient(app, base_url="http://127.0.0.1")
    # 无 key 环境：action_audit=true 被接受、响应含 action_audit 键（值为 None，fail-open）。
    r = client.post("/api/recommend", json={"query": "人类肺癌单细胞", "top_k": 3,
                                            "use_llm": False, "action_audit": True})
    assert r.status_code == 200
    data = r.json()
    assert "action_audit" in data
    assert data["action_audit"] is None


# ============================ 5. 前端接线（三门不执行 JS，靠字符串钉住） ============================

def test_frontend_wires_action_audit():
    root = Path(__file__).resolve().parents[1]
    shell = (root / "web" / "static" / "js" / "core" / "shell.js").read_text(encoding="utf-8")
    search = (root / "web" / "static" / "js" / "search" / "search.js").read_text(encoding="utf-8")
    results = (root / "web" / "static" / "js" / "search" / "results.js").read_text(encoding="utf-8")
    # 真实 LLM 开时自动带上（用户「LLM 开启时应包含」）
    assert "action_audit: useLlm && !mockLlm" in shell, "getConfig 应在真实 LLM 开时置 action_audit"
    # 请求体真的把它发出去
    assert "action_audit: cfg.action_audit" in search, "search.js reqBody 应带 action_audit"
    # actionHint 消费 LLM 核对结果：规则漏认时靠 missed_by_rule 补认指路
    assert "action_audit" in results and "missed_by_rule" in results, \
        "renderActionHint 应消费 action_audit.missed_by_rule"
