"use strict";

/* 本文件是 ES Module：core 的存储/工具经 import 取，排序/淘汰/归一由纯核
   #memory_rank import（此前经共享全局裸调）。LAST_RECOMMEND_DATA 自 #search import
   （live binding 只读，函数体内触碰）。 */
import { LS, nsKey, readJSON, writeJSON, $, toast, escapeHtml, fmtTime, currentProjectScope, learningTargetScope, setLearningTargetScope, learningKey, currentAccountScope } from "#core";
import { memRankOrder, memRankEvict, memRankNormalize } from "#memory_rank";
import { LAST_RECOMMEND_DATA } from "#search";
import { approveMemoryAsRule } from "#rules";
import { COPY, armTwoStepConfirm, closeModal, openModal, trapModalFocus } from "../core/copy.js";

const MEMORY_LIMIT = 40;
/* 存储写失败（配额满）的提示：记住需求 / 手工加偏好两处共用，字面量只留一份。 */
const MEMORY_FULL_COPY = "浏览器存储空间已满，保存不了。可先到「历史记录」清空旧记录，或删掉不用的记忆";
let _memoryReturnFocus = null;

export function userMemoryEnabled() {
    // 默认开（缺失/异常均归 true）、键为机器级（刻意不 nsKey，见 test_frontend_namespacing.py）——
    // 与 usage 采集的默认口径相反是产品既定口径；两处都已在设置页文案写明（2026-08-15）。
    try { return localStorage.getItem(LS.memoryEnabled) !== "0"; } catch (_e) { return true; }
}

export function getUserMemories(projectId = currentProjectScope()) {
    const raw = readJSON(learningKey(LS.memory, projectId), []);
    if (!Array.isArray(raw)) return [];
    return raw.filter((item) => item && typeof item === "object" && typeof item.text === "string" && item.text.trim()).map((item) => ({
        scope_project_id: projectId, id: String(item.id || ""),
        kind: item.kind === "note" ? "note" : (item.kind === "dream" ? "dream" : "search"),
        text: item.text.trim(), status: item.status === "draft" ? "draft" : "legacy",
        summary: String(item.summary || "").trim(), createdAt: Number(item.createdAt) || Date.now(),
        updatedAt: Number(item.updatedAt) || Number(item.createdAt) || Date.now(), lastUsedAt: Number(item.lastUsedAt) || 0,
        useCount: Math.max(0, Number(item.useCount) || 0),
        // 已转为协作规则的时间戳（旧标记，规则已迁入知识空间；0=没转过）。
        // 已提交知识提案的时间戳（统一记忆空间批起；0=没提交过）。必须在白名单里：
        // 落盘只写白名单字段，漏了它 → 打标在下一次保存时就静默消失。
        ruleConvertedAt: Number(item.ruleConvertedAt) || 0,
        ruleProposedAt: Number(item.ruleProposedAt) || 0,
    }));   // 不在读取处截断——淘汰策略（偏好防挤出）统一在 saveUserMemories 落盘时决定
}

/* 三类记忆的展示名（AI 整理进来的一律标「AI 整理」，绝不冒充手写偏好）。 */
const MEMORY_KIND_ZH = { note: "研究偏好", search: "已存需求", dream: "AI 整理" };
export function memoryKindZh(kind) { return MEMORY_KIND_ZH[kind] || MEMORY_KIND_ZH.search; }

// 返回是否真正落盘：localStorage 满时 setItem 抛 QuotaExceededError，这里吞掉并返回 false，
// 由显式保存入口给用户可见反馈（而不是像以前那样静默失败、还误报"已保存"）。历史快照(pushHist)
// 是本地存储的主要占用者，它自己会逐步剥离自愈，故此路径极少触发。
export function saveUserMemories(items, projectId = currentProjectScope()) {
    const previous = readJSON(learningKey(LS.memory, projectId), []);
    const semantic = (row) => JSON.stringify([row.kind, row.text, row.summary || ""]);
    const rows = items.flatMap((item) => {
        const old = previous.find((row) => row.id === item.id);
        const unchanged = old && semantic(old) === semantic(item);
        if (old && !unchanged && old.status !== "draft") {
            return [{ ...old, status: "legacy" }, { ...item, id: globalThis.crypto.randomUUID(), status: "draft", original_id: old.id }];
        }
        return [{ ...old, ...item, status: unchanged && old.status !== "draft" ? "legacy" : "draft" }];
    });
    try { writeJSON(learningKey(LS.memory, projectId), rows); return true; }
    catch (_e) { return false; }
}
function normalizedMemoryText(value) { return memRankNormalize(value); }   // 去重键与排序核共用单一归一

function constraintSummary(data) {
    const groups = Array.isArray(data && data.query_constraints) ? data.query_constraints : [];
    const parts = groups.map((group) => {
        const values = Array.isArray(group.values) ? group.values : (group.value ? [group.value] : []);
        const shown = values.map((v) => typeof v === "object" ? (v.display || v.value || "") : v).filter(Boolean);
        return shown.length ? `${group.label || group.dim || "条件"}：${shown.join("、")}` : "";
    }).filter(Boolean);
    return parts.length ? parts.join("；") : "未识别到可保存的筛选条件";
}

export function upsertMemory(kind, text, summary, projectId = currentProjectScope()) {
    text = String(text || "").trim();
    if (!text) return null;
    const items = getUserMemories(projectId);
    const key = `${kind}|${normalizedMemoryText(text)}`;
    const now = Date.now();
    const existing = items.find((item) => `${item.kind}|${normalizedMemoryText(item.text)}` === key);
    if (existing) {
        existing.summary = summary || existing.summary;
        existing.updatedAt = now;
        existing.lastUsedAt = now;
        existing.useCount += 1;
        items.splice(items.indexOf(existing), 1);
        items.unshift(existing);
        return saveUserMemories(items, projectId) ? existing : null;
    }
    const item = { id: `${now}-${Math.random().toString(36).slice(2, 8)}`, kind, text: text.slice(0, 500), summary: String(summary || "").slice(0, 500), createdAt: now, updatedAt: now, lastUsedAt: now, useCount: 1 };
    items.unshift(item);
    return saveUserMemories(items, projectId) ? item : null;
}

export function setRememberSearchAvailable(available) {
    const btn = $("rememberSearchBtn");
    if (!btn) return;
    btn.hidden = !available;
    btn.disabled = !available || !userMemoryEnabled();
    btn.title = userMemoryEnabled() ? "保存这次检索和识别出的条件" : "请先在设置中开启用户记忆";
}

export function rememberLatestSearch() {
    const query = ($("queryInput").value || "").trim();
    if (!userMemoryEnabled()) { toast("请先在设置中开启用户记忆"); return; }
    if (!query || !LAST_RECOMMEND_DATA) { toast("请先完成一次检索"); return; }
    const item = upsertMemory("search", query, constraintSummary(LAST_RECOMMEND_DATA));
    if (!item) { toast(MEMORY_FULL_COPY); return; }
    toast("已保存本机需求草稿；未经评估，不会被引用");
    renderMemorySuggestions(); renderMemoryManager();
}

// 相关性打分 / 分词（CJK bi-gram）/ 分层 / 淘汰 全在纯核 memory_rank.js（memRank*，
// 无 DOM/localStorage/Date、node 可单测）。这里只做 DOM 接线与 localStorage 读写。

let _useMemoryArm = { id: "", until: 0 };   // 覆盖填入的二次确认窗口（3 秒内同一条再次点击才真覆盖）

export function useMemory(item) {
    const input = $("queryInput");
    // 输入框已有**不同**内容时不直接覆盖：第一次点击只提醒，3 秒内再次点击同一条才填入。
    // 全文查询场景下「追加」会拼出垃圾查询，故采用覆盖前确认而非追加。
    const stored = getUserMemories(item.scope_project_id || "").find((row) => row.id === item.id);
    if (!stored || stored.status !== "legacy" || stored.text !== item.text || stored.kind !== item.kind || stored.summary !== item.summary) {
        toast("这份本机资料是未评估草稿，不能填入查询。请显式提交提案并在知识空间确认。"); return;
    }
    const existing = (input.value || "").trim();
    if (existing && existing !== item.text.trim()) {
        const now = Date.now();
        if (!(_useMemoryArm.id === item.id && now < _useMemoryArm.until)) {
            _useMemoryArm = { id: item.id, until: now + 3000 };
            toast("输入框已有内容，再次点击该条将覆盖填入");
            return;
        }
    }
    _useMemoryArm = { id: "", until: 0 };
    input.value = item.text;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.focus();
    const items = getUserMemories(item.scope_project_id || "");
    const found = items.find((x) => x.id === item.id);
    if (found) { found.lastUsedAt = Date.now(); found.useCount += 1; saveUserMemories(items, item.scope_project_id || ""); }
    if (!$("memoryModal").hidden) closeMemoryModal();
    renderMemorySuggestions();
}

export function renderMemorySuggestions() {
    const box = $("memorySuggestions"), list = $("memorySuggestionList");
    if (!box || !list || !userMemoryEnabled()) { if (box) box.hidden = true; return; }
    const query = ($("queryInput").value || "").trim();
    // 召回 Top3：确定性智能排序（IDF 区分度 + useCount 强化 + 时间衰减 + 偏好分层）在纯核。
    const scope = currentProjectScope();
    const candidates = scope ? [...getUserMemories(scope), ...getUserMemories("")] : getUserMemories("");
    const seen = new Set();
    const effective = candidates.filter((item) => { if (item.status !== "legacy") return false; const key = item.kind + "|" + normalizedMemoryText(item.text); if (seen.has(key)) return false; seen.add(key); return true; });
    const items = memRankOrder(effective, query, Date.now()).slice(0, 3);
    box.hidden = !items.length;
    list.innerHTML = "";
    items.forEach((item) => {
        const button = document.createElement("button");
        button.type = "button"; button.className = "memory-suggestion";
        button.innerHTML = `<span>${memoryKindZh(item.kind)}</span><strong>${escapeHtml(item.text)}</strong>`;
        button.addEventListener("click", () => useMemory(item));
        list.appendChild(button);
    });
}

export function renderMemoryManager() {
    const targetScope = learningTargetScope(), owner = currentAccountScope();
    const list = $("memoryList");
    if (!list) return;
    const items = getUserMemories(targetScope);
    $("memoryCount").textContent = String(items.length);
    list.innerHTML = "";
    if (!items.length) { list.innerHTML = `<div class="memory-empty">还没有记忆。在上方添加研究偏好（例如你常关注的物种、组织或平台）后，检索时会显示为可点击的建议。</div>`; return; }
    items.forEach((item) => {
        const row = document.createElement("article"); row.className = "memory-item";
        // 已提交知识提案（统一记忆空间批起）与已转为协作规则（旧标记，规则已迁入知识空间）
        // 都**打标保留**（不静默删：历史不丢，用户可手动再删）——标记说的是「这条已经进
        // 知识空间」，确认/撤回入口在「知识空间」浮窗（本弹窗头部的「知识空间」按钮）。
        const ruleMark = item.ruleProposedAt
            ? `<small>已提交知识提案 · 在「知识空间」确认后才生效</small>`
            : (item.ruleConvertedAt
                ? `<small>已转为协作规则 · 在「知识空间」查看与撤回</small>` : "");
        const toRule = `<button class="btn memory-to-rule" type="button">提交知识提案</button>`;
        const stateLabel = item.status === "draft" ? "本机草稿 · 不可引用" : "旧资料 · 未改版本";
        row.innerHTML = `<div class="memory-item-copy"><span>${memoryKindZh(item.kind)} · ${stateLabel}</span><strong>${escapeHtml(item.text)}</strong>${item.summary ? `<p>${escapeHtml(item.summary)}</p>` : ""}<small>使用 ${item.useCount} 次 · ${escapeHtml(fmtTime(item.updatedAt))}</small>${ruleMark}</div><div class="memory-item-actions"><button class="btn memory-use" type="button">填入查询</button>${item.kind === "dream" ? `<button class="btn memory-promote" type="button">存为偏好草稿</button>` : ""}${toRule}<button class="btn memory-delete" type="button">删除</button></div>`;
        row.querySelector(".memory-use").disabled = item.status !== "legacy";
        row.querySelector(".memory-use").addEventListener("click", () => useMemory(item));
        // 「存为偏好」：AI 整理的工作层条目转正为研究偏好（持久层、永不自动淘汰）。
        const promote = row.querySelector(".memory-promote");
        if (promote) promote.addEventListener("click", () => {
            const items2 = getUserMemories(targetScope);
            const found = items2.find((x) => x.id === item.id);
            if (found) { found.kind = "note"; saveUserMemories(items2, targetScope); }
            renderMemoryManager(); renderMemorySuggestions(); toast("已存为研究偏好草稿；未经评估，不会被引用");
        });
        // 「提交知识提案」：咨询层 → 知识空间的**显式**桥（逐条、二段确认；网络在 #rules 里，
        // 本文件保持零网络原语）。统一记忆空间批起走 knowledge propose 道闸（kind=skill、
        // tier=explicit、evidence=原话、request_id 幂等）——只报「已提交提案」，确认在知识窗；
        // 提交成功才打 ruleProposedAt 标（没提案就不能说「已提交」）。
        const bridge = row.querySelector(".memory-to-rule");
        if (bridge) bridge.addEventListener("click", async (e) => {
            const btn = e.currentTarget;
            if (!armTwoStepConfirm(btn, { idleText: "提交知识提案", confirmText: COPY.common.confirmConvertRule })) return;
            const result = await approveMemoryAsRule(item.text, targetScope, "memrule-" + item.id);
            if (owner !== currentAccountScope()) return;
            if (!result || !result.pending) return;
            const items2 = getUserMemories(targetScope);
            const found = items2.find((x) => x.id === item.id);
            const marked = found ? (found.ruleProposedAt = Date.now(), saveUserMemories(items2, targetScope)) : false;
            renderMemoryManager();
            if (!marked) { toast("已提交知识提案，但这台浏览器的存储满了、这条打标没存下（提案本身已提交，在「知识空间」确认）"); return; }
            toast("已提交知识提案：在「知识空间」确认后生效");
        });
        // 单条删除与「清空全部」同款二次确认：研究偏好属持久层、防挤出，误点即删不可恢复。
        // 删除只动本机这条记录；若它已提交过知识提案，提案在服务端道闸里，撤回要去知识空间——如实说明。
        row.querySelector(".memory-delete").addEventListener("click", (e) => {
            const btn = e.currentTarget;
            if (!armTwoStepConfirm(btn, { idleText: COPY.common.delete })) return;
            saveUserMemories(getUserMemories(targetScope).filter((x) => x.id !== item.id), targetScope);
            renderMemoryManager(); renderMemorySuggestions();
            toast("已删除这条本机资料" + ((item.ruleProposedAt || item.ruleConvertedAt) ? "；已提交的提案不受影响，撤回请到「知识空间」" : ""));
        });
        list.appendChild(row);
    });
}

export function openMemoryModal(trigger) {
    _memoryReturnFocus = trigger || document.activeElement;
    const input = $("memoryNoteInput"); input.value = "";
    openModal($("memoryModal"), { returnFocus: _memoryReturnFocus, initialFocus: input });
    renderMemoryManager();
}

export function closeMemoryModal() {
    const modal = $("memoryModal"); if (!closeModal(modal)) return;
    $("memoryClearBtn").textContent = "清空全部";
}

export function initUserMemory() {
    const scopeSelect = $("memoryScopeSelect");
    if (scopeSelect) scopeSelect.addEventListener("change", () => setLearningTargetScope(scopeSelect.value));
    const enabled = $("memoryEnabled");
    enabled.checked = userMemoryEnabled();
    enabled.addEventListener("change", () => {
        try { localStorage.setItem(LS.memoryEnabled, enabled.checked ? "1" : "0"); } catch (_e) {}
        setRememberSearchAvailable(!!LAST_RECOMMEND_DATA); renderMemorySuggestions();
        toast(enabled.checked ? "用户记忆已开启" : "用户记忆已关闭（已有内容仍保留）");
    });
    // 「记住这次需求」按钮已从结果头移除；绑定加 null 守卫（否则 initUserMemory 里 $()==null 抛错、整个前端启动崩）。
    // rememberLatestSearch / constraintSummary 保留（记忆能力不删，待需要时挂新入口；用户记忆契约测试亦钉存在）。
    { const rsb = $("rememberSearchBtn"); if (rsb) rsb.addEventListener("click", rememberLatestSearch); }
    $("memoryManageBtn").addEventListener("click", (e) => openMemoryModal(e.currentTarget));
    $("memoryModalClose").addEventListener("click", closeMemoryModal);
    $("memoryModal").addEventListener("click", (e) => { if (e.target === $("memoryModal")) closeMemoryModal(); });
    $("memoryAddForm").addEventListener("submit", (e) => {
        e.preventDefault(); const input = $("memoryNoteInput"); const text = input.value.trim();
        if (!text) { toast("请先写下研究偏好"); return; }
        if (text.length > 300) { toast("研究偏好请控制在 300 字以内，写最关键的方向即可"); return; }
        if (!upsertMemory("note", text, "用户手工添加", learningTargetScope())) { toast(MEMORY_FULL_COPY); return; }
        input.value = ""; renderMemoryManager(); renderMemorySuggestions(); toast("已添加研究偏好草稿；未经评估，不会被引用");
    });
    $("memoryClearBtn").addEventListener("click", () => {
        const btn = $("memoryClearBtn");
        if (!armTwoStepConfirm(btn, { idleText: "清空全部", confirmText: COPY.common.confirmClear })) return;
        const hadProposal = getUserMemories(learningTargetScope()).some((x) => x.ruleProposedAt || x.ruleConvertedAt);
        saveUserMemories([], learningTargetScope()); renderMemoryManager(); renderMemorySuggestions();
        toast("本机资料已清空" + (hadProposal ? "；已提交的提案不受影响，撤回请到「知识空间」" : ""));
    });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("memoryModal").hidden) closeMemoryModal(); });
    // 焦点陷阱：模态开着时 Tab / Shift+Tab 在模态内循环，不逃到背景控件（对齐 #settings 的隔离语义）。
    $("memoryModal").addEventListener("keydown", (e) => trapModalFocus(e, $("memoryModal")));
    renderMemorySuggestions();
}
