# -*- coding: utf-8 -*-
"""patrol_links 探测分桶 + diff_snapshots 问题口径的确定性回归（全程无网络）。

病根（修前）：
- `patrol_links.probe` 对 429/503 直接判 `http_dead`（计入 problem，下游按死链处置），
  但限流/服务端繁忙是**临时状态**、证据不足，不该下结论；
- `diff_snapshots` 的 problem 口径只有 `r=dead / s=mismatch`，漏掉帧内 tally 已计入的
  `i=mismatch`（integrity）与 `l=failed`（load），头条 problem 与帧自报 tally 对不上。

不变量：429/503 有限退避重试，仍受限 → `throttled`（reach 映射 unknown，保守不结论）；
404 等确定性状态仍 `http_dead` 且不重试；diff 的 problem 判据与 patrol 单一真源，
头条优先采用帧内权威 tally。合成替身注入 URL 出口与 sleep，不触网、不落盘 tracked 文件。
"""
from __future__ import annotations

import sys
import types
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "scripts"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import diff_snapshots as DS  # noqa: E402
import patrol_links as P  # noqa: E402

URL = "https://cf.10xgenomics.com/some/file.bam"


# ---------------------------------------------------------------- 假 HTTP 出口

class _FakeOK:
    status = 206

    def __init__(self):
        self.headers = {"Content-Range": "bytes 0-0/12345"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return b"x"

    def getcode(self):
        return 206


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, "synthetic", None, None)


def _install_urlopen(monkeypatch, seq):
    """按序返回异常/响应；用尽后重复最后一个，返回调用计数。"""
    calls = {"n": 0}

    def fake(req, timeout=None):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        item = seq[i]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(P.urllib.request, "urlopen", fake)
    return calls


def _install_sleep(monkeypatch) -> list:
    slept: list = []
    monkeypatch.setattr(P, "_sleep", lambda s: slept.append(s))
    return slept


# ---------------------------------------------------------------- 429/503 保守分桶

def test_probe_429_retries_then_alive(monkeypatch):
    calls = _install_urlopen(monkeypatch, [_http_error(429), _FakeOK()])
    slept = _install_sleep(monkeypatch)
    res = P.probe(URL)
    assert res["bucket"] == "alive"
    assert calls["n"] == 2
    assert len(slept) == 1


def test_probe_429_exhausted_is_throttled_not_dead(monkeypatch):
    calls = _install_urlopen(monkeypatch, [_http_error(429)])
    slept = _install_sleep(monkeypatch)
    res = P.probe(URL)
    assert res["bucket"] == "throttled"
    assert res["status"] == 429
    # reach 映射（与 run/work 同一口径）：新分桶落 unknown，绝不落 dead
    assert {"alive": "ok", "http_dead": "dead"}.get(res["bucket"], "unknown") == "unknown"
    assert calls["n"] == P.PROBE_ATTEMPTS
    assert len(slept) == P.PROBE_ATTEMPTS - 1


def test_probe_503_exhausted_is_throttled(monkeypatch):
    _install_urlopen(monkeypatch, [_http_error(503)])
    _install_sleep(monkeypatch)
    assert P.probe(URL)["bucket"] == "throttled"


def test_probe_404_stays_dead_and_not_retried(monkeypatch):
    """确定性 4xx 不许被「保守」放行：仍 dead，且不做无谓重试。"""
    calls = _install_urlopen(monkeypatch, [_http_error(404)])
    slept = _install_sleep(monkeypatch)
    res = P.probe(URL)
    assert res["bucket"] == "http_dead" and res["status"] == 404
    assert calls["n"] == 1 and slept == []


def test_probe_network_error_still_unknown_after_retries(monkeypatch):
    calls = _install_urlopen(monkeypatch, [urllib.error.URLError("boom")])
    _install_sleep(monkeypatch)
    res = P.probe(URL)
    assert res["bucket"] == "network_error"
    assert calls["n"] == P.PROBE_ATTEMPTS


def test_retry_after_parsing_rejects_http_date():
    assert P._retry_after_seconds(types.SimpleNamespace(headers={"Retry-After": "7"})) == 7.0
    # HTTP-date 形式不猜：退回固定退避序列
    assert P._retry_after_seconds(
        types.SimpleNamespace(headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})) is None
    assert P._retry_after_seconds(types.SimpleNamespace(headers=None)) is None


def test_probe_throttle_sleep_is_capped(monkeypatch):
    err = _http_error(429)
    err.headers = {"Retry-After": "9999"}
    _install_urlopen(monkeypatch, [err])
    slept = _install_sleep(monkeypatch)
    assert P.probe(URL)["bucket"] == "throttled"
    assert slept and max(slept) == P.THROTTLE_MAX_SLEEP_S


# ---------------------------------------------------------------- diff 问题口径

def _frame(path: Path, rows, *, tally=None, date="2026-01-01T00:00:00Z") -> Path:
    meta = {"schema": "biodata-inspection/v0", "snapshot_id": "syn", "snapshot_date": date,
            "source": "patrol", "n_files": len(rows), "n_problem": (tally or {}).get("problem", 0),
            "tally": tally or {}, "probed": len(rows)}
    return P.write_snapshot_frame(path, meta, rows)


def _row(k: str, **vec) -> dict:
    return {"k": k, **vec}


def test_diff_problem_counts_integrity_and_load(tmp_path, capsys):
    old = _frame(tmp_path / "old.jsonl", [
        _row("u1\thttps://x/1", r="ok", h=200, s="match", srv=1, i="mismatch"),
        _row("u2\thttps://x/2", r="ok", h=200, s="match", srv=1, l="failed"),
        _row("u3\thttps://x/3", r="ok", h=200, s="match", srv=1),
    ])
    new = _frame(tmp_path / "new.jsonl", [
        _row("u1\thttps://x/1", r="ok", h=200, s="match", srv=1, i="verified"),
        _row("u2\thttps://x/2", r="ok", h=200, s="match", srv=1, l="loaded"),
        _row("u3\thttps://x/3", r="ok", h=200, s="match", srv=1),
    ])
    assert DS.diff(old, new) == 0
    out = capsys.readouterr().out
    assert "problem 2" in out          # 旧帧：i mismatch + l failed 都要计入
    assert "problem 0" in out          # 新帧：两腿都恢复
    assert "resolved_integrity_mismatch" in out and "resolved_load_failed" in out


def test_diff_integrity_to_unknown_is_inconclusive(tmp_path, capsys):
    old = _frame(tmp_path / "old.jsonl", [
        _row("u1\thttps://x/1", r="ok", h=200, s="match", srv=1, i="mismatch"),
    ])
    new = _frame(tmp_path / "new.jsonl", [
        _row("u1\thttps://x/1", r="ok", h=200, s="match", srv=1),
    ])
    assert DS.diff(old, new) == 0
    out = capsys.readouterr().out
    assert "problem 1" in out
    assert "became_inconclusive" in out


def test_diff_headline_prefers_frame_tally(tmp_path, capsys):
    clean = [_row("u1\thttps://x/1", r="ok", h=200, s="match", srv=1)]
    old = _frame(tmp_path / "old.jsonl", clean, tally={"problem": 5})
    new = _frame(tmp_path / "new.jsonl", clean, tally={"problem": 7})
    assert DS.diff(old, new) == 0
    out = capsys.readouterr().out
    assert "problem 5" in out and "problem 7" in out
    assert "帧内 tally" in out


def test_diff_without_tally_falls_back_to_canonical_predicate(tmp_path, capsys):
    old = _frame(tmp_path / "old.jsonl", [
        _row("u1\thttps://x/1", r="ok", h=200, s="match", srv=1, i="mismatch"),
        _row("u2\thttps://x/2", r="ok", h=200, s="match", srv=1, l="failed"),
    ])
    new = _frame(tmp_path / "new.jsonl", [
        _row("u1\thttps://x/1", r="ok", h=200, s="match", srv=1, i="verified"),
        _row("u2\thttps://x/2", r="ok", h=200, s="match", srv=1, l="loaded"),
    ])
    assert DS.diff(old, new) == 0
    out = capsys.readouterr().out
    assert "problem 2" in out and "按判据复算" in out


def test_diff_problem_predicate_single_source():
    """不保留第二份判据：diff 直接复用 patrol 的 problem 口径。"""
    assert DS._file_problem is P.file_problem
