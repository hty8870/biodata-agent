# -*- coding: utf-8 -*-
"""外部平台库 ingest 脚本的快照写入安全回归。

钉死四条不变量（全程合成响应替身：不联网、不写任何 tracked 快照）：

1. 快速/降级自测（--limit；ebi --no-enrich / AE --detail 0）不得覆写 database/external
   正式快照——没有显式 --out 就 fail-closed 非零退出；
2. 抓取失败、分页失败或结果为空时必须非零退出，且原快照**字节不变**（原子写，不留半截）；
3. --dry-run 不写任何文件；
4. refinebio/zenodo 的 --dry-run 保留读缓存、但不写缓存（help 文案＝语义）。

修前背景：--limit 自测直接覆写正式快照；AE 分页失败 break 后仍写部分数据并
exit 0；refinebio --dry-run 自称「不写任何文件」却写 staging/raw 缓存。各模块 OUT_PATH 一律
指向 tmp 哨兵，正式快照只读不写。
"""
from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT / "scripts"), str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ingest_arrayexpress as ax  # noqa: E402
import ingest_cellxgene as cxg  # noqa: E402
import ingest_ebi_scea as eb  # noqa: E402
import ingest_hca as hc  # noqa: E402
import ingest_refinebio as rb  # noqa: E402
import ingest_zenodo as zn  # noqa: E402

SENTINEL = b'{"source": "sentinel", "record_count": 0, "records": []}\n'


# ---------------------------------------------------------------- 合成响应替身

def _install_hca(monkeypatch, *, fail=False, empty=False, always_next=False):
    def fake(url):
        if fail:
            raise OSError("synthetic fetch failure")
        if empty:
            return {"hits": [], "pagination": {"next": None}}
        return {"hits": [{"projects": [{"projectTitle": "HCA project", "projectId": "p-1"}]}],
                "pagination": {"next": f"{url}&page=next" if always_next else None}}
    monkeypatch.setattr(hc, "fetch", fake)


def _install_cxg(monkeypatch, *, fail=False, empty=False, always_next=False):
    def fake(url):
        if fail:
            raise OSError("synthetic fetch failure")
        if empty:
            return []
        return [{"dataset_id": "d-1", "title": "CELLxGENE dataset",
                 "assets": [{"url": "https://example.org/a.h5ad",
                             "filetype": "H5AD", "filesize": 10}]}]
    monkeypatch.setattr(cxg, "fetch", fake)


def _install_ebi(monkeypatch, *, fail=False, empty=False, always_next=False):
    def fake(url):
        if fail:
            raise OSError("synthetic fetch failure")
        if empty:
            return {"experiments": []}
        return {"experiments": [{"experimentAccession": "E-MTAB-9999",
                                 "experimentDescription": "EBI experiment"}]}
    monkeypatch.setattr(eb, "fetch", fake)
    monkeypatch.setattr(eb, "fetch_design_facets",
                        lambda acc: ([], [], {"origin": "declared", "complete": True}))


def _ae_hit(acc: str = "E-MTAB-1") -> dict:
    return {"accession": acc, "title": "ArrayExpress study", "content": "single cell"}


def _install_ae(monkeypatch, *, fail=False, empty=False, pages=None,
                fail_on_page=None, total_hits=None):
    pages = pages if pages is not None else [[_ae_hit()]]
    state = {"page": 0}

    def fake(url):
        state["page"] += 1
        if fail or (fail_on_page is not None and state["page"] == fail_on_page):
            raise OSError("synthetic search failure")
        idx = min(state["page"], len(pages)) - 1
        want = 0 if empty else (total_hits if total_hits is not None
                                else sum(len(p or []) for p in pages))
        return {"totalHits": want, "hits": [] if empty else pages[idx]}

    monkeypatch.setattr(ax, "fetch", fake)
    monkeypatch.setattr(ax, "fetch_detail", lambda acc: None)
    # 富化限速用的模块级 time 换成不吃真实时间的替身。
    monkeypatch.setattr(ax, "time", types.SimpleNamespace(sleep=lambda *_a: None))
    return state


@dataclass(frozen=True)
class Case:
    name: str
    module: object
    install: object
    partial_argv: tuple = ("--limit", "1")
    degraded_argv: "tuple | None" = None

    def run(self, monkeypatch, argv):
        monkeypatch.setattr(sys, "argv", [f"{self.module.__name__}.py", *argv])
        return self.module.main()


CASES = (
    Case("hca", hc, lambda mp, **kw: _install_hca(mp, **kw)),
    Case("cellxgene", cxg, lambda mp, **kw: _install_cxg(mp, **kw)),
    Case("ebi_scea", eb, lambda mp, **kw: _install_ebi(mp, **kw),
         degraded_argv=("--no-enrich",)),
    Case("arrayexpress", ax, lambda mp, **kw: _install_ae(mp, **kw),
         degraded_argv=("--detail", "0")),
)
_CASE_IDS = [c.name for c in CASES]


def _sentinel_at(monkeypatch, module, tmp_path) -> Path:
    snap = tmp_path / f"{module.__name__}-snapshot.json"
    snap.write_bytes(SENTINEL)
    monkeypatch.setattr(module, "OUT_PATH", snap)
    return snap


# ---------------------------------------------------------------- 不变量 1/2：不得覆写正式快照

@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_partial_self_test_refuses_default_snapshot(case, monkeypatch, tmp_path):
    """--limit 快速自测没有显式 --out 时必须 fail-closed，正式快照字节不变。"""
    snap = _sentinel_at(monkeypatch, case.module, tmp_path)
    case.install(monkeypatch)
    assert case.run(monkeypatch, list(case.partial_argv)) != 0
    assert snap.read_bytes() == SENTINEL


@pytest.mark.parametrize("case", [c for c in CASES if c.degraded_argv],
                         ids=[c.name for c in CASES if c.degraded_argv])
def test_degraded_self_test_refuses_default_snapshot(case, monkeypatch, tmp_path):
    """降级快速模式（--no-enrich / --detail 0）同属自测，同样不得覆写正式快照。"""
    snap = _sentinel_at(monkeypatch, case.module, tmp_path)
    case.install(monkeypatch)
    assert case.run(monkeypatch, list(case.degraded_argv)) != 0
    assert snap.read_bytes() == SENTINEL


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_partial_with_explicit_out_writes_only_target(case, monkeypatch, tmp_path):
    snap = _sentinel_at(monkeypatch, case.module, tmp_path)
    case.install(monkeypatch)
    target = tmp_path / "explicit-out.json"
    assert case.run(monkeypatch, [*case.partial_argv, "--out", str(target)]) == 0
    assert snap.read_bytes() == SENTINEL
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["record_count"] == len(payload["records"]) == 1


@pytest.mark.parametrize("case", [c for c in CASES if c.degraded_argv],
                         ids=[c.name for c in CASES if c.degraded_argv])
def test_degraded_with_explicit_out_is_allowed(case, monkeypatch, tmp_path):
    """显式 --out 是可审计的逃生门：降级模式写用户指定的目标，正式快照仍不动。"""
    snap = _sentinel_at(monkeypatch, case.module, tmp_path)
    case.install(monkeypatch)
    target = tmp_path / "degraded-out.json"
    assert case.run(monkeypatch, [*case.degraded_argv, "--out", str(target)]) == 0
    assert snap.read_bytes() == SENTINEL
    assert target.is_file()


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_dry_run_writes_nothing(case, monkeypatch, tmp_path):
    """--dry-run：不写任何文件（含子目录与缓存），且不改变既有快照。"""
    snap = _sentinel_at(monkeypatch, case.module, tmp_path)
    before = set(tmp_path.rglob("*"))
    case.install(monkeypatch)
    assert case.run(monkeypatch, ["--dry-run"]) == 0
    assert snap.read_bytes() == SENTINEL
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_fetch_failure_nonzero_and_bytes_unchanged(case, monkeypatch, tmp_path):
    snap = _sentinel_at(monkeypatch, case.module, tmp_path)
    case.install(monkeypatch, fail=True)
    assert case.run(monkeypatch, []) != 0
    assert snap.read_bytes() == SENTINEL


@pytest.mark.parametrize("case", CASES, ids=_CASE_IDS)
def test_empty_result_does_not_wipe_snapshot(case, monkeypatch, tmp_path):
    """抓取成功但零记录＝来源形状/可用性异常，不得把正式快照洗成空库。"""
    snap = _sentinel_at(monkeypatch, case.module, tmp_path)
    case.install(monkeypatch, empty=True)
    assert case.run(monkeypatch, []) != 0
    assert snap.read_bytes() == SENTINEL


# ---------------------------------------------------------------- 不变量 2：分页失败/截断

def test_arrayexpress_pagination_failure_writes_no_partial(monkeypatch, tmp_path):
    """AE 第 2 页失败：修前 break 后仍写第 1 页部分快照并 exit 0。"""
    snap = _sentinel_at(monkeypatch, ax, tmp_path)
    _install_ae(monkeypatch, pages=[[_ae_hit("E-MTAB-1")], [_ae_hit("E-MTAB-2")]],
                fail_on_page=2, total_hits=2)
    monkeypatch.setattr(sys, "argv", ["ingest_arrayexpress.py"])
    assert ax.main() != 0
    assert snap.read_bytes() == SENTINEL


def test_arrayexpress_incomplete_pagination_writes_no_partial(monkeypatch, tmp_path):
    """页数上限截断（收集 200 < totalHits 250）同样不得写部分快照。"""
    snap = _sentinel_at(monkeypatch, ax, tmp_path)
    page = [_ae_hit(f"E-MTAB-{i}") for i in range(100)]
    _install_ae(monkeypatch, pages=[page, page], total_hits=250)
    monkeypatch.setattr(ax, "MAX_PAGES", 2)
    monkeypatch.setattr(sys, "argv", ["ingest_arrayexpress.py"])
    assert ax.main() != 0
    assert snap.read_bytes() == SENTINEL


def test_hca_page_cap_truncation_writes_no_partial(monkeypatch, tmp_path):
    """HCA 页数上限后仍有 next：不完整集合不得落盘。"""
    snap = _sentinel_at(monkeypatch, hc, tmp_path)
    _install_hca(monkeypatch, always_next=True)
    monkeypatch.setattr(sys, "argv", ["ingest_hca.py"])
    assert hc.main() != 0
    assert snap.read_bytes() == SENTINEL


def test_atomic_write_failure_keeps_original_bytes(monkeypatch, tmp_path):
    """原子写：替换失败时既有文件字节不变，且不留下 .tmp 残片。"""
    target = tmp_path / "snap.json"
    target.write_bytes(SENTINEL)

    def boom(src, dst):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(cxg.os, "replace", boom)
    with pytest.raises(OSError):
        cxg.write_snapshot(target, {"records": [1, 2, 3]})
    assert target.read_bytes() == SENTINEL
    assert not list(tmp_path.glob("*.tmp"))


# ---------------------------------------------------------------- 不变量 4：dry-run 读缓存、不写缓存

_RB_QUERY = "single-cell RNA-seq"
_RB_PAYLOAD = {"results": [{"accession_code": "GSE999999",
                            "title": "single cell synthetic study"}]}


def _prep_rb(monkeypatch, tmp_path) -> Path:
    raw = tmp_path / "raw"
    monkeypatch.setattr(rb, "RAW_DIR", raw)
    monkeypatch.setattr(rb, "STAGING_DIR", tmp_path / "staging")
    monkeypatch.setattr(rb, "REVIEW_TSV", tmp_path / "staging" / "candidates_review.tsv")
    monkeypatch.setattr(rb, "OUT_PATH", tmp_path / "refinebio.json")
    monkeypatch.setattr(rb, "DISCOVERY_QUERIES", (_RB_QUERY,))
    return raw


def test_refinebio_dry_run_writes_no_cache(monkeypatch, tmp_path):
    """修前：--dry-run 仍写 staging/raw 缓存，与「不写任何文件」文案相反。"""
    raw = _prep_rb(monkeypatch, tmp_path)
    monkeypatch.setattr(rb.cc, "_fetch_logged", lambda *a, **k: dict(_RB_PAYLOAD))
    monkeypatch.setattr(sys, "argv", ["ingest_refinebio.py", "--dry-run"])
    assert rb.main() == 0
    assert not raw.exists()
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


def test_refinebio_dry_run_still_reads_cached_raw(monkeypatch, tmp_path):
    raw = _prep_rb(monkeypatch, tmp_path)
    raw.mkdir(parents=True)
    cache = raw / f"search_{rb._slug(_RB_QUERY)}.json"
    cache.write_text(json.dumps(_RB_PAYLOAD), encoding="utf-8")

    def no_network(*_a, **_k):
        raise AssertionError("dry-run 命中缓存时不应联网")

    monkeypatch.setattr(rb.cc, "_fetch_logged", no_network)
    monkeypatch.setattr(sys, "argv", ["ingest_refinebio.py", "--dry-run"])
    assert rb.main() == 0
    assert json.loads(cache.read_text(encoding="utf-8")) == _RB_PAYLOAD


def test_refinebio_fetch_json_write_cache_flag(monkeypatch, tmp_path):
    raw = tmp_path / "raw"
    monkeypatch.setattr(rb, "RAW_DIR", raw)
    monkeypatch.setattr(rb.cc, "_fetch_logged", lambda *a, **k: dict(_RB_PAYLOAD))
    got = rb._fetch_json("https://example.org", endpoint="e", query="q",
                         cache=raw / "c.json", use_cache=True, write_cache=False)
    assert got == _RB_PAYLOAD
    assert not raw.exists()


_ZN_RID = 999999999
_ZN_PAYLOAD = {"id": _ZN_RID, "doi": "10.5281/zenodo.999999999",
               "metadata": {"title": "Synthetic zenodo record", "access_right": "open",
                            "resource_type": {"type": "dataset"},
                            "description": "single cell"}}

_ZENODO_NET_ERROR = AssertionError("dry-run 命中缓存时不应联网")


def _prep_zn(monkeypatch, tmp_path) -> Path:
    raw = tmp_path / "raw"
    monkeypatch.setattr(zn, "RAW_DIR", raw)
    monkeypatch.setattr(zn, "OUT_PATH", tmp_path / "zenodo.json")
    monkeypatch.setattr(zn, "CURATED_IDS", (("synthetic", (_ZN_RID,)),))
    return raw


def test_zenodo_dry_run_reads_cache_and_writes_nothing(monkeypatch, tmp_path):
    """修前：--dry-run 关缓存读、强制重联网；修后读缓存但不写任何文件。"""
    raw = _prep_zn(monkeypatch, tmp_path)
    raw.mkdir(parents=True)
    cache = raw / f"{_ZN_RID}.json"
    cache.write_text(json.dumps(_ZN_PAYLOAD), encoding="utf-8")

    def no_network(*_a, **_k):
        raise _ZENODO_NET_ERROR

    monkeypatch.setattr(zn.cc, "_fetch_logged", no_network)
    monkeypatch.setattr(sys, "argv", ["ingest_zenodo.py", "--dry-run"])
    assert zn.main() == 0
    assert json.loads(cache.read_text(encoding="utf-8")) == _ZN_PAYLOAD
    assert not (tmp_path / "zenodo.json").exists()
    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == [f"{_ZN_RID}.json"]


def test_zenodo_fetch_payload_write_cache_flag(monkeypatch, tmp_path):
    raw = tmp_path / "raw"
    monkeypatch.setattr(zn, "RAW_DIR", raw)
    monkeypatch.setattr(zn.cc, "_fetch_logged",
                        lambda *a, **k: {"id": 1, "metadata": {"title": "T"}})
    payload = zn._fetch_record_payload(1, project_root=tmp_path,
                                       read_cache=False, write_cache=False)
    assert payload["id"] == 1
    assert not raw.exists()
