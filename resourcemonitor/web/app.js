// carrot GPUs dashboard: fetches the JSON API and renders it; the only writes are
// bookings (honour system) through the Bookings form and the Cancel button.
// All data enters the DOM through textContent or SVG attributes (never HTML strings).
"use strict";

// Minimum free MiB on `gpu` over [startMs, endMs), counting only uncancelled claims on that
// GPU (a claim is active from its start, inclusive, to its end, exclusive). The worst instant
// is always the window start or the start of a claim inside the window.
function freeVramMiB(claims, gpu, startMs, endMs, cardMiB) {
  const live = [];
  for (const c of claims) {
    if (c.gpu !== gpu || c.cancelled_at) continue;
    const s = Date.parse(c.start), e = Date.parse(c.end);
    if (s < endMs && e > startMs) live.push({ s: s, e: e, mib: c.vram_mib });
  }
  let worst = 0;
  for (const t of [startMs].concat(live.map((c) => c.s).filter((s) => s > startMs))) {
    let sum = 0;
    for (const c of live) if (c.s <= t && t < c.e) sum += c.mib;
    worst = Math.max(worst, sum);
  }
  return Math.max(0, cardMiB - worst);
}

// Per-key debounce: deliver(key, value) runs delayMs after the last push for that key, with
// only the latest value; flush(key) delivers a pending value at once. schedule/cancel are
// setTimeout/clearTimeout in the page and a fake clock in the tests.
function makeDebouncer(delayMs, deliver, schedule, cancel) {
  const pending = new Map();   // key -> { value, timer }
  function fire(key) {
    const p = pending.get(key);
    if (!p) return;
    pending.delete(key);
    deliver(key, p.value);
  }
  return {
    push(key, value) {
      const p = pending.get(key);
      if (p) cancel(p.timer);
      pending.set(key, { value: value, timer: schedule(() => fire(key), delayMs) });
    },
    flush(key) {
      const p = pending.get(key);
      if (p) { cancel(p.timer); fire(key); }
    },
    pending(key) { return pending.has(key); },
  };
}
// VRAM panel stacking order: users alphabetically, "(unattributed)" last; only buckets that
// held some VRAM in `points` ([{by_user: {bucket: mib}}]).
function vramBucketOrder(points) {
  const seen = new Set();
  for (const p of points) for (const b in p.by_user) if (p.by_user[b] > 0) seen.add(b);
  const unatt = "(unattributed)";
  const users = Array.from(seen).filter((b) => b !== unatt).sort();
  return seen.has(unatt) ? users.concat([unatt]) : users;
}

// Stacked bands, bottom-up in `order`: per bucket, its lower and upper edge at every point
// (a bucket missing from a point counts as 0).
function stackSeries(points, order) {
  const base = points.map(() => 0);
  return order.map((bucket) => {
    const lower = base.slice();
    const upper = points.map((p, i) => (base[i] += p.by_user[bucket] || 0));
    return { bucket: bucket, lower: lower, upper: upper };
  });
}

// Booked share on `gpu` over [t0, t1] as a step line [{t, mib}]: the sum of the claims active
// from each point on (a claim holds from its start, inclusive, to its end or cancellation,
// exclusive). [] when no claim on that GPU touches the window.
function bookedSteps(claims, gpu, t0, t1) {
  const live = [];
  for (const c of claims) {
    if (c.gpu !== gpu) continue;
    const s = Date.parse(c.start);
    let e = Date.parse(c.end);
    if (c.cancelled_at) e = Math.min(e, Date.parse(c.cancelled_at));
    if (s < t1 && e > t0 && e > s) live.push({ s: s, e: e, mib: c.vram_mib });
  }
  if (!live.length) return [];
  const times = new Set([t0]);
  for (const c of live) for (const t of [c.s, c.e]) if (t > t0 && t < t1) times.add(t);
  const steps = Array.from(times).sort((a, b) => a - b).map((t) => {
    let mib = 0;
    for (const c of live) if (c.s <= t && t < c.e) mib += c.mib;
    return { t: t, mib: mib };
  });
  steps.push({ t: t1, mib: steps[steps.length - 1].mib });
  return steps;
}
if (typeof module !== "undefined") {
  module.exports = { freeVramMiB, makeDebouncer, vramBucketOrder, stackSeries, bookedSteps };
}

// Start-up only in a browser: Node loads this file for the tests above.
if (typeof document !== "undefined") (function () {
  // Split so the file holds no URL literal (it is a namespace name, never fetched).
  const SVG_NS = "http:" + "//www.w3.org/2000/svg";
  const PALETTE_SIZE = 8;
  const EUR_PER_KWH = 0.30;
  const MAX_W = 700;
  const ALERT_ICONS = {
    booked_gpu: "⚠", over_booking: "▲", idle: "◔", capacity: "▣", unattributed: "?", report: "Σ",
  };
  const RED_ALERTS = new Set(["booked_gpu", "over_booking"]);   // the only kinds that turn a card red
  const DEFAULT_CARD_MIB = 81559;                                // same fallback as the server
  const DEFAULT_GPUS = [0, 1, 2, 3, 4, 5, 6, 7];
  const H_MS = 3600000, DAY_MS = 24 * H_MS;
  const CALENDAR_DAYS = 14;
  const CHANGES_SHOWN = 20;
  const USER_KEY = "rm.user";
  const HOLDER_ERROR_MS = 20000;              // how long a card shows a quick-booking error
  // Arrow keys / type-ahead on a closed select fire change per keystroke (Windows, Linux):
  // only the pick that stays this long is booked. Enter and leaving the dropdown send at once.
  const HOLDER_DEBOUNCE_MS = 600;

  const state = {
    now: null, timeline: null, usage: null, timeseries: null,
    vram: null,              // GET /api/vram?hours=24: VRAM per GPU, by user
    timelineHours: 24, usageDays: 7, usageBy: "day",
    users: [],               // sorted, every user ever seen this session
    inflight: 0, failed: new Set(),
    timelineSeq: 0, usageSeq: 0,
    timelineTimer: null,
    claims: null,            // GET /api/claims: the calendar, the form and recent changes
    timelineClaims: [],      // GET /api/claims?days=1&back=N: bands behind the timeline
    bookUsers: null,         // GET /api/users: who may book
    selectedClaim: null,     // id shown in the details panel
    cancelArmed: null,       // id whose "Cancel booking?" step is showing
    untilMode: "4h",         // "4h" | "1d" | "fri" | "custom"
    fromAuto: true,          // "from" follows the clock until the user edits it
    booking: false,          // a POST is in flight
    holderWant: new Map(),   // gpu -> the latest holder picked on its card ("" = free)
    holderBusy: new Set(),   // gpus with a quick-booking POST in flight
    holderErrors: new Map(), // gpu -> the last quick-booking error shown on its card
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

  // ---------- bookings: shared helpers ----------
  function nowGpu(gpu) {
    return state.now ? state.now.gpus.find((x) => x.gpu === gpu) || null : null;
  }
  function cardMiB(gpu) {
    const g = nowGpu(gpu);
    return g && g.total_mib ? g.total_mib : DEFAULT_CARD_MIB;
  }
  function gpuIds() {
    return state.now && state.now.gpus.length ? state.now.gpus.map((g) => g.gpu).sort((a, b) => a - b)
      : DEFAULT_GPUS;
  }
  // When a claim really held its share: a cancelled one stops at its cancellation (null: never).
  function claimSpan(c) {
    const s = Date.parse(c.start);
    let e = Date.parse(c.end);
    if (c.cancelled_at) e = Math.min(e, Date.parse(c.cancelled_at));
    return e > s ? { s: s, e: e } : null;
  }
  // Display hint only (the VRAM-precise judgement is the live booked_gpu alert): the job's user
  // had no booking on this GPU while someone else's booking was active during part of the job.
  function onSomeoneElsesBooking(user, gpu, s, e) {
    if (user === null) return false;          // unattributed is never accused
    let own = false, other = false;
    for (const c of state.timelineClaims) {
      if (c.gpu !== gpu) continue;
      const w = claimSpan(c);
      if (!w || w.s >= e || w.e <= s) continue;
      if (c.user === user) own = true; else other = true;
    }
    return other && !own;
  }
  function gibText(mib) {                       // 40960 -> "40", 40500 -> "39.6"
    const g = mib / 1024;
    return Number.isInteger(g) ? String(g) : g.toFixed(1);
  }
  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }
  // "18:00" today, "Fri 18:00" within a week either way, "Fri 16 Oct 18:00" further off.
  function fmtWhen(d) {
    const now = new Date();
    if (sameDay(d, now)) return fmtTime(d);
    const opts = Math.abs(d - now) < 6 * DAY_MS ? { weekday: "short" }
      : { weekday: "short", day: "2-digit", month: "short" };
    return d.toLocaleDateString(undefined, opts) + " " + fmtTime(d);
  }
  // ISO with the browser's offset, as the server requires: 2026-10-09T18:00:00+02:00
  function isoWithOffset(d) {
    const off = -d.getTimezoneOffset(), a = Math.abs(off);
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate())
      + "T" + pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":00"
      + (off < 0 ? "-" : "+") + pad2(Math.floor(a / 60)) + ":" + pad2(a % 60);
  }
  function inputValue(d) {                      // value for <input type="datetime-local">
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate())
      + "T" + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }
  function parseInput(v) {                      // "YYYY-MM-DDTHH:MM" is local time; NaN if empty
    return v ? new Date(v).getTime() : NaN;
  }
  function nextFriday18(fromMs) {               // the next Friday 18:00 local strictly after fromMs
    const d = new Date(fromMs);
    d.setHours(18, 0, 0, 0);
    d.setDate(d.getDate() + ((5 - d.getDay() + 7) % 7));
    if (d.getTime() <= fromMs) d.setDate(d.getDate() + 7);
    return d.getTime();
  }
  function storedUser() {
    try { return window.localStorage.getItem(USER_KEY); } catch (e) { return null; }
  }
  function storeUser(name) {
    try { window.localStorage.setItem(USER_KEY, name); } catch (e) { /* page works without it */ }
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
      if (r.status === 503) {
        let body = null;
        try { body = await r.json(); } catch (e) { /* not JSON: treat as no history */ }
        // "busy" is transient: keep the page and retry on the next tick, do not blank it
        if (body && body.error === "busy") throw new Error("busy");
        setFailed(key, false); showEmpty(true); return null;
      }
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
    for (const g of data.gpus) {
      for (const b of g.bookings) names.push(b.user);
      for (const p of g.procs) names.push(p.user);
    }
    if (registerUsers(names)) renderAll(); else { renderNow(); renderTimeline(); renderBookings(); }
  }
  async function loadTimeline() {
    const seq = ++state.timelineSeq;
    const [data, claims] = await Promise.all([
      getJson("timeline", "/api/timeline?hours=" + state.timelineHours),
      // bookings for the whole visible range (+1 day of margin), capped by the server at 30 days back
      getJson("timeline-claims", "/api/claims?days=1&back="
        + Math.min(30, Math.ceil(state.timelineHours / 24) + 1)),
    ]);
    if (!data || seq !== state.timelineSeq) return;
    state.timeline = data;
    if (claims) state.timelineClaims = claims.claims;
    const names = state.timelineClaims.map((c) => c.user);
    for (const k in data.gpus) for (const j of data.gpus[k]) names.push(j.user);
    if (registerUsers(names)) renderAll(); else { renderTimeline(); renderVram(); }   // claims feed both
  }
  async function loadClaims() {
    const data = await getJson("claims", "/api/claims");
    if (!data) return;
    state.claims = data.claims;
    if (registerUsers(data.claims.map((c) => c.user))) renderAll(); else renderBookings();
  }
  async function loadBookUsers() {
    const data = await getJson("users", "/api/users");
    if (!data) return;
    state.bookUsers = data.users;
    renderUserSelect();
    renderNow();                                  // the cards' holder dropdowns list the users
  }
  // POST a JSON body; resolves to {status, body} (body null when not JSON), status 0 if unreachable.
  async function postJson(url, payload) {
    setBusy(1);
    try {
      const r = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      let body = null;
      try { body = await r.json(); } catch (e) { /* not JSON */ }
      return { status: r.status, body: body };
    } catch (e) {
      return { status: 0, body: null };
    } finally {
      setBusy(-1);
    }
  }
  function errorText(res) {
    if (res.status === 0) return "Couldn't reach the server — nothing was booked or cancelled.";
    const b = res.body || {};
    if (b.detail) return b.detail;                        // the server's detail, verbatim
    if (res.status === 429) return "Too many bookings from this computer in the last hour — try again later.";
    if (res.status === 403) return "This request was refused (cross-site check).";
    return b.error || "HTTP " + res.status;
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
  async function loadVram() {
    const data = await getJson("vram", "/api/vram?hours=24");
    if (!data) return;                            // keep the last data; getJson shows why
    state.vram = data;
    const names = [];
    for (const k in data.gpus) for (const p of data.gpus[k].points) names.push(...Object.keys(p.by_user));
    if (registerUsers(names)) renderAll(); else renderVram();
  }

  function renderAll() {
    renderNow(); renderBookings(); renderTimeline(); renderUsage(); renderTimeseries(); renderVram();
  }

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
    const red = new Set(d.alerts.filter((a) => RED_ALERTS.has(a.kind)).map((a) => a.gpu));
    // A holder dropdown that has focus (or is open) is never rebuilt under the user: its
    // card is refreshed around it instead, and the dropdown catches up after the change.
    const keep = document.activeElement;
    if (!(keep && keep.classList.contains("holder") && grid.contains(keep))) {
      clear(grid);
      for (const g of d.gpus) grid.appendChild(gpuCard(g, red.has(g.gpu)));
    } else {
      const oldCard = keep.closest(".card");
      for (const c of Array.from(grid.childNodes)) if (c !== oldCard) grid.removeChild(c);
      let before = true;
      for (const g of d.gpus) {
        const card = gpuCard(g, red.has(g.gpu));
        if (String(g.gpu) === keep.dataset.gpu) { refreshCardAround(oldCard, card, keep.parentNode); before = false; }
        else if (before) grid.insertBefore(card, oldCard);
        else grid.appendChild(card);
      }
    }

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

  function gpuCard(g, red) {
    const card = el("div", "card" + (red ? " flagged" : ""));
    const head = el("div", "card-head");
    head.appendChild(el("span", "gpu", "GPU " + g.gpu));
    head.appendChild(holderNode(g));
    head.appendChild(el("span", "free", fmtGiB(g.free_mib) + " free"));
    card.appendChild(head);
    const err = state.holderErrors.get(g.gpu);
    if (err) card.appendChild(el("p", "holder-error", err));

    const booked = el("ul", "card-bookings");
    if (!g.bookings.length) booked.appendChild(el("li", "none", "not booked"));
    for (const b of g.bookings.slice(0, 2)) {
      const li = el("li");
      li.appendChild(swatch(b.user));
      li.appendChild(el("span", "", b.user + " " + gibText(b.vram_mib) + " GiB until " + fmtWhen(new Date(b.end))));
      if (b.note) li.title = b.note;
      booked.appendChild(li);
    }
    if (g.bookings.length > 2) booked.appendChild(el("li", "more", "+" + (g.bookings.length - 2) + " more"));
    card.appendChild(booked);

    // VRAM bar: booked shares as outlined segments, actual usage filled on top
    // (one segment per process in its user's colour; the rest of "used" in grey).
    const VB_H = 12;
    const bar = svg("svg", { viewBox: "0 0 1000 " + VB_H, preserveAspectRatio: "none", role: "img" }, "vram");
    bar.appendChild(svg("rect", { x: 0, y: 0, width: 1000, height: VB_H, rx: 0 }, "track"));
    const total = g.total_mib || 1;
    let bx = 0;
    for (const b of g.bookings) {
      const w = Math.min(1000 - bx, (b.vram_mib / total) * 1000);
      if (w <= 0) continue;
      const seg = svg("rect", { x: bx, y: 0.75, width: w, height: VB_H - 1.5 }, "booked " + colourClass(b.user));
      addTitle(seg, b.user + " booked " + gibText(b.vram_mib) + " GiB until " + fmtWhen(new Date(b.end)));
      bar.appendChild(seg);
      bx += w;
    }
    let x = 0, procMib = 0;
    for (const p of g.procs) {
      const w = Math.min(1000 - x, (p.used_mib / total) * 1000);
      if (w <= 0) continue;
      const seg = svg("rect", { x: x, y: 3, width: w, height: VB_H - 6 }, colourClass(p.user));
      addTitle(seg, (p.user || "unattributed") + " · " + p.used_mib + " MiB");
      bar.appendChild(seg);
      x += w; procMib += p.used_mib;
    }
    const rest = Math.min(1000 - x, (Math.max(0, g.used_mib - procMib) / total) * 1000);
    if (rest > 0) bar.appendChild(svg("rect", { x: x, y: 3, width: rest, height: VB_H - 6 }, "idle"));
    addTitle(bar, g.used_mib + " / " + g.total_mib + " MiB used · " + g.booked_mib + " MiB booked");
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

  // ---------- quick booking: the holder dropdown on each card ----------
  // The GPU's quick booking (whole card until 09:00) and whether a calendar booking is active.
  function holderOf(g) {
    const cal = g.bookings.filter((b) => b.kind !== "quick");
    return {
      quick: g.bookings.find((b) => b.kind === "quick") || null,
      calendar: cal.length > 0,
      wholeCard: cal.length > 0 && cal.reduce((s, b) => s + b.vram_mib, 0) >= g.total_mib,
    };
  }
  function holderHint(h) {
    if (h.calendar) return h.wholeCard ? "booked — use the calendar" : "partly booked — use the calendar";
    return h.quick ? "until " + fmtTime(new Date(h.quick.end)) : "";
  }
  function holderNode(g) {
    const h = holderOf(g);
    const wrap = el("span", "holder-wrap");
    const sel = el("select", "holder");
    sel.setAttribute("aria-label", "holder of GPU " + g.gpu);
    sel.dataset.gpu = String(g.gpu);
    if (h.calendar) {                     // the quick dropdown never touches calendar bookings
      sel.appendChild(el("option", "", "— calendar —"));
      sel.disabled = true;
    } else {
      const free = el("option", "", "— free —");
      free.value = "";
      sel.appendChild(free);
      const names = (state.bookUsers || []).slice();
      if (h.quick && !names.includes(h.quick.user)) names.push(h.quick.user);
      for (const u of names) {
        const o = el("option", "", u);
        o.value = u;
        sel.appendChild(o);
      }
      sel.value = h.quick ? h.quick.user : "";
      sel.addEventListener("change", () => holderDebounce.push(g.gpu, { sel: sel, value: sel.value }));
      sel.addEventListener("keydown", (e) => { if (e.key === "Enter") holderDebounce.flush(g.gpu); });
      sel.addEventListener("blur", () => holderDebounce.flush(g.gpu));
    }
    wrap.appendChild(sel);
    wrap.appendChild(el("span", "hint holder-hint", holderHint(h)));
    return wrap;
  }
  // Replace oldNode's children with newNode's, except `kept` (in oldNode), which stays where
  // `slot` (in newNode) is.
  function replaceAround(oldNode, newNode, kept, slot) {
    for (const c of Array.from(oldNode.childNodes)) if (c !== kept) oldNode.removeChild(c);
    let after = false;
    for (const c of Array.from(newNode.childNodes)) {
      if (c === slot) after = true;
      else if (after) oldNode.appendChild(c);
      else oldNode.insertBefore(c, kept);
    }
    oldNode.className = newNode.className;
  }
  function refreshCardAround(oldCard, newCard, oldWrap) {
    const newWrap = newCard.querySelector(".holder-wrap");
    replaceAround(oldCard, newCard, oldWrap.parentNode, newWrap.parentNode);
    replaceAround(oldWrap.parentNode, newWrap.parentNode, oldWrap, newWrap);
  }
  // Show the server's state in a dropdown that was kept because it had focus.
  function syncHolder(gpu, sel) {
    const g = nowGpu(gpu);
    if (!g || !sel.isConnected || holderDebounce.pending(gpu)) return;   // a newer pick waits
    const h = holderOf(g);
    if (h.calendar) { sel.blur(); renderNow(); return; }   // rebuilt as the disabled dropdown
    const want = h.quick ? h.quick.user : "";
    if (want && !Array.from(sel.options).some((o) => o.value === want)) { sel.blur(); renderNow(); return; }
    if (sel.value !== want) sel.value = want;
    sel.parentNode.querySelector(".holder-hint").textContent = holderHint(h);
  }
  const holderDebounce = makeDebouncer(HOLDER_DEBOUNCE_MS,
    (gpu, pick) => setHolder(gpu, pick.sel, pick.value),
    (fn, ms) => setTimeout(fn, ms), (t) => clearTimeout(t));
  // One request per GPU at a time; picks made meanwhile are sent next, the latest one wins.
  async function setHolder(gpu, sel, value) {
    state.holderWant.set(gpu, value);
    if (state.holderBusy.has(gpu)) return;
    state.holderBusy.add(gpu);
    let res, sent;
    try {
      do {
        sent = state.holderWant.get(gpu);
        res = await postJson("/api/claims/quick", { user: sent || null, gpu: gpu });
      } while ((res.status === 200 || res.status === 201) && state.holderWant.get(gpu) !== sent);
    } finally {
      state.holderBusy.delete(gpu);
      state.holderWant.delete(gpu);
    }
    if (res.status === 200 || res.status === 201) state.holderErrors.delete(gpu);
    else {
      const text = errorText(res);
      state.holderErrors.set(gpu, text);
      setTimeout(() => {                      // the error line goes after a while
        if (state.holderErrors.get(gpu) === text) { state.holderErrors.delete(gpu); renderNow(); }
      }, HOLDER_ERROR_MS);
    }
    await loadNow();
    syncHolder(gpu, sel);
    renderNow();                              // shows or clears the card's error line
    loadClaims();
  }

  // ---------- Bookings ----------
  function renderBookings() {
    if (!state.claims) return;
    renderGpuOptions();
    renderPreview();
    renderCalendar();
    renderDetail();
    renderChanges();
  }

  // The window the form describes, in ms (NaN when unset or unparsable).
  function formWindow() {
    if (state.fromAuto) {
      const now = new Date();
      now.setSeconds(0, 0);
      $("book-from").value = inputValue(now);
    }
    const from = parseInput($("book-from").value);
    let until = NaN;
    if (!isNaN(from)) {
      if (state.untilMode === "4h") until = from + 4 * H_MS;
      else if (state.untilMode === "1d") until = from + DAY_MS;
      else if (state.untilMode === "fri") until = nextFriday18(from);
      else until = parseInput($("book-until").value);
    }
    return { from: from, until: until };
  }
  function windowFree(gpu, w) {
    if (isNaN(w.from) || isNaN(w.until) || w.until <= w.from) return cardMiB(gpu);
    return freeVramMiB(state.claims || [], gpu, w.from, w.until, cardMiB(gpu));
  }

  function renderUserSelect() {
    const sel = $("book-user");
    const keep = sel.value || storedUser();
    clear(sel);
    const ph = el("option", "", "— who are you? —");
    ph.value = "";
    sel.appendChild(ph);
    for (const u of state.bookUsers || []) {
      const o = el("option", "", u);
      o.value = u;
      sel.appendChild(o);
    }
    if (keep && (state.bookUsers || []).includes(keep)) sel.value = keep;
  }

  function renderGpuOptions() {
    const sel = $("book-gpu");
    const keep = sel.value;
    const w = formWindow();
    clear(sel);
    for (const gpu of gpuIds()) {
      const o = el("option", "", "GPU " + gpu + " — " + fmtGiB(windowFree(gpu, w)) + " free");
      o.value = String(gpu);
      sel.appendChild(o);
    }
    if (keep !== "") sel.value = keep;
  }

  function renderPreview() {
    const w = formWindow();
    const gpu = Number($("book-gpu").value);
    const vram = $("book-vram");
    const preview = $("book-preview");
    $("book-until-text").textContent = isNaN(w.until) ? "" : "until " + fmtWhen(new Date(w.until));
    if (isNaN(w.from) || isNaN(w.until)) { preview.textContent = "Pick when the booking starts and ends."; return; }
    if (w.until <= w.from) { preview.textContent = "“Until” must be after “from”."; return; }
    const free = windowFree(gpu, w), card = cardMiB(gpu);
    vram.max = String(Math.floor(free / 1024));
    const span = " on GPU " + gpu + " from " + fmtWhen(new Date(w.from)) + " until " + fmtWhen(new Date(w.until));
    if (!vram.value) {                          // VRAM is optional: empty books the whole card
      preview.textContent = free < card
        ? "Only " + fmtGiB(free) + " of GPU " + gpu + " is free in that window — enter less VRAM or pick another time."
        : "You'd book the whole card (" + fmtGiB(card) + ")" + span + ".";
      return;
    }
    const gib = Number(vram.value);
    if (!(gib > 0)) {
      preview.textContent = "GPU " + gpu + " has " + fmtGiB(free) + " of " + fmtGiB(card)
        + " free from " + fmtWhen(new Date(w.from)) + " until " + fmtWhen(new Date(w.until)) + ".";
      return;
    }
    const left = free - gib * 1024;
    preview.textContent = left < 0
      ? "Only " + fmtGiB(free) + " of GPU " + gpu + " is free in that window — book less or pick another time."
      : "You'd book " + gibText(gib * 1024) + " of " + fmtGiB(card) + span
        + "; " + fmtGiB(left) + " stays free.";
  }

  function showBookError(text) {
    const e = $("book-error");
    e.textContent = text || "";
    e.hidden = !text;
  }

  async function submitBooking(ev) {
    ev.preventDefault();
    if (state.booking) return;
    const w = formWindow();
    const user = $("book-user").value;
    const note = $("book-note").value.trim();
    $("book-ok").hidden = true;
    if (!user) { showBookError("Pick your user name first."); return; }
    if (isNaN(w.from) || isNaN(w.until)) { showBookError("Pick when the booking starts and ends."); return; }
    const payload = {
      user: user, gpu: Number($("book-gpu").value),
      start: isoWithOffset(new Date(w.from)), end: isoWithOffset(new Date(w.until)),
    };
    const vram = $("book-vram").value;
    if (vram) payload.vram_gib = Number(vram);  // omitted: the server books the whole card
    if (note) payload.note = note;
    state.booking = true;
    $("book-submit").disabled = true;
    const res = await postJson("/api/claims", payload);
    state.booking = false;
    $("book-submit").disabled = false;
    if (res.status !== 201 || !res.body || !res.body.claim) { showBookError(errorText(res)); return; }
    showBookError("");
    const c = res.body.claim;
    const ok = $("book-ok");
    ok.textContent = "Booked GPU " + c.gpu + ", " + gibText(c.vram_mib) + " GiB for " + c.user
      + " until " + fmtWhen(new Date(c.end)) + ".";
    ok.hidden = false;
    $("book-note").value = "";
    state.selectedClaim = c.id;
    state.cancelArmed = null;
    loadClaims();
    loadNow();
  }

  // Bars are stacked by booked share: each claim (in start order) takes the lowest offset that
  // does not collide with an already placed claim overlapping it in time. Offsets are 0..1.
  function stackClaims(claims) {
    const placed = [];
    const sorted = claims.slice().sort((a, b) => a.s - b.s || a.c.id - b.c.id);
    for (const it of sorted) {
      const over = placed.filter((p) => p.s < it.e && it.s < p.e);
      const cands = [0].concat(over.map((p) => p.off + p.h)).sort((a, b) => a - b);
      const fits = (off) => over.every((p) => off + it.h <= p.off + 1e-9 || off >= p.off + p.h - 1e-9);
      it.off = cands.find((off) => fits(off) && off + it.h <= 1 + 1e-9);
      if (it.off === undefined) it.off = cands.find(fits);
      placed.push(it);
    }
    return sorted;
  }

  function renderCalendar() {
    const wrap = $("booking-calendar");
    clear(wrap);
    const W = Math.max(560, wrap.clientWidth || 0);
    const labelW = 56, padR = 12, axisH = 22, rowH = 46, rowPad = 3;
    const t0 = Date.now(), t1 = t0 + CALENDAR_DAYS * DAY_MS;
    const ids = gpuIds();
    const H = axisH + ids.length * rowH + 4;
    const plotW = W - labelW - padR;
    const x = (t) => labelW + ((Math.min(Math.max(t, t0), t1) - t0) / (t1 - t0)) * plotW;
    const chart = svg("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img" }, "calendar");
    addTitle(chart, "Bookings per GPU, now to +" + CALENDAR_DAYS + " days; bar height = booked share of the card");

    ids.forEach((gpu, i) => {
      const y = axisH + i * rowH;
      if (i % 2 === 0) chart.appendChild(svg("rect", { x: 0, y: y, width: W, height: rowH }, "row-band"));
      chart.appendChild(svgText(8, y + rowH / 2 + 4, "GPU " + gpu, "row-label"));
    });
    // day ticks at local midnight, the day's name centred in its span
    const d = new Date(t0);
    d.setHours(0, 0, 0, 0);
    for (; d.getTime() < t1; d.setDate(d.getDate() + 1)) {
      const s = d.getTime(), next = new Date(d); next.setDate(next.getDate() + 1);
      if (s > t0) chart.appendChild(svg("line", { x1: x(s), x2: x(s), y1: axisH - 4, y2: H - 4 }, "grid"));
      const xs = x(s), xe = x(next.getTime()), cx = (xs + xe) / 2;
      if (cx - labelW < 34) continue;               // keep "now" legible
      const label = xe - xs >= 50 ? d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" })
        : xe - xs >= 18 ? String(d.getDate()) : "";
      if (label) chart.appendChild(svgText(cx, 13, label, "", "middle"));
    }
    chart.appendChild(svg("line", { x1: labelW, x2: labelW, y1: axisH - 4, y2: H - 4 }, "axis-line"));

    let any = false;
    ids.forEach((gpu, i) => {
      const rowY = axisH + i * rowH + rowPad, inner = rowH - 2 * rowPad, card = cardMiB(gpu);
      const items = [];
      for (const c of state.claims) {
        if (c.gpu !== gpu || c.cancelled_at) continue;
        const s = Date.parse(c.start), e = Date.parse(c.end);
        if (e <= t0 || s >= t1) continue;
        items.push({ c: c, s: s, e: e, h: Math.min(1, c.vram_mib / card) });
      }
      for (const it of stackClaims(items)) {
        any = true;
        const c = it.c;
        const bx = x(it.s), bw = Math.max(3, x(it.e) - bx);
        const bh = Math.max(3, it.h * inner - 1);
        const by = rowY + inner - (it.off * inner) - bh;   // first booking sits on the row's floor
        const g = svg("g", { tabindex: 0, role: "button" },
          "cal-bar" + (state.selectedClaim === c.id ? " selected" : ""));
        g.appendChild(svg("rect", { x: bx, y: by, width: bw, height: bh, rx: 2 }, colourClass(c.user)));
        // longest label that fits: with the note, "user · NN GiB", then just "NN GiB"
        const full = c.user + " · " + gibText(c.vram_mib) + " GiB";
        const maxChars = Math.floor((bw - 8) / 6.3);
        const label = [c.note ? full + " · " + c.note : null, full, gibText(c.vram_mib) + " GiB"]
          .find((t) => t !== null && t.length <= maxChars);
        if (bh >= 13 && label) g.appendChild(svgText(bx + 4, by + bh / 2 + 4, label, "bar-label"));
        addTitle(g, c.user + " · " + gibText(c.vram_mib) + " GiB on GPU " + gpu + "\n"
          + fmtLocal(new Date(c.start)) + " – " + fmtLocal(new Date(c.end))
          + (c.note ? "\n" + c.note : "") + "\nclick for details");
        const pick = () => { state.selectedClaim = c.id; state.cancelArmed = null; renderCalendar(); renderDetail(); };
        g.addEventListener("click", pick);
        g.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); }
        });
        chart.appendChild(g);
      }
    });
    if (!any) {
      chart.appendChild(svgText(labelW + plotW / 2, axisH + (ids.length * rowH) / 2,
        "No bookings in the next " + CALENDAR_DAYS + " days — every GPU is free", "empty-note", "middle"));
    }
    chart.appendChild(svg("line", { x1: labelW, x2: labelW, y1: axisH - 6, y2: H - 2 }, "now-line"));
    chart.appendChild(svgText(labelW, 13, "now", "now-label", "start"));
    wrap.appendChild(chart);
  }

  function detailRow(dl, term, value) {
    dl.appendChild(el("dt", "", term));
    dl.appendChild(el("dd", "", value));
  }
  function renderDetail() {
    const box = $("booking-detail");
    const c = state.claims && state.claims.find((x) => x.id === state.selectedClaim);
    clear(box);
    box.hidden = !c;
    if (!c) return;
    const head = el("div", "detail-head");
    head.appendChild(swatch(c.user));
    head.appendChild(el("b", "", c.user + " · GPU " + c.gpu + " · " + gibText(c.vram_mib) + " GiB"));
    const close = el("button", "link", "close");
    close.type = "button";
    close.addEventListener("click", () => {
      state.selectedClaim = null; state.cancelArmed = null; renderCalendar(); renderDetail();
    });
    head.appendChild(close);
    box.appendChild(head);
    const dl = el("dl");
    detailRow(dl, "window", fmtLocal(new Date(c.start)) + " – " + fmtLocal(new Date(c.end)));
    if (c.note) detailRow(dl, "note", c.note);
    detailRow(dl, "booked", fmtLocal(new Date(c.created_at)) + " from " + c.created_ip);
    if (c.cancelled_at) detailRow(dl, "cancelled", fmtLocal(new Date(c.cancelled_at)) + " from " + c.cancelled_ip);
    box.appendChild(dl);

    const actions = el("div", "detail-actions");
    const err = el("p", "book-error");
    err.hidden = true;
    if (!c.cancelled_at && Date.parse(c.end) > Date.now()) {
      if (state.cancelArmed !== c.id) {
        const b = el("button", "danger", "Cancel booking");
        b.type = "button";
        b.addEventListener("click", () => { state.cancelArmed = c.id; renderDetail(); });
        actions.appendChild(b);
      } else {
        actions.appendChild(el("span", "confirm-q", "Cancel booking?"));
        const yes = el("button", "danger", "Yes, cancel");
        yes.type = "button";
        const keep = el("button", "", "Keep");
        keep.type = "button";
        keep.addEventListener("click", () => { state.cancelArmed = null; renderDetail(); });
        yes.addEventListener("click", async () => {
          yes.disabled = true; keep.disabled = true;
          const res = await postJson("/api/claims/" + c.id + "/cancel", {});
          if (res.status !== 200) {
            yes.disabled = false; keep.disabled = false;
            err.textContent = errorText(res); err.hidden = false;
            return;
          }
          state.cancelArmed = null;
          loadClaims();
          loadNow();
        });
        actions.appendChild(yes);
        actions.appendChild(keep);
      }
    } else {
      actions.appendChild(el("span", "muted", c.cancelled_at ? "cancelled" : "ended"));
    }
    box.appendChild(actions);
    box.appendChild(err);
  }

  function renderChanges() {
    const ul = $("booking-changes");
    clear(ul);
    const events = [];
    for (const c of state.claims) {
      events.push({ t: Date.parse(c.created_at), what: "booked", ip: c.created_ip, c: c });
      if (c.cancelled_at) events.push({ t: Date.parse(c.cancelled_at), what: "cancelled", ip: c.cancelled_ip, c: c });
    }
    events.sort((a, b) => b.t - a.t || b.c.id - a.c.id);
    if (!events.length) ul.appendChild(el("li", "none", "No bookings yet."));
    for (const ev of events.slice(0, CHANGES_SHOWN)) {
      const c = ev.c;
      const li = el("li", ev.what);
      li.appendChild(swatch(c.user));
      // the window's start only when it was not "right away"
      const from = Date.parse(c.start) - Date.parse(c.created_at) > 5 * 60000
        ? " from " + fmtWhen(new Date(c.start)) : "";
      li.appendChild(el("span", "", fmtWhen(new Date(ev.t)) + " · " + ev.what + " GPU " + c.gpu + ", "
        + gibText(c.vram_mib) + " GiB, for " + c.user + from + " until " + fmtWhen(new Date(c.end))
        + " · from " + ev.ip));
      if (c.note) li.title = c.note;
      ul.appendChild(li);
    }
  }

  function bindBookingForm() {
    $("book-form").addEventListener("submit", submitBooking);
    $("book-user").addEventListener("change", () => { if ($("book-user").value) storeUser($("book-user").value); });
    $("book-gpu").addEventListener("change", renderPreview);
    $("book-vram").addEventListener("input", renderPreview);
    $("book-from").addEventListener("input", () => {
      state.fromAuto = $("book-from").value === "";
      renderGpuOptions(); renderPreview();
    });
    $("book-until").addEventListener("input", () => { renderGpuOptions(); renderPreview(); });
    const quick = $("book-until-quick");
    quick.addEventListener("click", (ev) => {
      const b = ev.target.closest("button");
      if (!b || !quick.contains(b)) return;
      const mode = b.getAttribute("data-until");
      if (mode === "custom" && state.untilMode !== "custom") {
        const w = formWindow();                    // start the custom value from the current pick
        if (!isNaN(w.until)) $("book-until").value = inputValue(new Date(w.until));
      }
      state.untilMode = mode;
      for (const other of quick.querySelectorAll("button")) other.classList.toggle("active", other === b);
      $("book-until").hidden = mode !== "custom";
      renderGpuOptions(); renderPreview();
    });
    renderUserSelect();
    renderGpuOptions();
    renderPreview();
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

  function bookedNowLabel(gpu) {
    const g = nowGpu(gpu);
    if (!g || !g.bookings.length) return "not booked";
    let label = "booked: " + Array.from(new Set(g.bookings.map((b) => b.user))).join(", ");
    if (label.length > 30) label = label.slice(0, 29) + "…";
    return label;
  }

  // Greedy sub-lanes for one GPU row: each job goes in the first lane whose last end
  // is <= its start. jobs: [{start, end}] in ms. Returns {lane: [index per job], count}.
  function packLanes(jobs) {
    const order = jobs.map((_, i) => i).sort((a, b) => jobs[a].start - jobs[b].start || a - b);
    const laneEnds = [], lane = new Array(jobs.length);
    for (const i of order) {
      let k = laneEnds.findIndex((end) => end <= jobs[i].start);
      if (k < 0) { k = laneEnds.length; laneEnds.push(0); }
      laneEnds[k] = jobs[i].end;
      lane[i] = k;
    }
    return { lane: lane, count: Math.max(1, laneEnds.length) };
  }

  function renderTimeline() {
    const wrap = $("timeline-chart"), legend = $("timeline-legend");
    const d = state.timeline;
    if (!d) return;
    clear(wrap); clear(legend);
    const gpuIds = new Set(Object.keys(d.gpus).map(Number));
    if (state.now) for (const g of state.now.gpus) gpuIds.add(g.gpu);

    const W = Math.max(640, wrap.clientWidth || 0);
    const labelW = 190, padR = 18, axisH = 22, barH = 22, laneGap = 4, rowPad = 5;
    const t0 = new Date(d.from).getTime(), t1 = new Date(d.to).getTime();
    // A stopped monitor cannot vouch for "still running": ongoing bars end at its last poll.
    const stale = Boolean(state.now && state.now.stale);
    const liveEnd = stale ? Math.min(t1, new Date(state.now.ts).getTime()) : t1;
    const endOf = (job) => (job.ongoing ? liveEnd : new Date(job.end).getTime());

    // One row per GPU, as many lanes as it has concurrent jobs (one lane = the old row).
    let rowY = axisH;
    const rows = Array.from(gpuIds).sort((a, b) => a - b).map((gpu) => {
      const jobs = d.gpus[String(gpu)] || [];
      const packed = packLanes(jobs.map((j) => ({ start: new Date(j.start).getTime(), end: endOf(j) })));
      const h = 2 * rowPad + packed.count * barH + (packed.count - 1) * laneGap;
      const row = { gpu: gpu, jobs: jobs, lane: packed.lane, y: rowY, h: h };
      rowY += h;
      return row;
    });
    const H = rowY + 6;
    const plotW = W - labelW - padR;
    const x = (t) => labelW + ((Math.min(Math.max(t, t0), t1) - t0) / (t1 - t0)) * plotW;

    const chart = svg("svg", { width: W, height: H, viewBox: "0 0 " + W + " " + H, role: "img" });
    addTitle(chart, "Timeline of GPU jobs, last " + state.timelineHours + " h");

    rows.forEach((r, i) => {
      if (i % 2 === 0) chart.appendChild(svg("rect", { x: 0, y: r.y, width: W, height: r.h }, "row-band"));
      chart.appendChild(svgText(8, r.y + 14, "GPU " + r.gpu, "row-label"));
      chart.appendChild(svgText(8, r.y + 27, bookedNowLabel(r.gpu), "row-sub"));
    });

    // Bookings as faint bands behind the job bars (x() clips them to the axis).
    let anyBand = false;
    for (const r of rows) {
      for (const c of state.timelineClaims) {
        const w = c.gpu === r.gpu ? claimSpan(c) : null;
        if (!w || w.e <= t0 || w.s >= t1) continue;
        anyBand = true;
        const band = svg("rect", { x: x(w.s), y: r.y + 1, width: Math.max(1, x(w.e) - x(w.s)), height: r.h - 2 },
          "band " + colourClass(c.user));
        addTitle(band, "booked by " + c.user + " · " + gibText(c.vram_mib) + " GiB · "
          + fmtLocal(new Date(c.start)) + " – " + fmtLocal(new Date(c.end))
          + (c.cancelled_at ? " (cancelled " + fmtLocal(new Date(c.cancelled_at)) + ")" : ""));
        chart.appendChild(band);
      }
    }

    const nowX = x(t1);
    for (const tk of timeTicks(t0, t1, plotW)) {
      const tx = x(tk.getTime());
      chart.appendChild(svg("line", { x1: tx, x2: tx, y1: axisH - 4, y2: H - 4 }, "grid"));
      if (nowX - tx > 44) chart.appendChild(svgText(tx, 13, tickLabel(tk), "", "middle"));  // keep "now" legible
    }
    chart.appendChild(svg("line", { x1: labelW, x2: labelW, y1: axisH - 4, y2: H - 4 }, "axis-line"));

    const usersSeen = new Set();
    let anyUnatt = false, anyOutside = false, anyJob = false;
    for (const r of rows) {
      const gpu = r.gpu;
      r.jobs.forEach((job, ji) => {
        anyJob = true;
        const y = r.y + rowPad + r.lane[ji] * (barH + laneGap);
        const s = new Date(job.start).getTime();
        const e = endOf(job);
        const x1 = x(s), w = Math.max(2, x(e) - x1);
        const cls = colourClass(job.user);
        const outside = onSomeoneElsesBooking(job.user, gpu, s, e);
        if (job.user === null) anyUnatt = true; else usersSeen.add(job.user);
        if (outside) anyOutside = true;
        const g = svg("g");
        const rect = svg("rect", { x: x1, y: y, width: w, height: barH, rx: 3 },
          "job " + cls + (outside ? " unbooked" : ""));
        g.appendChild(rect);
        if (job.ongoing && !stale) {
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
        const endTxt = !job.ongoing ? fmtLocal(new Date(job.end))
          : stale ? fmtLocal(new Date(liveEnd)) + " (last poll; monitor not running since)"
          : "now (ongoing)";
        addTitle(g, [
          job.user === null ? "unattributed" : job.user,
          "what: " + (job.name || "unknown"),
          "pid " + job.pid + " on GPU " + gpu
            + (outside ? " — no booking of theirs while someone else's booking was active" : ""),
          fmtLocal(new Date(job.start)) + " – " + endTxt,
          "duration " + fmtDuration((e - s) / 1000),
          "peak VRAM " + fmtGiB(job.max_mib) + " (" + job.max_mib.toLocaleString() + " MiB)",
        ].join("\n"));
        chart.appendChild(g);
      });
    }
    if (!anyJob) {
      chart.appendChild(svgText(labelW + plotW / 2, (axisH + rowY) / 2, "No GPU jobs in this range", "empty-note", "middle"));
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
      const item = el("span", "item"); item.appendChild(el("span", "outside-key"));
      item.appendChild(el("span", "", "ran without a booking during someone else's booking"));
      legend.appendChild(item);
    }
    if (anyBand) {
      const item = el("span", "item"); item.appendChild(el("span", "band-key"));
      item.appendChild(el("span", "", "faint band = booking"));
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
    // Not hSum: a card shared by two people in one poll counts once in the total.
    const cardHours = typeof d.gpu_hours_total === "number" ? d.gpu_hours_total : hSum;
    const hCell = el("td", "num", fmtNum(cardHours, 1));
    hCell.title = "GPU-hours of the cards: a GPU shared by several people at once counts once, "
      + "so this can be less than the sum of the rows above";
    tr.appendChild(hCell);
    tbody.appendChild(tr);
  }

  // ---------- Power & utilisation — last 24 h sparklines ----------
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

  // ---------- VRAM — last 24 h, stacked by user ----------
  function bucketLabel(b) { return b === "(unattributed)" ? "unattributed" : b; }
  function renderVram() {
    const grid = $("vram-grid"), legend = $("vram-legend");
    const d = state.vram;
    if (!d) return;
    clear(grid); clear(legend);
    const t1 = state.now ? Math.max(Date.now(), new Date(state.now.ts).getTime()) : Date.now();
    const t0 = t1 - DAY_MS;
    const VW = 1000, VH = 100;
    const x = (t) => (((Math.min(Math.max(t, t0), t1) - t0) / (t1 - t0)) * VW).toFixed(1);
    const present = new Set();
    let anyBooked = false;
    for (const gpu of Object.keys(d.gpus).map(Number).sort((a, b) => a - b)) {
      const g = d.gpus[String(gpu)];
      const total = g.total_mib || DEFAULT_CARD_MIB;
      const y = (mib) => (VH - (Math.min(mib, total) / total) * VH).toFixed(1);
      const pts = g.points.map((p) => ({ t: new Date(p.ts).getTime(), gap: Boolean(p.gap), by_user: p.by_user }))
        .filter((p) => p.t >= t0);
      const order = vramBucketOrder(pts);
      const bands = stackSeries(pts, order);
      const sums = pts.map((p) => Object.values(p.by_user).reduce((a, v) => a + v, 0));

      const box = el("div", "spark");
      const head = el("div", "spark-head");
      head.appendChild(el("b", "", "GPU " + gpu));
      head.appendChild(el("span", "muted", pts.length
        ? "peak " + fmtGiB(Math.max(...sums)) + " · now " + fmtGiB(sums[sums.length - 1]) : "no samples"));
      box.appendChild(head);

      const s = svg("svg", { viewBox: "0 0 " + VW + " " + VH, preserveAspectRatio: "none", role: "img" }, "vram-chart");
      addTitle(s, "GPU " + gpu + ": VRAM by user (stacked) and booked share (dashed), last 24 h");
      s.appendChild(svg("line", { x1: 0, x2: VW, y1: VH - 0.5, y2: VH - 0.5 }, "base"));
      // Segments break at monitoring gaps, so no area bridges the downtime.
      const segs = [];
      pts.forEach((p, i) => { if (!segs.length || p.gap) segs.push([]); segs[segs.length - 1].push(i); });
      for (const band of bands) {
        let path = "";
        for (const seg of segs) {
          seg.forEach((i, k) => { path += (k ? "L" : "M") + x(pts[i].t) + "," + y(band.upper[i]); });
          for (const i of seg.slice().reverse()) path += "L" + x(pts[i].t) + "," + y(band.lower[i]);
          path += "Z";
        }
        const own = pts.map((_, i) => band.upper[i] - band.lower[i]);
        const area = svg("path", { d: path }, "area " + colourClass(band.bucket));
        addTitle(area, bucketLabel(band.bucket) + " — peak " + fmtGiB(Math.max(...own))
          + ", now " + fmtGiB(own[own.length - 1]));
        s.appendChild(area);
        present.add(band.bucket);
      }
      const steps = bookedSteps(state.timelineClaims, gpu, t0, t1);
      if (steps.length) {
        anyBooked = true;
        let line = "M" + x(steps[0].t) + "," + y(steps[0].mib);
        for (let i = 1; i < steps.length; i++) line += "H" + x(steps[i].t) + "V" + y(steps[i].mib);
        const booked = svg("path", { d: line }, "booked-share");
        const peak = Math.max(...steps.map((st) => st.mib));
        addTitle(booked, "booked share — peak " + fmtGiB(peak) + ", now " + fmtGiB(steps[steps.length - 1].mib));
        s.appendChild(booked);
      }
      box.appendChild(s);

      const foot = el("div", "spark-foot");
      foot.appendChild(el("span", "", fmtTime(new Date(t0))));
      foot.appendChild(el("span", "", "0–" + Math.round(total / 1024) + " GiB"));
      foot.appendChild(el("span", "", fmtTime(new Date(t1))));
      box.appendChild(foot);
      grid.appendChild(box);
    }

    const legendOrder = Array.from(present).filter((b) => b !== "(unattributed)").sort();
    if (present.has("(unattributed)")) legendOrder.push("(unattributed)");
    for (const b of legendOrder) {
      const item = el("span", "item");
      item.appendChild(swatch(b));
      item.appendChild(el("span", b === "(unattributed)" ? "unattributed" : "", bucketLabel(b)));
      legend.appendChild(item);
    }
    if (anyBooked) {
      const item = el("span", "item"); item.appendChild(el("span", "booked-key"));
      item.appendChild(el("span", "", "dashed line = booked share"));
      legend.appendChild(item);
    }
  }

  // ---------- controls + scheduling ----------
  function scheduleTimeline() {
    if (state.timelineTimer) clearInterval(state.timelineTimer);
    const every = state.timelineHours <= 24 ? 60000 : 300000;
    state.timelineTimer = setInterval(loadTimeline, every);
    $("refresh-indicator").title = "auto-refresh: live every 30 s, bookings every 1 min, timeline every "
      + (every / 60000) + " min, usage and the last-24 h panels every 5 min";
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
    resizeTimer = setTimeout(() => { renderTimeline(); renderUsage(); if (state.claims) renderCalendar(); }, 150);
  });

  bindBookingForm();
  loadBookUsers();
  loadNow().then(() => { loadClaims(); loadTimeline(); loadUsage(); loadTimeseries(); loadVram(); });
  setInterval(loadNow, 30000);
  setInterval(loadClaims, 60000);
  scheduleTimeline();
  setInterval(loadUsage, 300000);
  setInterval(loadTimeseries, 300000);
  setInterval(loadVram, 300000);
})();
