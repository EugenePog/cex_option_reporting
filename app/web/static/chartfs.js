/* ChartFS — full-screen mode for Dashboard chart cards (OKX-style corner button).

   Mark a card with the charts that should grow:
     <div class="card" data-fs="c-pricebox">            one chart takes the free height
     <div class="card" data-fs="c-equity c-pnl">        several share it in their normal-page
                                                        proportions (both grow; "id:2" = own weight)
   ChartFS.init() (once, on load) puts the button at the right end of the card's .rpbar
   ("Reporting period" row); a card without one gets it in its top-right corner.

   Full screen: the card fills the screen, the charts take all the height that is left, the button
   turns into "exit full screen". The button or Esc returns to the normal page. Paragraphs with class
   "fs-hide" (long how-to hints) are hidden while full screen. Uses the browser Fullscreen API; where
   that is missing or refused (iPhone Safari, an iframe without allowfullscreen) the card is pinned
   over the page instead — same look, same button / Esc to return. Chart views (zoom, period, filters)
   are kept: the charts are only resized. */
(function () {
  "use strict";
  const MIN_H = 140;                                  // px, smallest chart height in full screen
  const svg = d => '<svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="' + d +
    '" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const ICON = {
    enter: svg("M2.5 6V2.5H6M10 2.5h3.5V6M13.5 10v3.5H10M6 13.5H2.5V10"),   // corners out
    exit:  svg("M6 2.5V6H2.5M13.5 6H10V2.5M10 13.5V10h3.5M2.5 10H6v3.5"),   // corners in
  };
  let cur = null;      // {card, btn, charts:[{el, w, h0}], native, ro, raf}

  const fsEl = () => document.fullscreenElement || document.webkitFullscreenElement || null;

  function setBtn(btn, on) {
    btn.innerHTML = on ? ICON.exit : ICON.enter;
    btn.title = on ? "Exit full screen (Esc)" : "Full screen";
    btn.setAttribute("aria-label", btn.title);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  }

  function chartsOf(card) {
    return card.dataset.fs.trim().split(/\s+/).map(t => {
      const [id, w] = t.split(":");
      const el = document.getElementById(id);
      return {el, w: +w > 0 ? +w : (el ? el.offsetHeight : 0) || 1};   // default: normal height
    }).filter(c => c.el && card.contains(c.el));
  }

  // the card's direct child that holds a chart (ChartNav wraps the chart in a .cn-host)
  const slotOf = (card, el) => { let n = el; while (n && n.parentNode !== card) n = n.parentNode; return n; };

  function resizeCharts(charts) {
    if (!window.Plotly) return;
    for (const c of charts) {
      if (!c.el._fullLayout) continue;                              // not plotted yet
      Promise.resolve(Plotly.Plots.resize(c.el)).then(() => {
        if (typeof c.el.emit === "function") c.el.emit("plotly_afterplot");  // ChartNav: re-place A ⟲ ?
      }).catch(() => {});
    }
  }

  // give the charts all the height the rest of the card does not use
  function fit() {
    if (!cur) return;
    const {card, charts} = cur;
    const cs = getComputedStyle(card);
    const avail = card.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
    const slots = new Set(charts.map(c => slotOf(card, c.el)));
    let other = 0;                                                   // header, filters, hints, …
    for (const ch of card.children) {
      if (slots.has(ch)) continue;
      const s = getComputedStyle(ch);
      if (s.display === "none" || s.position === "absolute" || s.position === "fixed") continue;
      other += ch.offsetHeight + parseFloat(s.marginTop) + parseFloat(s.marginBottom);
    }
    const free = Math.max(avail - other - 2, MIN_H * charts.length);
    const tw = charts.reduce((s, c) => s + c.w, 0);
    for (const c of charts) c.el.style.height = Math.max(MIN_H, Math.floor(free * c.w / tw)) + "px";
    resizeCharts(charts);
  }
  const queueFit = () => { if (!cur) return; cancelAnimationFrame(cur.raf); cur.raf = requestAnimationFrame(fit); };

  function pin() {                                    // no Fullscreen API: pin the card over the page
    if (!cur) return;
    cur.native = false;
    cur.card.classList.add("fs-pseudo");
    document.documentElement.classList.add("fs-lock");
    queueFit();
  }

  function enter(card, btn) {
    if (cur) leave();
    const charts = chartsOf(card).map(c => Object.assign(c, {h0: c.el.style.height}));
    if (!charts.length) return;
    cur = {card, btn, charts, native: false, ro: null, raf: 0};
    card.classList.add("fs-on");
    setBtn(btn, true);
    // a hint / meta line that changes height (new data) → charts fit again
    if (window.ResizeObserver) {
      const slots = new Set(charts.map(c => slotOf(card, c.el)));
      cur.ro = new ResizeObserver(queueFit);
      for (const ch of card.children) if (!slots.has(ch)) cur.ro.observe(ch);
    }
    const req = card.requestFullscreen || card.webkitRequestFullscreen;
    if (!req) { pin(); return; }
    cur.native = true;
    try {
      const p = req.call(card);
      if (p && typeof p.catch === "function") p.catch(() => { if (cur && cur.card === card && !fsEl()) pin(); });
    } catch (e) { pin(); }
  }

  function restore() {                                // back to the page: original heights
    if (!cur) return;
    const {card, btn, charts, ro} = cur;
    cancelAnimationFrame(cur.raf);
    if (ro) ro.disconnect();
    card.classList.remove("fs-on", "fs-pseudo");
    document.documentElement.classList.remove("fs-lock");
    for (const c of charts) c.el.style.height = c.h0;
    setBtn(btn, false);
    cur = null;
    resizeCharts(charts);
  }

  function leave() {
    if (!cur) return;
    if (cur.native && fsEl() === cur.card) {
      (document.exitFullscreen || document.webkitExitFullscreen).call(document);  // → onChange → restore
    } else {
      restore();
    }
  }

  function onChange() {
    if (!cur || !cur.native) return;
    if (fsEl() === cur.card) queueFit();              // now full screen: fit to the screen
    else if (!fsEl()) restore();                      // Esc / browser UI / leave()
  }

  function init() {
    document.querySelectorAll(".card[data-fs]").forEach(card => {
      if (card._fsBtn) return;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "fs-btn";
      setBtn(btn, false);
      btn.addEventListener("click", () => (cur && cur.card === card) ? leave() : enter(card, btn));
      const bar = card.querySelector(".rpbar");
      if (bar) bar.appendChild(btn); else { card.classList.add("fs-abs"); card.appendChild(btn); }
      card._fsBtn = btn;
    });
  }

  document.addEventListener("fullscreenchange", onChange);
  document.addEventListener("webkitfullscreenchange", onChange);
  window.addEventListener("resize", queueFit);
  document.addEventListener("keydown", e => {         // Esc for the pinned fallback (native: browser)
    if (e.key === "Escape" && cur && !cur.native) restore();
  });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();

  window.ChartFS = {init, enter: card => card._fsBtn && enter(card, card._fsBtn), leave,
                    get active() { return cur ? cur.card : null; }};
})();
