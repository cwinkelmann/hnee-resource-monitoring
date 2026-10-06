// carrot GPUs dashboard. Read-only: fetches the JSON API and renders it.
// All data enters the DOM through textContent or SVG attributes (never HTML strings).
"use strict";

(function () {
  // Split so the file holds no URL literal (it is a namespace name, never fetched).
  const SVG_NS = "http:" + "//www.w3.org/2000/svg";
  const PALETTE_SIZE = 8;
  const EUR_PER_KWH = 0.30;
  const MAX_W = 700;
  const ALERT_ICONS = { allocation: "⚠", idle: "◔", capacity: "▣", unattributed: "?", report: "Σ" };

  const state = {
    now: null, timeline: null, usage: null, timeseries: null,
    timelineHours: 24, usageDays: 7, usageBy: "day",
    users: [],               // sorted, every user ever seen this session
    inflight: 0, failed: new Set(),
    timelineSeq: 0, usageSeq: 0,
    timelineTimer: null,
  };

  const $ = (id) => document.getElementById(id);

  // ---------- DOM helpers ----------
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function svg(tag, attrs, cls) {
    const e = document.createElementNS(SVG_NS, tag);
    if (attrs) for (const k in attrs) e.setAttribute(k, String(attrs[k]));
    if (cls) e.setAttribute("class", cls);
    return e;
  }
  function svgText(x, y, text, cls, anchor) {
    const t = svg("text", { x: x, y: y }, cls);
    if (anchor) t.setAttribute("text-anchor", anchor);
    t.textContent = text;
    return t;
  }
  function addTitle(node, text) {
    const t = svg("title");
    t.textContent = text;
    node.appendChild(t);
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  // ---------- formatting ----------
  const pad2 = (n) => String(n).padStart(2, "0");
  function fmtTime(d) { return pad2(d.getHours()) + ":" + pad2(d.getMinutes()); }
  function fmtLocal(d) {
    return d.toLocaleDateString(undefined, { weekday: "short", day: "2-digit", month: "short" })
      + " " + fmtTime(d);
  }
  function fmtGiB(mib) { return (mib / 1024).toFixed(1) + " GiB"; }
  function fmtDuration(seconds) {
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return "< 1 min";
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d) return d + " d " + h + " h";
    if (h) return h + " h " + m + " min";
    return m + " min";
  }
  function fmtNum(x, digits) {
    return x.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }
  function utcDay(iso) {        // "2026-10-06" -> "06 Oct" (UTC calendar day)
    const d = new Date(iso + "T00:00:00Z");
    return d.toLocaleDateString(undefined, { day: "2-digit", month: "short", timeZone: "UTC" });
  }
  function isoDay(d) { return d.toISOString().slice(0, 10); }

  // ---------- colours ----------
  function registerUsers(names) {
    let added = false;
    for (const n of names) {
      if (n === null || n === undefined || n.startsWith("(")) continue;
      if (!state.users.includes(n)) { state.users.push(n); added = true; }
    }
    if (added) state.users.sort();
    return added;
  }
  function colourClass(user) {
    if (user === null || user === undefined || user === "(unattributed)") return "unatt";
    if (user === "(idle)") return "idle";
    const i = state.users.indexOf(user);
    return "u" + ((i < 0 ? 0 : i) % PALETTE_SIZE);
  }
  function whoNode(user, tag) {
    const n = el(tag || "span", user === null || user === "(unattributed)" ? "unattributed" : "",
      user === null ? "unattributed" : user);
    return n;
  }
  function swatch(user) { return el("span", "swatch " + colourClass(user)); }
  function isOutside(user, assignee) {
    // Same rule as the allocation alert: unattributed is never accused, unassigned GPUs are free.
    return user !== null && assignee !== null && assignee !== undefined && user !== assignee;
  }
  function assigneeOf(gpu) {
    if (!state.now) return null;
    const g = state.now.gpus.find((x) => x.gpu === gpu);
    return g ? g.assigned_to : null;
  }

  // ---------- fetching ----------
  function setBusy(delta) {
    state.inflight += delta;
    $("refresh-indicator").classList.toggle("busy", state.inflight > 0);
  }
  function setFailed(key, failed) {
    if (failed) state.failed.add(key); else state.failed.delete(key);
    $("net-error").hidden = state.failed.size === 0;
  }
  function showEmpty(empty) {
    $("empty").hidden = !empty;
    $("app").hidden = empty;
    if (empty) $("stale-banner").hidden = true;
  }

  // Resolves to the JSON body, or null when there is no history yet (503) or the fetch failed.
  async function getJson(key, url) {
    setBusy(1);
    try {
      const r = await fetch(url, { cache: "no-store" });
      if (r.status === 503) { setFailed(key, false); showEmpty(true); return null; }
      if (!r.ok) throw new Error("HTTP " + r.status);
      const data = await r.json();
      setFailed(key, false);
      return data;
    } catch (e) {
      setFailed(key, true);
      return null;
    } finally {
      setBusy(-1);
    }
  }

  async function loadNow() {
    const data = await getJson("now", "/api/now");
    if (!data) return;
    showEmpty(false);
    state.now = data;
    const names = [];
    for (const g of data.gpus) { names.push(g.assigned_to); for (const p of g.procs) names.push(p.user); }
    if (registerUsers(names)) renderAll(); else { renderNow(); renderTimeline(); }
  }
  async function loadTimeline() {
    const seq = ++state.timelineSeq;
    const data = await getJson("timeline", "/api/timeline?hours=" + state.timelineHours);
    if (!data || seq !== state.timelineSeq) return;
    state.timeline = data;
    const names = [];
    for (const k in data.gpus) for (const j of data.gpus[k]) names.push(j.user);
    if (registerUsers(names)) renderAll(); else renderTimeline();
  }
  async function loadUsage() {
    const seq = ++state.usageSeq;
    const to = new Date();
    const from = new Date(to.getTime() - (state.usageDays - 1) * 86400000);
    const url = "/api/usage?from=" + isoDay(from) + "&to=" + isoDay(to) + "&by=" + state.usageBy;
    const data = await getJson("usage", url);
    if (!data || seq !== state.usageSeq) return;
    state.usage = data;
    if (registerUsers(Object.keys(data.totals.kwh))) renderAll(); else renderUsage();
  }
  async function loadTimeseries() {
    const data = await getJson("timeseries", "/api/timeseries?hours=24");
    if (!data) return;
    state.timeseries = data;
    renderTimeseries();
  }

  function renderAll() { renderNow(); renderTimeline(); renderUsage(); renderTimeseries(); }

  // ---------- header + Now ----------
  function renderNow() {
    const d = state.now;
    if (!d) return;
    const polled = new Date(d.ts);
    const lp = $("last-poll");
    lp.textContent = "last poll " + fmtLocal(polled);
    lp.title = d.ts + " (UTC)";
    const banner = $("stale-banner");
    banner.hidden = !d.stale;
    banner.textContent = d.stale
      ? "Monitor not running since " + fmtLocal(polled) + " — numbers below are not live" : "";

    const grid = $("gpu-grid");
    clear(grid);
    for (const g of d.gpus) grid.appendChild(gpuCard(g));

    // Slack wording only when the monitor recorded its mode: one line for a dry run,
    // a per-alert status only when it is really posting, nothing for older histories.
    $("slack-note").hidden = d.slack !== "dry-run";
    const alerts = $("alerts");
    clear(alerts);
    if (!d.alerts.length) {
      alerts.appendChild(el("li", "ok", "No alerts at the last poll."));
    }
    for (const a of d.alerts) {
      const li = el("li");
      const icon = el("span", "icon", ALERT_ICONS[a.kind] || "•");
      icon.title = a.kind;
      li.appendChild(icon);
      li.appendChild(el("span", "", a.text));
      if (d.slack === "posting") {
        li.appendChild(el("span", "sent", a.sent ? "(Slack: sent)" : "(Slack: in cooldown)"));
      }
      alerts.appendChild(li);
    }
  }

  function gpuCard(g) {
    const outside = g.procs.some((p) => isOutside(p.user, g.assigned_to));
    const card = el("div", "card" + (outside ? " outside" : ""));
    const head = el("div", "card-head");
    head.appendChild(el("span", "gpu", "GPU " + g.gpu));
    head.appendChild(el("span", "assignee", "assigned to " + (g.assigned_to || "—")));
    card.appendChild(head);
    if (outside) card.appendChild(el("span", "tag-outside", "outside allocation"));

    // VRAM bar: one segment per process in its user's colour; the rest of "used" in grey.
    const bar = svg("svg", { viewBox: "0 0 1000 10", preserveAspectRatio: "none", role: "img" }, "vram");
    bar.appendChild(svg("rect", { x: 0, y: 0, width: 1000, height: 10, rx: 0 }, "track"));
    const total = g.total_mib || 1;
    let x = 0, procMib = 0;
    for (const p of g.procs) {
      const w = Math.min(1000 - x, (p.used_mib / total) * 1000);
      if (w <= 0) continue;
      const seg = svg("rect", { x: x, y: 0, width: w, height: 10 }, colourClass(p.user));
      addTitle(seg, (p.user || "unattributed") + " · " + p.used_mib + " MiB");
      bar.appendChild(seg);
      x += w; procMib += p.used_mib;
    }
    const rest = Math.min(1000 - x, (Math.max(0, g.used_mib - procMib) / total) * 1000);
    if (rest > 0) bar.appendChild(svg("rect", { x: x, y: 0, width: rest, height: 10 }, "idle"));
    addTitle(bar, g.used_mib + " / " + g.total_mib + " MiB used");
    card.appendChild(bar);
    card.appendChild(el("div", "vram-label",
      "VRAM " + fmtGiB(g.used_mib) + " / " + fmtGiB(g.total_mib)));

    const m = el("div", "metrics");
    const util = el("span"); util.appendChild(el("b", "", Math.round(g.util_pct) + " %")); util.appendChild(document.createTextNode(" util"));
    const pw = el("span"); pw.appendChild(el("b", "", Math.round(g.power_w) + " W"));
    m.appendChild(util); m.appendChild(pw);
    card.appendChild(m);

    const ul = el("ul", "procs");
    if (!g.procs.length) ul.appendChild(el("li", "none", "no processes"));
    for (const p of g.procs) {
      const li = el("li");
      li.appendChild(swatch(p.user));
      li.appendChild(whoNode(p.user));
      const what = el("span", "what", p.name ? "· " + p.name : "");
      if (p.name) what.title = p.name + " (pid " + p.pid + ")";
      li.appendChild(what);
      li.appendChild(el("span", "mib", "· " + p.used_mib.toLocaleString() + " MiB"));
      ul.appendChild(li);
    }
    card.appendChild(ul);
    return card;
  }

  // ---------- Timeline ----------
  function timeTicks(t0, t1, plotW) {
    const span = t1 - t0, H = 3600000;
    const steps = [1, 2, 3, 6, 12, 24, 48, 168];
    let step = steps[steps.length - 1];
    for (const s of steps) { if ((s * H / span) * plotW >= 70) { step = s; break; } }
    if (state.timelineHours > 24 && step < 24) step = 24;   // days for 7 d / 30 d
    const ticks = [];
    const d = new Date(t0);
    if (step < 24) {
      d.setMinutes(0, 0, 0);
      while (d.getTime() < t0 || d.getHours() % step !== 0) d.setHours(d.getHours() + 1);
      for (; d.getTime() <= t1; d.setHours(d.getHours() + step)) ticks.push(new Date(d));
    } else {
      d.setHours(0, 0, 0, 0);
      if (d.getTime() < t0) d.setDate(d.getDate() + 1);
      const days = step / 24;
      for (; d.getTime() <= t1; d.setDate(d.getDate() + days)) ticks.push(new Date(d));
    }
    return ticks;
  }
  function tickLabel(d) {
    if (d.getHours() === 0 && d.getMinutes() === 0) {
      return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" });
    }
    return fmtTime(d);
  }

  function renderTimeline() {
    const wrap = $("timeline-chart"), legend = $("timeline-legend");
    const d = state.timeline;
    if (!d) return;
    clear(wrap); clear(legend);
    const gpuIds = new Set(Object.keys(d.gpus).map(Number));
    if (state.now) for (const g of state.now.gpus) gpuIds.add(g.gpu);
    const rows = Array.from(gpuIds).sort((a, b) => a - b);

    const W = Math.max(640, wrap.clientWidth || 0);
    const labelW = 190, padR = 18, axisH = 22, rowH = 32, barH = 22;
    const H = axisH + rows.length * rowH + 6;
    const t0 = new Date(d.from).getTime(), t1 = new Date(d.to).getTime();
    const plotW = W - labelW - padR;
    const x = (t) => labelW + ((Math.min(Math.max(t, t0), t1) - t0) / (t1 - t0)) * plotW;

    const chart = svg("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img" });
    addTitle(chart, "Timeline of GPU jobs, last " + state.timelineHours + " h");

    rows.forEach((gpu, i) => {
      const y = axisH + i * rowH;
      if (i % 2 === 0) chart.appendChild(svg("rect", { x: 0, y: y, width: W, height: rowH }, "row-band"));
      chart.appendChild(svgText(8, y + rowH / 2 - 2, "GPU " + gpu, "row-label"));
      chart.appendChild(svgText(8, y + rowH / 2 + 11, "assigned to " + (assigneeOf(gpu) || "—"), "row-sub"));
    });

    const nowX = x(t1);
    for (const tk of timeTicks(t0, t1, plotW)) {
      const tx = x(tk.getTime());
      chart.appendChild(svg("line", { x1: tx, x2: tx, y1: axisH - 4, y2: H - 4 }, "grid"));
      if (nowX - tx > 44) chart.appendChild(svgText(tx, 13, tickLabel(tk), "", "middle"));  // keep "now" legible
    }
    chart.appendChild(svg("line", { x1: labelW, x2: labelW, y1: axisH - 4, y2: H - 4 }, "axis-line"));

    const usersSeen = new Set();
    let anyUnatt = false, anyOutside = false, anyJob = false;
    rows.forEach((gpu, i) => {
      const y = axisH + i * rowH + (rowH - barH) / 2;
      const assignee = assigneeOf(gpu);
      for (const job of d.gpus[String(gpu)] || []) {
        anyJob = true;
        const s = new Date(job.start).getTime();
        const e = job.ongoing ? t1 : new Date(job.end).getTime();
        const x1 = x(s), w = Math.max(2, x(e) - x1);
        const cls = colourClass(job.user);
        const outside = isOutside(job.user, assignee);
        if (job.user === null) anyUnatt = true; else usersSeen.add(job.user);
        if (outside) anyOutside = true;
        const g = svg("g");
        const rect = svg("rect", { x: x1, y: y, width: w, height: barH, rx: 3 },
          "job " + cls + (outside ? " outside" : ""));
        g.appendChild(rect);
        if (job.ongoing) {
          const tipX = x1 + w;
          g.appendChild(svg("polygon", {
            points: tipX + "," + y + " " + (tipX + 7) + "," + (y + barH / 2) + " " + tipX + "," + (y + barH),
          }, cls + " ongoing-tip"));
        }
        if (w >= 120) {
          const who = job.user === null ? "unattributed" : job.user;
          let label = job.name ? who + " · " + job.name : who;
          const maxChars = Math.floor((w - 10) / 6.3);
          if (label.length > maxChars) label = label.slice(0, Math.max(1, maxChars - 1)) + "…";
          g.appendChild(svgText(x1 + 5, y + barH / 2 + 4, label,
            "bar-label" + (job.user === null ? " on-unatt unattributed" : "")));
        }
        const endTxt = job.ongoing ? "now (ongoing)" : fmtLocal(new Date(job.end));
        addTitle(g, [
          job.user === null ? "unattributed" : job.user,
          "what: " + (job.name || "unknown"),
          "pid " + job.pid + " on GPU " + gpu + (outside ? " — outside allocation (assigned to " + assignee + ")" : ""),
          fmtLocal(new Date(job.start)) + " – " + endTxt,
          "duration " + fmtDuration(((job.ongoing ? t1 : new Date(job.end).getTime()) - s) / 1000),
          "peak VRAM " + fmtGiB(job.max_mib) + " (" + job.max_mib.toLocaleString() + " MiB)",
        ].join("\n"));
        chart.appendChild(g);
      }
    });
    if (!anyJob) {
      chart.appendChild(svgText(labelW + plotW / 2, axisH + (rows.length * rowH) / 2, "No GPU jobs in this range", "empty-note", "middle"));
    }

    chart.appendChild(svg("line", { x1: nowX, x2: nowX, y1: axisH - 6, y2: H - 2 }, "now-line"));
    chart.appendChild(svgText(nowX, 13, "now", "now-label", "middle"));
    wrap.appendChild(chart);

    for (const u of Array.from(usersSeen).sort()) {
      const item = el("span", "item"); item.appendChild(swatch(u)); item.appendChild(el("span", "", u));
      legend.appendChild(item);
    }
    if (anyUnatt) {
      const item = el("span", "item"); item.appendChild(swatch(null)); item.appendChild(el("span", "unattributed", "unattributed"));
      legend.appendChild(item);
    }
    if (anyOutside) {
      const item = el("span", "item"); item.appendChild(el("span", "outside-key")); item.appendChild(el("span", "", "outside allocation"));
      legend.appendChild(item);
    }
    const tz = el("span", "item", "times in local time · ▸ = still running");
    legend.appendChild(tz);
  }

  // ---------- Usage ----------
  function niceStep(v) {            // smallest 1/2/5 x 10^k step >= v
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }
  function bucketsByKwh(d) {
    const names = new Set(Object.keys(d.totals.kwh).concat(Object.keys(d.totals.gpu_hours)));
    return Array.from(names).sort((a, b) => (d.totals.kwh[b] || 0) - (d.totals.kwh[a] || 0) || a.localeCompare(b));
  }

  function renderUsage() {
    const d = state.usage;
    if (!d) return;
    const buckets = bucketsByKwh(d);
    renderUsageChart(d, buckets);
    renderUsageTable(d, buckets);

    const cov = $("coverage"), c = d.coverage;
    const pct = Math.round(c.ratio * 100);
    if (!c.elapsed_s || !c.since) {
      cov.textContent = "No monitoring data in this period.";
    } else if (c.since === d.from) {
      cov.textContent = "Monitor was running for " + pct + " % of this period (UTC days " + d.from + " – " + d.to + ").";
    } else {
      cov.textContent = "Monitor was running for " + pct + " % of the time since monitoring began on "
        + c.since + " (range " + d.from + " – " + d.to + ", UTC days).";
    }
    cov.classList.toggle("low", Boolean(c.elapsed_s && c.since) && pct < 95);

    const cav = $("caveats");
    clear(cav);
    for (const c of d.caveats) cav.appendChild(el("li", "", c));
  }

  function renderUsageChart(d, buckets) {
    const wrap = $("usage-chart"), legend = $("usage-legend");
    clear(wrap); clear(legend);
    const W = Math.max(480, wrap.clientWidth || 0), H = 210;
    const padL = 48, padR = 8, padT = 22, padB = 24;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const n = d.periods.length || 1;
    const totals = d.periods.map((p) => Object.values(p.kwh).reduce((a, b) => a + b, 0));
    const step = niceStep(Math.max(0, ...totals) / 4);
    const ymax = step * Math.max(1, Math.ceil(Math.max(0, ...totals) / step));
    const nTicks = Math.round(ymax / step);
    const y = (v) => padT + plotH - (v / ymax) * plotH;
    const chart = svg("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img" });
    addTitle(chart, "kWh per " + d.by + " by user");

    for (let i = 0; i <= nTicks; i++) {
      const v = step * i, gy = y(v);
      chart.appendChild(svg("line", { x1: padL, x2: W - padR, y1: gy, y2: gy }, i ? "grid" : "axis-line"));
      chart.appendChild(svgText(padL - 6, gy + 4, fmtNum(v, step < 1 ? Math.ceil(-Math.log10(step)) : 0), "", "end"));
    }
    chart.appendChild(svgText(padL - 6, 10, "kWh", "", "end"));

    const band = plotW / n, bw = Math.max(2, Math.min(48, band * 0.7));
    const every = Math.max(1, Math.ceil(56 / band));
    d.periods.forEach((p, i) => {
      const cx = padL + band * i + band / 2;
      let acc = 0;
      for (const b of buckets.slice().reverse()) {   // largest bucket on the bottom
        const v = p.kwh[b] || 0;
        if (v <= 0) continue;
        const top = y(acc + v), bottom = y(acc);
        const r = svg("rect", { x: cx - bw / 2, y: top, width: bw, height: Math.max(0.5, bottom - top - 1) }, colourClass(b));
        addTitle(r, b + " · " + (d.by === "week" ? "week of " : "") + utcDay(p.start) + ": " + fmtNum(v, 2) + " kWh");
        chart.appendChild(r);
        acc += v;
      }
      if (i % every === 0) chart.appendChild(svgText(cx, H - 8, utcDay(p.start), "", "middle"));
    });
    wrap.appendChild(chart);

    for (const b of buckets) {
      const item = el("span", "item");
      item.appendChild(swatch(b));
      item.appendChild(el("span", b === "(unattributed)" ? "unattributed" : "", b));
      legend.appendChild(item);
    }
  }

  function renderUsageTable(d, buckets) {
    const tbody = $("usage-table").tBodies[0];
    clear(tbody);
    let kwhSum = 0, hSum = 0;
    for (const b of buckets) {
      const kwh = d.totals.kwh[b] || 0, h = d.totals.gpu_hours[b] || 0;
      kwhSum += kwh; hSum += h;
      const tr = el("tr");
      const who = el("td"); const w = el("span", "who"); w.appendChild(swatch(b));
      w.appendChild(el("span", b === "(unattributed)" ? "unattributed" : "", b)); who.appendChild(w);
      tr.appendChild(who);
      tr.appendChild(el("td", "num", fmtNum(kwh, 2)));
      tr.appendChild(el("td", "num", fmtNum(kwh * EUR_PER_KWH, 2)));
      tr.appendChild(el("td", "num", fmtNum(h, 1)));
      tbody.appendChild(tr);
    }
    const tr = el("tr", "total");
    tr.appendChild(el("td", "", "total"));
    tr.appendChild(el("td", "num", fmtNum(kwhSum, 2)));
    tr.appendChild(el("td", "num", fmtNum(kwhSum * EUR_PER_KWH, 2)));
    tr.appendChild(el("td", "num", fmtNum(hSum, 1)));
    tbody.appendChild(tr);
  }

  // ---------- Last 24 h sparklines ----------
  function renderTimeseries() {
    const grid = $("spark-grid");
    const d = state.timeseries;
    if (!d) return;
    clear(grid);
    const t1 = state.now ? Math.max(Date.now(), new Date(state.now.ts).getTime()) : Date.now();
    const t0 = t1 - 24 * 3600000;
    const VW = 1000, VH = 100, GAP_MS = 20 * 60000;
    const x = (t) => ((t - t0) / (t1 - t0)) * VW;
    const gpus = Object.keys(d.gpus).map(Number).sort((a, b) => a - b);
    for (const gpu of gpus) {
      const pts = d.gpus[String(gpu)].map((p) => ({ t: new Date(p.ts).getTime(), w: p.power_w, u: p.util_pct }))
        .filter((p) => p.t >= t0);
      const box = el("div", "spark");
      const head = el("div", "spark-head");
      head.appendChild(el("b", "", "GPU " + gpu));
      if (pts.length) {
        const avg = pts.reduce((a, p) => a + p.w, 0) / pts.length;
        const peak = Math.max(...pts.map((p) => p.w));
        head.appendChild(el("span", "muted", "avg " + Math.round(avg) + " W · peak " + Math.round(peak) + " W"));
      } else {
        head.appendChild(el("span", "muted", "no samples"));
      }
      box.appendChild(head);

      const s = svg("svg", { viewBox: "0 0 " + VW + " " + VH, preserveAspectRatio: "none", role: "img" });
      addTitle(s, "GPU " + gpu + ": power (line) and utilisation (shaded), last 24 h");
      // split into runs at monitoring gaps so the line does not bridge downtime
      const runs = [];
      let run = [];
      for (const p of pts) {
        if (run.length && p.t - run[run.length - 1].t > GAP_MS) { runs.push(run); run = []; }
        run.push(p);
      }
      if (run.length) runs.push(run);
      let area = "", line = "";
      for (const r of runs) {
        const py = (p) => (VH - (Math.min(p.w, MAX_W) / MAX_W) * VH).toFixed(1);
        const uy = (p) => (VH - (Math.min(p.u, 100) / 100) * VH).toFixed(1);
        area += "M" + x(r[0].t).toFixed(1) + "," + VH;
        for (const p of r) area += "L" + x(p.t).toFixed(1) + "," + uy(p);
        area += "L" + x(r[r.length - 1].t).toFixed(1) + "," + VH + "Z";
        r.forEach((p, i) => { line += (i ? "L" : "M") + x(p.t).toFixed(1) + "," + py(p); });
      }
      s.appendChild(svg("line", { x1: 0, x2: VW, y1: VH - 0.5, y2: VH - 0.5 }, "base"));
      if (area) s.appendChild(svg("path", { d: area }, "util"));
      if (line) s.appendChild(svg("path", { d: line }, "power"));
      box.appendChild(s);

      const foot = el("div", "spark-foot");
      foot.appendChild(el("span", "", fmtTime(new Date(t0))));
      foot.appendChild(el("span", "", "0–700 W"));
      foot.appendChild(el("span", "", fmtTime(new Date(t1))));
      box.appendChild(foot);
      grid.appendChild(box);
    }
  }

  // ---------- controls + scheduling ----------
  function scheduleTimeline() {
    if (state.timelineTimer) clearInterval(state.timelineTimer);
    const every = state.timelineHours <= 24 ? 60000 : 300000;
    state.timelineTimer = setInterval(loadTimeline, every);
    $("refresh-indicator").title = "auto-refresh: live every 30 s, timeline every "
      + (every / 60000) + " min, usage and last 24 h every 5 min";
  }
  function bindButtons(groupId, attr, onPick) {
    const group = $(groupId);
    group.addEventListener("click", (ev) => {
      const b = ev.target.closest("button");
      if (!b || !group.contains(b) || b.classList.contains("active")) return;
      for (const other of group.querySelectorAll("button")) other.classList.toggle("active", other === b);
      onPick(b.getAttribute(attr));
    });
  }

  bindButtons("timeline-range", "data-hours", (v) => {
    state.timelineHours = Number(v);
    scheduleTimeline();
    loadTimeline();
  });
  bindButtons("usage-range", "data-days", (v) => { state.usageDays = Number(v); loadUsage(); });
  bindButtons("usage-by", "data-by", (v) => { state.usageBy = v; loadUsage(); });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { renderTimeline(); renderUsage(); }, 150);
  });

  loadNow().then(() => { loadTimeline(); loadUsage(); loadTimeseries(); });
  setInterval(loadNow, 30000);
  scheduleTimeline();
  setInterval(loadUsage, 300000);
  setInterval(loadTimeseries, 300000);
})();
