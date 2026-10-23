// replay_graph.js — 复盘决策因果图（移植自内部工具「复盘器」web/js/graph.js 的 SVG 渲染 + 交互层，2026-09-28 复盘批）。
// window 事件总线改为 ctx 回调注入：reload / openSnapshot 由宿主调，标注与快照写入成功后调 ctx.onChanged()；
// 本模块不派发、不监听任何 window 事件（原 points-reloaded / snapshot-updated / graph-reshow / goto-event /
// open-snapshot 通道全部退役）。未移植：列表视图、模型选择器；turn_ref chip 只作信息 chip（不回跳主聊天）。
// 布局纯函数在 #replay_layout（布局内部自行浅拷贝边、不改写传入的 digest）。
// 宿主注入：ctx.mdLite（内部先整体转义）、ctx.escapeHtml 与 ctx.skipReasonZh（跳过原因映射，来自 #replay_core，
// 透传注入——本模块不新增静态 import 边，缺省静默不显示）；本文件进 innerHTML 的其余文本一律先 escapeHtml。
// biodata 端点：GET /api/replay/points、GET /api/replay/snapshots、POST /api/replay/annotate、
// POST /api/replay/snapshot/{snapId}/annotate（当前图 / 快照图双通道，互不覆盖）。
// reload(points, opts?)：只消费宿主数据；null 清空，opts.redraw=false 仅更新元数据，默认重画。取数归 panel/replay.js。
// viewState() 回 {tab, digestVersion}：只有当前图真正渲染且成果/决策节点可见才有版本；宿主再核窗口/折叠。
// 可选 ctx.onViewChanged(state) 只在显示身份/tab/版本变化后通知，供宿主核销实际看到的版本。
import { computeLayout, PAD, GUTTER } from "#replay_layout";

const NS = "http://www.w3.org/2000/svg";
const TYPE_LABEL = { start: "动机", outputs: "成果", knowledge: "知识点", decisions: "决策点" };
/* 图例：不新增 DOM——render 里按当前是否有新进展，在 hint 后追加半句（措辞只此一份）。 */
const HINT = "滚轮缩放 · 拖空白平移 · 点节点/边看详情与标注";
const HINT_FRESH = " · 左上空心圆 = 本次新进展";
/* 同页可挂多个实例：SVG marker 的 id 必须实例内唯一，否则 url(#g-arrow) 会跨实例串台。 */
let _instSeq = 0;

function el(tag, attrs = {}, text) {
    const d = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) d.setAttribute(k, v);
    if (text != null) d.textContent = text;
    return d;
}

/* 快照自包含图（快照自带 items[].point/kind + edges）→ 与 points.digest
   同形的合成 digest，**走同一个 computeLayout**（不在快照页另写一套渲染）。
   重整理之后旧标注对象已不在当前 digest 里，靠这份自带数据仍能画出当时的图。
   老快照没有 point/edges → 返回 null，回退到「用当前 digest + 快照自己的标注」的现状行为。
   纯函数提到模块级：node 规格（frontend_lifecycle_spec）直接取真源码跑。 */
export function snapshotDigest(snap) {
    const items = (snap && Array.isArray(snap.items)) ? snap.items : [];
    const captured = items.filter((it) => it && it.point && typeof it.point === "object");
    if (!captured.length) return null;
    const digest = { motivation: null, outputs: [], knowledge: [], decisions: [], edges: [] };
    for (const it of captured) {
        const kind = String(it.kind || "");
        const point = Object.assign({}, it.point);
        if (!point.pid) point.pid = String(it.pid || "");
        if (kind === "motivation" || kind === "start") digest.motivation = point;
        else if (digest[kind]) digest[kind].push(point);
    }
    if (!digest.motivation && !digest.outputs.length && !digest.knowledge.length && !digest.decisions.length) {
        return null;   // 全是边标注（point 为空）→ 没有可画的节点，回退现状
    }
    if (Array.isArray(snap.edges)) digest.edges = snap.edges.map((e) => Object.assign({}, e));
    return digest;
}

/* 当前渲染的数据源（纯函数）：当前标签页吃宿主注入的 points；快照标签页优先吃快照自带的图。 */
export function activeGraphData(activeTab, data, snap) {
    if (activeTab === "current") return data;
    const digest = snapshotDigest(snap);
    return digest ? { digest: digest, threads: {}, skip_reason: "" } : data;
}

export function initReplayGraph(hostEl, ctx) {
    if (!hostEl) return { reload: () => {}, openSnapshot: () => {},
        viewState: () => ({ tab: "current", digestVersion: null }), refit: () => {} };
    const arrowId = `g-arrow-${++_instSeq}`;
    let data = null, layout = null, fitted = false;
    let fresh = new Set();   // 本次新进展的 pid 集（render 按激活标签页取快照；快照标签页恒空）
    const visible = { start: true, outputs: true, knowledge: true, decisions: true };
    let selected = null; // {kind:'node'|'edge', id}
    const view = { k: 1, tx: 0, ty: 0 };

    const query = () => `conv_id=${encodeURIComponent(ctx.convId())}&key=${encodeURIComponent(ctx.key())}`;
    const snapshotsUrl = () => `/api/replay/snapshots?${query()}`;
    const writeBody = (pid, text) => ({ conv_id: ctx.convId(), key: ctx.key(), pid, text });

    // ---------- 标签页：当前图 + 快照图（pill 找回，互不覆盖） ----------
    const tabs = document.createElement("div");
    tabs.className = "g-tabs";
    tabs.hidden = true;
    let activeTab = "current";
    const snapTabs = new Map(); // snap_id -> snapshot obj
    /* 身份/代序闸：标签页归「当前账户 scope × 对话」所有（ctx.convId/key 是宿主
       注入的身份串，与 replay.js 迟回包闸同源）。换号/切对话/清空（reload(null)）推进
       epochs，在途取快照与标注回包一律整段丢弃——不许写任何 tab/state/DOM；换身份后
       已缓存的标签页也整批作废（A→B→A 也作废：epochs 记的是「换过几次」，不是当前值）。 */
    let tabsIdentity = null;
    let tabsEpochs = 0;
    const identityOf = () => String(ctx.convId() || "") + "|" + String(ctx.key() || "");
    const epochKey = () => tabsEpochs + "|" + identityOf();
    let renderedVersion = null, renderedIdentity = null, viewStamp = null;
    function viewState() {
        return { tab: activeTab, digestVersion: renderedIdentity === epochKey() ? renderedVersion : null };
    }
    function notifyViewChanged() {
        const state = viewState();
        const stamp = epochKey() + "|" + state.tab + "|" + state.digestVersion;
        if (stamp === viewStamp) return;
        viewStamp = stamp;
        if (typeof ctx.onViewChanged === "function") ctx.onViewChanged(state);
    }
    function syncTabsIdentity() {
        const ident = identityOf();
        if (ident === tabsIdentity) return false;
        tabsIdentity = ident;
        tabsEpochs += 1;
        snapTabs.clear();
        activeTab = "current";
        selected = null;
        return true;
    }

    function renderTabs() {
        tabs.innerHTML = "";
        const mk = (id, label, closable) => {
            const b = document.createElement("button");
            b.className = "g-tab" + (activeTab === id ? " on" : "");
            b.textContent = label;
            b.addEventListener("click", () => activateTab(id));
            if (closable) {
                const x = document.createElement("span");
                x.className = "g-tab-x";
                x.textContent = "×";
                x.title = "关闭此快照标签页（快照仍保留，点 pill 可再打开）";
                x.addEventListener("click", (ev) => { ev.stopPropagation(); closeTab(id); });
                b.append(x);
            }
            tabs.append(b);
        };
        mk("current", "当前");
        for (const [id, s] of snapTabs) {
            const when = new Date(s.created_at * 1000)
                .toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
            mk(id, `📎 ${when} ×${s.count}`, true);
        }
        tabs.hidden = snapTabs.size === 0;
    }

    function activateTab(id) {
        activeTab = id;
        selected = null;
        panel.hidden = true;
        panel.innerHTML = "";
        renderTabs();
        render();
        syncPanelHook();
    }

    function closeTab(id) {
        snapTabs.delete(id);
        if (activeTab === id) activateTab("current");
        else renderTabs();
    }

    async function openSnapshot(snapId) {
        const id = String(snapId || "");
        if (!id) return;
        if (syncTabsIdentity()) renderTabs();   // 身份已变：先让上一身份的标签从 DOM 消失，再决定是否重新取
        if (!snapTabs.has(id)) {
            const launch = epochKey();
            const d = await ctx.api.getJSON(snapshotsUrl());
            // 迟回包闸：期间 reset（reload(null)）/换号/切对话 → 旧快照整段丢弃（A→B→A 亦然）
            if (epochKey() !== launch) return;
            const s = (d && d.snapshots || []).find((x) => x.snap_id === id);
            if (!s) return;
            snapTabs.set(id, s);
        }
        activateTab(id);
    }

    // ---------- DOM 骨架 ----------
    const bar = document.createElement("div");
    bar.className = "graph-bar";
    for (const t of ["start", "knowledge", "decisions", "outputs"]) {
        const b = document.createElement("button");
        b.className = `g-chip g-chip-${t} on`;
        b.textContent = TYPE_LABEL[t];
        b.addEventListener("click", () => {
            visible[t] = !visible[t];
            b.classList.toggle("on", visible[t]);
            render();
        });
        bar.append(b);
    }
    const hint = document.createElement("span");
    hint.className = "dim graph-hint";
    hint.textContent = HINT;
    bar.append(hint);
    /* 自动整理说明行：skip_reason 有值时给一句人话（映射经 ctx.skipReasonZh 注入，#replay_core 单一真源；
       与宿主状态行同一决策——展开态归这里单处显示，折叠态才由 #replayNote 兜底；全空态另有 svg 内引导文案）。 */
    const skipNote = document.createElement("div");
    skipNote.className = "g-skip dim";
    skipNote.hidden = true;
    const svg = el("svg", { class: "graph-svg", tabindex: "0" });
    const vp = el("g", {});
    svg.append(vp);
    const panel = document.createElement("div");
    panel.className = "graph-panel";
    panel.hidden = true;
    hostEl.append(tabs, bar, skipNote, svg, panel);

    /* 面板显隐的唯一上报口：宿主（replay.js）据此把分割线动画下移（弹出详情+标注框腾出空间）
       或收回到拖拽记住的比例。render()/activateTab 末尾各核一次，只在变化时上报。 */
    let panelShown = false;
    function syncPanelHook() {
        const vis = !panel.hidden;
        if (vis === panelShown) return;
        panelShown = vis;
        if (typeof ctx.onPanel === "function") ctx.onPanel(vis);
    }

    /* 容器由隐藏变可见（尺寸 0 → 非 0）时补一次初始落位；已落位后不再自动改动用户视图。 */
    if (typeof ResizeObserver !== "undefined") {
        new ResizeObserver(() => {
            if (layout && !fitted) { fitView(layout.W, layout.H); applyView(); }
        }).observe(svg);
    }

    /* 跳过原因的人话映射经宿主 ctx 注入（#replay_core 单一真源，本模块不新增静态 import 边；
       ctx 未注入时静默不显示，绝不自带第二份映射）。 */
    function renderSkipNote() {
        const skip = (activeTab === "current" && data && String(data.skip_reason || "")) || "";
        const zh = skip && typeof ctx.skipReasonZh === "function" ? String(ctx.skipReasonZh(skip) || "") : "";
        skipNote.textContent = zh;
        skipNote.hidden = !zh;
    }

    function fitView(W, H) {
        const r = svg.getBoundingClientRect();
        if (!r.width) return;
        view.k = Math.min(1.2, r.width / W, (r.height || 420) / H);
        view.tx = (r.width - W * view.k) / 2;
        view.ty = 8;
        fitted = true;
    }
    function applyView() {
        vp.setAttribute("transform", `translate(${view.tx},${view.ty}) scale(${view.k})`);
    }

    // ---------- 缩放/平移 ----------
    svg.addEventListener("wheel", (e) => {
        e.preventDefault();
        const r = svg.getBoundingClientRect();
        const mx = e.clientX - r.left, my = e.clientY - r.top;
        const f = e.deltaY < 0 ? 1.12 : 1 / 1.12;
        const k2 = Math.max(0.2, Math.min(3, view.k * f));
        view.tx = mx - (mx - view.tx) * (k2 / view.k);
        view.ty = my - (my - view.ty) * (k2 / view.k);
        view.k = k2;
        applyView();
    }, { passive: false });
    let drag = null, dragSuppress = false;
    svg.addEventListener("pointerdown", (e) => { drag = { x: e.clientX, y: e.clientY, moved: 0, captured: false }; });
    svg.addEventListener("pointermove", (e) => {
        if (!drag) return;
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        drag.moved += Math.abs(dx) + Math.abs(dy);
        if (drag.moved > 5) {
            dragSuppress = true;
            // 只在确认是平移后才捕获指针——否则会抢走节点/边上的真实 click
            if (!drag.captured) { drag.captured = true; try { svg.setPointerCapture(e.pointerId); } catch (_e) { /* 捕获失败不致命 */ } }
        }
        view.tx += dx; view.ty += dy;
        drag.x = e.clientX; drag.y = e.clientY;
        applyView();
    });
    svg.addEventListener("pointerup", () => { drag = null; });
    svg.addEventListener("click", () => {
        if (dragSuppress) { dragSuppress = false; return; } // 平移后的抬起不算点击
        select(null); // 点空白取消选中（节点/边已 stopPropagation）
    });

    // ---------- 选中与详情面板 ----------
    function select(sel) {
        selected = sel;
        if (!sel) { panel.hidden = true; panel.innerHTML = ""; }   // 先清再 render：render 末尾的 syncPanelHook 才能读到合拢态
        render();
    }

    function threadOf(id) {
        if (activeTab === "current") return (data && data.threads && data.threads[id]) || [];
        const s = snapTabs.get(activeTab);
        const it = s && s.items.find((i) => i.pid === id);
        return it ? it.notes.map((n) => ({ role: "user", content: n })) : [];
    }

    function countNotes(pid) {
        if (activeTab === "current") {
            return ((data && data.threads && data.threads[pid]) || []).filter((m) => m.role === "user" && m.content).length;
        }
        const cur = snapTabs.get(activeTab);
        const it = cur && cur.items.find((i) => i.pid === pid);
        return it ? it.notes.length : 0;
    }

    // 节点装饰的唯一入口（本次新进展环 + 标注数角标一并在这趟里画）；render 末尾与记录后各刷一次
    function updateBadges() {
        if (!layout) return;
        for (const n of layout.nodes) {
            const g = vp.querySelector(`g.g-node[data-pid="${n.pid}"]`);
            if (!g) continue;
            const oldBadge = g.querySelector(".g-badge");
            if (oldBadge) oldBadge.remove();
            const oldFresh = g.querySelector(".g-fresh");
            if (oldFresh) oldFresh.remove();
            if (fresh.has(n.pid)) {
                const fd = el("g", { class: "g-fresh", transform: `translate(${-n.w / 2 + 2},${-n.h / 2 + 2})` });
                fd.append(el("circle", { r: 5 }));
                fd.append(el("title", {}, "本次新进展"));
                g.append(fd);
            }
            const c = countNotes(n.pid);
            if (!c) continue;
            const bg = el("g", { class: "g-badge", transform: `translate(${n.w / 2 - 2},${-n.h / 2 + 2})` });
            bg.append(el("circle", { r: 8 }));
            bg.append(el("text", { y: 3.5, class: "g-badge-n" }, String(c)));
            g.append(bg);
        }
    }

    // ---------- 渲染 ----------
    function render() {
        renderedVersion = null;
        renderedIdentity = epochKey();
        vp.innerHTML = "";
        // 本次新进展快照：只对当前图生效（快照标签页是历史存档，不打标）
        fresh = activeTab === "current" ? new Set((ctx.freshPids && ctx.freshPids()) || []) : new Set();
        hint.textContent = fresh.size ? HINT + HINT_FRESH : HINT;
        layout = computeLayout(activeGraphData(activeTab, data, snapTabs.get(activeTab)));
        bar.hidden = !layout;   // 空图连图例行一起收起（chips/提示对着一块空灰盒只会读成一列杂物）
        renderSkipNote();
        if (!layout) {
            view.k = 1; view.tx = 0; view.ty = 0;
            applyView();
            // svg text 不自动换行：空态文案按行拆多个 text，单行在侧栏宽度下会被裁断
            const emptyLines = activeTab === "current"
                ? ["尚无整理数据——每轮对话后会自动整理；", "也可点上方「重新整理」立即生成。"]
                : ["这张快照里没有可画的节点", "（旧快照或只记了关系）——", "标注内容仍在右侧详情里。"];
            emptyLines.forEach((line, i) => vp.append(el("text", { x: 20, y: 40 + i * 22, class: "g-empty" }, line)));
            notifyViewChanged();
            syncPanelHook();
            return;
        }
        const { nodes, edges, byId } = layout;
        const showN = (n) => visible[n.type];
        const showE = (e) => showN(byId[e.from]) && showN(byId[e.to]);

        const defs = el("defs", {});
        const marker = el("marker", { id: arrowId, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" });
        marker.append(el("path", { d: "M0,0 L10,5 L0,10 z", class: "g-arrowhead" }));
        defs.append(marker);
        vp.append(defs);

        // 时间刻度槽：横线 + 回合号（时序即纵轴）
        for (const tl of layout.timeLabels || []) {
            vp.append(el("line", { x1: GUTTER - 10, y1: tl.y, x2: layout.W - PAD, y2: tl.y, class: "g-timeline" }));
            vp.append(el("text", { x: GUTTER - 16, y: tl.y - 5, class: "g-tick" }, tl.text));
        }

        for (const e of edges) {
            if (!showE(e)) continue;
            const a = byId[e.from], b = byId[e.to];
            // 路由唯一依据是 layout 算出的 e.route：bezier=短边三次曲线；lane=右侧车道正交折线
            let d, labelPos;
            if (e.route && e.route.kind === "lane") {
                const pts = e.route.points;
                d = "M" + pts.map((p) => `${p[0]},${p[1]}`).join(" L");
                labelPos = { x: (pts[1][0] + pts[2][0]) / 2, y: pts[1][1] - 4 }; // 第一段水平空隙通道中点
            } else {
                const y1 = a.y + (a.type === "decisions" ? a.h * 0.7 : a.h / 2);
                const y2 = b.y - (b.type === "decisions" ? b.h * 0.7 : b.h / 2);
                const my = (y1 + y2) / 2;
                d = `M${a.x},${y1} C${a.x},${my} ${b.x},${my} ${b.x},${y2}`;
                labelPos = { x: (a.x + b.x) / 2, y: my - 4 };
            }
            const g = el("g", { class: `g-edge${e.backward ? " g-back" : ""}${selected && selected.kind === "edge" && selected.id === e.eid ? " sel" : ""}` });
            const hit = el("path", { d, class: "g-edge-hit", role: "button", tabindex: "0",
                "aria-label": `关系 ${e.label || ""}：${byId[e.from].title || ""} → ${byId[e.to].title || ""}` });
            hit.addEventListener("click", (ev) => { ev.stopPropagation(); select({ kind: "edge", id: e.eid }); });
            hit.addEventListener("keydown", (ev) => {
                if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); select({ kind: "edge", id: e.eid }); }
            });
            g.append(el("path", { d, class: "g-edge-line", "marker-end": `url(#${arrowId})` }), hit);
            if (e.label) g.append(el("text", { x: labelPos.x, y: labelPos.y, class: "g-edge-label" }, e.label));
            vp.append(g);
        }
        for (const n of nodes) {
            if (!showN(n)) continue;
            const isFresh = fresh.has(n.pid);
            const g = el("g", { class: `g-node g-${n.type}${selected && selected.kind === "node" && selected.id === n.pid ? " sel" : ""}`, transform: `translate(${n.x},${n.y})`,
                role: "button", tabindex: "0",
                "aria-label": `${TYPE_LABEL[n.type]} · ${n.title || ""}${isFresh ? " · 本次新进展" : ""}`, "data-pid": n.pid });
            let shape;
            if (n.type === "decisions") {
                shape = el("polygon", { points: `0,${-n.h * 0.7} ${n.w / 2 + 14},0 0,${n.h * 0.7} ${-n.w / 2 - 14},0`, class: "g-shape" });
            } else {
                shape = el("rect", { x: -n.w / 2, y: -n.h / 2, width: n.w, height: n.h, rx: n.type === "start" ? n.h / 2 : 7, class: "g-shape" });
            }
            const text = el("text", { y: 4, class: "g-title" }, n.label);
            const tip = el("title", {}, `${TYPE_LABEL[n.type]} · ${n.title || "（无题）"}${isFresh ? " · 本次新进展" : ""}`);
            g.append(shape, text, tip);
            g.addEventListener("click", (ev) => { ev.stopPropagation(); select({ kind: "node", id: n.pid }); });
            g.addEventListener("keydown", (ev) => {
                if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); select({ kind: "node", id: n.pid }); }
            });
            vp.append(g);
        }
        fitView(layout.W, layout.H);
        applyView();
        updateBadges();
        if (selected) showPanel(); // 重渲染后保持选中面板
        syncPanelHook();
        const version = data && data.digest_version;
        if (activeTab === "current" && version !== undefined && version !== null
                && Number.isInteger(Number(version)) && Number(version) >= 0
                && nodes.filter((n) => n.type === "outputs" || n.type === "decisions").every(showN)) {
            renderedVersion = Number(version);
        }
        notifyViewChanged();
    }

    function msgBubble(role, content) {
        const b = document.createElement("div");
        b.className = `msg msg-${role} md`;
        b.innerHTML = ctx.mdLite(content || (role === "assistant" ? "…" : ""));
        return b;
    }

    function showPanel() {
        if (!layout || !selected) return;
        panel.innerHTML = "";
        panel.hidden = false;
        let pid, headTitle, headDetail, refs;
        if (selected.kind === "node") {
            const n = layout.byId[selected.id];
            if (!n) return;
            pid = n.pid;
            headTitle = `${TYPE_LABEL[n.type]} · ${n.title || ""}`;
            headDetail = n.detail || "";
            refs = n.turn_refs || [];
        } else {
            const e = layout.edges.find((x) => x.eid === selected.id);
            if (!e) return;
            const a = layout.byId[e.from], b = layout.byId[e.to];
            pid = e.eid;
            headTitle = `${(a && a.title) || "?"} —[${e.label || ""}]→ ${(b && b.title) || "?"}`;
            headDetail = `起点：${(a && a.detail) || "（无）"}\n终点：${(b && b.detail) || "（无）"}`;
            refs = [...new Set([...(e.turn_refs || []), ...((a && a.turn_refs) || []).slice(0, 3), ...((b && b.turn_refs) || []).slice(0, 3)])];
        }
        const head = document.createElement("div");
        head.className = "graph-panel-head";
        head.innerHTML = `<b>${ctx.escapeHtml(headTitle)}</b>`;
        const close = document.createElement("button");
        close.className = "graph-panel-close";
        close.textContent = "×";
        close.addEventListener("click", () => select(null));
        head.append(close);
        panel.append(head);
        if (headDetail) {
            const det = document.createElement("div");
            det.className = "point-detail md";
            det.innerHTML = ctx.mdLite(headDetail);
            panel.append(det);
        }
        if (refs.length) {
            // 回合引用只作信息 chip：不再回跳主聊天（原 goto-event 通道已退役）
            const rs = document.createElement("span");
            rs.className = "refs";
            for (const r of refs.slice(0, 8)) {
                const chip = document.createElement("span");
                chip.className = "ref-chip";
                chip.textContent = `#${r}`;
                chip.title = `回合 #${r}`;
                rs.append(chip);
            }
            panel.append(rs);
        }
        const thread = document.createElement("div");
        thread.className = "thread";
        for (const m of threadOf(pid)) thread.append(msgBubble(m.role, m.content));
        panel.append(thread);
        const row = document.createElement("div");
        row.className = "point-input";
        const ta = document.createElement("textarea");
        ta.rows = 2;
        ta.placeholder = "标注你的疑问 / 想法 / 备注…（Enter 记录，Shift+Enter 换行）";
        const send = document.createElement("button");
        send.className = "send record";
        send.textContent = "记录";
        row.append(ta, send);
        panel.append(row);
        let busy = false;
        async function doSend() {
            const text = ta.value.trim();
            if (!text || busy) return;
            busy = true;
            send.disabled = true;
            const launch = epochKey(), tab = activeTab;
            const identityCurrent = () => epochKey() === launch;
            const panelCurrent = () => identityCurrent() && activeTab === tab && !panel.hidden && panel.contains(row);
            try {
                if (tab === "current") {
                    await ctx.api.postJSON("/api/replay/annotate", writeBody(pid, text));
                    if (!identityCurrent()) return;
                    ((data.threads ||= {})[pid] ||= []).push({ role: "user", content: text });
                } else {
                    const r = await ctx.api.postJSON(`/api/replay/snapshot/${encodeURIComponent(tab)}/annotate`, writeBody(pid, text));
                    if (!identityCurrent()) return;
                    if (snapTabs.has(tab)) snapTabs.set(tab, r && r.snapshot ? r.snapshot : r);
                    renderTabs();
                }
                if (panelCurrent()) {
                    thread.append(msgBubble("user", text));
                    ta.value = "";
                    updateBadges();
                }
                ctx.onChanged();
                /* 提交即缩回：标注已落库（线程/角标同源刷新由 onChanged 链负责），面板合拢、
                   分割线回到拖拽记住的比例（select(null) → render → syncPanelHook → 宿主 onPanel(false)）。 */
                if (panelCurrent()) select(null);
            } catch (err) {
                if (!panelCurrent()) return;
                const line = document.createElement("div");
                line.className = "err";
                line.textContent = `记录失败：${String(err)}`;
                thread.append(line);
            } finally {
                if (panelCurrent()) { busy = false; send.disabled = false; }
            }
        }
        send.addEventListener("click", doSend);
        ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doSend(); } });
        thread.scrollTop = thread.scrollHeight;
    }

    // ---------- 数据 ----------
    /* 宿主注入最新 points；元数据刷新保留编辑面板，清空/换身份始终重画并作废旧视图。
       快照标签页跟随最新 points.snapshots 刷新，不重开用户关闭的标签。 */
    function load(injected, opts) {
        data = injected || null;
        const identityChanged = syncTabsIdentity();
        if (!data) {
            tabsEpochs += 1;   // 清空＝重置：在途 openSnapshot 的旧回包此后一律失效
            snapTabs.clear();
            activeTab = "current";
            selected = null;
            panel.hidden = true;
            panel.innerHTML = "";
        } else if (identityChanged) {
            panel.hidden = true;   // 换身份：详情面板不许停在上一身份的条目上
            panel.innerHTML = "";
        }
        for (const id of [...snapTabs.keys()]) {
            const fresh = (data && data.snapshots || []).find((x) => x.snap_id === id);
            if (fresh) snapTabs.set(id, fresh);
        }
        renderTabs();
        if (!data || identityChanged || !opts || opts.redraw !== false) render();
        else { renderSkipNote(); updateBadges(); }
    }

    load(null); // 失败静默：复盘是旁路观测，不掀翻主路径
    /* refit：宿主在拖动分割线/弹层伸缩改变 svg 尺寸后显式调用（自动 ResizeObserver 仍只补
       初始落位——已落位后用户缩放/平移的视图不许被容器 resize 静默重置）。 */
    function refit() {
        if (!layout) return;
        fitView(layout.W, layout.H);
        applyView();
    }
    return { reload: load, openSnapshot, viewState, refit };
}
