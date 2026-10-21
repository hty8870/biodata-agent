"""独立学习真实存储/传输测试；模型替身仅证明 ReAct 接线与安全契约。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dataset_recommender.agent import collab_rules as rules
from dataset_recommender.agent.learning_loop import run_learning_loop
from dataset_recommender.app.conversation_runs import RunError
from dataset_recommender.app.conversation_service import ConversationService
from dataset_recommender.app.conversation_store import ConversationStore
from dataset_recommender.app.learning_api import build_learning_router
from dataset_recommender.app.learning_service import LearningService
from dataset_recommender.app.learning_store import LearningStore
from dataset_recommender.app.workspace_store import WorkspaceStore
from dataset_recommender.llm.llm_client import LLMConfig

CFG = LLMConfig(enable_llm=True, api_key='synthetic-key', base_url='https://model.example.test/v1', model='test')
ITEM = {'text': '汇报先给结论再给依据', 'tier': 'explicit', 'evidence': ['以后汇报先给结论再给依据'], 'permanence': 'persistent'}


def relation(kind='overlap_incomparable', ids=None, refs=None):
    return {'relation': kind, 'related_ids': ids or [], 'reason': '依据当前明确反馈',
            'evidence_refs': refs or ['feedback:test'], 'error_code': ''}


@pytest.fixture
def learning(tmp_path, monkeypatch):
    monkeypatch.setenv('BIODATA_WORKSPACE_DB', str(tmp_path / 'workspace.sqlite'))
    app = LearningService(ConversationStore(tmp_path), WorkspaceStore(tmp_path))
    yield app
    app.close()


def source(app, text=ITEM['evidence'][0], request='test', project=''):
    from dataset_recommender.agent.agent_exec import _endpoint_fp_from_config
    fp = _endpoint_fp_from_config(CFG)
    sid = app.store.feedback('local', fp, project, request_id=request, text=text)
    row = next(r for r in app.store.evidence('local', fp, project) if r['id'] == sid)
    return fp, sid, dict(row, principal='local')


def candidate(app, *, kind='overlap_incomparable', existing=None, known=True, project=''):
    fp, sid, evidence = source(app, project=project)
    existing = existing if existing is not None else []
    return fp, rules.save_learning_candidate(app.root, ITEM, principal='local', endpoint_fp=fp,
        project_id=project, sources=[evidence], relation=relation(kind, [r['id'] for r in existing], [sid]),
        existing=existing, permanence='persistent' if known else 'unknown')['candidate']


def review(app, fp, row, decision='approve', **kwargs):
    return app.review('local', fp, row['project_id'], candidate_id=row['id'], decision=decision,
        expected_fingerprint=row['fingerprint'], expected_rules_fingerprint=row['existing_fingerprint'], **kwargs)


# ---------------------------------------------------------------- 统一知识道闸测试链
#
# 旧「候选 review 即生效」语义已迁移到统一道闸：候选只是草稿提案，须经服务端评估
# （本文件用离线 predictor 接缝）+ 真人确认提交才生效。approve/review 的旧计数口径
# （approved 即生效）相应升级为「草稿 → 评估 → 真人提交」三段；竞态/额度/隔离钉保留。

def _knowledge(app):
    from dataset_recommender.app.knowledge_service import KnowledgeService
    return KnowledgeService(app.store.conversations)


def _known(request, kind='overlap_incomparable', ids=None, refs=None):
    from test_memory_relations import decision
    result = decision(request, kind if ids is None else 'overlap_incomparable')
    for pair in result['semantic_pairs']:
        if ids is None or pair['old_id'] in ids:
            pair.update(relation=kind, error_code='model_uncertain' if kind is None else None)
    if refs is not None:
        result['evidence_refs'] = refs
    return result


def gate_assess(app, fp, row, *, project='', kind='overlap_incomparable', ids=None, refs=None, principal='local', cfg=None):
    """草稿提案经真实道闸评估（离线关系接缝）；不断言 ok——未通过也是合法结果。"""
    assessed = _knowledge(app).assess(principal, fp, project, change_id=row['id'],
        expected_change_revision=row['change_revision'], cfg=cfg or CFG,
        predictor=lambda req: _known(req, kind, ids, refs))
    return assessed


def gate_commit(app, fp, assessed, *, project='', principal='local'):
    committed = _knowledge(app).commit(principal, fp, project, change_id=assessed['change_id'],
        expected_change_revision=assessed['change_revision'], assessment_id=assessed['assessment_id'],
        expected_catalog_generation=assessed['catalog_generation'], human_confirmed=True)
    assert committed['ok'], committed
    return committed


def approve_commit(app, items, *, principal='local', fp=None, project='', cfg=None):
    """旧「approve 即生效」的新链等价：提案 + 评估 + 真人提交。返回草稿回执。"""
    out = rules.approve_collab_rules(app.root, items, principal=principal,
                                     endpoint_fp=fp, project_id=project)
    for receipt in out['changes']:
        if receipt.get('ok'):
            assessed = gate_assess(app, fp, {'id': receipt['change_id'],
                'change_revision': receipt['change_revision']}, project=project, principal=principal, cfg=cfg)
            assert assessed['ok'], assessed
            gate_commit(app, fp, assessed, project=project, principal=principal)
    return out


def fresh_candidate(app, fp, row, *, project=''):
    """评估后重新取候选投影（fingerprint/existing_fingerprint 随版本更新）。"""
    return rules.get_learning_candidate(app.root, row['id'], principal='local',
                                        endpoint_fp=fp, project_id=project)


def knowledge_digest(app):
    """统一知识库状态指纹（替代旧账本文件字节快照——原档已封存，活跃态在 SQLite）。"""
    service = _knowledge(app)
    with service.store.database() as conn:
        rows = [dict(r) for r in conn.execute(
            'SELECT owner,endpoint_fp,id,project_id,kind,status,revision,data_json FROM knowledge_entries ORDER BY id')]
        changes = [dict(r) for r in conn.execute(
            'SELECT owner,endpoint_fp,id,status,revision,fingerprint FROM knowledge_changes ORDER BY id')]
    return rules.content_fingerprint({'entries': rows, 'changes': changes})


def finish_traces(kwargs, evidence):
    """模拟真实 loop 的核销回执形状：实读观察 + finish 成功回执（核销只认它）。"""
    kwargs['on_trace']({'action': {'tool': 'read_evidence', 'args': {}}, 'observation': {'ok': True,
        'evidence': [{'id': e['id'], 'source_hash': e['source_hash']} for e in evidence]}})
    kwargs['on_trace']({'action': {'tool': 'finish', 'args': {}}, 'observation': {'ok': True,
        'finished': True, 'processed_source_ids': [e['id'] for e in evidence]}})


def test_pending_legacy_exact_counts_and_reject_audit(learning):
    fp, row = candidate(learning)
    assert rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp) == []
    assert _prompt_rules(learning, fp) == ''
    denied = review(learning, fp, row)
    assert denied['error_code'] == 'human_confirmation_required'
    rejected = review(learning, fp, row, 'reject', feedback='只是本轮临时要求')
    assert rejected['ok'] and rejected['candidate']['status'] == 'rejected'
    audit = learning.candidates('local', fp, record_type='audit')['items'][0]
    assert audit['feedback'] == '只是本轮临时要求' and audit['relation_label'] is None
    out = rules.approve_collab_rules(learning.root, [row, dict(audit, text=ITEM['text'])], principal='local', endpoint_fp=fp)
    assert out['approved'] == 0 and out['rejected'] == 2 and out['proposed_count'] == 0, \
        "候选投影/审计行不是规则提案素材，诚实计数 rejected"
    out = rules.approve_collab_rules(learning.root, [ITEM], principal='local', endpoint_fp=fp)
    assert out['approved'] == 0 and out['proposed_count'] == 1, "approve 只产生草稿"
    gate_commit(learning, fp, gate_assess(learning, fp, {'id': out['changes'][0]['change_id'],
        'change_revision': out['changes'][0]['change_revision']}))
    assert rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp)[0]['status'] == 'active'
    assert len(learning.candidates('local', fp, status='draft')['items']) == 0


def _prompt_rules(app, fp, project_id=''):
    """真实消费端：统一知识投影（旧 collab_rules_block_zh 检索通道已退役）。"""
    from dataset_recommender.agent.knowledge import knowledge_context, registered_tools
    return knowledge_context(app.root, ITEM['text'], principal='local', endpoint_fp=fp,
                             project_id=project_id, phase='decide', available_tools=registered_tools())


def test_confirm_refresh_and_actual_injection(learning):
    fp, row = candidate(learning)
    gate_assess(learning, fp, row)
    row = fresh_candidate(learning, fp, row)
    assert review(learning, fp, row, human_confirmed=True)['candidate']['status'] == 'committed'
    actual = rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp)
    assert len(actual) == 1 and actual[0]['status'] == 'active'
    assert ITEM['text'] in _prompt_rules(learning, fp)
    assert rules.list_collab_rules(learning.root, principal='other', endpoint_fp=fp) == []
    assert rules.list_collab_rules(learning.root, principal='local', endpoint_fp='other') == []
    assert len(LearningStore(ConversationStore(learning.root)).evidence('local', fp)) == 1


@pytest.mark.parametrize('kind,known', [('temporary', True), ('uncertain', True), ('overlap_incomparable', False), ('overlap_incomparable', False)])
def test_nonpermanent_cannot_activate(learning, kind, known):
    fp, row = candidate(learning, kind=kind, known=known)
    with pytest.raises(RunError):
        review(learning, fp, row, human_confirmed=True)
    assert rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp) == []


def test_atomic_replace_and_stale_snapshot(learning):
    """「替代」的新链形态：同一提案显式 [新增新规则 + 撤回旧规则] 两操作原子生效；
    目录代际或提案版本过期则拒绝提交（stale CAS）。撤回走确定性评估，新增走语义评估。"""
    fp, sid, evidence = source(learning)
    approve_commit(learning, [dict(ITEM, text='汇报先给依据再给结论')], fp=fp)
    old = rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp)[0]
    service = _knowledge(learning)
    proposal = service.propose('local', fp, '', request_id='replace-1', operations=[
        {'op': 'create', 'after': {'kind': 'memory', 'title': '', 'text': ITEM['text'],
            'trigger': '', 'tier': 'explicit', 'permanence': 'persistent', 'payload': {}, 'links': []}},
        {'op': 'retract', 'entry_id': old['id'], 'expected_entry_revision': old['revision']}],
        source_refs=[{'type': 'learning', 'id': sid}])
    assert proposal['ok'], proposal
    assessed = service.assess('local', fp, '', change_id=proposal['change_id'],
        expected_change_revision=proposal['change_revision'], cfg=CFG,
        predictor=lambda req: _known(req))
    assert assessed['ok'], assessed
    stale = service.commit('local', fp, '', change_id=proposal['change_id'],
        expected_change_revision=assessed['change_revision'], assessment_id=assessed['assessment_id'],
        expected_catalog_generation=assessed['catalog_generation'] + 1, human_confirmed=True)
    assert stale['error_code'] == 'knowledge_assessment_stale', "代际不符不得提交"
    gate_commit(learning, fp, assessed)
    actual = rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp)
    assert sorted(r['status'] for r in actual) == ['active', 'retracted'], "替代原子生效"
    late = service.commit('local', fp, '', change_id=proposal['change_id'],
        expected_change_revision=assessed['change_revision'], assessment_id=assessed['assessment_id'],
        expected_catalog_generation=assessed['catalog_generation'], human_confirmed=True)
    assert late['error_code'] == 'knowledge_change_conflict', "已提交提案不能重复生效"


def test_cas_failure_is_zero_mutation(learning):
    fp, row = candidate(learning)
    gate_assess(learning, fp, row)
    row = fresh_candidate(learning, fp, row)
    approve_commit(learning, [dict(ITEM, text='用简短段落')], fp=fp)
    before = knowledge_digest(learning)
    denied = review(learning, fp, row, human_confirmed=True)
    assert denied['error_code'] == 'knowledge_assessment_stale', "评估后目录已变化：拒绝提交（回执口径）"
    assert knowledge_digest(learning) == before, "CAS 失败必须零写入"
    assert rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp)[0]['text'] == '用简短段落'


def test_project_cannot_replace_global(learning):
    """项目提案与仍生效的继承规则不相容时阻挡；语义判断不授予跨范围替换权。"""
    project = learning.workspace.create_project('local', '项目')['id']
    fp, sid, evidence = source(learning, project=project)
    approve_commit(learning, [ITEM], fp=fp)
    globals_ = rules.learning_rules_snapshot(learning.root, principal='local', endpoint_fp=fp, project_id='')
    row = rules.save_learning_candidate(learning.root, ITEM, principal='local', endpoint_fp=fp,
        project_id=project, sources=[evidence], existing=rules.learning_rules_snapshot(
            learning.root, principal='local', endpoint_fp=fp, project_id=project),
        relation=None, permanence='persistent')['candidate']
    assessed = gate_assess(learning, fp, row, project=project, kind='disjoint',
        ids=[globals_[0]['id']], refs=[sid])
    assert not assessed['ok'] and assessed['error_code'] == 'knowledge_relation_blocked'
    denied = review(learning, fp, fresh_candidate(learning, fp, row, project=project),
                    human_confirmed=True)
    assert denied['error_code'] == 'knowledge_not_committable'
    assert rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp,
        project_id='')[0]['status'] == 'active'


def test_sources_commit_once_not_revision_import_or_branch(learning):
    store = learning.store.conversations
    fp, _, _ = source(learning)
    conv = store.create('local')
    run, fresh = store.claim('local', 'origin', 'fingerprint', conversation_id=conv['id'], request={'shared': True, 'text': ITEM['evidence'][0], 'expected_revision': 0}, boot_id='test')
    store.bind_learning_endpoint('local', run['id'], fp)
    store.transition('local', run['id'], 'done', {'ok': True, 'result_payload': {'result_total': 1}})
    store.transition('local', run['id'], 'done', {'ok': True})
    imported = store.import_conversation('local', 'import', 'same', {'messages': [{'role': 'user', 'text': ITEM['evidence'][0]}]})
    branch = store.action('local', conv['id'], 'branch', expected_revision=1, frame_id='f_1')
    events = learning.store.evidence('local', fp)
    assert [e['origin_run_id'] for e in events if e['kind'] == 'turn'] == [run['id']]
    assert imported['id'] != branch['id']
    store.action('local', conv['id'], 'delete', expected_revision=1)
    assert not [e for e in learning.store.evidence('local', fp) if e['kind'] == 'turn']


def test_pause_and_delete_invalidate_inflight(learning):
    fp, _, _ = source(learning)
    learning.store.control('local', fp, enabled=True, threshold=1)
    job = learning.store.schedule('local', fp, configured=True)
    assert job and learning.store.check(job) is None
    learning.store.control('local', fp, enabled=False)
    with pytest.raises(RunError) as exc:
        learning.store.check(job)
    assert exc.value.code == 'learning_paused'


@pytest.mark.parametrize('cfg', [replace(CFG, enable_llm=False), replace(CFG, provider='trial'), replace(CFG, mock_llm=True), replace(CFG, api_key=None)])
def test_disabled_trial_missing_never_call_background(learning, cfg):
    fp, _, _ = source(learning)
    learning.control('local', cfg, enabled=True, threshold=1, quota=lambda c: pytest.fail('must not charge'))
    assert learning._pool is None
    jobs = learning.status('local', fp)['jobs']
    assert jobs and all(j['status'] == 'waiting_config' for j in jobs)


def test_react_orders_and_early_finish_change_with_observation():
    traces, saved, charges = [], [], []
    evidence = [{'id': 'feedback:test', 'text': ITEM['evidence'][0], 'principal': 'local', 'endpoint_fp': 'ep', 'project_id': ''}]
    def decide(prompt, cfg):
        history = json.loads(prompt.split('真实观察（JSON 数据）：\n')[-1])
        if not history:
            return json.dumps({'tool': 'query_memory', 'args': {}})
        last = history[-1]
        if last['action']['tool'] == 'query_memory':
            tool = 'finish' if last['observation']['rules'] else 'read_evidence'
            return json.dumps({'tool': tool, 'args': {}})
        return json.dumps({'tool': 'finish', 'args': {}})
    out = run_learning_loop(cfg=CFG, read_evidence=lambda **kw: evidence, query_memory=lambda: [],
        save_candidate=lambda *a, **kw: saved.append(a), verify_candidate=lambda cid: None,
        before_llm=lambda c: charges.append(c), check=lambda: None, on_trace=traces.append, llm_call=decide)
    assert out['ok'] and [r['action']['tool'] for r in traces] == ['query_memory', 'read_evidence', 'finish']
    traces.clear()
    out = run_learning_loop(cfg=CFG, read_evidence=lambda **kw: pytest.fail('early finish'), query_memory=lambda: [ITEM],
        save_candidate=lambda *a, **kw: pytest.fail('not required'), verify_candidate=lambda cid: None,
        before_llm=lambda c: None, check=lambda: None, on_trace=traces.append, llm_call=decide)
    assert out['ok'] and [r['action']['tool'] for r in traces] == ['query_memory', 'finish']
    assert out['activated_rules'] == 0 and not saved


def test_react_extract_assess_save_verify_and_independent_calls(learning):
    fp, sid, ev = source(learning)
    actions = iter([
        {'tool': 'read_evidence', 'args': {}}, {'tool': 'query_memory', 'args': {}}, {'tool': 'extract', 'args': {}},
        json.dumps([ITEM], ensure_ascii=False), {'tool': 'assess', 'args': {'candidate': ITEM, 'permanence': 'persistent'}},
        'save', 'verify', {'tool': 'finish', 'args': {}},
    ])
    traces, charges = [], []
    def llm(prompt, cfg):
        action = next(actions)
        if action == 'save':
            action = {'tool': 'save_candidate', 'args': {'assessment_id': traces[-1]['observation']['assessment_id']}}
        elif action == 'verify':
            action = {'tool': 'verify', 'args': {'candidate_id': traces[-1]['observation']['candidate']['id']}}
        return action if isinstance(action, str) else json.dumps(action, ensure_ascii=False)
    out = run_learning_loop(cfg=CFG, read_evidence=lambda **kw: [ev], query_memory=lambda: [],
        save_candidate=lambda item, **kw: rules.save_learning_candidate(learning.root, item, principal='local', endpoint_fp=fp, project_id='', **kw),
        verify_candidate=lambda cid: learning.candidates('local', fp)['items'][0],
        before_llm=lambda c: charges.append(c), check=lambda: None, on_trace=traces.append,
        llm_call=llm, assessor=lambda *a, **kw: relation(refs=[sid]))
    assert out['ok'] and len(out['saved_candidate_ids']) == 1 and out['activated_rules'] == 0
    assert len(charges) == out['calls'] == 8
    assert [r['action']['tool'] for r in traces] == ['read_evidence', 'query_memory', 'extract', 'assess', 'save_candidate', 'verify', 'finish']


def test_tool_failure_never_success_and_budget_stops():
    traces = []
    out = run_learning_loop(cfg=CFG, read_evidence=lambda **kw: [], query_memory=lambda: [],
        save_candidate=lambda *a, **kw: pytest.fail('unassessed'), verify_candidate=lambda cid: None,
        before_llm=lambda c: None, check=lambda: None, on_trace=traces.append,
        llm_call=lambda *a: '{"tool":"save_candidate","args":{"assessment_id":"fake"}}')
    assert not out['ok'] and not out['saved_candidate_ids']
    assert all(not e['observation']['ok'] for e in traces)


def test_http_review_is_web_only_mcp_service_cannot_approve(learning):
    fp, row = candidate(learning)
    gate_assess(learning, fp, row)
    row = fresh_candidate(learning, fp, row)
    shared = SimpleNamespace(store=learning.store.conversations, workspace=learning.workspace)
    from dataset_recommender.app import learning_service
    learning_service._SERVICES[(str(shared.store.path), str(shared.store.project_root))] = learning
    app = FastAPI()
    app.include_router(build_learning_router(service=lambda: shared, principal=lambda req: 'local',
        endpoint=lambda *a: fp, require_origin=lambda req: None, configure=lambda *a: CFG, quota=lambda *a: None))
    client = TestClient(app)
    payload = {'candidate_id': row['id'], 'decision': 'approve', 'expected_fingerprint': row['fingerprint'], 'expected_rules_fingerprint': row['existing_fingerprint']}
    assert client.post('/api/learning/review', json=payload).status_code == 400
    assert client.post('/api/learning/review', json={**payload, 'owner': 'other', 'confirm': True}).status_code == 422
    assert client.post('/api/learning/review', json={**payload, 'confirm': True}).status_code == 200
    assert client.get('/api/learning/candidates').json()['active_rules'][0]['learning_rule']


def test_ledger_multiprocess_no_lost_confirmations(tmp_path):
    """多进程竞态钉的新链形态：每个进程走完整统一道闸（提案→评估→真人提交），
    SQLite 事务 + 幂等请求编号 + 评估 CAS（stale 拒绝旧评估、重评后再提交）
    保证并发不丢已提交确认。"""
    fp = _fp_of_cfg()
    code = '''import sys
from pathlib import Path
from dataset_recommender.agent.collab_rules import approve_collab_rules
from dataset_recommender.app.conversation_store import ConversationStore
from dataset_recommender.app.knowledge_service import KnowledgeService
from dataset_recommender.llm.llm_client import LLMConfig
root=Path(sys.argv[1])
text=sys.argv[2]
cfg=LLMConfig(enable_llm=True,api_key='synthetic-mp-key',base_url='https://model.example.test/v1',model='test')
out=approve_collab_rules(root,[{'text':text,'tier':'explicit','evidence':[text],'permanence':'persistent'}],principal='local',endpoint_fp=sys.argv[3])
assert out['proposed_count']==1,out
service=KnowledgeService(ConversationStore(root))
receipt=out['changes'][0]
def known(req):
 refs=[req['evidence'][0]['id']]
 return {'semantic_pairs':[{'old_id':r['id'],'relation':'overlap_incomparable','reason':'离线接缝','old_quote':r['text'][:100],'new_quote':req['candidate']['text'][:100],'evidence_refs':refs,'error_code':None} for r in req['existing']], 'edit_checks':[], 'evidence_refs':refs,'error_code':None}
revision=receipt['change_revision']
for attempt in range(8):
 assessed=service.assess('local',sys.argv[3],'',change_id=receipt['change_id'],expected_change_revision=revision,cfg=cfg,predictor=known)
 if not assessed['ok']:
  assert assessed['error_code']=='knowledge_assessment_stale',assessed
  revision=assessed['change_revision']
  continue
 committed=service.commit('local',sys.argv[3],'',change_id=receipt['change_id'],expected_change_revision=assessed['change_revision'],assessment_id=assessed['assessment_id'],expected_catalog_generation=assessed['catalog_generation'],human_confirmed=True)
 if committed['ok']:
  break
 assert committed['error_code']=='knowledge_assessment_stale',committed
 revision=committed['change_revision']
else:
 raise AssertionError('concurrent commits starved the retry budget')
'''
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    processes = [subprocess.Popen([sys.executable, '-B', '-c', code, str(tmp_path), '汇报偏好-' + str(i), fp], env=env) for i in range(4)]
    assert all(p.wait(timeout=30) == 0 for p in processes)
    assert len(rules.list_collab_rules(tmp_path, principal='local', endpoint_fp=fp)) == 4


def _fp_of_cfg():
    from dataset_recommender.agent.agent_exec import _endpoint_fp_from_config
    return _endpoint_fp_from_config(CFG)


def test_background_executor_commit_config_quota_and_restart(learning, monkeypatch):
    from dataset_recommender.app import conversation_service, learning_service
    key = (str(learning.store.path), str(learning.root))
    learning_service._SERVICES[key] = learning
    started, release = threading.Event(), threading.Event()
    observed, charges = [], []
    def loop(**kwargs):
        kwargs['before_llm'](kwargs['cfg'])
        started.set()
        assert release.wait(5)
        kwargs['check']()
        evidence = kwargs['read_evidence']()
        observed.extend(evidence)
        finish_traces(kwargs, evidence)
        return {'ok': True, 'activated_rules': 0, 'detail': '零提案结束'}
    learning.loop = loop
    fp, _, _ = source(learning)
    learning.store.control('local', fp, enabled=True, threshold=1)
    def fake_turn(text, **kwargs):
        return {'ok': True, 'route': 'none', 'plan': None, 'echo_zh': '真实回执', 'via': 'test', 'agent': {}}
    monkeypatch.setattr(conversation_service, 'execute_turn', fake_turn)
    monkeypatch.setattr('dataset_recommender.app.replay_service.record_turn', lambda *a, **kw: None)
    shared = ConversationService(learning.root, workspace=learning.workspace)
    run = shared.chat('local', ITEM['evidence'][0], request_id='committed', configure=lambda: CFG, quota=lambda c: charges.append(c))
    shared.runtime.wait_result('local', run['id'], timeout=5)
    assert started.wait(5)
    assert shared.store.load('local', run['id'])['status'] == 'done'
    assert not shared.runtime._tasks, '独立学习不能占主owner回合名额'
    release.set()
    deadline = time.monotonic() + 5
    while learning._tasks and time.monotonic() < deadline:
        time.sleep(.02)
    jobs = learning.status('local', fp)['jobs']
    assert jobs[0]['status'] == 'done' and len(charges) == 2, '独立真实调用不得复用主Admission._admitted'
    assert any(e['origin_run_id'] == run['id'] for e in observed)
    assert learning.status('local', fp, job_id=jobs[0]['id'])['trace']
    shared.runtime.close()
    source(learning, request='new-after-restart')
    restarted = LearningService(ConversationStore(learning.root), learning.workspace)
    restarted.notify('local', fp, force=True)
    assert restarted._pool is None and restarted.status('local', fp)['jobs'][0]['status'] == 'waiting_config'
    assert not restarted._configs
    restarted.close()


def test_late_model_result_pause_cannot_save(learning):
    fp, sid, evidence = source(learning)
    entered, release = threading.Event(), threading.Event()
    def loop(**kwargs):
        entered.set(); assert release.wait(5)
        try:
            kwargs['save_candidate'](ITEM, sources=[evidence], existing=[], relation=relation(refs=[sid]), permanence='persistent')
        except RunError as exc:
            return {'ok': False, 'state': 'paused', 'error_code': exc.code}
        pytest.fail('late candidate persisted')
    learning.loop = loop
    learning.control('local', CFG, enabled=True, threshold=1)
    assert entered.wait(5)
    learning.control('local', CFG, enabled=False)
    release.set()
    deadline = time.monotonic() + 5
    while learning._tasks and time.monotonic() < deadline:
        time.sleep(.02)
    assert not learning.candidates('local', fp)['items']
    assert learning.status('local', fp)['jobs'][0]['status'] == 'paused'


def test_mcp_learning_same_service_and_no_approval_flag(learning, monkeypatch):
    import asyncio
    from mcp.server.fastmcp import FastMCP
    from dataset_recommender.app import mcp_agent_tools, llm_admission, learning_service
    fp, row = candidate(learning)
    shared = SimpleNamespace(store=learning.store.conversations, workspace=learning.workspace)
    monkeypatch.setattr(llm_admission, 'materialize', lambda **kwargs: CFG)
    monkeypatch.setattr(mcp_agent_tools, 'service_for', lambda root: shared)
    learning_service._SERVICES[(str(shared.store.path), str(shared.store.project_root))] = learning
    mcp = FastMCP('learning-test')
    mcp_agent_tools.register_agent_tools(mcp, root=lambda: learning.root)
    async def exercise():
        with mcp_agent_tools.bind_identity('local'):
            status = await mcp.call_tool('agent_learning', {'action': 'status'})
            assert json.loads(status[0].text)['scope']['owner'] == 'local'
            candidates = await mcp.call_tool('agent_learning', {'action': 'candidates'})
            assert json.loads(candidates[0].text)['items'][0]['id'] == row['id']
            result = await mcp.call_tool('agent_learning', {'action': 'review', 'candidate_id': row['id'], 'decision': 'approve', 'expected_fingerprint': row['fingerprint']})
            assert json.loads(result[0].text)['error_code'] == 'human_confirmation_required'
            names = {tool.name for tool in await mcp.list_tools()}
            assert 'agent_learning' in names
            tool = next(t for t in await mcp.list_tools() if t.name == 'agent_learning')
            assert not {'owner', 'confirmed', 'human_confirmed'} & set(tool.inputSchema['properties'])
    asyncio.run(exercise())
    assert not rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp)


def test_pending_reassessment_updates_cas_not_rows_or_reviewed_state(learning):
    """重评只推进提案 CAS 版本、不新增行：同内容再存幂等命中同一提案；旧指纹/旧评估
    的 review 被拒（CAS）；驳回在知识审计留痕（旧 review: 来源账已并入统一审计）。"""
    fp, row = candidate(learning, kind='uncertain', known=False)
    first = dict(row)
    assessed = gate_assess(learning, fp, row, kind='uncertain', refs=None)
    assert not assessed['ok'] and assessed['assessment']['committable'] is False
    again = gate_assess(learning, fp, {'id': row['id'], 'change_revision': assessed['change_revision']},
        kind='uncertain')
    assert again['change_revision'] > first['change_revision']
    assert len(learning.candidates('local', fp)['items']) == 1, "重评不新增提案行"
    with pytest.raises(RunError) as stale:
        review(learning, fp, first, 'reject', feedback='旧指纹')
    assert stale.value.code == 'candidate_revision_conflict'
    fresh = fresh_candidate(learning, fp, row)
    rejected = review(learning, fp, fresh, 'reject', feedback='本轮例外')
    assert rejected['ok'] and rejected['candidate']['status'] == 'rejected'
    audits = learning.candidates('local', fp, record_type='audit')['items']
    assert any(a.get('action') == 'reject' and a.get('feedback') == '本轮例外' for a in audits)
    fp, repeated = candidate(learning, kind='uncertain', known=False)
    assert repeated['status'] == 'rejected', \
        "同内容+同出处再提案幂等命中已驳回提案，不复活（旧口径保留，由统一道闸请求幂等实现）"
    assert len(learning.candidates('local', fp)['items']) == 1, "幂等命中不新增提案行"


def test_candidate_audit_pagination_keeps_more_than_legacy_rotation(learning):
    """统一存储无旧 200 行旋转：205 份提案+驳回全部保留，SQL 分页边界精确；
    审计面为每个提案/驳回各留一行。"""
    fp, _, ev = source(learning)
    ids = []
    for i in range(205):
        row = rules.save_learning_candidate(learning.root, dict(ITEM, text='持久要求 ' + str(i)),
            principal='local', endpoint_fp=fp, project_id='', sources=[ev], existing=[],
            relation=None, permanence='persistent')['candidate']
        ids.append(row['id'])
        rules.review_learning_candidate(learning.root, row['id'], principal='local', endpoint_fp=fp,
            decision='reject', expected_fingerprint=row['fingerprint'], feedback='不采纳')
    assert len(learning.candidates('local', fp, limit=200)['items']) == 200
    assert learning.candidates('local', fp, offset=200, limit=200)['next_offset'] is None
    assert len(learning.candidates('local', fp, offset=200, limit=200)['items']) == 5
    audits = learning.candidates('local', fp, record_type='audit', limit=200)
    assert audits['next_offset'] == 200 and len(audits['items']) == 200
    assert len(learning.candidates('local', fp, record_type='audit', offset=400)['items']) == 10
    assert rules.get_learning_candidate(learning.root, ids[0], principal='local', endpoint_fp=fp)['status'] == 'rejected'
    out = rules.approve_collab_rules(learning.root, [ITEM], principal='local', endpoint_fp=fp)
    assert out['approved'] == 0 and out['proposed_count'] == 1
    gate_commit(learning, fp, gate_assess(learning, fp, {'id': out['changes'][0]['change_id'],
        'change_revision': out['changes'][0]['change_revision']}))
    assert len(rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp)) == 1
    assert len(learning.candidates('local', fp, offset=200, limit=200)['items']) == 6


def test_deleted_source_blocks_late_candidate_and_unblocks_future_job(learning):
    from dataset_recommender.agent.agent_exec import _endpoint_fp_from_config
    fp = _endpoint_fp_from_config(CFG)
    conv = learning.store.conversations.create('local')
    sid = learning.store.feedback('local', fp, '', request_id='deletable', text=ITEM['evidence'][0], conversation_id=conv['id'])
    ev = dict(learning.store.evidence('local', fp)[0], principal='local')
    entered, release = threading.Event(), threading.Event()
    def loop(**kwargs):
        entered.set(); assert release.wait(5)
        try:
            kwargs['save_candidate'](ITEM, sources=[ev], existing=[], relation=relation(refs=[sid]), permanence='persistent')
        except RunError as exc:
            return {'ok': False, 'state': 'paused', 'error_code': exc.code}
        pytest.fail('deleted source persisted candidate')
    learning.loop = loop
    learning.control('local', CFG, enabled=True, threshold=1)
    assert entered.wait(5)
    learning.store.conversations.action('local', conv['id'], 'delete', expected_revision=0)
    release.set()
    deadline = time.monotonic() + 5
    while learning._tasks and time.monotonic() < deadline:
        time.sleep(.02)
    assert not learning.candidates('local', fp)['items']
    assert learning.status('local', fp)['jobs'][0]['status'] == 'paused'
    source(learning, request='new-valid')
    job = learning.store.schedule('local', fp)
    assert job and job['status'] == 'waiting_config'


def test_trigger_counts_all_origins_not_bounded_evidence_page(learning):
    from dataset_recommender.agent.agent_exec import _endpoint_fp_from_config
    from dataset_recommender.app.learning_store import register_turn
    fp = _endpoint_fp_from_config(CFG)
    learning.store.control('local', fp, enabled=True, threshold=20)
    with learning.store.conversations.database(write=True) as conn:
        for i in range(20):
            register_turn(conn, {'owner': 'local', 'id': 'origin-' + str(i), 'project_id': '', 'conversation_id': ''},
                {'learning_endpoint_fp': fp, 'text': '独立回合 ' + str(i)}, 'done')
    assert learning.store.fresh_stats('local', fp)['total'] == 20
    assert learning.status('local', fp)['fresh_count'] == 20
    job = learning.store.schedule('local', fp)
    assert job and len(json.loads(job['sources_json'])) == 12


def test_local_legacy_rules_validate_shared_project_and_are_consumed(learning, monkeypatch):
    from dataset_recommender.app import webapp
    from dataset_recommender.agent.agent_exec import _endpoint_fp_from_config
    monkeypatch.setattr(webapp, 'PROJECT_ROOT', learning.root)
    monkeypatch.setattr(webapp, 'load_llm_config', lambda **kwargs: CFG)
    monkeypatch.setenv('BIODATA_REQUIRE_ACCOUNT', '0')
    project = learning.workspace.create_project('local', '共享项目')['id']
    fp = _endpoint_fp_from_config(CFG)
    approve_commit(learning, [ITEM], principal='anonymous', fp=fp, project=project)
    before = rules.list_collab_rules(learning.root, principal='anonymous', endpoint_fp=fp, project_id=project)[0]
    client = TestClient(webapp.app, base_url='http://127.0.0.1')
    params = {'base_url': CFG.base_url, 'model': CFG.model, 'provider': CFG.provider, 'project_id': project}
    loaded = client.get('/api/rules/list', params=params)
    assert loaded.status_code == 200 and loaded.json()['rules'][0]['id'] == before['id']
    assert ITEM['text'] in _prompt_rules(learning, fp, project_id=project)
    dup = rules.approve_collab_rules(learning.root, [ITEM], principal='local', endpoint_fp=fp, project_id=project)
    assert dup['approved'] == 0 and dup['duplicated'] == 1 and dup['rejected'] == 0
    owned = client.get('/api/learning/candidates', params=params)
    assert owned.status_code == 200 and owned.json()['active_rules'][0]['id'] == before['id']
    # 跨账户读别人项目的知识：fail-closed 作用域错误（不是静默空表）
    with pytest.raises(RunError) as exc:
        rules.list_collab_rules(learning.root, principal='other-account', endpoint_fp=fp, project_id=project)
    assert exc.value.code == 'knowledge_project_not_found'
    physical = _knowledge(learning).entry('anonymous', fp, before['id'])
    assert physical['id'] == before['id'] and physical['principal'] == 'anonymous'


def test_contextual_same_text_new_condition_does_not_overwrite_old_rule(learning):
    """同文异条件并存的新链钉：旧规则原子保留（同 id、内容逐位不变），新条件是独立条目。"""
    fp, sid, evidence = source(learning)
    approve_commit(learning, [dict(ITEM, trigger='写代码时')], principal='anonymous', fp=fp)
    existing = rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp)
    snapshot = rules.learning_rules_snapshot(learning.root, principal='local', endpoint_fp=fp, project_id='')
    row = rules.save_learning_candidate(learning.root, dict(ITEM, trigger='汇报结果时'), principal='local',
        endpoint_fp=fp, project_id='', sources=[evidence], existing=snapshot,
        relation=None, permanence='persistent')['candidate']
    assessed = gate_assess(learning, fp, row, kind='overlap_incomparable', ids=[existing[0]['id']], refs=[sid])
    assert assessed['ok'], assessed
    review(learning, fp, fresh_candidate(learning, fp, row), human_confirmed=True)
    active = [r for r in rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp) if r['status'] == 'active']
    assert len(active) == 2 and {r['trigger'] for r in active} == {'写代码时', '汇报结果时'}
    assert len({r['id'] for r in active}) == 2
    assert next(r for r in active if r['id'] == existing[0]['id']) == existing[0]


def test_real_relation_module_in_react_uses_llmresult_cfg_and_human_cas(learning):
    from dataset_recommender.llm.llm_client import LLMResult
    fp, sid, evidence = source(learning)
    approve_commit(learning, [dict(ITEM, text='汇报先给依据再给结论')], principal='anonymous', fp=fp)
    existing = [dict(r, principal='local', project_id='') for r in rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp)]
    traces, charges, relation_prompts = [], [], []
    actions = iter([{'tool': 'query_memory', 'args': {}}, {'tool': 'read_evidence', 'args': {}},
        {'tool': 'assess', 'args': {'candidate': ITEM, 'permanence': 'persistent'}}, 'save',
        {'tool': 'finish', 'args': {}}])
    def completion(payload, cfg):
        return LLMResult(text=json.dumps(payload, ensure_ascii=False), attempted=True, succeeded=True,
            response_used=False, provider='openai-compatible', model=cfg.model, finish_reason='stop')
    def llm(prompt, cfg):
        if '事实 JSON：\n' in prompt:
            relation_prompts.append(prompt)
            request = json.loads(prompt.split('事实 JSON：\n', 1)[1])
            return completion(_known(request, 'new_entails_old_strict'), cfg)
        action = next(actions)
        if action == 'save':
            action = {'tool': 'save_candidate', 'args': {'assessment_id': traces[-1]['observation']['assessment_id']}}
        return completion(action, cfg)
    out = run_learning_loop(cfg=CFG, read_evidence=lambda **kwargs: [evidence], query_memory=lambda: existing,
        save_candidate=lambda item, **kwargs: rules.save_learning_candidate(learning.root, item, principal='local', endpoint_fp=fp, project_id='', **kwargs),
        verify_candidate=lambda cid: rules.get_learning_candidate(learning.root, cid, principal='local', endpoint_fp=fp),
        before_llm=lambda cfg: charges.append(cfg), check=lambda: None, on_trace=traces.append, llm_call=llm)
    assert out['ok'] and len(relation_prompts) == 1 and out['calls'] == len(charges) == 6
    assessment = next(t['observation']['relation'] for t in traces if t['action']['tool'] == 'assess')
    assert assessment['semantic_pairs'][0]['relation'] == 'new_entails_old_strict' and assessment['error_code'] is None
    row = learning.candidates('local', fp)['items'][0]
    assert row['status'] == 'draft' and out['activated_rules'] == 0, "循环只产草稿提案"
    assert review(learning, fp, row, 'replace')['error_code'] == 'human_confirmation_required'
    assessed = gate_assess(learning, fp, row)
    assert assessed['ok'] and assessed['assessment']['reports'][0]['relation']['coverage']['complete']
    review(learning, fp, fresh_candidate(learning, fp, row), 'approve', human_confirmed=True)
    statuses = sorted(r['status'] for r in rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp))
    assert statuses == ['active', 'active'], "严格蕴含不授予替换权；只含新增的提案不会改写旧项。"


def _drive_relation_review(learning, evidence, item, expected, *, project='', relation_predictor=None, permanence='persistent'):
    from dataset_recommender.llm.llm_client import LLMResult
    traces, charges, api_relations = [], [], []
    fp = evidence['endpoint_fp']
    actions = iter([{'tool': 'read_evidence', 'args': {}}, {'tool': 'query_memory', 'args': {}},
        {'tool': 'assess', 'args': {'candidate': item, 'permanence': permanence}}, 'save',
        {'tool': 'finish', 'args': {}}])
    def llm(prompt, cfg):
        if '事实 JSON：\n' in prompt:
            api_relations.append(json.loads(prompt.split('事实 JSON：\n', 1)[1]))
            payload = expected(api_relations[-1]) if callable(expected) else expected
        else:
            payload = next(actions)
            if payload == 'save':
                payload = {'tool': 'save_candidate', 'args': {'assessment_id': traces[-1]['observation']['assessment_id']}}
        return LLMResult(text=json.dumps(payload, ensure_ascii=False), attempted=True, succeeded=True,
            response_used=False, provider='openai-compatible', model=cfg.model, finish_reason='stop')
    out = run_learning_loop(cfg=CFG, read_evidence=lambda **kwargs: [evidence],
        query_memory=lambda: rules.learning_rules_snapshot(learning.root, principal='local', endpoint_fp=fp, project_id=project),
        save_candidate=lambda item, **kwargs: rules.save_learning_candidate(learning.root, item, principal='local', endpoint_fp=fp, project_id=project, **kwargs),
        verify_candidate=lambda cid: rules.get_learning_candidate(learning.root, cid, principal='local', endpoint_fp=fp, project_id=project),
        before_llm=lambda cfg: charges.append(cfg), check=lambda: None, on_trace=traces.append,
        llm_call=llm, relation_predictor=relation_predictor)
    return out, traces, charges, api_relations


@pytest.mark.parametrize('permanence', ['temporary', 'unknown'])
def test_first_candidate_keeps_upstream_permanence_without_making_a_semantic_leaf(learning, permanence):
    text = '只这次先给结论；今后不固定这个顺序。'
    fp, sid, ev = source(learning, text=text)
    item = dict(ITEM, evidence=[text])
    out, traces, charges, api_calls = _drive_relation_review(learning, ev, item, _known, permanence=permanence)
    assert out['ok'] and not api_calls and out['calls'] == len(charges) == 5
    actual = next(t['observation']['relation'] for t in traces if t['action']['tool'] == 'assess')
    assert actual['semantic_pairs'] == [] and actual['coverage']['status'] == 'no_existing'
    row = learning.candidates('local', fp)['items'][0]
    assert row['permanence'] == permanence
    assessed = gate_assess(learning, fp, row, refs=[sid])
    assert assessed['error_code'] == 'knowledge_not_permanent'
    before = knowledge_digest(learning)
    denied = review(learning, fp, fresh_candidate(learning, fp, row), human_confirmed=True)
    assert denied['error_code'] == 'knowledge_not_committable'
    assert knowledge_digest(learning) == before
    assert not rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp)


@pytest.mark.parametrize('text,quote', [('先给结论\n 再给依据', '先给结论 再给依据'),
    ('Prefer\nFASTQ   Files', 'prefer fastq files')])
def test_react_and_relation_gate_agree_on_normalized_evidence(learning, text, quote):
    fp, sid, ev = source(learning, text=text)
    item = dict(ITEM, evidence=[quote])
    out, traces, charges, api_calls = _drive_relation_review(learning, ev, item, _known)
    assert out['ok'] and not api_calls and out['calls'] == len(charges) == 5
    row = learning.candidates('local', fp)['items'][0]
    assessed = gate_assess(learning, fp, row, refs=[sid])
    assert assessed['ok'], assessed
    relation_report = fresh_candidate(learning, fp, row)['relation']
    assert relation_report['coverage']['complete'] and relation_report['error_code'] is None


@pytest.mark.parametrize('fail', [False, True])
def test_react_predictor_channel_is_explicit_counted_and_never_falls_back_to_relation_api(learning, fail):
    fp, sid, ev = source(learning)
    approve_commit(learning, [dict(ITEM, text='已有另一项约定')], fp=fp)
    requests = []
    def predict(request):
        requests.append(request)
        if fail:
            raise RuntimeError('private predictor failure')
        return _known(request)
    out, traces, charges, api_calls = _drive_relation_review(learning, ev, ITEM, _known, relation_predictor=predict)
    assert out['ok'] and len(requests) == 1 and not api_calls
    assert out['calls'] == len(charges) == 6
    actual = next(t['observation']['relation'] for t in traces if t['action']['tool'] == 'assess')
    assert actual['error_code'] == ('predictor_failed' if fail else None)
    assert actual['coverage']['complete'] is not fail
    assert actual['error_code'] != 'ambiguous_predictor'


def test_inherited_global_participates_in_snapshot_and_cas_without_scope_rewrite(learning):
    """项目继承用户规则；目录变化后重评，不相容的幸存规则阻挡；同范围显式操作才可替代。"""
    project = learning.workspace.create_project('local', '目标项目')['id']
    other = learning.workspace.create_project('local', '其它项目')['id']
    fp, sid, ev = source(learning, project=project)
    approve_commit(learning, [dict(ITEM, text='全局先给依据')], fp=fp, project='')
    approve_commit(learning, [dict(ITEM, text='本项目偏好')], fp=fp, project=project)
    approve_commit(learning, [dict(ITEM, text='其它项目偏好')], fp=fp, project=other)
    approve_commit(learning, [dict(ITEM, text='其它账户偏好')], principal='other-account', fp=fp, project='')
    other_cfg = replace(CFG, base_url='https://other-endpoint.example/v1')
    from dataset_recommender.agent.agent_exec import _endpoint_fp_from_config
    other_fp = _endpoint_fp_from_config(other_cfg)
    approve_commit(learning, [dict(ITEM, text='其它端点偏好')], fp=other_fp, project='', cfg=other_cfg)
    legacy = rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp, project_id=project)
    assert len(legacy) == 1 and legacy[0]['project_id'] == project
    snapshot = rules.learning_rules_snapshot(learning.root, principal='local', endpoint_fp=fp, project_id=project)
    assert len(snapshot) == 2 and {str(r.get('project_id') or '') for r in snapshot} == {'', project}
    global_rule = next(r for r in snapshot if not r.get('project_id'))
    row = rules.save_learning_candidate(learning.root, dict(ITEM, trigger='仅本项目'), principal='local', endpoint_fp=fp,
        project_id=project, sources=[ev], existing=snapshot, relation=None, permanence='persistent')['candidate']
    assessed = gate_assess(learning, fp, row, project=project, kind='overlap_incomparable', ids=[global_rule['id']], refs=[sid])
    assert assessed['ok'], assessed
    # 目录代际被推进（新全局约定生效）→ 旧评估拒绝提交，且零写入。
    approve_commit(learning, [dict(ITEM, text='新全局约定')], fp=fp, project='')
    before = knowledge_digest(learning)
    denied = review(learning, fp, fresh_candidate(learning, fp, row, project=project), human_confirmed=True)
    assert denied['error_code'] == 'knowledge_assessment_stale', "目录代际推进后旧评估拒绝提交"
    assert knowledge_digest(learning) == before
    # 与仍生效的继承规则不相容，不从关系推导撤回操作。
    snapshot = rules.learning_rules_snapshot(learning.root, principal='local', endpoint_fp=fp, project_id=project)
    row = rules.save_learning_candidate(learning.root, dict(ITEM, text='新项目要求'), principal='local', endpoint_fp=fp,
        project_id=project, sources=[ev], existing=snapshot, relation=None, permanence='persistent')['candidate']
    assessed = gate_assess(learning, fp, row, project=project, kind='disjoint',
        ids=[global_rule['id']], refs=[sid])
    assert not assessed['ok']
    assert assessed['error_code'] == 'knowledge_relation_blocked'
    before = knowledge_digest(learning)
    denied = review(learning, fp, fresh_candidate(learning, fp, row, project=project), 'replace', human_confirmed=True)
    assert denied['error_code'] == 'knowledge_not_committable'
    assert knowledge_digest(learning) == before
    # 同项目条目：显式 [新增+撤回] 原子替代生效；全局条目不受影响。
    own_rule = next(r for r in snapshot if r.get('project_id') == project)
    service = _knowledge(learning)
    replacement = service.propose('local', fp, project, request_id='project-replace-1', operations=[
        {'op': 'create', 'after': {'kind': 'memory', 'title': '', 'text': '明确的新项目约定', 'trigger': '',
            'tier': 'explicit', 'permanence': 'persistent', 'payload': {}, 'links': []}},
        {'op': 'retract', 'entry_id': own_rule['id'], 'expected_entry_revision': own_rule['revision']}],
        source_refs=[{'type': 'learning', 'id': sid}])
    assessed = service.assess('local', fp, project, change_id=replacement['change_id'],
        expected_change_revision=replacement['change_revision'], cfg=CFG,
        predictor=lambda req: _known(req))
    assert assessed['ok'], assessed
    gate_commit(learning, fp, assessed, project=project)
    assert next(r for r in rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp, project_id='') if r['id'] == global_rule['id'])['status'] == 'active'
    assert next(r for r in rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp, project_id=project) if r['id'] == own_rule['id'])['status'] == 'retracted'


def test_service_memory_tool_preserves_global_scope_for_real_decider(learning):
    from dataset_recommender.agent.memory_relations import assess_relations
    project = learning.workspace.create_project('local', '项目')['id']
    fp, sid, ev = source(learning, project=project)
    approve_commit(learning, [dict(ITEM, text='全局约定')], fp=fp, project='')
    approve_commit(learning, [dict(ITEM, text='项目约定')], fp=fp, project=project)
    global_id = rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp, project_id='')[0]['id']
    project_id = rules.list_collab_rules(learning.root, principal='local', endpoint_fp=fp, project_id=project)[0]['id']
    observed, done = [], threading.Event()
    def loop(**kwargs):
        read = kwargs['read_evidence']()
        memories = kwargs['query_memory']()
        observed.extend(memories)
        assert {r['project_id'] for r in memories} == {'', project}
        item = dict(ITEM, trigger='仅本项目')
        def predict(request):
            assert global_id in request['context']['inherited_global_ids']
            assert 'replaceable_ids' not in request['context']
            assert project_id in {r['id'] for r in request['existing']}
            return _known(request, 'overlap_incomparable')
        evaluated = assess_relations(dict(item, principal='local', endpoint_fp=fp, project_id=project),
            memories, [ev], kwargs['cfg'], predictor=predict)
        assert evaluated['coverage']['complete'] and len(evaluated['semantic_pairs']) == len(memories)
        receipt = kwargs['save_candidate'](item, sources=[ev], existing=memories, relation=evaluated, permanence='persistent')
        finish_traces(kwargs, read)
        done.set()
        return {'ok': True, 'state': 'done', 'saved_candidate_ids': [receipt['candidate']['id']], 'activated_rules': 0}
    learning.loop = loop
    learning.control('local', CFG, project_id=project, enabled=True, threshold=1)
    assert done.wait(5)
    deadline = time.monotonic() + 5
    while learning._tasks and time.monotonic() < deadline:
        time.sleep(.02)
    # 循环只产草稿提案（真实关系评估已带入 projection）；目录指纹口径见统一知识道闸。
    assert observed and learning.status('local', fp, project)['jobs'][0]['status'] == 'done'
    draft = learning.candidates('local', fp, project)['items'][0]
    assert draft['status'] == 'draft' and draft['project_id'] == project
    assert draft['relation'] is None, "服务端评估前不展示客户端关系"


def test_release_invalidates_all_owner_endpoint_projects_but_keeps_consent_and_data(learning):
    from dataset_recommender.agent.agent_exec import _endpoint_fp_from_config
    project = learning.workspace.create_project('local', '项目')['id']
    fp, row = candidate(learning)
    other_cfg = replace(CFG, base_url='https://other-model.example.invalid/v1')
    other_fp = _endpoint_fp_from_config(other_cfg)
    for owner, endpoint, selected in [('local', fp, ''), ('local', fp, project), ('other-user', fp, ''), ('local', other_fp, '')]:
        learning.store.control(owner, endpoint, selected, enabled=True)
    learning.authorize('local', CFG, None)
    learning.authorize('local', CFG, None, project_id=project)
    learning.authorize('other-user', CFG, None)
    learning.authorize('local', other_cfg, None)
    before = knowledge_digest(learning)
    result = learning.release('local', fp, expected_identity='local')
    assert result['released'] and not result['configured']
    assert learning._config(('local', fp, '')) is None and learning._config(('local', fp, project)) is None
    assert learning._config(('other-user', fp, '')) is not None
    assert learning._config(('local', other_fp, '')) is not None
    assert learning.store.scope('local', fp)['enabled'] and learning.store.scope('local', fp, project)['enabled']
    assert knowledge_digest(learning) == before, "释放配置不得触碰知识数据"
    assert learning.candidates('local', fp)['items'][0]['id'] == row['id']
    learning.authorize('local', CFG, None)
    assert learning.status('local', fp)['configured']
    assert learning._config(('local', fp, ''))[4] == result['config_epoch']


def test_release_api_uses_trusted_identity_condition_and_never_loads_or_sends_key(learning):
    fp, _, _ = source(learning)
    for owner in ('owner-a', 'owner-b'):
        learning.store.control(owner, fp, enabled=True)
        learning.authorize(owner, CFG, None)
    shared = SimpleNamespace(store=learning.store.conversations, workspace=learning.workspace)
    from dataset_recommender.app import learning_service
    learning_service._SERVICES[(str(shared.store.path), str(shared.store.project_root))] = learning
    identity = {'owner': 'owner-a'}
    app = FastAPI()
    app.include_router(build_learning_router(service=lambda: shared, principal=lambda req: identity['owner'],
        endpoint=lambda *args: fp, require_origin=lambda req: None,
        configure=lambda *args: pytest.fail('release must not materialize credentials'), quota=lambda *args: pytest.fail('release has no model cost')))
    client = TestClient(app)
    body = {'action': 'release', 'expected_identity': 'owner-a', 'expected_config_epoch': 0,
            'base_url': CFG.base_url, 'model': CFG.model}
    identity['owner'] = 'owner-b'
    denied = client.post('/api/learning/control', json=body)
    assert denied.status_code == 409 and denied.headers['x-error-code'] == 'learning_identity_conflict'
    assert learning._config(('owner-a', fp, '')) and learning._config(('owner-b', fp, ''))
    identity['owner'] = 'owner-a'
    assert client.post('/api/learning/control', json={**body, 'api_key': 'must-not-send'}).status_code == 400
    released = client.post('/api/learning/control', json=body)
    assert released.status_code == 200 and released.json()['released']
    assert learning._config(('owner-a', fp, '')) is None and learning._config(('owner-b', fp, ''))
    assert learning.store.scope('owner-a', fp)['enabled']


def test_release_rejects_late_model_result_even_after_new_authorization(learning):
    fp, sid, evidence = source(learning)
    entered, resume = threading.Event(), threading.Event()
    observed = []
    def loop(**kwargs):
        kwargs['before_llm'](kwargs['cfg'])
        entered.set(); assert resume.wait(5)
        try:
            kwargs['save_candidate'](ITEM, sources=[evidence], existing=[], relation=relation(refs=[sid]), permanence='persistent')
        except RunError as exc:
            observed.append(exc.code)
            return {'ok': False, 'state': 'paused', 'error_code': exc.code}
        pytest.fail('old-epoch model result wrote a candidate')
    learning.loop = loop
    learning.control('local', CFG, enabled=True, threshold=1)
    assert entered.wait(5)
    learning.release('local', fp)
    learning.authorize('local', replace(CFG, api_key='new-synthetic-key'), None)
    assert learning.status('local', fp)['configured'], 'new authorization should be usable, but cannot revive old worker'
    resume.set()
    deadline = time.monotonic() + 5
    while learning._tasks and time.monotonic() < deadline:
        time.sleep(.02)
    assert observed == ['learning_config_released'] and not learning.candidates('local', fp)['items']
    assert learning.store.scope('local', fp)['enabled']
    assert learning.status('local', fp)['jobs'][0]['status'] == 'paused'


def test_ai_off_observed_by_backend_releases_all_project_snapshots(learning):
    fp, _, _ = source(learning)
    project = learning.workspace.create_project('local', '项目')['id']
    for selected in ('', project):
        learning.store.control('local', fp, selected, enabled=True)
        learning.authorize('local', CFG, None, project_id=selected)
    learning.authorize('local', replace(CFG, enable_llm=False), None, project_id=project)
    assert learning._config(('local', fp, '')) is None and learning._config(('local', fp, project)) is None
    assert learning.store.scope('local', fp)['enabled'] and learning.store.scope('local', fp, project)['enabled']


def test_cross_process_release_invalidates_peer_cache_and_late_save(learning, tmp_path):
    fp, _, _ = source(learning)
    learning.store.control('local', fp, enabled=True, threshold=1)
    learning.authorize('local', CFG, None)
    job = learning.store.schedule('local', fp, configured=True)
    ready, proceed, result = (tmp_path / name for name in ('peer-ready', 'peer-proceed', 'peer-result.json'))
    code = '''import sys,json,time
from pathlib import Path
from dataset_recommender.app.conversation_store import ConversationStore
from dataset_recommender.app.workspace_store import WorkspaceStore
from dataset_recommender.app.learning_service import LearningService
from dataset_recommender.app.conversation_runs import RunError
from dataset_recommender.agent import collab_rules
from dataset_recommender.llm.llm_client import LLMConfig
root,fp,jid,ready,proceed,result=sys.argv[1:]
app=LearningService(ConversationStore(Path(root)),WorkspaceStore(Path(root)))
cfg=LLMConfig(enable_llm=True,api_key='synthetic-peer-key',base_url='https://model.example.test/v1',model='test')
app.authorize('local',cfg,None)
with app.store.conversations.database() as conn:
 job=dict(conn.execute('SELECT * FROM learning_jobs WHERE owner=? AND id=?',('local',jid)).fetchone())
Path(ready).write_text('ready')
deadline=time.monotonic()+10
while not Path(proceed).exists() and time.monotonic()<deadline: time.sleep(.02)
state={'configured':app._config(('local',fp,'')) is not None}
try:
 with app.store.conversations.database(write=True) as conn:
  app.store.check(job,conn=conn)
  raise AssertionError('old epoch admitted a late write')
except RunError as exc: state['code']=exc.code
Path(result).write_text(json.dumps(state))
app.close()
'''
    process = subprocess.Popen([sys.executable, '-B', '-c', code, str(learning.root), fp, job['id'], str(ready), str(proceed), str(result)], env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        learning.release('local', fp)
        proceed.write_text('released', encoding='utf-8')
        assert process.wait(timeout=10) == 0
        state = json.loads(result.read_text(encoding='utf-8'))
        assert state == {'configured': False, 'code': 'learning_config_released'}
        assert not learning.candidates('local', fp)['items']
    finally:
        if process.poll() is None:
            process.terminate(); process.wait(timeout=5)


def test_old_scope_epoch_cache_cannot_reappear_after_control_or_release(learning):
    fp, _, _ = source(learning)
    learning.store.control('local', fp, enabled=True)
    learning.authorize('local', CFG, None)
    old = learning._config(('local', fp, ''))
    learning.release('local', fp)
    learning._configs[('local', fp, '')] = old
    assert learning._config(('local', fp, '')) is None
    learning.authorize('local', CFG, None)
    assert learning._config(('local', fp, '')) is not None
    learning.store.control('local', fp, enabled=False)
    assert learning._config(('local', fp, '')) is None


def test_mcp_release_is_same_service_and_can_select_old_endpoint_without_owner_override(learning, monkeypatch):
    import asyncio
    from mcp.server.fastmcp import FastMCP
    from dataset_recommender.app import mcp_agent_tools, llm_admission, learning_service
    fp, _, _ = source(learning)
    for owner in ('local', 'other-user'):
        learning.store.control(owner, fp, enabled=True)
        learning.authorize(owner, CFG, None)
    shared = SimpleNamespace(store=learning.store.conversations, workspace=learning.workspace)
    monkeypatch.setattr(llm_admission, 'materialize', lambda **kwargs: replace(CFG, base_url='https://new.example.invalid/v1', model='new-model'))
    monkeypatch.setattr(mcp_agent_tools, 'service_for', lambda root: shared)
    learning_service._SERVICES[(str(shared.store.path), str(shared.store.project_root))] = learning
    mcp = FastMCP('release-test')
    mcp_agent_tools.register_agent_tools(mcp, root=lambda: learning.root)
    async def invoke():
        with mcp_agent_tools.bind_identity('local'):
            response = await mcp.call_tool('agent_learning', {'action': 'release', 'base_url': CFG.base_url, 'model': CFG.model})
            body = json.loads(response[0].text)
            assert body['released'] and body['endpoint_fp'] == fp
            tool = next(t for t in await mcp.list_tools() if t.name == 'agent_learning')
            assert not {'owner', 'confirmed', 'human_confirmed', 'api_key'} & set(tool.inputSchema['properties'])
    asyncio.run(invoke())
    assert learning._config(('local', fp, '')) is None and learning._config(('other-user', fp, ''))
    assert learning.store.scope('local', fp)['enabled']


def wait_learning(app):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with app._lock:
            if not app._tasks:
                return
        time.sleep(.01)
    pytest.fail('learning executor did not retire within the test deadline')


def learning_client(app, *, identity='local', configure=None):
    shared = SimpleNamespace(store=app.store.conversations, workspace=app.workspace)
    from dataset_recommender.app import learning_service
    from dataset_recommender.agent.agent_exec import _endpoint_fp_from_config
    learning_service._SERVICES[(str(shared.store.path), str(shared.store.project_root))] = app
    web = FastAPI()
    web.include_router(build_learning_router(service=lambda: shared,
        principal=identity if callable(identity) else lambda req: identity,
        endpoint=lambda *args: _endpoint_fp_from_config(CFG), require_origin=lambda req: None,
        configure=configure or (lambda *args: CFG), quota=lambda *args: None))
    return TestClient(web)


@pytest.mark.parametrize('operation', ['save', 'review'])
def test_unified_gate_never_waits_on_the_legacy_ledger_lock(learning, tmp_path, monkeypatch, operation):
    """旧「账本锁等待不占主 SQLite 写者」钉的新形态：迁移一次性持锁之后，统一道闸的
    提案/评估/提交只走 SQLite 短事务，**绝不触碰** learning-ledgers.lock——外部进程
    持有该锁时知识写入与主对话写都照常完成（旧 save/review 的锁等待路径已退役）。"""
    from dataset_recommender.corpus import fs_utils
    from dataset_recommender.app.runtime_paths import instance_data_dir_for
    fp, row = candidate(learning)   # 迁移已在此时一次性完成（唯一持锁点）
    _, sid, evidence = source(learning)
    ready, proceed = tmp_path / 'lock-ready', tmp_path / 'lock-release'
    lock = instance_data_dir_for(learning.root, '.userdata') / 'learning-ledgers.lock'
    code = '''import sys,time
from pathlib import Path
from dataset_recommender.corpus.fs_utils import file_critical_section
with file_critical_section(Path(sys.argv[1])):
 Path(sys.argv[2]).write_text('held')
 deadline=time.monotonic()+15
 while not Path(sys.argv[3]).exists() and time.monotonic()<deadline: time.sleep(.01)
'''
    process = subprocess.Popen([sys.executable, '-B', '-c', code, str(lock), str(ready), str(proceed)], env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    acquire = fs_utils.acquire_file_lock
    def guard(path, **kwargs):
        from pathlib import Path as _P
        if _P(str(path)).name == 'learning-ledgers.lock':
            raise AssertionError('统一道闸写路径不得再持旧账本锁')
        return acquire(path, **kwargs)
    monkeypatch.setattr(fs_utils, 'acquire_file_lock', guard)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), '外部进程应已持有旧账本锁'
        if operation == 'review':
            assessed = gate_assess(learning, fp, row)
            assert assessed['ok'], assessed
            assert review(learning, fp, fresh_candidate(learning, fp, row), human_confirmed=True)['ok']
        else:
            def loop(**kwargs):
                read = kwargs['read_evidence']()
                receipt = kwargs['save_candidate'](dict(ITEM, text='新的持久偏好'), sources=[evidence], existing=[], relation=None, permanence='persistent')
                finish_traces(kwargs, read)
                return {'ok': True, 'saved_candidate_ids': [receipt['candidate']['id']]}
            learning.loop = loop
            learning.control('local', CFG, enabled=True, threshold=1)
            wait_learning(learning)
            assert any(r['text'] == '新的持久偏好' for r in learning.candidates('local', fp)['items'])
        created = learning.store.conversations.create('local', title='持锁期间的主对话')
        assert created['id'], '主对话 SQLite 写不被旧账本锁影响'
    finally:
        proceed.write_text('release', encoding='utf-8')
        assert process.wait(timeout=10) == 0
        wait_learning(learning)


def test_notify_cannot_run_new_scope_job_with_old_authorization(learning, monkeypatch):
    fp, _, _ = source(learning)
    learning.store.control('local', fp, enabled=True, threshold=1)
    learning.authorize('local', CFG, None)
    original = learning.store.schedule
    def raced(*args, **kwargs):
        learning.store.control('local', fp, enabled=True, threshold=1)
        return original(*args, **kwargs)
    monkeypatch.setattr(learning.store, 'schedule', raced)
    calls = []
    learning.loop = lambda **kwargs: calls.append(kwargs['cfg']) or {'ok': True}
    learning.notify('local', fp)
    wait_learning(learning)
    assert not calls, 'epoch 1 配置不能执行 epoch 2 的任务'


def test_learning_budget_rejection_never_charges_shared_quota(learning):
    fp, _, _ = source(learning)
    for _ in range(40):
        learning.store.charge('local')
    charged = []
    def loop(**kwargs):
        kwargs['before_llm'](kwargs['cfg'])
        pytest.fail('no model request is admitted')
    learning.loop = loop
    learning.control('local', CFG, enabled=True, threshold=1, quota=lambda cfg: charged.append(cfg))
    wait_learning(learning)
    assert charged == []
    assert learning.status('local', fp)['jobs'][0]['result']['error_code'] == 'learning_budget_exceeded'


def test_shared_quota_rejection_rolls_back_learning_reservation(learning):
    def denied(cfg):
        raise RunError('llm_quota_exceeded', 'synthetic quota refusal')
    with pytest.raises(RunError) as exc:
        learning.store.charge('local', quota=lambda: denied(CFG))
    assert exc.value.code == 'llm_quota_exceeded'
    with learning.store.conversations.database() as conn:
        assert conn.execute('SELECT COALESCE(SUM(calls),0) FROM learning_usage').fetchone()[0] == 0


def test_learning_budget_multiprocess_reservation_and_rollback(learning, tmp_path):
    code = '''import sys
from pathlib import Path
from dataset_recommender.app.conversation_store import ConversationStore
from dataset_recommender.app.learning_store import LearningStore
from dataset_recommender.app.conversation_runs import RunError
store=LearningStore(ConversationStore(Path(sys.argv[1])))
for n in range(30):
 def quota():
  if n%3==0: raise RunError('llm_quota_exceeded','synthetic refusal')
  with store.conversations.database(write=True) as conn:
   conn.execute('UPDATE normal_test_usage SET calls=calls+1')
 try: store.charge('local',quota=quota)
 except RunError as exc: assert exc.code in {'learning_budget_exceeded','llm_quota_exceeded'},exc.code
'''
    with learning.store.conversations.database(write=True) as conn:
        conn.execute('CREATE TABLE normal_test_usage(calls INTEGER NOT NULL)')
        conn.execute('INSERT INTO normal_test_usage VALUES(0)')
    processes = [subprocess.Popen([sys.executable, '-B', '-c', code, str(learning.root)], env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1')) for _ in range(4)]
    assert all(p.wait(timeout=30) == 0 for p in processes)
    with learning.store.conversations.database() as conn:
        learning_calls = conn.execute('SELECT SUM(calls) FROM learning_usage').fetchone()[0]
        normal_calls = conn.execute('SELECT calls FROM normal_test_usage').fetchone()[0]
    assert learning_calls == normal_calls == 40


def test_capacity_waiter_wakes_when_paused_old_worker_retires(learning):
    fp, _, _ = source(learning)
    entered, release, next_entered = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def loop(**kwargs):
        calls.append(kwargs['cfg'])
        if len(calls) == 1:
            entered.set()
            assert release.wait(8)
            kwargs['check']()
        else:
            next_entered.set()
            evidence = kwargs['read_evidence']()
            finish_traces(kwargs, evidence)
        return {'ok': True}
    learning.loop = loop
    learning.control('local', CFG, enabled=True, threshold=1)
    assert entered.wait(5)
    learning.control('local', CFG, enabled=False)
    learning.control('local', CFG, enabled=True, threshold=1)
    release.set()
    assert next_entered.wait(5), '旧任务退场应唤醒已授权pending任务，无需新回合'
    wait_learning(learning)
    assert len(calls) == 2
    assert learning.status('local', fp)['jobs'][0]['status'] == 'done'


@pytest.mark.parametrize('route,extra', [('control', {'enabled': True}), ('trigger', {'feedback': '持久反馈', 'request_id': 'late'})])
def test_http_stale_epoch_and_identity_are_rejected_before_configuration(learning, route, extra):
    fp, _, _ = source(learning)
    learning.store.control('owner-a', fp, enabled=True)
    learning.authorize('owner-a', CFG, None)
    epoch = learning.store.config_epoch('owner-a', fp)
    learning.release('owner-a', fp)
    configured = []
    client = learning_client(learning, identity='owner-a', configure=lambda *args: configured.append(True) or CFG)
    body = dict(extra, expected_identity='owner-a', expected_config_epoch=epoch)
    before = learning.store.scope('owner-a', fp)
    response = client.post('/api/learning/' + route, json=body)
    assert response.status_code == 409 and response.headers['x-error-code'] == 'learning_config_conflict'
    assert not configured and learning.store.scope('owner-a', fp) == before
    assert learning._config(('owner-a', fp, '')) is None
    client = learning_client(learning, identity='owner-b', configure=lambda *args: configured.append(True) or CFG)
    response = client.post('/api/learning/' + route, json=dict(body, expected_config_epoch=0))
    assert response.status_code == 409 and response.headers['x-error-code'] == 'learning_identity_conflict'
    assert not configured and not learning.store.scope('owner-b', fp)['enabled']
    assert not learning.store.evidence('owner-b', fp)


@pytest.mark.parametrize('route,extra', [('control', {'enabled': True}), ('trigger', {'feedback': '反馈', 'request_id': 'in-flight'})])
def test_http_release_during_materialization_fails_second_cas(learning, route, extra):
    fp, _, _ = source(learning)
    learning.store.control('local', fp, enabled=True)
    epoch = learning.store.config_epoch('local', fp)
    def configure(*args):
        learning.release('local', fp)
        return CFG
    client = learning_client(learning, configure=configure)
    response = client.post('/api/learning/' + route, json=dict(extra, expected_identity='local', expected_config_epoch=epoch))
    assert response.status_code == 409 and response.headers['x-error-code'] == 'learning_config_conflict'
    assert learning._config(('local', fp, '')) is None
    assert learning.store.scope('local', fp)['epoch'] == 2
    assert len(learning.store.evidence('local', fp)) == 1


@pytest.mark.parametrize('route,extra', [('control', {'enabled': True}), ('trigger', {})])
def test_http_requires_identity_and_epoch_for_new_bindings(learning, route, extra):
    configured = []
    client = learning_client(learning, configure=lambda *args: configured.append(True) or CFG)
    for markers in ({}, {'expected_identity': 'local'}, {'expected_config_epoch': 0}):
        assert client.post('/api/learning/' + route, json=dict(extra, **markers)).status_code == 400
    for epoch in (-1, True, '0', 1.5):
        assert client.post('/api/learning/' + route, json=dict(extra, expected_identity='local', expected_config_epoch=epoch)).status_code == 422
    assert not configured
    assert client.get('/api/learning/status').json()['config_epoch'] == 0


@pytest.mark.parametrize('method', ['control', 'trigger', 'authorize'])
def test_trusted_service_captures_epoch_before_slow_work(learning, monkeypatch, method):
    fp, _, _ = source(learning)
    learning.store.control('local', fp, enabled=True)
    if method == 'authorize':
        from dataset_recommender.app import learning_service
        available = learning_service.should_use_llm
        def raced(cfg):
            learning.release('local', fp)
            return available(cfg)
        monkeypatch.setattr(learning_service, 'should_use_llm', raced)
    else:
        checked = learning.checked_project
        def raced(*args, **kwargs):
            learning.release('local', fp)
            return checked(*args, **kwargs)
        monkeypatch.setattr(learning, 'checked_project', raced)
    with pytest.raises(RunError) as exc:
        if method == 'control':
            learning.control('local', CFG, enabled=True)
        elif method == 'trigger':
            learning.trigger('local', CFG, feedback='迟到反馈', request_id='late')
        else:
            learning.authorize('local', CFG, None)
    assert exc.value.code == 'learning_config_conflict'
    assert learning._config(('local', fp, '')) is None
    assert len(learning.store.evidence('local', fp)) == 1


@pytest.mark.parametrize('budget', ['learning', 'shared'])
def test_real_shared_quota_admission_preserves_no_call_accounting(learning, tmp_path, monkeypatch, budget):
    from dataset_recommender.app import llm_admission, llm_quota
    quota_path = tmp_path / 'normal-quota.json'
    monkeypatch.setenv('BIODATA_LLM_QUOTA_FILE', str(quota_path))
    monkeypatch.setenv('BIODATA_LLM_DAILY_PER_USER', '1')
    monkeypatch.setenv('BIODATA_LLM_QUOTA_EXEMPT', '')
    if budget == 'learning':
        for _ in range(40):
            learning.store.charge('local')
    else:
        llm_quota.check_and_increment(learning.root, 'local', trial=False, per_user_limit=1, global_limit=100)
    def admission(cfg):
        learning.store.charge('local', quota=lambda: llm_admission.gate_quota(learning.root,
            username='local', cfg=cfg, api_key=None, account_required=True))
    result = run_learning_loop(cfg=CFG, read_evidence=lambda **kwargs: [], query_memory=lambda: [],
        save_candidate=lambda *args, **kwargs: pytest.fail('no candidate'), verify_candidate=lambda cid: None,
        before_llm=admission, check=lambda: None, on_trace=lambda entry: None,
        llm_call=lambda *args: pytest.fail('no model invocation'))
    assert not result['ok'] and result['calls'] == 0
    assert result['error_code'] == ('learning_budget_exceeded' if budget == 'learning' else 'llm_quota_exceeded')
    assert llm_quota.usage_snapshot(learning.root, 'local', trial=False) == (0 if budget == 'learning' else 1)
    with learning.store.conversations.database() as conn:
        assert conn.execute('SELECT COALESCE(SUM(calls),0) FROM learning_usage').fetchone()[0] == (40 if budget == 'learning' else 0)


@pytest.mark.parametrize('method', ['control', 'trigger', 'authorize'])
def test_cross_process_old_control_trigger_and_authorize_cannot_restore_key(learning, tmp_path, method):
    fp, _, _ = source(learning)
    learning.store.control('local', fp, enabled=True)
    ready, proceed, result = (tmp_path / name for name in ('request-ready', 'request-proceed', 'request-result.json'))
    code = '''import sys,json,time
from pathlib import Path
from dataset_recommender.app.conversation_store import ConversationStore
from dataset_recommender.app.workspace_store import WorkspaceStore
from dataset_recommender.app.learning_service import LearningService
from dataset_recommender.app.conversation_runs import RunError
from dataset_recommender.llm.llm_client import LLMConfig
root,fp,method,ready,proceed,result=sys.argv[1:]
app=LearningService(ConversationStore(Path(root)),WorkspaceStore(Path(root)))
cfg=LLMConfig(enable_llm=True,api_key='synthetic-old-key',base_url='https://model.example.test/v1',model='test')
epoch=app.store.config_epoch('local',fp)
Path(ready).write_text('captured')
deadline=time.monotonic()+10
while not Path(proceed).exists() and time.monotonic()<deadline: time.sleep(.01)
state={}
try:
 if method=='control': app.control('local',cfg,enabled=True,expected_config_epoch=epoch)
 elif method=='trigger': app.trigger('local',cfg,feedback='late feedback',request_id='late',expected_config_epoch=epoch)
 else: app.authorize('local',cfg,None,expected_config_epoch=epoch)
except RunError as exc: state['code']=exc.code
state['configured']=app._config(('local',fp,'')) is not None
Path(result).write_text(json.dumps(state))
app.close()
'''
    process = subprocess.Popen([sys.executable, '-B', '-c', code, str(learning.root), fp, method, str(ready), str(proceed), str(result)], env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        learning.release('local', fp)
        proceed.write_text('released', encoding='utf-8')
        assert process.wait(timeout=15) == 0
        assert json.loads(result.read_text(encoding='utf-8')) == {'code': 'learning_config_conflict', 'configured': False}
        assert learning.store.scope('local', fp)['epoch'] == 2
        assert len(learning.store.evidence('local', fp)) == 1
    finally:
        if process.poll() is None:
            process.terminate(); process.wait(timeout=5)


@pytest.mark.parametrize('route,extra', [('control', {'enabled': True}), ('trigger', {'feedback': '迟到反馈', 'request_id': 'identity-switch'})])
def test_http_identity_changes_during_configuration_write_nothing(learning, route, extra):
    fp, _, _ = source(learning)
    for owner in ('owner-a', 'owner-b'):
        learning.store.control(owner, fp, enabled=True)
    identity = {'owner': 'owner-a'}
    def configure(*args):
        identity['owner'] = 'owner-b'
        return CFG
    client = learning_client(learning, identity=lambda req: identity['owner'], configure=configure)
    response = client.post('/api/learning/' + route, json=dict(extra, expected_identity='owner-a', expected_config_epoch=0))
    assert response.status_code == 409 and response.headers['x-error-code'] == 'learning_identity_conflict'
    for owner in ('owner-a', 'owner-b'):
        assert learning.store.scope(owner, fp)['epoch'] == 1
        assert learning._config((owner, fp, '')) is None
        assert not learning.store.evidence(owner, fp)


def test_http_current_conditions_enable_trigger_and_release_end_to_end(learning):
    calls = []
    def loop(**kwargs):
        kwargs['before_llm'](kwargs['cfg'])
        calls.append(kwargs['cfg'])
        finish_traces(kwargs, kwargs['read_evidence']())
        return {'ok': True}
    learning.loop = loop
    client = learning_client(learning)
    status = client.get('/api/learning/status').json()
    body = {'expected_identity': status['scope']['owner'], 'expected_config_epoch': status['config_epoch']}
    enabled = client.post('/api/learning/control', json=dict(body, enabled=True))
    assert enabled.status_code == 200 and enabled.json()['configured']
    triggered = client.post('/api/learning/trigger', json=dict(body, feedback='明确的长期偏好', request_id='current'))
    assert triggered.status_code == 200
    wait_learning(learning)
    assert len(calls) == 1
    observed = client.get('/api/learning/status').json()
    assert observed['jobs'][0]['status'] == 'done'
    released = client.post('/api/learning/control', json=dict(body, action='release'))
    assert released.status_code == 200 and released.json()['config_epoch'] == body['expected_config_epoch'] + 1
    old = client.post('/api/learning/control', json=dict(body, enabled=True, api_key='synthetic-old-key'))
    assert old.status_code == 409 and old.headers['x-error-code'] == 'learning_config_conflict'


@pytest.mark.parametrize('invalidate', [False, True])
def test_pending_failure_is_not_retried_and_released_pending_is_not_woken(learning, invalidate):
    fp, _, _ = source(learning)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def loop(**kwargs):
        calls.append(kwargs['cfg'])
        if len(calls) == 1:
            entered.set()
            assert release.wait(8)
            kwargs['check']()
        return {'ok': False, 'error_code': 'learning_model_failed'}
    learning.loop = loop
    learning.control('local', CFG, enabled=True, threshold=1)
    assert entered.wait(5)
    learning.control('local', CFG, enabled=False)
    learning.control('local', CFG, enabled=True, threshold=1)
    assert learning._pending
    if invalidate:
        learning.release('local', fp)
        learning.authorize('local', CFG, None)
    release.set()
    wait_learning(learning)
    time.sleep(.1)
    assert len(calls) == (1 if invalidate else 2)
    assert not learning._pending


@pytest.mark.parametrize('route,extra', [('control', {'enabled': True}), ('trigger', {})])
def test_identity_conflict_does_not_even_initialize_cold_service(route, extra):
    app = FastAPI()
    def forbidden(*args):
        pytest.fail('identity drift must reject before service, endpoint or credential initialization')
    app.include_router(build_learning_router(service=forbidden, principal=lambda request: 'owner-b',
        endpoint=forbidden, require_origin=lambda request: None, configure=forbidden, quota=forbidden))
    response = TestClient(app).post('/api/learning/' + route,
        json=dict(extra, expected_identity='owner-a', expected_config_epoch=0))
    assert response.status_code == 409 and response.headers['x-error-code'] == 'learning_identity_conflict'


def test_original_authorization_expiry_is_not_revived_by_fresh_cache(learning):
    fp, _, _ = source(learning)
    learning.store.control('local', fp, enabled=True, threshold=1)
    learning.authorize('local', CFG, None)
    old = learning._config(('local', fp, ''))
    expired = (*old[:2], time.monotonic() - 1, *old[3:])
    learning.authorize('local', CFG, None)
    job = learning.store.schedule('local', fp)
    with pytest.raises(RunError) as failure:
        learning._check_authorization(job, expired)
    assert failure.value.code == 'learning_config_expired'
    assert learning._config(('local', fp, '')) is not None


def test_all_authorized_capacity_waiters_are_drained_once_without_overbooking(learning):
    fp, _, _ = source(learning)
    learning.capacity = 1
    entered, release = threading.Event(), threading.Event()
    calls = []
    def loop(**kwargs):
        calls.append(kwargs['read_evidence']()[0]['owner'])
        assert len(learning._tasks) <= 1
        if len(calls) == 1:
            entered.set()
            assert release.wait(8)
        return {'ok': True}
    learning.loop = loop
    learning.control('local', CFG, enabled=True, threshold=1)
    assert entered.wait(5)
    for owner in ('owner-b', 'owner-c'):
        learning.store.feedback(owner, fp, '', request_id='current', text='synthetic preference')
        learning.control(owner, CFG, enabled=True, threshold=1)
    assert len(learning._pending) == 2
    release.set()
    wait_learning(learning)
    assert calls == ['local', 'owner-b', 'owner-c']
    assert not learning._pending


@pytest.mark.parametrize('route,extra', [('control', {'enabled': True}), ('trigger', {'feedback': '迟到反馈', 'request_id': 'cookie-drift'})])
def test_real_account_cookie_drift_rejects_learning_before_key_materialization(learning, monkeypatch, route, extra):
    from dataset_recommender.app import accounts, webapp, learning_service
    from dataset_recommender.agent.agent_exec import _endpoint_fp_from_config
    fp = _endpoint_fp_from_config(CFG)
    monkeypatch.setattr(webapp, 'PROJECT_ROOT', learning.root)
    monkeypatch.setenv('BIODATA_REQUIRE_ACCOUNT', '1')
    monkeypatch.setenv('BIODATA_INVITE_CODE', 'synthetic-invite')
    monkeypatch.setenv('BIODATA_ACCOUNTS_FILE', str(learning.root / 'accounts.json'))
    monkeypatch.setenv('BIODATA_SESSIONS_FILE', str(learning.root / 'sessions.json'))
    monkeypatch.setattr(webapp, 'load_llm_config', lambda **kwargs: CFG)
    materialized = []
    monkeypatch.setattr(webapp, '_materialize_request_llm_config', lambda *args, **kwargs: materialized.append(True) or CFG)
    learning_service._SERVICES[(str(learning.store.path), str(learning.root))] = learning
    accounts._reset_state_for_tests()
    try:
        with TestClient(webapp.app, base_url='http://127.0.0.1') as client:
            def register(username):
                response = client.post('/api/account/register', json={'username': username,
                    'password': 'synthetic-password', 'invite_code': 'synthetic-invite'})
                assert response.status_code == 200
                return client.get('/api/account/whoami').json()['user']['id']
            owner_a = register('alice')
            learning.store.control(owner_a, fp, enabled=True)
            learning.authorize(owner_a, CFG, None)
            params = {'base_url': CFG.base_url, 'model': CFG.model, 'provider': CFG.provider}
            status = client.get('/api/learning/status', params=params).json()
            assert status['scope']['owner'] == owner_a and status['configured']
            owner_b = register('bob')
            response = client.post('/api/learning/' + route, json=dict(extra, **params,
                api_key='synthetic-old-key', expected_identity=owner_a, expected_config_epoch=status['config_epoch']))
            assert response.status_code == 409 and response.headers['x-error-code'] == 'learning_identity_conflict'
            assert not materialized
            assert not learning.store.scope(owner_b, fp)['enabled']
            assert not learning.store.evidence(owner_b, fp)
            assert learning._config((owner_b, fp, '')) is None
            assert learning._config((owner_a, fp, '')) is not None
    finally:
        accounts._reset_state_for_tests()


def _cross_process_learning_change(app, fp, mutation):
    code = '''import sys
from pathlib import Path
from dataset_recommender.app.conversation_store import ConversationStore
from dataset_recommender.app.learning_store import LearningStore
store=LearningStore(ConversationStore(Path(sys.argv[1])))
if sys.argv[3]=='release': store.release_configs('local',sys.argv[2])
else:
 store.control('local',sys.argv[2],enabled=False)
 store.control('local',sys.argv[2],enabled=True)
'''
    completed = subprocess.run([sys.executable, '-B', '-c', code, str(app.root), fp, mutation],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'), capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize('action', ['control', 'trigger'])
@pytest.mark.parametrize('mutation', ['release', 'scope'])
def test_real_mcp_learning_snapshot_precedes_configuration_materialization(learning, monkeypatch, action, mutation):
    import asyncio
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from dataset_recommender.app import mcp_agent_tools, learning_service, llm_admission
    from dataset_recommender.agent.agent_exec import _endpoint_fp_from_config
    fp = _endpoint_fp_from_config(CFG)
    learning.store.control('local', fp, enabled=True)
    shared = ConversationService(learning.root)
    monkeypatch.setattr(mcp_agent_tools, 'service_for', lambda root: shared)
    monkeypatch.setitem(learning_service._SERVICES, (str(shared.store.path), str(learning.root)), learning)
    configurations = []
    def materialize(**kwargs):
        configurations.append(True)
        _cross_process_learning_change(learning, fp, mutation)
        return CFG
    monkeypatch.setattr(llm_admission, 'materialize', materialize)
    learning.loop = lambda **kwargs: pytest.fail('stale configuration must not call the learning model')
    mcp = FastMCP('learning-snapshot')
    mcp_agent_tools.register_agent_tools(mcp, root=lambda: learning.root)
    async def protocol():
        with mcp_agent_tools.bind_identity('local'):
            with pytest.raises(ToolError, match='learning_config_conflict'):
                await mcp.call_tool('agent_learning', {'action': action, 'enabled': True,
                    'feedback': 'late preference', 'request_id': 'stale-materialization'})
    try:
        asyncio.run(protocol())
        assert configurations == [True]
        assert learning._config(('local', fp, '')) is None
        assert not learning.store.evidence('local', fp)
        assert learning.store.scope('local', fp)['epoch'] == (2 if mutation == 'release' else 3)
    finally:
        shared.runtime.close()


@pytest.mark.parametrize('mutation', ['release', 'scope', 'snapshot-failure'])
def test_shared_chat_does_not_reauthorize_stale_key_or_fail_main_turn(learning, monkeypatch, mutation):
    from dataset_recommender.app import conversation_service, learning_service
    from dataset_recommender.agent.agent_exec import _endpoint_fp_from_config
    fp = _endpoint_fp_from_config(CFG)
    learning.store.control('local', fp, enabled=True)
    shared = ConversationService(learning.root)
    monkeypatch.setitem(learning_service._SERVICES, (str(shared.store.path), str(learning.root)), learning)
    if mutation == 'snapshot-failure':
        def unavailable(*args, **kwargs):
            raise RunError('learning_store_unavailable', 'synthetic snapshot failure')
        monkeypatch.setattr(learning.store, 'authorization_snapshot', unavailable, raising=False)
    def configure():
        if mutation != 'snapshot-failure':
            _cross_process_learning_change(learning, fp, mutation)
        return CFG
    monkeypatch.setattr(conversation_service, 'route_with_project', lambda *args, **kwargs: {
        'route': 'none', 'query': '', 'plan': None, 'echo_zh': '主回合仍正常完成', 'retrieval': None, 'via': 'rule_direct'})
    try:
        run = shared.chat('local', '普通共享对话', request_id='snapshot-main', configure=configure, use_agent=False)
        shared.runtime.wait_result('local', run['id'], timeout=10)
        final = shared.store.load('local', run['id'])
        assert final['status'] == 'done' and final['result']['echo_zh'] == '主回合仍正常完成'
        assert learning._config(('local', fp, '')) is None
        assert final['request']['learning_authorization']['configured'] is False
        assert final['request']['learning_authorization']['error_code'] == (
            'learning_store_unavailable' if mutation == 'snapshot-failure' else 'learning_config_conflict')
        assert len(learning.store.evidence('local', fp)) == 1
        assert not learning.status('local', fp)['jobs']
    finally:
        shared.runtime.close()


def test_owner_authorization_snapshot_is_one_read_transaction(learning, monkeypatch):
    from contextlib import contextmanager
    fp, _, _ = source(learning)
    learning.store.control('local', fp, enabled=True)
    project = learning.workspace.create_project('local', 'project')['id']
    learning.store.control('local', fp, project, enabled=True)
    learning.store.control('other', fp, enabled=True)
    learning.store.release_configs('local', fp)
    reads = []
    database = learning.store.conversations.database
    @contextmanager
    def observed(*args, **kwargs):
        with database(*args, **kwargs) as conn:
            reads.append(kwargs.get('write', False))
            yield conn
    monkeypatch.setattr(learning.store.conversations, 'database', observed)
    snapshot = learning.store.authorization_snapshot('local')
    assert reads == [False]
    assert snapshot['config_epochs'] == {fp: 1}
    assert snapshot['scope_epochs'] == {(fp, ''): 2, (fp, project): 2}


@pytest.mark.parametrize('operation', ['control', 'authorize', 'snapshot', 'authorization'])
def test_fresh_learning_authorization_joins_migration_transaction(learning, monkeypatch, operation):
    from contextlib import contextmanager
    from dataset_recommender.agent.agent_exec import _endpoint_fp_from_config
    fp = _endpoint_fp_from_config(CFG)
    assert not learning.store.path.exists()
    transactions = []
    database = learning.store.conversations.database
    @contextmanager
    def observed(*args, **kwargs):
        with database(*args, **kwargs) as conn:
            transactions.append(conn.in_transaction)
            yield conn
    monkeypatch.setattr(learning.store.conversations, 'database', observed)
    if operation == 'control':
        result = learning.control('local', CFG, enabled=True)
        assert result['scope']['enabled'] and result['configured']
    elif operation == 'authorize':
        assert learning.authorize('local', CFG, None) == fp
        assert not learning._configs
    elif operation == 'snapshot':
        assert learning.store.authorization_snapshot('local') == {'config_epochs': {}, 'scope_epochs': {}}
    else:
        scope, epoch = learning.store.authorization('local', fp)
        assert not scope['enabled'] and scope['epoch'] == epoch == 0
    assert transactions[0], '首次 message_boundary 迁移已持有事务，授权不能再次 BEGIN'
    with database() as conn:
        assert 'message_boundary' in {row['name'] for row in conn.execute('PRAGMA table_info(shared_frames)')}
    assert learning._pool is None


@pytest.mark.parametrize('operation', ['snapshot', 'authorization'])
def test_authorization_reads_keep_both_epochs_before_concurrent_release(learning, monkeypatch, operation):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    fp, _, _ = source(learning)
    learning.store.control('local', fp, enabled=True)
    peer = LearningStore(ConversationStore(learning.root))
    staged = threading.Event()
    peer_database = peer.conversations.database
    @contextmanager
    def observed_write(*args, **kwargs):
        with peer_database(*args, **kwargs) as conn:
            yield conn
            if kwargs.get('write'):
                staged.set()
    monkeypatch.setattr(peer.conversations, 'database', observed_write)
    method = 'config_epochs' if operation == 'snapshot' else 'config_epoch'
    capture = getattr(learning.store, method)
    releases = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        def release_between_reads(*args, **kwargs):
            result = capture(*args, **kwargs)
            assert kwargs['conn'].in_transaction
            releases.append(pool.submit(peer.release_configs, 'local', fp))
            assert staged.wait(5)
            assert not releases[0].done(), '读快照释放前，并发配置与范围代际更新不能提交'
            return result
        monkeypatch.setattr(learning.store, method, release_between_reads)
        if operation == 'snapshot':
            snapshot = learning.store.authorization_snapshot('local')
            assert snapshot == {'config_epochs': {}, 'scope_epochs': {(fp, ''): 1}}
        else:
            scope, epoch = learning.store.authorization('local', fp)
            assert epoch == 0 and scope['epoch'] == 1
        assert [task.result(timeout=5) for task in releases] == [1]
    assert peer.config_epoch('local', fp) == 1 and peer.scope('local', fp)['epoch'] == 2


@pytest.mark.parametrize('operation', ['control', 'authorize', 'snapshot'])
def test_cold_authorization_read_error_propagates_without_config_cache(learning, monkeypatch, operation):
    import sqlite3
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic authorization read failure')
    monkeypatch.setattr(learning.store, 'config_epochs' if operation == 'snapshot' else 'config_epoch', unavailable)
    with pytest.raises(sqlite3.OperationalError, match='synthetic authorization read failure'):
        if operation == 'control':
            learning.control('local', CFG, enabled=True)
        elif operation == 'authorize':
            learning.authorize('local', CFG, None)
        else:
            learning.store.authorization_snapshot('local')
    assert not learning._configs and learning._pool is None


@pytest.mark.parametrize('fault', ['lock_oserror', 'queued_sqlite', 'all_status_sqlite', 'file_lock_busy'])
def test_pending_enqueue_failure_keeps_waiter_and_processes_next_once(learning, monkeypatch, fault):
    import sqlite3
    from dataset_recommender.corpus import fs_utils
    fp, _, _ = source(learning)
    learning.capacity = 1
    entered, retire, c_entered, c_retire = (threading.Event() for _ in range(4))
    calls, attempts = [], []
    failed = {'enabled': True}
    def loop(**kwargs):
        def model(prompt, cfg):
            history = json.loads(prompt.split('真实观察（JSON 数据）：\n', 1)[1])
            if not history:
                return json.dumps({'tool': 'read_evidence', 'args': {}})
            if history[-1]['action']['tool'] == 'read_evidence':
                owner = history[-1]['observation']['evidence'][0]['owner']
                calls.append(owner)
                assert len(learning._tasks) <= learning.capacity
                if owner == 'local':
                    entered.set()
                    assert retire.wait(8)
                elif owner == 'owner-c':
                    c_entered.set()
                    assert c_retire.wait(8)
                return json.dumps({'tool': 'query_memory', 'args': {}})
            return json.dumps({'tool': 'finish', 'args': {}})
        return run_learning_loop(**kwargs, llm_call=model)
    learning.loop = loop
    try:
        learning.control('local', CFG, enabled=True, threshold=1)
        assert entered.wait(5)
        with learning._lock:
            first = next(iter(learning._tasks.values()))
        for owner in ('owner-b', 'owner-c'):
            learning.store.feedback(owner, fp, '', request_id='current', text='synthetic persistent preference')
            learning.control(owner, CFG, enabled=True, threshold=1)
        key = ('owner-b', fp, '')
        job = learning._pending[key][0]
        assert len(learning._pending) == 2
        acquire, set_status = fs_utils.acquire_file_lock, learning.store.set_status
        def flaky_lock(path, **kwargs):
            if path.name == job['id'] + '.lock' and failed['enabled'] and fault in {'lock_oserror', 'file_lock_busy'}:
                attempts.append('lock')
                if fault == 'file_lock_busy':
                    raise fs_utils.FileLockBusy('synthetic learning lease contention')
                raise OSError('synthetic learning lease failure')
            return acquire(path, **kwargs)
        def flaky_status(selected, status, result=None):
            if selected['id'] == job['id'] and failed['enabled'] and (
                fault == 'all_status_sqlite' or fault == 'queued_sqlite' and status == 'queued'):
                attempts.append(status)
                raise sqlite3.OperationalError('synthetic learning status failure')
            return set_status(selected, status, result)
        monkeypatch.setattr(fs_utils, 'acquire_file_lock', flaky_lock)
        monkeypatch.setattr(learning.store, 'set_status', flaky_status)
        retire.set()
        assert c_entered.wait(5), 'B 的入队故障不能中断未尝试的 C'
        assert first.result(timeout=5) is None
        with learning._lock:
            assert learning._pending[key][0]['id'] == job['id']
            assert len(learning._tasks) == 1
        before = list(attempts)
        time.sleep(.05)
        assert attempts == before and before, '无新事件时不得紧循环重试暂失败项'
        assert calls == ['local', 'owner-c']
        failed['enabled'] = False
        c_retire.set()
        wait_learning(learning)
        assert calls == ['local', 'owner-c', 'owner-b']
        assert not learning._pending
        assert learning.status('owner-b', fp)['jobs'][0]['status'] == 'done'
        with learning.store.conversations.database() as conn:
            assert conn.execute('SELECT SUM(calls) FROM learning_usage').fetchone()[0] == 9
    finally:
        failed['enabled'] = False
        retire.set()
        c_retire.set()
        learning.close()
