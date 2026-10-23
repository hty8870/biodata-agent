import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { webcrypto } from "node:crypto";
import { runInNewContext } from "node:vm";
import test from "node:test";
import { cbHistoryPrefix, cbHistoryRestore } from "../../web/static/js/panel/board_core.js";
import * as knowledgeCore from "../../web/static/js/panel/knowledge_core.js";
import { apiErrorText } from "../../web/static/js/core/copy.js";

// Execute the actual browser functions with controlled IO and completion order.
const source = (file) => readFileSync(new URL("../../web/static/js/" + file, import.meta.url), "utf8");
function functions(file, names, globals = {}) {
    const src = source(file);
    globals.apiErrorText ??= apiErrorText;
    globals.knowledgeDedupRequestId ??= knowledgeCore.knowledgeDedupRequestId;
    if (file === "panel/rules.js" && names.includes("_confirm")) names = [...names, "_skillProposePayload"];
    if (file === "panel/rules.js" && names.includes("releaseLearningConfiguration")) {
        names = [...names, "learningEndpointIdentity"];
        globals._learningReleaseEpochs ??= new WeakMap(); globals._learningAuthorizations ??= new Map();
        globals._render ??= noop; globals.renderCollabRules ??= async () => {};
    }
    if (file === "panel/board.js") {
        names = Array.from(new Set([...names, "cbResetSharedRequests", "cbResetComments"]));
        globals._sharedPollRequests ??= {}; globals._sharedForegroundToken ??= null; globals._sharedRefreshToken ??= null;
        globals._commentRequests ??= {}; globals._cbCommentFor ??= ""; globals._cbCommentDraft ??= ""; globals._cbCommentBusy ??= false;
    }
    const code = names.map((name) => {
        const re = new RegExp("^(?:export )?(?:async )?function " + name + "\\(", "m");
        const match = re.exec(src);
        assert.ok(match, "Missing production function " + name);
        const lineEnd = src.indexOf("\n", match.index);
        const singleLine = src.slice(match.index, lineEnd).trimEnd().endsWith("}");
        const end = singleLine ? lineEnd : src.indexOf("\n}", match.index);
        assert.ok(end > match.index);
        const body = src.slice(match.index, singleLine ? end : end + 2).replace(/^export /, "");
        return globals.__import ? body.replace(/\bimport\(/g, "__import(") : body;
    }).join("\n");
    for (const name of ["cbBeginLayoutTransition", "layoutMotionActive", "cancelLayoutTransition"]) {
        if (!(name in globals)) globals[name] = () => false;
    }
    if (file === "core/project_updates.js" && !globals.beginRequest) globals.beginRequest = requestTools().beginRequest;
    if (file === "panel/replay.js" && !globals._setBadge) globals._setBadge = noop;
    if (file === "panel/board.js") {
        globals.history ??= { replaceState: noop }; globals.location ??= { pathname: "/" };
        globals._sharedRequests ??= {}; globals._sharedPresentation ??= false;
        globals._sharedMode ??= false; globals._sharedConversation ??= null; globals._sharedControls ??= null;
        for (const name of ["cbArchiveChatOnly", "resetFacetState", "cbLogClear"]) globals[name] ??= noop;
    }
    return runInNewContext(code + "\n({" + names.join(",") + "})", globals);
}
const noop = () => {};

function historyHarness() {
    const rows = [1, 2, 3].map((n) => ({ convId: "original", at: n, frameId: n, query: "query-" + n,
        snap: { results: [{ uid: "result-" + n }] },
        chat: Array.from({ length: n }, (_, i) => [
            { k: "say", t: "query-" + (i + 1), i: "u" + i, r: i + 1 },
            { k: "sys", t: "reply-" + (i + 1), i: "s" + i, r: i + 1 },
        ]).flat() }));
    rows[2].conversation = { version: 2, frameIds: [1, 2, 3], cursorId: 1,
        view: { chatDraft: "later draft", queryDraft: "later query" } };
    let restored, rendered, adopted;
    const input = element();
    const ctx = { cbSaveConversation: noop, getHist: () => rows.slice().reverse(),
        workspaceConversationProject: () => "project", workspaceSelect: () => true,
        bumpRecSeq: noop, resetSubmitButton: noop, showView: noop, exitResultsLayout: noop,
        $: () => input, autoGrow: noop, setFacetState: noop, renderCondBoard: noop, swSync: noop, toast: noop,
        applyRecommendResult: (data) => { rendered = data; },
        cbRestoreConversation: (chron, options) => { restored = cbHistoryRestore(chron, 20, options); },
        cbAdoptAsBranch: () => { adopted = restored; },
        setHistRenderer: noop, renderHistory: noop, setLibRenderer: noop, renderFavorites: noop,
        cbHistoryPrefix, histGroups: (hist) => [hist], location: { search: "?fork=original:2", pathname: "/" },
        history: { replaceState: noop }, URLSearchParams, ACCOUNTS_READY: Promise.resolve() };
    const api = functions("search/browse.js", ["viewHistorySnapshot", "initHistWin"], ctx);
    return { api, rows, read: () => ({ restored, rendered, adopted }) };
}

test("history prefixes preserve the selected messages, results and frame ownership", () => {
    for (const n of [1, 2, 3]) {
        const h = historyHarness(), before = JSON.stringify(h.rows);
        h.api.viewHistorySnapshot(h.rows.slice(0, n).reverse(), { prefix: true });
        const { restored, rendered } = h.read();
        assert.deepEqual(restored.stack.map((f) => f.query), h.rows.slice(0, n).map((r) => r.query));
        assert.deepEqual(restored.log.map((m) => m.text), h.rows[n - 1].chat.map((m) => m.t));
        assert.deepEqual(restored.log.map((m) => m.frameId), Array.from({ length: n }, (_, i) => [21 + i, 21 + i]).flat());
        assert.equal(restored.cursor, n - 1, "fork starts at its selected result, even if the parent viewed an older result");
        assert.deepEqual(restored.view, {}, "parent drafts and presentation state cannot redirect a fork");
        assert.equal(rendered.results[0].uid, "result-" + n);
        assert.equal(JSON.stringify(h.rows), before, "the parent history remains unchanged");
    }
});

test("ordinary history opening still refreshes the whole saved conversation", () => {
    const h = historyHarness();
    h.api.viewHistorySnapshot([h.rows[0]]);
    const { restored } = h.read();
    assert.equal(restored.stack.length, 3);
    assert.equal(restored.log.length, 6);
    assert.equal(restored.cursor, 0);
    assert.equal(restored.view.chatDraft, "later draft");
});

test("the fork URL restores only its prefix after account initialization", async () => {
    const h = historyHarness();
    h.rows[1].chat.push({ k: "say", t: "query-3", i: "u2", r: null });
    h.api.initHistWin();
    assert.equal(h.read().adopted, undefined);
    await Promise.resolve();
    assert.deepEqual(h.read().adopted.stack.map((f) => f.query), ["query-1", "query-2"]);
    assert.deepEqual(h.read().adopted.log.map((m) => m.text), ["query-1", "reply-1", "query-2", "reply-2"]);
});

test("adopting a branch cancels the parent restore callback and saves inherited frames", () => {
    const h = historyHarness(), callbacks = [], saved = [];
    let scrolled = 0;
    const ctx = { _cbRestoreSeq: 0, _cbRestoring: false, _cbHistoryTimer: 1, _cbHistoryOwner: "owner",
        _routeRequests: {}, _ubSeq: 0, _cbFrameSeq: 0, _cbConvId: "parent", clearTimeout: noop,
        invalidateRequest: noop, flowReset: noop, cbHistoryRestore, _cbMsgIdSeen: noop,
        _cbMsgIdNext: () => "new-message", cbSafeHistoryHtml: (html) => html,
        currentAccountScope: () => "owner", $: () => null, replayReset: noop, cbReplay: noop,
        cbRenderSteps: noop, cbRenderHistory: noop, requestAnimationFrame: (fn) => callbacks.push(fn),
        cbLogPush: () => { ctx._cbConvId = "new-branch"; },
        syncHistConversation: (options) => saved.push({ options, conv: ctx._cbConvId, stack: ctx._cbStack }),
        window: { scrollTo: () => scrolled++ } };
    const mod = functions("panel/board.js", ["cbRestoreConversation", "cbRollConvId", "cbAdoptAsBranch"], ctx);
    mod.cbRestoreConversation(h.rows.slice(0, 2), { asBranch: true });
    assert.equal(ctx._cbRestoring, true);
    mod.cbAdoptAsBranch();
    assert.equal(ctx._cbRestoring, false);
    assert.equal(saved[0].conv, "new-branch");
    assert.equal(saved[0].options.createFrames, true);
    assert.equal(saved[0].stack.length, 2);
    callbacks[0]();
    assert.equal(scrolled, 0);
    assert.equal(ctx._cbConvId, "new-branch");
});

test("a missing branch point cannot be replaced by a later surviving result", async () => {
    const h = historyHarness();
    h.rows.splice(0, 2);
    h.api.initHistWin();
    await Promise.resolve();
    assert.equal(h.read().adopted, undefined);
    assert.equal(h.read().rendered, undefined);
});

function requestTools() {
    const ctx = { _requestTokens: new WeakMap(), _requestEpoch: 0, CURRENT_USER: null,
        _currentProject: null, _learningTarget: null, notifyContextChanged: noop };
    ctx.currentAccountScope = () => ctx.CURRENT_USER?.id || "";
    return { ...functions("core/core.js", ["beginRequest", "invalidateRequest", "setCurrentUser"], ctx),
        currentAccountScope: ctx.currentAccountScope };
}
function deferred() {
    let resolve, reject;
    const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
    return { promise, resolve, reject };
}
function element() {
    const classes = new Set();
    return { value: "test query", disabled: false, hidden: false, innerHTML: "", dataset: {},
        classList: { add: (x) => classes.add(x), remove: (x) => classes.delete(x), contains: (x) => classes.has(x), toggle: noop },
        setAttribute: noop, removeAttribute: noop, querySelector: () => null,
        addEventListener: noop, removeEventListener: noop, focus: noop };
}

test("route busy responses release both submit guards and allow retry", async () => {
    for (const status of [503, 409]) {
        const input = element(), submit = element();
        let calls = 0;
        const ctx = { ...requestTools(), _routeRequests: {}, $: (id) => id === "submitBtn" ? submit : input, _askRelaxPending: null,
            _ubBusy: false, _ubSeq: 0, flowReset: noop, toast: noop, usageEnabled: () => false,
            cbChatInMain: () => false, cbRollConvId: noop, ubInputGhost: noop, autoGrow: noop,
            cbLogPush: noop, getConfig: () => ({}), usageBeginTurn: noop, benchfbTurnBegin: noop,
            cbConvId: () => "conversation", usageScope: () => "account", cbProgressBegin: noop,
            startProgress: () => submit.classList.add("loading"), estimateDuration: () => 0,
            recSeqNow: () => 0, ubNewReqId: () => "request", ubRouteBody: () => ({}),
            API: { utterance: "/route" }, fetch: async () => { calls++; return { ok: false, status, json: async () => ({ detail: "busy" }) }; },
            ubFillQuery: (box, value) => { box.value = value; }, benchfbTurnError: noop,
            startConversationRun: async () => { calls++; throw Object.assign(new Error("busy"), { durableRun: true, busy: true }); },
            cbProgressDrop: noop,
            _fireCtxAfterSend: noop, resetSubmitButton: () => { submit.disabled = false; submit.classList.remove("loading"); } };
        const mod = functions("panel/board.js", ["ubSubmit"], ctx);
        await mod.ubSubmit("hero");
        assert.equal(submit.disabled, false);
        assert.equal(submit.classList.contains("loading"), false);
        assert.equal(ctx._ubBusy, false);
        await mod.ubSubmit("hero");
        assert.equal(calls, 2);
    }
});

test("shared routing sends server revisions and preserves untouched MCP search controls", () => {
    const cfg = { sources: ["all"], auto_parse_sources: false, auto_allow_llm: false, rerank_top_n: 12 };
    const snapshot = { top_k: 10, date_from: "", date_to: "", facet_filters: [], suppressed_constraints: [], lenient_dims: [] };
    const ctx = { _sharedMode: true, _sharedConversation: { id: "server", revision: 7,
        current_state: { query: "MCP query", sources: ["MCP source"], search_params: {
            top_k: 30, date_from: "2020-03-12", date_to: "2021-07-09", auto_parse_sources: true, auto_allow_llm: true, rerank_top_n: 40 } } },
        _sharedControls: { sources: ["all"], search_params: { ...snapshot, auto_parse_sources: false, auto_allow_llm: false, rerank_top_n: 12 } },
        getConfig: () => cfg, searchParamSnapshot: () => snapshot,
        cbFrameData: () => ({ results: [{}], result_total: 999, query_constraints: [{ dim: "stale" }] }),
        cbFrameQuery: () => "browser stale", currentProjectScope: () => "p", replayConvFields: () => ({ conv_id: "server", shared: true }),
        _attachArtifactCtx: (body) => body };
    const api = functions("panel/board.js", ["cbControlSnapshot", "cbSharedPatch", "ubRouteBody"], ctx);
    const body = api.ubRouteBody("continue", { askAnswer: { choice_key: "real-key", suppress: ["exclude:species"] } });
    assert.equal(body.shared, true); assert.equal(body.conv_id, "server"); assert.equal(body.expected_revision, 7);
    assert.equal(body.ask_choice, "real-key");
    for (const field of ["has_results", "result_total", "current_filters", "replay_key", "query", "sources", "auto_parse_sources", "auto_allow_llm", "rerank_top_n"]) assert.equal(field in body, false);
    assert.deepEqual(Array.from(body.state_patch.sources), ["MCP source"]);
    assert.equal(body.state_patch.search_params.date_from, "2020-03-12");
    assert.equal(body.state_patch.search_params.top_k, 30);
    assert.equal(body.state_patch.search_params.auto_parse_sources, true);
    assert.equal(body.state_patch.search_params.auto_allow_llm, true);
    assert.equal(body.state_patch.search_params.rerank_top_n, 40);
    cfg.auto_parse_sources = true; cfg.auto_allow_llm = true; cfg.rerank_top_n = 20;
    const changed = api.ubRouteBody("continue", {});
    assert.equal(changed.state_patch.search_params.rerank_top_n, 20);
    ctx._sharedConversation = null; ctx._sharedControls = null;
    const fresh = api.ubRouteBody("new", {});
    assert.equal("conv_id" in fresh, false); assert.equal(fresh.expected_revision, 0);
    assert.equal(fresh.state_patch.search_params.auto_parse_sources, true);
    assert.equal(fresh.state_patch.search_params.auto_allow_llm, true);
    assert.equal(fresh.state_patch.search_params.rerank_top_n, 20);
});

test("owned replay uses key-free URLs and an explicit observed-version payload", async () => {
    let shared = true; const requests = [];
    const ctx = { JSON_HEADERS: { "Content-Type": "application/json" },
        replayConvAuth: () => ({ conv_id: "owned", shared, key: "legacy-key" }),
        _replayIdentity: () => "identity", _identityCurrent: () => true,
        fetch: async (url, options) => { requests.push([url, options?.body && JSON.parse(options.body)]); return { ok: true, json: async () => ({ ok: true }) }; } };
    const api = functions("panel/replay.js", ["_ownedReplayURL", "_get", "_post"], ctx);
    await api._get("/api/replay/points?conv_id=owned");
    await api._post("/api/replay/seen", { conv_id: "owned", key: "legacy-key", observed_version: 4, observed_pids: ["p1"] });
    assert.equal(requests[0][0], "/api/conversations/owned/replay?action=points");
    assert.equal(requests[1][0], "/api/conversations/owned/replay/seen");
    assert.deepEqual(requests[1][1], { data: { observed_version: 4, observed_pids: ["p1"] } });
    await api._post("/api/replay/snapshot/s1/annotate", { conv_id: "owned", key: "legacy-key", pid: "p1", text: "note" });
    assert.equal(requests[2][0], "/api/conversations/owned/replay/annotate_snapshot");
    assert.equal(requests[2][1].data.snap_id, "s1");
    shared = false;
    await api._post("/api/replay/annotate", { conv_id: "legacy", key: "legacy-key", pid: "p1", text: "note" });
    assert.equal(requests[3][0], "/api/replay/annotate"); assert.equal(requests[3][1].key, "legacy-key");
});

function reviewNode(tag = "div") {
    const node = element();
    node.tag = tag; node.children = []; node.listeners = {}; node.valid = true;
    node.appendChild = (child) => { node.children.push(child); return child; };
    node.addEventListener = (name, callback) => { node.listeners[name] = callback; };
    node.reportValidity = () => node.valid;
    node.remove = () => { node.removed = true; };
    return node;
}

function sharedDeletionHarness(queryValue, chatValue) {
    const api = requestTools(), query = element(), chat = element(), notices = [], forgotten = [], urls = [];
    api.setCurrentUser({ id: "alice" }); query.value = queryValue; chat.value = chatValue;
    let reads = 0;
    const ctx = { ...api, _sharedMode: true, _sharedConversation: { id: "gone", revision: 1, current_state: { query: "canonical" } },
        _sharedRequests: {}, _routeRequests: {}, _cbConvId: "gone", _cbHistoryTimer: 0, _cbRestoreSeq: 0,
        _ubSeq: 7, _ubBusy: false, clearTimeout: noop,
        $: (id) => id === "queryInput" ? query : id === "chatInput" ? chat : null,
        getSharedConversation: async () => { reads++; throw Object.assign(new Error("gone"), { status: 404, code: "conversation_not_found" }); },
        forgetSharedConversationRuns: (id) => forgotten.push(id), exitResultsLayout: noop, autoGrow: noop,
        bumpRecSeq: noop, resetSubmitButton: noop, replayReset: noop, swSync: noop, notifyContextChanged: noop,
        cbProgressBegin: noop, cbProgressDrop: noop, cbLogPush: () => { throw new Error("deleted task must not publish an error bubble into a new conversation"); },
        toast: (text) => notices.push(text), history: { replaceState: (_a, _b, url) => urls.push(url) }, location: { pathname: "/" } };
    const mod = functions("panel/board.js", ["cbRollConvId", "cbClear", "cbSharedConversation", "cbSharedMissing", "cbSharedRunMissing", "cbRefreshSharedConversation", "cbRecoverRun"], ctx);
    return { ctx, mod, query, chat, notices, forgotten, urls, reads: () => reads };
}

test("a deleted shared conversation is invalidated once without losing the unfinished draft", async () => {
    for (const [queryDraft, chatDraft] of [["typed query draft", "typed chat draft"], ["canonical", "typed chat draft"]]) {
        const h = sharedDeletionHarness(queryDraft, chatDraft), oldRoute = h.ctx.beginRequest(h.ctx._routeRequests);
        await h.mod.cbRefreshSharedConversation();
        await h.mod.cbRefreshSharedConversation();
        assert.equal(h.reads(), 1); assert.equal(h.notices.length, 1);
        assert.equal(h.mod.cbSharedConversation().id, ""); assert.equal(h.ctx._cbConvId, "");
        assert.equal(h.ctx._ubBusy, false); assert.equal(oldRoute.isCurrent(), false);
        assert.deepEqual(h.forgotten, ["gone"]); assert.equal(h.urls.at(-1), "/");
        assert.equal(h.query.value, queryDraft === "canonical" ? chatDraft : queryDraft);
        assert.equal(h.chat.value, queryDraft === "canonical" ? "" : chatDraft);
    }
});

test("only the matching conversation_not_found code may invalidate the displayed shared identity", () => {
    const h = sharedDeletionHarness("draft", "");
    assert.equal(h.mod.cbSharedMissing({ status: 404, code: "other_not_found" }, "gone"), false);
    assert.equal(h.mod.cbSharedMissing({ status: 404, code: "conversation_not_found" }, "another"), false);
    assert.equal(h.ctx._cbConvId, "gone"); assert.equal(h.notices.length, 0);
});

test("a run removed with its shared conversation stops recovery and releases the old binding", async () => {
    const h = sharedDeletionHarness("draft", "");
    h.ctx.awaitConversationRun = async () => { throw Object.assign(new Error("run gone"), { status: 404, code: "run_not_found" }); };
    await h.mod.cbRecoverRun({ shared: true, owner: "alice", conversation_id: "gone", run_id: "old-run" });
    assert.equal(h.reads(), 1); assert.equal(h.ctx._cbConvId, ""); assert.equal(h.ctx._ubBusy, false);
    assert.deepEqual(h.forgotten, ["gone"]); assert.equal(h.notices.length, 1);
});

test("a zero-hit shared history row keeps rerun and failed opening keeps the history window open", async () => {
    const row = reviewNode(), main = reviewNode("button"), rerun = reviewNode("button"), del = reviewNode("button");
    row.querySelector = (selector) => selector === ".hist-main" ? main : selector === ".hist-del" ? del : selector === ".hist-rerun" ? rerun : null;
    let opened = false, closed = 0;
    const ctx = { document: { createElement: () => row }, workspaceConversationMenu: noop, escapeHtml: String,
        fmtTime: () => "time", openSharedHistory: async () => opened, viewHistorySnapshot: noop, toast: noop,
        COPY: { common: { delete: "delete" } }, armTwoStepConfirm: () => false };
    const api = functions("search/browse.js", ["createHistoryRow"], ctx);
    const summary = { shared: true, convId: "zero", revision: 2, frame_count: 1, message_count: 2, chatOnly: false, count: 0, query: "query", at: 1 };
    api.createHistoryRow([summary, { ...summary }], { onOpen: () => closed++ });
    assert(row.innerHTML.includes("hist-rerun")); assert(!row.innerHTML.includes("仅对话")); assert(!row.innerHTML.includes("共 2 轮"));
    main.listeners.click(); await new Promise((resolve) => setImmediate(resolve)); assert.equal(closed, 0);
    opened = true; main.listeners.click(); await new Promise((resolve) => setImmediate(resolve)); assert.equal(closed, 1);
});

test("import preview lazily exposes every message and complete frame without truncating the data", () => {
    const container = reviewNode(), data = { title: "complete", current_state: { query: "query" },
        messages: Array.from({ length: 45 }, (_, i) => ({ role: "user", text: "message-" + i })),
        frames: Array.from({ length: 3 }, (_, i) => ({ payload: { results: [{ id: i, text: "full".repeat(300) }] } })) };
    const before = JSON.stringify(data), ctx = { document: { createElement: (tag) => reviewNode(tag) } };
    functions("search/browse.js", ["renderHistoryImportPreview"], ctx).renderHistoryImportPreview(container, data);
    assert.equal(container.children.length, 7);
    assert.equal(container.children.filter((n) => n.children.some((c) => c.tag === "pre")).length, 1);
    const messages = [], frames = [];
    for (const detail of container.children.slice(1)) {
        detail.open = true; detail.listeners.toggle(); detail.listeners.toggle();
        const pre = detail.children.filter((n) => n.tag === "pre"); assert.equal(pre.length, 1);
        const records = JSON.parse(pre[0].textContent);
        (detail.children[0].textContent.startsWith("消息") ? messages : frames).push(...records);
    }
    assert.deepEqual(messages, data.messages); assert.deepEqual(frames, data.frames); assert.equal(JSON.stringify(data), before);
});

test("an unchecked import keeps its preview, expired identity closes it, and confirmation sends the full stable snapshot", async () => {
    const contextListeners = new Set();
    let current = true, modal, closed = 0;
    const requests = [], data = { title: "complete", current_state: {}, messages: Array.from({ length: 45 }, (_, i) => ({ role: "user", text: "m" + i })), frames: [{ payload: { results: [] } }] };
    const createModal = () => {
        const node = reviewNode(), form = reviewNode("form"), cancel = reviewNode("button"), button = reviewNode("button"), input = reviewNode("input"), preview = reviewNode();
        node.form = form;
        node.querySelector = (selector) => selector === "form" ? form : selector === ".hist-import-cancel" ? cancel : selector === '[type="submit"]' ? button : selector === "input" ? input : selector === ".history-import-preview" ? preview : null;
        return node;
    };
    const ctx = { currentAccountScope: () => "alice", beginRequest: () => ({ isCurrent: () => current }), cbLegacyImport: () => data,
        registerContextChanged: (fn) => { contextListeners.add(fn); return () => contextListeners.delete(fn); },
        crypto: webcrypto, TextEncoder, document: { createElement: () => (modal = createModal()), body: { appendChild: noop } },
        renderHistoryImportPreview: (_container, snapshot) => assert.equal(snapshot, data), closeModal: () => closed++, openModal: noop,
        trapModalFocus: noop, sharedConversationAction: async (_id, action, _revision, body) => { requests.push({ action, body }); return { conversation: { id: "imported" } }; },
        listSharedConversations: async () => [], renderHistory: noop, notifyContextChanged: noop, openSharedHistory: async () => false, toast: noop };
    const api = functions("search/browse.js", ["previewHistoryImport"], ctx), group = [{ convId: "legacy", project_id: "" }];
    const submit = () => modal.form.listeners.submit({ preventDefault: noop, currentTarget: modal.form });
    await api.previewHistoryImport(group); modal.form.valid = false;
    await submit(); assert.equal(closed, 0); assert.equal(requests.length, 0); assert.equal(modal.removed, undefined);
    current = false; await submit(); assert.equal(closed, 1); assert.equal(requests.length, 0);
    current = true; await api.previewHistoryImport(group); await submit();
    await api.previewHistoryImport(group); await submit();
    assert.equal(requests.length, 2); assert.equal(requests[0].body.import_id, requests[1].body.import_id);
    assert.equal(requests[0].body.data.messages.length, 45); assert.equal(requests[0].body.data.frames.length, 1);
    assert.equal(contextListeners.size, 0, "successful import removes its account listener");
    await api.previewHistoryImport(group); assert.equal(contextListeners.size, 1);
    contextListeners.forEach((fn) => fn("before-account"));
    assert.equal(modal.removed, true, "account transition immediately hides the old private preview without waiting for submit");
    assert.equal(contextListeners.size, 0); assert.equal(closed, 4);
    await api.previewHistoryImport(group); contextListeners.forEach((fn) => fn("account"));
    assert.equal(modal.removed, true); assert.equal(contextListeners.size, 0);
});

for (const [title, file, names, argument, setup] of [
    ["staged facets", "search/facets.js", ["facetStageSubmit"], undefined, (ctx) => { ctx._facetStage = [{ dim: "species", value: "human" }]; ctx.facetStageDiff = () => ({ added: ctx._facetStage.slice(), removed: [] }); ctx.renderFacetStage = noop; ctx.facetDrawerSet = noop; ctx.swShowBoard = noop; }],
    ["facet toggle", "search/facets.js", ["_facetIdx", "toggleFacet"], ["species", "human", "人", "物种"], (ctx) => { ctx.facetStageActive = () => false; }],
    ["facet removal", "search/facets.js", ["_facetIdx", "removeFacet"], ["species", "human"], (ctx) => { ctx._facetFilters = [{ dim: "species", value: "human", label: "物种" }]; }],
    ["constraint suppression", "search/facets.js", ["toggleQueryHit"], ["include:species"], (ctx) => { ctx.cbLabelForFilterId = () => "物种"; }],
    ["coverage relaxation", "search/results.js", ["relaxDimFully"], ["species"], (ctx) => { ctx.CB_DIM_LABEL = { species: "物种" }; }],
    ["lenient filtering", "search/results.js", ["toggleLenient"], ["species"], (ctx) => { ctx.CB_DIM_LABEL = { species: "物种" }; }],
    ["source and date scope", "core/interactions.js", ["scopeSidebarRerun"], ["范围：发表时间 2020–2024"], (ctx) => { ctx.document = { body: { classList: { contains: () => true } } }; ctx.$ = () => ({ value: "effective query" }); }],
    ["condition reset", "panel/board.js", ["cbResetAllConditions"], undefined, (ctx) => { ctx.cbCancel = noop; ctx.resetFacetState = noop; }],
    ["condition plan", "panel/board.js", ["cbCommit"], [{ message: "忽略条件：物种", next_request: { query: "effective query", suppressed_constraints: ["include:species"], facet_filters: [] } }], (ctx) => { ctx.cbProgressBegin = noop; }],
]) {
    test("shared operation text stays explicit through " + title, () => {
        const logs = [], calls = [], ctx = { _facetFilters: [], _suppressed: [], _lenientDims: [], $: () => null,
            cbLogPush: (kind, text) => logs.push({ kind, text }), runRecommend: (opts) => calls.push(opts),
            prettyFacetValue: String, usageLog: noop, USAGE_KINDS: {}, toast: noop };
        ctx.setFacetState = (patch) => {
            if (patch.facetFilters) ctx._facetFilters = patch.facetFilters;
            if (patch.suppressed) ctx._suppressed = patch.suppressed;
            if (patch.lenientDims) ctx._lenientDims = patch.lenientDims;
        };
        setup(ctx);
        const api = functions(file, names, ctx); api[names.at(-1)](...(argument || []));
        assert.equal(logs.length, 1); assert.equal(logs[0].kind, "refine"); assert.equal(calls.length, 1);
        assert.equal(calls[0].sharedUtterance, logs[0].text); assert.equal(calls[0].sayPushed, true); assert.equal(calls[0].messageKind, "refine");
        assert(calls[0].keepFacets || calls[0].fromBoard);
    });
}

test("shared recommendation separates the clicked operation from its effective query without inspecting old messages", async () => {
    const calls = [], ctx = { cbSharedConversation: () => ({ id: "shared", current_state: { query: "server query" } }),
        $: () => ({ value: "unfinished composer draft" }), toast: noop, _cbLog: [{ text: "unrelated historical text" }],
        ubSubmit: async (source, opts) => calls.push({ source, opts }) };
    const api = functions("search/search.js", ["runRecommend"], ctx);
    await api.runRecommend({ keepFacets: true, sharedUtterance: "忽略条件：物种", sayPushed: true, messageKind: "refine" });
    assert.equal(calls[0].source, "chat"); assert.equal(calls[0].opts.text, "忽略条件：物种");
    assert.equal(calls[0].opts.sharedQuery, "server query"); assert.equal(calls[0].opts.sayPushed, true);
    assert.equal(calls[0].opts.preserveComposer, true);
    await api.runRecommend({ queryOverride: "explicit effective query", sharedUtterance: "改条件", sayPushed: true });
    assert.equal(calls[1].opts.sharedQuery, "explicit effective query");
});

test("shared submit honors sayPushed and otherwise renders one operation bubble rather than a retrieval sentence", async () => {
    for (const [messageKind, sayPushed] of [["refine", true], ["refine", false], [undefined, true], [undefined, false]]) {
        const query = element(), chat = element(), submit = element(), logs = [];
        query.value = "typed query draft"; chat.value = "typed chat draft";
        const ctx = { ...requestTools(), _routeRequests: {}, _sharedMode: true, _sharedConversation: { id: "shared", revision: 2 },
            _askRelaxPending: null, _ubBusy: false, _ubSeq: 0, $: (id) => id === "submitBtn" ? submit : id === "queryInput" ? query : chat,
            flowReset: noop, toast: noop, usageEnabled: () => false, cbChatInMain: () => false, autoGrow: noop,
            cbLogPush: (kind, text) => logs.push({ kind, text }), getConfig: () => ({}), usageBeginTurn: noop,
            benchfbTurnBegin: noop, cbConvId: () => "shared", usageScope: () => "alice", cbProgressBegin: noop,
            startProgress: noop, estimateDuration: () => 0, recSeqNow: () => 0, ubNewReqId: () => "request", ubRouteBody: (text) => ({ utterance: text }),
            startConversationRun: async () => ({ row: { shared: true }, result: { ok: true, shared: true } }),
            cbSharedFinal: async (_reply, _row, _text, _current, opts) => { assert.equal(opts.preserveComposer, true); return true; },
            forgetConversationRun: noop, _fireCtxAfterSend: noop };
        await functions("panel/board.js", ["ubSubmit"], ctx).ubSubmit("chat", { text: "忽略条件：物种", sharedQuery: "effective query", sharedRerun: true, sayPushed, messageKind, preserveComposer: true });
        assert.equal(logs.length, sayPushed ? 0 : 1);
        if (!sayPushed) assert.deepEqual(logs[0], { kind: messageKind || "say", text: "忽略条件：物种" });
        assert.equal(query.value, "typed query draft"); assert.equal(chat.value, "typed chat draft");
    }
});

test("owned redraw leaves absent configuration to the server and does not short-circuit a browser mock setting", async () => {
    const coreContext = { _sharedFn: () => ({ id: "owned" }), _llmFn: null, _LLM_FALLBACK: { provider: "mock", use_llm: false } };
    const core = functions("core/replay_core.js", ["llmOverrides"], coreContext);
    assert.equal("provider" in core.llmOverrides(), false);
    coreContext._llmFn = () => ({ use_llm: true, model: "configured" });
    assert.equal("provider" in core.llmOverrides(), false); assert.equal(core.llmOverrides().use_llm, true);
    for (const overrides of [{}, { provider: "mock", use_llm: false, mock_llm: true }]) {
        const calls = [], notes = []; let loads = 0;
        const ctx = { $: () => null, replayConvAuth: () => ({ conv_id: "owned", shared: true }), llmOverrides: () => overrides,
            _setNote: (text) => notes.push(text), _post: async (_url, body) => { calls.push(body); return { ok: true }; },
            loadPoints: async () => loads++, RP: { digest: "/api/replay/digest" }, toast: noop, skipReasonZh: String,
            _graphCollapsed: () => false };
        await functions("panel/replay.js", ["_redraw"], ctx)._redraw();
        assert.equal(calls.length, 1); assert.equal(loads, 1);
        assert.equal(calls[0].provider, overrides.provider); assert(!notes.some((n) => n === "mock"));
    }
    // 整理被跳过：展开态这句话归图谱内 .g-skip 单处显示，只有图谱折叠时状态行才兜底
    for (const collapsed of [false, true]) {
        const notes = [];
        const ctx = { $: () => null, replayConvAuth: () => ({ conv_id: "owned", shared: true }), llmOverrides: () => ({}),
            _setNote: (text) => notes.push(text), _post: async () => ({ ok: false, skipped: "no_key" }),
            loadPoints: async () => false, RP: { digest: "/api/replay/digest" }, toast: noop, skipReasonZh: String,
            _graphCollapsed: () => collapsed };
        await functions("panel/replay.js", ["_redraw"], ctx)._redraw();
        assert.equal(notes.includes("no_key"), collapsed,
            collapsed ? "折叠态：状态行兜底显示 skip 原因" : "展开态：skip 原因不写状态行（.g-skip 单处显示）");
    }
});

test("favorite animation fallback removes listeners without animationend", () => {
    const listeners = new Set(), timers = new Map(), node = element(); let seq = 0;
    node.addEventListener = (_event, fn) => listeners.add(fn);
    node.removeEventListener = (_event, fn) => listeners.delete(fn);
    const ctx = { _favAnimations: new WeakMap(), setTimeout: (fn) => { timers.set(++seq, fn); return seq; },
        clearTimeout: (id) => timers.delete(id) };
    const mod = functions("panel/fav_folders.js", ["_favFlash"], ctx);
    for (let i = 0; i < 20; i++) mod._favFlash(node, "fav-pop");
    assert.equal(listeners.size, 1); assert.equal(timers.size, 1);
    [...timers.values()][0]();
    assert.equal(listeners.size, 0); assert.equal(timers.size, 0);
    mod._favFlash(node, "fav-pop"); [...listeners][0]();
    assert.equal(timers.size, 0, "normal animation completion also cancels fallback");
});

test("facet consumers commit new arrays through the owner", () => {
    const original = [{ dim: "species", value: "human" }], suppressed = [];
    let writes = 0;
    const ctx = { _facetFilters: original, _suppressed: suppressed, facetStageActive: () => false,
        _facetIdx: () => 0, cbLogPush: noop, prettyFacetValue: String, cbLabelForFilterId: String,
        usageLog: noop, USAGE_KINDS: {}, runRecommend: noop,
        setFacetState: (patch) => { writes++; if (patch.facetFilters) ctx._facetFilters = patch.facetFilters;
            if (patch.suppressed) ctx._suppressed = patch.suppressed; } };
    const mod = functions("search/facets.js", ["toggleFacet", "toggleQueryHit"], ctx);
    mod.toggleFacet("species", "human", "human", "species"); mod.toggleQueryHit("species");
    assert.equal(writes, 2); assert.equal(original.length, 1); assert.equal(suppressed.length, 0);
    const bs = { page: 1, q: "" };
    functions("search/browse.js", ["setBrowseState"], { bs }).setBrowseState({ page: 2, q: "query" });
    assert.equal(bs.page, 2); assert.equal(bs.q, "query");
    assert.doesNotMatch(source("core/interactions.js"), /\bbs\.\w+\s*(?:=(?!=)|\+\+|--)/);
    assert.doesNotMatch(source("core/progress.js"), /export const _queryCache/);
});

test("skill and citation download links enter the shared queue delegation", () => {
    const html = readFileSync(new URL("../../web/static/index.html", import.meta.url), "utf8");
    const links = [...html.matchAll(/<a\b[^>]*href="\/api\/guide\/skill.zip"[^>]*>/g)];
    assert.equal(links.length, 2);
    for (const [tag] of links) assert.match(tag, /data-dlq="other"/);
    const card = functions("act/act.js", ["actCiteExportCardHtml"], {
        API: { citationsDownload: "/citations" }, escapeHtml: String, tpBytes: String,
    }).actCiteExportCardHtml({ files: [{ filename: "citation.ris" }] });
    assert.match(card, /download data-dlq="cite"/);
    let handler, queued, prevented = false;
    functions("core/downloads.js", ["initDownloads"], {
        _inited: false, document: { addEventListener: (_type, fn) => { handler = fn; } },
        isHttp: () => true, dlqEnqueue: (items) => { queued = items; return [{ url: items[0].url, status: "fired" }]; },
        toast: noop, COPY: { common: {} },
    }).initDownloads();
    const anchor = { href: "https://example.invalid/citations?f=citation.ris",
        getAttribute: (key) => ({ "data-dlq": "cite", "data-dlq-name": "citation.ris" })[key] || "" };
    handler({ target: { closest: () => anchor }, preventDefault: () => { prevented = true; } });
    assert.equal(prevented, true); assert.equal(queued[0].kind, "cite");
});

test("feasibility results cannot overwrite a reopened panel", async () => {
    const api = requestTools(), a = deferred(), b = deferred(), panel = element(), input = element();
    panel.hidden = true; input.value = "A";
    const bodies = [];
    /* 统计请求体必须是**主检索同一段提取**——facet/suppressed/lenient/日期一个不漏。
       注入的 searchParamSnapshot 就是主检索那段提取的替身，断言它整段进了请求体。 */
    const ctx = { ...api, $: (id) => id === "feasibilityPanel" ? panel : input,
        API: {}, fetch: (_url, options) => { bodies.push(JSON.parse(options.body));
            return JSON.parse(options.body).query === "a" ? a.promise : b.promise; },
        getSelectedSources: () => [], getSourceMode: () => "auto", toast: noop, escapeHtml: String,
        queryForRetrieval: (q) => String(q).trim().toLowerCase(),
        searchParamSnapshot: () => ({ facet_filters: [{ dim: "species", value: "homo sapiens" }],
            suppressed_constraints: ["tissue"], lenient_dims: ["disease"], date_from: "2020-01-01", date_to: "2024-12-31" }),
        renderFeasibility: (node, report) => { node.innerHTML = report.name; } };
    const mod = functions("search/results.js", ["loadFeasibility"], ctx);
    const old = mod.loadFeasibility();
    await mod.loadFeasibility(); input.value = "B";
    const current = mod.loadFeasibility();
    b.resolve({ ok: true, json: async () => ({ ok: true, report: { name: "B" } }) }); await current;
    a.reject(new Error("old failure")); await old;
    assert.equal(panel.innerHTML, "B"); assert.equal(panel.hidden, false);
    assert.equal(bodies[0].query, "a", "查询句同走主检索的归一（queryForRetrieval）");
    assert.deepEqual(bodies[0].facet_filters, [{ dim: "species", value: "homo sapiens" }]);
    assert.deepEqual(bodies[0].suppressed_constraints, ["tissue"]);
    assert.deepEqual(bodies[0].lenient_dims, ["disease"]);
    assert.equal(bodies[0].date_from, "2020-01-01");
    assert.equal(bodies[0].date_to, "2024-12-31");
    assert.equal(bodies[0].sources.length, 0);
    assert.equal(bodies[0].auto_parse_sources, true);
});

test("reuse packs reject changed selections even when their counts match", async () => {
    const api = requestTools(), response = deferred();
    let selection = "A:one,two", rendered = 0, downloaded = 0;
    const ctx = { ...api, _packBusy: false, _packRequest: null, _reuseRequests: {}, _reuseScope: "A",
        CURRENT_USER: { id: "user" }, favUidsForPack: () => ["one", "two"],
        reuseSelectionKey: () => selection, reuseScopeLabel: () => "folder-A", $: () => element(),
        API: {}, fetch: () => response.promise, toast: noop, syncReuseBar: noop,
        renderReusePack: () => rendered++, autoDownloadReuseTrio: () => downloaded++, escapeHtml: String };
    const pending = functions("act/reuse_pack.js", ["buildReusePack"], ctx).buildReusePack();
    selection = "B:three,four";
    response.resolve({ ok: true, json: async () => ({ ok: true, pack: {}, markdown: "A" }) });
    await pending;
    assert.equal(rendered, 0); assert.equal(downloaded, 0); assert.equal(ctx._packBusy, false);
});

test("feedback completions and auto-close timers belong to one dialog instance", async () => {
    const api = requestTools(), a = deferred(), b = deferred(), timers = [];
    let next = a, status = "";
    const ctx = { ...api, _feedbackRequests: {}, _feedbackCloseTimer: null,
        _dialog: { state: "idle", fid: "A" }, $: () => element(), _scope: () => "account",
        feedbackTextState: () => ({ ok: true }), feedbackEntryBuild: () => ({}), feedbackEnqueue: noop,
        _loadUploadModule: async () => { const captured = next; return { sendFeedback: () => captured.promise }; },
        _setStatus: (s) => { status = s; }, toast: noop, closeModal: noop,
        setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout: noop };
    const mod = functions("core/feedback.js", ["_onSend", "_closeDialog", "_resetDialogRequest"], ctx);
    const first = mod._onSend(); await new Promise(setImmediate);
    mod._closeDialog(); ctx._dialog = { state: "idle", fid: "B" }; next = b;
    const second = mod._onSend(); await new Promise(setImmediate);
    a.resolve(true); await first;
    assert.equal(ctx._dialog.state, "sending"); assert.equal(timers.length, 0);
    b.resolve(true); await second;
    assert.equal(ctx._dialog.state, "done"); assert.equal(timers.length, 1);
    mod._closeDialog(); ctx._dialog = { state: "idle", fid: "C" };
    timers[0](); assert.equal(ctx._dialog.fid, "C");
});

test("replay points cannot cross conversations or overwrite newer loads", async () => {
    const api = requestTools(), a = deferred(), b = deferred();
    let conversation = "A", graph = null, chat = null;
    const ctx = { ...api, _pointsRequests: {}, replayConvAuth: () => ({ conv_id: conversation }),
        _q: (auth) => auth.conv_id, RP: { points: "/points/" },
        _get: (url) => url.endsWith("A") ? a.promise : b.promise,
        _points: null, _graph: { reload: (d) => { graph = d; } }, _setNote: noop,
        _captureFresh: () => true, _stamp: (d) => d.id, _renderEmpty: noop,
        _renderChat: (msgs) => { chat = msgs; }, _applyNote: noop, _refreshSnapshotPills: noop };
    const mod = functions("panel/replay.js", ["loadPoints"], ctx);
    const first = mod.loadPoints(); conversation = "B";
    const second = mod.loadPoints();
    b.resolve({ ok: true, id: "B", threads: { session: ["B"] } }); await second;
    a.resolve({ ok: true, id: "A", threads: { session: ["A"] } });
    assert.equal(await first, false); assert.equal(graph.id, "B"); assert.equal(chat[0], "B");
    assert.ok(!source("panel/replay_graph.js").includes("pointsUrl"), "graph has one injected data channel");
});

test("confirming a proposal removes its row after another row is dismissed without duplicate submission", async () => {
    const api = requestTools(), response = deferred();
    api.setCurrentUser({ id: "A" });
    const [a,b,c] = ["first","second","third"].map((id) => ({id,text:"明确偏好 " + id,tier:"explicit"}));
    const calls = [];
    const ctx = { ...api, learningTargetScope: () => "project_A", _pendingToken: api.beginRequest({}), _pending: [a,b,c],
        _confirming: new WeakMap(), API: {knowledgePropose:"/api/knowledge/propose"},
        _post: (url,body) => {calls.push({url,body});return response.promise;},
        rulesStatus: noop, toast: noop, _render: noop, renderCollabRules: async () => {} };
    const mod = functions("panel/rules.js", ["_confirm", "_dismiss"], ctx);
    const pending = mod._confirm(b, element());
    await mod._confirm(b, element());
    assert.equal(calls.length,1,"rebuilt buttons cannot submit an in-flight row twice");
    assert.equal(calls[0].body.operations[0].after.kind,"memory");
    assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body.operations[0].after.payload)),{});
    assert.match(calls[0].body.request_id,/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/);
    assert.equal(calls[0].body.source_refs[0].project_id,"project_A");
    mod._dismiss(a);
    response.resolve({ok:true,stage:"draft",change_id:"kc_second",change_revision:1}); await pending;
    assert.equal(ctx._pending.length,1);assert.equal(ctx._pending[0],c);
});

test("resetting task packs rejects both stale previews and stale builds", async () => {
    const api = requestTools(), response = deferred(), panel = element();
    let rendered = 0, downloaded = 0;
    const ctx = { ...api, _tpRequests: {}, _tpBuildRequests: {}, _tpPlan: null, _tpChosen: new Set(),
        _tpCount: {}, _tpBusy: false, $: () => panel, tpRequestBody: (x) => x,
        API: {}, fetch: () => response.promise, tpIdentityLookup: async () => {},
        escapeHtml: String, renderTaskPackPlan: () => rendered++, toast: noop, dlqFireBlob: () => downloaded++ };
    const mod = functions("act/task_pack.js", ["previewTaskPack", "resetTaskPack", "buildTaskPack"], ctx);
    const pending = mod.previewTaskPack();
    mod.resetTaskPack();
    response.resolve({ ok: true, json: async () => ({ ok: true, plan: { candidate_uids: ["A"] } }) });
    assert.equal((await pending).stale, true); assert.equal(ctx._tpPlan, null); assert.equal(rendered, 0);
    const build = deferred(); ctx.fetch = () => build.promise;
    ctx._tpPlan = { retrieval: {} }; ctx._tpChosen = new Set(["A"]);
    const building = mod.buildTaskPack();
    mod.resetTaskPack(); ctx._tpPlan = { retrieval: {}, name: "B" }; ctx._tpBusy = true;
    build.resolve({ ok: true, status: 200, blob: async () => ({}) });
    assert.equal((await building).stale, true);
    assert.equal(downloaded, 0); assert.equal(ctx._tpBusy, true, "old finally cannot unlock a new build");
});

function selectedLearningHarness() {
    const api=requestTools(),calls=[],notices=[],cfg={base_url:"https://example.invalid/v1",model:"m",provider:"openai-compatible",use_llm:true};
    api.setCurrentUser({id:"A"});let project="project_A",published=0;
    const ctx={...api,...knowledgeCore,URLSearchParams,_requests:{},_sourceRequests:{},_sourcesError:"",_sourceIdentity:"",
        _sources:[{id:"source_A",project_id:project,conversation_id:"conv_A",source_hash:"hash_A",fresh:true}],
        _learnState:{conversationIds:["source_A"],targetProject:project,localText:"",manualOnly:true,busy:false},
        learningTargetScope:()=>project,setLearningTargetScope:(p)=>{project=p;},getConfig:()=>cfg,endpointCfg:()=>cfg,
        workspaceProjects:()=>[],workspaceProjectName:(p)=>p,window:{confirm:()=>true},
        learnStatus:(text,error)=>notices.push({text,error}),loadLearningStatus:async()=>{},
        submitLearningTrigger:(body)=>{const io=deferred();calls.push({body,...io});return io.promise;},refreshLearnSummary:async()=>published++};
    const mod=functions("panel/dream.js",["defaultLearnState","state","endpointIdentity","identity","runLearn"],ctx);
    Object.assign(ctx,mod);ctx._sourceIdentity=mod.endpointIdentity();
    return {api,ctx,mod,cfg,calls,notices,project:(p)=>{project=p;},published:()=>published};
}

test("selected learning and source loading cannot publish or save a previous account after A/B/A", async () => {
    const h=selectedLearningHarness(),oldState=h.ctx._learnState,button=element();
    const pending=h.mod.runLearn(button);await settleIO();
    assert.equal(h.calls.length,1);
    assert.deepEqual(Array.from(h.calls[0].body.source_ids),["source_A"]);
    h.api.setCurrentUser({id:"B"});h.api.setCurrentUser({id:"A"});
    const newState={conversationIds:[],targetProject:"project_A",localText:"new A draft",manualOnly:true,busy:true};h.ctx._learnState=newState;
    h.calls[0].resolve({ok:true,scheduled:true,job_id:"old_A",remaining_source_ids:[]});await pending;
    assert.equal(oldState.lastResult,undefined);assert.equal(newState.localText,"new A draft");assert.equal(newState.busy,true);
    assert.equal(h.published(),0,"late completion cannot publish counts or unlock another request");
    const response=deferred();let rendered=0;
    const sourceCtx={...h.api,URLSearchParams,_sourceRequests:{},_sources:[],_sourcesError:"",_sourceIdentity:"",API:{learningSources:"/sources"},
        endpointCfg:()=>h.cfg,fetch:()=>response.promise,$:()=>({}),apiErrorText,renderLearnPanel:()=>rendered++};
    const sources=functions("panel/dream.js",["endpointIdentity","loadSources"],sourceCtx);Object.assign(sourceCtx,sources);
    const loading=sources.loadSources();h.api.setCurrentUser({id:"B"});h.api.setCurrentUser({id:"A"});
    const own=[{id:"new_A",source_hash:"new_hash",project_id:"project_A",fresh:true}];sourceCtx._sources=own;
    response.resolve({ok:true,json:async()=>({ok:true,items:[{id:"old_A",source_hash:"old_hash",project_id:"project_A",fresh:true}],next_offset:null})});await loading;
    assert.equal(sourceCtx._sources,own);assert.equal(rendered,0);
});

test("request tokens reject superseded owners and account round trips", () => {
    const api = requestTools(), owner = {}, another = {};
    const first = api.beginRequest(owner), separate = api.beginRequest(another);
    const second = api.beginRequest(owner);
    assert.equal(first.isCurrent(), false); assert.equal(second.isCurrent(), true);
    api.invalidateRequest(owner);
    assert.equal(second.isCurrent(), false); assert.equal(separate.isCurrent(), true);
    api.setCurrentUser({ id: "A" });
    const account = api.beginRequest(owner);
    api.setCurrentUser({ id: "B" }); api.setCurrentUser({ id: "A" });
    assert.equal(account.isCurrent(), false);
    let identity = "view-A";
    const view = api.beginRequest(owner, () => identity);
    identity = "view-B";
    assert.equal(view.isCurrent(), false);
});

test("selected learning rejects late scope and endpoint results and old finally cannot unlock rebuilt controls", async () => {
    for (const changed of ["scope","endpoint"]) {
        const h=selectedLearningHarness(),oldState=h.ctx._learnState;
        const old=h.mod.runLearn(element());await settleIO();assert.equal(h.calls.length,1);
        if(changed==="scope") h.project("project_B");
        else {h.cfg.base_url="https://second.example.invalid/v1";h.ctx._sourceIdentity=h.mod.endpointIdentity();}
        const fresh={conversationIds:["source_A"],targetProject:changed==="scope"?"project_B":"project_A",localText:"fresh draft",manualOnly:true,busy:false};
        h.ctx._learnState=fresh;const button=element(),current=h.mod.runLearn(button);await settleIO();
        assert.equal(h.calls.length,2);await h.mod.runLearn(element());assert.equal(h.calls.length,2,"rebuilt controls share one in-flight selection");
        h.calls[0].resolve({ok:true,scheduled:true,job_id:"old",remaining_source_ids:[]});await old;
        assert.equal(oldState.lastResult,undefined);assert.equal(fresh.busy,true);assert.equal(button.disabled,true);assert.equal(h.published(),0);
        h.calls[1].resolve({ok:true,scheduled:true,job_id:"new",request_id:h.calls[1].body.request_id,remaining_source_ids:["source_A"]});await current;
        assert.equal(fresh.jobId,"new");assert.equal(fresh.busy,false);assert.equal(button.disabled,false);assert.equal(h.published(),1);
        assert.equal(fresh.localText,"fresh draft");
    }
});

test("clearing a conversation invalidates route and recommendation completions", () => {
    const api = requestTools(), owner = {};
    const token = api.beginRequest(owner);
    let invalidated = 0, reset = 0;
    const ctx = { ...api, _routeRequests: owner, _ubSeq: 7, _ubBusy: true,
        bumpRecSeq: () => invalidated++, resetSubmitButton: () => reset++,
        cbArchiveChatOnly: noop, cbRollConvId: noop, cbLogClear: noop, $: () => null, swSync: noop };
    functions("panel/board.js", ["cbClear"], ctx).cbClear({ archive: false });
    assert.equal(token.isCurrent(), false); assert.equal(ctx._ubBusy, false);
    assert.equal(invalidated, 1); assert.equal(reset, 1);
});

test("HTTP and malformed file responses never poison the successful cache", async () => {
    const cache = new Map(), body = element();
    const ctx = { ...requestTools(), _filesCache: cache, API: { files: "/files" }, renderFilesList: (rows) => JSON.stringify(rows) };
    const mod = functions("search/cards.js", ["loadFilesInto"], ctx);
    for (const [ok, data] of [[false, { detail: "busy" }], [true, { ok: true }], [true, null], [true, { ok: true, files: {} }]]) {
        ctx.fetch = async () => ({ ok, json: async () => data });
        assert.equal((await mod.loadFilesInto(body, { dataset_uid: "A" })).ok, false);
        assert.equal(cache.size, 0);
    }
    ctx.fetch = async () => ({ ok: true, json: async () => ({ ok: true, files: [] }) });
    assert.equal((await mod.loadFilesInto(body, { dataset_uid: "A" })).ok, true);
    assert.equal(cache.size, 1, "a genuine empty success is cacheable");
});

test("file dialog ignores out-of-order success, errors and closed requests", async () => {
    for (const failOld of [false, true]) {
        const api = requestTools(), first = deferred(), second = deferred(), body = element();
        const ctx = { ...api, _filesCache: new Map(), API: { files: "/files" },
            renderFilesList: (rows) => rows.join(","), fetch: (url) => url.includes("uid=A") ? first.promise : second.promise };
        const mod = functions("search/cards.js", ["loadFilesInto"], ctx);
        const a = mod.loadFilesInto(body, { dataset_uid: "A" });
        const b = mod.loadFilesInto(body, { dataset_uid: "B" });
        second.resolve({ ok: true, json: async () => ({ ok: true, files: ["B"] }) }); await b;
        if (failOld) first.reject(new Error("network"));
        else first.resolve({ ok: true, json: async () => ({ ok: true, files: ["A"] }) });
        assert.equal((await a).stale, true); assert.equal(body.innerHTML, "B");
        const closed = deferred(); ctx.fetch = () => closed.promise;
        const pending = mod.loadFilesInto(body, { dataset_uid: "C" });
        api.invalidateRequest(body); body.innerHTML = "closed";
        closed.resolve({ ok: true, json: async () => ({ ok: true, files: ["C"] }) });
        await pending; assert.equal(body.innerHTML, "closed");
    }
});

test("catalog HTTP failure remains retryable", async () => {
    const node = element(); let requests = 0;
    const ctx = { allDatasets: null, browseLoadFailed: false, browseYearFacets: [], browseUnknownYears: 0,
        browseUnknownYearsLive: 0, datasetsGen: 0, bs: {}, $: () => node, API: { datasets: "/datasets" },
        renderBrowse: noop, countUp: noop, escapeHtml: String, prettyPlatform: String,
        fetch: async () => { requests++; return { ok: false, json: async () => ({ detail: "busy" }) }; } };
    const mod = functions("search/browse.js", ["ensureDatasetsLoaded"], ctx);
    await mod.ensureDatasetsLoaded();
    assert.equal(ctx.allDatasets, null); assert.equal(ctx.browseLoadFailed, true);
    ctx.fetch = async () => { requests++; return { ok: true, json: async () => ({ ok: true, records: [], facets: {} }) }; };
    await mod.ensureDatasetsLoaded();
    assert.equal(requests, 2); assert.equal(ctx.browseLoadFailed, false);
});

test("dataset waits for identity before rendering account-scoped favorites", async () => {
    const identity = deferred();
    let ready = false, rendered = false, refresh;
    const ctx = { initAccounts: (opts) => { assert.equal(opts.identityOnly, true); return identity.promise; },
        DS_EMBED: false, DS_ITEM: null, _dsLoaded: {}, location: { hash: "" },
        initDownloads: noop, dsResolveItem: () => ({}), dsParams: () => ({}),
        dsRenderHeader: () => { assert.ok(ready); rendered = true; }, dsRenderTabs: noop,
        dsRenderIntro: noop, setAccountChangedHook: (fn) => { refresh = fn; },
        $: () => null, document: { addEventListener: noop } };
    const pending = functions("search/dataset_page.js", ["dsInit"], ctx).dsInit();
    assert.equal(rendered, false);
    ready = true; identity.resolve(); await pending;
    assert.equal(rendered, true); assert.equal(typeof refresh, "function");
    let notifications = 0;
    functions("panel/accounts.js", ["onAccountChanged"], {
        _accountUIEnabled: false, resetFavFolderState: noop,
        _accountChangedHooks: [() => notifications++],
    }).onAccountChanged();
    assert.equal(notifications, 1, "identity-only changes notify without accessing homepage DOM");
});

function motionHarness(enabled = true) {
    const ghosts = [], microtasks = [], timers = new Map(), timelines = [];
    let timerId = 0;
    function node(box, id = "", tagName = "DIV") {
        const classes = new Set();
        return { box, id, tagName, isConnected: true, style: { opacity: "", width: "", paddingLeft: "",
                setProperty(name, value) { this[name] = value; } },
            classList: { add: (value) => classes.add(value), remove: (value) => classes.delete(value), contains: (value) => classes.has(value) },
            getClientRects() { return this.isConnected ? [this.getBoundingClientRect()] : []; },
            getBoundingClientRect() {
                return { left: this.style.position === "fixed" ? parseFloat(this.style.left) : this.box.left,
                    top: this.style.position === "fixed" ? parseFloat(this.style.top) : this.box.top,
                    width: parseFloat(this.style.width) || this.box.width,
                    height: parseFloat(this.style.height) || this.box.height };
            },
            querySelectorAll: () => [], cloneNode() { return node({ ...this.box }, this.id, this.tagName); },
            removeAttribute(name) { if (name === "id") this.id = ""; }, setAttribute: noop,
            remove() { this.isConnected = false; },
        };
    }
    const body = node({ left: 0, top: 0, width: 1400, height: 900 });
    body.paddingBase = 328; body.appendChild = (ghost) => ghosts.push(ghost);
    const ctx = { _layoutMotion: null, MOTION: enabled, window: { innerWidth: 1366 }, document: { body, hidden: false },
        getComputedStyle: (element) => ({ visibility: element.style.visibility || "visible", opacity: element.style.opacity || "1",
            paddingLeft: element.style.paddingLeft || (element.paddingBase || 0) + "px",
            getPropertyValue: (name) => element.style[name] || ({ display: "block", opacity: "1" }[name] || "") }),
        queueMicrotask: (fn) => microtasks.push(fn),
        setTimeout: (fn) => { timers.set(++timerId, fn); return timerId; }, clearTimeout: (id) => timers.delete(id),
        _contextListeners: new Set(),
        gsap: { timeline(options) {
            const timeline = { options, entries: [], kill: noop,
                to(target, values, start) { this.entries.push({ target, values, start }); return this; } };
            timelines.push(timeline); return timeline;
        } },
    };
    const api = functions("core/core.js", ["_layoutRect", "_layoutGhost", "_layoutCleanup", "_layoutFinish",
        "beginLayoutTransition", "cancelLayoutTransition", "layoutMotionActive", "notifyContextChanged"], ctx);
    return { api, body, node, ghosts, microtasks, timers, timelines, ctx,
        flush: () => { while (microtasks.length) microtasks.shift()(); },
        live: () => ghosts.filter((ghost) => ghost.isConnected) };
}

test("layout moves from pre-update geometry and keeps the real focused draft", () => {
    const h = motionHarness();
    const input = h.node({ left: 560, top: 420, width: 520, height: 80 }, "chatComposer");
    input.focused = true; input.value = "未发送草稿";
    const navigation = h.node({ left: 14, top: 14, width: 300, height: 860 });
    const content = h.node({ left: 328, top: 30, width: 1038, height: 900 });
    const options = { elements: [{ source: input, target: input }], navigation, content, duration: .48 };
    assert.equal(h.api.beginLayoutTransition(options), true);
    assert.equal(h.api.beginLayoutTransition(options), true, "same-turn layout updates coalesce");
    assert.equal(h.live().length, 1); assert.equal(h.live()[0].id, "", "visual copy cannot duplicate control ids");
    input.box = { left: 108, top: 838, width: 280, height: 50 };
    navigation.box.width = 68; content.box.width = 958; h.body.paddingBase = 408;
    h.flush();
    const move = h.timelines[0].entries.find((entry) => entry.target === h.live()[0]);
    assert.equal(move.values.left, 108); assert.equal(move.values.top, 838); assert.equal(move.values.width, 280);
    assert.equal(move.values.duration, .48); assert.equal(move.start, 0);
    assert.equal(input.style.opacity, "0"); assert.equal(input.style.visibility, undefined);
    input.value = "动画中继续输入";
    h.timelines[0].options.onComplete();
    assert.equal(input.style.opacity, ""); assert.equal(input.value, "动画中继续输入"); assert.equal(input.focused, true);
    assert.equal(h.live().length, 0); assert.equal(h.body.classList.contains("layout-motion"), false);
    assert.equal(content.style.width, ""); assert.equal(navigation.style.width, "");
});

test("interrupted layout continues from the moving visual instead of jumping to its destination", () => {
    const h = motionHarness(), input = h.node({ left: 560, top: 420, width: 520, height: 80 }, "chatComposer");
    const options = { elements: [{ source: input, target: input }] };
    h.api.beginLayoutTransition(options); input.box.left = 300; h.flush();
    h.live()[0].style.left = "460px";
    h.api.beginLayoutTransition(options);
    assert.equal(h.live().length, 1); assert.equal(h.live()[0].getBoundingClientRect().left, 460);
    input.box.left = 108; h.flush();
    assert.equal(h.timelines[1].entries[0].values.left, 108);
    h.timelines[0].options.onComplete();
    assert.equal(h.live().length, 1, "late completion of the cancelled transition cannot clear its successor");
    assert.equal(input.style.opacity, "0");
    h.api.notifyContextChanged("before-account");
    assert.equal(h.live().length, 0); assert.equal(input.style.opacity, "");
    assert.equal(h.api.layoutMotionActive(), false);
});

test("layout cleanup works without animation callbacks and reduced motion creates no copies", () => {
    const h = motionHarness(), input = h.node({ left: 560, top: 420, width: 520, height: 80 });
    h.api.beginLayoutTransition({ elements: [{ source: input, target: input }] }); input.box.left = 108; h.flush();
    [...h.timers.values()][0]();
    assert.equal(h.live().length, 0); assert.equal(input.style.opacity, "");
    const reduced = motionHarness(false);
    assert.equal(reduced.api.beginLayoutTransition({ elements: [{ source: input, target: input }] }), false);
    assert.equal(reduced.live().length, 0); assert.equal(reduced.microtasks.length, 0);
});

/* ============================================================================
 * 追踪检查的异步回包身份闸（账户 scope + 挂点属主）
 * ----------------------------------------------------------------------------
 * 直接取 project_updates.js 的真源码函数跑：会话态按（scope × projectId）分区、
 * 回包必须核「发起时 scope == 当前 scope」、挂点必须核「还属于这个追踪」。
 * ========================================================================== */

async function projectUpdatesHarness() {
    const deltas = new Map();
    const logged = [];
    let scope = "acctA";
    const ctx = {
        _deltas: deltas,
        currentAccountScope: () => scope,
        API: { watchCheck: "/api/watch/check" },
        fetch: async () => ({ ok: true, json: async () => ({ ok: true, result: {
            uids: ["u1"], fingerprints: {}, result_total: 1, truncated: false,
            checked_at: "2026-10-04T00:00:00Z" } }) }),
        artifactsUpdateProject: async () => ({}),
        artifactsSetBaseline: (p) => p,
        artifactsTouchCheckedAt: (p) => p,
        watchDiff: () => ({ kind: "diff" }),
        watchDeltaEntries: () => [{ uid: "u1", kind: "added" }],
        watchChangedFlag: () => true,
        usageLog: (kind, payload) => logged.push([kind, payload]),
        USAGE_KINDS: { watch_checked: "watch_checked" },
    };
    const api = functions("core/project_updates.js",
        ["_scope", "_sessionKey", "_scopeStillCurrent", "_deltasOf", "_deltasSet", "_watchCheckOnce"],
        ctx);
    return { api, deltas, logged, setScope: (s) => { scope = s; }, getScope: () => scope };
}

const _P1 = () => ({ project_id: "p1", check_condition: { spec: { spec_version: "v1" }, baseline: { uids: [] } } });

test("session deltas are partitioned per account, never shared across a switch", async () => {
    const h = await projectUpdatesHarness();
    h.api._deltasSet("p1", [{ uid: "u1", kind: "added" }], "acctA");
    assert.equal(h.api._deltasOf("p1").length, 1, "A 自己看得到自己的待查看更新");
    h.setScope("acctB");
    assert.equal(h.api._deltasOf("p1").length, 0, "B 不该看到 A 的待查看更新（同名 projectId 也不行）");
    h.setScope("acctA");
    assert.equal(h.api._deltasOf("p1").length, 1, "A 的会话态仍在自己的分区里");
});

test("a late watch check is dropped once the account switched", async () => {
    const deltas = new Map();
    const logged = [];
    let scope = "acctA";
    const ctx = {
        _deltas: deltas, currentAccountScope: () => scope,
        API: { watchCheck: "/api/watch/check" },
        fetch: async () => {
            scope = "acctB";   // 回包到达前用户已换号（真实窗口）
            return { ok: true, json: async () => ({ ok: true, result: {
                uids: ["u1"], fingerprints: {}, result_total: 1, truncated: false,
                checked_at: "2026-10-04T00:00:00Z" } }) };
        },
        artifactsUpdateProject: async () => ({}), artifactsSetBaseline: (p) => p,
        artifactsTouchCheckedAt: (p) => p, watchDiff: () => ({ kind: "diff" }),
        watchDeltaEntries: () => [{ uid: "u1", kind: "added" }], watchChangedFlag: () => true,
        usageLog: (kind, payload) => logged.push([kind, payload]),
        USAGE_KINDS: { watch_checked: "watch_checked" },
    };
    const api = functions("core/project_updates.js",
        ["_scope", "_sessionKey", "_scopeStillCurrent", "_deltasOf", "_deltasSet", "_watchCheckOnce"], ctx);
    const out = await api._watchCheckOnce(_P1());
    assert.equal(out.error, "account_switched", "换号后的回包必须被整段丢弃");
    assert.equal(api._deltasOf("p1").length, 0, "不许把 A 的检查结果写进 B 的会话态");
    assert.equal(logged.length, 0, "换号后不许埋 watch_checked（那是旧身份的检查）");
});

test("an in-scope watch check still writes its deltas (regression)", async () => {
    const deltas = new Map();
    const logged = [];
    const ctx = {
        _deltas: deltas, currentAccountScope: () => "acctA",
        API: { watchCheck: "/api/watch/check" },
        fetch: async () => ({ ok: true, json: async () => ({ ok: true, result: {
            uids: ["u1"], fingerprints: {}, result_total: 1, truncated: false,
            checked_at: "2026-10-04T00:00:00Z" } }) }),
        artifactsUpdateProject: async () => ({}), artifactsSetBaseline: (p) => p,
        artifactsTouchCheckedAt: (p) => p, watchDiff: () => ({ kind: "diff" }),
        watchDeltaEntries: () => [{ uid: "u1", kind: "added" }], watchChangedFlag: () => true,
        usageLog: (kind) => logged.push(kind), USAGE_KINDS: { watch_checked: "watch_checked" },
    };
    const api = functions("core/project_updates.js",
        ["_scope", "_sessionKey", "_scopeStillCurrent", "_deltasOf", "_deltasSet", "_watchCheckOnce"], ctx);
    const out = await api._watchCheckOnce(_P1());
    assert.ok(out.diff, "未换号时照常返回 diff");
    assert.equal(api._deltasOf("p1").length, 1, "未换号时待查看更新照常入库");
    assert.equal(logged.length, 1, "未换号时照常埋点");
});

test("a late detail mount cannot paint into another project's mount", async () => {
    const deltas = new Map();
    const currentMount = { dataset: { p4ProjectId: "p2" } };   // 屏幕上现在是 B 的 p2 详情
    const api = functions("core/project_updates.js",
        ["_scope", "_sessionKey", "_scopeStillCurrent", "_deltasOf", "_deltasSet", "_detailMountEl"],
        { _deltas: deltas, currentAccountScope: () => "acctA",
          $: () => ({ querySelector: () => currentMount }) });
    assert.equal(api._detailMountEl({ project_id: "p2" }), currentMount, "同属主的回包仍可渲染");
    assert.equal(api._detailMountEl({ project_id: "p1" }), null, "别的追踪的旧回包拿不到挂点（不画进别人的详情）");
});

/* ============================================================================
 * 复盘窗口的输入框隔离、快照 pill 载荷刷新、seen 版本绑定
 * ----------------------------------------------------------------------------
 * 同样取 panel/replay.js 的真源码函数 + 注入 IO：这些性质只有真跑函数才测得出
 * （web_smoke 只查字符串）。
 * ========================================================================== */

function replayModuleCtx(overrides) {
    const ctx = Object.assign({
        $: () => null, currentAccountScope: () => "acctA", toast: noop,
        _setBadge: noop, invalidateRequest: noop, replayConvAuth: () => ({ conv_id: "c1", key: "k1" }),
        _pointsToken: { isCurrent: () => true }, _viewRequests: {}, _badgeRequests: {}, _stopView: noop,
        _bound: false, _active: false,
        RP: { seen: "/api/replay/seen", snapshot: "/api/replay/snapshot" },
    }, overrides || {});
    ctx._displayedVersion = () => Number(ctx._points && ctx._points.digest_version);
    return ctx;
}

test("replayReset clears the composer so no draft or pill survives a new conversation", () => {
    const box = { innerHTML: "A 的草稿<pill>", querySelectorAll: () => [] };
    let synced = 0; const chatRenders = [];
    const ctx = replayModuleCtx({
        $: (id) => (id === "replayComposer" ? box : null),
        _syncSend: () => { synced++; },
        _renderChat: (msgs) => chatRenders.push(msgs), _graph: null, _points: { digest_version: 3 }, _fresh: new Set(["p1"]),
        _freshVer: 3, _chatStamp: 2, _badgeTimer: null, _badgeTries: 4, _pointsRequests: {},
    });
    const api = functions("panel/replay.js", ["_clearComposer", "replayReset"], ctx);
    api.replayReset();
    assert.equal(box.innerHTML, "", "换号/新对话后上一段的草稿与 pill 不许留在输入框");
    assert.ok(synced >= 1, "清空后必须重算发送键可用态");
    assert.equal(ctx._points, null);
    assert.equal(ctx._fresh.size, 0);
    assert.equal(chatRenders.length, 1, "换对话后追问流必须回落空态（_renderChat 是消息列表的唯一写点）");
    assert.equal(chatRenders[0].length, 0, "回落的是空态提示，不残留上一段的消息");
    assert.equal(ctx._chatStamp, -1, "stamp 复位，服务端数据落地后照常重画");
});

test("a late snapshot reply cannot drop an account's pill into the next account", async () => {
    const calls = [];
    let scope = "acctA";
    const ctx = replayModuleCtx({
        currentAccountScope: () => scope,
        $: () => null,
        _post: async () => { scope = "acctB"; return { ok: true, count: 2, text: "A 的标注", snap_id: "s1" }; },
        _insertPill: (...args) => calls.push(["pill", ...args]),
        toast: (t) => calls.push(["toast", t]),
        loadPointsQuiet: async () => calls.push(["reload"]),
    });
    const api = functions("panel/replay.js",
        ["_replayIdentity", "_identityCurrent", "_addToChat"], ctx);
    await api._addToChat();
    assert.deepEqual(calls, [], "换号后的快照回包必须整段丢弃（不插 pill、不 toast、不刷新）");
});

test("an in-scope snapshot reply still drops its pill (regression)", async () => {
    const calls = [];
    const ctx = replayModuleCtx({
        $: () => null,
        _post: async () => ({ ok: true, count: 2, text: "本轮标注", snap_id: "s1" }),
        _insertPill: (...args) => calls.push(["pill", ...args]),
        toast: () => {}, loadPointsQuiet: async () => calls.push(["reload"]),
    });
    const api = functions("panel/replay.js",
        ["_replayIdentity", "_identityCurrent", "_addToChat"], ctx);
    await api._addToChat();
    assert.equal(calls[0][0], "pill");
    assert.equal(calls[0][1], "复盘标注 2 条");
    assert.equal(calls[0][2], "本轮标注");
    assert.ok(calls.some((c) => c[0] === "reload"), "打包成功后照常刷新角标/线程");
});

test("snapshot pills refresh their payload from the latest snapshot text", () => {
    const pill = (snapId, payload) => ({ dataset: { snapId: snapId, payload: payload } });
    const matching = pill("s1", "旧文本：只有 a");
    const other = pill("s2", "另一张快照");
    const box = { querySelectorAll: (sel) => (sel.indexOf("data-snap-id") >= 0 ? [matching, other] : []) };
    const ctx = replayModuleCtx({ $: () => box });
    const api = functions("panel/replay.js", ["_refreshSnapshotPills"], ctx);
    api._refreshSnapshotPills([
        { snap_id: "s1", text: "新文本：a 与后来追加的 b" },
        { snap_id: "s2" },                       // 没有 text（旧数据）：保留原值
    ]);
    assert.equal(matching.dataset.payload, "新文本：a 与后来追加的 b",
        "打包后追加的标注必须进 pill 载荷，否则发送时静默漏掉刚记录的那条");
    assert.equal(other.dataset.payload, "另一张快照", "没有 text 的旧快照不许被空串抹掉");
    api._refreshSnapshotPills(null);
    assert.equal(matching.dataset.payload, "新文本：a 与后来追加的 b");
});

test("seen acknowledges the rendered digest version and retries once when stale", async () => {
    const posts = [];
    let points = { digest_version: 4 };
    let reloads = 0;
    const ctx = replayModuleCtx({
        _points: points,
        _post: async (url, body) => { posts.push(body); return posts.length === 1 ? { ok: true, stale: true } : { ok: true }; },
        loadPointsQuiet: async () => { reloads++; points = { digest_version: 5 }; Object.assign(ctx, { _points: points }); return true; },
    });
    const api = functions("panel/replay.js", ["_markSeen"], ctx);
    await api._markSeen();
    assert.equal(posts.length, 2, "stale=true 必须刷新后按新版本重发一次");
    assert.equal(posts[0].observed_version, 4, "回执必须带用户实际渲染过的版本");
    assert.equal(posts[1].observed_version, 5, "重发必须用刷新后的新版本（后台新成果不许被吞）");
    assert.ok(reloads >= 1);
    // 未 stale：只发一次
    const posts2 = [];
    const ctx2 = replayModuleCtx({
        _points: { digest_version: 9 },
        _post: async (url, body) => { posts2.push(body); return { ok: true }; },
        loadPointsQuiet: async () => { throw new Error("unexpected reload"); },
    });
    const api2 = functions("panel/replay.js", ["_markSeen"], ctx2);
    await api2._markSeen();
    assert.equal(posts2.length, 1);
    assert.equal(posts2[0].observed_version, 9);
});

test("every trapModalFocus call site passes the event first", () => {
    /* 契约：trapModalFocus(event, modal)。workspaces.js 曾把参数写反（modal, event）——
       结果所有 Tab 事件都被当成「不是 Tab」直接返回 false，项目弹窗焦点可逃逸。
       这里机械扫描全部调用点（行为契约由下一个测试钉），防止再写反。 */
    const files = ["panel/workspaces.js", "panel/memory.js", "core/feedback.js"];
    for (const file of files) {
        const text = source(file);
        const calls = [...text.matchAll(/trapModalFocus\(([^)]*)\)/g)].map((m) => m[1].trim());
        assert.ok(calls.length, "没有找到调用点（门失效？）：" + file);
        for (const args of calls) {
            assert.ok(/^(e|ev|event)\b/.test(args), `${file}: trapModalFocus 第一个实参必须是事件对象，实际「${args}」`);
        }
    }
});

test("trapModalFocus wraps focus at both ends of a modal (contract)", () => {
    const nodes = [{ getClientRects: () => [{}], hidden: false }, { getClientRects: () => [{}], hidden: false }];
    const doc = { activeElement: nodes[1], body: { contains: () => true } };
    nodes[0].focus = () => { doc.activeElement = nodes[0]; };
    nodes[1].focus = () => { doc.activeElement = nodes[1]; };
    const modal = {
        hidden: false,
        querySelectorAll: () => nodes,
        contains: (el) => nodes.indexOf(el) >= 0,
    };
    const ctx = { document: doc, _FOCUSABLE: "button, [tabindex]" };
    const api = functions("core/copy.js", ["trapModalFocus"], ctx);
    const first = { key: "Tab", shiftKey: false, preventDefault() { this.prevented = true; } };
    assert.equal(api.trapModalFocus(first, modal), true, "末元素 Tab 必须回绕到首个");
    assert.equal(doc.activeElement, nodes[0]);
    assert.equal(first.prevented, true);
    const back = { key: "Tab", shiftKey: true, preventDefault() { this.prevented = true; } };
    assert.equal(api.trapModalFocus(back, modal), true, "首元素 Shift+Tab 必须回绕到末个");
    assert.equal(back.prevented, true);
    const none = { key: "Escape", preventDefault() {} };
    assert.equal(api.trapModalFocus(none, modal), false);
    assert.equal(api.trapModalFocus(first, { hidden: true }), false);
});

test("loadPoints refreshes snapshot pill payloads on every fetch (wiring)", async () => {
    /* 载荷刷新挂在取数唯一通道上：漏挂 → pill 永远停留在打包那一刻的文本。 */
    const seen = [];
    const ctx = { ...requestTools(), _pointsRequests: {}, replayConvAuth: () => ({ conv_id: "c1", key: "k1" }),
        _q: () => "c1", RP: { points: "/points" }, _get: async () => ({ ok: true, snapshots: [{ snap_id: "s1", text: "x" }] }),
        _points: null, _graph: null, _setNote: noop, _captureFresh: () => true, _stamp: () => "1",
        _renderEmpty: noop, _renderChat: noop, _applyNote: noop, _refreshSnapshotPills: (s) => seen.push(s) };
    const api = functions("panel/replay.js", ["loadPoints"], ctx);
    await api.loadPoints();
    assert.equal(seen.length, 1, "每次取数都要刷一次快照 pill 载荷");
    assert.equal(seen[0][0].snap_id, "s1");
});

/* 布局模块是纯函数零 DOM：整文件在 vm 里求值即可拿到真 computeLayout（含真常量与内部
   辅助函数），不手抄任何常量。用于把后端真实快照载荷送进真渲染引擎。 */
function layoutFunctions() {
    const src = source("core/replay_layout.js").replace(/^export /gm, "");
    return runInNewContext(src + "\n({ computeLayout, displayTitle, PAD, GUTTER })", {});
}

/* 图谱模块整文件求值（只剥掉唯一的 #replay_layout import，由 test 注入真实现）：
   initReplayGraph 的真 openSnapshot/标签页状态机只有这样才测得到（字符串门测不出迟回包）。 */
function replayGraphModule(globals) {
    const src = source("panel/replay_graph.js")
        .replace(/^import .*$/gm, "")
        .replace(/^export /gm, "");
    return runInNewContext(src + "\n({ initReplayGraph, snapshotDigest, activeGraphData })", globals);
}

/* 最小 DOM 保留节点引用、附着关系与事件回调；各场景注入空态或真实布局核。 */
function replayGraphHarness() {
    const nodes = [];
    function node(tag) {
        const kids = [];
        const el = {
            tagName: String(tag || "").toUpperCase(), className: "", textContent: "", hidden: false,
            children: kids, dataset: {}, title: "", disabled: false, value: "", _html: "", listeners: {}, attrs: {},
            classList: {
                add(cls) { if (!this.contains(cls)) el.className = (el.className + " " + cls).trim(); },
                remove(cls) { el.className = el.className.split(/\s+/).filter((c) => c !== cls).join(" "); },
                contains(cls) { return el.className.split(/\s+/).includes(cls); },
                toggle(cls) { const add = !this.contains(cls); if (add) this.add(cls); else this.remove(cls); return add; },
            },
            setAttribute(k, v) { el.attrs[k] = String(v); if (k === "class") el.className = String(v); },
            getAttribute: (k) => el.attrs[k] ?? null,
            childNodes: kids,
            contains(target) { return el === target || kids.some((child) => child.contains && child.contains(target)); },
            append(...items) { kids.push(...items); },
            addEventListener(k, fn) { el.listeners[k] = fn; }, removeEventListener() {}, remove() {},
            querySelector: () => null, querySelectorAll: () => [],
            getBoundingClientRect: () => ({ width: 0, height: 0, left: 0, top: 0 }),
            set innerHTML(v) { el._html = String(v); if (v === "") kids.length = 0; },
            get innerHTML() { return el._html; },
        };
        nodes.push(el);
        return el;
    }
    const document = {
        createElement: (tag) => node(tag),
        createElementNS: (ns, tag) => node(tag),
    };
    const host = node("div");
    const byClass = (cls) => nodes.filter((n) => String(n.className).split(/\s+/).includes(cls));
    const tabLabels = () => {
        const bar = byClass("g-tabs")[0];
        return bar ? bar.children.map((c) => String(c.textContent)) : [];
    };
    return { document, host, byClass, tabLabels, panes: () => byClass("graph-panel") };
}

test("a real backend snapshot payload renders in the real layout engine", () => {
    /* 由 tests/test_replay_api.py 驱动：只标了一条关系，载荷是真实 /api/replay/snapshot 的响应。
       载荷手造会让这条门失真（两端注解是真实闭包还是测试自己塞的，肉眼分不出），故要求
       REPLAY_SNAPSHOT_PAYLOAD；未设置时只提示不失败（本文件其余断言照跑）。 */
    const payloadPath = process.env.REPLAY_SNAPSHOT_PAYLOAD;
    if (!payloadPath) {
        console.log("  (REPLAY_SNAPSHOT_PAYLOAD 未设置：跳过真实载荷→布局桥，由 tests/test_replay_api.py 驱动)");
        return;
    }
    const snap = JSON.parse(readFileSync(payloadPath, "utf8"));
    const api = functions("panel/replay_graph.js", ["snapshotDigest", "activeGraphData"], {});
    const captured = api.snapshotDigest(snap);
    assert.ok(captured, "只标关系的真实载荷必须构成自带图（漏闭包时 snapshotDigest 返回 null → 回退当前图）");
    const layout = layoutFunctions().computeLayout({ digest: captured });
    assert.ok(layout, "真 computeLayout 必须画得出来（无节点时返回 null → 空态）");
    assert.equal(snap.count, 1, "标注计数只数用户写的标注");
    assert.equal(snap.items.filter((it) => it.notes && it.notes.length).length, 1,
        "两端只是画布闭包：notes 空，不许冒充标注");
    assert.equal(layout.nodes.length, 2, "被标关系的两端必须都能渲染");
    assert.equal(layout.edges.length, 1, "被标关系本身必须在快照图里");
    const edge = layout.edges[0];
    assert.ok(layout.byId[edge.from] && layout.byId[edge.to], "关系的两端都在布局里（否则边被静默丢掉）");
    assert.ok(edge.route, "边必须带 route（渲染/测量唯一依据）");
    console.log(`  REPLAY_SNAPSHOT_LAYOUT nodes=${layout.nodes.length} edges=${layout.edges.length}`
        + ` count=${snap.count} items=${snap.items.length}`);
});

/* openSnapshot 的 GET /api/replay/snapshots 是慢请求。迟回包若没有身份/代序闸，
   用户 A 找回途中切 B 会话或点新对话（replayReset → reload(null)）后，旧回包会把 A 的
   快照标签页重新激活（activateTab 直接改 activeTab + 画 DOM）——用户看到的是上一身份的图与标注。
   闸必须落在回包写任何 state/tab/DOM **之前**，且 A→B→A 也不能漏（比「当前身份相等」更严）。 */
test("a late snapshot lookup cannot resurrect a tab after reset or identity switch", async () => {
    const L = layoutFunctions();
    const mkHost = (state) => {
        const dom = replayGraphHarness();
        const mod = replayGraphModule({
            document: dom.document, computeLayout: () => null, PAD: L.PAD, GUTTER: L.GUTTER,
        });
        const lookups = [];
        return {
            dom, mod, lookups, state,
            handle: mod.initReplayGraph(dom.host, {
                convId: () => state.conv, key: () => state.key, freshPids: () => [],
                api: {
                    getJSON: (url) => { const d = deferred(); lookups.push(d); return d.promise; },
                    postJSON: async () => ({}),
                },
                mdLite: (s) => String(s == null ? "" : s), escapeHtml: String, onChanged: () => {},
            }),
        };
    };
    const snap = { snap_id: "s1", created_at: 1, count: 1, items: [], edges: [], text: "A 的标注" };

    // ① 找回途中 replayReset（新对话/换号）→ 迟回包整段丢弃
    const a = mkHost({ conv: "cA", key: "kA" });
    const pendingA = a.handle.openSnapshot("s1");
    a.handle.reload(null);                       // replay.js replayReset 的原样调用
    a.lookups[0].resolve({ snapshots: [snap] });
    await pendingA;
    assert.deepEqual(a.dom.tabLabels(), ["当前"], "迟回包不许复活旧快照标签页");
    assert.equal(a.dom.panes().every((p) => p.hidden !== false), true, "也不许画进详情面板");

    // ② A→B→A 往返：身份串回到原值也必须作废（epochs 记「换过几次」）
    const stateB = { conv: "cA", key: "kA" };
    const b = mkHost(stateB);
    const pendingB = b.handle.openSnapshot("s1");
    stateB.conv = "cB"; stateB.key = "kB";                            // 找回途中换到 B
    b.handle.reload({ ok: true, digest: null, snapshots: [] });       // B 会话的 points
    assert.deepEqual(b.dom.tabLabels(), ["当前"]);
    stateB.conv = "cA"; stateB.key = "kA";                            // 又切回 A
    b.handle.reload({ ok: true, digest: null, snapshots: [] });
    b.lookups[0].resolve({ snapshots: [snap] });
    await pendingB;
    assert.deepEqual(b.dom.tabLabels(), ["当前"], "A→B→A 后旧回包同样不许复活");

    // ③ 已缓存的标签页不得跨身份存活：同 id 的快照在 B 会话里必须重新按 B 的身份取
    const c = mkHost({ conv: "cA", key: "kA" });
    const first = c.handle.openSnapshot("s1");
    c.lookups[0].resolve({ snapshots: [snap] });
    await first;
    assert.equal(c.dom.tabLabels().length, 2, "同身份下正常找回：当前 + 快照标签");
    c.handle.reload({ ok: true, digest: null, snapshots: [snap] });
    assert.equal(c.dom.tabLabels().length, 2, "同身份的刷新不许关掉用户打开的快照");
    const stateC = c.state;
    stateC.key = "kB";                                                // 换号：key 变（conv 不变）
    c.handle.reload({ ok: true, digest: null, snapshots: [snap] });
    assert.deepEqual(c.dom.tabLabels(), ["当前"], "换身份后已缓存标签页整批作废，不许串号");
});

test("the pill in the composer opens a snapshot through the real graph entry (wiring)", () => {
    const opened = [];
    const pill = { dataset: { snapId: "s7" } };
    const ctx = {
        $: () => null, _graph: { openSnapshot: (id) => opened.push(id) }, _toggleGraph: () => {},
    };
    const api = functions("panel/replay.js", ["_onComposerClick"], ctx);
    api._onComposerClick({ target: { closest: (sel) => (sel === ".pill" ? pill : null) } });
    assert.deepEqual(opened, ["s7"], "点 pill 必须把 snapId 交给图谱的真实 openSnapshot");
    pill.dataset.snapId = "";
    api._onComposerClick({ target: { closest: (sel) => (sel === ".pill" ? pill : null) } });
    assert.deepEqual(opened, ["s7"], "没有 snapId 的 pill（纯文本载荷）不开快照");
});

test("snapshot tabs render their own captured graph and fall back for old snapshots", () => {
    /* 快照自带 items[].point/kind + edges——重整理后旧标注对象已不在当前 digest 里，
       快照页必须用自己的捕获数据渲染（同一个 computeLayout）；老快照回退现状，不白屏不抛错。 */
    const api = functions("panel/replay_graph.js", ["snapshotDigest", "activeGraphData"], {});
    const snap = {
        snap_id: "s1",
        items: [
            { pid: "start", kind: "motivation", notes: ["a"], point: { pid: "start", title: "动机", detail: "d", turn_refs: [1] } },
            { pid: "k1", kind: "knowledge", notes: ["b"], point: { pid: "k1", title: "知识点", detail: "", turn_refs: [2] } },
            { pid: "e1", kind: "edge", notes: ["c"], point: null },
        ],
        edges: [{ eid: "e1", from: "start", to: "k1", label: "支撑", turn_refs: [2] }],
    };
    const dg = api.snapshotDigest(snap);
    assert.ok(dg && dg.motivation && dg.motivation.title === "动机", "捕获的动机节点必须在");
    assert.equal(dg.knowledge.map((p) => p.pid).join(","), "k1");   // vm 跨 realm：比内容不比原型
    assert.equal(dg.edges.map((e) => e.eid).join(","), "e1");
    const source = api.activeGraphData("s1", { digest: { knowledge: [{ pid: "x" }] } }, snap);
    assert.equal(source.digest.knowledge.map((p) => p.pid).join(","), "k1", "快照页用自己的捕获图，不是当前图");
    assert.equal(source.threads && Object.keys(source.threads).length, 0);

    // 旧快照（无 point/edges）：回退到现状渲染（当前 digest），不抛错
    const legacy = { snap_id: "s0", items: [{ pid: "k1", head: "知识点", kind: "knowledge", notes: ["b"] }] };
    assert.equal(api.snapshotDigest(legacy), null);
    const current = { digest: { knowledge: [{ pid: "now" }] } };
    assert.equal(api.activeGraphData("s0", current, legacy), current, "老快照必须回退到现状数据源");
    assert.equal(api.activeGraphData("current", current, snap), current, "当前页恒吃宿主注入的数据");
    // 只记了关系（point 为空）也不能造出空图
    assert.equal(api.snapshotDigest({ items: [{ pid: "e1", kind: "edge", point: null }] }), null);
});


/* ============================================================================
 * provider 接线、MCP 令牌属主、护栏密钥政策、试用额度代序、
 * 执行身份、文件对话框取消、语料代
 * ========================================================================== */

test("endpointCfg carries the injected provider wire, never the DOM preset key", () => {
    const inputs = { cfgBaseUrl: { value: "" }, cfgModel: { value: "deepseek-v4-flash" },
        cfgProvider: { value: "trial" } };
    const ctx = { $: (id) => inputs[id] || null, learningTargetScope: () => "proj-1",
        currentAccountScope: () => "u1", _providerWire: null };
    const api = functions("panel/examples.js",
        ["setEndpointProviderWire", "endpointCfg", "_endpointQuery", "_scopeKey"], ctx);

    assert.equal(api.endpointCfg().provider, "", "未注入时 provider 空（服务端默认语义不变）");
    api.setEndpointProviderWire(() => "trial");
    assert.equal(api.endpointCfg().provider, "trial", "wire 值原样（trial/mock 不映射）");
    assert.ok(api._endpointQuery().indexOf("provider=trial") >= 0, "GET query 必须带 provider");
    const q1 = api._endpointQuery();
    api.setEndpointProviderWire(() => "openai-compatible");
    assert.notEqual(api._endpointQuery(), q1, "换 provider（同为兼容地址）必须换端点身份");
    assert.equal(api.endpointCfg().provider, "openai-compatible");
    assert.notEqual(api._scopeKey(), "", "分区键含账户/目标/端点");

    const shellApi = functions("core/shell.js", ["llmProviderWire"], { getConfig: () => ({ provider: "zhipuai" }) });
    assert.equal(shellApi.llmProviderWire(), "zhipuai", "llmProviderWire 必须委托 getConfig（唯一真源）");
});

test("rules' endpoint query and scope key include the provider", () => {
    const cfg = { base_url: "", model: "m", project_id: "p", provider: "trial" };
    const ctx = { endpointCfg: () => cfg, currentAccountScope: () => "u1" };
    const api = functions("panel/rules.js", ["_endpointQuery", "scopeKey"], ctx);
    assert.ok(api._endpointQuery().indexOf("provider=trial") >= 0);
    const kTrial = api.scopeKey();
    cfg.provider = "openai-compatible";
    assert.notEqual(api.scopeKey(), kTrial, "provider 变化必须换分区键（不显示上个端点的规则）");
});

test("a minted MCP token stays with its owner account", async () => {
    let scope = "acctA";
    const out = { hidden: true };
    const cfg = { textContent: "" };
    const ctx = { currentAccountScope: () => scope, _lastMcpConfig: null, _mcpTokenOwner: null,
        API: { mcpTokenMint: "/api/mcp/token/mint" },
        $: (id) => (id === "mcpTokenOut" ? out : (id === "mcpTokenConfig" ? cfg : null)),
        fetch: async () => ({ ok: true, json: async () => ({ ok: true, config: {
            url: "https://mcp.example/rpc", headers: { Authorization: "Bearer sk-AAA" } } }) }),
        toast: noop, loadMcpTokenList: noop };
    const api = functions("core/interactions.js",
        ["_mcpTokenSameOwner", "_mcpTokenClear", "_mcpTokenGuardForCopy", "mintOnlineMcpToken", "copyOnlineMcpPrompt"], ctx);
    await api.mintOnlineMcpToken({ disabled: false });
    assert.equal(ctx._mcpTokenOwner, "acctA");
    assert.equal(out.hidden, false, "生成后显示接入配置");
    scope = "acctB";
    assert.equal(api._mcpTokenGuardForCopy(), false, "换号后复制必须被拒");
    assert.equal(ctx._lastMcpConfig, null, "A 的令牌状态必须清空");
    assert.equal(out.hidden, true, "输出区必须收起（B 看不到 A 的凭证）");
    assert.equal(cfg.textContent, "", "配置 JSON 必须清空");
});

test("a late MCP mint reply cannot re-show the previous account's credential", async () => {
    let scope = "acctA";
    const out = { hidden: true };
    const ctx = { currentAccountScope: () => scope, _lastMcpConfig: null, _mcpTokenOwner: null,
        API: { mcpTokenMint: "/api/mcp/token/mint" },
        $: (id) => (id === "mcpTokenOut" ? out : (id === "mcpTokenConfig" ? { textContent: "" } : null)),
        fetch: async () => { scope = "acctB"; return { ok: true, json: async () => ({ ok: true,
            config: { url: "https://mcp.example/rpc", headers: { Authorization: "Bearer sk-AAA" } } }) }; },
        toast: noop, loadMcpTokenList: noop };
    const api = functions("core/interactions.js",
        ["_mcpTokenSameOwner", "_mcpTokenClear", "_mcpTokenGuardForCopy", "mintOnlineMcpToken"], ctx);
    await api.mintOnlineMcpToken({ disabled: false });
    assert.equal(ctx._lastMcpConfig, null, "迟回包不得落进新账户的状态");
    assert.equal(out.hidden, true, "迟回包不得重展旧凭证");
});

test("the remember-key secret is only restored once the guard policy is known", () => {
    const keyInput = { value: "" };
    const rememberBox = { checked: false };
    const els = { cfgApiKey: keyInput, cfgRememberApiKey: rememberBox };
    let known = false, guard = false;
    const LS_KEY = "cfg-key";
    const store = new Map([[LS_KEY, JSON.stringify({ save: true, rememberApiKey: true, apiKey: "sk-stored" })]]);
    const ctx = { _policySecretApplied: false, _healthSnapshot: null, LS: { cfg: LS_KEY },
        webGuardKnown: () => known, webGuardOn: () => guard,
        $: (id) => els[id] || null,
        readJSON: (k, d) => (store.has(k) ? JSON.parse(store.get(k)) : d),
        writeJSON: (k, v) => { store.set(k, JSON.stringify(v)); } };
    const api = functions("core/shell.js", ["_applyPolicyDependentSecret"], ctx);

    api._applyPolicyDependentSecret();
    assert.equal(keyInput.value, "", "未知期不许把 key 恢复进输入框");
    assert.ok(store.get(LS_KEY).indexOf("sk-stored") >= 0, "未知期不许删存档");

    known = true; guard = true;
    api._applyPolicyDependentSecret();
    assert.equal(keyInput.value, "");
    assert.equal(rememberBox.checked, false);
    assert.ok(store.get(LS_KEY).indexOf("sk-stored") < 0, "护栏模式必须剔除明文 key");

    const store2 = new Map([[LS_KEY, JSON.stringify({ save: true, rememberApiKey: true, apiKey: "sk-local" })]]);
    const key2 = { value: "" }, box2 = { checked: false };
    const ctx2 = { _policySecretApplied: false, _healthSnapshot: { ok: true }, LS: { cfg: LS_KEY },
        webGuardKnown: () => true, webGuardOn: () => false,
        $: (id) => (id === "cfgApiKey" ? key2 : (id === "cfgRememberApiKey" ? box2 : null)),
        readJSON: (k, d) => (store2.has(k) ? JSON.parse(store2.get(k)) : d),
        writeJSON: (k, v) => { store2.set(k, JSON.stringify(v)); } };
    const api2 = functions("core/shell.js", ["_applyPolicyDependentSecret"], ctx2);
    api2._applyPolicyDependentSecret();
    assert.equal(key2.value, "sk-local", "非护栏按存档恢复");
    assert.equal(box2.checked, true);
});

test("switching away from trial invalidates the in-flight quota reply", async () => {
    const box = { hidden: false };
    let wire = "trial";
    const pending = deferred();
    const ctx = { _trialQuotaSeq: 0, _healthLlm: { trial: { available: true } },
        _llmWire: () => wire, $: (id) => (id === "trialQuotaInfo" ? box : null),
        API: { accountTrialQuota: "/api/account/trial-quota" },
        fetch: () => pending.promise };
    const api = functions("core/shell.js", ["refreshTrialQuota"], ctx);
    const inflight = api.refreshTrialQuota();
    wire = "mock";
    await api.refreshTrialQuota();
    box.hidden = true;
    pending.resolve({ ok: true, json: async () => ({ available: true, remaining: 5, daily_limit: 10 }) });
    await inflight;
    assert.equal(box.hidden, true, "迟到的额度回包不许把试用卡重新点亮");
});

test("a plan chain stops dispatching once the identity changed", async () => {
    let scope = "acctA", conv = "c1", dispatched = [];
    const ctx = { _actLaunchId: null, _actBusy: false,
        currentAccountScope: () => scope, cbConvId: () => conv,
        actDispatchPlan: async (p) => { dispatched.push(p.verb); scope = "acctB"; return true; } };
    const api = functions("act/act.js",
        ["_actIdentityNow", "_actIdentityChangedSince", "_actStale", "actDispatchPlanList"], ctx);
    const mark = await api.actDispatchPlanList([
        { kind: "exec", verb: "curate.search_online" },
        { kind: "exec", verb: "curate.remove" },
    ], "说的");
    assert.deepEqual(dispatched, ["curate.search_online"], "换身份后不得再派发后续写步");
    assert.equal(mark, true);

    // 身份未变时逐项派发（回归：闸不会误伤正常多意图链）
    const ctx2 = { _actLaunchId: null, _actBusy: false,
        currentAccountScope: () => "acctA", cbConvId: () => "c1",
        actDispatchPlan: async (p) => { dispatched.push(p.verb); return true; } };
    const api2 = functions("act/act.js",
        ["_actIdentityNow", "_actIdentityChangedSince", "_actStale", "actDispatchPlanList"], ctx2);
    await api2.actDispatchPlanList([{ kind: "exec", verb: "a" }, { kind: "exec", verb: "b" }], "");
    assert.deepEqual(dispatched.slice(1), ["a", "b"]);
});

test("cancelling the file dialog settles the picker exactly once", async () => {
    const listeners = {};
    const input = { type: "", accept: "", click: noop,
        addEventListener: (name, fn) => { listeners[name] = fn; } };
    const ctx = { document: { createElement: () => input } };
    const api = functions("act/act.js", ["actPickJsonFile"], ctx);
    const picked = api.actPickJsonFile();
    let settled = 0;
    picked.then(() => { settled += 1; });
    listeners.cancel();
    listeners.cancel();
    assert.equal(await picked, null, "取消必须结算为 null（否则 runner 的 finally 永不到）");
    await Promise.resolve();
    assert.equal(settled, 1, "只结算一次");
});

test("a corpus mutation invalidates the query memo and changes the key", () => {
    const cache = new Map([["k", { ok: true }]]);
    const ctx = { _queryCache: cache, _corpusMemoGen: 0 };
    const api = functions("search/search.js",
        ["queryCacheKey", "invalidateRecommendMemo", "queryCacheGet", "queryCacheSet"], ctx);
    const keyBefore = api.queryCacheKey({ query: "q", sources: ["10x Genomics"] });
    assert.equal(cache.size, 1);
    api.invalidateRecommendMemo();
    assert.equal(cache.size, 0, "mutation 必须清空本会话查询缓存");
    assert.notEqual(api.queryCacheKey({ query: "q", sources: ["10x Genomics"] }), keyBefore,
        "语料代必须进键（在途旧回包按旧代落库，不会被新检索命中）");
});

test("every settings switch is keyboard-reachable and Space toggles it", () => {
    const html = readFileSync(new URL("../../web/static/index.html", import.meta.url), "utf8");
    const switches = html.match(/<(?:label|span) class="switch"[^>]*>/g) || [];
    assert.ok(switches.length >= 10, "找到的设置开关数量异常：" + switches.length);
    for (const tag of switches) {
        assert.ok(/role="switch"/.test(tag) && /tabindex="0"/.test(tag) && /aria-checked="(true|false)"/.test(tag),
            "开关必须可聚焦且带辅助树语义：" + tag);
    }
    // 行为：Space/Enter 经原生 input.click() 切换（沿用既有 change 监听链），aria-checked 同步
    const inputListeners = {};
    const input = { checked: false, disabled: false, clicks: 0,
        addEventListener: (n, fn) => { inputListeners[n] = fn; },
        click() { this.clicks += 1; this.checked = !this.checked; } };
    const listeners = {};
    const label = { attrs: {}, setAttribute(k, v) { label.attrs[k] = v; },
        hasAttribute: (k) => Object.prototype.hasOwnProperty.call(label.attrs, k),
        querySelector: () => input, addEventListener: (n, fn) => { listeners[n] = fn; } };
    const api = functions("core/interactions.js", ["bindSwitchA11y"], { document: { querySelectorAll: () => [label] } });
    api.bindSwitchA11y();
    assert.equal(label.attrs["aria-checked"], "false");
    listeners.keydown({ key: " ", preventDefault: noop });
    assert.equal(input.clicks, 1, "空格必须切换原生 input");
    inputListeners.change();
    assert.equal(label.attrs["aria-checked"], "true", "aria-checked 必须跟随真实勾选态");
});

test("the sync runner waits for the async job instead of claiming no change", async () => {
    const calls = [];
    const startQueue = [{ kind: "async" }];
    const jobQueue = [];
    const ctx = {
        arxStep: (t) => calls.push(["step", t]),
        startSyncUpdates: async (opts) => { calls.push(["start", opts]); return startQueue.shift(); },
        awaitSyncJob: async () => { calls.push(["await"]); return jobQueue.shift(); },
        _noteCorpusWrite: () => calls.push(["memo"]),
        actSyncUpdatesCardHtml: () => "<card>",
    };
    const api = functions("act/act.js", ["actRunCurateSyncUpdates"], ctx);

    // 202 异步：等终态，按 job 的真实计数出回执（旧实现会写「没有改动」）
    jobQueue.push({ kind: "result", result: { imported_total: 3, sources: [{ filename: "synced.json" }], hint_zh: "" } });
    const out = await api.actRunCurateSyncUpdates({ slots: {} });
    assert.equal(out.ok, true);
    assert.equal(out.artifact.imported_total, 3, "必须用 job 终态的真实计数");
    assert.equal(out.artifact.filename, "synced.json");
    assert.ok(out.policy.join("").indexOf("撤回") >= 0, "真入库必须有撤回指路");
    assert.ok(calls.some((c) => c[0] === "await"), "异步响应必须等 job 终态");
    assert.ok(calls.some((c) => c[0] === "memo"), "真入库要作废 recommend memo");

    // 同步形态：直接用响应 result（老后端逐字节不变）
    ctx.startSyncUpdates = async () => ({ kind: "result", result: { imported_total: 0, sources: [], hint_zh: "" } });
    const out2 = await api.actRunCurateSyncUpdates({ slots: {} });
    assert.equal(out2.ok, true);
    assert.equal(out2.artifact.imported_total, 0);
    assert.ok(out2.policy.join("").includes("没有需要入库的新增"));

    // busy / error / 超时：如实报错，不写「没有改动」
    ctx.startSyncUpdates = async () => ({ kind: "busy", detail: "另一个同步任务进行中" });
    const busy = await api.actRunCurateSyncUpdates({ slots: {} });
    assert.equal(busy.ok, false);
    assert.ok(busy.error.indexOf("另一个同步任务") >= 0);
    ctx.startSyncUpdates = async () => ({ kind: "async" });
    ctx.awaitSyncJob = async () => ({ kind: "timeout" });
    const to = await api.actRunCurateSyncUpdates({ slots: {} });
    assert.equal(to.ok, false);
    assert.ok(to.error.indexOf("没有拿到终态计数") >= 0, "超时必须如实说没拿到计数");
});

function browserModule(file, names, globals) {
    const code = source(file).replace(/^import\b[\s\S]*?;\s*/gm, "").replace(/^export /gm, "");
    return runInNewContext(code + "\n({" + names.join(",") + "})", globals);
}
const settleIO = () => new Promise(setImmediate);

function annotationHarness() {
    const dom = replayGraphHarness(), L = layoutFunctions(), replies = [], changed = [], panelEvents = [];
    const state = { conv: "conversation-A", key: "key-A" };
    const snapshot = { snap_id: "snap-A", created_at: 1, count: 1,
        items: [{ pid: "o-0", kind: "outputs", point: { pid: "o-0", title: "Saved output" }, notes: ["saved"] }], edges: [] };
    const mod = replayGraphModule({ document: dom.document, computeLayout: L.computeLayout, PAD: L.PAD, GUTTER: L.GUTTER });
    const handle = mod.initReplayGraph(dom.host, {
        convId: () => state.conv, key: () => state.key, freshPids: () => [],
        mdLite: String, escapeHtml: String, onChanged: () => changed.push(state.conv),
        onPanel: (vis) => panelEvents.push(vis),
        api: { getJSON: async () => ({ snapshots: [snapshot] }), postJSON: (url, body) => {
            const reply = deferred(); replies.push({ url, body, ...reply }); return reply.promise;
        } },
    });
    const data = () => ({ ok: true, digest: { outputs: [{ pid: "o-0", title: state.conv }],
        knowledge: [], decisions: [], edges: [] }, threads: {}, snapshots: [snapshot] });
    const start = async (tab) => {
        const points = data(); handle.reload(points);
        if (tab === "snapshot") await handle.openSnapshot(snapshot.snap_id);
        dom.byClass("g-node").at(-1).listeners.click({ stopPropagation: noop });
        const send = dom.byClass("record").at(-1), thread = dom.byClass("thread").at(-1);
        const textarea = dom.byClass("point-input").at(-1).children[0]; textarea.value = "new annotation";
        const pending = send.listeners.click();
        assert.equal(send.disabled, true);
        assert.equal(replies[0].body.conv_id, "conversation-A");
        return { points, send, thread, textarea, pending, count: thread.children.length };
    };
    return { dom, handle, state, replies, changed, data, snapshot, start, panelEvents };
}

for (const tab of ["current", "snapshot"]) {
    for (const boundary of ["reset", "switch", "roundtrip"]) {
        for (const result of ["success", "error"]) {
            test(`late ${tab} annotation ${result} cannot cross ${boundary}`, async () => {
                const h = annotationHarness(), request = await h.start(tab), before = JSON.stringify(request.points);
                if (boundary === "reset") h.handle.reload(null);
                else {
                    h.state.conv = "conversation-B"; h.state.key = "key-B";
                    h.handle.reload(h.data());
                    if (boundary === "roundtrip") {
                        h.state.conv = "conversation-A"; h.state.key = "key-A"; h.handle.reload(h.data());
                    }
                }
                const current = h.data(); h.handle.reload(current);
                if (result === "error") h.replies[0].reject(new Error("old request failure"));
                else h.replies[0].resolve({ ok: true, snapshot: { ...h.snapshot, text: "old private annotation" } });
                await request.pending;
                assert.deepEqual(h.dom.tabLabels(), ["当前"], "old POST cannot resurrect snapshot tabs");
                assert.equal(JSON.stringify(request.points), before, "old data is not mutated after reset");
                assert.deepEqual(current.threads, {}, "the next conversation cannot receive the old note");
                assert.equal(h.changed.length, 0, "old completion cannot refresh the next conversation");
                assert.equal(request.thread.children.length, request.count, "stale errors cannot append to detached UI");
                assert.equal(request.send.disabled, true, "stale finally cannot update a discarded control");
            });
        }
    }
    test(`in-scope ${tab} annotation still updates its graph`, async () => {
        const h = annotationHarness(), request = await h.start(tab);
        h.replies[0].resolve({ ok: true, snapshot: { ...h.snapshot, text: "saved new annotation" } });
        await request.pending;
        assert.equal(h.changed.length, 1);
        assert.equal(request.textarea.value, "");
        assert.equal(request.thread.children.length, request.count + 1);
        // 提交即缩回：面板合拢（宿主据此把分割线收回基准比例），被拆下的控件不再被 finally 回写
        assert.deepEqual(h.panelEvents, [true, false], "panel opens on select and collapses after a saved annotation");
        assert.equal(h.dom.byClass("graph-panel").at(-1).hidden, true);
        assert.equal(request.send.disabled, true, "collapsed panel controls are detached and keep their final state");
        if (tab === "current") assert.equal(request.points.threads["o-0"][0].content, "new annotation");
        else assert.equal(h.dom.tabLabels().length, 2);
    });
}

async function autoWatchHarness() {
    const watchCore = await import("../../web/static/js/core/project_updates_core.js");
    const transforms = await import("../../web/static/js/core/artifacts.js");
    const tools = requestTools(); tools.setCurrentUser({ id: "account-A" });
    const keyCode = source("core/core.js").match(/^export function nsKeyFor\(.*$/m)[0].replace(/^export /, "");
    const keyTools = runInNewContext(keyCode + "\n({ nsKeyFor })", {});
    const lists = new Map(), stored = new Map(), gen = new Map(), calls = [], writes = [], notices = [], logged = [];
    const project = (query, id = "shared") => ({ project_id: id, check_condition: {
        spec: { query, spec_version: "v1" }, baseline: { uids: [], fingerprints: {}, result_total: 0, truncated: false } } });
    stored.set("account-A", new Map([["shared", project("query-A")]]));
    stored.set("account-B", new Map([["shared", project("query-B")]]));
    const hooks = {}, pendingWrites = [], failures = { http: 0, write: 0, list: 0 };
    const ctx = { ...watchCore, ...tools, ...keyTools, document: { addEventListener: (_event, fn) => { hooks.ready = fn; } },
        API: { watchCheck: "/api/watch/check" }, webGuardOn: () => true,
        healthSnapshot: () => ({ corpus: { gen: "generation-1" } }),
        setHealthArrivedHook: (fn) => { hooks.health = fn; }, setAccountChangedHook: (fn) => { hooks.account = fn; },
        nsKey: (key) => keyTools.nsKeyFor(key, tools.currentAccountScope()),
        readJSON: (key, fallback) => gen.get(key) ?? fallback, writeJSON: (key, value) => gen.set(key, value),
        artifactsListProjects: async (scope) => {
            if (failures.list-- > 0) throw new Error("synthetic list failure");
            if (lists.has(scope)) return lists.get(scope).promise;
            return [...stored.get(scope).values()].map((p) => structuredClone(p));
        },
        artifactsUpdateProject: async (scope, id, mutator) => {
            if (pendingWrites.length) await pendingWrites.shift().promise;
            if (failures.write-- > 0) throw new Error("synthetic transaction abort");
            const p = mutator(structuredClone(stored.get(scope).get(id)));
            stored.get(scope).set(id, p); writes.push({ scope, id }); return p;
        },
        artifactsSetBaseline: transforms.artifactsSetBaseline, artifactsTouchCheckedAt: transforms.artifactsTouchCheckedAt,
        fetch: async (_url, opts) => {
            const spec = JSON.parse(opts.body), scope = tools.currentAccountScope(); calls.push({ scope, spec });
            if (failures.http-- > 0) return { ok: false, status: 503, json: async () => ({ detail: "synthetic watch failure" }) };
            return { ok: true, json: async () => ({ ok: true, result: {
                uids: [spec.query + "-result"], fingerprints: {}, result_total: 1, truncated: false,
                checked_at: "2026-10-04T00:00:00Z", executed_spec: spec,
            } }) };
        },
        usageLog: (...args) => logged.push({ scope: tools.currentAccountScope(), args }), USAGE_KINDS: { watch_checked: "watch_checked" },
        toast: (text) => notices.push({ scope: tools.currentAccountScope(), text }),
        setTimeout: (fn) => { queueMicrotask(fn); return 1; },
    };
    const mod = browserModule("core/project_updates.js", ["pendingDeltaCount", "setWatchesRefreshedHook"], ctx);
    const refreshed = []; mod.setWatchesRefreshedHook(() => refreshed.push(tools.currentAccountScope()));
    hooks.ready();
    return { hooks, mod, tools, lists, stored, gen, calls, writes, notices, refreshed, logged, failures, pendingWrites, project,
        switchTo: (scope) => tools.setCurrentUser({ id: scope }),
        generation: (scope) => gen.get(keyTools.nsKeyFor("biodata_watch_gen", scope)) };
}

test("auto watch batches discard old lists and allow the new account to refresh", async () => {
    const h = await autoWatchHarness(), list = deferred(); h.lists.set("account-A", list);
    const old = h.hooks.health(); h.switchTo("account-B");
    await h.hooks.account();
    const current = JSON.stringify(h.stored.get("account-B").get("shared"));
    list.resolve([h.project("query-A")]); await old;
    assert.equal(JSON.stringify(h.stored.get("account-B").get("shared")), current);
    assert.deepEqual(h.calls.map((c) => c.spec.query), ["query-B"], "A's list is not executed as B's spec");
    assert.deepEqual(h.writes.map((w) => w.scope), ["account-B"]);
    assert.equal(h.generation("account-A"), undefined);
    assert.equal(h.generation("account-B"), "generation-1");
    assert.deepEqual(h.refreshed, ["account-B"]);
    assert.equal(h.notices.length, 1);
});

for (const failure of ["http", "write", "list"]) {
    test(`auto watch ${failure} failures do not complete the generation and are retryable`, async () => {
        const h = await autoWatchHarness(); h.failures[failure] = 1;
        h.stored.get("account-A").set("other", h.project("other-query", "other"));
        await h.hooks.health();
        assert.equal(h.generation("account-A"), undefined, "partial failure cannot mark all projects complete");
        const before = h.calls.length;
        await h.hooks.health();
        assert.equal(h.calls.length, before + 2, "the same generation must retry all saved specs");
        assert.equal(h.generation("account-A"), "generation-1");
        assert.equal(h.stored.get("account-A").get("shared").check_condition.baseline.uids[0], "query-A-result");
        await h.hooks.health(); assert.equal(h.calls.length, before + 2, "completed generations remain deduplicated");
    });
}

test("auto watch transaction callbacks and notifications cannot cross an account epoch", async () => {
    const h = await autoWatchHarness(), write = deferred(); h.pendingWrites.push(write);
    const before = JSON.stringify(h.stored.get("account-A").get("shared"));
    const old = h.hooks.health(); await settleIO(); h.switchTo("account-B");
    await h.hooks.account(); write.resolve(); await old;
    assert.equal(JSON.stringify(h.stored.get("account-A").get("shared")), before, "stale mutator must not commit");
    assert.equal(h.generation("account-A"), undefined);
    assert.equal(h.generation("account-B"), "generation-1");
    assert.deepEqual(h.refreshed, ["account-B"]);
    assert.deepEqual(h.logged.map((l) => l.scope), ["account-B"]);
});

function replayWindowHarness() {
    const tools = requestTools(); tools.setCurrentUser({ id: "account-A" });
    const dom = replayGraphHarness(), ids = new Map(), timers = new Map(), requests = [], rendered = [], points = [];
    let seq = 0, status = { ok: true, drawing: false, unseen: 1 }, seenReply = { ok: true };
    const identity = { conv_id: "conversation-A", key: "key-A" };
    for (const id of ["sideReplayPane", "replayNavBadge", "replayNote", "replayChat", "replayEmpty"]) {
        ids.set(id, dom.document.createElement("div"));
    }
    const ctx = { ...tools, document: dom.document, window: {}, $: (id) => ids.get(id) || null,
        escapeHtml: String, toast: noop, mdLite: String, llmOverrides: () => ({}), skipReasonZh: String,
        replayConvAuth: () => ({ ...identity }),
        initReplayGraph: () => ({ reload: (d) => rendered.push(d),
            viewState: () => ({ tab: "current", digestVersion: Number(rendered.at(-1)?.digest_version) }) }),
        setTimeout: (fn) => { timers.set(++seq, fn); return seq; }, clearTimeout: (id) => timers.delete(id),
        fetch: async (url, opts = {}) => {
            requests.push({ url, body: opts.body ? JSON.parse(opts.body) : null });
            if (url.startsWith("/api/replay/status")) return { ok: true, json: async () => status };
            if (url.startsWith("/api/replay/points")) {
                const reply = points.shift();
                if (reply instanceof Error) throw reply;
                return { ok: true, json: async () => reply };
            }
            if (url === "/api/replay/seen") {
                const reply = await (typeof seenReply === "function" ? seenReply() : seenReply);
                return { ok: true, json: async () => reply };
            }
            throw new Error("Unexpected request " + url);
        },
    };
    const mod = browserModule("panel/replay.js", ["initReplay", "replaySetActive", "replayReset", "replayTurnDone"], ctx);
    mod.initReplay();
    const payload = (version) => ({ ok: true, digest_version: version, digest: { created_at: version },
        unseen: ["o-0"], threads: {}, snapshots: [], n_turns: 1, drawing: false });
    const tick = async () => { const [id, fn] = timers.entries().next().value; timers.delete(id); await fn(); await settleIO(); };
    return { mod, tools, ids, identity, points, rendered, requests, payload, tick,
        open: () => { ids.get("sideReplayPane").hidden = false; mod.replaySetActive(true); },
        seen: () => requests.filter((r) => r.url === "/api/replay/seen"),
        setStatus: (d) => { status = d; }, setSeen: (d) => { seenReply = d; } };
}

test("replay status polling never acknowledges points that failed to load", async () => {
    const h = replayWindowHarness();
    h.mod.replayTurnDone(); await settleIO();
    assert.equal(h.ids.get("replayNavBadge").textContent, "1");
    h.points.push(new Error("initial points unavailable")); h.open(); await settleIO();
    h.points.push(new Error("poll points unavailable")); await h.tick();
    assert.equal(h.seen().length, 0, "status success is not evidence that any digest was rendered");
    assert.equal(h.ids.get("replayNavBadge").hidden, false, "failed points leave the unread badge intact");
    assert.equal(h.ids.get("replayNavBadge").textContent, "1");
    h.points.push(h.payload(3)); await h.tick();
    assert.equal(h.seen().length, 1); assert.equal(h.seen()[0].body.observed_version, 3);
    assert.equal(h.rendered.at(-1).digest_version, 3);
    assert.equal(h.ids.get("replayNavBadge").hidden, true);
});

test("stale seen replies require a successful version refresh before retrying", async () => {
    const h = replayWindowHarness(); h.setSeen({ ok: true, stale: true, digest_version: 2 });
    h.points.push(h.payload(1), new Error("stale points unavailable")); h.open(); await settleIO();
    assert.equal(h.seen().length, 1, "failed stale reload must not submit a second acknowledgement");
    assert.equal(h.seen()[0].body.observed_version, 1);
    assert.equal(h.ids.get("replayNavBadge").hidden, false);
    assert.equal(h.rendered.at(-1).digest_version, 1, "failed reload retains the last rendered version");
    h.setSeen({ ok: true }); h.points.push(h.payload(2)); await h.tick();
    assert.equal(h.seen().length, 2); assert.equal(h.seen()[1].body.observed_version, 2);
    assert.equal(h.ids.get("replayNavBadge").hidden, true);
});

test("a late stale seen reply cannot refresh or acknowledge the next identity", async () => {
    const h = replayWindowHarness(), reply = deferred(); h.setSeen(() => reply.promise);
    h.points.push(h.payload(1)); h.open(); await settleIO();
    h.tools.setCurrentUser({ id: "account-B" }); h.identity.conv_id = "conversation-B"; h.identity.key = "key-B";
    h.points.push(new Error("next identity unavailable")); h.mod.replayReset(); await settleIO();
    const before = h.requests.length;
    reply.resolve({ ok: true, stale: true, digest_version: 2 }); await settleIO();
    assert.equal(h.requests.length, before, "A's stale response cannot start B's refresh");
    assert.equal(h.seen().length, 1);
    assert.equal(h.seen()[0].body.conv_id, "conversation-A");
});

test("auto watch account round trips discard the original batch and keep the latest result", async () => {
    const h = await autoWatchHarness(), oldList = deferred(); h.lists.set("account-A", oldList);
    const old = h.hooks.health(); h.switchTo("account-B"); await h.hooks.account();
    h.lists.delete("account-A"); h.stored.get("account-A").set("shared", h.project("query-A-latest"));
    h.switchTo("account-A"); await h.hooks.account();
    const current = JSON.stringify(h.stored.get("account-A").get("shared"));
    oldList.resolve([h.project("query-A-old")]); await old;
    assert.equal(JSON.stringify(h.stored.get("account-A").get("shared")), current);
    assert.deepEqual(h.calls.map((c) => c.spec.query), ["query-B", "query-A-latest"]);
    assert.deepEqual(h.refreshed, ["account-B", "account-A"]);
    assert.equal(h.generation("account-A"), "generation-1");
});

test("auto watch notifications stop if a callback changes the account", async () => {
    const h = await autoWatchHarness(); let laterHook = 0;
    h.mod.setWatchesRefreshedHook(() => h.switchTo("account-B"));
    h.mod.setWatchesRefreshedHook(() => laterHook++);
    await h.hooks.health();
    assert.equal(laterHook, 0);
    assert.equal(h.generation("account-A"), "generation-1");
    assert.equal(h.generation("account-B"), undefined);
});

test("successful stale refresh renders and acknowledges the new version even at the same timestamp", async () => {
    const h = replayWindowHarness(); let posted = 0;
    h.setSeen(() => (++posted === 1 ? { ok: true, stale: true } : { ok: true }));
    const next = h.payload(2); next.digest.created_at = 1;
    h.points.push(h.payload(1), next); h.open(); await settleIO();
    assert.deepEqual(h.seen().map((r) => r.body.observed_version), [1, 2]);
    assert.equal(h.rendered.at(-1).digest_version, 2, "a new digest version must actually be rendered");
    assert.equal(h.ids.get("replayNavBadge").hidden, true);
});

for (const version of [undefined, null, "invalid"]) {
    test(`points with missing or invalid version ${String(version)} cannot submit seen`, async () => {
        const h = replayWindowHarness(); h.mod.replayTurnDone(); await settleIO();
        const payload = h.payload(1); payload.digest_version = version;
        h.points.push(payload); h.open(); await settleIO();
        assert.equal(h.seen().length, 0, "the legacy unversioned path must not be used by this frontend");
        assert.equal(h.ids.get("replayNavBadge").hidden, false);
    });
}

test("a badge poll that returns after opening a failed points view keeps unread state", async () => {
    const h = replayWindowHarness(); h.mod.replayTurnDone(); await settleIO();
    assert.equal(h.ids.get("replayNavBadge").textContent, "1");
    const status = deferred(); h.setStatus(status.promise); h.mod.replayTurnDone();
    h.points.push(new Error("points unavailable")); h.open(); await settleIO();
    status.resolve({ ok: true, drawing: false, unseen: 1 }); await settleIO();
    assert.equal(h.seen().length, 0);
    assert.equal(h.ids.get("replayNavBadge").hidden, false, "opening is not evidence that points were seen");
    assert.equal(h.ids.get("replayNavBadge").textContent, "1");
});

for (const origin of ["current", "snapshot"]) {
    for (const result of ["success", "error"]) {
        test(`same-identity ${origin} annotation ${result} respects ordinary tab navigation`, async () => {
            const h = annotationHarness(), request = await h.start(origin);
            if (origin === "snapshot") h.dom.byClass("g-tabs")[0].children[0].listeners.click();
            else await h.handle.openSnapshot(h.snapshot.snap_id);
            const next = h.dom.byClass("g-tabs")[0].children.find((b) => b.classList.contains("on")).textContent;
            const updated = { ...h.snapshot, count: 2, text: "saved plus new annotation",
                items: h.snapshot.items.map((it) => ({ ...it, notes: [...it.notes, "new annotation"] })) };
            if (result === "error") h.replies[0].reject(new Error("synthetic annotation failure"));
            else h.replies[0].resolve({ ok: true, snapshot: updated });
            await request.pending;
            assert.equal(h.dom.byClass("g-tabs")[0].children.find((b) => b.classList.contains("on")).textContent, next);
            assert.equal(request.thread.children.length, request.count, "detached panel content is never changed");
            assert.equal(request.textarea.value, "new annotation");
            assert.equal(request.send.disabled, true, "detached controls do not receive finally writes");
            assert.equal(h.changed.length, result === "success" ? 1 : 0,
                "same-identity success must refresh the host even when its original tab is inactive");
            if (result === "success" && origin === "current") {
                assert.equal(request.points.threads["o-0"][0].content, "new annotation");
            }
            if (result === "success" && origin === "snapshot") {
                await h.handle.openSnapshot(h.snapshot.snap_id);
                h.dom.byClass("g-node").at(-1).listeners.click({ stopPropagation: noop });
                assert.equal(h.dom.byClass("thread").at(-1).children.length, 2,
                    "the existing open snapshot cache must carry the saved annotation");
            }
        });
    }
}

test("closing a snapshot tab during annotation keeps it closed but refreshes host data", async () => {
    const h = annotationHarness(), request = await h.start("snapshot");
    h.dom.byClass("g-tabs")[0].children[1].children[0].listeners.click({ stopPropagation: noop });
    h.replies[0].resolve({ ok: true, snapshot: { ...h.snapshot, count: 2, text: "saved after tab closed" } });
    await request.pending;
    assert.deepEqual(h.dom.tabLabels(), ["当前"]);
    assert.equal(h.changed.length, 1, "the points/pill owner still needs to learn that the write succeeded");
    assert.equal(request.thread.children.length, request.count);
    assert.equal(request.send.disabled, true);
});

test("a real empty replay chat renders the single-source hint and swaps it for bubbles", () => {
    const dom = replayGraphHarness();
    const list = dom.document.createElement("div");
    const ctx = { document: dom.document, $: (id) => (id === "replayChat" ? list : null),
        escapeHtml: String, mdLite: (t) => String(t) };
    const mod = browserModule("panel/replay.js", ["_renderChat", "CHAT_HINT"], ctx);
    mod._renderChat([]);
    assert.equal(list.children.length, 1, "无消息时只渲染空态提示");
    assert.equal(list.children[0].className, "rp-side-hint", "空态提示是 .rp-side-hint 盒");
    assert.equal(list.children[0].textContent, mod.CHAT_HINT, "提示文本来自唯一真源 CHAT_HINT（HTML 不再静态摆放）");
    mod._renderChat([{ role: "user", content: "追问一句" }]);
    assert.equal(list.children.length, 1, "有消息时提示让位给气泡（两者互斥）");
    assert.equal(list.children[0].className, "msg msg-user", "气泡仍是既有形状");
    mod._renderChat([]);
    assert.equal(list.children[0].className, "rp-side-hint", "清空后提示必须回来（reset 走同一条路）");
});

test("an empty graph hides the legend row and renders the host-injected skip mapping", () => {
    const L = layoutFunctions();
    const dom = replayGraphHarness();
    let layoutResult = null;
    const mod = replayGraphModule({ document: dom.document, computeLayout: () => layoutResult, PAD: L.PAD, GUTTER: L.GUTTER });
    const handle = mod.initReplayGraph(dom.host, {
        convId: () => "c1", key: () => "k1", freshPids: () => [], mdLite: String, escapeHtml: String,
        skipReasonZh: (code) => "ZH:" + code, api: { getJSON: async () => ({}), postJSON: async () => ({}) },
    });
    handle.reload({ ok: true, digest: null, snapshots: [], threads: {}, skip_reason: "no_key" });
    assert.equal(dom.byClass("graph-bar")[0].hidden, true, "空图连图例行一起收起（chips/提示不再对着一块空灰盒）");
    assert.equal(dom.byClass("g-skip")[0].textContent, "ZH:no_key", "skip 原因用宿主注入的映射（本模块不再自带第二份）");
    const empties = dom.byClass("g-empty");
    assert.ok(empties.length >= 2, "空态文案分行渲染（svg text 不换行，单行会被侧栏裁断）");
    const emptyText = empties.map((n) => n.textContent).join("");
    assert.ok(emptyText.includes("点上方「重新整理」") && !emptyText.includes("右上角"),
        "空态指路指向 pane 头行，浮窗时代残句已清");
    layoutResult = L.computeLayout({ digest: { motivation: null, outputs: [{ pid: "o-0", title: "out" }],
        knowledge: [], decisions: [], edges: [] } });
    assert.ok(layoutResult, "预置：真布局必须画得出来（否则本条自己失真）");
    handle.reload({ ok: true, digest_version: 1, digest: { outputs: [{ pid: "o-0", title: "out" }],
        knowledge: [], decisions: [], edges: [] }, snapshots: [], threads: {} });
    assert.equal(dom.byClass("graph-bar")[0].hidden, false, "有图时图例行回来");
});

function linkedReplayHarness() {
    const tools = requestTools(); tools.setCurrentUser({ id: "account-A" });
    const dom = replayGraphHarness(), L = layoutFunctions(), ids = new Map(), timers = new Map(), requests = [];
    let seq = 0, graph, annotation = null;
    const identity = { conv_id: "conversation-A", key: "key-A" };
    const snapshot = { snap_id: "snap-A", created_at: 1, count: 1, text: "saved annotation",
        items: [{ pid: "o-0", kind: "outputs", point: { pid: "o-0", title: "Archived output" }, notes: ["saved"] }], edges: [] };
    const payload = (version) => ({ ok: true, digest_version: version,
        digest: { created_at: version, outputs: [{ pid: "o-0", title: "Current output " + version }],
            knowledge: [], decisions: [], edges: [] },
        unseen: ["o-0"], threads: {}, snapshots: [structuredClone(snapshot)], n_turns: 1, drawing: false });
    let server = payload(1);
    for (const id of ["sideReplayPane", "replayGraph", "replayGraphToggle", "replayNavBadge",
        "replayNote", "replayChat", "replayEmpty", "replayComposer"]) ids.set(id, dom.document.createElement("div"));
    const pill = dom.document.createElement("span"); pill.className = "pill";
    pill.dataset = { snapId: "snap-A", payload: snapshot.text };
    ids.get("replayComposer").append(pill);
    ids.get("replayComposer").querySelectorAll = () => [pill];
    const graphModule = replayGraphModule({ document: dom.document, ...L });
    const ctx = { ...tools, document: dom.document, window: {}, Node: { TEXT_NODE: 3, ELEMENT_NODE: 1 },
        $: (id) => ids.get(id) || null, escapeHtml: String, toast: noop, mdLite: String, skipReasonZh: String,
        llmOverrides: () => ({}), replayConvAuth: () => ({ ...identity }),
        initReplayGraph: (host, context) => { graph = graphModule.initReplayGraph(host, context); return graph; },
        setTimeout: (fn) => { timers.set(++seq, fn); return seq; }, clearTimeout: (id) => timers.delete(id),
        fetch: async (url, opts = {}) => {
            const body = opts.body ? JSON.parse(opts.body) : null; requests.push({ url, body });
            if (url.startsWith("/api/replay/status")) return { ok: true, json: async () => ({ ok: true, unseen: server.unseen.length }) };
            if (url.startsWith("/api/replay/points")) return { ok: true, json: async () => structuredClone(server) };
            if (url.startsWith("/api/replay/snapshots")) return { ok: true, json: async () => ({ snapshots: structuredClone(server.snapshots) }) };
            if (url.endsWith("/annotate")) {
                assert.ok(annotation, "annotation IO must be explicitly delayed");
                const result = await annotation.promise;
                if (result.snapshot) server.snapshots = [structuredClone(result.snapshot)];
                return { ok: true, json: async () => result };
            }
            if (url === "/api/replay/seen") {
                assert.ok(Number.isInteger(body.observed_version));
                const stale = body.observed_version !== server.digest_version;
                if (!stale) server.unseen = [];
                return { ok: true, json: async () => ({ ok: true, stale }) };
            }
            throw new Error("Unexpected linked replay request " + url);
        },
    };
    const mod = browserModule("panel/replay.js", ["initReplay", "replaySetActive", "replayTurnDone"], ctx);
    mod.initReplay();
    const current = () => dom.byClass("g-tabs")[0].children[0].listeners.click();
    const active = () => dom.byClass("g-tabs")[0].children.find((b) => b.classList.contains("on")).textContent;
    return { mod, dom, ids, pill, payload, current, active,
        graph: () => graph, requests, seen: () => requests.filter((r) => r.url === "/api/replay/seen"),
        open: () => { ids.get("sideReplayPane").hidden = false; mod.replaySetActive(true); },
        close: () => { ids.get("sideReplayPane").hidden = true; mod.replaySetActive(false); },
        update: (version) => { server = payload(version); },
        delayAnnotation: () => { annotation = deferred(); return annotation; },
        tick: async () => { const [id, fn] = timers.entries().next().value; timers.delete(id); await fn(); await settleIO(); } };
}

for (const reopen of [false, true]) {
    test(`a real snapshot view does not acknowledge a newer current digest${reopen ? " after reopening" : " while open"}`, async () => {
        const h = linkedReplayHarness(); h.open(); await settleIO();
        assert.deepEqual(h.seen().map((r) => r.body.observed_version), [1]);
        await h.graph().openSnapshot("snap-A");
        if (reopen) h.close();
        h.update(2);
        if (reopen) { h.mod.replayTurnDone(); await settleIO(); h.open(); await settleIO(); }
        else await h.tick();
        assert.notEqual(h.active(), "当前", "snapshot view is actually retained by the real graph");
        assert.deepEqual(h.seen().map((r) => r.body.observed_version), [1], "points injection is not display evidence");
        assert.equal(h.ids.get("replayNavBadge").hidden, false);
        h.current(); await settleIO();
        assert.deepEqual(h.seen().map((r) => r.body.observed_version), [1, 2], "selecting current renders and acknowledges v2");
        assert.equal(h.ids.get("replayNavBadge").hidden, true);
    });
}

test("a collapsed real graph keeps its digest unread until it is expanded", async () => {
    const h = linkedReplayHarness(); h.ids.get("sideReplayPane").classList.add("rp-graph-collapsed");
    h.open(); await settleIO();
    assert.equal(h.seen().length, 0, "the hidden right-hand graph is not an observed digest");
    h.ids.get("replayGraphToggle").listeners.click(); await settleIO();
    assert.deepEqual(h.seen().map((r) => r.body.observed_version), [1]);
});

for (const closeTab of [false, true]) {
    test(`same-identity snapshot annotation refreshes its real host pill after ${closeTab ? "tab close" : "tab switch"}`, async () => {
        const h = linkedReplayHarness(); h.open(); await settleIO(); await h.graph().openSnapshot("snap-A");
        h.dom.byClass("g-node").at(-1).listeners.click({ stopPropagation: noop });
        const row = h.dom.byClass("point-input").at(-1), thread = h.dom.byClass("thread").at(-1);
        const textarea = row.children[0], send = row.children[1]; textarea.value = "later note";
        const io = h.delayAnnotation(), pending = send.listeners.click(), initialThread = thread.children.length;
        if (closeTab) h.dom.byClass("g-tabs")[0].children[1].children[0].listeners.click({ stopPropagation: noop });
        else h.current();
        io.resolve({ ok: true, snapshot: { snap_id: "snap-A", created_at: 1, count: 2, text: "saved plus later note",
            items: [{ pid: "o-0", kind: "outputs", point: { pid: "o-0", title: "Archived output" }, notes: ["saved", "later note"] }], edges: [] } });
        await pending; await settleIO();
        assert.equal(h.pill.dataset.payload, "saved plus later note", "the single points payload channel must refresh the pill");
        assert.equal(thread.children.length, initialThread, "the detached annotation panel is not touched");
        assert.equal(send.disabled, true);
        assert.equal(h.dom.tabLabels().length, closeTab ? 1 : 2, "a user-closed snapshot tab is not resurrected");
    });
}

test("a late snapshot annotation refresh preserves the current tab's live draft", async () => {
    const h = linkedReplayHarness(); h.open(); await settleIO(); await h.graph().openSnapshot("snap-A");
    h.dom.byClass("g-node").at(-1).listeners.click({ stopPropagation: noop });
    const oldRow = h.dom.byClass("point-input").at(-1); oldRow.children[0].value = "snapshot note";
    const io = h.delayAnnotation(), pending = oldRow.children[1].listeners.click();
    h.current(); h.dom.byClass("g-node").at(-1).listeners.click({ stopPropagation: noop });
    const currentRow = h.dom.byClass("point-input").at(-1); currentRow.children[0].value = "current draft";
    io.resolve({ ok: true, snapshot: { snap_id: "snap-A", created_at: 1, count: 2, text: "saved plus snapshot note",
        items: [{ pid: "o-0", kind: "outputs", point: { pid: "o-0", title: "Archived output" }, notes: ["saved", "snapshot note"] }], edges: [] } });
    await pending; await settleIO();
    assert.equal(h.dom.panes()[0].contains(currentRow), true, "metadata refresh must not replace the next tab's controls");
    assert.equal(currentRow.children[0].value, "current draft");
    assert.equal(h.pill.dataset.payload, "saved plus snapshot note");
});

function aiReleaseHarness() {
    const identity = requestTools(); identity.setCurrentUser({ id: "owner-A" });
    const listeners = [], requests = [], messages = [], warnings = [];
    const cfg = { provider: "openai-compatible", base_url: "https://old.example.invalid/v1", model: "old-model",
        use_llm: true, mock_llm: false, api_key: "synthetic-secret-sentinel" };
    const state = { project: "p1" };
    const ctx = { ...identity, getConfig: () => ({ ...cfg }), endpointCfg: () => ({ provider: cfg.provider,
        base_url: cfg.base_url, model: cfg.model, project_id: state.project }),
        registerContextChanged: (fn) => listeners.push(fn),
        _aiConfigurationHook: null, _aiConfigurationContext: null, _aiConfigurationBound: false, _aiConfigurationPending: new Map(),
        _releaseRequests: {}, _listRequests: {}, _candidateRequests: {}, _learningRequests: {}, _moreRequests: { candidate: {}, audit: {} },
        _learningConfigGeneration: 0, _learningStatusIdentity: null, _pendingToken: null,
        _learning: { configured: true, scope: { endpoint_fp: "ep-old" } },
        API: { learningControl: "/api/learning/control", learningStatus: "/api/learning/status" }, toast: (text) => warnings.push(text),
        rulesStatus: (text, error) => messages.push({ text, error }),
        fetch: (url, options) => {
            if (!options?.body) return Promise.resolve({ ok: true, json: async () => ({ ok: true, config_epoch: 7,
                scope: { owner: identity.currentAccountScope() || "local" } }) });
            const io = deferred(); requests.push({ url, body: JSON.parse(options.body), io }); return io.promise;
        } };
    const release = functions("panel/rules.js", ["scopeKey", "learningIdentity", "invalidateLearningConfiguration", "_get", "releaseLearningConfiguration"], ctx);
    const shell = functions("core/shell.js", ["aiConfigurationContext", "setAiConfigurationChangedHook", "notifyAiConfigurationChanged"], ctx);
    shell.setAiConfigurationChangedHook(release.releaseLearningConfiguration);
    function account(id) {
        listeners.forEach((fn) => fn("before-account"));
        identity.setCurrentUser(id ? { id } : null);
        listeners.forEach((fn) => fn("account"));
    }
    function resolve(index, payload = { ok: true, released: true, endpoint_fp: "ep-old", detail: "旧授权已释放" }, status = 200) {
        requests[index].io.resolve({ ok: status < 400, status, json: async () => payload });
    }
    return { cfg, state, ctx, shell, release, requests, messages, warnings, account, resolve };
}

test("effective AI edits immediately release the old endpoint without sending or retaining its key", async () => {
    const h = aiReleaseHarness();
    h.cfg.api_key = ""; h.cfg.use_llm = false;
    h.shell.notifyAiConfigurationChanged({ configurationChanged: true }); await settleIO();
    assert.equal(h.requests.length, 1, "no query or memory-window opening is required");
    assert.equal(h.requests[0].body.action, "release");
    assert.equal(h.requests[0].body.expected_identity, "owner-A");
    assert.equal(h.requests[0].body.model, "old-model");
    assert.equal("api_key" in h.requests[0].body, false);
    assert.equal(JSON.stringify(h.ctx._aiConfigurationContext).includes("synthetic-secret-sentinel"), false);
    assert.equal(h.ctx._learning.configured, true, "must not claim release before a successful acknowledgement");
    h.resolve(0); await settleIO();
    assert.equal(h.ctx._learning.configured, false);
    assert.equal(h.messages.at(-1).error, false);
});

test("switching endpoints releases only the old descriptor and does not clear the new endpoint's visible authorization", async () => {
    const h = aiReleaseHarness();
    h.cfg.base_url = "https://new.example.invalid/v1"; h.cfg.model = "new-model";
    h.shell.notifyAiConfigurationChanged({ configurationChanged: true }); await settleIO();
    assert.equal(h.requests[0].body.base_url, "https://old.example.invalid/v1");
    assert.equal(h.requests[0].body.model, "old-model");
    h.ctx._learning = { configured: true, scope: { endpoint_fp: "ep-new" } };
    h.resolve(0); await settleIO();
    assert.equal(h.ctx._learning.configured, true);
});

test("account switches cannot dispatch an old owner's release or publish late acknowledgement after a round trip", async () => {
    const h = aiReleaseHarness();
    const old = { ...h.ctx._aiConfigurationContext };
    h.shell.notifyAiConfigurationChanged({ configurationChanged: true }); await settleIO();
    h.account("owner-B");
    const skipped = await h.release.releaseLearningConfiguration(old);
    assert.equal(skipped.skipped, true); assert.equal(h.requests.length, 1);
    h.account("owner-A");
    const count = h.messages.length;
    h.resolve(0); await settleIO();
    assert.equal(h.messages.length, count); assert.equal(h.ctx._learning.configured, true);
});

test("scope changes invalidate stale learning status and release responses, while release failures stay visible", async () => {
    const h = aiReleaseHarness();
    const oldList = h.ctx.beginRequest(h.ctx._listRequests, () => "same");
    h.shell.notifyAiConfigurationChanged({ configurationChanged: true }); await settleIO();
    assert.equal(oldList.isCurrent(), false);
    h.state.project = "p2";
    const count = h.messages.length;
    h.resolve(0); await settleIO();
    assert.equal(h.messages.length, count);
    assert.equal(h.ctx._learning.configured, true);
    h.shell.notifyAiConfigurationChanged({ configurationChanged: true }); await settleIO();
    h.resolve(1, { ok: false, detail: "无法释放旧授权" }, 503); await settleIO();
    assert.equal(h.messages.at(-1).error, true);
    assert.ok(h.warnings.some((text) => text.includes("释放失败")));
    assert.equal(h.ctx._learning.configured, true, "failure must not masquerade as a successful stop");
});

function sharedRefreshHarness() {
    const identity = requestTools(), io = [], query = element(), chat = element();
    identity.setCurrentUser({ id: "alice" });
    const resets = [], restored = [], opened = [];
    const ctx = { ...identity, _sharedRequests: {}, _sharedPollRequests: {}, _sharedForegroundToken: null,
        _sharedRefreshToken: null, _sharedMode: true, _sharedConversation: { id: "A", revision: 1 }, _cbConvId: "A",
        _cbHistoryOwner: "alice", _ubBusy: false, _routeRequests: {}, _ubSeq: 0, _cbHistoryTimer: null,
        _cbRestoring: false, _cbCommentFor: "A-message", _cbCommentDraft: "A draft", _cbCommentBusy: false,
        _commentRequests: {}, $: (id) => id === "queryInput" ? query : id === "chatInput" ? chat : null,
        getSharedConversation: (id, options) => { const item = deferred(); io.push({ id, options, ...item }); return item.promise; },
        workspaceEnsureSelect: async () => true, cbRestoreShared: (row) => restored.push(row.id),
        sharedConversationAction: async () => ({ conversation: { id: "branch" } }),
        listSharedConversations: async () => [], notifyContextChanged: noop, resetSubmitButton: noop,
        bumpRecSeq: noop, toast: noop, cbSharedMissing: () => false, cbRecoverRun: async () => {},
        openPopup: (url) => opened.push(url), clearTimeout: noop, setFacetState: noop, cbReplay: noop,
        swSync: noop, replayReset: () => resets.push(ctx._cbConvId), exitResultsLayout: noop,
        setLastRecommendData: noop, cbRenderSteps: noop, cbRenderHistory: noop, resetFacetState: noop,
        cbControlSnapshot: () => ({}), cbSharedRestore: (row) => ({ stack: [], log: [{ id: row.id + "-message" }], cursor: -1 }),
        askRelaxShow: noop, location: { pathname: "/" }, history: { replaceState: noop } };
    return { ctx, io, query, chat, resets, restored, opened };
}

test("latest shared frame goes through persistent select_frame rather than local replay", () => {
    const actions = [], ctx = { _cbStack: [{ id: 1, serverId: "f1" }, { id: 2, serverId: "f2" }], _cbCursor: 0,
        _sharedConversation: { id: "A" }, cbFrameIndexById: (id) => id - 1,
        cbSharedAction: (...args) => actions.push(args), cbReplay: () => assert.fail("shared selection must persist"), toast: noop };
    functions("panel/board.js", ["cbViewFrame", "cbToLatest"], ctx).cbToLatest();
    assert.equal(actions.length, 1); assert.equal(actions[0][0], "select_frame"); assert.equal(actions[0][1].frame_id, "f2");
});

test("same conversation refresh preserves replay composer while identity change clears comments", () => {
    const h = sharedRefreshHarness();
    const api = functions("panel/board.js", ["cbRestoreShared"], h.ctx);
    api.cbRestoreShared({ id: "A", revision: 2, current_state: {} }, { preserveDraft: true });
    assert.equal(h.resets.length, 0, "MCP refresh must not erase replay draft or pills");
    h.query.value = "A unfinished query"; h.chat.value = "A unfinished chat";
    api.cbRestoreShared({ id: "B", revision: 1, current_state: {} }, { preserveDraft: true });
    assert.deepEqual(h.resets, ["B"], "replay restart must observe the new identity, never the old conversation");
    assert.equal(h.ctx._cbCommentFor, ""); assert.equal(h.ctx._cbCommentDraft, "");
    assert.equal(h.query.value, ""); assert.equal(h.chat.value, "");
});

test("shared polling is single flight and cannot invalidate a foreground opening or branch", async () => {
    const h = sharedRefreshHarness(), api = functions("panel/board.js", ["cbOpenSharedConversation", "cbRefreshSharedConversation", "cbSharedAction"], h.ctx);
    const opening = api.cbOpenSharedConversation("B");
    api.cbRefreshSharedConversation(); api.cbRefreshSharedConversation();
    assert.equal(h.io.length, 1, "no poll starts while foreground GET waits");
    h.io[0].resolve({ id: "B", revision: 1 }); await opening;
    assert.deepEqual(h.restored, ["B"]);
    const poll = api.cbRefreshSharedConversation(); await api.cbRefreshSharedConversation();
    assert.equal(h.io.length, 2, "two timer ticks share one refresh");
    h.io[1].resolve({ id: "A", revision: 1 }); await poll;
    const branch = api.cbSharedAction("branch", {}, true);
    await api.cbRefreshSharedConversation(); await branch;
    assert.equal(h.opened.length, 1);
});

function rulesLifecycleHarness() {
    const identity = requestTools(), box = reviewNode(), status = reviewNode(), calls = [], notices = [], nodes = [];
    identity.setCurrentUser({ id: "alice" });
    const cfg = { provider: "openai-compatible", base_url: "https://example.invalid/v1", model: "m", project_id: "p", use_llm: true, api_key: "synthetic-only" };
    function walk(node) { return [node, ...node.children.flatMap(walk)]; }
    Object.defineProperty(box, "innerHTML", { get: () => "", set: () => { box.children.length = 0; } });
    box.querySelectorAll = () => walk(box).filter((node) => node.id || node.tag === "details");
    const listeners = new Set();
    const globals = { ...identity, apiErrorText, knowledgeDedupRequestId: knowledgeCore.knowledgeDedupRequestId,
        knowledgeRelationSummary: knowledgeCore.knowledgeRelationSummary,
        endpointCfg: () => ({ provider: cfg.provider, base_url: cfg.base_url, model: cfg.model, project_id: cfg.project_id }),
        registerContextChanged: (fn) => { listeners.add(fn); return () => listeners.delete(fn); },
        $: (id) => id === "rulesList" ? box : id === "rulesStatus" ? status : walk(box).find((node) => node.id === id),
        document: { createElement: (tag) => { const node = reviewNode(tag); nodes.push(node);
            if (tag === "input") node.value = "";
            if (tag === "div" || tag === "article") node.querySelector = () => { const acts = reviewNode(); node.appendChild(acts); return acts; };
            return node; } }, window: { confirm: () => true }, crypto: webcrypto,
        API: { rulesList: "/rules", learningStatus: "/status", learningCandidates: "/candidates", learningControl: "/control", learningTrigger: "/trigger", learningReview: "/review" },
        escapeHtml: String, fmtTime: String, toast: noop, COPY: { common: { failedRetry: "failed" } },
        setStatusLine: (_node, text, error) => notices.push({ text, error }), learningTargetScope: () => cfg.project_id,
        fetch: (url, options) => {
            if (!options?.body && url.startsWith("/status?") && !url.includes("project_id=")) {
                return Promise.resolve({ ok: true, json: async () => ({ ok: true, config_epoch: 7,
                    scope: { owner: identity.currentAccountScope() || "local" } }) });
            }
            const io = deferred(); calls.push({ url, body: options?.body && JSON.parse(options.body), ...io }); return io.promise;
        } };
    const code = source("panel/rules.js").replace(/^import .*$/gm, "").replace(/^export /gm, "");
    const api = runInNewContext(code + '\n({renderCollabRules,setLearningConfigProvider,releaseLearningConfiguration,_learningPost,_learningMore,_render,read:()=>({_learning,_persistent,_reviews,_next,_reviewNext})})', globals);
    api.setLearningConfigProvider(() => cfg);
    function resolve(index, data, ok = true) { calls[index].resolve({ ok, json: async () => data }); }
    async function load(items = [{ id: "c1", record_type: "candidate", text: "candidate" }], reviews = [{ id: "a1", decision: "reject" }], epoch = 7) {
        const index = calls.length, pending = api.renderCollabRules();
        resolve(index, { ok: true, rules: [] }); resolve(index + 1, { ok: true, config_epoch: epoch, configured: true, scope: { enabled: true } });
        resolve(index + 2, { ok: true, items, next_offset: 100 }); resolve(index + 3, { ok: true, items: reviews, next_offset: 100 });
        await pending;
    }
    return { api, cfg, box, nodes, notices, calls, resolve, load, listeners, identity, walk, globals };
}

test("learning authorization waits for loaded status and sends identity plus config epoch", async () => {
    const h = rulesLifecycleHarness();
    const unconfigured = h.api._learningPost("/control", { enabled: true });
    assert.equal(h.calls.length, 0, "no status means no trusted CAS baseline, not silent authorization");
    await unconfigured;
    await h.load();
    const request = h.api._learningPost("/trigger", {});
    assert.equal(h.calls[4].body.expected_identity, "alice"); assert.equal(h.calls[4].body.expected_config_epoch, 7);
    h.resolve(4, { ok: false, detail: "learning_config_conflict" }, false); await request;
});

test("failed refresh preserves pause and refresh controls and reports its new error", async () => {
    const h = rulesLifecycleHarness(); await h.load();
    const pending = h.api.renderCollabRules(true), index = h.calls.length - 4;
    h.resolve(index, { ok: false, detail: "network unavailable" }, false); await pending;
    assert.equal(h.box.hidden, false); assert(h.walk(h.box).some((n) => n.textContent === "暂停学习"));
    assert(h.walk(h.box).some((n) => n.textContent === "刷新状态")); assert.equal(h.notices.at(-1).error, true);
    assert(h.notices.at(-1).text.includes("network unavailable"));
});

test("candidate and audit pages are single flight, deduplicated and preserve unfinished inputs", async () => {
    const h = rulesLifecycleHarness(); await h.load();
    const feedback = h.walk(h.box).find((n) => n.id === "learningFeedback-c1"); feedback.value = "unfinished review";
    const scopeFeedback = h.walk(h.box).find((n) => n.id === "learningScopeFeedback"); scopeFeedback.value = "unfinished explicit correction";
    const details = h.walk(h.box).find((n) => n.tag === "details"); details.open = true;
    const first = h.api._learningMore("candidate"); const duplicate = h.api._learningMore("candidate");
    assert.equal(h.calls.length, 5, "double click cannot load the same candidate page twice");
    h.resolve(4, { ok: true, items: [{ id: "c1", record_type: "candidate", text: "updated" }, { id: "c2", record_type: "candidate", text: "next" }], next_offset: null });
    await first; await duplicate;
    assert.deepEqual(Array.from(h.api.read()._persistent, (r) => r.id), ["c1", "c2"]);
    assert.equal(h.walk(h.box).find((n) => n.id === "learningFeedback-c1").value, "unfinished review");
    assert.equal(h.walk(h.box).find((n) => n.tag === "details").open, true);
    const audit = h.api._learningMore("audit"), auditAgain = h.api._learningMore("audit");
    assert.equal(h.calls.length, 6); h.resolve(5, { ok: true, items: [{ id: "a1", decision: "reject" }, { id: "a2", decision: "approve" }], next_offset: null });
    await audit; await auditAgain; assert.equal(h.api.read()._reviews.length, 2);
    assert.equal(h.walk(h.box).find((n) => n.id === "learningScopeFeedback").value, "unfinished explicit correction");
    const ids = h.walk(h.box).map((n) => n.id).filter(Boolean); assert.equal(new Set(ids).size, ids.length);
});

test("failed endpoint release remains retryable for the original endpoint", async () => {
    const h = aiReleaseHarness();
    h.cfg.base_url = "https://new.example.invalid/v1"; h.cfg.model = "new-model";
    h.shell.notifyAiConfigurationChanged({ configurationChanged: true }); await settleIO();
    h.resolve(0, { ok: false, detail: "offline" }, 503); await settleIO();
    h.shell.notifyAiConfigurationChanged(); await settleIO();
    assert.equal(h.requests.length, 2, "retry without another edit still dispatches the pending old release");
    assert.equal(h.requests[1].body.base_url, "https://old.example.invalid/v1"); assert.equal(h.requests[1].body.model, "old-model");
    assert.equal("api_key" in h.requests[1].body, false);
    h.resolve(1); await settleIO(); h.shell.notifyAiConfigurationChanged(); await settleIO(); assert.equal(h.requests.length, 2);
});

test("key-only configuration changes discard every old learning completion after release", async () => {
    const h = rulesLifecycleHarness(); await h.load();
    const oldPost = h.api._learningPost("/trigger", {});
    assert.equal(h.calls[4].body.expected_config_epoch, 7);
    h.cfg.api_key = "new-synthetic-key";
    const release = h.api.releaseLearningConfiguration({ owner: "alice", provider: h.cfg.provider, base_url: h.cfg.base_url, model: h.cfg.model }); await settleIO();
    const count = h.calls.length;
    h.resolve(5, { ok: true, released: true, detail: "released" }); await release;
    const notices = h.notices.length;
    h.resolve(4, { ok: true, detail: "old config authorized" }); await oldPost;
    assert.equal(h.calls.length, count, "late POST cannot start a fresh status read and revive its old cache");
    assert.equal(h.notices.length, notices, "late POST cannot replace the release acknowledgement");
    await h.api._learningPost("/control", { enabled: true }); assert.equal(h.calls.length, count);
    assert(!JSON.stringify(h.calls[5].body).includes("api_key"));
});

test("status and candidate pages from before a configuration change cannot reappear", async () => {
    const h = rulesLifecycleHarness(); await h.load();
    const page = h.api._learningMore("candidate"), refresh = h.api.renderCollabRules();
    const release = h.api.releaseLearningConfiguration({ owner: "alice", provider: h.cfg.provider, base_url: h.cfg.base_url, model: h.cfg.model }); await settleIO();
    h.resolve(9, { ok: true, released: true, detail: "released" }); await release;
    h.resolve(4, { ok: true, items: [{ id: "stale", record_type: "candidate", text: "old" }], next_offset: null }); await page;
    h.resolve(5, { ok: true, rules: [] }); h.resolve(6, { ok: true, configured: true, config_epoch: 7, scope: { enabled: true } });
    h.resolve(7, { ok: true, items: [{ id: "stale" }], next_offset: null }); h.resolve(8, { ok: true, items: [], next_offset: null });
    await refresh;
    assert.deepEqual(Array.from(h.api.read()._persistent, (row) => row.id), ["c1"]);
    assert.equal(h.api.read()._learning.configured, false);
});

test("learning mutations are single flight and account round trips invalidate the loaded status baseline", async () => {
    const h = rulesLifecycleHarness(); await h.load();
    const first = h.api._learningPost("/trigger", {}); await h.api._learningPost("/trigger", {});
    assert.equal(h.calls.length, 5);
    h.identity.setCurrentUser({ id: "bob" }); h.identity.setCurrentUser({ id: "alice" });
    const noticeCount = h.notices.length;
    h.resolve(4, { ok: true, detail: "old reply" }); await first;
    assert.equal(h.notices.length, noticeCount); assert.equal(h.calls.length, 5);
    await h.api._learningPost("/trigger", {}); assert.equal(h.calls.length, 5);
});

test("a new full candidate refresh invalidates old pagination without losing feedback or audit expansion", async () => {
    const h = rulesLifecycleHarness(); await h.load();
    h.walk(h.box).find((node) => node.id === "learningFeedback-c1").value = "not yet submitted";
    h.walk(h.box).find((node) => node.tag === "details").open = true;
    const oldPage = h.api._learningMore("candidate");
    await h.load([{ id: "c1", record_type: "candidate", text: "latest" }]);
    h.resolve(4, { ok: true, items: [{ id: "obsolete" }], next_offset: null }); await oldPage;
    assert.deepEqual(Array.from(h.api.read()._persistent, (row) => row.id), ["c1"]);
    assert.equal(h.walk(h.box).find((node) => node.id === "learningFeedback-c1").value, "not yet submitted");
    assert.equal(h.walk(h.box).find((node) => node.tag === "details").open, true);
});

test("failed old endpoint releases remain queued across additional endpoint edits without storing credentials", async () => {
    const h = aiReleaseHarness();
    h.cfg.model = "model-B"; h.shell.notifyAiConfigurationChanged({ configurationChanged: true }); await settleIO();
    h.resolve(0, { ok: false, detail: "offline" }, 503); await settleIO();
    h.cfg.model = "model-C"; h.shell.notifyAiConfigurationChanged({ configurationChanged: true }); await settleIO();
    assert.deepEqual(h.requests.slice(1).map((row) => row.body.model), ["old-model", "model-B"]);
    assert(!JSON.stringify(Array.from(h.ctx._aiConfigurationPending)).includes("synthetic-secret-sentinel"));
    h.resolve(1); h.resolve(2); await settleIO();
    assert.equal(h.ctx._aiConfigurationPending.size, 0);
});

for (const phase of ["imports", "upload", "same"]) {
    test("message comments keep their captured account during deferred " + phase, async () => {
        const identity = requestTools(), imports = deferred(), upload = deferred(), queued = [], sent = [], notices = [];
        identity.setCurrentUser({ id: "alice" });
        const dialog = { feedbackTextState: () => ({ ok: true }), feedbackEntryBuild: (text) => ({ text }) };
        const ctx = { ...identity, _commentRequests: {}, _cbConvId: "conversation-A", _cbCommentFor: "message-A", _cbCommentDraft: "private A draft", _cbCommentBusy: false,
            $: () => null, cbRenderHistory: noop, cbMsgCommentText: (text) => text, toast: (text) => notices.push(text),
            __import: (name) => name === "#feedback_dialog_core" ? imports.promise : name.includes("feedback_core")
                ? Promise.resolve({ feedbackEnqueue: (owner, row) => { queued.push({ owner, row }); return 1; } })
                : Promise.resolve({ sendFeedback: (owner) => { sent.push(owner); return upload.promise; } }) };
        const api = functions("panel/board.js", ["_cbMsgCommentSend"], ctx);
        const request = api._cbMsgCommentSend({ id: "message-A", text: "A reply" });
        if (phase === "imports") {
            identity.setCurrentUser({ id: "bob" }); api.cbResetComments(); ctx._cbConvId = "conversation-B"; ctx._cbCommentDraft = "B draft";
        }
        imports.resolve(dialog); await settleIO();
        if (phase === "upload") {
            assert.equal(queued[0].owner, "alice"); assert.deepEqual(sent, ["alice"]);
            identity.setCurrentUser({ id: "bob" }); api.cbResetComments(); ctx._cbConvId = "conversation-B"; ctx._cbCommentDraft = "B draft";
        }
        upload.resolve(true); await request;
        if (phase === "imports") { assert.equal(queued.length, 0); assert.equal(sent.length, 0); }
        if (phase !== "same") { assert.equal(ctx._cbCommentDraft, "B draft"); assert.equal(notices.length, 0); }
        else { assert.equal(queued[0].owner, "alice"); assert.equal(ctx._cbCommentDraft, ""); assert.equal(notices.length, 1); }
    });
}

for (const changeView of [false, true]) {
    test("shared client actions await their exclusive claim" + (changeView ? " and stop after identity changes" : " before dispatch"), async () => {
        const claim = deferred(), dispatched = [];
        let current = true;
        const ctx = { _cbConvId: "A", getSharedConversation: async () => ({ id: "A", project_id: "" }), workspaceEnsureSelect: async () => true,
            flowFinish: noop, cbRestoreShared: noop, resetSubmitButton: noop, setPrelimBadge: noop,
            claimConversationClientAction: () => claim.promise,
            actDispatchPlanChain: async (plan) => dispatched.push(plan), dlqEnqueue: () => dispatched.push("download"),
            listSharedConversations: async () => [], notifyContextChanged: noop, replayTurnDone: noop, toast: noop,
            location: { origin: "https://example.invalid" }, URL };
        const api = functions("panel/board.js", ["cbSharedFinal"], ctx);
        const request = api.cbSharedFinal({ conv_id: "A", client_actions: [{ verb: "open-panel" }] }, { owner: "alice" }, "operation", () => current);
        await settleIO(); assert.equal(dispatched.length, 0, "a pending Promise is not a successful claim");
        if (changeView) current = false;
        claim.resolve(true); await request;
        assert.equal(dispatched.length, changeView ? 0 : 1);
    });
}

test("learning configuration epochs must be nonnegative safe integers before authorization", async () => {
    for (const epoch of [-1, null, true, "7", 1.5, Number.MAX_SAFE_INTEGER + 1]) {
        const h = rulesLifecycleHarness(); await h.load([], [], epoch);
        const request = h.api._learningPost("/control", { enabled: true });
        assert.equal(h.calls.length, 4, "invalid status epoch cannot dispatch a write"); await request;
    }
});

test("AI input and preset wiring enters the same authorization lifecycle immediately", () => {
    const input = source("core/interactions.js");
    assert.ok(input.includes('["input", "change"]') && input.includes('syncAiGates({ configurationChanged: true })'));
    const shell = source("core/shell.js");
    assert.ok(shell.includes('syncAiGates({ configurationChanged: force })'));
    assert.ok(shell.includes('notifyAiConfigurationChanged(options)'));
    const dream = source("panel/dream.js");
    assert.ok(dream.includes('setAiConfigurationChangedHook(releaseLearningConfiguration)'));
});

test("keyless release HTTP contract uses the old endpoint epoch and never modifies a switched owner", async () => {
    const { createServer } = await import("node:http");
    const previous = { owner: "alice", provider: "openai-compatible", base_url: "https://old.example.invalid/v1", model: "old-model" };
    const epochs = { alice: 7, bob: 11 }, requests = [], modified = [];
    let cookieOwner = "alice", releaseConflict = false, switchAfterRead = false;
    const server = createServer(async (request, response) => {
        const url = new URL(request.url, "http://127.0.0.1"), owner = request.headers["x-test-owner"];
        const send = (status, data) => { response.writeHead(status, { "Content-Type": "application/json" }); response.end(JSON.stringify(data)); };
        if (request.method === "GET" && url.pathname === "/api/learning/status") {
            requests.push({ method: "GET", owner, query: Object.fromEntries(url.searchParams) });
            if (["provider", "base_url", "model"].some((key) => url.searchParams.get(key) !== previous[key]) || url.searchParams.has("api_key")) {
                send(400, { ok: false, detail: "wrong old endpoint descriptor" }); return;
            }
            send(200, { ok: true, config_epoch: epochs[owner], scope: { owner, endpoint_fp: "old-endpoint" } });
            if (releaseConflict) { epochs[owner]++; releaseConflict = false; }
            if (switchAfterRead) { cookieOwner = "bob"; switchAfterRead = false; }
            return;
        }
        let text = ""; for await (const chunk of request) text += chunk;
        const body = JSON.parse(text || "{}"); requests.push({ method: "POST", owner, body });
        if (request.method !== "POST" || url.pathname !== "/api/learning/control" || body.action !== "release"
            || !Number.isSafeInteger(body.expected_config_epoch) || "api_key" in body) {
            send(400, { ok: false, detail: "learning_config_required" }); return;
        }
        if (owner !== body.expected_identity || epochs[owner] !== body.expected_config_epoch) {
            send(409, { ok: false, detail: "learning authorization conflict" }); return;
        }
        if (["provider", "base_url", "model"].some((key) => body[key] !== previous[key])) {
            send(400, { ok: false, detail: "wrong released endpoint" }); return;
        }
        modified.push(owner); epochs[owner]++;
        send(200, { ok: true, released: true, configured: false, endpoint_fp: "old-endpoint", config_epoch: epochs[owner] });
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const origin = "http://127.0.0.1:" + server.address().port;
    const identity = requestTools(); identity.setCurrentUser({ id: "alice" });
    const ctx = { ...identity, _learningConfigGeneration: 0, _learningStatusIdentity: null,
        _listRequests: {}, _learningRequests: {}, _candidateRequests: {}, _moreRequests: { candidate: {}, audit: {} }, _pendingToken: null,
        _learning: { configured: true, scope: { endpoint_fp: "new-endpoint" }, config_epoch: 999 },
        endpointCfg: () => ({ project_id: "new-project", base_url: "https://new.example.invalid/v1", model: "new-model" }),
        API: { learningControl: "/api/learning/control", learningStatus: "/api/learning/status" },
        URLSearchParams, rulesStatus: noop,
        fetch: (path, options = {}) => fetch(origin + path, { ...options, headers: { ...options.headers, "X-Test-Owner": cookieOwner } }) };
    const api = functions("panel/rules.js", ["scopeKey", "learningIdentity", "invalidateLearningConfiguration", "_get", "releaseLearningConfiguration"], ctx);
    try {
        assert.equal((await api.releaseLearningConfiguration(previous)).released, true);
        assert.deepEqual(requests.map((row) => row.method), ["GET", "POST"]);
        assert.equal(requests[1].body.expected_config_epoch, 7, "new endpoint epoch 999 must never authorize the old release");
        assert.equal(ctx._learning.configured, true, "old endpoint release cannot rewrite new endpoint status");
        releaseConflict = true;
        await assert.rejects(api.releaseLearningConfiguration(previous), /conflict/);
        assert.equal(modified.length, 1, "stale CAS is not silently retried");
        assert.equal((await api.releaseLearningConfiguration(previous)).released, true, "a new keyless read can retry revocation");
        switchAfterRead = true;
        await assert.rejects(api.releaseLearningConfiguration(previous), /conflict/);
        assert.equal(epochs.bob, 11); assert.deepEqual(modified, ["alice", "alice"]);
        identity.setCurrentUser({ id: "bob" });
        const count = requests.length;
        assert.equal((await api.releaseLearningConfiguration(previous)).skipped, true);
        assert.equal(requests.length, count);
        assert(!JSON.stringify(requests).includes("api_key"));
    } finally {
        server.closeAllConnections(); await new Promise((resolve) => server.close(resolve));
    }
});

function sharedRunLifecycleHarness() {
    const h = sharedRefreshHarness(), submit = element();
    h.ctx.$ = (id) => id === "queryInput" ? h.query : id === "chatInput" ? h.chat : id === "submitBtn" ? submit : null;
    h.ctx.currentProjectScope = () => "project_A";
    Object.assign(h.ctx, { _sharedRunRequestId: null, _askRelaxPending: null, _cbFrameSeq: 0,
        _cbStack: [], _cbCursor: -1, flowReset: noop, flowFinish: noop, flowPushEvent: noop,
        usageEnabled: () => false, cbChatInMain: () => true, cbLogPush: noop, getConfig: () => ({}),
        usageBeginTurn: noop, benchfbTurnBegin: noop, cbConvId: () => h.ctx._cbConvId,
        usageScope: () => "alice", cbProgressBegin: noop, startProgress: noop, estimateDuration: () => 0,
        recSeqNow: () => 0, ubNewReqId: () => "request-new", ubRouteBody: () => ({}),
        setPrelimBadge: noop, forgetConversationRun: noop, cbProgressDrop: noop,
        listSharedConversations: async () => [], replayTurnDone: noop, _fireCtxAfterSend: noop,
        autoGrow: noop });
    const names = ["cbRestoreShared", "cbRefreshSharedConversation", "cbSharedFinal", "ubSubmit", "cbRecoverRun"];
    return { ...h, submit, api: functions("panel/board.js", names, h.ctx) };
}

for (const start of ["submit", "recover"]) {
    test("a same-conversation old full poll cannot roll back a newer " + start + " final", async () => {
        const h = sharedRunLifecycleHarness();
        const poll = h.api.cbRefreshSharedConversation();
        h.io[0].resolve({ id: "A", revision: 2 }); await settleIO();
        assert.equal(h.io.length, 2, "the old poll is waiting for its full snapshot");
        const row = { owner: "alice", project_id: "project_A", shared: true,
            conversation_id: "A", request_id: "request-new", run_id: "run-new" };
        h.ctx.startConversationRun = h.ctx.awaitConversationRun = async () => ({ row, result: { ok: true, shared: true, conv_id: "A" } });
        const final = start === "submit" ? h.api.ubSubmit("chat", { text: "continue" }) : h.api.cbRecoverRun(row);
        await settleIO(); assert.equal(h.io.length, 3);
        h.io[2].resolve({ id: "A", revision: 3, current_state: { query: "new final" } }); await final;
        h.io[1].resolve({ id: "A", revision: 2, current_state: { query: "old poll" } }); await poll;
        assert.equal(h.ctx._sharedConversation.revision, 3, "revision and messages must never move backwards");
        assert.equal(h.ctx._sharedConversation.current_state.query, "new final");
        h.api.cbRestoreShared({ id: "A", revision: 1, current_state: { query: "obsolete direct restore" } }, { preserveDraft: true });
        assert.equal(h.ctx._sharedConversation.revision, 3, "all same-identity restore paths share the monotonic guard");
    });

    test("first shared " + start + " continuation preserves drafts typed after sending", async () => {
        const h = sharedRunLifecycleHarness(), result = deferred();
        h.ctx._sharedConversation = null; h.ctx._cbConvId = start === "submit" ? "temporary" : "";
        const row = { owner: "alice", project_id: "project_A", shared: true, conversation_id: "server",
            request_id: "request-new", run_id: "run-new", text: "sent text" };
        h.ctx.startConversationRun = h.ctx.awaitConversationRun = () => result.promise;
        const pending = start === "submit" ? h.api.ubSubmit("chat", { text: "sent text" }) : h.api.cbRecoverRun(row);
        h.query.value = "new unsent query"; h.chat.value = "new unsent chat";
        result.resolve({ row, result: { ok: true, shared: true, conv_id: "server" } }); await settleIO();
        h.io[0].resolve({ id: "server", revision: 1, project_id: "project_A", current_state: { query: "canonical sent text" } });
        await pending;
        assert.equal(h.ctx._cbConvId, "server"); assert.equal(h.query.value, "new unsent query"); assert.equal(h.chat.value, "new unsent chat");
        h.api.cbRestoreShared({ id: "different", revision: 1, current_state: { query: "different query" } }, { preserveDraft: true });
        assert.equal(h.query.value, "different query"); assert.equal(h.chat.value, "", "an actual conversation switch still clears the old drafts");
    });
}

test("shared final passes its request and displayed view guard into the lock claimant", async () => {
    const h = sharedRunLifecycleHarness(), row = { owner: "alice", project_id: "project_A", shared: true,
        conversation_id: "A", request_id: "request-new", run_id: "run-new" };
    let requestCurrent = true, claimCurrent;
    h.ctx.claimConversationClientAction = async (_row, _index, current) => { claimCurrent = current; return false; };
    const pending = h.api.cbSharedFinal({ conv_id: "A", client_actions: [{ verb: "open-panel" }] }, row, "sent", () => requestCurrent);
    h.io[0].resolve({ id: "A", revision: 2, current_state: {} }); await pending;
    assert.equal(typeof claimCurrent, "function"); assert.equal(claimCurrent(), true);
    h.ctx._cbConvId = "B"; assert.equal(claimCurrent(), false, "same-owner conversation changes must be checked inside Web Locks");
    h.ctx._cbConvId = "A"; requestCurrent = false; assert.equal(claimCurrent(), false, "superseded requests must be checked inside Web Locks");
});

function learningStatus(enabled = true, epoch = 7) {
    return { ok: true, configured: enabled, config_epoch: epoch, scope: { enabled, epoch: 1 } };
}
function completeLearningRefresh(h, start, enabled = true) {
    h.resolve(start, { ok: true, rules: [] }); h.resolve(start + 1, learningStatus(enabled));
    h.resolve(start + 2, { ok: true, items: [], next_offset: null });
    h.resolve(start + 3, { ok: true, items: [], next_offset: null });
}

test("Pause can submit after a successful learning POST while its four refresh reads are still pending", async () => {
    const h = rulesLifecycleHarness(); await h.load();
    const trigger = h.api._learningPost("/trigger", {});
    const busy = await h.api._learningPost("/control", { enabled: false });
    assert.equal(h.calls.length, 5); const busyNotice = h.notices.at(-1).text;
    h.resolve(4, learningStatus()); await settleIO();
    assert.equal(h.calls.length, 9);
    const pause = h.api._learningPost("/control", { enabled: false }); await settleIO();
    assert.equal(h.calls.length, 10, "refresh IO must not silently consume the Pause action");
    assert.equal(busy?.busy, true, "busy mutations must return an explicit result"); assert(busyNotice.includes("提交"));
    assert.equal(h.calls[9].body.enabled, false); assert.equal(h.calls[9].body.expected_config_epoch, 7);
    h.resolve(9, learningStatus(false)); await settleIO();
    assert.equal(h.api.read()._learning.scope.enabled, false, "a successful Pause response is trusted before auxiliary reads complete");
    completeLearningRefresh(h, 5, true); await trigger;
    assert.equal(h.api.read()._learning.scope.enabled, false, "old trigger refresh cannot cover the newer Pause");
    completeLearningRefresh(h, 10, false); await pause;
    assert.equal(h.api.read()._learning.scope.enabled, false);
});

for (const failedPart of [0, 2, 3]) {
    test("a trustworthy status enables Pause independently of auxiliary GET failure " + failedPart, async () => {
        const h = rulesLifecycleHarness(), refresh = h.api.renderCollabRules();
        h.resolve(failedPart, { ok: false, detail: "auxiliary read failed" }, false);
        await refresh;
        h.resolve(1, learningStatus()); await settleIO();
        for (const part of [0, 2, 3].filter((part) => part !== failedPart)) {
            h.resolve(part, part === 0 ? { ok: true, rules: [] } : { ok: true, items: [], next_offset: null });
        }
        await settleIO();
        assert.equal(h.api.read()._learning?.scope.enabled, true, "successful status must not be discarded with the audit/candidate reads");
        assert(h.walk(h.box).some((node) => node.textContent === "暂停学习"));
        const pause = h.api._learningPost("/control", { enabled: false });
        assert.equal(h.calls.length, 5); assert.equal(h.calls[4].body.expected_config_epoch, 7);
        h.resolve(4, { ok: false, detail: "test ends after verified dispatch" }, false); await pause;
    });
}

test("Pause remains fail-closed when no trustworthy status epoch was ever loaded", async () => {
    const h = rulesLifecycleHarness(), refresh = h.api.renderCollabRules();
    h.resolve(1, { ok: false, detail: "status unavailable" }, false);
    h.resolve(0, { ok: true, rules: [] }); h.resolve(2, { ok: true, items: [] }); h.resolve(3, { ok: true, items: [] });
    await refresh; await h.api._learningPost("/control", { enabled: false });
    assert.equal(h.calls.length, 4); assert(h.notices.at(-1).text.includes("刷新"));
});

test("a candidate feedback draft survives a full first-page refresh and reloading its absent second page", async () => {
    const h = rulesLifecycleHarness(); await h.load();
    async function secondPage() {
        const index = h.calls.length, page = h.api._learningMore("candidate");
        h.resolve(index, { ok: true, items: [{ id: "c2", record_type: "candidate", text: "second-page candidate" }], next_offset: null });
        await page;
        return h.walk(h.box).find((node) => node.id === "learningFeedback-c2");
    }
    (await secondPage()).value = "unfinished second-page feedback";
    await h.load();
    assert.equal(h.walk(h.box).some((node) => node.id === "learningFeedback-c2"), false, "full refresh temporarily returns to page one");
    assert.equal((await secondPage()).value, "unfinished second-page feedback");
    const reviewIndex = h.calls.length, review = h.api._learningPost("/review", { candidate_id: "c2", decision: "reject", feedback: "unfinished second-page feedback" });
    h.resolve(reviewIndex, { ok: true }); await settleIO(); completeLearningRefresh(h, reviewIndex + 1); await review;
    await h.load(); assert.equal((await secondPage()).value, "", "successful submission must clear that candidate's independent draft");
    h.walk(h.box).find((node) => node.id === "learningFeedback-c2").value = "private project draft";
    h.cfg.project_id = "another-project"; await h.load();
    h.cfg.project_id = "p"; await h.load();
    assert.equal((await secondPage()).value, "", "leaving the learning scope clears its draft instead of exposing it on a later round trip");
});

test("repeated same-descriptor AI edits reuse the pending in-flight release", async () => {
    const h = aiReleaseHarness();
    h.cfg.api_key = "changed synthetic key";
    h.shell.notifyAiConfigurationChanged({ configurationChanged: true }); await settleIO();
    const pending = Array.from(h.ctx._aiConfigurationPending.values())[0];
    h.cfg.api_key = "another changed synthetic key";
    h.shell.notifyAiConfigurationChanged({ configurationChanged: true }); await settleIO();
    assert.equal(h.requests.length, 1, "editing the same endpoint must not reset inFlight and duplicate a keyless release");
    assert.equal(Array.from(h.ctx._aiConfigurationPending.values())[0], pending);
    h.resolve(0); await settleIO(); assert.equal(h.ctx._aiConfigurationPending.size, 0);
});

test("restoring only the AI form keeps the old failed release intent retryable", async () => {
    const h = aiReleaseHarness();
    h.cfg.api_key = ""; h.cfg.use_llm = false;
    h.shell.notifyAiConfigurationChanged({ configurationChanged: true }); await settleIO();
    h.resolve(0, { ok: false, detail: "offline" }, 503); await settleIO();
    const pending = Array.from(h.ctx._aiConfigurationPending.values())[0];
    h.cfg.api_key = "restored synthetic key"; h.cfg.use_llm = true;
    h.shell.notifyAiConfigurationChanged({ configurationChanged: true }); await settleIO();
    assert.equal(h.requests.length, 2, "form availability is not a successful server authorization");
    assert.equal(Array.from(h.ctx._aiConfigurationPending.values())[0], pending);
    assert.equal(h.requests[1].body.action, "release");
    assert.equal(h.requests[1].body.expected_config_epoch, 7);
    assert.equal("api_key" in h.requests[1].body, false);
    h.resolve(1); await settleIO();
    assert.equal(h.ctx._aiConfigurationPending.size, 0);
    assert.equal(h.ctx._learning.configured, false);
});

for (const cleanupPending of [false, true]) {
    test("shared history deletion reports " + (cleanupPending ? "deferred file cleanup without restoring the tombstoned view" : "normal completion unchanged"), async () => {
        const row = reviewNode(), main = reviewNode("button"), del = reviewNode("button"), notices = [], actions = [];
        row.querySelector = (selector) => selector === ".hist-main" ? main : selector === ".hist-del" ? del : null;
        let active = "deleted", cleared = 0, removed = 0;
        const ctx = { ...requestTools(), _historyOpenRequests: {}, document: { createElement: () => row },
            workspaceConversationMenu: noop, escapeHtml: String, fmtTime: () => "time", toast: (text) => notices.push(text),
            COPY: { common: { delete: "delete" } }, armTwoStepConfirm: () => true,
            sharedConversationAction: async (id, action) => { actions.push([id, action]); return { ok: true, conversation: { id, deleted: true, cleanup_pending: cleanupPending } }; },
            forgetSharedConversationRuns: noop,
            cbSharedConversation: () => ({ id: active }), cbClear: () => { active = ""; cleared++; }, exitResultsLayout: noop,
            cbRefreshSharedConversation: async () => assert.fail("deleted conversations must not be reopened"),
            listSharedConversations: async () => [], renderHistory: noop, notifyContextChanged: noop };
        const api = functions("search/browse.js", ["createHistoryRow", "changeSharedHistory"], ctx);
        api.createHistoryRow([{ shared: true, convId: "deleted", revision: 2, chatOnly: true, query: "conversation", at: 1 }], { onDelete: () => removed++ });
        await del.listeners.click();
        assert.equal(cleared, 1); assert.equal(removed, 1); assert.deepEqual(actions, [["deleted", "delete"]]);
        if (cleanupPending) { assert(notices.at(-1).includes("无法访问")); assert(notices.at(-1).includes("后台清理")); }
        else assert.equal(notices.at(-1), "已删除这条对话");
    });
}

test("confirmed deletion survives a failed list refresh without summaries or restore receipts", async () => {
    const queue = await import("../../web/static/js/core/storage_queue.js"), identity = requestTools();
    identity.setCurrentUser({ id: "alice" });
    const values = new Map(), storage = { get length() { return values.size; }, key: (i) => Array.from(values.keys())[i] ?? null,
        getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) };
    const actions = [], notices = [], painted = [], row = reviewNode(), main = reviewNode("button"), del = reviewNode("button");
    const summary = { id: "deleted", revision: 2, frame_count: 0, message_count: 1, title: "conversation", project_id: "p", updated_at: "2026-01-01" };
    let allowList = true, active = "deleted", removed = 0;
    queue.queueWrite(storage, "pending::alice::run::", "original", { owner: "alice", project_id: "p", shared: true,
        conversation_id: "deleted", request_id: "original", run_id: "run-original", at: 1 });
    const client = browserModule("act/conversation_client.js", ["listSharedConversations", "sharedHistoryRows", "pendingConversationRuns", "forgetSharedConversationRuns", "sharedConversationAction"], {
        ...identity, ...queue, localStorage: storage, LS: { pendingRuns: "pending" }, nsKeyFor: (key, owner) => key + "::" + owner,
        currentProjectScope: () => "p", $: () => null,
        fetch: async (_url, options) => {
            if (options?.method === "POST") {
                actions.push(JSON.parse(options.body));
                return { ok: true, json: async () => ({ ok: true, conversation: { id: "deleted", deleted: true,
                    cleanup_pending: true, detail: "files pending" } }) };
            }
            if (!allowList) throw new Error("synthetic list failure");
            return { ok: true, json: async () => ({ ok: true, items: [summary], next_offset: null }) };
        } });
    await client.listSharedConversations();
    assert.equal(client.sharedHistoryRows().length, 1); assert.equal(client.pendingConversationRuns().length, 1);
    allowList = false;
    row.querySelector = (selector) => selector === ".hist-main" ? main : selector === ".hist-del" ? del : null;
    const api = functions("search/browse.js", ["createHistoryRow", "changeSharedHistory"], {
        ...identity, ...client, _historyOpenRequests: {}, document: { createElement: () => row },
        workspaceConversationMenu: noop, escapeHtml: String, fmtTime: () => "time", toast: (text) => notices.push(text),
        COPY: { common: { delete: "delete" } }, armTwoStepConfirm: () => true,
        cbSharedConversation: () => ({ id: active }), cbClear: () => { active = ""; }, exitResultsLayout: noop,
        cbRefreshSharedConversation: async () => assert.fail("a committed tombstone cannot reopen"),
        renderHistory: () => painted.push(client.sharedHistoryRows()), notifyContextChanged: noop });
    api.createHistoryRow([{ ...summary, shared: true, convId: "deleted", chatOnly: true, at: 1 }], { onDelete: () => removed++ });
    await del.listeners.click();
    assert.equal(actions.length, 1); assert.equal(actions[0].action, "delete");
    assert.equal(removed, 1, "list refresh failure cannot turn a confirmed deletion into false");
    assert.equal(active, ""); assert.equal(client.sharedHistoryRows().length, 0);
    assert.equal(client.pendingConversationRuns().length, 0);
    assert(painted.every((rows) => rows.length === 0));
    assert(notices.some((text) => text.includes("列表")));
    assert(notices.at(-1).includes("无法访问") && notices.at(-1).includes("后台清理"));
});

for (const outcome of ["lost acknowledgement", "restored authorization"]) {
    test("a managed keyless release retry cannot revoke a new authorization after " + outcome, async () => {
        const { createServer } = await import("node:http"), h = rulesLifecycleHarness(); await h.load();
        let epoch = 7, configured = true, failNextRelease = true;
        const requests = [], releases = [], completions = [];
        const server = createServer(async (request, response) => {
            const url = new URL(request.url, "http://127.0.0.1");
            const send = (status, data) => { response.writeHead(status, { "Content-Type": "application/json" }); response.end(JSON.stringify(data)); };
            if (request.method === "GET") {
                requests.push({ method: "GET", path: url.pathname, query: Object.fromEntries(url.searchParams) });
                if (url.pathname === "/status") send(200, { ...learningStatus(true, epoch), configured, scope: { enabled: true, owner: "alice" } });
                else if (url.pathname === "/rules") send(200, { ok: true, rules: [] });
                else send(200, { ok: true, items: [], next_offset: null });
                return;
            }
            let raw = ""; for await (const chunk of request) raw += chunk;
            const body = JSON.parse(raw); requests.push({ method: "POST", path: url.pathname, body });
            if (body.expected_identity !== "alice" || body.expected_config_epoch !== epoch) { send(409, { ok: false, detail: "learning authorization conflict" }); return; }
            if (body.action === "release") {
                assert.equal("api_key" in body, false); releases.push(body.expected_config_epoch);
                if (failNextRelease) {
                    failNextRelease = false;
                    if (outcome === "lost acknowledgement") { epoch++; configured = false; }
                    send(503, { ok: false, detail: "synthetic unavailable acknowledgement" }); return;
                }
                epoch++; configured = false;
                send(200, { ok: true, released: true, configured: false, config_epoch: epoch }); return;
            }
            configured = true; send(200, { ...learningStatus(true, epoch), configured });
        });
        await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
        const origin = "http://127.0.0.1:" + server.address().port;
        h.globals.fetch = (path, options) => fetch(origin + path, options);
        const shellContext = { ...h.identity, getConfig: () => h.cfg, registerContextChanged: noop, toast: noop,
            _aiConfigurationHook: null, _aiConfigurationContext: null, _aiConfigurationBound: false, _aiConfigurationPending: new Map() };
        const shell = functions("core/shell.js", ["aiConfigurationContext", "setAiConfigurationChangedHook", "notifyAiConfigurationChanged"], shellContext);
        shell.setAiConfigurationChangedHook((...args) => {
            const completion = h.api.releaseLearningConfiguration(...args); completions.push(completion); return completion;
        });
        try {
            h.cfg.api_key = "synthetic key edit";
            shell.notifyAiConfigurationChanged({ configurationChanged: true });
            await assert.rejects(completions[0], /unavailable acknowledgement/); await settleIO();
            assert.deepEqual(releases, [7]);
            configured = true;
            if (outcome === "restored authorization") {
                await h.api.renderCollabRules(); await h.api._learningPost("/trigger", {});
                assert.equal(h.api.read()._learning.configured, true);
            }
            shell.notifyAiConfigurationChanged();
            await completions.at(-1); await settleIO();
            assert.deepEqual(releases, [7], "retry must not fetch a fresh epoch and use it to revoke the new authorization");
            assert.equal(configured, true); assert.equal(shellContext._aiConfigurationPending.size, 0, "a terminal old intent is retired, not retried indefinitely");
            const keylessReads = requests.filter((row) => row.method === "GET" && row.path === "/status" && !row.query.project_id);
            assert(keylessReads.length >= 1); assert(keylessReads.every((row) => !("api_key" in row.query)));
        } finally {
            server.closeAllConnections(); await new Promise((resolve) => server.close(resolve));
        }
    });
}

test("the real board and locked client execute a resumed A action once after an undispatched view switch", async () => {
    const queue = await import("../../web/static/js/core/storage_queue.js");
    const values = new Map(), storage = { get length() { return values.size; }, key: (i) => Array.from(values.keys())[i] ?? null,
        getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: (key) => values.delete(key) };
    const h = sharedRunLifecycleHarness(), locks = [], dispatched = [];
    const client = browserModule("act/conversation_client.js", ["claimConversationClientAction"], {
        ...queue, localStorage: storage, LS: { pendingRuns: "pending" }, nsKeyFor: (key, owner) => key + "::" + owner,
        currentAccountScope: h.ctx.currentAccountScope, currentProjectScope: h.ctx.currentProjectScope, $: () => null,
        navigator: { locks: { request: (_key, _options, callback) => new Promise((resolve, reject) => {
            locks.push(() => { try { resolve(callback()); } catch (error) { reject(error); } });
        }) } } });
    const row = { owner: "alice", project_id: "project_A", shared: true, conversation_id: "A", request_id: "request-new", run_id: "run-new" };
    h.ctx.claimConversationClientAction = client.claimConversationClientAction;
    h.ctx.actDispatchPlanChain = async (plan) => dispatched.push(plan);
    const result = { ok: true, conv_id: "A", client_actions: [{ verb: "open-panel" }] };
    const old = h.api.cbSharedFinal(result, row, "open", () => true);
    h.io[0].resolve({ id: "A", revision: 2, current_state: {} }); await settleIO();
    assert.equal(locks.length, 1); h.ctx._cbConvId = "B"; locks.shift()();
    await assert.rejects(old, (error) => error.contextChanged);
    assert.equal(values.size, 0); assert.equal(dispatched.length, 0);
    h.ctx._cbConvId = "A";
    for (const revision of [3, 4]) {
        const next = h.api.cbSharedFinal(result, row, "open", () => true);
        h.io.at(-1).resolve({ id: "A", revision, current_state: {} }); await settleIO();
        locks.shift()(); await next;
    }
    assert.equal(dispatched.length, 1, "A is recoverable and its real client receipt prevents a second dispatch");
});

test("filling K2 during a paused keyless read releases K1 without authorizing K2", async () => {
    const { createServer } = await import("node:http"), h = aiReleaseHarness();
    const entered = deferred(), status = deferred(), completions = [], requests = [];
    let configuredKey = "K1", authorizations = 0;
    const server = createServer(async (request, response) => {
        const send = (data) => { response.writeHead(200, { "Content-Type": "application/json" }); response.end(JSON.stringify(data)); };
        if (request.method === "GET") {
            entered.resolve(); await status.promise;
            send({ ok: true, config_epoch: 7, scope: { owner: "owner-A" } }); return;
        }
        let raw = ""; for await (const chunk of request) raw += chunk;
        const body = JSON.parse(raw); requests.push(body);
        if (body.action === "release") {
            assert.equal(body.expected_identity, "owner-A"); assert.equal(body.expected_config_epoch, 7);
            assert.equal("api_key" in body, false); configuredKey = null;
            send({ ok: true, released: true, endpoint_fp: "ep-old", configured: false });
        } else {
            authorizations++; configuredKey = "K2"; send({ ok: true, configured: true });
        }
    });
    await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
    const origin = "http://127.0.0.1:" + server.address().port;
    h.ctx.fetch = (url, options) => fetch(origin + url, options);
    h.shell.setAiConfigurationChangedHook((...args) => {
        const completion = h.release.releaseLearningConfiguration(...args); completions.push(completion); return completion;
    });
    try {
        h.cfg.api_key = ""; h.cfg.use_llm = false;
        h.shell.notifyAiConfigurationChanged({ configurationChanged: true }); await entered.promise;
        const pending = Array.from(h.ctx._aiConfigurationPending.values())[0];
        h.cfg.api_key = "K2-synthetic-input"; h.cfg.use_llm = true;
        h.shell.notifyAiConfigurationChanged({ configurationChanged: true });
        status.resolve(); await completions[0]; await settleIO();
        assert.equal(requests.length, 1); assert.equal(requests[0].action, "release");
        assert.equal(configuredKey, null, "the old server-side K1 authorization must not survive a key edit");
        assert.equal(authorizations, 0, "typing K2 is not server authorization");
        assert.equal(JSON.stringify(requests).includes("K2-synthetic-input"), false);
        assert.equal(pending.inFlight, false); assert.equal(h.ctx._aiConfigurationPending.size, 0);
        assert.equal(h.ctx._learning.configured, false);
    } finally {
        status.resolve(); server.closeAllConnections(); await new Promise((resolve) => server.close(resolve));
    }
});

test("a managed release keeps its original generation even when its first keyless status read failed", async () => {
    const h = rulesLifecycleHarness(); await h.load();
    const originalFetch = h.globals.fetch, completions = [];
    h.globals.fetch = async () => { throw new Error("initial status unavailable"); };
    const ctx = { ...h.identity, getConfig: () => h.cfg, registerContextChanged: noop, toast: noop,
        _aiConfigurationHook: null, _aiConfigurationContext: null, _aiConfigurationBound: false, _aiConfigurationPending: new Map() };
    const shell = functions("core/shell.js", ["aiConfigurationContext", "setAiConfigurationChangedHook", "notifyAiConfigurationChanged"], ctx);
    shell.setAiConfigurationChangedHook((...args) => {
        const pending = h.api.releaseLearningConfiguration(...args); completions.push(pending); return pending;
    });
    h.cfg.api_key = "changed synthetic key"; shell.notifyAiConfigurationChanged({ configurationChanged: true });
    await assert.rejects(completions[0], /initial status unavailable/); await settleIO();
    h.globals.fetch = originalFetch; await h.load();
    const index = h.calls.length, authorize = h.api._learningPost("/trigger", {});
    h.resolve(index, learningStatus()); await settleIO(); completeLearningRefresh(h, index + 1); await authorize;
    const count = h.calls.length;
    shell.notifyAiConfigurationChanged(); await settleIO();
    assert.equal(h.calls.length, count, "an initial read failure must not erase the release intent's pre-restoration generation");
    await completions.at(-1); assert.equal(ctx._aiConfigurationPending.size, 0);
});

async function releaseCaptionHarness() {
    const h = rulesLifecycleHarness(), index = h.calls.length, load = h.api.renderCollabRules();
    h.resolve(index, { ok: true, rules: [] });
    h.resolve(index + 1, { ...learningStatus(true, 7), configured: true,
        scope: { enabled: true, owner: "alice", endpoint_fp: "ep-old" }, jobs: [{ id: "j-old", status: "running" }] });
    h.resolve(index + 2, { ok: true, items: [{ id: "c1", record_type: "candidate", text: "candidate" }], next_offset: 100 });
    h.resolve(index + 3, { ok: true, items: [], next_offset: null }); await load;
    const moreIndex = h.calls.length, more = h.api._learningMore("candidate");
    h.resolve(moreIndex, { ok: true, items: [{ id: "c2", record_type: "candidate", text: "page two" }], next_offset: null }); await more;
    h.walk(h.box).find((node) => node.id === "learningFeedback-c2").value = "page two feedback";
    const ctx = { ...h.identity, getConfig: () => h.cfg, registerContextChanged: noop, toast: noop,
        _aiConfigurationHook: null, _aiConfigurationContext: null, _aiConfigurationBound: false, _aiConfigurationPending: new Map() };
    const shell = functions("core/shell.js", ["aiConfigurationContext", "setAiConfigurationChangedHook", "notifyAiConfigurationChanged"], ctx);
    let returned = false;
    shell.setAiConfigurationChangedHook((...args) => h.api.releaseLearningConfiguration(...args).then((data) => { returned = true; return data; }));
    h.cfg.api_key = ""; h.cfg.use_llm = false;
    const releaseIndex = h.calls.length;
    shell.notifyAiConfigurationChanged({ configurationChanged: true }); await settleIO();
    assert.equal(h.calls[releaseIndex].body.action, "release");
    h.resolve(releaseIndex, { ok: true, released: true, configured: false, config_epoch: 8, endpoint_fp: "ep-old" });
    await settleIO();
    return { ...h, ctx, shell, releaseIndex, refreshIndex: releaseIndex + 1,
        returned: () => returned, text: () => h.walk(h.box).map((node) => node.textContent || "").join("\n") };
}

for (const refresh of ["hung", "failed", "fresh"]) {
    test("release acknowledgement renders known invalid configuration before " + refresh + " refresh", async () => {
        const h = await releaseCaptionHarness();
        assert(h.returned(), "release POST must finish independently of its automatic keyless reads");
        assert.equal(h.ctx._aiConfigurationPending.size, 0, "hung refresh cannot occupy shell.pending inFlight");
        assert(!h.text().includes("本次配置有效")); assert(h.text().includes("没有有效配置"));
        assert(h.walk(h.box).some((node) => node.textContent?.includes("学习任务 running") && node.textContent.includes("尚未刷新")));
        assert.equal(h.calls.length, h.refreshIndex + 4, "use the existing four-part refresh, not a new rendering/read channel");
        assert(h.calls.slice(h.refreshIndex).every((call) => !call.body && !call.url.includes("api_key")));
        if (refresh === "hung") {
            assert.equal(h.walk(h.box).find((node) => node.id === "learningFeedback-c2").value, "page two feedback"); return;
        }
        h.resolve(h.refreshIndex, { ok: true, rules: [] });
        h.resolve(h.refreshIndex + 1, refresh === "failed" ? { ok: false, detail: "status unavailable" } : {
            ...learningStatus(true, 8), configured: false, scope: { enabled: true, owner: "alice", endpoint_fp: "ep-old" },
            jobs: [{ id: "j-old", status: "paused", result: { error_code: "learning_config_released" } }] }, refresh !== "failed");
        h.resolve(h.refreshIndex + 2, { ok: true, items: [{ id: "c1", record_type: "candidate", text: "candidate" }], next_offset: 100 });
        h.resolve(h.refreshIndex + 3, { ok: true, items: [], next_offset: null }); await settleIO();
        assert(!h.text().includes("本次配置有效"));
        if (refresh === "failed") assert(h.text().includes("尚未刷新"));
        else { assert(h.text().includes("学习任务 paused")); assert(!h.text().includes("学习任务 running")); }
        const next = h.calls.length, page = h.api._learningMore("candidate");
        h.resolve(next, { ok: true, items: [{ id: "c2", record_type: "candidate", text: "page two" }], next_offset: null }); await page;
        assert.equal(h.walk(h.box).find((node) => node.id === "learningFeedback-c2").value, "page two feedback");
    });
}

for (const drift of ["account", "endpoint"]) {
    test("release status refresh cannot overwrite a new " + drift + " scope", async () => {
        const h = await releaseCaptionHarness();
        assert.equal(h.calls.length, h.refreshIndex + 4);
        if (drift === "account") h.identity.setCurrentUser({ id: "bob" });
        else h.cfg.base_url = "https://new.example.invalid/v1";
        const next = h.calls.length, load = h.api.renderCollabRules();
        h.resolve(next, { ok: true, rules: [] });
        h.resolve(next + 1, { ...learningStatus(true, 10), configured: true,
            scope: { enabled: true, owner: h.identity.currentAccountScope(), endpoint_fp: "ep-new" }, jobs: [{ id: "j-new", status: "done" }] });
        h.resolve(next + 2, { ok: true, items: [], next_offset: null }); h.resolve(next + 3, { ok: true, items: [], next_offset: null }); await load;
        h.resolve(h.refreshIndex, { ok: true, rules: [] });
        h.resolve(h.refreshIndex + 1, { ...learningStatus(true, 8), configured: false, jobs: [{ id: "j-old", status: "paused" }] });
        h.resolve(h.refreshIndex + 2, { ok: true, items: [{ id: "old-candidate" }], next_offset: null }); h.resolve(h.refreshIndex + 3, { ok: true, items: [] }); await settleIO();
        assert.equal(h.api.read()._learning.scope.endpoint_fp, "ep-new"); assert(h.text().includes("本次配置有效"));
        assert(!h.text().includes("学习任务 paused")); assert(!h.api.read()._persistent.some((row) => row.id === "old-candidate"));
    });
}
