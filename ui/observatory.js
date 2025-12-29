(() => {
  "use strict";

  const cfg = (window.__RF_CONFIG__ && typeof window.__RF_CONFIG__ === "object")
    ? window.__RF_CONFIG__
    : {};

  const wsPort = Number(cfg.ws_port || 8766);
  const wsUrl = `${location.protocol === "https:" ? "wss" : "ws"}://${location.hostname}:${wsPort}`;

  const freqsMhz = Array.isArray(cfg.freqs_mhz) ? cfg.freqs_mhz : [315, 433.92, 868, 915];
  const captureDuration = Number(cfg.capture_duration || 0.8);
  const workDir = String(cfg.work_dir || "/tmp/flipper_explore");

  const baseColors = {
    "315": "#3fb950",
    "433.92": "#58a6ff",
    "868": "#d29922",
    "915": "#f85149",
  };
  const palette = ["#3fb950", "#58a6ff", "#d29922", "#f85149", "#56d4dd", "#a371f7", "#f0883e"];

  const protoIconIds = {
    princeton: "rf-car",
    came_12bit: "rf-gate",
    nice_flo: "rf-gate",
    keeloq: "rf-lock",
    oregon_v2: "rf-weather",
    smart_meter: "rf-meter",
    tpms: "rf-tire",
    doorbell: "rf-bell",
    honeywell: "rf-alarm",
    amb_weather: "rf-weather",
    fixed_code: "rf-radio",
    fsk_signal: "rf-signal",
    slow_signal: "rf-antenna",
    complex_signal: "rf-question",
    ook_signal: "rf-radio",
    unknown: "rf-question",
  };

  function iconHref(id) { return `icons.svg#${id}`; }

  function makeIcon(id, className = "ico") {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", className);
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("viewBox", "0 0 24 24");

    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    const href = iconHref(id);
    use.setAttribute("href", href);
    use.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href", href);
    svg.appendChild(use);

    return svg;
  }

  function setIconText(node, iconId, text, className = "ico") {
    node.textContent = "";
    node.appendChild(makeIcon(iconId, className));
    node.appendChild(document.createTextNode(` ${text}`));
  }

  function protoIconId(protoName) {
    const key = String(protoName || "unknown");
    return protoIconIds[key] || "rf-radio";
  }

  const protoColors = {
    princeton: "#3fb950",
    came_12bit: "#58a6ff",
    nice_flo: "#d29922",
    keeloq: "#f85149",
    oregon_v2: "#56d4dd",
    smart_meter: "#f0883e",
    tpms: "#a371f7",
    doorbell: "#a371f7",
    honeywell: "#f85149",
    amb_weather: "#56d4dd",
    fixed_code: "#8b949e",
    fsk_signal: "#56d4dd",
    slow_signal: "#8b949e",
    complex_signal: "#6e7681",
    ook_signal: "#8b949e",
    unknown: "#6e7681",
  };

  const el = {
    wsDot: document.getElementById("wsDot"),
    wsStatus: document.getElementById("wsStatus"),
    srcDot: document.getElementById("srcDot"),
    srcStatus: document.getElementById("srcStatus"),
    cycleStatus: document.getElementById("cycleStatus"),
    uniqueStatus: document.getElementById("uniqueStatus"),
    decodedStatus: document.getElementById("decodedStatus"),
    bandMeta: document.getElementById("bandMeta"),
    protoMeta: document.getElementById("protoMeta"),
    waterfallMeta: document.getElementById("waterfallMeta"),
    eventMeta: document.getElementById("eventMeta"),
    inspectMeta: document.getElementById("inspectMeta"),
    inspectLabel: document.getElementById("inspectLabel"),
    inspectBadges: document.getElementById("inspectBadges"),
    inspectStats: document.getElementById("inspectStats"),
    timings: document.getElementById("timings"),
    histogram: document.getElementById("histogram"),
    waveCanvas: document.getElementById("waveCanvas"),
    bandGrid: document.getElementById("bandGrid"),
    healthGrid: document.getElementById("healthGrid"),
    protoList: document.getElementById("protoList"),
    waterfall: document.getElementById("waterfall"),
    waterfallLegend: document.getElementById("waterfallLegend"),
    eventStream: document.getElementById("eventStream"),
    filterInput: document.getElementById("filterInput"),
    onlyNewToggle: document.getElementById("onlyNewToggle"),
    onlyDecodedToggle: document.getElementById("onlyDecodedToggle"),
    pauseBtn: document.getElementById("pauseBtn"),
    clearBtn: document.getElementById("clearBtn"),
    toasts: document.getElementById("toasts"),
    pauseBtnLabel: document.querySelector("#pauseBtn .btn-label"),
    pauseBtnUse: document.querySelector("#pauseBtn use"),
  };

  function nowSec() { return Date.now() / 1000; }

  function fkey(n) {
    const x = Number(n);
    if (!Number.isFinite(x)) return "";
    const rounded = Math.round(x * 100) / 100;
    return String(rounded);
  }

  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }
  function lerp(a, b, t) { return a + (b - a) * t; }

  function parseHexColor(hex) {
    const m = /^#([0-9a-f]{6})$/i.exec(hex);
    if (!m) return { r: 88, g: 166, b: 255 };
    const int = parseInt(m[1], 16);
    return { r: (int >> 16) & 255, g: (int >> 8) & 255, b: int & 255 };
  }

  function colorWithIntensity(hex, intensity) {
    const c = parseHexColor(hex);
    const v = clamp(intensity, 0, 1);
    // Slightly lift the floor so low activity still glows.
    const floor = 0.10;
    const k = floor + (1 - floor) * v;
    const r = Math.round(c.r * k);
    const g = Math.round(c.g * k);
    const b = Math.round(c.b * k);
    const a = 0.08 + 0.92 * v;
    return `rgba(${r},${g},${b},${a.toFixed(3)})`;
  }

  const freqOrder = freqsMhz.map(fkey);
  const colors = {};
  freqOrder.forEach((k, i) => { colors[k] = baseColors[k] || palette[i % palette.length]; });

  const state = {
    ws: null,
    wsConnected: false,
    source: { connected: null, mock: false, port: null },
    paused: false,
    pausedBuffer: [],
    pendingRaw: [],
    renderScheduled: false,
    lastMsgAt: 0,

    freq: new Map(),          // key -> latest freq summary
    freqDisplay: new Map(),   // key -> smoothed values
    freqHistory: new Map(),   // key -> array of intensity samples

    protocols: {},            // protocol -> count (unique)
    protocolTotal: 0,

    events: [],               // newest first
    selected: null,

    waterfall: {
      dpr: 1,
      w: 0,
      h: 0,
      max: 1,
      lastCycleAt: 0,
    },
  };

  const bandEls = new Map();     // key -> elements
  const healthEls = new Map();   // key -> elements
  const protoEls = new Map();    // protocol -> elements

  function toast(text, kind = "ok") {
    const node = document.createElement("div");
    node.className = `toast ${kind}`;
    node.textContent = text;
    el.toasts.appendChild(node);
    setTimeout(() => node.remove(), 3500);
  }

  function setDot(dot, status, label) {
    dot.classList.toggle("live", status === "live");
    dot.classList.toggle("warn", status === "warn");
    dot.classList.toggle("err", status === "err");
    label.textContent = status === "live" ? "live" : status === "warn" ? "stale" : status === "err" ? "err" : "connecting";
  }

  function setWsConnected(connected) {
    state.wsConnected = connected;
    el.wsStatus.textContent = connected ? "live" : "connecting";
    setDot(el.wsDot, connected ? "live" : "warn", el.wsStatus);
  }

  function setSourceStatus() {
    const s = state.source;
    if (s.connected === true) {
      el.srcStatus.textContent = s.mock ? "mock" : (s.port ? s.port.split("/").slice(-1)[0] : "flipper");
      setDot(el.srcDot, "live", el.srcStatus);
      return;
    }
    if (s.connected === false) {
      el.srcStatus.textContent = "offline";
      setDot(el.srcDot, "err", el.srcStatus);
      return;
    }
    el.srcStatus.textContent = "--";
    setDot(el.srcDot, "warn", el.srcStatus);
  }

  function fmtNum(n) {
    if (!Number.isFinite(n)) return "--";
    if (n >= 1000) return n.toLocaleString();
    return String(Math.round(n));
  }

  function fmtPct(n) {
    if (!Number.isFinite(n)) return "--";
    return `${Math.round(n)}%`;
  }

  function fmtAge(ts) {
    if (!Number.isFinite(ts)) return "--";
    const s = nowSec() - ts;
    if (s < 60) return `${Math.round(s)}s`;
    if (s < 3600) return `${Math.round(s / 60)}m`;
    return `${Math.round(s / 3600)}h`;
  }

  function buildLegend() {
    el.waterfallLegend.textContent = "";
    freqOrder.forEach(k => {
      const pill = document.createElement("div");
      pill.className = "legend-pill";
      const sw = document.createElement("div");
      sw.className = "swatch";
      sw.style.background = colors[k];
      const t = document.createElement("div");
      t.textContent = `${k} MHz`;
      pill.appendChild(sw);
      pill.appendChild(t);
      el.waterfallLegend.appendChild(pill);
    });
  }

  function makeBandCard(k) {
    const card = document.createElement("div");
    card.className = "band-card";
    card.dataset.freq = k;

    const top = document.createElement("div");
    top.className = "band-top";
    const freq = document.createElement("div");
    freq.className = "band-freq";
    freq.textContent = `${k} MHz`;
    freq.style.color = colors[k] || "var(--text)";
    const hz = document.createElement("div");
    hz.className = "band-hz";
    const hzVal = Math.round(Number(k) * 1_000_000);
    hz.textContent = Number.isFinite(hzVal) ? `${hzVal.toLocaleString()} Hz` : "--";
    top.appendChild(freq);
    top.appendChild(hz);

    const metrics = document.createElement("div");
    metrics.className = "band-metrics";

    function metric(label) {
      const m = document.createElement("div");
      m.className = "m";
      const mk = document.createElement("div");
      mk.className = "m-k";
      mk.textContent = label;
      const mv = document.createElement("div");
      mv.className = "m-v";
      mv.textContent = "--";
      m.appendChild(mk);
      m.appendChild(mv);
      return { root: m, value: mv };
    }

    const mSignals = metric("signals");
    const mTransitions = metric("transitions");
    const mDecoded = metric("decoded");
    const mUniq = metric("uniq/min");
    metrics.appendChild(mSignals.root);
    metrics.appendChild(mTransitions.root);
    metrics.appendChild(mDecoded.root);
    metrics.appendChild(mUniq.root);

    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("div");
    fill.style.background = `linear-gradient(90deg, ${colors[k]}, rgba(86,212,221,.95))`;
    bar.appendChild(fill);

    const spark = document.createElement("canvas");
    spark.className = "spark";

    card.appendChild(top);
    card.appendChild(metrics);
    card.appendChild(bar);
    card.appendChild(spark);

    card.addEventListener("click", () => {
      const snap = state.freq.get(k);
      if (snap && snap.best_signal) selectSignal(snap.best_signal, { focus: true });
    });

    return {
      card,
      mSignals,
      mTransitions,
      mDecoded,
      mUniq,
      fill,
      spark,
    };
  }

  function buildBandGrid() {
    el.bandGrid.textContent = "";
    freqOrder.forEach(k => {
      const els = makeBandCard(k);
      bandEls.set(k, els);
      el.bandGrid.appendChild(els.card);

      state.freq.set(k, { freq: k, n_signals: 0, n_transitions: 0, decoded_signals: 0, health: null, best_signal: null });
      state.freqDisplay.set(k, { activity: 0, nSignals: 0, nTransitions: 0, decodedPct: 0, uniqPerMin: 0, lastTs: 0 });
      state.freqHistory.set(k, []);
    });
    el.bandMeta.textContent = `${freqOrder.length} bands · cap ${captureDuration.toFixed(2)}s`;
  }

  function makeHealthRow(k) {
    const row = document.createElement("div");
    row.className = "health-row";

    const freq = document.createElement("div");
    freq.className = "health-freq";
    freq.textContent = `${k}`;
    freq.style.color = colors[k];

    const cells = document.createElement("div");
    cells.className = "health-cells";

    function cell(label) {
      const c = document.createElement("div");
      c.className = "cell";
      const ck = document.createElement("div");
      ck.className = "cell-k";
      ck.textContent = label;
      const cv = document.createElement("div");
      cv.className = "cell-v";
      cv.textContent = "--";
      c.appendChild(ck);
      c.appendChild(cv);
      return { root: c, value: cv };
    }

    const rate = cell("sig/min");
    const unique = cell("uniq/min");
    const entropy = cell("entropy");
    const duty = cell("duty");

    [rate, unique, entropy, duty].forEach(x => cells.appendChild(x.root));

    row.appendChild(freq);
    row.appendChild(cells);

    return { row, rate, unique, entropy, duty };
  }

  function buildHealthGrid() {
    el.healthGrid.textContent = "";
    freqOrder.forEach(k => {
      const els = makeHealthRow(k);
      healthEls.set(k, els);
      el.healthGrid.appendChild(els.row);
    });
  }

  function upsertProtocolRow(proto, count, total) {
    const key = String(proto || "unknown");
    let els = protoEls.get(key);
    if (!els) {
      const row = document.createElement("div");
      row.className = "proto-row";

      const ic = document.createElement("div");
      ic.className = "proto-icon";
      ic.appendChild(makeIcon(protoIconId(key)));

      const name = document.createElement("div");
      name.className = "proto-name";
      name.textContent = key.replace(/_/g, " ");

      const bar = document.createElement("div");
      bar.className = "proto-bar";
      const fill = document.createElement("div");
      fill.style.background = colors["433.92"] || "var(--blue)";
      bar.appendChild(fill);

      const c = document.createElement("div");
      c.className = "proto-count";

      row.appendChild(ic);
      row.appendChild(name);
      row.appendChild(bar);
      row.appendChild(c);

      els = { row, fill, count: c };
      protoEls.set(key, els);
    }

    const pct = total > 0 ? (count / total) * 100 : 0;
    els.fill.style.width = `${pct.toFixed(1)}%`;
    els.fill.style.background = protoColors[key] || "rgba(110,118,129,.85)";
    els.count.textContent = String(count);

    return els;
  }

  function renderProtocols(stats) {
    if (!stats || typeof stats !== "object") return;

    const entries = Object.entries(stats)
      .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0))
      .slice(0, 10);

    const total = entries.reduce((acc, [, n]) => acc + Number(n || 0), 0);
    el.protoMeta.textContent = total ? `${total} tracked` : "--";

    const seen = new Set();
    el.protoList.textContent = "";
    entries.forEach(([p, n]) => {
      const row = upsertProtocolRow(p, Number(n || 0), total);
      seen.add(p);
      el.protoList.appendChild(row.row);
    });

    // Clean up removed protocols.
    [...protoEls.keys()].forEach(k => {
      if (!seen.has(k) && k !== "unknown") protoEls.delete(k);
    });
  }

  function resizeCanvas(canvas) {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.floor(rect.width * dpr));
    const h = Math.max(1, Math.floor(rect.height * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
    }
    return { w, h, dpr };
  }

  function resizeAllCanvases() {
    state.waterfall = { ...state.waterfall, ...resizeCanvas(el.waterfall) };
    resizeCanvas(el.waveCanvas);

    bandEls.forEach((b) => resizeCanvas(b.spark));
    drawWaterfallFrame(true);
  }

  function drawWaterfallFrame(clear = false) {
    const ctx = el.waterfall.getContext("2d");
    const { w, h } = state.waterfall;
    if (!w || !h) return;

    if (clear) {
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = "rgba(0,0,0,.25)";
      ctx.fillRect(0, 0, w, h);
    }

    // Grid overlay.
    ctx.save();
    ctx.globalAlpha = 0.35;
    ctx.strokeStyle = "rgba(255,255,255,.06)";
    ctx.lineWidth = 1;
    const cols = freqOrder.length || 1;
    const seg = w / cols;
    for (let i = 1; i < cols; i++) {
      const x = Math.floor(i * seg) + 0.5;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h);
      ctx.stroke();
    }
    ctx.restore();
  }

  function pushSparkSample(k, value) {
    const arr = state.freqHistory.get(k);
    if (!arr) return;
    arr.push(value);
    if (arr.length > 60) arr.shift();
  }

  function drawSparkline(k) {
    const els = bandEls.get(k);
    if (!els) return;
    const canvas = els.spark;
    const ctx = canvas.getContext("2d");
    const arr = state.freqHistory.get(k) || [];
    const w = canvas.width, h = canvas.height;

    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "rgba(0,0,0,.10)";
    ctx.fillRect(0, 0, w, h);

    if (arr.length < 2) return;

    const max = Math.max(...arr, 1);
    const pad = 3;
    const xs = (w - pad * 2) / (arr.length - 1);
    ctx.beginPath();
    ctx.strokeStyle = colorWithIntensity(colors[k], 0.85);
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    arr.forEach((v, i) => {
      const x = pad + i * xs;
      const y = h - pad - (v / max) * (h - pad * 2);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  function drawWaterfallRow(freqs) {
    const ctx = el.waterfall.getContext("2d");
    const { w, h } = state.waterfall;
    if (!w || !h) return;

    // Shift down by 1px to make room at top.
    ctx.drawImage(el.waterfall, 0, 1);
    ctx.fillStyle = "rgba(0,0,0,.20)";
    ctx.fillRect(0, 0, w, 1);

    // Compute normalization.
    const vals = freqOrder.map(k => {
      const f = freqs.find(x => fkey(x.freq) === k) || {};
      // Prefer transitions for a denser waterfall.
      return Number(f.n_transitions || 0) + Number(f.n_signals || 0) * 20;
    });
    const maxNow = Math.max(...vals, 1);
    state.waterfall.max = Math.max(1, state.waterfall.max * 0.985, maxNow);

    const cols = freqOrder.length || 1;
    const seg = w / cols;
    vals.forEach((raw, i) => {
      const k = freqOrder[i];
      const norm = Math.log1p(raw) / Math.log1p(state.waterfall.max);
      ctx.fillStyle = colorWithIntensity(colors[k], norm);
      ctx.fillRect(Math.floor(i * seg), 0, Math.ceil(seg), 1);
    });

    drawWaterfallFrame(false);
  }

  function updateHealthRow(k, health) {
    const els = healthEls.get(k);
    if (!els || !health) return;
    els.rate.value.textContent = fmtNum(health.signal_rate);
    els.unique.value.textContent = fmtNum(health.unique_per_min);
    els.entropy.value.textContent = Number.isFinite(health.entropy) ? health.entropy.toFixed(2) : "--";
    els.duty.value.textContent = Number.isFinite(health.duty_cycle) ? fmtPct(health.duty_cycle) : "--";
  }

  function applyFreqUpdate(data) {
    const k = fkey(data.freq);
    if (!k) return;
    const current = state.freq.get(k) || {};
    const merged = { ...current, ...data, freq: k };
    state.freq.set(k, merged);

    // Compute a stable activity value for smoothing/sparkline.
    const activity = Number(merged.n_transitions || 0) + Number(merged.n_signals || 0) * 20;
    pushSparkSample(k, activity);
    drawSparkline(k);

    if (merged.health) updateHealthRow(k, merged.health);

    // Update targets for smoothing.
    const disp = state.freqDisplay.get(k) || {};
    disp.lastTs = Number(merged.ts || nowSec());
    disp.activityTarget = activity;
    disp.nSignalsTarget = Number(merged.n_signals || 0);
    disp.nTransitionsTarget = Number(merged.n_transitions || 0);
    disp.decodedPctTarget = merged.n_signals ? (Number(merged.decoded_signals || 0) / Number(merged.n_signals || 1)) * 100 : 0;
    disp.uniqPerMinTarget = merged.health ? Number(merged.health.unique_per_min || 0) : 0;
    state.freqDisplay.set(k, disp);

    // If nothing selected yet, keep the inspector alive.
    if (!state.selected && merged.best_signal) selectSignal(merged.best_signal, { silent: true });
  }

  function applyCycleUpdate(cycle) {
    const dur = Number(cycle.duration);
    const c = Number(cycle.cycle);
    el.cycleStatus.textContent = Number.isFinite(c) ? `#${c} · ${dur.toFixed(2)}s` : "--";
    el.uniqueStatus.textContent = fmtNum(Number(cycle.total_unique || 0));
    el.decodedStatus.textContent = `${fmtNum(Number(cycle.decoded_signals || 0))}/${fmtNum(Number(cycle.total_signals || 0))}`;

    state.waterfall.lastCycleAt = Number(cycle.ts || nowSec());
    el.waterfallMeta.textContent = `last ${fmtAge(state.waterfall.lastCycleAt)} · max ${fmtNum(state.waterfall.max)}`;

    if (Array.isArray(cycle.freqs)) drawWaterfallRow(cycle.freqs);

    if (cycle.protocol_stats) renderProtocols(cycle.protocol_stats);

    // New signals are always interesting: add to stream.
    if (Array.isArray(cycle.new_signals) && cycle.new_signals.length) {
      cycle.new_signals.slice(0, 30).forEach(s => {
        addEvent({
          ts: Number(cycle.ts || nowSec()),
          fp: s.fp,
          freq: s.freq,
          label: s.label,
          is_new: true,
          protocol: { protocol: "unknown", confidence: 0, icon: "✨" },
        });
      });
      toast(`+${cycle.new_signals.length} new fingerprint${cycle.new_signals.length === 1 ? "" : "s"}`, "ok");
    }
  }

  function applyInit(m) {
    if (typeof m.total_unique === "number") el.uniqueStatus.textContent = fmtNum(m.total_unique);
    if (m.protocol_stats) renderProtocols(m.protocol_stats);
    if (Array.isArray(m.top_signals) && m.top_signals[0]) selectSignal(m.top_signals[0], { silent: true });
  }

  function applyStatus(m) {
    state.source.connected = m.connected;
    state.source.mock = !!m.mock;
    state.source.port = m.port || null;
    setSourceStatus();
  }

  function applyError(m) {
    const msg = m && (m.msg || m.message) ? String(m.msg || m.message) : "unknown error";
    toast(`ERROR: ${msg}`, "err");
    state.source.connected = false;
    setSourceStatus();
  }

  function addEvent(signal) {
    if (!signal) return;
    const evt = {
      ts: Number(signal.ts || nowSec()),
      fp: String(signal.fp || "--"),
      freq: fkey(signal.freq || "--"),
      label: String(signal.label || "Signal"),
      is_new: !!signal.is_new,
      protocol: signal.protocol || {},
      n_pulses: signal.n_pulses,
      duration_ms: signal.duration_ms,
      confidence: signal.protocol && typeof signal.protocol.confidence === "number" ? signal.protocol.confidence : 0,
      _raw: signal,
    };

    state.events.unshift(evt);
    if (state.events.length > 160) state.events.pop();
    renderEventStream();
  }

  function passesFilter(evt) {
    const q = (el.filterInput.value || "").trim().toLowerCase();
    const onlyNew = !!el.onlyNewToggle.checked;
    const onlyDecoded = !!el.onlyDecodedToggle.checked;

    if (onlyNew && !evt.is_new) return false;
    if (onlyDecoded && Number(evt.confidence || 0) < 30) return false;

    if (!q) return true;
    const p = evt.protocol && evt.protocol.protocol ? String(evt.protocol.protocol) : "";
    const hay = `${evt.fp} ${evt.label} ${p} ${evt.freq}`.toLowerCase();
    return hay.includes(q);
  }

  function renderEventStream() {
    const filtered = state.events.filter(passesFilter);
    el.eventMeta.textContent = `${filtered.length}/${state.events.length}`;
    el.eventStream.textContent = "";

    filtered.slice(0, 120).forEach(evt => {
      const node = document.createElement("div");
      node.className = `evt${evt.is_new ? " new" : ""}`;

      const top = document.createElement("div");
      top.className = "top";
      const label = document.createElement("div");
      label.className = "label";
      const pLabel = evt.protocol && evt.protocol.protocol ? evt.protocol.protocol : "unknown";
      setIconText(label, protoIconId(pLabel), evt.label, "ico ico-sm");
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `${evt.freq} MHz · ${fmtAge(evt.ts)}`;
      top.appendChild(label);
      top.appendChild(meta);

      const row = document.createElement("div");
      row.className = "row";

      const p = evt.protocol && evt.protocol.protocol ? evt.protocol.protocol : "unknown";
      const conf = fmtPct(Number(evt.confidence || 0));

      row.appendChild(pill("fp", evt.fp));
      row.appendChild(pill("proto", String(p).replace(/_/g, " "), protoIconId(p)));
      row.appendChild(pill("conf", conf));
      if (Number.isFinite(evt.n_pulses)) row.appendChild(pill("pulses", fmtNum(evt.n_pulses)));
      if (Number.isFinite(evt.duration_ms)) row.appendChild(pill("dur", `${evt.duration_ms}ms`));

      node.appendChild(top);
      node.appendChild(row);

      node.addEventListener("click", () => selectSignal(evt._raw, { focus: true }));
      el.eventStream.appendChild(node);
    });
  }

  function pill(k, v, iconId = null) {
    const p = document.createElement("span");
    p.className = "pill";
    const kk = document.createElement("b");
    kk.textContent = k;
    p.appendChild(kk);
    if (iconId) {
      p.appendChild(document.createTextNode(" "));
      p.appendChild(makeIcon(iconId, "ico ico-xs"));
      p.appendChild(document.createTextNode(` ${v}`));
    } else {
      p.appendChild(document.createTextNode(` ${v}`));
    }
    return p;
  }

  function drawWave(timings, freqK) {
    const c = el.waveCanvas;
    const ctx = c.getContext("2d");
    const w = c.width, h = c.height;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "rgba(0,0,0,.22)";
    ctx.fillRect(0, 0, w, h);

    if (!Array.isArray(timings) || timings.length < 4) return;

    const pad = Math.floor(w * 0.03);
    const hi = pad;
    const lo = h - pad;
    const mid = Math.round((hi + lo) / 2);

    const n = Math.min(100, timings.length);
    const xs = (w - pad * 2) / n;
    let x = pad;

    ctx.strokeStyle = colorWithIntensity(colors[freqK] || "#58a6ff", 0.95);
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x, mid);
    for (let i = 0; i < n; i++) {
      const v = timings[i];
      const y = v > 0 ? hi : lo;
      ctx.lineTo(x, y);
      x += xs;
      ctx.lineTo(x, y);
    }
    ctx.stroke();

    // center line
    ctx.globalAlpha = 0.4;
    ctx.strokeStyle = "rgba(255,255,255,.10)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, mid + 0.5);
    ctx.lineTo(w, mid + 0.5);
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  function renderHistogram(hist) {
    el.histogram.textContent = "";
    const arr = Array.isArray(hist) ? hist : [];
    if (!arr.length) return;
    const max = Math.max(...arr, 1);
    for (let i = 0; i < 8; i++) {
      const v = arr[i] || 0;
      const bar = document.createElement("div");
      bar.style.height = `${clamp((v / max) * 100, 2, 100).toFixed(2)}%`;
      el.histogram.appendChild(bar);
    }
  }

  function renderInspector(signal) {
    if (!signal) return;

    const proto = signal.protocol || {};
    const protoName = proto.protocol || "unknown";
    const freqK = fkey(signal.freq);

    setIconText(el.inspectLabel, protoIconId(protoName), signal.label || proto.desc || "Signal", "ico ico-sm");
    el.inspectMeta.textContent = `${freqK} MHz · fp ${signal.fp || "--"} · ${fmtAge(Number(signal.ts || nowSec()))}`;

    el.inspectBadges.textContent = "";
    el.inspectBadges.appendChild(badge("fp", signal.fp || "--"));
    el.inspectBadges.appendChild(badge("proto", `${String(protoName).replace(/_/g, " ")}`));
    el.inspectBadges.appendChild(badge("conf", fmtPct(Number(proto.confidence || 0))));
    if (Number.isFinite(signal.n_pulses)) el.inspectBadges.appendChild(badge("pulses", fmtNum(signal.n_pulses)));
    if (Number.isFinite(signal.duration_ms)) el.inspectBadges.appendChild(badge("dur", `${signal.duration_ms}ms`));
    if (Number.isFinite(signal.on_duration_ms)) el.inspectBadges.appendChild(badge("on", `${signal.on_duration_ms}ms`));

    const stats = [
      ["pulse_mean", signal.pulse_mean],
      ["pulse_median", signal.pulse_median],
      ["pulse_cv", signal.pulse_cv],
      ["distinct", signal.n_distinct_widths],
      ["gap_mean", signal.gap_mean],
      ["gap_ratio", signal.gap_ratio],
      ["transitions", signal.n_transitions],
      ["duration_ms", signal.duration_ms],
    ];
    el.inspectStats.textContent = "";
    stats.forEach(([k, v]) => {
      const node = document.createElement("div");
      const kk = document.createElement("div");
      kk.className = "kv-k";
      kk.textContent = String(k).replace(/_/g, " ");
      const vv = document.createElement("div");
      vv.className = "kv-v";
      vv.textContent = Number.isFinite(Number(v)) ? String(v) : "--";
      node.appendChild(kk);
      node.appendChild(vv);
      el.inspectStats.appendChild(node);
    });

    const t = Array.isArray(signal.timings) ? signal.timings : [];
    el.timings.textContent = t.length ? t.slice(0, 120).join(", ") : "--";
    drawWave(t, freqK);
    renderHistogram(signal.histogram);
  }

  function badge(k, v) {
    const b = document.createElement("span");
    b.className = "badge";
    const kk = document.createElement("b");
    kk.textContent = k;
    b.appendChild(kk);
    b.appendChild(document.createTextNode(` ${v}`));
    return b;
  }

  function selectSignal(signal, opts = {}) {
    if (!signal) return;
    state.selected = signal;
    renderInspector(signal);
    if (opts.focus) toast("inspecting signal", "ok");
    if (!opts.silent) addEvent(signal);
  }

  function scheduleFlush() {
    if (state.renderScheduled) return;
    state.renderScheduled = true;
    requestAnimationFrame(flushQueue);
  }

  function enqueueRaw(raw) {
    state.pendingRaw.push(raw);
    scheduleFlush();
  }

  function flushQueue() {
    state.renderScheduled = false;
    const raws = state.pendingRaw.splice(0, state.pendingRaw.length);
    if (!raws.length) return;

    if (state.paused) {
      state.pausedBuffer.push(...raws);
      if (state.pausedBuffer.length > 2000) state.pausedBuffer.splice(0, state.pausedBuffer.length - 2000);
      return;
    }

    raws.forEach(raw => {
      let msg;
      try { msg = JSON.parse(raw); } catch { return; }
      if (!msg || typeof msg !== "object") return;
      state.lastMsgAt = nowSec();

      switch (msg.type) {
        case "init": applyInit(msg); break;
        case "status": applyStatus(msg); break;
        case "error": applyError(msg); break;
        case "freq": applyFreqUpdate(msg.data || {}); break;
        case "cycle": applyCycleUpdate(msg); break;
        case "signal": addEvent(msg.data || msg); break;
        default: break;
      }
    });
  }

  function renderBandsSmoothed(dt) {
    let maxActivity = 1;
    state.freqDisplay.forEach(d => { maxActivity = Math.max(maxActivity, Number(d.activity || 0)); });

    state.freqDisplay.forEach((d, k) => {
      const els = bandEls.get(k);
      const snap = state.freq.get(k) || {};
      if (!els) return;

      d.activity = Number.isFinite(d.activity) ? d.activity : 0;
      d.nSignals = Number.isFinite(d.nSignals) ? d.nSignals : 0;
      d.nTransitions = Number.isFinite(d.nTransitions) ? d.nTransitions : 0;
      d.decodedPct = Number.isFinite(d.decodedPct) ? d.decodedPct : 0;
      d.uniqPerMin = Number.isFinite(d.uniqPerMin) ? d.uniqPerMin : 0;

      const rate = clamp(dt * 10, 0.05, 0.25);
      d.activity = lerp(d.activity, Number(d.activityTarget || 0), rate);
      d.nSignals = lerp(d.nSignals, Number(d.nSignalsTarget || 0), rate);
      d.nTransitions = lerp(d.nTransitions, Number(d.nTransitionsTarget || 0), rate);
      d.decodedPct = lerp(d.decodedPct, Number(d.decodedPctTarget || 0), rate);
      d.uniqPerMin = lerp(d.uniqPerMin, Number(d.uniqPerMinTarget || 0), rate);

      els.mSignals.value.textContent = fmtNum(d.nSignals);
      els.mTransitions.value.textContent = fmtNum(d.nTransitions);
      els.mDecoded.value.textContent = fmtPct(d.decodedPct);
      els.mUniq.value.textContent = fmtNum(d.uniqPerMin);

      const pct = maxActivity > 0 ? (d.activity / maxActivity) * 100 : 0;
      els.fill.style.width = `${clamp(pct, 0, 100).toFixed(1)}%`;

      // Fade bands that haven't updated recently.
      const age = nowSec() - Number(d.lastTs || 0);
      els.card.style.opacity = age < Math.max(3, captureDuration * 3) ? "1" : "0.75";

      // Show best proto hint via border.
      const proto = snap.best_signal && snap.best_signal.protocol ? snap.best_signal.protocol.protocol : "";
      if (proto) {
        els.card.style.boxShadow = `0 0 0 1px rgba(88,166,255,.10) inset, 0 0 22px rgba(88,166,255,.08)`;
      } else {
        els.card.style.boxShadow = "";
      }
    });
  }

  function tick() {
    const t = nowSec();
    const dt = tick._t ? (t - tick._t) : 0.016;
    tick._t = t;

    // WS staleness indicator.
    const since = t - state.lastMsgAt;
    if (state.wsConnected && since > 4) {
      el.wsStatus.textContent = "stale";
      setDot(el.wsDot, "warn", el.wsStatus);
    } else if (state.wsConnected) {
      el.wsStatus.textContent = "live";
      setDot(el.wsDot, "live", el.wsStatus);
    }

    renderBandsSmoothed(dt);
    requestAnimationFrame(tick);
  }

  function connect() {
    if (state.ws) {
      try { state.ws.close(); } catch {}
    }
    const ws = new WebSocket(wsUrl);
    state.ws = ws;

    ws.onopen = () => {
      setWsConnected(true);
      toast(`connected ${wsUrl}`, "ok");
    };
    ws.onclose = () => {
      setWsConnected(false);
      setTimeout(connect, 1000);
    };
    ws.onerror = () => {
      setWsConnected(false);
    };
    ws.onmessage = (e) => enqueueRaw(e.data);
  }

  function togglePause() {
    state.paused = !state.paused;
    el.pauseBtn.classList.toggle("on", state.paused);
    if (el.pauseBtnLabel) el.pauseBtnLabel.textContent = state.paused ? "Resume" : "Pause";
    if (el.pauseBtnUse) {
      const id = state.paused ? "rf-play" : "rf-pause";
      const href = iconHref(id);
      el.pauseBtnUse.setAttribute("href", href);
      el.pauseBtnUse.setAttributeNS("http://www.w3.org/1999/xlink", "xlink:href", href);
    }
    toast(state.paused ? "paused" : "resumed", "ok");

    if (!state.paused && state.pausedBuffer.length) {
      const buf = state.pausedBuffer.splice(0, state.pausedBuffer.length);
      state.pendingRaw.push(...buf);
      scheduleFlush();
    }
  }

  function clearEvents() {
    state.events = [];
    el.eventStream.textContent = "";
    el.eventMeta.textContent = "--";
    toast("cleared", "ok");
  }

  function initUI() {
    buildLegend();
    buildBandGrid();
    buildHealthGrid();
    resizeAllCanvases();
    drawWaterfallFrame(true);

    el.pauseBtn.addEventListener("click", togglePause);
    el.clearBtn.addEventListener("click", clearEvents);

    el.filterInput.addEventListener("input", renderEventStream);
    el.onlyNewToggle.addEventListener("change", renderEventStream);
    el.onlyDecodedToggle.addEventListener("change", renderEventStream);

    window.addEventListener("resize", () => {
      resizeAllCanvases();
      renderEventStream();
    });
  }

  // Boot
  initUI();
  connect();
  requestAnimationFrame(tick);
})();
