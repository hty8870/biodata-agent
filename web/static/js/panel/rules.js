"use strict";



import { API, $, escapeHtml, fmtTime, toast, learningTargetScope, currentAccountScope, beginRequest, invalidateRequest } from "#core";
import { endpointCfg } from "#examples";
import { knowledgeDedupRequestId, knowledgeRelationSummary } from "#knowledge_core";
import { COPY, setStatusLine, apiErrorText } from "../core/copy.js";

/* 一框两段：上=待确认候选（dream 刚提炼出、还没被确认/驳回），下=生效中的规则。
   `_pending` 是本地会话内状态（候选不落地）；`_active` 是后端账本 active 行的投影，
   确认/撤回后整块重拉——界面状态永远以后端为准，不靠本地加减推测。 */
const _candidateRequests = {};
let _pendingToken = null;
const _confirming = new WeakMap();
let _pending = [];
let _active = [];
let _scope = "";
const _listRequests = {};
let _learning = null, _persistent = [], _reviews = [], _next = null, _reviewNext = null;
let _learningConfig = null, _learningConfigGeneration = 0, _learningStatusIdentity = null, _learningStale = false;
const _learningRequests = {}, _moreRequests = { candidate: {}, audit: {} };
const _moreTokens = { candidate: null, audit: null };
let _learningPostToken = null, _learningStatusToken = null, _renderedScope = "";
const _learningDrafts = new Map(), _learningReadStale = { rules: false, candidate: false, audit: false };
const _learningReleaseEpochs = new WeakMap(), _learningAuthorizations = new Map();
function learningEndpointIdentity(cfg, owner = currentAccountScope()) {
    return JSON.stringify([owner || "local", cfg.provider || "", cfg.base_url || "", cfg.model || ""]);
}
export function setLearningConfigProvider(fn) { _learningConfig = fn; }
function scopeKey() { const cfg = endpointCfg(); return JSON.stringify([currentAccountScope(), cfg.project_id || "", cfg.base_url || "", cfg.model || "", cfg.provider || ""]); }
function learningIdentity() { return JSON.stringify([scopeKey(), _learningConfigGeneration]); }
function learningReady(fresh = true) {
    return _learningStatusToken?.isCurrent() && _learningStatusIdentity === learningIdentity() && Number.isSafeInteger(_learning?.config_epoch)
        && _learning.config_epoch >= 0 && (!fresh || !_learningStale);
}
function invalidateLearningConfiguration() {
    _learningConfigGeneration++; _learningStatusIdentity = null;
    invalidateRequest(_listRequests); invalidateRequest(_learningRequests); invalidateRequest(_candidateRequests);
    for (const owner of Object.values(_moreRequests)) invalidateRequest(owner);
    _pendingToken = null;
}
export async function releaseLearningConfiguration(previous, _current, stillCurrent) {
    if (!previous || previous.owner !== currentAccountScope()) return { ok: false, skipped: true };
    const managed = typeof stillCurrent === "function", key = learningEndpointIdentity(previous, previous.owner);
    let receipt = managed ? _learningReleaseEpochs.get(previous) : null;
    const generation = _learningAuthorizations.get(key) || 0;
    if (managed && (!stillCurrent() || receipt?.terminal || (receipt && receipt.generation !== generation))) {
        if (receipt) receipt.terminal = true;
        return { ok: true, obsolete: true };
    }
    if (managed && !receipt) {
        receipt = { epoch: null, generation, terminal: false };
        _learningReleaseEpochs.set(previous, receipt);
    }
    invalidateLearningConfiguration();
    const viewIdentity = learningIdentity(), token = beginRequest({}, scopeKey);
    const current = () => token.isCurrent() && (!managed || stillCurrent());
    rulesStatus("AI配置已更改，正在释放旧配置授权…", false);
    try {
        const endpoint = { provider: previous.provider || "", base_url: previous.base_url || "", model: previous.model || "" };
        const status = await _get(API.learningStatus + "?" + Object.entries(endpoint)
            .map(([key, value]) => encodeURIComponent(key) + "=" + encodeURIComponent(value)).join("&"));
        if (!token.isCurrent() || previous.owner !== currentAccountScope()) return { ok: false, skipped: true };
        if (!current()) return { ok: true, obsolete: true };
        const identity = previous.owner || "local";
        if (status.scope?.owner !== identity && !(identity === "local" && status.scope?.owner === "anonymous")) {
            throw new Error("账户已切换，没有释放新账户的授权；请在当前账户重新操作。");
        }
        if (!Number.isSafeInteger(status.config_epoch) || status.config_epoch < 0) throw new Error("旧AI配置授权版本未取得，请重试。");
        if ((managed && generation !== (_learningAuthorizations.get(key) || 0))
            || (receipt && receipt.epoch !== null && receipt.epoch !== status.config_epoch)) {
            if (receipt) receipt.terminal = true;
            rulesStatus("旧释放请求已过期，未修改新授权；请刷新学习状态。", false);
            return { ok: true, obsolete: true };
        }
        if (!receipt) receipt = { epoch: status.config_epoch, generation, terminal: false };
        else if (receipt.epoch === null) receipt.epoch = status.config_epoch;
        const response = await fetch(API.learningControl, { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "release", expected_identity: identity,
                expected_config_epoch: receipt.epoch, ...endpoint }) });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok || data.released !== true) throw new Error(data.detail || "旧AI授权未能释放，请重试或手动暂停学习。");
        receipt.terminal = true;
        if (current() && viewIdentity === learningIdentity()) {
            invalidateRequest(_listRequests); _learningStatusIdentity = null;
            const visibleEndpoint = _learning && _learning.scope && _learning.scope.endpoint_fp === data.endpoint_fp;
            if (visibleEndpoint) {
                _learning = { ..._learning, configured: false }; _learningStale = true;
                _render();
            }
            rulesStatus(data.detail || "旧AI授权已释放，已有候选和学习开关保留。请刷新状态再恢复配置。", false);
            if (visibleEndpoint && data.endpoint_fp) void renderCollabRules(true);
        }
        return data;
    } catch (error) {
        if (current() && viewIdentity === learningIdentity()) rulesStatus("旧AI授权释放失败：" + String(error.message || error), true);
        throw error;
    }
}

function rulesStatus(text, isError) {
    setStatusLine($("rulesStatus"), text, isError);
}

function _endpointQuery() {
    const cfg = endpointCfg();
    return "base_url=" + encodeURIComponent(cfg.base_url || "") + "&model=" + encodeURIComponent(cfg.model || "")
        + "&project_id=" + encodeURIComponent(cfg.project_id || "")
        + "&provider=" + encodeURIComponent(cfg.provider || "");
}

async function _get(url) {
    const res = await fetch(url);
    const data = await res.json().catch(function () { return {}; });
    if (!res.ok || !data.ok) throw new Error((data && data.detail) || "规则加载失败");
    return data;
}

async function _post(url, body) {
    const res = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...endpointCfg(), ...body }),
    });
    const data = await res.json().catch(function () { return {}; });
    if (!res.ok || !data.ok) throw new Error(apiErrorText(data));
    return data;
}

/* 依据档的中文说法（诚实层：explicit=你亲口说过；implicit=AI 从行为里推断）。
   统一记忆空间批起与纯核 KNOWLEDGE_TIER_ZH 同文案；这里保留本地读法（本文件不重复引文案锚点）。 */
function _tierZh(tier) {
    return tier === "explicit" ? "你说过这条要求" : "AI 从你的多次行为里推断";
}

/* 提案道闸（统一记忆空间批）：把一条行为候选提交为 knowledge 提案（kind=skill）。
   构造体全内联（字面量，不依赖 import 名——tests/js/frontend_lifecycle_spec.mjs 的
   vm harness 会剥掉 import 行、只留本文件顶层声明与注入 ctx）。
   request_id 由行身份幂等；只报「已提交提案（待确认）」，绝不谎报「已生效」。
   project_id 不在此处写死——浏览器里 _post 会并入 endpointCfg()（学习范围唯一真源）。 */
function _skillProposePayload(text, trigger, tier, requestBase) {
    const body = String(text || "").trim();
    if (!body || body.length > 2000 || String(trigger || "").length > 500) throw new Error("正文须为 1–2000 字，适用条件不超过 500 字");
    const project = learningTargetScope();
    const after = { kind: "memory", title: body.slice(0, 60), text: body, trigger: String(trigger || ""),
        tier: tier === "implicit" ? "implicit" : "explicit", permanence: "persistent", payload: {}, links: [] };
    return { expected_identity: currentAccountScope() || "local",
        request_id: knowledgeDedupRequestId(JSON.stringify([requestBase, project, after])),
        operations: [{ op: "create", after }], source_refs: [{ type: "user", text: body, project_id: project }] };
}

function _candidateRow(row) {
    const el = document.createElement("div");
    el.className = "dream-cand";
    const ev = (Array.isArray(row.evidence) ? row.evidence : []).filter(Boolean);
    el.innerHTML = '<span class="dream-cand-copy"><strong>' + escapeHtml(String(row.text || "")) + "</strong>"
        + (row.trigger ? "<small>适用：" + escapeHtml(String(row.trigger)) + "</small>" : "")
        + "<small>" + escapeHtml(_tierZh(row.tier)) + "</small>"
        + (ev.length ? "<small>出处：「" + escapeHtml(ev.join("」「")) + "」</small>" : "")
        + "</span>"
        + '<span class="dream-cand-acts"></span>';
    const acts = el.querySelector(".dream-cand-acts");
    const yes = document.createElement("button");
    yes.type = "button"; yes.className = "btn btn-primary"; yes.textContent = "提交提案";
    yes.addEventListener("click", function () { _confirm(row, yes); });
    const no = document.createElement("button");
    no.type = "button"; no.className = "btn"; no.textContent = "驳回";
    no.addEventListener("click", function () { _dismiss(row); });
    const pending = _confirming.get(row);
    yes.disabled = !!(pending && pending.isCurrent());
    acts.appendChild(yes); acts.appendChild(no);
    return el;
}

function _activeRow(row) {
    const el = document.createElement("article");
    el.className = "memory-item";
    el.innerHTML = '<div class="memory-item-copy"><span>AI 提炼 · 已确认</span>'
        + "<strong>" + escapeHtml(String(row.text || "")) + "</strong>"
        + (row.trigger ? "<p>适用：" + escapeHtml(String(row.trigger)) + "</p>" : "")
        + "<small>" + escapeHtml(_tierZh(row.tier)) + " · 确认于 "
        + escapeHtml(fmtTime(Date.parse(String(row.confirmed_at || "")) || 0)) + "</small></div>"
        + '<div class="memory-item-actions"></div>';
    const drop = document.createElement("button");
    drop.type = "button"; drop.className = "btn memory-delete"; drop.textContent = "撤回";
    drop.addEventListener("click", function () { _revoke(String(row.id || "")); });
    el.querySelector(".memory-item-actions").appendChild(drop);
    return el;
}

function _section(title, note) {
    const head = document.createElement("div");
    head.className = "dream-preview-head";
    head.innerHTML = "<strong>" + escapeHtml(title) + "</strong><small>" + escapeHtml(note) + "</small>";
    return head;
}

function _render() {
    const box = $("rulesList");
    if (!box) return;
    if (_renderedScope !== _scope) _learningDrafts.clear();
    const saved = _learningDrafts;
    if (_renderedScope === _scope) for (const node of box.querySelectorAll("input[id],details[data-rules-view]")) {
        saved.set(node.id || node.dataset.rulesView, node.dataset.rulesView ? node.open : node.value);
    }
    box.innerHTML = "";
    _renderLearning(box);
    if (_pending.length) {
        box.appendChild(_section("待确认 " + _pending.length + " 条",
            "AI 从历史对话提炼的候选，逐条确认后才生效（不确认就不写任何地方）"));
        const list = document.createElement("div");
        list.className = "dream-preview-list";
        _pending.forEach(function (row, i) { list.appendChild(_candidateRow(row, i)); });
        box.appendChild(list);
    }
    box.appendChild(_section("生效中 " + _active.length + " 条",
        _active.length
            ? "AI 在执行下一步时会参考它们；随时可撤回（撤回后不再参考）"
            : "还没有生效的协作规则——先用「整理」提炼候选"));
    if (_active.length) {
        const list = document.createElement("div");
        list.className = "dream-preview-list";
        _active.forEach(function (row) { list.appendChild(_activeRow(row)); });
        box.appendChild(list);
    }
    for (const node of box.querySelectorAll("input[id],details[data-rules-view]")) {
        const key = node.id || node.dataset.rulesView;
        if (!saved.has(key)) continue;
        if (node.dataset.rulesView) node.open = saved.get(key); else node.value = saved.get(key);
    }
    _renderedScope = _scope;
    box.hidden = false;
}

/* 「转为协作规则」桥（2026-09-29 融合批；2026-10-09 统一记忆空间批改走 knowledge 道闸）：
   把一条**咨询层记忆**（note/dream）提交为技能提案。逐条显式动作 + 二段确认（在 memory.js
   那侧）。tier 固定 "explicit"：这条要求是用户自己写下的原话（declared）；evidence 也取该
   条目文本——服务端不重跑出处核验（出处核验发生在 dream 抽取时）。
   回执 {pending:true, change_id}：提案已入道闸、尚未生效；失败返回 null——调用方据此不打标。 */
export async function approveMemoryAsRule(text, projectId = learningTargetScope(), requestId) {
    const body = String(text || "").trim();
    if (!body) return null;
    const token = beginRequest({}, scopeKey);
    try {
        const payload = _skillProposePayload(body, "", "explicit", requestId || "memrule-" + String(projectId || ""));
        payload.project_id = String(projectId || ""); payload.source_refs[0].project_id = payload.project_id;
        const data = await _post(API.knowledgePropose, payload);
        if (!token.isCurrent()) return null;
        return { pending: true, change_id: String(data.change_id || ""), detail: data.detail || "" };
    } catch (err) {
        if (!token.isCurrent()) return null;
        rulesStatus(String((err && err.message) || err), true);
        toast("提案失败：" + String(err.message || err));
        return null;
    }
}

/* dream.js 把 /api/dream 响应里的协作规则候选交过来（那一刻候选还没有任何持久化状态）。
   兼容保留：dream 旧链已删 UI，此入口仍被 vm 规格与潜在旧调用点引用。 */
export function setRuleCandidates(rows, projectId = learningTargetScope()) {
    if (projectId !== learningTargetScope()) return;
    const selectedScope = scopeKey();
    if (_scope !== selectedScope) { _active = []; _persistent = []; _reviews = []; _learning = null; }
    _scope = selectedScope;
    _pendingToken = beginRequest(_candidateRequests, () => JSON.stringify(endpointCfg()));
    _pending = (Array.isArray(rows) ? rows : []).slice();
    _render();
}

/* 记忆弹窗打开时由 memory.js 调用：拉本分区生效中的规则并整块重渲染。
   preserveStatus=true 只在成功刷新时保留操作结果；失败须显示新错误与上次记录。 */
function mergeLearningRows(previous, incoming) {
    const rows = new Map();
    for (const row of [...previous, ...(Array.isArray(incoming) ? incoming : [])]) {
        if (row && typeof row.id === "string" && row.id) rows.set(row.id, row);
    }
    return Array.from(rows.values());
}
function acceptLearningStatus(learning) {
    _learning = learning; _learningStatusIdentity = learningIdentity(); _learningStale = false;
    _learningStatusToken = beginRequest({}, learningIdentity);
}
export async function renderCollabRules(preserveStatus) {
    const key = scopeKey(), token = beginRequest(_listRequests, learningIdentity);
    for (const owner of Object.values(_moreRequests)) invalidateRequest(owner);
    if (_scope !== key) {
        _pending = []; _active = []; _persistent = []; _reviews = []; _learning = null;
        _learningStatusIdentity = null; _learningStale = false;
        for (const part of Object.keys(_learningReadStale)) _learningReadStale[part] = false;
        _next = null; _reviewNext = null; _scope = key; _render();
    }
    let rules = null, candidateRules = null;
    const syncActive = () => {
        _active = (rules || _active).filter((row) => row.status === "active");
        for (const rule of candidateRules || []) {
            _active = _active.filter((row) => row.id !== rule.id); _active.push(rule);
        }
    };
    const load = async (url, part, label, apply) => {
        try {
            const data = await _get(url);
            if (!token.isCurrent()) return;
            apply(data);
            if (part !== "status") _learningReadStale[part] = false;
            _render();
        } catch (err) {
            if (!token.isCurrent()) return;
            if (part === "status") _learningStale = true; else _learningReadStale[part] = true;
            _render();
            rulesStatus(label + "未刷新，仍显示上次记录：" + String((err && err.message) || err), true);
            throw err;
        }
    };
    try {
        await Promise.all([
            load(API.rulesList + "?" + _endpointQuery(), "rules", "生效规则", (data) => {
                rules = Array.isArray(data.rules) ? data.rules : []; syncActive();
            }),
            load(API.learningStatus + "?" + _endpointQuery(), "status", "学习状态", acceptLearningStatus),
            load(API.learningCandidates + "?" + _endpointQuery() + "&status=pending", "candidate", "候选", (data) => {
                _persistent = mergeLearningRows([], data.items); _next = data.next_offset ?? null;
                candidateRules = data.active_rules || []; syncActive();
            }),
            load(API.learningCandidates + "?" + _endpointQuery() + "&record_type=audit", "audit", "审核记录", (data) => {
                _reviews = mergeLearningRows([], data.items); _reviewNext = data.next_offset ?? null;
            }),
        ]);
    } catch (_err) { return; }
    if (token.isCurrent() && !preserveStatus) rulesStatus("", false);
}

/* dream 候选确认（统一记忆空间批改道闸）：不再直调 /api/rules/approve——提交为
   knowledge 提案，回执只报「已提交提案」；候选行照旧从待确认列表移除（提案已入变更审查）。
   载荷构造内联：tests/js/frontend_lifecycle_spec.mjs 的 vm harness 只提取本函数单个体、
   ctx 只提供 _post/API/_pending 等注入名——同一形状见 _skillProposePayload（供学习候选路径复用）。 */
async function _confirm(row, btn) {
    if (row.record_type === "candidate") { await _learningReview(row, "approve"); return; }
    if (!_pendingToken || !_pendingToken.isCurrent()) return;
    const candidatesToken = _pendingToken;
    if (!_pending.includes(row)) return;
    const inFlight = _confirming.get(row);
    if (inFlight && inFlight.isCurrent()) return;
    const token = beginRequest(row, () => candidatesToken.isCurrent() && _pending.includes(row));
    _confirming.set(row, token);
    if (btn) btn.disabled = true;
    try {
        await _post(API.knowledgePropose, _skillProposePayload(row.text, row.trigger, row.tier, row.id || row.text));
        if (!token.isCurrent()) return;
        _pending = _pending.filter((candidate) => candidate !== row);
        rulesStatus("已提交提案：在「变更审查」确认后才生效。", false);
        toast("已提交知识提案：确认后才生效");
    } catch (err) {
        if (!token.isCurrent()) return;
        _confirming.delete(row);
        if (btn) btn.disabled = false;
        rulesStatus(String((err && err.message) || err), true);
        return;
    }
    await renderCollabRules(true);
}

/* 驳回：候选本来就没落地，删掉本地这一条即完成——如实说「没有写进任何地方」。 */
function _dismiss(row) {
    if (row.record_type === "candidate") { _learningReview(row, "reject"); return; }
    if (!_pending.includes(row)) return;
    _pending = _pending.filter((candidate) => candidate !== row);
    _render();
    rulesStatus("已驳回这条候选（没有写进任何地方）。", false);
}

async function _revoke(id) {
    if (!id) return;
    const token = beginRequest({}, learningIdentity);
    try {
        const row = _active.find((rule) => rule.id === id);
        if (!row || !Number.isSafeInteger(row.revision) || row.revision < 0) {
            rulesStatus("这条旧资料没有当前知识版本，请到知识空间刷新后撤回。", true); return;
        }
        const operation = { op: "retract", entry_id: id, expected_entry_revision: row.revision };
        const data = await _post(API.knowledgePropose, { expected_identity: currentAccountScope() || "local",
            request_id: knowledgeDedupRequestId(JSON.stringify([scopeKey(), operation])), operations: [operation], source_refs: [] });
        if (!token.isCurrent()) return;
        rulesStatus("撤回提案已提交：" + data.change_id + "；经评估、真人提交后才停止引用，原记录保留。", false);
        toast("撤回提案已提交，请到知识空间审核");
    } catch (err) {
        if (!token.isCurrent()) return;
        rulesStatus(String((err && err.message) || err), true);
        return;
    }
    await renderCollabRules(true);
}

function _button(text, action) {
    const button = document.createElement("button");
    button.type = "button"; button.className = "btn"; button.textContent = text;
    button.addEventListener("click", action); return button;
}

async function _learningPost(url, body, strict = false) {
    if (_learningPostToken?.isCurrent()) {
        rulesStatus("上一项学习操作还在提交，请稍后再试。", true);
        return { ok: false, busy: true };
    }
    const configRequest = url === API.learningControl || url === API.learningTrigger;
    if (configRequest && !learningReady(!(url === API.learningControl && body.enabled === false))) {
        rulesStatus("请先刷新学习状态，再授权或恢复配置。", true); return;
    }
    const token = beginRequest(_learningRequests, learningIdentity);
    invalidateRequest(_listRequests);
    for (const owner of Object.values(_moreRequests)) invalidateRequest(owner);
    _learningPostToken = token;
    let data;
    try {
        const cfg = _learningConfig ? _learningConfig() : {};
        const fields = configRequest ? { provider: cfg.provider, api_key: cfg.api_key,
            base_url: cfg.base_url, model: cfg.model, use_llm: cfg.use_llm,
            mock_llm: cfg.mock_llm, expected_identity: currentAccountScope() || "local",
            expected_config_epoch: _learning.config_epoch } : {};
        data = await _post(url, { ...body, ...fields });
        if (!token.isCurrent()) return;
        if (configRequest && data.scope && Number.isSafeInteger(data.config_epoch) && data.config_epoch >= 0) {
            acceptLearningStatus(data);
            if (data.configured === true && (url === API.learningTrigger || body.enabled === true)) {
                const key = learningEndpointIdentity(cfg);
                _learningAuthorizations.set(key, (_learningAuthorizations.get(key) || 0) + 1);
            }
        }
        const draftId = url === API.learningReview && body.candidate_id ? "learningFeedback-" + body.candidate_id
            : url === API.learningTrigger && body.feedback ? "learningScopeFeedback" : "";
        if (draftId) {
            const field = $(draftId);
            if (!field || field.value === (body.feedback || "")) {
                _learningDrafts.delete(draftId);
                if (field) field.value = "";
            }
        }
        if (data.detail) rulesStatus(data.detail, false);
        _render();
    } catch (err) {
        if (token.isCurrent()) rulesStatus(String(err.message || err), true);
        if (strict && token.isCurrent()) throw err;
        return;
    } finally {
        if (_learningPostToken === token) _learningPostToken = null;
    }
    if (token.isCurrent()) await renderCollabRules(true);
    return data;
}

/* 学习持久候选的批准/驳回（统一记忆空间批改道闸）：
   approve → knowledge propose（create/update 提案）；reject → 有 change_id 走 knowledge
   reject 道闸，旧行无 change_id 回退 /api/learning/review（旧端点由 C 适配 propose）。 */
async function _learningReview(row, decision) {
    if (row.record_type !== "candidate" || !_persistent.includes(row)) return;
    if (decision === "approve" || decision === "replace") {
        rulesStatus(row.change_id ? "请到「变更审查」核对这条真实提案并评估、确认提交。" : "这份旧候选尚未关联知识提案，不能直接生效；请在知识空间显式提交材料。", false);
        return;
    }
    const field = $("learningFeedback-" + row.id);
    if (row.change_id) {
        // 复用 _learningPost 的 single-flight token：连击/与别的学习操作并发只发一次
        const data = await _learningPost(API.knowledgeReject, {
            change_id: String(row.change_id), expected_change_revision: Number(row.change_revision) || 0,
            feedback: field ? field.value : "",
        });
        if (data && data.ok !== false && !data.busy) rulesStatus("已驳回（没有写进任何地方）。", false);
        return;
    }
    await _learningPost(API.learningReview, { candidate_id: row.id, decision,
        expected_fingerprint: row.fingerprint, expected_rules_fingerprint: row.existing_fingerprint,
        feedback: field ? field.value : "", confirm: decision !== "reject" });
}

async function _learningMore(recordType) {
    if (!_moreRequests[recordType] || _moreTokens[recordType]?.isCurrent()) return;
    const offset = recordType === "audit" ? _reviewNext : _next;
    if (!Number.isSafeInteger(offset) || offset < 0) return;
    const token = beginRequest(_moreRequests[recordType], learningIdentity);
    _moreTokens[recordType] = token;
    try {
        const data = await _get(API.learningCandidates + "?" + _endpointQuery() + "&offset=" + offset
            + "&record_type=" + recordType + (recordType === "candidate" ? "&status=pending" : ""));
        if (!token.isCurrent()) return;
        if (recordType === "audit") { _reviews = mergeLearningRows(_reviews, data.items); _reviewNext = data.next_offset ?? null; }
        else { _persistent = mergeLearningRows(_persistent, data.items); _next = data.next_offset ?? null; }
        _render();
    } catch (err) { if (token.isCurrent()) rulesStatus(String(err.message || err), true); }
    finally { if (_moreTokens[recordType] === token) _moreTokens[recordType] = null; }
}

function _renderLearning(box) {
    box.appendChild(_section("独立学习", "默认关闭。只整理当前范围的新共享回合和明确反馈；会使用独立 AI 额度。候选与审核记录保存在服务端，未经逐条确认不生效。"));
    for (const [part, label] of [["rules", "生效规则"], ["candidate", "候选"], ["audit", "审核记录"]]) {
        if (!_learningReadStale[part]) continue;
        const note = document.createElement("p"); note.textContent = label + "未刷新，仍显示上次记录。"; box.appendChild(note);
    }
    if (!_learning) { box.appendChild(_button("刷新状态", () => renderCollabRules(true))); return; }
    const scope = _learning.scope || {};
    const controls = document.createElement("div"); controls.className = "dream-preview-actions";
    controls.appendChild(_button(scope.enabled ? "暂停学习" : "授权开启学习", () => {
        if (!scope.enabled && !window.confirm("允许把当前范围的新共享对话发给已配置的 AI，持续生成待确认规则？不上传浏览器旧历史，不自动生效，可随时暂停。")) return;
        _learningPost(API.learningControl, { enabled: !scope.enabled, threshold: scope.threshold || 3 });
    }));
    if (scope.enabled) controls.appendChild(_button("整理新回合 / 恢复配置", () => _learningPost(API.learningTrigger, {})));
    controls.appendChild(_button("刷新状态", () => renderCollabRules(true)));
    box.appendChild(controls);
    if (scope.enabled) {
        const feedback = document.createElement("input"); feedback.id = "learningScopeFeedback";
        feedback.placeholder = "补充明确要求或纠正（不会自动成为规则）"; feedback.maxLength = 2000;
        box.appendChild(feedback);
        box.appendChild(_button("提交反馈并整理", () => {
            if (!feedback.value.trim()) return;
            const requestId = globalThis.crypto.randomUUID();
            _learningPost(API.learningTrigger, { feedback: feedback.value.trim(), request_id: requestId });
        }));
    }
    const state = document.createElement("p");
    state.textContent = (_learningStale ? "上次记录（尚未刷新） · " : "")
        + (scope.enabled ? "已授权" : "未授权") + " · " + (_learning.configured ? "本次配置有效" : "没有有效配置，不会后台调用")
        + " · 新证据 " + (_learning.fresh_count || 0) + " 条";
    box.appendChild(state);
    for (const job of (_learning.jobs || []).slice(0, 5)) {
        const line = document.createElement("div");
        line.textContent = (_learningStale ? "上次记录（尚未刷新） · " : "")
            + "学习任务 " + job.status + (job.result ? " · " + (job.result.detail || job.result.error_code || "") : "");
        line.appendChild(_button("执行记录", async () => {
            const token = beginRequest({}, learningIdentity);
            try {
                const data = await _get(API.learningStatus + "?" + _endpointQuery() + "&job_id=" + encodeURIComponent(job.id));
                if (!token.isCurrent()) return;
                const pre = document.createElement("pre"); pre.textContent = JSON.stringify(data.trace || [], null, 2); line.appendChild(pre);
            } catch (err) { if (token.isCurrent()) rulesStatus(String(err.message || err), true); }
        })); box.appendChild(line);
    }
    if (_persistent.length) box.appendChild(_section("已保存待确认 " + _persistent.length + " 条", "确认前核对出处、适用条件和新旧差异。驳回反馈会保留，可回查。"));
    for (const row of _persistent) {
        const element = _candidateRow(row), detail = document.createElement("div");
        const relation = row.relation;
        detail.textContent = "关系判断：" + knowledgeRelationSummary(relation)
            + "\n" + (relation?.semantic_pairs || []).map((pair) => pair.old_id + " · " + (pair.label || "未能判断") + "：" + pair.reason).join("；")
            + "\n编辑依据与完整前后请到「变更审查」核对；关系不代表替代授权。";
        const feedback = document.createElement("input"); feedback.id = "learningFeedback-" + row.id;
        feedback.placeholder = "驳回原因或审阅反馈（可选）"; feedback.maxLength = 2000;
        detail.appendChild(feedback); element.appendChild(detail); box.appendChild(element);
    }
    if (_next !== null) box.appendChild(_button("更多待确认候选", () => _learningMore("candidate")));
    if (_reviews.length) {
        const details = document.createElement("details"); details.dataset.rulesView = "learning-audit";
        const summary = document.createElement("summary");
        summary.textContent = "审核记录 " + _reviews.length + " 条"; details.appendChild(summary);
        for (const row of _reviews) { const line = document.createElement("p"); line.textContent = row.decision + " · " + (row.feedback || "") + " · " + row.created_at; details.appendChild(line); }
        if (_reviewNext !== null) details.appendChild(_button("更多审核记录", () => _learningMore("audit")));
        box.appendChild(details);
    }
}

/* 引擎导出别名（dream.js 整理视图经 ESM import 调用；vm 规格直接取顶层名）。 */
export function loadLearningStatus() { return renderCollabRules(true); }
export function learningStatusSnapshot() {
    if (!_learning) return null;
    const scope = _learning.scope || {};
    return {
        config_epoch: Number.isSafeInteger(Number(_learning.config_epoch)) ? Number(_learning.config_epoch) : null,
        enabled: scope.enabled === true,
        configured: _learning.configured === true,
        threshold: Number.isSafeInteger(Number(scope.threshold)) ? Number(scope.threshold) : null,
        fresh_count: Number.isSafeInteger(Number(_learning.fresh_count)) ? Number(_learning.fresh_count) : null,
        jobs: Array.isArray(_learning.jobs) ? _learning.jobs.slice() : [],
        stale: _learningStale,
    };
}
export function submitLearningControl(body) { return _learningPost(API.learningControl, body, true); }
export function submitLearningTrigger(body) { return _learningPost(API.learningTrigger, body, true); }
