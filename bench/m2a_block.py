"""
Nov 2 step 9 (report only, offline, existing runs): M2a's full pass block for each of the 23 windows at m = 0, $5 and
$10 (100 MW, arm C, 1p, cap 100).

Columns, per cell:
  - no_net_harm (section 8, part 1): no agent beyond tolerance, max(1% x |acting alone|, $10), under rule 1p. From
    the D56 cell (bench/d56_resettle, unchanged), so the 7 D58 windows that ran under the recorded-schedule reference
    are judged on 1p like the rest. The in-run summary's own verdict is shown next to it.
  - peak_below_selfish: the run's peak below the selfish run's peak by more than 1 kW (summary `pass`).
  - no_step_over_target (strict): metered system net load at or below the operator's target at every step outside
    need windows from the first gate on (summary target_check_metered.steps_over == 0). Arm C has no need windows,
    so this is M2a's "no rebound outside need windows" read strictly.
  - d51_band_rule: the same check ignoring exceedance within the participants' aggregate free band (D51;
    steps_over_beyond_band == 0). This is what the in-run m2a_pass uses.
  - roster_complete: every roster agent in the harm check (summary net_harm.missing_from_check empty, and the cell's
    roster check).
  - pass: all of no_net_harm, peak_below_selfish, d51_band_rule and roster_complete (M2a's rule as the summary
    applies it, with no-net-harm on 1p). The strict target column is reported, not gated.

Run: python -m bench.m2a_block [--out runs/nov02/item9]
"""
import argparse
import json
import sys
from pathlib import Path

from bench import d56_resettle as D
from bench import nov01 as N
from bench import d58_horizon as H

MARGINS = (0.0, 5.0, 10.0)


def cell_block(w: str, m: float) -> dict:
    run, cell = N.window_cell(w, m)
    if cell is None:
        return {"window": w, "margin": m, "run": run, "missing": True}
    s = N.summary(run)
    (hk, ev), = s["exchange_by_headroom_mw"].items()
    met = D.cell_metrics(cell, H.RULE)
    tc = ev.get("target_check_metered") or {}
    nh = ev["net_harm"]
    row = {"window": w, "margin": m, "run": run, "feasible": bool(cell.get("feasible", True)),
           "settled_in_run": (s["header"].get("config") or {}).get("settlement_reference", "recorded"),
           "harmed_1p": met["harmed"], "harm_1p_usd": met["harm_usd"],
           "no_net_harm": met["harmed"] == 0,
           "in_run_harmed": nh["agents_harmed"], "in_run_no_net_harm": bool(nh["pass"]),
           "peak_vs_selfish_mw": ev["peak_vs_selfish_mw"], "peak_below_selfish": bool(ev["pass"]),
           "steps_over_target": tc.get("steps_over"), "steps_checked": tc.get("steps_checked"),
           "no_step_over_target": tc.get("steps_over") == 0,
           "steps_over_beyond_band": tc.get("steps_over_beyond_band"),
           "max_excess_mw": tc.get("max_excess_mw"), "aggregate_band_mw": tc.get("aggregate_band_mw"),
           "d51_band_rule": tc.get("steps_over_beyond_band", 1) == 0,
           "roster_complete": not nh["missing_from_check"] and len(cell["roster"]) == len(s["header"]["roster"]),
           "in_run_m2a_pass": (ev.get("m2a_pass") or {}).get("pass"),
           "settlement_harmed_1p": met["settlement_harmed"], "settlement_harm_1p_usd": met["settlement_harm_usd"],
           "d57_peak_before_first_gate": bool(H.bystander(cell, s)["summary_peak_before_first_gate"])}
    row["pass"] = all(row[k] for k in ("no_net_harm", "peak_below_selfish", "d51_band_rule", "roster_complete"))
    return row


def restated_gate(rows: list[dict], horizon_pass: bool) -> dict:
    """M2a's gate as restated by the owner on Nov 3 (D75; set after seeing results, disclosed), for one margin:
      (i)   1p settlement-caused harm 0 in every window;
      (ii)  the D58 horizon check passes over all windows (bench.d58_horizon.horizon_check, unchanged);
      (iii) peak below selfish and the D51 band rule in feasible windows, leaving out D57 start-of-slice cases (the
            run's peak falls before the first nomination deadline, as d58_horizon.bystander detects it);
      and, as every pass test (CLAUDE.md), the roster complete in every window.
    Fails closed on a missing cell, an empty window list or a mixed margin."""
    if not rows:
        raise D.CheckFailed("no windows")
    if any(r.get("missing") for r in rows):
        raise D.CheckFailed(f"missing cells: {[r['window'] for r in rows if r.get('missing')]}")
    if len({r["margin"] for r in rows}) != 1:
        raise D.CheckFailed("rows from more than one margin")
    infeasible = [r["window"] for r in rows if not r["feasible"]]
    d57 = [r["window"] for r in rows if r["d57_peak_before_first_gate"]]
    judged = [r for r in rows if r["feasible"] and not r["d57_peak_before_first_gate"]]
    fails = {"settlement_harm": [r["window"] for r in rows if r["settlement_harmed_1p"] != 0],
             "horizon": [] if horizon_pass else ["all windows"],
             "peak_below_selfish": [r["window"] for r in judged if not r["peak_below_selfish"]],
             "d51_band_rule": [r["window"] for r in judged if not r["d51_band_rule"]],
             "roster": [r["window"] for r in rows if not r["roster_complete"]]}
    return {"margin": rows[0]["margin"], "pass": not any(fails.values()), "fails": fails,
            "windows": len(rows), "judged_for_iii": len(judged), "excluded_infeasible": infeasible,
            "excluded_d57": d57}


def gate_cmd(out: Path) -> int:
    """Nov 3, owner item 3: the restated gate at each margin, next to the original per-window block."""
    rows = [cell_block(w, m) for w in N.D58_WINDOWS + N.OVN_WINDOWS for m in MARGINS]
    res = []
    for m in MARGINS:
        rm = [r for r in rows if r["margin"] == m]
        hz = N.horizon(m)["horizon"]
        g = restated_gate(rm, bool(hz and hz["pass"]))
        g["original_pass"] = sum(r["pass"] for r in rm)
        res.append(g)
        print(json.dumps(g), flush=True)
    (out / "m2a_gate.json").write_text(json.dumps({"gates": res, "rows": rows}, indent=1))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(N.REPO / "runs" / "nov02" / "item9"))
    ap.add_argument("--gate", action="store_true", help="the restated gate (D75) at each margin")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.gate:
        return gate_cmd(out)
    rows = [cell_block(w, m) for w in N.D58_WINDOWS + N.OVN_WINDOWS for m in MARGINS]
    (out / "m2a_block.json").write_text(json.dumps(rows, indent=1))
    yn = lambda b: "yes" if b else "**no**"
    print("| Window | Margin | Feasible | No net harm (1p): harmed / $ | Peak below selfish (MW) | Steps over target, strict | D51 band rule (steps beyond band) | Roster | **M2a pass** |")
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if r.get("missing"):
            print(f"| {r['window']} | ${r['margin']:g} | missing | | | | | | |")
            continue
        print(f"| {r['window']} | ${r['margin']:g} | {'yes' if r['feasible'] else 'no'} | "
              f"{yn(r['no_net_harm'])}: {r['harmed_1p']} / ${r['harm_1p_usd']:,.0f} | "
              f"{yn(r['peak_below_selfish'])} ({r['peak_vs_selfish_mw']:+.1f}) | "
              f"{r['steps_over_target']} of {r['steps_checked']} | {yn(r['d51_band_rule'])} ({r['steps_over_beyond_band']}) | "
              f"{yn(r['roster_complete'])} | {'PASS' if r['pass'] else 'FAIL'} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
