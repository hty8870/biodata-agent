"use strict";

/* 选中/当前项目圆点的 WebGL 活点。
   shader 本体移植自 OpenShaders 参考（@kilogram，https://openshaders.com/@kilogram）：
   79 层域扭曲流场逐字保留、无后处理单 pass，
   唯一改动是把三个颜色常量（HUE/CHROMA/LIGHTNESS）换成 uniform，由项目色 a 经 OKLCH 映射喂入——
   背景 b = a 的深色映射，粒子/辉光 c = a 的亮色摆动，满足「b、c 皆从 a 得出」。
   架构：全页仅一个隐藏 WebGL2 上下文（96×96 离屏渲染），各圆点持 2D canvas 逐帧 drawImage 拷贝——
   规避每点一个 GL 上下文的浏览器上限；MutationObserver 跟随侧栏 innerHTML 重渲染自动挂/摘。
   命中的选择器列表必须与 app.css 的「WebGL 活点」共享块保持一致（双端各一份，改时同步）。 */

const TARGET_SELECTOR = ".workspace-nav-item.active .workspace-color-dot,"
    + ".workspace-color-swatch[aria-pressed=\"true\"],.kw-project-dot[aria-pressed=\"true\"]";
const SIZE = 96;                 // 离屏与各点 canvas 的设备像素边长（36px 圆点 × dpr2 余量充足）
const TAU = Math.PI * 2;

const VERTEX_SHADER = `#version 300 es
void main() {
  vec2 position = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2));
  gl_Position = vec4(position * 2.0 - 1.0, 0.0, 1.0);
}
`;

/* 参考 FIELD_SHADER：仅 HUE/CHROMA/LIGHTNESS 三个 const 改 uniform（uHue 单位=圈，shader 内乘 TAU），
   其余常量与 79 层循环原样。 */
const FIELD_SHADER = `#version 300 es
precision highp float;

uniform vec2 iResolution;
uniform float iTime;
uniform float uLightMode;
uniform vec3 uDarkBackground;
uniform vec3 uLightBackground;
uniform float uHue;
uniform float uChroma;
uniform float uLight;
out vec4 fragColor;

const float HUE_SPREAD = 0.0378805064;
const float HUE_TRAVEL = 1.4325037;
const float COLOUR_CYCLE = 0.145176128;
const float THETA = 2.1091249;
const float SHEAR = 0.958078027;
const float SHRINK = 0.94729352;
const float LAYERS = 79.0;
const float WARP_FREQ_X = 0.539533138;
const float WARP_FREQ_Y = 2.17205215;
const float WARP_AMP_X = 0.148749575;
const float WARP_AMP_Y = 0.0305007119;
const float ASPECT_X = 2.07888436;
const float ASPECT_Y = 0.159805536;
const float OFFSET_X = 0.362921625;
const float OFFSET_Y = 0.0453170389;
const float TILT = 0.988690376;
const float ZOOM = 1.07031608;
const float CENTRE_X = 0.225728095;
const float CENTRE_Y = 0.469466686;
const float GLOW_SIZE = 0.00132607028;
const float FALLOFF = 0.252465129;
const float VIGNETTE = 0.139108315;
const float FLOW_SPEED = 0.470689297;
const float FLOW_DIRECTION = 1.0;
const float BREATH_RATE = 0.550638378;
const float BREATH_AMOUNT = 0.0928965956;
const float PHASE = 28.4976501;
const float ECHO = 0.0;
const float ECHO_SHIFT = -0.143684164;
const float SOFTNESS = 0.00147855573;
const float LIGHT_SWING = 0.281192094;

const float TAU = 6.28318530718;

vec3 oklchToLinear(float L, float C, float h) {
  float a = C * cos(h), b = C * sin(h);
  float l_ = L + 0.3963377774 * a + 0.2158037573 * b;
  float m_ = L - 0.1055613458 * a - 0.0638541728 * b;
  float s_ = L - 0.0894841775 * a - 1.2914855480 * b;
  vec3 lms = vec3(l_, m_, s_);
  lms = lms * lms * lms;
  return mat3(4.0767416621, -1.2684380046, -0.0041960863,
              -3.3077115913, 2.6097574011, -0.7034186147,
              0.2309699292, -0.3413193965, 1.7076147010) * lms;
}

float blueNoise(vec2 p, float frame) {
  p += 5.588238 * mod(frame, 64.0);
  return fract(52.9829189 * fract(0.06711056 * p.x + 0.00583715 * p.y));
}

void main() {
  vec2 R = iResolution.xy;
  vec2 pos = (gl_FragCoord.xy - 0.5 * R) / R.y;
  float t = iTime * FLOW_SPEED * FLOW_DIRECTION + PHASE;
  float breath = (-sin(iTime * BREATH_RATE * 1.5) + sin(iTime * BREATH_RATE + 1.0)) * 0.25 + 0.5;

  vec2 u = (pos - vec2(CENTRE_X, CENTRE_Y)) * (ZOOM - breath * BREATH_AMOUNT);
  float ct = cos(TILT), st = sin(TILT);
  u = mat2(ct, st, -st, ct) * u;

  mat2 fold = mat2(cos(THETA), sin(THETA), -SHEAR, cos(THETA));

  float hue0 = uHue * TAU;
  float hue1 = hue0 + HUE_SPREAD * TAU;
  vec3 color = vec3(0.0);

  for (float i = 1.0; i <= 96.0; i += 1.0) {
    if (i > LAYERS) break;
    u.x += -sin(u.y * WARP_FREQ_X + t + i * 0.007) * WARP_AMP_X;
    u.y += -sin(u.x * WARP_FREQ_Y - t + i * 0.02) * WARP_AMP_Y;
    u = fold * u * SHRINK;

    vec2 q = u - vec2(OFFSET_X + breath * 0.1, OFFSET_Y);
    vec2 s = vec2(q.x * ASPECT_X, q.y * ASPECT_Y);
    float glow = GLOW_SIZE / (dot(s, s) + SOFTNESS);
#ifndef SKIP_ECHO
    vec2 e = vec2((q.x - ECHO_SHIFT) * ASPECT_X, s.y);
    glow += ECHO * GLOW_SIZE / (dot(e, e) + SOFTNESS);
#endif
    glow *= 0.25 + breath * 0.4;

    float r = length(u);
    float k = sin(i * COLOUR_CYCLE + t * 1.2 + r * HUE_TRAVEL) * 0.5 + 0.5;
    vec3 tint = clamp(oklchToLinear(uLight + LIGHT_SWING * k, uChroma * (0.75 + 0.35 * k), mix(hue0, hue1, k)), 0.0, 1.0);
    color += glow * tint * exp2(-r * FALLOFF);
  }

  vec3 x = max(color, 0.0);
  color = (x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14);
  color = pow(clamp(color, 0.0, 1.0), vec3(0.85, 0.92, 0.98));

  float edge = smoothstep(0.5, 1.6, length(pos));
  color *= 1.0 - edge * VIGNETTE;

  vec3 dark = uDarkBackground + color * (1.0 - uDarkBackground);
  float strength = max(color.r, max(color.g, color.b));
  vec3 light = uLightBackground * (1.0 - strength) + color * 0.96;
  color = mix(dark, light, uLightMode);

  color += (blueNoise(gl_FragCoord.xy, floor(iTime * 24.0)) - 0.5) / 255.0;
  fragColor = vec4(clamp(color, 0.0, 1.0), 1.0);
}
`;

/* —— 颜色映射：项目色 a → OKLCH（uHue/uChroma/uLight）与背景 b（uDarkBackground）—— */

function hexToRgb(hex) {
    const m = /^#([0-9a-f]{6})$/i.exec(String(hex || "").trim());
    if (!m) return null;
    return [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16));
}

function srgbToOklch([r8, g8, b8]) {
    const lin = (x) => { x /= 255; return x <= 0.04045 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); };
    const r = lin(r8), g = lin(g8), b = lin(b8);
    const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
    const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
    const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
    const L = 0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s;
    const A = 1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s;
    const B = 0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s;
    return [L, Math.hypot(A, B), Math.atan2(B, A)];
}

/* 注意 GLSL mat3(...) 是列主序构造：参考 shader 里 mat3(a..i)*v 的实际行是 (a,d,g)/(b,e,h)/(c,f,i)。
   JS 侧必须按行主序展开，下面的三行才是标准 OKLCH→linear sRGB。 */
function oklchToLinearSrgb(L, C, h) {
    const A = C * Math.cos(h), B = C * Math.sin(h);
    const l3 = Math.pow(L + 0.3963377774 * A + 0.2158037573 * B, 3);
    const m3 = Math.pow(L - 0.1055613458 * A - 0.0638541728 * B, 3);
    const s3 = Math.pow(L - 0.0894841775 * A - 1.2914855480 * B, 3);
    return [4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3,
        -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3,
        -0.0041960863 * l3 - 0.7034186147 * m3 + 1.7076147010 * s3];
}

function oklchToSrgb(L, C, h) {
    const gam = (x) => { x = Math.min(1, Math.max(0, x)); return x <= 0.0031308 ? 12.92 * x : 1.055 * Math.pow(x, 1 / 2.4) - 0.055; };
    return oklchToLinearSrgb(L, C, h).map(gam);
}

/* 低明度下 OKLCH 色域极窄，直接截断会把绿通道压负、色相滑成品红——二分降彩度直到落在 sRGB 色域内，
   保证背景 b 的色相与 a 一致。 */
function oklchToSrgbInGamut(L, C, h) {
    let c = C;
    for (let i = 0; i < 12 && oklchToLinearSrgb(L, c, h).some((x) => x < 0 || x > 1); i += 1) c *= 0.5;
    return oklchToSrgb(L, c, h);
}

/* a → shader 参数：色相直通、彩度保留灰度色的低彩度（防止灰蓝被硬拉成紫）、明度收窄摆动中心；
   背景 b = 同色相深映射（色域安全）。 */
function colorDef(hex) {
    const rgb = hexToRgb(hex);
    if (!rgb) return null;
    const [L, C, h] = srgbToOklch(rgb);
    return {
        key: hex.toLowerCase(),
        hue: (h / TAU + 1) % 1,
        chroma: Math.min(0.14, Math.max(0.02, C)),
        light: Math.min(0.62, Math.max(0.42, L)),
        bg: oklchToSrgbInGamut(Math.min(0.34, Math.max(0.2, L * 0.38)), Math.min(C, 0.12), h),
    };
}

/* —— 共享 GL 渲染器（单上下文；结构沿用参考 createShader，去掉主题插值）—— */

let _gl = null;      // { canvas, gl, field, fieldU }
let _glBroken = false;

function compile(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(shader) || "shader compile failed");
    return shader;
}

function program(gl, fragSource) {
    const prog = gl.createProgram();
    const vs = compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER), fs = compile(gl, gl.FRAGMENT_SHADER, fragSource);
    gl.attachShader(prog, vs); gl.attachShader(prog, fs);
    gl.deleteShader(vs); gl.deleteShader(fs);
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog) || "program link failed");
    return prog;
}

function ensureGl() {
    if (_gl || _glBroken) return _gl;
    try {
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = SIZE;
        const gl = canvas.getContext("webgl2", { alpha: false, antialias: false, depth: false, stencil: false });
        if (!gl) throw new Error("WebGL2 unavailable");
        const field = program(gl, FIELD_SHADER);
        const locs = (prog, names) => Object.fromEntries(names.map((n) => [n, gl.getUniformLocation(prog, n)]));
        const fieldU = locs(field, ["iResolution", "iTime", "uLightMode", "uDarkBackground", "uLightBackground", "uHue", "uChroma", "uLight"]);
        _gl = { canvas, gl, field, fieldU };
    } catch (_e) {
        _glBroken = true;   // 回退：圆点保持 CSS 纯色填充
    }
    return _gl;
}

function renderColor(def, time) {
    const { gl, field, fieldU, canvas } = _gl;
    gl.viewport(0, 0, SIZE, SIZE);
    gl.useProgram(field);
    gl.uniform2f(fieldU.iResolution, SIZE, SIZE);
    gl.uniform1f(fieldU.iTime, time);
    gl.uniform1f(fieldU.uLightMode, 0);
    gl.uniform3fv(fieldU.uDarkBackground, def.bg);
    gl.uniform3fv(fieldU.uLightBackground, def.bg);
    gl.uniform1f(fieldU.uHue, def.hue);
    gl.uniform1f(fieldU.uChroma, def.chroma);
    gl.uniform1f(fieldU.uLight, def.light);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    return canvas;
}

/* —— 圆点挂载管理 —— */

const _dots = new Map();   // el -> { canvas, ctx, key }
let _observer = null;
let _syncQueued = false;
let _started = false;

function readColor(el) {
    const inline = el.style.getPropertyValue("--project-color") || el.style.getPropertyValue("--kw-project-color");
    if (inline) return inline.trim();
    const computed = getComputedStyle(el);
    return (computed.getPropertyValue("--project-color") || computed.getPropertyValue("--kw-project-color")).trim();
}

function attach(el) {
    let rec = _dots.get(el);
    if (rec && rec.canvas.isConnected) return rec;
    const canvas = document.createElement("canvas");
    canvas.className = "live-dot-canvas";
    canvas.width = canvas.height = SIZE;
    canvas.setAttribute("aria-hidden", "true");
    // 带文字标签的圆点（如知识轨序号点）：文字收进 .live-dot-label 抬到 canvas 之上
    const labelNodes = Array.from(el.childNodes).filter((n) => n.nodeType !== Node.ELEMENT_NODE || !n.classList.contains("live-dot-canvas"));
    if (labelNodes.length) {
        const label = document.createElement("span");
        label.className = "live-dot-label";
        labelNodes.forEach((n) => label.appendChild(n));
        el.appendChild(label);
    }
    el.appendChild(canvas);
    rec = { canvas, ctx: canvas.getContext("2d"), key: "" };
    _dots.set(el, rec);
    return rec;
}

function sync() {
    _syncQueued = false;
    const targets = document.querySelectorAll(TARGET_SELECTOR);
    const live = new Set();
    targets.forEach((el) => {
        const rec = attach(el);
        rec.key = readColor(el) || rec.key;
        live.add(el);
    });
    for (const [el, rec] of Array.from(_dots)) {
        if (!live.has(el) || !el.isConnected) { rec.canvas.remove(); _dots.delete(el); }
    }
    if (_dots.size && ensureGl()) scheduleFrame();
}

function queueSync() {
    if (_syncQueued) return;
    _syncQueued = true;
    queueMicrotask(sync);
}

let _raf = 0;
const _startTime = performance.now();
const _still = window.matchMedia("(prefers-reduced-motion: reduce)");

function frame(now) {
    _raf = 0;
    if (!_dots.size || !_gl || document.hidden) return;   // 隐藏页签静默；visibilitychange/sync 负责唤醒
    const time = (now - _startTime) / 1000;
    const groups = new Map();
    for (const rec of _dots.values()) {
        if (!rec.key) continue;
        if (!groups.has(rec.key)) groups.set(rec.key, []);
        groups.get(rec.key).push(rec);
    }
    for (const [key, recs] of groups) {
        const def = colorDef(key);
        if (!def) continue;
        const source = renderColor(def, time);
        for (const rec of recs) {
            rec.ctx.clearRect(0, 0, SIZE, SIZE);
            rec.ctx.drawImage(source, 0, 0);
        }
    }
    if (!_still.matches) scheduleFrame();
}

function scheduleFrame() {
    if (!_raf && _dots.size && _gl) _raf = requestAnimationFrame(frame);
}

/* 调试探针（测试与排障用）：返回当前挂载圆点、解析出的颜色参数与一次受控渲染的 GL 像素读回。 */
export function __liveDotProbe() {
    const keys = [...new Set([..._dots.values()].map((r) => r.key))];
    const out = { glBroken: _glBroken, dots: _dots.size, keys, defs: keys.map((k) => colorDef(k)) };
    if (_gl && keys.length) {
        renderColor(colorDef(keys[0]), 12.0);
        const px = new Uint8Array(4);
        _gl.gl.readPixels(48, 60, 1, 1, _gl.gl.RGBA, _gl.gl.UNSIGNED_BYTE, px);
        out.glPixel = Array.from(px);
    }
    return out;
}

export function initLiveDots() {
    if (_started) return;
    _started = true;
    if (!ensureGl()) return;   // WebGL2 不可用：整体保持 CSS 纯色回退
    _observer = new MutationObserver(queueSync);
    _observer.observe(document.body, { subtree: true, childList: true, attributes: true, attributeFilter: ["class", "aria-pressed", "style"] });
    _still.addEventListener("change", sync);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) scheduleFrame(); });
    sync();
}
