"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const form = $("#search");
const store = {
  get(k, fallback) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : fallback; } catch { return fallback; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
  del(k) { try { localStorage.removeItem(k); } catch { /* ignore */ } },
};

const state = { job: null, sortBy: "price", pollTimer: null, estimateTimer: null, config: null };

/* ---------- dates & formatting ---------- */
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const parseISO = (s) => { const [y, m, d] = s.slice(0, 10).split("-").map(Number); return new Date(y, m - 1, d); };
const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
const fmtDay = (s) => parseISO(s).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
const fmtTime = (s) => new Date(s).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
const fmtDur = (m) => (m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`);
const narrow = () => window.matchMedia("(max-width: 640px)").matches;
function money(n, cur) {
  try { return new Intl.NumberFormat("en-US", { style: "currency", currency: cur, maximumFractionDigits: 0 }).format(n); }
  catch { return `${cur} ${Math.round(n)}`; }
}
function compactMoney(n, cur) {
  try {
    const sym = new Intl.NumberFormat("en-US", { style: "currency", currency: cur, maximumFractionDigits: 0 })
      .formatToParts(0).find((p) => p.type === "currency")?.value ?? "";
    const limit = narrow() ? 1000 : 10000;  // calendar cells are tiny on phones
    return n >= limit ? `${sym}${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}k` : `${sym}${Math.round(n).toLocaleString("en-US")}`;
  } catch { return String(Math.round(n)); }
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------- form ---------- */
function defaults() {
  const today = new Date();
  return {
    origin: "FLL", destination: "DEL", trip_type: "round-trip",
    depart_from: iso(addDays(today, 45)), depart_to: iso(addDays(today, 75)),
    min_days: 20, max_days: 25, stops: "any", max_stops: "", rank_by: "price",
    cabin: "economy", currency: "USD", adults: 1, children: 0, infants_in_seat: 0, infants_on_lap: 0, top_n: 10,
  };
}

function fillForm(v) {
  for (const [k, val] of Object.entries(v)) {
    const els = form.elements[k];
    if (!els) continue;
    if (els instanceof RadioNodeList) { for (const r of els) r.checked = r.value === String(val); }
    else els.value = val;
  }
}

function readForm() {
  const f = new FormData(form);
  const num = (k, d) => { const n = parseInt(f.get(k), 10); return Number.isFinite(n) ? n : d; };
  const stops = f.get("stops");
  return {
    origin: String(f.get("origin") || "").trim().toUpperCase(),
    destination: String(f.get("destination") || "").trim().toUpperCase(),
    depart_from: f.get("depart_from"), depart_to: f.get("depart_to"),
    min_days: num("min_days", 1), max_days: num("max_days", 1),
    trip_type: f.get("trip_type"), stops,
    max_stops: stops === "direct" || !f.get("max_stops") ? null : num("max_stops", null),
    cabin: f.get("cabin"), currency: f.get("currency"),
    passengers: { adults: num("adults", 1), children: num("children", 0),
      infants_in_seat: num("infants_in_seat", 0), infants_on_lap: num("infants_on_lap", 0) },
    top_n: num("top_n", 10), rank_by: f.get("rank_by"),
  };
}

function flatForm() {
  const b = readForm();
  return { ...b, ...b.passengers, max_stops: b.max_stops ?? "" };
}

function syncForm() {
  const b = readForm();
  $$("[data-round-only]").forEach((el) => { el.hidden = b.trip_type !== "round-trip"; });
  $$("[data-stops-limit]").forEach((el) => { el.hidden = b.stops === "direct"; });
  const p = b.passengers;
  const travellers = p.adults + p.children + p.infants_in_seat + p.infants_on_lap;
  const cabin = form.elements.cabin.selectedOptions[0]?.textContent ?? "";
  $("#more-summary").textContent = `${cabin}, ${travellers} ${travellers === 1 ? "traveler" : "travelers"}, ${b.currency}`;
  if (!state.job || state.job.final) setRouteTitle(b.origin || "···", b.destination || "···");
  store.set("ft.form", flatForm());
  scheduleEstimate();
}

function setRouteTitle(a, b) {
  const spans = $$("#route-title span");
  spans[0].textContent = a; spans[1].textContent = b;
}

function scheduleEstimate() {
  clearTimeout(state.estimateTimer);
  state.estimateTimer = setTimeout(estimate, 350);
}

async function estimate() {
  const el = $("#estimate");
  const b = readForm();
  if (b.origin.length !== 3 || b.destination.length !== 3 || !b.depart_from || !b.depart_to) {
    el.textContent = ""; el.classList.remove("error"); return;
  }
  try {
    const res = await api("/api/estimate", b);
    const mins = res.minutes < 1.5 ? "under 2 minutes" : `about ${Math.round(res.minutes)} minutes`;
    el.textContent = `${res.checks} date ${res.checks === 1 ? "combination" : "combinations"} to check, ${mins}.`;
    el.classList.remove("error");
    $("#go").disabled = isRunning();
  } catch (e) {
    el.textContent = e.message; el.classList.add("error");
  }
}

async function api(path, body) {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    let msg = data.detail;
    if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join(" ");
    const err = new Error(msg || `Something went wrong (HTTP ${res.status}).`);
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ---------- recent routes ---------- */
function renderRecent() {
  const routes = store.get("ft.routes", []);
  const box = $("#recent");
  box.hidden = routes.length === 0;
  box.innerHTML = routes.map((r) => `<button type="button" data-o="${esc(r[0])}" data-d="${esc(r[1])}">${esc(r[0])} to ${esc(r[1])}</button>`).join("");
}
function rememberRoute(o, d) {
  const routes = store.get("ft.routes", []).filter((r) => !(r[0] === o && r[1] === d));
  routes.unshift([o, d]);
  store.set("ft.routes", routes.slice(0, 4));
  renderRecent();
}

/* ---------- searching ---------- */
const isRunning = () => state.job && !state.job.final && ["queued", "running"].includes(state.job.status);

async function startSearch(ev) {
  ev.preventDefault();
  const b = readForm();
  $("#go").disabled = true;
  try {
    const job = await api("/api/searches", b);
    rememberRoute(b.origin, b.destination);
    state.sortBy = b.rank_by;
    store.set("ft.job", job.id);
    showJob(job);
    poll();
    if (window.matchMedia("(max-width: 920px)").matches) $(".board").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) {
    $("#estimate").textContent = e.message;
    $("#estimate").classList.add("error");
    $("#go").disabled = false;
  }
}

function poll() {
  clearTimeout(state.pollTimer);
  if (!state.job || state.job.final) return;
  state.pollTimer = setTimeout(async () => {
    try {
      showJob(await api(`/api/searches/${state.job.id}`));
    } catch (e) {
      if (e.status === 404) { store.del("ft.job"); state.job = null; setStatus(e.message, 0); $("#go").disabled = false; return; }
    }
    poll();
  }, 1500);
}

async function stopSearch() {
  if (!state.job) return;
  $("#stop").disabled = true;
  try { await api(`/api/searches/${state.job.id}/cancel`, {}); } catch { /* it may have just finished */ }
}

function setStatus(html, pct) {
  $("#status").hidden = false;
  $("#status-text").innerHTML = html;
  $("#bar").style.width = `${pct}%`;
  $(".bar").setAttribute("aria-valuenow", String(Math.round(pct)));
}

function showJob(job) {
  state.job = job;
  setRouteTitle(job.origin, job.destination);
  const pct = job.checks_total ? (job.checks_done / job.checks_total) * 100 : 0;
  const prices = Object.values(job.days);
  const cheapest = prices.length ? money(Math.min(...prices), job.currency) : null;
  const of = `${job.checks_done} of ${job.checks_total}`;
  let text;
  if (job.status === "queued") text = `Waiting for another search to finish${job.queue_position > 1 ? ` (${job.queue_position - 1} ahead)` : ""}.`;
  else if (job.status === "running") text = `Checked <strong>${of}</strong> date combinations.${cheapest ? ` Cheapest so far: <strong>${cheapest}</strong>.` : ""}`;
  else if (job.status === "cancelled") text = `Search stopped after ${of} checks. Showing what was found.`;
  else if (job.status === "failed") text = `The search failed: ${esc(job.error)}`;
  else if (job.blocked) text = `Google Flights started refusing requests, so the search stopped after ${of} checks. Showing what was found; try again later.`;
  else text = `Checked all ${job.checks_total} date combinations.${cheapest ? ` Cheapest: <strong>${cheapest}</strong>.` : ""}`;
  setStatus(text, job.final ? 100 : pct);
  $("#stop").hidden = !isRunning();
  $("#stop").disabled = false;
  $("#go").disabled = isRunning();
  $("#go").textContent = isRunning() ? "Searching…" : "Find cheapest trips";

  const results = sortTrips(job.results, state.sortBy);
  renderCalendar(job, results);
  renderResults(job, results);
  $("#notes").innerHTML = (job.messages || []).slice(-4).map((m) => `<li>${esc(m)}</li>`).join("");
  if (job.final) { store.del("ft.job"); }
}

function sortTrips(trips, by) {
  const k = by === "duration"
    ? (t) => [t.total_duration_min, t.price, t.depart_date]
    : (t) => [t.price, t.total_duration_min, t.depart_date];
  return [...trips].sort((a, b) => {
    const x = k(a), y = k(b);
    for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1;
    return 0;
  });
}

/* ---------- calendar ---------- */
function renderCalendar(job, results) {
  const cal = $("#calendar");
  $("#empty").hidden = true;
  const start = parseISO(job.depart_from), end = parseISO(job.depart_to);
  const prices = Object.values(job.days).sort((a, b) => a - b);
  const tierOf = (p) => {
    if (prices.length < 2) return 5;
    const below = prices.filter((x) => x < p).length;   // rank-based, so colors spread across the range
    return 5 - Math.min(4, Math.floor((below / prices.length) * 5));
  };
  const rankOf = new Map(results.map((t, i) => [t.depart_date, i + 1]));
  const dow = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => `<div class="cal-dow" aria-hidden="true">${d}</div>`).join("");

  // One continuous grid: only the weeks your departure window touches, Monday first.
  const first = addDays(start, -((start.getDay() + 6) % 7));
  const last = addDays(end, 6 - ((end.getDay() + 6) % 7));
  let cells = "";
  for (let date = first, i = 0; date <= last; date = addDays(date, 1), i++) {
    const key = iso(date);
    const d = date.getDate();
    const inWindow = date >= start && date <= end;
    const price = job.days[key];
    const showMonth = d === 1 || i === 0;
    const num = showMonth ? `${date.toLocaleDateString("en-US", { month: "short" })} ${d}` : d;
    let cls = "day", fare = "", label = date.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });
    if (!inWindow) {
      cls += " off";
    } else if (price !== undefined) {
      cls += ` priced t${tierOf(price)}`;
      fare = compactMoney(price, job.currency);
      label += `, cheapest ${money(price, job.currency)}`;
    } else if (job.final) {
      cls += " in-window none"; fare = "—"; label += ", no flights found";
    } else {
      cls += " in-window"; label += ", not checked yet";
    }
    const rank = inWindow ? rankOf.get(key) : undefined;
    if (rank) label += `, top pick number ${rank}`;
    cells += `<div class="${cls}" data-date="${key}" ${inWindow ? `aria-label="${esc(label)}"` : `aria-hidden="true"`}><span class="num${showMonth ? " month" : ""}">${num}</span><span class="fare">${fare}</span>${rank ? `<span class="rank" aria-hidden="true">${rank}</span>` : ""}</div>`;
  }
  const span = start.getMonth() === end.getMonth() && start.getFullYear() === end.getFullYear()
    ? start.toLocaleDateString("en-US", { month: "long", year: "numeric" })
    : `${start.toLocaleDateString("en-US", { month: "long", day: "numeric" })} to ${end.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" })}`;
  cal.innerHTML = `<div class="cal-month"><h4>${span}</h4><div class="cal-grid" role="group" aria-label="Departure days">${dow}${cells}</div></div>`;

  // Only animate cells that just got a price.
  const prev = state.pricedDays || new Set();
  const now = new Set(Object.keys(job.days));
  $$(".day.priced", cal).forEach((el) => { if (prev.has(el.dataset.date)) el.classList.remove("priced"); });
  state.pricedDays = now;

  const legend = $("#legend");
  legend.hidden = prices.length === 0;
  if (prices.length) {
    $("#legend-low").textContent = money(prices[0], job.currency);
    $("#legend-high").textContent = money(prices[prices.length - 1], job.currency);
  }
}

/* ---------- results ---------- */
const openRows = new Set();

function renderResults(job, results) {
  const wrap = $("#results-wrap");
  wrap.hidden = false;
  $("#res-h").textContent = job.final ? `Best ${results.length} ${results.length === 1 ? "trip" : "trips"}` : "Best trips so far";
  $$(".sort button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.sort === state.sortBy)));
  const list = $("#results");
  if (!results.length) {
    list.innerHTML = job.final
      ? `<li class="no-results">No flights matched. Try allowing stops, a wider window, or a different trip length.</li>`
      : `<li class="no-results">Results appear here as dates are checked.</li>`;
    return;
  }
  list.innerHTML = results.map((t, i) => tripHTML(t, i + 1, job)).join("");
}

function tripHTML(t, pos, job) {
  const id = `${t.depart_date}_${t.return_date ?? "ow"}`;
  const open = openRows.has(id);
  const dates = t.return_date ? `${fmtDay(t.depart_date)} to ${fmtDay(t.return_date)}` : fmtDay(t.depart_date);
  const len = t.trip_days != null ? `${t.trip_days}-day trip` : "One way";
  const stopsTxt = (leg) => leg ? (leg.stops === 0 ? "Nonstop" : `${leg.stops} stop${leg.stops > 1 ? "s" : ""}${leg.via.length ? ` via ${leg.via.join(", ")}` : ""}`) : "";
  const stops = t.max_stops === 0 ? "Nonstop both ways" : stopsTxt(t.outbound);
  const timeTxt = t.inbound || !job.round_trip ? fmtDur(t.total_duration_min) : `${fmtDur(t.outbound.duration_min)} out`;
  return `<li class="trip">
    <button type="button" class="trip-row" aria-expanded="${open}" aria-controls="d-${id}" data-id="${id}">
      <span class="pos" aria-label="Rank">${pos}</span>
      <span class="dates"><strong>${dates}</strong><span>${len}</span></span>
      <span class="metas"><span class="meta"><strong>${esc(stops)}</strong><span>${esc(t.airlines.join(", "))}</span></span>
      <span class="meta"><strong>${timeTxt}</strong><span>travel time</span></span></span>
      <span class="price">${money(t.price, t.currency)}</span>
      <svg class="chev" viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>
    <div class="detail" id="d-${id}" ${open ? "" : "hidden"}>
      ${legHTML("Outbound", t.depart_date, t.outbound)}
      ${t.return_date ? (t.inbound ? legHTML("Return", t.return_date, t.inbound)
        : `<p class="unknown">Return flight details weren't available. Open the trip on Google Flights to see them.</p>`) : ""}
      <a class="book" href="${esc(t.search_url)}" target="_blank" rel="noopener">Open on Google Flights</a>
    </div>
  </li>`;
}

function legHTML(title, day, leg) {
  const segs = leg.segments.map((s, i) => {
    const lay = i > 0 ? `<p class="layover">${fmtDur(leg.layovers_min[i - 1])} layover in ${esc(s.from_airport)}</p>` : "";
    const flight = [s.airline, s.flight_number].filter(Boolean).join(" ");
    return `${lay}<div class="seg">
      <time>${fmtTime(s.depart)}</time><span>${esc(s.from_airport)} to ${esc(s.to_airport)}</span>
      <time>${fmtTime(s.arrive)}</time><span class="where">${fmtDur(s.duration_min)}${flight ? `, ${esc(flight)}` : ""}${s.aircraft ? `, ${esc(s.aircraft)}` : ""}</span>
    </div>`;
  }).join("");
  return `<div class="leg"><h5>${title} <span>${fmtDay(day)}, ${fmtDur(leg.duration_min)}</span></h5>${segs}</div>`;
}

/* ---------- wiring ---------- */
form.addEventListener("input", syncForm);
form.addEventListener("change", syncForm);
form.addEventListener("submit", startSearch);
$("#stop").addEventListener("click", stopSearch);
$("#swap").addEventListener("click", () => {
  const o = form.elements.origin, d = form.elements.destination;
  [o.value, d.value] = [d.value, o.value];
  syncForm();
});
$("#recent").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  form.elements.origin.value = b.dataset.o;
  form.elements.destination.value = b.dataset.d;
  syncForm();
});
$$("[name=origin], [name=destination]").forEach((el) => el.addEventListener("input", () => {
  el.value = el.value.toUpperCase().replace(/[^A-Z]/g, "");
}));
$$(".sort button").forEach((b) => b.addEventListener("click", () => {
  state.sortBy = b.dataset.sort;
  if (state.job) showJob(state.job);
}));
$("#results").addEventListener("click", (e) => {
  const row = e.target.closest(".trip-row");
  if (!row) return;
  const open = row.getAttribute("aria-expanded") !== "true";
  row.setAttribute("aria-expanded", String(open));
  row.nextElementSibling.hidden = !open;
  if (open) openRows.add(row.dataset.id); else openRows.delete(row.dataset.id);
});

(async function init() {
  const saved = store.get("ft.form", null);
  const v = { ...defaults(), ...(saved || {}) };
  // Don't restore a window that's already in the past.
  const today = iso(new Date());
  if (v.depart_from < today) { const d = defaults(); v.depart_from = d.depart_from; v.depart_to = d.depart_to; }
  fillForm(v);
  renderRecent();
  try {
    state.config = await api("/api/config");
    form.elements.depart_from.min = form.elements.depart_to.min = state.config.today;
    form.elements.depart_from.max = form.elements.depart_to.max = state.config.max_date;
  } catch { /* page still works */ }
  syncForm();
  const jobId = store.get("ft.job", null);
  if (jobId) {
    try { const job = await api(`/api/searches/${jobId}`); state.sortBy = readForm().rank_by; showJob(job); poll(); }
    catch { store.del("ft.job"); }
  }
})();
