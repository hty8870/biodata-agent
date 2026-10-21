# -*- coding: utf-8 -*-
"""N10 引文导出（RIS / BibTeX）。核心诚实点：collection_doi 指向**论文**、非数据集本身 →
条目一律**数据集类型**（RIS TY-DATA / BibTeX @misc，绝不 @article），DOI 只作关联论文放 note。"""
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.content.reuse_pack import to_ris, to_bibtex  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402

_PACK = {
    "table": [
        {"dataset_name": "Human lung atlas", "source": "CELLxGENE Discover",
         "identifier": "abc-uuid-123", "url": "https://cellxgene.example/x",
         "collection_doi": "10.1000/somepaper"},
        {"dataset_name": "10x demo | pipe", "source": "10x Genomics",
         "identifier": "", "url": "https://10x.example/y", "collection_doi": None},
    ]
}


def test_ris_entries_are_datasets_not_articles():
    ris = to_ris(_PACK)
    assert ris.count("TY  - DATA") == 2
    assert "TY  - JOUR" not in ris        # 绝不把数据集写成期刊文章
    assert "AN  - abc-uuid-123" in ris    # accession
    assert "UR  - https://cellxgene.example/x" in ris


def test_ris_doi_only_as_associated_paper_note():
    ris = to_ris(_PACK)
    # DOI 只出现在 N1 note，且明说是论文不是数据集
    assert "10.1000/somepaper" in ris
    assert "not the dataset" in ris
    # 第二条无 DOI → 不臆造
    assert ris.count("Associated publication DOI") == 1


def test_bibtex_entries_are_misc_not_article():
    bib = to_bibtex(_PACK)
    assert bib.count("@misc{") == 2
    assert "@article" not in bib
    # 绝不把 collection_doi 当数据集自己的 DOI 挂条目（无 entry-level doi = 字段）
    assert "\n  doi = " not in bib and "\n  doi=" not in bib
    # DOI 在 note 里，标注是论文
    assert "Associated publication DOI: 10.1000/somepaper" in bib
    assert "not the dataset" in bib


def test_bibtex_escapes_braces_and_pipes_survive():
    bib = to_bibtex(_PACK)
    # 数据集名里的 | 不该破坏 BibTeX；花括号被转义成圆括号
    assert "10x demo | pipe" in bib
    assert "{" in bib and "}" in bib  # 结构花括号仍在


def test_empty_pack_exports_empty():
    assert to_ris({"table": []}) == ""
    assert to_bibtex({"table": []}) == ""


# ---------------------------------------------------------------- 引用键唯一性 / LaTeX 转义 / Dataset DOI

def test_bibtex_keys_stay_unique_when_uid_plus_index_collides():
    """UID＋序号直接拼接会撞键（`SCP11` 的第 2 条与 `SCP1` 的第 12 条都得 `SCP112`，
    两条都在官方快照里）——重复键让 BibTeX 报错跳过，甚至把引用指到另一个数据集。
    全表键集合必须等于条目数；未撞键的条目键逐字不变。"""
    table = []
    for i in range(12):
        ident = "SCP11" if i == 1 else ("SCP1" if i == 11 else "")
        table.append({"dataset_name": f"数据集 {i}", "source": "SCP", "identifier": ident,
                      "url": f"https://scp.example/{i}", "collection_doi": None})
    bib = to_bibtex({"table": table})
    keys = re.findall(r"@misc\{([^,]+),", bib)
    assert len(keys) == len(table) == 12
    assert len(set(keys)) == 12, f"引用键撞了：{keys}"
    assert keys[1] == "SCP112" and keys[11] == "SCP112-2", "先到的保持原键，后到的加区分后缀"


def test_bibtex_escapes_latex_specials_in_text_and_keeps_urls_real():
    """真实题名里的 `_ & # %` 原样输出会让普通 LaTeX 排版报错（`%` 吞掉后半行）。
    转义只作用于**文字字段**；URL 保持真实值（`\\_`/`\\%` 会把链接改坏，点开指到别处）。"""
    pack = {"table": [{
        "dataset_name": "scRNA-seq of A_549 & HeLa cells #1 (100% pure)",
        "source": "CELLxGENE Discover", "identifier": "abc",
        "url": "https://example.org/a_b%20c?x=1&y=2",
        "collection_doi": "10.1000/some_paper%1"}]}
    bib = to_bibtex(pack)
    title = next(ln for ln in bib.splitlines() if ln.strip().startswith("title = "))
    assert r"A\_549" in title and r"\&" in title and r"\#" in title and r"100\%" in title
    assert "https://example.org/a_b%20c?x=1&y=2" in bib, "URL 必须逐字可点（不做 TeX 转义）"
    assert r"some\_paper\%1" in bib, "note 是文字字段：DOI 转义后排版仍是同一个值"


def test_dataset_doi_is_labelled_as_a_dataset_not_a_paper():
    """数据记录自己的 DOI（Zenodo）只能叫 **Dataset DOI**；
    「关联论文 DOI」的措辞与语义逐字不变——两类 DOI 粒度不同，不许混用一句话。"""
    from dataset_recommender.corpus import provenance
    table = [
        {"dataset_name": "Zenodo deposit", "source": provenance.SOURCE_ZENODO,
         "identifier": "6962483", "url": "https://zenodo.org/records/6962483",
         "dataset_doi": "10.5281/zenodo.5720285", "collection_doi": None},
        {"dataset_name": "CELLxGENE set", "source": "CELLxGENE Discover",
         "identifier": "abc", "url": "https://cellxgene.example/x",
         "dataset_doi": None, "collection_doi": "10.1000/somepaper"},
    ]
    ris = to_ris({"table": table})
    assert "Dataset DOI: 10.5281/zenodo.5720285" in ris
    assert "10.5281/zenodo.5720285 (this DOI identifies the dataset record, not a paper)" in ris
    assert ris.count("Dataset DOI:") == 1, "只有真有数据 DOI 的条目才写"
    assert "Associated publication DOI: 10.1000/somepaper" in ris
    assert "not the dataset" in ris
    bib = to_bibtex({"table": table})
    assert "Dataset DOI: 10.5281/zenodo.5720285" in bib
    assert "Dataset DOI: 10.1000/somepaper" not in bib, "论文 DOI 不许被写成数据集 DOI"


def test_reuse_pack_rows_carry_the_typed_dataset_doi():
    """生产者侧：`_row` 经 provenance 唯一入口读出 dataset_doi；Zenodo 的 collection_doi
    仍被拦成 None（语料侧修复不许被展示层又抄回去）。"""
    from dataset_recommender.content import reuse_pack
    from dataset_recommender.corpus import provenance
    pack = reuse_pack.build_reuse_pack([{
        "dataset_name": "Zenodo deposit", "source": provenance.SOURCE_ZENODO,
        "dataset_uid": "zenodo:6962483", "url": "https://zenodo.org/records/6962483",
        "dataset_doi": "10.5281/zenodo.5720285", "collection_doi": "10.5281/zenodo.5720285",
    }])
    row = pack["table"][0]
    assert row["dataset_doi"] == "10.5281/zenodo.5720285"
    assert row["collection_doi"] is None
    assert "Dataset DOI: 10.5281/zenodo.5720285" in to_ris(pack)


def test_api_reuse_pack_returns_ris_and_bibtex():
    client = TestClient(app, base_url="http://127.0.0.1")
    ds = client.get("/api/datasets").json()
    uid = ""
    for rec in ds.get("records") or []:
        uid = str(rec.get("dataset_uid") or "").strip()
        if uid:
            break
    assert uid, "没有可用的 dataset_uid 做回归"
    r = client.post("/api/reuse-pack", json={"uids": [uid]}).json()
    assert r["ok"] and "ris" in r and "bibtex" in r
    assert "TY  - DATA" in r["ris"]
    assert "@misc{" in r["bibtex"]
