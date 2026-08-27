"use strict";

/* C4 起本文件是 ES Module：core 的工具、act_core 纯核（回执/事实句构造）、act_run 行动流、
   task_pack 的 previewTaskPack/buildTaskPack 与 _tpPlan/_tpChosen（活绑定只读）、
   reuse_pack 的 downloadTextBlob、results 的 loadFeasibility、cards 的 openFilesModal、
   search 的 LAST_RECOMMEND_DATA（活绑定只读）、shell 的 getConfig、
   board 的 cbMarkLastSayAsAction/cbLogPush/cbUpdateEntry/cbRenderHistory/cbProgressDrop
   经 import 取（act↔board 成环，但绑定都只在函数体内使用，ESM 允许）。
   search.js 经 import 取 actAfterSearch（C5 起绞杀桥全退役）。
   2026-08-03 agent2 起问卷弹窗（survey.js）退役：管护动词全自动化直推。
   2026-08-16 pack1：「按原话重新检索」「以后别自动执行」两颗 chip 退役（agent 能力已足够），
   runRecommend/syncAiGates 随之不再引用。 */
import { API, $, downloadBlobAs, escapeHtml, isHttp, toast } from "#core";
import { ACT_BUSY_NOTE, actReceiptFrom, actSecondOrderGaps, actWhatHappened, tpBytes } from "#act_core";
import { arxActive, arxBegin, arxDecision, arxDecisionDone, arxFail, arxFinish, arxOnChange, arxStep } from "#act_run";
import { previewTaskPack, buildTaskPack, tpDownloadConfirm, tpDownloadStart, _tpPlan, _tpChosen } from "#task_pack";
import { downloadTextBlob } from "#reuse_pack";
import { loadFeasibility } from "#results";
import { openFilesModal } from "#cards";
import { LAST_RECOMMEND_DATA } from "#search";
import { getConfig, webGuardOn } from "#shell";
import { cbExecReceiptCovered, cbLogPush, cbMarkLastSayAsAction, cbProgressDrop, cbRenderHistory, cbUpdateEntry, ubSubmit } from "#board";
import { benchfbTurnAction } from "#benchfb";

/* P2 · 一句话执行层的界面侧：**派发既有能力 + 行动流播报 + 由真实产物构造事实句**。

   2026-08-04 长程多步执行起，本层多一条**图内已执行渲染通道**：agent 的 langgraph 图
   已在后端真跑过 LOOP_TOOLS 工具时（plan.steps 非空，含每步结果/失败原样），这里只渲染
   卡片与总结、**绝不调 runner 再执行一遍**（双执行红线）；其余 plan 照旧走 runner 派发。

   后端 `/api/action/plan` 只回「该做哪一个动作」，一个字节的产物都不产。真正做事的是这里，
   而且做的都是**页面上本来就有的那几件事**（预览/产包/导出引文/投稿材料/可行性/文件清单）——
   这一层没有自己的执行通道，也就没有「只有自动执行才会走到」的代码路径。

   ## 执行形态（2026-08-03 p10 重做，用户图2 指令）

   - **行动流播报**：每次派发开一条 act_run（arxBegin）——关键节点逐条上屏
     （arxStep：plan / 联网查询 / apply 各是一条，粒度=真实步骤边界，绝不伪造流式），
     2026-08-03 agent2 起**全自动化**：管护动词不再开问卷——plan（预览）→ apply（写盘）
     由 runner 链式直推（后端两步端点照走，confirm_token 重算指纹 fail-closed），
     记账 + 回收站可回退；唯一停点是浏览器安全边界（import 的系统文件对话框）。
     完成后步骤块折叠消失，对话流里只留下一条总结 sys（执行过程折进总结泡的 <details>）。
   - **总结正文**（§5.3 降噪）：先入**事实句**（act_core 构造，数字全部取自真实返回值——诚实红线），
     随后异步请 `/api/act/summary`（brief:true）让 LLM 改写成**一句话**（成功则原位替换正文 + 「AI 总结」标），
     LLM 缺席/失败则事实句留存（fail-open，与后端同哲学）；事实明细 / hint / uncertainty 标注
     一律折进总结泡的 details（只挪不删）。
   - **管护两步端点保留**：`/api/curate/plan` → `confirm_token` → `/api/curate/apply`
     是 MCP/CLI 与前端 runner 共用的同一通道；问卷弹窗（survey.js）随全自动化退役。
     结果区的 #curatePanel / #actReceipt 两块静态面板早已退役。

   ## 回执为什么这么写

   「做了什么」这一行只能由**真实返回值**构造：文件名取自 Content-Disposition、字节数取自 blob。
   `previewTaskPack` / `buildTaskPack` 在 P0 里被改成有返回值，就是为了这一刻；在那之前它们
   所有出口都是裸 return，回执**只能照「成功」渲染**，断网 / 409 / 后端无命中全会被写成「已经打包好了」。

   成功与失败是**两个互不相通的模板**（`ACT_LEAD`）：失败那支从头到尾取不到「已」字——
   不是拼字符串时小心翼翼地绕开它，而是那条分支根本没有那个字面量。配 node 真行为门。

   ## `confidence` 不是确认门

   `plan.confidence === "low"` **仍然执行同一个 verb、同一套参数**，唯一差别是回执上多一行醒目标注。
   本文件里不存在任何以 confidence 为条件的 return / 跳过分支，配静态门钉死——不然
   「低置信度转确认」这个旧哲学最体面的马甲会从那个悬空字段的空白处长出来。 */

let _actBusy = false;
let _actLastSaid = "";   // 当前这句话的原话：curate.search_online 的关键词预填从它解析（用户可改，不是替用户拍板）

/* ---------------- 开关 ---------------- */

export function actEnabled() {
    // AI 执行（维度 C，2026-08-03 agent2 合并旧「说了就直接做」+「Agent 规划执行」）。
    const box = $("cfgAgentExec");
    return !!(box && box.checked);
}

/* ---------------- 派发：每个 runner 都返回 {ok, artifact?, gaps?, policy?, error?, cancelled?} ---------------- */

function actResultItems(limit) {
    const items = (LAST_RECOMMEND_DATA && LAST_RECOMMEND_DATA.results) || [];
    const n = Number(limit) || 0;
    return n > 0 ? items.slice(0, n) : items.slice();
}

function actUids(limit) {
    return actResultItems(limit)
        .map(function (it) { return String((it && it.dataset_uid) || "").trim(); })
        .filter(Boolean);
}

/* 任务包面板里那三行口径（装不了什么 / 只取主文件 / 另有 N 个文件没列入）。
   自动执行绕过面板就把它们丢了，而 `download_script.py` 逐字写着「只列 1 个代表性主文件」
   这句是四处必现的核心诚实载体 —— 所以回执里**无条件**带上，不看用户有没有提范围。 */
function actPackPolicyLines() {
    if (!_tpPlan) return [];
    const chosen = (_tpPlan.items || []).filter(function (it) { return _tpChosen.has(it.dataset_uid); });
    const lines = [];
    const cannot = (_tpPlan.cannot_include || []).filter(function (m) { return _tpChosen.has(m.dataset_uid); });
    if (cannot.length) lines.push("这一批里有 " + cannot.length + " 个数据集装不进包，面板里逐条写了原因。");
    if (_tpPlan.primary_only_policy_zh || _tpPlan.primary_only_zh) {
        lines.push(String(_tpPlan.primary_only_policy_zh || _tpPlan.primary_only_zh));
    }
    let excluded = 0;
    chosen.forEach(function (it) {
        excluded += Math.max(0, (it.n_files_total || 0) - (it.n_files_selected || 0));
    });
    if (excluded > 0) lines.push("这几个数据集的来源清单里另有 " + excluded + " 个文件没有列入。");
    return lines;
}

function actPackCommands() {
    if (!_tpPlan) return null;
    let rows = 0;
    (_tpPlan.items || []).forEach(function (it) {
        if (_tpChosen.has(it.dataset_uid)) rows += it.rows_planned || 0;
    });
    return rows;
}

async function actRunPackPreview(plan) {
    const want = Number((plan.slots || {}).limit) || 0;
    arxStep("整理这批结果的清单");
    const pre = await previewTaskPack(want ? { count: want } : {});
    if (!pre.ok) return { ok: false, error: pre.error || "没能整理出这一批的清单" };
    /* 2026-08-16 pack1 分流（previewTaskPack 起不再自动开面板）：
       · pack.preview（用户明说「我自己挑/先给我看看清单」）——开面板是履约：unhide + 滚进视野 +
         「清单面板已在结果区展开」口径照旧；
       · pack.download（actRunPackDownload 内部调本函数）——面板保持关闭、不滚动、
         不写「面板已展开」这类谎话；要看清单可点回执里的「打开下载面板自己挑」。 */
    const forDownload = String((plan && plan.verb) || "") === "pack.download";
    if (!forDownload) {
        const panel = $("taskPackPanel");
        if (panel) {
            panel.hidden = false;
            if (panel.scrollIntoView) panel.scrollIntoView({ block: "nearest" });
        }
        arxStep("清单面板已在结果区展开");
    }
    return {
        ok: true,
        artifact: { n_datasets: pre.chosen, commands: actPackCommands() },
        policy: actPackPolicyLines(),
        gaps: actSecondOrderGaps(want, pre.chosen, actPackCommands()),
        extra: forDownload
            ? "清单没有自动展开：想逐条核对/勾选，点下方「打开下载面板自己挑」即可；点面板底部的按钮才会真正生成任务包。"
            : "清单面板已在结果区展开：可勾选、可修改；点面板底部的按钮才会真正生成任务包。",
    };
}

async function actRunPackDownload(plan) {
    arxStep("整理这批结果的清单");
    const pre = await actRunPackPreview(plan);
    if (!pre.ok) return pre;
    /* dl2：真实数据下载优先。preview 之后先问 /api/download/plan 分级——
       有 supported 就进「真实下载」流程；无 supported / plan 失败则诚实降级 + 自动落任务包兜底
       （原 zip 链不变）。
       dl-auto-1：分级 OK 后**直接开始真实下载**，不再停在确认闸——模型第一次调用就主动发起，
       无需用户在面板点「开始下载」；失败（409 在途冲突 / no_downloadable / 磁盘不足 / 网络错）
       如实回报，不伪造成功。手动从面板发起仍保留确认条（用户在面板里已有意确认）。
       公网护栏硬化（2026-08-26）：护栏模式（webGuardOn）后端 download 系列端点一律 403——
       跳过真实下载分支，直接走任务包 zip 兜底（zip 是既有核心功能，不受影响）。 */
    if (webGuardOn()) {
        arxStep("网页版不提供服务端代下数据，直接生成任务包");
        arxStep("生成任务包并下载");
        const guardBuilt = await buildTaskPack();
        if (!guardBuilt.ok) return { ok: false, error: guardBuilt.error || "打包没有完成", policy: pre.policy };
        const guardRequested = (guardBuilt.requested && guardBuilt.requested.n_datasets) || 0;
        return {
            ok: true,
            artifact: {
                filename: guardBuilt.artifact.filename,
                bytes: guardBuilt.artifact.bytes,
                n_datasets: guardRequested,
                commands: pre.artifact.commands,
            },
            policy: pre.policy,
            gaps: pre.gaps,
            onDisk: true,
        };
    }
    const dlg = await tpDownloadConfirm();
    if (dlg.ok) {
        arxStep(dlg.supported + " 个数据集可直接下载真实数据（共约 " + tpBytes(dlg.totalBytes) + "）"
            + (dlg.unsupported ? "；另有 " + dlg.unsupported + " 个暂不支持" : ""));
        arxStep("直接开始下载");
        const started = await tpDownloadStart(dlg.uids);
        if (!started.ok) {
            return {
                ok: false,
                error: started.message || started.error || "下载没能开始",
                policy: pre.policy,
                gaps: pre.gaps,
            };
        }
        return {
            ok: true,
            artifact: {
                n_datasets: started.n, bytes: started.total_bytes, mode: "real-download",
                dir: started.dir, job_id: started.job_id,
            },
            policy: pre.policy,
            gaps: pre.gaps,
            extra: "已开始下载 " + started.n + " 个数据集的真实文件（仅含代表性主文件，共约 "
                + tpBytes(started.total_bytes) + "），写入 " + started.dir
                + "；进度可在下载面板查看，过程中可在列表里勾选/取消勾选数据集并点「更新下载」增删条目。"
                + (dlg.unsupported ? "另有 " + dlg.unsupported + " 个数据集暂不支持直接下载，可随后生成任务包。" : ""),
        };
    }
    if (dlg.reason === "none_supported") {
        arxStep("这批 " + dlg.unsupported + " 个数据集暂不支持直接下载，自动改生成任务包");
    } else {
        arxStep("没能确认哪些可直接下载，自动改生成任务包");
    }
    arxStep("生成任务包并下载");
    const built = await buildTaskPack();
    if (!built.ok) return { ok: false, error: built.error || "打包没有完成", policy: pre.policy };
    const requested = (built.requested && built.requested.n_datasets) || 0;
    return {
        ok: true,
        artifact: {
            filename: built.artifact.filename,
            bytes: built.artifact.bytes,
            n_datasets: requested,
            commands: pre.artifact.commands,
        },
        policy: pre.policy,
        gaps: pre.gaps,
        onDisk: true,
    };
}

async function actFetchReusePack(limit) {
    const uids = actUids(limit);
    if (!uids.length) return { ok: false, error: "这一批结果里没有可用的数据集编号" };
    const res = await fetch(API.reusePack, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ uids }),
    });
    const data = await res.json();
    if (!res.ok || !data.ok || !data.pack) return { ok: false, error: data.detail || "未能生成清单" };
    return { ok: true, uids: uids, data: data };
}

async function actRunCiteExport(plan) {
    const want = Number((plan.slots || {}).limit) || 0;
    arxStep("取这批结果的编号，生成引文");
    const got = await actFetchReusePack(want);
    if (!got.ok) return got;
    const ris = got.data.ris || "";
    const bib = got.data.bibtex || "";
    if (!ris && !bib) return { ok: false, error: "这一批里没有可导出的引文" };
    // 字节数取自**同一份**文本要生成的 blob，不是估算。
    const bytes = new Blob([ris || ""]).size;
    // dl-auto-1 任务B：引文导出自动下载 **RIS + BibTeX 两个文件**（有值才下；主产物 .ris
    // 没存下才算失败，.bib 尽力而为）。返回值必须看：`downloadTextBlob` 内部 try/catch 只
    // toast 一句「下载失败」，不看它就会在浏览器根本没存下文件时照样写「已导出引文」。
    arxStep("下载引文文件（ris + bib）");
    // ris 是引文主产物（/api/reuse-pack 恒生成）；saved 判定必须消费返回值，否则浏览器没存下
    // 也会写成「已导出引文」。.bib 尽力而为（有值才下）。
    const saved = downloadTextBlob(ris, "reused-public-datasets.ris",
        "application/x-research-info-systems;charset=utf-8", "已下载引文文件", "下载失败");
    const savedBib = bib ? downloadTextBlob(bib, "reused-public-datasets.bib",
        "application/x-bibtex;charset=utf-8", "已下载引文文件（bib）", "引文下载失败") : false;
    if (!saved) return { ok: false, error: "浏览器没能把引文文件存下来" };
    return {
        ok: true, onDisk: true,
        artifact: { filename: "reused-public-datasets.ris", bytes: bytes, n_datasets: got.uids.length },
        policy: ["单独导出的引文不含任务包里的「投稿前需要自己核实」清单；投稿前请自行核对。"
            + (savedBib ? "（已同时下载 reused-public-datasets.bib）" : "")],
        extra: "已自动下载引文：reused-public-datasets.ris"
            + (savedBib ? " 与 reused-public-datasets.bib" : "") + "。",
        gaps: actSecondOrderGaps(want, got.uids.length, null),
    };
}

async function actRunReusePack(plan) {
    const want = Number((plan.slots || {}).limit) || 0;
    arxStep("取这批结果的编号，整理投稿材料");
    const got = await actFetchReusePack(want);
    if (!got.ok) return got;
    const md = got.data.markdown || "";
    if (!md) return { ok: false, error: "这一批没能整理出投稿材料" };
    const bytes = new Blob([md]).size;
    // dl-auto-1 任务C：投稿材料三个文件（.md 主产物 + .ris/.bib 引文）**一起自动下载**。
    // 主产物 .md 没存下才算失败；.ris/.bib 尽力而为（有空值/失败如实标注，不假装成功）。
    arxStep("下载材料文件（md + ris + bib）");
    const saved = downloadTextBlob(md, "reused-public-datasets.md", "text/markdown;charset=utf-8",
        "已下载投稿材料", "下载失败");
    const ris = got.data.ris || "", bib = got.data.bibtex || "";
    const savedRis = ris ? downloadTextBlob(ris, "reused-public-datasets.ris",
        "application/x-research-info-systems;charset=utf-8", "已下载引文（ris）", "引文下载失败") : false;
    const savedBib = bib ? downloadTextBlob(bib, "reused-public-datasets.bib",
        "application/x-bibtex;charset=utf-8", "已下载引文（bib）", "引文下载失败") : false;
    if (!saved) return { ok: false, error: "浏览器没能把这份材料存下来" };
    const dlNote = ["投稿材料已下载：reused-public-datasets.md"
        + (savedRis ? " + reused-public-datasets.ris" : "")
        + (savedBib ? " + reused-public-datasets.bib" : "") + "。"]
        .concat((got.data.pack.gaps || []).length
            ? ["这份材料里列了 " + got.data.pack.gaps.length + " 项需要你自己核实的事，请照着核一遍。"]
            : []);
    return {
        ok: true, onDisk: true,
        artifact: { filename: "reused-public-datasets.md", bytes: bytes, n_datasets: got.uids.length },
        policy: dlNote,
        extra: "已自动下载投稿材料三件套：Markdown（正文+补充表）、RIS 与 BibTeX 引文（若该批引文可生成）。",
        gaps: actSecondOrderGaps(want, got.uids.length, null),
    };
}

async function actRunFeasibility() {
    const panel = $("feasibilityPanel");
    // `loadFeasibility` 是开关语义（已展开就收起）。这里要的是「确保打开」，先复位再调，
    // 否则同一句话说两次会把面板关掉、回执却写着已经打开了。
    if (panel && !panel.hidden) { panel.hidden = true; panel.innerHTML = ""; }
    arxStep("统计这个方向有多少可复用数据");
    const out = await loadFeasibility();
    if (!out || !out.ok) return { ok: false, error: (out && out.error) || "没能统计出可行性概览" };
    if (panel && panel.scrollIntoView) panel.scrollIntoView({ block: "nearest" });
    return { ok: true, artifact: { n_datasets: out.candidate_count }, extra: "可行性概览已在结果区展开。" };
}

async function actRunFilesShow() {
    const items = actResultItems(1);
    if (!items.length) return { ok: false, error: "这一批结果里没有可看的数据集" };
    const it = items[0];
    arxStep("取第 1 条的文件清单");
    const out = await openFilesModal(it, null);
    if (!out || !out.ok) return { ok: false, error: (out && out.error) || "没能取到文件清单" };
    return {
        ok: true,
        artifact: { n_files: out.count, dataset_name: it.dataset_name || "" },
        // 只说本工具做了什么，**不替用户复述他说了什么**：「你没有点名是哪一条」是一句
        // 本工具无从核实的断言（他完全可能说了「前5条的文件」，而这一步照样只处理第 1 条）。
        policy: ["这一步只处理当前结果里的第 1 条，打开的就是它的文件清单。"],
    };
}

/* ---------------- 管护（curate.*）：全自动化直推（2026-08-03 agent2） ----------------

   产品所有者预先授权（推翻「写操作必须人批准」）：AI 执行开启时一切动作**直接执行**，
   只保留两条——① 审计（后端账本逐行记账）；② 回退（删除走回收站；导入/联网入库的撤销
   复用同一回收站机制，回执里写明「说『删掉 …』即可撤销」）。四个写动作
   （import / search_online / remove / restore）的 runner 链式直推：plan（预览零写盘）
   → apply（confirm_token 回传，后端重算指纹 fail-closed）。唯一停点是浏览器安全边界
   （import 的系统文件对话框——那不是确认面板，是文件只能由用户亲手给）。
   curate.list / curate.check_updates 纯只读：行动流 + 结果卡 + 总结。

   后端 fail-closed：token 重算比对不一致 → token_mismatch，一个字节都不动。所以
   apply 失败的总结必须照实带「本次没有任何改动」（在 error 里拼上）。 */

/* 两步共用的 POST 壳：后端 CurateError → 400、detail 是「code: hint」，原样取出来给用户看
   （hint 里已经写明「未写入」）；422 / 网络错也都有可读句。返回 {ok, result?, error?}。 */
async function actCuratePost(url, body) {
    let res, data = null;
    try {
        res = await fetch(url, {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
        data = await res.json();
    } catch (err) {
        return { ok: false, error: "没能连上服务：" + String((err && err.message) || err) };
    }
    if (!res.ok || !data || data.ok !== true) {
        const raw = data && data.detail;
        const detail = typeof raw === "string" ? raw
            : (raw ? JSON.stringify(raw) : ("请求失败（HTTP " + res.status + "）"));
        return { ok: false, error: String(detail) };
    }
    return { ok: true, result: data.result || {} };
}

/* ---- curate.list：只读清点（无问卷） ---- */

function actListCardHtml(r) {
    const files = r.files || [];
    const recycle = r.recycle || [];
    const row = function (name, meta) {
        return '<div class="arx-card-row"><span class="arx-card-name">' + escapeHtml(name)
            + '</span><span class="arx-card-meta">' + escapeHtml(meta) + "</span></div>";
    };
    let html = '<div class="arx-card"><div class="arx-card-title">外部库（'
        + escapeHtml(r.external_dir || "database/external") + "）· " + files.length + " 个文件</div>";
    html += files.length ? files.map(function (f) {
        return row(f.filename, (f.record_count == null ? "条数未知" : f.record_count + " 条")
            + " · " + (f.curatable ? "可对话管护" : "官方快照"));
    }).join("") : '<p class="arx-card-empty">外部库现在是空的。</p>';
    if (recycle.length) {
        html += '<div class="arx-card-title">回收站 · ' + recycle.length + " 个（说「把删掉的找回来」可移回）</div>"
            + recycle.map(function (x) {
                return row(x.original_filename || x.recycle_name, x.record_count == null ? "条数未知" : x.record_count + " 条");
            }).join("");
    }
    return html + "</div>";
}

async function actRunCurateList() {
    arxStep("清点外部库与回收站");
    const got = await actCuratePost(API.curatePlan, { action: "list" });
    if (!got.ok) return { ok: false, error: got.error };
    const r = got.result;
    return {
        ok: true,
        artifact: { n_files: r.file_count, n_recycle: r.recycle_count },
        policy: ["这一步只做了清点，没有改动任何文件。"],
        cardHtml: actListCardHtml(r),
    };
}

/* ---- curate.check_updates：只读检查来源更新（无问卷） ----
   每个来源一条：online 模式（ArrayExpress / ENCODE / 10x / HCA 等在线通道源）给「线上最近 N 条 / 本地已有 X 条 /
   疑似新增 Y 条」+ 疑似新增前几条的编号与标题；snapshot 模式（离线快照源，或在线源拉不到时的如实降级）
   如实给快照日期/条数 + 官网链接（人工核对入口）+ note_zh——不伪造在线比对能力。 */

function actCheckUpdatesCardHtml(r) {
    const list = r.sources || [];
    const rows = list.map(function (s) {
        let html = "";
        if (s.mode === "online") {
            const news = s.new_candidates || [];
            // new_count 是真实新增数（候选样本可能被截断到前几条，别拿样本长度冒充总数）。
            const newCount = (s.new_count != null) ? s.new_count : news.length;
            html += '<div class="arx-card-row"><span class="arx-card-name">' + escapeHtml(s.source || "")
                + '</span><span class="arx-card-meta">线上最近 ' + (s.online_recent || 0) + " 条 · 本地已有 "
                + (s.local_count || 0) + " 条 · 疑似新增 " + newCount + " 条</span></div>";
            html += news.slice(0, 3).map(function (c) {
                return '<div class="arx-card-row"><span class="arx-card-name">' + escapeHtml(c.accession || "")
                    + '</span><span class="arx-card-meta">' + escapeHtml(c.title || "") + "</span></div>";
            }).join("");
            // online 支也必须如实带 note（如在线通道失败降级、比对口径等诚实声明，后端写了什么就显示什么）。
            if (s.note_zh) html += '<p class="arx-card-empty">' + escapeHtml(s.note_zh) + "</p>";
        } else {
            html += '<div class="arx-card-row"><span class="arx-card-name">' + escapeHtml(s.source || "")
                + '</span><span class="arx-card-meta">本地快照 ' + escapeHtml(s.snapshot_date || "日期未知")
                + " · " + (s.local_count || 0) + " 条</span></div>";
            // site_url 是 check-updates 在线源回传的数据派生值，escapeHtml 只封引号不封协议，
            // javascript: 伪协议须 isHttp 门禁拦下（对齐 reuse_pack.js 的同类守卫，审计 S-3）。
            if (s.site_url && isHttp(s.site_url)) {
                html += '<div class="arx-card-row"><a class="btn" href="' + escapeHtml(s.site_url)
                    + '" target="_blank" rel="noopener noreferrer">打开官网核对</a></div>';
            }
            if (s.note_zh) html += '<p class="arx-card-empty">' + escapeHtml(s.note_zh) + "</p>";
        }
        return html;
    }).join("");
    return '<div class="arx-card"><div class="arx-card-title">来源更新检查 · ' + list.length + " 个来源</div>"
        + (rows || '<p class="arx-card-empty">没有可检查的来源。</p>') + "</div>";
}

async function actRunCurateCheckUpdates(plan) {
    // plan.slots.source 命中来源名时只查那个来源子集；没点名就 null 全查（后端契约）。
    const src = String(((plan && plan.slots) || {}).source || "").trim();
    arxStep(src ? "检查来源更新：" + src : "检查全部来源的更新");
    const got = await actCuratePost(API.curateCheckUpdates, { sources: src ? [src] : null });
    if (!got.ok) return { ok: false, error: got.error };
    const r = got.result;
    return {
        ok: true,
        artifact: { n_sources: (r.sources || []).length },
        policy: ["这一步只做了比对，没有改动任何文件。"],
        extra: String(r.hint_zh || ""),
        cardHtml: actCheckUpdatesCardHtml(r),
    };
}

/* ---- curate.sync_updates：检查更新 → 有新增则自动入库（复合流，2026-08-06「工作流即工具」批） ----
   后端一次原子调用跑完整条固定流程（先只读比对、再把能闭环来源的疑似新增逐编号搜回入库），
   前端只展示事实：每源「疑似新增 X · 已入库 M · 文件名」，闭不了环的来源 note_zh 如实写明。 */

function actSyncUpdatesCardHtml(r) {
    const list = r.sources || [];
    const rows = list.map(function (s) {
        let html = '<div class="arx-card-row"><span class="arx-card-name">' + escapeHtml(s.label || s.source || "")
            + '</span><span class="arx-card-meta">疑似新增 ' + (s.new_count == null ? "未知" : s.new_count + " 条")
            + " · 已自动入库 " + (s.imported_count || 0) + " 条</span></div>";
        (s.imported_titles || []).slice(0, 3).forEach(function (t) {
            html += '<div class="arx-card-row"><span class="arx-card-name">' + escapeHtml(s.filename || "")
                + '</span><span class="arx-card-meta">' + escapeHtml(t) + "</span></div>";
        });
        if (s.note_zh) html += '<p class="arx-card-empty">' + escapeHtml(s.note_zh) + "</p>";
        return html;
    }).join("");
    return '<div class="arx-card"><div class="arx-card-title">检查更新并同步入库 · ' + list.length + " 个来源</div>"
        + (rows || '<p class="arx-card-empty">没有可检查的来源。</p>') + "</div>";
}

async function actRunCurateSyncUpdates(plan) {
    const src = String(((plan && plan.slots) || {}).source || "").trim();
    arxStep(src ? "检查更新并同步入库：" + src : "检查全部来源的更新并同步入库");
    const got = await actCuratePost(API.curateSyncUpdates, { sources: src ? [src] : null });
    if (!got.ok) return { ok: false, error: got.error };
    const r = got.result;
    const imported = Number(r.imported_total || 0);
    const files = (r.sources || []).map(function (s) { return s.filename; }).filter(Boolean);
    const policy = [];
    if (imported > 0 && files.length) {
        policy.push("新入库的文件已进外部库；说「删掉 " + files[0] + "」即可撤回（进回收站，还能移回）。");
    } else {
        policy.push("没有需要入库的新增，没有改动任何文件。");
    }
    return {
        ok: true,
        artifact: { n_sources: (r.sources || []).length, imported_total: imported, filename: files[0] },
        policy: policy,
        extra: String(r.hint_zh || ""),
        cardHtml: actSyncUpdatesCardHtml(r),
    };
}

/* ---- curate.search_online：直接执行（2026-08-03 agent2 全自动化，问卷已退役） ----
   条件由规划侧槽位供给（LLM 已解析 keywords/species/source），解析不出走确定性预填兜底；
   plan 拿候选 → 立刻 apply 入库（后端两步端点照走，前端链式直推）——记账 + 回收站可回退。 */

function actSearchCardHtml(pr, r) {
    const titles = (pr.sample_titles || []).map(function (t) {
        return '<div class="arx-card-row"><span class="arx-card-name">' + escapeHtml(t) + "</span></div>";
    }).join("");
    // P1-4（2026-08-10 实体级去重）：已在库中的候选由后端跳过、不重复入库，这里如实分行呈现；
    // 零新候选时 filename 为空，绝不渲染「0 条 → 」这种像故障的行。
    const skipped = Number(r.skipped_existing_count || pr.skipped_existing_count || 0);
    const wrote = Number(r.record_count || 0);
    const inMeta = wrote > 0 ? wrote + " 条 → " + escapeHtml(r.filename || "")
        : (skipped ? "0 条（候选全部已在库中，未重复入库）" : "0 条");
    const skippedRow = (skipped && wrote > 0)
        ? '<div class="arx-card-row"><span class="arx-card-name">已在库中</span><span class="arx-card-meta">'
            + skipped + " 条已跳过，未重复入库</span></div>"
        : "";
    return '<div class="arx-card"><div class="arx-card-title">联网搜索入库 · ' + escapeHtml(pr.source_label || "ArrayExpress")
        + "</div>"
        + '<div class="arx-card-row"><span class="arx-card-name">关键词</span><span class="arx-card-meta">'
        + escapeHtml(String(pr.query || "") + (pr.species ? " · 物种 " + pr.species : "")) + "</span></div>"
        + '<div class="arx-card-row"><span class="arx-card-name">入库</span><span class="arx-card-meta">'
        + inMeta + "</span></div>"
        + skippedRow
        + (titles ? '<div class="arx-card-title">前 ' + (pr.sample_titles || []).length + " 条标题样本</div>" + titles : "")
        + "</div>";
}

async function actRunCurateSearchOnline(plan) {
    arxStep("从你的原话提取搜索条件");
    const prefill = await actCuratePrefill(plan, _actLastSaid);
    if (!prefill.query) {
        return { ok: false, error: "这句话里没有能用作搜索关键词的内容。换个说法再试，例如「联网搜 human lung single cell」。" };
    }
    const slots = (plan && plan.slots) || {};
    // 点名来源按槽位走（护栏已保证与原话一致）；没点名回退默认在线源 ArrayExpress。
    const source = String(slots.source || "arrayexpress").trim().toLowerCase() || "arrayexpress";
    arxStep("联网查询 " + source + "（" + prefill.query + (prefill.species ? " · " + prefill.species : "") + "）");
    const body = { action: "search_online", query: prefill.query, source: source };
    if (prefill.species) body.species = prefill.species;
    const got = await actCuratePost(API.curatePlan, body);
    if (!got.ok) return { ok: false, error: got.error };
    const pr = got.result;
    arxStep((pr.record_count || 0) > 0
        ? "找到 " + pr.record_count + " 条候选，写入外部库"
        : "候选全部已在库中，不重复入库");
    const applied = await actCuratePost(API.curateApply, {
        action: "search_online", confirm_token: (pr && pr.confirm_token) || "", plan_result: pr,
    });
    if (!applied.ok) return { ok: false, error: applied.error + "；本次没有任何改动" };
    const r = applied.result;
    return {
        ok: true,
        artifact: { filename: r.filename || "", n_records: r.record_count },
        policy: [String(r.write_boundary || ""),
            // 零写入（候选全部已在库中）时没有可撤的对象——绝不给撤回指引（actUndoSpec 同口径空值守卫）
            r.filename ? "撤回这次入库：点下方「撤回这次执行」即可（进回收站，还能移回）；也可以说「删掉 " + r.filename + "」。" : ""].filter(Boolean),
        cardHtml: actSearchCardHtml(pr, r),
    };
}

/* 关键词提取优先级（2026-08-03 agent1 起；agent2 全自动化后它直接决定执行参数，不再是问卷初值）：
   ① plan.slots.keywords/species——规划侧（agent / action_plan）已解析好的槽位直接供给；
   ② 槽位没有时走 /api/interpret 的**确定性**解析（零 LLM、离线可用）——constraints 的
   英文规范值（Lung/Breast Cancer…）+ ASCII 自由词；
   ③ 都取不到时按原话兜底。
   旧的 ACT_PREFILL_STRIP 正则剥词补丁随「检查更新并入联网搜」的病灶一起退役：
   关键词改由规划侧槽位 / 确定性解析供给，前端不再靠剥词猜。
   解析失败回退原话并如实提示；原话也提不出关键词时 runner 如实报错（不瞎搜）。 */
// 与后端 corpus_curation._SEARCH_SOURCE_ALIASES / 检索 SOURCE_ALIASES 同口径：来源名不是检索关键词，兜底分词时剔除。
// 2026-08-14 补 zenodo（第 10 源登记时漏同步此处）与 refinebio/refine.bio（第 11 源）。
const ACT_SOURCE_TOKEN_RE = /^(arrayexpress|ae|cellxgene|cxg|cellxgene discover|hca|human cell atlas|ebi scea|scea|encode|10x|10x genomics|hubmap|scp|single cell portal|broad single cell portal|geo|ncbi geo|zenodo|refinebio|refine\.?bio|refine bio)$/i;
function actPrefillSpeciesZh(v) {
    const s = String(v || "").trim();
    if (/^human$/i.test(s)) return "Human";
    if (/^mouse$/i.test(s)) return "Mouse";
    return s;
}
async function actCuratePrefill(plan, said) {
    const fallback = {
        query: String(said || ""), species: "",
        hint: "没能自动提取，先按你的原话填上了，请改成能搜到东西的关键词（英文效果更好）。",
    };
    // ① 规划侧槽位优先：agent / action_plan 已经解析出关键词或物种时直接用（agent2 起即执行参数）。
    const slots = (plan && plan.slots) || {};
    const slotQuery = String(slots.keywords || "").trim();
    const slotSpecies = actPrefillSpeciesZh(slots.species);
    if (slotQuery || slotSpecies) {
        return {
            query: slotQuery, species: slotSpecies,
            hint: "搜索条件已从你的原话解析出来（可改；联网源都是英文源，英文关键词效果更好）。",
        };
    }
    // ② 原话为空时连确定性解析都不必跑。
    const raw = String(said || "").trim();
    if (!raw) return {
        query: "", species: "",
        hint: "这句话里没有能当关键词的内容，请填要搜的关键词（英文效果更好）。",
    };
    try {
        const res = await fetch(API.interpret, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ query: raw, auto_parse_sources: false }),
        });
        const data = await res.json();
        if (!res.ok || !data || !data.ok) return fallback;
        const intent = (data.interpretation && data.interpretation.intent) || {};
        const cons = intent.constraints || {};
        const species = actPrefillSpeciesZh(((cons.species || [])[0]) || "");
        const terms = [];
        Object.keys(cons).forEach(function (dim) {
            if (dim === "species" || dim === "sources") return;
            (cons[dim] || []).forEach(function (v) { terms.push(String(v)); });
        });
        (intent.free_text_terms || []).forEach(function (t) {
            const s = String(t);
            if (!ACT_SOURCE_TOKEN_RE.test(s.trim())) terms.push(s);
        });
        const query = Array.from(new Set(terms)).join(" ").trim();
        if (!query) return fallback;
        return {
            query: query, species: species,
            hint: "已从你的原话解析出关键词（联网源都是英文源，规范词本身是英文），可改。",
        };
    } catch (_e) {
        return fallback;
    }
}

/* ---- curate.import：直接执行（2026-08-03 agent2 全自动化，问卷已退役） ----
   本地文件只能由用户亲手给（浏览器安全边界，不是确认面板）：调起系统文件对话框选 .json
   → plan 预览 → 立刻 apply 入库。撞整集重复：按授权直接 force 入库（记账 + 回执如实说 +
   回收站可撤销），不再单开一道题。 */

function actPickJsonFile() {
    return new Promise(function (resolve) {
        const input = document.createElement("input");
        input.type = "file";
        input.accept = ".json";
        input.addEventListener("change", function () {
            const file = input.files && input.files[0];
            if (!file) { resolve(null); return; }
            const reader = new FileReader();
            reader.onload = function () {
                resolve({ name: file.name || "", text: String(reader.result || ""), size: file.size || 0 });
            };
            reader.onerror = function () { resolve(null); };
            reader.readAsText(file, "utf-8");
        });
        input.click();
    });
}

function actImportCardHtml(r) {
    return '<div class="arx-card"><div class="arx-card-title">导入本地数据</div>'
        + '<div class="arx-card-row"><span class="arx-card-name">文件</span><span class="arx-card-meta">'
        + escapeHtml(r.filename || "") + "</span></div>"
        + '<div class="arx-card-row"><span class="arx-card-name">记录</span><span class="arx-card-meta">'
        + (r.record_count || 0) + " 条 · 已写入外部库</span></div>"
        + ((r.warnings || []).map(function (w) { return '<p class="arx-card-empty">⚠ ' + escapeHtml(w) + "</p>"; }).join(""))
        + "</div>";
}

async function actRunCurateImport() {
    arxDecision("选择要导入的 JSON 文件");
    const picked = await actPickJsonFile();
    if (!picked || !picked.text) return { ok: false, cancelled: true };
    arxDecisionDone(picked.name || "已选文件");
    arxStep("生成导入预览（暂不写入文件）");
    const got = await actCuratePost(API.curatePlan, { action: "import", payload_json: picked.text, filename: picked.name });
    if (!got.ok) return { ok: false, error: got.error };
    const pr = got.result;
    const dup = pr.duplicate || {};
    if (dup.is_duplicate) {
        arxStep("内容与已有文件完全重复（" + (dup.matched_files || []).join("、") + "）；仍按你的要求入库，已做记录");
    }
    arxStep("写入外部库：" + (pr.record_count || 0) + " 条记录");
    const applied = await actCuratePost(API.curateApply, {
        action: "import", confirm_token: (pr && pr.confirm_token) || "",
        payload_json: picked.text, filename: picked.name, force: !!dup.is_duplicate,
    });
    if (!applied.ok) return { ok: false, error: applied.error + "；本次没有任何改动" };
    const r = applied.result;
    const policy = [String(r.write_boundary || "")];
    if (r.forced) policy.push("这次的内容与已有文件完全重复，仍已按你的要求入库，并做了记录。");
    policy.push("撤回这次导入：点下方「撤回这次执行」即可（进回收站，还能移回）；也可以说「删掉 " + (r.filename || "那个文件") + "」。");
    return { ok: true, artifact: { filename: r.filename || "", n_records: r.record_count },
        policy: policy.filter(Boolean), cardHtml: actImportCardHtml(Object.assign({}, pr, r)) };
}

/* ---- curate.remove：直接执行（2026-08-03 agent2 全自动化，问卷已退役） ----
   对象由 plan.slots.target（LLM 从原话读出的文件名）定位：清单里子串匹配——唯一命中 →
   plan → apply 链式直推（回收站可逆）；多个命中如实列出候选请用户说具体点；没命中如实报错。 */

function actMatchFile(target, files, keyOf) {
    const t = String(target || "").trim().toLowerCase();
    if (!t) return { none: true };
    const hits = (files || []).filter(function (f) {
        return String(keyOf(f) || "").toLowerCase().indexOf(t) >= 0;
    });
    if (hits.length === 1) return { one: hits[0] };
    // 零命中绝不能返回 { many: [] }——空数组是真值，会截胡消费方的 if (m.many) 多命中分支，
    // 让「没有这个名字的文件。现有：…」分支（!m.one）变死代码（2026-08-15 触发点审计 H-01）。
    if (hits.length) return { many: hits };
    return { none_target: true };
}

async function actRunCurateRemove(plan) {
    arxStep("读取外部库清单");
    const lst = await actCuratePost(API.curatePlan, { action: "list" });
    if (!lst.ok) return { ok: false, error: lst.error };
    const options = (lst.result.files || []).filter(function (f) { return f.curatable; });
    if (!options.length) {
        return { ok: false, error: "外部库里现在没有可以删除的上传文件；官方快照不能用对话删除。" };
    }
    const target = String(((plan && plan.slots) || {}).target || "");
    const m = actMatchFile(target, options, function (f) { return f.filename; });
    if (m.none) {
        return { ok: false, error: "没说删哪一份——外部库里有：" + options.map(function (f) { return f.filename; }).join("、")
            + "。说「删掉 文件名」就行（进回收站，能移回）" };
    }
    if (m.many) {
        return { ok: false, error: "「" + target + "」对上了 " + m.many.length + " 个文件：" + m.many.map(function (f) { return f.filename; }).join("、")
            + "——说具体一点（给完整文件名）" };
    }
    if (!m.one) {
        return { ok: false, error: "外部库里没有名字含「" + target + "」的文件。现有：" + options.map(function (f) { return f.filename; }).join("、") };
    }
    const filename = m.one.filename;
    arxStep("生成删除预告：" + filename);
    const got = await actCuratePost(API.curatePlan, { action: "remove", filename: filename });
    if (!got.ok) return { ok: false, error: got.error };
    const pr = got.result;
    arxStep("移入回收站：" + (pr.record_count || 0) + " 条记录");
    const applied = await actCuratePost(API.curateApply, { action: "remove", confirm_token: (pr && pr.confirm_token) || "", filename: filename });
    if (!applied.ok) return { ok: false, error: applied.error + "；本次没有任何改动" };
    // moved_to = ".userdata/recycle/<recycle_name>"——取叶子名供「撤回这次执行」（restore 的定位键）。
    const movedTo = String((applied.result && applied.result.moved_to) || "");
    return {
        ok: true,
        artifact: { filename: filename, recycle_name: movedTo.split("/").pop() || "", n_records: applied.result.record_count },
        policy: [applied.result.write_boundary, "撤回这次删除：点下方「撤回这次执行」即可移回；也可以说「把删掉的找回来」。"].filter(Boolean),
    };
}

/* ---- curate.restore：直接执行（2026-08-03 agent2 全自动化，问卷已退役） ----
   对象定位与 remove 同径（回收站清单子串匹配）；撞同名时后端 fail-closed，前端提前如实说。 */

async function actRunCurateRestore(plan) {
    arxStep("读取回收站清单");
    const lst = await actCuratePost(API.curatePlan, { action: "list" });
    if (!lst.ok) return { ok: false, error: lst.error };
    const options = lst.result.recycle || [];
    if (!options.length) return { ok: false, error: "回收站现在是空的，没有可以移回的文件" };
    const target = String(((plan && plan.slots) || {}).target || "");
    let m = actMatchFile(target, options, function (f) { return f.original_filename || f.recycle_name; });
    // 回收站只有一个候选时，「把删掉的找回来」这类不点名的说法指向唯一无歧义——直接移回，
    // 不必逼用户念文件名（e2e 实测：LLM 对无文件名的句子抽不出 target 槽位，此前必死在这里）。
    if (m.none && options.length === 1) m = { one: options[0] };
    if (m.none) {
        return { ok: false, error: "没说移回哪一份——回收站里有：" + options.map(function (f) { return f.original_filename || f.recycle_name; }).join("、") };
    }
    if (m.many) {
        return { ok: false, error: "「" + target + "」对上了 " + m.many.length + " 个文件：" + m.many.map(function (f) { return f.original_filename || f.recycle_name; }).join("、")
            + "——说具体一点" };
    }
    if (!m.one) {
        return { ok: false, error: "回收站里没有名字含「" + target + "」的文件。现有：" + options.map(function (f) { return f.original_filename || f.recycle_name; }).join("、") };
    }
    const filename = m.one.recycle_name || "";
    arxStep("生成移回预览：" + (m.one.original_filename || filename));
    const got = await actCuratePost(API.curatePlan, { action: "restore", filename: filename });
    if (!got.ok) return { ok: false, error: got.error };
    const pr = got.result;
    if (pr.will_conflict) {
        return { ok: false, error: "外部库里已有同名文件 " + String(pr.target_filename || "")
            + "。为避免覆盖，这次没有移回；请先处理那个文件（改名或移入回收站）再重试。" };
    }
    arxStep("移回外部库：" + (pr.record_count || 0) + " 条记录");
    const applied = await actCuratePost(API.curateApply, { action: "restore", confirm_token: (pr && pr.confirm_token) || "", filename: filename });
    if (!applied.ok) return { ok: false, error: applied.error + "；本次没有任何改动" };
    // restored_to = "database/external/<原名>"——取叶子名供「撤回这次执行」（再 remove 的定位键）。
    const restoredTo = String((applied.result && applied.result.restored_to) || "");
    return { ok: true, artifact: { filename: filename, restored_name: restoredTo.split("/").pop() || "", n_records: applied.result.record_count },
        policy: [applied.result.write_boundary].filter(Boolean) };
}

/* ---- curate.db_status：数据库状态汇报（2026-08-03 agent2 P1-B6，只读） ----
   事实双通道同一真源（corpus_status.db_status）：agent 图内 execute 已调过工具 → 用
   plan.observation；未装扩展（保底规划）→ POST /api/curate/status 现取。
   汇报措辞：数字卡在下面渲染；「组织成简明中文汇报」agent 路径由 narrate（plan.report_zh）、
   保底路径由 /api/act/summary 的 LLM 一句话完成（actFinish 既有通道）。 */

function actDbStatusCardHtml(obs) {
    const srcRows = (obs.sources || []).map(function (s) {
        return '<div class="arx-card-row"><span class="arx-card-name">' + escapeHtml(s.label || s.source || "")
            + '</span><span class="arx-card-meta">' + (s.local_count || 0) + " 条"
            + (s.snapshot_date ? " · 快照 " + escapeHtml(s.snapshot_date) : "") + "</span></div>";
    }).join("");
    const ext = obs.external_files || [], rec = obs.recycle || [];
    const extRows = ext.map(function (f) {
        return '<div class="arx-card-row"><span class="arx-card-name">' + escapeHtml(f.filename || "")
            + '</span><span class="arx-card-meta">' + (f.record_count == null ? "条数未知" : f.record_count + " 条") + "</span></div>";
    }).join("");
    const recRows = rec.map(function (f) {
        return '<div class="arx-card-row"><span class="arx-card-name">' + escapeHtml(f.original_filename || "")
            + '</span><span class="arx-card-meta">' + (f.record_count == null ? "条数未知" : f.record_count + " 条") + "</span></div>";
    }).join("");
    const ledger = obs.ledger || {};
    const ledgerLine = ledger.entries
        ? '<div class="arx-card-row"><span class="arx-card-name">近期联网操作记录</span><span class="arx-card-meta">'
            + ledger.entries + " 条</span></div>"
        : '<p class="arx-card-empty">近期没有联网操作记录。</p>';
    return '<div class="arx-card"><div class="arx-card-title">本地库 · 共 ' + (obs.total_records || 0)
        + " 条 · " + (obs.sources || []).length + " 个来源</div>"
        + srcRows
        + '<div class="arx-card-title">外部库 · ' + ext.length + " 个文件（可对话管护）</div>"
        + (extRows || '<p class="arx-card-empty">外部库现在是空的。</p>')
        + '<div class="arx-card-title">回收站 · ' + rec.length + " 个（说「把删掉的找回来」可移回）</div>"
        + (recRows || "")
        + '<div class="arx-card-title">审计</div>' + ledgerLine
        + "</div>";
}

async function actRunCurateDbStatus(plan) {
    arxStep("读取数据库状态");
    let obs = plan && plan.observation;
    if (!obs) {
        const got = await actCuratePost(API.curateStatus, { action: "db_status" });
        if (!got.ok) return { ok: false, error: got.error };
        obs = got.result;
    }
    return {
        ok: true,
        artifact: { n_sources: (obs.sources || []).length, n_records: obs.total_records },
        policy: ["这一步只做了汇报，没有改动任何文件。"],
        cardHtml: actDbStatusCardHtml(obs),
    };
}

/* ---- 图内已执行渲染通道（2026-08-04 长程多步执行） ----
   agent 图内已**真跑过**工具（plan.steps 是后端执行的真实记录，含结果或失败原因）时，
   前端只渲染、**绝不调 runner**（双执行红线：同一批工具绝不在后端跑一遍、前端再跑一遍）。
   卡片复用既有渲染函数（同一种事实只有一份渲染真源）；ok=false 的步渲染错误卡——
   同 .arx-card 容器 + .arx-card-empty 错误句原样。 */

function actRollbackPolicyLine(s) {
    const r = (s && s.result) || {};
    if (r.rolled_back !== true) {
        return String(r.note_zh || "回滚没有完成，未改动本地库。");
    }
    const recovered = (r.recycled || []).length + (r.restored || []).length;
    const failed = (r.unrestorable || []).length + (r.errors || []).length;
    return "这一步已回滚，实际恢复或移入回收站 " + recovered + " 个文件"
        + (failed ? "；" + failed + " 项未能恢复" : "") + "。";
}

function actLoopStepCardHtml(s) {
    if (!s || s.ok !== true) {
        return '<div class="arx-card"><div class="arx-card-title">'
            + escapeHtml(String((s && s.verb_zh) || "执行")) + " · 没有完成</div>"
            + '<p class="arx-card-empty">' + escapeHtml(String((s && s.error) || "这一步没有完成")) + "</p></div>";
    }
    const r = s.result || {};
    if (s.card_kind === "db_status") return actDbStatusCardHtml(r);
    if (s.card_kind === "check_updates") return actCheckUpdatesCardHtml(r);
    if (s.card_kind === "search_online") return actSearchCardHtml(r, r);   // 合并 dict：pr/r 字段同在一份
    if (s.card_kind === "sync_updates") return actSyncUpdatesCardHtml(r);
    if (s.card_kind === "search_rerun") return actSearchRerunCardHtml(r);
    // cd1（2026-08-19）四工具专项卡：同一种事实只有一份渲染真源（与既有 card_kind 分支同纪律）。
    if (s.card_kind === "compare") return actCompareCardHtml(r);
    if (s.card_kind === "cite_export") return actCiteExportCardHtml(r);
    if (s.card_kind === "compat_find") return actCompatFindCardHtml(r);
    if (s.card_kind === "fair_check") return actFairCheckCardHtml(r);
    if (s.card_kind === "rollback") {
        return '<div class="arx-card"><div class="arx-card-title">回滚写操作</div>'
            + '<p class="arx-card-empty">' + escapeHtml(actRollbackPolicyLine(s)) + "</p></div>";
    }
    // fp1（2026-08-17）：环内 display=false 探测步的兜底回执——如实说「没有要展示的」，
    // 不暗示有结果被藏起来（结果只进 observation 供后续步骤判断，本就设计上屏外）。
    return '<div class="arx-card"><p class="arx-card-empty">这一步已经跑完，没有需要展示的内容。</p></div>';
}

/* ---- 环内四工具专项卡（cd1 2026-08-19）：compare.datasets / cite.export / compat.find / fair.check ----
   图内已执行通道（plan.steps 非空）里这四种 card_kind 的**专项卡**：一卡一工具、默认只给核心
   信息，明细收进 <details>。degraded=true 是**数据不是故障**（找不到对象/无结果/歧义等如实
   降级）——卡片只显示诚实降级句（comparison_zh/note_zh 即降级句），绝不渲染空表格/空计数。
   动态值一律 escapeHtml 后拼接（XSS 红线）；字节数走 act_core 的 tpBytes 单一真源；
   下载链接端点集中声明在 core.js 的 API（act.js 不手写端点地址）。HTML 随 actFinish 的
   html 通道进 .cbh-sys-extra（entry.html 是对话流重画的真源——历史重画随 html 一起恢复）。 */

function actCompareCardHtml(r) {
    // 降级档：comparison_zh 即诚实降级句（无结果/找不到/歧义/同数据集/只有一条可比），只上这一句。
    if (r.degraded === true) {
        return '<div class="arx-card"><div class="arx-card-title">数据集对比</div>'
            + '<p class="arx-card-empty">' + escapeHtml(String(r.comparison_zh || "")) + "</p></div>";
    }
    const fields = Array.isArray(r.fields) ? r.fields : [];
    const nameOf = function (side) {
        const d = (r[side] || {});
        return String(d.dataset_name || d.dataset_uid || side);
    };
    const segs = [
        { n: Number(r.n_same || 0), label: "项相同", cls: "same" },
        { n: Number(r.n_diff || 0), label: "项不同", cls: "diff" },
        { n: Number(r.n_unknown || 0), label: "项未标注", cls: "unknown" },
    ].map(function (s) {
        return '<span class="arx-cmp-seg arx-cmp-' + s.cls + '">' + s.n + " " + s.label + "</span>";
    }).join("");
    const rows = fields.map(function (f) {
        return '<div class="arx-card-row"><span class="arx-card-name">' + escapeHtml(String(f.label_zh || f.field || ""))
            + '</span><span class="arx-card-meta">' + escapeHtml(String(f.a || "")) + " ↔ " + escapeHtml(String(f.b || ""))
            + "</span></div>";
    }).join("");
    return '<div class="arx-card"><div class="arx-card-title">数据集对比 · '
        + escapeHtml(nameOf("a")) + " ↔ " + escapeHtml(nameOf("b")) + "</div>"
        + '<p class="arx-cmp-concl">' + escapeHtml(String(r.comparison_zh || "")) + "</p>"
        + (r.assumption_zh ? '<p class="arx-card-empty">' + escapeHtml(String(r.assumption_zh)) + "</p>" : "")
        + '<div class="arx-cmp-bar">' + segs + "</div>"
        + (fields.length ? '<details class="arx-card-details"><summary>字段明细（' + fields.length + " 项）</summary>" + rows + "</details>" : "")
        + (r.caveat_zh ? '<p class="arx-card-empty">' + escapeHtml(String(r.caveat_zh)) + "</p>" : "")
        + "</div>";
}

/* dl-auto-1 任务B：环内 cite.export 引文导出后，把落盘产物（RIS + BibTeX）自动下载到浏览器
   下载目录（卡片渲染即触发一次）；卡片上的「下载」按钮保留为重下入口。防重：同一份产物
   （按文件名集合签名）只自动下一次，重画/重渲染不再重复触发。自动下载失败不阻断——手动按钮仍在。 */
let _citeAutoDownloaded = new Set();
function actAutoDownloadCiteFiles(r) {
    const files = Array.isArray(r.files) ? r.files : [];
    if (!files.length) return;
    const key = files.map(function (f) { return String(f.filename || ""); }).join("|");
    if (_citeAutoDownloaded.has(key)) return;
    _citeAutoDownloaded.add(key);
    files.forEach(function (f) {
        const fn = String(f.filename || "");
        if (!fn) return;
        fetch(API.citationsDownload + "?f=" + encodeURIComponent(fn))
            .then(function (resp) { if (!resp.ok) throw new Error("http " + resp.status); return resp.blob(); })
            .then(function (blob) { downloadBlobAs(blob, fn); })
            .catch(function (_e) { /* 自动下载失败：手动按钮仍在，不报错 */ });
    });
}

function actCiteExportCardHtml(r) {
    const files = Array.isArray(r.files) ? r.files : [];
    // 无产物档（无结果/都解析不到）：note_zh 即诚实句，只上这一句——不给空的文件列表占位。
    if (!files.length) {
        return '<div class="arx-card"><div class="arx-card-title">引文导出</div>'
            + '<p class="arx-card-empty">' + escapeHtml(String(r.note_zh || "")) + "</p></div>";
    }
    const rows = files.map(function (f) {
        const fn = String(f.filename || "");
        const href = API.citationsDownload + "?f=" + encodeURIComponent(fn);
        return '<div class="arx-card-row"><span class="arx-card-name">' + escapeHtml(fn)
            + " · " + escapeHtml(String(f.format || "")) + " · " + tpBytes(Number(f.bytes) || 0) + "</span>"
            + '<a class="btn act-chip" href="' + escapeHtml(href) + '" download>下载</a></div>';
    }).join("");
    const uids = Array.isArray(r.uids) ? r.uids : [];
    return '<div class="arx-card"><div class="arx-card-title">引文导出 · ' + (Number(r.n_datasets) || 0) + " 个数据集</div>"
        + (r.note_zh ? '<p class="arx-card-empty">' + escapeHtml(String(r.note_zh)) + "</p>" : "")
        + rows
        + (uids.length ? '<details class="arx-card-details"><summary>导出对象（' + uids.length + " 个编号）</summary>"
            + uids.map(function (u) {
                return '<div class="arx-card-row"><span class="arx-card-name">' + escapeHtml(String(u)) + "</span></div>";
            }).join("") + "</details>" : "")
        + "</div>";
}

function actCompatFindCardHtml(r) {
    // 降级档（找不到种子/无结果/歧义）：note_zh 即诚实降级句，只上这一句 + 恒带的 caveat。
    if (r.degraded === true) {
        return '<div class="arx-card"><div class="arx-card-title">元数据兼容</div>'
            + '<p class="arx-card-empty">' + escapeHtml(String(r.note_zh || "")) + "</p>"
            + (r.caveat ? '<p class="arx-card-empty">' + escapeHtml(String(r.caveat)) + "</p>" : "")
            + "</div>";
    }
    const seed = r.seed || {};
    const comps = Array.isArray(r.compatible) ? r.compatible : [];
    const crit = r.criteria || {};
    const critBits = [].concat(crit.species || []).map(String);
    if (crit.chemistry) critBits.push("chemistry " + crit.chemistry);
    if (crit.platform_family) critBits.push("platform " + crit.platform_family);
    const rows = comps.map(function (c) {
        return '<div class="arx-card-row"><span class="arx-card-name">' + escapeHtml(String(c.dataset_name || ""))
            + '</span><span class="arx-card-meta">' + escapeHtml(String(c.dataset_uid || ""))
            + (c._compat_basis ? " · " + escapeHtml(String(c._compat_basis)) : "") + "</span></div>";
    }).join("");
    return '<div class="arx-card"><div class="arx-card-title">元数据兼容 · ' + (Number(r.total) || 0) + " 个兼容数据集</div>"
        + (r.note_zh ? '<p class="arx-card-empty">' + escapeHtml(String(r.note_zh)) + "</p>" : "")
        + (seed.dataset_name
            ? '<div class="arx-card-row"><span class="arx-card-name">种子数据集</span><span class="arx-card-meta">'
                + escapeHtml(String(seed.dataset_name || "")) + "</span></div>"
            : "")
        + (critBits.length
            ? '<div class="arx-card-row"><span class="arx-card-name">兼容判据</span><span class="arx-card-meta">'
                + escapeHtml(critBits.join(" · ")) + "</span></div>"
            : "")
        + (rows || '<p class="arx-card-empty">没有找到兼容数据集。</p>')
        + (r.caveat ? '<p class="arx-card-empty">' + escapeHtml(String(r.caveat)) + "</p>" : "")
        + "</div>";
}

function actFairCheckCardHtml(r) {
    // 降级档（找不到对象/无结果/歧义）：note_zh 即诚实降级句，只上这一句——不给空的逐项表占位。
    if (r.degraded === true) {
        return '<div class="arx-card"><div class="arx-card-title">FAIR 自检</div>'
            + '<p class="arx-card-empty">' + escapeHtml(String(r.note_zh || "")) + "</p></div>";
    }
    const fair = r.fair || {};
    const summary = fair.summary || {};
    const pct = Number(summary.readiness_pct);
    const checks = Array.isArray(fair.checks) ? fair.checks : [];
    const statusZh = { pass: "充分", partial: "部分", unknown: "未知" };
    const rows = checks.map(function (c) {
        return '<div class="arx-card-row"><span class="arx-card-name">'
            + escapeHtml(String(c.id || "") + " " + String(c.label || "")) + '</span>'
            + '<span class="arx-card-meta">' + escapeHtml(String(statusZh[c.status] || c.status || ""))
            + (c.evidence ? " · " + escapeHtml(String(c.evidence)) : "") + "</span></div>";
    }).join("");
    const counts = (Number(summary.pass) || 0) + " 项充分 · " + (Number(summary.partial) || 0)
        + " 项部分 · " + (Number(summary.unknown) || 0) + " 项未知";
    return '<div class="arx-card"><div class="arx-card-title">FAIR 自检 · ' + escapeHtml(String(r.dataset_name || "")) + "</div>"
        + (Number.isFinite(pct)
            ? '<div class="arx-fair-pct">' + pct + '<span class="arx-fair-pct-unit">%</span></div>'
            : '<p class="arx-card-empty">' + escapeHtml(String(r.note_zh || "")) + "</p>")
        + '<div class="arx-fair-counts">' + counts + "</div>"
        + (rows ? '<details class="arx-card-details"><summary>逐项明细（' + checks.length + " 项）</summary>" + rows + "</details>" : "")
        + (r.note_zh ? '<p class="arx-card-empty">' + escapeHtml(String(r.note_zh)) + "</p>" : "")
        + "</div>";
}

/* ---- search.rerun（2026-08-16 检索工具化 sr1，设计 §4）：换词重检步骤卡 ----
   只出摘要三要素：改写词 + 采纳/拒绝 + n_before→n_after（择优闸口径，后端实算）。
   动作链里本工具恒 replace_screen=false（结果只进 observation 供后续步骤判断）——
   卡片措辞绝不暗示换屏；零命中救回通道的换屏不走这张卡（search.js 直接换屏 + sys 留痕）。
   n_before 可能为 null（链内无现场基准，后端如实记 null）→ 显示「—」，不编造 0。 */
function actSearchRerunCardHtml(r) {
    const rw = String(r.query || "");
    const nb = (r.n_before == null) ? "—" : String(r.n_before);
    const na = (r.n_after == null) ? "—" : String(r.n_after);
    let verdict;
    if (r.adopted === true) {
        verdict = "已采纳，结果供后续步骤判断（不换屏）";
    } else if (r.reason === "rewrite_no_change_kept_original") {
        verdict = "未采纳：改写后结果集没有变化，保留原结果";
    } else {
        verdict = "未采纳，保留原结果";
    }
    return '<div class="arx-card"><div class="arx-card-title">换词重检</div>'
        + '<div class="arx-card-row"><span class="arx-card-name">改写为「' + escapeHtml(rw) + "」</span>"
        + '<span class="arx-card-meta">' + nb + " → " + na + " 条</span></div>"
        + '<p class="arx-card-empty">' + escapeHtml(verdict) + "</p></div>";
}

const ACT_RUNNERS = {
    "pack.download": actRunPackDownload,
    "pack.preview": actRunPackPreview,
    "cite.export": actRunCiteExport,
    "reuse.pack": actRunReusePack,
    "feasibility.run": actRunFeasibility,
    "files.show": actRunFilesShow,
    "curate.list": actRunCurateList,
    "curate.check_updates": actRunCurateCheckUpdates,
    "curate.sync_updates": actRunCurateSyncUpdates,
    "curate.db_status": actRunCurateDbStatus,
    "curate.import": actRunCurateImport,
    "curate.search_online": actRunCurateSearchOnline,
    "curate.remove": actRunCurateRemove,
    "curate.restore": actRunCurateRestore,
};

/* ---------------- 总结（事实句 + LLM 改写 + 「没做到的」紧凑行 + 纠错 chips + 过程 details） ----------------
   事实句/「没做到的」行的构造在纯核 act_core.js（ACT_LEAD / actWhatHappened / actReceiptFrom，
   配 node 真行为门）——本文件只负责把它们挂进对话流（sys 条目 + entry.html），
   并异步请后端 LLM 把事实句改写成自然语言总结（fail-open：不成则事实句留存）。 */

/* 「撤回这次执行」的对象解析（2026-08-04 用户：最近一次执行直接给撤回钮）。
   只有 curate 系**写**动词可撤——它们都在文件粒度有干净互逆：import/search_online 的撤＝remove 进回收站；
   remove 的撤＝restore 移回（定位键 recycle_name）；restore 的撤＝再 remove（定位键 restored_to 原名）。
   pack/cite/reuse 落的是用户下载目录，本工具删不掉——绝不给钮（test_act_frontend 门钉死）；只读动词无需撤。
   图内已执行通道（多步）由 outcome.undo 直接供给（只撤最后一个真写成的写步）。 */
function actUndoSpec(plan, outcome) {
    if (!outcome || outcome.ok !== true || outcome.cancelled) return null;
    if (outcome.undo && outcome.undo.file) return outcome.undo;
    const verb = String((plan && plan.verb) || "");
    const art = outcome.artifact || {};
    if (verb === "curate.import" || verb === "curate.search_online" || verb === "curate.sync_updates") {
        return art.filename ? { action: "remove", file: String(art.filename) } : null;
    }
    if (verb === "curate.remove") {
        return art.recycle_name ? { action: "restore", file: String(art.recycle_name) } : null;
    }
    if (verb === "curate.restore") {
        return art.restored_name ? { action: "remove", file: String(art.restored_name) } : null;
    }
    return null;
}

function actFixChips(plan, said, outcome) {
    /* 纠错通道。每颗都是**真会发生点什么**的按钮，不是安慰性文案。
       刻意不叫「撤销」：已经落到下载目录的文件，本工具删不掉，给一颗撤销按钮就是骗人。
       curate 写动词的「撤回这次执行」用锚点注释包裹——「只留最近一次可撤」时整段好摘（actStripUndoChip）。 */
    const verb = String((plan && plan.verb) || "");
    const bits = [];
    if (verb === "pack.download" || verb === "pack.preview") {
        bits.push('<button type="button" class="btn act-chip" data-act-fix="panel">打开下载面板自己挑</button>');
    }
    // 文件类管护动词失败（典型：「未指明删哪份 / 文件不在外部库」——用户不知道确切的文件名）：
    // 给一颗「列出外部库的文件」候选 chip（2026-08-09 五机制批 · 婉拒候选；点击即把这句重新入环，
    // curate.list 是只读动词）。死胡同变成下一步的入口，而不是一句报错了事。
    if (outcome && outcome.ok === false && (verb === "curate.remove" || verb === "curate.restore")) {
        bits.push('<button type="button" class="btn act-chip" data-act-say="列出外部库里现在有哪些文件">列出外部库的文件</button>');
    }
    // 2026-08-16 pack1：「按原话重新检索」「以后别自动执行」两颗 chip 退役——
    // 以现在 agent 的性能它们已无存在必要（用户原话）；keepConv 重搜通道本身保留（board 路径仍在用）。
    const undo = actUndoSpec(plan, outcome);
    if (undo) {
        bits.push('<!--act-undo--><button type="button" class="btn act-chip" data-act-fix="undo" data-undo-action="'
            + escapeHtml(undo.action) + '" data-undo-file="' + escapeHtml(undo.file) + '">撤回这次执行</button><!--/act-undo-->');
    }
    return bits.length ? '<div class="act-fix">' + bits.join("") + "</div>" : "";
}

function actSummaryHtml(opts) {
    // 执行披露精简（ux1 2026-08-18）：工具结果卡 / 「明细（x条）」/「执行过程（x步）」
    // 折叠条全部撤下；工具调用计数摘要自 ku3-w3 起归信息流压缩行（flow_trace.compressFlow，
    // 渲染在气泡上方）。这里只留**功能钮**（撤回/纠错 chips）——
    // 它们是按钮不是披露；卡片/明细/执行过程的构造函数仍保留（测试钉死措辞），只是不再上屏。
    return (opts.chips ? opts.chips : "");
}

let _sumSeq = 0;   // LLM 总结请求代号：晚到的旧回包不改泡（与 _actBusy 串行叠加，双保险）

/* 异步请 LLM 把事实句改写成**一句话**自然语言总结（brief:true，设计 §5.3：≤35 字、只用事实）；
   成功则原位替换那条 sys 的正文（加「AI 总结」标；明细/hint/uncertainty 已在 details 折叠区，不动）。
   fail-open：任何不成（无 key/mock/网络/后端判否）都静默——事实句本来就已经在泡上。 */
function actFetchLlmSummary(plan, outcome, said, factual, entry) {
    if (!entry) return;
    const cfg = getConfig();
    if (cfg.provider === "mock") return;   // 结构性不调（后端同判否）：省一次注定无果的往返
    const mySeq = ++_sumSeq;
    const receipt = actReceiptFrom(plan, outcome, said);
    const gaps = receipt.rows.filter(function (r) { return r.k !== "做了什么"; })
        .reduce(function (acc, r) { return acc.concat(r.v); }, []);
    fetch(API.actSummary, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            verb_zh: String(plan.verb_zh || ""), utterance: String(said || ""), ok: !!outcome.ok,
            done_lines: [factual], gap_lines: gaps, policy_lines: [],
            brief: true,   // 一句话模式（设计 §5.3）：总结泡正文只留这一句，明细全在折叠区
            provider: cfg.provider, use_llm: true, mock_llm: false,
            api_key: cfg.api_key, base_url: cfg.base_url, model: cfg.model,
        }),
    }).then(function (res) { return res.json(); }).then(function (d) {
        if (mySeq !== _sumSeq) return;
        // 原位替换**正文**：「AI 总结」标照挂（ux1 起 entry.html 只剩功能钮，无折叠区可留）。
        if (d && d.ok && d.summary_zh) cbUpdateEntry(entry, { text: String(d.summary_zh), llmTag: true });
    }).catch(function () { /* fail-open：事实句留存 */ });
}

/* 持有撤回钮的那颗泡（=最近一次可撤执行）。entry.html 是对话流重画的真源——
   摘钮/换注记都必须改写 html 再 cbRenderHistory，只动 DOM 会在下一次重画时复活。 */
let _actUndoEntry = null;
function actStripUndoChip() {
    const prev = _actUndoEntry;
    _actUndoEntry = null;
    if (!prev || typeof prev.html !== "string" || prev.html.indexOf("<!--act-undo-->") < 0) return;
    prev.html = prev.html
        .replace(/<div class="act-fix"><!--act-undo-->[\s\S]*?<!--\/act-undo--><\/div>/, "")   // 撤回区是唯一 chip → 整区摘
        .replace(/<!--act-undo-->[\s\S]*?<!--\/act-undo-->/, "");                            // 兜底：与其他 chip 并存时只摘钮
    cbRenderHistory();
}

/* ku3-w3（唯一气泡规则的判定件）：计划是否纯检索（环内只有 rank/rerank/search.rerun）。
   与 board.js _RETRIEVAL_VERBS 同一口径——两处同步维护。 */
const _ACT_RETRIEVAL_VERBS = { "rank": true, "rerank": true, "search.rerun": true };
function _actRetrievalOnly(plan) {
    if (!plan || typeof plan !== "object") return false;
    let verbs;
    if (Array.isArray(plan.steps) && plan.steps.length) {
        verbs = plan.steps.map(function (s) { return String((s && s.verb) || "").trim(); }).filter(Boolean);
    } else {
        const v = String(plan.verb || "").trim();
        verbs = v ? [v] : [];
    }
    return verbs.length > 0 && verbs.every(function (v) { return !!_ACT_RETRIEVAL_VERBS[v]; });
}

/* 一次执行的收尾：步骤块折叠（trace 进总结泡 details）→ 总结 sys 上屏 → LLM 改写（若可用）。 */
function actFinish(plan, outcome, said, opts) {
    opts = opts || {};
    let factual;
    if (outcome.cancelled) {
        // 取消不是失败：用户主动叫停（如 import 的文件对话框取消），一个字节都没动——照实说，
        // 不走 ACT_LEAD.fail（那不是「没完成」，是「没有要做」；写成失败是谎报）。
        factual = "你取消了「" + String(plan.verb_zh || "执行") + "」，没有执行，也没有任何改动。";
    } else {
        // agent 的 narrate 已据真实工具数据写好汇报（plan.report_zh，如 curate.db_status）：
        // 正文直接用它——不再二次调 /api/act/summary 改写（那是给没有汇报的执行路径的通道）。
        factual = plan.report_zh ? String(plan.report_zh) : actWhatHappened(plan, outcome);
    }
    // ux1（2026-08-18 执行披露精简）：工具结果卡 / 「明细（x条）」/「执行过程（x步）」折叠条
    // 全部撤下——actSummaryHtml 只留功能钮（撤回/纠错 chips）；正文仍是 factual 这一句。
    // cd1（2026-08-19）：环内四工具专项卡经 opts.cardsHtml 走同一 html 通道上屏
    // （.cbh-sys-extra 区、气泡下方）——卡片进 entry.html，历史重画（cbRenderHistory）随 html
    // 一起恢复；其余执行路径保持 ux1 精简（无卡片）。
    // ku3-w3：「执行了 N 次检索」摘要句不再走本泡的 execSummary 通道——职能由信息流压缩行
    // （entry.flow，渲染在气泡上方、与流式工具行同一口径）取代。
    const cardsHtml = String((opts && opts.cardsHtml) || "");
    const chips = outcome.cancelled ? "" : actFixChips(plan, said, outcome);
    const html = cardsHtml
        ? cardsHtml + (chips ? chips : "")
        : actSummaryHtml({ chips: chips });
    // dl-auto-1 任务B：环内 cite.export 步执行成功 → 卡片上屏即自动下载产物（RIS + BibTeX）。
    // 放这里一次性触发（actFinish 每轮执行恰一次），不干扰 actLoopStepCardHtml 的纯卡片收集；
    // 自动下载失败不阻断（手动「下载」按钮仍在）。
    (Array.isArray(plan.steps) ? plan.steps : []).forEach(function (s) {
        if (s && s.ok && s.card_kind === "cite_export") actAutoDownloadCiteFiles(s.result || {});
    });
    // 「撤回」只留给**最近一次**执行（用户 2026-08-04）：新的可撤回执上屏时，旧泡的撤回钮就地摘除。
    const undo = outcome.cancelled ? null : actUndoSpec(plan, outcome);
    /* ku3-w3（唯一气泡规则）：纯检索计划（环内只有 rank/rerank/search.rerun）且 board 的批次
       回执已认领本轮唯一气泡（cbExecReceiptCovered）→ 本函数闭嘴，不再推第二颗泡。
       保守例外：有可撤回产物 / 环内专项卡 / 取消回音时照出本泡（这些信息批次回执扛不了）。
       抑制时 benchfb 采集照跑（留痕不断档）。 */
    if (!outcome.cancelled && !undo && !cardsHtml && _actRetrievalOnly(plan) && cbExecReceiptCovered()) {
        benchfbTurnAction({ verb: plan.verb_zh || plan.verb || "", cancelled: false, receipt: factual, trace: opts.trace || [] });
        return;
    }
    const entry = cbLogPush("sys", factual, { html: html });
    if (undo) { actStripUndoChip(); _actUndoEntry = entry; }
    // 汇报来源标注：后端契约字段 report_source 说明汇报是不是 LLM 写成——deterministic 兜底
    // 拼接不挂「AI 总结」小标（归因诚实）；字段缺席（旧后端）回退旧口径「有汇报即标」。
    if (plan.report_zh && plan.report_source !== "deterministic") cbUpdateEntry(entry, { llmTag: true });
    if (!outcome.cancelled && !plan.report_zh) actFetchLlmSummary(plan, outcome, said, factual, entry);   // 失败也改写（铁律禁说「已」）
    // benchmark 采集（b1）：工具执行回执收尾进档（在途轮并入、2 分钟内刚检索完的并回同一条）。
    benchfbTurnAction({ verb: plan.verb_zh || plan.verb || "", cancelled: !!outcome.cancelled, receipt: factual, trace: opts.trace || [] });
}

function actFixClick(event) {
    // 婉拒候选 chips（2026-08-09 五机制批）：data-act-say = 把这句当用户的话重新入环
    // （写进微信式输入行再走统一提交——与用户亲手打字同一条路径，不发明第二条入环通道）。
    const sayBtn = event.target.closest ? event.target.closest("[data-act-say]") : null;
    if (sayBtn) {
        const utterance = String(sayBtn.dataset.actSay || "").trim();
        if (!utterance) return;
        const input = $("chatInput");
        if (!input) return;
        input.value = utterance;
        ubSubmit("chat");
        return;
    }
    const btn = event.target.closest ? event.target.closest("[data-act-fix]") : null;
    if (!btn) return;
    const what = btn.dataset.actFix;
    if (what === "panel") {
        const panel = $("taskPackPanel");
        if (panel) panel.hidden = false;
        previewTaskPack();
        return;
    }
    // 2026-08-16 pack1：research（按原话重新检索）/off（以后别自动执行）两分支随 chip 一并退役。
    if (what === "undo") {
        actUndoRun(btn);
        return;
    }
}

/* 撤回最近一次执行（2026-08-04 用户）：撤回对象在执行落地那一刻就烙进 chip 的 data 属性——
   不靠账本反查（联网账本/agent 账行不落文件名），也不重新解析用户原话。
   plan/apply 两步与四个 curate runner 完全同径；逆映射在 actUndoSpec 一处说清。 */
async function actUndoRun(btn) {
    const action = String(btn.dataset.undoAction || "");
    const file = String(btn.dataset.undoFile || "");
    if (!file || (action !== "remove" && action !== "restore")) return;
    if (btn.disabled) return;
    btn.disabled = true;
    btn.textContent = "撤回中…";
    const fail = function (msg) {
        btn.disabled = false;
        btn.textContent = "撤回这次执行";
        toast("撤回没有完成：" + msg);
    };
    const got = await actCuratePost(API.curatePlan, { action: action, filename: file });
    if (!got.ok) { fail(got.error); return; }
    const pr = got.result || {};
    if (pr.will_conflict) {   // restore 撞同名：后端 fail-closed 零写入，如实上报
        fail("外部库里已有同名文件 " + String(pr.target_filename || "") + "；先处理那个文件再撤回");
        return;
    }
    const applied = await actCuratePost(API.curateApply, { action: action, confirm_token: pr.confirm_token || "", filename: file });
    if (!applied.ok) { fail(applied.error + "；本次没有任何改动"); return; }
    // 成功：撤回钮原位换成「已撤回」注记。这次撤回本身**不再**挂新钮（它不是新一轮对话里的「最近一次执行」，
    // 再挂会滚出无限撤回链）；想再动那个文件，说一句话就行。
    const done = action === "remove"
        ? "已撤回：" + file + " 已进回收站（说「把删掉的找回来」可移回）。"
        : "已撤回：文件已移回外部库。";
    const entry = _actUndoEntry;
    _actUndoEntry = null;
    const noteHtml = '<div class="act-fix act-undone">' + escapeHtml(done) + "</div>";
    if (entry && typeof entry.html === "string" && entry.html.indexOf("<!--act-undo-->") >= 0) {
        entry.html = entry.html.replace(/<div class="act-fix"><!--act-undo-->[\s\S]*?<!--\/act-undo--><\/div>/, noteHtml);
        cbRenderHistory();
    } else {   // 泡已被改写/剪枝的退化路径：就地改 DOM（下一次重画前用户看得到）
        const zone = btn.closest(".act-fix");
        if (zone) { zone.textContent = done; zone.classList.add("act-undone"); }
    }
    toast(action === "remove" ? "已撤回：文件进了回收站，可移回" : "已撤回：文件移回外部库");
}

/* ---------------- 主入口 ---------------- */

/* 派发一份**已拿到的** plan：取消态 → busy 闸 → 结果闸（按派发时屏上真实状态复算）→
   行动流（arxBegin + runner 的 arxStep/arxDecision）→ actFinish 总结。

   返回值契约：
   - **false** = 不属于执行（kind 路由类 / 词表与派发表不同步），调用方走原来的指路分支；
   - **true** = 接住了且全程由行动流 + 总结 sys 呈现（调用方只把那句原话标成 action，不挂注记）；
   - **一个字符串** = 接住了但要以注记形式挂到那句原话旁（取消态 reason / 上一步还在跑），
     与真实发生的事一致——调用方拿到它就原样挂，不许自己编「已按你说的执行」。 */
export async function actDispatchPlan(plan, said) {
    if (!plan || plan.kind !== "exec") return false;              // 路由类交回调用方，不算执行
    // 取消态（后端恒带 cancelled 字段）：动词照留但执行层**不得执行**——
    // 不开行动流、不出总结，只把后端的 reason_zh 原样交回，由调用方挂进对话流。
    if (plan.cancelled) return String(plan.reason_zh || "你说先不做这一步，所以这次没有执行。");
    /* plan.trace（后端各节点真实记录）渲染进行动流，本函数两通道（图内已执行 / runner 派发）
       共用这一个闭包。去重（设计 §5.2）：流式已播过的（plan._traceStreamed）不再二次渲染——
       步骤已经实时上屏，且会被 arxFinish 收进总结泡 details，再渲染一遍就是两套步骤。
       skipArx=true（行动流被在途 runner 占用）时不上屏、只返回快照；快照与 arxFinish 返回值
       同形（{text,state,detail}），供调用方直接进总结泡 details——同一事实，不落一行。 */
    const renderPlanTrace = (skipArx) => {
        const trace = plan._traceStreamed ? [] : (Array.isArray(plan.trace) ? plan.trace : []);
        const snap = [];
        trace.forEach(function (t, i) {
            if (!t) return;
            const label = String(t.label_zh || t.node || "").trim();
            if (!label) return;
            const text = (i === 0 ? "Agent 规划 · " : "") + label;
            // R2-2 P1-1：state/detail 一并进行动流——空闲通道（!skipArx）此前只推文案，
            // state 恒 running→done、detail 永远丢失，失败节点（如 repair「第一次规划不合法」）
            // 被渲染成 ✓ 且无原因；busy 快照通道却如实 → 同一事实两通道两个说法。
            // 现在流行与快照同源同形（{text,state,detail}），arxFinish 交回的快照与 snap 逐位一致。
            const state = t.ok === false ? "failed" : "done";
            const detail = String(t.detail || "");
            snap.push({ text: text, state: state, detail: detail });
            if (!skipArx) arxStep(text, { state: state, detail: detail });
        });
        return snap;
    };
    /* 图内已执行渲染通道（2026-08-04 长程多步执行）：plan.steps 非空 = agent 图内已真跑过
       工具（结果/失败都已在后端落定，流式路径步骤也已实时播过）——这里只渲染卡片与总结，
       绝不走 runner 再执行一遍（双执行红线）。
       必须在 busy 闸**之前**（2026-08-04 A4）：上一句的 runner 还在跑时，这一句的图内执行
       已经在后端真实发生（search_online 已入库、账本已落行）——先撞 busy 闸就永不渲染，
       还挂「没有执行」的注记，真实写入对界面隐身。本块无 await（纯同步渲染），且**绝不动
       _actBusy**：那是在途 runner 的闸，在这里清掉它，后续 runner 派发就失去保护。 */
    if (Array.isArray(plan.steps) && plan.steps.length) {
        // 行动流被在途 runner 占用时（_actBusy）不挤它的流：不 begin/step/finish，
        // 总结照出（ux1 起不再有执行过程快照进 details——折叠条已撤）。
        // ku3-w3：有未播过的 trace 步才开流——流式已播（_traceStreamed）或无 trace 时
        // 开流只会闪现一条「0 步」空行动流（过程展示归信息流工具行，见 board.js SSE 回调）。
        const flowFree = !_actBusy;
        const _traceSteps = plan._traceStreamed ? [] : (Array.isArray(plan.trace) ? plan.trace : []);
        if (flowFree && _traceSteps.length && !arxActive()) arxBegin(plan.verb_zh || "执行");
        cbProgressDrop();
        renderPlanTrace(!flowFree);
        let loopUndo = null;   // 撤回钮对象：最后一个真写成的写步（多步执行只撤最后一刀；其余说一句话即可）
        plan.steps.forEach(function (s) {
            // 写步判定用后端契约字段 s.readonly（LOOP_TOOLS 注册表真源随 step 带出）；
            // 字段缺席（旧后端/老记录）回退旧启发式「只有 search_online 是写步」。
            const isWrite = !!(s && (s.readonly === false || (s.readonly == null && s.card_kind === "search_online")));
            if (s && s.ok && isWrite && s.result && s.result.filename) {
                loopUndo = { action: "remove", file: String(s.result.filename) };
            }
        });
        // cd1（2026-08-19）：四工具专项卡——图内多步只渲染 compare/cite_export/compat_find/
        // fair_check 四种 card_kind（ux1 精简哲学：其余工具不上卡），按执行顺序拼接，
        // 随 actFinish 的 html 通道进 .cbh-sys-extra。失败步/降级步交给 actLoopStepCardHtml
        // 各自的诚实分支（错误卡 / 降级句），不在这里静默吞掉。
        const FOUR_TOOL_CARD_KINDS = { compare: 1, cite_export: 1, compat_find: 1, fair_check: 1 };
        const cardsHtml = plan.steps.map(function (s) {
            if (s && s.card_kind && FOUR_TOOL_CARD_KINDS[s.card_kind]) return actLoopStepCardHtml(s);
            return "";
        }).join("");
        const allOk = plan.steps.every(function (s) { return !!(s && s.ok); });
        const outcome = { ok: allOk, undo: loopUndo };
        if (!allOk && flowFree) arxFail();
        if (flowFree) arxFinish();
        // ux1：工具调用摘要统计 plan.steps 全量 verb（含失败步——失败也是真调用过）。
        actFinish(plan, outcome, said, {
            toolVerbs: plan.steps.map(function (s) { return s && s.verb; }),
            cardsHtml: cardsHtml,
        });
        return true;
    }
    // busy 闸只拦 runner 派发（没有 steps = 后端一个工具都没跑，「没有执行」是实话）；
    // 图内已执行的 plan 在上方通道已被接走，永远到不了这里。
    if (_actBusy) { toast("上一步还在执行，稍等一下"); return ACT_BUSY_NOTE; }
    const runner = ACT_RUNNERS[plan.verb];
    if (!runner) return false;                            // 词表与派发表不同步 → 交回，不瞎做
    _actLastSaid = String(said || "");   // curate.search_online 的关键词从它做确定性解析（槽位没有时）
    _actBusy = true;
    // 流式规划（设计 §5.2）：ubSubmit 的 SSE 已开过行动流并播过规划步骤——这里接力续跑，
    // 不重开（重开会把已播的规划步骤抹掉）；非流式路径照旧由这里开流。
    if (!arxActive()) arxBegin(plan.verb_zh || "执行");
    cbProgressDrop();   // 进度泡（点8）退场：这句的回复以行动流呈现，不重复回
    // agent 路径的规划步骤（plan.trace）：后端 langgraph 各节点随响应带回的**真实**记录
    // （{node, label_zh, detail, ok, ms}），路由阶段照单渲染、开头一步标明「Agent 规划」；
    // 执行阶段仍走 runner 自有步骤。trace 为空（规则兜底路径）时维持原样——不伪造步骤。
    renderPlanTrace(false);
    try {
        // 结果闸按**派发时**屏上真实状态复算，不照抄 plan 里规划时烙下的那档「没结果」旧态：
        // 统一框「先检索后派发」路径里 plan 是检索前规划的，检索落地后走到这里时屏上已经有结果了。
        const data = LAST_RECOMMEND_DATA || {};
        const hasResults = !!((data.results || []).length);
        if (plan.requires_results && !hasResults) {
            // 系统明明读懂了（verb 都判出来了）。把「没东西可做」谎报成「没听懂」，
            // 用户会去改说法，而问题根本不在说法上。
            arxFail();
            arxFinish();   // 折叠行动流（420ms 动效后整块撤下，只留总结泡）
            const blocked = { ok: false, error: "现在屏幕上还没有检索结果，先查一批数据再说一次就行" };
            actFinish(plan, blocked, said);   // 没真调工具 → 不传 toolVerbs（无摘要句）
            return true;
        }
        let outcome;
        try {
            outcome = await runner(plan);
        } catch (err) {
            outcome = { ok: false, error: String((err && err.message) || err) };
        }
        outcome = outcome || { ok: false, error: "这一步没有返回结果" };
        if (outcome.ok || outcome.cancelled) {
            arxFinish();   // 折叠行动流，只留总结泡
            // cancelled（用户叫停，如 import 对话框取消）＝没有执行，不报工具摘要；
            // 正常完成报 plan.verb 一次。
            actFinish(plan, outcome, said, { toolVerbs: outcome.cancelled ? [] : [plan.verb] });
        } else {
            arxFail();
            arxFinish();
            actFinish(plan, outcome, said, { toolVerbs: [plan.verb] });   // 失败也是真调用过
        }
        return true;
    } finally {
        _actBusy = false;
    }
}

/* 检索落地后的执行挂点。唯一档：`opts.actPlan`（统一框「先检索后派发」，uni1）——
   plan 是 /api/utterance 路由那次就规划好的，这里只派发、**不再二次调 /api/action/plan**。
   （旧主框 userSubmit 档与 actMaybeAutoAct 已退役：一切输入都过 /api/utterance 统一路由，
   不存在「检索落地后才想起要规划执行」的路径。） */
export function actAfterSearch(query, opts) {
    const stashed = opts && opts.actPlan;
    if (!stashed || !actEnabled()) return;
    const said = String(opts.actSaid || query || "");
    actDispatchPlan(stashed, said).then(function (mark) {
        // 执行注记挂到那句原话上（cbMarkLastSayAsAction 按 kind==="say" 定位——
        // 总结 sys 先落地，末尾已不是原话，按位置找会标错泡）。
        if (mark === true) cbMarkLastSayAsAction("");
        else if (mark) cbMarkLastSayAsAction(mark);
    }).catch(function (err) {
        toast("这一步没有执行：" + String((err && err.message) || err));
    });
}

export function initAct() {
    arxOnChange(cbRenderHistory);   // 行动流的重画收口到对话流（断环：act_run 不 import board）
    const hist = $("cbHistory");
    if (hist) hist.addEventListener("click", actFixClick);   // 纠错 chips 现在在总结泡里（事件委托，重建不丢）
}
