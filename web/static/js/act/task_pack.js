"use strict";

/* C4 起本文件是 ES Module：core 的工具、shell 的 getConfig、interactions 的 queryForRetrieval/
   getDateRange、search 的 LAST_RECOMMEND_DATA（活绑定只读）、results 的三个分面状态（活绑定只读）、
   usage_log/usage_core/act_core 经 import 取。_tpPlan/_tpChosen 导出成活绑定供 act.js 只读
   （同 LAST_RECOMMEND_DATA 例）。results.js/search.js 经 import 取
   resetTaskPack/syncTaskPackBar（C5 起绞杀桥全退役）。
   ：本文件同时承载「真实数据下载」UX——/api/download/plan 分级 →
   主按钮「直接下载真实数据」（确认条 → start → 1s 轮询 → 终态摘要），任务包 zip 降为次按钮兜底。 */
import { API, $, downloadBlobAs, escapeHtml, sampleFrom, toast } from "#core";
import { getConfig, webGuardOn } from "#shell";
import { queryForRetrieval, getDateRange } from "#interactions";
import { LAST_RECOMMEND_DATA } from "#search";
import { _facetFilters, _suppressed, _lenientDims } from "#results";
import { usageLog } from "#usage_log";
import { USAGE_KINDS } from "#usage_core";
import { tpBytes } from "#act_core";

/* 一句话任务包的界面层：先看清单，确认后再产包。

   刻意不做「一句话直接产包」：用户没勾过任何东西就打包，等于替他决定了「哪几条算数」，
   而这份材料是要拿去投稿的。先把「会收录什么、装不了什么、缺什么」摊开给他看。

   面板里坏消息排在最前面——装不了什么、只取主文件的声明，都在四档计数之前。

   ## 真实下载：主/次双按钮

   面板底部现在有两颗按钮，语义完全不同：
   - 主按钮「直接下载真实数据」（只有 /api/download/plan 判定有 supported 条目时才显示）：
     把**真实数据文件**经服务端直接下载到本机下载目录（~/Downloads/BioData数据-<时间戳>/，
     每个数据集一个子文件夹，下载完做 md5/大小校验）。确认条 → start → 1s 轮询 → 终态摘要。
   - 次按钮「仍生成任务包（清单/下载脚本/引文）」：走既有 zip 链不变，是主按钮的兜底。

   诚实约束与 同款：行动流只放关键行、长明细收 <details>；同一时刻只允许一个下载任务
   （start 409 时如实提示「有下载任务进行中」并给「查看进度」回跳）；终态里 md5_mismatch /
   skipped / error 的文件逐条列出，绝不报成「全部成功」。 */

export let _tpPlan = null;      // 上一次预览拿到的清单（act.js 经活绑定只读：回执要复述口径行）
export let _tpChosen = new Set();
let _tpSeq = 0;          // 并发代号：晚到的旧预览一律丢弃
let _tpBusy = false;
/* 用户这次说的条数与实际用上的条数。两者不等必须**显式告诉用户**，不许静默偏离。
   {said, used} 都是 0 表示这次没有从话里读出条数（走默认口径）。 */
let _tpCount = { said: 0, used: 0 };

/* ---- 真实下载状态。状态单一真源在这里，renderTaskPackPlan 每次重渲染都照它画下载区。---- */
let _dl = {
    plan: null,        // 最近一次下载分级响应缓存（items/unsupported/totalBytes/uids）；null=未查/待重查
    planSeq: 0,        // plan 请求代号：晚到的旧 plan 响应一律丢弃（uids 变了就作废）
    planError: "",     // plan 查询失败的原因（非空时下载区只给诚实错误 + 次按钮）
    stage: "idle",     // idle | confirm（确认条）| running（下载中）| done | cancelled | error
    job: null,         // {job_id, dir, total_bytes}（start 成功后）
    status: null,      // 最近一次下载状态快照（终态渲染与进度行共用）
    lastJobId: "",     // 本会话最近一次成功 start 的 job_id（409「查看进度」回跳用）
    note: "",          // 一般提示行（如「有下载任务进行中」）
    startBusy: false,  // start 请求在途（防双击）
    timer: null,       // 轮询定时器句柄
    /* dl-auto-1（运行中可增删）：在途下载队列里的数据集编号集合（「更新下载」差量的基准）。
       start 成功时置为本次 uids；update 响应后按队列真源刷新。勾选=目标集，运行时勾选只改
       差量、不即时提交；两者不一致时下载区出现「更新下载」提交按钮。 */
    runningUids: [],
};

function dlReset() {
    if (_dl.timer) { clearTimeout(_dl.timer); _dl.timer = null; }
    _dl.plan = null; _dl.planError = ""; _dl.stage = "idle"; _dl.job = null;
    _dl.status = null; _dl.note = ""; _dl.startBusy = false;
}

export function resetTaskPack() {
    /* 守卫（验证抓到）：下载进行中，任何「结果重渲/换查询/检索失败」触发的 resetTaskPack
       都不得抹掉下载进度 UI。晚到的检索落地（慢 LLM 的 /api/recommend 可能十几秒才回）会走
       renderResults → syncTaskPackBar → 这里清空面板 + 杀轮询——用户正在看的下载进度瞬间消失、
       `_dl.timer` 被 dlReset 清掉、服务端线程照下但界面永远停在半截。下载期间跳过整个重置：
       panel 保留进度、轮询继续，新结果照常在结果区落地；下载终态仍能正常渲染。 */
    if (_dl.stage === "running") return;
    dlReset();
    _tpPlan = null;
    _tpChosen = new Set();
    _tpBusy = false;
    _tpCount = { said: 0, used: 0 };
    const panel = $("taskPackPanel");
    if (panel) { panel.hidden = true; panel.innerHTML = ""; }
    const btn = $("taskPackBtn");
    if (btn) btn.hidden = true;
}

/* 候选池只有 10/20/50 三档（服务端 ALLOWED_LIMITS），但**勾选可以是候选池的任意子集**——
   `plan_spec` 刻意不含 selected_uids，产包时服务端按 selected_uids 重建。
   于是「前5条」不必把档位改成 5：取 10 条的池子、只勾前 5 条即可，且这 5 条就是确定性排序前 5 条。 */
const TP_POOL_TIERS = [10, 20, 50];
export function tpPoolFor(count) {
    for (let i = 0; i < TP_POOL_TIERS.length; i += 1) {
        if (TP_POOL_TIERS[i] >= count) return TP_POOL_TIERS[i];
    }
    return TP_POOL_TIERS[TP_POOL_TIERS.length - 1];
}

/* 每次有新结果落地都作废上一份清单。
   只在「没有结果」时才作废是不够的：搜 A → 打开面板拿到 A 的清单 → 改成搜 B → 有结果、
   按钮照常在 → 再点开面板，`_tpPlan` 还在，直接画出 **A 的清单**并允许照它产包。
   清单便宜（点开就重新预览一次），而拿着上一条查询的材料去投稿不便宜。 */
export function syncTaskPackBar(data) {
    const btn = $("taskPackBtn");
    if (!btn) return;
    const hasResults = !!(data && (data.results || []).length);
    resetTaskPack();
    btn.hidden = !hasResults;
}

/* 按钮只负责展开/收起面板；产包是面板底部那颗独立按钮。
   一个按钮既开面板又产文件，用户点第二下时不知道会发生什么。 */
function toggleTaskPackPanel() {
    const panel = $("taskPackPanel");
    if (!panel) return;
    if (!panel.hidden) { panel.hidden = true; return; }
    panel.hidden = false;
    // dl-auto-1：运行中重新打开面板 = 放弃未提交的改勾（差量没落地），勾选恢复成当前在途集。
    if (_dl.stage === "running" && _dl.runningUids.length) {
        _tpChosen = new Set(_dl.runningUids.slice());
        if (_tpPlan) { renderTaskPackPlan(_tpPlan); return; }
    }
    if (_tpPlan) { renderTaskPackPlan(_tpPlan); return; }
    previewTaskPack();
}

function tpRequestBody(extra) {
    const cfg = getConfig();
    const query = ($("queryInput").value || "").trim();
    const data = LAST_RECOMMEND_DATA || {};
    const rewritten = (data.audit && data.audit.used && data.audit.rewritten_query) || "";
    const body = Object.assign({
        query: queryForRetrieval(query),
        query_effective: rewritten,
        use_llm: false,
        sources: cfg.sources,
        auto_parse_sources: cfg.auto_parse_sources,
        facet_filters: _facetFilters.map(function (f) { return { dim: f.dim, value: f.value }; }),
        suppressed_constraints: _suppressed.slice(),
        lenient_dims: _lenientDims.slice(),
        limit: 10,
        scope: "primary"
    }, getDateRange(query));
    return Object.assign(body, extra || {});
}

/* 返回 `{ok, plan?, chosen?, error?, stale?}`。
   **必须有返回值**：调用方（尤其是「一句话直接执行」的派发器）要据此决定敢不敢接着产包，
   也要据此渲染回执——没有返回值就只能照「成功」渲染，断网/409/后端无命中全会被写成「已经打包好了」。 */
export async function previewTaskPack(opts) {
    opts = opts || {};
    const panel = $("taskPackPanel");
    if (!panel) return { ok: false, error: "页面上没有任务包面板" };
    // 这次话里说的条数（0 = 没说）。档位按它取，勾选按它截。
    const wantSaid = Math.max(0, parseInt(opts.count, 10) || 0);
    const want = wantSaid > 50 ? 50 : wantSaid;
    const myGen = ++_tpSeq;
    /* pack1：预览**不再自动打开面板**（原 panel.hidden=false 在此 + renderTaskPackPlan 末尾，
       连「下载top5」这类自动打包都会把面板弹到用户脸上；且用户手动关面板后迟到的 fetch 会把它重开
       —— 起在册的 race，随本处一并消掉）。显式开面板的调用方（toggleTaskPackPanel /
       actFixClick panel 分支 / cbRouteAsFirstBox / actRunPackPreview 的 pack.preview 分流）
       都是自己先 unhide 再调本函数，零影响。 */
    panel.innerHTML = '<p class="tp-loading">正在整理这一批数据能打包成什么…</p>';
    let payload = null;
    try {
        const body = tpRequestBody({
            limit: want ? tpPoolFor(want) : (opts.limit || 10),
            keep_selected: want ? [] : Array.from(_tpChosen)
        });
        const res = await fetch(API.taskPackPreview, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
        });
        payload = await res.json();
        if (!res.ok || !payload.ok) throw new Error(payload.detail || "没能整理出这一批的清单");
    } catch (err) {
        if (myGen !== _tpSeq) return { ok: false, stale: true, error: "已被更晚的一次预览取代" };
        // **失败必须作废上一份清单**。以前这里只改 panel 就 return，`_tpPlan` 原封不动——
        // 而 buildTaskPack 的全部前置条件只有 `_tpPlan && _tpChosen.size`。于是「预览失败 → 接着产包」
        // 会拿**上一条查询**的候选池产出一个货真价实的 zip，四道 409 锁一个都不会响
        //（它们锁的是「回传参数与重跑自洽」，而回传的整套参数都是旧的、完全自洽）。
        // 今天这条路走不通只是因为错误面板里没渲染产包按钮；一旦有代码直接调 buildTaskPack 就绕开了。
        _tpPlan = null; _tpChosen = new Set(); _tpCount = { said: 0, used: 0 };
        const msg = String((err && err.message) || err);
        panel.innerHTML = '<p class="tp-msg">没能整理出这一批的清单：' + escapeHtml(msg) + "</p>";
        return { ok: false, error: msg };
    }
    if (myGen !== _tpSeq) return { ok: false, stale: true, error: "已被更晚的一次预览取代" };
    await tpIdentityLookup();   // ：同名行身份锚点（发表日期）用的全库索引；本地服务亚秒级，失败静默降级
    if (myGen !== _tpSeq) return { ok: false, stale: true, error: "已被更晚的一次预览取代" };
    if (!payload.plan) {
        _tpPlan = null; _tpChosen = new Set(); _tpCount = { said: 0, used: 0 };
        const msg = payload.message_zh || "这次检索没有可以打包的内容。";
        panel.innerHTML = '<p class="tp-msg">' + escapeHtml(msg) + "</p>";
        return { ok: false, error: msg };
    }
    _tpPlan = payload.plan;
    const pool = _tpPlan.candidate_uids || [];
    if (want) {
        // 用户明确说了条数 → 只勾前 want 条（少选合法）。够不够得看池子实际有多少，见 _tpCount。
        _tpChosen = new Set(pool.slice(0, want));
    } else {
        const keep = payload.keep_selected || [];
        _tpChosen = new Set(keep.length ? keep : pool);
    }
    _tpCount = { said: wantSaid, used: want ? _tpChosen.size : 0 };
    renderTaskPackPlan(_tpPlan, payload);
    return { ok: true, plan: _tpPlan, chosen: _tpChosen.size, count: Object.assign({}, _tpCount) };
}

/* 「你说的条数」和「实际用上的条数」不一致时说清楚。两种不一致，措辞不同：
     · 说了 80 → 一次最多 50（被钳）
     · 说了 20 但这次只检索到 12 条 → 池子里就这么多
   静默按 min(说的, 实际) 执行而不吭声，正是本项目反复修过的「静默偏离」。 */
function tpCountNoteZh() {
    const said = _tpCount.said, used = _tpCount.used;
    if (!said || said === used) return "";
    if (said > 50 && used === 50) return "你说的是 " + said + " 条，一次最多打包 50 条，这次装了 50 条。";
    if (said > 50) return "你说的是 " + said + " 条，一次最多打包 50 条；这次实际只有 " + used + " 条可装。";
    return "你说的是 " + said + " 条，这次检索只有 " + used + " 条可装，装了 " + used + " 条。";
}

/* 面板行的身份锚点（普查）：同名数据集（uid …-ff-ultima 与 …-ff-ultima-4，同名同来源）
   在行里长成无法区分的两行——12/14 的文件数差异埋在 tier_evidence 长句里，算不上身份。
   预览投影（webapp _preview_projection）不带日期/样本量字段，而候选池是服务端**确定性排序**
   截的（可复现口径，见面板脚注），与屏幕展示集（可开大模型重排、top_k 不同）不是同一刀——
   实测池里的 -4 条根本不在展示结果里，从 LAST_RECOMMEND_DATA 借身份会漏。故用全库真源
   /api/datasets（browse.js 本就整取它）：本地服务一次拉取、模块级缓存，面板开前 await。
   每条取最紧凑的身份——发表日期（这对同名记录是 vs；只给年份 2025
   区分不开，必须全日期），缺日期退样本量（count+unit 按 core.sampleFrom 客户端格式化，
   与后端 format_sample_size 逐位对齐 F8）。拉取失败静默降级：行维持原样，不硬编、不报错。 */
let _tpIdentityMap = null;      // Map<uid, string>；null=未拉取
let _tpIdentityPromise = null;
function tpIdentityLookup() {
    if (_tpIdentityMap) return Promise.resolve(_tpIdentityMap);
    if (_tpIdentityPromise) return _tpIdentityPromise;
    _tpIdentityPromise = fetch(API.datasets)
        .then(function (res) { return res.json(); })
        .then(function (d) {
            const map = new Map();
            ((d && d.records) || []).forEach(function (r) {
                const uid = String(r.dataset_uid || "");
                if (!uid || map.has(uid)) return;
                const date = String(r.published_date || "").slice(0, 10);
                if (date) { map.set(uid, "发表 " + date); return; }
                const sample = String(r.count || "").trim() ? sampleFrom(r.count, r.unit) : "";
                if (sample) map.set(uid, "样本量 " + sample);
            });
            _tpIdentityMap = map;
            return map;
        })
        .catch(function () { _tpIdentityPromise = null; return new Map(); });   // 下次开面板重试
    return _tpIdentityPromise;
}
function tpRowIdentity(uid) {
    return (_tpIdentityMap && _tpIdentityMap.get(String(uid || ""))) || "";
}

/* 面板里的每一个数字都必须描述**当前勾选的那几条**。
   以前只有按钮上的数字跟着勾选走，「只取主文件」那句话、四档计数、装不了什么全停在候选池口径——
   勾掉一个带 FASTQ 的数据集，屏幕上仍写着「其中原始测序数据 3 个」，用户会以为它们还在包里。
   凡是随勾选变的，一律在这里按 `_tpChosen` 重算；不随勾选变的（口径政策、候选池怎么截的）
   照实标明是「候选池」口径，不混着说。 */
function renderTaskPackPlan(plan, payload) {
    const panel = $("taskPackPanel");
    if (!panel) return;
    const items = (plan.items || []).filter(function (it) { return _tpChosen.has(it.dataset_uid); });
    let html = '<div class="tp-head"><strong>下载这批数据</strong>'
        + '<button type="button" class="btn" id="taskPackCloseBtn">✕ 关闭</button></div>';

    // 坏消息先说——但只说勾选里的那几条。
    const cannot = (plan.cannot_include || []).filter(function (m) { return _tpChosen.has(m.dataset_uid); });
    if (cannot.length) {
        html += '<div class="tp-cannot"><strong>装不了什么</strong><ul>'
            + cannot.map(function (m) {
                return "<li>" + escapeHtml(m.dataset_name) + "（" + escapeHtml(m.source) + "）："
                    + escapeHtml(m.why_zh) + "</li>";
            }).join("") + "</ul></div>";
    }

    let files = 0, bytes = 0, excluded = 0;
    const tierCount = {}, tierRows = {};
    items.forEach(function (it) {
        files += it.rows_planned || 0;
        bytes += it.bytes_selected || 0;
        excluded += Math.max(0, (it.n_files_total || 0) - (it.n_files_selected || 0));
        tierCount[it.tier] = (tierCount[it.tier] || 0) + 1;
        tierRows[it.tier] = (tierRows[it.tier] || 0) + (it.rows_planned || 0);
    });

    const countNote = tpCountNoteZh();
    if (countNote) html += '<p class="tp-msg">' + escapeHtml(countNote) + "</p>";

    const allScope = String(plan.scope || "primary") !== "primary";
    html += '<p class="tp-primary">' + escapeHtml(plan.primary_only_policy_zh || plan.primary_only_zh) + "</p>";
    html += '<p class="tp-primary">已勾选 ' + items.length + " 个数据集，共 " + files + (allScope ? " 个文件" : " 个主文件")
        + (excluded > 0 ? ("；这几个数据集的来源清单里另有 " + excluded + " 个文件没有列入") : "")
        + "。</p>";

    html += '<div class="tp-tiers">' + (plan.tiers || []).map(function (t) {
        const count = tierCount[t.tier] || 0, rows = tierRows[t.tier] || 0;
        if (!count) return "";
        const note = (count > 0 && rows === 0) ? "（这一档有数据集，但没有为它们生成下载命令）" : "";
        return '<span class="tp-tier-chip" title="' + escapeHtml(t.explain_zh) + '">'
            + escapeHtml(t.label_zh) + " " + count + " 个 · 下载命令 " + rows + " 条"
            + escapeHtml(note) + "</span>";
    }).join("") + "</div>";

    if (payload && payload.message_zh) html += '<p class="tp-msg">' + escapeHtml(payload.message_zh) + "</p>";
    if (plan.ledger && plan.ledger.degrade_sentence_zh) {
        html += '<p class="tp-msg">' + escapeHtml(plan.ledger.degrade_sentence_zh) + "</p>";
    }

    html += '<div class="tp-rows">' + (plan.items || []).map(function (it) {
        const checked = _tpChosen.has(it.dataset_uid) ? " checked" : "";
        // dl-auto-1：运行中允许改勾——勾选只影响「待更新」差量集（不即时提交），
        // 与在途集不一致时下载区出现「更新下载」按钮；不再 disabled。
        // ：名称之外补一个最紧凑的身份锚点（发表日期），同名两行由此可区分；
        // 仍塞在同一个 .tp-row-meta 静音行里，不动行高与布局节奏。
        const identity = tpRowIdentity(it.dataset_uid);
        return '<label class="tp-row"><input type="checkbox" data-tp-uid="' + escapeHtml(it.dataset_uid) + '"'
            + checked + '><span class="tp-row-name">' + escapeHtml(it.dataset_name) + "</span>"
            + '<span class="tp-row-meta">' + escapeHtml(it.source)
            + (identity ? " · " + escapeHtml(identity) : "") + " · "
            + escapeHtml(it.tier_evidence) + "</span></label>";
    }).join("") + "</div>";

    // 这一段是按**整个候选池**算的（后端一次算好，逐条拆不开），所以标题里就把口径写死，
    // 不让它冒充「你勾的这几条需要确认的事」。
    html += '<details class="tp-todo"><summary>需要你自己确认的事（按候选池 '
        + (plan.candidate_uids || []).length + " 条统计，共 " + (plan.todo || []).length
        + " 条）</summary><ul>"
        + (plan.todo || []).map(function (t) { return "<li>" + escapeHtml(t) + "</li>"; }).join("")
        + "</ul></details>";

    //  主/次双按钮：次按钮（任务包 zip 兜底）恒在，label 明确「真实数据共约 X」——
    // 这是清单里文件的真实体积（download_plan 逐文件 bytes 求和），不是 zip 体积（B 文案修正）。
    const packLabel = files > 0
        ? ("仍生成任务包（" + items.length + " 个数据集 · " + files
           + (allScope ? " 个文件" : " 个主文件") + " · 真实数据共约 " + tpBytes(bytes) + "）")
        : "这几条都没有可下载的文件，包里不会有下载命令（仍会生成清单、FAIR 与引文）";
    html += '<div class="tp-actions"><button type="button" class="btn tp-build-btn" id="taskPackBuildBtn"'
        + (_dl.stage === "running" ? " disabled" : "") + ">"
        + escapeHtml(packLabel) + "</button></div>";
    // 排序口径必须说清楚：候选池是服务端用**不含大模型重排**的确定性检索截的，
    // 页面上那一屏可能开着重排或向量召回。不说，用户会默认「包里就是我看到的前几条」。
    html += '<p class="tp-foot">本包由检索语句「' + escapeHtml(plan.retrieval_params.query || "")
        + '」在 ' + escapeHtml(plan.retrieval.date) + " 生成，取的是不含大模型重排的确定性排序前 "
        + (plan.candidate_uids || []).length + " 条（这样这一包才能被原样复现）。"
        + "如果你在页面上开了大模型重排或向量召回，屏幕上的顺序可能与这里不同；"
        + "这也不是「最相关的几条」的保证。勾掉不要的不会让这份清单失效。</p>";
    // 真实下载区：插在动作区之后；状态单一真源是 _dl，重渲染只重画这一块。
    html += '<div id="tpDlZone"></div>';
    panel.innerHTML = html;
    renderTaskPackDownloadZone();
    /* pack1：渲染不接管显隐（原 panel.hidden=false 已删）——面板开不开由调用方决定；
       本函数既服务「面板开着时刷新清单」，也服务 pack.download 的隐藏预览。 */
}

/* ==================== 真实数据下载区 ====================
   面板「下载这批数据」下方的下载分区。状态单一真源是文件头的 `_dl`，
   本区所有渲染都只从它画、不维护第二份状态；每次重渲染都调 renderTaskPackDownloadZone。 */

/* 用当前勾选（_tpChosen）调 /api/download/plan 分级。
   返回 `{items, unsupported, totalBytes, uids}`（items 为空 = 全暂不支持）或 null（查询失败）。
   act.js 动词入口经 tpDownloadConfirm 复用；勾选变化后 _dl.plan 自动作废重查。 */
export async function tpDownloadPlan() {
    //  公网护栏硬化：护栏模式后端 download 系列端点一律 403（服务端不代下），
    // 前端直接不发分级请求、不渲染下载区——任务包 zip 路径不受影响。
    if (webGuardOn()) { _dl.plan = null; return null; }
    const uids = Array.from(_tpChosen);
    if (!uids.length) { _dl.plan = null; return null; }
    const mySeq = ++_dl.planSeq;
    try {
        const res = await fetch(API.downloadPlan, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ uids })
        });
        const data = await res.json();
        if (mySeq !== _dl.planSeq) return null;   // 勾选已被更新 → 旧响应作废（不落盘状态）
        if (!res.ok || !data.ok) throw new Error(data.message_zh || data.detail || "没能确认哪些可以直接下载");
        _dl.plan = {
            items: data.items || [],
            unsupported: data.unsupported || [],
            totalBytes: Number(data.total_bytes) || 0,
            uids: uids,
        };
        _dl.planError = "";
        renderTaskPackDownloadZone();
        return _dl.plan;
    } catch (err) {
        if (mySeq !== _dl.planSeq) return null;
        _dl.plan = null;
        _dl.planError = String((err && err.message) || err);
        renderTaskPackDownloadZone();
        return null;
    }
}

/* act.js 动词入口（pack.download）用：preview 后把面板下载区推进到「确认条」等用户点开始。
   返回分级信息供行动流播报：{ok:true, supported, unsupported, totalBytes}
   或 {ok:false, reason:"none_supported"|"plan_failed", unsupported?}（由调用方诚实降级走任务包）。 */
export async function tpDownloadConfirm() {
    const dl = await tpDownloadPlan();
    if (!dl) return { ok: false, reason: "plan_failed" };
    if (!dl.items.length) return { ok: false, reason: "none_supported", unsupported: dl.unsupported.length };
    _dl.stage = "confirm";
    _dl.note = "";
    renderTaskPackDownloadZone();
    return {
        ok: true, supported: dl.items.length, unsupported: dl.unsupported.length,
        totalBytes: dl.totalBytes,
        uids: dl.items.map(function (it) { return it.dataset_uid; }),
    };
}

function dlZoneHtml(zone, html) { zone.innerHTML = html; }

/* _dl.plan 是否还对应当前勾选（勾选变了就要重查 plan）。 */
function dlPlanMatchesChosen() {
    const p = _dl.plan;
    if (!p) return false;
    const now = Array.from(_tpChosen).sort();
    const prev = (p.uids || []).slice().sort();
    return now.length === prev.length && now.every(function (u, i) { return u === prev[i]; });
}

function renderTaskPackDownloadZone() {
    const zone = $("tpDlZone");
    if (!zone) return;
    //  公网护栏硬化：护栏模式隐藏整个真实下载区（含「直接下载真实数据」主按钮）。
    if (webGuardOn()) { dlZoneHtml(zone, ""); return; }
    const st = _dl;
    if (st.stage === "confirm") { dlZoneHtml(zone, dlConfirmHtml()); return; }
    if (st.stage === "running") { dlZoneHtml(zone, dlRunningHtml()); return; }
    if (st.stage === "done" || st.stage === "cancelled" || st.stage === "error") {
        dlZoneHtml(zone, dlFinalHtml()); return;
    }
    // idle：分级 + 主按钮
    if (st.planError) {
        dlZoneHtml(zone, '<p class="tp-msg">没能确认哪些能直接下载：' + escapeHtml(st.planError)
            + "（仍可生成任务包）</p>");
        return;
    }
    if (!st.plan || !dlPlanMatchesChosen()) {
        if (_tpChosen.size) {
            dlZoneHtml(zone, '<p class="tp-msg">正在确认哪些可以直接下载真实数据…</p>');
            tpDownloadPlan();
        } else {
            dlZoneHtml(zone, "");
        }
        return;
    }
    const plan = st.plan;
    const supported = plan.items.length;
    let html = '<div class="tp-dl-tier">';
    if (supported) {
        html += "<strong>" + supported + " 个数据集可直接下载真实数据（共约 "
            + tpBytes(plan.totalBytes) + "）</strong>";
    } else {
        html += "<strong>这批暂不支持直接下载</strong>";
    }
    if (plan.unsupported.length) {
        html += "<br><span>" + plan.unsupported.length + " 个暂不支持（"
            + plan.unsupported.map(function (u) { return escapeHtml(u.title || u.dataset_uid); }).join("、")
            + "）</span>"
            + '<details class="tp-todo"><summary>为什么这 ' + plan.unsupported.length + " 个下不了</summary><ul>"
            + plan.unsupported.map(function (u) {
                return "<li>" + escapeHtml(u.title || u.dataset_uid) + "：" + escapeHtml(u.reason || "未知原因") + "</li>";
            }).join("") + "</ul></details>";
    }
    html += "</div>";
    if (st.note) html += '<p class="tp-msg">' + escapeHtml(st.note) + "</p>";
    if (supported) {
        html += '<div class="tp-actions"><button type="button" class="btn btn-primary" id="tpDlStartBtn">'
            + "直接下载真实数据（" + supported + " 个数据集 · 共约 " + tpBytes(plan.totalBytes) + "）</button></div>";
    }
    if (st.note && st.lastJobId) {
        html += '<p class="tp-foot"><button type="button" class="btn" id="tpDlViewBtn">查看进行中的下载进度</button></p>';
    }
    dlZoneHtml(zone, html);
}

/* 确认条：start 成功后才返回真实 dir，确认条只能诚实展示「默认下载目录的生成规则」。 */
function dlConfirmHtml() {
    const st = _dl;
    const plan = st.plan;
    const n = (plan && plan.items.length) || 0;
    const total = tpBytes((plan && plan.totalBytes) || 0);
    return '<div class="tp-dl-confirm"><p>将把 ' + n + " 个数据集的真实文件（共约 " + total
        + "）直接下载到本机默认下载目录（~ 的 Downloads 里的 BioData数据 文件夹，"
        + "形如 BioData数据-20260819-120000；每个数据集一个子文件夹；下载完做 md5/大小校验）。"
        + "真实目录以开始下载后返回的为准。</p>"
        + '<div class="tp-actions"><button type="button" class="btn btn-primary" id="tpDlConfirmBtn">开始下载</button>'
        + '<button type="button" class="btn" id="tpDlConfirmCancelBtn">取消</button></div>'
        + (st.startBusy ? '<p class="tp-msg">正在启动下载…</p>' : "") + "</div>";
}

/* dl-auto-1：在途队列的权威 uid 集合（_dl.runningUids，start/update 维护）。 */
function dlRunningUids() {
    return (_dl.runningUids || []).slice();
}

/* 运行中的「待更新」差量：目标集（_tpChosen，含用户改勾）与在途集（runningUids）不一致的部分。
   只计算不提交——点「更新下载」才把它发给 /api/download/update。 */
function dlPendingChanges() {
    const running = new Set(dlRunningUids());
    const add = [];
    const remove = [];
    _tpChosen.forEach(function (uid) { if (!running.has(uid)) add.push(uid); });
    running.forEach(function (uid) { if (!_tpChosen.has(uid)) remove.push(uid); });
    return { hasChanges: add.length > 0 || remove.length > 0, add: add, remove: remove };
}

function dlRunningHtml() {
    const st = _dl;
    const s = st.status || st.job || {};
    const files = (s.files || []);
    const doneFiles = files.filter(function (f) {
        return f.status !== "pending" && f.status !== "downloading";
    }).length;
    const done = Number(s.done_bytes) || 0;
    const total = Number(s.total_bytes) || ((st.plan && st.plan.totalBytes) || 0);
    const bar = total > 0 ? Math.min(100, Math.round(done / total * 100)) : 0;
    let html = '<div class="tp-dl-progress"><p class="tp-msg">正在下载 ' + doneFiles + "/" + files.length
        + " 个文件 · " + tpBytes(done) + "/" + tpBytes(total) + "</p>"
        + '<div class="tp-bar"><i style="width:' + bar + '%"></i></div>'
        + '<p class="tp-foot">保存到：' + escapeHtml(String(s.dir || "")) + "</p>";
    const work = dlPendingChanges();
    if (work.hasChanges) {
        // 运行中可勾选/取消勾选并点「更新下载」增删条目：提示差量与「重开面板恢复在途集」的退路
        html += '<p class="tp-msg">已勾选与在途下载不一致：'
            + (work.add.length ? "将新增 " + work.add.length + " 个数据集；" : "")
            + (work.remove.length ? "将去掉 " + work.remove.length + " 个数据集" : "")
            + "<br>点「更新下载」提交后生效；不想改就重新打开面板，勾选会恢复成当前在途集。</p>";
    }
    if (st.note) html += '<p class="tp-msg">' + escapeHtml(st.note) + "</p>";
    html += '<div class="tp-actions">';
    if (work.hasChanges) {
        html += '<button type="button" class="btn btn-primary" id="tpDlUpdateBtn">更新下载（+'
            + work.add.length + " / -" + work.remove.length + "）</button>";
    }
    html += '<button type="button" class="btn" id="tpDlStopBtn">取消下载</button></div></div>';
    return html;
}

/* 终态文件统计（纯函数，node 行为门直接执行）：文件状态 → 分组计数 + 成功字节合计。
   所有终态文案都从它取数，保证「报告的数字」与「实际状态」同源。 */
function dlFileStats(files) {
    const stats = { ok: [], sizeOk: [], mismatch: [], skipped: [], errs: [], cancelled: [], doneBytes: 0 };
    (files || []).forEach(function (f) {
        const st = f.status;
        if (st === "ok") { stats.ok.push(f); stats.doneBytes += Number(f.bytes) || 0; }
        else if (st === "size_ok") { stats.sizeOk.push(f); stats.doneBytes += Number(f.bytes) || 0; }
        else if (st === "md5_mismatch") { stats.mismatch.push(f); }
        else if (st === "skipped") { stats.skipped.push(f); }
        else if (st === "error") { stats.errs.push(f); }
        else if (st === "cancelled") { stats.cancelled.push(f); }
    });
    return stats;
}

/* 终态摘要：done/cancelled/error 三套诚实话术；md5_mismatch/skipped/error 逐条列出，不报「全部成功」。 */
function dlFinalHtml() {
    const st = _dl;
    const s = st.status || {};
    const files = s.files || [];
    const dir = String(s.dir || "");
    const stat = dlFileStats(files);
    const ok = stat.ok, sizeOk = stat.sizeOk, mismatch = stat.mismatch;
    const skipped = stat.skipped, errs = stat.errs;
    const doneBytes = stat.doneBytes;
    const unsupported = ((st.plan && st.plan.unsupported) || []).filter(function (u) {
        return _tpChosen.has(u.dataset_uid);
    });
    let html = "";
    if (st.stage === "done") {
        html += '<p class="tp-msg"><strong>已下载到 ' + escapeHtml(dir) + "：" + (ok.length + sizeOk.length)
            + " 个文件 " + tpBytes(doneBytes) + "；md5 校验 " + ok.length + " 个通过"
            + (sizeOk.length ? "，" + sizeOk.length + " 个仅核对大小一致" : "") + "。</strong></p>";
        const problems = mismatch.concat(skipped).concat(errs);
        if (problems.length) {
            html += '<details class="tp-todo"><summary>有 ' + problems.length
                + " 个文件没有干净地下载成功（逐条如下）</summary><ul>"
                + problems.map(function (f) {
                    return "<li>" + escapeHtml(f.filename || "")
                        + "（" + escapeHtml(f.dataset_title || f.dataset_uid || "") + "）："
                        + escapeHtml(f.error || f.status) + "</li>";
                }).join("") + "</ul></details>";
        }
        if (unsupported.length) {
            html += '<p class="tp-msg">另 ' + unsupported.length + " 个数据集暂不支持直接下载，可以生成这部分的任务包。</p>"
                + '<div class="tp-actions"><button type="button" class="btn btn-primary" id="tpDlPackBtn">生成这部分的任务包</button></div>';
        }
    } else if (st.stage === "cancelled") {
        html += '<p class="tp-msg"><strong>已取消，已下载的部分保留在 ' + escapeHtml(dir) + "。</strong></p>";
        const doneNow = ok.concat(sizeOk).length;
        if (doneNow) html += '<p class="tp-foot">其中 ' + doneNow + " 个文件已完成下载。</p>";
    } else {
        html += '<p class="tp-msg"><strong>下载失败：' + escapeHtml(s.error || st.note || "未知错误") + "。</strong></p>"
            + '<p class="tp-foot">已下载的部分保留在 ' + escapeHtml(dir || "原目标目录")
            + "；可改用任务包。</p>";
    }
    if (st.stage === "error" || st.stage === "cancelled") {
        html += '<div class="tp-actions"><button type="button" class="btn" id="tpDlBackBtn">返回</button></div>';
    }
    return html;
}

/* 面板「开始下载」按钮的薄包装：取当前分级的 uids，交给可复用的 tpDownloadStart 内核。
   手动发起路径仍保留确认条（tpDlStartBtn → ConfirmBtn → 本函数），与 act.js 自动开始共用
   同一套启动逻辑。 */
async function dlStart() {
    const st = _dl;
    const plan = st.plan;
    if (!plan || !plan.items.length) return;
    const uids = plan.items.map(function (it) { return it.dataset_uid; });
    await tpDownloadStart(uids);
}

/* 真实下载启动内核（dl-auto-1 抽出供 act.js runner 复用）：调 /api/download/start → stage 置
   running → 记 usage → 起 1s 轮询。返回 `{ok:true, job_id, dir, total_bytes, n}` 或
   `{ok:false, conflict?, message}`——调用方（面板按钮 / act.js）据此如实渲染，绝不伪造成功。
   面板未打开也可调用：renderTaskPackDownloadZone 在 tpDlZone 不存在时是 no-op，`_dl` 状态与
   dlPoll 轮询不依赖面板 DOM，用户之后打开面板能看到 running 态与真实进度。 */
export async function tpDownloadStart(uids) {
    const st = _dl;
    const list = Array.isArray(uids) ? uids : [];
    if (!list.length || st.startBusy) {
        return { ok: false, message: "没有可下载的数据集" };
    }
    st.startBusy = true;
    renderTaskPackDownloadZone();
    try {
        const res = await fetch(API.downloadStart, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ uids: list })
        });
        const data = await res.json();
        if (res.status === 409) {
            // 同一时刻只允许一个下载任务：如实提示 + 给「查看进度」回跳（本会话曾 start 过才有 job_id）
            st.startBusy = false;
            st.stage = "idle";
            st.note = data.message_zh || "有下载任务进行中";
            renderTaskPackDownloadZone();
            return { ok: false, conflict: true, message: st.note };
        }
        if (!res.ok || !data.ok) {
            st.startBusy = false;
            st.stage = "idle";
            st.note = (data.message_zh || data.detail || "下载没能开始") + "（仍可生成任务包）";
            renderTaskPackDownloadZone();
            return { ok: false, message: (data.message_zh || data.detail || "下载没能开始") };
        }
        st.startBusy = false;
        st.lastJobId = data.job_id || "";
        st.job = { job_id: data.job_id, dir: data.dir, total_bytes: data.total_bytes };
        st.status = null;
        st.stage = "running";
        st.runningUids = list.slice();
        renderTaskPackDownloadZone();
        //  dl what:"script"：UI 里没有独立的「下载脚本」动作——脚本在任务包 zip 内
        // （pack 已记）；真实下载由服务端下载器执行，正是原「下载脚本自己跑」能力的在线形态，
        // 故在这个语义等价点接线（「下载脚本」语义标签的等价映射）。只在真启动成功后记。
        usageLog(USAGE_KINDS.dl, { what: "script", n: list.length });
        dlPoll();
        return { ok: true, job_id: data.job_id, dir: data.dir, total_bytes: data.total_bytes, n: list.length };
    } catch (err) {
        st.startBusy = false;
        st.stage = "idle";
        st.note = "没能启动下载：" + String((err && err.message) || err) + "（仍可生成任务包）";
        renderTaskPackDownloadZone();
        return { ok: false, message: "没能启动下载：" + String((err && err.message) || err) };
    }
}

/* 1s 轮询。状态查询失败（网络抖动）不结束任务，1s 后重试；终态才停表。 */
async function dlPoll() {
    const st = _dl;
    if (st.timer) { clearTimeout(st.timer); st.timer = null; }
    if (st.stage !== "running" || !st.job) return;
    let s = null;
    try {
        const res = await fetch(API.downloadStatus + "?job=" + encodeURIComponent(st.job.job_id));
        s = await res.json();
        if (res.status === 404 || !s.ok) throw new Error("任务不存在或状态不可读");
    } catch (_e) {
        st.timer = setTimeout(dlPoll, 1000);
        return;
    }
    st.status = s;
    if (s.state === "running") {
        renderTaskPackDownloadZone();
        st.timer = setTimeout(dlPoll, 1000);
        return;
    }
    st.stage = s.state === "cancelled" ? "cancelled" : (s.state === "error" ? "error" : "done");
    st.note = "";
    st.timer = null;
    renderTaskPackDownloadZone();
}

/* 更新提示（dl-auto-1）：把服务端确实处理过的增删如实报给用户；拒绝/不在队列的也点名。 */
function dlUpdateNote(work, data) {
    const added = (data.added || []).filter(function (a) { return a.status === "added"; }).length;
    const removed = (data.removed || []).length;
    const rejected = (data.rejected || []).length;
    const unsupported = (data.added_unsupported || []).length;
    let note = "已更新下载队列：" + (added ? "新增 " + added + " 个数据集；" : "")
        + (removed ? "去掉 " + removed + " 个数据集；" : "") + "。";
    if (rejected) {
        const names = data.rejected.map(function (r) { return r.dataset_title || r.dataset_uid; }).join("、");
        note += "其中 " + names + " 已有文件下载完成（在磁盘上），未从队列移除，如不需要请自行删除。";
    }
    if (unsupported) note += "另有 " + unsupported + " 个数据集暂不支持直接下载，未加入队列。";
    return note;
}

async function dlApplyUpdate() {
    const st = _dl;
    const work = dlPendingChanges();
    if (!work.hasChanges || !st.job) return;
    st.note = "正在更新队列…";
    renderTaskPackDownloadZone();
    try {
        const res = await fetch(API.downloadUpdate, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ add: work.add, remove: work.remove })
        });
        const data = await res.json();
        if (!res.ok || !data.ok) {
            st.note = data.message_zh || data.detail || "更新下载没有成功";
            renderTaskPackDownloadZone();
            return;
        }
        if (data.snapshot) st.status = data.snapshot;   // 刷新队列/进度快照
        if (Array.isArray(data.queue_uids)) {
            st.runningUids = data.queue_uids.slice();
            _tpChosen = new Set(st.runningUids);        // 提交后勾选对齐在途集（后续改动再算差量）
        }
        st.note = dlUpdateNote(work, data);
        st.job = { job_id: st.job.job_id, dir: (data.snapshot && data.snapshot.dir) || st.job.dir,
            total_bytes: Number(data.total_bytes) || st.job.total_bytes };
        renderTaskPackDownloadZone();
        if (_tpPlan) renderTaskPackPlan(_tpPlan);
        if (!st.timer) dlPoll();   // 轮询已停则重新起（仍在 running 的话）
    } catch (err) {
        st.note = "没能更新下载队列：" + String((err && err.message) || err);
        renderTaskPackDownloadZone();
    }
}

async function dlStop() {
    const st = _dl;
    if (!st.job) return;
    st.note = "正在取消…";
    renderTaskPackDownloadZone();
    try {
        await fetch(API.downloadCancel, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ job_id: st.job.job_id })
        });
    } catch (_e) { /* 取消请求失败不阻断：轮询继续如实反映 */ }
    if (!st.timer) dlPoll();   // cancel 立即置 cancelled，轮询下一次拿到终态
}

async function dlViewProgress() {
    const st = _dl;
    const jobId = st.lastJobId;
    if (!jobId) return;
    try {
        const res = await fetch(API.downloadStatus + "?job=" + encodeURIComponent(jobId));
        const s = await res.json();
        if (res.status === 404 || !s.ok) {
            st.note = "找不到那个下载任务（可能已结束）";
            renderTaskPackDownloadZone();
            return;
        }
        st.job = { job_id: jobId, dir: s.dir, total_bytes: s.total_bytes };
        st.status = s;
        st.note = "";
        if (s.state === "running") {
            st.stage = "running"; st.timer = null; renderTaskPackDownloadZone(); dlPoll();
        } else {
            st.stage = s.state === "cancelled" ? "cancelled" : (s.state === "error" ? "error" : "done");
            renderTaskPackDownloadZone();
        }
    } catch (err) {
        st.note = "没能查看那个下载任务：" + String((err && err.message) || err);
        renderTaskPackDownloadZone();
    }
}

/* 终态 done 且有 unsupported 时：只把这部分数据集（还在候选池里勾选过的）打包成任务包。 */
async function dlBuildPackForUnsupported() {
    const st = _dl;
    const unsupported = ((st.plan && st.plan.unsupported) || []).map(function (u) { return u.dataset_uid; });
    const keep = unsupported.filter(function (uid) { return _tpPlan && _tpChosen.has(uid); });
    if (!keep.length) { toast("这些数据集没有可打包的内容"); return; }
    const saved = _tpChosen;
    _tpChosen = new Set(keep);
    await buildTaskPack();
    _tpChosen = saved;
}

/* 从「打包前20条 / 下载前5个 / 导出这3份」这类话里认出**用户说的条数**；认不出返回 0（调用方用默认口径）。

   旧写法 `tpLimitFromUtterance` 取整句里第一个 1-3 位数字、且只认 10/20/50 三档，两处都错：
     · 「2020年后的人类肺癌数据，打包前20条」——`/(\d{1,3})/` 咬中 2020 的前三位「202」→ 不在三档 → 落默认 10。
       用户说了 20 拿到 10，屏幕上没有任何提示。
     · 「打包前5条」——5 不在三档 → 同样静默变 10，**多给**了 5 条，正是本文件自己反对的行为。

   现在改成「数字必须带量词收尾」：
     · 高置信：前/头/取/首/这/那 + 数字 + 条/个/份/项
     · 次之：数字 + 条/个/份/项，且量词后面就是句读或结尾
   这两条天然避开 10x、COVID-19、GSE123456、2020年 —— 它们都没有量词收尾。
   刻意**不**在这里手抄一份执行词表：本函数只在服务端已经把这句话判成执行档之后才被调用，
   「是不是执行诉求」那一问已经有单一真源（vocabulary.ACTION_MARKERS），这里只负责把数字读出来。 */
export function tpCountFromUtterance(text) {
    const s = String(text || "");
    const strong = s.match(/(?:前|头|取|首|这|那)\s*(\d{1,3})\s*(?:条|个|份|项)/);
    if (strong) return parseInt(strong[1], 10);
    const plain = s.match(/(\d{1,3})\s*(?:条|个|份|项)(?=$|[，。,.、；;！!？?\s])/);
    if (plain) return parseInt(plain[1], 10);
    return 0;
}

/* tpBytes 已归入纯核 act_core.js（Phase C · C0，task_pack / act 共用单一真源）——
   C4 起经 import 取（见文件头）。 */

function toggleTaskPackItem(uid) {
    if (_tpChosen.has(uid)) _tpChosen.delete(uid); else _tpChosen.add(uid);
    // dl-auto-1：运行中允许改勾——只改目标集（待提交差量），不即时提交、不动已下载的队列/进度。
    if (_dl.stage === "running") {
        renderTaskPackDownloadZone();   // 刷新「更新下载」按钮可见性
        if (_tpPlan) renderTaskPackPlan(_tpPlan);
        return;
    }
    // ：勾选变了 → 下载区状态作废（plan 要按新 uids 重查；确认条/终态重置回 idle）
    if (_dl.stage !== "idle") dlReset(); else { _dl.plan = null; _dl.note = ""; }
    if (_tpPlan) renderTaskPackPlan(_tpPlan);
}

/* 返回 `{ok, artifact?, error?}`。
   `artifact` 里只放**真实产物**能证明的东西：文件名取自 Content-Disposition、字节数取自 blob。
   数据集条数单列在 `requested` 里并标明它是「送去打包的条数」而不是产物自证的数字——
   zip 响应体里没有条数字段，把 `_tpChosen.size` 说成「产物里有 N 条」就是拿请求参数冒充产物证据。 */
export async function buildTaskPack() {
    if (!_tpPlan) return { ok: false, error: "现在没有可用的打包清单" };
    if (_tpBusy) return { ok: false, error: "上一次打包还在进行中" };
    if (!_tpChosen.size) { toast("先勾选至少一个数据集"); return { ok: false, error: "还没有勾选任何数据集" }; }
    const btn = $("taskPackBuildBtn");
    _tpBusy = true;
    let replaced = false;   // 面板已被别的内容接管（例如 409 的「没有生成任何文件」）
    let outcome = { ok: false, error: "打包没有完成" };
    try {
        const res = await fetch(API.taskPackBuild, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                plan_token: _tpPlan.plan_token,
                selected_uids: Array.from(_tpChosen),
                snapshot_id: _tpPlan.retrieval.snapshot_id,
                content_digest: _tpPlan.retrieval.content_digest,
                retrieval_date: _tpPlan.retrieval.date,
                scope: _tpPlan.scope || "primary",
                retrieval_params: _tpPlan.retrieval_params,
                format: "zip"
            })
        });
        if (res.status === 409) {
            const info = await res.json();
            tpShowStale(info);
            replaced = true;   // 下面的 finally 不许再重画清单，否则这条提示当场被盖掉
            outcome = { ok: false, stale: true, error: info.message_zh || "这份清单已经不是刚才那一批了。" };
            return outcome;
        }
        if (!res.ok) {
            let detail = "生成失败";
            try { const j = await res.json(); detail = j.message_zh || j.detail || detail; } catch (_e) {}
            toast(detail);
            outcome = { ok: false, error: detail };
            return outcome;
        }
        const blob = await res.blob();
        const name = tpFilenameFrom(res.headers.get("content-disposition")) || "biodata-task-pack.zip";
        downloadBlobAs(blob, name);
        // 只在**真拿到产物之后**记 —— 记在函数入口会把失败的打包也算成成功，
        // 那就成了「反馈包自己在撒谎」，正是这个项目一直在修的那类毛病。
        usageLog(USAGE_KINDS.dl, { what: "pack", n: _tpChosen.size });
        toast("任务包已生成，解压后先看 00-START-HERE.txt");
        outcome = {
            ok: true,
            artifact: { filename: name, bytes: blob.size },
            requested: { n_datasets: _tpChosen.size, count: Object.assign({}, _tpCount) }
        };
        return outcome;
    } catch (err) {
        const msg = String((err && err.message) || err);
        toast("生成失败：" + msg);
        outcome = { ok: false, error: msg };
        return outcome;
    } finally {
        _tpBusy = false;
        if (btn) { btn.disabled = false; }
        if (_tpPlan && !replaced) renderTaskPackPlan(_tpPlan);
    }
}

/* 服务端发现「和你刚才看到的不是同一批了」——四种原因分开说，
   因为用户要做的事不一样：目录变了 vs 内容被更新 vs 检索条件对不上。 */
function tpShowStale(info) {
    const panel = $("taskPackPanel");
    if (!panel) return;
    panel.innerHTML = '<div class="tp-head"><strong>没有生成任何文件</strong></div>'
        + '<p class="tp-msg">' + escapeHtml(info.message_zh || "这份清单已经不是刚才那一批了。") + "</p>"
        + '<p class="tp-msg-detail">' + escapeHtml(info.hint_zh || "") + "</p>"
        + '<div class="tp-actions"><button type="button" class="btn btn-primary" id="taskPackRetryBtn">重新预览</button></div>';
    panel.hidden = false;
}

function tpFilenameFrom(disposition) {
    const match = /filename="([^"]+)"/.exec(String(disposition || ""));
    return match ? match[1] : "";
}

/* downloadBlobAs（批）上移 #core 共用——本文件 757 行的调用点不变。 */

export function initTaskPack() {
    const btn = $("taskPackBtn");
    if (btn) btn.addEventListener("click", toggleTaskPackPanel);
    const panel = $("taskPackPanel");
    if (!panel) return;
    panel.addEventListener("click", function (event) {
        const target = event.target;
        if (target.id === "taskPackCloseBtn") { panel.hidden = true; return; }
        if (target.id === "taskPackBuildBtn") { buildTaskPack(); return; }
        if (target.id === "taskPackRetryBtn") { previewTaskPack(); return; }
        //  真实下载区按钮（状态单一真源 _dl）
        if (target.id === "tpDlStartBtn") { _dl.stage = "confirm"; _dl.note = ""; renderTaskPackDownloadZone(); return; }
        if (target.id === "tpDlConfirmBtn") { dlStart(); return; }
        if (target.id === "tpDlConfirmCancelBtn") { _dl.stage = "idle"; _dl.note = ""; renderTaskPackDownloadZone(); return; }
        if (target.id === "tpDlStopBtn") { dlStop(); return; }
        if (target.id === "tpDlUpdateBtn") { dlApplyUpdate(); return; }
        if (target.id === "tpDlViewBtn") { dlViewProgress(); return; }
        if (target.id === "tpDlPackBtn") { dlBuildPackForUnsupported(); return; }
        if (target.id === "tpDlBackBtn") { dlReset(); if (_tpPlan) renderTaskPackPlan(_tpPlan); return; }
    });
    panel.addEventListener("change", function (event) {
        const uid = event.target.dataset ? event.target.dataset.tpUid : null;
        if (uid) toggleTaskPackItem(uid);
    });
}
