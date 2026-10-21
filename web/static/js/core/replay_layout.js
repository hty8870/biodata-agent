/** 决策因果图布局（纯函数零 DOM）。移植自内部工具「复盘器」web/js/layout.js（2026-09-28 复盘批），逻辑逐位保留；改版时两边同步。 */
// 因果图谱分层 DAG 布局（纯函数，零 DOM）。
// 输入：digest 数据（{motivation, outputs[], knowledge[], decisions[], edges[], from}；
//       节点带 pid/title/turn_refs，边带 eid/from/to/label）。
// 输出：{nodes, edges, byId, timeLabels, tMap, layerOf, crossings, nLanes, W, H}；节点带
//       x/y/w/h/label（label=显示文本，宽度唯一真源），边带 route（{kind:'bezier'|'lane',
//       points?, laneX?}，渲染与碰撞测量的唯一依据）。
//
// 布局哲学：
// - 时间轴即纵轴，必须诚实：逆向边（t(from) > t(to)）不参与定层约束，
//   节点不被推离真实时间秩；逆向边画成右侧车道的上指虚线弧（「回看/修正」一眼可辨）。
// - 跨层长边（span≥2）走右侧竖直车道（正交 5 段路由），不穿中间层节点带。
//   每条车道独立 x，竖直段互不相交；水平段走在层间空隙（不压节点）。
// - 层内排序用收敛式 barycenter：保底 6 轮，之后连续 3 轮交叉数无改进才停（上限 30），
//   全程保留历史最优序。

export const LAYER_H = 112;
export const GAP_X = 26;
export const NODE_H = 46;
export const PAD = 30;
export const GUTTER = 58;
const LANE_GAP = 16, LANE_OFF = 24;
const TYPE_ORDER = { start: 0, knowledge: 1, decisions: 2, outputs: 3 };

// 12px 字下的逐字宽估算：CJK/全角 ≈12.2，ASCII ≈6.8，其余 ≈9。
// 按 title.length 一刀切会让 CJK 略虚宽、ASCII 严重虚宽（6.8 被当 13），画布被无谓撑大。
function textWidth(s) {
  let w = 0;
  for (const ch of s || '') {
    const cp = ch.codePointAt(0);
    w += cp > 0x2e7f ? 12.2 : (cp < 0x80 ? 6.8 : 9);
  }
  return w;
}

// 显示文本：超 13 字截断加省略号（宽度按显示文本算，不按全名）。
export function displayTitle(title) {
  const t = title || '（无题）';
  return t.length > 13 ? t.slice(0, 12) + '…' : t;
}

// 相邻层缝的交叉计数：对所有缝 (l, l+1)，取跨越该缝的边，
// 两条边在缝上下端的 x 顺序不一致即交叉（Gansner 式分层图标准做法）。
// 只统计 bezier 短边（lane 边水平段在层间空隙走，与节点无关）。
export function countCrossings(nodes, edges, layerOf, posX) {
  const maxLayer = Math.max(0, ...Object.values(layerOf));
  let total = 0;
  for (let l = 0; l < maxLayer; l++) {
    const segs = [];
    for (const e of edges) {
      if (e.route?.kind === 'lane') continue;
      const lf = layerOf[e.from], lt = layerOf[e.to];
      if (lf <= l && lt > l) segs.push([posX[e.from], posX[e.to]]);
      else if (lt <= l && lf > l) segs.push([posX[e.to], posX[e.from]]);
    }
    for (let i = 0; i < segs.length; i++)
      for (let j = i + 1; j < segs.length; j++) {
        const [a1, b1] = segs[i], [a2, b2] = segs[j];
        if ((a1 - a2) * (b1 - b2) < 0) total++;
      }
  }
  return total;
}

export function computeLayout(data) {
  const dg = data?.digest;
  if (!dg) return null;
  const nodes = [];
  if (dg.motivation) nodes.push({ pid: 'start', type: 'start', ...dg.motivation });
  for (const p of dg.knowledge || []) nodes.push({ ...p, type: 'knowledge' });
  for (const p of dg.decisions || []) nodes.push({ ...p, type: 'decisions' });
  for (const p of dg.outputs || []) nodes.push({ ...p, type: 'outputs' });
  const byId = Object.fromEntries(nodes.map(n => [n.pid, n]));
  let edges = (dg.edges || []).filter(e => byId[e.from] && byId[e.to]);
  // 平行重边去重（同 from→to 只留第一条）；浅拷贝——布局会给边写 backward/route/laneX，
  // 不得污染调用方持有的 digest 数据。
  const seenPair = new Set();
  edges = edges.filter(e => {
    const k = e.from + '>' + e.to;
    if (seenPair.has(k)) return false;
    seenPair.add(k);
    return true;
  }).map(e => ({ ...e }));
  if (!nodes.length) return null;

  for (const n of nodes) {
    n.label = displayTitle(n.title);
    n.w = Math.max(84, Math.min(220, textWidth(n.label) + 30));
    n.h = NODE_H;
  }

  // ---- 时序：t(n)=min(turn_refs)，缺引用者用前驱时间兜底、再缺用范围起点 ----
  const scopeFrom = dg.from ?? 0;
  const predsOf = pid => edges.filter(e => e.to === pid).map(e => e.from);
  const tMap = {};
  for (const n of nodes) {
    const refs = (n.turn_refs || []).filter(Number.isInteger);
    tMap[n.pid] = refs.length ? Math.min(...refs) : null;
  }
  for (let pass = 0; pass < nodes.length; pass++) {
    let changed = false;
    for (const n of nodes) if (tMap[n.pid] == null) {
      const ps = predsOf(n.pid).map(p => tMap[p]).filter(t => t != null);
      if (ps.length) { tMap[n.pid] = Math.max(...ps); changed = true; }
    }
    if (!changed) break;
  }
  for (const n of nodes) if (tMap[n.pid] == null) tMap[n.pid] = scopeFrom;

  // 逆向边（t(from) > t(to)）：画成向下箭头、或把节点推离时间秩，两者都是撒谎。
  // 故它们不参与定层，渲染为上指虚线车道弧。
  for (const e of edges) e.backward = tMap[e.from] > tMap[e.to];

  // ---- 定层：基线=时间带的秩；只有顺向边的因果约束把节点往下推 ----
  const layerOf = {};
  const fwdPredsOf = pid => edges.filter(e => e.to === pid && !e.backward).map(e => e.from);
  const distinctT = [...new Set(nodes.map(n => tMap[n.pid]))].sort((a, b) => a - b);
  if (!edges.length && distinctT.length <= 1) {
    // 无边且全无时间信息：按类型分行
    for (const n of nodes) layerOf[n.pid] = TYPE_ORDER[n.type];
  } else {
    const rankOfT = Object.fromEntries(distinctT.map((t, i) => [t, i]));
    // Kahn 拓扑定层（只用顺向边；有环时按时间秩最小者先定，层数受最长简单路径约束）。
    const remaining = new Set(nodes.map(n => n.pid));
    const settle = (pid) => {
      const settled = fwdPredsOf(pid).filter(p => p !== pid && !remaining.has(p));
      layerOf[pid] = Math.max(rankOfT[tMap[pid]], 0, ...settled.map(p => layerOf[p] + 1));
      remaining.delete(pid);
    };
    while (remaining.size) {
      let progressed = false;
      for (const pid of [...remaining]) {
        if (!fwdPredsOf(pid).some(p => p !== pid && remaining.has(p))) {
          settle(pid);
          progressed = true;
        }
      }
      if (!progressed) {
        const pid = [...remaining].sort((a, b) => rankOfT[tMap[a]] - rankOfT[tMap[b]])[0];
        settle(pid); // 破环：环内时间秩最小者先落位
      }
    }
  }
  const maxLayer = Math.max(...Object.values(layerOf));
  const layers = Array.from({ length: maxLayer + 1 }, () => []);
  for (const n of nodes) layers[layerOf[n.pid]].push(n);

  // ---- 层内排序：收敛式 barycenter（保底 6 轮 + 耐心 3 轮，留最优） ----
  const posOf = l => Object.fromEntries(layers[l].map((n, i) => [n.pid, i]));
  const curCrossings = () => {
    const px = {};
    for (let l = 0; l <= maxLayer; l++) for (const [pid, i] of Object.entries(posOf(l))) px[pid] = i;
    return countCrossings(nodes, edges, layerOf, px);
  };
  const bary = (n, neigh) => {
    const ps = neigh(n).filter(m => m != null);
    return ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : null;
  };
  let best = layers.map(l => [...l]), bestC = curCrossings(), stale = 0;
  for (let sweep = 0; sweep < 30 && (sweep < 6 || stale < 3); sweep++) {
    const down = sweep % 2 === 0;
    const seq = down ? [...Array(maxLayer + 1).keys()].slice(1) : [...Array(maxLayer).keys()].reverse();
    for (const l of seq) {
      const ref = down ? posOf(l - 1) : posOf(l + 1);
      const neigh = down
        ? n => edges.filter(e => e.to === n.pid && !e.backward && layerOf[e.from] === l - 1).map(e => ref[e.from])
        : n => edges.filter(e => e.from === n.pid && !e.backward && layerOf[e.to] === l + 1).map(e => ref[e.to]);
      const keyed = layers[l].map(n => [bary(n, neigh), layers[l].indexOf(n), n]);
      keyed.sort((a, b) => (a[0] == null ? 1e9 : a[0]) - (b[0] == null ? 1e9 : b[0]) || a[1] - b[1]);
      layers[l] = keyed.map(k => k[2]);
    }
    const c = curCrossings();
    if (c < bestC) { bestC = c; best = layers.map(l => [...l]); stale = 0; }
    else stale++;
  }
  for (let l = 0; l <= maxLayer; l++) layers[l] = best[l];

  // ---- 坐标：层内累计宽度，整体居中于最宽层；左侧留时间刻度槽 ----
  const widths = layers.map(l => l.reduce((a, n) => a + n.w + GAP_X, -GAP_X));
  const maxW = Math.max(1, ...widths);
  for (let l = 0; l <= maxLayer; l++) {
    let x = GUTTER + (maxW - widths[l]) / 2;
    for (const n of layers[l]) {
      n.x = x + n.w / 2; n.y = PAD + l * LAYER_H + NODE_H / 2;
      x += n.w + GAP_X;
    }
  }

  // ---- 车道分配：上指边（逆向/破环，span≤0）+ 跨层顺向边（span≥2）走右侧竖直车道 ----
  // 几何上指的一律按「回看」处理：定层后才看得出破环边（同刻成环），与逆向边同等诚实渲染。
  // 车道区间复用（区间图贪心着色）：竖直段不占同一空隙区间的边共用车道，压缩画布宽度。
  for (const e of edges) e.backward = e.backward || layerOf[e.to] <= layerOf[e.from];
  const laneEdges = edges.filter(e => e.backward || layerOf[e.to] - layerOf[e.from] >= 2);
  laneEdges.sort((a, b) =>
    Math.min(layerOf[a.from], layerOf[a.to]) - Math.min(layerOf[b.from], layerOf[b.to]) ||
    (Math.abs(layerOf[b.to] - layerOf[b.from]) - Math.abs(layerOf[a.to] - layerOf[a.from])));
  const laneBase = GUTTER + maxW + LANE_OFF;
  const laneEnd = []; // 每条车道已占用到的最大空隙序号
  for (const e of laneEdges) {
    const g0 = Math.min(layerOf[e.from], layerOf[e.to]) - 1; // 保守含上下空隙
    const g1 = Math.max(layerOf[e.from], layerOf[e.to]);
    let lane = laneEnd.findIndex(end => end < g0);
    if (lane < 0) { lane = laneEnd.length; laneEnd.push(-1); }
    laneEnd[lane] = g1;
    e.laneX = laneBase + lane * LANE_GAP;
  }
  const laneW = laneEnd.length ? LANE_OFF + laneEnd.length * LANE_GAP : 0;

  // 路由计算（渲染/测量唯一依据）。层间空隙中线的 y：
  const seamY = l => PAD + l * LAYER_H + NODE_H + (LAYER_H - NODE_H) / 2; // 层 l 与其下一层之间的空隙中线
  for (const e of edges) {
    const a = byId[e.from], b = byId[e.to];
    if (e.route) continue;
    if (e.backward) {
      // 上指虚线：a 顶 → 上方空隙 → 车道 → b 层下方空隙 → b 底（箭头向上进底）
      const y1 = a.y - a.h / 2, y2 = b.y + b.h / 2;
      const chA = a.y - a.h / 2 - (LAYER_H - NODE_H) / 4;   // a 上方空隙
      const chB = b.y + b.h / 2 + (LAYER_H - NODE_H) / 4;   // b 下方空隙
      e.route = { kind: 'lane', points: [[a.x, y1], [a.x, chA], [e.laneX, chA], [e.laneX, chB], [b.x, chB], [b.x, y2]] };
    } else {
      const span = layerOf[e.to] - layerOf[e.from];
      if (span >= 2) {
        // 长边：a 底 → 下方空隙 → 车道 → b 上方空隙 → b 顶（箭头向下进顶）
        const y1 = a.y + a.h / 2, y2 = b.y - b.h / 2;
        e.route = { kind: 'lane', points: [[a.x, y1], [a.x, seamY(layerOf[e.from])], [e.laneX, seamY(layerOf[e.from])], [e.laneX, seamY(layerOf[e.to] - 1)], [b.x, seamY(layerOf[e.to] - 1)], [b.x, y2]] };
      } else {
        e.route = { kind: 'bezier' };
      }
    }
  }

  // 时间刻度：每层取其最早时间，与上行不同才标注（含一条淡横线）
  const timeLabels = [];
  let lastT = null;
  for (let l = 0; l <= maxLayer; l++) {
    const ts = layers[l].map(n => tMap[n.pid]).filter(t => t != null);
    if (!ts.length) continue;
    const t = Math.min(...ts);
    if (t === lastT) continue;
    lastT = t;
    timeLabels.push({ y: PAD + l * LAYER_H, text: `#${t}` });
  }
  const posX = Object.fromEntries(nodes.map(n => [n.pid, n.x]));
  return {
    nodes, edges, byId, timeLabels, tMap, layerOf,
    crossings: countCrossings(nodes, edges, layerOf, posX),
    nLanes: laneEnd.length,
    W: GUTTER + maxW + laneW + PAD * 2, H: PAD * 2 + (maxLayer + 1) * LAYER_H,
  };
}
