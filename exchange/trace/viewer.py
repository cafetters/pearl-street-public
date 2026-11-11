"""
Run viewer (M1b): one self-contained HTML file per run, built from the run's summary.json and its trace.jsonl files.

Run:  python -m exchange.trace.viewer runs/m1      -> runs/m1/viewer.html

Reads only runs/<run>/summary.json and runs/<run>/*/trace.jsonl. Net load comes from summary.json's `series`
block (written by sim/run_m1.py); without it the chart is left out. The HTML has no network requests.
"""
import json, sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ISO = "%Y-%m-%dT%H:%M:%SZ"
TIMELINE_TYPES = ["post_need", "offer", "revise", "award", "settle", "reject", "call_nominations",
                  "nominate", "nomination_aggregate", "imbalance", "peak_price", "peak_settle"]


def hours(window: dict) -> float:
    t0, t1 = (datetime.strptime(window[k], ISO) for k in ("start_utc", "end_utc"))
    return (t1 - t0).total_seconds() / 3600


def read_trace(path: Path) -> tuple[dict, list[dict]]:
    lines = [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]
    return lines[0], lines[1:]


def r1(x):
    return None if x is None else round(float(x), 1)


def variant_data(header: dict, events: list[dict]) -> dict:
    """Compact timeline records, nomination counts and a per-agent rollup for one variant."""
    op = header.get("exchange", {}).get("operator_id") or header.get("config", {}).get("operator_id", "GRID_OP")
    ev = {t: [] for t in TIMELINE_TYPES if t != "nominate"}
    noms_per_step = Counter()
    needs, agents = {}, {}

    def agent(aid):
        return agents.setdefault(aid, {"id": aid, "max_kw": None, "offers": 0, "revises": 0, "awards": 0,
                                       "awarded_kwh": 0.0, "delivered_kwh": 0.0, "credits_usd": 0.0,
                                       "charges_usd": 0.0, "imbalance_usd": 0.0, "imbalance_kwh": 0.0,
                                       "nominations": 0, "rejects": 0})

    for e in events:
        t, typ, p, frm, to = e["t_utc"], e["msg_type"], e["payload"], e["from"], e["to"]
        if typ == "telemetry":
            agent(p["asset_id"].split("/")[0])
            continue
        if typ == "register":
            agent(frm)["max_kw"] = p["mandate"]["max_kw"]
        elif typ == "post_need" and frm == op:
            needs[e["msg_id"]] = p
            ev["post_need"].append({"t": t, "id": e["msg_id"], "round": p.get("round", 1), "kw": r1(p["kw_needed"]),
                                    "w": [p["window"]["start_utc"], p["window"]["end_utc"]], "gate": p["gate_utc"]})
        elif typ == "offer" and to == "exchange":
            agent(frm)["offers"] += 1
            ev["offer"].append({"t": t, "a": frm, "need": p["need_id"], "kw": r1(p["kw"]), "floor": p["floor_usd_mwh"]})
        elif typ == "revise" and to == "exchange":
            agent(frm)["revises"] += 1
            ev["revise"].append({"t": t, "a": frm, "offer": p["offer_id"], "withdraw": bool(p.get("withdraw")),
                                 "kw": r1(p.get("kw")), "floor": p.get("floor_usd_mwh")})
        elif typ == "award":
            a = agent(to[0])
            a["awards"] += 1
            a["awarded_kwh"] += p["accredited_kw"] * hours(p["window"])
            ev["award"].append({"t": t, "a": to[0], "need": p["need_id"], "kw": r1(p["accredited_kw"]),
                                "price": p["price_usd_mwh"]})
        elif typ == "settle":
            a = agent(to[0])
            need = needs.get(p["need_id"])
            h = hours(need["window"]) if need else 0.0
            a["delivered_kwh"] += p["delivered_kw"] * h
            a["credits_usd"] += p["credit_usd"]
            a["charges_usd"] += p["charge_usd"]
            ev["settle"].append({"t": t, "a": to[0], "need": p["need_id"], "delivered": r1(p["delivered_kw"]),
                                 "credit": round(p["credit_usd"], 2), "charge": round(p["charge_usd"], 2)})
        elif typ == "reject":
            if to != op:
                agent(to)["rejects"] += 1
            ev["reject"].append({"t": t, "a": to, "code": p["code"], "detail": p["detail"]})
        elif typ == "call_nominations" and frm == op:
            ev["call_nominations"].append({"t": t, "id": e["msg_id"], "round": p.get("round"), "binding": p["binding"],
                                           "deadline": p["deadline_utc"],
                                           "w": [p["window"]["start_utc"], p["window"]["end_utc"]]})
        elif typ == "nominate" and to == "exchange":
            agent(frm)["nominations"] += 1
            noms_per_step[t] += 1
        elif typ == "nomination_aggregate":
            ev["nomination_aggregate"].append({"t": t, "call": p["call_id"], "binding": p.get("binding", False),
                                               "n": p["n_agents"], "start": p["window"]["start_utc"],
                                               "kw": [r1(v) for v in p["kw"]]})
        elif typ == "imbalance":
            a = agent(to)
            a["imbalance_usd"] += p["charge_usd"]
            a["imbalance_kwh"] += p["deviation_kwh"]
            ev["imbalance"].append({"t": t, "a": to, "dev": r1(p["deviation_kwh"]),
                                    "raw": r1(p.get("raw_deviation_kwh")), "charge": round(p["charge_usd"], 2)})
        elif typ == "peak_price" and frm == "exchange":      # D53: one broadcast per price round; arrays not embedded
            priced = [v for v in p["price_usd_mwh"] if v > 0]
            ev["peak_price"].append({"t": t, "call": p["call_id"], "round": p["round"], "final": bool(p["final"]),
                                     "rho": p["rho"], "max": round(max(priced, default=0.0), 2), "n": len(priced)})
        elif typ == "peak_settle" and frm == "exchange":
            ev["peak_settle"].append({"t": t, "a": to, "steps": p["steps"], "charge": round(p["charge_usd"], 2),
                                      "refund": round(p["refund_usd"], 2), "net": round(p["net_usd"], 2)})
    rows = sorted(agents.values(), key=lambda a: a["id"])
    for a in rows:
        for k in ("awarded_kwh", "delivered_kwh", "imbalance_kwh"):
            a[k] = round(a[k], 1)
        for k in ("credits_usd", "charges_usd", "imbalance_usd"):
            a[k] = round(a[k], 2)
    return {"events": ev, "nominations_per_step": dict(noms_per_step), "agents": rows,
            "counts": {t: (sum(noms_per_step.values()) if t == "nominate" else len(ev[t])) for t in TIMELINE_TYPES}}


def summary_block(summary: dict, header: dict) -> tuple[str, dict | None]:
    """Where this variant's numbers sit in summary.json, and the block itself."""
    variant, h = header.get("variant"), header.get("headroom_mw")
    if variant == "selfish":
        return "selfish", summary.get("selfish")
    key = f"{h:g}"
    main_rounds = summary.get("rounds")
    same_rounds = main_rounds is None or header.get("rounds") in (None, main_rounds)     # M1 summaries have no rounds
    if same_rounds and key in summary.get("exchange_by_headroom_mw", {}):
        return f"exchange_by_headroom_mw.{key}", summary["exchange_by_headroom_mw"][key]
    sweep = summary.get("rounds_sweep") or {}
    if sweep and f"{sweep.get('headroom_mw'):g}" == key and str(header.get("rounds")) in sweep.get("by_rounds", {}):
        return f"rounds_sweep.by_rounds.{header['rounds']}", sweep["by_rounds"][str(header["rounds"])]
    return "", None


def build(run_dir: str | Path) -> Path:
    run_dir = Path(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    variants, skipped = {}, []
    for tp in sorted(run_dir.glob("*/trace.jsonl")):
        header, events = read_trace(tp)
        path, block = summary_block(summary, header)
        if not path:            # a trace left over from an earlier run that this summary.json does not cover
            skipped.append(tp.parent.name)
            continue
        variants[header.get("variant", tp.parent.name)] = {
            "header": {k: header.get(k) for k in ("run_id", "variant", "headroom_mw", "rounds", "seed",
                                                  "git_commit", "run_at_utc")},
            "summary_path": path, "summary": block, **variant_data(header, events)}
    order = sorted(variants, key=lambda v: (v != "selfish", variants[v]["header"].get("headroom_mw") or 0,
                                            str(variants[v]["header"].get("rounds") or "")))
    data = {"run": run_dir.name, "summary_header": summary.get("header"), "m0": summary.get("m0"),
            "recorded": summary.get("recorded"), "pass_test": summary.get("m1_1_pass_test"),
            "pass_all_headrooms": summary.get("pass_all_headrooms"), "series": summary.get("series"),
            "order": order, "variants": variants, "skipped_traces": skipped}
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.replace("__TITLE__", f"Run {run_dir.name}").replace("/*__DATA__*/null", blob)
    out = run_dir / "viewer.html"
    out.write_text(html, encoding="utf-8")
    return out


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#fbfaf8;--panel:#ffffff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e3e1dc;--accent:#2f6fd6;
--rec:#9a9aa0;--self:#d9822b;--exch:#2f6fd6;--ceil:#2e9e6b;--win:rgba(47,111,214,.10);--cursor:#c2185b;
--ok:#2e9e6b;--bad:#c0392b;--chip:#f1efea}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--panel:#1d1d20;--ink:#ececef;--muted:#9a9aa3;--line:#33333a;
--accent:#6aa0ff;--rec:#8a8a92;--self:#f0a050;--exch:#6aa0ff;--ceil:#4cc38a;--win:rgba(106,160,255,.13);
--cursor:#ff5c93;--chip:#26262b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1200px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0 2px}
h2{font-size:15px;margin:0 0 8px}
.sub{color:var(--muted);font-size:13px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
select,button,input[type=range]{font:inherit;color:inherit}
select,button{background:var(--chip);border:1px solid var(--line);border-radius:6px;padding:5px 9px;cursor:pointer}
button:hover{border-color:var(--accent)}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.stat{background:var(--chip);border-radius:8px;padding:8px 10px}
.stat .k{color:var(--muted);font-size:12px}.stat .v{font-size:16px;font-variant-numeric:tabular-nums}
.ok{color:var(--ok)}.bad{color:var(--bad)}
svg{display:block;width:100%;height:auto}
.legend span{display:inline-flex;align-items:center;gap:5px;margin-right:12px;font-size:12px;color:var(--muted)}
.legend i{display:inline-block;width:18px;height:3px;border-radius:2px}
.lanes text{fill:var(--muted);font-size:11px}
#events{max-height:340px;overflow:auto;font-size:13px}
#events .grp{margin:6px 0 2px;font-weight:600}
#events .e{padding:2px 0;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}
.tbl{overflow:auto}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{cursor:pointer;color:var(--muted);font-weight:600;position:sticky;top:0;background:var(--panel)}
.note{color:var(--muted);font-size:13px}
input[type=range]{flex:1;min-width:160px}
#readout{font-size:12px;color:var(--muted);min-height:18px}
</style>
</head>
<body>
<main>
<h1 id="title"></h1>
<div class="sub" id="meta"></div>

<div class="card">
  <div class="row"><label for="variant">Variant</label><select id="variant"></select>
    <span class="sub" id="vpath"></span></div>
  <div class="stats" id="stats" style="margin-top:10px"></div>
</div>

<div class="card">
  <h2>Net load</h2>
  <div class="legend" id="legend"></div>
  <div id="chartwrap"></div>
  <div id="readout"></div>
</div>

<div class="card">
  <h2>Timeline</h2>
  <div class="row">
    <button id="prev" title="Previous step">&#9664;</button>
    <button id="play">Play</button>
    <button id="next" title="Next step">&#9654;</button>
    <button id="nextev" title="Next step with events">Next event</button>
    <input type="range" id="slider" min="0" value="0">
    <span id="when" class="sub"></span>
  </div>
  <div id="laneswrap" style="margin-top:8px"></div>
  <div id="events"></div>
</div>

<div class="card">
  <h2>Agents</h2>
  <div class="tbl"><table id="agents"></table></div>
</div>
<p class="note">Built from summary.json and trace.jsonl only. Times are Central (UTC on hover).<span id="skipped"></span></p>
</main>
<script>
const DATA = /*__DATA__*/null;
const S = DATA.series;
const T = S ? S.t_utc.length : 0;
let V = DATA.order.find(v => v !== "selfish") || DATA.order[0];
let k = 0, timer = null;
const $ = id => document.getElementById(id);
const fmt = (x, d = 1) => x === null || x === undefined ? "–" : (typeof x === "number" ? x.toLocaleString(undefined, {maximumFractionDigits: d, minimumFractionDigits: 0}) : String(x));
const central = iso => { if (!S) return iso; const i = S.t_utc.indexOf(iso); return i >= 0 ? S.t_central[i] : iso; };
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

function init() {
  $("title").textContent = "Pearl Street run: " + DATA.run;
  const h = DATA.summary_header || {};
  $("meta").textContent = `commit ${(h.git_commit || "?").slice(0, 7)} · run ${h.run_at_utc || "?"} · seed ${h.seed ?? "–"}`;
  const sel = $("variant");
  for (const v of DATA.order) { const o = document.createElement("option"); o.value = v; o.textContent = v; sel.appendChild(o); }
  sel.value = V;
  if (DATA.skipped_traces && DATA.skipped_traces.length) $("skipped").textContent = " Skipped traces not covered by this summary.json (left over from an earlier run): " + DATA.skipped_traces.join(", ") + ".";
  sel.onchange = () => { V = sel.value; render(); };
  $("slider").max = Math.max(T - 1, 0);
  $("slider").oninput = e => { k = +e.target.value; renderStep(); };
  $("prev").onclick = () => { k = Math.max(0, k - 1); renderStep(); };
  $("next").onclick = () => { k = Math.min(T - 1, k + 1); renderStep(); };
  $("nextev").onclick = () => { const ks = eventSteps(); const n = ks.find(x => x > k); if (n !== undefined) { k = n; renderStep(); } };
  $("play").onclick = () => {
    if (timer) { clearInterval(timer); timer = null; $("play").textContent = "Play"; return; }
    $("play").textContent = "Pause";
    timer = setInterval(() => { if (k >= T - 1) { clearInterval(timer); timer = null; $("play").textContent = "Play"; return; } k++; renderStep(); }, 120);
  };
  render();
}

function stat(kname, v, cls) { return `<div class="stat"><div class="k">${esc(kname)}</div><div class="v ${cls || ""}">${v}</div></div>`; }

function renderStats() {
  const vd = DATA.variants[V], s = vd.summary;
  $("vpath").textContent = vd.summary_path ? "summary.json → " + vd.summary_path : "not in summary.json";
  if (!s) { $("stats").innerHTML = stat("summary", "none for this variant"); return; }
  const out = [];
  if (s.peak_mw !== undefined) out.push(stat("Peak", fmt(s.peak_mw) + " MW"));
  if (s.peak_central) out.push(stat("Peak at (Central)", esc(s.peak_central.slice(5, 16))));
  if (s.peak_vs_selfish_mw !== undefined) out.push(stat("vs selfish", (s.peak_vs_selfish_mw > 0 ? "+" : "") + fmt(s.peak_vs_selfish_mw) + " MW"));
  if (s.share_of_m0_ceiling !== undefined && s.share_of_m0_ceiling !== null) out.push(stat("Share of M0 ceiling", fmt(100 * s.share_of_m0_ceiling) + "%"));
  if (s.unfilled_need_kwh !== undefined) out.push(stat("Unfilled need", fmt(s.unfilled_need_kwh / 1000, 0) + " MWh"));
  if (s.rebound) out.push(stat("Peak outside windows", fmt(s.rebound.peak_outside_windows_mw) + " MW"));
  if (s.rebound) out.push(stat("Peak moved outside", s.rebound.peak_moved_outside_windows ? "yes" : "no", s.rebound.peak_moved_outside_windows ? "bad" : "ok"));
  if (s.pass !== undefined) out.push(stat("Below selfish", s.pass ? "yes" : "no", s.pass ? "ok" : "bad"));
  if (s.pass_peak_inside_windows !== undefined) out.push(stat("M1.1 pass rule", s.pass_peak_inside_windows ? "yes" : "no", s.pass_peak_inside_windows ? "ok" : "bad"));
  if (s.imbalance_honest) out.push(stat("Imbalance, honest", "$" + fmt(s.imbalance_honest.charge_usd, 0) + " · " + fmt(s.imbalance_honest.deviation_beyond_band_kwh / 1000) + " MWh"));
  if (s.binding_forecast_vs_actual || s.binding_forecast_gap) { const g = s.binding_forecast_vs_actual || s.binding_forecast_gap; out.push(stat("Binding forecast gap", "mean " + fmt(g.mean_abs_mw, 2) + " · max " + fmt(g.max_abs_mw) + " MW")); }
  if (s.credits_usd !== undefined) out.push(stat("Credits / charges", "$" + fmt(s.credits_usd, 0) + " / $" + fmt(s.charges_usd, 0)));
  if (s.net_harm) { const nh = s.net_harm; out.push(stat("Net harm (vs acting alone)", "$" + fmt(nh.total_net_harm_usd, 0) + " · " + fmt(nh.agents_harmed, 0) + " agents", nh.pass ? "ok" : "bad")); }
  else if (s.agents_harmed !== undefined) out.push(stat("Net harm (vs acting alone)", "$" + fmt(s.total_net_harm_usd, 0) + " · " + fmt(s.agents_harmed, 0) + " agents", s.agents_harmed ? "bad" : "ok"));
  else if (s.agents_worse_off_than_selfish !== undefined) out.push(stat("Agents worse off than selfish", fmt(s.agents_worse_off_than_selfish, 0)));
  if (s.energy_revenue_usd !== undefined) out.push(stat("Energy revenue", "$" + fmt(s.energy_revenue_usd, 0)));
  $("stats").innerHTML = out.join("");
}

const W = 1000, H = 300, PL = 56, PR = 12, PT = 10, PB = 28;
let yMin = 0, yMax = 1;
const xs = i => PL + (W - PL - PR) * (T > 1 ? i / (T - 1) : 0);
const ys = v => PT + (H - PT - PB) * (1 - (v - yMin) / (yMax - yMin));

function lineFor(arr) { return arr.map((v, i) => (i ? "L" : "M") + xs(i).toFixed(1) + " " + ys(v / 1000).toFixed(1)).join(""); }

function renderChart() {
  const legend = $("legend"), wrap = $("chartwrap");
  if (!S) { legend.innerHTML = ""; wrap.innerHTML = '<p class="note">This summary.json has no net-load series (written by sim/run_m1.py since M1b). Rerun the run to add it.</p>'; return; }
  const lines = [["recorded_kw", "Recorded", "var(--rec)", ""], ["selfish_kw", "Selfish", "var(--self)", ""]];
  if (V !== "selfish" && S[V + "_kw"]) lines.push([V + "_kw", "Exchange", "var(--exch)", ""]);
  if (S.m0_ceiling_kw) lines.push(["m0_ceiling_kw", "M0 ceiling", "var(--ceil)", "6 4"]);
  const all = lines.flatMap(l => S[l[0]]).map(v => v / 1000);
  yMin = Math.floor(Math.min(...all) / 50) * 50; yMax = Math.ceil(Math.max(...all) / 50) * 50;
  let g = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Net load in MW over the run">`;
  const win = S[V + "_in_window"];
  if (win) for (let i = 0; i < T; i++) if (win[i]) g += `<rect x="${xs(i) - (W - PL - PR) / (2 * (T - 1))}" y="${PT}" width="${(W - PL - PR) / (T - 1)}" height="${H - PT - PB}" fill="var(--win)"/>`;
  for (let v = yMin; v <= yMax; v += Math.max(50, Math.round((yMax - yMin) / 5 / 50) * 50)) {
    g += `<line x1="${PL}" x2="${W - PR}" y1="${ys(v)}" y2="${ys(v)}" stroke="var(--line)"/><text x="${PL - 6}" y="${ys(v) + 4}" text-anchor="end" font-size="11" fill="var(--muted)">${v.toLocaleString()}</text>`;
  }
  for (let i = 0; i < T; i += 24) g += `<text x="${xs(i)}" y="${H - 8}" text-anchor="middle" font-size="11" fill="var(--muted)">${esc(S.t_central[i].slice(5, 16))}</text>`;
  g += `<text x="12" y="${PT + 10}" font-size="11" fill="var(--muted)">MW</text>`;
  for (const [key, , color, dash] of lines) g += `<path d="${lineFor(S[key])}" fill="none" stroke="${color}" stroke-width="${key === V + "_kw" ? 2.2 : 1.6}" ${dash ? `stroke-dasharray="${dash}"` : ""}/>`;
  g += `<line id="cursor" y1="${PT}" y2="${H - PB}" stroke="var(--cursor)" stroke-width="1.5"/>`;
  g += `<rect id="hit" x="${PL}" y="${PT}" width="${W - PL - PR}" height="${H - PT - PB}" fill="transparent"/></svg>`;
  wrap.innerHTML = g;
  legend.innerHTML = lines.map(([, name, color, dash]) => `<span><i style="background:${dash ? "repeating-linear-gradient(90deg," + color + " 0 6px,transparent 6px 10px)" : color}"></i>${name}</span>`).join("") + (win ? '<span><i style="background:var(--win);height:10px"></i>Need windows</span>' : "");
  const svg = wrap.querySelector("svg");
  const pick = ev => { const r = svg.getBoundingClientRect(); const x = (ev.clientX - r.left) / r.width * W; return Math.max(0, Math.min(T - 1, Math.round((x - PL) / (W - PL - PR) * (T - 1)))); };
  $("hit").onmousemove = ev => { const i = pick(ev); $("readout").textContent = `${S.t_central[i]} (UTC ${S.t_utc[i].slice(11, 16)}): ` + lines.map(([key, name]) => `${name} ${fmt(S[key][i] / 1000)} MW`).join(" · "); };
  $("hit").onclick = ev => { k = pick(ev); renderStep(); };
}

const LANES = [["post_need", "Needs, round 1"], ["post_need2", "Needs, later rounds"], ["offer", "Offers"], ["revise", "Revisions"],
  ["award", "Awards"], ["settle", "Settlements"], ["call_nominations", "Nomination calls"], ["nominate", "Nominations"],
  ["nomination_aggregate", "Zone aggregates"], ["peak_price", "Peak price rounds"], ["peak_settle", "Peak-price settlement"],
  ["imbalance", "Imbalance charges"], ["reject", "Rejects"]];

function stepsOf(lane) {
  const vd = DATA.variants[V], ev = vd.events, out = new Map();
  const add = t => { const i = S ? S.t_utc.indexOf(t) : -1; const j = i >= 0 ? i : T - 1; out.set(j, (out.get(j) || 0) + 1); };
  if (lane === "nominate") { for (const [t, n] of Object.entries(vd.nominations_per_step)) { const i = S ? S.t_utc.indexOf(t) : -1; out.set(i >= 0 ? i : T - 1, n); } return out; }
  if (lane === "post_need") { ev.post_need.filter(e => e.round === 1).forEach(e => add(e.t)); return out; }
  if (lane === "post_need2") { ev.post_need.filter(e => e.round > 1).forEach(e => add(e.t)); return out; }
  (ev[lane] || []).forEach(e => add(e.t));
  return out;
}

function eventSteps() { const s = new Set(); for (const [lane] of LANES) for (const i of stepsOf(lane).keys()) s.add(i); return [...s].sort((a, b) => a - b); }

function renderLanes() {
  const rowH = 18, top = 4, LW = 150, w = 1000;
  const used = LANES.filter(([lane]) => stepsOf(lane).size);
  let g = `<svg class="lanes" viewBox="0 0 ${w} ${top + rowH * Math.max(used.length, 1) + 4}">`;
  const xl = i => LW + (w - LW - 12) * (T > 1 ? i / (T - 1) : 0);
  used.forEach(([lane, name], r) => {
    const y = top + r * rowH;
    g += `<text x="0" y="${y + 12}">${name}</text><line x1="${LW}" x2="${w - 12}" y1="${y + 8}" y2="${y + 8}" stroke="var(--line)"/>`;
    for (const [i, n] of stepsOf(lane)) g += `<rect data-k="${i}" x="${xl(i) - 2}" y="${y + 2}" width="4" height="12" rx="1" fill="var(--accent)" opacity="${Math.min(1, 0.35 + Math.log10(n + 1) / 2)}"><title>${n} at ${esc(S ? S.t_central[i] : i)}</title></rect>`;
  });
  if (!used.length) g += `<text x="0" y="16">No exchange traffic in this variant.</text>`;
  g += `<line id="lcursor" y1="0" y2="${top + rowH * Math.max(used.length, 1)}" stroke="var(--cursor)" stroke-width="1.5"/></svg>`;
  $("laneswrap").innerHTML = g;
  $("laneswrap").querySelectorAll("rect[data-k]").forEach(r => r.onclick = () => { k = +r.dataset.k; renderStep(); });
  $("laneswrap").dataset.lw = LW;
}

function line(e, typ) {
  switch (typ) {
    case "post_need": return `round ${e.round} · ${fmt(e.kw / 1000, 2)} MW · ${central(e.w[0]).slice(5)} to ${central(e.w[1]).slice(11) || e.w[1]} · gate ${central(e.gate).slice(11)}`;
    case "offer": return `${esc(e.a)} · ${fmt(e.kw, 0)} kW at $${fmt(e.floor, 2)}/MWh · need ${esc(e.need)}`;
    case "revise": return `${esc(e.a)} · ${e.withdraw ? "withdraw" : fmt(e.kw, 0) + " kW at $" + fmt(e.floor, 2)} · offer ${esc(e.offer)}`;
    case "award": return `${esc(e.a)} · ${fmt(e.kw, 0)} kW at $${fmt(e.price, 2)}/MWh · need ${esc(e.need)}`;
    case "settle": return `${esc(e.a)} · delivered ${fmt(e.delivered, 0)} kW · credit $${fmt(e.credit, 2)} · charge $${fmt(e.charge, 2)}`;
    case "reject": return `${esc(e.a)} · ${esc(e.code)}: ${esc(e.detail)}`;
    case "call_nominations": return `round ${e.round}${e.binding ? " (binding)" : ""} · deadline ${central(e.deadline).slice(5)} · window from ${central(e.w[0]).slice(5)}`;
    case "nomination_aggregate": return `${e.binding ? "binding · " : ""}${e.n} agents · zone net ${fmt(Math.min(...e.kw) / 1000)} to ${fmt(Math.max(...e.kw) / 1000)} MW`;
    case "imbalance": return `${esc(e.a)} · ${fmt(e.dev, 1)} kWh beyond band (raw ${fmt(e.raw, 1)}) · $${fmt(e.charge, 2)}`;
    case "peak_price": return `call ${esc(e.call)} · round ${e.round}${e.final ? " (final)" : ""} · max $${fmt(e.max, 2)}/MWh over ${e.n} priced steps · rho ${fmt(e.rho, 4)}`;
    case "peak_settle": return `${esc(e.a)} · ${e.steps} priced steps · charge $${fmt(e.charge, 2)} · refund $${fmt(e.refund, 2)} · net $${fmt(e.net, 2)}`;
  }
  return "";
}

function renderEvents() {
  const vd = DATA.variants[V], t = S ? S.t_utc[k] : null, box = $("events");
  let html = "";
  const groups = [["post_need", "Needs posted"], ["call_nominations", "Nomination calls"], ["offer", "Offers"], ["revise", "Revisions"],
    ["award", "Awards"], ["settle", "Settlements"], ["nomination_aggregate", "Zone aggregates (operator)"], ["peak_price", "Peak price rounds"],
    ["peak_settle", "Peak-price settlement"], ["imbalance", "Imbalance charges"], ["reject", "Rejects"]];
  for (const [typ, name] of groups) {
    const list = (vd.events[typ] || []).filter(e => e.t === t || (k === T - 1 && S && S.t_utc.indexOf(e.t) < 0));
    if (!list.length) continue;
    html += `<div class="grp">${name} (${list.length})</div>` + list.slice(0, 150).map(e => `<div class="e">${line(e, typ)}</div>`).join("");
    if (list.length > 150) html += `<div class="e note">… ${list.length - 150} more</div>`;
  }
  const n = t ? vd.nominations_per_step[t] : 0;
  if (n) html = `<div class="grp">Nominations sent: ${n}</div>` + html;
  box.innerHTML = html || '<p class="note">No exchange events at this step.</p>';
}

function renderStep() {
  $("slider").value = k;
  $("when").textContent = S ? `${S.t_central[k]} Central · step ${k + 1}/${T}` : "";
  const c = document.getElementById("cursor"); if (c) { c.setAttribute("x1", xs(k)); c.setAttribute("x2", xs(k)); }
  const lc = document.getElementById("lcursor");
  if (lc) { const LW = 150, x = LW + (1000 - LW - 12) * (T > 1 ? k / (T - 1) : 0); lc.setAttribute("x1", x); lc.setAttribute("x2", x); }
  renderEvents();
}

const COLS = [["id", "Agent", 0], ["max_kw", "Max kW", 0], ["offers", "Offers", 0], ["revises", "Revisions", 0], ["awards", "Awards", 0],
  ["awarded_kwh", "Awarded kWh", 0], ["delivered_kwh", "Delivered kWh", 0], ["credits_usd", "Credits $", 2], ["charges_usd", "Charges $", 2],
  ["nominations", "Nominations", 0], ["imbalance_kwh", "Imbalance kWh", 1], ["imbalance_usd", "Imbalance $", 2], ["rejects", "Rejects", 0],
  ["net_harm_usd", "Net harm $", 2]];
let sortKey = "awarded_kwh", sortDir = -1;

function renderAgents() {
  const nh = (DATA.variants[V].summary || {}).net_harm;
  const harm = nh ? nh.by_agent_usd : null;
  const rows = DATA.variants[V].agents.map(a => ({...a, net_harm_usd: harm ? (harm[a.id] ?? null) : null})).sort((a, b) => { const x = a[sortKey], y = b[sortKey]; return (x > y ? 1 : x < y ? -1 : 0) * sortDir; });
  let h = "<thead><tr>" + COLS.map(([key, name]) => `<th data-k="${key}">${name}${key === sortKey ? (sortDir > 0 ? " ▲" : " ▼") : ""}</th>`).join("") + "</tr></thead><tbody>";
  const tol = r => harm && r.net_harm_usd > 0 ? r.net_harm_usd : 0;
  for (const r of rows) h += "<tr>" + COLS.map(([key, , d]) => `<td${key === "net_harm_usd" && tol(r) > 0 ? ' class="bad"' : ""}>${key === "id" ? esc(r[key]) : fmt(r[key], d)}</td>`).join("") + "</tr>";
  $("agents").innerHTML = h + "</tbody>";
  $("agents").querySelectorAll("th").forEach(th => th.onclick = () => { const key = th.dataset.k; sortDir = key === sortKey ? -sortDir : -1; sortKey = key; renderAgents(); });
}

function render() { renderStats(); renderChart(); renderLanes(); renderAgents(); renderStep(); }
init();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    for d in sys.argv[1:] or ["runs/m1"]:
        print(build(d))
