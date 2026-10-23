"use strict";

/* ============================================================================
 * knowledge.js —— 统一知识窗界面层
 * ----------------------------------------------------------------------------
 * 文件夹式浮窗 #knowledgeWin（骨架属主 shell.js initFloatingWin，由 boot 注入，
 * 与复盘同构——本模块不 import #shell，防新环）：
 *   顶部：系统 / 用户 / 项目 层级（层级 scope 唯一真源在 core：currentProjectScope）。
 *   项目层：左一级圆点竖栏（每项目一点，点=切换项目，workspaceEnsureSelect 统一入口）。
 *   第二竖栏：记忆 / 技能 / 样例 + 变更审查 / 整理（次级入口）。
 *   右侧：文件列表 + 编辑；系统层只读内置说明。
 *
 * 道闸语义（冻结合同）：一切写入走 /api/knowledge/propose（create/update/retract/move），
 * 变更审查页逐条 评估→确认提交/驳回；未确认的内容是 draft/pending，不进 active 列表、
 * 不可引用。失败/候选/冲突三态可见；trace 摘要低强调。所有请求带 beginRequest token +
 * 身份核验——scope 变回包整段丢弃；编辑器草稿按 scope+kind+entry 保留，刷新/切视图不丢。
 * ========================================================================== */

import { API, $, escapeHtml, toast, currentAccountScope, currentProjectScope,
    beginRequest, invalidateRequest, registerContextChanged } from "#core";
import { knowledgeEntryFromServer, knowledgeChangeFromServer, knowledgeEntryBadge,
    knowledgeKindZh, knowledgeScopeLevelOf, KNOWLEDGE_KINDS, KNOWLEDGE_OP_ZH,
    KNOWLEDGE_TIER_ZH, KNOWLEDGE_SCOPE_ZH, knowledgeOperationsFromForm,
    knowledgeRetractOperation, knowledgeMoveOperation, knowledgeChangesVisibility,
    knowledgeTraceSummary, knowledgeCommitReady, knowledgeDedupRequestId,
    knowledgePayloadText, knowledgeDraftFromEntry, knowledgeUserSources,
    knowledgeRelationSummary, knowledgeBodyText } from "#knowledge_core";
import { endpointCfg, setExamplesCompatRefresh } from "#examples";
import { workspaceProjects, workspaceEnsureSelect } from "#workspaces";
import { workspaceProjectColor } from "#workspaces_core";
import { COPY, armTwoStepConfirm, closeModal, setStatusLine, apiErrorText } from "../core/copy.js";

/* 学习配置读法（getConfig）由 dream.init 经 setKnowledgeConfigProvider 注入——
   与 rules.setLearningConfigProvider 同一模式；本模块不 import #shell。 */
let _configProvider = null;
export function setKnowledgeConfigProvider(fn) {
    _configProvider = (typeof fn === "function") ? fn : null;
}

let _ctrl = null;
let _scopeLevel = "user";           // system | user | project（层级由本窗口选择）
let _kind = "memory";               // memory | skill | example
let _view = "entries";              // entries | review | learn
let _status = null;                 // /api/knowledge/status 回执（catalog_generation / config_epoch）
let _entries = [];
let _selectedId = "";
let _changes = [];
let _statusError = "";
const _requests = {};
let _mutationToken = null;
function beginMutation() {
    if (_mutationToken?.isCurrent()) { kwStatus("上一项变更正在提交，请稍后再试。", true); return null; }
    _mutationToken = beginRequest(_requests, identity);
    return _mutationToken;
}
function finishMutation(token) { if (_mutationToken === token) _mutationToken = null; }
const _readRequests = { status: {}, entries: {}, changes: {} };
/* 变更审查动作（评估/确认提交/驳回）按「动作 + change_id」各自单飞：
   同一条同一动作在途时再点直接拒绝并保留按钮禁用；不同变更/动作互不使对方失效
   （旧实现共用 _requests owner，连击/跨动作会互相 invalidate、吞掉先完成者的回执）。 */
const _reviewRequests = new Map();
function beginReviewAction(action, change, btn) {
    const key = action + ":" + String(change && change.change_id || "");
    let slot = _reviewRequests.get(key);
    if (slot && slot.token && slot.token.isCurrent()) {
        kwStatus("这条变更的上一操作还在进行，请稍候。", true);
        return null;
    }
    if (!slot) { slot = { owner: {}, token: null }; _reviewRequests.set(key, slot); }
    slot.token = beginRequest(slot.owner, identity);
    if (btn) btn.disabled = true;
    return slot.token;
}
function endReviewAction(action, change, token, btn) {
    const slot = _reviewRequests.get(action + ":" + String(change && change.change_id || ""));
    if (slot && slot.token === token) slot.token = null;
    if (btn && btn.isConnected !== false) btn.disabled = false;
}
const _drafts = new Map();
const _reviewFeedback = new Map();
const _proposeRequestIds = new Map();
let _closeSurfaces = null, _returnFocus = null;
let _message = { text: "", error: false };

function scopeKey() {
    return JSON.stringify([currentAccountScope(), _scopeLevel, projectId(),
        endpointCfg().base_url, endpointCfg().model, endpointCfg().provider]);
}
function identity() { return JSON.stringify([scopeKey(), configSnapshot().use_llm, configSnapshot().api_key]); }
function endpointQuery() {
    const cfg = endpointCfg();
    return "base_url=" + encodeURIComponent(cfg.base_url || "") + "&model=" + encodeURIComponent(cfg.model || "")
        + "&project_id=" + encodeURIComponent(projectId()) + "&provider=" + encodeURIComponent(cfg.provider || "");
}
function projectId() { return _scopeLevel === "project" ? currentProjectScope() : ""; }
function isSystem() { return _scopeLevel === "system"; }
function isOpen() { return !!(_ctrl && _ctrl.isOpen()); }
function kwStatus(text, isError) {
    _message = { text, error: !!isError };
    setStatusLine($("kwStatus"), text, isError);
}
function responseError(data) {
    const error = new Error(apiErrorText(data));
    error.receipt = data;
    return error;
}
async function _get(url) {
    const res = await fetch(url), data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw responseError(data);
    return data;
}
async function _post(url, body) {
    const cfg = endpointCfg();
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: projectId(), base_url: cfg.base_url, model: cfg.model, provider: cfg.provider, ...body }) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw responseError(data);
    return data;
}
function configSnapshot() { return _configProvider ? _configProvider() : {}; }

async function readPages(url, token) {
    const items = [], seen = new Set();
    let offset = 0, generation = null;
    do {
        if (seen.has(offset)) throw new Error("分页游标重复，请刷新后重试");
        seen.add(offset);
        const data = await _get(url + "&offset=" + offset + "&limit=100");
        if (!token.isCurrent()) return null;
        if (generation !== null && generation !== data.catalog_generation) throw new Error("读取期间知识已变化，请刷新后重试");
        generation = data.catalog_generation;
        items.push(...(data.items || []));
        offset = data.next_offset;
        if (offset != null && (!Number.isSafeInteger(offset) || offset < 0)) throw new Error("分页游标无效");
    } while (offset != null);
    return items;
}
async function loadStatus(quiet) {
    const token = beginRequest(_readRequests.status, identity);
    try {
        const data = await _get(API.knowledgeStatus + "?" + endpointQuery());
        if (!token.isCurrent()) return;
        const accountScope = currentAccountScope();
        // 未登录时后端把本机身份 canonical 为 "anonymous"（learning_scope._PRINCIPAL_ALIASES:
        // local→anonymous）；旧写法期望 "local" 会让便携/未登录用户永远拿不到状态、审核流假死。
        const identityOk = accountScope ? data.identity === accountScope
            : data.identity === "anonymous" || data.identity === "local";
        if (!identityOk) throw new Error("账户状态不匹配，请刷新");
        _status = data; _statusError = "";
    } catch (err) {
        if (!token.isCurrent()) return;
        _status = null; _statusError = String(err.message || err);
        kwStatus("状态未刷新：" + _statusError, true);
    }
}
async function loadEntries() {
    const token = beginRequest(_readRequests.entries, () => identity() + _kind);
    if (_view === "entries") {
        const box = renderToolbar("加载中…", [{ label: "刷新", fn: refreshAll }]);
        const list = document.createElement("div"); list.id = "kwList"; list.appendChild(_loadingNode()); box.appendChild(list);
    }
    try {
        const items = await readPages(API.knowledgeEntries + "?" + endpointQuery()
            + "&scope_level=" + encodeURIComponent(_scopeLevel) + "&kind=" + encodeURIComponent(_kind)
            + "&status=&include_inherited=" + (_scopeLevel === "project" ? "true" : "false"), token);
        if (!token.isCurrent() || !items) return;
        _entries = items.map(knowledgeEntryFromServer);
        if (_selectedId && !_entries.some((e) => e.id === _selectedId)) _selectedId = "";
        if (_view === "entries") renderEntriesView();
    } catch (err) { if (token.isCurrent() && _view === "entries") renderError(String(err.message || err)); }
}
async function loadChanges() {
    const token = beginRequest(_readRequests.changes, identity);
    try {
        const items = await readPages(API.knowledgeChanges + "?" + endpointQuery() + "&status=", token);
        if (!token.isCurrent() || !items) return;
        _changes = items.map(knowledgeChangeFromServer);
        if (_view === "review") renderReviewView();
    } catch (err) { if (token.isCurrent() && _view === "review") renderReviewView(String(err.message || err)); }
}

/* ---------- 视图渲染 ---------- */
function _loadingNode() {
    const el = document.createElement("div");
    el.className = "kw-loading"; el.textContent = "加载中…";
    return el;
}
function renderError(message) {
    const list = $("kwList");
    if (!list) return;
    list.innerHTML = "";
    const el = document.createElement("div");
    el.className = "kw-empty";
    el.textContent = "加载失败：" + message + "。可以点「刷新」重试。";
    list.appendChild(el);
}

function draftKey(entryId) { return scopeKey() + "|" + _kind + "|" + (entryId || "new"); }
function draftOf(entryId) {
    const key = draftKey(entryId);
    if (!_drafts.has(key)) _drafts.set(key, { ...knowledgeDraftFromEntry(_entries.find((e) => e.id === entryId), _kind), intentId: globalThis.crypto.randomUUID() });
    return _drafts.get(key);
}
function saveDraft(entryId, values) { _drafts.set(draftKey(entryId), { ...draftOf(entryId), ...values }); }
function clearDraft(entryId) { _drafts.delete(draftKey(entryId)); }

function renderToolbar(title, actions) {
    const box = $("kwContent");
    box.innerHTML = "";
    const bar = document.createElement("div");
    bar.className = "kw-toolbar";
    const head = document.createElement("span");
    head.className = "kw-title"; head.textContent = title;
    bar.appendChild(head);
    (actions || []).forEach(({ label, cls, fn, id }) => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "btn" + (cls ? " " + cls : "");
        b.textContent = label;
        if (id) b.id = id;
        b.addEventListener("click", fn);
        bar.appendChild(b);
    });
    const status = document.createElement("div");
    status.className = "kw-status"; status.id = "kwStatus";
    status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    setStatusLine(status, _statusError ? "状态未刷新：" + _statusError : _message.text, !!_statusError || _message.error);
    bar.appendChild(status);
    box.appendChild(bar);
    return box;
}

function renderEntriesView() {
    const box = renderToolbar(knowledgeKindZh(_kind) + " · " + KNOWLEDGE_SCOPE_ZH[_scopeLevel], isSystem()
        ? [{ label: "刷新", fn: () => refreshAll() }]
        : [{ label: "新建", cls: "btn-primary", fn: () => renderEditor(null) },
           { label: "刷新", fn: () => refreshAll() }]);
    const list = document.createElement("div");
    list.className = "kw-list"; list.id = "kwList";
    box.appendChild(list);
    if (!_entries.length) {
        const empty = document.createElement("div");
        empty.className = "kw-empty";
        empty.textContent = isSystem()
            ? "这一层还没有内置说明。"
            : "还没有" + knowledgeKindZh(_kind) + "。点「新建」写一条，提案确认后才会生效并被引用。";
        list.appendChild(empty);
        renderEditor(null, box);
        return;
    }
    _entries.forEach((entry) => {
        const row = document.createElement("article");
        row.className = "kw-item" + (entry.id === _selectedId ? " is-selected" : "");
        row.tabIndex = 0;
        row.setAttribute("role", "button");
        row.setAttribute("aria-pressed", entry.id === _selectedId ? "true" : "false");
        const badge = knowledgeEntryBadge(entry.status);
        const scopeNote = _scopeLevel === "project" && entry.scope_level === "user" ? "继承自用户层 · " : "";
        row.innerHTML = '<div class="kw-item-copy"><div class="kw-item-title">' + escapeHtml(entry.title) + '</div>'
            + (entry.text ? '<div class="kw-item-text">' + escapeHtml(entry.text) + "</div>" : "")
            + '<div class="kw-item-meta">' + scopeNote + escapeHtml(knowledgeKindZh(entry.kind)) + " · "
            + escapeHtml(KNOWLEDGE_TIER_ZH[entry.tier]) + (entry.trigger ? " · 适用：" + escapeHtml(entry.trigger) : "") + "</div></div>"
            + '<span class="kw-badge ' + badge.cls + '">' + escapeHtml(badge.label) + "</span>";
        row.addEventListener("click", () => { _selectedId = entry.id; renderEntriesView(); });
        row.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); row.click(); } });
        list.appendChild(row);
    });
    renderEditor(_entries.find((e) => e.id === _selectedId) || null, box);
}

function renderEditor(entry, box) {
    if (!box) box = $("kwContent");
    box.querySelector(".kw-editor")?.remove();
    if (entry && (entry.project_id !== projectId() || entry.status !== "active" || workspaceProjects().some((p) => p.id === projectId() && p.archived))) {
        const note = document.createElement("div"); note.className = "kw-editor-note";
        note.textContent = entry.project_id !== projectId() ? "这是继承的用户知识。请到用户层修改；修改会影响所有继承项目。" : "已撤回或归档内容只读。";
        note.appendChild(_editorReadonlyBody(entry));
        if (entry.project_id !== projectId() && entry.scope_level !== "system") {
            const jump = document.createElement("button"); jump.type = "button"; jump.className = "btn"; jump.textContent = "到用户层查看";
            jump.addEventListener("click", () => selectScope("user")); note.appendChild(jump);
        }
        box.appendChild(note); return;
    }
    const editor = document.createElement("div");
    editor.className = "kw-editor";
    const isNew = !entry;
    const draft = draftOf(isNew ? "" : entry.id);
    const head = document.createElement("div");
    head.className = "kw-editor-head";
    const title = document.createElement("span");
    title.className = "kw-editor-title";
    title.textContent = isSystem() ? "内置说明（只读）" : (isNew ? "新建" + knowledgeKindZh(_kind) : "编辑 · " + (entry.title || ""));
    head.appendChild(title);
    editor.appendChild(head);
    if (isSystem()) {
        if (entry) {
            editor.appendChild(_editorReadonlyBody(entry));
        } else {
            const note = document.createElement("div");
            note.className = "kw-editor-note";
            note.textContent = COPY.knowledge.systemReadonly + "——从左侧列表选择一条查看正文。";
            editor.appendChild(note);
        }
        box.appendChild(editor);
        return;
    }
    const makeField = (label, value, onChange, opts) => {
        const field = document.createElement("div");
        field.className = "kw-field";
        const lb = document.createElement("label");
        lb.textContent = label;
        field.appendChild(lb);
        if (opts && opts.tag === "textarea") {
            const ta = document.createElement("textarea");
            ta.value = value;
            if (opts.mono) ta.className = "kw-mono";
            ta.addEventListener("input", () => onChange(ta.value));
            field.appendChild(ta);
        } else if (opts && opts.tag === "select") {
            const sel = document.createElement("select");
            (opts.options || []).forEach((o) => {
                const option = document.createElement("option");
                option.value = o.value; option.textContent = o.label;
                sel.appendChild(option);
            });
            sel.value = value;
            sel.addEventListener("change", () => onChange(sel.value));
            field.appendChild(sel);
        } else {
            const input = document.createElement("input");
            input.type = "text"; input.value = value;
            input.addEventListener("input", () => onChange(input.value));
            field.appendChild(input);
        }
        if (opts && opts.note) {
            const small = document.createElement("small");
            small.textContent = opts.note;
            field.appendChild(small);
        }
        field.querySelector("input,textarea,select")?.setAttribute("aria-label", label);
        return field;
    };
    const set = (key, v) => saveDraft(isNew ? "" : entry.id, { [key]: v });
    const titleField = makeField("标题", draft.title, (v) => set("title", v));
    const textField = makeField("正文", draft.text, (v) => set("text", v), { tag: "textarea", note: COPY.knowledge.draftNote });
    const grid = document.createElement("div");
    grid.className = "kw-field-grid";
    grid.appendChild(makeField("适用场景（可选）", draft.trigger, (v) => set("trigger", v), { note: "例如「人类数据」" }));
    const tierSel = makeField("依据", draft.tier, (v) => set("tier", v), { tag: "select", options: [
        { value: "explicit", label: KNOWLEDGE_TIER_ZH.explicit },
        { value: "implicit", label: KNOWLEDGE_TIER_ZH.implicit }] });
    grid.appendChild(tierSel);
    const permSel = makeField("有效期", draft.permanence, (v) => set("permanence", v), { tag: "select", options: [
        { value: "persistent", label: "长期保留" }, { value: "temporary", label: "临时" }, { value: "unknown", label: "尚不明确" }] });
    grid.appendChild(permSel);
    editor.appendChild(titleField);
    editor.appendChild(textField);
    editor.appendChild(grid);
    editor.appendChild(makeField("结构内容（JSON）", draft.payloadText, (v) => set("payloadText", v), { tag: "textarea", mono: true,
        note: _kind === "memory" ? "记忆可以保留 {}。" : _kind === "skill"
            ? "tools 须为非空清单。已注册工具：" + (_status?.capabilities?.registered_tools || []).join("、")
            : "原话 utterance；steps 为 [{verb,args}]；手工样例 verified_success 必须为 false，不能伪标成功。已注册工具：" + (_status?.capabilities?.registered_tools || []).join("、") }));
    editor.appendChild(makeField("关联知识编号（每行一个，可选）", draft.linksText, (v) => set("linksText", v), { tag: "textarea" }));
    editor.appendChild(makeField("本次提交材料（必填）", draft.sourceText, (v) => set("sourceText", v), { tag: "textarea",
        note: "明确写出你授权的内容修改或移动要求；只发送这次填写的材料。" }));
    if (!isNew) {
        const moveField = document.createElement("div");
        moveField.className = "kw-field-grid";
        moveField.appendChild(makeField("移动到", draft.moveScope, (v) => { set("moveScope", v); renderEditor(entry); }, { tag: "select", options: [
            { value: "user", label: "用户层（全局）" }, { value: "project", label: "项目层" }] }));
        if (draft.moveScope === "project") {
            const projects = workspaceProjects().filter((p) => !p.archived);
            moveField.appendChild(makeField("目标项目", draft.moveProject, (v) => set("moveProject", v), { tag: "select",
                options: [{ value: "", label: "请选择项目" }, ...projects.map((p) => ({ value: p.id, label: p.name || p.id }))] }));
        }
        editor.appendChild(moveField);
    }
    const acts = document.createElement("div");
    acts.className = "kw-editor-acts";
    const save = document.createElement("button");
    save.type = "button"; save.className = "btn btn-primary";
    save.textContent = isNew ? "保存为提案" : "保存修改为提案";
    save.addEventListener("click", () => proposeFromEditor(isNew ? null : entry));
    acts.appendChild(save);
    if (!isNew) {
        const retract = document.createElement("button");
        retract.type = "button"; retract.className = "btn";
        retract.textContent = "撤回本条";
        retract.addEventListener("click", (e) => {
            if (!armTwoStepConfirm(e.currentTarget, { idleText: "撤回本条", confirmText: "再点一次确认撤回（不再生效，记录保留）" })) return;
            proposeOperations([knowledgeRetractOperation(entry)].filter(Boolean), [], "retract-" + entry.id, "撤回提案已提交");
        });
        acts.appendChild(retract);
        const move = document.createElement("button");
        move.type = "button"; move.className = "btn";
        move.textContent = "移动";
        move.addEventListener("click", () => {
            const current = draftOf(entry.id);
            const destination = { scope_level: current.moveScope, project_id: current.moveScope === "project" ? current.moveProject : "" };
            const op = knowledgeMoveOperation(entry, destination), sources = knowledgeUserSources(current.sourceText, entry.project_id, entry);
            if (!op || destination.project_id === entry.project_id || (destination.scope_level === "project" && !destination.project_id)) { kwStatus("请选择不同的目标层与项目。", true); return; }
            if (!sources.ok) { kwStatus(sources.error, true); return; }
            proposeOperations([op], sources.source_refs, "move-" + entry.id, "移动提案已提交");
        });
        acts.appendChild(move);
    }
    const cancel = document.createElement("button");
    cancel.type = "button"; cancel.className = "btn";
    cancel.textContent = isNew ? "清空草稿" : "还原草稿";
    cancel.addEventListener("click", () => { clearDraft(isNew ? "" : entry.id); renderEntriesView(); });
    acts.appendChild(cancel);
    editor.appendChild(acts);
    box.appendChild(editor);
}

function _editorReadonlyBody(entry) {
    const wrap = document.createElement("div");
    const body = document.createElement("div");
    body.className = "kw-editor-note";
    body.innerHTML = "<strong>" + escapeHtml(entry.title) + "</strong>"
        + (entry.text ? "<br>" + escapeHtml(entry.text) : "")
        + (entry.trigger ? "<br>适用：" + escapeHtml(entry.trigger) : "")
        + (entry.payload ? "<br>payload：" + escapeHtml(knowledgePayloadText(entry.payload)) : "");
    wrap.appendChild(body);
    return wrap;
}

/* ---------- 提案（唯一写入通道） ---------- */
async function proposeFromEditor(entry) {
    if (isSystem()) return;
    const isNew = !entry;
    const draft = draftOf(isNew ? "" : entry.id);
    const form = {
        kind: _kind, title: draft.title, text: draft.text, trigger: draft.trigger,
        tier: draft.tier, permanence: draft.permanence, payloadText: draft.payloadText,
        linksText: draft.linksText, sourceText: draft.sourceText,
        projectId: projectId(), entryRevision: draft.entryRevision,
        registeredTools: _status?.capabilities?.registered_tools || [],
    };
    const built = knowledgeOperationsFromForm(form, entry || null);
    if (!built.ok) { kwStatus(built.error, true); return; }
    await proposeOperations(built.operations, built.source_refs, isNew ? "" : entry.id,
        "已提交提案（" + (isNew ? "新建" : "修改") + "）：确认后才会生效并被引用", isNew ? draft : null);
}

async function proposeOperations(operations, sourceRefs, entryId, okMessage, draftToClear) {
    if (isSystem()) return;
    const key = JSON.stringify([draftKey(entryId), draftOf(entryId).intentId, operations, sourceRefs]);
    const requestId = _proposeRequestIds.get(key) || knowledgeDedupRequestId(key);
    _proposeRequestIds.set(key, requestId);
    const token = beginMutation();
    if (!token) return;
    kwStatus("提交提案中…", false);
    try {
        const data = await _post(API.knowledgePropose, {
            expected_identity: currentAccountScope() || "local",
            request_id: requestId,
            operations: operations,
            source_refs: Array.isArray(sourceRefs) ? sourceRefs : [],
        });
        if (!token.isCurrent()) return;
        _proposeRequestIds.delete(key);
        if (draftToClear) clearDraft(entryId);
        kwStatus((data.detail || okMessage) + "（变更 " + (data.change_id || "已入列") + "，在「变更审查」里确认）", false);
        toast(okMessage);
        await Promise.all([loadEntries(), loadChanges()]);
    } catch (err) {
        if (!token.isCurrent()) return;
        kwStatus("提交失败（重试不会重复提案）：" + String((err && err.message) || err), true);
    } finally { finishMutation(token); }
}

/* ---------- 变更审查 ---------- */
function renderReviewView(error) {
    const box = renderToolbar("变更审查", [
        { label: "刷新", fn: () => loadChanges() },
    ]);
    if (error) {
        const err = document.createElement("div");
        err.className = "kw-empty"; err.textContent = error;
        box.appendChild(err);
        return;
    }
    const vis = knowledgeChangesVisibility(_changes);
    const summary = document.createElement("div");
    summary.className = "kw-status";
    summary.textContent = "共 " + vis.items.length + " 条变更"
        + (vis.failed ? " · 失败 " + vis.failed : "")
        + (vis.candidates ? " · 候选 " + vis.candidates : "")
        + (vis.conflicts ? " · 冲突 " + vis.conflicts : "")
        + "。提交前先评估；失败与冲突不会藏起来。";
    box.appendChild(summary);
    if (!vis.items.length) {
        const empty = document.createElement("div");
        empty.className = "kw-empty";
        empty.textContent = "还没有待确认的提案。在左侧「新建」，或从记忆弹窗把本机资料「提交知识提案」，都会先出现在这里。";
        box.appendChild(empty);
        return;
    }
    vis.items.forEach((change) => renderChangeRow(change, box));
}

function renderChangeRow(change, box) {
    const vis = knowledgeChangesVisibility([change]);
    const row = document.createElement("div");
    row.className = "kw-change" + (change.status === "failed" ? " is-failed" : (vis.conflicts ? " is-conflict" : ""));
    const badge = knowledgeEntryBadge(change.status === "candidate" ? "candidate" : change.status);
    const ops = (change.operations || []).map((op) => KNOWLEDGE_OP_ZH[op.op] || op.op).join("、");
    const head = document.createElement("div");
    head.className = "kw-change-head";
    const title = change.operations?.[0]?.after?.title || change.assessment?.differences?.[0]?.before?.title || "知识变更";
    head.innerHTML = '<span class="kw-change-title">' + escapeHtml(title) + "</span>"
        + '<span class="kw-change-ops">' + escapeHtml(ops) + " · " + escapeHtml(change.created_at || "") + "</span>"
        + '<span class="kw-badge ' + badge.cls + '">' + escapeHtml(badge.label) + "</span>";
    row.appendChild(head);
    if (change.error_code || change.detail) {
        const err = document.createElement("div");
        err.className = "kw-change-error";
        err.textContent = (change.error_code ? change.error_code + "：" : "") + (change.detail || "处理未通过");
        row.appendChild(err);
    }
    const trace = knowledgeTraceSummary(change.assessment);
    if (trace) {
        const t = document.createElement("div");
        t.className = "kw-change-trace";
        t.textContent = (["committed", "rejected"].includes(change.status) ? "历史评估：" : "评估：") + trace;
        row.appendChild(t);
    }
    for (const report of change.assessment?.reports || []) {
        const r = document.createElement("p"); r.className = "kw-change-relation";
        r.textContent = knowledgeRelationSummary(report.relation); row.appendChild(r);
        for (const pair of report.relation?.semantic_pairs || []) {
            const details = document.createElement("details"), summary = document.createElement("summary");
            summary.textContent = (pair.old?.body?.title || pair.old_id) + " · " + (pair.label || "未能判断") + "：" + pair.reason;
            details.appendChild(summary);
            const p = document.createElement("div"); p.className = "kw-change-copy";
            p.textContent = "旧项 " + pair.old_id + " · 版本 " + (pair.old?.revision ?? "未知")
                + "\n旧项引用：" + (pair.old_quote || "未取得") + "\n新项引用：" + (pair.new_quote || "未取得")
                + "\n本次出处：" + (pair.sources || []).map((s) => s.id + "：" + s.text).join("；")
                + "\n旧项出处：" + (pair.old?.provenance?.quotes || []).join("；")
                + (pair.error_code ? "\n未能判断：" + pair.error_code : "");
            details.appendChild(p); row.appendChild(details);
        }
    }
    for (const check of change.assessment?.edit_checks || []) {
        const p = document.createElement("div"); p.className = "kw-change-copy";
        p.textContent = "编辑依据 · " + check.entry_id + "：" + (check.supported === true ? "模型认为有依据" : "尚无充分依据")
            + "\n" + check.reason + "\n来源引用：" + (check.source_refs || []).map((s) => s.id + "：" + s.quote).join("；")
            + "\n保留的其它义务：" + (check.preserved || []).map((item) => item.before.quote + " → " + item.after.quote).join("；")
            + "\n" + check.verification_note;
        row.appendChild(p);
    }
    const differences = change.assessment?.differences || change.operations || [];
    for (const difference of differences) {
        const copy = document.createElement("pre"); copy.className = "kw-change-copy";
        copy.textContent = "原版本：\n" + knowledgeBodyText(difference.before)
            + "\n新版本：\n" + knowledgeBodyText(difference.after)
            + (difference.op === "retract" ? "\n拟撤回，不再引用；原记录保留。"
                : difference.op === "move" ? "\n拟移动到：" + (difference.after?.project_id || difference.destination?.project_id || "用户层") : "");
        row.appendChild(copy);
    }
    const details = document.createElement("details"), summary = document.createElement("summary"); summary.textContent = "完整差异与出处";
    details.appendChild(summary);
    const diff = document.createElement("pre"); diff.className = "kw-change-diff";
    diff.textContent = JSON.stringify({ change_id: change.change_id, differences, source_refs: change.source_refs || [],
        reports: change.assessment?.reports || [], edit_checks: change.assessment?.edit_checks || [] }, null, 2);
    details.appendChild(diff); row.appendChild(details);
    if (["committed", "rejected"].includes(change.status)) { box.appendChild(row); return; }
    const acts = document.createElement("div");
    acts.className = "kw-change-acts";
    const assess = document.createElement("button");
    assess.type = "button"; assess.className = "btn";
    assess.textContent = change.assessment_id ? "重新评估" : "评估";
    assess.addEventListener("click", () => assessChange(change, assess));
    acts.appendChild(assess);
    const ready = knowledgeCommitReady(change, _status);
    const commit = document.createElement("button");
    commit.type = "button"; commit.className = "btn btn-primary";
    commit.textContent = "确认提交";
    commit.title = ready.ok ? "" : ready.reason;
    commit.disabled = !ready.ok;
    commit.addEventListener("click", (e) => {
        if (!armTwoStepConfirm(e.currentTarget, { idleText: "确认提交", confirmText: COPY.knowledge.confirmCommit })) return;
        commitChange(change, e.currentTarget);
    });
    acts.appendChild(commit);
    const reject = document.createElement("button");
    reject.type = "button"; reject.className = "btn";
    reject.textContent = "驳回";
    reject.addEventListener("click", (e) => rejectChange(change, e.currentTarget));
    acts.appendChild(reject);
    row.appendChild(acts);
    const feedback = document.createElement("input");
    feedback.className = "kw-feedback";
    feedback.id = "kwFeedback-" + change.change_id;
    feedback.placeholder = "驳回原因或反馈（可选）";
    feedback.maxLength = 2000;
    feedback.value = _reviewFeedback.get(change.change_id) || "";
    feedback.addEventListener("input", () => _reviewFeedback.set(change.change_id, feedback.value));
    row.appendChild(feedback);
    box.appendChild(row);
}

async function assessChange(change, btn) {
    if (!change || !change.change_id) return;
    if (_statusError || !_status || !Number.isSafeInteger(_status.config_epoch) || _status.config_epoch < 0) {
        kwStatus("评估需要当前配置代际：请先刷新状态。", true);
        await loadStatus(false); return;
    }
    const cfg = configSnapshot();
    if (cfg.use_llm !== true && change.operations.some((op) => op.op !== "retract")) { kwStatus("AI 未开启，内容提案保留为草稿，不会被引用。开启后可再评估。", true); return; }
    const token = beginReviewAction("assess", change, btn);
    if (!token) return;
    kwStatus("评估中…", false);
    try {
        const data = await _post(API.knowledgeAssess, {
            use_llm: cfg.use_llm, mock_llm: cfg.mock_llm,
            api_key: cfg.api_key, expected_identity: currentAccountScope() || "local",
            expected_config_epoch: _status.config_epoch,
            change_id: change.change_id, expected_change_revision: change.change_revision,
        });
        if (!token.isCurrent()) return;
        kwStatus("评估完成。" + knowledgeTraceSummary(data.assessment), false);
        await loadChanges();
    } catch (err) {
        if (!token.isCurrent()) return;
        kwStatus("评估失败：" + String(err.message || err), true);
        await loadChanges();
    } finally { endReviewAction("assess", change, token, btn); }
}

async function commitChange(change, btn) {
    const ready = knowledgeCommitReady(change, _status);
    if (!ready.ok) { kwStatus(ready.reason, true); return; }
    const token = beginReviewAction("commit", change, btn);
    if (!token) return;
    kwStatus("提交中…", false);
    try {
        const data = await _post(API.knowledgeCommit, {
            expected_identity: currentAccountScope() || "local",
            change_id: change.change_id, expected_change_revision: change.change_revision,
            assessment_id: change.assessment_id,
            expected_catalog_generation: _status.catalog_generation, confirm: true,
        });
        if (!token.isCurrent()) return;
        kwStatus("已生效" + (data.detail ? "：" + data.detail : "") + "。只有真正提交的内容才计入生效。", false);
        await Promise.all([loadStatus(true), loadEntries(), loadChanges()]);
    } catch (err) {
        if (!token.isCurrent()) return;
        kwStatus("提交失败：" + String((err && err.message) || err), true);
    } finally { endReviewAction("commit", change, token, btn); }
}

async function rejectChange(change, btn) {
    const token = beginReviewAction("reject", change, btn);
    if (!token) return;
    const feedback = _reviewFeedback.get(change.change_id) || "";
    kwStatus("驳回中…", false);
    try {
        const data = await _post(API.knowledgeReject, {
            change_id: change.change_id, expected_change_revision: change.change_revision,
            feedback: feedback,
        });
        if (!token.isCurrent()) return;
        _reviewFeedback.delete(change.change_id);
        kwStatus("已驳回（没有写进任何地方）。", false);
        await loadChanges();
    } catch (err) {
        if (!token.isCurrent()) return;
        kwStatus("驳回失败：" + String((err && err.message) || err), true);
    } finally { endReviewAction("reject", change, token, btn); }
}

/* ---------- 整理视图（容器交给 dream.js 渲染） ---------- */
let _learnRenderer = null;
export function setKnowledgeLearnRenderer(fn) { _learnRenderer = (typeof fn === "function") ? fn : null; }

function renderLearnView() {
    const box = renderToolbar("整理（来源选择）", [
        { label: "刷新", fn: () => refreshAll() },
    ]);
    const host = document.createElement("div");
    host.id = "kwLearn";
    box.appendChild(host);
    if (_learnRenderer) _learnRenderer({ host });
    else {
        const empty = document.createElement("div");
        empty.className = "kw-empty"; empty.textContent = "整理面板还没有准备好。";
        box.appendChild(empty);
    }
    // 独立学习引擎块（rules.js 渲染；id 与 vm 规格 harness 一致，宿主从记忆弹窗移到本视图）。
    // 加载由 dream.renderLearnPanel → refreshLearnSummary → loadLearningStatus 完成（单一加载通道）。
    const rulesStatus = document.createElement("div");
    rulesStatus.className = "kw-status"; rulesStatus.id = "rulesStatus"; rulesStatus.hidden = true;
    box.appendChild(rulesStatus);
    const rulesList = document.createElement("div");
    rulesList.className = "kw-rules"; rulesList.id = "rulesList"; rulesList.hidden = true;
    box.appendChild(rulesList);
}

/* ---------- 竖栏与项目圆点 ---------- */
function renderProjectRail() {
    const rail = $("kwProjectRail");
    const projects = workspaceProjects().filter((p) => !p.archived);
    rail.innerHTML = "";
    if (!projects.length) {
        const empty = document.createElement("div");
        empty.className = "kw-project-empty";
        empty.textContent = "还没有项目。在「项目」页签新建后这里会出现圆点。";
        rail.appendChild(empty);
        return;
    }
    const active = currentProjectScope();
    projects.forEach((project) => {
        const dot = document.createElement("button");
        dot.type = "button";
        dot.className = "kw-project-dot";
        dot.style.setProperty("--kw-project-color", workspaceProjectColor(project));
        dot.title = project.name || project.id;
        dot.textContent = String(projects.indexOf(project) + 1);
        dot.setAttribute("aria-label", "切换到项目 " + (project.name || project.id));
        dot.setAttribute("aria-pressed", project.id === active ? "true" : "false");
        dot.addEventListener("click", () => {
            if (project.id === active) return;
            workspaceEnsureSelect(project.id).then(() => { if (isOpen()) refreshAll(); });
        });
        rail.appendChild(dot);
    });
}

function renderRails() {
    renderProjectRail();
    document.querySelectorAll("#kwKindRail .kw-kind-btn").forEach((btn) => {
        const kind = btn.dataset.kwKind, view = btn.dataset.kwView;
        const on = kind ? kind === _kind && _view === "entries" : view === _view;
        btn.setAttribute("aria-pressed", on ? "true" : "false");
        if (btn.classList.contains("kw-rail-secondary")) {
            btn.hidden = isSystem();   // 系统层只读：无审查/整理
        }
    });
    $("kwProjectRail").hidden = _scopeLevel !== "project";
    document.querySelectorAll("#kwScopeTabs .kw-scope-tab").forEach((btn) => {
        btn.setAttribute("aria-selected", btn.dataset.kwScope === _scopeLevel ? "true" : "false");
    });
    $("kwWinSub").textContent = isSystem()
        ? COPY.knowledge.systemReadonly + "。"
        : "你的提案确认后才会生效并被引用；草稿不会被引用。";
}

/* ---------- 汇总刷新与开合 ---------- */
function invalidateAll() {
    invalidateRequest(_requests);
    Object.values(_readRequests).forEach(invalidateRequest);
    for (const slot of _reviewRequests.values()) invalidateRequest(slot.owner);
    _reviewRequests.clear();
    _status = null; _statusError = "";
}

async function refreshAll() {
    if (!isOpen()) return;
    const captured = scopeKey(), view = _view;
    renderRails();
    await loadStatus(true);
    if (captured !== scopeKey() || view !== _view || !isOpen()) return;
    if (_view === "entries") await loadEntries();
    else if (_view === "review") await loadChanges();
    else renderLearnView();
}

function openWindow() {
    if (!_ctrl) return;
    _returnFocus = document.activeElement;
    if (_closeSurfaces) _closeSurfaces();
    closeModal($("memoryModal"));
    _ctrl.open();
    $("knowledgeWinClose")?.focus();
}

function selectScope(level) {
    if (_scopeLevel === level) return;
    invalidateAll();                 // scope 变：在途回包整段丢弃
    _scopeLevel = knowledgeScopeLevelOf(level);
    _selectedId = "";
    if (isSystem()) { _view = "entries"; }
    refreshAll();
}

function selectKind(kind) {
    _kind = KNOWLEDGE_KINDS.includes(kind) ? kind : "memory";
    _view = "entries";
    _selectedId = "";
    refreshAll();
}

function selectView(view) {
    _view = view === "review" || view === "learn" ? view : "entries";
    refreshAll();
}

export function initKnowledge(options) {
    const opts = options || {};
    _closeSurfaces = typeof opts.beforeOpen === "function" ? opts.beforeOpen : null;
    _ctrl = (typeof opts.floating === "function")
        ? opts.floating($("knowledgeWin"), {
            head: $("knowledgeWinHead"), resize: $("knowledgeWinResize"),
            close: $("knowledgeWinClose"), onOpen: () => refreshAll(),
            onClose: () => {
                invalidateAll();
                const target = _returnFocus?.isConnected && !_returnFocus.closest('[inert],[hidden]') ? _returnFocus : $("settingsBtn");
                target?.focus();
            } })
        : null;
    document.querySelectorAll("#kwScopeTabs .kw-scope-tab").forEach((btn) => {
        btn.addEventListener("click", () => selectScope(btn.dataset.kwScope));
    });
    document.querySelectorAll("#kwKindRail .kw-kind-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
            if (btn.dataset.kwKind) selectKind(btn.dataset.kwKind);
            else selectView(btn.dataset.kwView);
        });
    });
    const openButtons = [$("knowledgeWinBtn"), $("knowledgeWinBtnSettings")];
    openButtons.forEach((btn) => { if (btn) btn.addEventListener("click", openWindow); });
    registerContextChanged((kind) => {
        if (["project", "learning-scope", "account", "workspace-data", "membership"].includes(kind) && isOpen()) {
            invalidateAll();
            refreshAll();
        }
    });
    const win = $("knowledgeWin");
    if (win) win.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && isOpen() && _ctrl) _ctrl.close();
    });
    setExamplesCompatRefresh(() => { _kind = "example"; _view = "entries"; return refreshAll(); });
}
