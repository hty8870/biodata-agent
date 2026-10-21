"""Exercise pytest teardown against real executors, service caches and rate limits."""
from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.agent import learning_loop
from dataset_recommender.app import accounts, learning_service, mcp_agent_tools, replay_service, webapp
from dataset_recommender.llm.llm_client import LLMConfig

_PREVIOUS = None


def _assert_drained(state):
    for runtime in state['runtimes']:
        assert runtime._closed.is_set()
        assert not runtime._heartbeat_thread.is_alive()
        assert all(not thread.is_alive() for thread in runtime._pool._threads)
    learning = state['learning']
    assert learning._closing and learning._pool is None
    assert not learning._tasks and not learning._configs and not learning._pending
    assert all(not thread.is_alive() for thread in state['learning_threads'])
    assert all(not thread.is_alive() for thread in state['replay_threads'])
    assert sorted(row[0] for row in state['calls']) == ['learning', 'replay', 'replay', 'shared', 'web']
    assert all(patched and unlocked for _, patched, unlocked in state['calls'])
    assert not mcp_agent_tools._SERVICES
    assert not learning_service._SERVICES
    assert webapp._CONVERSATION_RUNTIME is None
    assert not replay_service._DRAW
    assert not webapp._rate_buckets


@pytest.fixture(scope='module', autouse=True)
def _verify_last_teardown():
    yield
    if _PREVIOUS is not None:
        try:
            _assert_drained(_PREVIOUS)
        finally:
            for event in _PREVIOUS['releases']:
                event.set()
            for close in _PREVIOUS['real_closes']:
                close()
            for thread in _PREVIOUS['replay_threads']:
                thread.join(timeout=5)


@pytest.mark.parametrize('iteration', range(3))
def test_cached_workers_finish_before_the_next_test(iteration, tmp_path, monkeypatch):
    global _PREVIOUS
    if _PREVIOUS is not None:
        _assert_drained(_PREVIOUS)
    monkeypatch.setenv('BIODATA_RUNS_DB', str(tmp_path / 'explicit-runs.sqlite'))
    monkeypatch.setenv('BIODATA_WORKSPACE_DB', str(tmp_path / 'workspace.sqlite'))
    monkeypatch.setenv('BIODATA_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('BIODATA_SESSION_LOG', 'on')
    monkeypatch.setattr(webapp, 'PROJECT_ROOT', tmp_path)
    web = webapp._get_conversation_runtime()
    shared = mcp_agent_tools.service_for(tmp_path)
    learning = learning_service.service_for(shared.store, shared.workspace)
    assert web.store.path == shared.store.path == tmp_path / 'explicit-runs.sqlite'
    cfg = LLMConfig(enable_llm=True, mock_llm=False, provider='openai-compatible',
                    api_key='synthetic-key', base_url='https://model.example.test/v1', model='synthetic-model')
    calls = []
    releases, real_closes = [], [web.close, shared.runtime.close, learning.close]
    _PREVIOUS = {'runtimes': [web, shared.runtime], 'learning': learning, 'calls': calls,
                 'learning_threads': [], 'replay_threads': [],
                 'releases': releases, 'real_closes': real_closes}
    learning_closing = threading.Event()
    releases.append(learning_closing)
    started = {name: threading.Event() for name in ('web', 'shared', 'learning', 'replay')}

    def observe(label, patched):
        unlocked = True
        for lock in (mcp_agent_tools._LOCK, learning_service._SERVICES_LOCK,
                     webapp._CONVERSATION_RUNTIME_LOCK, replay_service._DRAW_GUARD):
            acquired = lock.acquire(timeout=1)
            unlocked = unlocked and acquired
            if acquired:
                lock.release()
        calls.append((label, patched, unlocked))

    def model_reply(prompt, config):
        started['learning'].set()
        assert learning_closing.wait(5)
        observe('learning', learning_loop.call_llm is model_reply)
        return '{"tool":"finish","args":{}}'

    def replay_reply(prompt, config):
        started['replay'].set()
        assert learning_closing.wait(5)
        observe('replay', replay_service._call_text is replay_reply)
        return json.dumps({'motivation': {'title': 'Synthetic turn', 'turn_refs': [1]},
                           'outputs': [], 'knowledge': [], 'decisions': [], 'edges': []})

    monkeypatch.setattr(learning_loop, 'call_llm', model_reply)
    monkeypatch.setattr(replay_service, '_call_text', replay_reply)
    for label, runtime in [('web', web), ('shared', shared.runtime)]:
        release = threading.Event()
        releases.append(release)
        real_close = runtime.close
        def close(*args, _release=release, _close=real_close, **kwargs):
            _release.set()
            return _close(*args, **kwargs)
        monkeypatch.setattr(runtime, 'close', close)
        def execute(emit, _label=label, _release=release):
            started[_label].set()
            assert _release.wait(5)
            observe(_label, replay_service._call_text is replay_reply)
            return {'ok': True}
        runtime.submit(label, label + str(iteration), label, prepare=lambda operation=execute: operation)
        assert started[label].wait(5)
    real_learning_close = learning.close
    def close_learning():
        learning_closing.set()
        return real_learning_close()
    monkeypatch.setattr(learning, 'close', close_learning)
    learning.control('local', cfg, enabled=True)
    learning.trigger('local', cfg, request_id='feedback', feedback='Synthetic persistent feedback')
    assert started['learning'].wait(5)
    _PREVIOUS['learning_threads'] = list(learning._pool._threads)
    conv = shared.store.create('local')['id']
    replay_service.record_turn(tmp_path, 'local', conv, '', 'chat',
                               {'user': 'Synthetic turn', 'reply': 'Synthetic reply'}, cfg)
    assert started['replay'].wait(5)
    _PREVIOUS['replay_threads'] = [t for t in threading.enumerate() if t.name == 'replay-digest']
    replay_service.schedule_draw(tmp_path, 'local', conv, cfg)
    assert replay_service.drawing('local', conv)
    assert webapp._rate_limited('fixture-window', limit=1, window=60)
    assert not webapp._rate_limited('fixture-window', limit=1, window=60)


@pytest.mark.parametrize('iteration', range(2))
def test_registration_limit_remains_active_in_each_test(iteration, tmp_path, monkeypatch):
    assert not webapp._rate_buckets
    monkeypatch.setenv('BIODATA_ACCOUNTS_FILE', str(tmp_path / 'accounts.json'))
    monkeypatch.setenv('BIODATA_SESSIONS_FILE', str(tmp_path / 'sessions.json'))
    monkeypatch.setenv('BIODATA_INVITE_CODE', 'synthetic-invite')
    monkeypatch.setenv('BIODATA_REQUIRE_ACCOUNT', '1')
    accounts._reset_state_for_tests()
    try:
        with TestClient(webapp.app, base_url='http://127.0.0.1') as client:
            for number in range(6):
                response = client.post('/api/account/register', json={
                    'username': f'user{number}', 'password': 'synthetic-password',
                    'invite_code': 'synthetic-invite'})
                assert response.status_code == (200 if number < 5 else 429)
    finally:
        accounts._reset_state_for_tests()


def test_explicit_bucket_override_is_not_replaced_during_test(monkeypatch):
    buckets = {}
    monkeypatch.setattr(webapp, '_rate_buckets', buckets)
    assert webapp._rate_limited('explicit-window', limit=1, window=60)
    assert not webapp._rate_limited('explicit-window', limit=1, window=60)
    assert webapp._rate_buckets is buckets
