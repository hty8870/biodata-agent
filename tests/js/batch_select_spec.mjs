"use strict";

/* ============================================================================
 * batch_select_spec.mjs —— 结果覆盖策略纯逻辑核「真行为」规格（node 跑）
 * ----------------------------------------------------------------------------
 * 由 tests/test_act_frontend.py 经 node <this> 驱动（若有）或直接 node 跑。断言失败 → 非零退出。
 * 存在意义：web_smoke 只静态查字符串、node --check 只验语法，两门都测不出「初步结果被弱批顶掉」
 * 的覆盖语义红线——「严格更高级才自动换屏」「同 scope 去重不追加」「换词批作备选」「未知 trace
 * 不可比较不自动覆盖」这些设计 §10.3 的红线，错一处就是覆盖策略事故。
 *
 * selectDisplayBatch 是两 route（search a 档 / route=tool 档）共用的纯函数：两档落地逻辑一致，
 * 故本规格直接测纯函数真行为，两 route 各自只补一条「确实调用了它」的结构钉（test_act_frontend.py）。
 * ========================================================================== */

import * as B from "../../web/static/js/core/batch_select.js";

let failures = 0;
function check(name, cond, detail) {
    if (cond) { console.log(`  ok   ${name}`); }
    else { failures++; console.log(`  FAIL ${name}${detail ? "  —— " + detail : ""}`); }
}
function end(name) {
    console.log(`\n${name}: ${failures === 0 ? "PASS" : failures + " FAIL"}`);
    if (failures) process.exit(1);
}

/* ---------- 造批工具（scope=范围指纹；levels=排序层；uids=记录键向量；noTrace=无 trace） ---------- */
function steps(levels) {
    const out = [{ id: "rule_rank", status: "used" }];
    if (levels >= 2) out.push({ id: "local_semantic", status: "used" });
    if (levels >= 3) out.push({ id: "llm_rerank", status: "used" });
    return out;
}
function mkBatch(id, kind, query, { scope, levels = 1, uids, noTrace = false, emptyUid = false } = {}) {
    const results = uids.map((u) => ({ dataset_uid: u, dataset_name: "DS" + u }));
    if (emptyUid) results.push({ dataset_uid: "", dataset_name: "DS?" });
    const payload = {
        ok: true, result_total: results.length, query, results,
        search_trace: noTrace ? null : { steps: steps(levels) },
    };
    return {
        batch_id: id, kind, label: query.slice(0, 20),
        query_effective: query, query_raw: query,
        scope_fingerprint: scope, payload,
    };
}
/* 当前屏 = 裸 payload（无 result_batches）：模拟 preliminary 先落地（applyRecommendResult 不挂批组） */
function bareView(batch) {
    return Object.assign({}, batch.payload, { ok: true });
}

const S = "scope-aaa111";   // 同一检索范围
const T = "scope-bbb222";   // 换过的检索范围

/* ================= 1. rankingLevel（设计 §10.3：polish 不计、未知=null） ================= */
function rankingSuite() {
    check("无 trace → null（不可比较）", B.rankingLevel(null) === null);
    check("trace 无 steps → null", B.rankingLevel({}) === null);
    check("trace steps 空数组 → null（不得默认规则层 1）", B.rankingLevel({ steps: [] }) === null);
    check("规则 only → 1", B.rankingLevel({ steps: [{ id: "rule_rank", status: "used" }] }) === 1);
    check("规则+local_semantic → 2", B.rankingLevel({
        steps: [{ id: "rule_rank", status: "used" }, { id: "local_semantic", status: "used" }] }) === 2);
    check("规则+local+llm → 3", B.rankingLevel({
        steps: [{ id: "rule_rank", status: "used" }, { id: "local_semantic", status: "used" },
                { id: "llm_rerank", status: "used" }] }) === 3);
    check("polish 不计（仍 2）", B.rankingLevel({
        steps: [{ id: "rule_rank", status: "used" }, { id: "local_semantic", status: "used" },
                { id: "llm_polish", status: "used" }] }) === 2);
    check("fallback 不计入（只算 used）", B.rankingLevel({
        steps: [{ id: "rule_rank", status: "used" }, { id: "local_semantic", status: "fallback" }] }) === 1);
    check("llm_rerank 单独存在（无 local）仍 3", B.rankingLevel({
        steps: [{ id: "rule_rank", status: "used" }, { id: "llm_rerank", status: "used" }] }) === 3);
}

/* ================= 2. 记录键向量 / scope / 同批 ================= */
function identitySuite() {
    const a = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"] });
    const b = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"] });
    const c = mkBatch("b3", "rank", "lung", { scope: S, uids: ["B", "A"] });   // 同 uid 不同序
    const d = mkBatch("b4", "rank", "lung", { scope: T, uids: ["A", "B"] });   // 换 scope
    const e = mkBatch("b5", "rank", "lung", { scope: S, uids: ["A", ""] });    // 空 uid
    const f = mkBatch("b6", "rank", "lung", { scope: S, uids: ["A", "B"], noTrace: true });

    check("同 scope + 同序同 uid → sameBatch", B.sameBatch(a, b) === true);
    check("同 scope + 同 uid 不同序 → 不同批", B.sameBatch(a, c) === false, JSON.stringify(B.recordKeyVector(a).keys) + " vs " + JSON.stringify(B.recordKeyVector(c).keys));
    check("不同 scope → 不同批", B.sameBatch(a, d) === false);
    check("空 uid → 记录键不稳（保守不同批）", B.sameBatch(a, e) === false);
    check("空 uid 批自身不稳", B.recordKeyVector(e).stable === false);
    check("无 trace 不影响判同（只看 scope+记录键）", B.sameBatch(a, f) === true);
    check("裸视图（无指纹）判同走保守", B.sameBatch(a, Object.assign({}, a.payload)) === false);
}

/* ================= 3. mergeBatches：同 scope 只留更高层 ================= */
function mergeSuite() {
    const prelimL1 = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
    const loopL2 = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
    const merged = B.mergeBatches([], [prelimL1, loopL2]);
    check("同 scope 合并 → 只留更高层（b2）", merged.length === 1 && merged[0].batch_id === "b2", JSON.stringify(merged.map((m) => m.batch_id)));
    const mixed = B.mergeBatches([], [prelimL1, mkBatch("b3", "rerank", "lung2", { scope: T, uids: ["A", "C"], levels: 1 })]);
    check("不同 scope 合并 → 两者都留", mixed.length === 2, JSON.stringify(mixed.map((m) => m.batch_id)));
}

/* ================= 4. selectDisplayBatch 行为矩阵（设计 §10.3 逐字） ================= */
function selectSuite() {
    const trace = (b) => B.traceOf(b);

    /* S1 同批（同 scope 同记录同层）→ 去重（不新增 pill、不换屏） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const loop = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("S1 同批 → dedupe（不换屏、不新增 pill）", d.mode === "dedupe" && d.view === null && d.activeBatchId === "b1");
        check("S1 回执如实（不得说已更新）", d.sysText && d.sysText.indexOf("更匹配") < 0);
        check("S1 摘徽标", d.stripPrelimBadge === true);
    }
    /* S2 弱批（同 scope 同记录、级别更低）→ 去重，保住更优批 */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const loop = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("S2 弱批 → dedupe（保住更优初屏）", d.mode === "dedupe" && d.view === null && d.activeBatchId === "b1");
    }
    /* S3 换词批（不同 scope、级别更低）→ display 整屏覆盖（条件变更：不比较级别） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const loop = mkBatch("b2", "rank", "lung cancer", { scope: T, uids: ["A", "C"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("S3 换词批（scope 异）→ display 整屏覆盖（不再作备选）", d.mode === "display" && d.view !== null);
        check("S3 view 指向候选批（b2）", d.view.active_batch === "b2");
        check("S3 merged 含两批（旧屏+候选）", d.mergedBatches.length === 2);
        check("S3 回执空串（诚实句由调用方/后端披露句给）", d.sysText === "");
    }
    /* S4 未知 trace = 不可比较（不得默认规则层 1 自动覆盖） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const loopSame = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"], noTrace: true });
        const ds = B.selectDisplayBatch({ result_batches: [prelim, loopSame], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("S4a 未知 trace+同批 → 去重（不自动覆盖）", ds.mode === "dedupe" && ds.view === null);
        const loopDiff = mkBatch("b3", "rank", "lung2", { scope: T, uids: ["A", "C"], noTrace: true });
        const dd = B.selectDisplayBatch({ result_batches: [prelim, loopDiff], active_batch: "b3", _prelimShown: true }, bareView(prelim));
        check("S4b 未知 trace+异 scope → display（条件变更不比较级别）", dd.mode === "display" && dd.view !== null);
    }
    /* S5 preliminary 仅规则（镜像：候选即初屏）→ 去重（保留初屏、摘徽标） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim], active_batch: "b1", _prelimShown: true }, null);
        check("S5 preliminary 仅规则 → 去重（保留初屏不重渲）", d.mode === "dedupe" && d.view === null && d.stripPrelimBadge === true);
    }
    /* S6 同 uid 不同序 → 不同批（记录键向量 + 序）→ alternate 保守 */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["B", "A"], levels: 2 });
        const loop = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("S6 同 uid 不同序 → 不同批 → alternate", d.mode === "alternate" && d.view === null);
    }
    /* S7 空 uid → 缺稳定键 → 保守不去重、不自动覆盖（即使候选层更高） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const loop = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A"], levels: 3, emptyUid: true });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("S7 空 uid → 不自动覆盖（作备选）", d.mode === "alternate" && d.view === null);
    }
    /* S8 结构化条件丢失（指纹不同=换了 scope）→ display 覆盖（条件变更；回执由披露句说明） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const loop = mkBatch("b2", "rank", "lung", { scope: T, uids: ["A", "B"], levels: 1 });  // 条件丢 → 指纹变
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("S8 条件丢失（scope 异）→ display", d.mode === "display");
    }
    /* S9 严格升级（同 scope 同记录、层更高）→ display 自动换屏 */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const loop = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 3 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("S9 严格升级 → display（自动换屏）", d.mode === "display" && d.view !== null);
        check("S9 view.active_batch = 候选批", d.view.active_batch === "b2");
        check("S9 merged 一层（更高层替换）", d.mergedBatches.length === 1 && d.mergedBatches[0].batch_id === "b2");
    }
    /* S10 首次落屏（无参考批）→ display */
    {
        const loop = mkBatch("b1", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const d = B.selectDisplayBatch({ result_batches: [loop], active_batch: "b1" }, null);
        check("S10 首次落屏 → display", d.mode === "display" && d.view !== null);
        check("S10 query = 生效检索句", d.query === "lung");
    }
    /* S11 跨轮回看时（currentView 已有批组）：换词批（scope 异）→ display 覆盖（条件变更优先） */
    {
        const oldStrong = mkBatch("old1", "search_rerun", "lung", { scope: T, uids: ["A", "B"], levels: 3 });
        const curView = Object.assign({}, oldStrong.payload, { result_batches: [oldStrong], active_batch: "old1" });
        const newWeak = mkBatch("new1", "rerank", "heart", { scope: T + "-x", uids: ["A", "C"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [newWeak], active_batch: "new1" }, curView);
        check("S11 跨轮回看换词 → display（条件变更覆盖，不再保旧批）", d.mode === "display" && d.view !== null);
        check("S11 display 指向候选批（new1）", d.view.active_batch === "new1");
        check("S11 merged 只含本轮候选批（不并入上一轮屏态批，防跨轮泄漏）", d.mergedBatches.length === 1 && d.mergedBatches[0].batch_id === "new1");
    }
    /* ku3-w6：跨轮泄漏——上一轮屏态（currentView 的 result_batches）不得并入本轮 display 的 pill 组 */
    {
        const oldTurn = mkBatch("t1", "rank", "breast", { scope: "S1", uids: ["A", "B"], levels: 1 });
        const oldTurn2 = mkBatch("t2", "rank", "breast-fastq", { scope: "S2", uids: ["A"], levels: 1 });
        const curView = Object.assign({}, oldTurn2.payload, { result_batches: [oldTurn, oldTurn2], active_batch: "t2" });
        const newTurn = mkBatch("t3", "rerank", "pig", { scope: "S3", uids: [], levels: 1 });   // 换词后 0 命中（胜者）
        const d = B.selectDisplayBatch({ result_batches: [newTurn], active_batch: "t3" }, curView);
        check("ku3-w6 跨轮 0 命中 display → mode=display（条件变更覆盖）", d.mode === "display" && d.view !== null);
        check("ku3-w6 跨轮 display 只含本轮新批（上一轮屏态批不并入）", d.mergedBatches.length === 1 && d.mergedBatches[0].batch_id === "t3", JSON.stringify(d.mergedBatches.map((m) => m.batch_id)));
    }
}

/* ================= 5. activeBatchId 鲁棒性（ku2-w1：批缺 batch_id 不再塌成 ""） =================
   缺陷 X：参考批/候选批缺 batch_id（preliminary 批常不带）时，旧实现 `主动batchId=裸 ref.batch_id`
   塌成 "" → 渲染高亮错位 + switchBatch 空转（pill 点不动）。新实现按归一 id（batch_id || "b(序号)"）
   解析，保证非空且指向「真正在屏/被采纳的那批」。 */
function activeBatchIdSuite() {
    /* A1 换词批（不同 scope）→ display：参考批缺 batch_id 不塌陷，activeBatchId = 候选批 b2 */
    {
        const prelim = mkBatch("", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const loop = mkBatch("b2", "rank", "lung cancer", { scope: T, uids: ["A", "C"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("A1 缺 id 参考批+换词 → display（整屏覆盖）", d.mode === "display" && d.view !== null);
        check("A1 activeBatchId 指向候选批（b2，非空）", d.activeBatchId === "b2", JSON.stringify(d.activeBatchId));
        check("A1 merged 两批（初屏+候选）", d.mergedBatches.length === 2);
    }
    /* A2 换词批 display：候选批缺 batch_id、参考批有 id → activeBatchId = 合成 b2（非空不塌陷） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const loop = mkBatch("", "rank", "lung cancer", { scope: T, uids: ["A", "C"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("A2 候选缺 id+换词 → display，activeBatchId 合成非空（b2）", d.mode === "display" && d.activeBatchId === "b2", JSON.stringify(d.activeBatchId));
    }
    /* A3 严格升级 display：候选缺 batch_id 但可被 merged 引用 → activeBatchId 非空 */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const loop = mkBatch("", "rerank", "lung", { scope: T, uids: ["A", "B", "C"], levels: 3 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("A3 升级候选缺 id → display，activeBatchId 非空", d.mode === "display" && !!d.activeBatchId, JSON.stringify(d.activeBatchId));
    }
    /* A4 跨轮回看 display：参考批缺 batch_id → activeBatchId 非空（不回落空串） */
    {
        const oldStrong = mkBatch("", "search_rerun", "lung", { scope: T, uids: ["A", "B"], levels: 3 });
        const curView = Object.assign({}, oldStrong.payload, { result_batches: [oldStrong], active_batch: "" });
        const newWeak = mkBatch("new1", "rerank", "heart", { scope: T + "-x", uids: ["A", "C"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [newWeak], active_batch: "new1" }, curView);
        check("A4 跨轮回看换词（缺 id）→ display，activeBatchId 非空", d.mode === "display" && !!d.activeBatchId, JSON.stringify(d.activeBatchId));
    }
}

/* ============== 6. ku3-w4 条件变更 0 命中必换屏（rerun-gate 前端侧，用户投诉修复） ==============
   投诉「换成猪的」：换词批 0 命中却被 alternate 档拦下、结果区保持不变 + 旧话术气泡。
   修正后：条件变更（scope 不同）→ display 整屏覆盖（含 0 命中），回执由调用方/后端披露句给；
   ALTERNATE_SYS_TEXT 只留给「同 scope 重检较弱批」且去黑话。 */
function rerunGateSuite() {
    /* R1 换词 0 命中批 → display 整屏覆盖（空结果集如实上屏；stable=false 不拦截） */
    {
        const prelim = mkBatch("b1", "preliminary", "人类乳腺癌 FASTQ", { scope: S, uids: ["A", "B", "C"], levels: 1 });
        const loop0 = mkBatch("b2", "rerank", "猪乳腺癌 FASTQ", { scope: T, uids: [], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, loop0], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("R1 换词 0 命中批 → display（不再 alternate/保持不变）", d.mode === "display" && d.view !== null);
        check("R1 view 空结果集如实上屏", d.view.results.length === 0);
        check("R1 view 指向候选批（b2）", d.view.active_batch === "b2");
        check("R1 sysText 空（诚实句由调用方/披露句给，不再假「保持不变」）", d.sysText === "");
        check("R1 不因 0 命中（stable=false）拦截", d.view !== null);
        const v = B.recordKeyVector(loop0);
        check("R1 0 命中 = 空键 + stable=false（事实，非不稳定）", v.keys.length === 0 && v.stable === false);
    }
    /* R2 参考缺指纹（legacy 裸视图）→ 保守 alternate（无法确证换词，不猜测覆盖） */
    {
        const legacy = Object.assign({}, mkBatch("b1", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 1 }).payload, { ok: true });
        const loop0 = mkBatch("b2", "rank", "猪", { scope: T, uids: [], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [loop0], active_batch: "b2" }, legacy);
        check("R2 参考缺指纹 → 保守 alternate（不猜测「换了条件」）", d.mode === "alternate" && d.view === null);
    }
    /* R3 同 scope 重检较弱（不同记录）→ alternate，回执去黑话、如实 */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 2 });
        const weak = mkBatch("b2", "rerank", "lung", { scope: S, uids: ["A", "C"], levels: 1 });   // 同 scope，更弱+不同结果
        const d = B.selectDisplayBatch({ result_batches: [prelim, weak], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("R3 同 scope 弱批 → alternate（保住当前更优结果）", d.mode === "alternate" && d.view === null);
        check("R3 回执 = 去黑话后的 ALTERNATE_SYS_TEXT", d.sysText === B.ALTERNATE_SYS_TEXT);
        check("R3 回执不再含误导性「按新条件」", d.sysText.indexOf("按新条件") < 0);
    }
    /* R4 同 scope 重检更强 → display 升级（排序层择优只保留在同 scope 内） */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const strong = mkBatch("b2", "rerank", "lung", { scope: S, uids: ["A", "B", "C"], levels: 3 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, strong], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("R4 同 scope 更强（3>1）→ display 升级", d.mode === "display" && d.view !== null);
    }
    /* R5 同 scope 同批 → dedupe（不追加、不换屏），回执 = DEDUPE_SYS_TEXT */
    {
        const prelim = mkBatch("b1", "preliminary", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const same2 = mkBatch("b2", "rank", "lung", { scope: S, uids: ["A", "B"], levels: 1 });
        const d = B.selectDisplayBatch({ result_batches: [prelim, same2], active_batch: "b2", _prelimShown: true }, bareView(prelim));
        check("R5 同 scope 同批 → dedupe", d.mode === "dedupe" && d.view === null && d.sysText === B.DEDUPE_SYS_TEXT);
    }
}

/* ================= 7. ku3-w5：零命中救回链退役 —— 纯逻辑核 =================
   救回选项不再以 sys 气泡出现；改由 board.js 选择条呈现。这里锁死三件事：
   ① 批是否零命中（payload.results 空数组）；② 从零命中批派生选择条选项
   （relaxation_options → degraded_search → query_constraints → 兜底换词）；
   ③ 最新结果判定（最后一个带 pill 的回执 entry 的活跃批）。 */ 
function zeroHitRescueSuite() {
    function zb(id, payload, extra) {
        return Object.assign({ batch_id: id, kind: "rank", query_effective: "猪脑数据",
            label: "猪脑数据", query_raw: "猪脑数据", scope_fingerprint: "EB", payload: payload,
            disclosure_zh: "「猪脑数据」没有匹配到数据集。" }, extra || {});
    }

    /* ① isZeroHitBatch */
    check("null → 非零命中", B.isZeroHitBatch(null) === false);
    check("非零命中批 → 非零命中", B.isZeroHitBatch(mkBatch("b1", "rank", "lung", { scope: S, uids: ["A"] })) === false);
    check("零命中批（results=[]）→ true", B.isZeroHitBatch(zb("eb", { ok: true, results: [], result_total: 0 })) === true);
    check("results 非数组 → 非零命中", B.isZeroHitBatch(zb("eb", { ok: true, result_total: 0 })) === false);

    /* ② deriveRescueOptions */
    check("空批 → 空数组", B.deriveRescueOptions(null).length === 0);
    {
        const b = zb("eb", { ok: true, results: [], result_total: 0,
            relaxation_options: [
                { key: "dim:disease", kind: "drop", label: "疾病", count: 12 },
                { key: "dim:species", kind: "only", label: "物种", count: 40 },
            ] });
        const o = B.deriveRescueOptions(b);
        check("relaxation_options 全量派生", o.length === 2, JSON.stringify(o));
        check("drop 项 summary = 去掉某人话", o[0].summary === "去掉「疾病」条件再搜", o[0].summary);
        check("drop 项 full 带预计条数", o[0].full.indexOf("12 条") >= 0, o[0].full);
        check("drop 项 submitText 即下一句", o[0].submitText === "去掉「疾病」条件再搜");
        check("only 项 summary = 只按某人话", o[1].summary === "只按「物种」搜，其它条件都放开", o[1].summary);
        check("only 项 kind=only", o[1].kind === "only");
    }
    {
        const b = zb("eb", { ok: true, results: [], result_total: 0,
            degraded_search: { ignored_terms: ["猪", "脑"], count: 7 } });
        const o = B.deriveRescueOptions(b);
        check("degraded_search → 忽略词选项", o.length === 1 && o[0].kind === "degrade", JSON.stringify(o));
        check("degrade summary 指向忽略词", o[0].summary === "忽略「猪」、「脑」这个说法再搜", o[0].summary);
    }
    {
        const b = zb("eb", { ok: true, results: [], result_total: 0,
            query_constraints: [
                { filter_id: "include:disease", polarity: "include", dim: "disease", label: "疾病", values: ["猪乳腺癌"] },
                { filter_id: "exclude:tissue", polarity: "exclude", dim: "tissue", label: "排除·组织", values: ["肝"] },
            ] });
        const o = B.deriveRescueOptions(b);
        check("query_constraints 兜底派生（include→去掉 / exclude→纳入）", o.length === 2, JSON.stringify(o));
        check("include 约束 → 去掉某人话", o[0].summary === "去掉「疾病=猪乳腺癌」条件再搜", o[0].summary);
        check("exclude 约束 → 纳入某人话", o[1].summary === "把「排除·组织=肝」也纳入再搜", o[1].summary);
    }
    {
        const b = zb("eb", { ok: true, results: [], result_total: 0 });
        const o = B.deriveRescueOptions(b);
        check("空载荷兜底换词（至少一个可用句）", o.length === 1 && o[0].kind === "reword", JSON.stringify(o));
        check("兜底换词 summary", o[0].summary === "换个说法再查一次");
    }

    /* ③ latestActiveBatchId */
    check("空 entries → 空串", B.latestActiveBatchId([]) === "");
    check("无 pill entry → 空串", B.latestActiveBatchId([{ kind: "sys", text: "x" }]) === "");
    {
        const entries = [
            { kind: "sys", text: "回执1", pills: [{ batchId: "a", active: true }] },
            { kind: "sys", text: "回执2", pills: [{ batchId: "b", active: true }, { batchId: "a", active: false }] },
        ];
        check("取最后一个回执 entry 的活跃批", B.latestActiveBatchId(entries) === "b");
    }
    {
        // 前面还有带 pill 的旧 entry，但最后一条无 pill → 回退到「最后一个带 pill 的 entry」
        const entries = [
            { kind: "sys", text: "回执1", pills: [{ batchId: "a", active: true }] },
            { kind: "say", text: "用户" },
        ];
        check("跳过无 pill entry，取上一个带 pill 的活跃批", B.latestActiveBatchId(entries) === "a");
    }
}

rankingSuite();
identitySuite();
mergeSuite();
selectSuite();
activeBatchIdSuite();
rerunGateSuite();
zeroHitRescueSuite();
end("batch_select_spec.mjs");
