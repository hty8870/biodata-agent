# -*- coding: utf-8 -*-
"""统一知识窗前端门：纯核 node 真行为 + 静态接线契约。

web_smoke 只做静态字符串检查、不执行 JS，所以「把草稿当生效 / 系统层可写 /
写库不走道闸 / 变更失败不可见 / 迟回包不丢弃」这类问题只能在这里拦。
纯核行为由 tests/js/knowledge_spec.mjs 经真实 node 断言（同 dream_core 门构型）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
DATASET_HTML = (ROOT / "web/static/dataset.html").read_text(encoding="utf-8")
CORE = (ROOT / "web/static/js/core/core.js").read_text(encoding="utf-8")
KNOWLEDGE = (ROOT / "web/static/js/panel/knowledge.js").read_text(encoding="utf-8")
KNOWLEDGE_CORE = (ROOT / "web/static/js/panel/knowledge_core.js").read_text(encoding="utf-8")
BOOT = (ROOT / "web/static/js/core/boot.js").read_text(encoding="utf-8")
PACKAGE = (ROOT / "package.json").read_text(encoding="utf-8")
SPEC = ROOT / "tests" / "js" / "knowledge_spec.mjs"


def _resolve_node() -> str | None:
    override = os.environ.get("BIODATA_NODE")
    if override and (shutil.which(override) or Path(override).exists()):
        return override
    for cand in ("node", "node.exe"):
        found = shutil.which(cand)
        if found:
            return found
    return None


def test_knowledge_core_behavior_spec_passes_under_node() -> None:
    assert SPEC.is_file(), f"缺少行为规格文件：{SPEC}"
    node = _resolve_node()
    if not node:
        pytest.skip("未解析到 node.js —— 跳过纯核行为门（full 质量门的 javascript-syntax 环境必有 node）。")
    proc = subprocess.run(
        [node, str(SPEC)],
        capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    assert proc.returncode == 0, f"knowledge_core 行为门失败：\n{proc.stdout}\n{proc.stderr}"
    assert "KNOWLEDGE_CORE_SPEC_OK" in proc.stdout


def test_knowledge_modules_registered_in_both_pages_and_package() -> None:
    """新模块三端同键：两页 importmap + package.json imports（parity 门钉的同一不变量）。"""
    assert '"#knowledge":' in HTML and '"#knowledge_core":' in HTML, "index.html importmap 缺知识窗键"
    assert '"#knowledge":' in DATASET_HTML and '"#knowledge_core":' in DATASET_HTML, "dataset.html importmap 缺知识窗键"
    assert '"#knowledge":' in PACKAGE and '"#knowledge_core":' in PACKAGE, "package.json imports 缺知识窗键"
    m = re.search(r'<script type="module" src="(/static/js/panel/knowledge\.js\?v=[0-9a-z-]+)"></script>', HTML)
    assert m, "index.html 没有以 type=module + ?v= 加载 knowledge.js"


def test_knowledge_endpoints_declared_in_api_table() -> None:
    for frag in (
        'knowledgeStatus: "/api/knowledge/status"',
        'knowledgeEntries: "/api/knowledge/entries"',
        'knowledgeChanges: "/api/knowledge/changes"',
        'knowledgePropose: "/api/knowledge/propose"',
        'knowledgeAssess: "/api/knowledge/assess"',
        'knowledgeCommit: "/api/knowledge/commit"',
        'knowledgeReject: "/api/knowledge/reject"',
    ):
        assert frag in CORE, f"core.js 的 API 表缺 {frag}"


def test_boot_wires_knowledge_window_with_injected_floating_factory() -> None:
    """知识窗与复盘同构：浮窗工厂由 boot 注入（knowledge 全家不 import #shell，防新环）。"""
    assert "initKnowledge({ floating: initFloatingWin," in BOOT, "boot 未注入浮窗工厂"
    assert "beforeOpen: () => closeSettings({ returnFocus: false })" in BOOT
    assert "_closeSurfaces" in KNOWLEDGE
    assert re.search(r'import\s*\{[^}]*\binitKnowledge\b[^}]*\}\s*from\s*"#knowledge"', BOOT), "boot 未从 #knowledge import initKnowledge"
    assert "export function initKnowledge" in KNOWLEDGE
    assert 'from "#shell"' not in KNOWLEDGE, "knowledge.js 不应 import #shell（浮窗工厂经 boot 注入）"


def test_knowledge_core_is_a_pure_separate_module() -> None:
    """纯核零宿主依赖（node 可单测）：DOM/localStorage/墙钟/网络一律不得出现。"""
    for forbidden in ("localStorage", "document", "window", "Date.now(", "new Date",
                      "performance.now", "fetch(", "setTimeout"):
        assert forbidden not in KNOWLEDGE_CORE, f"纯核 knowledge_core.js 不应出现 {forbidden!r}"
    assert "export function knowledgeOperationsFromForm" in KNOWLEDGE_CORE
    assert "module.exports" not in KNOWLEDGE_CORE


def test_window_skeleton_present_and_defaults_to_active_knowledge() -> None:
    """文件夹浮窗骨架：顶部系统/用户/项目、左一级圆点竖栏（项目层）+ 第二竖栏记忆/技能/样例。
    默认看知识成果（active 列表），审查/整理是同级次要入口，不做横排两行 tab 顶替竖栏。"""
    for frag in ('id="knowledgeWin"', 'id="kwScopeTabs"', 'id="kwProjectRail"', 'id="kwKindRail"'):
        assert frag in HTML, f"知识窗缺骨架 {frag}"
    for label in ("系统", "用户", "项目", "记忆", "技能", "样例"):
        assert label in KNOWLEDGE, f"knowledge.js 缺层级/类目标签 {label}"
    assert "knowledge-win" in (ROOT / "web/static/css/app.css").read_text(encoding="utf-8"), "app.css 缺知识窗样式"
    # 默认 = 知识成果：首屏是条目列表（entries 视图），draft/pending 只在审查视图；
    # trace 低强调但失败/候选/冲突可见（三态徽标在纯核，单一真源）
    assert "_view = \"entries\"" in KNOWLEDGE, "知识窗默认视图必须是条目列表（知识成果）"
    assert 'active: "生效中"' in KNOWLEDGE_CORE, "纯核必须定义 active 语义"
    assert "失败" in KNOWLEDGE or "KNOWLEDGE_STATUS_ZH" in KNOWLEDGE


def test_system_scope_is_read_only() -> None:
    """系统层只读内置说明：新建/编辑/撤回/移动/审查/整理的写入口全部不出现在系统层。"""
    assert "system" in KNOWLEDGE, "knowledge.js 没有系统层概念"
    assert "只读" in KNOWLEDGE, "系统层必须如实标注只读"
    for frag in ("propose", "retract", "commit"):
        assert frag in KNOWLEDGE_CORE or frag in KNOWLEDGE, f"知识窗缺 {frag} 语义"
    # 写路径唯一：propose →（评估）→ commit / reject 道闸；不直调旧 approve 端点
    assert "API.knowledgePropose" in KNOWLEDGE, "新建/编辑必须走 knowledge propose 道闸"


def test_scope_identity_guards_and_late_responses_dropped() -> None:
    """层级 scope 唯一真源在 core；scope 变回包丢弃：请求 token + 身份核验，迟回不渲染。"""
    assert "beginRequest" in KNOWLEDGE, "知识窗请求必须走 beginRequest token"
    assert "currentProjectScope" in KNOWLEDGE, "项目 scope 唯一真源必须是 core 的 currentProjectScope"
    assert "isCurrent" in KNOWLEDGE, "迟回包必须核 isCurrent 后整段丢弃"


def test_writes_go_through_the_propose_gate_not_direct_approve() -> None:
    """兼容壳（rules/examples）不能再有第二条直接 approve 通道：批准/驳回/撤回一律过
    knowledge propose/assess/commit/reject；未确认只报「已提交提案」，不谎报生效。"""
    rules = (ROOT / "web/static/js/panel/rules.js").read_text(encoding="utf-8")
    assert "API.rulesApprove" not in rules, (
        "rules.js 还留着直调旧 approve 端点的批准通道（应全部改走 knowledge 道闸）")
    assert "knowledgePropose" in rules, "rules.js 的兼容桥必须改走 knowledge propose"
    assert "已提交提案" in rules or "待确认" in rules, "提案提交必须如实说待确认（不是已生效）"


def test_draft_is_not_referenceable_and_editor_drafts_survive_refresh() -> None:
    """未评估新内容是 draft/pending：不进 active 列表、不可引用；编辑器草稿按
    scope+kind+entry 保留，刷新/换 scope 不丢当前草稿、换 scope 不互串。"""
    assert "draft" in KNOWLEDGE_CORE, "纯核缺 draft 语义"
    assert "pending" in KNOWLEDGE_CORE
    assert "草稿" in KNOWLEDGE or "待确认" in KNOWLEDGE, "界面必须如实呈现草稿/待确认态"
    assert "_draft" in KNOWLEDGE or "_drafts" in KNOWLEDGE or "draft" in KNOWLEDGE, "编辑器草稿没有保留机制"


def test_learning_selection_is_explicit_and_counts_are_real() -> None:
    """旧 dream 独立学习 UI 链删除，改「选择来源并整理」：多项目/多对话 + 本机资料显式勾选
    target 与模型告知，同 loop；阈值控件；成功计数真实刷新（不首 12 项后谎报全归零）。"""
    dream = (ROOT / "web/static/js/panel/dream.js").read_text(encoding="utf-8")
    assert "API.dream" not in dream, "旧 /api/dream 独立学习链必须删除"
    assert "submitLearningTrigger" in dream, "整理必须走学习 trigger 通道（rules 引擎）"
    assert "knowledgeSelectionPayload" in dream, "来源勾选必须经纯核构造（不自动全选）"
    assert "knowledgeThresholdClamp" in dream, "阈值控件必须经纯核钳制"
    assert "knowledgeTriggerCounts" in dream, "成功计数必须直读回执真实字段"
    assert "模型" in dream or "model" in dream, "整理前必须告知将使用的模型"


def test_learning_release_and_stale_token_machinery_survives() -> None:
    """配置释放/授权 stale token 机制不能丢：shell 的配置变更钩子 + keyless GET status →
    CAS release 链原样保留，dream.init 仍注册。"""
    shell = (ROOT / "web/static/js/core/shell.js").read_text(encoding="utf-8")
    rules = (ROOT / "web/static/js/panel/rules.js").read_text(encoding="utf-8")
    dream = (ROOT / "web/static/js/panel/dream.js").read_text(encoding="utf-8")
    assert "setAiConfigurationChangedHook" in shell
    assert "releaseLearningConfiguration" in rules
    assert "expected_config_epoch" in rules, "CAS 释放不能丢配置代际"
    assert "setAiConfigurationChangedHook(releaseLearningConfiguration)" in dream, "dream.init 必须仍注册配置释放处理器"


def test_local_memory_originals_are_kept_and_not_auto_uploaded() -> None:
    """旧本机记忆原件保留不自动上传；已有填框能力保留但不静默改过滤。"""
    memory = (ROOT / "web/static/js/panel/memory.js").read_text(encoding="utf-8")
    assert "fetch(" not in memory, "memory.js 零网络原语契约不变（本机记忆不上传）"
    assert "upsertMemory" in memory and "useMemory" in memory, "本机记忆写入/填框能力必须保留"
    assert "input.dispatchEvent" in memory and "runRecommend" not in memory, "填框只填入、绝不自动提交检索"


def test_logged_out_identity_matches_backend_canonical() -> None:
    """未登录便携模式：后端把本机身份 canonical 为 "anonymous"（learning_scope 别名
    local→anonymous），前端 loadStatus 的身份核验必须接受 canonical 值；旧写法
    `data.identity !== (currentAccountScope() || "local")` 在未登录时恒不匹配，
    会让知识空间 _status 永远为空、变更审查的确认提交永久禁用（2026-10-10 实证）。"""
    assert 'data.identity === "anonymous"' in KNOWLEDGE, (
        "未登录身份核验必须接受后端 canonical 值 anonymous")
    assert 'data.identity !== (currentAccountScope() || "local")' not in KNOWLEDGE, (
        "旧的身份直比写法会让未登录用户审核流假死")
