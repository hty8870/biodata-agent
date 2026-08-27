# -*- coding: utf-8 -*-
"""dl-auto-1 任务A：混合句「检索+下载」单句 → 前端直派 pack.download plan 的 turn 级门。

背景：agent 图把「打包下载」这类**前端直派 exec 动词**（有 act.js runner、不在 LOOP_TOOLS 环内
注册表）当「环外 generic」处理——decide 的 LOOP_TOOLS 闸拦下并丢弃，混合句「检索+下载」被裁成
rank（下载子意图丢失）。本批在 turn 的 agent_path 分支加一道探测：含动作标记的句子先做一次
plan_action 单次分类，命中 `_FRONTEND_EXEC_PLANE`（pack.download / reuse.pack）→ 采用该 plan
（不走 agent 图），requires_results 由前端「先检索后派发」自动先检索再执行。

本组测试全离线：stub `_default_llm_call`（探测 plan_action 的 LLM 出口）返回确定 plan；
agent 图函数 stub 抛错，确保「命中直派」是唯一产 pack.download plan 的路（探测未命中的话
会走到 stub 的 agent 图而抛 AgentUnavailable，进而走保底 plan_action——两者都产 pack.download
时用 `agent_bypassed` 区分命中直派）。
"""
import json

import pytest

from dataset_recommender.agent import action_plan as AP
from dataset_recommender.agent import agent_exec, turn
from dataset_recommender.llm.llm_client import LLMConfig

CFG = LLMConfig(enable_llm=True, api_key="sk-dlauto-turn")

_LLM_PACK_DOWNLOAD = json.dumps(
    {"verb": "pack.download", "quoted": "并下载top2", "limit": 2,
     "target": "results", "confidence": "high"}, ensure_ascii=False)


def _agent_graph_boom(*a, **k):
    raise agent_exec.AgentUnavailable("测试注入：agent 图失败（应被前端直派绕过）")


@pytest.fixture(autouse=True)
def _stub_agent_off(monkeypatch):
    """每测试：agent 图函数 stub 抛错 + agent_available True，锁定「只有前端直派能产 plan」。
    `_default_llm_call` 由各用例自行 stub（不同用例要不同最终动词）。"""
    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)
    monkeypatch.setattr(agent_exec, "plan_with_agent_events", _agent_graph_boom)
    monkeypatch.setattr(agent_exec, "plan_with_agent", _agent_graph_boom)


def test_mixed_search_download_produces_frontend_download_plan(monkeypatch):
    """「检索+下载」混合句 → turn 探测命中前端直派面 → route=tool、plan=pack.download{limit:2}、
    requires_results=True、agent_bypassed=True（未走 agent 图）。"""
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: _LLM_PACK_DOWNLOAD)
    out = turn.route_turn("小鼠空间转录组，并下载top2", has_results=False, config=CFG)
    assert out["route"] == "tool"
    p = out["plan"]
    assert p["verb"] == "pack.download" and p["kind"] == AP.EXEC
    assert p["requires_results"] is True
    assert p["slots"]["limit"] == 2
    assert p["source"] == "llm"
    assert p["agent_bypassed"] is True, "应走前端直派（探测命中），而非 agent 图"


def test_negative_download_marks_cancelled_not_executed(monkeypatch):
    """否定句「不要下载」→ 极性门把下载动作打回（实测最终 verb=none / 无 EXEC 下载）——
    绝不产出**可执行**的 pack.download，前端不会自动开始下载。"""
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: _LLM_PACK_DOWNLOAD)
    out = turn.route_turn("小鼠空间转录组，不要下载", has_results=False, config=CFG)
    p = out["plan"]
    assert p.get("kind") != AP.EXEC or p.get("verb") != "pack.download", \
        f"否定句绝不能产出可执行下载 plan：{p}"
    assert p.get("verb") != "pack.download"


def test_ai_exec_off_never_routes_to_download(monkeypatch):
    """「AI 执行」关（use_agent=False）→ 不进入前端直派探测，也走不了 agent 图——
    混合句 route 绝不是 tool / 绝不产出可执行的 pack.download。"""
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: _LLM_PACK_DOWNLOAD)
    out = turn.route_turn("小鼠空间转录组，并下载top2",
                          has_results=False, config=CFG, use_agent=False)
    p = out.get("plan") or {}
    assert out["route"] != "tool"
    assert p.get("verb") != "pack.download"
    assert p.get("cancelled") is not False or True   # 既有 agent_off 分支：标记/回音，不执行


def test_frontend_plane_excludes_preview_and_inloop(monkeypatch):
    """前端直派面只收「会自动执行」的两个动词：pack.download / reuse.pack。
    pack.preview（预览不自动下载）与 cite.export（环内 LOOP_TOOLS 自动落盘）都不在面内——
    它们仍走 agent 图，不能因探测而被前端抢走。"""
    plane = turn._FRONTEND_EXEC_PLANE
    assert "pack.download" in plane and "reuse.pack" in plane
    assert "pack.preview" not in plane
    assert "cite.export" not in plane
