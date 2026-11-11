"""
Head-to-head report: arm A (F1+F3) vs. arm B (A + published zone room, D50) under D49, and arm C (price mode) vs. arm A
under D53, on Jan 2-3 and Jan 12-13 at 100 and 150 MW. Reads runs/h2h_<arm>_<window>_h<MW>/ (summary.json and the
traces) and runs/h2h/feasibility.json (bench.h2h_feasibility), and writes runs/h2h/table.md.

Run:  python -m bench.h2h_report
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "runs" / "h2h"
ARMS = {"a": "A (F1+F3)", "b": "B (A + zone room)", "c": "C (price mode)"}
WINDOWS = {"jan02": "Jan 2-3", "jan12": "Jan 12-13"}
HEADROOMS = (100, 150)
QUIET = {"telemetry", "tick"}


def _t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _trace(path):
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    return lines[0], lines[1:]


def _participant_load(events, iso):
    """Participants' metered net load per step (kW), from telemetry."""
    pos = {t: i for i, t in enumerate(iso)}
    load = np.zeros(len(iso))
    for e in events:
        if e["msg_type"] == "telemetry" and "net_kw" in e["payload"]:
            load[pos[e["t_utc"]]] -= e["payload"]["net_kw"]
    return load


def _arr(start, vals, iso):
    pos = {t: i for i, t in enumerate(iso)}
    a = np.full(len(iso), np.nan)
    i = pos[start]
    v = np.asarray([np.nan if x is None else x for x in vals], float)[:len(iso) - i]
    a[i:i + len(v)] = v
    return a


def over_target(run_dir, h, agg_band_kw):
    """Steps outside need windows (from the first gate) where participants' metered load is over the zone target:
    strict count, count beyond the aggregate band (D51), and the excess the exchange caused (over both the target and
    the selfish run's load at that step). Also the late-reveal diagnostic: the zone's nominated load at those steps
    in the last non-binding round vs. the last binding call."""
    _, ex = _trace(run_dir / f"exchange_h{h}_rinfo" / "trace.jsonl")
    _, sf = _trace(run_dir / "selfish" / "trace.jsonl")
    t0 = min(e["t_utc"] for e in ex if e["msg_type"] == "telemetry")
    t1 = max(e["t_utc"] for e in ex if e["msg_type"] == "telemetry")
    n = int((_t(t1) - _t(t0)) / timedelta(minutes=15)) + 1
    iso = [(_t(t0) + i * timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(n)]
    pos = {t: i for i, t in enumerate(iso)}
    pe, ps = _participant_load(ex, iso), _participant_load(sf, iso)
    target = np.full(n, np.nan)
    for e in ex:
        if e["msg_type"] == "zone_target":
            v = _arr(e["payload"]["window"]["start_utc"], e["payload"]["target_kw"], iso)
            target = np.where(np.isnan(v), target, v)
    needs = [e["payload"] for e in ex if e["msg_type"] == "post_need" and e["to"] == "exchange"]
    inw = np.zeros(n, bool)
    for p in needs:
        inw[pos[p["window"]["start_utc"]]:pos.get(p["window"]["end_utc"], n)] = True
    calls = [e["payload"] for e in ex if e["msg_type"] == "call_nominations" and e["to"] == "exchange"]
    if needs:
        first = min(pos[p["gate_utc"]] for p in needs)
    else:       # price mode (no needs): the operator's first gate is its first call's deadline
        first = min((pos[c["deadline_utc"]] for c in calls if c["deadline_utc"] in pos), default=n)
    steps = [i for i in range(first, n) if not inw[i] and not np.isnan(target[i]) and pe[i] - target[i] > 1e-3]
    caused = [max(min(pe[i] - target[i], pe[i] - ps[i]), 0.0) for i in steps]
    aggs = [e for e in ex if e["msg_type"] == "nomination_aggregate"]
    last_nb = [a for a in aggs if not a["payload"]["binding"]]
    last_b = [a for a in aggs if a["payload"]["binding"]]
    reveal = None
    if steps and last_nb and last_b:
        nb = -_arr(last_nb[-1]["payload"]["window"]["start_utc"], last_nb[-1]["payload"]["kw"], iso)
        bd = -_arr(last_b[-1]["payload"]["window"]["start_utc"], last_b[-1]["payload"]["kw"], iso)
        k = max(steps, key=lambda i: pe[i] - target[i])
        reveal = {"step_utc": iso[k], "last_residual_round_mw": None if np.isnan(nb[k]) else nb[k] / 1e3,
                  "last_binding_mw": None if np.isnan(bd[k]) else bd[k] / 1e3, "target_mw": target[k] / 1e3,
                  "residual_closed": last_nb[-1]["t_utc"], "binding_closed": last_b[-1]["t_utc"]}
    return {"strict": len(steps), "beyond_band": sum(pe[i] - target[i] > agg_band_kw + 1e-3 for i in steps),
            "max_excess_mw": max((pe[i] - target[i] for i in steps), default=0.0) / 1e3,
            "caused_steps": sum(c > 1e-3 for c in caused), "caused_max_mw": max(caused, default=0.0) / 1e3,
            "reveal": reveal}


def stopped(arm, win, h):
    """The referee's message if the run was stopped by `referee: abort` (from runs/<run_name>.log), else None."""
    log = REPO / "runs" / f"h2h_{arm}_{win}_h{h}.log"
    if not log.exists():
        return None
    msgs = [l.split("RuntimeError: ", 1)[1] for l in log.read_text(encoding="utf-8").splitlines()
            if "RuntimeError: award not deliverable" in l]
    return msgs[-1] if msgs else None


def row(arm, win, h):
    run_dir = REPO / "runs" / f"h2h_{arm}_{win}_h{h}"
    path = run_dir / "summary.json"
    if not path.exists():
        msg = stopped(arm, win, h)
        return None if msg is None else {"arm": arm, "window": win, "h": h, "stopped": msg}
    s = json.loads(path.read_text())
    e = s["exchange_by_headroom_mw"][str(h)]
    nh = e["net_harm"]
    tc = e["target_check_metered"]
    cut = s["selfish"]["peak_mw"] - e["peak_mw"]
    days = {d: v for d, v in e["messages_per_day"].items()}
    n_days = len(s["slice"]["local_days"]) if "local_days" in s.get("slice", {}) else 2
    total = sum(sum(v.values()) for v in days.values())
    market = sum(c for v in days.values() for t, c in v.items() if t not in QUIET)
    ot = over_target(run_dir, h, tc["aggregate_band_mw"] * 1e3)
    return {
        "arm": arm, "window": win, "h": h,
        "peak_mw": e["peak_mw"], "selfish_mw": s["selfish"]["peak_mw"], "vs_selfish_mw": e["peak_vs_selfish_mw"],
        "share_m0": e["share_of_m0_ceiling"], "gap_share": e["share_of_selfish_to_ceiling_gap"],
        "harm_usd": nh["total_net_harm_usd"], "harmed": nh["agents_harmed"],
        "harm_components": {k: v for k, v in nh["components"]["harmed_agents"].items() if k != "total" and abs(v) > 0.005},
        "harm_per_mw_cut": nh["total_net_harm_usd"] / cut if cut > 1e-6 else None,
        "unfilled_mwh": e["unfilled_need_kwh"] / 1e3, "need_mwh": e["need_kwh"] / 1e3,
        "msgs_per_day": total / n_days, "market_msgs_per_day": market / n_days,
        "over": ot, "pass": e["m2a_pass"], "referee": (e.get("referee") or {}).get("failures"),
        "commit": s["header"].get("git_commit", "")[:7],
        "price": price_fields(e.get("price_mode")),
        "roster": s["header"].get("roster"),
        "roster_complete": bool((e.get("m2a_pass") or {}).get("roster_complete"))
                           and nh.get("missing_from_check", ["(not reported)"]) == [],
    }


def price_fields(pm):
    """D53 columns from a price-mode summary block, or None for arms A and B."""
    if not pm:
        return None
    return {"payout_usd": pm["operator_net_payout_usd"], "payout_per_mw_cut": pm.get("payout_per_mw_cut_usd"),
            "rounds": [g["rounds"] for g in pm["gates"]], "rounds_total": pm["rounds_total"],
            "rounds_at_cap": pm["rounds_at_cap"], "max_price": pm["max_peak_price_usd_mwh"],
            "final_price": pm["settled"]["max_usd_mwh"], "cap": pm["rounds_per_gate_cap"],
            "basis": pm.get("rounds_basis"),
            "converged": [g["converged"] for g in pm["gates"]],
            "final_excess_mw": [g["final_max_excess_mw"] for g in pm["gates"]]}


def load_feasibility():
    path = OUT / "feasibility.json"
    return json.loads(path.read_text())["cells"] if path.exists() else {}


def roster_problem(x, y):
    """Why a cell can't be judged on its rosters (CLAUDE.md: every check takes its roster from the run header and fails
    if any agent is missing), or None. Fails closed: a row without roster fields is incomplete."""
    for r in (x, y):
        if not r.get("roster_complete") or not r.get("roster"):
            return f"{ARMS[r['arm']].split(' ')[0]} roster incomplete"
    if set(x["roster"]) != set(y["roster"]):
        return "the two runs' rosters differ"
    return None


def decide_d53(rows, feas, tie_band_mw=None):
    """D53: C is adopted over A only if, in every feasible cell, it closes more of the selfish-to-ceiling gap than A
    without raising net harm per MW cut. Where A has no cut, the harm condition compares total net harm instead
    (agents over tolerance and $). A cell is feasible per bench.h2h_feasibility; infeasible cells are reported, not
    judged. A cell whose rosters are incomplete or differ is not a C win.
    tie_band_mw (D55, future comparisons only; None = D53 as pre-registered): peaks within the band are a tie on the
    gap, and the cell is judged on the harm condition alone."""
    inf = float("inf")
    cells, ok, judged = [], True, 0
    for win in WINDOWS:
        for h in HEADROOMS:
            name = f"{WINDOWS[win]} {h} MW"
            f = feas.get(f"h2h_c_{win}_h{h}")
            if f is None:
                cells.append(f"{name}: feasibility not checked; not judged")
                ok = False
                continue
            if not f["feasible"]:
                cells.append(f"{name}: infeasible target (max {f['max_short_mw']:.1f} MW short); not judged")
                continue
            judged += 1
            a, c = rows.get(("a", win, h)), rows.get(("c", win, h))
            if a is None or c is None or "stopped" in a or "stopped" in c:
                cells.append(f"{name}: missing or stopped run; C not shown to win")
                ok = False
                continue
            problem = roster_problem(a, c)
            if problem:
                cells.append(f"{name}: {problem}; C not shown to win")
                ok = False
                continue
            tie = tie_band_mw is not None and abs(c["peak_mw"] - a["peak_mw"]) <= tie_band_mw
            better_gap = tie or c["gap_share"] > a["gap_share"]
            if a["harm_per_mw_cut"] is None:          # A cut nothing: compare total net harm
                harm_ok = c["harmed"] <= a["harmed"] and c["harm_usd"] <= a["harm_usd"] + 1e-6
                harm_txt = (f"A has no cut, so total net harm: C {c['harmed']} agents ${c['harm_usd']:,.0f} vs A "
                            f"{a['harmed']} agents ${a['harm_usd']:,.0f} ({'not higher' if harm_ok else 'higher'})")
            else:
                hpa = a["harm_per_mw_cut"]
                hpc = inf if c["harm_per_mw_cut"] is None else c["harm_per_mw_cut"]
                harm_ok = hpc <= hpa
                harm_txt = (f"harm/MW cut C {fmt(c['harm_per_mw_cut'], '${:,.2f}')} vs A {fmt(hpa, '${:,.2f}')} "
                            f"({'not higher' if harm_ok else 'higher'})")
            gap_txt = (f"tie, peaks within {tie_band_mw:g} MW" if tie else 'better' if better_gap else 'not better')
            cells.append(f"{name}: gap C {c['gap_share']:.1%} vs A {a['gap_share']:.1%} ({gap_txt}); {harm_txt}")
            ok &= better_gap and harm_ok
    if judged == 0:
        ok = False
    return ok, cells


def fmt(v, f="{:,.1f}"):
    return "n/a" if v is None else f.format(v)


def decide(rows):
    """D49: B is adopted only if, in both windows at both headrooms, it closes more of the selfish-to-ceiling gap than A
    without raising net harm per MW cut (no cut counts as infinite harm per MW cut). A cell with a run stopped by the
    referee has no comparison, so B is not shown to win it (the rule's default, A stays); an A stop is flagged. A cell
    whose rosters are incomplete or differ is not a B win."""
    inf = float("inf")
    cells, ok = [], True
    for win in WINDOWS:
        for h in HEADROOMS:
            a, b = rows.get(("a", win, h)), rows.get(("b", win, h))
            if a is None or b is None:
                cells.append(f"{WINDOWS[win]} {h} MW: missing run")
                ok = False
                continue
            if "stopped" in a or "stopped" in b:
                who = " and ".join(x for x, r in (("A", a), ("B", b)) if "stopped" in r)
                cells.append(f"{WINDOWS[win]} {h} MW: {who} stopped by the referee, no comparison; B not shown to "
                             "win" + ("" if "stopped" in b else " (A stopped: the rule does not cover this; owner's call)"))
                ok = False
                continue
            problem = roster_problem(a, b)
            if problem:
                cells.append(f"{WINDOWS[win]} {h} MW: {problem}; B not shown to win")
                ok = False
                continue
            hpa = inf if a["harm_per_mw_cut"] is None else a["harm_per_mw_cut"]
            hpb = inf if b["harm_per_mw_cut"] is None else b["harm_per_mw_cut"]
            better_gap = b["gap_share"] > a["gap_share"]
            not_worse_harm = hpb <= hpa
            cells.append(f"{WINDOWS[win]} {h} MW: gap B {b['gap_share']:.1%} vs A {a['gap_share']:.1%} "
                         f"({'better' if better_gap else 'not better'}); harm/MW cut B {fmt(b['harm_per_mw_cut'], '${:,.2f}')} "
                         f"vs A {fmt(a['harm_per_mw_cut'], '${:,.2f}')} ({'not higher' if not_worse_harm else 'higher'})")
            ok &= better_gap and not_worse_harm
    return ok, cells


def d53_section(rows):
    """Arm C vs. arm A (D53): the C columns, infeasible cells on their own rows, and the rule."""
    if not any(arm == "c" for arm, _, _ in rows):
        return []
    feas = load_feasibility()
    out = ["## Arm C (price mode) vs. arm A (D53)", "",
           "| Window | MW | Target | Arm | Peak vs selfish | Gap share | Share of M0 ceiling | Net harm $ (agents) | "
           "$ per MW cut | Operator net payout $ | Payout per MW cut $ | Rounds used (per gate) | Msgs/day (market) |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for win in WINDOWS:
        for h in HEADROOMS:
            f = feas.get(f"h2h_c_{win}_h{h}")
            tgt = "not checked" if f is None else ("feasible" if f["feasible"] else
                                                   f"infeasible ({f['max_short_mw']:.1f} MW short)")
            for arm in ("a", "c"):
                r = rows.get((arm, win, h))
                if r is None or "stopped" in r:
                    out.append(f"| {WINDOWS[win]} | {h} | {tgt} | {ARMS[arm]} | "
                               f"{'not run' if r is None else 'stopped by the referee'} | | | | | | | | |")
                    continue
                pr = r["price"]
                out.append(
                    f"| {WINDOWS[win]} | {h} | {tgt} | {ARMS[arm]} | {r['vs_selfish_mw']:+.1f} | {r['gap_share']:.1%} | "
                    f"{r['share_m0']:.1%} | {r['harm_usd']:,.0f} ({r['harmed']}) | "
                    f"{fmt(r['harm_per_mw_cut'], '{:,.2f}')} | "
                    + (f"{pr['payout_usd']:,.0f} | {fmt(pr['payout_per_mw_cut'], '{:,.0f}')} | "
                       f"{pr['rounds_total']} ({', '.join(map(str, pr['rounds']))}) | " if pr else "n/a | n/a | n/a | ")
                    + f"{r['msgs_per_day']:,.0f} ({r['market_msgs_per_day']:,.0f}) |")
    inf_rows = [(win, h) for win in WINDOWS for h in HEADROOMS
                if (feas.get(f"h2h_c_{win}_h{h}") or {}).get("feasible") is False and rows.get(("c", win, h))
                and "stopped" not in rows[("c", win, h)]]
    if inf_rows:
        out += ["", "**Infeasible targets (finding; not judged):** arm C where the target is more than 1 MW below "
                "the central cap LP's lowest reachable peak.", "",
                "| Window | MW | Max peak price $/MWh | Final (settled) peak price $/MWh | Rounds at the price cap | "
                "Operator net payout $ | Payout per MW cut $ | Net harm $ (agents) |", "|---|---|---|---|---|---|---|---|"]
        for win, h in inf_rows:
            r = rows[("c", win, h)]
            pr = r["price"]
            out.append(f"| {WINDOWS[win]} | {h} | {pr['max_price']:,.1f} | {pr['final_price']:,.1f} | "
                       f"{pr['rounds_at_cap']} | {pr['payout_usd']:,.0f} | {fmt(pr['payout_per_mw_cut'], '{:,.0f}')} | "
                       f"{r['harm_usd']:,.0f} ({r['harmed']}) |")
    c_rows = [r for (arm, _, _), r in sorted(rows.items()) if arm == "c" and "stopped" not in r]
    if c_rows:
        out += ["", "**Price rounds:** " + "; ".join(
            f"{WINDOWS[r['window']]} {r['h']} MW: cap {r['price']['cap']} per gate ({r['price']['basis']}), "
            f"converged {sum(r['price']['converged'])} of {len(r['price']['converged'])} gates, final max excess "
            + ", ".join("n/a" if x is None else f"{x:.1f}" for x in r["price"]["final_excess_mw"]) + " MW"
            for r in c_rows)]
    ok, cells = decide_d53(rows, feas)
    out += ["", "**D53 rule** (C adopted over A only if, in every feasible cell, it closes more of the selfish-to-ceiling "
            "gap than A without raising net harm per MW cut; where A has no cut, total net harm is compared instead):",
            ""]
    out += [f"- {c}" for c in cells]
    out += ["", f"**Result: {'C adopted over A' if ok else 'C not adopted; A stays'}** under D53 as written. "
            "If C fails, revisit B (D50).", ""]
    return out


def main():
    rows = {}
    for arm in ARMS:
        for win in WINDOWS:
            for h in HEADROOMS:
                r = row(arm, win, h)
                if r is not None:
                    rows[(arm, win, h)] = r
    lines = ["# Head-to-head: system-peak clearing (D49)", "",
             "Arms: A = need/offer/prep clearing with D46 + D48 (F1+F3); B = A + published per-step zone room (D50, F4). "
             "All other settings as configs/m2a_h*.yaml (D34 cap on, cap_rebase off). Pre-registered; no tuning between "
             "runs. Arm C (price mode, D53) entered after prototype v3 passed D49's amended entry test (Oct 26); it is judged "
             "against A under D53 below.", "",
             "| Window | MW | Arm | Peak MW | vs selfish | Share of M0 ceiling | Gap share | Net harm $ (agents) | "
             "$ per MW cut | Unfilled / needed MWh | Msgs/day (market) | Over target outside windows: strict / beyond band / "
             "exchange-caused (max MW) | Referee | Pass |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for win in WINDOWS:
        for h in HEADROOMS:
            for arm in ARMS:
                r = rows.get((arm, win, h))
                if r is None:
                    lines.append(f"| {WINDOWS[win]} | {h} | {ARMS[arm]} | not run | | | | | | | | | | |")
                    continue
                if "stopped" in r:
                    lines.append(f"| {WINDOWS[win]} | {h} | {ARMS[arm]} | stopped by the referee | | | | | | | | | "
                                 "abort | no |")
                    continue
                o = r["over"]
                lines.append(
                    f"| {WINDOWS[win]} | {h} | {ARMS[arm]} | {r['peak_mw']:,.1f} | {r['vs_selfish_mw']:+.1f} | "
                    f"{r['share_m0']:.1%} | {r['gap_share']:.1%} | {r['harm_usd']:,.0f} ({r['harmed']}) | "
                    f"{fmt(r['harm_per_mw_cut'], '{:,.2f}')} | "
                    + ("n/a (no needs) | " if r["price"] else f"{r['unfilled_mwh']:,.0f} / {r['need_mwh']:,.0f} | ")
                    + f"{r['msgs_per_day']:,.0f} ({r['market_msgs_per_day']:,.0f}) | {o['strict']} / {o['beyond_band']} / "
                    f"{o['caused_steps']} ({o['max_excess_mw']:.1f}; caused {o['caused_max_mw']:.1f}) | "
                    f"{fmt(r['referee'], '{:d}')} | {'yes' if r['pass']['pass'] else 'no'} |")
    done = {k: r for k, r in rows.items() if "stopped" not in r}
    halted = {k: r for k, r in rows.items() if "stopped" in r}
    if halted:
        lines += ["", "**Stopped by the referee (`referee: abort`, an undeliverable award; no summary):**", ""]
        lines += [f"- {WINDOWS[r['window']]} {r['h']} MW {ARMS[r['arm']]}: {r['stopped']}" for k, r in sorted(halted.items())]
    lines += ["", "**Net harm by component (harmed agents, $):**", ""]
    for k, r in sorted(done.items()):
        lines.append(f"- {WINDOWS[r['window']]} {r['h']} MW {ARMS[r['arm']]}: " +
                     (", ".join(f"{c} {v:,.0f}" for c, v in r["harm_components"].items()) or "none"))
    lines += ["", "**Pass block (M2a, D51 band rule):**", ""]
    for k, r in sorted(done.items()):
        failed = [c for c, v in r["pass"].items() if c != "pass" and not v]
        lines.append(f"- {WINDOWS[r['window']]} {r['h']} MW {ARMS[r['arm']]}: "
                     + ("pass" if r["pass"]["pass"] else "fails " + ", ".join(failed)) + f" (commit {r['commit']})")
    ok, cells = decide(rows)
    lines += ["", "**D49 rule** (B adopted only if, in both windows at both headrooms, it closes more of the "
              "selfish-to-ceiling gap than A without raising net harm per MW cut; no cut counts as infinite):", ""]
    lines += [f"- {c}" for c in cells]
    lines += ["", f"**Result: {'B adopted' if ok else 'A stays; F4 (D50) not adopted'}** under the rule as written.", ""]
    lines += d53_section(rows)
    lines += ["**Late-reveal diagnostic (no rule, D52):** zone nominated load at the worst over-target step outside "
              "windows, in the last residual round vs. the last binding call.", ""]
    for k, r in sorted(done.items()):
        rv = r["over"]["reveal"]
        if rv is None:
            lines.append(f"- {WINDOWS[r['window']]} {r['h']} MW {ARMS[r['arm']]}: no over-target step")
        else:
            lines.append(f"- {WINDOWS[r['window']]} {r['h']} MW {ARMS[r['arm']]}: step {rv['step_utc']}, target "
                         f"{rv['target_mw']:.1f} MW; last residual round {fmt(rv['last_residual_round_mw'])} MW "
                         f"(closed {rv['residual_closed']}) vs. last binding {fmt(rv['last_binding_mw'])} MW "
                         f"(closed {rv['binding_closed']})")
    lines += ["", "**Notes**",
              "- Gap share = (selfish peak - exchange peak) / (selfish peak - M0 ceiling peak), per window's own M0.",
              "- $ per MW cut = total net harm / (selfish peak - exchange peak); n/a when the exchange cuts nothing.",
              "- Msgs/day: all messages over the slice / 2 local days; market excludes telemetry and ticks.",
              "- Over target: strict count; beyond the participants' aggregate free band (D51, the pass rule); "
              "steps where the exchange's load is over both the target and the selfish run's load (max MW)."]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "rows.json").write_text(json.dumps({f"{a}_{w}_h{h}": r for (a, w, h), r in rows.items()}, indent=2,
                                              default=float), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
