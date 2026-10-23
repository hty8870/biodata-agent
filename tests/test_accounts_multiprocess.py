"""真实子进程的凭据撤销、读改写和原子额度；不使用缓存 reset 或 TTL 代替证据。"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from dataset_recommender.app import accounts, mcp_tokens, llm_quota


def wait_for(path, child):
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        assert child.poll() is None, child.communicate()[1].decode()
        time.sleep(.01)
    assert path.exists()


def spawn(code, paths, data=None):
    child = subprocess.Popen([sys.executable, '-B', '-c', code, *map(str, paths)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if data is not None:
        child.stdin.write(json.dumps(data).encode())
        child.stdin.close()
        child.stdin = None
    return child


def finish(child):
    out, err = child.communicate(timeout=15)
    assert child.returncode == 0, err.decode('utf-8', 'replace')
    return out.decode().strip()


def test_cross_process_token_revocation_and_stale_writer(tmp_path):
    path, ready, release = [tmp_path / name for name in ('tokens.json', 'ready', 'release')]
    token, record = mcp_tokens.mint_token('account-a', 'alice', store_path=path)
    code = '''import sys,json,time
from pathlib import Path
from dataset_recommender.app import mcp_tokens as m
path,ready,release=map(Path,sys.argv[1:])
data=json.loads(sys.stdin.read())
assert m.resolve_token(data['token'],store_path=path)
ready.touch()
while not release.exists():time.sleep(.01)
assert m.resolve_token(data['token'],store_path=path) is None
m.mint_token('account-b','bob',store_path=path)
assert m.resolve_token(data['token'],store_path=path) is None
'''
    child = spawn(code, [path, ready, release], {'token': token})
    try:
        wait_for(ready, child)
        assert mcp_tokens.revoke_token('account-a', record['token_id'], store_path=path)
        mcp_tokens.mint_token('account-c', 'carol', store_path=path)
        release.touch()
        finish(child)
        assert mcp_tokens.resolve_token(token, store_path=path) is None
        assert len(mcp_tokens.list_tokens('account-b', store_path=path)) == 1
        assert len(mcp_tokens.list_tokens('account-c', store_path=path)) == 1
    finally:
        if child.poll() is None:
            child.terminate(); child.wait(timeout=5)


def test_cross_process_session_logout_and_stale_writer(tmp_path):
    path, ready, release = [tmp_path / name for name in ('sessions.json', 'ready', 'release')]
    token = accounts.create_session(accounts.PublicUser('account-a', 'alice'), sessions_path=path)
    code = '''import sys,json,time
from pathlib import Path
from dataset_recommender.app import accounts as a
path,ready,release=map(Path,sys.argv[1:])
token=json.loads(sys.stdin.read())['token']
assert a.resolve_session(token,sessions_path=path)
ready.touch()
while not release.exists():time.sleep(.01)
assert a.resolve_session(token,sessions_path=path) is None
a.create_session(a.PublicUser('account-b','bob'),sessions_path=path)
assert a.resolve_session(token,sessions_path=path) is None
'''
    child = spawn(code, [path, ready, release], {'token': token})
    try:
        wait_for(ready, child)
        accounts.destroy_session(token, sessions_path=path)
        accounts.create_session(accounts.PublicUser('account-c', 'carol'), sessions_path=path)
        release.touch()
        finish(child)
        assert accounts.resolve_session(token, sessions_path=path) is None
        data = json.loads(path.read_text())['sessions']
        assert accounts.session_token_digest(token) not in data
        assert {r['username'] for r in data.values()} == {'bob', 'carol'}
    finally:
        if child.poll() is None:
            child.terminate(); child.wait(timeout=5)


def test_cross_process_quota_exact_limit(tmp_path):
    path, release = tmp_path / 'quota.json', tmp_path / 'release'
    code = '''import sys,time
from pathlib import Path
from dataset_recommender.app import llm_quota as q
path,release=map(Path,sys.argv[1:])
while not release.exists():time.sleep(.01)
success=0
for n in range(15):
    try:q.check_and_increment(path.parent,'alice',trial=False,per_user_limit=7,global_limit=7,quota_path=path);success+=1
    except q.QuotaExceeded:pass
print(success)
'''
    children = [spawn(code, [path, release]) for _ in range(4)]
    try:
        release.touch()
        assert sum(int(finish(child)) for child in children) == 7
        assert llm_quota.usage_snapshot(tmp_path, 'alice', trial=False, quota_path=path) == 7
        bucket = json.loads(path.read_text())['days'][llm_quota._utc_day()]
        assert bucket['global'] == bucket['u:alice'] == 7
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate(); child.wait(timeout=5)


def test_cross_process_register_keeps_both_users(tmp_path):
    path, release = tmp_path / 'accounts.json', tmp_path / 'release'
    code = '''import sys,time
from pathlib import Path
from dataset_recommender.app import accounts as a
path,release=map(Path,sys.argv[1:3])
while not release.exists():time.sleep(.01)
a.register(sys.argv[3],'synthetic-password',store_path=path)
'''
    children = [spawn(code, [path, release, name]) for name in ('alice', 'bob')]
    try:
        release.touch()
        for child in children:finish(child)
        assert set(json.loads(path.read_text())['users']) == {'alice', 'bob'}
    finally:
        for child in children:
            if child.poll() is None:child.terminate();child.wait(timeout=5)


def test_quota_storage_failure_remains_explicit_fail_open(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(llm_quota, '_save', lambda *a: (_ for _ in ()).throw(OSError('synthetic')))
    llm_quota.check_and_increment(tmp_path, 'alice', trial=True, per_user_limit=1, global_limit=1, quota_path=tmp_path / 'quota.json')
    assert '计数存储故障，本次放行' in caplog.text
