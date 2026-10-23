"""Drive the workspace/browse/replay interaction review with the real Node runtime.

The embedded spec extracts the actual production functions from the shipped ES
modules (the same extraction seam as tests/js/frontend_lifecycle_spec.mjs) and runs
them against a minimal but behaviorally honest DOM harness. Focus, menu keyboard
handling, popover rebuilds, <select> value semantics and the replay seen-judgment
are exercised for real; this file never matches production source text as a proxy
for behavior.

BIODATA_NODE may override the Node binary; BIODATA_TEST_ROOT is set by this test to
the repository root before the spec runs. This file only spawns Node against
BIODATA_TEST_ROOT derived from its own __file__, so at the repository root it always
exercises the root production JS; there are no dataset_recommender Python imports to
pin (the ROOT module assertion pattern lives in tests/test_knowledge_gate.py and
tests/test_learning_selection.py).
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SPEC = r'''import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { pathToFileURL } from "node:url";
import test from "node:test";

const ROOT = process.env.BIODATA_TEST_ROOT;
assert.ok(ROOT, "BIODATA_TEST_ROOT must point at the repository root");
const source = (file) => readFileSync(ROOT + "/web/static/js/" + file, "utf8");
const wsc = await import(pathToFileURL(ROOT + "/web/static/js/core/workspaces_core.js").href);
const { workspaceHistoryGroups, workspaceConversationGroups, workspaceProjectOf,
    workspaceProjectColor, WORKSPACE_COPY } = wsc;

/* ---------- production source extraction (the repo's established seam) ---------- */
function sliceUnit(file, kind, name) {
    const src = source(file);
    const decl = kind === "function" ? "function " + name + "\\(" : "const " + name + " ";
    const re = new RegExp("^(?:export )?(?:async )?" + decl, "m");
    const match = re.exec(src);
    assert.ok(match, "Missing production " + kind + " " + name + " in " + file);
    const lineEnd = src.indexOf("\n", match.index);
    const head = src.slice(match.index, lineEnd).trimEnd();
    const singleLine = kind === "function" ? head.endsWith("}") : head.endsWith(";");
    const end = singleLine ? lineEnd : src.indexOf(kind === "function" ? "\n}" : "\n};", match.index);
    assert.ok(end > match.index, name + " has no parseable end in " + file);
    return src.slice(match.index, singleLine ? end : end + (kind === "function" ? 2 : 3)).replace(/^export /, "");
}
function extract(file, fnNames, constNames, globals) {
    const code = (constNames || []).map((n) => sliceUnit(file, "const", n)).join("\n")
        + "\n" + fnNames.map((n) => sliceUnit(file, "function", n)).join("\n")
        + "\n({" + fnNames.join(",") + "})";
    return runInNewContext(code, globals);
}
const escapeHtml = extract("core/core.js", ["escapeHtml"], [], {}).escapeHtml;
const fmtTime = extract("core/core.js", ["fmtTime"], [], {}).fmtTime;

/* ---------- minimal but behaviorally honest DOM ---------- */
const VOID_TAGS = new Set(["br", "input", "img", "hr"]);
function makeDom() {
    const byId = new Map();
    const doc = { activeElement: null, addEventListener() {}, _byId: byId };
    class El {
        constructor(tag) {
            this.tagName = String(tag).toUpperCase();
            this.parent = null;
            this.children = [];
            this.attributes = {};
            this.classes = new Set();
            this.listeners = {};
            this.dataset = {};
            this.style = { setProperty(k, v) { this[k] = v; } };
            this.disabled = false;
            this.hidden = false;
            this.inert = false;
            this.type = "";
            this.title = "";
            this.id = "";
            this._value = "";
        }
        get value() { return this._value == null ? "" : this._value; }
        set value(v) { this._value = v; }
        get isConnected() { return !!(this.parent && (this.parent === doc.body || this.parent.isConnected)); }
        get className() { return Array.from(this.classes).join(" "); }
        set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
        get classList() {
            const self = this;
            return {
                add: (x) => self.classes.add(x),
                remove: (x) => self.classes.delete(x),
                contains: (x) => self.classes.has(x),
                toggle: (x) => {
                    if (self.classes.has(x)) { self.classes.delete(x); return false; }
                    self.classes.add(x); return true;
                },
            };
        }
        setAttribute(k, v) {
            this.attributes[k] = String(v);
            if (k === "id") { this.id = String(v); byId.set(String(v), this); }
            if (k === "class") this.classes = new Set(String(v).split(/\s+/).filter(Boolean));
            if (k === "type") this.type = String(v);
            if (k === "disabled") this.disabled = true;
        }
        removeAttribute(k) { delete this.attributes[k]; }
        hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attributes, k); }
        getAttribute(k) { return this.attributes[k]; }
        append(...nodes) { nodes.forEach((n) => this.appendChild(n)); return this; }
        appendChild(node) {
            if (node == null) return node;
            if (typeof node === "string") node = makeText(node);
            if (node.parent) node.parent.removeChild(node);
            node.parent = this;
            this.children.push(node);
            return node;
        }
        insertBefore(node, ref) {
            if (typeof node === "string") node = makeText(node);
            if (node.parent) node.parent.removeChild(node);
            const i = ref ? this.children.indexOf(ref) : -1;
            node.parent = this;
            if (i < 0) this.children.push(node);
            else this.children.splice(i, 0, node);
            return node;
        }
        removeChild(node) {
            const i = this.children.indexOf(node);
            if (i >= 0) { this.children.splice(i, 1); node.parent = null; }
            if (doc.activeElement === node) doc.activeElement = null;
            return node;
        }
        remove() { if (this.parent) this.parent.removeChild(this); }
        contains(node) { for (let el = node; el; el = el.parent) if (el === this) return true; return false; }
        get firstChild() { return this.children[0] || null; }
        addEventListener(type, fn) { (this.listeners[type] || (this.listeners[type] = [])).push(fn); }
        getBoundingClientRect() { return { left: 0, top: 0, right: 200, bottom: 60, width: 200, height: 60 }; }
        focus() { doc.activeElement = this; }
        set innerHTML(html) { this.children = []; parseHtml(this, String(html)); }
        get innerHTML() { return this.children.map((c) => (c.nodeType === 3 ? c.textContent : c.outerHTML)).join(""); }
        set textContent(text) { this.children = [makeText(String(text))]; }
        get textContent() { return this.children.map((c) => c.textContent || "").join(""); }
        get outerHTML() {
            return "<" + this.tagName.toLowerCase() + ">" + this.innerHTML + "</" + this.tagName.toLowerCase() + ">";
        }
        querySelector(sel) { return findAll(this, sel)[0] || null; }
        querySelectorAll(sel) { return findAll(this, sel); }
        get options() { return this.children.filter((c) => c.nodeType !== 3 && c.tagName === "OPTION"); }
    }
    class SelectEl extends El {
        constructor(tag) { super(tag); this._selected = ""; }
        get value() {
            const chosen = this.options.find((o) => o.getAttribute("value") === this._selected);
            return chosen ? this._selected : "";
        }
        set value(v) {
            const val = String(v);
            this._selected = this.options.some((o) => o.getAttribute("value") === val) ? val : "";
        }
    }
    function makeText(text) {
        return { nodeType: 3, textContent: String(text), parent: null,
            get isConnected() { return !!(this.parent && this.parent.isConnected); } };
    }
    function makeElement(tag) {
        return String(tag).toUpperCase() === "SELECT" ? new SelectEl(tag) : new El(tag);
    }
    function parseHtml(root, html) {
        const stack = [root];
        const token = /<\/?([a-zA-Z][a-zA-Z0-9-]*)((?:\s+[a-zA-Z0-9-_:]+(?:="[^"]*")?)*)\s*\/?>|[^<]+/g;
        let m;
        while ((m = token.exec(html))) {
            if (m[0][0] !== "<") {
                if (m[0].length) stack[stack.length - 1].appendChild(m[0]);
                continue;
            }
            const close = m[0][1] === "/";
            const selfClose = m[0].endsWith("/>");
            const tag = m[1];
            if (close) { if (stack.length > 1) stack.pop(); continue; }
            const el = makeElement(tag);
            const attrRe = /([a-zA-Z0-9-_:]+)(?:="([^"]*)")?/g;
            let am;
            while ((am = attrRe.exec(m[2] || ""))) el.setAttribute(am[1], am[2] == null ? "" : am[2]);
            stack[stack.length - 1].appendChild(el);
            if (!selfClose && !VOID_TAGS.has(tag.toLowerCase())) stack.push(el);
        }
    }
    function compileSelector(sel) {
        const conds = { tags: [], classes: [], attrs: [], notDisabled: false };
        const re = /([a-zA-Z][a-zA-Z0-9-]*)|\.([a-zA-Z0-9_-]+)|\[([a-zA-Z0-9-]+)(?:="([^"]*)")?\]|:not\(:disabled\)/g;
        let m;
        while ((m = re.exec(sel))) {
            if (m[1]) conds.tags.push(m[1].toLowerCase());
            else if (m[2]) conds.classes.push(m[2]);
            else if (m[3]) conds.attrs.push([m[3], m[4] === undefined ? null : m[4]]);
            else conds.notDisabled = true;
        }
        return conds;
    }
    function matchesCond(node, conds) {
        if (!node || node.nodeType === 3 || !node.tagName) return false;
        if (conds.tags.length && !conds.tags.includes(node.tagName.toLowerCase())) return false;
        if (!conds.classes.every((c) => node.classes.has(c))) return false;
        if (!conds.attrs.every(([k, v]) => (v === null ? node.hasAttribute(k) : node.getAttribute(k) === v))) return false;
        if (conds.notDisabled && node.disabled) return false;
        return true;
    }
    function findAll(root, sel) {
        const conds = compileSelector(sel), out = [];
        const walk = (node) => (node.children || []).forEach((c) => { if (matchesCond(c, conds)) out.push(c); walk(c); });
        walk(root);
        return out;
    }
    doc.body = makeElement("div");
    doc.createElement = (tag) => makeElement(tag);
    return doc;
}
function fire(el, type, props = {}) {
    const ev = Object.assign({ type, target: el, currentTarget: el, key: "", clientX: 0, clientY: 0,
        preventDefault() { ev.defaultPrevented = true; }, stopPropagation() {} }, props);
    (el.listeners[type] || []).forEach((fn) => fn(ev));
    return ev;
}
async function fireAsync(el, type, props = {}) {
    const ev = Object.assign({ type, target: el, currentTarget: el, key: "", clientX: 0, clientY: 0,
        preventDefault() { ev.defaultPrevented = true; }, stopPropagation() {} }, props);
    for (const fn of el.listeners[type] || []) await fn(ev);
    return ev;
}

/* ---------- shared module contexts ---------- */
function workspacesCtx() {
    const dom = makeDom();
    return {
        document: dom, $: (id) => dom._byId.get(id) || null,
        _projects: [], _assignments: {}, _owner: null, _generation: 0,
        _loadFailed: false, _loading: false, _loaded: false,
        _hooks: {}, _menu: null, _popover: null, _popoverAnchor: null, _dismissTimer: null,
        _settingsResolve: null, _pinned: false, _restoringFocus: false,
        _dragProject: "", _orderPending: false, _pointerDrag: null, _dragClickSuppressed: false,
        _orderRequest: {},
        C: WORKSPACE_COPY, escapeHtml,
        workspaceHistoryGroups, workspaceConversationGroups, workspaceProjectOf, workspaceProjectColor,
        toast: () => {}, notifyContextChanged: () => {},
        currentAccountScope: () => "u1", currentProjectScope: () => "",
        learningTargetScope: () => "", setLearningTargetScope: () => {},
        getHist: () => [], sharedHistoryRows: () => [],
        beginRequest: () => ({ isCurrent: () => true }),
        readJSON: () => "", writeJSON: () => {}, nsKey: (k) => k, LS: { activeProject: "act" },
        setCurrentProject: () => {}, API: { conversationProjects: "/api/conversation-projects" },
        clearTimeout: () => {}, clearInterval: () => {}, setTimeout: (fn) => 0,
        innerWidth: 1200, innerHeight: 800, JSON, Promise,
    };
}
function replayCtx() {
    const dom = makeDom();
    return {
        document: dom, $: (id) => dom._byId.get(id) || null,
        _active: false, _bound: false,
        _graph: null, _points: null, _pointsToken: null,
        _fresh: new Set(), _freshVer: null,
        _badgeRequests: {}, _badgeTimer: null, _badgeTries: 0, _viewTimer: null,
        _busy: false, _chatStamp: -1, _logGetter: null,
        replayConvAuth: () => ({ conv_id: "c1", key: "", shared: false }),
        escapeHtml, mdLite: (t) => t, toast: () => {},
        beginRequest: () => ({ isCurrent: () => true }), invalidateRequest: () => {},
        llmOverrides: () => ({}), skipReasonZh: (k) => "skip:" + k,
        initReplayGraph: () => ({ reload: () => {}, viewState: () => null, openSnapshot: () => {} }),
        _setBadge: () => {}, loadPointsQuiet: async () => false, _markSeen: () => {},
        _post: async () => ({ ok: true }), _get: async () => ({}), _onOpen: () => {},
        setTimeout: (fn) => 0, clearTimeout: () => {}, JSON, Promise,
    };
}

/* ---------- workspaces ---------- */
test("archived learning target stays visible and selected; a ghost target falls back to global", () => {
    const ctx = workspacesCtx();
    const select = ctx.document.createElement("select");
    ctx.document._byId.set("memoryScopeSelect", select);
    ctx._owner = "u1";
    ctx._projects = [{ id: "A", name: "甲" }, { id: "B", name: "乙", archived: true }];
    ctx.learningTargetScope = () => "B";
    const mod = extract("panel/workspaces.js",
        ["renderLearningScopeSelect", "workspaceProjects"], [], ctx);
    mod.renderLearningScopeSelect();
    assert.equal(select.value, "B", "an archived target must stay selected, not silently switch");
    const optB = select.querySelector('option[value="B"]');
    assert.ok(optB, "archived projects stay visible in the scope select");
    assert.equal(optB.disabled, true, "the archived option is disabled but explicit");
    assert.ok(optB.textContent.includes(WORKSPACE_COPY.archived), "the archived marker is explicit");
    const optA = select.querySelector('option[value="A"]');
    assert.ok(optA && !optA.disabled, "active projects remain selectable");
    ctx.learningTargetScope = () => "ghost";
    mod.renderLearningScopeSelect();
    assert.equal(select.value, "", "a missing target falls back to global, never to another project");
});

test("workspace menu follows menu keyboard conventions and returns focus on Escape", () => {
    const ctx = workspacesCtx();
    const anchor = ctx.document.createElement("button");
    ctx.document.body.appendChild(anchor);
    const mod = extract("panel/workspaces.js",
        ["menuAt", "closeMenu", "closeWorkspacePopover", "clearPopoverDismiss"], [], ctx);
    const runs = [];
    mod.menuAt({ preventDefault() {}, clientX: 100, clientY: 100, currentTarget: anchor },
        ["一", "二", "三"].map((label, i) => ({ label, disabled: i === 1, run: () => runs.push(label) })));
    const menu = ctx._menu;
    assert.ok(menu, "menu opens");
    const buttons = menu.querySelectorAll('button[role="menuitem"]:not(:disabled)');
    assert.equal(buttons.length, 2, "disabled items are skipped");
    assert.equal(ctx.document.activeElement, buttons[0], "menu opens with the first enabled item focused");
    fire(menu, "keydown", { key: "ArrowDown" });
    assert.equal(ctx.document.activeElement, buttons[1]);
    fire(menu, "keydown", { key: "ArrowDown" });
    assert.equal(ctx.document.activeElement, buttons[0], "arrows wrap around");
    fire(menu, "keydown", { key: "ArrowUp" });
    assert.equal(ctx.document.activeElement, buttons[1]);
    fire(menu, "keydown", { key: "Home" });
    assert.equal(ctx.document.activeElement, buttons[0]);
    fire(menu, "keydown", { key: "End" });
    assert.equal(ctx.document.activeElement, buttons[1]);
    fire(menu, "keydown", { key: "Escape" });
    assert.equal(ctx._menu, null, "Escape closes the menu");
    assert.equal(ctx.document.activeElement, anchor, "Escape returns focus to the anchor");
});

test("async popover refresh keeps keyboard focus inside the rebuilt popover", () => {
    const ctx = workspacesCtx();
    const anchor = ctx.document.createElement("button");
    ctx.document.body.appendChild(anchor);
    ctx._owner = "u1";
    ctx._projects = [{ id: "A", name: "甲" }];
    ctx.listSharedConversations = () => Promise.resolve();
    ctx._hooks = { historyRow: () => ctx.document.createElement("div") };
    const mod = extract("panel/workspaces.js",
        ["refreshWorkspacePopover", "closeWorkspacePopover", "clearPopoverDismiss",
         "dismissWorkspacePopoverSoon", "showWorkspacePopover", "closeMenu",
         "workspaceProjects", "workspaceAssignments", "workspaceConversationProject",
         "currentConversation"],
        ["EDIT_ICON", "CLOSE_ICON"], ctx);
    mod.showWorkspacePopover(anchor, "A", { pin: true, refresh: false });
    const first = ctx._popover;
    assert.ok(first, "popover opens");
    const move = first.querySelector(".workspace-pop-move");
    move.focus();
    assert.equal(ctx.document.activeElement, move);
    mod.refreshWorkspacePopover();
    const second = ctx._popover;
    assert.ok(second && second !== first, "popover rebuilds");
    assert.ok(second.contains(ctx.document.activeElement), "focus stays inside the rebuilt popover");
    assert.equal(ctx.document.activeElement.disabled, false, "focus lands on an enabled control");
});

test("workspaceReload keeps last-known projects on transient failure and offers retry", async () => {
    const ctx = workspacesCtx();
    const nav = ctx.document.createElement("div");
    const select = ctx.document.createElement("select");
    ctx.document._byId.set("workspaceNav", nav);
    ctx.document._byId.set("memoryScopeSelect", select);
    ctx._owner = "u1";
    ctx._loaded = true;
    ctx._projects = [{ id: "A", name: "甲" }];
    const calls = [];
    let fail = true;
    ctx.fetch = async () => {
        if (fail) return { ok: false, json: async () => ({ ok: false, detail: "读取失败" }) };
        return { ok: true, json: async () => ({ ok: true,
            projects: [{ id: "A", name: "甲" }, { id: "B", name: "乙" }], assignments: {} }) };
    };
    ctx.setCurrentProject = (project) => calls.push(project ? project.id : "null");
    ctx.toast = () => calls.push("toast");
    ctx.projectNavigationButton = () => ctx.document.createElement("button");
    const mod = extract("panel/workspaces.js",
        ["workspaceReload", "request", "renderWorkspaceNavigation", "renderLearningScopeSelect",
         "workspaceProjects", "workspaceSelect", "workspaceProjectName",
         "closeWorkspacePopover", "clearPopoverDismiss"],
        ["CREATE_ICON"], ctx);
    await mod.workspaceReload();
    assert.deepEqual(ctx._projects.map((p) => p.id), ["A"], "transient failure keeps last-known projects");
    assert.equal(calls.includes("null"), false, "a transient failure must not clear the current project");
    assert.ok(calls.includes("toast"), "the user is told the read failed");
    assert.equal(ctx._loadFailed, true);
    const note = nav.querySelector(".workspace-nav-note");
    assert.ok(note, "the nav shows a failure note instead of a false-empty list");
    const retry = note.querySelector("button");
    assert.ok(retry, "the note offers a retry affordance");
    fail = false;
    fire(retry, "click");
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(ctx._loadFailed, false);
    assert.deepEqual(ctx._projects.map((p) => p.id), ["A", "B"], "retry restores the authoritative list");
    assert.equal(nav.querySelector(".workspace-nav-note"), null, "the note clears after a successful retry");
});

test("first load shows an explicit loading note instead of a false-empty nav", () => {
    const ctx = workspacesCtx();
    const nav = ctx.document.createElement("div");
    const select = ctx.document.createElement("select");
    ctx.document._byId.set("workspaceNav", nav);
    ctx.document._byId.set("memoryScopeSelect", select);
    ctx.fetch = () => new Promise(() => {});
    ctx.projectNavigationButton = () => ctx.document.createElement("button");
    const mod = extract("panel/workspaces.js",
        ["workspaceReload", "request", "renderWorkspaceNavigation", "renderLearningScopeSelect",
         "workspaceProjects", "workspaceSelect", "workspaceProjectName",
         "closeWorkspacePopover", "clearPopoverDismiss"],
        ["CREATE_ICON"], ctx);
    mod.workspaceReload();   // never settles; the synchronous part renders the loading state
    const note = nav.querySelector(".workspace-nav-note");
    assert.ok(note, "the loading state is visible before the first payload");
    assert.ok(note.textContent.includes("正在读取项目"));
});

/* ---------- browse ---------- */
test("shared conversation rename is an inline two-step editor and never window.prompt", async () => {
    const dom = makeDom();
    const submitSpy = [];
    const renderCalls = [];
    let failRename = true;
    const ctx = {
        document: dom, window: {},
        COPY: { common: { delete: "删除" } },
        escapeHtml, fmtTime,
        armTwoStepConfirm: () => false, workspaceConversationMenu: () => {},
        openPopup: () => {}, cbSaveConversation: () => {}, $: () => null, runRecommend: () => {},
        showView: () => {}, cbSharedConversation: () => null, openSharedHistory: async () => false,
        viewHistorySnapshot: () => {}, previewHistoryImport: async () => {}, deleteHistoryGroup: () => {},
        toast: () => {}, notifyContextChanged: () => {},
        _historyOpenRequests: {},
        beginRequest: () => ({ isCurrent: () => true }),
        sharedConversationAction: async (convId, action, revision, fields) => {
            submitSpy.push({ convId, action, revision, fields });
            if (failRename) throw new Error("服务端没接上");
            return { conversation: { title: fields.title } };
        },
        forgetSharedConversationRuns: () => {}, cbClear: () => {}, exitResultsLayout: () => {},
        cbRefreshSharedConversation: async () => {},
        listSharedConversations: async () => {},
        renderHistory: async (options) => renderCalls.push(options || null),
        location: { pathname: "/" }, Promise,
    };
    const mod = extract("search/browse.js",
        ["createHistoryRow", "changeSharedHistory"], [], ctx);
    const h = { convId: "c1", shared: true, query: "原始标题", at: 1700000000000, count: 2,
        message_count: 5, archived: false };
    const row = mod.createHistoryRow([h], {});
    dom.body.appendChild(row);
    const rename = row.querySelector(".hist-rename");
    assert.ok(rename, "shared rows expose rename");
    fire(rename, "click");
    const editor = row.querySelector(".hist-rename-editor");
    assert.ok(editor, "the inline editor opens instead of a native prompt");
    const input = editor.querySelector("input");
    assert.equal(input.value, h.query, "the editor starts from the current title");
    assert.equal(dom.activeElement, input, "the editor takes focus");
    const buttons = editor.querySelectorAll("button");
    assert.ok(buttons.some((b) => b.textContent === "保存")
        && buttons.some((b) => b.textContent === "取消"), "two-step confirm language matches the rest of the UI");
    fire(input, "keydown", { key: "Escape" });
    assert.equal(row.querySelector(".hist-rename-editor"), null, "Escape cancels without a request");
    assert.equal(dom.activeElement, rename, "cancel returns focus to the rename button");
    assert.equal(rename.hidden, false, "the original actions come back");
    assert.equal(submitSpy.length, 0);
    fire(rename, "click");
    const input2 = row.querySelector(".hist-rename-editor").querySelector("input");
    input2.value = "新标题";
    const saveBtn = row.querySelector(".hist-rename-editor").querySelectorAll("button")
        .find((b) => b.textContent === "保存");
    await fireAsync(saveBtn, "click");
    assert.equal(submitSpy.length, 1, "rename goes through the single shared-conversation channel");
    assert.equal(submitSpy[0].action, "rename");
    assert.equal(submitSpy[0].fields.title, "新标题");
    assert.equal(renderCalls.length, 0, "a failed rename must not wipe the draft by re-rendering");
    assert.equal(row.querySelector(".hist-rename-editor").querySelector("input").value, "新标题", "the draft survives the failure");
    assert.equal(saveBtn.disabled, false, "the editor is retryable");
    failRename = false;
    await fireAsync(saveBtn, "click");
    assert.equal(submitSpy.length, 2);
    assert.equal(submitSpy[1].fields.title, "新标题");
    assert.equal(renderCalls.length, 1, "a successful rename re-renders the list");
});

/* ---------- replay ---------- */
test("the seen judgment never fires when inactive, hidden, collapsed or on a snapshot", () => {
    const ctx = replayCtx();
    const pane = ctx.document.createElement("div");
    ctx.document._byId.set("sideReplayPane", pane);
    ctx._graph = { viewState: () => ({ tab: "current", digestVersion: 7 }) };
    const mod = extract("panel/replay.js",
        ["_displayedVersion", "_markSeen", "_setBadge"], ["RP"], ctx);
    assert.equal(mod._displayedVersion(), null, "an inactive pane is not seen");
    ctx._active = true;
    assert.equal(mod._displayedVersion(), 7);
    pane.hidden = true;
    assert.equal(mod._displayedVersion(), null, "a hidden window is not seen");
    pane.hidden = false;
    pane.classList.add("rp-graph-collapsed");
    assert.equal(mod._displayedVersion(), null, "a collapsed graph is not seen");
    pane.classList.remove("rp-graph-collapsed");
    ctx._graph = { viewState: () => ({ tab: "snapshot", digestVersion: 7 }) };
    assert.equal(mod._displayedVersion(), null, "a snapshot tab is not the live digest");
    ctx._graph = { viewState: () => ({ tab: "current", digestVersion: "7.x" }) };
    assert.equal(mod._displayedVersion(), null, "a non-integer digest version is not seen");
});

test("no seen request is sent while the graph is collapsed", async () => {
    const ctx = replayCtx();
    const pane = ctx.document.createElement("div");
    ctx.document._byId.set("sideReplayPane", pane);
    const badge = ctx.document.createElement("div");
    ctx.document._byId.set("replayNavBadge", badge);
    ctx._pointsToken = { isCurrent: () => true };
    ctx._points = { digest_version: 7, unseen: [1, 2] };
    ctx._graph = { viewState: () => ({ tab: "current", digestVersion: 7 }) };
    const posts = [];
    ctx._post = async (url, body) => { posts.push({ url, body }); return { ok: true }; };
    const mod = extract("panel/replay.js",
        ["_markSeen", "_displayedVersion", "_setBadge"], ["RP"], ctx);
    ctx._active = true;
    pane.classList.add("rp-graph-collapsed");
    await mod._markSeen();
    assert.equal(posts.length, 0, "collapsed = not seen = no request");
    pane.classList.remove("rp-graph-collapsed");
    await mod._markSeen();
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url, "/api/replay/seen", "the real seen endpoint");
    assert.equal(posts[0].body.observed_version, 7, "the version actually displayed is reported");
});

test("graph collapse is keyboard-safe and explicit", () => {
    const ctx = replayCtx();
    const pane = ctx.document.createElement("div");
    const col = ctx.document.createElement("div");
    const toggle = ctx.document.createElement("button");
    ctx.document._byId.set("sideReplayPane", pane);
    ctx.document._byId.set("replayGraphCol", col);
    ctx.document._byId.set("replayGraphToggle", toggle);
    const reloads = [], notes = [];
    ctx._graph = { reload: (points) => reloads.push(points) };
    ctx._points = { digest_version: 7, skip_reason: "no_key" };
    ctx._setNote = (text) => notes.push(text);
    let seen = 0;
    ctx._markSeen = () => { seen += 1; };
    const mod = extract("panel/replay.js", ["_toggleGraph", "_applyNote", "_graphCollapsed"], [], ctx);
    mod._toggleGraph();
    assert.equal(pane.classList.contains("rp-graph-collapsed"), true);
    assert.equal(col.inert, true, "the collapsed column leaves the keyboard tab order");
    assert.equal(toggle.getAttribute("aria-expanded"), "false");
    assert.equal(seen, 0, "collapsing is not seeing");
    assert.equal(notes.at(-1), "skip:no_key", "折叠后 skip 原因落到状态行（图谱内 .g-skip 已藏）");
    mod._toggleGraph();
    assert.equal(pane.classList.contains("rp-graph-collapsed"), false);
    assert.equal(col.inert, false);
    assert.equal(toggle.getAttribute("aria-expanded"), "true");
    assert.equal(reloads.length, 1, "expanding redraws with the live points");
    assert.equal(seen, 1, "expanding re-evaluates the seen judgment");
    assert.equal(notes.at(-1), "", "展开后状态行收起——skip 原因归图谱内 .g-skip 单处显示");
    const node = ctx.document.createElement("button");
    col.appendChild(node);
    node.focus();
    mod._toggleGraph();
    assert.equal(col.inert, true);
    assert.equal(ctx.document.activeElement, toggle, "focus escapes the hidden column");
});

test("replay wiring announces the send convention and labels the graph region", () => {
    const ctx = replayCtx();
    const pane = ctx.document.createElement("div");
    const composer = ctx.document.createElement("div");
    const send = ctx.document.createElement("button");
    const graph = ctx.document.createElement("div");
    ctx.document._byId.set("sideReplayPane", pane);
    ctx.document._byId.set("replayComposer", composer);
    ctx.document._byId.set("replaySendBtn", send);
    ctx.document._byId.set("replayGraph", graph);
    ctx.document.body.append(pane, composer, send, graph);
    let bound = 0, splitInit = 0, chatHint = 0;
    ctx._bindComposer = () => { bound += 1; };
    ctx._initSplit = () => { splitInit += 1; };
    ctx._panelSplit = () => {};
    ctx._renderChat = () => { chatHint += 1; };
    const mod = extract("panel/replay.js", ["initReplay"], [], ctx);
    mod.initReplay();
    assert.equal(composer.title, "Enter 换行，Ctrl+Enter 发送");
    assert.equal(composer.getAttribute("aria-description"), "Enter 换行，Ctrl+Enter 发送");
    assert.ok(send.title.includes("Ctrl+Enter"), "the send button states the convention");
    assert.equal(graph.getAttribute("role"), "region");
    assert.ok(graph.getAttribute("aria-label"), "the graph region has an explicit name");
    assert.equal(bound, 1, "the composer binds exactly once");
    assert.equal(splitInit, 1, "the split divider binds exactly once");
    assert.equal(pane.dataset.rpBound, "1");
    assert.equal(chatHint, 1, "首次进页签先落追问空态提示，不等 loadPoints 返回");
    mod.initReplay();
    assert.equal(bound, 1, "init is idempotent");
    assert.equal(splitInit, 1, "split init is idempotent");
    assert.equal(chatHint, 1, "init is idempotent（不重复渲染空态提示）");
});

/* 空态提示与气泡互斥：发送路径（replay.js _sendChat）本地先渲染泡、不清列——泡落下时提示必须让位。 */
test("the empty-chat hint yields to a locally rendered bubble", () => {
    const ctx = replayCtx();
    const list = ctx.document.createElement("div");
    ctx.document._byId.set("replayChat", list);
    ctx.CHAT_HINT = "针对本会话的图谱提问 / 质疑 / 学习——拥有主对话全部上下文";
    const mod = extract("panel/replay.js", ["_renderChat", "_bubble"], [], ctx);
    mod._renderChat([]);
    assert.equal(list.children.length, 1, "空态先落提示");
    assert.equal(list.children[0].className, "rp-side-hint");
    mod._bubble("user", "本地先渲染一句");
    assert.equal(list.children.some((c) => c.classes.has("rp-side-hint")), false, "落泡后提示让位（不许残留在消息上方）");
    assert.equal(list.children.at(-1).textContent, "本地先渲染一句");
});

process.on("exit", (code) => { if (code === 0) console.log("WORKSPACE_INTERACTION_REVIEW_OK"); });
'''


def test_workspace_interaction_review():
    node = os.environ.get("BIODATA_NODE") or shutil.which("node") or shutil.which("node.exe")
    if not node:
        pytest.skip("Node is unavailable")
    tmp = Path(tempfile.mkdtemp(prefix="ws-interaction-review-"))
    spec = tmp / "workspace_interaction_review.mjs"
    spec.write_text(SPEC, encoding="utf-8")
    env = dict(os.environ)
    env["BIODATA_TEST_ROOT"] = str(ROOT)
    try:
        result = subprocess.run(
            [node, str(spec)], cwd=ROOT, env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=120,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WORKSPACE_INTERACTION_REVIEW_OK" in result.stdout
    print(result.stdout)
