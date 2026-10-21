"use strict";
/* 条件板纯核的真行为规格。三门都不执行 JS，所以四分区归并与撤销栈的行为
   如果没有这份规格，就完全没有回归网。 */

import * as core from "../../web/static/js/panel/board_core.js";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
const { cbRowsFrom, cbSummary, cbPushFrame, cbGo, cbIsNoop, cbLabelForFilterId, CB_MAX_FRAMES } = core;

let checks = 0;
function ok(cond, what) {
    checks += 1;
    if (!cond) { console.error("FAIL: " + what); process.exit(1); }
}
function eq(actual, expected, what) {
    ok(JSON.stringify(actual) === JSON.stringify(expected),
       what + "  期望 " + JSON.stringify(expected) + " 实得 " + JSON.stringify(actual));
}

/* ---------------- 条件名还原 ---------------- */
eq(cbLabelForFilterId("include:species"), "物种", "正向物种");
eq(cbLabelForFilterId("exclude:disease"), "排除·疾病", "负向疾病要带排除前缀");
eq(cbLabelForFilterId("raw:required"), "原始数据", "原始数据");
eq(cbLabelForFilterId("raw:forbidden"), "原始数据", "原始数据（不要那一档）");
eq(cbLabelForFilterId("date:range"), "发表时间", "发表时间");
ok(cbLabelForFilterId("") === "", "空编号不崩且不编造名字");

/* ---------------- 四分区 ---------------- */
const ACTIVE = [
    { filter_id: "include:species", polarity: "include", dim: "species", label: "物种", values: ["Human"] },
    { filter_id: "include:tissue", polarity: "include", dim: "tissue", label: "组织", values: ["Lung"] },
    { filter_id: "raw:required", polarity: "include", dim: "has_raw_data", label: "原始数据", values: ["需要 FASTQ"] }
];
const rows = cbRowsFrom(ACTIVE, [{ dim: "species", value: "homo sapiens", display: "Homo sapiens" }],
                        ["disease"], ["include:platform"], ["tissue"]);
eq(rows.map(function (r) { return r.zone; }),
   ["query", "query", "query", "facet", "lenient", "suppressed"], "四分区顺序稳定");

const tissueRow = rows.filter(function (r) { return r.zone === "query" && r.dim === "tissue"; })[0];
ok(tissueRow.lenientable === true, "有覆盖缺口的那一项才可以放宽");
const speciesRow = rows.filter(function (r) { return r.zone === "query" && r.dim === "species"; })[0];
ok(speciesRow.lenientable === false, "没有缺口的项不显示放宽按钮");
ok(speciesRow.editable === true, "结构化正向项可以改");

const rawRow = rows.filter(function (r) { return r.dim === "has_raw_data"; })[0];
ok(rawRow.editable === false, "原始数据不是可以「换成别的值」的项");
ok(rawRow.lenientable === false, "原始数据不支持放宽");

const suppressedRow = rows.filter(function (r) { return r.zone === "suppressed"; })[0];
eq(suppressedRow.values, [], "已忽略的行不显示取值（那是可能过期的快照）");
eq(suppressedRow.label, "平台", "已忽略的行仍然要有中文条件名");

const negRows = cbRowsFrom([{ filter_id: "exclude:disease", polarity: "exclude", dim: "disease", label: "排除·疾病", values: ["Tumor"] }], [], [], [], []);
ok(negRows[0].editable === false, "负向条件不提供「换成」按钮");

/* ---------------- 软偏好：只排序、不筛选 ---------------- */
eq(cbLabelForFilterId("prefer:platform"), "优先·平台", "软偏好的条件名必须带「优先」");
eq(cbLabelForFilterId("prefer:raw"), "优先·原始数据", "prefer:raw 不能被还原成硬条件「原始数据」");
eq(cbLabelForFilterId("prefer:date"), "优先·发表时间", "prefer:date 不能被还原成硬条件「发表时间」");
eq(cbLabelForFilterId("prefer:source"), "优先·数据来源", "来源偏好也要带「优先」");

const PREF = [
    { filter_id: "include:tissue", polarity: "include", dim: "tissue", label: "组织", values: ["Lung"] },
    { filter_id: "prefer:platform", polarity: "prefer", dim: "platform", label: "优先·平台", values: ["Visium"] }
];
const prefRows = cbRowsFrom(PREF, [], [], [], []);
eq(prefRows.map(function (r) { return r.zone; }), ["query", "prefer"], "软偏好单独一个分区，不混进筛选条件");
ok(prefRows[1].editable === false, "软偏好不提供「换成」按钮");
ok(prefRows[1].removable === true, "软偏好可以单独停用");
/* 这是这个特性最核心的一句话：加一条「优先」，屏幕上的筛选条件数**一个都不能多**。
   多了就等于告诉用户「结果都满足这一条」，而其实一条数据都没被筛掉。 */
const filterCountIn = function (text) { return parseInt(/正在按 (\d+) 个/.exec(text)[1], 10); };
eq(filterCountIn(cbSummary(cbRowsFrom(PREF, [], [], [], []), 235)),
   filterCountIn(cbSummary(cbRowsFrom([PREF[0]], [], [], [], []), 235)), "加一条「优先」不会让筛选条件数变大");
ok(cbSummary(prefRows, 235).indexOf("只用来排先后，没有筛掉数据") > 0, "摘要必须点明它没有筛掉数据");

/* 停用一条排序偏好：结果集一条都不会变，所以不能说成「没有用它筛」 */
const prefOff = cbRowsFrom([PREF[0]], [], [], ["prefer:platform"], []);
eq(prefOff.map(function (r) { return r.zone; }), ["query", "prefer_off"], "被停用的偏好不混进「没有用它筛」");
eq(prefOff[1].label, "优先·平台", "停用后仍然看得出这是一条排序偏好");
ok(cbSummary(prefOff, 235).indexOf("排序偏好这次没有生效") > 0, "停用偏好要说成「没有生效」而不是「没有用它筛」");
ok(cbSummary(prefOff, 235).indexOf("没有用它筛") < 0, "停用偏好绝不能说成放宽了筛选");

eq(cbRowsFrom(null, null, null, null, null), [], "全空入参返回空表、不崩");
eq(cbRowsFrom([null, 3, "x"], [null], [], [], []), [], "非法项被安全丢弃");

/* 缺字段的老数据不能崩 */
const legacy = cbRowsFrom([{ dim: "species" }], [], [], [], []);
ok(legacy.length === 1 && legacy[0].label === "物种", "缺 filter_id 的老数据仍能还原条件名");
ok(legacy[0].removable === false, "没有编号就不该给「不按这条筛」按钮（点了也没用）");

/* ---------------- 摘要句 ---------------- */
eq(cbSummary(rows, 128), "正在按 4 个条件筛选，其中 1 项已放宽，另有 1 个这次没有用它筛 · 当前结果 128 条", "摘要三个数字都来自入参");
/* 放宽是给已有条件松绑，不是新加一条限制：松绑之后「正在按 N 个条件筛选」的 N 绝不许变大。 */
const beforeLenient = cbRowsFrom(ACTIVE, [], [], [], ["tissue"]);
const afterLenient = cbRowsFrom(ACTIVE, [], ["tissue"], [], ["tissue"]);
const nOf = function (text) { return parseInt(/正在按 (\d+) 个/.exec(text)[1], 10); };
ok(nOf(cbSummary(afterLenient, 9)) <= nOf(cbSummary(beforeLenient, 9)),
   "点了「放宽」之后，条件计数不会反而变多");
ok(cbSummary(afterLenient, 9).indexOf("其中 1 项已放宽") > 0, "放宽单独报，且报成「其中」");
ok(cbSummary(rows, 128).indexOf("库中") < 0, "绝不写「库中」——这个数是套过再缩小之后的存活数");
eq(cbSummary([], null), "当前没有任何筛选条件", "没有条件时不说「正在按 0 个条件筛选」");
eq(cbSummary([{ zone: "query" }], 0), "正在按 1 个条件筛选 · 当前结果 0 条", "0 条也要如实说");

/* ---------------- 撤销栈 ---------------- */
let stack = [], cursor = -1;
// 推 CB_MAX_FRAMES+5 帧：栈必须收在上限、丢最旧 5 帧（上限值由常量给出，规格不硬写数字）。
for (let i = 0; i < CB_MAX_FRAMES + 5; i += 1) {
    const next = cbPushFrame(stack, cursor, { query: "q" + i });
    stack = next.stack; cursor = next.cursor;
}
ok(stack.length === CB_MAX_FRAMES, "栈满之后不再增长，实得 " + stack.length);
ok(cursor === stack.length - 1, "丢最旧那一帧之后游标必须跟着回来，不能越界一格");
eq(stack[stack.length - 1].query, "q" + (CB_MAX_FRAMES + 4), "栈顶是最新一帧");
eq(stack[0].query, "q5", "丢掉的是最旧的那些");

const back1 = cbGo(stack, cursor, -1);
eq(back1.frame.query, "q" + (CB_MAX_FRAMES + 3), "上一步是倒数第二帧");
const back2 = cbGo(stack, back1.cursor, -1);
eq(back2.frame.query, "q" + (CB_MAX_FRAMES + 2), "连续两次上一步落到两个不同的帧");
ok(back1.frame.query !== back2.frame.query, "两次上一步不能原地不动");

const clampLow = cbGo(stack, 0, -1);
ok(clampLow.cursor === 0, "已经是第一步时夹住不动");
const clampHigh = cbGo(stack, stack.length - 1, 1);
ok(clampHigh.cursor === stack.length - 1, "已经是最后一步时夹住不动");
const empty = cbGo([], -1, -1);
ok(empty.cursor === -1 && empty.frame === null, "空栈是 no-op");

/* 在游标不在栈顶时推帧，会丢掉后面被放弃的分支 */
let branched = cbPushFrame(["a", "b", "c"], 0, "d");
eq(branched.stack, ["a", "d"], "从中间继续改会丢掉后面那些步");
ok(branched.cursor === 1, "游标停在新推的那一帧上");

/* 入参不被就地改动 */
const original = ["a", "b"];
cbPushFrame(original, 1, "c");
eq(original, ["a", "b"], "推帧不改调用方的数组");

/* ---------------- 有没有真的改到 ---------------- */
const base = { query: "人类肺", suppressed_constraints: [], lenient_dims: [], facet_filters: [], date_from: "", date_to: "" };
ok(cbIsNoop(base, JSON.parse(JSON.stringify(base))) === true, "一模一样就是没改");
ok(cbIsNoop(base, Object.assign({}, base, { query: "小鼠肺" })) === false, "换了句子就是改了");
ok(cbIsNoop(base, Object.assign({}, base, { lenient_dims: ["tissue"] })) === false, "放宽了也是改了");
ok(cbIsNoop(null, base) === false, "缺一边时不敢判「没改」");

/* ---------------- 逐条反馈操作条（msgfb，2026-08-28） ---------------- */
const { cbMsgFbNext, cbMsgCommentText, cbMsgForkable } = core;

/* 赞/倒赞互斥三态机 */
eq(cbMsgFbNext("", "up"), "up", "空态点赞 = 赞");
eq(cbMsgFbNext("up", "up"), "", "再点已选中的赞 = 取消");
eq(cbMsgFbNext("up", "down"), "down", "赞上点倒赞 = 换边");
eq(cbMsgFbNext("", "down"), "down", "空态点倒赞 = 倒赞");
eq(cbMsgFbNext("down", "down"), "", "再点已选中的倒赞 = 取消");
eq(cbMsgFbNext("down", "up"), "up", "倒赞上点赞 = 换边");
eq(cbMsgFbNext("bogus", "up"), "up", "脏初态按空态处理");
eq(cbMsgFbNext("up", "bogus"), "up", "脏动作不改变现状");

/* 评论入队文本：正文 + 引用尾（mid + 摘段） */
const cmt = cbMsgCommentText("这句很有用", "m7", "检索完成：库中共 36 条匹配");
ok(cmt.indexOf("这句很有用") === 0, "评论正文在前");
ok(cmt.indexOf("m7") > 0 && cmt.indexOf("检索完成") > 0, "引用尾带 mid 与摘段");
const longSnip = "一二三四五六七八九十".repeat(10);
const cmt2 = cbMsgCommentText("x", "m1", longSnip);
ok(cmt2.indexOf("…") > 0, "超长摘段截断加省略号");
ok(cmt2.length < longSnip.length + 40, "截断确实变短");
ok(cbMsgCommentText("x", "m1", "").indexOf("（原文不在本地）") > 0, "空摘段如实标注");
ok(cbMsgCommentText("x", "m1", "a\n\nb").indexOf("a b") > 0, "摘段压平换行");

/* 分支点判据 */
ok(cbMsgForkable(3, true) === true, "挂在存活帧上的回复可分支");
ok(cbMsgForkable(null, false) === false, "无帧回复不可分支");
ok(cbMsgForkable(3, false) === false, "帧已不在栈里不可分支");

/* ---------------- 纯度 ---------------- */
const src = readFileSync(fileURLToPath(new URL("../../web/static/js/panel/board_core.js", import.meta.url)), "utf8");
["localStorage", "sessionStorage", "document", "fetch(", "Date.now(", "new Date", "performance.now"]
    .forEach(function (bad) {
        ok(src.indexOf(bad) < 0, "纯核里不该出现 " + bad);
    });

/* 会话原样恢复：消息和结果引用独立保存，重编号后仍指向同一批结果。 */
const richLog = [
    { kind: "say", text: "第一句", id: "m1", frameId: 11 },
    { kind: "sys", text: "最终 AI 回复\n第二行", id: "m2", frameId: 11, llmTag: true,
        html: '<div class="receipt">附件</div>', flow: { summaryText: "执行了检索", expanded: [] },
        pills: [{ frameId: 11, batchId: "", label: "第一批", count: 3, active: true }], msgFb: "up" },
    { kind: "say", text: "第二句", id: "m3", frameId: 12 },
    { kind: "sys", text: "暂时失败", id: "m4", frameId: 12, isError: true, needsAgent: true },
    { kind: "sys", text: "检索之后的继续对话", id: "m5", frameId: null },
];
const savedMessages = core.cbHistoryMessages(richLog);
const historyRows = [
    { at: 100, query: "第一句", frameId: 11, snap: { results: [{ uid: "first" }] }, chat: savedMessages.slice(0, 2) },
    { at: 200, query: "第二句", frameId: 12, snap: { results: [] }, chat: savedMessages,
        facetFilters: [{ dim: "species", value: "Human" }], suppressed: ["include:tissue"], lenientDims: ["disease"],
        conversation: { version: 2, frameIds: [11, 12], cursorId: 11,
            view: { queryDraft: "草稿\n原样保留", chatDraft: "继续问", mode: "board", chatScroll: 37, openDetails: [true] } } },
];
const restoredHistory = core.cbHistoryRestore(historyRows, 80);
const pendingNextRows = structuredClone(historyRows);
pendingNextRows[0].chat.push({ k: "say", t: "第二句", i: "m3", r: null });
const beforePrefix = JSON.stringify(pendingNextRows);
const selectedPrefix = core.cbHistoryPrefix(pendingNextRows, 1);
eq(core.cbHistoryRestore(selectedPrefix).log.map((m) => m.text), richLog.slice(0, 2).map((m) => m.text),
    "按最终归属截断累计消息，较早行提前保存的下一句不混入分支");
eq(JSON.stringify(pendingNextRows), beforePrefix, "裁剪前缀不改写原会话");
eq(core.cbHistoryRestore(core.cbHistoryPrefix(historyRows, 2)).cursor, 1, "完整前缀分支停在所选末轮而非父会话旧游标");
const prunedRows = structuredClone(historyRows);
prunedRows[1].conversation.frameIds = [12];
eq(core.cbHistoryPrefix(prunedRows, 1).map((h) => h.frameId), [12], "已回退删除的旧帧不计入分支轮次");
eq(core.cbHistoryPrefix(historyRows.slice(1), 1), [], "首轮历史行被容量裁掉后不能拿后面的结果当分支点");
const retainedLatestPrefix = core.cbHistoryPrefix(historyRows.slice(1), 2);
eq(retainedLatestPrefix.map((h) => h.frameId), [12], "缺失更早结果时按保存的帧序列定位最新分支点");
eq(core.cbHistoryRestore(retainedLatestPrefix).log.map((m) => m.text), richLog.map((m) => m.text), "仍保留截至分支点的完整消息");
eq(restoredHistory.log.map((e) => e.text), richLog.map((e) => e.text), "全部消息和原有换行原样恢复");
eq(restoredHistory.log.map((e) => e.frameId), [81, 81, 82, 82, null], "消息显式结果归属不按累计长度猜测");
eq(restoredHistory.cursor, 0, "离开时查看旧批次，重新打开仍停在旧批次");
eq(restoredHistory.log[1].pills[0].frameId, 81, "结果按钮随帧号重映射");
eq(restoredHistory.log[1].flow, richLog[1].flow, "工具摘要恢复");
ok(restoredHistory.log[1].llmTag && restoredHistory.log[1].msgFb === "up", "AI 来源和反馈保留");
ok(restoredHistory.log[3].isError && restoredHistory.log[3].needsAgent, "错误和设置入口保留");
eq(restoredHistory.view, historyRows[1].conversation.view, "草稿、滚动和展开态保留");
eq(restoredHistory.stack[1].suppressed, ["include:tissue"], "被忽略的过滤条件保留");
eq(restoredHistory.stack[1].lenient, ["disease"], "宽容维度保留");
richLog[1].pills[0].count = 99;
eq(savedMessages[1].pills[0].count, 3, "保存数据与运行时数据隔离");
const longChat = Array.from({ length: 90 }, (_, i) => ({ k: i % 2 ? "sys" : "say", t: "消息" + i, i: "m" + i, r: null }));
eq(core.cbHistoryRestore([{ chatOnly: true, chat: longChat, conversation: { version: 2, frameIds: [] } }]).log.length,
    90, "超过旧40条窗口的对话不丢消息");
const missingResult = core.cbHistoryRestore([{ ...historyRows[1], snap: null, snap_evicted: true }]);
eq(missingResult.stack.length, 0, "缺失快照不伪造结果");
eq(missingResult.log.map((e) => e.text), savedMessages.map((e) => e.t), "结果被清退仍保留原回复");
const legacyHistory = core.cbHistoryRestore([
    { query: "旧句", snap: { results: [1] }, chat: [{ k: "say", t: "旧句", i: "m1" }] },
    { query: "新句", snap: { results: [2] }, chat: [{ k: "sys", t: "旧回复", i: "m2" }, { k: "say", t: "新句", i: "m3" }] },
]);
eq(legacyHistory.log.map((e) => e.text), ["旧句", "旧回复", "新句"], "旧版累计窗口滚动仍找回新增消息");
eq(core.cbHistoryRestore([{ query: "只剩查询" }]).log[0].text, "只剩查询", "最早格式保留原查询");
const prefixMissingResult = core.cbHistoryRestore([
    { ...historyRows[0], snap: null, snap_evicted: true },
], 0, { asBranch: true });
eq(prefixMissingResult.stack, [], "首轮快照缺失时分支不借用后续结果");
eq(prefixMissingResult.log.map((m) => m.text), richLog.slice(0, 2).map((m) => m.text), "快照缺失的分支仍保留所选轮次的消息");
eq(prefixMissingResult.log.map((m) => m.frameId), [null, null], "缺失结果的消息不伪造帧归属");
eq(core.cbHistoryRestore([{ query: "旧句", snap: { results: [1] }, chat: [{ k: "say", t: "旧句" }] }],
    0, { asBranch: true }).log[0].text, "旧句", "旧格式仍能按前缀建立分支");

/* 使用真正 core.js 的存储实现，验证晚到的回复、账户隔离与配额降级。 */
const coreSource = readFileSync(fileURLToPath(new URL("../../web/static/js/core/core.js", import.meta.url)), "utf8")
    .replace(/^import .*;\r?$/gm, "");
globalThis.window = {};
const historyStorage = new Map();
let storageLimit = Infinity;
globalThis.localStorage = {
    getItem: (key) => historyStorage.get(key) ?? null,
    setItem: (key, value) => { if (value.length > storageLimit) throw new Error("quota"); historyStorage.set(key, value); },
    removeItem: (key) => historyStorage.delete(key),
};
const historyCore = await import("data:text/javascript;base64," + Buffer.from(coreSource).toString("base64"));
let liveLog = [{ kind: "say", text: "提问", id: "m1", frameId: 1 }];
let liveState = { owner: "history_owner", frames: [{ id: 1, query: "提问", resp: { results: [1] }, facets: [], suppressed: [], lenient: [], queryHits: [] }], cursorId: 1,
    view: { chatDraft: "未发送的草稿", chatScroll: 17 } };
historyCore.setCurrentUser({ id: "history_owner" });
historyCore.setHistHooks({ convId: () => "saved-conversation", logForHistory: () => core.cbHistoryMessages(liveLog), stateForHistory: () => liveState });
historyCore.pushHist("提问", liveState.frames[0].resp, [], [], [], [], false);
liveLog.push({ kind: "sys", text: "实际系统回复", id: "m2", frameId: 1 });
historyCore.syncHistConversation();
eq(historyCore.getHist()[0].chat[1].t, "实际系统回复", "初次结果写入后到达的系统回复同步保存");
liveLog[1].text = "AI 最终原位改写"; liveLog[1].llmTag = true;
historyCore.syncHistConversation();
eq(historyCore.getHist()[0].chat[1].t, "AI 最终原位改写", "异步改写后的最终显示内容同步保存");
eq(historyCore.getHist()[0].conversation.view.chatDraft, "未发送的草稿", "离开前的草稿写入同一会话");
historyCore.setCurrentUser({ id: "other_owner" });
eq(historyCore.syncHistConversation(), false, "旧会话异步保存拒绝写入新账户");
eq(historyCore.getHist(), [], "其他账户看不到本会话");
historyCore.setCurrentUser({ id: "history_owner" });
liveState.frames[0].resp = { results: ["X".repeat(10000)] };
storageLimit = 3000;
historyCore.syncHistConversation();
ok(historyCore.getHist()[0].snap_evicted, "空间不足按结果载荷降级");
eq(historyCore.getHist()[0].chat[1].t, "AI 最终原位改写", "配额降级不丢系统回复");
ok(historyCore.getHist()[0].conversation.version === 2, "配额降级保留恢复状态");
historyCore.setCurrentUser({ id: "full_storage_owner" });
liveState = { frames: [], cursorId: null, view: {} }; storageLimit = 0;
eq(historyCore.syncHistConversation(), false, "连纯对话都存不下时终止重试，避免递归溢出");
storageLimit = Infinity;
historyCore.setCurrentUser({ id: "fork_owner" });
let forkConvId = "parent";
liveLog = richLog.slice(0, 4);
liveState = { owner: "fork_owner", frames: [
    { id: 11, query: "第一句", resp: { results: [{ uid: "first" }] }, facets: [], suppressed: [], lenient: [], queryHits: [] },
    { id: 12, query: "第二句", resp: { results: [{ uid: "second" }] }, facets: [], suppressed: [], lenient: [], queryHits: [] },
], cursorId: 12, view: {} };
historyCore.setHistHooks({ convId: () => forkConvId, logForHistory: () => core.cbHistoryMessages(liveLog), stateForHistory: () => liveState });
eq(historyCore.syncHistConversation(), false, "普通保存不复活不存在的会话");
ok(historyCore.syncHistConversation({ createFrames: true }), "显式分支立即独立保存继承的结果帧");
const parentRows = historyCore.getHist();
forkConvId = "fork";
liveLog.push({ kind: "sys", text: "分支提示", id: "m5", frameId: 12 });
ok(historyCore.syncHistConversation({ createFrames: true }), "新分支保留继承的帧与新消息");
const forkRows = historyCore.getHist().filter((r) => r.convId === "fork").reverse();
eq(historyCore.getHist().filter((r) => r.convId === "parent"), parentRows, "分支保存不改写原会话");
const reopenedFork = core.cbHistoryRestore(forkRows);
eq(reopenedFork.stack.map((f) => f.resp.results[0].uid), ["first", "second"], "独立回看仍有继承的每轮结果");
eq(reopenedFork.log.map((m) => m.text), liveLog.map((m) => m.text), "独立回看仍保留分支全部消息");
eq(core.cbHistoryRestore(forkRows.slice(0, 1), 0, { asBranch: true }).log.map((m) => m.text),
    richLog.slice(0, 2).map((m) => m.text), "对分支再次从首轮分支仍只保留首轮历史");
historyCore.setCurrentUser({ id: "another_fork_owner" });
eq(historyCore.syncHistConversation({ createFrames: true }), false, "显式分支保存同样遵守账户所属校验");
eq(historyCore.getHist(), [], "跨账户分支不写入继承内容");
const shared = core.cbSharedRestore({ current_state: { active_frame_id: "f_2" },
    messages: [{ seq: 1, role: "user", text: "第一轮", run_id: "r1" }, { seq: 2, role: "assistant", text: "结果", run_id: "r1" },
        { seq: 3, role: "assistant", text: "MCP 新轮", run_id: "r2" }],
    frames: [{ id: "f_1", seq: 1, run_id: "r1", payload: { results: [{ uid: "a" }] }, state: { query: "首句", search_params: { date_from: "2020-03-12", facet_filters: [{ dim: "species", value: "human" }] } } },
        { id: "f_2", seq: 2, run_id: "r2", payload: { results: [] }, state: { query: "零命中" } }] });
eq(shared.stack.map((f) => f.serverId), ["f_1", "f_2"], "共享帧保留服务端不透明编号和零命中帧");
eq(shared.cursor, 1, "共享现场由 active_frame_id 决定");
eq(shared.log.map((m) => m.text), ["第一轮", "结果", "MCP 新轮"], "共享消息不沿用本机累计缓存");
eq(shared.log[2].frameId, 2, "MCP 新轮按 run_id 关联结果");
eq(shared.stack[0].state.search_params.date_from, "2020-03-12", "共享帧保留精确日期而非折为年份");
let missingFrameRejected = false;
try { core.cbSharedRestore({ current_state: { has_results: true }, frames: [], messages: [] }); }
catch (_error) { missingFrameRejected = true; }
ok(missingFrameRejected, "共享缺帧是协议错误，不会用本机缓存假装已恢复");
const importData = core.cbLegacyImport(forkRows);
eq(importData.frames.length, 2, "显式导入复用既有历史恢复机制保留每轮快照");
eq(importData.messages.map((m) => m.role), liveLog.map((m) => m.kind === "sys" ? "assistant" : "user"), "显式导入消息身份准确");
eq(importData.frames.map((f) => f.message_boundary), [2, 5], "显式导入保存每个结果的消息边界");
const imported = core.cbSharedRestore({ current_state: importData.current_state,
    frames: importData.frames.map((f, i) => ({ ...f, id: "f_" + (i + 1), seq: i + 1, run_id: "" })),
    messages: importData.messages.map((m, i) => ({ ...m, seq: i + 1, run_id: "original-task" })) });
eq(imported.log.map((m) => m.frameId), [1, 1, 2, 2, 2], "分支或导入没有原 run_id 时以真实消息边界关联各轮结果");
historyCore.setCurrentUser({ id: "shared_owner" });
historyCore.setHistHooks({ convId: () => "server_conv", logForHistory: () => [{ k: "say", t: "共享" }], stateForHistory: () => liveState, shared: () => true });
historyCore.pushHistChatOnly(); historyCore.pushHist("共享", { results: [] }, [], [], [], []); historyCore.syncHistConversation({ createFrames: true });
eq(historyCore.getHist(), [], "共享会话不会混写或覆盖本机旧历史");
const messageIn = (id) => core.cbSharedRestore({ id, messages: [{ seq: 1, role: "assistant", text: "reply" }], frames: [], current_state: {} }).log[0].id;
ok(messageIn("conversation-A") !== messageIn("conversation-B"), "共享消息id必须带会话身份，避免评论草稿串会话");
eq(messageIn("conversation-A"), messageIn("conversation-A"), "同一会话刷新保留稳定消息id");
// 命中关键词唯一真源：只取 include 生效值、去重、顿号连接、超长截断；两条 pill 路径共用。
eq(core.cbHitKeywords([{ polarity: "include", values: ["FASTQ", "乳腺癌"] },
    { polarity: "exclude", values: ["石蜡"] }, { polarity: "include", values: ["乳腺癌", "Human"] }]),
    "FASTQ、乳腺癌、Human", "pill 关键词只取正向硬条件且去重");
eq(core.cbHitKeywords([]), "", "空约束回退空串交给调用方回退原 query");
eq(core.cbHitKeywords([{ polarity: "prefer", values: ["新"] }]), "", "软偏好不算命中关键词");
ok(core.cbHitKeywords([{ polarity: "include", values: ["超长词".repeat(20)] }]).endsWith("…"), "超长关键词整体截断");
const kwShared = core.cbSharedRestore({ current_state: {},
    messages: [{ seq: 1, role: "assistant", text: "结果", run_id: "r1" }],
    frames: [{ id: "f_1", seq: 1, run_id: "r1", state: { query: "整句查询" },
        payload: { results: [{ uid: "a" }], query_constraints: [{ polarity: "include", values: ["Human", "FASTQ"] }] } }] });
eq(kwShared.log[0].pills[0].label, "Human、FASTQ", "共享恢复 pill 显示命中关键词而非原始 query");
const kwFallback = core.cbSharedRestore({ current_state: {},
    messages: [{ seq: 1, role: "assistant", text: "结果", run_id: "r1" }],
    frames: [{ id: "f_1", seq: 1, run_id: "r1", state: { query: "整句查询" }, payload: { results: [{ uid: "a" }] } }] });
eq(kwFallback.log[0].pills[0].label, "整句查询", "无约束信息时 pill 回退整句 query");
console.log("BOARD_CORE_SPEC_OK  断言 " + checks + " 条");
