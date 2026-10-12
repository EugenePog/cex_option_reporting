/* Settings tab (admin) — a list of settings on the left, the selected editor on the right.
 *
 * Telegram notifications: edits core.notification_setting through /api/admin/settings/notifications.
 * Each row is edited in place and saved on its own (Save / Delete); "+ Add notification" adds a draft
 * row (Add / Cancel). The server checks every rule (allowed code and asset, one row per asset +
 * notification, threshold 0–100); the editor also greys out the notifications an asset already has.
 */
const API = "/api/admin/settings";
const T = { rows: [], assets: [], codes: [], th: {min: 0, max: 100}, edits: {}, drafts: [], armed: null, nextDraft: 1 };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const codeInfo = (code) => T.codes.find(c => c.code === code) || {letter: "?", label: code, unit: null, critical: false};
// "a · top_strike — price ≥ top strike − X %" (build 7: the code is shown)
const codeLabel = (code) => { const c = codeInfo(code); return `${c.letter} · ${code}`; };
const optionLabel = (c) => c.code;              // the Notification column shows the code itself
const defaultTh = (code) => codeInfo(code).default ?? 0;

async function send(method, path, body) {
  const opt = {method, headers: {"Content-Type": "application/json"}};
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(API + path, opt);
  if (r.status === 401) { location.href = "/login"; return null; }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail ? (typeof j.detail === "string" ? j.detail : j.detail.map(d => d.msg).join("; ")) : "HTTP " + r.status);
  return j;
}
function toast(msg, kind = "") {
  const t = $("st-toast"); t.textContent = msg; t.className = "st-toast " + kind; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, 4500);
}

// ------------------------------------------------------------------ left list
function showPanel(name) {
  document.querySelectorAll(".st-item").forEach(b => b.classList.toggle("on", b.dataset.panel === name));
  document.querySelectorAll(".st-main").forEach(p => { p.hidden = p.id !== "panel-" + name; });
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
}

// ------------------------------------------------------------------ telegram notifications
async function loadTelegram() {
  const d = await send("GET", "/notifications");
  T.rows = d.rows; T.assets = d.assets; T.codes = d.codes; T.th = d.threshold;
  T.edits = {}; T.armed = null;
  $("tg-codes").innerHTML = T.codes.map(c => `<tr><td class="l">${c.letter}</td><td><code>${c.code}</code></td><td>${esc(c.label)}</td></tr>`).join("");
  renderTelegram();
}

// current values of a row (saved row + unsaved edits, or a draft)
function cur(key) {
  if (String(key).startsWith("d")) return T.drafts.find(x => x.key === key);
  const r = T.rows.find(x => x.id === +key);
  return {...r, ...(T.edits[key] || {})};
}
function isDirty(key) {
  const e = T.edits[key]; if (!e) return false;
  const r = T.rows.find(x => x.id === +key);
  return Object.keys(e).some(k => String(e[k] ?? "") !== String(r[k] ?? ""));
}
// codes an asset already uses, except in row `key`
function usedCodes(asset, key) {
  const used = new Set();
  for (const r of T.rows) if (String(r.id) !== String(key) && cur(r.id).asset === asset) used.add(cur(r.id).code);
  for (const d of T.drafts) if (d.key !== key && d.asset === asset) used.add(d.code);
  return used;
}

function rowHtml(key, v, draft) {
  const assets = [...T.assets];
  const orig = draft ? null : T.rows.find(x => x.id === +key);
  if (orig && !assets.includes(orig.asset)) assets.push(orig.asset);    // keep a legacy asset editable
  const used = usedCodes(v.asset, key);
  const info = codeInfo(v.code);
  const assetOpts = assets.map(a => `<option value="${esc(a)}" ${a === v.asset ? "selected" : ""}>${esc(a)}${T.assets.includes(a) ? "" : " (no positions)"}</option>`).join("");
  const codeOpts = T.codes.map(c => `<option value="${c.code}" ${c.code === v.code ? "selected" : ""} ${used.has(c.code) && c.code !== v.code ? "disabled" : ""}>${esc(optionLabel(c))}${used.has(c.code) && c.code !== v.code ? " (exists)" : ""}</option>`).join("");
  // build 7: every code has a threshold — X / Y % for a–d, N days for e / f; 0 = at the strike / expiry
  const thTitle = info.unit === "%" ? "Percent away from the strike, towards the inside of the box (0 = at the strike)"
                                   : "Days before the last expiry (0 = at the expiry; 0.25 = 6 h)";
  const th = `<input type="number" class="th" data-f="threshold" min="${T.th.min}" max="${T.th.max}" step="any" value="${v.threshold}" title="${thTitle}"><span class="unit">${info.unit === "%" ? "%" : "days"}</span>`;
  const dirty = draft || isDirty(key);
  const armed = String(T.armed) === String(key);
  const conflict = used.has(v.code);                      // a second row for the same asset + code
  const actions = draft
    ? `<button type="button" class="st-btn primary" data-act="add" ${conflict ? "disabled" : ""}>Add</button><button type="button" class="st-btn ghost" data-act="cancel">Cancel</button>`
    : `<button type="button" class="st-btn primary" data-act="save" ${dirty && !conflict ? "" : "disabled"}>Save</button>`
      + `<button type="button" class="st-btn danger ${armed ? "armed" : ""}" data-act="del" title="Delete this notification">${armed ? "Confirm delete" : "Delete"}</button>`;
  const msg = conflict ? `Only one per asset and notification: ${v.asset} already has “${codeLabel(v.code)}”. Pick another notification or asset.` : "";
  return `<tr class="${draft ? "draft" : dirty ? "dirty" : ""} ${v.is_enabled ? "" : "off"} ${conflict ? "conflict" : ""}" data-key="${key}">
    <td><select data-f="asset">${assetOpts}</select></td>
    <td><select data-f="code" class="code">${codeOpts}</select></td>
    <td class="thc">${th}</td>
    <td class="ck"><input type="checkbox" data-f="is_enabled" ${v.is_enabled ? "checked" : ""} aria-label="On"></td>
    <td class="ck"><input type="checkbox" data-f="is_critical" ${v.is_critical ? "checked" : ""} aria-label="Critical"><span class="crit" ${v.is_critical ? "" : "hidden"}>🔴</span></td>
    <td><input type="text" data-f="note" maxlength="500" value="${esc(v.note || "")}" placeholder="optional"></td>
    <td class="act">${actions}</td>
  </tr>
  <tr class="errrow" data-err="${key}" ${msg ? "" : "hidden"}><td colspan="7"><div class="err">${esc(msg)}</div></td></tr>`;
}

function renderTelegram() {
  let h = "";
  for (const r of T.rows) h += rowHtml(r.id, cur(r.id), false);
  for (const d of T.drafts) h += rowHtml(d.key, d, true);
  if (!T.rows.length && !T.drafts.length) h = `<tr><td colspan="7" class="muted">No notifications yet — nothing is sent. Use <b>+ Add notification</b>.</td></tr>`;
  $("tg-body").innerHTML = h;
  $("tg-add").disabled = !T.assets.length;
  $("tg-add").title = T.assets.length ? "" : "No asset with positions in the accounts yet";
  wireRows();
}

function freeCode(asset, key) {
  const used = usedCodes(asset, key);
  const c = T.codes.find(x => !used.has(x.code));
  return c ? c.code : null;
}
function setField(key, f, value) {
  if (String(key).startsWith("d")) {
    const d = cur(key); d[f] = value;
    if (f === "asset" && usedCodes(value, key).has(d.code)) {   // keep a new row free of clashes
      const fc = freeCode(value, key);
      if (fc) { f = "code"; value = fc; d.code = fc; }
    }
    if (f === "code") {                                   // a new row follows the code's defaults
      d.threshold = defaultTh(value);
      if (!d.touchedCritical) d.is_critical = codeInfo(value).critical;
    }
    if (f === "is_critical") d.touchedCritical = true;
  } else {
    const e = T.edits[key] || (T.edits[key] = {});
    e[f] = value;
    if (f === "code") {
      const r = T.rows.find(x => x.id === +key);
      // same unit (% ↔ %, days ↔ days): keep the number; otherwise the new code's default
      e.threshold = r.code === value ? r.threshold
        : codeInfo(value).unit === codeInfo(r.code).unit ? (e.threshold ?? r.threshold) : defaultTh(value);
    }
  }
}

function wireRows() {
  $("tg-body").querySelectorAll("tr[data-key]").forEach(tr => {
    const key = tr.dataset.key;
    tr.querySelectorAll("[data-f]").forEach(el => {
      const f = el.dataset.f;
      const read = () => el.type === "checkbox" ? el.checked : el.type === "number" ? (el.value === "" ? "" : +el.value) : el.value;
      // selects re-render (code list / threshold unit change); text and numbers update in place
      if (el.tagName === "SELECT" || el.type === "checkbox") {
        el.onchange = () => { setField(key, f, read()); T.armed = null; renderTelegram(); };
      } else {
        el.oninput = () => {
          setField(key, f, read());
          if (!String(key).startsWith("d")) {
            tr.classList.toggle("dirty", isDirty(key));
            tr.querySelector('[data-act="save"]').disabled = !isDirty(key);
          }
        };
      }
    });
    tr.querySelectorAll("[data-act]").forEach(b => b.onclick = () => act(key, b.dataset.act, b));
  });
}

function showErr(key, msg) {
  const r = $("tg-body").querySelector(`[data-err="${key}"]`);
  if (!r) return toast(msg, "bad");
  r.querySelector(".err").textContent = msg; r.hidden = !msg;
}
function payload(v) {
  return {asset: v.asset, code: v.code, threshold: v.threshold === "" ? 0 : +v.threshold,
          is_enabled: !!v.is_enabled, is_critical: !!v.is_critical, note: v.note || ""};
}

async function act(key, what, btn) {
  if (what === "cancel") { T.drafts = T.drafts.filter(d => d.key !== key); renderTelegram(); return; }
  if (what === "del" && String(T.armed) !== String(key)) { T.armed = key; renderTelegram(); return; }   // 1st click arms
  btn.disabled = true;
  try {
    if (what === "add") {
      const r = await send("POST", "/notifications", payload(cur(key)));
      T.drafts = T.drafts.filter(d => d.key !== key);
      toast(`Added: ${r.asset} · ${codeLabel(r.code)}`, "ok");
    } else if (what === "save") {
      const r = await send("PUT", "/notifications/" + key, payload(cur(key)));
      toast(`Saved: ${r.asset} · ${codeLabel(r.code)}`, "ok");
    } else if (what === "del") {
      const r = await send("DELETE", "/notifications/" + key);
      toast(`Deleted: ${r.asset} · ${codeLabel(r.code)}`, "ok");
    }
    const drafts = T.drafts, edits = T.edits;
    await loadTelegram();
    T.drafts = drafts;                                    // keep other unsaved work
    for (const [k, e] of Object.entries(edits)) if (k !== key && T.rows.some(r => String(r.id) === k)) T.edits[k] = e;
    renderTelegram();
  } catch (err) {
    btn.disabled = false; showErr(key, err.message);
  }
}

function addDraft() {
  // first asset that still misses a notification, and its first missing one
  const asset = T.assets.find(a => freeCode(a, null));
  if (!asset) { toast("Every asset already has all " + T.codes.length + " notifications — edit a row instead.", "bad"); return; }
  const code = freeCode(asset, null);
  const key = "d" + T.nextDraft++;
  T.drafts.push({key, asset, code, threshold: defaultTh(code),
                 is_enabled: true, is_critical: codeInfo(code).critical, note: ""});
  renderTelegram();
  const sel = $("tg-body").querySelector(`tr[data-key="${key}"] select`);
  if (sel) sel.focus();
}

async function boot() {
  document.querySelectorAll(".st-item").forEach(b => b.onclick = () => showPanel(b.dataset.panel));
  showPanel("telegram");                                  // the only setting so far
  $("tg-add").onclick = addDraft;
  try { await loadTelegram(); }
  catch (err) { $("tg-body").innerHTML = `<tr><td colspan="7"><div class="err">Could not load: ${esc(err.message)}</div></td></tr>`; }
}
boot();
