"""真实ROOT知识核、学习器与来源事务的联合回归；仅模型响应使用离线接缝。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from dataset_recommender.agent import learning_loop
from dataset_recommender.agent.agent_exec import _endpoint_fp_from_config
from dataset_recommender.app import knowledge_service, learning_service, learning_store
from dataset_recommender.app.conversation_store import ConversationStore
from dataset_recommender.app.conversation_runs import RunError
from dataset_recommender.app.learning_api import LearningRequest
from dataset_recommender.app.learning_service import LearningService
from dataset_recommender.app.workspace_store import WorkspaceStore
from dataset_recommender.llm.llm_client import LLMConfig, LLMResult

ROOT = Path(__file__).resolve().parents[1]
CFG = LLMConfig(enable_llm=True, api_key='synthetic-offline-key', base_url='https://model.example/v1', model='test')
FP = _endpoint_fp_from_config(CFG)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for module in (learning_loop, knowledge_service, learning_service, learning_store):
        assert Path(module.__file__).resolve().is_relative_to(ROOT / 'src')
    monkeypatch.setenv('BIODATA_RUNS_DB', str(tmp_path / 'runs.sqlite'))
    monkeypatch.setenv('BIODATA_WORKSPACE_DB', str(tmp_path / 'workspace.sqlite'))
    conversations = ConversationStore(tmp_path)
    workspace = WorkspaceStore(tmp_path)
    conversations.workspace = workspace
    app = LearningService(conversations, workspace)
    state = {'mode': 'empty', 'limit': 12, 'calls': 0, 'block': None}

    def model(prompt, cfg):
        state['calls'] += 1
        if '真实观察（JSON 数据）：\n' not in prompt:
            request = json.loads(prompt.split('事实 JSON：\n', 1)[1])
            from test_memory_relations import decision
            result = decision(request, 'overlap_incomparable')
        else:
            history = json.loads(prompt.split('真实观察（JSON 数据）：\n', 1)[1])
            previous = history[-1]['action']['tool'] if history else ''
            if not previous:
                result = {'tool': 'query_memory', 'args': {}}
            elif previous == 'query_memory':
                result = {'tool': 'read_evidence', 'args': {'limit': state['limit']}}
            elif previous == 'read_evidence':
                if state['block']:
                    entered, release = state['block']
                    entered.set()
                    assert release.wait(8)
                if state['mode'] == 'model_fail':
                    raise TimeoutError('synthetic model failure')
                if state['mode'] == 'forged_finish':
                    result = {'tool': 'finish', 'args': {'processed_source_ids': ['invented']}}
                elif state['mode'] in {'learn', 'invalid_propose', 'unassessed', 'refs_implicit', 'refs_explicit'}:
                    after = {'kind': 'memory', 'title': '汇报习惯', 'text': '汇报先给结论。', 'permanence': 'persistent'}
                    if state['mode'] == 'invalid_propose':
                        after['payload'] = None
                    args = {'operations': [{'op': 'create', 'after': after}]}
                    if state['mode'] == 'refs_explicit':
                        args['evidence_ids'] = [row['id'] for row in history[-1]['observation']['evidence'][:8]]
                    result = {'tool': 'propose', 'args': args}
                else:
                    result = {'tool': 'finish', 'args': {}}
            elif previous == 'propose' and history[-1]['observation']['ok'] and state['mode'] in {'learn', 'refs_explicit'}:
                result = {'tool': 'assess_change', 'args': {'change_id': history[-1]['observation']['change_id']}}
            elif previous == 'assess_change' and history[-1]['observation']['ok']:
                result = {'tool': 'verify_change', 'args': {'change_id': history[-1]['action']['args']['change_id']}}
            else:
                result = {'tool': 'finish', 'args': {}}
        return LLMResult(text=json.dumps(result, ensure_ascii=False), attempted=True, succeeded=True,
            response_used=False, provider='openai-compatible', model=cfg.model, finish_reason='stop')

    monkeypatch.setattr(learning_loop, 'call_llm', model)
    monkeypatch.setattr(knowledge_service, 'call_llm', model)
    yield app, state
    if state['block']:
        state['block'][1].set()
    app.close()


def sources(app, count=1, project='', prefix='source'):
    return [app.store.feedback('local', FP, project, request_id=f'{prefix}-{index}',
        text=f'公开合成来源{prefix}-{index}，汇报先给结论。') for index in range(count)]


def trigger(app, ids, *, project='', request='request'):
    return app.trigger('local', CFG, project_id=project, source_ids=ids, manual=True,
        request_id=request, expected_identity='local', expected_config_epoch=app.store.config_epoch('local', FP))


def settled(app, job_id):
    deadline = time.monotonic() + 10
    while app._tasks and time.monotonic() < deadline:
        time.sleep(.01)
    assert not app._tasks
    return app.store.job_receipt('local', job_id)


@pytest.mark.parametrize('mode', ['invalid_propose', 'unassessed', 'model_fail', 'forged_finish', 'refs_implicit'])
def test_failed_or_unassessed_finish_never_consumes(env, mode):
    app, state = env
    state['mode'] = mode
    ids = sources(app, 12 if mode == 'refs_implicit' else 1)
    result = trigger(app, ids)
    job = settled(app, result['job_id'])
    assert job['status'] == 'failed', job
    assert job['handled_count'] == job['consumed_count'] == 0
    assert job['remaining_selected_count'] == len(ids)
    assert app.store.fresh_stats('local', FP)['total'] == len(ids)
    assert app.knowledge.entries('local', FP)['items'] == []
    if mode == 'unassessed':
        assert app.knowledge.changes('local', FP)['items'][0]['status'] == 'draft'
    if mode == 'refs_implicit':
        assert app.knowledge.changes('local', FP)['items'] == []


def test_true_gate_actor_assessment_and_no_automatic_activation(env):
    app, state = env
    state['mode'] = 'learn'
    result = trigger(app, sources(app))
    job = settled(app, result['job_id'])
    assert job['status'] == 'done'
    row = app.knowledge.changes('local', FP)['items'][0]
    assert row['actor'] == 'learning' and row['status'] == 'pending'
    assert row['assessment']['committable'] is True
    assert row['revision'] == row['assessment']['change_revision']
    assert app.knowledge.entries('local', FP)['items'] == []
    assert job['consumed_count'] == 1 and state['calls'] == 7


def test_explicit_eight_refs_from_twelve_are_not_silently_truncated(env):
    app, state = env
    state['mode'] = 'refs_explicit'
    ids = sources(app, 12)
    result = trigger(app, ids)
    job = settled(app, result['job_id'])
    assert job['status'] == 'done'
    change = app.knowledge.changes('local', FP)['items'][0]
    assert len(change['source_refs']) == 8
    assert {row['id'] for row in change['source_refs']} <= set(ids)
    assert job['selected_total'] == job['handled_count'] == 12


def test_thirteen_fixed_selection_continues_same_id_and_replay_does_not_run(env):
    app, state = env
    ids = sources(app, 13)
    first = trigger(app, ids, request='fixed-selection')
    first_job = settled(app, first['job_id'])
    assert first['selected_total'] == 13 and first_job['selected_total'] == 13
    assert first_job['status'] == 'partial' and first_job['handled_count'] == 12
    assert first_job['remaining_selected_count'] == 1
    extra = sources(app, prefix='new-unselected')
    second = trigger(app, ids, request='fixed-selection')
    assert second['job_id'] == first['job_id']
    final = settled(app, second['job_id'])
    assert final['status'] == 'done' and final['attempt'] == 1
    assert final['handled_count'] == final['consumed_count'] == 13
    assert app.store.fresh_stats('local', FP)['total'] == 1
    assert next(item for item in app.sources('local', FP)['items'] if item['id'] == extra[0])['fresh'] is True
    calls = state['calls']
    replay = trigger(app, ids, request='fixed-selection')
    assert replay['scheduled'] is False and replay['complete'] is True
    assert state['calls'] == calls and not app._tasks


def test_unread_sources_are_partial_and_retained(env):
    app, state = env
    state['limit'] = 2
    ids = sources(app, 5)
    job = settled(app, trigger(app, ids)['job_id'])
    assert job['status'] == 'partial'
    assert job['read_count'] == job['handled_count'] == job['consumed_count'] == 2
    assert job['remaining_selected_count'] == app.store.fresh_stats('local', FP)['total'] == 3


def test_consumed_cross_scope_relearning_does_not_subtract_target_fresh(env):
    app, state = env
    origin = app.workspace.create_project('local', '来源项目')['id']
    target = app.workspace.create_project('local', '目标项目')['id']
    ids = sources(app, project=origin)
    settled(app, trigger(app, ids, project=origin, request='origin')['job_id'])
    unselected = sources(app, project=target, prefix='target-new')
    job = settled(app, trigger(app, ids, project=target, request='relearn')['job_id'])
    assert job['status'] == 'done' and job['handled_count'] == 1 and job['consumed_count'] == 0
    assert app.store.fresh_stats('local', FP, target)['total'] == 1
    with app.store.conversations.database() as conn:
        rows = conn.execute('SELECT project_id,source_id FROM learning_consumed').fetchall()
    assert [(row['project_id'], row['source_id']) for row in rows] == [(origin, ids[0])]
    assert next(row for row in app.sources('local', FP)['items'] if row['id'] == unselected[0])['fresh']


@pytest.mark.parametrize('pause', [False, True])
def test_failed_and_paused_same_request_retries_real_executor(env, pause):
    app, state = env
    ids = sources(app)
    if pause:
        entered, release = threading.Event(), threading.Event()
        state['block'] = (entered, release)
        initial = trigger(app, ids, request='retry')
        assert entered.wait(5)
        app.store.control('local', FP, enabled=False)
        release.set()
    else:
        state['mode'] = 'model_fail'
        initial = trigger(app, ids, request='retry')
    failed = settled(app, initial['job_id'])
    assert failed['status'] in {'failed', 'paused'}
    assert failed['consumed_count'] == 0
    state['mode'], state['block'] = 'empty', None
    calls = state['calls']
    retry = trigger(app, ids, request='retry')
    assert retry['job_id'] == initial['job_id']
    done = settled(app, retry['job_id'])
    assert done['status'] == 'done' and done['attempt'] == 1
    assert state['calls'] > calls and done['consumed_count'] == 1


def test_active_selection_is_idempotent_but_new_selection_is_busy(env):
    app, state = env
    entered, release = threading.Event(), threading.Event()
    state['block'] = (entered, release)
    ids = sources(app)
    first = trigger(app, ids, request='active')
    assert entered.wait(5)
    same = trigger(app, ids, request='active')
    assert same['job_id'] == first['job_id'] and same['attempt'] == 0
    other = sources(app, prefix='other')
    with pytest.raises(RunError, match='未受理') as error:
        trigger(app, other, request='other-selection')
    assert error.value.code == 'learning_job_conflict'
    release.set()
    assert settled(app, first['job_id'])['status'] == 'done'
    assert app.store.fresh_stats('local', FP)['total'] == 1


def test_same_request_cannot_change_selection(env):
    app, state = env
    ids = sources(app)
    first = trigger(app, ids, request='stable')
    settled(app, first['job_id'])
    other = sources(app, prefix='changed')
    with pytest.raises(RunError) as error:
        trigger(app, other, request='stable')
    assert error.value.code == 'learning_request_conflict'
    assert app.store.fresh_stats('local', FP)['total'] == 1


def test_original_scope_epoch_change_blocks_late_cross_scope_processing(env):
    app, state = env
    origin = app.workspace.create_project('local', '来源')['id']
    target = app.workspace.create_project('local', '目标')['id']
    ids = sources(app, project=origin)
    entered, release = threading.Event(), threading.Event()
    state['block'] = (entered, release)
    job = trigger(app, ids, project=target)
    assert entered.wait(5)
    app.store.control('local', FP, origin, enabled=False)
    release.set()
    result = settled(app, job['job_id'])
    assert result['status'] in {'failed', 'paused'} and result['consumed_count'] == 0
    assert app.store.fresh_stats('local', FP, origin)['total'] == 1


def test_fabricated_result_without_real_finish_receipt_cannot_consume(env):
    app, state = env
    ids = sources(app)
    job = app.store.schedule('local', FP, source_ids=ids, manual=True)
    result = app.store.set_status(job, 'done', {'ok': True, 'processed_source_ids': ids},
        consumed_sources=app.store.sources_by_ids('local', FP, ids))
    assert result['state'] == 'failed' and result['consumed_count'] == 0
    assert result['error_code'] == 'learning_processing_receipt_required'


@pytest.mark.parametrize('value', [{'manual': 1}, {'source_ids': ['x' * 129]},
    {'source_ids': ['x'] * 201}, {'local_materials': [{'client_id': 'm', 'text': 'x' * 4001}]}])
def test_api_parameters_are_strict_and_bounded(value):
    with pytest.raises(ValidationError):
        LearningRequest(**value)


def test_real_http_fixed_selection_receipts_and_done_replay(env, monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from dataset_recommender.app import learning_api
    app, state = env
    assert Path(learning_api.__file__).resolve().is_relative_to(ROOT / 'src')
    monkeypatch.setattr(learning_api, 'service_for', lambda conversations, workspace: app)
    shared = SimpleNamespace(store=app.store.conversations, workspace=app.workspace)
    server = FastAPI()
    server.include_router(learning_api.build_learning_router(service=lambda: shared,
        principal=lambda request: 'local', endpoint=lambda *args: FP, require_origin=lambda request: None,
        configure=lambda request, payload: CFG, quota=lambda *args: None))
    ids = sources(app, 13)
    body = {'manual': True, 'source_ids': ids, 'request_id': 'http-fixed',
        'expected_identity': 'local', 'expected_config_epoch': 0}
    with TestClient(server) as client:
        listing = client.get('/api/learning/sources').json()
        assert listing['ok'] and len(listing['items']) == 13
        first = client.post('/api/learning/trigger', json=body)
        assert first.status_code == 200, first.text
        first = first.json()
        assert first['selected_total'] == 13 and first['request_id'] == 'http-fixed'
        assert settled(app, first['job_id'])['status'] == 'partial'
        second = client.post('/api/learning/trigger', json=body).json()
        assert second['job_id'] == first['job_id']
        assert settled(app, second['job_id'])['status'] == 'done'
        calls = state['calls']
        replay = client.post('/api/learning/trigger', json=body).json()
        assert replay['scheduled'] is False and replay['complete'] is True
        assert replay['handled_count'] == 13 and replay['remaining_selected_count'] == 0
        assert state['calls'] == calls
        assert client.post('/api/learning/trigger', json={**body, 'manual': 1}).status_code == 422
        assert client.post('/api/learning/trigger', json={**body, 'expected_identity': 'other'}).status_code == 409


def test_old_attempt_cannot_overwrite_successful_retry(env):
    app, state = env
    ids = sources(app)
    state['mode'] = 'model_fail'
    first = trigger(app, ids, request='attempt-cas')
    settled(app, first['job_id'])
    with app.store.conversations.database() as conn:
        old = dict(conn.execute('SELECT * FROM learning_jobs WHERE owner=? AND id=?', ('local', first['job_id'])).fetchone())
    state['mode'] = 'empty'
    second = trigger(app, ids, request='attempt-cas')
    assert settled(app, second['job_id'])['status'] == 'done'
    stale = app.store.set_status(old, 'failed', {'ok': False, 'error_code': 'old-worker'})
    assert stale['error_code'] == 'learning_job_superseded'
    current = app.store.job_receipt('local', second['job_id'])
    assert current['status'] == 'done' and current['consumed_count'] == 1


def test_allow_cross_project_manual_user_rollup_keeps_origin_provenance(env, monkeypatch):
    """跨项目用户层汇总只在「显式选来源 + 一次性手动 + 明确授权标记」同时成立时放行；
    材料出处保留原项目编号，核销仍在来源原 scope（设计：用户显式一次性确认才可用户级汇总）。"""
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from dataset_recommender.app import learning_api
    app, state = env
    state['mode'] = 'learn'
    monkeypatch.setattr(learning_api, 'service_for', lambda conversations, workspace: app)
    shared = SimpleNamespace(store=app.store.conversations, workspace=app.workspace)
    server = FastAPI()
    server.include_router(learning_api.build_learning_router(service=lambda: shared,
        principal=lambda request: 'local', endpoint=lambda *args: FP, require_origin=lambda request: None,
        configure=lambda request, payload: CFG, quota=lambda *args: None))
    origin = app.workspace.create_project('local', '来源项目')['id']
    base = {'expected_identity': 'local', 'expected_config_epoch': app.store.config_epoch('local', FP)}
    material = {'client_id': 'm-1', 'project_id': origin, 'text': '项目里约定：汇报先给结论。'}
    with TestClient(server) as client:
        denied = client.post('/api/learning/control', json={**base, 'enabled': True, 'allow_cross_project': True})
        assert denied.headers['x-error-code'] == 'invalid_learning_control', denied.text
        denied = client.post('/api/learning/trigger', json={**base, 'local_materials': [material],
            'allow_cross_project': True})
        assert denied.headers['x-error-code'] == 'invalid_learning_trigger', denied.text
        denied = client.post('/api/learning/trigger', json={**base, 'manual': True, 'local_materials': [material]})
        assert denied.headers['x-error-code'] == 'learning_scope_conflict', denied.text
        denied = client.post('/api/learning/trigger', json={**base, 'manual': True, 'allow_cross_project': True,
            'local_materials': [{'client_id': 'm-0', 'text': '用户层材料。'}]})
        assert denied.headers['x-error-code'] == 'invalid_learning_trigger', denied.text
        ok = client.post('/api/learning/trigger', json={**base, 'manual': True, 'allow_cross_project': True,
            'local_materials': [material], 'request_id': 'cross-1'})
        assert ok.status_code == 200, ok.text
        receipt = ok.json()
        assert receipt['scheduled'] is True and receipt['selected_total'] == 1
        row = next(item for item in app.sources('local', FP)['items'])
        assert row['project_id'] == origin and row['kind'] == 'user_provided'
        job = settled(app, receipt['job_id'])
        assert job['status'] == 'done' and job['consumed_count'] == 1
        with app.store.conversations.database() as conn:
            consumed = conn.execute('SELECT project_id,source_id FROM learning_consumed').fetchall()
        assert [(r['project_id'], r['source_id']) for r in consumed] == [(origin, row['id'])]


def test_cross_project_materials_rejected_without_rollup_authorization(env):
    """服务层兜底：不经 HTTP 也一样——材料项目与目标不一致且非授权汇总时拒绝；
    授权汇总时同一 materialize 调用不再因签名缺参崩溃（回归钉）。"""
    app, state = env
    origin = app.workspace.create_project('local', '来源项目')['id']
    epoch = app.store.config_epoch('local', FP)
    with pytest.raises(RunError) as conflict:
        app.trigger('local', CFG, project_id='', manual=True, request_id='m-plain',
            local_materials=[{'client_id': 'm-1', 'project_id': origin, 'text': '项目材料。'}],
            expected_identity='local', expected_config_epoch=epoch)
    assert conflict.value.code == 'learning_scope_conflict'
    with pytest.raises(RunError) as mismatch:
        app.trigger('local', CFG, project_id=origin, manual=True, request_id='m-target',
            local_materials=[{'client_id': 'm-2', 'project_id': '', 'text': '跨目标材料。'}],
            expected_identity='local', expected_config_epoch=epoch)
    assert mismatch.value.code == 'learning_scope_conflict'
