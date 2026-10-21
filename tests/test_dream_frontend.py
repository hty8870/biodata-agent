# -*- coding: utf-8 -*-
"""dream 前端静态门：整理视图接线、旧链删除、端点声明、诚实语义（统一记忆空间批更新）。"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
CORE = (ROOT / "web/static/js/core/core.js").read_text(encoding="utf-8")
DREAM = (ROOT / "web/static/js/panel/dream.js").read_text(encoding="utf-8")
DREAM_CORE = (ROOT / "web/static/js/panel/dream_core.js").read_text(encoding="utf-8")
MEMORY = (ROOT / "web/static/js/panel/memory.js").read_text(encoding="utf-8")
BOOT = (ROOT / "web/static/js/core/boot.js").read_text(encoding="utf-8")


def test_dream_js_loaded_as_es_module_with_cache_token() -> None:
    """dream.js 是 ES Module——必须 type=module 且带缓存令牌。"""
    m = re.search(r'<script type="module" src="(/static/js/panel/dream\.js\?v=[0-9a-z-]+)"></script>', HTML)
    assert m, "index.html 里没有以 type=module + ?v= 加载 dream.js"


def test_dream_core_loaded_before_memory() -> None:
    """dream_core.js（纯核）必须先于 dream.js 可用。"""
    assert HTML.index("dream_core.js?v=") < HTML.index('type="module" src="/static/js/panel/dream.js')


def test_old_dream_endpoint_chain_is_deleted() -> None:
    """旧 /api/dream 独立学习 UI 链整体删除：端点不再登记、无知情同意入口、无按钮绑定、
    dreamBlock 骨架删除；dreamWriteAccepted 只作为规格兼容的守卫写路径保留（无 UI 入口）。"""
    assert 'dream: "/api/dream"' not in CORE, "core.js 的 API 表还留着 /api/dream（浏览器已不再调用）"
    assert "API.dream" not in DREAM, "dream.js 还调旧 /api/dream 链"
    assert "dreamConsentGiven" not in DREAM and "dreamConsentGive" not in DREAM, "旧 dream 知情同意链已删除（改由整理视图模型告知）"
    assert 'id="dreamBlock"' not in HTML and 'id="dreamRunBtn"' not in HTML, "旧 dreamBlock 骨架必须删除"
    assert 'addEventListener("click", dreamRun)' not in DREAM, "旧 dream 运行按钮绑定必须删除"


def test_boot_calls_init_dream() -> None:
    assert "initDream();" in BOOT, "boot.js 没有调 initDream"


def test_dream_js_is_a_pure_module_and_boot_imports_it() -> None:
    """dream.js 不挂 window，boot.js（同为 ESM）经 import 取 initDream。"""
    assert "export function initDream" in DREAM, "dream.js 必须导出 initDream（boot 经 import 取）"
    assert "Object.assign(window" not in DREAM, "dream.js 的 window 桥接已退役"
    assert re.search(r'import\s*\{[^}]*\binitDream\b[^}]*\}\s*from\s*"#dream"', BOOT), (
        "boot.js 没有从 #dream import initDream"
    )


def test_learning_selection_is_explicit_and_model_is_disclosed() -> None:
    """整理 = 显式选择来源：多项目/多对话逐段勾选 + 本机资料，target 项目显式选择，
    模型告知；来源为空不自动全选（knowledgeSelectionPayload 纯核已钉，这里钉接线）。"""
    assert "learningTrigger" not in DREAM or "submitLearningTrigger" in DREAM, "整理必须走学习 trigger 通道"
    assert "knowledgeSelectionPayload" in DREAM, "来源勾选必须经纯核构造（不自动全选）"
    assert "data-kw-conv" in DREAM, "对话来源必须逐段勾选"
    assert "本机资料" in DREAM, "本机资料入口必须有"
    assert "workspaceProjects" in DREAM, "多项目来源必须列项目"
    assert "整理到" in DREAM, "target 项目必须显式选择"
    assert "cfg.model" in DREAM or "model" in DREAM, "整理前必须告知将使用的模型"
    assert "不会自动上传" in DREAM, "必须明说旧记忆原件不会自动上传"


def test_learning_threshold_and_real_counts() -> None:
    """阈值控件同 loop；成功计数直读回执真实字段（不首 12 项后谎报全归零）。"""
    assert "knowledgeThresholdClamp" in DREAM, "阈值必须经纯核钳制"
    assert "submitLearningControl" in DREAM, "阈值保存必须走学习 control 通道"
    assert "knowledgeTriggerCounts" in DREAM, "成功计数必须直读回执真实字段"
    assert "partial" in DREAM or "remaining_selected_count" in DREAM, "部分完成必须如实呈现"
    assert "request_id" in DREAM, "手动触发必须带 request_id 幂等"


def test_dream_core_keeps_pure_exports_and_gains_grouping() -> None:
    """纯核既有导出保留（dream_core_spec.mjs 不动的硬契约）+ 新分组纯函数（零依赖）。"""
    for fn in ("dreamNormalize", "dreamCollectConversations", "dreamClipConversations",
               "dreamFilterNew", "dreamGroupByProject"):
        assert f"export function {fn}" in DREAM_CORE, f"dream_core 缺导出 {fn}"
    for forbidden in ("localStorage", "document", "Date.now(", "new Date", "fetch("):
        assert forbidden not in DREAM_CORE, f"纯核 dream_core.js 不应出现 {forbidden!r}"


def test_memory_accepts_dream_kind_and_badges_it() -> None:
    assert 'item.kind === "dream"' in MEMORY, "getUserMemories 不接受 dream kind"
    assert 'dream: "AI 整理"' in MEMORY, "dream 条目必须徽标「AI 整理」（诚实标识，不冒充手写）"
    assert "memory-promote" in MEMORY, "dream 条目缺「存为偏好」转正入口"


# ---------------------------------------------------------------- 知识窗与兼容壳（2026-10-09）
#
# 规则/样例的候选与生效列表搬进知识窗（#knowledgeWin：变更审查 = knowledge 变更道闸）；
# rules/examples 只留引擎与兼容壳导出名。这里静态钉住：兼容壳不保留第二条直接 approve
# 通道、批准改走 knowledge propose、记忆弹窗不再渲染旧规则/样例块。

RULES = (ROOT / "web/static/js/panel/rules.js").read_text(encoding="utf-8")
EXAMPLES = (ROOT / "web/static/js/panel/examples.js").read_text(encoding="utf-8")
PACKAGE = (ROOT / "package.json").read_text(encoding="utf-8")


def test_rules_js_registered_as_module_with_cache_token_and_importmap_key() -> None:
    m = re.search(r'<script type="module" src="(/static/js/panel/rules\.js\?v=[0-9a-z-]+)"></script>', HTML)
    assert m, "index.html 里没有以 type=module + ?v= 加载 rules.js"
    assert '"#rules":' in HTML, "index.html importmap 缺 #rules 键"
    assert '"#rules":' in (ROOT / "web/static/dataset.html").read_text(encoding="utf-8"), (
        "dataset.html importmap 缺 #rules 键——parity 门只查被使用的 specifier，两页都要有映射")
    assert '"#rules"' in PACKAGE, "package.json imports 缺 #rules（import 图门按它解析）"


def test_memory_modal_stays_fetch_free_and_no_longer_renders_old_blocks() -> None:
    """记忆弹窗回归纯咨询档：零网络原语契约不变；规则/样例块移入知识窗，不再随弹窗渲染。"""
    assert "fetch(" not in MEMORY, "memory.js 必须保持零网络原语（提案的网络在 rules.js）"
    assert 'from "#rules"' in MEMORY, "memory.js 的过桥网络仍在 #rules"
    assert "renderCollabRules()" not in MEMORY, "记忆弹窗不再渲染旧规则块（移入知识窗）"
    assert "renderExampleCandidates" not in MEMORY, "记忆弹窗不再渲染旧样例块（移入知识窗）"
    assert 'id="rulesBlock"' not in HTML and 'id="examplesBlock"' not in HTML, "旧规则/样例块骨架必须删除"


def test_compat_shells_keep_names_but_drop_the_direct_approve_route() -> None:
    """兼容壳：renderCollabRules / renderExampleCandidates / approveMemoryAsRule 名字保留，
    但不再直调旧 /api/rules/approve 与 /api/curate-examples/*——批准只有 knowledge 道闸
    （rulesRevoke 撤回兜底保留：撤回不是批准，旧账本行只在新通道不可用时兜底）。"""
    assert "API.rulesApprove" not in RULES, "rules.js 还留着直调旧 approve 端点（批准必须走 knowledge 道闸）"
    assert "API.curateExamplesApprove" not in EXAMPLES and "API.curateExamplesDismiss" not in EXAMPLES, (
        "examples.js 还留着旧入库通道（应并入知识窗 kind=example）")
    assert "export async function renderCollabRules" in RULES, "rules.js 兼容壳缺 renderCollabRules"
    assert "export async function renderExampleCandidates" in EXAMPLES, "examples.js 兼容壳缺 renderExampleCandidates"
    assert "setExamplesCompatRefresh" in EXAMPLES, (
        "examples 兼容壳必须走注册反转刷新知识窗，不 import #knowledge（防新环）")


def test_rule_bridge_goes_through_knowledge_propose_and_reports_pending() -> None:
    """「转为协作规则」桥 = knowledge propose（kind=skill、tier=explicit、evidence=原话、
    request_id 幂等）；只报「已提交提案」，绝不谎报「已生效」。"""
    assert "API.knowledgePropose" in RULES, "过桥必须走 knowledge propose 道闸"
    fn = re.search(r"export async function approveMemoryAsRule\((.*?)\n\}", RULES, re.S)
    assert fn, "rules.js 缺 approveMemoryAsRule"
    body = fn.group(1)
    assert "_skillProposePayload" in body, "提案载荷必须经共享构造（与候选确认同一形状）"
    assert "tier === \"implicit\" ? \"implicit\" : \"explicit\"" in RULES, "tier 必须 explicit 语义"
    assert 'kind: "memory"' in RULES, "持久偏好应为 memory，不伪装成没有工具的 skill"
    assert "knowledgeDedupRequestId" in RULES
    assert "payload: null" not in RULES
    assert "source_refs" in RULES, "过桥必须带 user 来源（原话）"
    assert "pending" in body, "过桥回执必须如实报 pending（待确认），不是 approved"
    assert "API.rulesApprove" not in body


def test_rules_js_keeps_learning_engine_cas_and_release_machinery() -> None:
    """学习引擎与配置释放不丢：status/control/trigger 的 expected_identity +
    expected_config_epoch CAS、授权代际、keyless 释放链原样保留。"""
    assert "expected_config_epoch" in RULES, "CAS 不能丢配置代际"
    assert "expected_identity" in RULES
    assert "releaseLearningConfiguration" in RULES and "setLearningConfigProvider" in RULES
    assert "learningStatusSnapshot" in RULES and "loadLearningStatus" in RULES
    assert "submitLearningTrigger" in RULES and "submitLearningControl" in RULES
    assert 'API.learningStatus' in RULES and 'API.learningControl' in RULES and 'API.learningTrigger' in RULES
    assert "beginRequest" in RULES and "isCurrent" in RULES, "迟回包必须核 token 后丢弃"


def test_endpoint_table_dropped_dead_routes_and_registered_knowledge() -> None:
    """core.js API 表：浏览器不再调用的端点不登记（dream/curate-examples/rules approve 批准通道）；
    只读面（rulesList）与撤回兜底（rulesRevoke）保留；knowledge 七端点登记。"""
    for dead in ('dream: "/api/dream"', 'curateExamplesPending', 'rulesApprove: "/api/rules/approve"'):
        assert dead not in CORE, f"API 表还留着浏览器死端点 {dead}"
    for kept in ('rulesList: "/api/rules/list"', 'rulesRevoke: "/api/rules/revoke"',
                 'learningCandidates: "/api/learning/candidates"'):
        assert kept in CORE, f"API 表误删了读取/兜底端点 {kept}"
    for live in ('knowledgeStatus: "/api/knowledge/status"', 'knowledgeEntries: "/api/knowledge/entries"',
                 'knowledgeChanges: "/api/knowledge/changes"', 'knowledgePropose: "/api/knowledge/propose"',
                 'knowledgeAssess: "/api/knowledge/assess"', 'knowledgeCommit: "/api/knowledge/commit"',
                 'knowledgeReject: "/api/knowledge/reject"'):
        assert live in CORE, f"API 表缺知识端点 {live}"
