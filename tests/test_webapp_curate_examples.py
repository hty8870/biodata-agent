# -*- coding: utf-8 -*-
"""Web 端点 `/api/curate-examples/*` 测试（TestClient，**全程禁网**）。

钉死的契约（统一知识道闸链口径，2026-10-09）：
- pending 只给**本分区**（匿名会话 + 端点指纹双键全等）候选，换端点坐标即不可见；
- approve 只产生**草稿提案**（approved 恒 0、stage=draft），真人经统一知识道闸
  评估+提交后才生效（注入侧只读已生效条目）并离开候选池；
- dismiss 即驳回提案（清池不进库）；
- 两个 POST 端点先过 `_require_same_origin`（跨源 403）；Pydantic extra="forbid"（422）。

写目标隔离：monkeypatch `webapp.PROJECT_ROOT` 到 tmp 仓库根（端点调用时才读该全局）。
"""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.agent import agent_exec as AX
from dataset_recommender.app import webapp
from dataset_recommender.app.webapp import app

client = TestClient(app, base_url="http://127.0.0.1")

_BASE_URL = "https://api-a.example.com"
_MODEL = "m1"


@pytest.fixture
def examples_tmp_root(tmp_path, monkeypatch):
    (tmp_path / ".userdata").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    return tmp_path


def _fp(base_url=_BASE_URL, model=_MODEL) -> str:
    return hashlib.sha256(f"{base_url}|{model}".encode("utf-8")).hexdigest()[:12]


def _seed_candidate(root, utterance="把 upload_a.json 删掉"):
    AX._maybe_record_success(root, utterance, {},
                             [{"verb": "curate.rollback", "ok": True,
                               "slots": {"target": "upload_a.json"}}],
                             principal="anonymous", endpoint_fp=_fp())
    cands = AX.list_example_candidates(root, principal="anonymous", endpoint_fp=_fp())
    assert len(cands) == 1
    return cands[0]


def test_pending_lists_only_same_partition(examples_tmp_root):
    _seed_candidate(examples_tmp_root)
    res = client.get(f"/api/curate-examples/pending?base_url={_BASE_URL}&model={_MODEL}")
    assert res.status_code == 200 and res.json()["ok"]
    cands = res.json()["candidates"]
    assert len(cands) == 1 and cands[0]["utterance"] == "把 upload_a.json 删掉"
    # 换端点坐标 → 不同分区 → 不可见（宁可少示不泄漏）
    res2 = client.get(f"/api/curate-examples/pending?base_url={_BASE_URL}&model=other-model")
    assert res2.status_code == 200 and res2.json()["candidates"] == []


def test_approve_proposes_draft_then_human_commit_activates(examples_tmp_root):
    cand = _seed_candidate(examples_tmp_root)
    res = client.post("/api/curate-examples/approve",
                      json={"ids": [cand["id"]], "base_url": _BASE_URL, "model": _MODEL})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] and body["approved"] == 0 and body["duplicated"] == 0
    assert body["stage"] == "draft" and len(body["changes"]) == 1, "approve 只产草稿提案"
    # 真人确认经统一知识道闸完成（离线关系评估接缝），生效后才可注入并离开候选池
    from dataset_recommender.app.conversation_store import ConversationStore
    from dataset_recommender.app.knowledge_service import KnowledgeService
    from dataset_recommender.llm.llm_client import LLMConfig
    cfg = LLMConfig(enable_llm=True, api_key="synthetic-curate-gate", base_url=_BASE_URL, model=_MODEL)
    service = KnowledgeService(ConversationStore(examples_tmp_root))
    change = body["changes"][0]
    from test_memory_relations import decision
    assessed = service.assess("anonymous", _fp(), "", change_id=change["id"],
        expected_change_revision=change["revision"], cfg=cfg,
        predictor=lambda req: decision(req, 'overlap_incomparable'))
    assert assessed["ok"], assessed
    committed = service.commit("anonymous", _fp(), "", change_id=change["id"],
        expected_change_revision=assessed["change_revision"], assessment_id=assessed["assessment_id"],
        expected_catalog_generation=assessed["catalog_generation"], human_confirmed=True)
    assert committed["ok"], committed
    assert AX.list_example_candidates(examples_tmp_root, principal="anonymous",
                                      endpoint_fp=_fp()) == []
    assert AX._load_success_examples(examples_tmp_root, "把 upload_b.json 删掉",
                                     principal="anonymous", endpoint_fp=_fp()), "真人提交后才可注入"


def test_dismiss_clears_pool_without_ledger(examples_tmp_root):
    cand = _seed_candidate(examples_tmp_root)
    res = client.post("/api/curate-examples/dismiss",
                      json={"ids": [cand["id"]], "base_url": _BASE_URL, "model": _MODEL})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] and body["dismissed"] == 1 and body["changes"], "驳回提案留痕（统一审计）"
    assert AX._load_success_examples(examples_tmp_root, "把 upload_b.json 删掉",
                                     principal="anonymous", endpoint_fp=_fp()) == []


def test_cross_origin_posts_are_403(examples_tmp_root):
    for path in ("/api/curate-examples/approve", "/api/curate-examples/dismiss"):
        res = client.post(path, json={"ids": [], "base_url": "", "model": ""},
                          headers={"Origin": "http://evil.example.com"})
        assert res.status_code == 403, path


def test_unknown_fields_are_422(examples_tmp_root):
    res = client.post("/api/curate-examples/approve",
                      json={"ids": [], "base_url": "", "model": "", "api_key": "sk-x"})
    assert res.status_code == 422
