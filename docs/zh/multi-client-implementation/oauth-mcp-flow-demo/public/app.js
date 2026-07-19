/*
 * app.js — 动画引擎 + 时序图渲染 + 详情面板 + Demo/Live 两种模式的控制。
 *
 * 核心概念：
 *   - goToStep(n)  跳到第 n 步：更新时序图高亮、飞一个 packet、渲染右侧 request/response 详情。
 *   - Demo 模式    用 steps.js 里烘焙好的数据回放，纯前端，随时可跑。
 *   - Live 模式    调后端 server.py 真正访问 MCP server（含浏览器登录），把真实抓包并入动画。
 */

const { CFG } = window.FLOWS;
const $ = (s) => document.querySelector(s);
const SVGNS = "http://www.w3.org/2000/svg";
const DIR_COLOR = { req: "#5b9dff", res: "#38d39f", note: "#f5b64a" };

// 当前激活的流程（mcpproxy / mcp）——泳道、步骤、Live 区间都随它切换
let LANES, STEPS, TOTAL, laneIndex, FLOW;
function applyFlow(key) {
  FLOW = window.FLOWS[key];
  LANES = FLOW.lanes;
  STEPS = FLOW.steps;
  TOTAL = STEPS.length;
  laneIndex = Object.fromEntries(LANES.map((l, i) => [l.key, i]));
}

const state = {
  mode: "demo",
  flow: "mcpproxy",
  cur: 0,             // 0 = 未开始
  playing: false,
  speed: 1,
  animating: false,
  playTimer: null,
  live: { phase: "idle", data: {}, authorizeUrl: null, poll: null },
  rows: {},           // n -> {x1,x2,y,dir,group,packet,noteEl}
};
applyFlow(state.flow);

// ── DOM ─────────────────────────────────────────────────────────────
const svg = $("#diagram");
const scroll = $("#diagramScroll");
const wire = $("#wire");
const placeholder = $("#placeholder");
const badge = $("#stepBadge");
const titleEl = $("#stepTitle");
const descEl = $("#stepDesc");
const dirEl = $("#stepDir");
const hlEl = $("#highlights");
const progressBar = $("#progressBar");
const livePanel = $("#livePanel");

// ── 小工具 ──────────────────────────────────────────────────────────
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const el = (tag, attrs = {}) => {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};

// JSON 语法高亮（输入是已 pretty 的 JSON 字符串）
function highlightJson(str) {
  return esc(str).replace(
    /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false)\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?)/g,
    (m) => {
      let cls = "tok-num";
      if (/^"/.test(m)) cls = /:$/.test(m) ? "tok-key" : "tok-str";
      else if (/true|false/.test(m)) cls = "tok-bool";
      else if (/null/.test(m)) cls = "tok-null";
      return `<span class="${cls}">${m}</span>`;
    }
  );
}

// 渲染一组 query / form 参数（支持 stripped / added 标记）
function renderParams(list, cls) {
  return `<div class="qparams">${list
    .map(([k, v, flag]) => {
      const c = flag === "stripped" ? "qp stripped" : flag === "added" ? "qp added" : "qp";
      return `<span class="${c}"><span class="qk">${esc(k)}</span>=<span class="qv">${esc(v)}</span></span>`;
    })
    .join("")}</div>`;
}

function renderHeaders(headers) {
  const keys = Object.keys(headers || {});
  if (!keys.length) return "";
  return `<ul class="hdrs">${keys
    .map((k) => {
      const v = headers[k];
      if (v && typeof v === "object" && (v.urlBase || v.query)) {
        // e.g. Location 重定向
        return `<li><span class="hk">${esc(k)}</span>: <span class="hv">${esc(v.urlBase)}</span>${
          v.query ? renderParams(v.query) : ""
        }</li>`;
      }
      return `<li><span class="hk">${esc(k)}</span>: <span class="hv">${esc(v)}</span></li>`;
    })
    .join("")}</ul>`;
}

// ── 详情卡片渲染 ────────────────────────────────────────────────────
function requestCard(req, tag) {
  let line;
  if (req.urlBase) {
    line = `<div class="reqline"><span class="m">${req.method}</span> ${esc(req.urlBase)}<span class="muted">?</span></div>${renderParams(req.query || [])}`;
  } else {
    line = `<div class="reqline"><span class="m">${req.method}</span> ${esc(req.url)}</div>`;
  }
  let body = "";
  if (req.form) {
    body = `<div class="section-label">form body</div>${renderParams(req.form)}`;
  } else if (req.body) {
    body = `<div class="section-label">body</div><pre class="body-block">${highlightJson(req.body)}</pre>`;
  }
  return `<div class="msgcard req">
    <div class="msgcard-head"><span class="dot"></span> Request ${tag || ""}</div>
    <div class="msgcard-body">${line}${renderHeaders(req.headers)}${body}</div>
  </div>`;
}

function responseCard(res, tag) {
  const cls = res.status >= 400 ? "err" : res.status >= 300 ? "redir" : "ok";
  let body = "";
  if (res.body) body = `<div class="section-label">body</div><pre class="body-block">${highlightJson(res.body)}</pre>`;
  let decoded = "";
  if (res.decoded) {
    decoded = `<div class="decoded"><div class="dc-title">🔎 ${esc(res.decoded.title)}</div><pre class="body-block">${highlightJson(
      JSON.stringify(res.decoded.claims, null, 2)
    )}</pre></div>`;
  }
  return `<div class="msgcard res">
    <div class="msgcard-head"><span class="dot"></span> Response ${tag || ""}
      <span class="status-pill ${cls}">${res.status} ${esc(res.statusText || "")}</span></div>
    <div class="msgcard-body">${renderHeaders(res.headers)}${body}</div>
  </div>${decoded}`;
}

function noteCard(note) {
  return `<div class="note-card"><div class="nc-title">💡 ${esc(note.title)}</div>${note.lines
    .map((l, i) => `<div class="nc-line ${i === note.lines.length - 1 ? "muted" : ""}">${esc(l)}</div>`)
    .join("")}</div>`;
}
function actorNoteCard(an) {
  return `<div class="actor-note"><div class="an-title">⚙️ ${esc(an.title)}</div>${an.lines
    .map((l) => `<div class="an-line">${esc(l)}</div>`)
    .join("")}</div>`;
}

// 取第 n 步的“有效数据”（Live 优先真实抓包，否则回退示例）
function effectiveData(n) {
  const meta = STEPS[n - 1];
  if (state.mode === "live") {
    const live = state.live.data[n];
    if (live) return { meta, request: live.request, response: live.response, tag: "🟢 LIVE" };
    return { meta, request: meta.request, response: meta.response, tag: meta.dir === "note" ? "" : "示例" };
  }
  return { meta, request: meta.request, response: meta.response, tag: "" };
}

function renderDetail(n) {
  const { meta, request, response, tag } = effectiveData(n);
  badge.textContent = `${n} / ${TOTAL}`;
  titleEl.textContent = meta.title;
  descEl.textContent = meta.desc;
  dirEl.className = `step-dir ${meta.dir}`;
  dirEl.textContent = meta.dir === "req" ? "请求" : meta.dir === "res" ? "响应" : "交互";

  // highlights
  hlEl.innerHTML = (meta.highlights || [])
    .map((h) => {
      const ic = { removed: "✕", added: "+", "removed-preview": "⚠", diff: "⇄", clean: "✓" }[h.type] || "★";
      return `<div class="hl ${h.type}"><span class="hl-ic">${ic}</span><span>${esc(h.text)}</span></div>`;
    })
    .join("");

  // wire
  const tagHtml = tag ? `<span class="status-pill">${tag}</span>` : "";
  let html = "";
  if (request) html += requestCard(request, tagHtml);
  if (response) html += responseCard(response, request ? "" : tagHtml);
  if (meta.note) html += noteCard(meta.note);
  if (meta.actorNote) html += actorNoteCard(meta.actorNote);
  if (!html) html = `<div class="placeholder"><div class="placeholder-inner"><p class="muted">（该步在 Live 模式下尚未抓取，切到 Demo 或运行到该步查看真实数据）</p></div></div>`;
  wire.innerHTML = html;
  placeholder.remove?.();
}

// ── 时序图构建 ──────────────────────────────────────────────────────
function buildDiagram() {
  svg.innerHTML = "";
  state.rows = {};
  const W = Math.max(scroll.clientWidth - 12, 320);
  const headH = 66, topPad = 14, rowH = 46, botPad = 30;
  const leftPad = 64, rightPad = 34;
  const gap = (W - leftPad - rightPad) / (LANES.length - 1);
  const laneX = LANES.map((_, i) => leftPad + i * gap);
  const H = headH + topPad + TOTAL * rowH + botPad;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("height", H);

  // lifelines + heads
  LANES.forEach((lane, i) => {
    const x = laneX[i];
    svg.appendChild(el("line", { class: "lifeline", x1: x, y1: headH, x2: x, y2: H - 10 }));
    const boxW = Math.min(gap - 8, 120);
    const box = el("rect", { class: "lane-head-box", x: x - boxW / 2, y: 8, width: boxW, height: 46, rx: 9 });
    svg.appendChild(box);
    const t1 = el("text", { class: "lane-head-label", x, y: 28, "text-anchor": "middle" });
    t1.textContent = `${lane.icon} ${lane.label}`;
    // 截断过长 label
    if (lane.label.length > 9) t1.textContent = `${lane.icon} ${lane.label}`;
    svg.appendChild(t1);
    const t2 = el("text", { class: "lane-head-sub", x, y: 43, "text-anchor": "middle" });
    t2.textContent = lane.sub.length > 22 ? lane.sub.slice(0, 21) + "…" : lane.sub;
    svg.appendChild(t2);
  });

  // arrows
  STEPS.forEach((step, idx) => {
    const y = headH + topPad + idx * rowH + 24;
    const x1 = laneX[laneIndex[step.from]];
    const x2 = laneX[laneIndex[step.to]];
    const color = DIR_COLOR[step.dir];
    const g = el("g", { class: "arrow", "data-n": step.n });

    if (step.dir === "note") {
      // 虚线双向（浏览器 ↔ Entra），不带 packet
      const line = el("line", { class: "arrow-line", x1, y1: y, x2, y2: y, stroke: color, "stroke-dasharray": "5 4" });
      g.appendChild(line);
    } else {
      const line = el("line", { class: "arrow-line", x1, y1: y, x2, y2: y, stroke: color });
      g.appendChild(line);
      // arrowhead
      const dir = x2 > x1 ? 1 : -1;
      const hx = x2 - dir * 2;
      const head = el("polygon", {
        class: "arrow-head",
        points: `${hx},${y} ${hx - dir * 8},${y - 4} ${hx - dir * 8},${y + 4}`,
        fill: color,
      });
      g.appendChild(head);
    }

    // 标签
    const midX = (x1 + x2) / 2;
    const label = el("text", { class: "arrow-label", x: midX, y: y - 6, "text-anchor": "middle", fill: color });
    label.textContent = arrowLabel(step);
    g.appendChild(label);

    // 序号徽标（源端）
    const nx = x1 + (x2 > x1 ? 1 : -1) * 9;
    g.appendChild(el("circle", { class: "arrow-num-bg", cx: nx, cy: y, r: 8, fill: color }));
    const numT = el("text", { class: "arrow-num", x: nx, y: y + 3, "text-anchor": "middle" });
    numT.textContent = step.n;
    g.appendChild(numT);

    // packet（隐藏，激活时飞）
    let packet = null;
    if (step.dir !== "note") {
      packet = el("circle", { class: "packet", cx: x1, cy: y, r: 5, fill: color, opacity: 0, style: `color:${color}` });
      g.appendChild(packet);
    }
    svg.appendChild(g);
    state.rows[step.n] = { x1, x2, y, dir: step.dir, group: g, packet };
  });
}

function arrowLabel(step) {
  return step.label || step.title;
}

// packet 飞行动画；resolve 于到达时
function flyPacket(n) {
  return new Promise((resolve) => {
    const row = state.rows[n];
    if (!row || !row.packet) return resolve();
    const { packet, x1, x2 } = row;
    const dur = 620 / state.speed;
    const t0 = performance.now();
    packet.setAttribute("opacity", "1");
    function frame(t) {
      const p = Math.min(1, (t - t0) / dur);
      const e = p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2; // easeInOut
      packet.setAttribute("cx", x1 + (x2 - x1) * e);
      packet.setAttribute("r", 5 + Math.sin(p * Math.PI) * 2.5);
      if (p < 1) requestAnimationFrame(frame);
      else {
        packet.setAttribute("opacity", "0");
        packet.setAttribute("cx", x1);
        resolve();
      }
    }
    requestAnimationFrame(frame);
  });
}

// ── 跳转到某一步 ────────────────────────────────────────────────────
async function goToStep(n, { animate = true } = {}) {
  n = Math.max(1, Math.min(TOTAL, n));
  state.cur = n;
  // 图上高亮
  for (let i = 1; i <= TOTAL; i++) {
    const g = state.rows[i]?.group;
    if (!g) continue;
    g.classList.toggle("done", i < n);
    g.classList.toggle("active", i === n);
    const lbl = g.querySelector(".arrow-label");
    lbl && lbl.classList.toggle("active", i === n);
  }
  scrollRowIntoView(n);
  renderDetail(n);
  updateControls();
  if (animate) await flyPacket(n);
}

function scrollRowIntoView(n) {
  const row = state.rows[n];
  if (!row) return;
  const svgH = parseFloat(svg.getAttribute("height"));
  const ratio = scroll.querySelector("svg").clientHeight / svgH || 1;
  const yPix = row.y * ratio;
  const target = yPix - scroll.clientHeight / 2;
  scroll.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
}

// ── 控制条 ──────────────────────────────────────────────────────────
function updateControls() {
  $("#btnPrev").disabled = state.cur <= 1;
  $("#btnNext").disabled = state.cur >= TOTAL && !state.playing;
  $("#btnPlay").textContent = state.playing ? "⏸ 暂停" : state.cur === 0 ? "▶ 播放" : state.cur >= TOTAL ? "⟲ 重看" : "▶ 继续";
  progressBar.style.width = `${(state.cur / TOTAL) * 100}%`;
}

async function next() {
  if (state.cur >= TOTAL) return;
  await goToStep(state.cur + 1);
}
async function prev() {
  if (state.cur <= 1) return;
  await goToStep(state.cur - 1, { animate: false });
}

async function play() {
  if (state.playing) { pause(); return; }
  if (state.cur >= TOTAL) await goToStep(1);
  else if (state.cur === 0) await goToStep(1);
  state.playing = true;
  updateControls();
  loop();
}
function pause() {
  state.playing = false;
  clearTimeout(state.playTimer);
  updateControls();
}
function loop() {
  if (!state.playing) return;
  const dwell = 1500 / state.speed;
  state.playTimer = setTimeout(async () => {
    if (!state.playing) return;
    if (state.cur >= TOTAL) { pause(); return; }
    await goToStep(state.cur + 1);
    loop();
  }, dwell);
}

function restart() {
  pause();
  state.cur = 0;
  buildDiagram();
  badge.textContent = `0 / ${TOTAL}`;
  titleEl.textContent = "准备就绪";
  descEl.textContent = "点击 ▶ 播放 或 下一步，从“无 token 撞 401”开始，一步步走完整条授权流。";
  dirEl.textContent = "";
  hlEl.innerHTML = "";
  wire.innerHTML = `<div class="placeholder"><div class="placeholder-inner">
      <div class="pl-icon">🎬</div>
      <p>这条流水线在 Client、代理路由、Entra、MCP 端点四者之间传递消息。</p>
      <p class="muted">Demo 模式用贴近真实抓包的数据回放；Live 模式会真正调用你的 MCP server（含浏览器登录）。</p>
    </div></div>`;
  updateControls();
}

// ── Live 模式 ───────────────────────────────────────────────────────
function renderLivePanel() {
  if (state.mode !== "live") { livePanel.classList.add("hidden"); return; }
  livePanel.classList.remove("hidden");
  const p = state.live.phase;
  const authLabel = state.flow === "mcpproxy"
    ? "② 构造 /authorize（代理删 resource）"
    : "② 构造 Entra /authorize（直连 · 无 resource）";
  livePanel.innerHTML = `
    <h3>🔴 Live · 真正调用你的 MCP server <span class="live-flowtag">${esc(FLOW.name)}</span></h3>
    <p>会真实访问 <code>${esc(CFG.base)}</code>。第 ①/② 步无需登录（发现是公开 GET）；第 ③ 步会打开浏览器到 Entra 真实登录，成功后自动抓取“换 token + tools/list”。</p>
    <div class="live-actions">
      <button id="liveDiscover" class="btn" ${p !== "idle" ? "disabled" : ""}>① 真实发现 (401 + metadata)</button>
      <button id="liveAuthorize" class="btn ${p === "discovered" ? "" : "ghost"}" ${p === "discovered" ? "" : "disabled"}>${authLabel}</button>
      <button id="liveLogin" class="btn ${p === "awaiting_login" || p === "done" ? "" : "ghost"}" ${p === "awaiting_login" || p === "done" ? "" : "disabled"}>③ 打开浏览器登录 → 抓 token + tools</button>
    </div>
    <div id="liveStatus" class="status-line ${liveStatusClass()}">${liveStatusText()}</div>`;
  $("#liveDiscover").onclick = liveDiscover;
  $("#liveAuthorize").onclick = liveAuthorize;
  $("#liveLogin").onclick = liveLogin;
}
function liveStatusClass() {
  return { idle: "", discovered: "ok", authorizing: "wait", awaiting_login: "wait", done: "ok", error: "err" }[state.live.phase] || "";
}
function liveStatusText() {
  return {
    idle: "尚未开始 —— 点“① 真实发现”。",
    discovered: "已真实抓到 401 + 两份发现元数据（步骤 1–4）。",
    authorizing: "正在构造授权请求并抓取代理的 302…",
    awaiting_login: "已抓到真实 302（步骤 5–6，resource 已被删）。点“③ 打开浏览器登录”。",
    done: "全部完成：换 token + tools/list 均已真实抓取。",
    error: "出错：" + (state.live.error || "见控制台"),
  }[state.live.phase] || "";
}

async function playRange(a, b) {
  pause();
  for (let i = a; i <= b; i++) {
    await goToStep(i);
    await new Promise((r) => setTimeout(r, 700 / state.speed));
  }
}

async function liveDiscover() {
  state.live.phase = "authorizing"; renderLivePanel();
  try {
    const r = await fetch(`/api/live/discover?flow=${state.flow}`, { method: "POST" });
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "discover failed");
    mergeLive(j.steps);
    state.live.phase = "discovered"; renderLivePanel();
    await playRange(...FLOW.live.discover);
  } catch (e) {
    state.live.phase = "error"; state.live.error = e.message; renderLivePanel();
  }
}

async function liveAuthorize() {
  state.live.phase = "authorizing"; renderLivePanel();
  try {
    const r = await fetch(`/api/live/authorize?flow=${state.flow}`);
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "authorize failed");
    state.live.authorizeUrl = j.authorizeUrl;
    mergeLive(j.steps);
    state.live.phase = "awaiting_login"; renderLivePanel();
    await playRange(...FLOW.live.authorize);
  } catch (e) {
    state.live.phase = "error"; state.live.error = e.message; renderLivePanel();
  }
}

function liveLogin() {
  window.open(state.live.authorizeUrl, "_blank", "noopener");
  $("#liveStatus").textContent = "已打开登录窗口，请在新标签页完成 Entra 登录…（本页会自动检测完成）";
  $("#liveStatus").className = "status-line wait";
  clearInterval(state.live.poll);
  state.live.poll = setInterval(async () => {
    try {
      const r = await fetch("/api/live/status");
      const j = await r.json();
      if (j.phase === "done") {
        clearInterval(state.live.poll);
        mergeLive(j.steps);
        state.live.phase = "done"; renderLivePanel();
        await playRange(...FLOW.live.login);
      } else if (j.phase === "error") {
        clearInterval(state.live.poll);
        state.live.phase = "error"; state.live.error = j.error; renderLivePanel();
      }
    } catch (_) {}
  }, 1500);
}

function mergeLive(steps) {
  for (const [k, v] of Object.entries(steps || {})) state.live.data[+k] = v;
}

// ── 流程切换（mcpproxy ↔ mcp）────────────────────────────────────────
function setFlow(key) {
  if (state.flow === key) return;
  state.flow = key;
  applyFlow(key);
  $("#flowProxy").classList.toggle("active", key === "mcpproxy");
  $("#flowDirect").classList.toggle("active", key === "mcp");
  $("#flowTagline").textContent = FLOW.tagline;
  clearInterval(state.live.poll);
  state.live = { phase: "idle", data: {}, authorizeUrl: null, poll: null };
  restart();            // 会用新流程的 LANES/STEPS 重建时序图
  renderLivePanel();
}

// ── 模式切换 ────────────────────────────────────────────────────────
function setMode(m) {
  if (state.mode === m) return;
  state.mode = m;
  $("#modeDemo").classList.toggle("active", m === "demo");
  $("#modeLive").classList.toggle("active", m === "live");
  clearInterval(state.live.poll);
  state.live = { phase: "idle", data: {}, authorizeUrl: null, poll: null };
  restart();
  renderLivePanel();
}

// ── 绑定 & 初始化 ───────────────────────────────────────────────────
function init() {
  buildDiagram();
  $("#flowTagline").textContent = FLOW.tagline;
  updateControls();
  $("#btnNext").onclick = () => { pause(); next(); };
  $("#btnPrev").onclick = () => { pause(); prev(); };
  $("#btnPlay").onclick = play;
  $("#btnRestart").onclick = restart;
  $("#modeDemo").onclick = () => setMode("demo");
  $("#modeLive").onclick = () => setMode("live");
  $("#flowProxy").onclick = () => setFlow("mcpproxy");
  $("#flowDirect").onclick = () => setFlow("mcp");
  $("#speed").oninput = (e) => {
    state.speed = parseFloat(e.target.value);
    $("#speedVal").textContent = state.speed.toFixed(1) + "×";
  };
  document.addEventListener("keydown", (e) => {
    if (e.key === "ArrowRight") { pause(); next(); }
    else if (e.key === "ArrowLeft") { pause(); prev(); }
    else if (e.key === " ") { e.preventDefault(); play(); }
  });
  let rt;
  window.addEventListener("resize", () => {
    clearTimeout(rt);
    rt = setTimeout(() => { const c = state.cur; buildDiagram(); if (c > 0) goToStep(c, { animate: false }); }, 200);
  });
}
init();
