/* Shared helpers: JSON fetch + Plotly dark theme + formatters. */
const DARK = {
  paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
  font: { color: "#c7cdd6", family: "-apple-system,Segoe UI,Roboto,sans-serif" },
  margin: { l: 60, r: 20, t: 30, b: 40 },
  xaxis: { gridcolor: "#232833", zerolinecolor: "#333" },
  yaxis: { gridcolor: "#232833", zerolinecolor: "#333" },
  legend: { orientation: "h", y: 1.12, x: 0 },
};
const COL = { blue:"#3b82f6", green:"#22c55e", red:"#ef4444", orange:"#e2632a", muted:"#8b93a1" };
const CFG = { displayModeBar:false, responsive:true };

async function api(path, params) {
  const qs = params ? "?" + new URLSearchParams(Object.entries(params).filter(([,v])=>v!=null&&v!=="")).toString() : "";
  const r = await fetch("/api" + path + qs);
  if (r.status === 401) { location.href = "/login"; return null; }
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}
const layout = (o={}) => JSON.parse(JSON.stringify({ ...DARK, ...o,
  xaxis:{...DARK.xaxis,...(o.xaxis||{})}, yaxis:{...DARK.yaxis,...(o.yaxis||{})} }));
const usd = (v,d=0) => (v<0?"-":"") + "$" + Math.abs(v).toLocaleString(undefined,{maximumFractionDigits:d});
const usdK = (v) => Math.abs(v)>=1000 ? (v<0?"-":"")+"$"+(Math.abs(v)/1000).toFixed(1)+"k" : usd(v,2);
const pct = (v,d=1) => v==null ? "–" : v.toFixed(d)+"%";
const signCls = (v) => v>0?"pos":(v<0?"neg":"");
function fillSelect(el, items, {value,label,blank}={}) {
  el.innerHTML = "";
  if (blank) el.appendChild(new Option(blank, ""));
  for (const it of items) {
    const v = value ? it[value] : it, l = label ? it[label] : it;
    el.appendChild(new Option(l, v));
  }
}
