"""高层 MCP 真协议、跨端所属会话与旧工具安全档；不调用真实模型。"""
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def test_real_stdio_chat_retry_restart_and_replay(tmp_path, monkeypatch):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = dict(os.environ)
    env.update(BIODATA_DATA_ROOT=str(tmp_path), BIODATA_RUNS_DB=str(tmp_path / 'runs.sqlite'),
               BIODATA_WORKSPACE_DB=str(tmp_path / 'ws.sqlite'), BIODATA_MCP_CALL_LOG='off',
               AGENT_TRACE='off', ENABLE_LLM='false', MOCK_LLM='false')
    args = StdioServerParameters(command=sys.executable, args=['-B', str(ROOT / 'src/dataset_recommender/app/mcp_server.py')], env=env)
    async def session_round(conv=None):
        async with stdio_client(args) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                names = {tool.name for tool in (await client.list_tools()).tools}
                assert {'agent_chat', 'agent_conversations', 'agent_task', 'agent_projects', 'agent_replay'} <= names
                tool = await client.call_tool('agent_chat', {'message': '人类肺癌的单细胞数据', 'request_id': 'stdio-first', 'wait_seconds': 20})
                assert not tool.isError, tool.content
                first = json.loads(tool.content[0].text)
                deadline = asyncio.get_running_loop().time() + 90
                while first['result'] is None and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(.2)
                    status = await client.call_tool('agent_task', {'run_id': first['run']['id']})
                    first['run'] = json.loads(status.content[0].text)['run']
                    first['result'] = first['run']['result']
                assert first['result'] is not None, first['run']
                if conv:
                    assert first['conversation_id'] == conv
                assert first['result']['shared'] and first['result']['result_payload']['result_total'] > 0
                history = await client.call_tool('agent_conversations', {'action': 'get', 'conversation_id': first['conversation_id']})
                snapshot = json.loads(history.content[0].text)['conversation']
                assert snapshot['message_count'] == 2 and snapshot['frame_count'] == 1
                replay = await client.call_tool('agent_replay', {'conversation_id': first['conversation_id'], 'action': 'status'})
                assert json.loads(replay.content[0].text)['n_turns'] == 1
                bad = await client.call_tool('agent_conversations', {'action': 'get', 'conversation_id': 'unknown'})
                assert bad.isError
                status = await client.call_tool('biodata_status', {})
                assert not status.isError
                return first['conversation_id']
    conv = asyncio.run(session_round())
    asyncio.run(session_round(conv))


def test_online_high_agent_web_continuation_and_quota(tmp_path, monkeypatch):
    from dataset_recommender.app import accounts, mcp_server, mcp_tokens, webapp, conversation_service
    from dataset_recommender.app.mcp_agent_tools import _SERVICES
    from dataset_recommender.llm.llm_client import LLMConfig, should_use_llm
    paths = SimpleNamespace(data_root=tmp_path)
    monkeypatch.setattr(mcp_server, 'get_app_paths', lambda: paths)
    monkeypatch.setattr(webapp, 'PROJECT_ROOT', tmp_path)
    for name, file in [('BIODATA_ACCOUNTS_FILE', 'accounts.json'), ('BIODATA_SESSIONS_FILE', 'sessions.json'),
                       ('BIODATA_LLM_QUOTA_FILE', 'quota.json'), ('BIODATA_MCP_TOKENS_FILE', 'tokens.json'),
                       ('BIODATA_WORKSPACE_DB', 'workspace.sqlite')]:
        monkeypatch.setenv(name, str(tmp_path / file))
    monkeypatch.setenv('BIODATA_REQUIRE_ACCOUNT', '1')
    monkeypatch.setenv('BIODATA_INVITE_CODE', 'test-invite')
    monkeypatch.setenv('BIODATA_LLM_DAILY_PER_USER', '2')
    monkeypatch.setenv('BIODATA_LLM_DAILY_GLOBAL', '10')
    monkeypatch.setenv('AGENT_TRACE', 'off')
    cfg = LLMConfig(enable_llm=True, mock_llm=False, provider='openai-compatible', api_key='synthetic-key')
    monkeypatch.setattr(conversation_service, 'materialize', lambda **kwargs: cfg)
    monkeypatch.setattr(webapp, 'load_llm_config', lambda **kwargs: cfg)
    calls = []
    def route(project, text, **kwargs):
        assert should_use_llm(kwargs['config'])[0], 'high tool retained old force_llm_off'
        calls.append((text, kwargs))
        return {'route': 'none', 'query': '', 'plan': {'report_zh': 'reply', 'steps': []}, 'echo_zh': '', 'retrieval': None, 'via': 'agent'}
    monkeypatch.setattr(conversation_service, 'route_with_project', route)
    accounts._reset_state_for_tests()
    mcp_tokens._reset_state_for_tests()
    def post(client, token, name, arguments):
        response = client.post('/mcp', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'},
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
        assert response.status_code == 200, response.text
        return response.json()['result']
    try:
        with TestClient(webapp.app, base_url='http://127.0.0.1') as client:
            assert client.post('/api/account/register', json={'username': 'alice', 'password': 'password12', 'invite_code': 'test-invite'}).status_code == 200
            user = client.get('/api/account/whoami').json()['user']
            token, _ = mcp_tokens.mint_token(user['id'], 'alice', store_path=tmp_path / 'tokens.json')
            first = post(client, token, 'agent_chat', {'message': '第一轮', 'request_id': 'online-first', 'wait_seconds': 10})
            assert not first.get('isError'), first
            body = json.loads(first['content'][0]['text'])
            conv = body['conversation_id']
            assert client.get('/api/conversations/' + conv).json()['conversation']['message_count'] == 2
            second = client.post('/api/utterance', json={'shared': True, 'conv_id': conv, 'req_id': 'web-second', 'expected_revision': 1, 'utterance': '第二轮'})
            assert second.status_code == 200, second.text
            assert second.json()['revision'] == 2
            assert len(calls[-1][1]['conversation_history']) == 2
            retry = post(client, token, 'agent_chat', {'message': '第一轮', 'request_id': 'online-first', 'wait_seconds': 0})
            assert not retry.get('isError') and len(calls) == 2
            denied = post(client, token, 'agent_chat', {'message': '第三轮', 'conversation_id': conv, 'expected_revision': 2, 'request_id': 'denied'})
            assert denied.get('isError') and 'llm_quota_exceeded' in denied['content'][0]['text']
            assert len(calls) == 2
            low = post(client, token, 'recommend_datasets', {'query': '人类肺', 'use_llm': True})
            assert low.get('isError') and 'online_llm_disabled' in low['content'][0]['text']
            other, _ = mcp_tokens.mint_token('other-account', 'bob', store_path=tmp_path / 'tokens.json')
            hidden = post(client, other, 'agent_conversations', {'action': 'get', 'conversation_id': conv})
            assert hidden.get('isError') and 'conversation_not_found' in hidden['content'][0]['text']
            hidden_replay = post(client, other, 'agent_replay', {'conversation_id': conv})
            assert hidden_replay.get('isError')
    finally:
        for key in list(_SERVICES):
            app = _SERVICES.pop(key)
            app.runtime.close()
        mcp_tokens._reset_state_for_tests()
        accounts._reset_state_for_tests()


def _project_protocol_params(tmp_path):
    from mcp import StdioServerParameters
    env = dict(os.environ)
    env.update(BIODATA_DATA_ROOT=str(tmp_path), BIODATA_WORKSPACE_DB=str(tmp_path / 'projects.sqlite'),
        BIODATA_REQUIRE_ACCOUNT='0', BIODATA_MCP_CALL_LOG='off', AGENT_TRACE='off', ENABLE_LLM='false', MOCK_LLM='false')
    env.pop('BIODATA_MCP_ACCESS_TOKEN', None)
    return StdioServerParameters(command=sys.executable, args=['-B', str(ROOT / 'src/dataset_recommender/app/mcp_server.py')], env=env)


def test_real_mcp_project_rename_preserves_explicit_color(tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    from dataset_recommender.app.workspace_store import DEFAULT_PROJECT_COLOR
    async def protocol():
        async with stdio_client(_project_protocol_params(tmp_path)) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                async def project(arguments):
                    reply = await client.call_tool('agent_projects', arguments)
                    assert not reply.isError, reply.content
                    return json.loads(reply.content[0].text)['project']
                red = await project({'action': 'create', 'name': 'red project', 'color': '#ef4444'})
                renamed = await project({'action': 'update', 'project_id': red['id'], 'name': 'renamed project'})
                assert renamed['name'] == 'renamed project' and renamed['color'] == '#ef4444'
                default = await project({'action': 'create', 'name': 'default project'})
                assert default['color'] == DEFAULT_PROJECT_COLOR
    asyncio.run(protocol())


def test_real_mcp_http_conversation_project_projection_matches(tmp_path, monkeypatch):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    from dataset_recommender.app import webapp
    monkeypatch.setattr(webapp, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setenv('BIODATA_WORKSPACE_DB', str(tmp_path / 'projects.sqlite'))
    monkeypatch.setenv('BIODATA_REQUIRE_ACCOUNT', '0')
    with TestClient(webapp.app, base_url='http://127.0.0.1') as http:
        async def protocol():
            async with stdio_client(_project_protocol_params(tmp_path)) as (reader, writer):
                async with ClientSession(reader, writer) as client:
                    await client.initialize()
                    async def call(name, arguments):
                        reply = await client.call_tool(name, arguments)
                        assert not reply.isError, reply.content
                        return json.loads(reply.content[0].text)
                    project = (await call('agent_projects', {'action': 'create', 'name': 'project'}))['project']
                    conversation = (await call('agent_conversations', {'action': 'create', 'title': 'conversation'}))['conversation']
                    await call('agent_projects', {'action': 'assign', 'project_id': project['id'],
                        'conversation_id': conversation['id'], 'expected_revision': conversation['revision']})
                    mcp_get = (await call('agent_conversations', {'action': 'get', 'conversation_id': conversation['id']}))['conversation']
                    mcp_list = (await call('agent_conversations', {'action': 'list', 'limit': 1}))['items'][0]
                    http_get = http.get('/api/conversations/' + conversation['id']).json()['conversation']
                    http_list = http.get('/api/conversations', params={'limit': 1}).json()['items'][0]
                    assert mcp_get.get('project_id') == mcp_list.get('project_id') == project['id']
                    assert http_get['project_id'] == http_list['project_id'] == project['id']
                    assert mcp_get == http_get and mcp_list == http_list
                    blank = (await call('agent_conversations', {'action': 'create', 'title': 'unassigned'}))['conversation']
                    blank_get = (await call('agent_conversations', {'action': 'get', 'conversation_id': blank['id']}))['conversation']
                    assert blank_get['project_id'] == ''
                    created = (await call('agent_conversations', {'action': 'create', 'title': 'in project', 'project_id': project['id']}))['conversation']
                    assert created['project_id'] == project['id']
                    imported = (await call('agent_conversations', {'action': 'import', 'import_id': 'protocol-project-import',
                        'project_id': project['id'], 'data': {'messages': [{'role': 'user', 'text': 'message'}],
                            'frames': [{'payload': {'results': []}, 'state': {}, 'message_boundary': 1}]}}))['conversation']
                    assert imported['project_id'] == project['id']
                    branch = (await call('agent_conversations', {'action': 'branch', 'conversation_id': imported['id'],
                        'frame_id': 'f_1', 'expected_revision': imported['revision'], 'project_id': project['id']}))['conversation']
                    assert branch['project_id'] == project['id']
                    assert http.get('/api/conversations/' + branch['id']).json()['conversation']['project_id'] == project['id']
        asyncio.run(protocol())


def test_high_tools_dispatch_off_loop_with_transport_identity(tmp_path, monkeypatch):
    import threading
    import time
    import httpx
    from fastapi import FastAPI
    from mcp.server.fastmcp import FastMCP
    from dataset_recommender.app import mcp_agent_tools as tools
    from dataset_recommender.corpus.patch_package import bind_patch_scope, current_patch_scope
    entered = threading.Event()
    observed = []
    def slow_replay(owner, *args, **kwargs):
        observed.append((owner, tools._IDENTITY.get(), current_patch_scope(), threading.get_ident()))
        entered.set()
        time.sleep(.25)
        return {'ok': True}
    monkeypatch.setattr(tools, 'service_for', lambda root: SimpleNamespace(replay=slow_replay))
    mcp = FastMCP('transport')
    tools.register_agent_tools(mcp, root=lambda: tmp_path)
    app = FastAPI()
    @app.get('/api/health')
    async def health():
        return {'ok': True}
    async def protocol():
        main_thread = threading.get_ident()
        with tools.bind_identity('trusted-account', 'alice'), bind_patch_scope('trusted-account'):
            task = asyncio.create_task(mcp.call_tool('agent_replay', {'conversation_id': 'c1'}))
            deadline = asyncio.get_running_loop().time() + 2
            while not entered.is_set() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.005)
            assert entered.is_set()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                assert not task.done(), 'synchronous MCP tool blocked the HTTP loop'
                assert (await client.get('/api/health')).json()['ok']
            await task
        assert observed[0][:3] == ('trusted-account', ('trusted-account', 'alice'), 'trusted-account')
        assert observed[0][3] != main_thread
    asyncio.run(protocol())


def test_mcp_learning_control_trigger_capture_config_epoch(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP
    from dataset_recommender.app import mcp_agent_tools as tools, learning_service
    from dataset_recommender.llm.llm_client import LLMConfig
    seen = []
    cfg = LLMConfig(enable_llm=False)
    from dataset_recommender.agent.agent_exec import _endpoint_fp_from_config
    fp = _endpoint_fp_from_config(cfg)
    class Learning:
        def __init__(self):
            self.store = self
        def authorization_snapshot(self, owner):
            seen.append(('capture', owner))
            return {'config_epochs': {fp: 7}, 'scope_epochs': {(fp, ''): 3}}
        def control(self, owner, cfg, *, expected_config_epoch, **kwargs):
            assert kwargs['expected_scope_epoch'] == 3 and kwargs['expected_identity'] == owner
            seen.append(('control', owner, expected_config_epoch))
            return {'ok': True}
        def trigger(self, owner, cfg, *, expected_config_epoch, **kwargs):
            assert kwargs['expected_scope_epoch'] == 3 and kwargs['expected_identity'] == owner
            seen.append(('trigger', owner, expected_config_epoch))
            return {'ok': True}
    monkeypatch.setattr(learning_service, 'service_for', lambda *args: Learning())
    monkeypatch.setattr(tools, 'service_for', lambda root: SimpleNamespace(store=None, workspace=None))
    def materialize(**kwargs):
        assert seen[-1] == ('capture', 'trusted-account'), 'snapshot must precede credential materialization'
        return cfg
    monkeypatch.setattr('dataset_recommender.app.llm_admission.materialize', materialize)
    mcp = FastMCP('epoch')
    tools.register_agent_tools(mcp, root=lambda: tmp_path)
    async def protocol():
        for action in ('control', 'trigger'):
            with tools.bind_identity('trusted-account'):
                await mcp.call_tool('agent_learning', {'action': action, 'enabled': True})
    asyncio.run(protocol())
    assert seen == [('capture', 'trusted-account'), ('control', 'trusted-account', 7),
                    ('capture', 'trusted-account'), ('trigger', 'trusted-account', 7)]


def test_six_high_tools_share_bounded_async_dispatch(tmp_path, monkeypatch):
    import threading
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from dataset_recommender.app import mcp_agent_tools as tools
    lock = threading.Lock()
    release = threading.Event()
    entered = threading.Event()
    active = peak = 0
    def slow(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(active, peak)
            if active == 6:
                entered.set()
        try:
            assert release.wait(5), 'test did not release the slow service'
            return {'ok': True}
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(tools, 'service_for', lambda root: SimpleNamespace(replay=slow))
    mcp = FastMCP('bounded')
    tools.register_agent_tools(mcp, root=lambda: tmp_path)
    assert all(mcp._tool_manager.get_tool(name).is_async for name in tools.AGENT_TOOLS)
    async def protocol():
        requests = [asyncio.create_task(mcp.call_tool('agent_replay', {'conversation_id': 'c1'})) for _ in range(25)]
        try:
            deadline = asyncio.get_running_loop().time() + 2
            while not entered.is_set() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.005)
            assert entered.is_set()
            await asyncio.sleep(.05)
            rejected = [request.exception() for request in requests if request.done()]
            assert len(rejected) == 19, 'excess requests must be refused, not left queued'
            assert all(isinstance(error, ToolError) and 'mcp_transport_busy:' in str(error) for error in rejected)
            for name, arguments in (
                ('agent_chat', {'message': 'question', 'wait_seconds': 0}),
                ('agent_conversations', {}), ('agent_projects', {}), ('agent_learning', {}),
            ):
                try:
                    await mcp.call_tool(name, arguments)
                except ToolError as exc:
                    assert 'mcp_transport_busy:' in str(exc)
                else:
                    raise AssertionError(f'{name} bypassed the shared slow-call admission')
        finally:
            release.set()
            results = await asyncio.gather(*requests, return_exceptions=True)
        assert sum(not isinstance(result, BaseException) for result in results) == 6
        assert sum(isinstance(result, ToolError) for result in results) == 19
        assert not isinstance(await mcp.call_tool('agent_replay', {'conversation_id': 'c1'}), BaseException)
    asyncio.run(protocol())
    assert 1 < peak <= 8 and active == 0
    assert peak == 6


def test_mcp_task_control_slots_are_reserved_and_bounded(tmp_path, monkeypatch):
    import threading
    import pytest
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from dataset_recommender.app import mcp_agent_tools as tools
    release = threading.Event()
    lock = threading.Lock()
    active = peak = 0
    def slow(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(active, peak)
        try:
            assert release.wait(5)
            return {'ok': True}
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(tools, 'service_for', lambda root: SimpleNamespace(replay=slow, store=SimpleNamespace(load=slow)))
    mcp = FastMCP('control')
    tools.register_agent_tools(mcp, root=lambda: tmp_path)
    async def protocol():
        requests = [asyncio.create_task(mcp.call_tool('agent_replay', {'conversation_id': 'c1'})) for _ in range(6)]
        extra = None
        try:
            deadline = asyncio.get_running_loop().time() + 2
            while active < 6 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.005)
            assert active == 6
            requests.extend(asyncio.create_task(mcp.call_tool('agent_task', {'run_id': 'r1'})) for _ in range(2))
            deadline = asyncio.get_running_loop().time() + 2
            while active < 8 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.005)
            assert active == 8
            extra = asyncio.create_task(mcp.call_tool('agent_task', {'action': 'cancel', 'run_id': 'r1'}))
            with pytest.raises(ToolError, match='mcp_transport_busy:'):
                await asyncio.wait_for(asyncio.shield(extra), .5)
        finally:
            release.set()
            results = await asyncio.gather(*requests, *([extra] if extra else []), return_exceptions=True)
        assert all(not isinstance(result, BaseException) for result in results[:8])
        for _ in range(3):
            with pytest.raises(ToolError, match='invalid_task_action'):
                await mcp.call_tool('agent_task', {'action': 'invalid'})
        await mcp.call_tool('agent_task', {'run_id': 'r1'})
    asyncio.run(protocol())
    assert peak == 8 and active == 0


def test_mcp_cancelled_wait_holds_capacity_until_worker_finishes(tmp_path, monkeypatch):
    import threading
    import pytest
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from dataset_recommender.app import mcp_agent_tools as tools
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    def slow(*args, **kwargs):
        nonlocal active
        with lock:
            active += 1
        try:
            assert release.wait(5)
            return {'ok': True}
        finally:
            with lock:
                active -= 1
    monkeypatch.setattr(tools, 'service_for', lambda root: SimpleNamespace(replay=slow))
    mcp = FastMCP('cancel-wait')
    tools.register_agent_tools(mcp, root=lambda: tmp_path)
    async def protocol():
        requests = [asyncio.create_task(mcp.call_tool('agent_replay', {'conversation_id': 'c1'})) for _ in range(6)]
        extra = None
        try:
            deadline = asyncio.get_running_loop().time() + 2
            while active < 6 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.005)
            assert active == 6
            requests[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await requests[0]
            assert active == 6
            extra = asyncio.create_task(mcp.call_tool('agent_replay', {'conversation_id': 'c1'}))
            with pytest.raises(ToolError, match='mcp_transport_busy:'):
                await asyncio.wait_for(asyncio.shield(extra), .5)
        finally:
            release.set()
            await asyncio.gather(*requests, *([extra] if extra else []), return_exceptions=True)
            deadline = asyncio.get_running_loop().time() + 2
            while active and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.005)
            assert active == 0
            await asyncio.sleep(.01)
        await mcp.call_tool('agent_replay', {'conversation_id': 'c1'})
    asyncio.run(protocol())


def test_mcp_chat_wait_reuses_runtime_transport_limit(tmp_path, monkeypatch):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from dataset_recommender.app import mcp_agent_tools as tools
    from dataset_recommender.app.conversation_service import ConversationService
    app = ConversationService(tmp_path)
    pending = {'id': 'r_pending', 'conversation_id': 'c1', 'status': 'queued', 'result': None}
    started = []
    def chat(*args, **kwargs):
        started.append(True)
        return dict(pending)
    monkeypatch.setattr(app, 'chat', chat)
    monkeypatch.setattr(app.store, 'load', lambda *args: dict(pending))
    monkeypatch.setattr(tools, 'service_for', lambda root: app)
    mcp = FastMCP('wait')
    tools.register_agent_tools(mcp, root=lambda: tmp_path)
    async def protocol():
        results = await asyncio.gather(*(mcp.call_tool('agent_chat', {'message': 'question', 'wait_seconds': .2})
            for _ in range(3)), return_exceptions=True)
        errors = [r for r in results if isinstance(r, ToolError)]
        assert len(errors) == 1 and 'run_transport_capacity' in str(errors[0])
        assert len(started) == 2, 'transport refusal must happen before starting a task'
        assert app.runtime._waiters == {}
    try:
        asyncio.run(protocol())
    finally:
        app.runtime.close()


def test_real_mcp_http_replay_slow_call_leaves_web_health_responsive(tmp_path, monkeypatch):
    import httpx
    import threading
    import time
    from dataset_recommender.app import mcp_server, mcp_tokens, webapp, replay_service, mcp_agent_tools
    from dataset_recommender.app.conversation_service import ConversationService
    from dataset_recommender.app.mcp_agent_tools import _SERVICES
    from dataset_recommender.llm.llm_client import LLMConfig
    monkeypatch.setenv('BIODATA_MCP_TOKENS_FILE', str(tmp_path / 'tokens.json'))
    monkeypatch.setenv('BIODATA_WORKSPACE_DB', str(tmp_path / 'workspace.sqlite'))
    monkeypatch.setenv('BIODATA_REQUIRE_ACCOUNT', '0')
    monkeypatch.setattr(webapp, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(mcp_server, 'get_app_paths', lambda: SimpleNamespace(data_root=tmp_path))
    monkeypatch.setenv('ENABLE_LLM', 'true')
    monkeypatch.setenv('MOCK_LLM', 'false')
    monkeypatch.setenv('LLM_API_KEY', 'synthetic-key')
    entered = threading.Event()
    release = threading.Event()
    run_entered = threading.Event()
    observed = []
    lock = threading.Lock()
    def slow_network(*args):
        with lock:
            observed.append(mcp_agent_tools._IDENTITY.get())
            if len(observed) == 6:
                entered.set()
        assert release.wait(5), 'test did not release replay'
        return 'synthetic reply'
    def execute(emit):
        run_entered.set()
        assert release.wait(5), 'test did not release the conversation run'
        return {'ok': True}
    monkeypatch.setattr(replay_service, '_call_text', slow_network)
    app = ConversationService(tmp_path)
    monkeypatch.setattr(mcp_agent_tools, 'service_for', lambda root: app)
    conv = app.conversation_action('http-account', '', 'create')['conversation']['id']
    replay_service.record_turn(tmp_path, 'http-account', conv, '', 'chat', {'user': 'turn'}, LLMConfig(enable_llm=False))
    token, _ = mcp_tokens.mint_token('http-account', 'alice', store_path=tmp_path / 'tokens.json')
    run = app.runtime.submit('http-account', 'saturated-cancel', 'synthetic-fingerprint', prepare=lambda: execute)
    async def protocol():
        async with webapp.app.router.lifespan_context(webapp.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url='http://127.0.0.1') as client:
                async def call(request_id, name, arguments):
                    return await client.post('/mcp', headers={'Authorization': 'Bearer ' + token,
                        'Accept': 'application/json, text/event-stream'}, json={'jsonrpc': '2.0', 'id': request_id,
                        'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
                requests = [asyncio.create_task(call(index, 'agent_replay',
                    {'conversation_id': conv, 'action': 'chat', 'data': {'text': 'question'}})) for index in range(8)]
                control = extra = None
                try:
                    deadline = asyncio.get_running_loop().time() + 3
                    while (not entered.is_set() or not run_entered.is_set() or
                           len(observed) + sum(request.done() for request in requests) < 8) and asyncio.get_running_loop().time() < deadline:
                        await asyncio.sleep(.005)
                    request = requests[0]
                    assert entered.is_set() and run_entered.is_set() and not request.done()
                    before = time.monotonic()
                    response = await client.get('/api/health')
                    assert response.status_code == 200 and response.json()['ok']
                    assert time.monotonic() - before < .3 and not request.done()
                    control = asyncio.create_task(call(100, 'agent_task', {'action': 'cancel', 'run_id': run['id']}))
                    cancelled = await asyncio.wait_for(asyncio.shield(control), .5)
                    assert cancelled.status_code == 200 and not cancelled.json()['result'].get('isError')
                    assert json.loads(cancelled.json()['result']['content'][0]['text'])['run']['cancel_requested']
                    assert app.store.load('http-account', run['id'])['cancel_requested']
                    assert app.runtime._tasks[('http-account', run['id'])][0].is_set()
                    assert not release.is_set() and not request.done(), 'cancel must not wait for replay to finish'
                    extra = asyncio.create_task(call(101, 'agent_replay', {'conversation_id': conv}))
                    denied = await asyncio.wait_for(asyncio.shield(extra), .5)
                    assert denied.status_code == 200 and denied.json()['result']['isError']
                    assert 'mcp_transport_busy:' in denied.json()['result']['content'][0]['text']
                finally:
                    release.set()
                    replies = await asyncio.gather(*requests, *(task for task in (control, extra) if task is not None), return_exceptions=True)
                assert all(reply.status_code == 200 for reply in replies)
                successful = [reply for reply in replies[:8] if not reply.json()['result'].get('isError')]
                assert len(successful) == 6
                for replay in successful:
                    assert replay.status_code == 200 and not replay.json()['result'].get('isError')
                refused = [reply for reply in replies[:8] if reply.json()['result'].get('isError')]
                assert len(refused) == 2
                assert all('mcp_transport_busy:' in reply.json()['result']['content'][0]['text'] for reply in refused)
                assert observed == [('http-account', 'alice')] * 6
    try:
        asyncio.run(protocol())
    finally:
        release.set()
        app.runtime.close()
        for key in list(_SERVICES):
            _SERVICES.pop(key).runtime.close()


def test_mcp_anyio_cancel_scope_withdraws_before_worker_execution(tmp_path, monkeypatch):
    import anyio
    import threading
    from mcp.server.fastmcp import FastMCP
    from dataset_recommender.app import mcp_agent_tools as tools
    monkeypatch.setenv('BIODATA_WORKSPACE_DB', str(tmp_path / 'workspace.sqlite'))
    app = tools.service_for(tmp_path)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    real_run_sync = anyio.to_thread.run_sync
    async def before_execute(operation, *args, **kwargs):
        def held():
            entered.set()
            try:
                assert release.wait(5)
                return operation(*args)
            finally:
                finished.set()
        return await real_run_sync(held, **kwargs)
    monkeypatch.setattr(anyio.to_thread, 'run_sync', before_execute)
    mcp = FastMCP('scope-withdrawal')
    tools.register_agent_tools(mcp, root=lambda: tmp_path)
    async def protocol():
        scopes, done = [], anyio.Event()
        async def request():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                with tools.bind_identity('trusted-account', 'alice'):
                    await mcp.call_tool('agent_conversations', {'action': 'create', 'title': 'must not exist'})
            done.set()
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(request)
                with anyio.fail_after(2):
                    while not entered.is_set():
                        await anyio.sleep(.005)
                scopes[0].cancel()
                with anyio.move_on_after(.2):
                    await done.wait()
                withdrew_before_execute = done.is_set()
                release.set()
                with anyio.fail_after(2):
                    await done.wait()
                    while not finished.is_set():
                        await anyio.sleep(.005)
                assert withdrew_before_execute, 'real CancelScope must withdraw before the execution barrier opens'
                assert app.list_conversations('trusted-account')['items'] == []
                with tools.bind_identity('trusted-account', 'alice'):
                    await mcp.call_tool('agent_conversations', {'action': 'list'})
        finally:
            release.set()
    try:
        anyio.run(protocol)
    finally:
        release.set()
        app.runtime.close()


def test_mcp_learning_sources_trigger_params_and_knowledge_face(tmp_path, monkeypatch):
    """MCP agent_learning 新面：sources 读面、trigger 的固定选择/manual 参数透传、
    知识 entries/changes/propose/assess 暴露；commit 永不在 MCP 开放。"""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from dataset_recommender.app import mcp_agent_tools as tools, learning_service
    from dataset_recommender.llm.llm_client import LLMConfig
    from dataset_recommender.agent.agent_exec import _endpoint_fp_from_config
    seen = []
    cfg = LLMConfig(enable_llm=False)
    fp = _endpoint_fp_from_config(cfg)

    class Knowledge:
        def entries(self, owner, endpoint_fp, project_id='', **kwargs):
            seen.append(('entries', owner, endpoint_fp, project_id, kwargs))
            return {'ok': True, 'items': []}
        def changes(self, owner, endpoint_fp, project_id='', **kwargs):
            seen.append(('changes', owner, project_id, kwargs))
            return {'ok': True, 'items': []}
        def propose(self, owner, endpoint_fp, project_id='', **kwargs):
            seen.append(('propose', owner, project_id, kwargs))
            return {'ok': True, 'stage': 'draft', 'change_id': 'kc_1', 'change_revision': 1,
                    'change_fingerprint': 'fp', 'catalog_generation': 0, 'assessment_id': None}
        def assess(self, owner, endpoint_fp, project_id='', **kwargs):
            seen.append(('assess', owner, project_id, sorted(kwargs.items(), key=lambda item: item[0])))
            return {'ok': True, 'stage': 'pending'}

    class Learning:
        knowledge = Knowledge()
        def __init__(self):
            self.store = self
        def authorization_snapshot(self, owner):
            seen.append(('capture', owner))
            return {'config_epochs': {fp: 0}, 'scope_epochs': {}}
        def authorization(self, *args, **kwargs):
            return {'epoch': 0}, 0
        def sources(self, owner, endpoint_fp, project_id=None, **kwargs):
            seen.append(('sources', owner, project_id, kwargs))
            return {'ok': True, 'items': []}
        def trigger(self, owner, cfg, *, project_id='', **kwargs):
            seen.append(('trigger', project_id, {key: kwargs[key] for key in
                ('selection', 'source_ids', 'local_materials', 'manual', 'allow_cross_project')}))
            return {'ok': True, 'scheduled': False}

    monkeypatch.setattr(learning_service, 'service_for', lambda *args: Learning())
    monkeypatch.setattr(tools, 'service_for', lambda root: SimpleNamespace(store=None, workspace=None))
    monkeypatch.setattr('dataset_recommender.app.llm_admission.materialize', lambda **kwargs: cfg)
    mcp = FastMCP('learning-face')
    tools.register_agent_tools(mcp, root=lambda: tmp_path)

    async def protocol():
        with tools.bind_identity('trusted-account'):
            await mcp.call_tool('agent_learning', {'action': 'sources'})
            await mcp.call_tool('agent_learning', {'action': 'sources', 'project_id': 'p1'})
            await mcp.call_tool('agent_learning', {'action': 'entries', 'scope_level': 'project',
                                                   'kind': 'skill', 'status': 'retracted', 'project_id': 'p1'})
            await mcp.call_tool('agent_learning', {'action': 'changes', 'status': 'draft'})
            await mcp.call_tool('agent_learning', {'action': 'propose', 'request_id': 'r-1',
                'operations': [{'op': 'create', 'after': {'kind': 'memory', 'text': 'x'}}],
                'source_refs': [{'type': 'user', 'text': 'x'}]})
            await mcp.call_tool('agent_learning', {'action': 'assess', 'change_id': 'kc_1',
                                                   'expected_change_revision': 1})
            await mcp.call_tool('agent_learning', {'action': 'trigger', 'manual': True,
                'source_ids': ['s-1'], 'selection': [{'project_id': 'p1', 'conversation_ids': ['c1']}],
                'local_materials': [{'client_id': 'm1', 'text': '材料'}]})
            for denied in ({'action': 'commit', 'change_id': 'kc_1'},
                           {'action': 'propose'},
                           {'action': 'assess'}):
                try:
                    await mcp.call_tool('agent_learning', denied)
                    raise AssertionError(f'{denied} must be rejected')
                except ToolError as exc:
                    assert 'bad_param' in str(exc) or 'invalid_learning_action' in str(exc), str(exc)
    asyncio.run(protocol())
    assert ('sources', 'trusted-account', None, {'offset': 0, 'limit': 100}) in seen
    assert ('sources', 'trusted-account', 'p1', {'offset': 0, 'limit': 100}) in seen
    assert ('entries', 'trusted-account', fp, 'p1',
            {'scope_level': 'project', 'kind': 'skill', 'status': 'retracted', 'offset': 0, 'limit': 100}) in seen
    assert ('changes', 'trusted-account', '', {'status': 'draft', 'offset': 0, 'limit': 100}) in seen
    propose = next(row for row in seen if row[0] == 'propose')
    assert propose[3]['expected_identity'] == 'trusted-account'
    assert propose[3]['operations'][0]['op'] == 'create' and propose[3]['source_refs'][0]['type'] == 'user'
    assess = next(row for row in seen if row[0] == 'assess')
    assert ('expected_change_revision', 1) in assess[3]
    assert any(key == 'check' for key, _ in assess[3]) and any(key == 'before_llm' for key, _ in assess[3])
    trigger = next(row for row in seen if row[0] == 'trigger')
    assert trigger[2] == {'selection': [{'project_id': 'p1', 'conversation_ids': ['c1']}],
        'source_ids': ['s-1'], 'local_materials': [{'client_id': 'm1', 'text': '材料'}],
        'manual': True, 'allow_cross_project': False}, 'MCP 永不携带跨项目汇总授权'
