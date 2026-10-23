// web/static/js/core/replay_core.js
"use strict";

/* 复盘纯逻辑核（2026-09-28 复盘批）：零 DOM 直写、零网络——复盘 key 的生成与持久化、
   对话鉴权取值、后台整理跳过原因的人话映射、LLM 覆盖字段（注入式）；供复盘窗口
   （panel/replay.js）与图谱（panel/replay_graph.js）共用同一份口径（唯一真源，不各拼一份）。

   mdLite 移植自内部工具「复盘器」web/js/md.js（2026-09-28）：先整体转义，再选择性还原
   ```代码块``` / `行内` / **粗体** / [链接](url) / #..#### 标题 / `- ` 列表 / 换行；
   **链接只允许 http/https**（javascript:、data: 等原样留文字，不成锚点）。
   与源实现的一处有意差异：转义用 #core.escapeHtml（额外转义引号），补上源版 href 属性
   可被引号突破的洞——语义与源一致（转义只加不减），安全性只升不降。

   import 环纪律：本模块只 import #core。LLM 覆盖字段由 board 经 setReplayLlmFn 注入
   （board 持有 #shell 的 getConfig；本模块不 import #shell——否则 board→replay_core→shell
   会把 replay_core 拉进既有 SCC，import 环门只许缩不许扩）。 */
import { LS, escapeHtml, nsKey } from "#core";

/* 复盘 key 的 localStorage 基键（LS.replay 由本批在 core.js 的 LS 表登记；
   兜底常量只为「表还没登记」时不写坏别人的键——两处同名即逐位一致）。 */
const LS_REPLAY = LS.replay || "biodata_replay_v1";
const _HEX32 = /^[0-9a-f]{32}$/;
let _memKey = "";

/* ---------- markdown-lite（唯一渲染锚点：窗口 sidechat、图谱详情、标注气泡全走它） ---------- */
export function mdLite(src) {
    const blocks = String(src == null ? "" : src).split("```");
    let html = "";
    blocks.forEach((block, i) => {
        if (i % 2 === 1) {
            // 奇数段 = 围栏内代码：只转义，不再解析任何标记（源实现同判）
            html += '<pre class="code">' + escapeHtml(block.replace(/^\w*\n/, "")) + "</pre>";
            return;
        }
        let t = escapeHtml(block);
        t = t.replace(/`([^`\n]+)`/g, "<code>$1</code>");
        t = t.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
        t = t.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
        t = t.replace(/^(#{1,4})\s*(.+)$/gm, (m, h, s) => '<div class="h' + h.length + '">' + s + "</div>");
        t = t.replace(/^\s*[-*]\s+(.+)$/gm, '<div class="li">· $1</div>');
        t = t.replace(/\n{2,}/g, "<br><br>").replace(/\n/g, "<br>");
        html += t;
    });
    return html;
}

/* ---------- 复盘 key ---------- */
/* 32 位小写 hex，按账户命名空间持久化。服务端首写落 key、之后每次读写必须匹配——
   anonymous 分区下 conv_id 熵不高，key 是防串号的第二道闸（replay_store.ensure_key）。
   存储不可用（隐私模式/配额满）时退回模块内存键：本次会话内恒定，刷新即换——
   如实如此，不假装持久（写不进 localStorage 是环境事实，不是错误）。 */
export function replayKey() {
    try {
        const stored = String(localStorage.getItem(nsKey(LS_REPLAY)) || "").trim();
        if (_HEX32.test(stored)) return stored;
        _memKey = _randomKey();
        localStorage.setItem(nsKey(LS_REPLAY), _memKey);
        return _memKey;
    } catch (_e) {
        if (!_memKey) _memKey = _randomKey();
        return _memKey;
    }
}

function _randomKey() {
    const buf = new Uint8Array(16);
    if (typeof crypto !== "undefined" && crypto.getRandomValues) crypto.getRandomValues(buf);
    else for (let i = 0; i < buf.length; i++) buf[i] = Math.floor(Math.random() * 256);
    return Array.from(buf).map((b) => b.toString(16).padStart(2, "0")).join("");
}

/* ---------- 对话鉴权（conv id 的真源在 board） ----------
   board.js 在 initCondBoard 时经 setReplayConvIdFn 注入 cbConvId（与 core.setHistHooks
   同一条注册反转纪律）：本模块**不** import #board，避免多一条反向边。 */
let _convIdFn = null, _sharedFn = null;
export function setReplayConvIdFn(fn) { _convIdFn = typeof fn === "function" ? fn : null; }
export function setReplaySharedFn(fn) { _sharedFn = typeof fn === "function" ? fn : null; }

export function replayConvAuth() {
    const shared = _sharedFn ? _sharedFn() : null;
    if (shared) return { conv_id: String(shared.id || ""), shared: true };
    let conv = "";
    try { conv = _convIdFn ? String(_convIdFn() || "") : ""; } catch (_e) { conv = ""; }
    return conv ? { conv_id: conv, key: replayKey() } : { conv_id: "", key: "" };
}

/* 共享对话由所属身份访问；旧对话保留 replay_key。空对话不请求复盘。 */
export function replayConvFields() {
    const a = replayConvAuth();
    return a.shared ? { conv_id: a.conv_id, shared: true } : { conv_id: a.conv_id, replay_key: a.key };
}

/* ---------- 跳过原因的人话 ----------
   口径来自后端 replay_service._draw_worthy / replay_store.skip_reason。未知 code **原样返回**：
   不替后端编词（编了就是撒谎），也不吞诊断信息。 */
const _SKIP_ZH = {
    mock: "未配置大模型，自动整理未启用",
    no_key: "未配置 API Key，自动整理未启用",
    trial: "试用额度不用于后台自动整理——点「重新整理」手动生成",
    no_config: "LLM 配置缺失",
    parse_failed: "这次没整理出来，可重试",
};

export function skipReasonZh(code) {
    const c = String(code == null ? "" : code);
    return _SKIP_ZH[c] || c;
}

/* ---------- LLM 覆盖字段（board 注入；字段名与 search.js 发 /api/recommend 的请求体逐位对齐） ----------
   board 的注入函数返回 getConfig() 的六个相关字段；本函数只做形状归一与兜底——
   owned 未提供的字段不补成 mock，留给服务端配置；旧页面保持原有兜底。 */
const _LLM_FALLBACK = { provider: "mock", use_llm: false, mock_llm: true, api_key: "", base_url: "", model: "" };
let _llmFn = null;
export function setReplayLlmFn(fn) { _llmFn = typeof fn === "function" ? fn : null; }

export function llmOverrides() {
    const owned = _sharedFn ? !!_sharedFn() : false;
    if (!_llmFn) return owned ? {} : Object.assign({}, _LLM_FALLBACK);
    try {
        const o = _llmFn() || {};
        if (owned) {
            const supplied = {};
            for (const key of ["provider", "use_llm", "mock_llm", "api_key", "base_url", "model"]) {
                if (Object.prototype.hasOwnProperty.call(o, key) && o[key] !== undefined) supplied[key] = o[key];
            }
            return supplied;
        }
        return {
            provider: o.provider || "mock",
            use_llm: !!o.use_llm,
            mock_llm: !!o.mock_llm,
            api_key: String(o.api_key || ""),
            base_url: String(o.base_url || ""),
            model: String(o.model || ""),
        };
    } catch (_e) {
        return owned ? {} : Object.assign({}, _LLM_FALLBACK);
    }
}
