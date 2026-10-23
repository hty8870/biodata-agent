# -*- coding: utf-8 -*-
"""session_log 已知答案单测：落盘/读取回环、seq 递增、脱敏第一道工序、开关、坏行容错。"""
from __future__ import annotations

import json

import pytest

from dataset_recommender.app import session_log as S  # noqa: E402


@pytest.fixture(autouse=True)
def _session_log_on(monkeypatch):
    """离线验证 harness 会把 BIODATA_SESSION_LOG 钉成 off 做隔离；本文件专门测这套落痕机制，
    显式打开（测试内部要验证开关语义时再自己 setenv off）。不影响隔离防线。"""
    monkeypatch.setenv("BIODATA_SESSION_LOG", "on")


def _records(root, principal="u1", conv="c1"):
    return S.read_records(root, principal, conv)


def test_append_read_roundtrip_seq_increments(tmp_path):
    S.append_turn(tmp_path, "u1", "c1", "search", {"query": "肺癌 单细胞", "total": 3, "top": ["A", "B"]})
    S.append_turn(tmp_path, "u1", "c1", "chat", {"user": "前三个对比下", "reply": "好的", "steps": []})
    recs = _records(tmp_path)
    assert [r["seq"] for r in recs] == [1, 2]
    assert recs[0]["kind"] == "search" and recs[0]["payload"]["total"] == 3
    assert recs[1]["kind"] == "chat" and recs[1]["payload"]["user"] == "前三个对比下"


def test_redaction_is_first_step(tmp_path):
    fake_key = "sk" + "-" + "TESTFAKEKEY1234567890abcd"  # Synthetic value assembled for the redaction probe.
    S.append_turn(tmp_path, "u1", "c1", "chat", {"user": f"我的 key 是 {fake_key}", "reply": "收到", "steps": []})
    raw = S.log_path(tmp_path, "u1", "c1").read_text(encoding="utf-8")
    assert fake_key not in raw
    assert "[REDACTED:openai-secret-key]" in raw
    recs = _records(tmp_path)
    assert "REDACTED" in recs[0]["payload"]["user"]


def test_switch_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("BIODATA_SESSION_LOG", "off")
    S.append_turn(tmp_path, "u1", "c1", "search", {"query": "x"})
    assert not S.log_path(tmp_path, "u1", "c1").exists()
    assert _records(tmp_path) == []


def test_empty_conv_id_noop(tmp_path):
    S.append_turn(tmp_path, "u1", "", "search", {"query": "x"})  # 无 conv 不落痕
    assert _records(tmp_path) == []


def test_bad_lines_skipped(tmp_path):
    p = S.log_path(tmp_path, "u1", "c1")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        '{"seq": 1, "ts": 1, "kind": "search", "payload": {"query": "a"}}\n'
        "这不是 json\n"
        '{"seq": 2, "ts": 1, "kind": "chat"}\n'
        '{"seq": 3, "ts": 1, "kind": "chat", "payload": {"user": "b"}}\n',
        encoding="utf-8",
    )
    recs = _records(tmp_path)
    assert [r["seq"] for r in recs] == [1, 3]


def test_path_segments_sanitized(tmp_path):
    S.append_turn(tmp_path, "u/../../etc", "c1$%^", "search", {"query": "a"})
    p = S.log_path(tmp_path, "u/../../etc", "c1$%^")
    assert ".userdata" in str(p)
    assert ".." not in p.name and "$" not in p.name
    assert p.exists()


def test_principal_namespace_isolated(tmp_path):
    S.append_turn(tmp_path, "u1", "c1", "search", {"query": "a"})
    S.append_turn(tmp_path, "anonymous", "c1", "search", {"query": "b"})
    assert len(_records(tmp_path, "u1")) == 1
    assert len(_records(tmp_path, "anonymous")) == 1
    assert _records(tmp_path, "u1")[0]["payload"]["query"] == "a"


def test_seq_survives_counter_reset(tmp_path):
    """进程重启（路径锁缓存清空）后 seq 从持久最大值续编，不回绕。"""
    S.append_turn(tmp_path, "u1", "c1", "search", {"query": "a"})
    S._reset_for_tests()
    S.append_turn(tmp_path, "u1", "c1", "search", {"query": "b"})
    assert [r["seq"] for r in _records(tmp_path)] == [1, 2]
    S._reset_for_tests()


def test_record_turn_seq_across_three_alternating_processes(tmp_path):
    import os
    import subprocess
    import sys
    from dataset_recommender.app import replay_service
    from dataset_recommender.llm.llm_client import LLMConfig
    cfg = LLMConfig(enable_llm=False, mock_llm=False)
    replay_service.record_turn(tmp_path, 'u1', 'c1', 'key', 'chat', {'user': 'A1'}, cfg)
    code = '''import sys
from pathlib import Path
from dataset_recommender.app import replay_service
from dataset_recommender.llm.llm_client import LLMConfig
root=Path(sys.argv[1]);cfg=LLMConfig(enable_llm=False,mock_llm=False)
for line in sys.stdin:
    replay_service.record_turn(root,'u1','c1','key','chat',{'user':line.strip()},cfg)
    print('recorded',flush=True)
'''
    children = []
    errors = []
    try:
        for _ in range(2):
            children.append(subprocess.Popen([sys.executable, '-B', '-c', code, str(tmp_path)], env=dict(os.environ),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        for proc, label in ((children[0], 'B1'), (children[1], 'C1')):
            proc.stdin.write(label + '\n'); proc.stdin.flush()
            assert proc.stdout.readline().strip() == 'recorded'
        replay_service.record_turn(tmp_path, 'u1', 'c1', 'key', 'chat', {'user': 'A2'}, cfg)
        for proc, label in ((children[0], 'B2'), (children[1], 'C2')):
            proc.stdin.write(label + '\n'); proc.stdin.flush()
            assert proc.stdout.readline().strip() == 'recorded'
        assert [r['seq'] for r in _records(tmp_path)] == list(range(1, 7))
        assert [r['payload']['user'] for r in _records(tmp_path)] == ['A1', 'B1', 'C1', 'A2', 'B2', 'C2']
    finally:
        for proc in children:
            proc.stdin.close()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            errors.append(proc.stderr.read())
            proc.stdout.close()
            proc.stderr.close()
        for proc, error in zip(children, errors):
            assert proc.returncode == 0, error


@pytest.mark.parametrize("unterminated", [False, True])
def test_direct_append_parallel_processes_share_seq_lock(tmp_path, unterminated):
    import subprocess
    import sys
    before = b""
    if unterminated:
        path = S.log_path(tmp_path, 'u1', 'c1')
        path.parent.mkdir(parents=True, exist_ok=True)
        before = (b'{"seq": 1, "ts": 1, "kind": "chat", "payload": {}}\n'
                  b'{"seq": 2, "ts": 1, "kind": "chat", "payload": {}}')
        path.write_bytes(before)
    code = '''import sys
from pathlib import Path
from dataset_recommender.app.session_log import append_turn
for n in range(25):append_turn(Path(sys.argv[1]),'u1','c1','chat',{'n':n})
'''
    children = []
    try:
        for _ in range(3):
            children.append(subprocess.Popen([sys.executable, '-B', '-c', code, str(tmp_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for proc in children:
            _, error = proc.communicate(timeout=15)
            assert proc.returncode == 0, error.decode()
        if unterminated:
            assert path.read_bytes().startswith(before)
            assert [r['seq'] for r in _records(tmp_path)] == list(range(1, 78))
        else:
            assert [r['seq'] for r in _records(tmp_path)] == list(range(1, 76))
    finally:
        for proc in children:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            proc.stdout.close()
            proc.stderr.close()


def test_unreadable_persisted_seq_does_not_restart_numbering(tmp_path, monkeypatch):
    S.append_turn(tmp_path, 'u1', 'c1', 'chat', {'user': 'first'})
    path = S.log_path(tmp_path, 'u1', 'c1')
    before = path.read_bytes()
    def unreadable(path):
        raise PermissionError('synthetic read failure')
    with monkeypatch.context() as patch:
        patch.setattr(S, '_read_lines', unreadable)
        S.append_turn(tmp_path, 'u1', 'c1', 'chat', {'user': 'second'})
    assert path.read_bytes() == before


@pytest.mark.parametrize(("tail", "expected_seqs"), [
    (b'{"seq": 2, "ts": 1, "kind": "chat", "payload": {"user": "tail"}}', [1, 2, 3]),
    (b'{"seq": 2, "ts": 1, "kind": "chat", "paylo', [1, 2]),
    ('{"seq": 2, "ts": 1, "kind": "chat", "payload": {"user": "中'.encode('utf-8')[:-1], [1, 2]),
], ids=["complete-json", "half-json", "half-utf8"])
def test_unterminated_tail_sealed_before_seq_allocation(tmp_path, tail, expected_seqs):
    S.append_turn(tmp_path, 'u1', 'c1', 'chat', {'user': 'first'})
    path = S.log_path(tmp_path, 'u1', 'c1')
    before = path.read_bytes() + tail
    path.write_bytes(before)
    assert [r['seq'] for r in _records(tmp_path)] == [1]

    S.append_turn(tmp_path, 'u1', 'c1', 'chat', {'user': 'next'})
    assert path.read_bytes().startswith(before)
    recs = _records(tmp_path)
    assert [r['seq'] for r in recs] == expected_seqs
    assert recs[-1]['payload']['user'] == 'next'
    S.append_turn(tmp_path, 'u1', 'c1', 'chat', {'user': 'again'})
    assert [r['seq'] for r in _records(tmp_path)] == expected_seqs + [expected_seqs[-1] + 1]


@pytest.mark.parametrize("landed", ["none", "half-json", "half-utf8", "complete-json"])
def test_failed_append_retry_uses_sealed_persisted_seq(tmp_path, monkeypatch, capsys, landed):
    from contextlib import contextmanager

    S.append_turn(tmp_path, 'u1', 'c1', 'chat', {'user': 'first'})
    path = S.log_path(tmp_path, 'u1', 'c1')
    before = path.read_bytes()
    real_open = type(path).open

    @contextmanager
    def interrupted_append():
        with real_open(path, 'ab') as fh:
            class InterruptedWriter:
                def write(self, line):
                    raw = line.encode('utf-8')
                    if landed == 'none':
                        raw = b''
                    elif landed == 'half-json':
                        raw = raw[:len(raw) // 2]
                    elif landed == 'half-utf8':
                        raw = raw[:raw.index('中'.encode('utf-8')) + 1]
                    else:
                        raw = raw.rstrip(b'\r\n')
                    fh.write(raw)
                    fh.flush()
                    raise OSError('synthetic interrupted append')
            yield InterruptedWriter()

    def interrupted_open(self, mode='r', *args, **kwargs):
        if self == path and mode == 'a':
            return interrupted_append()
        return real_open(self, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(type(path), 'open', interrupted_open)
        S.append_turn(tmp_path, 'u1', 'c1', 'chat', {'user': '中文'})
    assert 'synthetic interrupted append' in capsys.readouterr().err
    failed_bytes = path.read_bytes()
    assert failed_bytes.startswith(before)
    assert [r['seq'] for r in _records(tmp_path)] == [1]

    S._reset_for_tests()
    S.append_turn(tmp_path, 'u1', 'c1', 'chat', {'user': 'retry'})
    assert path.read_bytes().startswith(failed_bytes)
    expected_seqs = [1, 2, 3] if landed == 'complete-json' else [1, 2]
    recs = _records(tmp_path)
    assert [r['seq'] for r in recs] == expected_seqs
    assert recs[-1]['payload']['user'] == 'retry'
    S.append_turn(tmp_path, 'u1', 'c1', 'chat', {'user': 'next'})
    assert [r['seq'] for r in _records(tmp_path)] == expected_seqs + [expected_seqs[-1] + 1]


@pytest.mark.parametrize('failure', [PermissionError, OSError])
def test_unreadable_tail_aborts_append_and_retry_preserves_complete_record(tmp_path, monkeypatch, capsys, failure):
    S.append_turn(tmp_path, 'u1', 'c1', 'chat', {'user': 'first'})
    path = S.log_path(tmp_path, 'u1', 'c1')
    before = path.read_bytes() + b'{"seq": 2, "ts": 1, "kind": "chat", "payload": {"user": "tail"}}'
    path.write_bytes(before)
    real_open = type(path).open
    reads = 0
    def fail_first_tail_read(self, mode='r', *args, **kwargs):
        nonlocal reads
        if self == path and mode == 'rb':
            reads += 1
            if reads == 1:
                raise failure('synthetic tail read unavailable')
        return real_open(self, mode, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(type(path), 'open', fail_first_tail_read)
        S.append_turn(tmp_path, 'u1', 'c1', 'chat', {'user': 'not appended'})
    assert path.read_bytes() == before
    assert reads == 1
    assert 'synthetic tail read unavailable' in capsys.readouterr().err
    S.append_turn(tmp_path, 'u1', 'c1', 'chat', {'user': 'retry'})
    assert path.read_bytes().startswith((before + b'\n', before + b'\r\n'))
    recs = _records(tmp_path)
    assert [row['seq'] for row in recs] == [1, 2, 3]
    assert [row['payload']['user'] for row in recs] == ['first', 'tail', 'retry']
