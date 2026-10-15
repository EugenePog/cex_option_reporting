/* ChartNav — TradingView-style navigation for Plotly time charts (build 9; build 10: also graphs ②
   and ②b). Used by the Dashboard: ① "Price with strategy boxes", ② "Equity curve & daily net P&L"
   and ②b "Asset equity & daily net P&L" (two charts each).

   Mouse
     drag on the chart ............ move in time; drag up/down (more than a little) also moves the
                                    values, and the value scale stops fitting itself
     drag on the value scale ...... stretch / squeeze values around the middle (up = zoom in) — the
                                    price scale of ①, the equity / P&L scale of ② (either side)
     drag on the date scale ....... stretch / squeeze time, right edge fixed (right = zoom in)
     wheel on the chart / dates ... zoom time at the cursor; values fit themselves
     wheel on the value scale ..... zoom values at the cursor (turns the fit off)
     double-click: chart = back to the reporting period, values fit again;
                   value scale = values fit again; date scale = time back to the reporting period
   Trackpad (Mac): two fingers left / right = move in time, up / down = zoom time, pinch = zoom.
   The wheel / two-finger scroll works after a click into the chart (a blue outline shows it);
   until then it scrolls the page. A pinch (or Ctrl + wheel) always zooms the chart. Esc or a
   click outside the chart gives the wheel back to the page.
   Buttons under the value scale: A = values fit themselves (on / off), ⟲ = reporting period, ? = help.

   ChartNav.attach(div) once; after every Plotly.newPlot call nav.reset({...}) with
     home()        → [t0, t1] ms: the reporting-period view (double-click / ⟲)
     fitY(t0, t1)  → [lo, hi] value range for a time window, or null
     extra(t0, t1) → more Plotly.relayout keys for a new view (e.g. labels kept in view)
     onView(t0, t1)  after every view change (the page loads more data there)
     limits()      → {minSpan, maxSpan} ms for the time zoom
     ticks         → build 9 rev 2: date ticks this many times denser than Plotly's own (e.g. 2)
     now           → build 9 rev 2: {shape, traces, skew} — the "now" line (ChartNav.nowShape /
                     nowTrace) moves on every minute; skew = server clock − browser clock (ms)
     words         → {scale, values} for the help texts ("price scale" / "prices" by default)
   ChartNav.group([navA, navB], {syncX}) — build 10: charts of one card share the wheel (one click
   activates both); syncX = they also move / zoom together (graph ② "Sync timelines").
   The x axis must be a Plotly date axis ('xaxis'), values on 'yaxis' (left or right); other y
   axes should have fixedrange: true. Times are ms since epoch (Plotly date axes are UTC-naive). */
(function () {
  "use strict";
  const K = {
    wheel: 0.0015,               // zoom per wheel pixel: one 100 px notch ≈ ×1.16
    pinch: 0.01,                 // zoom per pinch unit (ctrl + wheel)
    maxDelta: 150,               // biggest single wheel step used (fast mice / momentum)
    priceDrag: Math.LN2 / 160,   // 160 px on the value scale = ×2
    timeDrag: Math.LN2 / 160,    // 160 px on the date scale = ×2
    deadY: 18,                   // px up/down before a chart drag starts moving the values
    gestureMs: 160,              // quiet time that ends a trackpad gesture (axis lock)
    hintMs: 1600,
    nowMs: 60e3,                 // the "now" line moves on every minute
  };
  const DELTA_UNIT = {0: 1, 1: 33, 2: 800};     // deltaMode: pixels / lines / pages → px
  const WORDS = {scale: "price scale", values: "prices"};
  const help = w => [
    `Drag the chart: move in time (drag up / down: move ${w.values} too)`,
    `Drag the ${w.scale}: stretch / squeeze ${w.values}`,
    "Drag the date scale: stretch / squeeze time",
    "Wheel on the chart or dates: zoom time — click the chart first",
    `Wheel on the ${w.scale}: zoom ${w.values}`,
    "Trackpad: two fingers ← → move, ↑ ↓ zoom, pinch zoom",
    `Double-click: chart = reporting period · ${w.scale} = fit ${w.values} · dates = reporting period`,
    `A: ${w.values} fit what is on screen · ⟲: back to the reporting period · Esc: release the wheel`,
  ].join("\n");

  // ---- shared helpers (also used by the page) -------------------------------------------------
  const msPlot = ms => new Date(ms).toISOString().slice(0, 23).replace("T", " ");  // UTC-naive
  // Date ticks: Plotly picks about one date tick per 80 px (at most 10) from a list of round steps;
  // dateTicks takes the round step closest to 1/density of that (density 2 = twice as many ticks:
  // 3 days instead of 7 for a month, 12 hours instead of a day for a week).
  const T_MIN = 6e4, T_HOUR = 36e5, T_DAY = 864e5, T_MONTH = 365.25 / 12 * T_DAY, T_YEAR = 365.25 * T_DAY;
  const LADDER = [1, 2, 5, 10, 15, 30].map(m => m * T_MIN)
    .concat([1, 2, 3, 6, 12].map(h => h * T_HOUR), [1, 2, 3, 7, 14].map(d => d * T_DAY),
            [1, 2, 3, 6, 12, 24, 60, 120].map(m => m * T_MONTH));
  function plotlyStep(span, len) {               // Plotly's own pick (axes.autoTicks, date axis)
    const nt = Math.min(Math.max(len / 80, 4), 9) + 1, rough = span / nt, r2 = 2 * rough;
    const up = (v, set) => set.find(x => x > v) ?? set[set.length - 1];
    if (r2 > T_YEAR) {
      const y = rough / T_YEAR, b = Math.pow(10, Math.floor(Math.log10(y)));
      return up(y / b, [2, 5, 10]) * b * T_YEAR;
    }
    if (r2 > T_MONTH) return up(rough / T_MONTH, [1, 2, 3, 6, 12]) * T_MONTH;
    if (r2 > T_DAY) return up(rough / T_DAY, [1, 2, 3, 7, 14]) * T_DAY;
    if (r2 > T_HOUR) return up(rough / T_HOUR, [1, 2, 3, 6, 12]) * T_HOUR;
    return up(rough / T_MIN, [1, 2, 5, 10, 15, 30]) * T_MIN;
  }
  function dateTicks(t0, t1, len, density) {
    const want = plotlyStep(Math.max(t1 - t0, 1), Math.max(len || 0, 1)) / (density || 2);
    let best = LADDER[0];
    for (const st of LADDER)
      if (Math.abs(Math.log(st / want)) < Math.abs(Math.log(best / want)) - 1e-9) best = st;
    if (best >= T_MONTH * 0.99) return {dtick: "M" + Math.round(best / T_MONTH), tick0: "2000-01-01"};
    return {dtick: best, tick0: best >= T_DAY ? "2000-01-02" : "2000-01-01"};   // days: from a Sunday
  }
  // "Now": a dotted line (shape) + invisible points along it that carry the hover label. The points
  // sit on a hidden 0…1 axis over the plot (NOW_AXIS, overlaying 'y'), so no value range changes.
  const NOW_AXIS = {overlaying: "y", range: [0, 1], visible: false, fixedrange: true};
  function nowText(ms) {
    const t = new Date(ms), p = n => String(n).padStart(2, "0"), off = -t.getTimezoneOffset();
    const local = `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())} ${p(t.getHours())}:${p(t.getMinutes())}`;
    const tz = "UTC" + (off < 0 ? "−" : "+") + Math.floor(Math.abs(off) / 60) +
      (Math.abs(off) % 60 ? ":" + p(Math.abs(off) % 60) : "");
    return `<b>Now</b><br>${t.toISOString().slice(0, 16).replace("T", " ")} UTC<br>${local} your time (${tz})`;
  }
  const nowShape = ms => ({type: "line", xref: "x", x0: msPlot(ms), x1: msPlot(ms), yref: "paper", y0: 0, y1: 1,
    line: {color: "#8b93a1", width: 1, dash: "dot"}});
  function nowTrace(ms, yaxis, ys) {
    ys = ys || Array.from({length: 41}, (_, i) => i / 40);
    return {type: "scatter", mode: "markers", name: "now", showlegend: false, xaxis: "x", yaxis,
      x: ys.map(() => msPlot(ms)), y: ys, marker: {size: 8, color: "rgba(0,0,0,0)"},
      hoverinfo: "text", text: nowText(ms),
      hoverlabel: {bgcolor: "#1b1f26", bordercolor: "#8b93a1", font: {color: "#e6edf7", size: 12}}};
  }

  let cssDone = false;
  function addCss() {
    if (cssDone) return;
    cssDone = true;
    const st = document.createElement("style");
    st.textContent = `
.cn-host{position:relative;border-radius:6px;transition:box-shadow .15s}
.cn-host.cn-active{box-shadow:0 0 0 1px rgba(59,130,246,.55)}
.cn-host .nsewdrag{cursor:grab!important}
.cn-host .nsdrag,.cn-host .ndrag,.cn-host .sdrag{cursor:ns-resize!important}
.cn-host .ewdrag,.cn-host .wdrag,.cn-host .edrag{cursor:ew-resize!important}
.cn-host.cn-drag-plot,.cn-host.cn-drag-plot *{cursor:grabbing!important}
.cn-host.cn-drag-price,.cn-host.cn-drag-price *{cursor:ns-resize!important}
.cn-host.cn-drag-date,.cn-host.cn-drag-date *{cursor:ew-resize!important}
.cn-host.cn-drag .hoverlayer{display:none}
.cn-hint{position:absolute;left:50%;top:42%;transform:translate(-50%,-50%);z-index:3;pointer-events:none;
  background:rgba(14,17,23,.9);border:1px solid #2e3a4d;color:#e6edf7;padding:8px 14px;border-radius:8px;
  font-size:13px;white-space:nowrap;opacity:0;transition:opacity .18s}
.cn-hint.show{opacity:1}
.cn-tools{position:absolute;display:flex;gap:2px;z-index:2}
.cn-tools button{width:19px;height:18px;padding:0;border-radius:4px;border:1px solid #333b47;background:#1b2028;
  color:#9aa4b2;font:700 11px/16px -apple-system,Segoe UI,Roboto,sans-serif;cursor:pointer}
.cn-tools button:hover{color:#e6edf7;border-color:#4a5566}
.cn-tools button.on{background:#1f3b63;border-color:#2e5f9b;color:#cfe3ff}
.cn-tools button.help{cursor:help}
.cn-host.cn-compact .cn-tools{opacity:0;transition:opacity .15s}
.cn-host.cn-compact:hover .cn-tools,.cn-host.cn-compact.cn-active .cn-tools,
.cn-host.cn-compact.cn-fitoff .cn-tools{opacity:1}`;
    document.head.appendChild(st);
  }

  function attach(gd) {
    if (gd._chartNav) return gd._chartNav;
    addCss();
    // a positioned host around the chart, for the outline, the hint and the buttons
    const host = document.createElement("div");
    host.className = "cn-host";
    gd.parentNode.insertBefore(host, gd);
    host.appendChild(gd);
    const hint = document.createElement("div");
    hint.className = "cn-hint";
    hint.textContent = "Click the chart to zoom with the wheel";
    host.appendChild(hint);
    const tools = document.createElement("div");
    tools.className = "cn-tools";
    tools.innerHTML = '<button type="button" class="auto">A</button>' +
      '<button type="button" class="home" title="Back to the reporting period (double-click the chart)">⟲</button>' +
      '<button type="button" class="help">?</button>';
    host.appendChild(tools);

    const S = {opts: null, active: false, auto: true, drag: null, next: null, raf: 0, own: 0,
               gest: {axis: null, t: 0}, hintT: 0, pinch: 1, peers: [], xPeers: [], dtick: null,
               nowTimer: 0};

    // ---- geometry / view ------------------------------------------------------------------
    function geo() {
      const L = gd._fullLayout;
      if (!L || !L.xaxis || !L.yaxis || !L._size || !L.xaxis._length) return null;
      const xa = L.xaxis, ya = L.yaxis, sz = L._size;
      return {xa, ya, l: sz.l, t: sz.t, w: sz.w, h: sz.h, W: L.width, H: L.height,
              x0: xa._offset, xw: xa._length, y0: ya._offset, yh: ya._length, left: ya.side === "left"};
    }
    function current(g) {
      const xr = g.xa.range, yr = g.ya.range;
      return {x0: g.xa.r2l(xr[0]), x1: g.xa.r2l(xr[1]), y0: +yr[0], y1: +yr[1]};
    }
    // the view as it will be after the queued change (wheel steps add up within a frame)
    function view(g) {
      const c = current(g), n = S.next;
      if (n && n.x) { c.x0 = n.x[0]; c.x1 = n.x[1]; }
      if (n && n.y) { c.y0 = n.y[0]; c.y1 = n.y[1]; }
      return c;
    }
    function zoneAt(e, g) {
      if (e.target && e.target.closest && e.target.closest(".legend")) return null;
      const r = gd.getBoundingClientRect(), px = e.clientX - r.left, py = e.clientY - r.top;
      const inX = px >= g.l && px <= g.l + g.w, inY = py >= g.t && py <= g.t + g.h;
      if (inX && inY) return {zone: "plot", px, py, price: py >= g.y0 && py <= g.y0 + g.yh};
      // the value scale: the margin on the side of the y axis (build 10: left or right)
      const onScale = g.left ? px >= 0 && px < g.l : px > g.l + g.w && px <= g.W;
      if (onScale && py >= g.y0 - 6 && py <= g.y0 + g.yh + 6) return {zone: "price", px, py};
      if (inX && py > g.t + g.h && py <= g.H) return {zone: "date", px, py};
      return null;
    }
    function clampSpan(x0, x1, anchor) {
      const lim = S.opts && S.opts.limits ? S.opts.limits() : null;
      const sp = x1 - x0;
      if (!lim || !(sp > 0)) return [x0, x1];
      const ns = Math.min(Math.max(sp, lim.minSpan || 0), lim.maxSpan || Infinity);
      if (ns === sp) return [x0, x1];
      const f = (anchor - x0) / sp;
      return [anchor - f * ns, anchor + (1 - f) * ns];
    }
    function zoomTime(v, anchor, f) {
      return clampSpan(anchor - (anchor - v.x0) * f, anchor + (v.x1 - anchor) * f, anchor);
    }

    // ---- applying a view: one Plotly.relayout per animation frame ---------------------------
    // ch.peer = the change came from a linked chart (not passed on again)
    function queue(ch) {
      const n = S.next || (S.next = {});
      if (ch.x) n.x = ch.x;
      if (ch.y) n.y = ch.y;
      n.peer = n.peer === undefined ? !!ch.peer : n.peer && !!ch.peer;
      if (!S.raf) S.raf = requestAnimationFrame(frame);
    }
    function frame() {
      S.raf = 0;
      const n = S.next, g = geo();
      S.next = null;
      if (!n || !g || !S.opts) return;
      const c = current(g), x = n.x || [c.x0, c.x1];
      const upd = {};
      if (n.x) upd["xaxis.range"] = [g.xa.l2r(x[0]), g.xa.l2r(x[1])];
      let y = n.y || null;
      if (S.auto) y = fit(x[0], x[1]) || y;
      if (y && (y[0] !== c.y0 || y[1] !== c.y1)) upd["yaxis.range"] = y;
      if (S.opts.ticks && n.x) Object.assign(upd, tickUpdate(x[0], x[1], g.xw));
      if (S.opts.extra) Object.assign(upd, S.opts.extra(x[0], x[1]) || {});
      if (Object.keys(upd).length) relayout(upd);
      if (n.x && !n.peer) for (const p of S.xPeers) p._follow(x);
      if (n.x && S.opts.onView) S.opts.onView(x[0], x[1]);
    }
    function relayout(upd) {
      S.own++;
      const done = () => { S.own--; };
      const p = Plotly.relayout(gd, upd);
      if (p && p.then) p.then(done, done); else done();
    }
    function fit(x0, x1) {
      const r = S.opts && S.opts.fitY ? S.opts.fitY(x0, x1) : null;
      return r && isFinite(r[0]) && isFinite(r[1]) && r[1] > r[0] ? r : null;
    }
    function setAuto(on) {
      if (S.auto === on) return;
      S.auto = on;
      tools.querySelector(".auto").classList.toggle("on", on);
      host.classList.toggle("cn-fitoff", !on);
      if (on) queue({});                       // fit now
    }
    function goHome() {
      if (!S.opts || !S.opts.home) return;
      const h = S.opts.home();
      if (h) queue({x: h});
    }
    // dense date ticks (build 9 rev 2): the step for a view; only the keys that change
    function tickUpdate(x0, x1, len) {
      const tk = dateTicks(x0, x1, len, S.opts.ticks);
      if (tk.dtick === S.dtick) return {};
      S.dtick = tk.dtick;
      return {"xaxis.dtick": tk.dtick, "xaxis.tick0": tk.tick0};
    }
    function retick() {                        // after a resize: same view, new axis length
      const g = geo();
      if (!g || !S.opts || !S.opts.ticks) return;
      const c = current(g), upd = tickUpdate(c.x0, c.x1, g.xw);
      if (Object.keys(upd).length) relayout(upd);
    }
    // the "now" line (build 9 rev 2): moves on every minute
    function moveNow() {
      const o = S.opts && S.opts.now, L = gd._fullLayout;
      if (!o || !L || !L.shapes || !L.shapes[o.shape] || !gd.data) return;
      const ms = Date.now() + (o.skew || 0), x = msPlot(ms);
      relayout({[`shapes[${o.shape}].x0`]: x, [`shapes[${o.shape}].x1`]: x});
      Plotly.restyle(gd, {x: o.traces.map(i => gd.data[i].x.map(() => x)),
                          text: o.traces.map(() => nowText(ms))}, o.traces);
    }

    // ---- wheel only after a click into the chart (build 10: shared by the charts of a group) --
    function setActive(on, fromPeer) {
      if (S.active !== on) {
        S.active = on;
        host.classList.toggle("cn-active", on);
        if (on) hint.classList.remove("show");
      }
      if (!fromPeer) for (const p of S.peers) p._setActive(on);
    }
    function showHint() {
      hint.classList.add("show");
      clearTimeout(S.hintT);
      S.hintT = setTimeout(() => hint.classList.remove("show"), K.hintMs);
    }
    const inGroup = t => host.contains(t) || S.peers.some(p => p._host.contains(t));
    host.addEventListener("pointerdown", () => setActive(true), true);
    document.addEventListener("pointerdown", e => { if (!inGroup(e.target)) setActive(false); }, true);
    document.addEventListener("keydown", e => { if (e.key === "Escape") setActive(false); });

    // ---- drag: chart = move, value scale = scale values, date scale = scale time ------------
    function onDown(e) {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      const g = geo();
      if (!g || !S.opts) return;
      const z = zoneAt(e, g);
      if (!z || e.pointerType === "touch") return;   // touch: Plotly's own pan, refit after it
      e.preventDefault();                             // no Plotly drag, no text selection
      e.stopPropagation();
      if (Plotly.Fx) Plotly.Fx.unhover(gd);
      const v = view(g);
      S.drag = {zone: z.zone, price: z.price, sx: e.clientX, sy: e.clientY, g, v, moved: false,
                yFree: !S.auto, ay: e.clientY, vy: [v.y0, v.y1]};
      host.classList.add("cn-drag", "cn-drag-" + z.zone);
      window.addEventListener("pointermove", onMove, true);
      window.addEventListener("pointerup", onUp, true);
      window.addEventListener("pointercancel", onUp, true);
    }
    function onMove(e) {
      const d = S.drag;
      if (!d) return;
      e.preventDefault();
      const dx = e.clientX - d.sx, dy = e.clientY - d.sy, g = d.g, v = d.v, span = v.x1 - v.x0;
      if (!d.moved && Math.abs(dx) < 3 && Math.abs(dy) < 3) return;
      d.moved = true;
      if (d.zone === "plot") {
        const sh = -dx * span / g.xw;
        const ch = {x: [v.x0 + sh, v.x1 + sh]};
        if (d.price) {
          if (!d.yFree && Math.abs(dy) > K.deadY && Math.abs(dy) > 0.5 * Math.abs(dx)) {
            const c = view(g);
            d.yFree = true; d.ay = e.clientY; d.vy = [c.y0, c.y1];
            setAuto(false);
          }
          if (d.yFree) {
            const k = (d.vy[1] - d.vy[0]) / g.yh, s = (e.clientY - d.ay) * k;
            ch.y = [d.vy[0] + s, d.vy[1] + s];
          }
        }
        queue(ch);
      } else if (d.zone === "price") {
        setAuto(false);
        const f = Math.exp(dy * K.priceDrag), mid = (v.y0 + v.y1) / 2, half = (v.y1 - v.y0) / 2 * f;
        queue({y: [mid - half, mid + half]});
      } else {
        queue({x: zoomTime(v, v.x1, Math.exp(-dx * K.timeDrag))});
      }
    }
    function onUp() {
      S.drag = null;
      host.classList.remove("cn-drag", "cn-drag-plot", "cn-drag-price", "cn-drag-date");
      window.removeEventListener("pointermove", onMove, true);
      window.removeEventListener("pointerup", onUp, true);
      window.removeEventListener("pointercancel", onUp, true);
    }

    // ---- wheel / trackpad --------------------------------------------------------------------
    function onWheel(e) {
      const g = geo();
      if (!g || !S.opts) return;
      const z = zoneAt(e, g);
      if (!z) return;
      const pinch = e.ctrlKey;                  // Mac trackpad pinch (Chrome / Firefox), Ctrl + wheel
      if (!S.active && !pinch) { showHint(); return; }   // the page scrolls
      e.preventDefault();
      e.stopPropagation();
      const u = DELTA_UNIT[e.deltaMode] || 1, lim = x => Math.max(-K.maxDelta, Math.min(K.maxDelta, x));
      let dx = e.deltaX * u, dy = e.deltaY * u;
      if (e.shiftKey && !dx) { dx = dy; dy = 0; }   // Shift + wheel = sideways
      const v = view(g);
      if (z.zone === "price") {
        if (!dy) return;
        setAuto(false);
        const p = v.y1 - (z.py - g.y0) / g.yh * (v.y1 - v.y0);
        const f = Math.exp(lim(dy) * (pinch ? K.pinch : K.wheel));
        queue({y: [p - (p - v.y0) * f, p + (v.y1 - p) * f]});
        return;
      }
      const anchor = v.x0 + (z.px - g.x0) / g.xw * (v.x1 - v.x0);
      if (pinch) { queue({x: zoomTime(v, anchor, Math.exp(lim(dy) * K.pinch))}); return; }
      // one direction per gesture: a sideways swipe doesn't zoom, a vertical one doesn't move
      const now = performance.now();
      if (now - S.gest.t > K.gestureMs || !S.gest.axis) S.gest.axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
      S.gest.t = now;
      if (S.gest.axis === "x") {
        if (!dx) return;
        const sh = dx * (v.x1 - v.x0) / g.xw;
        queue({x: [v.x0 + sh, v.x1 + sh]});
      } else if (dy) {
        queue({x: zoomTime(v, anchor, Math.exp(lim(dy) * K.wheel))});
      }
    }
    // Safari pinch (gesture events instead of ctrl + wheel)
    function onGesture(e) {
      const g = geo();
      if (!g || !S.opts) return;
      e.preventDefault();
      if (e.type === "gesturestart") { S.pinch = 1; return; }
      const r = S.pinch / (e.scale || 1);
      S.pinch = e.scale || 1;
      const v = view(g), rect = gd.getBoundingClientRect();
      const px = (e.clientX != null ? e.clientX : rect.left + g.x0 + g.xw / 2) - rect.left;
      const anchor = v.x0 + (px - g.x0) / g.xw * (v.x1 - v.x0);
      queue({x: zoomTime(v, anchor, r)});
    }

    // ---- double-click ----------------------------------------------------------------------
    // chart / ⟲: back to the period and the values fit again — on the linked charts too
    function homeAll() {
      setAuto(true);
      for (const p of S.xPeers) p._setAuto(true);
      goHome();
    }
    function onDbl(e) {
      const g = geo();
      if (!g || !S.opts) return;
      const z = zoneAt(e, g);
      if (!z) return;
      e.preventDefault();
      e.stopPropagation();
      if (z.zone === "price") { setAuto(true); queue({}); }
      else if (z.zone === "date") goHome();
      else homeAll();
    }

    // ---- cursor over the scales (the parts Plotly has no handles for) -----------------------
    function onHover(e) {
      if (S.drag) return;
      const g = geo();
      const z = g ? zoneAt(e, g) : null;
      gd.style.cursor = z && z.zone === "price" ? "ns-resize" : z && z.zone === "date" ? "ew-resize" : "";
    }

    gd.addEventListener("pointerdown", onDown, true);
    gd.addEventListener("wheel", onWheel, {capture: true, passive: false});
    gd.addEventListener("dblclick", onDbl, true);
    gd.addEventListener("pointermove", onHover, {passive: true});
    gd.addEventListener("gesturestart", onGesture, {passive: false});
    gd.addEventListener("gesturechange", onGesture, {passive: false});
    tools.querySelector(".auto").onclick = () => setAuto(!S.auto);
    tools.querySelector(".home").onclick = homeAll;

    // buttons under the value scale: right scale (①) = just right of the plot, under the price
    // area; left scale (② / ②b) = the corner under the scale's labels — shown there only while the
    // mouse is over the chart, the chart has the wheel or the fit is off (they would cover the
    // first date label)
    function place() {
      const g = geo();
      if (!g) { tools.style.display = "none"; return; }
      tools.style.display = "";
      host.classList.toggle("cn-compact", g.left);
      tools.style.left = (g.left ? Math.max(2, g.l - 62) : g.l + g.w + 6) + "px";
      tools.style.top = (g.y0 + g.yh + (g.left ? 3 : 4)) + "px";
    }

    const nav = {
      /** after every Plotly.newPlot: new callbacks, values fit again, Plotly events re-bound */
      reset(opts) {
        S.opts = opts;
        S.next = null;
        S.auto = true;
        host.classList.remove("cn-fitoff");
        S.peers = [];
        S.xPeers = [];
        const w = Object.assign({}, WORDS, opts.words || {});
        tools.querySelector(".auto").classList.add("on");
        tools.querySelector(".auto").title =
          `${w.values[0].toUpperCase() + w.values.slice(1)} fit what is on screen (on / off) — double-click the ${w.scale} turns it on`;
        tools.querySelector(".help").title = help(w);
        if (S.drag) onUp();
        // Plotly.newPlot drops gd.on handlers → bind again
        gd.on("plotly_afterplot", () => { place(); retick(); });
        gd.on("plotly_restyle", () => { if (S.auto) queue({}); });     // legend click, new data
        gd.on("plotly_relayout", ev => {                                // Plotly's own (touch) pan
          if (S.own) return;
          if ("xaxis.range[0]" in ev || "xaxis.range" in ev) {
            const g = geo();
            if (g) { const c = current(g); queue({x: [c.x0, c.x1]}); }
          }
        });
        const L = gd._fullLayout;
        S.dtick = L && L.xaxis ? L.xaxis.dtick : null;
        clearInterval(S.nowTimer);
        if (opts.now) S.nowTimer = setInterval(moveNow, K.nowMs);
        place();
        retick();
      },
      refit() { if (S.auto) queue({}); },
      get autoFit() { return S.auto; },
      get active() { return S.active; },
      view() { const g = geo(); return g ? view(g) : null; },
      setView(x, y) { queue({x, y}); },
      moveNow,
      // group plumbing (ChartNav.group)
      _host: host,
      _setActive(on) { setActive(on, true); },
      _setAuto(on) { setAuto(on); },
      _follow(x) { queue({x, peer: true}); },
      _setPeers(peers, syncX) { S.peers = peers; S.xPeers = syncX ? peers : []; },
      _K: K,
    };
    gd._chartNav = nav;
    return nav;
  }

  /** Charts of one card: one click gives the wheel to all of them; syncX = they move together. */
  function group(navs, o) {
    for (const a of navs) a._setPeers(navs.filter(b => b !== a), !!(o && o.syncX));
  }

  window.ChartNav = {attach, group, dateTicks, msPlot, nowText, nowShape, nowTrace, NOW_AXIS};
})();
