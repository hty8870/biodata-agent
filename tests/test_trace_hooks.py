# -*- coding: utf-8 -*-
"""可追溯性挂钩集成的端到端钉：

- **ON**：一轮真实 turn（FakeModel 替身走完整 agent 图：understand→validate→execute→
  decide→narrate）→ 五类以上事件、seq 连续、route_decision 并入 understand 原始投票、
  可经 `export_turn` 导出单 JSON；
- **OFF**：`AGENT_TRACE=off` → 零文件产出、响应无 trace_turn_id（行为逐位不变）；
- **fail-soft**：落盘全程抛异常 / 载荷构造抛异常 → turn 照常返回（观测设施绝不掀翻主流程）；
- **rollback 端到端**：写动词（search_online 替身真写文件）→ state_snapshot 留锚 →
  rollback dry-run 零副作用 → apply 移回收站 → 复跑幂等 → 无 preimage 改/删 fail-closed 拒动。

全离线：项目根重定向 tmp_path（trace/快照/回收站/账本全落 tmp），LOOP_TOOLS 用替身，
绝不碰真实库、绝不联网。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("langgraph", reason="langchain 扩展未安装：挂钩集成测试跳过")
from langchain_core.messages import AIMessage  # noqa: E402

from dataset_recommender.agent import action_plan as _ap  # noqa: E402
from dataset_recommender.agent import agent_exec, turn  # noqa: E402
from dataset_recommender.agent.trace import (  # noqa: E402
    TraceRecorder, bind_recorder, snapshot_store, trace_root)
from dataset_recommender.agent.trace import events as ev  # noqa: E402
from dataset_recommender.agent.trace import export as trace_export  # noqa: E402
from dataset_recommender.agent.trace import recorder as trace_recorder  # noqa: E402
from dataset_recommender.agent.trace import rollback as trace_rollback  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402

CFG = LLMConfig(enable_llm=True, api_key="sk-trace-hooks")

# conftest 把 _run_route_consensus 全局 stub 成 general；本文件多数测试正好依赖该 stub
# （脚本替身不预置投票应答），唯独 test_scoped_route_consensus_votes_fully_traced 测真投票，
# 在测试体内用 import 期存的真引用后执行 setattr 恢复（后执行生效）。
_REAL_RUN_ROUTE_CONSENSUS = agent_exec._run_route_consensus

UTTERANCE = "检查ArrayExpress是否有更新"

CHECK_OK = {"checked_at": "2026-08-17T00:00:00+08:00",
            "sources": [{"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
                         "local_count": 10, "online_recent": 10, "new_count": 0,
                         "new_candidates": []}],
            "hint_zh": ""}

SEARCH_OK = {"source_label": "ArrayExpress", "query": "人类肺", "species": "人类",
             "sample_titles": ["human lung atlas"], "record_count": 2,
             "filename": "upload_20260817_curate_arrayexpress.json", "warnings": []}


class _FakeModel:
    """bind_tools 返回自身；invoke 依次弹预置 AIMessage（用尽后 pop 抛 IndexError——
    各节点按「LLM 缺席」fail-safe 处理）。"""

    def __init__(self, *answers):
        self.answers = list(answers)

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        return self

    def invoke(self, messages):
        return self.answers.pop(0)


def _tool_call(verb, **args):
    return AIMessage(content="", tool_calls=[
        {"name": verb.replace(".", "_"), "args": args, "id": "t1"}])


@pytest.fixture
def _tmp_root(monkeypatch, tmp_path):
    """项目根重定向 tmp——trace/快照/回收站/账本全部落 tmp，绝不碰真实库。"""
    monkeypatch.setattr(agent_exec, "_agent_project_root", lambda: tmp_path)
    monkeypatch.setattr(agent_exec, "_task_checklist_call", lambda *a, **k: ([], 0, ""))
    return tmp_path


def _install_check_tool(monkeypatch):
    def run(slots, root):
        return dict(CHECK_OK)
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        "curate.check_updates": {"run": run, "label_zh": "检查来源更新",
                                 "card_kind": "check_updates", "readonly": True},
    })


def _drive_turn(monkeypatch, tmp_path):
    """一轮真实 turn：understand 提 check_updates → 真跑 → decide 一次给两个调用
    （finish + 尾随只读——batch_emission 现场）→ narrate。返回 route_turn 的结果。"""
    _install_check_tool(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted=UTTERANCE, source="ArrayExpress",
                   confidence="high", reason="查更新"),
        AIMessage(content="", tool_calls=[
            {"name": "finish",
             "args": {"completion_report": "检查来源更新：已做（第1步），没有新增。"},
             "id": "t2"},
            {"name": "curate_db_status", "args": {"quoted": UTTERANCE}, "id": "t3"},
        ]),
        AIMessage(content="ArrayExpress 没有更新。"),
    )
    monkeypatch.setattr(agent_exec, "_build_chat_model", lambda config: model)
    return turn.route_turn(UTTERANCE, config=CFG)


def _read_events(root, result):
    path = trace_root(root) / "anonymous" / f"{result['trace_turn_id']}.jsonl"
    assert path.is_file(), "trace ON：turn 日志必须落盘"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------- ON：五类以上 + seq 连续 + 导出

def test_turn_emits_five_plus_kinds_continuous_seq_and_exports(monkeypatch, _tmp_root):
    result = _drive_turn(monkeypatch, _tmp_root)
    assert result["route"] == "tool" and result["via"] == "agent"
    assert result.get("trace_turn_id"), "trace ON：additive 回显 turn id（报障给号）"

    events = _read_events(_tmp_root, result)
    kinds = {e["kind"] for e in events}
    assert {"route_decision", "llm_call", "tool_call", "batch_emission",
            "finish_reason"} <= kinds, f"五类事件齐全，实际：{sorted(kinds)}"
    # seq 从 0 连续（DSH 连续契约的读取侧校验）
    assert [e["seq"] for e in events] == list(range(len(events)))

    # llm_call：understand/decide 各一次（digest + 档位 + 延迟），不记全文
    llm_nodes = [e["payload"]["node"] for e in events if e["kind"] == "llm_call"]
    assert "understand" in llm_nodes and "decide" in llm_nodes
    for e in events:
        if e["kind"] == "llm_call":
            assert e["payload"]["prompt"]["sha256"] and e["payload"]["channel"]

    # tool_call：check_updates 真跑留痕（含预算计数现场）
    tool_evs = [e for e in events if e["kind"] == "tool_call"]
    assert [e["payload"]["verb"] for e in tool_evs] == ["curate.check_updates"]
    assert tool_evs[0]["payload"]["ok"] is True
    assert tool_evs[0]["payload"]["budgets"]["steps"] == 1

    # batch_emission：decide 一次给了 2 个调用，finish 收尾、尾随 1 个不消费
    batch_evs = [e for e in events if e["kind"] == "batch_emission"]
    assert len(batch_evs) == 1
    assert batch_evs[0]["payload"]["n_calls"] == 2
    assert batch_evs[0]["payload"]["adopted"] == 0
    assert batch_evs[0]["payload"]["dropped"] == 1

    # finish_reason：正常收尾
    fin = [e for e in events if e["kind"] == "finish_reason"]
    assert fin[-1]["payload"]["kind"] == "completed"
    assert fin[-1]["payload"]["steps"] == 1

    # route_decision：错误分析第一现场——understand 原始投票并入 votes
    rd = [e for e in events if e["kind"] == "route_decision"]
    assert len(rd) == 1
    payload = rd[0]["payload"]
    assert payload["route"] == "tool" and payload["via"] == "agent"
    assert payload["plan_verb"] == "curate.check_updates"
    assert payload["votes"]["understand"]["verb"] == "curate.check_updates"
    assert payload["votes"]["understand"]["confidence"] == "high"
    assert payload["votes"]["understand"]["slots"]["source"] == "ArrayExpress"

    # 导出：单 JSON、六键健康指标、与 JSONL 同一真源
    exported = trace_export.export_turn(_tmp_root, result["trace_turn_id"])
    assert exported["event_count"] == len(events)
    assert exported["seq_gaps"] == [] and exported["bad_tail_lines"] == 0
    assert exported["corrupt"] == [] and exported["unknown_kinds"] == []
    for kind in kinds:
        assert exported["kinds"][kind] >= 1


# ---------------------------------------------------------------- OFF：零文件产出、行为逐位不变

def test_trace_off_produces_zero_files_and_no_turn_id(monkeypatch, _tmp_root):
    monkeypatch.setenv("AGENT_TRACE", "off")
    result = _drive_turn(monkeypatch, _tmp_root)
    assert result["route"] == "tool" and result["via"] == "agent"
    assert "trace_turn_id" not in result, "OFF：additive 键不出现（响应逐位不变）"
    assert not trace_root(_tmp_root).exists(), "OFF：零文件产出"


# ---------------------------------------------------------------- fail-soft：emit 炸不影响 turn

def test_emit_failure_never_breaks_the_turn(monkeypatch, _tmp_root):
    def boom(self, kind, payload):
        raise RuntimeError("磁盘满了（模拟）")
    monkeypatch.setattr(trace_recorder.TraceRecorder, "append", boom)
    result = _drive_turn(monkeypatch, _tmp_root)
    assert result["route"] == "tool" and result["via"] == "agent"
    assert result["plan"]["verb"] == "curate.check_updates"


def test_payload_constructor_failure_never_breaks_the_turn(monkeypatch, _tmp_root):
    """载荷构造本身抛异常同样 fail-soft（warn-once + 丢弃）——全路径不掀翻主流程。"""
    def boom(**kwargs):
        raise ValueError("坏载荷（模拟）")
    monkeypatch.setattr(ev, "llm_call_payload", boom)
    result = _drive_turn(monkeypatch, _tmp_root)
    assert result["route"] == "tool"
    events = _read_events(_tmp_root, result)
    kinds = {e["kind"] for e in events}
    assert "llm_call" not in kinds, "构造失败的 llm_call 如实丢弃"
    assert {"route_decision", "tool_call", "finish_reason"} <= kinds, "其余事件照常落盘"


# -------------------------------------------------- 写动词快照 + rollback 端到端（幂等 + fail-closed）

def _runtime():
    return SimpleNamespace(context=SimpleNamespace(on_progress=None, chat_model=None))


def test_write_verb_snapshot_and_rollback_roundtrip(monkeypatch, _tmp_root):
    ext = _tmp_root / "database" / "external"
    ext.mkdir(parents=True)
    fname = SEARCH_OK["filename"]

    def run(slots, root):
        (ext / fname).write_text(json.dumps({"rows": ["x"]}, ensure_ascii=False),
                                 encoding="utf-8")
        return dict(SEARCH_OK)

    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        "curate.search_online": {"run": run, "label_zh": "联网搜索入库",
                                 "card_kind": "search_online", "readonly": False},
    })
    rec = TraceRecorder(_tmp_root, "test", "rb1", enabled=True)
    state = {"utterance": "联网搜人类肺数据入库",
             "plan": {"verb": "curate.search_online",
                      "slots": {"quoted": "联网搜人类肺数据入库", "source": "ArrayExpress",
                                "keywords": "人类肺", "species": "人类"}},
             "loop_plan": None, "steps": [], "pending_reask_write": False}
    with bind_recorder(rec):
        out = agent_exec.execute(state, runtime=_runtime())
    step = out["steps"][0]
    assert step["ok"] is True and (ext / fname).is_file(), "替身 search_online 真写文件"

    # state_snapshot 事件留锚：created 含新文件；tool_call 同场（写动词 readonly=False）
    path = trace_root(_tmp_root) / "test" / "rb1.jsonl"
    events = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    snap = next(e for e in events if e["kind"] == "state_snapshot")
    assert snap["payload"]["verb"] == "curate.search_online"
    assert [c["name"] for c in snap["payload"]["created"]] == [fname]
    tool = next(e for e in events if e["kind"] == "tool_call")
    assert tool["payload"]["readonly"] is False
    assert tool["payload"]["budgets"]["write_steps"] == 1
    sid = snap["payload"]["snapshot_id"]

    # dry-run：只出计划，零副作用
    plan = trace_rollback.plan_rollback(_tmp_root, sid)
    assert plan["recycle_created"] == [fname] and plan["restore_bytes"] == []
    assert (ext / fname).is_file(), "dry-run 绝不动文件"

    # apply：新文件移入回收站（移动非删除 + manifest 行）
    res = trace_rollback.apply_rollback(_tmp_root, sid)
    assert [e["name"] for e in res["applied"]["recycled"]] == [fname]
    assert not (ext / fname).exists()
    recycle = _tmp_root / ".userdata" / "recycle"
    assert recycle.is_dir() and any(p.name.endswith(fname) for p in recycle.iterdir())

    # 幂等：复跑如实 skipped，不重复动手
    res2 = trace_rollback.apply_rollback(_tmp_root, sid)
    assert res2["applied"]["recycled"] == []
    assert any(e["name"] == fname for e in res2["skipped"])

    # fail-closed：modified 且无 preimage → 拒动、如实报、文件原样
    victim = ext / "keep_existing.json"
    victim.write_text("v1", encoding="utf-8")
    store = snapshot_store(_tmp_root)
    sid2 = store.capture("curate.sync_updates", preimage_paths=[])
    victim.write_text("v2", encoding="utf-8")
    store.finalize(sid2)
    plan2 = trace_rollback.plan_rollback(_tmp_root, sid2)
    assert plan2["restore_bytes"] == []
    assert [e["name"] for e in plan2["unrestorable"]] == ["keep_existing.json"]
    res3 = trace_rollback.apply_rollback(_tmp_root, sid2)
    assert res3["applied"]["restored"] == []
    assert victim.read_text(encoding="utf-8") == "v2", "无 preimage 拒动——宁可少退不毁数据"


# -------------------------------------------------- 补丁作用域快照 + 回滚

def _patch_scope_execute(monkeypatch, root, account_id, payload_records, *,
                         before_ingest=None, after_ingest=None):
    """在绑定账户作用域下跑一次真实 execute：替身 search_online **真调 ingest_dataset**
    （写漏斗 → 本人补丁包），返回 (out, sid)。"""
    from dataset_recommender.corpus import uploads
    from dataset_recommender.corpus.patch_package import bind_patch_scope

    def run(slots, r):
        if before_ingest:
            before_ingest()
        raw = json.dumps(payload_records, ensure_ascii=False).encode("utf-8")
        safe = uploads.new_upload_name("x.json", "20260715_010203_000004")
        res = uploads.ingest_dataset(raw_bytes=raw, safe_name=safe, project_root=r,
                                     form_source="trace-test")
        if after_ingest:
            after_ingest()
        return dict(SEARCH_OK, record_count=res.record_count, filename=res.filename,
                    warnings=list(res.warnings))

    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        "curate.search_online": {"run": run, "label_zh": "联网搜索入库",
                                 "card_kind": "search_online", "readonly": False},
    })
    rec = TraceRecorder(root, "test", "p1", enabled=True)
    state = {"utterance": "联网搜人类肺数据入库",
             "plan": {"verb": "curate.search_online",
                      "slots": {"quoted": "联网搜人类肺数据入库", "keywords": "人类肺"}},
             "loop_plan": None, "steps": [], "pending_reask_write": False}
    with bind_recorder(rec), bind_patch_scope(account_id):
        out = agent_exec.execute(state, runtime=_runtime())
    return out, out["steps"][0]["snapshot_id"]


def test_patch_scope_snapshot_rollback_restores_own_patch(monkeypatch, _tmp_root):
    """真实 execute 链（绑定账户 ingest）→ 快照记补丁文件 → 回滚恢复
    本人补丁到写前状态；其他账户补丁与共享 external 零变动。"""
    from dataset_recommender.corpus import patch_package as pp
    from dataset_recommender.corpus.patch_package import load_patch

    pp.ingest_records_to_patch(payload={}, project_root=_tmp_root, account_id="acct_a",
                               records=[{"dataset_uid": "P-1", "dataset_name": "P1",
                                         "species": "Human"}])
    pp.ingest_records_to_patch(payload={}, project_root=_tmp_root, account_id="acct_b",
                               records=[{"dataset_uid": "B-1", "dataset_name": "B1",
                                         "species": "Human"}])
    before_a = pp.patch_path_for(_tmp_root, "acct_a").read_bytes()

    out, sid = _patch_scope_execute(monkeypatch, _tmp_root, "acct_a",
                                    [{"dataset_uid": "P-2", "dataset_name": "P2",
                                      "species": "Human"}])
    assert out["steps"][0]["ok"] is True
    assert sid, "写步必须留快照锚"
    patch_a = load_patch(_tmp_root, "acct_a")
    assert sorted(pp.record_uid(r) for r in patch_a["adds"]) == ["P-1", "P-2"], \
        "真实 ingest 落本人补丁包"

    plan = trace_rollback.plan_rollback(_tmp_root, sid)
    assert plan.get("mode") == "patch"
    assert plan["restore_bytes"] == ["acct_a.json"] and plan["recycle_created"] == []
    res = trace_rollback.apply_rollback(_tmp_root, sid)
    assert [e["name"] for e in res["applied"]["restored"]] == ["acct_a.json"]
    assert pp.patch_path_for(_tmp_root, "acct_a").read_bytes() == before_a, \
        "本人补丁字节恢复到写前"
    assert [pp.record_uid(r) for r in load_patch(_tmp_root, "acct_b")["adds"]] == ["B-1"], \
        "其他账户补丁零变动"
    ext = _tmp_root / "database" / "external"
    assert not ext.exists() or list(ext.glob("*.json")) == [], "共享 external 零变动"
    # 幂等：复跑拒动（现状已与快照「写后」不一致）
    res2 = trace_rollback.apply_rollback(_tmp_root, sid)
    assert res2["applied"]["restored"] == []
    assert any(e.get("code") == "state_conflict" for e in res2["skipped"])


def test_patch_scope_snapshot_rollback_removes_first_write(monkeypatch, _tmp_root):
    """验收（created 档）：写前补丁文件不存在 → 回滚恢复「不存在」，
    现状字节先归档进回收站（可逆保险）。"""
    from dataset_recommender.corpus import patch_package as pp

    out, sid = _patch_scope_execute(monkeypatch, _tmp_root, "acct_fresh",
                                    [{"dataset_uid": "P-9", "dataset_name": "P9",
                                      "species": "Human"}])
    assert out["steps"][0]["ok"] is True
    target = pp.patch_path_for(_tmp_root, "acct_fresh")
    assert target.is_file()
    plan = trace_rollback.plan_rollback(_tmp_root, sid)
    assert plan["recycle_created"] == ["acct_fresh.json"] and plan["restore_bytes"] == []
    res = trace_rollback.apply_rollback(_tmp_root, sid)
    assert [e["name"] for e in res["applied"]["recycled"]] == ["acct_fresh.json"]
    assert not target.exists(), "写前不存在 → 回滚恢复「不存在」"
    recycle = _tmp_root / ".userdata" / "recycle"
    assert any(p.name.endswith("acct_fresh.json") for p in recycle.iterdir()), "字节归档可逆"


def test_patch_rollback_conflicts_on_same_account_later_write(monkeypatch, _tmp_root):
    """同一 principal 的**后续会话写入**绝不被旧快照覆盖——旧快照回滚
    在全量 preflight CAS 处明确 conflict、整体零修改（补丁含后续新条目，一个字节不动）。"""
    from dataset_recommender.corpus import patch_package as pp
    from dataset_recommender.corpus.patch_package import load_patch

    pp.ingest_records_to_patch(payload={}, project_root=_tmp_root, account_id="acct_a",
                               records=[{"dataset_uid": "P-1", "dataset_name": "P1",
                                         "species": "Human"}])
    out, sid = _patch_scope_execute(monkeypatch, _tmp_root, "acct_a",
                                    [{"dataset_uid": "P-2", "dataset_name": "P2",
                                      "species": "Human"}])
    assert out["steps"][0]["ok"] is True

    # 后续会话（同账户）：又写入 P-3 —— 快照 after-state 自此过时
    pp.ingest_records_to_patch(payload={}, project_root=_tmp_root, account_id="acct_a",
                               records=[{"dataset_uid": "P-3", "dataset_name": "P3",
                                         "species": "Human"}])
    snapshot_bytes = pp.patch_path_for(_tmp_root, "acct_a").read_bytes()

    plan = trace_rollback.plan_rollback(_tmp_root, sid)
    assert [c["code"] for c in plan["conflicts"]] == ["state_conflict"]
    assert plan["restore_bytes"] == [] and plan["recycle_created"] == []
    res = trace_rollback.apply_rollback(_tmp_root, sid)
    assert res["conflict"] is True and res["applied"] == {"recycled": [], "restored": []}
    assert pp.patch_path_for(_tmp_root, "acct_a").read_bytes() == snapshot_bytes, \
        "同一账户后续写入不得被覆盖"
    assert sorted(pp.record_uid(r) for r in load_patch(_tmp_root, "acct_a")["adds"]) == \
        ["P-1", "P-2", "P-3"]

    # 后续删除（同账户 trash）同样构成 conflict
    pp.trash_adds(_tmp_root, "acct_a", ["P-1"])
    res2 = trace_rollback.apply_rollback(_tmp_root, sid)
    assert res2["conflict"] is True and res2["applied"] == {"recycled": [], "restored": []}
    assert [pp.record_uid(r) for r in load_patch(_tmp_root, "acct_a")["trash"]] == ["P-1"], \
        "后续删除保持原样（不被旧快照的写回「复活」）"


# ---------------------------------------------------------------- webapp additive 回显

def test_utterance_response_body_echoes_trace_turn_id_only_when_present():
    from dataset_recommender.app import webapp
    base = {"route": "search", "query": "x", "plan": None, "echo_zh": "",
            "retrieval": None, "via": "rule_direct", "needs_agent": False,
            "suggestions": [], "result_payload": None, "preliminary_final": False}
    body_off = webapp._utterance_response_body(dict(base))
    assert "trace_turn_id" not in body_off, "缺席（OFF）→ 不加键，响应逐位不变"
    body_on = webapp._utterance_response_body(dict(base, trace_turn_id="abc123"))
    assert body_on["trace_turn_id"] == "abc123"


# ------------------------------------------- route_consensus 原始投票留痕

def test_scoped_route_consensus_votes_fully_traced(monkeypatch, tmp_path):
    """分流共识的**全部原始投票**留痕（route_decision.votes.route_consensus + 每票
    一条 llm_call）——scoped 路由常驻，无需开关装配。"""
    monkeypatch.setattr(agent_exec, "_agent_project_root", lambda: tmp_path)
    monkeypatch.setattr(agent_exec, "_task_checklist_call", lambda *a, **k: ([], 0, ""))
    monkeypatch.setattr(agent_exec, "_run_route_consensus", _REAL_RUN_ROUTE_CONSENSUS)  # 测真投票
    _install_check_tool(monkeypatch)
    vote = AIMessage(content=json.dumps(
        {"route": "action", "reason": "检查更新是管护动作"}, ensure_ascii=False))
    model = _FakeModel(
        vote, vote,  # route_consensus 并行两票一致 → action（不加投第三票）
        _tool_call("curate.check_updates", quoted=UTTERANCE, source="ArrayExpress",
                   confidence="high", reason="查更新"),
        AIMessage(content="", tool_calls=[
            {"name": "finish",
             "args": {"completion_report": "检查来源更新：已做（第1步）。"}, "id": "t2"}]),
        AIMessage(content="ArrayExpress 没有更新。"),
    )
    monkeypatch.setattr(agent_exec, "_build_chat_model", lambda config: model)
    result = turn.route_turn(UTTERANCE, config=CFG)
    assert result["route"] == "tool"
    events = _read_events(tmp_path, result)
    rd = next(e for e in events if e["kind"] == "route_decision")
    rc = rd["payload"]["votes"]["route_consensus"]
    assert rc["route"] == "action"
    assert len(rc["votes"]) == 2, "两票一致即定，无第三票"
    assert {v["temperature"] for v in rc["votes"]} == {0.0, 0.8}
    assert all(v["ok"] and v["route"] == "action" and v["raw"] for v in rc["votes"])
    assert rd["payload"]["votes"]["understand"]["verb"] == "curate.check_updates"
    # 每票一条 llm_call（票在并行线程够不到 contextvar，由节点代发）
    rc_calls = [e for e in events
                if e["kind"] == "llm_call" and e["payload"]["node"] == "route_consensus"]
    assert len(rc_calls) == 2
    assert all(e["payload"]["channel"] == "consensus_vote" for e in rc_calls)


def _bound_upload(root, account, uid):
    from dataset_recommender.corpus import uploads
    from dataset_recommender.corpus.patch_package import bind_patch_scope

    with bind_patch_scope(account):
        return uploads.ingest_dataset(
            raw_bytes=json.dumps([{"dataset_uid": uid, "dataset_name": uid,
                                   "species": "Human"}]).encode("utf-8"),
            safe_name=uploads.new_upload_name("concurrent.json"), project_root=root)


def _upload_in_other_thread(root, account, uid):
    import threading

    errors = []
    def write():
        try:
            _bound_upload(root, account, uid)
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=write)
    thread.start()
    thread.join(10)
    assert not thread.is_alive() and not errors


@pytest.mark.parametrize("outcome", ["write", "zero", "failure"])
@pytest.mark.parametrize("initial", [False, True])
def test_execute_patch_concurrent_before_rmw_is_not_owned(monkeypatch, _tmp_root,
                                                        outcome, initial):
    from dataset_recommender.corpus import patch_package as pp
    from dataset_recommender.corpus.uploads import UploadError

    account = "acct_overlap"
    if initial:
        _bound_upload(_tmp_root, account, "seed")
    target = pp.patch_path_for(_tmp_root, account)
    actual_preimage = []
    def concurrent_write():
        _upload_in_other_thread(_tmp_root, account, "theirs")
        actual_preimage.append(target.read_bytes())
        if outcome == "failure":
            raise UploadError("unavailable", "模拟采集失败，尚未写入")
    uid = "mine" if outcome == "write" else "theirs"
    out, sid = _patch_scope_execute(
        monkeypatch, _tmp_root, account,
        [{"dataset_uid": uid, "dataset_name": uid, "species": "Human"}],
        before_ingest=concurrent_write)
    assert out["steps"][0]["ok"] is (outcome != "failure")
    store = snapshot_store(_tmp_root)
    diff = store.load(sid)["diff"]
    if outcome == "write":
        assert diff["created"] == []
        assert [e["name"] for e in diff["modified"]] == [target.name]
        assert store.preimage_path(sid, target.name).read_bytes() == actual_preimage[0]
    else:
        assert all(not diff[k] for k in ("created", "modified", "deleted"))
    result = trace_rollback.apply_rollback(_tmp_root, sid)
    assert result["conflict"] is False
    assert target.read_bytes() == actual_preimage[0]
    assert "theirs" in {pp.record_uid(r) for r in pp.load_patch(_tmp_root, account)["adds"]}


def test_execute_patch_after_rmw_before_finalize_uses_real_after(monkeypatch, _tmp_root):
    import hashlib
    from dataset_recommender.corpus import patch_package as pp

    account = "acct_after"
    _bound_upload(_tmp_root, account, "seed")
    target = pp.patch_path_for(_tmp_root, account)
    after_mine = []
    def write_after():
        after_mine.append(target.read_bytes())
        _upload_in_other_thread(_tmp_root, account, "theirs")
    _, sid = _patch_scope_execute(
        monkeypatch, _tmp_root, account,
        [{"dataset_uid": "mine", "dataset_name": "mine", "species": "Human"}],
        after_ingest=write_after)
    current = target.read_bytes()
    entry = snapshot_store(_tmp_root).load(sid)["diff"]["modified"][0]
    assert entry["after"]["sha256"] == hashlib.sha256(after_mine[0]).hexdigest()
    result = trace_rollback.apply_rollback(_tmp_root, sid)
    assert result["conflict"] is True
    assert result["applied"] == {"recycled": [], "restored": []}
    assert target.read_bytes() == current


def test_patch_legacy_snapshot_without_rmw_receipt_conflicts(monkeypatch, _tmp_root):
    from dataset_recommender.corpus import patch_package as pp

    account = "acct_legacy"
    _, sid = _patch_scope_execute(
        monkeypatch, _tmp_root, account,
        [{"dataset_uid": "mine", "dataset_name": "mine", "species": "Human"}])
    store = snapshot_store(_tmp_root)
    meta = store.load(sid)
    meta.pop("patch_receipt", None)
    store.save_meta(sid, meta)
    target = pp.patch_path_for(_tmp_root, account)
    before = target.read_bytes()
    plan = trace_rollback.plan_rollback(_tmp_root, sid)
    assert [c["code"] for c in plan["conflicts"]] == ["missing_mutation_receipt"]
    result = trace_rollback.apply_rollback(_tmp_root, sid)
    assert result["conflict"] and not result["applied"]["recycled"]
    consumer = agent_exec._loop_curate_rollback({}, _tmp_root, {"steps": [
        {"verb": "curate.search_online", "snapshot_id": sid, "ok": True}]})
    assert not consumer["rolled_back"] and consumer["reason"] == "rollback_incomplete"
    assert consumer["skipped"][0]["code"] == "missing_mutation_receipt"
    assert target.read_bytes() == before


@pytest.mark.parametrize("initial", [False, True])
def test_patch_rollback_cas_restore_blocks_real_bound_upload_thread(
        monkeypatch, _tmp_root, initial):
    import threading
    from dataset_recommender.corpus import patch_package as pp

    account = "acct_barrier"
    if initial:
        _bound_upload(_tmp_root, account, "seed")
    _, sid = _patch_scope_execute(
        monkeypatch, _tmp_root, account,
        [{"dataset_uid": "mine", "dataset_name": "mine", "species": "Human"}])
    ready, done = threading.Event(), threading.Event()
    errors, overlap = [], []
    def upload():
        ready.set()
        try:
            _bound_upload(_tmp_root, account, "theirs")
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()
    thread = threading.Thread(target=upload)
    real_plan = trace_rollback._plan
    def preflight(*args):
        plan = real_plan(*args)
        thread.start()
        assert ready.wait(10)
        overlap.append(done.wait(0.3))
        return plan
    monkeypatch.setattr(trace_rollback, "_plan", preflight)
    result = trace_rollback.apply_rollback(_tmp_root, sid)
    thread.join(10)
    assert not thread.is_alive() and not errors
    assert overlap == [False], "preflight 到 restore 之间实际写锁必须一直持有"
    assert not result["conflict"] and result["rolled_back"]
    assert {pp.record_uid(r) for r in pp.load_patch(_tmp_root, account)["adds"]} == (
        {"seed", "theirs"} if initial else {"theirs"})


@pytest.mark.parametrize("initial", [False, True])
def test_patch_rollback_cas_restore_blocks_real_bound_upload_process(
        monkeypatch, _tmp_root, initial):
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path
    from dataset_recommender.corpus import patch_package as pp

    account = "acct_process"
    if initial:
        _bound_upload(_tmp_root, account, "seed")
    _, sid = _patch_scope_execute(
        monkeypatch, _tmp_root, account,
        [{"dataset_uid": "mine", "dataset_name": "mine", "species": "Human"}])
    repo = Path(__file__).resolve().parents[1]
    ready = _tmp_root / "child-ready"
    processes, overlap = [], []
    child = r'''
import json, os, sys
from pathlib import Path
root, repo = Path(sys.argv[1]).resolve(), Path.cwd().resolve()
def audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname"}:
        raise AssertionError("offline child forbids network")
    if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        raw = os.fsdecode(args[0])
        if raw in {os.devnull, "nul", "NUL", "/dev/null"}:
            return
        p = Path(raw).resolve()
        if p.name.startswith(".env") or p.suffix == ".env" or p.is_relative_to(repo / ".userdata"):
            raise AssertionError("offline child forbids private files")
        mode, flags = args[1:3]
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if writing and not p.is_relative_to(root):
            raise AssertionError("offline child only writes synthetic root")
sys.addaudithook(audit)
from dataset_recommender.corpus.patch_package import bind_patch_scope
from dataset_recommender.corpus.uploads import ingest_dataset, new_upload_name
Path(sys.argv[3]).write_text("ready", encoding="utf-8")
with bind_patch_scope(sys.argv[2]):
    res = ingest_dataset(raw_bytes=json.dumps([{"dataset_uid":"theirs",
        "dataset_name":"theirs", "species":"Human"}]).encode(),
        safe_name=new_upload_name("child.json"), project_root=root)
assert res.record_count == 1
print("BOUND_UPLOAD_COMMITTED", os.getpid())
'''
    real_plan = trace_rollback._plan
    def preflight(*args):
        plan = real_plan(*args)
        proc = subprocess.Popen([sys.executable, "-B", "-c", child,
                                 str(_tmp_root), account, str(ready)],
                                cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(proc)
        deadline = time.monotonic() + 15
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "独立进程必须到达真实 upload 调用"
        try:
            proc.wait(timeout=0.3)
            overlap.append(True)
        except subprocess.TimeoutExpired:
            overlap.append(False)
        return plan
    monkeypatch.setattr(trace_rollback, "_plan", preflight)
    try:
        result = trace_rollback.apply_rollback(_tmp_root, sid)
    finally:
        for proc in processes:
            stdout, stderr = proc.communicate(timeout=20)
            assert proc.returncode == 0, stderr
            assert "BOUND_UPLOAD_COMMITTED" in stdout
            print(stdout.strip(), "ROLLBACK_PID=", os.getpid(),
                  "CAS_WINDOW_BLOCKED=", not overlap[-1])
    assert overlap == [False], "OS 事务锁必须阻止另一个进程在 CAS 后插队"
    assert not result["conflict"] and result["rolled_back"]
    assert {pp.record_uid(r) for r in pp.load_patch(_tmp_root, account)["adds"]} == (
        {"seed", "theirs"} if initial else {"theirs"})


def test_execute_patch_failure_after_commit_keeps_real_receipt(monkeypatch, _tmp_root):
    from dataset_recommender.corpus import patch_package as pp

    account = "acct_partial"
    _bound_upload(_tmp_root, account, "seed")
    target = pp.patch_path_for(_tmp_root, account)
    before = target.read_bytes()
    def fail_after_write():
        raise RuntimeError("模拟结果构造失败，实际写入已提交")
    out, sid = _patch_scope_execute(
        monkeypatch, _tmp_root, account,
        [{"dataset_uid": "mine", "dataset_name": "mine", "species": "Human"}],
        after_ingest=fail_after_write)
    assert not out["steps"][0]["ok"]
    result = trace_rollback.apply_rollback(_tmp_root, sid)
    assert result["rolled_back"] and not result["conflict"]
    assert target.read_bytes() == before


@pytest.mark.parametrize("interleaved", [False, True])
def test_execute_patch_multiple_rmw_requires_continuous_receipts(monkeypatch, _tmp_root, interleaved):
    from dataset_recommender.corpus import patch_package as pp

    account = "acct_chain"
    _bound_upload(_tmp_root, account, "seed")
    target = pp.patch_path_for(_tmp_root, account)
    before = target.read_bytes()
    def second_write():
        if interleaved:
            _upload_in_other_thread(_tmp_root, account, "theirs")
        _bound_upload(_tmp_root, account, "mine2")
    out, sid = _patch_scope_execute(
        monkeypatch, _tmp_root, account,
        [{"dataset_uid": "mine1", "dataset_name": "mine1", "species": "Human"}],
        after_ingest=second_write)
    assert out["steps"][0]["ok"]
    current = target.read_bytes()
    result = trace_rollback.apply_rollback(_tmp_root, sid)
    if interleaved:
        assert result["conflict"] and result["applied"] == {"recycled": [], "restored": []}
        assert result["conflicts"][0]["code"] == "untrusted_mutation_receipt"
        assert target.read_bytes() == current
    else:
        assert not result["conflict"] and result["rolled_back"]
        assert target.read_bytes() == before


@pytest.mark.parametrize("operation", ["block", "unblock", "trash", "restore", "delete_file"])
def test_patch_non_ingest_receipts_preserve_true_preimages(tmp_path, operation):
    from dataset_recommender.corpus import patch_package as pp

    account = "acct_operations"
    _bound_upload(tmp_path, account, "seed")
    pp.block_uids(tmp_path, account, ["shared"])
    if operation == "restore":
        pp.trash_adds(tmp_path, account, ["seed"])
    target = pp.patch_path_for(tmp_path, account)
    before = target.read_bytes()
    store = snapshot_store(tmp_path)
    with pp.bind_patch_scope(account):
        sid = store.capture("curate.remove")
        with pp.collect_patch_mutations() as receipts:
            if operation == "block":
                pp.block_uids(tmp_path, account, ["other"])
            elif operation == "unblock":
                pp.unblock_uids(tmp_path, account, ["shared"])
            elif operation == "trash":
                pp.trash_adds(tmp_path, account, ["seed"])
            elif operation == "restore":
                pp.restore_adds(tmp_path, account, ["seed"])
            else:
                pp.restore_patch_file(tmp_path, account, existed=False)
        store.finalize(sid, mutation={"patch": receipts})
    result = trace_rollback.apply_rollback(tmp_path, sid)
    assert result["rolled_back"] and not result["conflict"]
    assert target.read_bytes() == before


@pytest.mark.parametrize("damage", ["after", "preimage"])
def test_patch_rmw_fingerprint_damage_is_conflict_not_overwrite(monkeypatch, _tmp_root, damage, capsys):
    from dataset_recommender.corpus import patch_package as pp

    account = "acct_damage"
    _bound_upload(_tmp_root, account, "seed")
    _, sid = _patch_scope_execute(
        monkeypatch, _tmp_root, account,
        [{"dataset_uid": "mine", "dataset_name": "mine", "species": "Human"}])
    store = snapshot_store(_tmp_root)
    if damage == "after":
        meta = store.load(sid)
        meta["diff"]["modified"][0]["after"].pop("sha256")
        store.save_meta(sid, meta)
    else:
        store.preimage_path(sid, f"{account}.json").write_bytes(b"damaged")
    target = pp.patch_path_for(_tmp_root, account)
    before = target.read_bytes()
    assert trace_rollback.main(["--root", str(_tmp_root), "--snapshot", sid, "--apply"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["conflict"] and not result["rolled_back"]
    assert target.read_bytes() == before
