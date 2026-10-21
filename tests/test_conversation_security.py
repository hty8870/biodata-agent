"""共享后端最终安全接缝：真实临时库/进程/协议，模型回复仅作可控替身。"""
import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.app import accounts, llm_quota, mcp_tokens, replay_service, replay_store, session_log, webapp
from dataset_recommender.app.conversation_service import ConversationService
from dataset_recommender.app.conversation_runs import ConversationRuntime, RunStore
from dataset_recommender.app.conversation_store import ConversationStore
from dataset_recommender.llm.config import get_settings
from dataset_recommender.llm.llm_client import LLMConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, 'PROJECT_ROOT', tmp_path)
    for key, path in {
        'BIODATA_DATA_ROOT': tmp_path, 'BIODATA_PATCHES_DIR': tmp_path / 'patches',
        'BIODATA_WORKSPACE_DB': tmp_path / 'workspace.sqlite',
        'BIODATA_ACCOUNTS_FILE': tmp_path / 'accounts.json',
        'BIODATA_SESSIONS_FILE': tmp_path / 'sessions.json',
        'BIODATA_MCP_TOKENS_FILE': tmp_path / 'tokens.json',
        'BIODATA_LLM_QUOTA_FILE': tmp_path / 'quota.json',
        'DATA_DIR': ROOT / 'database/base',
    }.items():
        monkeypatch.setenv(key, str(path))
    monkeypatch.setenv('AGENT_TRACE', 'off')
    monkeypatch.setenv('BIODATA_REQUIRE_ACCOUNT', '1')
    monkeypatch.setenv('BIODATA_INVITE_CODE', 'synthetic-invite')
    monkeypatch.setenv('LLM_PROVIDER', 'openai-compatible')
    monkeypatch.setenv('ENABLE_LLM', 'true')
    monkeypatch.setenv('MOCK_LLM', 'false')
    monkeypatch.setenv('LLM_API_KEY', 'synthetic-managed-key')
    monkeypatch.setenv('BIODATA_TRIAL_API_KEY', 'synthetic-trial-key')
    get_settings.cache_clear()
    accounts._reset_state_for_tests()
    mcp_tokens._reset_state_for_tests()
    yield tmp_path
    from dataset_recommender.app.mcp_agent_tools import _SERVICES
    for key in list(_SERVICES):
        service = _SERVICES.pop(key)
        service.runtime.close()
    get_settings.cache_clear()
    accounts._reset_state_for_tests()
    mcp_tokens._reset_state_for_tests()


def register(client, username):
    response = client.post('/api/account/register', json={'username': username, 'password': 'synthetic-password', 'invite_code': 'synthetic-invite'})
    assert response.status_code == 200
    return client.get('/api/account/whoami').json()['user']


def seed_turn(root, owner, conv_id):
    replay_service.record_turn(root, owner, conv_id, '', 'chat', {'user': 'synthetic turn', 'reply': 'synthetic result'},
                               LLMConfig(enable_llm=False, mock_llm=False))


def test_owned_replay_envelope_strict_config_quota_and_no_secret_persistence(isolated, monkeypatch):
    calls = []
    def reply(prompt, cfg):
        calls.append((cfg.provider, cfg.api_key, cfg.model))
        return 'synthetic reply'
    monkeypatch.setattr(replay_service, '_call_text', reply)
    with TestClient(webapp.app, base_url='http://127.0.0.1') as client:
        alice = register(client, 'alice')
        service = webapp._get_conversation_service()
        conv = service.conversation_action(alice['id'], '', 'create')['conversation']['id']
        seed_turn(isolated, alice['id'], conv)
        base = '/api/conversations/' + conv + '/replay/'
        byok = {'data': {'text': 'question'}, 'provider': 'openai-compatible', 'use_llm': True,
                'mock_llm': False, 'api_key': 'synthetic-byok-key', 'model': 'synthetic-model'}
        response = client.post(base + 'chat', json=byok)
        assert response.status_code == 200 and response.json()['ok']
        assert calls[-1] == ('openai-compatible', 'synthetic-byok-key', 'synthetic-model')
        assert llm_quota.usage_snapshot(isolated, 'alice', trial=False) == 0
        trial = client.post(base + 'chat', json={'data': {'text': 'trial question'}, 'provider': 'trial',
            'use_llm': True, 'api_key': 'synthetic-ignored-key'})
        assert trial.status_code == 200 and trial.json()['ok']
        assert calls[-1][0:2] == ('trial', 'synthetic-trial-key')
        assert llm_quota.usage_snapshot(isolated, 'alice', trial=True) == 1
        assert llm_quota.usage_snapshot(isolated, 'alice', trial=False) == 0
        assert client.post(base + 'snapshot', json={'data': {}}).status_code == 200
        assert client.post(base + 'chat', json={'data': {'text': 'x', 'api_key': 'forbidden'}}).status_code == 422
        assert client.post(base + 'chat', json={'data': {'text': 'x'}, 'owner': alice['id']}).status_code == 422
        before = len(calls)
        bob = register(client, 'bob')
        denied = client.post(base + 'chat', json={'data': {'text': 'forbidden'}, 'provider': 'trial', 'use_llm': True})
        assert denied.status_code == 404 and len(calls) == before
        assert llm_quota.usage_snapshot(isolated, 'bob', trial=True) == 0
        raw_replay = replay_store.store_path(isolated, alice['id'], conv).read_bytes()
        raw_db = service.store.path.read_bytes()
        for secret in (b'synthetic-byok-key', b'synthetic-managed-key', b'synthetic-trial-key', b'synthetic-ignored-key'):
            assert secret not in raw_replay and secret not in raw_db


def test_shared_accounts_have_distinct_patch_results_and_matching_policy(isolated, monkeypatch):
    from dataset_recommender.corpus import patch_package as patches
    from dataset_recommender.corpus.corpus import corpus_snapshot, load_normalized_corpus
    from dataset_recommender.agent.learning_scope import current_project_id
    from dataset_recommender.app import conversation_service as module
    owners = [('account_A', 'owned-a'), ('account_B', 'owned-b')]
    for owner, uid in owners:
        path = patches.patch_path_for(isolated, owner)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'schema_version': 1, 'account_id': owner, 'updated_at': 1, 'blocks': [], 'trash': [],
            'adds': [{'dataset_uid': uid, 'dataset_name': uid + ' human lung', 'species': 'Human',
                      'tissue': 'lung', 'source': 'Private Catalog', 'url': 'https://example.org/' + uid}]}), encoding='utf-8')
    observed = []
    run = module.DatasetRecommendationWorkflow.run_with_meta
    def real_workflow(flow, params, **kwargs):
        observed.append((patches.current_patch_scope(), current_project_id()))
        return run(flow, params, **kwargs)
    monkeypatch.setattr(module.DatasetRecommendationWorkflow, 'run_with_meta', real_workflow)
    service = ConversationService(isolated)
    try:
        hashes = []
        for owner, uid in owners:
            project = service.workspace.create_project(owner, owner)['id']
            task = service.chat(owner, '人类肺', request_id='same-request', project_id=project, use_agent=False,
                state_patch={'sources': ['Private Catalog'], 'search_params': {'polish': False}},
                configure=lambda: LLMConfig(enable_llm=False, mock_llm=False))
            body = service.runtime.wait_result(owner, task['id'], timeout=10)
            assert [row['dataset_uid'] for row in body['result_payload']['results']] == [uid]
            assert (owner, project) in observed
            with patches.bind_patch_scope(owner):
                expected = corpus_snapshot(load_normalized_corpus(get_settings().data_dir, isolated, ['Private Catalog']))
            assert body['policy_id']['corpus'] == {'snapshot_id': expected['snapshot_id'], 'n_records': expected['n_records']}
            assert body['policy_id'] == body['result_payload']['policy_id']
            hashes.append(body['policy_id']['corpus']['snapshot_id'])
            assert patches.current_patch_scope() is None and current_project_id() == ''
        assert hashes[0] != hashes[1]
    finally:
        service.runtime.close()


def child(code, paths, payload=None, env=None):
    process = subprocess.Popen([sys.executable, '-B', '-c', code, *map(str, paths)], env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if payload is not None:
        process.stdin.write(json.dumps(payload).encode())
        process.stdin.close(); process.stdin = None
    return process


def ready(path, process):
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        assert process.poll() is None, process.communicate()[1].decode('utf-8', 'replace')
        time.sleep(.01)
    assert path.exists()


def completed(process):
    output, error = process.communicate(timeout=15)
    assert process.returncode == 0, error.decode('utf-8', 'replace')
    return output.decode().strip()


def test_live_executor_lock_wins_over_forward_wallclock(isolated, monkeypatch):
    db, marker, release = isolated / 'clock.sqlite', isolated / 'clock-ready', isolated / 'clock-release'
    code = '''import sys,time
from pathlib import Path
from dataset_recommender.app.conversation_runs import ConversationRuntime,RunStore
root,db,marker,release=map(Path,sys.argv[1:])
r=ConversationRuntime(RunStore(root,path=db))
def execute(emit):
    marker.touch()
    while not release.exists():time.sleep(.01)
    emit('step',{'completed':True})
    return {'ok':True}
run=r.submit('clock-owner','clock-request','fp',prepare=lambda:execute)
r.wait_result('clock-owner',run['id'],timeout=10)
r.close()
'''
    process = child(code, [isolated, db, marker, release])
    try:
        ready(marker, process)
        store = RunStore(isolated, path=db)
        real_time = time.time
        monkeypatch.setattr(time, 'time', lambda: real_time() + 365 * 86400)
        observer = ConversationRuntime(store)
        try:
            observer.store.reconcile_interrupted(observer.boot_id)
            task = store.find_request('clock-owner', 'clock-request')
            with store.database() as conn:
                expires = conn.execute('SELECT expires_at FROM conversation_executors WHERE boot_id<>(?)', (observer.boot_id,)).fetchone()[0]
            assert expires < time.time()
            assert task['status'] == 'running'
            release.touch()
            assert completed(process) == ''
            assert store.load('clock-owner', task['id'])['status'] == 'done'
        finally:
            observer.close()
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=5)


@pytest.mark.parametrize('action', ['digest', 'chat'])
def test_delete_during_replay_network_return_cannot_recreate_files(isolated, action):
    store = ConversationStore(isolated)
    conv = store.create('local')['id']
    seed_turn(isolated, 'local', conv)
    marker, release = isolated / (action + '-ready'), isolated / (action + '-release')
    code = '''import sys,json,time
from pathlib import Path
from dataset_recommender.app import replay_service as r,replay_store,session_log
from dataset_recommender.llm.llm_client import LLMConfig
root,marker,release=map(Path,sys.argv[1:4]);conv,action=sys.argv[4:]
def network(prompt,cfg):
    marker.touch()
    while not release.exists():time.sleep(.01)
    return json.dumps({'outputs':[{'title':'result','detail':'synthetic','turn_refs':[1]}],'knowledge':[],'decisions':[],'edges':[]}) if action=='digest' else 'synthetic reply'
r._call_text=network
cfg=LLMConfig(enable_llm=True,mock_llm=False,api_key='synthetic-key')
result=r.manual_digest(root,'local',conv,'',cfg) if action=='digest' else r.chat(root,'local',conv,'','question',cfg)
assert result is None
assert not replay_store.store_path(root,'local',conv).exists()
assert not session_log.log_path(root,'local',conv).exists()
'''
    process = child(code, [isolated, marker, release, conv, action])
    try:
        ready(marker, process)
        store.action('local', conv, 'delete', expected_revision=0)
        release.touch()
        completed(process)
        assert replay_service.status(isolated, 'local', conv, '') is None
        client = TestClient(webapp.app, base_url='http://127.0.0.1')
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(webapp, '_account_gate_required', lambda: False)
            assert client.get('/api/replay/status', params={'conv_id': conv}).status_code == 404
            assert client.get('/api/conversations/' + conv + '/replay').status_code == 404
    finally:
        if process.poll() is None:process.terminate();process.wait(timeout=5)


def test_stdio_explicit_token_protocol_cross_process_revoke(isolated):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    token, record = mcp_tokens.mint_token('stdio-account', 'alice', store_path=isolated / 'tokens.json')
    env = dict(os.environ, BIODATA_MCP_ACCESS_TOKEN=token, BIODATA_MCP_CALL_LOG='off')
    params = StdioServerParameters(command=sys.executable, args=['-B', str(ROOT / 'src/dataset_recommender/app/mcp_server.py')], env=env)
    async def protocol():
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                result = await session.call_tool('agent_conversations', {'action': 'create', 'title': 'owned'})
                assert not result.isError
                conv = json.loads(result.content[0].text)['conversation']['id']
                assert ConversationStore(isolated).exists('stdio-account', conv)
                assert not ConversationStore(isolated).exists('local', conv)
                mcp_tokens.revoke_token('stdio-account', record['token_id'], store_path=isolated / 'tokens.json')
                refused = await session.call_tool('agent_conversations', {'action': 'get', 'conversation_id': conv})
                assert refused.isError and 'invalid_token' in refused.content[0].text
    asyncio.run(protocol())


@pytest.mark.parametrize('account_switch', ['1', 'on', ' ON ', ' true '])
def test_bearer_account_a_cookie_b_uses_a_budget_and_owner(isolated, monkeypatch, account_switch):
    from dataset_recommender.app import conversation_service
    monkeypatch.setenv('BIODATA_REQUIRE_ACCOUNT', account_switch)
    monkeypatch.setattr(webapp, '_rate_buckets', {})
    monkeypatch.setenv('BIODATA_LLM_DAILY_PER_USER', '1')
    monkeypatch.setenv('BIODATA_LLM_DAILY_GLOBAL', '10')
    def route(project, text, **kwargs):
        return {'route': 'none', 'query': '', 'plan': {'report_zh': 'reply'}, 'echo_zh': '', 'retrieval': None, 'via': 'agent'}
    monkeypatch.setattr(conversation_service, 'route_with_project', route)
    with TestClient(webapp.app, base_url='http://127.0.0.1') as client:
        alice = register(client, 'alice')
        token, _ = mcp_tokens.mint_token(alice['id'], 'alice', store_path=isolated / 'tokens.json')
        bob = register(client, 'bob')
        assert client.get('/api/account/whoami').json()['user']['id'] == bob['id']
        def call(request_id):
            result = client.post('/mcp', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'},
                json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'agent_chat',
                    'arguments': {'message': 'question', 'request_id': request_id, 'wait_seconds': 10}}})
            assert result.status_code == 200
            return result.json()['result']
        first = call('bearer-first')
        assert not first.get('isError')
        conv = json.loads(first['content'][0]['text'])['conversation_id']
        assert ConversationStore(isolated).exists(alice['id'], conv)
        assert not ConversationStore(isolated).exists(bob['id'], conv)
        assert llm_quota.usage_snapshot(isolated, 'alice', trial=False) == 1
        assert llm_quota.usage_snapshot(isolated, 'bob', trial=False) == 0
        denied = call('bearer-second')
        assert denied.get('isError') and 'llm_quota_exceeded' in denied['content'][0]['text']


@pytest.mark.parametrize('value', ['on', ' ON ', ' true ', ' yes ', ' 1 '])
def test_account_switch_uses_same_parser_and_quota_everywhere(isolated, monkeypatch, value):
    from dataset_recommender.app import llm_admission
    monkeypatch.setenv('BIODATA_REQUIRE_ACCOUNT', value)
    monkeypatch.setenv('BIODATA_LLM_DAILY_PER_USER', '1')
    monkeypatch.setenv('BIODATA_LLM_DAILY_GLOBAL', '10')
    assert webapp._account_gate_required()
    cfg = LLMConfig(enable_llm=True, mock_llm=False, api_key='synthetic-managed-key')
    llm_admission.gate_quota(isolated, username='alice', cfg=cfg)
    assert llm_quota.usage_snapshot(isolated, 'alice', trial=False) == 1
    with pytest.raises(Exception) as denied:
        llm_admission.gate_quota(isolated, username='alice', cfg=cfg)
    assert getattr(denied.value, 'code', '') == 'llm_quota_exceeded'


@pytest.mark.skipif(os.name != 'nt', reason='Windows 文件共享句柄拒绝删除的真实恢复边界')
@pytest.mark.parametrize('restart_mode', ['service', 'asgi', 'asgi-no-mcp'])
def test_deleted_cleanup_recovers_on_service_start_after_windows_process_exit(isolated, restart_mode):
    code = '''import ctypes,json,sys
from ctypes import wintypes
from pathlib import Path
from dataset_recommender.app.conversation_service import ConversationService
from dataset_recommender.app import replay_service,replay_store,session_log
from dataset_recommender.llm.llm_client import LLMConfig
root=Path(sys.argv[1])
service=ConversationService(root)
handle=None
kernel=ctypes.WinDLL('kernel32',use_last_error=True)
kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
kernel.CreateFileW.restype=wintypes.HANDLE
kernel.CloseHandle.argtypes=[wintypes.HANDLE]
kernel.CloseHandle.restype=wintypes.BOOL
try:
    conv=service.store.create('local')['id']
    replay_service.record_turn(root,'local',conv,'','chat',{'user':'synthetic turn'},LLMConfig(enable_llm=False,mock_llm=False))
    log=session_log.log_path(root,'local',conv)
    handle=kernel.CreateFileW(str(log),0x80000000,3,None,3,0x80,None)
    assert handle not in (None,ctypes.c_void_p(-1).value),ctypes.get_last_error()
    deleted=service.conversation_action('local',conv,'delete',expected_revision=0)['conversation']
    assert deleted['deleted'] and deleted['cleanup_pending'] and log.exists()
    assert not service.store.exists('local',conv)
    assert replay_service.status(root,'local',conv,'') is None
    with service.store.database() as db:
        assert db.execute('SELECT deleted FROM shared_conversations WHERE owner=? AND id=?',('local',conv)).fetchone()[0]==1
        pending=db.execute('SELECT attempts,last_error FROM shared_cleanup WHERE owner=? AND conversation_id=?',('local',conv)).fetchone()
        assert pending['attempts']==1 and pending['last_error']=='PermissionError'
    print(json.dumps(deleted))
finally:
    service.runtime.close()
    assert not service.runtime._heartbeat_thread.is_alive()
    if handle not in (None,ctypes.c_void_p(-1).value):kernel.CloseHandle(handle)
'''
    process = child(code, [isolated])
    restarted = None
    try:
        deleted = json.loads(completed(process))
        conv = deleted['conv_id']
        log = session_log.log_path(isolated, 'local', conv)
        assert log.exists()
        store = ConversationStore(isolated)
        with store.database() as db:
            assert db.execute('SELECT COUNT(*) FROM shared_cleanup').fetchone()[0] == 1
        if restart_mode == 'service':
            restarted = ConversationService(isolated)
            active_store = restarted.store
        else:
            startup = '''import json,os,sqlite3,sys
from pathlib import Path
from fastapi.testclient import TestClient
from dataset_recommender.app import webapp,learning_service,replay_store,session_log
root=Path(sys.argv[1]);conv=sys.argv[2]
webapp.PROJECT_ROOT=root
if sys.argv[3]=='asgi-no-mcp':webapp._mcp_server=None
with TestClient(webapp.app):
    runtime=webapp._CONVERSATION_RUNTIME
    assert runtime is not None,'standard ASGI startup did not activate persisted maintenance'
    assert runtime._heartbeat_thread.is_alive() and not runtime._tasks
    assert not learning_service._SERVICES,'startup must not authorize or schedule learning'
    assert not session_log.log_path(root,'local',conv).exists()
    assert not replay_store.store_path(root,'local',conv).exists()
    db=sqlite3.connect(os.environ['BIODATA_RUNS_DB'])
    try:
        assert db.execute('SELECT COUNT(*) FROM shared_cleanup').fetchone()[0]==0
        assert db.execute('SELECT deleted FROM shared_conversations WHERE owner=? AND id=?',('local',conv)).fetchone()[0]==1
    finally:db.close()
assert webapp._CONVERSATION_RUNTIME is None
assert runtime._closed.is_set() and not runtime._heartbeat_thread.is_alive()
assert all(not thread.is_alive() for thread in runtime._pool._threads)
print(json.dumps({'startup_without_business_requests':True,'pid':os.getpid(),'boot_id':runtime.boot_id,'closed':True}))
'''
            started_process = child(startup, [isolated, conv, restart_mode])
            try:
                observed = json.loads(completed(started_process))
                assert started_process.pid != process.pid and observed['startup_without_business_requests']
                assert observed['closed']
            finally:
                if started_process.poll() is None:
                    started_process.terminate()
                    started_process.communicate(timeout=5)
            active_store = store
        assert not log.exists(), '新服务启动必须自动消费已提交的清理记录，而非要求重发删除'
        assert not replay_store.store_path(isolated, 'local', conv).exists()
        with active_store.database() as db:
            assert db.execute('SELECT COUNT(*) FROM shared_cleanup').fetchone()[0] == 0
            tombstone = db.execute('SELECT deleted FROM shared_conversations WHERE owner=? AND id=?', ('local', conv)).fetchone()
            assert tombstone['deleted'] == 1
        assert not active_store.exists('local', conv)
        seed_turn(isolated, 'local', conv)
        assert not log.exists() and not replay_store.store_path(isolated, 'local', conv).exists()
    finally:
        if restarted is not None:
            restarted.runtime.close()
            assert not restarted.runtime._heartbeat_thread.is_alive()
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)
