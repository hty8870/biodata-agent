// web/static/js/panel/replay.js
"use strict";

/* 复盘 pane 属主（2026-09-28 复盘批建为浮窗；2026-10-10 rp1 迁入侧栏第三页签，浮窗退役）：
   页签角标 / 轮询 / sidechat / pill / 折叠 / 补建。pane 的显隐与「是否在场」由 board.js 的
   页签机制统一裁决（swApplyMode 链尾调 replaySetActive）——本文件不感知页签开关本身，
   只在激活时取数/观察轮询、离开时全停；图谱归 panel/replay_graph.js，取数全部在本文件
   （单一通道），图谱吃注入的数据。

   import 环纪律：board.js import 本模块（replayTurnDone/replayReset/replaySetActive）并注入
   cbConvId/本机对话 getter/LLM 覆盖（replay_core.setReplayConvIdFn / 本模块 setReplayLogGetter /
   replay_core.setReplayLlmFn）；本模块**不** import #board / #shell——否则 replay 会被拉进
   既有 SCC（import 环门只许缩不许扩）。

   假定 DOM（全部自守卫，缺一不炸）：#sideReplayPane(.sw-pane.rp-pane)、#replayChat（消息列表，
   进场空）、#replayComposer(contenteditable)+#replaySendBtn、#replayGraph、#replayGraphToggle、
   #replayEmpty（空态容器，本模块控显隐并在此增删补建按钮）、#replayNote（状态行）、
   #replayAddBtn、#replayRedrawBtn、#replayNavBadge（页签上的角标）、
   #replayGraphCol+#replaySplit（上下分区与可拖分割线；无分割线则分屏机制整段静默）。
   CSS 约定（样式归 app.css）：折叠类 .rp-graph-collapsed（藏图谱栏、对话占满 pane）、消息
   .msg/.msg-user/.msg-assistant/.msg-err、补建按钮 .rp-backfill/.rp-empty；其余用既有 .btn/.pill 族。
   XSS：一切文本先 escapeHtml（用户原话）或 mdLite（模型/后端回复，内部先整体转义）；
   状态行与徽标恒 textContent。a11y：焦点环交全局 :focus-visible；本文件不做任何动画
   （reduced-motion 天然满足），滚动恒用不带 smooth 的默认行为。 */
import { $, currentAccountScope, escapeHtml, toast, beginRequest, invalidateRequest } from "#core";
import { llmOverrides, mdLite, replayConvAuth, skipReasonZh } from "#replay_core";
import { initReplayGraph } from "#replay_graph";

/* 端点表：本模块的唯一真源（端点若有调整只改这一处；图谱经 ctx.api 适配器取，不硬编码路径）。 */
const RP = {
    status: "/api/replay/status",
    points: "/api/replay/points",
    seen: "/api/replay/seen",
    chat: "/api/replay/chat",
    snapshot: "/api/replay/snapshot",
    digest: "/api/replay/digest",
    backfill: "/api/replay/backfill",
};
const JSON_HEADERS = { "Content-Type": "application/json" };
const BADGE_MS = 4000;    // 角标轮询间隔：后台整理通常数秒起，4s 足够近实时又不闹
const BADGE_MAX = 15;     // 角标轮询次数上限（≈1 分钟）：到顶不再追，绝不长轮询空转
const VIEW_MS = 5000;     // 开窗观察轮询间隔（drawing 时 live 刷新）
const BACKFILL_LABEL = "用本机对话补建痕迹";

let _bound = false;       // initReplay 已接线（pane 在场）；false 时整模块静默（旧页面/测试页）
let _active = false;      // 复盘页签当前是显示模式（board.js swApplyMode 经 replaySetActive 通知）
let _graph = null;        // initReplayGraph 句柄
let _badgeTimer = null;   // 角标轮询 timer
let _badgeTries = 0;
let _viewTimer = null;    // 开窗观察轮询 timer
const _pointsRequests = {};
const _viewRequests = {};
const _badgeRequests = {};
let _pointsToken = null;
let _points = null;       // 最近一次 /api/replay/points 响应（图谱重画的唯一数据源；null=未知）
let _busy = false;        // sidechat 发送在途（发送键禁用依据）
let _chatStamp = -1;      // sidechat 已渲染条数（threads.session 长度；-1=未渲染过）
let _logGetter = null;    // board 注入：本机对话 turns getter（补建用）
let _fresh = new Set();   // 本次开窗要打标的 pid（=触发角标的 worth 列表；开窗冻结、关窗清除）
let _freshVer = null;     // _fresh 对应的 digest 版本；换版即重算（pid 是位置式编号，跨版本不可比）

/* board 注入本机对话 getter（补建通道的数据源）。约定返回 cbLogForHistory 形状（{k,t}），
   下方 _localTurns 按 k 折算；若直接给 [{role,text}] 也吃——注入侧怎么方便怎么给。 */
export function setReplayLogGetter(fn) { _logGetter = typeof fn === "function" ? fn : null; }

/* ---------- 小工具 ---------- */
function _q(auth) {
    return "?conv_id=" + encodeURIComponent(auth.conv_id) + (auth.shared ? "" : "&key=" + encodeURIComponent(auth.key));
}
function _ownedReplayURL(url, auth, write = false) {
    if (!auth.shared || !url.startsWith("/api/replay/")) return url;
    const path = url.split("?")[0].slice("/api/replay/".length);
    const action = path.startsWith("snapshot/") ? "annotate_snapshot" : path;
    return "/api/conversations/" + encodeURIComponent(auth.conv_id) + "/replay"
        + (write ? "/" + action : "?action=" + (action === "snapshots" ? "points" : action));
}

/* 迟回包身份闸：发起时的（账户 scope × 对话）与
   当前必须一致——否则 A 的草稿/pill/标注既不能落进 B 的输入框，也不能按 B 的凭据提交。 */
function _replayIdentity(auth) {
    return { scope: currentAccountScope(), conv: String((auth || {}).conv_id || "") };
}
function _identityCurrent(launch) {
    const now = _replayIdentity(replayConvAuth());
    return now.scope === launch.scope && now.conv === launch.conv;
}

/* GET JSON（图谱 ctx.api 适配器之一）。HTTP 非 2xx 抛错。 */
async function _get(url) {
    const auth = replayConvAuth(), launch = _replayIdentity(auth);
    const res = await fetch(_ownedReplayURL(url, auth), { cache: "no-store" });
    if (!res.ok) { const err = new Error("HTTP " + res.status); err.status = res.status; throw err; }
    const data = await res.json();
    if (!_identityCurrent(launch)) throw new Error("对话已切换");
    return data;
}

/* POST JSON。HTTP 非 2xx 抛错（带 status/data），业务失败（200 + ok:false）**不抛**——
   那是后端的如实回答，由调用方显示（与 board 对 /api/utterance 的处理同口径）。 */
async function _post(url, body) {
    const auth = replayConvAuth(), launch = _replayIdentity(auth);
    let payload = body || {};
    if (auth.shared) {
        if (payload.conv_id && payload.conv_id !== auth.conv_id) throw new Error("对话已切换");
        const fields = {}, config = {};
        for (const key of ["pid", "text", "snap_id", "observed_version", "observed_pids"]) {
            if (payload[key] !== undefined) fields[key] = payload[key];
        }
        const snapshot = url.match(/\/snapshot\/([^/]+)\/annotate/);
        if (snapshot) fields.snap_id = decodeURIComponent(snapshot[1]);
        for (const key of ["provider", "use_llm", "mock_llm", "api_key", "base_url", "model"]) {
            if (payload[key] !== undefined) config[key] = payload[key];
        }
        payload = { data: fields, ...config };
    }
    const res = await fetch(_ownedReplayURL(url, auth, true), { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(payload) });
    let data = null;
    try { data = await res.json(); } catch (_e) { data = null; }
    if (!_identityCurrent(launch)) throw new Error("对话已切换");
    if (!res.ok) {
        const err = new Error("HTTP " + res.status);
        err.status = res.status;
        err.data = data;
        throw err;
    }
    return data;
}

/* 状态行（窗口内的一句人话）：恒 textContent——它承接的是后端给的短码与本地文案，不承接 HTML。 */
function _setNote(text) {
    const el = $("replayNote");
    if (!el) return;
    const t = String(text || "");
    el.hidden = !t;
    el.textContent = t;
}

/* 图谱栏当前是否折叠（类挂 pane，_toggleGraph 是唯一写点）。 */
function _graphCollapsed() {
    const pane = $("sideReplayPane");
    return !!(pane && pane.classList.contains("rp-graph-collapsed"));
}

/* 状态行按最新一次 points/status 落定：整理中恒显示 > 折叠态的跳过原因 > 空。
   跳过原因默认归图谱内 .g-skip 单处显示（replay_graph 渲染同一句话，映射同一份）；
   只有图谱折叠、.g-skip 被一起藏起时才由本行兜底——展开即收起。
   collapsed 不传＝就地读 pane 的折叠类；_toggleGraph 刚翻转类名时显式传入新值。 */
function _applyNote(d, collapsed) {
    if (d && d.drawing) { _setNote("正在整理这次的痕迹…"); return; }
    const skip = String((d && d.skip_reason) || "");
    const folded = collapsed === undefined ? _graphCollapsed() : !!collapsed;
    if (skip && folded) { _setNote(skipReasonZh(skip)); return; }
    _setNote("");
}

/* 图数据换版才重画；未读/标注等元数据单独刷新，不重建用户正在编辑的面板或重置视口。 */
function _stamp(d) {
    const dg = (d && d.digest) || null;
    return [Number(d && d.digest_version) || 0, dg ? Number(dg.created_at) || 0 : 0].join("|");
}

/* 本机对话 → 后端 backfill 的 turns 形状（[{role,text}]，后端只收前 60 条、单条截 2000）。
   先认 role/text；注入侧若给的是 board 的对话记录形状（{k,t}），按「say/refine/action = 用户侧、
   其余 = 系统侧」折算——注入点不必为复盘专门转一次形。 */
function _localTurns() {
    if (!_logGetter) return [];
    let raw = null;
    try { raw = _logGetter(); } catch (_e) { return []; }
    if (!Array.isArray(raw)) return [];
    const out = [];
    raw.forEach((t) => {
        if (!t) return;
        const text = String(t.text != null ? t.text : (t.t != null ? t.t : "")).trim();
        if (!text) return;
        const role = String(t.role || "").toLowerCase();
        if (role === "user" || role === "assistant") { out.push({ role: role, text: text }); return; }
        const k = String(t.k || "");
        out.push({ role: (k === "say" || k === "refine" || k === "action") ? "user" : "assistant", text: text });
    });
    return out.slice(0, 60);
}

/* ---------- 导航角标 ---------- */
function _setBadge(n) {
    const el = $("replayNavBadge");
    if (!el) return;
    const v = Math.max(0, Math.floor(Number(n) || 0));
    el.hidden = v <= 0;
    el.textContent = v <= 0 ? "" : (v > 99 ? "99+" : String(v));
    el.title = v <= 0 ? "" : (v + " 条新进展待看");
}

/* 一轮对话落地（board/search 调）：起角标轮询，把后台整理「整理完了、有未看的进展」
   如实反映在页签角标上。drawing 落回 false（或 15 次到顶）即停——角标是提示，不是长轮询。 */
export function replayTurnDone() {
    if (!_bound) return;                // pane 没接线（旧页面/测试页）：整模块静默
    if (_viewTimer) return;             // 页签激活中：5s 观察轮询已在跑，不必第二路
    _badgeTries = 0;
    _pollBadge();
}

function _pollBadge() {
    if (_badgeTimer) { clearTimeout(_badgeTimer); _badgeTimer = null; }
    const token = beginRequest(_badgeRequests, () => _q(replayConvAuth()));
    const auth = replayConvAuth();
    if (!auth.conv_id) { _setBadge(0); return; }
    _get(RP.status + _q(auth))
        .then((d) => {
            if (!token.isCurrent()) return;
            if (d && d.ok) _setBadge(d.unseen);
            _badgeTries += 1;
            if (d && d.ok && d.drawing && _badgeTries < BADGE_MAX) _badgeTimer = setTimeout(_pollBadge, BADGE_MS);
            else _badgeTimer = null;   // 整理落定（或到顶）：停
        })
        .catch(() => { if (token.isCurrent()) _badgeTimer = null; });
}

/* 输入框清空（新对话 / 换号）：草稿与 pill 都是**上一段对话**的内容——留着的话，用户按下
   发送就会用新身份把旧身份的草稿发出去。 */
function _clearComposer() {
    const box = $("replayComposer");
    if (box) box.innerHTML = "";
    _syncSend();
}

/* 对话边界（board 在 cbLogClear 后调）：角标清零、轮询全停、缓存/消息列表/本次新进展标记丢弃——
   新对话＝新复盘线程，pane 里不许残留上一段的图、对话与标记。页签激活中时顺手换成新对话的痕迹。 */
export function replayReset() {
    invalidateRequest(_pointsRequests);
    invalidateRequest(_viewRequests);
    invalidateRequest(_badgeRequests);
    _stopView();
    if (_graph) _graph.reload(null);
    _setBadge(0);
    if (_badgeTimer) { clearTimeout(_badgeTimer); _badgeTimer = null; }
    _badgeTries = 0;
    _points = null;
    _pointsToken = null;
    _fresh.clear();
    _freshVer = null;
    _chatStamp = -1;
    _renderChat([]);    // 消息列表回落空态提示（_renderChat 是唯一写点，不再单独清列）
    _clearComposer();   // 换号/新对话后上一段的草稿与 pill 不许留在输入框
    if (_bound && _active) _onOpen();
}

/* ---------- 页签激活 / 离开（board.js swApplyMode 链尾调 replaySetActive） ----------
   可见性的唯一裁决在 board 的页签机制；这里只响应通知：激活 = 取数渲染后核销 + 起观察轮询，
   离开 = 轮询全停、本次新进展标记清除。initReplay 未跑（无 pane 页）时只记账 _active，不动轮询。 */
export function replaySetActive(on) {
    const next = !!on;
    if (next === _active) return;
    _active = next;
    if (!_bound) return;
    if (next) _onOpen();
    else _onClose();
}

/* 先取数并渲染，再核销；失败保留角标，不能把打开的空 pane 当作已读。 */
function _onOpen() {
    loadPoints().then((loaded) => { if (loaded) _markSeen(); });
    _startView();
}

/* 离开页签停观察轮询（浮窗时代挂在浮窗 close 上；rp1 起由 replaySetActive(false) 触发）。 */
function _onClose() {
    invalidateRequest(_pointsRequests);
    invalidateRequest(_viewRequests);
    _stopView();
    _fresh.clear();   // 激活冻结、离开清除：pid 是位置式编号，跨激活轮次不可比，留着会指到别的节点
    _freshVer = null;
}

function _startView() {
    _stopView();
    _viewTimer = setTimeout(_viewTick, VIEW_MS);
}

function _stopView() {
    if (_viewTimer) { clearTimeout(_viewTimer); _viewTimer = null; }
}

/* 页签激活期间的 5s 观察轮询：整理在跑 → 悄悄刷新（live）；整理刚落地（unseen>0）→ 刷一次并核销
   （用户正看着 pane，不该再攒角标）。失败静默：pane 里的既有内容仍可用，下一拍照常。 */
async function _viewTick() {
    _viewTimer = null;
    if (!_active) return;
    const token = beginRequest(_viewRequests, () => _q(replayConvAuth()));
    try {
        const auth = replayConvAuth();
        if (auth.conv_id) {
            const d = await _get(RP.status + _q(auth));
            if (!token.isCurrent()) return;
            if (d && d.ok) {
                _setBadge(d.unseen);
                if (d.drawing) await loadPointsQuiet();
                else if (Number(d.unseen) > 0) {
                    const loaded = await loadPointsQuiet();
                    if (loaded && token.isCurrent()) await _markSeen();
                }
            }
        }
    } catch (_e) { /* 观察轮询失败：静默（内容仍在，不刷错误） */ }
    if (token.isCurrent() && _active) _viewTimer = setTimeout(_viewTick, VIEW_MS);
}

function _displayedVersion() {
    const pane = $("sideReplayPane");
    if (!_active || !pane || pane.hidden || pane.classList.contains("rp-graph-collapsed")
            || !_graph || typeof _graph.viewState !== "function") return null;
    const state = _graph.viewState();
    return state && state.tab === "current" && Number.isInteger(state.digestVersion)
        ? state.digestVersion : null;
}

/* 读取 token 证明数据身份，graph 观测证明实际显示；两者必须指向同一版本。
   stale 时成功刷新且显示当前图才重发一次，旧后端无版本核销兼容不用于这里。 */
function _markSeen(retried) {
    const token = _pointsToken, points = _points;
    const auth = replayConvAuth(), displayed = _displayedVersion();
    if (!auth.conv_id || !token || !token.isCurrent() || !points || displayed === null
            || points.digest_version === undefined || points.digest_version === null
            || !Number.isInteger(Number(points.digest_version))
            || Number(points.digest_version) !== displayed) return Promise.resolve();
    _setBadge((points.unseen || []).length);
    const body = { conv_id: auth.conv_id, key: auth.key, observed_version: Number(points.digest_version) };
    return _post(RP.seen, body).then((d) => {
        if (!token.isCurrent() || !d || d.ok !== true) return null;
        if (d.stale !== true) { invalidateRequest(_badgeRequests); _setBadge(0); return null; }
        if (retried) return null;
        return loadPointsQuiet().then((loaded) => loaded ? _markSeen(true) : null);
    }).catch(() => null);
}

/* 本次新进展打标集：与角标同真源（points 的 unseen = worth 列表，后端同一函数同一口径），
   故与角标数字 1:1 对账——知识点不在此列（角标也不数它）。digest 换版即清空重算：pid 是位置式
   编号，旧版的 pid 在新版指的是别的节点；同版内只并入不删除（观察轮询多拍时新标记不许自己消失）。 */
function _captureFresh(d) {
    // 服务端给出显式的 digest_version（无 digest=0，每次整理 +1）——有它就用它，
    // 老载荷回退到 digest.created_at（既有行为）。
    const ver = (d && d.digest_version !== undefined && d.digest_version !== null)
        ? (Number(d.digest_version) || 0)
        : (Number(((d && d.digest) || {}).created_at) || 0);
    if (ver !== _freshVer) { _fresh.clear(); _freshVer = ver; }
    let grew = false;
    for (const pid of ((d && d.unseen) || [])) {
        const p = String(pid || "");
        if (!p || _fresh.has(p)) continue;
        _fresh.add(p);
        grew = true;
    }
    return grew;
}

/* ---------- 数据：/api/replay/points 的唯一读取点 ----------
   quiet=true 用于后台/观察刷新：不写「正在读取」态，且数据指纹没变就不重画图谱。 */
async function loadPoints(opts) {
    const token = beginRequest(_pointsRequests, () => _q(replayConvAuth()));
    const quiet = !!(opts && opts.quiet);
    const auth = replayConvAuth();
    if (!auth.conv_id) {
        _points = null;
        if (_graph) _graph.reload(null);
        _renderEmpty();
        _renderChat([]);
        return;
    }
    if (!quiet) _setNote("正在读取痕迹…");
    try {
        const d = await _get(RP.points + _q(auth));
        if (!token.isCurrent()) return false;
        if (!d || !d.ok) throw new Error("points_unavailable");
        const grew = _captureFresh(d);
        const changed = !_points || _stamp(d) !== _stamp(_points);
        _points = d;
        _setBadge((d.unseen || []).length);
        _refreshSnapshotPills(d.snapshots);
        if (_graph) _graph.reload(d, { redraw: !quiet || changed || grew });
        _renderEmpty();
        _renderChat(((d.threads || {}).session) || []);
        _applyNote(d);
        _pointsToken = token;
        return true;
    } catch (_e) {
        if (!token.isCurrent()) return false;
        // 读不到：已有数据就留着（别把屏上的图抹成空白），首次读取失败才落空态。
        if (!_points && _graph) _graph.reload(null);
        if (!quiet) _setNote("痕迹暂时读不出来；稍后可点「重新整理」再试。");
    }
}

function loadPointsQuiet() { return loadPoints({ quiet: true }); }

/* 快照 pill 的载荷刷新：打包后用户还能在快照页继续「记录」标注（服务端已收），
   而 pill 里存的是打包那一刻的文本——发送时就会漏掉刚记的那条，用户却看到「记录成功」。
   points 是快照文本的**单一数据源**（服务端同一函数生成 text），每次取数后按 snapId 重贴
   payload：形状不对（旧数据没有 text）→ 保留原值，不拿空串抹掉用户已看到的内容。 */
function _refreshSnapshotPills(snapshots) {
    const box = $("replayComposer");
    if (!box || typeof box.querySelectorAll !== "function" || !Array.isArray(snapshots) || !snapshots.length) return;
    const byId = new Map();
    for (const s of snapshots) {
        if (s && s.snap_id) byId.set(String(s.snap_id), String(s.text || ""));
    }
    box.querySelectorAll(".pill[data-snap-id]").forEach((pill) => {
        const text = byId.get(String((pill.dataset && pill.dataset.snapId) || ""));
        if (text) pill.dataset.payload = text;
    });
}

/* ---------- 空态与补建 ----------
   服务端尚无痕迹（n_turns === 0）而本机有对话 → 在空态里补一颗「用本机对话补建痕迹」。
   _points 未到（null）时不动：那时 n_turns 未知，显示按钮就是猜。 */
function _renderEmpty() {
    const box = $("replayEmpty");
    if (!box) return;
    const known = !!_points;
    const nTurns = Number((_points && _points.n_turns) || 0);
    box.hidden = !(known && nTurns === 0);
    let btn = box.querySelector(".rp-backfill");
    if (replayConvAuth().shared || !(known && nTurns === 0) || !_localTurns().length) {
        if (btn) btn.remove();   // 已有痕迹/本机无对话：补建入口收起
        return;
    }
    if (btn) return;             // 已在（不重建：用户可能正按着它）
    btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn rp-backfill";
    btn.textContent = BACKFILL_LABEL;
    btn.addEventListener("click", () => _doBackfill(btn));
    box.append(btn);
}

/* 补建：用户显式点才把本机对话上传（对齐 /api/dream 的主动发送先例）；后端 fail-closed，
   服务端已有记录时 skipped:"has_records" 不重复补。补完不手动起轮询——窗口开着的 5s
   观察轮询自会看到 drawing 并 live 刷新。 */
async function _doBackfill(btn) {
    const auth = replayConvAuth();
    const turns = _localTurns();
    if (!auth.conv_id || !turns.length) { toast("本机没有可补建的对话"); return; }
    btn.disabled = true;
    btn.textContent = "正在补建…";
    try {
        const d = await _post(RP.backfill, Object.assign({ conv_id: auth.conv_id, key: auth.key, turns: turns }, llmOverrides()));
        if (!d || d.ok === false) throw new Error("backfill_failed");
        _setNote(d.skipped === "has_records"
            ? "服务端已有痕迹，这次没有重复补建。"
            : "已补建 " + (Number(d.n) || 0) + " 条本机对话，正在后台整理…");
        await loadPoints();
    } catch (_e) {
        _setNote("补建没成功，稍后可以再试。");
    } finally {
        btn.disabled = false;
        btn.textContent = BACKFILL_LABEL;
    }
}

/* ---------- 图谱折叠 ----------
   CSS 按 .rp-graph-collapsed 藏图谱栏、追问对话占满整 pane（样式归 app.css），本函数只切类与按钮文案。
   展开时按当前数据重画一次：折叠期间图谱量到的宽度是 0，不重画会留着折叠时的视口。 */
function _toggleGraph() {
    const pane = $("sideReplayPane");
    if (!pane) return;
    const collapsed = pane.classList.toggle("rp-graph-collapsed");
    const btn = $("replayGraphToggle");
    if (btn) {
        btn.textContent = collapsed ? "‹ 展开图谱" : "收起图谱 ›";
        btn.setAttribute("aria-expanded", collapsed ? "false" : "true");
        btn.title = collapsed ? "展开图谱" : "收起图谱，把空间留给追问对话";
    }
    const col = $("replayGraphCol");
    if (col) {
        col.inert = collapsed;
        if (collapsed && col.contains(document.activeElement) && btn) btn.focus({ preventScroll: true });
    }
    _applyNote(_points, collapsed);   // 折叠时状态行接管 skip 原因、展开即收起（按当前数据重放）
    if (!collapsed && _graph && _points) { _graph.reload(_points); _markSeen(); }
}

/* ---------- 上下分区分割线 ----------
   交互模式复用侧栏宽 resizer（core/shell.js initSidebarResize）：pointer capture 拖拽、
   方向键与 Home、End 键、双击复位、localStorage 记住比例；本模块不 import #shell，模式就地重写。
   比例 = 图谱栏 flex-basis 占 pane 高的百分比（CSS transition 负责程序化伸缩的补间，
   拖拽期间 pane 挂 .rp-split-drag 关过渡）。点节点弹详情面板时分割线下移腾出空间
   （replay_graph 经 ctx.onPanel 上报），提交/关闭后缩回用户拖拽记住的基准比例。 */
const SPLIT = { min: 25, max: 78, dflt: 50, expand: 68 };
const SPLIT_LS = "biodata_replay_split_v1";
let _splitBase = SPLIT.dflt;   // 拖拽/键盘定下的基准比例；面板伸缩不改动它
let _splitTimer = 0;           // 伸缩/拖拽收尾后重 fit 图谱的计时

function _setSplit(pct, persist) {
    const col = $("replayGraphCol"), sp = $("replaySplit");
    const n = Math.max(SPLIT.min, Math.min(SPLIT.max, Number(pct) || SPLIT.dflt));
    if (col) col.style.flex = "0 0 " + n + "%";
    if (sp) sp.setAttribute("aria-valuenow", String(Math.round(n)));
    if (persist) { try { localStorage.setItem(SPLIT_LS, String(n)); } catch (_e) { /* 无痕模式等：不持久不致命 */ } }
    return n;
}

/* 分割变化后 svg 尺寸变了但图谱不会自动重 fit（replay_graph 只在未落位时补初始 fit，
   用户的缩放/平移不许被 resize 静默重置）——交互收尾后显式 refit 一次。 */
function _scheduleRefit(ms) {
    if (_splitTimer) clearTimeout(_splitTimer);
    _splitTimer = setTimeout(() => {
        _splitTimer = 0;
        if (_graph && typeof _graph.refit === "function") _graph.refit();
    }, ms);
}

function _initSplit() {
    const pane = $("sideReplayPane"), sp = $("replaySplit"), col = $("replayGraphCol");
    if (!pane || !sp || !col || sp.dataset.bound) return;
    sp.dataset.bound = "1";
    try { _splitBase = Number(localStorage.getItem(SPLIT_LS)) || SPLIT.dflt; } catch (_e) { _splitBase = SPLIT.dflt; }
    _setSplit(_splitBase, false);
    let drag = null;
    sp.addEventListener("pointerdown", (ev) => {
        if (ev.button !== 0) return;
        drag = { id: ev.pointerId, top: col.getBoundingClientRect().top,
            total: pane.getBoundingClientRect().height };
        try { sp.setPointerCapture(ev.pointerId); } catch (_e) { /* 捕获失败不致命 */ }
        pane.classList.add("rp-split-drag");
        ev.preventDefault();
    });
    sp.addEventListener("pointermove", (ev) => {
        if (!drag || ev.pointerId !== drag.id) return;
        _splitBase = _setSplit(100 * (ev.clientY - drag.top) / drag.total, false);
    });
    const finish = (ev) => {
        if (!drag || (ev && typeof ev.pointerId === "number" && ev.pointerId !== drag.id)) return;
        drag = null;
        pane.classList.remove("rp-split-drag");
        _setSplit(_splitBase, true);
        _scheduleRefit(30);
    };
    sp.addEventListener("pointerup", finish);
    sp.addEventListener("pointercancel", finish);
    sp.addEventListener("lostpointercapture", finish);
    sp.addEventListener("keydown", (ev) => {
        let next = _splitBase;
        if (ev.key === "ArrowUp") next -= 4;
        else if (ev.key === "ArrowDown") next += 4;
        else if (ev.key === "Home") next = SPLIT.min;
        else if (ev.key === "End") next = SPLIT.max;
        else return;
        ev.preventDefault();
        _splitBase = _setSplit(next, true);
        _scheduleRefit(420);   // 键盘/双击走 CSS transition 补间，动画收尾后重 fit
    });
    sp.addEventListener("dblclick", (ev) => {
        ev.preventDefault();
        _splitBase = _setSplit(SPLIT.dflt, true);
        _scheduleRefit(420);
    });
}

/* 详情面板显隐通知（replay_graph 唯一上报口）：弹出 = 分割线下移腾出详情+标注框；
   合拢 = 回到拖拽记住的基准比例。折叠态先展开图谱，否则面板开在不可见栏里。 */
function _panelSplit(open) {
    const pane = $("sideReplayPane");
    if (!pane || !$("replayGraphCol")) return;
    if (open && pane.classList.contains("rp-graph-collapsed")) _toggleGraph();
    _setSplit(open ? Math.max(_splitBase, SPLIT.expand) : _splitBase, false);
    _scheduleRefit(420);
}

/* ---------- 「添加到对话」/「重新整理」 ---------- */
/* 打包全部标注成快照（服务端同时清空图上标注）→ 一颗 pill 落进 composer。
   count=0 不是失败，是「还没写标注」——按人话提示指路，不弹错误样式。 */
async function _addToChat() {
    const btn = $("replayAddBtn");
    const auth = replayConvAuth();
    if (!auth.conv_id) { toast("还没有可复盘的对话"); return; }
    if (btn) btn.disabled = true;
    const launch = _replayIdentity(auth);   // 快照回包必须核发起身份
    try {
        const d = await _post(RP.snapshot, { conv_id: auth.conv_id, key: auth.key });
        /* 迟回包闸：打包期间换号/切对话 → 这颗 pill 属于上一段（A 的标注内容），
           既不许落进 B 的输入框，也不许按 B 的凭据提交。 */
        if (!_identityCurrent(launch)) return;
        const count = Number((d && d.count) || 0);
        if (!d || d.ok === false || !count) { toast("还没有标注——先点图里的节点/弧写一条"); return; }
        _insertPill("复盘标注 " + count + " 条", String(d.text || ""), { snapId: String(d.snap_id || "") });
        toast("已放进输入框：接着说你要问的就行");
        await loadPointsQuiet();   // 图上标注已打包清空：角标与线程随之重画
    } catch (_e) {
        if (_identityCurrent(launch)) toast("这次没有打包成功，稍后再试");
    } finally {
        if (btn) btn.disabled = false;
    }
}

/* 整理能力和配置覆盖由服务端判定，浏览器不替它判定是否有模型。 */
async function _redraw() {
    const btn = $("replayRedrawBtn");
    const auth = replayConvAuth();
    if (!auth.conv_id) { toast("还没有可复盘的对话"); return; }
    const label = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "正在整理…"; }
    _setNote("整理中…（浓缩痕迹 → 模型整理，范围大时约需数十秒）");
    let skipped = "";
    try {
        const d = await _post(RP.digest, Object.assign({ conv_id: auth.conv_id, key: auth.key }, llmOverrides()));
        if (d && d.ok === false) skipped = String(d.skipped || "no_config");
        await loadPoints();
        // 载入后覆盖状态行：这句话比「读取完成」重要；展开态归图谱 .g-skip 单处显示，只有折叠才落状态行
        if (skipped && _graphCollapsed()) _setNote(skipReasonZh(skipped));
    } catch (_e) {
        _setNote("整理没成功，稍后再试一次。");
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = label || "重新整理"; }
    }
}

/* ---------- sidechat（复盘对话） ---------- */
/* 空态提示的唯一真源（此前静态摆在 index.html 的 #replayChat 里，首次渲染就被清掉，
   输入框上方会空一片）：无消息时由 _renderChat 渲染这句话，有消息时只渲染气泡。 */
const CHAT_HINT = "针对本会话的图谱提问 / 质疑 / 学习——拥有主对话全部上下文";

/* 服务端的复盘史（threads.session）→ 消息列表。只有条数变了才整列重画——
   本地发送后即渲染、服务端落地后同源回读，两处内容一致，不重复不闪。
   空数组 = 空态：渲染提示句；条数变化（含 0↔N）整列重画，提示与气泡互斥。 */
function _renderChat(msgs) {
    const list = $("replayChat");
    if (!list) return;
    if (msgs.length === _chatStamp) return;
    _chatStamp = msgs.length;
    list.innerHTML = "";
    if (!msgs.length) {
        const hint = document.createElement("p");
        hint.className = "rp-side-hint";
        hint.textContent = CHAT_HINT;
        list.append(hint);
        return;
    }
    for (const m of msgs) _bubble(m.role === "user" ? "user" : "assistant", String(m.content || ""));
}

/* 一颗消息泡。用户原话按纯文本（先 escapeHtml 再把换行转 <br>）；模型/后端回复走 mdLite
   （内部先整体转义）——两条路都不把未转义文本交给 innerHTML。 */
function _bubble(role, text) {
    const list = $("replayChat");
    const hint = list && list.querySelector ? list.querySelector(".rp-side-hint") : null;
    if (hint) hint.remove();   // 落泡＝列表不再空：空态提示让位（_sendChat 本地先渲染泡、不清列）
    const el = document.createElement("div");
    const mine = role === "user";
    el.className = "msg " + (mine ? "msg-user" : "msg-assistant");
    if (mine) el.innerHTML = escapeHtml(text).replace(/\n/g, "<br>");
    else el.innerHTML = mdLite(text);
    if (!list) return el;
    list.append(el);
    list.scrollTop = list.scrollHeight;   // 恒不带 smooth（reduced-motion 天然满足）
    return el;
}

/* composer 提文本：文本节点原样；pill 用 \n\n 包裹它的 dataset.payload 拼进来（与复盘器
   chat.js 的 extract 同口径）；<br> → 换行；DIV/P → 换行后递归（嵌套在行内的 pill 也照样拼进去）。 */
function _extract() {
    const box = $("replayComposer");
    if (!box) return "";
    let out = "";
    const walk = (nodes) => {
        for (const node of nodes) {
            if (node.nodeType === Node.TEXT_NODE) { out += node.textContent; continue; }
            if (node.nodeType !== Node.ELEMENT_NODE) continue;
            if (node.classList.contains("pill")) { out += "\n\n" + String(node.dataset.payload || "") + "\n\n"; continue; }
            if (node.tagName === "BR") { out += "\n"; continue; }
            if (node.tagName === "DIV" || node.tagName === "P") {
                if (out && !out.endsWith("\n")) out += "\n";
                walk(node.childNodes);
                continue;
            }
            walk(node.childNodes);
        }
    };
    walk(box.childNodes);
    return out.trim();
}

function _caretEnd() {
    const box = $("replayComposer");
    if (!box || typeof window.getSelection !== "function") return;
    box.focus();
    const r = document.createRange();
    r.selectNodeContents(box);
    r.collapse(false);
    const sel = window.getSelection();
    if (!sel) return;
    sel.removeAllRanges();
    sel.addRange(r);
}

/* 「添加到对话」的落点：span.pill[contenteditable=false] + dataset.payload（文本载荷）+
   dataset.snapId（有则点本体可找回快照）。与复盘器 chat.js 的 insertPill 同构，
   文案恒 textContent（标签来自本地拼接的数字，也不拼 HTML）。 */
function _insertPill(label, payload, opts) {
    const box = $("replayComposer");
    if (!box) return;
    const o = opts || {};
    const pill = document.createElement("span");
    pill.className = "pill";
    pill.contentEditable = "false";
    pill.dataset.payload = String(payload || "");
    if (o.snapId) {
        pill.dataset.snapId = String(o.snapId);
        pill.classList.add("pill-snap");
        pill.title = "点击在图谱窗里打开这张带标注的图";
    }
    const lab = document.createElement("span");
    lab.className = "pill-label";
    lab.textContent = String(label || "");
    const x = document.createElement("span");
    x.className = "pill-x";
    x.textContent = "×";
    x.title = "移除";
    x.addEventListener("click", (ev) => { ev.stopPropagation(); pill.remove(); _syncSend(); });
    pill.append(lab, x);
    const sel = window.getSelection && window.getSelection();
    if (sel && sel.rangeCount && box.contains(sel.anchorNode)) {
        const r = sel.getRangeAt(0);
        r.deleteContents();
        r.insertNode(pill);
        r.setStartAfter(pill);
        r.collapse(true);
        sel.removeAllRanges();
        sel.addRange(r);
    } else {
        box.append(pill);
    }
    box.append(document.createTextNode(" "));
    _caretEnd();
    _syncSend();
}

/* 发送键：在途或空 composer 都禁用（空态一眼看得出「没什么可发」）。 */
function _syncSend() {
    const send = $("replaySendBtn");
    if (!send) return;
    send.disabled = _busy || !_extract();
}

function _onPaste(ev) {
    // 粘贴只收纯文本：外部 HTML（样式/脚本/图片）一律不进门，也不让它带进 DOM。
    ev.preventDefault();
    const dt = ev.clipboardData || window.clipboardData;
    const text = dt ? String(dt.getData("text/plain") || "") : "";
    if (!text) return;
    const box = $("replayComposer");
    if (!box) return;
    const sel = window.getSelection && window.getSelection();
    if (sel && sel.rangeCount && box.contains(sel.anchorNode)) {
        const r = sel.getRangeAt(0);
        r.deleteContents();
        const node = document.createTextNode(text);
        r.insertNode(node);
        r.setStartAfter(node);
        r.collapse(true);
        sel.removeAllRanges();
        sel.addRange(r);
    } else {
        box.append(document.createTextNode(text));
    }
    _syncSend();
}

/* 退格删 pill（整颗删，不让用户卡在「半颗 pill」上）——与复盘器 chat.js 同口径。 */
function _backspacePill(ev) {
    const box = $("replayComposer");
    const sel = window.getSelection && window.getSelection();
    if (!box || !sel || !sel.isCollapsed || !sel.rangeCount || !sel.anchorNode) return;
    const anchorNode = sel.anchorNode, anchorOffset = sel.anchorOffset;
    let victim = null;
    if (anchorNode === box && anchorOffset > 0) {
        const prev = box.childNodes[anchorOffset - 1];
        if (prev && prev.classList && prev.classList.contains("pill")) victim = prev;
    } else if (anchorNode.nodeType === Node.TEXT_NODE && anchorOffset === 0) {
        const prev = anchorNode.previousSibling;
        if (prev && prev.classList && prev.classList.contains("pill")) victim = prev;
    }
    if (!victim) return;
    ev.preventDefault();
    victim.remove();
    if (!_extract()) box.innerHTML = "";   // 删干净后归一化，让 :empty 占位提示复活
    _syncSend();
}

/* pill 点本体 = 找回快照（图谱切到该快照标签页）；× 是移除，不触发找回。 */
function _onComposerClick(ev) {
    const target = ev.target;
    const pill = target && target.closest ? target.closest(".pill") : null;
    if (!pill) return;
    if (target.closest(".pill-x")) return;
    const snapId = String(pill.dataset.snapId || "");
    if (!snapId) return;
    const pane = $("sideReplayPane");
    if (pane && pane.classList.contains("rp-graph-collapsed")) _toggleGraph();   // 折叠着先展开，否则「找回了」也看不见
    if (_graph && typeof _graph.openSnapshot === "function") _graph.openSnapshot(snapId);
}

function _bindComposer() {
    const box = $("replayComposer");
    if (!box) return;
    const send = $("replaySendBtn");
    box.addEventListener("input", _syncSend);
    box.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); _sendChat(); return; }
        if (ev.key === "Backspace") _backspacePill(ev);
    });
    box.addEventListener("paste", _onPaste);
    box.addEventListener("click", _onComposerClick);
    if (send) send.addEventListener("click", () => _sendChat());
    _syncSend();
}

/* sidechat 一轮：提取文本（含 pill 载荷）→ POST /api/replay/chat → user/assistant 两颗泡。
   ok:false 的 reply 是**业务回答**（未配 key / 这次没接上模型），如实显示，不另编文案；
   只有传输/HTTP 层失败才落本地那句。发送中禁用按钮，落地即恢复。 */
async function _sendChat() {
    const box = $("replayComposer");
    const text = _extract();
    if (_busy) return;
    if (!text) { toast("先写点什么再发"); return; }
    const auth = replayConvAuth();
    if (!auth.conv_id) { toast("还没有可复盘的对话"); return; }
    _busy = true;
    _syncSend();
    if (box) box.innerHTML = "";     // 发送即清空（与主对话输入条同语义）
    _chatStamp += 1;                 // 本地先渲染一颗 user 泡；服务端落地后 _renderChat 同源对齐
    _bubble("user", text);
    const pending = _bubble("assistant", "…");
    try {
        const d = await _post(RP.chat, Object.assign({ conv_id: auth.conv_id, key: auth.key, text: text }, llmOverrides()));
        const reply = String((d && d.reply) || "");
        if (d && d.ok && reply) {
            pending.innerHTML = mdLite(reply);
            _chatStamp += 1;         // assistant 泡落地：与服务端 threads.session 条数对齐
        } else {
            pending.innerHTML = mdLite(reply || "（这次没接上大模型，稍后再试。）");
            pending.classList.add("msg-err");
            _chatStamp -= 1;         // 失败：服务端没存这轮，stamp 回退（下轮重渲染会修正）
        }
    } catch (_e) {
        pending.innerHTML = mdLite("（复盘对话没走通，稍后再试。）");
        pending.classList.add("msg-err");
        _chatStamp -= 1;
    } finally {
        _busy = false;
        _syncSend();
        const list = $("replayChat");
        if (list) list.scrollTop = list.scrollHeight;
    }
}

/* ---------- 入口（boot 调一次；DOM 自守卫 + 幂等，本文件不 import #board/#shell） ---------- */
export function initReplay() {
    const pane = $("sideReplayPane");
    if (!pane || pane.dataset.rpBound) return;   // DOM 自守卫（无 pane 页：dataset.html/测试页）+ 幂等
    pane.dataset.rpBound = "1";
    _bound = true;
    const graphHost = $("replayGraph");
    if (graphHost) {
        graphHost.setAttribute("role", "region");
        graphHost.setAttribute("aria-label", "痕迹图谱");
    }
    const composer = $("replayComposer");
    if (composer) {
        composer.title = "Enter 换行，Ctrl+Enter 发送";
        composer.setAttribute("aria-description", "Enter 换行，Ctrl+Enter 发送");
    }
    const send = $("replaySendBtn");
    if (send) send.title = "发送（Ctrl+Enter）";
    const toggle = $("replayGraphToggle");
    if (toggle) toggle.addEventListener("click", _toggleGraph);
    const addBtn = $("replayAddBtn");
    if (addBtn) addBtn.addEventListener("click", _addToChat);
    const redraw = $("replayRedrawBtn");
    if (redraw) redraw.addEventListener("click", _redraw);
    _bindComposer();
    _initSplit();
    _graph = initReplayGraph($("replayGraph"), {
        // convId/key 用函数而不是快照值：切对话后图上标注/取数必须落到**当前**对话
        convId: () => replayConvAuth().conv_id,
        key: () => replayConvAuth().key,
        freshPids: () => Array.from(_fresh),   // 本次新进展打标集（触发角标的那批 pid）；写权仍在本文件
        api: { getJSON: _get, postJSON: _post },   // 取数/写入与属主同一条通道（URL 真源在本文件）
        mdLite: mdLite,
        escapeHtml: escapeHtml,
        skipReasonZh: skipReasonZh,   // 跳过原因映射透传（#replay_core 单一真源；图谱侧不新增 import 边）
        onChanged: loadPointsQuiet,
        onViewChanged: () => _markSeen(),
        onPanel: _panelSplit,   // 详情面板显隐 → 分割线下移腾位 / 缩回基准比例
    });
    _setBadge(0);
    _renderChat([]);          // 进页签就落空态提示（不等 loadPoints 回来）
    if (_active) _onOpen();   // 页签激活通知先于 init 到达时（replaySetActive 只记账）：补一次开场取数
}
