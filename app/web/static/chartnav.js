/* ChartNav — TradingView-style navigation for one Plotly chart (build 9). Used by graph ①
   "Price with strategy boxes" on the Dashboard.

   Mouse
     drag on the chart ............ move in time; drag up/down (more than a little) also moves the
                                    prices, and the price scale stops fitting itself
     drag on the price scale ...... stretch / squeeze prices around the middle (up = zoom in)
     drag on the date scale ....... stretch / squeeze time, right edge fixed (right = zoom in)
     wheel on the chart / dates ... zoom time at the cursor; prices fit themselves
     wheel on the price scale ..... zoom prices at the cursor (turns the price fit off)
     double-click: chart = back to the reporting period, prices fit again;
                   price scale = prices fit again; date scale = time back to the reporting period
   Trackpad (Mac): two fingers left / right = move in time, up / down = zoom time, pinch = zoom.
   The wheel / two-finger scroll works after a click into the chart (a blue outline shows it);
   until then it scrolls the page. A pinch (or Ctrl + wheel) always zooms the chart. Esc or a
   click outside the chart gives the wheel back to the page.
   Buttons under the price scale: A = prices fit themselves (on / off), ⟲ = reporting period, ? = help.

   ChartNav.attach(div) once; after every Plotly.newPlot call nav.reset({...}) with
     home()        → [t0, t1] ms: the reporting-period view (double-click / ⟲)
     fitY(t0, t1)  → [lo, hi] price range for a time window, or null
     extra(t0, t1) → more Plotly.relayout keys for a new view (e.g. labels kept in view)
     onView(t0, t1)  after every view change (the page loads candles there)
     limits()      → {minSpan, maxSpan} ms for the time zoom
   The x axis must be a Plotly date axis ('xaxis'), prices on 'yaxis'; other y axes should have
   fixedrange: true. Times are ms since epoch (Plotly date axes are UTC-naive). */
(function () {
  "use strict";
  const K = {
    wheel: 0.0015,               // zoom per wheel pixel: one 100 px notch ≈ ×1.16
    pinch: 0.01,                 // zoom per pinch unit (ctrl + wheel)
    maxDelta: 150,               // biggest single wheel step used (fast mice / momentum)
    priceDrag: Math.LN2 / 160,   // 160 px on the price scale = ×2
    timeDrag: Math.LN2 / 160,    // 160 px on the date scale = ×2
    deadY: 18,                   // px up/down before a chart drag starts moving the prices
    gestureMs: 160,              // quiet time that ends a trackpad gesture (axis lock)
    hintMs: 1600,
  };
  const DELTA_UNIT = {0: 1, 1: 33, 2: 800};     // deltaMode: pixels / lines / pages → px
  const HELP = [
    "Drag the chart: move in time (drag up / down: move prices too)",
    "Drag the price scale: stretch / squeeze prices",
    "Drag the date scale: stretch / squeeze time",
    "Wheel on the chart or dates: zoom time — click the chart first",
    "Wheel on the price scale: zoom prices",
    "Trackpad: two fingers ← → move, ↑ ↓ zoom, pinch zoom",
    "Double-click: chart = reporting period · price scale = fit prices · dates = reporting period",
    "A: prices fit what is on screen · ⟲: back to the reporting period · Esc: release the wheel",
  ].join("\n");

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
.cn-tools{position:absolute;display:flex;gap:3px;z-index:2}
.cn-tools button{width:21px;height:19px;padding:0;border-radius:4px;border:1px solid #333b47;background:#1b2028;
  color:#9aa4b2;font:700 11px/17px -apple-system,Segoe UI,Roboto,sans-serif;cursor:pointer}
.cn-tools button:hover{color:#e6edf7;border-color:#4a5566}
.cn-tools button.on{background:#1f3b63;border-color:#2e5f9b;color:#cfe3ff}
.cn-tools button.help{cursor:help}`;
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
    tools.innerHTML = '<button type="button" class="auto" title="Prices fit what is on screen (on / off) — double-click the price scale turns it on">A</button>' +
      '<button type="button" class="home" title="Back to the reporting period (double-click the chart)">⟲</button>' +
      '<button type="button" class="help">?</button>';
    tools.querySelector(".help").title = HELP;
    host.appendChild(tools);

    const S = {opts: null, active: false, auto: true, drag: null, next: null, raf: 0, own: 0,
               gest: {axis: null, t: 0}, hintT: 0, pinch: 1, lastXY: null};

    // ---- geometry / view ------------------------------------------------------------------
    function geo() {
      const L = gd._fullLayout;
      if (!L || !L.xaxis || !L.yaxis || !L._size || !L.xaxis._length) return null;
      const xa = L.xaxis, ya = L.yaxis, sz = L._size;
      return {xa, ya, l: sz.l, t: sz.t, w: sz.w, h: sz.h, W: L.width, H: L.height,
              x0: xa._offset, xw: xa._length, y0: ya._offset, yh: ya._length};
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
      if (px > g.l + g.w && px <= g.W && py >= g.y0 - 6 && py <= g.y0 + g.yh + 6)
        return {zone: "price", px, py};
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
    function queue(ch) {
      const n = S.next || (S.next = {});
      if (ch.x) n.x = ch.x;
      if (ch.y) n.y = ch.y;
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
      if (S.opts.extra) Object.assign(upd, S.opts.extra(x[0], x[1]) || {});
      if (Object.keys(upd).length) relayout(upd);
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
      if (on) queue({});                       // fit now
    }
    function goHome() {
      if (!S.opts || !S.opts.home) return;
      const h = S.opts.home();
      if (h) queue({x: h});
    }

    // ---- wheel only after a click into the chart --------------------------------------------
    function setActive(on) {
      if (S.active === on) return;
      S.active = on;
      host.classList.toggle("cn-active", on);
      if (on) hint.classList.remove("show");
    }
    function showHint() {
      hint.classList.add("show");
      clearTimeout(S.hintT);
      S.hintT = setTimeout(() => hint.classList.remove("show"), K.hintMs);
    }
    host.addEventListener("pointerdown", () => setActive(true), true);
    document.addEventListener("pointerdown", e => { if (!host.contains(e.target)) setActive(false); }, true);
    document.addEventListener("keydown", e => { if (e.key === "Escape") setActive(false); });

    // ---- drag: chart = move, price scale = scale prices, date scale = scale time ------------
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
    function onDbl(e) {
      const g = geo();
      if (!g || !S.opts) return;
      const z = zoneAt(e, g);
      if (!z) return;
      e.preventDefault();
      e.stopPropagation();
      if (z.zone === "price") { setAuto(true); queue({}); }
      else if (z.zone === "date") goHome();
      else { setAuto(true); goHome(); }
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
    tools.querySelector(".home").onclick = () => { setAuto(true); goHome(); };

    // buttons sit under the price scale, in the gap above the lower strip
    function place() {
      const g = geo();
      if (!g) { tools.style.display = "none"; return; }
      tools.style.display = "";
      tools.style.left = (g.l + g.w + 6) + "px";
      tools.style.top = (g.y0 + g.yh + 4) + "px";
    }

    const nav = {
      /** after every Plotly.newPlot: new callbacks, prices fit again, Plotly events re-bound */
      reset(opts) {
        S.opts = opts;
        S.next = null;
        S.auto = true;
        tools.querySelector(".auto").classList.add("on");
        if (S.drag) onUp();
        // Plotly.newPlot drops gd.on handlers → bind again
        gd.on("plotly_afterplot", place);
        gd.on("plotly_restyle", () => { if (S.auto) queue({}); });     // legend click, new candles
        gd.on("plotly_relayout", ev => {                                // Plotly's own (touch) pan
          if (S.own) return;
          if ("xaxis.range[0]" in ev || "xaxis.range" in ev) {
            const g = geo();
            if (g) { const c = current(g); queue({x: [c.x0, c.x1]}); }
          }
        });
        place();
      },
      refit() { if (S.auto) queue({}); },
      get autoFit() { return S.auto; },
      get active() { return S.active; },
      view() { const g = geo(); return g ? view(g) : null; },
      setView(x, y) { queue({x, y}); },
      _K: K,
    };
    gd._chartNav = nav;
    return nav;
  }

  window.ChartNav = {attach};
})();
