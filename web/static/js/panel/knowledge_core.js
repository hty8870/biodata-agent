"use strict";



export const KNOWLEDGE_KINDS = Object.freeze(["memory", "skill", "example"]);
export const KNOWLEDGE_KIND_ZH = Object.freeze({ memory: "记忆", skill: "技能", example: "样例" });
export const KNOWLEDGE_SCOPE_LEVELS = Object.freeze(["system", "user", "project"]);
export const KNOWLEDGE_SCOPE_ZH = Object.freeze({ system: "系统", user: "用户", project: "项目" });
/* 状态中文徽标：失败/候选/冲突必须可见（低强调 trace 之外的例外三态）。 */
export const KNOWLEDGE_STATUS_ZH = Object.freeze({
    active: "生效中", pending: "待确认", draft: "草稿", retracted: "已撤回",
    failed: "失败", conflict: "有冲突", candidate: "候选",
    stale: "评估已过期", committed: "已提交", rejected: "已驳回",
});
export const KNOWLEDGE_OP_ZH = Object.freeze({ create: "新增", update: "修改", retract: "撤回", move: "移动" });
export const KNOWLEDGE_TIER_ZH = Object.freeze({ explicit: "你说过这条要求", implicit: "AI 从你的多次行为里推断" });
export const KNOWLEDGE_LIMITS = Object.freeze({
    title: 180, text: 2000, trigger: 500, feedback: 2000, source: 4000, links: 32, operations: 32,
});
const validId = (v) => typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(v);
const validRevision = (v) => Number.isSafeInteger(v) && v >= 0;
const isObject = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
/* 阈值控件范围（与学习阈值同一控件语义；服务端再收一遍口）。 */
export const KNOWLEDGE_THRESHOLD_MIN = 1;
export const KNOWLEDGE_THRESHOLD_MAX = 20;
export const KNOWLEDGE_THRESHOLD_DEFAULT = 3;

export function knowledgeKindZh(kind) {
    return KNOWLEDGE_KIND_ZH[kind] || "记忆";
}

export function knowledgeScopeLevelOf(level) {
    return KNOWLEDGE_SCOPE_LEVELS.includes(level) ? level : "user";
}

/* 条目状态 → 徽标（class + 中文）。未知状态如实标「未知状态」，不吞。 */
export function knowledgeEntryBadge(status) {
    const label = KNOWLEDGE_STATUS_ZH[status] || "未知状态";
    const cls = ({ active: "ok", retracted: "muted", pending: "pending", draft: "draft",
        stale: "warn", committed: "ok", rejected: "muted", failed: "no", conflict: "warn", candidate: "pending" })[status] || "unknown";
    return { cls, label };
}

export function knowledgeEntryFromServer(item) {
    item = isObject(item) ? item : {};
    return { ...item, id: String(item.id || ""),
        scope_level: item.scope_level || (item.project_id ? "project" : "user"),
        project_id: String(item.project_id || ""), kind: item.kind || "memory",
        title: String(item.title || ""), text: String(item.text || ""), trigger: String(item.trigger || ""),
        tier: item.tier || "explicit", permanence: item.permanence || "unknown",
        payload: isObject(item.payload) ? item.payload : {}, links: Array.isArray(item.links) ? item.links.slice() : [],
        provenance: item.provenance || {}, revision: validRevision(item.revision) ? item.revision : null,
        status: item.status || "unknown" };
}

export function knowledgeChangeFromServer(item) {
    item = isObject(item) ? item : {};
    return { ...item, id: String(item.id || item.change_id || ""), change_id: String(item.id || item.change_id || ""),
        change_revision: validRevision(item.revision) ? item.revision : (validRevision(item.change_revision) ? item.change_revision : null),
        change_fingerprint: item.fingerprint || item.change_fingerprint || "",
        status: item.status || item.stage || "draft", project_id: String(item.project_id || ""),
        operations: Array.isArray(item.operations) ? item.operations : [], assessment_id: item.assessment_id || "",
        assessment: isObject(item.assessment) ? item.assessment : null,
        error_code: item.error_code || item.assessment?.error_code || "", detail: item.detail || "" };
}

export function knowledgePayloadParse(text) {
    try {
        const value = String(text || "").trim() ? JSON.parse(text) : {};
        return isObject(value) ? { ok: true, payload: value } : { ok: false, error: "结构内容必须是 JSON 对象" };
    } catch (_e) { return { ok: false, error: "结构内容不是合法 JSON，请检查后重试" }; }
}
export function knowledgePayloadText(payload) { return JSON.stringify(payload || {}, null, 2); }

export function knowledgeDraftFromEntry(entry, kind = "memory") {
    return { kind: entry?.kind || kind, title: entry?.title || "", text: entry?.text || "", trigger: entry?.trigger || "",
        tier: entry?.tier || "explicit", permanence: entry?.permanence || "unknown",
        payloadText: knowledgePayloadText(entry?.payload || (kind === "skill" ? { tools: [] }
            : kind === "example" ? { utterance: "", steps: [], verified_success: false } : {})),
        linksText: (entry?.links || []).join("\n"), sourceText: "", moveScope: "user", moveProject: "",
        entryRevision: entry?.revision ?? null };
}

export function knowledgeLinksParse(text) {
    const links = String(text || "").split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    if (links.length > KNOWLEDGE_LIMITS.links || new Set(links).size !== links.length || links.some((id) => !validId(id))) {
        return { ok: false, error: "关联须为不重复的知识编号，每行一项，编号不超过 128 字符" };
    }
    return { ok: true, links };
}

export function knowledgeUserSources(text, projectId = "", verifiedEntry = null) {
    const value = String(text || "").trim();
    if (!value || value.length > KNOWLEDGE_LIMITS.source) return { ok: false, error: "请明确填写本次提交材料（1–4000 字）" };
    const source_refs = [{ type: "user", text: value, project_id: projectId }];
    if (verifiedEntry?.kind === "example" && verifiedEntry.payload?.verified_success === true) {
        const original = (verifiedEntry.provenance?.sources || []).filter((s) => s.type === "learning" && validId(s.id));
        if (!original.length) return { ok: false, error: "原版没有可重验的真实执行出处；请改为未验证样例，不能保留成功标记" };
        const ids = [...new Set(original.map((s) => s.id))];
        if (ids.length > 7) return { ok: false, error: "原版执行出处超过完整核验上限，草稿保留，请拆分后重新提交" };
        source_refs.push(...ids.map((id) => ({ type: "learning", id })));
    }
    return { ok: true, source_refs };
}

export function knowledgeOperationsFromForm(form, existing) {
    form = form || {};
    const kind = form.kind || "memory", title = String(form.title || "").trim(), text = String(form.text || "").trim();
    const trigger = String(form.trigger || "").trim(), tier = form.tier || "explicit", permanence = form.permanence || "unknown";
    if (!KNOWLEDGE_KINDS.includes(kind)) return { ok: false, error: "请选择记忆、技能或样例" };
    for (const [label, value, max, required] of [["标题", title, 180, true], ["正文", text, 2000, true], ["适用场景", trigger, 500, false]]) {
        if ((required && !value) || value.length > max) return { ok: false, error: label + "须为" + (required ? " 1–" : "不超过 ") + max + " 字" };
    }
    if (!["explicit", "implicit"].includes(tier) || !["persistent", "temporary", "unknown"].includes(permanence)) return { ok: false, error: "依据或有效期无效" };
    const parsed = knowledgePayloadParse(form.payloadText), linked = knowledgeLinksParse(form.linksText);
    if (!parsed.ok) return parsed;
    if (!linked.ok) return linked;
    const payload = parsed.payload, tools = form.registeredTools || [];
    const fields = { memory: [], skill: ["tools"], example: ["utterance", "steps", "verified_success"] }[kind];
    if (Object.keys(payload).some((k) => !fields.includes(k))) return { ok: false, error: "结构内容含未支持字段" };
    if (kind === "skill" && (!Array.isArray(payload.tools) || !payload.tools.length || payload.tools.length > 32
        || new Set(payload.tools).size !== payload.tools.length || payload.tools.some((t) => !tools.includes(t)))) {
        return { ok: false, error: "技能须选 1–32 个不重复的当前已注册工具，先刷新状态查看工具清单" };
    }
    if (kind === "example") {
        if (typeof payload.utterance !== "string" || !payload.utterance.trim() || payload.utterance.length > 2000
            || !Array.isArray(payload.steps) || !payload.steps.length || payload.steps.length > 16 || payload.steps.some((s) => !isObject(s)
                || Object.keys(s).some((k) => !["verb", "args"].includes(k)) || !tools.includes(s.verb) || typeof s.args !== "string" || s.args.length > 500)
            || typeof payload.verified_success !== "boolean") return { ok: false, error: "样例须含原话、真实工具步骤 {verb,args} 和布尔成功标记" };
        if (payload.verified_success && (!existing || existing.kind !== "example" || existing.payload?.verified_success !== true)) return { ok: false, error: "手工材料不能标为已验证成功，请设 verified_success 为 false；真实成功由服务端核验" };
    }
    if (existing && (!validId(existing.id) || !validRevision(existing.revision) || existing.status !== "active")) return { ok: false, error: "条目已失效或版本未取得，请刷新" };
    if (existing && form.entryRevision != null && form.entryRevision !== existing.revision) return { ok: false, error: "条目已更新；草稿保留，请先还原到当前版本再编辑" };
    const sources = knowledgeUserSources(form.sourceText, form.projectId || "", kind === "example" && payload.verified_success ? existing : null);
    if (!sources.ok) return sources;
    const after = { kind, title, text, trigger, tier, permanence, payload, links: linked.links };
    return { ok: true, operations: [existing ? { op: "update", entry_id: existing.id,
        expected_entry_revision: existing.revision, after } : { op: "create", after }], source_refs: sources.source_refs };
}

export function knowledgeRetractOperation(entry) {
    if (!entry || !entry.id) return null;
    return { op: "retract", entry_id: String(entry.id), expected_entry_revision: Number(entry.revision) || 0 };
}

export function knowledgeMoveOperation(entry, destination) {
    if (!entry || !entry.id || !destination || !KNOWLEDGE_SCOPE_LEVELS.includes(destination.scope_level)) return null;
    if (destination.scope_level === "system") return null;   // 系统层是只读内置，不可移入
    return {
        op: "move", entry_id: String(entry.id), expected_entry_revision: Number(entry.revision) || 0,
        destination: { scope_level: destination.scope_level,
            project_id: destination.scope_level === "project" ? String(destination.project_id || "") : "" },
    };
}

export function knowledgeChangesVisibility(changes) {
    const items = Array.isArray(changes) ? changes : [];
    return { items, failed: items.filter((c) => c.error_code || c.assessment?.error_code).length,
        candidates: items.filter((c) => ["draft", "pending"].includes(c.status)).length,
        conflicts: items.filter((c) => c.status === "stale" || (c.assessment?.reports || []).some((r) =>
            (r.relation?.semantic_pairs || []).some((pair) => pair.relation === "disjoint"))).length };
}

export function knowledgeRelationSummary(result) {
    if (!result) return "尚未评估";
    if (!result.coverage) return "历史分类（只读）：" + (result.relation || "未记录") + " · " + (result.reason || "");
    const coverage = result.coverage;
    const label = coverage.status === "no_existing" ? "没有旧项，已核出处" : coverage.complete === true ? "逐项比较完整" : "比较未完成";
    return label + " · " + (coverage.checked_ids || []).length + "/" + (coverage.required_ids || []).length + " 对"
        + (result.error_code ? " · " + result.error_code : "");
}

export function knowledgeBodyText(body) {
    if (!body) return "无";
    return "标题：" + (body.title || "无") + "\n正文：" + (body.text || "")
        + "\n适用条件：" + (body.trigger || "未限定")
        + "\n有效期：" + ({ persistent: "长期保留", temporary: "临时", unknown: "尚不明确" }[body.permanence] || "尚不明确")
        + "\n依据：" + (KNOWLEDGE_TIER_ZH[body.tier] || "未声明")
        + "\n结构内容：" + knowledgePayloadText(body.payload) + "\n关联：" + (body.links || []).join("、")
        + (validRevision(body.revision) ? "\n版本：" + body.revision : "");
}

export function knowledgeTraceSummary(assessment) {
    if (!assessment) return "";
    return (assessment.committable === true ? "可供真人确认（不是自动批准）" : "暂不可提交")
        + " · 检查 " + (assessment.assessed_count ?? "未知") + "/" + (assessment.required_count ?? "未知") + " 项"
        + " · 编辑核验 " + (assessment.edit_checks || []).length + " 项"
        + (assessment.error_code ? " · " + assessment.error_code : "");
}

export function knowledgeCommitReady(change, status) {
    const a = change?.assessment, reason = [];
    if (change?.status !== "pending" || change?.assessment_stale) reason.push("当前提案未通过或已过期，请重新评估");
    if (!a || a.committable !== true || a.complete !== true || a.error_code) reason.push("没有完整可提交评估");
    if (!status?.capabilities?.assessment_version || a?.version !== status.capabilities.assessment_version) reason.push("旧版评估不能提交，请重新评估");
    if (!validRevision(status?.catalog_generation) || status.catalog_generation !== a?.catalog_generation) reason.push("知识目录已变化，请刷新并重评");
    if (!validRevision(change?.change_revision) || a?.change_revision !== change.change_revision
        || !change?.change_fingerprint || a?.change_fingerprint !== change.change_fingerprint
        || !change?.assessment_id || a?.id !== change.assessment_id) reason.push("评估未绑定当前内容版本");
    return { ok: !reason.length, reason: reason.join("；") };
}

export function knowledgeDedupRequestId(base) {
    if (!base) return "";
    const bytes = new TextEncoder().encode(String(base));
    const hash = (seed) => {
        let value = seed;
        for (const byte of bytes) value = BigInt.asUintN(64, (value ^ BigInt(byte)) * 1099511628211n);
        return value.toString(16).padStart(16, "0");
    };
    return "kw-" + hash(14695981039346656037n) + hash(7809847782465536322n);
}

export function knowledgeLocalMaterialFromText(text, projectId, conversationId) {
    const t = String(text || "").trim();
    if (!t || t.length > 4000) return null;
    return { client_id: "lm-" + knowledgeDedupRequestId(JSON.stringify([t, projectId, conversationId])),
        project_id: String(projectId || ""), text: t, ...(conversationId ? { conversation_id: conversationId } : {}) };
}

export function knowledgeSelectionPayload(state) {
    state = state || {};
    const groups = new Map();
    for (const row of state.conversations || []) {
        if (!validId(row.conversation_id)) return null;
        const project = String(row.project_id || "");
        if (!groups.has(project)) groups.set(project, new Set());
        groups.get(project).add(row.conversation_id);
    }
    const selection = Array.from(groups, ([project_id, ids]) => ({ project_id, conversation_ids: [...ids].sort() }))
        .sort((a, b) => a.project_id.localeCompare(b.project_id));
    const source_ids = [...new Set(state.sourceIds || [])].sort();
    const local_materials = [];
    if (state.localText?.trim()) {
        const material = knowledgeLocalMaterialFromText(state.localText, state.localProject || "", null);
        if (!material) return null;
        local_materials.push(material);
    }
    if (!selection.length && !source_ids.length && !local_materials.length) return null;
    return { project_id: String(state.projectId || ""), selection, source_ids, local_materials, manual: state.manual === true };
}

export function knowledgeTriggerCounts(result) {
    result = { ...(result || {}), ...(result?.result || {}) };
    const num = (v) => validRevision(v) ? v : null;
    const bool = (v) => typeof v === "boolean" ? v : null;
    const ids = (v) => Array.isArray(v) ? v.slice() : null;
    return { selected_total: num(result.selected_total), selected_count: num(result.selected_count), batch_count: num(result.batch_count),
        read_count: num(result.read_count), handled_count: num(result.handled_count), consumed_count: num(result.consumed_count),
        remaining_selected_count: num(result.remaining_selected_count), remaining_source_ids: ids(result.remaining_source_ids),
        selected_source_ids: ids(result.selected_source_ids), attempt: num(result.attempt), request_id: result.request_id || null,
        job_id: result.job_id || result.id || null, partial: bool(result.partial), scheduled: bool(result.scheduled), complete: bool(result.complete) };
}

export function knowledgeThresholdClamp(value) {
    if (typeof value !== "number" && (typeof value !== "string" || !/^\d+$/.test(value))) return null;
    const n = Number(value);
    return Number.isSafeInteger(n) && n >= 1 && n <= 20 ? n : null;
}

/* 多对话勾选的展示摘要（超长截断并如实标注省略数）。 */
export function knowledgeSelectionSummary(conversationIds) {
    const ids = Array.isArray(conversationIds) ? conversationIds.map(String).filter(Boolean) : [];
    if (!ids.length) return "";
    const shown = ids.slice(0, 4).join("、");
    return shown + (ids.length > 4 ? " 等 " + ids.length + " 段" : "");
}
