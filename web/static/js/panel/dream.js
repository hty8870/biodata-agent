"use strict";



import { setLearningConfigProvider, releaseLearningConfiguration,
    loadLearningStatus, learningStatusSnapshot, submitLearningControl, submitLearningTrigger } from "#rules";
import { setKnowledgeLearnRenderer, setKnowledgeConfigProvider } from "#knowledge";
import { dreamGroupByProject } from "#dream_core";
import { knowledgeSelectionPayload, knowledgeTriggerCounts, knowledgeThresholdClamp, knowledgeDedupRequestId,
    KNOWLEDGE_THRESHOLD_MIN, KNOWLEDGE_THRESHOLD_MAX } from "#knowledge_core";
import { API, $, escapeHtml, learningTargetScope, setLearningTargetScope, currentAccountScope,
    registerContextChanged, beginRequest, invalidateRequest } from "#core";
import { endpointCfg } from "#examples";
import { getConfig, setAiConfigurationChangedHook } from "#shell";
import { setAccountChangedHook } from "#accounts";
import { workspaceProjects, workspaceProjectName } from "#workspaces";
import { COPY, apiErrorText } from "../core/copy.js";


/* ============================================================================
 * 新整理链（知识窗「整理」视图）
 * ========================================================================== */

const _requests = {};
/* 整理视图状态（草稿保护：刷新/切视图不丢勾选与本机资料草稿） */
let _learnState = null;
function defaultLearnState() {
    return {
        conversationIds: [], targetProject: learningTargetScope(),
        localText: "", localProject: "", manualOnly: false,
        threshold: null,          // 与学习状态同步；未取得前 null
        lastResult: null, lastError: "",
        busy: false,
    };
}
function state() { return _learnState || (_learnState = defaultLearnState()); }

function identity() { const cfg = getConfig(); return JSON.stringify([endpointIdentity(), learningTargetScope(), cfg.use_llm, cfg.api_key]); }
function learnStatus(text, isError) {
    const el = $("kwLearnStatus");
    if (el) { el.textContent = text || ""; el.classList.toggle("is-error", !!isError); el.hidden = !text; }
}

let _sources = [], _sourcesError = "", _sourceIdentity = "";
const _sourceRequests = {};
function endpointIdentity() { const c = endpointCfg(); return JSON.stringify([currentAccountScope(), c.base_url, c.model, c.provider]); }
function conversationSources() {
    return dreamGroupByProject(_sources, (row) => row.project_id).map((g) => ({
        projectId: g.projectId, label: g.projectId ? workspaceProjectName(g.projectId) || g.projectId : "用户层来源",
        convs: g.convs.map((row) => ({ ...row, convId: row.id, query: row.title || row.text_preview || row.id })) }));
}
async function loadSources() {
    const token = beginRequest(_sourceRequests, endpointIdentity), c = endpointCfg();
    try {
        const items = [], seen = new Set(); let offset = 0;
        do {
            if (seen.has(offset)) throw new Error("来源分页游标重复"); seen.add(offset);
            const q = new URLSearchParams({ base_url: c.base_url || "", model: c.model || "", provider: c.provider || "", offset: String(offset), limit: "100" });
            const res = await fetch(API.learningSources + "?" + q), data = await res.json();
            if (!token.isCurrent()) return;
            if (!res.ok || !data.ok) throw new Error(apiErrorText(data));
            items.push(...(data.items || [])); offset = data.next_offset;
        } while (offset != null);
        _sources = items; _sourcesError = ""; _sourceIdentity = endpointIdentity();
    } catch (error) { if (!token.isCurrent()) return; _sourcesError = error.message || String(error); _sources = []; _sourceIdentity = ""; }
    if (token.isCurrent() && $("kwLearn")) renderLearnPanel({ force: true, sourcesLoaded: true });
}

export function renderLearnPanel(options) {
    const opts = options || {};
    const host = opts.host || $("kwLearn");
    if (!host) return;
    if (opts.force !== true && host.dataset.rendered === "1") { refreshLearnSummary(); return; }
    host.dataset.rendered = "1";
    host.innerHTML = "";
    const s = state();
    const cfg = getConfig();

    const note = document.createElement("div");
    note.className = "kw-learn-note";
    note.innerHTML = "<strong>选择来源，再整理。</strong>勾选的对话与本机资料会发送给你配置的 AI（"
        + escapeHtml(cfg.model || "未配置模型") + "），只产出待确认的提案，不会自动生效；本机旧记忆原件不会自动上传。";
    host.appendChild(note);

    const box1 = document.createElement("div");
    box1.className = "kw-source-box";
    const head1 = document.createElement("div");
    head1.className = "kw-source-box-head";
    head1.textContent = "共享对话与材料来源（按原项目分组，逐项选择）";
    box1.appendChild(head1);
    const list = document.createElement("div");
    list.className = "kw-source-list";
    const groups = conversationSources();
    let total = 0;
    groups.forEach((group) => {
        const gh = document.createElement("div"); gh.className = "kw-source-box-head";
        gh.textContent = group.label + " · " + group.convs.length + " 项"; list.appendChild(gh);
        group.convs.forEach((conv) => {
            total += 1;
            const label = document.createElement("label"); label.className = "kw-source";
            const key = conv.id;
            label.innerHTML = '<input type="checkbox" data-kw-conv="' + escapeHtml(key) + '">'
                + '<span class="kw-source-copy"><strong>' + escapeHtml(conv.query) + "</strong>"
                + "<small>" + (conv.fresh ? "新来源" : "已处理，可明确再次整理") + " · " + escapeHtml(conv.kind)
                + " · 对话 " + escapeHtml(conv.conversation_id || "无") + "</small><small>" + escapeHtml(conv.text_preview || "")
                + "</small><small>来源 " + escapeHtml(conv.id) + " · 版本 " + escapeHtml(conv.source_hash) + "</small></span>";
            const input = label.querySelector("input"); input.checked = s.conversationIds.includes(key);
            input.addEventListener("change", () => {
                if (input.checked) { if (!s.conversationIds.includes(key)) s.conversationIds.push(key); }
                else s.conversationIds = s.conversationIds.filter((x) => x !== key);
                s.intent = null; syncSelectedNote();
            });
            list.appendChild(label);
        });
    });
    if (!total) {
        const empty = document.createElement("div"); empty.className = "kw-empty";
        empty.textContent = _sourcesError ? "来源读取失败：" + _sourcesError + "。刷新可重试。" : "尚无来源；旧浏览器历史不会自动上传。";
        list.appendChild(empty);
    }
    box1.appendChild(list);
    host.appendChild(box1);

    const box2 = document.createElement("div");
    box2.className = "kw-source-box";
    const head2 = document.createElement("div");
    head2.className = "kw-source-box-head";
    head2.textContent = "本机资料（可选 · 只整理这一段，标为你提供）";
    box2.appendChild(head2);
    const local = document.createElement("textarea");
    local.className = "kw-field-textarea";
    local.placeholder = "把一段你写好的要求或说明贴在这里，和勾选的对话一起整理（不会自动上传旧记忆）";
    local.value = s.localText;
    local.maxLength = 2000;
    local.addEventListener("input", () => { s.localText = local.value; });
    box2.appendChild(local);
    host.appendChild(box2);

    const controls = document.createElement("div");
    controls.className = "kw-threshold";
    const targetLabel = document.createElement("label");
    targetLabel.textContent = "整理到：";
    controls.appendChild(targetLabel);
    const target = document.createElement("select");
    target.setAttribute("aria-label", "整理目标范围");
    const targetOptions = [{ value: "", label: "用户层（跨项目汇总须明确同意）" }].concat(
        workspaceProjects().map((p) => ({ value: p.id, label: (p.name || p.id) + (p.archived ? "（归档，只读）" : ""), disabled: p.archived })));
    targetOptions.forEach((o) => {
        const option = document.createElement("option"); option.value = o.value; option.textContent = o.label;
        option.disabled = !!o.disabled; target.appendChild(option);
    });
    target.value = s.targetProject;
    target.addEventListener("change", () => { s.targetProject = target.value; s.intent = null; s.threshold = null; setLearningTargetScope(target.value); refreshLearnSummary(true); });
    controls.appendChild(target);
    const thrLabel = document.createElement("label");
    thrLabel.textContent = "证据阈值：";
    controls.appendChild(thrLabel);
    const threshold = document.createElement("input");
    threshold.type = "number"; threshold.min = String(KNOWLEDGE_THRESHOLD_MIN); threshold.max = String(KNOWLEDGE_THRESHOLD_MAX);
    threshold.step = "1";
    threshold.setAttribute("aria-label", "学习证据阈值");
    threshold.value = String(s.threshold == null ? "" : s.threshold);
    threshold.addEventListener("change", () => {
        const value = knowledgeThresholdClamp(threshold.value);
        s.threshold = value;
        threshold.value = String(value);
    });
    controls.appendChild(threshold);
    const saveThr = document.createElement("button");
    saveThr.type = "button"; saveThr.className = "btn";
    saveThr.textContent = "保存阈值";
    saveThr.addEventListener("click", () => saveThreshold());
    controls.appendChild(saveThr);
    const manual = document.createElement("label");
    manual.className = "kw-threshold";
    manual.innerHTML = '<input type="checkbox" id="kwManualOnly"><span>仅手动整理这一次（不改变自动学习开关）</span>';
    const manualInput = manual.querySelector("input");
    manualInput.checked = s.manualOnly;
    manualInput.addEventListener("change", () => { s.manualOnly = manualInput.checked; });
    controls.appendChild(manual);
    host.appendChild(controls);

    const acts = document.createElement("div");
    acts.className = "kw-learn-acts";
    const run = document.createElement("button");
    run.type = "button"; run.className = "btn btn-primary";
    run.textContent = "开始整理";
    run.addEventListener("click", () => runLearn(run));
    acts.appendChild(run);
    const statusBtn = document.createElement("button");
    statusBtn.type = "button"; statusBtn.className = "btn";
    statusBtn.textContent = "刷新学习状态";
    statusBtn.addEventListener("click", () => refreshLearnSummary(true));
    acts.appendChild(statusBtn);
    const sourceBtn = document.createElement("button"); sourceBtn.type = "button"; sourceBtn.className = "btn"; sourceBtn.textContent = "刷新来源";
    sourceBtn.addEventListener("click", loadSources); acts.appendChild(sourceBtn);
    const next = document.createElement("button"); next.type = "button"; next.className = "btn"; next.id = "kwContinue";
    next.textContent = "继续同一选择的未完成来源"; next.hidden = !s.remaining?.length;
    next.addEventListener("click", () => runLearn(next, true)); acts.appendChild(next);
    host.appendChild(acts);
    if (!opts.sourcesLoaded) void loadSources();

    const result = document.createElement("div");
    result.className = "kw-learn-result";
    result.id = "kwLearnStatus";
    result.hidden = true;
    host.appendChild(result);
    const trace = document.createElement("div");
    trace.className = "kw-learn-trace";
    trace.id = "kwLearnTrace";
    trace.hidden = true;
    host.appendChild(trace);

    syncSelectedNote();
    refreshLearnSummary();
}

function syncSelectedNote() {
    const s = state();
    const box = $("kwLearn");
    if (!box) return;
    const note = box.querySelector(".kw-learn-note");
    if (note) {
        note.innerHTML = "<strong>选择来源，再整理。</strong>勾选的对话与本机资料会发送给你配置的 AI（"
            + escapeHtml(getConfig().model || "未配置模型") + "），只产出待确认的提案，不会自动生效；本机旧记忆原件不会自动上传。"
            + (s.conversationIds.length ? "已勾选 " + s.conversationIds.length + " 段对话。" : "");
    }
}

const _summaryRequests = {};
let _pollTimer = null;
function showJob(job) {
    const s = state(), result = job.result || {}, counts = knowledgeTriggerCounts(job), parts = ["任务：" + job.status];
    for (const [key, label] of [["selected_count", "本次选择"], ["batch_count", "本批安排"], ["read_count", "实读"],
        ["handled_count", "实际处理"], ["consumed_count", "新核销"], ["remaining_selected_count", "未完成"]]) {
        if (counts[key] != null) parts.push(label + " " + counts[key]);
    }
    if (counts.complete === true && counts.remaining_selected_count === 0) parts.push("这份固定选择已完成");
    const remaining = counts.remaining_source_ids || s.lastResult?.remaining_source_ids;
    if (Array.isArray(remaining)) s.remaining = remaining.slice();
    if (counts.partial || s.remaining?.length) parts.push((counts.handled_count > 0 ? "部分完成" : "尚未完成") + " · 未完成 " + (s.remaining?.length ?? counts.remaining_selected_count ?? "未知") + " 项");
    if (result.error_code || result.detail) parts.push(result.error_code || result.detail);
    learnStatus(parts.join(" · "), ["failed", "paused", "waiting_config"].includes(job.status) || result.ok === false);
    const next = $("kwContinue");
    if (next) next.hidden = !s.remaining?.length || ["running", "queued"].includes(job.status);
}
async function refreshLearnSummary(force) {
    const s = state(), token = beginRequest(_summaryRequests, identity);
    try {
        if (force || !learningStatusSnapshot()) await loadLearningStatus();
        if (!token.isCurrent()) return;
        const snapshot = learningStatusSnapshot();
        if (!snapshot || snapshot.stale) throw new Error("状态未刷新，请重试");
        if (s.threshold == null) s.threshold = snapshot.threshold ?? 3;
        const t = $("kwLearnTrace");
        if (t) { t.hidden = false; t.textContent = (snapshot.enabled ? "持续学习已授权" : "持续学习未授权")
            + (snapshot.configured ? " · 配置有效" : " · 无有效后台配置") + " · 新证据 " + (snapshot.fresh_count ?? "未知") + " 条"; }
        const thr = $("kwLearn")?.querySelector('input[type="number"]');
        if (thr && !thr.value) thr.value = String(s.threshold);
        const job = snapshot.jobs.find((j) => j.id === s.jobId);
        if (job) {
            showJob(job);
            if (["running", "queued"].includes(job.status) && $("kwLearn") && !$("knowledgeWin")?.hidden) {
                clearTimeout(_pollTimer); _pollTimer = setTimeout(() => refreshLearnSummary(true), 1500);
            }
        }
    } catch (error) { if (token.isCurrent()) learnStatus("学习状态未刷新：" + (error.message || error), true); }
}
async function saveThreshold() {
    const s = state(), value = knowledgeThresholdClamp(s.threshold);
    if (value == null) { learnStatus("阈值须为 1–20 的整数；没有保存。", true); return; }
    if (learningTargetScope() !== s.targetProject) setLearningTargetScope(s.targetProject);
    const token = beginRequest(_requests, identity);
    try {
        await loadLearningStatus();
        if (!token.isCurrent()) return;
        const snapshot = learningStatusSnapshot();
        if (!snapshot || snapshot.stale) throw new Error("请先刷新状态");
        const data = await submitLearningControl({ project_id: s.targetProject, enabled: snapshot.enabled, threshold: value });
        if (!token.isCurrent()) return;
        if (!data?.ok) throw new Error("阈值未保存，请检查配置后重试");
        learnStatus("阈值已保存（" + value + "），持续学习开关保持不变。", false);
        await refreshLearnSummary(true);
    } catch (error) { if (token.isCurrent()) learnStatus("阈值保存失败：" + (error.message || error), true); }
}
async function runLearn(btn, continuing = false) {
    const s = state();
    if (s.busy) return;
    if (learningTargetScope() !== s.targetProject) setLearningTargetScope(s.targetProject);
    if (workspaceProjects().some((p) => p.id === s.targetProject && p.archived)) { learnStatus("目标项目已归档，只读；请明确选择新的目标。", true); return; }
    if (getConfig().use_llm !== true) { learnStatus("AI 未开启，选择与本机资料保留；没有发起整理。", true); return; }
    if (_sourceIdentity !== endpointIdentity() || _sourcesError) { learnStatus("先刷新当前账户与端点的来源。", true); return; }
    let payload;
    if (continuing) {
        if (!s.remaining?.length || !s.intent) { learnStatus("尚未取得未完成来源编号，不能猜测续批；请刷新任务状态。", true); return; }
        payload = s.intent.payload;
    } else {
        if (s.conversationIds.some((id) => !_sources.some((r) => r.id === id))) { learnStatus("所选来源已失效，请刷新并重新选择。", true); return; }
        payload = knowledgeSelectionPayload({ projectId: s.targetProject, sourceIds: s.conversationIds,
            conversations: [], localText: s.localText, localProject: s.targetProject, manual: s.manualOnly });
    }
    if (!payload) { learnStatus(COPY.knowledge.selectSourceFirst, true); return; }
    const cross = _sources.filter((r) => payload.source_ids.includes(r.id)).some((r) => r.project_id !== payload.project_id);
    if (cross && !payload.project_id && payload.manual !== true) {
        learnStatus("跨项目汇总到用户层仅支持明确授权的本次手动整理；自动模式未发起。", true); return;
    }
    if (cross && !window.confirm("确认将所选原项目的材料整理到" + (payload.project_id ? workspaceProjectName(payload.project_id) : "用户层，供各项目继承") + "？不会移动原对话，只产出待审核提案。")) return;
    if (!continuing) payload = { ...payload, allow_cross_project: cross && !payload.project_id && payload.manual === true };
    const versions = _sources.filter((r) => payload.source_ids.includes(r.id)).map((r) => [r.id, r.source_hash]);
    const same = s.intent && JSON.stringify(s.intent.payload) === JSON.stringify(payload);
    if (same && JSON.stringify(s.intent.versions) !== JSON.stringify(versions)) {
        learnStatus("来源版本已变化；旧选择没有重试。请刷新、核对材料并重新勾选。", true); return;
    }
    const signature = JSON.stringify([endpointIdentity(), payload, versions]);
    const request_id = same ? s.intent.request_id : knowledgeDedupRequestId(signature);
    s.intent = { payload, request_id, versions };
    const token = beginRequest(_requests, identity);
    s.busy = true; if (btn) btn.disabled = true;
    learnStatus("正在提交所选材料，来源与目标保持独立…", false);
    try {
        await loadLearningStatus();
        if (!token.isCurrent()) return;
        const data = await submitLearningTrigger({ ...payload, request_id });
        if (!token.isCurrent()) return;
        if (!data?.ok) throw new Error("没有发起任务，请检查学习状态与配置");
        s.lastResult = data;
        s.remaining = data.remaining_source_ids || [];
        s.jobId = data.job_id || data.job?.id || data.jobs?.find((j) => j.request_id === request_id)?.id || "";
        learnStatus(data.scheduled ? "已安排任务；实际处理与核销数以完成后的任务结果为准。" : (data.detail || "这次没有安排任务"), false);
        await refreshLearnSummary(true);
    } catch (error) { if (token.isCurrent()) learnStatus("整理失败（同一选择重试保持同一编号）：" + (error.message || error), true); }
    finally { if (token.isCurrent()) { s.busy = false; if (btn) btn.disabled = false; } }
}

export function initDream() {
    setLearningConfigProvider(getConfig); setKnowledgeConfigProvider(getConfig);
    setAiConfigurationChangedHook(releaseLearningConfiguration); setKnowledgeLearnRenderer(renderLearnPanel);
    const clearAccount = () => { invalidateRequest(_requests); invalidateRequest(_summaryRequests); invalidateRequest(_sourceRequests);
        clearTimeout(_pollTimer); _learnState = null; _sources = []; _sourceIdentity = ""; };
    setAccountChangedHook(clearAccount);
    registerContextChanged((kind) => {
        if (["before-account", "account"].includes(kind)) clearAccount();
        if (kind === "learning-scope") { invalidateRequest(_requests); state().targetProject = learningTargetScope(); state().busy = false; }
    });
}
