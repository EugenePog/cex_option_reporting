/* Box builder — admin tab for moving position legs between boxes (= core.strategy rows; the GUI
 * says "box", the API/DB say "strategy").
 *
 * Scope: the account picker in the page header scopes everything (legs AND boxes). The leg filters
 * in the Legs panel narrow the Legs list / Table only; every box always shows all its legs.
 * Legs are shown and moved one by one (no combo grouping).
 *
 * Data: GET /api/admin/box-builder/board → all legs of the account (gold.position_leg + current
 * manual links) + shown_leg_ids (the filtered subset).
 * Moves are STAGED client-side (S.staged: leg_id -> target strategy id), reviewed through
 * POST /preview, then written as one changeset by POST /apply (core.strategy_link pins, which beat
 * every strategy_rule). The server then recomputes silver+gold in the background; legs whose
 * link and computed strategy disagree come back with pending=true and show as SYNCING.
 */
const BB = "/admin/box-builder";
const S = {
  sub: null, subs: [], view: "board",
  f: { underlying: "", opened_from: "", opened_to: "", status: "", source: "", q: "" },
  data: null, legs: {}, strats: {},
  shown: [],                  // ids of the legs passing the leg filters (newest first)
  staged: new Map(),          // leg_id -> to_strategy_id
  batches: [],                // undo stack: [[{leg_id, prev}]]  (prev = previous staged target or undefined)
  selected: new Set(), lastClicked: null, showAll: new Set(),
  polling: null,
};

// ------------------------------------------------------------------ helpers
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money = (v) => v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + "$" + Math.abs(v).toLocaleString(undefined, {maximumFractionDigits: Math.abs(v) >= 1000 ? 0 : 2});
const moneyK = (v) => v == null ? "–" : Math.abs(v) >= 1000 ? (v > 0 ? "+" : v < 0 ? "−" : "") + "$" + (Math.abs(v) / 1000).toFixed(2) + "k" : money(v);
const pnlCls = (v) => v > 0 ? "pos" : v < 0 ? "neg" : "";
const d10 = (iso) => iso ? iso.slice(0, 10) : "";
const fmtDay = (iso) => { if (!iso) return ""; const d = new Date(iso); return d.toLocaleDateString(undefined, {day: "2-digit", month: "short", timeZone: "UTC"}); };
const fmtTs = (iso) => { if (!iso) return ""; const d = new Date(iso); return d.toLocaleString(undefined, {day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "UTC"}) + " UTC"; };
const shortInst = (inst) => (inst || "").split("-").slice(2).join("-");   // BTC-USD-260830-77500-C -> 260830-77500-C
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const fmtDM = (iso) => { if (!iso) return ""; const d = new Date(iso); return `${d.getUTCDate()} ${MON[d.getUTCMonth()]}`; };  // "12 Sep"
const kTxt = (k) => k == null ? "?" : (k / 1000).toFixed(1).replace(/\.0$/, "") + "k";
// readable leg name for box cards, e.g. "Put 12 Sep 77k" (coin prefix when the account trades several)
function legLabel(lg) {
  const cp = lg.opt_type === "C" ? "Call" : lg.opt_type === "P" ? "Put" : shortInst(lg.inst_id);
  const sub = S.subs.find(x => x.id === S.sub);
  const coin = sub && sub.underlyings.length > 1 ? (lg.underlying || "").split("-")[0] + " " : "";
  return `${coin}${cp} ${fmtDM(lg.expiry)} ${lg.strike != null ? kTxt(lg.strike) : ""}`.replace(/\s+/g, " ").trim();
}

async function post(path, body) { return send("POST", path, body); }
async function send(method, path, body) {
  const opt = {method, headers: {"Content-Type": "application/json"}};
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch("/api" + path, opt);
  if (r.status === 401) { location.href = "/login"; return null; }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail ? (typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail)) : r.status);
  return j;
}
function toast(msg, kind = "") {
  const t = $("bb-toast"); t.textContent = msg; t.className = "bb-toast " + kind; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, 4500);
}
function openDlg(id) { $(id).hidden = false; }
function closeDlg(id) { $(id).hidden = true; }
document.addEventListener("click", (e) => {
  const c = e.target.closest("[data-close]"); if (c) c.closest(".bb-overlay").hidden = true;
  if (e.target.classList && e.target.classList.contains("bb-overlay")) e.target.hidden = true;
  if (!e.target.closest(".bb-menu-wrap")) document.querySelectorAll(".bb-menu").forEach(m => m.hidden = true);
});

// effective strategy of a leg = staged target, else server effective
const effSid = (lg) => S.staged.has(lg.id) ? S.staged.get(lg.id) : lg.strategy_id;
const stratName = (sid) => S.strats[sid] ? S.strats[sid].name : "—";      // box name
const stratColor = (sid) => S.strats[sid] ? (S.strats[sid].color || "#888") : "#555";

function sourceChip(lg) {
  if (S.staged.has(lg.id)) return `<span class="chip pend">PENDING</span>`;
  if (lg.pending) return `<span class="chip sync" title="Applied — waiting for the recompute">SYNCING</span>`;
  if (lg.strategy_source === "manual") return `<span class="chip pin">PIN</span>`;
  if (lg.strategy_source === "default") return `<span class="chip def">DEFAULT</span>`;
  return `<span class="chip rule">RULE</span>`;
}
function openedClosed(lg) {
  const o = fmtTs(lg.pos_opened_at).replace(" UTC", "");
  if (lg.status === "open") return `${o} → <b class="open">open</b>`;
  if (lg.status === "stale") return `${o} → <span class="muted" title="No longer in the latest snapshot; close not collected yet">gone</span>`;
  const exp = lg.close_type === "expiry" ? "exp" : fmtTs(lg.closed_at).replace(" UTC", "");
  return `${o} → ${exp}`;
}
function sideChip(lg) {
  const sz = lg.size != null ? (+lg.size).toString() : "";
  return `<span class="chip ${lg.side === "long" ? "long" : "short"}">${(lg.side || "").toUpperCase()} ${sz}</span>`;
}

// ------------------------------------------------------------------ boot / load
async function boot() {
  const sc = await api(BB + "/scope"); if (!sc) return;
  S.subs = sc.subaccounts;
  const sel = $("f-sub"); sel.innerHTML = "";
  for (const s of S.subs) sel.appendChild(new Option(`${s.label} (${s.n_legs})`, s.id));
  S.sub = sc.default; sel.value = S.sub;
  fillUly();
  sel.onchange = () => {
    if (S.staged.size) toast(`${S.staged.size} staged move(s) discarded — moves are per account.`);
    S.sub = +sel.value; resetStage(); fillUly(); load();
  };
  // leg filters → Legs list / Table only (boxes are never filtered)
  $("f-uly").onchange = e => { S.f.underlying = e.target.value; load(); };
  $("f-from").onchange = e => { S.f.opened_from = e.target.value; load(); };
  $("f-to").onchange = e => { S.f.opened_to = e.target.value; load(); };
  $("f-status").onchange = e => { S.f.status = e.target.value; load(); };
  let qT; $("f-q").oninput = e => { clearTimeout(qT); qT = setTimeout(() => { S.f.q = e.target.value; load(); }, 300); };
  for (const b of $("f-source").querySelectorAll("button")) b.onclick = () => { setSource(b.dataset.v); load(); };
  $("f-clear").onclick = () => { clearFilters(); load(); };
  for (const b of $("bb-view").querySelectorAll("button")) b.onclick = () => setView(b.dataset.view);
  $("sel-all").onchange = e => { S.selected = e.target.checked ? new Set(S.shown) : new Set(); render(); };
  $("btn-moveto").onclick = (e) => { e.stopPropagation(); toggleMoveMenu(); };
  $("btn-newstrat").onclick = () => openStratDlg(null);
  $("ns-save").onclick = saveStrategy;
  $("ns-delete").onclick = deleteStrategy;
  $("pend-undo").onclick = undoStage;
  $("pend-discard").onclick = () => { resetStage(); render(); };
  $("pend-review").onclick = openReview;
  $("rv-apply").onclick = applyChanges;
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select")) return;
    if (e.key === "Escape") document.querySelectorAll(".bb-overlay").forEach(o => o.hidden = true);
    if ((e.key === "m" || e.key === "M") && S.selected.size) { e.preventDefault(); toggleMoveMenu(true); }
  });
  await load();
}
function fillUly() {
  const s = S.subs.find(x => x.id === S.sub) || {underlyings: []};
  fillSelect($("f-uly"), s.underlyings, {blank: "All"});
  S.f.underlying = "";
}
function setSource(v) {
  S.f.source = v;
  $("f-source").querySelectorAll("button").forEach(x => x.classList.toggle("on", x.dataset.v === v));
}
function clearFilters() {
  S.f = { underlying: "", opened_from: "", opened_to: "", status: "", source: "", q: "" };
  $("f-uly").value = ""; $("f-from").value = ""; $("f-to").value = ""; $("f-status").value = ""; $("f-q").value = "";
  setSource("");
}
const filtersOn = () => Object.values(S.f).some(v => v);
async function load() {
  const d = await api(BB + "/board", {subaccount: S.sub, ...S.f}); if (!d) return;
  S.data = d;
  S.legs = Object.fromEntries(d.legs.map(l => [l.id, l]));      // ALL legs of the account
  S.shown = d.shown_leg_ids;                                       // legs passing the leg filters
  S.strats = Object.fromEntries(d.strategies.map(s => [s.id, s]));
  const shown = new Set(S.shown);
  for (const id of [...S.selected]) if (!shown.has(id)) S.selected.delete(id);   // select only what you see
  render();
  syncBanner(d.recompute, d.counts.pending);
  if (S.view === "history") loadHistory();
}

// ------------------------------------------------------------------ staging
function stage(legIds, toSid) {
  // Stage moves of legs to strategy `toSid`. Moving a leg back to its current (server) strategy
  // un-stages it; legs already effectively in `toSid` are skipped.
  const batch = [];
  for (const id of legIds) {
    const lg = S.legs[id]; if (!lg || effSid(lg) === toSid) continue;
    batch.push({leg_id: id, prev: S.staged.get(id)});
    if (toSid === lg.strategy_id) S.staged.delete(id); else S.staged.set(id, toSid);
  }
  if (batch.length) S.batches.push(batch);
  render();
  return batch.length;
}
function undoStage() {
  const b = S.batches.pop(); if (!b) return;
  for (const {leg_id, prev} of b) { if (prev === undefined) S.staged.delete(leg_id); else S.staged.set(leg_id, prev); }
  render();
}
function resetStage() { S.staged.clear(); S.batches = []; S.selected.clear(); }

// ------------------------------------------------------------------ render
function render() {
  const c = S.data ? S.data.counts : {shown: 0, all_legs: 0};
  const cnt = c.shown === c.all_legs ? `${c.all_legs} legs` : `${c.shown} of ${c.all_legs} legs`;
  $("bb-count").textContent = cnt; $("bb-count-t").textContent = cnt;
  $("f-clear").hidden = !filtersOn();
  $("btn-moveto").disabled = !S.selected.size;
  $("btn-moveto").textContent = S.selected.size ? `Move ${S.selected.size} to… ▾` : "Move to… ▾";
  if (S.view === "board") { renderLegs(); renderCols(); }
  if (S.view === "table") renderTable();
  renderPending();
}

function legRow(lg) {
  const sel = S.selected.has(lg.id), sid = effSid(lg);
  return `<div class="bb-row ${sel ? "sel" : ""} ${S.staged.has(lg.id) ? "staged" : ""}" draggable="true" data-legs="${lg.id}" data-leg="${lg.id}">
    <span class="bb-hit" data-pick="${lg.id}"><input type="checkbox" class="cb" data-leg="${lg.id}" ${sel ? "checked" : ""}></span>
    <span class="inst" title="${esc(lg.inst_id)} · posId ${esc(lg.pos_id)}">${esc(lg.inst_id)}</span>
    <span>${sideChip(lg)}</span>
    <span class="t">${openedClosed(lg)}</span>
    <span class="r ${pnlCls(lg.pnl_usd)}">${money(lg.pnl_usd)}</span>
    <span class="st"><i class="dot" style="background:${esc(stratColor(sid))}"></i><span class="nm">${esc(stratName(sid))}</span> ${sourceChip(lg)}</span>
  </div>`;
}
function renderLegs() {
  const body = $("legs-body");
  if (!S.data || !S.shown.length) {
    body.innerHTML = `<div class="bb-empty">${S.data && S.data.counts.all_legs ? "No legs match these filters." : "No legs in this account yet."}</div>`;
    return;
  }
  body.innerHTML = S.shown.map(id => legRow(S.legs[id])).join("");
  // the whole .bb-hit cell (2× the checkbox width, full row height) toggles the leg; a click on the
  // checkbox itself lands here too (preventDefault: the state comes from S.selected on re-render)
  body.querySelectorAll(".bb-hit").forEach(h => h.onclick = (e) => {
    e.stopPropagation(); e.preventDefault();
    const id = +h.dataset.pick;
    if (e.shiftKey && S.lastClicked != null) {
      const a = S.shown.indexOf(S.lastClicked), b = S.shown.indexOf(id);
      for (const x of S.shown.slice(Math.min(a, b), Math.max(a, b) + 1)) S.selected.add(x);
    } else {
      S.selected.has(id) ? S.selected.delete(id) : S.selected.add(id);
    }
    S.lastClicked = id;
    render();
  });
  body.querySelectorAll(".bb-row[data-leg]").forEach(r => r.onclick = (e) => { if (!e.target.closest("input")) openLeg(+r.dataset.leg); });
  wireDrag(body);
}

function payoffPath(legs, w = 200, h = 34) {
  const ks = legs.map(l => l.strike).filter(k => k != null);
  if (!ks.length) return "";
  const lo = Math.min(...ks) * 0.9, hi = Math.max(...ks) * 1.1, pts = [];
  for (let i = 0; i <= 40; i++) {
    const S0 = lo + (hi - lo) * i / 40;
    let v = 0;
    for (const l of legs) {
      const sign = l.side === "short" ? -1 : 1, sz = +(l.size || 1);
      const intr = l.opt_type === "C" ? Math.max(S0 - l.strike, 0) : Math.max(l.strike - S0, 0);
      v += sign * sz * (intr - (+(l.entry_px || 0)) * S0);
    }
    pts.push(v);
  }
  const mn = Math.min(...pts), mx = Math.max(...pts), rng = (mx - mn) || 1;
  return pts.map((v, i) => `${i ? "L" : "M"}${(4 + (w - 8) * i / 40).toFixed(1)} ${(h - 4 - (h - 8) * (v - mn) / rng).toFixed(1)}`).join(" ");
}
function stratKpis(st) {
  // server totals (all legs of the account) adjusted for staged moves
  let n = st.n_legs, pnl = st.net_pnl_usd, open = st.n_open;
  for (const [id, to] of S.staged) {
    const lg = S.legs[id]; if (!lg) continue;
    const v = lg.pnl_usd || 0, isOpen = lg.status === "open" ? 1 : 0;
    if (lg.strategy_id === st.id && to !== st.id) { n--; pnl -= v; open -= isOpen; }
    if (to === st.id && lg.strategy_id !== st.id) { n++; pnl += v; open += isOpen; }
  }
  return {n, pnl, open};
}
function renderCols() {
  // Boxes are scoped by the account only: each shows ALL legs effectively in it (leg filters ignored).
  const cols = $("cols"); let h = "";
  const strategies = S.data ? S.data.strategies : [];
  const all = S.data ? S.data.legs : [];                       // newest first
  for (const st of strategies) {
    const k = stratKpis(st);
    const legsHere = all.filter(l => effSid(l) === st.id);
    // payoff sparkline: the open legs; if nothing is open, the legs of the box's latest expiry
    const openLegs = legsHere.filter(l => l.status === "open");
    const lastExp = legsHere.reduce((m, l) => (l.expiry && l.expiry > m ? l.expiry : m), "");
    const shapeLegs = openLegs.length ? openLegs : legsHere.filter(l => l.expiry === lastExp);
    const shapeTitle = openLegs.length ? "Payoff at expiry of the open legs" : `Payoff at expiry of the ${fmtDM(lastExp)} legs (nothing open)`;
    const path = payoffPath(shapeLegs);
    const rules = st.rules.length ? `rule${st.rules.length > 1 ? "s" : ""} ${st.rules.map(r => "#" + r.id + " " + esc(Object.values(r.match || {}).join(" "))).join(", ")}`
      : st.is_unassigned ? "no rule matched" : "manual only";
    const limit = S.showAll.has(st.id) ? 1e9 : 6;
    const edit = st.is_unassigned ? "" : `<button type="button" class="bb-edit" data-edit="${st.id}" title="Rename, recolor or delete this box" aria-label="Edit box ${esc(st.name)}"><svg viewBox="0 0 16 16" width="13" height="13"><path d="M11.3 1.7a1.6 1.6 0 0 1 2.3 0l.7.7a1.6 1.6 0 0 1 0 2.3L5.6 13.4 2 14l.6-3.6z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M10 3l3 3" stroke="currentColor" stroke-width="1.4"/></svg></button>`;
    h += `<div class="bb-col" data-drop="${st.id}">
      <h3><i class="dot" style="background:${esc(st.color)}"></i><span class="bn" title="${esc(st.description || st.name)}">${esc(st.name)}</span>${edit}</h3>
      <div class="meta">${k.n} legs · ${rules}</div>
      <div class="kp"><div>NET P&amp;L<b class="${pnlCls(k.pnl)}">${moneyK(k.pnl)}</b></div>
        <div>WIN<b>${st.win_rate == null ? "—" : Math.round(st.win_rate * 100) + "%"}</b></div><div>OPEN<b>${k.open}</b></div></div>
      ${path ? `<svg class="spark" viewBox="0 0 200 34" preserveAspectRatio="none"><title>${esc(shapeTitle)}</title><path d="${path}" stroke="${esc(st.color)}" stroke-width="2" fill="none"/></svg>` : `<div class="spark empty"></div>`}
      <div class="cards">
        ${legsHere.slice(0, limit).map(lg => {
          const staged = S.staged.has(lg.id);
          const stTxt = lg.status === "open" ? "open" : lg.status === "stale" ? "gone" : lg.close_type === "expiry" ? "expired" : "closed";
          return `<div class="card2 ${staged ? "pending" : ""}" draggable="true" data-legs="${lg.id}" data-leg="${lg.id}" title="${esc(lg.inst_id)} · posId ${esc(lg.pos_id)}">
            <div class="l1"><span>${esc(legLabel(lg))}</span><span class="${pnlCls(lg.pnl_usd)}">${money(lg.pnl_usd)}</span></div>
            <div class="l2"><span>${esc((lg.side || "").toUpperCase())} ${lg.size ?? ""} · ${stTxt}</span>${sourceChip(lg)}</div></div>`;
        }).join("")}
        ${legsHere.length > limit ? `<div class="more" data-more="${st.id}">+ ${legsHere.length - limit} more</div>` : ""}
        ${!legsHere.length ? `<div class="hint">${st.is_unassigned ? "Legs land here when no rule matches. Drag them out to pin them." : "Drop legs here."}</div>` : ""}
      </div>
      <div class="drop">Drop to move here</div>
    </div>`;
  }
  cols.innerHTML = h;
  cols.querySelectorAll("[data-more]").forEach(m => m.onclick = () => { S.showAll.add(+m.dataset.more); renderCols(); });
  cols.querySelectorAll(".card2").forEach(c => c.onclick = () => openLeg(+c.dataset.leg));
  cols.querySelectorAll("[data-edit]").forEach(b => b.onclick = (e) => { e.stopPropagation(); openStratDlg(S.strats[+b.dataset.edit]); });
  wireDrag(cols);
  cols.querySelectorAll("[data-drop]").forEach(col => {
    col.ondragover = (e) => { e.preventDefault(); col.classList.add("target"); };
    col.ondragleave = (e) => { if (!col.contains(e.relatedTarget)) col.classList.remove("target"); };
    col.ondrop = (e) => {
      e.preventDefault(); col.classList.remove("target");
      const ids = (e.dataTransfer.getData("text/plain") || "").split(",").filter(Boolean).map(Number);
      const n = stage(ids, +col.dataset.drop);
      if (n) toast(`${n} leg${n > 1 ? "s" : ""} staged → ${stratName(+col.dataset.drop)}`);
    };
  });
}
function wireDrag(root) {
  root.querySelectorAll("[draggable=true]").forEach(el => {
    el.ondragstart = (e) => {
      let ids = el.dataset.legs.split(",").map(Number);
      // dragging a selected row drags the whole selection
      if (ids.length === 1 && S.selected.has(ids[0]) && S.selected.size > 1) ids = [...S.selected];
      e.dataTransfer.setData("text/plain", ids.join(","));
      e.dataTransfer.effectAllowed = "move";
      el.classList.add("dragging");
      document.body.classList.add("bb-dragging");
    };
    el.ondragend = () => { el.classList.remove("dragging"); document.body.classList.remove("bb-dragging"); };
  });
}

function renderPending() {
  const n = S.staged.size, bar = $("bb-pending");
  bar.hidden = !n; if (!n) return;
  const by = {}; let shift = 0;
  for (const [id, to] of S.staged) { by[to] = (by[to] || 0) + 1; shift += Math.abs(S.legs[id] ? (S.legs[id].pnl_usd || 0) : 0); }
  $("pend-count").textContent = `${n} staged leg${n > 1 ? "s" : ""}`;
  $("pend-summary").innerHTML = Object.entries(by).map(([sid, c]) =>
    `<span><i class="dot" style="background:${esc(stratColor(+sid))}"></i>${c} → ${esc(stratName(+sid))}</span>`).join("") +
    `<span class="muted">· P&amp;L moved ${money(shift).replace("+", "")}</span>`;
  $("pend-undo").disabled = !S.batches.length;
}

function toggleMoveMenu(force) {
  const m = $("menu-moveto");
  if (!S.selected.size) { m.hidden = true; return; }
  m.innerHTML = Object.values(S.strats).map(st => `<button type="button" data-sid="${st.id}"><i class="dot" style="background:${esc(st.color)}"></i>${esc(st.name)}</button>`).join("");
  m.hidden = force ? false : !m.hidden;
  m.querySelectorAll("button").forEach(b => b.onclick = () => {
    const n = stage([...S.selected], +b.dataset.sid); m.hidden = true; S.selected.clear(); render();
    toast(n ? `${n} leg${n > 1 ? "s" : ""} staged → ${stratName(+b.dataset.sid)}` : "Nothing to move — already there");
  });
}

// ------------------------------------------------------------------ table view
function renderTable() {
  const tb = $("table-body"); if (!S.data) return;
  tb.innerHTML = S.shown.map(id => S.legs[id]).map(lg => {
    const sid = effSid(lg);
    const opts = Object.values(S.strats).map(st => `<option value="${st.id}" ${st.id === sid ? "selected" : ""}>${esc(st.name)}</option>`).join("");
    return `<tr class="${S.staged.has(lg.id) ? "staged" : ""}" data-leg="${lg.id}">
      <td class="inst">${esc(lg.inst_id)}</td><td>${sideChip(lg)}</td><td>${lg.size ?? ""}</td>
      <td>${fmtTs(lg.pos_opened_at)}</td><td>${lg.status === "closed" ? fmtTs(lg.closed_at) : lg.status}</td>
      <td class="${pnlCls(lg.pnl_usd)}">${money(lg.pnl_usd)}</td><td>${sourceChip(lg)}</td>
      <td><select data-leg="${lg.id}">${opts}</select></td></tr>`;
  }).join("") || `<tr><td colspan="8" class="muted">No legs match these filters.</td></tr>`;
  tb.querySelectorAll("select").forEach(sel => sel.onchange = () => { stage([+sel.dataset.leg], +sel.value); });
  tb.querySelectorAll("td.inst").forEach(td => td.onclick = () => openLeg(+td.parentElement.dataset.leg));
}

// ------------------------------------------------------------------ history view
async function loadHistory() {
  const d = await api(BB + "/history", {subaccount: S.sub}); if (!d) return;
  const body = $("history-body");
  if (!d.changesets.length) { body.innerHTML = `<div class="bb-empty">No manual changes yet — every leg follows the rules.</div>`; return; }
  body.innerHTML = d.changesets.map(cs => `
    <div class="bb-cs">
      <div class="l1"><b>${fmtTs(cs.created_at)}</b><span class="muted">· ${esc(cs.created_by || "?")} · ${cs.n_rows} leg${cs.n_rows > 1 ? "s" : ""}</span>
        <span class="spacer" style="flex:1"></span>
        ${cs.undoable ? `<button type="button" class="bb-btn ghost" data-undo="${cs.changeset_id}">${cs.deleted_box ? "Undo · restore box" : "Undo"}</button>` : `<span class="muted bb-small">superseded</span>`}</div>
      <div class="l2">“${esc(cs.reason)}”</div>
      <div class="l3">${cs.deleted_box ? `<span class="chip del"><i class="dot" style="background:${esc(cs.deleted_box.color || "#888")}"></i>box deleted · ${esc(cs.deleted_box.name)}</span>` : ""}
        ${Object.entries(cs.targets).map(([t, c]) => `<span class="chip ${t === "rules" ? "rule" : "pin"}">${c} → ${esc(t)}</span>`).join(" ")}
        <span class="muted bb-small">${cs.legs.map(esc).join(", ")}${cs.n_rows > cs.legs.length ? "…" : ""}</span>
        <span class="muted bb-small mono">#${cs.changeset_id.slice(0, 8)}</span></div>
    </div>`).join("");
  body.querySelectorAll("[data-undo]").forEach(b => b.onclick = async () => {
    if (b.dataset.armed !== "1") { b.dataset.armed = "1"; b.textContent = "Confirm undo"; b.classList.add("danger"); return; }
    b.disabled = true;
    try {
      const r = await post(BB + "/undo", {changeset_id: b.dataset.undo});
      toast(`Undone: ${r.written} leg(s) restored${r.restored_box ? ` · box “${r.restored_box}” is back` : ""}${r.skipped.length ? ` · ${r.skipped.length} skipped (changed later or box deleted)` : ""}. Recomputing…`, "ok");
      startPolling(); await load(); loadHistory();
    } catch (err) { toast("Undo failed: " + err.message, "bad"); b.disabled = false; }
  });
}
function setView(v) {
  S.view = v;
  $("bb-view").querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.view === v));
  $("view-board").hidden = v !== "board"; $("view-table").hidden = v !== "table"; $("view-history").hidden = v !== "history";
  // one set of leg filters, shown in whichever legs view is open
  if (v === "table") $("table-filters").appendChild($("bb-lfilters"));
  if (v === "board") $("legs-th").before($("bb-lfilters"));
  if (v === "history") loadHistory(); else render();
}

// ------------------------------------------------------------------ review & apply
function stagedMoves() { return [...S.staged].map(([leg_id, to]) => ({leg_id, to_strategy_id: to})); }
async function openReview() {
  if (!S.staged.size) return;
  $("rv-err").hidden = true; $("rv-reason").value = $("rv-reason").value || "";
  let pv;
  try { pv = await post(BB + "/preview", {subaccount_id: S.sub, moves: stagedMoves()}); }
  catch (err) { toast("Preview failed: " + err.message, "bad"); return; }
  const acct = (S.subs.find(s => s.id === S.sub) || {}).label || "";
  $("rv-sub").textContent = `${pv.moves.length} leg${pv.moves.length > 1 ? "s" : ""} · account ${acct} — nothing is written until you apply.`;
  $("rv-moves").innerHTML = pv.moves.map(m => {
    const lg = S.legs[m.leg_id] || {};
    return `<tr><td><b class="mono">${esc(m.inst_id)}</b><div class="muted bb-small">${esc((lg.side || "").toLowerCase())} ${lg.size ?? ""} · opened ${fmtDay(lg.pos_opened_at)} · ${esc(m.status)}</div></td>
      <td><i class="dot" style="background:${esc(stratColor(m.from.id))}"></i>${esc(m.from.name || "—")}</td>
      <td><i class="dot" style="background:${esc(stratColor(m.to.id))}"></i><b>${esc(m.to.name)}</b>${m.noop ? ' <span class="muted">(no change)</span>' : ""}</td>
      <td class="r mono">${m.rows.trade_fill} · ${m.rows.position_snapshot} · ${m.rows.closed_position}</td><td class="r ${pnlCls(m.pnl_usd)}">${money(m.pnl_usd)}</td></tr>`;
  }).join("");
  const maxAbs = Math.max(1, ...pv.impact.flatMap(i => [Math.abs(i.before.net_pnl_usd), Math.abs(i.after.net_pnl_usd)]));
  $("rv-impact").innerHTML = pv.impact.map(i => `
    <div class="bb-imp"><span><i class="dot" style="background:${esc(i.color)}"></i>${esc(i.name)}</span>
      <span class="bar"><i style="width:${(100 * Math.abs(i.after.net_pnl_usd) / maxAbs).toFixed(1)}%;background:${esc(i.color)}"></i></span>
      <span class="r">${money(i.before.net_pnl_usd)} → ${money(i.after.net_pnl_usd)}</span>
      <span class="r ${pnlCls(i.delta_pnl_usd)}">${money(i.delta_pnl_usd)}</span>
      <span class="muted bb-small">${i.before.n_deals} → ${i.after.n_deals} deals · win ${i.before.win_rate == null ? "—" : Math.round(i.before.win_rate * 100) + "%"} → ${i.after.win_rate == null ? "—" : Math.round(i.after.win_rate * 100) + "%"}</span></div>`).join("");
  $("rv-note").innerHTML = `Writes ${pv.n_links} row${pv.n_links === 1 ? "" : "s"} to <code>core.strategy_link</code> as one changeset, then recomputes silver + gold.`;
  $("rv-apply").textContent = `Apply ${pv.n_links} move${pv.n_links === 1 ? "" : "s"}`;
  $("rv-apply").disabled = !pv.n_links;
  openDlg("dlg-review"); setTimeout(() => $("rv-reason").focus(), 30);
}
async function applyChanges() {
  const reason = $("rv-reason").value.trim();
  if (!reason) { $("rv-err").textContent = "Please enter a reason — it is stored with the changeset."; $("rv-err").hidden = false; return; }
  $("rv-apply").disabled = true;
  try {
    const r = await post(BB + "/apply", {subaccount_id: S.sub, moves: stagedMoves(), reason});
    closeDlg("dlg-review"); $("rv-reason").value = "";
    resetStage();
    toast(`Applied ${r.written} pin${r.written === 1 ? "" : "s"} · changeset #${(r.changeset_id || "").slice(0, 8)} · recomputing silver + gold…`, "ok");
    await load(); startPolling();
  } catch (err) { $("rv-err").textContent = err.message; $("rv-err").hidden = false; }
  finally { $("rv-apply").disabled = false; }
}

// ------------------------------------------------------------------ recompute status
function syncBanner(rc, pending) {
  const b = $("bb-sync");
  if (rc && rc.running) {
    b.hidden = false; b.className = "bb-sync running";
    b.innerHTML = `<span class="spin"></span> Recomputing silver + gold — moved legs show as <span class="chip sync">SYNCING</span> until it finishes.`;
  } else if (pending) {
    b.hidden = false; b.className = "bb-sync";
    b.innerHTML = `${pending} leg${pending > 1 ? "s" : ""} waiting for a recompute${rc && rc.last_error ? ` · last run failed: ${esc(rc.last_error)}` : ""}. <button type="button" class="bb-btn ghost" id="btn-rc">Recompute now</button>`;
    $("btn-rc").onclick = async () => { await post(BB + "/recompute", {}); startPolling(); load(); };
  } else { b.hidden = true; }
}
function startPolling() {
  clearInterval(S.polling);
  S.polling = setInterval(async () => {
    const rc = await api(BB + "/recompute"); if (!rc) return;
    if (!rc.running) { clearInterval(S.polling); S.polling = null; await load(); if (rc.last_error) toast("Recompute failed: " + rc.last_error, "bad"); else toast("Recompute finished — reports are up to date.", "ok"); }
    else syncBanner(rc, 0);
  }, 2000);
}

// ------------------------------------------------------------------ leg drawer
async function openLeg(id) {
  const d = await api(BB + "/leg/" + id); if (!d || !d.leg) return;
  const lg = d.leg, sid = effSid(lg);
  const cur = S.strats[sid] || {name: "—", color: "#555"};
  const fp = d.footprint;
  const src = S.staged.has(lg.id) ? `<span class="chip pend">PENDING</span>` : sourceChip(lg);
  const pinInfo = lg.pin ? `Pinned by ${esc(lg.pin.created_by || "?")} · ${fmtTs(lg.pin.created_at)} · changeset #${esc((lg.pin.changeset_id || "").slice(0, 8))}` :
    lg.strategy_source === "default" ? "No rule matched — fallback box" : "Assigned by a rule";
  $("leg-body").innerHTML = `
    <h2 class="mono">${esc(lg.inst_id)}</h2>
    <div class="chips">${sideChip(lg)} <span class="chip out">${lg.opt_type === "C" ? "CALL" : "PUT"} · K ${(+lg.strike).toLocaleString()}</span>
      <span class="chip out">${lg.status === "closed" ? (lg.close_type === "expiry" ? "EXPIRED " : "CLOSED ") + fmtDay(lg.closed_at).toUpperCase() : lg.status.toUpperCase()}</span>
      <b class="${pnlCls(lg.pnl_usd)}">${money(lg.pnl_usd)}</b></div>
    <div class="kv"><span>Leg key</span><span class="mono">${esc(lg.cex_code)} · ${esc(lg.pos_id)} · ${new Date(lg.pos_opened_at).getTime()}</span>
      <span>posId / opened</span><span>${esc(lg.pos_id)} · ${fmtTs(lg.pos_opened_at)}</span>
      <span>Closed</span><span>${lg.status === "closed" ? fmtTs(lg.closed_at) + (lg.close_type === "expiry" ? " · at expiry" : "") : "—"}</span>
      <span>Entry / exit</span><span>${lg.entry_px ?? "–"} / ${lg.exit_px ?? "–"}</span></div>
    <div class="box"><div class="row"><i class="dot" style="background:${esc(cur.color)}"></i><b>${esc(cur.name)}</b> ${src}<span class="spacer" style="flex:1"></span>
        ${lg.strategy_source === "manual" && !lg.pending ? `<button type="button" class="bb-btn" id="btn-reset">Reset to rule</button>` : ""}</div>
      <div class="muted bb-small">${pinInfo}</div>
      ${lg.pin ? `<div class="muted bb-small">Reason: “${esc(lg.pin.reason)}”</div>` : ""}
      <div class="muted bb-small">Without a pin: <span style="color:var(--text)">${esc(d.rule_strategy ? d.rule_strategy.name : "—")}</span></div>
      <div id="reset-form" hidden><input id="reset-reason" placeholder="Reason for resetting to the rule"><button type="button" class="bb-btn primary" id="btn-reset-go">Reset</button></div>
    </div>
    <div class="bb-sec">Footprint — rows this leg tags in silver</div>
    <div class="fp">
      <div><span class="t">trade_fill</span><b>${fp.trade_fill.n}</b><span class="d">${fp.trade_fill.rows.map(f => `trade ${esc(f.trade_id)} · ${esc(f.side)} ${f.size} @ ${f.price}`).join("<br>") || "no fill collected"}</span></div>
      <div><span class="t">position_snapshot</span><b>${fp.position_snapshot.n}</b><span class="d">${fp.position_snapshot.n ? fmtTs(fp.position_snapshot.first).replace(" UTC", "") + " → " + fmtTs(fp.position_snapshot.last).replace(" UTC", "") : "none"}</span></div>
      <div><span class="t">closed_position</span><b>${fp.closed_position.n}</b><span class="d">${fp.closed_position.n ? "ext_id = posId" : "still open"}</span></div>
    </div>
    <div class="bb-sec">History</div>
    <div class="tl">${d.history.map(h => `<div class="ev ${h.action === "rule" ? "rule" : h.action === "unpin" ? "unpin" : ""}">
      <b>${h.action === "pin" ? "Pinned → " + esc(h.strategy ? h.strategy.name : "?") : h.action === "unpin" ? "Reset to rule" : "Rule → " + esc(h.strategy ? h.strategy.name : "?")}</b>${h.current ? ' <span class="chip pin">CURRENT</span>' : ""}
      <div class="w">${fmtTs(h.created_at)} · ${esc(h.created_by || "")}${h.reason ? " · “" + esc(h.reason) + "”" : ""}</div></div>`).join("")}</div>
`;
  const br = $("btn-reset");
  if (br) br.onclick = () => { $("reset-form").hidden = false; $("reset-reason").focus(); };
  const go = $("btn-reset-go");
  if (go) go.onclick = async () => {
    const reason = $("reset-reason").value.trim(); if (!reason) { $("reset-reason").focus(); return; }
    try { await post(BB + "/reset", {subaccount_id: S.sub, leg_ids: [lg.id], reason}); toast("Reset to rule — recomputing…", "ok"); closeDlg("dlg-leg"); await load(); startPolling(); }
    catch (err) { toast("Reset failed: " + err.message, "bad"); }
  };
  openDlg("dlg-leg");
}

// ------------------------------------------------------------------ new / edit / delete box (core.strategy)
let NS = {id: null, armed: false};          // id = box being edited (null = new box)
function openStratDlg(st) {
  NS = {id: st ? st.id : null, armed: false};
  const acct = esc((S.subs.find(s => s.id === S.sub) || {}).label || "");
  $("ns-title").textContent = st ? "Edit box" : "New box";
  $("ns-sub").innerHTML = st
    ? `Box in <b>${acct}</b>. Renaming keeps every leg, pin and rule; reports show the new name right away.`
    : `Creates a box in <b>${acct}</b>. It lives in the database — add it to <code>seed/strategy.csv</code> to keep it in git.`;
  $("ns-name").value = st ? st.name : "";
  $("ns-color").value = st && /^#[0-9a-f]{6}$/i.test(st.color || "") ? st.color : "#9b7be0";
  $("ns-desc").value = st ? (st.description || "") : "";
  $("ns-save").textContent = st ? "Save" : "Create";
  $("ns-save").disabled = false;
  const del = $("ns-delete");
  del.hidden = !st; del.disabled = false; del.textContent = "Delete box…"; del.classList.remove("armed");
  $("ns-del-confirm").hidden = true; $("ns-err").hidden = true;
  openDlg("dlg-strat"); setTimeout(() => $("ns-name").focus(), 30);
}
async function saveStrategy() {
  const body = {name: $("ns-name").value, color: $("ns-color").value, description: $("ns-desc").value};
  $("ns-save").disabled = true;
  try {
    if (NS.id) {
      const r = await send("PUT", BB + "/strategies/" + NS.id, body);
      closeDlg("dlg-strat"); toast(`Box “${r.name}” saved.`, "ok");
    } else {
      const r = await post(BB + "/strategies", {subaccount_id: S.sub, ...body});
      closeDlg("dlg-strat"); toast(`Box “${r.name}” created — drop legs on it.`, "ok");
    }
    await load();
  } catch (err) { $("ns-err").textContent = err.message; $("ns-err").hidden = false; }
  finally { $("ns-save").disabled = false; }
}
async function deleteStrategy() {
  const st = S.strats[NS.id]; if (!st) return;
  const del = $("ns-delete");
  if (!NS.armed) {                                   // 1st click: explain, 2nd click: delete
    const n = S.data.legs.filter(l => l.strategy_id === st.id).length;
    const r = st.rules.length;
    $("ns-del-confirm").innerHTML = `<b>Delete box “${esc(st.name)}”?</b> Its <b>${n} leg${n === 1 ? "" : "s"}</b> move to
      <b>unassigned</b> (pinned there)${r ? `, and its ${r} rule${r === 1 ? "" : "s"} stop${r === 1 ? "s" : ""} applying` : ""}.
      Reports follow after the recompute. You can undo this in <b>History</b> (restores the box).`;
    $("ns-del-confirm").hidden = false;
    NS.armed = true; del.classList.add("armed");
    del.textContent = `Delete · move ${n} leg${n === 1 ? "" : "s"} to unassigned`;
    return;
  }
  del.disabled = true;
  try {
    const r = await send("DELETE", BB + "/strategies/" + st.id);
    closeDlg("dlg-strat");
    // staged moves into the deleted box are dropped
    for (const [id, to] of [...S.staged]) if (to === st.id) S.staged.delete(id);
    S.batches = S.batches.map(b => b.filter(x => S.staged.has(x.leg_id))).filter(b => b.length);
    toast(`Box “${r.name}” deleted — ${r.moved} leg${r.moved === 1 ? "" : "s"} moved to unassigned. Undo in History.`, "ok");
    await load(); startPolling();
  } catch (err) { $("ns-err").textContent = err.message; $("ns-err").hidden = false; del.disabled = false; }
}

boot();
