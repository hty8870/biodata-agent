"use strict";

import { $, API, LS, nsKey, readJSON, writeJSON, escapeHtml, toast, currentAccountScope,
    currentProjectScope, setCurrentProject, notifyContextChanged, registerContextChanged,
    learningTargetScope, setLearningTargetScope, getHist, beginRequest, invalidateRequest, setConversationProjectResolver } from "#core";
import { WORKSPACE_COPY as C, WORKSPACE_COLORS, workspaceProjectOf, workspaceProjectColor, workspaceColorValue,
    workspaceConversationGroups, workspaceHistoryGroups, workspaceReorderIds } from "#workspaces_core";
import { openModal, closeModal, trapModalFocus } from "#copy";
import { sharedHistoryRows, listSharedConversations, sharedConversationAction } from "#conversation_client";

let _projects = [], _assignments = {}, _owner = null, _generation = 0;
let _loadFailed = false, _loading = false, _loaded = false;
let _hooks = {}, _menu = null;
let _settingsResolve = null, _popover = null, _popoverAnchor = null, _dismissTimer = null;
let _pinned = false, _restoringFocus = false;
let _dragProject = "", _orderPending = false, _pointerDrag = null, _dragClickSuppressed = false;
const _orderRequest = {};
const EDIT_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 5 4 4M4 20l4-1L20 7a2.8 2.8 0 0 0-4-4L4 15l-1 6Z"/></svg>';
const CLOSE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>';
const CREATE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>';

function askProjectSettings(initial = {}, returnFocus = document.activeElement) {
    let modal = $("workspaceNameModal");
    if (!modal) {
        modal = document.createElement("div"); modal.id = "workspaceNameModal";
        modal.className = "modal-backdrop"; modal.hidden = true;
        modal.innerHTML = '<div class="modal workspace-name-modal" role="dialog" aria-modal="true" aria-labelledby="workspaceNameTitle">'
            + '<div class="modal-head"><h3 id="workspaceNameTitle"></h3></div>'
            + '<form id="workspaceNameForm"><p class="workspace-editor-hint" id="workspaceEditorHint"></p>'
            + '<div class="workspace-editor-preview"><span class="workspace-color-dot" aria-hidden="true"></span>'
            + '<div><span class="workspace-editor-eyebrow">' + C.preview + '</span><strong id="workspaceNamePreview"></strong></div></div>'
            + '<label class="workspace-field-label" for="workspaceNameInput">' + C.name + '</label>'
            + '<input id="workspaceNameInput" maxlength="80" required autocomplete="off" placeholder="' + C.placeholder + '">'
            + '<fieldset class="workspace-color-field"><legend>' + C.color + '</legend><div class="workspace-color-palette">'
            + WORKSPACE_COLORS.map((item) => '<button type="button" class="workspace-color-swatch" data-ws-color="' + item.value + '" aria-label="' + item.label + '"></button>').join("")
            + '</div><div class="workspace-custom-color"><label for="workspaceColorPicker">' + C.customColor + '</label>'
            + '<input id="workspaceColorPicker" type="color" aria-label="' + C.customColor + '">'
            + '<input id="workspaceColorHex" type="text" maxlength="7" pattern="#[0-9a-fA-F]{6}" aria-label="' + C.colorHex + '" required autocomplete="off" spellcheck="false"></div></fieldset>'
            + '<div class="workspace-name-actions"><button type="button" id="workspaceNameCancel" class="btn">' + C.cancel + '</button>'
            + '<button type="submit" class="btn btn-primary">' + C.save + '</button></div></form></div>';
        document.body.appendChild(modal);
        const finish = (value) => {
            _restoringFocus = true;
            try { closeModal(modal); } finally { _restoringFocus = false; }
            const resolve = _settingsResolve; _settingsResolve = null; if (resolve) resolve(value);
        };
        $("workspaceNameForm").addEventListener("submit", (event) => {
            event.preventDefault();
            if (!event.currentTarget.reportValidity()) return;
            const name = $("workspaceNameInput").value.trim();
            if (name) finish({ name, color: workspaceColorValue($("workspaceColorHex").value) });
        });
        $("workspaceNameCancel").addEventListener("click", () => finish(null));
        modal.addEventListener("click", (event) => { if (event.target === modal) finish(null); });
        modal.addEventListener("keydown", (event) => {
            if (event.key === "Escape") { event.preventDefault(); finish(null); } else trapModalFocus(event, modal);
        });
        modal.querySelectorAll("[data-ws-color]").forEach((button) => {
            button.style.setProperty("--project-color", button.dataset.wsColor);
            button.addEventListener("click", () => setEditorColor(button.dataset.wsColor));
        });
        $("workspaceNameInput").addEventListener("input", renderEditorPreview);
        $("workspaceColorPicker").addEventListener("input", (event) => setEditorColor(event.target.value));
        $("workspaceColorHex").addEventListener("input", () => {
            if (/^#[0-9a-fA-F]{6}$/.test($("workspaceColorHex").value)) renderEditorPreview();
        });
    }
    if (_settingsResolve) { _settingsResolve(null); _settingsResolve = null; }
    $("workspaceNameTitle").textContent = initial.id ? C.edit : C.create;
    $("workspaceEditorHint").textContent = initial.id ? C.editHint : C.createHint;
    $("workspaceNameInput").value = initial.name || "";
    setEditorColor(initial.color || WORKSPACE_COLORS[workspaceProjects().filter((p) => !p.archived).length % WORKSPACE_COLORS.length].value);
    closeWorkspacePopover();
    openModal(modal, { initialFocus: $("workspaceNameInput"), returnFocus });
    return new Promise((resolve) => { _settingsResolve = resolve; });
}

function setEditorColor(value) {
    $("workspaceColorHex").value = workspaceColorValue(value);
    renderEditorPreview();
}

function renderEditorPreview() {
    const color = workspaceColorValue($("workspaceColorHex").value);
    $("workspaceColorPicker").value = color;
    $("workspaceNamePreview").textContent = $("workspaceNameInput").value.trim() || C.placeholder;
    $("workspaceNameModal").querySelector(".workspace-editor-preview").style.setProperty("--project-color", color);
    $("workspaceNameModal").querySelectorAll("[data-ws-color]").forEach((button) => {
        button.setAttribute("aria-pressed", String(button.dataset.wsColor === color));
    });
}

export function setWorkspaceHooks(hooks) { _hooks = hooks || {}; }
export function workspaceProjects() { return _owner === currentAccountScope() ? _projects.slice() : []; }
export function workspaceAssignments() { return _owner === currentAccountScope() ? { ..._assignments } : {}; }
export function workspaceConversationProject(id, fallback) {
    if (fallback === undefined) {
        const row = [...sharedHistoryRows(), ...getHist()].find((h) => String(h.convId || "") === String(id || ""));
        fallback = row && row.project_id;
    }
    return workspaceProjectOf(id, workspaceAssignments(), fallback);
}
export function workspaceProjectName(id) {
    const p = workspaceProjects().find((v) => v.id === id);
    return p ? p.name : C.global;
}

async function request(url, options) {
    const owner = currentAccountScope(), token = beginRequest({});
    if (url.startsWith(API.conversationProjects)) url += (url.includes("?") ? "&" : "?") + "shared=true";
    const res = await fetch(url, { cache: "no-store", ...options });
    const body = await res.json().catch(() => ({}));
    if (!token.isCurrent() || owner !== currentAccountScope()) throw new Error("账户已切换，已忽略原账户的回应。");
    if (!res.ok || !body.ok) throw new Error(body.detail || "项目操作失败，请重试");
    return body;
}

function post(url, body, method) {
    return request(url, { method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}

export async function workspaceReload() {
    const owner = currentAccountScope(), generation = ++_generation;
    _loading = true;
    if (!_loaded) renderWorkspaceNavigation();
    try {
        const data = await request(API.conversationProjects);
        if (owner !== currentAccountScope() || generation !== _generation) return;
        _owner = owner; _projects = data.projects || []; _assignments = data.assignments || {};
        _loaded = true; _loading = false; _loadFailed = false;
        const saved = String(readJSON(nsKey(LS.activeProject), "") || "");
        const chosen = _projects.find((p) => p.id === saved && !p.archived);
        setCurrentProject(chosen || null, { notify: false });
        renderWorkspaceNavigation();
        notifyContextChanged("workspace-data");
    } catch (error) {
        if (owner !== currentAccountScope() || generation !== _generation) return;
        _loading = false; _loadFailed = true;
        if (_owner !== owner) {
            _owner = owner; _projects = []; _assignments = {};
            setCurrentProject(null, { notify: false });
        }
        renderWorkspaceNavigation();
        toast(error.message);
    }
}

export async function workspaceEnsureSelect(id, options = {}) {
    const owner = currentAccountScope();
    if (id && !workspaceProjects().some((p) => p.id === id)) await workspaceReload();
    if (owner !== currentAccountScope()) return false;
    return workspaceSelect(id, options);
}
export function workspaceSelect(id, options) {
    const project = id ? workspaceProjects().find((p) => p.id === id && (!p.archived || options?.allowArchived)) : null;
    if (id && !project) { toast("项目已归档，请先恢复，或把对话移出项目"); return false; }
    if (currentProjectScope() !== String(id || "")) {
        if (!(options && options.preserveConversation)) notifyContextChanged("before-project");
        setCurrentProject(project || null);
        try { writeJSON(nsKey(LS.activeProject), project ? project.id : ""); } catch (_e) {}
    }
    renderWorkspaceNavigation();
    return true;
}

export async function workspaceMoveConversation(conversationId, projectId) {
    const owner = currentAccountScope();
    const active = _hooks.sharedConversation?.();
    const shared = active?.id === conversationId ? { revision: active.revision }
        : sharedHistoryRows().find((row) => row.convId === conversationId);
    try {
        if (shared) await sharedConversationAction(conversationId, "project", shared.revision, { project_id: projectId || "" });
        else {
            const rows = getHist();
            if (!rows.some((row) => row.convId === conversationId)) throw new Error("这条本机对话尚未保存，请先结束当前回合。");
            writeJSON(nsKey(LS.hist), rows.map((row) => row.convId === conversationId ? { ...row, project_id: projectId || "" } : row));
        }
        if (owner !== currentAccountScope()) return false;
        _assignments[conversationId] = projectId || "";
        if (_hooks.conversationId && _hooks.conversationId() === conversationId) {
            workspaceSelect(projectId || "", { preserveConversation: true });
            if (shared) await _hooks.refreshConversation?.();
        } else renderWorkspaceNavigation();
        if (shared) await listSharedConversations();
        if (owner !== currentAccountScope()) return false;
        notifyContextChanged("membership");
        toast(projectId ? "已移入「" + workspaceProjectName(projectId) + "」" : "已移出项目");
        return true;
    } catch (error) {
        if (owner === currentAccountScope()) { toast(error.message); if (error.conflict) await listSharedConversations(); }
        return false;
    }
}

function closeMenu() { if (_menu) _menu.remove(); _menu = null; }
function menuAt(event, items) {
    event.preventDefault(); closeMenu(); closeWorkspacePopover();
    const anchor = event.currentTarget || null;
    const box = document.createElement("div"); box.className = "workspace-menu"; box.setAttribute("role", "menu");
    items.forEach((item) => {
        const button = document.createElement("button"); button.type = "button"; button.textContent = item.label;
        button.setAttribute("role", "menuitem"); button.disabled = !!item.disabled;
        button.addEventListener("click", () => { closeMenu(); Promise.resolve(item.run()).catch((error) => toast(error.message)); });
        box.appendChild(button);
    });
    box.addEventListener("keydown", (menuEvent) => {
        const buttons = [...box.querySelectorAll('button[role="menuitem"]:not(:disabled)')];
        const index = buttons.indexOf(document.activeElement);
        const focusAt = (next) => { if (buttons[next]) buttons[next].focus(); };
        if (menuEvent.key === "ArrowDown") { menuEvent.preventDefault(); focusAt(index < 0 ? 0 : (index + 1) % buttons.length); }
        else if (menuEvent.key === "ArrowUp") { menuEvent.preventDefault(); focusAt(index <= 0 ? buttons.length - 1 : index - 1); }
        else if (menuEvent.key === "Home") { menuEvent.preventDefault(); focusAt(0); }
        else if (menuEvent.key === "End") { menuEvent.preventDefault(); focusAt(buttons.length - 1); }
        else if (menuEvent.key === "Escape" || menuEvent.key === "Tab") {
            menuEvent.preventDefault(); closeMenu();
            const target = anchor && (anchor.tagName === "BUTTON"
                ? anchor : (anchor.querySelector ? anchor.querySelector("button") : null));
            if (target && typeof target.focus === "function") target.focus({ preventScroll: true });
        }
    });
    document.body.appendChild(box); _menu = box;
    const rect = box.getBoundingClientRect();
    const anchorRect = event.currentTarget && event.currentTarget.getBoundingClientRect ? event.currentTarget.getBoundingClientRect() : { left: 12, top: 12 };
    box.style.left = Math.max(8, Math.min(event.clientX || anchorRect.left, innerWidth - rect.width - 8)) + "px";
    box.style.top = Math.max(8, Math.min(event.clientY || anchorRect.top, innerHeight - rect.height - 8)) + "px";
    box.querySelector("button:not(:disabled)")?.focus();
}

export function workspaceConversationMenu(event, conversationId) {
    const current = workspaceConversationProject(conversationId);
    const projects = workspaceProjects().filter((p) => !p.archived);
    menuAt(event, [
        ...projects.map((p) => ({ label: C.moveTo + "「" + p.name + "」", disabled: current === p.id,
            run: () => workspaceMoveConversation(conversationId, p.id) })),
        { label: C.moveOut, disabled: !current, run: () => workspaceMoveConversation(conversationId, "") },
        { label: C.create, run: () => createProject(conversationId) }
    ]);
}

async function createProject(conversationId) {
    const owner = currentAccountScope();
    const settings = await askProjectSettings();
    if (!settings) return;
    if (owner !== currentAccountScope()) return;
    const data = await post(API.conversationProjects, settings);
    if (owner !== currentAccountScope()) return;
    _projects.push(data.project); _owner = owner;
    renderWorkspaceNavigation(); notifyContextChanged("workspace-data");
    if (conversationId) await workspaceMoveConversation(conversationId, data.project.id);
    else workspaceSelect(data.project.id);
}

async function editProject(project, returnFocus) {
    const owner = currentAccountScope();
    const settings = await askProjectSettings(project, returnFocus);
    if (!settings || (settings.name === project.name && settings.color === workspaceProjectColor(project))) return;
    if (owner !== currentAccountScope()) return;
    const data = await post(API.conversationProjects + "/" + encodeURIComponent(project.id), settings, "PATCH");
    if (owner !== currentAccountScope()) return;
    _projects = _projects.map((p) => p.id === project.id ? data.project : p);
    // Appearance edits preserve project identity, the conversation and the selected learning target.
    renderWorkspaceNavigation(); notifyContextChanged("workspace-data");
}

async function archiveProject(project, archived) {
    const owner = currentAccountScope();
    const data = await post(API.conversationProjects + "/" + encodeURIComponent(project.id) + "/archive", { archived });
    if (owner !== currentAccountScope()) return;
    _projects = _projects.map((p) => p.id === project.id ? data.project : p);
    if (archived && currentProjectScope() === project.id) workspaceSelect("");
    if (archived && learningTargetScope() === project.id) {
        toast("「" + project.name + "」已归档；学习范围仍指向它，可在记忆设置里改选");
    }
    renderWorkspaceNavigation(); notifyContextChanged("workspace-data");
}

function projectMenu(event, project) {
    const anchor = event.currentTarget;
    menuAt(event, project.archived
        ? [{ label: C.restore, run: () => archiveProject(project, false) }]
        : [{ label: C.edit, run: () => editProject(project, anchor) }, { label: C.archive, run: () => archiveProject(project, true) }]);
}

function clearPopoverDismiss() { clearTimeout(_dismissTimer); _dismissTimer = null; }

function closeWorkspacePopover(returnFocus = false) {
    clearPopoverDismiss();
    const anchor = _popoverAnchor;
    if (anchor) anchor.setAttribute("aria-expanded", "false");
    if (_popover) _popover.remove();
    _popover = null; _popoverAnchor = null; _pinned = false;
    if (returnFocus && anchor && anchor.isConnected) {
        _restoringFocus = true;
        try { anchor.focus({ preventScroll: true }); } finally { _restoringFocus = false; }
    }
}

function dismissWorkspacePopoverSoon() {
    clearPopoverDismiss();
    if (_pinned) return;
    _dismissTimer = setTimeout(() => {
        if (!_popover || _popover.contains(document.activeElement) || _popoverAnchor?.contains(document.activeElement)) return;
        closeWorkspacePopover();
    }, 180);
}

function currentConversation() {
    const id = _hooks.conversationId ? String(_hooks.conversationId() || "") : "";
    const hasContent = _hooks.hasConversation ? _hooks.hasConversation() : getHist().some((h) => h.convId === id);
    return { id, available: !!id && !!hasContent };
}

function refreshWorkspacePopover() {
    if (!_popover || !_popoverAnchor) return;
    const anchor = _popoverAnchor, id = _popover.dataset.projectId || "", pin = _pinned;
    const restoreFocus = _popover.contains(document.activeElement);
    closeWorkspacePopover();
    showWorkspacePopover(anchor, id, { pin, refresh: false, focus: restoreFocus });
}

function clearProjectDrag() {
    _dragProject = ""; _pointerDrag = null; document.body.classList.remove("workspace-reordering");
    $("workspaceNav")?.querySelectorAll(".is-dragging,.drop-before,.drop-after").forEach((button) => {
        button.classList.remove("is-dragging", "drop-before", "drop-after");
    });
}

async function saveProjectOrder(ids) {
    if (_orderPending) return;
    const token = beginRequest(_orderRequest, currentAccountScope);
    _orderPending = true; $("workspaceNav")?.setAttribute("aria-busy", "true"); closeWorkspacePopover();
    try {
        const data = await post(API.conversationProjects + "/reorder", { project_ids: ids });
        if (!token.isCurrent()) return;
        _generation++; _projects = data.projects || []; _owner = currentAccountScope();
        renderWorkspaceNavigation(); notifyContextChanged("workspace-data"); toast(C.orderSaved);
    } catch (error) {
        if (token.isCurrent()) { toast(error.message); await workspaceReload(); }
    } finally {
        if (token.isCurrent()) { _orderPending = false; $("workspaceNav")?.removeAttribute("aria-busy"); }
    }
}

function showWorkspacePopover(anchor, projectId, { pin = false, focus = false, refresh = true } = {}) {
    if (_restoringFocus || _dragProject) return;
    clearPopoverDismiss(); closeMenu();
    if (_popover && _popoverAnchor === anchor) {
        _pinned = _pinned || pin;
        if (focus) _popover.querySelector("button:not(:disabled)")?.focus({ preventScroll: true });
        return;
    }
    closeWorkspacePopover();
    const project = projectId ? workspaceProjects().find((p) => p.id === projectId) : null;
    if (projectId && !project) return;
    const name = project ? project.name : C.global;
    const bucket = workspaceHistoryGroups(workspaceConversationGroups([...sharedHistoryRows(), ...getHist()]), workspaceProjects(), workspaceAssignments())
        .find((group) => group.id === projectId);
    const conversations = bucket ? bucket.conversations : [];
    const current = currentConversation();
    const assigned = current.available && workspaceConversationProject(current.id) === projectId;
    const archived = !!(project && project.archived);
    const box = document.createElement("section");
    box.id = "workspacePopover"; box.className = "workspace-popover";
    box.dataset.projectId = projectId;
    box.setAttribute("role", "dialog"); box.setAttribute("aria-label", name);
    box.style.setProperty("--project-color", workspaceProjectColor(project));
    box.innerHTML = '<div class="workspace-pop-head"><span class="workspace-color-dot" aria-hidden="true"></span>'
        + '<div class="workspace-pop-heading"><div class="workspace-pop-meta">' + (archived ? C.archived + ' · ' : '')
        + conversations.length + ' ' + C.conversations + '</div><h3><button type="button" class="workspace-pop-title"'
        + (archived ? ' disabled' : '') + ' aria-label="' + escapeHtml((project ? C.switchProject : C.switchGlobal) + '：' + name) + '">' + escapeHtml(name) + '</button></h3></div>'
        + '<div class="workspace-pop-tools">' + (project ? '<button type="button" class="workspace-icon-button" data-ws-edit aria-label="' + (archived ? C.restore : C.edit) + '">' + EDIT_ICON + '</button>' : '')
        + '<button type="button" class="workspace-icon-button" data-ws-close aria-label="' + C.close + '">' + CLOSE_ICON + '</button></div></div>'
        + '<div class="workspace-pop-list"></div><div class="workspace-pop-footer"><button type="button" class="btn btn-primary workspace-pop-move">'
        + (project ? C.moveCurrent : C.moveCurrentGlobal) + '</button><p class="workspace-pop-hint"></p></div>';
    const list = box.querySelector(".workspace-pop-list");
    if (!conversations.length) {
        list.innerHTML = '<div class="workspace-pop-empty"><span aria-hidden="true">▤</span><strong>' + C.emptyConversations + '</strong><p>' + C.emptyHint + '</p></div>';
    }
    conversations.forEach((rows) => {
        const row = rows[0] || {};
        const item = _hooks.historyRow(rows, { onOpen: closeWorkspacePopover, onDelete: refreshWorkspacePopover });
        if (row.convId === current.id) item.classList.add("is-current");
        list.appendChild(item);
    });
    const move = box.querySelector(".workspace-pop-move");
    move.disabled = !current.available || assigned || archived;
    box.querySelector(".workspace-pop-hint").textContent = archived ? C.archivedMoveHint
        : !current.available ? C.noConversation : assigned ? (project ? C.alreadyCurrent : C.alreadyGlobal) : "";
    move.addEventListener("click", async () => {
        const selected = currentConversation();
        if (!selected.available) return;
        move.disabled = true;
        const moved = await workspaceMoveConversation(selected.id, projectId);
        if (moved) closeWorkspacePopover();
        else if (_popover === box) move.disabled = false;
    });
    box.querySelector(".workspace-pop-title").addEventListener("click", () => { closeWorkspacePopover(); workspaceSelect(projectId); });
    box.querySelector("[data-ws-edit]")?.addEventListener("click", () => {
        closeWorkspacePopover();
        Promise.resolve(archived ? archiveProject(project, false) : editProject(project, anchor)).catch((error) => toast(error.message));
    });
    box.querySelector("[data-ws-close]").addEventListener("click", () => closeWorkspacePopover(true));
    box.addEventListener("pointerenter", clearPopoverDismiss);
    box.addEventListener("pointerleave", dismissWorkspacePopoverSoon);
    document.body.appendChild(box);
    _popover = box; _popoverAnchor = anchor; _pinned = pin;
    anchor.setAttribute("aria-expanded", "true");
    const rect = anchor.getBoundingClientRect(), bounds = box.getBoundingClientRect();
    let left = rect.right + 12;
    if (left + bounds.width > innerWidth - 12) left = rect.left - bounds.width - 12;
    box.style.left = Math.max(12, Math.min(left, innerWidth - bounds.width - 12)) + "px";
    box.style.top = Math.max(12, Math.min(rect.top - 8, innerHeight - bounds.height - 12)) + "px";
    if (focus) box.querySelector("button:not(:disabled)")?.focus({ preventScroll: true });
    if (refresh) {
        const token = beginRequest({});
        listSharedConversations().then(() => {
            if (token.isCurrent() && _popover === box) refreshWorkspacePopover();
        }).catch((error) => {
            if (token.isCurrent() && _popover === box) box.querySelector(".workspace-pop-hint").textContent = error.message;
        });
    }
}

function projectNavigationButton(project) {
    const id = project ? project.id : "", name = project ? project.name : C.global;
    const button = document.createElement("button"); button.type = "button";
    button.className = "workspace-nav-item" + (currentProjectScope() === id ? " active" : "");
    button.dataset.wsId = id; button.setAttribute("aria-label", name);
    button.setAttribute("aria-pressed", String(currentProjectScope() === id));
    button.setAttribute("aria-haspopup", "dialog"); button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", "workspacePopover");
    button.style.setProperty("--project-color", workspaceProjectColor(project));
    button.innerHTML = '<span class="workspace-color-dot" aria-hidden="true"></span><span class="workspace-nav-label">' + escapeHtml(name) + '</span>';
    if (project && !project.archived) {
        button.dataset.wsSortable = "true";
        button.setAttribute("aria-description", C.reorderHint);
        button.insertAdjacentHTML("beforeend", '<span class="workspace-drag-grip" aria-hidden="true">⠿</span>');
        button.addEventListener("pointerdown", (event) => {
            if (event.button !== 0 || _orderPending) return;
            _pointerDrag = { id, button, pointerId: event.pointerId, x: event.clientX, y: event.clientY, target: "", after: false };
            button.setPointerCapture(event.pointerId);
        });
    }
    button.addEventListener("pointerenter", (event) => { if (event.pointerType !== "touch") showWorkspacePopover(button, id); });
    button.addEventListener("pointerleave", dismissWorkspacePopoverSoon);
    button.addEventListener("focus", () => showWorkspacePopover(button, id));
    button.addEventListener("click", (event) => {
        if (_dragClickSuppressed) { event.preventDefault(); return; }
        if (_popoverAnchor === button && _pinned) { closeWorkspacePopover(); return; }
        showWorkspacePopover(button, id, { pin: true, focus: event.detail === 0 });
    });
    button.addEventListener("keydown", (event) => {
        if (project && !project.archived && event.altKey && ["ArrowUp", "ArrowDown"].includes(event.key)) {
            event.preventDefault();
            const ids = workspaceProjects().filter((p) => !p.archived).map((p) => p.id), index = ids.indexOf(id);
            const direction = event.key === "ArrowUp" ? -1 : 1, target = ids[index + direction];
            if (target) saveProjectOrder(workspaceReorderIds(ids, id, target, direction > 0));
            return;
        }
        if (event.key === "ArrowRight") { event.preventDefault(); showWorkspacePopover(button, id, { pin: true, focus: true }); }
        if (project && (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10"))) projectMenu(event, project);
    });
    if (project) button.addEventListener("contextmenu", (event) => projectMenu(event, project));
    return button;
}

export function renderLearningScopeSelect() {
    const select = $("memoryScopeSelect"); if (!select) return;
    const selected = learningTargetScope();
    const projects = workspaceProjects();
    const active = projects.filter((p) => !p.archived), archived = projects.filter((p) => p.archived);
    const option = (p, disabled) => '<option value="' + escapeHtml(p.id) + '"' + (disabled ? " disabled" : "") + '>'
        + escapeHtml(disabled ? p.name + "（" + C.archived + "）" : p.name) + '</option>';
    select.innerHTML = '<option value="">' + C.learningGlobal + '</option>'
        + active.map((p) => option(p, false)).join("") + archived.map((p) => option(p, true)).join("");
    select.value = selected;
}

export function renderWorkspaceNavigation() {
    const root = $("workspaceNav"); if (!root) return;
    closeWorkspacePopover();
    root.innerHTML = '<div class="workspace-nav-head"><span>' + C.title + '</span></div><div class="workspace-nav-list"></div>';
    const list = root.querySelector(".workspace-nav-list");
    if (_loading && (!_loaded || !_projects.length)) {
        const note = document.createElement("div"); note.className = "muted-block workspace-nav-note";
        note.textContent = "正在读取项目…"; list.appendChild(note);
    } else if (_loadFailed) {
        const note = document.createElement("div"); note.className = "muted-block workspace-nav-note";
        note.textContent = "项目暂时读不出来。";
        const retry = document.createElement("button"); retry.type = "button"; retry.className = "btn workspace-nav-retry";
        retry.textContent = "重试";
        retry.addEventListener("click", () => { workspaceReload().catch(() => {}); });
        note.appendChild(retry); list.appendChild(note);
    }
    const active = workspaceProjects().filter((p) => !p.archived), archived = workspaceProjects().filter((p) => p.archived);
    active.forEach((project) => {
        list.appendChild(projectNavigationButton(project));
    });
    if (archived.length) {
        const details = document.createElement("details"); details.className = "workspace-archived";
        const summary = document.createElement("summary"); summary.textContent = C.archived + " " + archived.length; details.appendChild(summary);
        archived.forEach((project) => {
            details.appendChild(projectNavigationButton(project));
        }); root.appendChild(details);
    }
    root.appendChild(projectNavigationButton(null));
    const create = document.createElement("button"); create.type = "button";
    create.className = "workspace-nav-item workspace-create-item"; create.dataset.wsCreate = "";
    create.setAttribute("aria-label", C.create);
    create.innerHTML = '<span class="workspace-create-mark" aria-hidden="true">' + CREATE_ICON + '</span><span class="workspace-nav-label">' + C.create + '</span>';
    root.appendChild(create);
    renderLearningScopeSelect();
}

function ensureLegacyIds() {
    const rows = getHist(); let changed = false;
    rows.forEach((row) => {
        if (!row.convId) { row.convId = "legacy_" + (crypto.randomUUID ? crypto.randomUUID().replaceAll("-", "") : Date.now().toString(36) + Math.random().toString(36).slice(2)); changed = true; }
    });
    if (changed) { try { writeJSON(nsKey(LS.hist), rows); } catch (_e) { toast("历史编号没能保存，请检查浏览器存储空间"); } }
}

export async function initWorkspaces() {
    setConversationProjectResolver(workspaceConversationProject);
    ensureLegacyIds();
    const root = $("workspaceNav");
    if (root) root.addEventListener("click", (event) => {
        const create = event.target.closest("[data-ws-create]"); if (create) { createProject().catch((error) => toast(error.message)); return; }
    });
    document.addEventListener("pointerdown", (event) => {
        if (_menu && !_menu.contains(event.target)) closeMenu();
        if (_popover && !_popover.contains(event.target) && !_popoverAnchor?.contains(event.target)) closeWorkspacePopover();
    });
    document.addEventListener("focusin", (event) => {
        if (_popover && !_popover.contains(event.target) && !_popoverAnchor?.contains(event.target)) closeWorkspacePopover();
    });
    document.addEventListener("scroll", (event) => { if (_popover && !_popover.contains(event.target)) closeWorkspacePopover(); }, true);
    document.addEventListener("pointermove", (event) => {
        const drag = _pointerDrag;
        if (!drag || drag.pointerId !== event.pointerId) return;
        if (!_dragProject) {
            if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 6) return;
            _dragProject = drag.id; closeWorkspacePopover(); closeMenu();
            drag.button.classList.add("is-dragging"); document.body.classList.add("workspace-reordering");
        }
        event.preventDefault();
        const target = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-ws-sortable]");
        drag.target = target && target.dataset.wsId !== drag.id ? target.dataset.wsId : "";
        $("workspaceNav").querySelectorAll(".drop-before,.drop-after").forEach((item) => item.classList.remove("drop-before", "drop-after"));
        if (drag.target) {
            const rect = target.getBoundingClientRect(); drag.after = event.clientY > rect.top + rect.height / 2;
            target.classList.add(drag.after ? "drop-after" : "drop-before");
        }
    }, { passive: false });
    document.addEventListener("pointerup", (event) => {
        const drag = _pointerDrag;
        if (!drag || drag.pointerId !== event.pointerId) return;
        const moved = !!_dragProject;
        if (drag.button.hasPointerCapture(event.pointerId)) drag.button.releasePointerCapture(event.pointerId);
        clearProjectDrag();
        if (!moved) return;
        event.preventDefault(); _dragClickSuppressed = true;
        setTimeout(() => { _dragClickSuppressed = false; }, 350);
        if (drag.target) {
            const ids = workspaceProjects().filter((p) => !p.archived).map((p) => p.id);
            saveProjectOrder(workspaceReorderIds(ids, drag.id, drag.target, drag.after));
        }
    });
    document.addEventListener("pointercancel", clearProjectDrag);
    window.addEventListener("resize", () => closeWorkspacePopover());
    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") { if (_popover) { event.preventDefault(); closeWorkspacePopover(true); } closeMenu(); }
    });
    registerContextChanged((kind) => {
        if (kind === "conversations") refreshWorkspacePopover();
        if (kind === "before-account" || kind === "before-project") closeWorkspacePopover();
        if (kind === "account") {
            invalidateRequest(_orderRequest); _orderPending = false; clearProjectDrag();
            _generation++; _projects = []; _assignments = {}; _owner = null;
            _loadFailed = false; _loaded = false; _loading = false;
            closeMenu(); closeWorkspacePopover();
            if (_settingsResolve) {
                const resolve = _settingsResolve; _settingsResolve = null; closeModal($("workspaceNameModal")); resolve(null);
            }
            renderWorkspaceNavigation(); workspaceReload();
        }
    });
    await workspaceReload();
}
