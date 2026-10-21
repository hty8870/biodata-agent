"use strict";

/* ============================================================================
 * examples.js —— 端点分区坐标 + 操作样例兼容壳
 * ----------------------------------------------------------------------------
 * endpointCfg() / setEndpointProviderWire() 是**样例/规则/知识窗共用的端点分区坐标唯一读法**
 * （账户 scope × 学习目标 × base_url/model/provider），必须保留、绝不写第二份读法。
 *
 * 统一知识空间（2026-10-09）起，样例的候选/入库界面搬进知识窗（kind=example 经 knowledge
 * 道闸）；本模块的旧候选网络链退役。renderExampleCandidates 保留为兼容壳：经注册反转
 * 通知知识窗刷新样例视图（knowledge.js init 注册），不注册时无操作——不保留第二条入库通道。
 * ========================================================================== */

import { $, learningTargetScope, currentAccountScope } from "#core";

/* 端点坐标（分区键入参）直接读设置表单两个输入框——刻意不 import #shell 的 getConfig：
   memory.js 已 import 本模块，而 #shell 经 browse 间接回到 #memory，再引 shell 会成环
   （tests/test_frontend_import_graph.py：环只缩不涨）。 */
let _providerWire = null;
export function setEndpointProviderWire(fn) {
    _providerWire = (typeof fn === "function") ? fn : null;
}
export function endpointCfg() {
    const b = $("cfgBaseUrl"), m = $("cfgModel");
    let provider = "";
    if (_providerWire) { try { provider = String(_providerWire() || ""); } catch (_e) { provider = ""; } }
    return { project_id: learningTargetScope(), base_url: ((b && b.value) || "").trim(),
             model: ((m && m.value) || "").trim(), provider: provider };
}

/* 端点分区查询串与分区键（唯一读法，与服务端端点指纹同一组坐标）——
   rules.js 经 endpointCfg() 复用同一份坐标，本文件的查询串/分区键供知识窗与 vm 规格使用。 */
function _endpointQuery() {
    const cfg = endpointCfg();
    return "base_url=" + encodeURIComponent(cfg.base_url || "") + "&model=" + encodeURIComponent(cfg.model || "")
        + "&project_id=" + encodeURIComponent(cfg.project_id || "")
        + "&provider=" + encodeURIComponent(cfg.provider || "");
}

function _scopeKey() {
    const c = endpointCfg();
    return JSON.stringify([currentAccountScope(), c.project_id || "", c.base_url || "", c.model || "", c.provider || ""]);
}

/* 知识窗注册的样例视图刷新（注册反转，不 import #knowledge 防成环）。 */
let _compatRefresh = null;
export function setExamplesCompatRefresh(fn) { _compatRefresh = (typeof fn === "function") ? fn : null; }

/* 兼容壳：样例的候选与入库已并入知识窗（kind=example 经 knowledge 道闸）；
   旧调用点改为通知知识窗刷新样例视图。 */
export async function renderExampleCandidates(_preserveStatus) {
    if (_compatRefresh) return _compatRefresh();
    return null;
}
