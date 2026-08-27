# -*- coding: utf-8 -*-
"""交付安全持续护栏：把 DEVELOPMENT §11「提交包不应含个人汇报/开发过程记录/未发表材料」钉成自动门。

不变量：
1. `.deliveryignore` 必须挡下已知的内部材料（开发日志 / 协同台账 / 内部规划 / Agent 契约）。
2. **交付文件集**（剔除内部件后剩下的对外文件）不得残留敏感词（真名 / 内部专名 / 本机个人路径）——
   这是主门：三门全绿也拦不住打包时把内部件塞进 ZIP，本测试在 CI/交付前把它拦下。
3. 扫描器本身能抓到植入的泄漏（防止有人把扫描逻辑误改成"永远通过"）。
4. **交付集 ∩ git 忽略集 == ∅**（2026-07-17 补）。见下。

## 关于第 4 条（这批测试存在的理由）

在此之前 `.gitignore` 与 `.deliveryignore` 是两份平行的**手抄** denylist，从未对账。后果实测：
交付集 223 文件 / 2.32 GB，其中 28 个是 git 明确拒绝跟踪的——`.userdata/accounts.json`（账户名 +
scrypt salt + pwd_hash）、`models/` 2.27 GB 权重、`artifacts/` 11 份内部质量门报告、`outputs/` 3 份。
而 `--check` 打印「复核通过：0 内部专名 / 0 secret 值命中」：敏感词扫描器抓不到（accounts.json 里
没有中文专名），secret 扫描器也抓不到（salt/hash 是裸 hex，`SECRET_VALUE_PATTERNS` 刻意不含裸 hex
正则以免误报）。**门是绿的，凭据漏出去了** —— fail-open 的最坏形态。

所以这里钉的不是"那 4 个目录被排除了"（那样只挡住已知的 4 个出口，第 5 个新目录照漏），而是
**结构不变量**：git 拒绝跟踪的文件，一律不在交付集里。它对未来任何新增的 gitignored 目录自动成立。

本测试**不含任何敏感词字面量**（检测词从 make_delivery.FORBIDDEN_TOKENS 导入），故自身干净、可随包交付。
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import make_delivery as MD  # noqa: E402


def _git_usable() -> bool:
    """本仓库工作树里 git 是否真能跑。缺 git 时跳过依赖它的断言，而不是把跳过伪装成通过。"""
    if shutil.which("git") is None:
        return False
    try:
        p = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                           cwd=str(ROOT), capture_output=True, timeout=30)
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


requires_git = pytest.mark.skipif(not _git_usable(), reason="需要工作树内可用的 git 才能与 .gitignore 对账")


def _subtree_has_files(rel_dir: str) -> bool:
    """该相对目录在工作树中是否有任何文件（不论是否被 git 跟踪）。

    用于区分「目录存在且被排除」与「目录不存在」：后者在陌生机 clone / 本机未创建时
    压根没有文件，此刻断言"它被 .deliveryignore 排除"是对一个不存在集合的冗余检查，应 skip。
    """
    base = ROOT / rel_dir
    if not base.is_dir():
        return False
    return any(p.is_file() for p in base.rglob("*"))


def _require_subtree_or_skip(rel_dir: str) -> None:
    if not _subtree_has_files(rel_dir):
        pytest.skip(
            f"{rel_dir} 目录不存在或无文件（未创建/未跟踪）——其排除性断言为空集，跳过"
        )


def test_deliveryignore_excludes_known_internal_materials():
    excluded = set(MD.excluded_internal())
    _require_subtree_or_skip("开发日志归档")
    assert any(p.startswith("开发日志归档/") for p in excluded), "开发日志未被交付排除"
    _require_subtree_or_skip("协同")
    assert any(p.startswith("协同/") for p in excluded), "协同台账未被交付排除"
    assert any("NEXT_STEPS_CANDIDATES" in p for p in excluded), "内部规划文档未被交付排除"
    assert "AGENTS.md" in excluded and "CLAUDE.md" in excluded, "Agent 契约未被交付排除"
    # 2026-08-05：采集流水线工作区（staging 124M 内部材料）入 git 时漏登 .deliveryignore——钉死防再漂移
    # （2026-08-27 迁移批：该工作区整体迁至顶层 research/，断言随迁）
    _require_subtree_or_skip("research")
    assert any(p.startswith("research/") for p in excluded), "采集流水线工作区未被交付排除"
    # 2026-08-22：om1 交付审计发现 docs/工作记录/（含未发表文章）漏登 .deliveryignore——钉死防再漂移
    _require_subtree_or_skip("docs/工作记录")
    assert any(p.startswith("docs/工作记录/") for p in excluded), "内部工作笔记未被交付排除"
    _require_subtree_or_skip("docs/归档")
    assert any(p.startswith("docs/归档/") for p in excluded), "旧逻辑归档快照未被交付排除"
    _require_subtree_or_skip("docs/汇报")
    assert any(p.startswith("docs/汇报/") for p in excluded), "内部汇报未被交付排除"


@requires_git
def test_internal_work_notes_and_reports_never_ship():
    """红线「未发表材料与内部工作笔记不得外发」的交付集直接断言。

    2026-08-22 发现 docs/工作记录/（任务记录/设计稿/未发表文章 PDF）与 docs/归档/（旧逻辑
    .removed.diff/.head-snapshot）混进交付集——.deliveryignore 漏登，且 .pdf 不在敏感词扫描
    后缀内故主门抓不到。这里在交付集层钉死，不再依赖 denylist 或 token 扫描。
    """
    rels = {p.relative_to(MD.REPO_ROOT).as_posix() for p in MD.collect_delivery_files()}
    for prefix in ("docs/工作记录/", "docs/归档/", "docs/汇报/"):
        assert not any(p.startswith(prefix) for p in rels), f"{prefix} 混进了交付集"


@requires_git
def test_delivery_keeps_runtime_brand_icon_but_excludes_packaging_build_tools():
    """source 窗口壳运行时需要品牌 ICO；其生成器/安装器工程仍不外发。"""
    rels = {p.relative_to(MD.REPO_ROOT).as_posix() for p in MD.collect_delivery_files()}
    assert "packaging/assets/BioDataAgent.ico" in rels
    assert "packaging/assets/make_ico.py" not in rels
    assert not any(path.startswith("packaging/inno/") for path in rels)
    assert not any(path.startswith("packaging/pyinstaller/") for path in rels)
    assert not any(path.startswith("packaging/requirements/") for path in rels)
    assert not any(path.startswith("packaging/signing/") for path in rels)


@requires_git
def test_delivery_includes_model_worker_source_but_not_frozen_only_model_lock():
    """能力边界：源码交付含 model_worker.py 源码，但不含 frozen 专属的模型 lock。

    frozen Setup 由 PyInstaller spec 随包带齐 uv.exe + model lock + worker（见
    tests/test_frozen_runtime_contract.py）；源码包走便携版手动 pip 安装、不依赖模型 lock，
    若要用设置页在线安装则需系统 uv——边界已写在 README 与 使用说明书 10.4。
    """
    rels = {p.relative_to(MD.REPO_ROOT).as_posix() for p in MD.collect_delivery_files()}
    assert "src/dataset_recommender/retrieval/model_worker.py" in rels
    assert "packaging/requirements/model-win-x64.lock" not in rels


@requires_git   # 本批次起 collect_delivery_files 内部会调 git check-ignore（fail-closed）→ 本条也成了 git 依赖项
def test_delivery_set_is_free_of_sensitive_tokens():
    files = MD.collect_delivery_files()
    violations = MD.scan_forbidden(files)
    # 定位信息用 token 在常量表里的下标，避免在测试文件里出现敏感词字面量。
    detail = "; ".join(f"{v['path']}:{v['line']}(token#{MD.FORBIDDEN_TOKENS.index(v['token'])})" for v in violations)
    assert not violations, "交付文件集残留敏感词：" + detail


def test_scanner_catches_planted_leak(tmp_path):
    planted = tmp_path / "leak.md"
    planted.write_text("harmless line\n预警内容含 " + MD.FORBIDDEN_TOKENS[0] + " 一处\n", encoding="utf-8")
    hits = MD.scan_forbidden([planted], root=tmp_path)
    assert len(hits) == 1 and hits[0]["line"] == 2, "扫描器未能抓到植入的敏感词"


# ------------------------------------------------------------------ 个人化 token 外部化（.delivery-tokens.local）


def test_local_tokens_missing_file_is_skipped_not_fatal(tmp_path, capsys):
    """`.delivery-tokens.local` 缺失（干净 clone 场景）：只提示、不抛异常、返回空集——
    交付安全检查不得因个人清单不在而崩溃或失效。"""
    missing = tmp_path / ".delivery-tokens.local"
    assert MD.load_local_forbidden_tokens(missing) == ()
    assert ".delivery-tokens.local" in capsys.readouterr().err, "缺失时应打印一句可发现的提示"


def test_local_tokens_parse_skips_comments_and_blank_lines(tmp_path):
    """机制：每行一个 token，`#` 注释行与空行忽略，首尾空白剥除。
    测试只用假 token——绝不把任何真实个人 token 字面量写进测试文件。"""
    f = tmp_path / ".delivery-tokens.local"
    f.write_text(
        "# dummy denylist\n"
        "\n"
        "dummy-personal-token\n"
        "  spaced-dummy-token  \n"
        "#another-comment\n",
        encoding="utf-8",
    )
    assert MD.load_local_forbidden_tokens(f) == ("dummy-personal-token", "spaced-dummy-token")


def test_forbidden_tokens_merge_generic_plus_local_no_duplicates():
    """结构不变量：合并表 = 通用层 ∪ 本地层；通用层必须始终在源码里；无重复、无空白项。"""
    assert set(MD.GENERIC_FORBIDDEN_TOKENS).issubset(set(MD.FORBIDDEN_TOKENS))
    assert len(set(MD.FORBIDDEN_TOKENS)) == len(MD.FORBIDDEN_TOKENS)
    assert all(t and t == t.strip() for t in MD.FORBIDDEN_TOKENS)


def test_scanner_catches_dummy_token_from_local_layer(tmp_path, monkeypatch):
    """本地层 token 同样参与主门扫描：用 monkeypatch 注入假 token 验证通路，不碰真实清单。"""
    monkeypatch.setattr(MD, "FORBIDDEN_TOKENS", ("dummy-personal-token",))
    planted = tmp_path / "leak.md"
    planted.write_text("harmless\nplease find dummy-personal-token here\n", encoding="utf-8")
    hits = MD.scan_forbidden([planted], root=tmp_path)
    assert [(h["path"], h["line"], h["token"]) for h in hits] == [
        ("leak.md", 2, "dummy-personal-token")
    ]


# ------------------------------------------------------------------ .gitignore 对账


@requires_git
def test_env_templates_ship_but_real_env_files_never_do():
    """`.env.example` / `.env.zhipu.example` 必须进交付包；真 `.env` 一律不进。

    2026-07-21 实测：硬排除正则 `^\\.env(\\..+)?$` 和 `.deliveryignore` 里的 `.env.*` 一起把**模板**也挡了，
    而 README「复制 `.env.example` 为 `.env`」、MCP 教程也指名要它——客户照做会找不到文件。
    模板只含占位符，且仍受 secret 值扫描覆盖（真 key 粘进去照样拒绝打包）。
    """
    rels = {p.relative_to(MD.REPO_ROOT).as_posix() for p in MD.collect_delivery_files()}
    for tpl in (".env.example", ".env.zhipu.example"):
        assert (MD.REPO_ROOT / tpl).exists(), f"仓库里没有 {tpl}，本断言的前提不成立"
        assert tpl in rels, f"{tpl} 没进交付包——README 里那条「复制它」的指引会断"
    for real in (".env", ".env.zhipu"):
        assert real not in rels, f"真 env 文件 {real} 混进了交付包"


def test_userdata_is_hard_skipped_without_needing_git():
    """账户凭据（用户名 + scrypt salt/pwd_hash）的**兜底**门：不依赖 git 是否可用。

    git 对账是主门，但凭据不该只有一道门——git 缺失时主门会 fail-closed 报错，这道门在任何情况下成立。
    """
    assert ".userdata" in MD._HARD_SKIP_DIRS


@requires_git
def test_delivery_set_contains_no_gitignored_file():
    """**核心结构不变量**：git 拒绝跟踪的文件，一律不在交付集里。

    这条今天（修复前）会红：实测 28 个命中，含 .userdata/accounts.json + models/ 2.27 GB。
    它不点名任何具体目录 —— 未来新增任何 gitignored 目录都自动被它覆盖，无需有人记得同步第二份清单。
    """
    files = MD.collect_delivery_files()
    rels = [f.relative_to(MD.REPO_ROOT).as_posix() for f in files]
    leaked = sorted(MD.gitignored_paths(rels))
    assert not leaked, (
        "交付集里混进了 git 拒绝跟踪的文件（干净 clone 里根本没有它们）：" + "; ".join(leaked[:20])
    )


@requires_git
def test_delivery_set_contains_only_git_tracked_files():
    """**收紧后的核心结构不变量**：交付集 ⊆ git 已跟踪集。

    只挡「被 git 忽略」还不够。仓库根曾躺着一份**未跟踪、也没被任何忽略规则覆盖**的个人周报 PDF：
    它进了交付集，而 `--check` 照样打印「复核通过」——`.pdf` 不在 `_TEXT_SUFFIXES` 里，
    敏感词扫描和 secret 扫描都不看它。denylist 永远追不上这一类；
    「与干净 clone 对齐」才是能自动成立的那条线。
    """
    files = MD.collect_delivery_files()
    rels = {f.relative_to(MD.REPO_ROOT).as_posix() for f in files}
    untracked = sorted(rels - MD.tracked_paths())
    assert not untracked, (
        "交付集里混进了 git 未跟踪的文件（干净 clone 里没有它们，可能是个人材料或临时导出）："
        + "; ".join(untracked[:20])
    )


@requires_git
def test_tracked_probe_is_not_vacuous():
    """反同义反复守卫：证明上一条的「空」是真的查过，而不是 `tracked_paths` 恒返回全集。"""
    tracked = MD.tracked_paths()
    assert "README.md" in tracked, "已跟踪文件没被认出来——交付集会被清空"
    # 本脚本被 .deliveryignore 排除、但确实被 git 跟踪：证明这里问的是「跟踪与否」而非「交付与否」。
    assert "scripts/make_delivery.py" in tracked
    assert "no/such/file/anywhere.xyz" not in tracked
    assert len(tracked) > 100, f"已跟踪集异常小（{len(tracked)}），git 对账可能没真的跑"


@requires_git
def test_gitignored_probe_actually_detects_ignored_paths():
    """反同义反复守卫：证明上一条的「空」是真的查过，而不是 `gitignored_paths` 恒返回空集。

    用 .gitignore 里字面存在的模式构造探针路径（不依赖这些文件真的存在——check-ignore 是纯规则匹配）。
    若有人把 gitignored_paths 改成 `return set()`，上一条仍绿、这一条会红。
    """
    probes = [".userdata/probe.json", "models/probe.bin", "artifacts/probe.json"]
    detected = MD.gitignored_paths(probes)
    assert detected == set(probes), f"git 对账探针失灵：期望全部命中，实得 {sorted(detected)}"

    tracked_probe = ["README.md"]
    assert MD.gitignored_paths(tracked_probe) == set(), "已跟踪文件被误判为 git 忽略——会把它错杀出交付包"


@requires_git
def test_check_reports_size_and_stays_under_a_sane_bound():
    """交付包体积哨兵：漏 models/ 那次，包体是 2.32 GB 而没有任何一处提示不对劲。

    不锁死具体数值（内容会长），只锁「量级」——一个只含元数据目录 + 前后端源码的交付包不该有几百 MB。
    真需要放大时，改这个常量本身就是一次需要解释的动作。
    """
    files = MD.collect_delivery_files()
    total_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
    assert total_mb < 200, f"交付包 {total_mb:.1f} MiB，超出量级哨兵——是不是又混进了权重/产物目录？"


# ------------------------------------------------------------------ secret 扫描白名单


def _fake_openai_key() -> str:
    """运行时拼装一个形似 OpenAI 旧版 key 的假串（不落 `sk-` 连续 alnum 字面量，免得本测试文件
    自身被交付 secret 扫描误报）。仅用于驱动 SECRET_VALUE_PATTERNS 命中，非真实凭据。"""
    return "sk-" + "abcdefghijklmnopqrstuvwxyz" + "0123"


def test_secret_scan_allowlist_skips_only_exact_line(tmp_path):
    """机制：白名单只豁免「精确 (相对路径, 行号)」，同文件其他行、其他文件同形串照常上报——
    证伪"加了白名单就能把任意 key 洗白"。"""
    d = tmp_path / "sub"
    d.mkdir()
    f = d / "t.py"
    key = _fake_openai_key()
    f.write_text(f'a = 1\nkey = "{key}"\nkey2 = "{key}"\n', encoding="utf-8")
    # 只豁免第 2 行 → 只有第 3 行上报（同文件同一串，第二处未被豁免仍被抓）
    hits = MD.scan_secret_values([f], root=tmp_path, allowlist={("sub/t.py", 2)})
    assert [(h["line"], h["pattern"]) for h in hits] == [(3, "openai-secret-key")]
    # 不豁免 → 两行都上报
    hits2 = MD.scan_secret_values([f], root=tmp_path, allowlist=set())
    assert {h["line"] for h in hits2} == {2, 3}
    # 默认白名单（指向真实 tests/ 文件）不影响这个无关文件 → 两行都上报
    hits3 = MD.scan_secret_values([f], root=tmp_path)
    assert {h["line"] for h in hits3} == {2, 3}


def test_secret_scan_allowlist_covers_exactly_the_audited_fixture_lines():
    """2026-08-24 交付审计 fix 3 回归钉：6 个脱敏测试夹具（故意放置形似 key 的字符串以验证遮蔽/
    不回显）被 SECRET_SCAN_ALLOWLIST 白名单化。钉死三件事：
      1. 白名单命中的 (文件:行) 在真是文件里确实存在、且全部命中 openai-secret-key；
      2. 这两个文件里除白名单外再无其他 secret 命中（夹具行号漂移或新增未豁免 key → 立即翻红）；
      3. 白名单豁免后这两个文件零 secret 命中 → 交付门不再被测试夹具误伤。
    """
    targets = [MD.REPO_ROOT / "tests" / "test_telemetry_export.py",
               MD.REPO_ROOT / "tests" / "test_telemetry_receiver.py"]
    hits = MD.scan_secret_values(targets, allowlist=set())  # 不豁免 → 应命中全部
    got = {(h["path"], h["line"]) for h in hits}
    assert got == set(MD.SECRET_SCAN_ALLOWLIST), f"夹具命中集与白名单不一致：{sorted(got)}"
    assert all(h["pattern"] == "openai-secret-key" for h in hits)
    # 白名单豁免后这两个文件 → 零 secret 命中
    assert MD.scan_secret_values(targets) == []
