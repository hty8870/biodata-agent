"use strict";

export const WORKSPACE_COPY = Object.freeze({
    title: "项目", global: "未归类", all: "所有对话", create: "新建项目", rename: "重命名",
    archive: "归档项目", restore: "恢复项目", moveOut: "移出项目", moveTo: "移入项目",
    learningGlobal: "全局", empty: "还没有项目", archived: "已归档", manage: "管理项目",
    edit: "编辑项目", name: "项目名称", color: "项目颜色", customColor: "自定义颜色",
    colorHex: "自定义颜色（十六进制）", save: "保存", cancel: "取消", close: "关闭项目卡片",
    placeholder: "给项目起个名字", preview: "项目预览", conversations: "段对话",
    emptyConversations: "还没有对话", emptyHint: "把相关对话放进来，随时接着聊。",
    moveCurrent: "把本会话移入此项目", moveCurrentGlobal: "把本会话移到未归类",
    alreadyCurrent: "本会话已在此项目", alreadyGlobal: "本会话已在未归类",
    noConversation: "开始一段对话后，即可在这里移动。", current: "当前项目",
    switchProject: "切换到此项目", switchGlobal: "切换到未归类", openConversation: "回看这段对话",
    createHint: "名称和颜色可随时修改", editHint: "换个名字或颜色，对话仍会保留。",
    archivedMoveHint: "恢复项目后，即可移动对话。", chatOnly: "（仅对话）",
    reorderHint: "拖动调整顺序；也可按 Alt + 上下方向键", orderSaved: "项目顺序已保存"
});

export const WORKSPACE_COLORS = Object.freeze([
    { value: "#0d9488", label: "青绿" }, { value: "#3b82f6", label: "蓝色" },
    { value: "#8b5cf6", label: "紫色" }, { value: "#f59e0b", label: "琥珀" },
    { value: "#f97316", label: "橙色" }, { value: "#ec4899", label: "粉色" },
    { value: "#66a6a0", label: "绿色" }, { value: "#64748b", label: "灰蓝" }
].map(Object.freeze));
export const WORKSPACE_UNCLASSIFIED_COLOR = "#94a3b8";

export function workspaceColorValue(value) {
    return /^#[0-9a-fA-F]{6}$/.test(String(value || "")) ? String(value).toLowerCase() : WORKSPACE_COLORS[0].value;
}

export function workspaceProjectColor(project) {
    return project ? workspaceColorValue(project.color) : WORKSPACE_UNCLASSIFIED_COLOR;
}

export function workspaceConversationGroups(hist) {
    const groups = new Map();
    (hist || []).forEach((row, index) => {
        const key = row && row.convId ? String(row.convId) : "legacy:" + index;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(row);
    });
    return Array.from(groups.values());
}

export function workspaceReorderIds(ids, draggedId, targetId, after = false) {
    const result = (ids || []).slice();
    if (draggedId === targetId || !result.includes(draggedId) || !result.includes(targetId)) return result;
    result.splice(result.indexOf(draggedId), 1);
    result.splice(result.indexOf(targetId) + (after ? 1 : 0), 0, draggedId);
    return result;
}

export function workspaceProjectOf(conversationId, assignments, fallback) {
    if (conversationId && Object.prototype.hasOwnProperty.call(assignments || {}, conversationId)) {
        return String(assignments[conversationId] || "");
    }
    return String(fallback || "");
}

export function workspaceHistoryGroups(conversations, projects, assignments) {
    const result = new Map();
    const byId = new Map((projects || []).map((p) => [String(p.id), p]));
    (conversations || []).forEach((rows) => {
        const first = rows[0] || {};
        const id = workspaceProjectOf(String(first.convId || ""), assignments, first.project_id);
        if (!result.has(id)) result.set(id, { id, project: byId.get(id) || null, conversations: [] });
        result.get(id).conversations.push(rows);
    });
    return Array.from(result.values());
}

export function learningStorageKey(base, projectId) {
    return projectId ? base + "::p:" + String(projectId) : base;
}

export function learningHistory(hist, projectId, assignments) {
    if (!projectId) return (hist || []).slice();
    return (hist || []).filter((h) => workspaceProjectOf(String(h.convId || ""), assignments, h.project_id) === projectId);
}
