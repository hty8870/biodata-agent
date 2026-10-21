"use strict";

import { API, LS, nsKeyFor, currentAccountScope, currentProjectScope, $, toast, registerContextChanged } from "#core";
import { queueRead, queueWrite, queueRemoveIds } from "#storage_queue";

const TERMINAL = new Set(["done", "failed", "cancelled", "interrupted"]);
let _displayRow = null;
function prefix(owner) { return nsKeyFor(LS.pendingRuns, owner) + "::run::"; }
function fail(message, options) { return Object.assign(new Error(message), { durableRun: true, ...options }); }
function requireCurrent(row, stillCurrent) {
    if (row.owner !== currentAccountScope() || row.project_id !== currentProjectScope()
        || (stillCurrent && !stillCurrent())) {
        hideRunControl(row);
        throw fail("对话视图已切换，任务仍保留在原账户和项目中。", { contextChanged: true });
    }
}

export function pendingConversationRuns() {
    const owner = currentAccountScope(), project = currentProjectScope();
    return queueRead(localStorage, prefix(owner), (a, b) => b.at - a.at).filter((r) => r.project_id === project);
}

function remember(row) { return queueWrite(localStorage, prefix(row.owner), row.request_id, row); }
export function forgetConversationRun(row) {
    const ids = queueRead(localStorage, prefix(row.owner), (a, b) => b.at - a.at)
        .filter((r) => r.request_id === row.request_id || (row.run_id && r.run_id === row.run_id)).map((r) => r.request_id);
    queueRemoveIds(localStorage, prefix(row.owner), ids);
    hideRunControl(row);
}
export function forgetSharedConversationRuns(id) {
    const owner = currentAccountScope();
    const ids = queueRead(localStorage, prefix(owner), (a, b) => b.at - a.at)
        .filter((row) => row.shared && row.conversation_id === id).map((row) => row.request_id);
    queueRemoveIds(localStorage, prefix(owner), ids);
    if (_displayRow?.owner === owner && _displayRow.conversation_id === id) hideRunControl();
    _listSeq++;
    if (_listOwner === owner) _summaries = _summaries.filter((row) => row.id !== id);
}
export async function claimConversationClientAction(row, index, stillCurrent) {
    if (!row || row.owner !== currentAccountScope()) return false;
    const guard = identityGuard();
    const base = nsKeyFor(LS.pendingRuns, row.owner) + "::browser::", id = row.run_id || row.request_id;
    if (!id || !Number.isSafeInteger(index) || index < 0) throw fail("页面动作编号无效，未派发。");
    if (!globalThis.navigator?.locks?.request) throw fail("浏览器不支持跨标签页动作锁，未派发；请核对任务回执，手动打开对应面板或下载。");
    return navigator.locks.request(base, { mode: "exclusive" }, () => {
        guard(); requireCurrent(row, stillCurrent);
        const records = queueRead(localStorage, base, (a, b) => b.at - a.at);
        const record = records.find((v) => v.id === id) || { id, at: Date.now(), actions: [] };
        if (record.actions.includes(index)) return false;
        guard(); requireCurrent(row, stillCurrent);
        record.actions.push(index);
        if (!queueWrite(localStorage, base, id, record)) throw fail("浏览器没能保存动作回执，未派发；请检查存储空间。");
        return true;
    });
}

let _identityEpoch = 0, _listOwner = null, _summaries = [], _listSeq = 0;
const PAGE_LIMIT = 200, MAX_PAGES = 100;
function identityGuard() {
    const owner = currentAccountScope(), epoch = _identityEpoch;
    return () => {
        if (owner !== currentAccountScope() || epoch !== _identityEpoch) {
            throw fail("账户已切换，已忽略原账户的回应。", { contextChanged: true });
        }
    };
}
async function response(url, options) {
    const guard = identityGuard();
    let res;
    try { res = await fetch(url, { cache: "no-store", ...options }); }
    catch (_error) { guard(); throw fail("连接中断，任务可能仍在执行。刷新页面后可核对任务状态。", { pending: true }); }
    const data = await res.json().catch(() => ({}));
    guard();
    if (!res.ok || !data.ok) {
        throw fail(data.detail || "任务请求未完成", {
            busy: res.status === 503 || res.status === 409,
            conflict: res.status === 409,
            fallbackAllowed: res.status === 422,
            code: res.headers?.get("X-Error-Code") || "",
            status: res.status
        });
    }
    return data;
}
function post(url, body) {
    return response(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}
function protocolInteger(value, label, minimum = 0) {
    if (!Number.isSafeInteger(value) || value < minimum) {
        throw fail(label + "无效，服务端协议回应不完整；请刷新核对。", { protocol: true });
    }
    return value;
}
function conversationHeader(c, id) {
    if (!c || typeof c.id !== "string" || !c.id || (id && c.id !== id)) {
        throw fail("对话编号无效，服务端协议回应不完整。", { protocol: true });
    }
    protocolInteger(c.revision, "对话版本");
    for (const key of ["frame_count", "message_count"]) if (c[key] != null) protocolInteger(c[key], "对话计数");
    return c;
}
export function sharedHistoryRows() {
    if (_listOwner !== currentAccountScope()) return [];
    return _summaries.map((c) => ({ shared: true, convId: c.id, revision: c.revision,
        project_id: c.project_id || "", archived: c.archived, query: c.title || c.current_state?.query || "（仅对话）",
        count: c.current_state?.result_total || 0,
        chatOnly: c.frame_count === 0 && !(c.current_state?.active_frame_id || c.current_state?.has_results),
        message_count: c.message_count, frame_count: c.frame_count, active_run_id: c.active_run_id,
        at: Date.parse(c.updated_at) || 0 }));
}
export async function listSharedConversations() {
    const guard = identityGuard(), seq = ++_listSeq, byId = new Map();
    let offset = 0;
    for (let page = 0; page < MAX_PAGES; page++) {
        const data = await response("/api/conversations?include_archived=true&limit=" + PAGE_LIMIT + "&offset=" + offset);
        guard();
        if (!Array.isArray(data.items)) throw fail("对话列表协议缺少条目数组。", { protocol: true });
        for (const raw of data.items) {
            const c = conversationHeader(raw), previous = byId.get(c.id);
            if (!previous || c.revision >= previous.revision) byId.set(c.id, c);
        }
        if (data.next_offset == null) {
            const items = Array.from(byId.values());
            if (seq === _listSeq) { _listOwner = currentAccountScope(); _summaries = items; }
            return items;
        }
        const next = protocolInteger(data.next_offset, "对话列表游标");
        if (next <= offset) throw fail("对话列表分页没有前进，请刷新后重试。", { protocol: true });
        offset = next;
    }
    throw fail("对话列表超过读取上限，未展示不完整记录。");
}
export async function getSharedConversation(id, { metadataOnly = false } = {}) {
    const guard = identityGuard();
    if (metadataOnly) {
        const data = await response("/api/conversations/" + encodeURIComponent(id) + "?limit=1");
        guard(); return conversationHeader(data.conversation, id);
    }
    for (let attempt = 0; attempt < 3; attempt++) {
        const messages = [], frames = [];
        let afterMessage = 0, afterFrame = 0, revision = null, changed = false;
        for (let page = 0; page < MAX_PAGES; page++) {
            const data = await response("/api/conversations/" + encodeURIComponent(id)
                + "?limit=" + PAGE_LIMIT + "&after_message=" + afterMessage + "&after_frame=" + afterFrame);
            guard(); const c = conversationHeader(data.conversation, id);
            if (revision != null && c.revision !== revision) { changed = true; break; }
            if (!Array.isArray(c.messages) || !Array.isArray(c.frames)) throw fail("对话分页协议缺少消息或帧数组。", { protocol: true });
            for (const row of [...c.messages, ...c.frames]) protocolInteger(row?.seq, "消息或帧序号", 1);
            revision = c.revision; messages.push(...c.messages); frames.push(...c.frames);
            if (c.next_message == null && c.next_frame == null) return { ...c, messages, frames };
            const m = c.next_message == null ? Math.max(afterMessage, ...messages.map((v) => v.seq)) : protocolInteger(c.next_message, "消息游标", 1);
            const f = c.next_frame == null ? Math.max(afterFrame, ...frames.map((v) => v.seq)) : protocolInteger(c.next_frame, "结果帧游标", 1);
            if (m <= afterMessage && f <= afterFrame) throw fail("对话分页没有前进，请刷新后重试。", { protocol: true });
            afterMessage = m; afterFrame = f;
        }
        if (!changed) throw fail("对话超过读取上限，未恢复不完整记录。");
    }
    throw fail("对话正在被其他页面更新，请稍后再打开。", { conflict: true });
}
export async function sharedConversationAction(id, action, revision, fields = {}) {
    return post("/api/conversations/" + (id ? encodeURIComponent(id) + "/" : "") + "actions",
        { action, ...(id ? { expected_revision: revision } : {}), ...fields });
}
export async function startConversationRun(body, onEvent, stillCurrent) {
    const row = { owner: currentAccountScope(), project_id: currentProjectScope(), shared: body.shared === true,
        conversation_id: String(body.conv_id || ""), request_id: body.req_id, text: body.utterance,
        run_id: "", at: Date.now() };
    if (!remember(row)) throw fail("浏览器没有保存这次任务的恢复编号，请检查存储空间后重试。");
    let data;
    try { data = await post(API.utterance, { ...body, stream: false, background: true }); }
    catch (error) {
        if (error.fallbackAllowed || error.busy || (error.status && error.status !== 500)) forgetConversationRun(row);
        throw error;
    }
    if (!data.run?.id) {
        if (data.shared && data.conv_id) return { row, result: data, run: { status: "done" } };
        throw fail("任务没有返回恢复编号，请刷新核对。", { pending: true });
    }
    row.run_id = data.run.id;
    row.conversation_id = String(data.run.conversation_id || row.conversation_id);
    remember(row);
    requireCurrent(row, stillCurrent);
    return awaitConversationRun(row, onEvent, stillCurrent);
}
function sharedQuery(row, separator = "?") { return row.shared ? separator + "shared=true" : ""; }
export async function awaitConversationRun(row, onEvent, stillCurrent) {
    requireCurrent(row, stillCurrent);
    row.at = row.at || Date.now();
    if (!remember(row)) throw fail("浏览器没有保存这次任务的恢复编号，请检查存储空间后重试。");
    let after = 0;
    if (!row.run_id) {
        const data = await post("/api/runs/resolve", { req_id: row.request_id, shared: !!row.shared });
        row.run_id = data.run.id; row.conversation_id = String(data.run.conversation_id || row.conversation_id); remember(row);
        requireCurrent(row, stillCurrent);
    }
    showRunControl(row);
    const deadline = Date.now() + 16 * 60 * 1000;
    while (Date.now() < deadline) {
        requireCurrent(row, stillCurrent);
        const data = await response("/api/runs/" + encodeURIComponent(row.run_id) + sharedQuery(row));
        requireCurrent(row, stillCurrent);
        let batch, eventPages = 0;
        do {
            const events = await response("/api/runs/" + encodeURIComponent(row.run_id) + "/events?after=" + after + sharedQuery(row, "&"));
            requireCurrent(row, stillCurrent);
            batch = events.events || [];
            for (const event of batch) {
                requireCurrent(row, stillCurrent);
                const sequence = protocolInteger(event?.seq, "任务事件序号", 1);
                if (sequence <= after) continue;
                after = sequence;
                if (onEvent) onEvent(event.data, event.event);
            }
            if (++eventPages > MAX_PAGES) throw fail("任务事件超过读取上限，请刷新核对完整记录。", { pending: true });
        } while (TERMINAL.has(data.run.status) && batch.length === 100);
        requireCurrent(row, stillCurrent);
        if (TERMINAL.has(data.run.status)) {
            hideRunControl(row);
            const result = data.run.result || { ok: false, detail: "任务没有保存可恢复的结果。" };
            return { row, result, run: data.run };
        }
        await new Promise((resolve) => setTimeout(resolve, 500));
    }
    hideRunControl(row);
    throw fail("任务仍未结束，恢复编号已保留；可刷新核对或停止任务。", { pending: true });
}
export async function cancelConversationRun(row) {
    if (!row || !row.run_id || row.owner !== currentAccountScope()) return;
    return post("/api/runs/" + encodeURIComponent(row.run_id) + "/cancel" + sharedQuery(row), {});
}

function hideRunControl(row) {
    if (!row || (_displayRow && _displayRow.request_id === row.request_id)) {
        _displayRow = null; const button = $("conversationCancelBtn"); if (button) button.hidden = true;
    }
}

function showRunControl(row) {
    _displayRow = row;
    let button = $("conversationCancelBtn");
    if (!button) {
        const composer = $("chatComposer"); if (!composer) return;
        button = document.createElement("button"); button.type = "button"; button.id = "conversationCancelBtn";
        button.className = "btn conversation-cancel";
        button.addEventListener("click", async () => {
            const active = _displayRow; if (!active) return;
            button.disabled = true; button.textContent = "正在停止…";
            try { await cancelConversationRun(active); }
            catch (error) { button.disabled = false; toast(error.message); }
        });
        composer.appendChild(button);
    }
    button.hidden = false; button.disabled = false; button.textContent = "停止";
}

export function initConversationClient() {
    registerContextChanged((kind) => {
        if (["before-project", "before-account"].includes(kind)) hideRunControl();
        if (["before-account", "account"].includes(kind)) {
            _identityEpoch++; _listSeq++; _listOwner = null; _summaries = [];
        }
    });
}
