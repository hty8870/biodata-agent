"use strict";

/* 本文件是 ES Module：core 的工具、board_core 的 cbLabelForFilterId、#results 的分面状态
   （live binding 读 + 原地改 push/splice；**重赋值**必经属主 setFacetState）、board 的
   cbLogPush/swSync、search 的 runRecommend 经 import 取（facets↔board / facets↔search 成环，
   绑定都只在函数体内使用，ESM 允许）。
   search/board/results/shell 经 import 取本文件导出（绞杀桥已全退役）。 */
import { $, escapeHtml, prettyFacetValue, toast } from "#core";
import { cbLabelForFilterId } from "#board_core";
import { _facetFilters, _queryHits, _suppressed, setFacetState } from "#results";
import { USAGE_KINDS } from "#usage_core";
import { usageLog } from "#usage_log";
import { cbLogPush, swShowBoard, swSync } from "#board";
import { runRecommend } from "#search";

function _facetIdx(dim, value) { return _facetFilters.findIndex((f) => f.dim === dim && f.value === value); }

/* ============ 数据细化「提交」暂存（用户 2026-07-24 反馈）============
   在筛选抽屉里（rp2 起 #facetBar 恒住右侧抽屉），勾选分面取值不再逐点立即重搜，而是先攒进草稿 _facetStage，点「提交」才一次性应用，
   并把这批改动拼成一条「加入：X；去掉：Y」的消息进对话（如手绘图 2）。
   **单一 choke point**：_facetStage 的清空只发生在 renderFacets 顶部——任何真检索/回放都会走到 renderFacets，
   于是「攒了草稿又点了别的动作（撤销/忽略/新查询…）」时草稿被丢弃、grid 高亮回退（可见反馈），
   不会散在各按钮里各自漏一处。renderFacets 只读 _facetStage 渲染 grid 高亮 + 提交条，不写。 */
let _facetStage = null;   // null=未暂存（已应用集即真相）；数组=待提交草稿（_facetFilters 的一份副本再编辑）
// 暂存态在分面 grid 落在筛选抽屉里时启用（那儿才有「提交」按钮）。rp2 起 #facetBar 恒住抽屉，
// 故本判定在查询页恒真；无抽屉的页面（dataset.html 无 #facetGroups）恒假、保持逐点立即应用兜底。
function facetStageActive() { const g = $("facetGroups"); return !!(g && g.closest("#facetDrawer")); }
function facetSameValue(a, b) { return a.dim === b.dim && a.value === b.value; }
function facetSameSet(a, b) {
    if (a.length !== b.length) return false;
    return a.every((x) => b.some((y) => facetSameValue(x, y)));
}
/* 草稿相对已应用集的增量：added=草稿有而已应用无，removed=已应用有而草稿无。用来拼「加入/去掉」消息 + 数改动项。 */
function facetStageDiff() {
    const draft = _facetStage || [];
    const added = draft.filter((f) => !_facetFilters.some((g) => facetSameValue(f, g)));
    const removed = _facetFilters.filter((g) => !draft.some((f) => facetSameValue(f, g)));
    return { added, removed };
}
function facetStageToggle(dim, value, display, label) {
    if (_facetStage === null) _facetStage = _facetFilters.map((f) => Object.assign({}, f));   // 首次改动：草稿=当前已应用
    const i = _facetStage.findIndex((f) => f.dim === dim && f.value === value);
    if (i >= 0) _facetStage.splice(i, 1);                                                      // 再点已选＝取消
    else { _facetStage = _facetStage.filter((f) => f.dim !== dim); _facetStage.push({ dim, value, display, label }); }   // 同维单选
    renderFacetStage();   // 只更新 grid 高亮 + 提交条；不重搜、不动已应用集
}
/* 更新 grid 分面 chip 的暂存高亮 + 底部「提交/取消」条。_facetStage=null 时清空高亮、收起条。 */
function renderFacetStage() {
    const groups = $("facetGroups");
    if (groups) groups.querySelectorAll(".facet-chip").forEach((c) => {
        const on = !!_facetStage && _facetStage.some((f) => f.dim === c.dataset.dim && f.value === c.dataset.val);
        c.classList.toggle("is-staged", on);
        c.setAttribute("aria-pressed", on ? "true" : "false");
    });
    const bar = $("facetStageBar");
    if (!bar) return;
    const dirty = _facetStage !== null && !facetSameSet(_facetStage, _facetFilters);
    bar.hidden = !dirty;
    const cnt = $("facetStageCount");
    if (dirty && cnt) { const d = facetStageDiff(); cnt.textContent = (d.added.length + d.removed.length) + " 项改动待应用"; }
}
export function facetStageSubmit() {
    if (_facetStage === null) return;
    const _sb = $("submitBtn");
    if (_sb && _sb.disabled) { toast("上一步还在检索，稍等一下再提交"); return; }   // 在途互斥
    const diff = facetStageDiff();
    if (!diff.added.length && !diff.removed.length) { _facetStage = null; renderFacetStage(); return; }   // 无实质改动
    setFacetState({ facetFilters: _facetStage.map((f) => Object.assign({}, f)) });   // 重赋值经属主 setter（live binding 只读）
    _facetStage = null;
    renderFacetStage();   // 立刻收起提交条 + 清 grid 暂存高亮（否则在这次自己的检索在途期间还残留可见，虽会自愈但闪一下）
    const label = (f) => (f.label || f.dim) + " = " + prettyFacetValue(f.display || f.value);
    const parts = [];
    if (diff.added.length) parts.push("加入：" + diff.added.map(label).join("、"));
    if (diff.removed.length) parts.push("去掉：" + diff.removed.map(label).join("、"));
    const message = parts.join("；");
    cbLogPush("refine", message);   // 一整批改动＝对话里的一条用户消息
    facetDrawerSet(false);   // 提交即关抽屉（_facetStage 已清空，不会再 toast）；回复在「对话」页签上演
    swShowBoard();   // 提交即落到「对话」页签：回复（进度泡→完成摘要）在那里上演，别让用户切过去找
    // 抽屉暂存模式下 toggleFacet 会在顶部提前 return（走草稿），所以那一处的打点
    // 覆盖不到这条路径 —— 必须在**提交**这里补记（也更准：草稿阶段的勾选可能被取消掉）。
    diff.added.concat(diff.removed).forEach(function (f) {
        usageLog(USAGE_KINDS.facet, { d: String(f.label || f.dim || ""), dim: String(f.dim || ""), v: String(f.display || f.value || "") });
    });
    runRecommend({ keepFacets: true, sharedUtterance: message, sayPushed: true, messageKind: "refine" });
}
export function facetStageCancel() { _facetStage = null; renderFacetStage(); }
/* 草稿**将被丢弃**时的统一出口：核心承诺是「未提交的改动不静默丢失」。有几条会绕过提交、
   又把草稿静默清掉的路径——① 攒着草稿时关掉筛选抽屉（× 钮 / Esc）→ facetDrawerSet(false) 走这里；
   ② 提交在途时又攒了一份新草稿，这次检索回来时被 renderFacets 顶部 choke point 清掉。
   两处都改成走这里：脏就 toast 一声再清，UI 同步。 */
function facetStageDiscardIfDirty() {
    if (_facetStage !== null && !facetSameSet(_facetStage, _facetFilters)) toast("未提交的筛选选择已放弃");
    _facetStage = null;
    renderFacetStage();
}

function toggleFacet(dim, value, display, label) {
    if (facetStageActive()) { facetStageToggle(dim, value, display, label); return; }   // 筛选抽屉：攒进草稿，等「提交」
    // 无抽屉的页面（dataset.html 没有 #facetDrawer，facetStageActive 恒假）：保持逐点立即应用（旧行为）。
    const i = _facetIdx(dim, value);
    const removing = i >= 0;
    if (removing) setFacetState({ facetFilters: _facetFilters.filter((_, index) => index !== i) });                     // 再点已选＝取消
    else {
        setFacetState({ facetFilters: _facetFilters.filter((f) => f.dim !== dim) });   // 同维度单选：换值（重赋值经属主 setter）
        setFacetState({ facetFilters: _facetFilters.concat({ dim, value, display, label }) });
    }
    const message = (removing ? "去掉：" : "加：") + (label || dim) + " = " + prettyFacetValue(display || value);
    cbLogPush("refine", message);
    usageLog(USAGE_KINDS.facet, { d: String(label || dim || ""), dim: String(dim || ""), v: String(display || value || "") });
    runRecommend({ keepFacets: true, sharedUtterance: message, sayPushed: true, messageKind: "refine" });
}
export function removeFacet(dim, value) {
    const i = _facetIdx(dim, value);
    if (i >= 0) {
        const f = _facetFilters[i];
        const message = "去掉：" + ((f && (f.label || f.dim)) || dim) + " = " + prettyFacetValue((f && (f.display || f.value)) || value);
        cbLogPush("refine", message);
        setFacetState({ facetFilters: _facetFilters.filter((_, index) => index !== i) });
        runRecommend({ keepFacets: true, sharedUtterance: message, sayPushed: true, messageKind: "refine" });
    }
}
/* 就地切换一条**原始命中**（查询语句解析出的硬约束）的生效状态：
   忽略＝把该维度加入抑制表 → 重跑时后端在检索前放宽该约束；还原＝从抑制表移除 → 该约束重新生效
   （还原入口：condBoard「已忽略」分区 / 对话「回退至此」/ 撤销步进条「恢复全部条件」）。
   被忽略的命中不再渲染进「查询条件」排（那里只展示当前真正在筛的条件），
   但快照 _queryHits 保留全量，供撤销帧/条件板还原。 */
export function toggleQueryHit(filterId) {
    if (!filterId) return;
    const i = _suppressed.indexOf(filterId);
    const restoring = i >= 0;
    if (restoring) setFacetState({ suppressed: _suppressed.filter((value) => value !== filterId) });   // 恢复：该命中重新参与检索
    else setFacetState({ suppressed: _suppressed.concat(filterId) });           // 忽略：后端检索前放宽该命中（按极性 filter_id，include/exclude 同维不连坐）
    const message = (restoring ? "恢复条件：" : "忽略条件：") + cbLabelForFilterId(filterId);
    cbLogPush("refine", message);
    runRecommend({ keepFacets: true, sharedUtterance: message, sayPushed: true, messageKind: "refine" });
}

/* 「查询条件」chips 行（#facetActive）的唯一渲染口。唯一调用方 renderFacets：
   真检索落地，hits = _queryHits 去掉被忽略（_suppressed）的原始命中。
   hits 为空且无细化筛选时整条收起（栏里不再放恢复入口；
   还原走 condBoard「已忽略」分区 / 对话「回退至此」/ 撤销步进条「恢复全部条件」）。
   2026-08-04 起：后加的细化筛选与原始命中**同款待遇**——同一 accent 底、同一「忽略」按钮，
   不再用 ❝ 标记/× 按钮/中性底区分来源；只剩 ⊘（排除）/ ↑（软偏好）两枚**行为**标记保留。 */
export function renderActiveChips(hits) {
    const active = $("facetActive");
    if (!active) return;
    const hasChips = hits.length || _facetFilters.length;
    if (!hasChips) { active.hidden = true; active.innerHTML = ""; return; }
    const queryChips = hits.map((g) => {
        const fid = g.filter_id || g.dim;
        const off = false;   // 被忽略的已在调用方过滤掉，能渲染到这里的必为当前生效（保留 off 变量供下方模板分支，恒 false）
        const exc = g.polarity === "exclude";    // 负向命中（排除类）额外标 is-exclude
        // 软偏好（「优先 X」）**没有筛掉任何数据**，只把符合的排前面。它和硬条件长一个样
        // 就是在骗人：用户会以为结果都满足这一条。故单独标记、单独文案，
        // 按钮也从「忽略」改成「不按这条排」——那边停的是筛选，这边停的是排序。
        const pref = g.polarity === "prefer";
        if (pref) {
            return `<span class="fa-chip is-query is-prefer${off ? " is-off" : ""}" title="${off ? "已停用这条排序偏好（点「恢复」重新生效）" : "你写了「优先」，这条没有筛掉任何数据，只让符合的排在前面"}">`
                + `<span class="fa-mark" aria-hidden="true">↑</span>`
                + `<span class="fa-k">${escapeHtml(g.label)}</span>`
                + `<span class="fa-v">${(g.values || []).map(escapeHtml).join("、")}</span>`
                + `<button type="button" class="fa-toggle" data-fid="${escapeHtml(fid)}" aria-pressed="${off}" aria-label="${off ? "恢复" : "停用"}排序偏好 ${escapeHtml(g.label)}">${off ? "恢复" : "不按这条排"}</button></span>`;
        }
        return `<span class="fa-chip is-query${exc ? " is-exclude" : ""}${off ? " is-off" : ""}" title="${off ? "已忽略此条件（点「恢复」重新生效）" : (exc ? "这条来自你的查询，用于排除某类数据（点「忽略」可不按此条件筛选）" : "来自你的查询语句（点「忽略」可不按此条件筛选）")}">`
            + (exc ? `<span class="fa-mark" aria-hidden="true">⊘</span>` : "")
            + `<span class="fa-k">${escapeHtml(g.label)}</span>`
            + `<span class="fa-v">${(g.values || []).map(escapeHtml).join("、")}</span>`
            + `<button type="button" class="fa-toggle" data-fid="${escapeHtml(fid)}" aria-pressed="${off}" aria-label="${off ? "恢复" : "忽略"}条件 ${escapeHtml(g.label)}">${off ? "恢复" : "忽略"}</button></span>`;
    }).join("");
    // 后加细化：与原始命中同款 chip（accent 底 + 「忽略」钮）。点「忽略」＝去掉这条（removeFacet）——
    // 它是前端加的前端撤，没有后端抑制表一说；想再加回，下方分面 grid 点一下就是。
    const facetChips = _facetFilters.map((f) =>
        `<span class="fa-chip is-query" title="这条来自你后加的细化筛选（点「忽略」去掉这条）">`
        + `<span class="fa-k">${escapeHtml(f.label || f.dim)}</span>`
        + `<span class="fa-v">${escapeHtml(prettyFacetValue(f.display || f.value))}</span>`
        + `<button type="button" class="fa-toggle" data-fkind="facet" data-dim="${escapeHtml(f.dim)}" data-val="${escapeHtml(f.value)}" aria-label="忽略条件 ${escapeHtml(f.label || f.dim)}">忽略</button></span>`
    ).join("");
    active.innerHTML = `<span class="facet-applied-label">查询条件：</span>${queryChips}${facetChips}`;
    active.hidden = false;
    active.querySelectorAll(".fa-toggle").forEach((b) =>
        b.addEventListener("click", () => {
            if (b.dataset.fkind === "facet") removeFacet(b.dataset.dim, b.dataset.val);
            else toggleQueryHit(b.dataset.fid);
        }));
}

/* 渲染分面面板：
   上排「查询条件」= 本次查询命中的**原始硬约束** ＋ 用户后加的**细化筛选**，二者同款 chip
   （accent 底 + 「忽略」钮；忽略原始命中＝后端放宽该维度，忽略后加细化＝前端撤掉这条）。
   下排 = 各维度可点取值。仅当有命中 / 已细化 / 可细化维度时显示；空则整条收起。命中总数在结果计数旁提示。 */
export function renderFacets(data) {
    const bar = $("facetBar"), groups = $("facetGroups");   // chips 行渲染已收口进 renderActiveChips，它自取 #facetActive
    // 单一 choke point：一旦有结果被（重新）应用——真检索 / 缓存命中 / 撤销重做 / 回退查看，
    // 都会走到这里——就丢弃「数据细化」里还没提交的草稿，grid 重建成干净态。不散在各按钮里各自清一处。
    // 用 facetStageDiscardIfDirty 而非裸 `=null`：脏草稿被丢时 toast 一声（提交在途又攒新草稿这类并发下有真损失）。
    // 提交路径 facetStageSubmit 已在发起前把 _facetStage 置 null，故这里对它不误报。
    facetStageDiscardIfDirty();
    const facets = (data && data.facets) || [];
    const qc = (data && data.query_constraints) || [];   // 放宽后的原始命中：被忽略的维度已不在其中（仅当无忽略时=完整集）
    // 「库中匹配 N 条」计数已并入结果摘要卡（renderResultSummary 的方法句），此处不再单独渲染 #resultsTotal。

    // 「查询条件」框要做**实时命中镜像**（用户 2026-07-24）：每次都从后端最新 query_constraints 刷新——
    // 不再冻结（旧行为是忽略后冻住快照、好让被忽略的 chip 留在原位；现在被忽略的 chip 整条不渲染，
    // 冻结反而会让框显示「忽略之前」的过期条件，与「实时」直接冲突）。qc 已是「放宽后的原始命中」
    // （被忽略维度不在其中），刷新后 _queryHits 天然=当前生效集。还原路径（用户 2026-08-01：
    // 本框只展示当前命中的关键词，不再放恢复入口）：condBoard 的「已忽略」分区可逐条恢复
    // （从 filter_id 还原条件名，不依赖 _queryHits，见 board_core cbRowsFrom），整批还原则走
    // 对话「回退至此」/ 撤销步进条的「恢复全部条件」。
    setFacetState({ queryHits: qc.map((g) => ({ filter_id: g.filter_id || g.dim, polarity: g.polarity || "include", dim: g.dim, label: g.label, values: (g.values || []).slice() })) });   // 重赋值经属主 setter（live binding 只读）

    // 「查询条件」＝**实时正在被命中的关键词镜像**（用户 2026-07-24；2026-08-01 收敛为「只展示
    // 当前命中的关键词」）：只显示当前真正在筛的条件（原始命中 + 后加的细化筛选），被忽略
    // （_suppressed）的原始命中**整条不渲染**（不再灰显残留），增删历史交给对话记录/「回退至此」。
    // _queryHits 快照仍保留被忽略的维度（撤销帧/条件板还原要用），只是渲染层过滤掉它们。
    // 渲染走 renderActiveChips（唯一渲染口）。
    renderActiveChips(_queryHits.filter((g) => !_suppressed.includes(g.filter_id || g.dim)));

    // 可细化维度分组
    if (facets.length) {
        groups.innerHTML = facets.map((g) => {
            const vals = g.values.map((v) =>
                `<button type="button" class="facet-chip" data-dim="${escapeHtml(g.dim)}" data-val="${escapeHtml(v.value)}"`
                + ` data-disp="${escapeHtml(v.display)}" data-label="${escapeHtml(g.label)}" title="${escapeHtml(v.display)}">`
                + `<span class="fc-t">${escapeHtml(prettyFacetValue(v.display))}</span><span class="fc-n">${v.count}</span></button>`
            ).join("");
            return `<div class="fg-row"><span class="fg-label">${escapeHtml(g.label)}</span><div class="fg-vals">${vals}</div></div>`;
        }).join("");
        groups.querySelectorAll(".facet-chip").forEach((b) =>
            b.addEventListener("click", () => toggleFacet(b.dataset.dim, b.dataset.val, b.dataset.disp, b.dataset.label)));
    } else { groups.innerHTML = ""; }

    // 整条：有查询命中约束 / 已细化筛选 / 可细化维度，任一即显示
    bar.hidden = !(qc.length || _facetFilters.length || facets.length || _suppressed.length);
    renderFacetStage();   // grid 刚重建 → 同步暂存高亮 + 提交条（此刻 _facetStage 已在顶部清空，故为干净态）
    placeFacetBar();   // 同步结果头行「筛选」入口钮显隐 + 收口 swSync（分面条本体恒住抽屉，不搬家）
}
/* 分面条的落位（2026-10-11 rp2 起）：#facetBar 恒住右侧筛选抽屉 #facetDrawerBody，**不再搬家**
   （单通道：桌面 / 移动 / 交换态同一抽屉；旧版「侧栏面板 ↔ 结果区上方」双宿主退役）。
   本函数只剩两件事：① 结果头行「筛选」入口钮显隐——唯一判据 = renderFacets 的 bar.hidden
   （有查询命中 / 已细化 / 可细化维度 / 被忽略项任一即有内容）；② 收口 swSync——
   shell.js 收展侧栏/切视图、results.js/search.js 重置等调用方都指望这条链刷新侧栏工作卡。
   刻意不加 typeof 守卫：board.js 在 boot.js 之前加载，运行期必定已定义；
   加守卫只会把「函数名打错」变成永久静默短路。 */
export function placeFacetBar() {
    const bar = $("facetBar"); if (!bar) return;
    const btn = $("facetOpenBtn");
    if (btn) btn.hidden = bar.hidden;
    swSync();
}

/* ============ 筛选抽屉（2026-10-11 rp2）============
   「筛选」自侧栏页签摘出：唯一入口在结果头行 #facetOpenBtn，面板从屏幕右侧推入。
   复用设置抽屉的 .drawer 范式（fixed 右贴、translateX 滑入、inert/aria-hidden 配对），
   但**非模态**：不加遮罩、不进 trapDialogFocus——旧侧栏面板本与结果同屏并存，边看结果边挑条件。
   关闭路径（× 钮 / Esc / 提交）都收口到 facetDrawerSet(false)：有未提交草稿先 toast 丢弃（不静默丢失）。 */
export function facetDrawerIsOpen() { const d = $("facetDrawer"); return !!(d && d.classList.contains("open")); }
export function facetDrawerSet(open) {
    const d = $("facetDrawer");
    if (!d || open === facetDrawerIsOpen()) return;
    if (open) {
        d.removeAttribute("inert");
        d.setAttribute("aria-hidden", "false");
        d.classList.add("open");
        requestAnimationFrame(() => { const c = $("facetDrawerClose"); if (c) c.focus(); });
    } else {
        facetStageDiscardIfDirty();   // 旧版在「搬出侧栏」分支丢弃草稿，现收口到关抽屉
        d.classList.remove("open");
        d.setAttribute("aria-hidden", "true");
        d.setAttribute("inert", "");
        const btn = $("facetOpenBtn");
        if (btn && !btn.hidden) btn.focus();
    }
}
