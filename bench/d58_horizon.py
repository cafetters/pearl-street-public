"""
D58: horizon no-net-harm test under D56's rule 1p (docs/plans/d56_settlement_report.md, "D58 horizon test").

Per agent, the result with Pearl Street minus acting alone (section 8's terms, settled on 1p) is summed over 11
two-day windows at 100 MW. An agent passes if that sum is at least -max(1% x |summed acting alone|, $10). Every
window must also have zero 1p settlement-caused harm (D56's gate), so only the DA vs. RT gap may even out across
windows. Worst window, gap signs, a type breakdown, the result without Nov 28 and the bystander items are reported,
not gated. Reads runs/d56/<cell>.json (bench.d56_resettle) and runs/<cell>/summary.json. No simulation runs.

Run:
    python -m bench.d58_horizon [--out runs/d58]
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

from bench import d56_resettle as D
from bench.d56_resettle import EPS_USD, TOL_ABS, TOL_REL, CheckFailed

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "runs" / "d58"
RULE = "1p"
CAP_USD_MWH = 100.0                     # D54; the re-settled cells must never have priced above it
IN_RUN = ["h2h_c_jan02_h100_1p", "h2h_c_jan12_h100_1p", "h2h_c_jan16_h100_1p", "h2h_c_jan31_h100_1p"]
RESETTLED = ["h2h_c_feb12_h100", "h2h_c_jun19_h100", "h2h_c_sep21_h100", "h2h_c_jul01_h100", "h2h_c_aug28_h100",
             "h2h_c_jul14_h100", "h2h_c_nov28_h100"]
CELLS = IN_RUN + RESETTLED
SENSITIVITY_DROP = "h2h_c_nov28_h100"   # D57 start-of-slice artifact; in the pass set, also reported without


def check_cell(cell: dict, max_price: float | None):
    """In-run cells must have settled on 1p at the 100 cap; re-settled cells must never have priced above 100 in
    any round of any gate, so their behaviour is what a cap-100 run gives (the reference does not move behaviour)."""
    run = cell["run"]
    if run in IN_RUN:
        if cell.get("settled_rule") != RULE or cell.get("price_cap_usd_mwh") != CAP_USD_MWH:
            raise CheckFailed(f"{run}: not settled in-run on {RULE} at the {CAP_USD_MWH:g} cap")
    elif max_price is None or not max_price <= CAP_USD_MWH:
        raise CheckFailed(f"{run}: round price {max_price} above {CAP_USD_MWH:g}, not cap-100 equivalent")


def window_rows(cell: dict) -> dict:
    ag = cell["agents"]
    e = np.array([a["e"] for a in ag])
    net = np.asarray(cell["rules"][RULE]["net"], float)
    return {"ids": [a["agent"] for a in ag], "gain": net - e, "alone": np.array([a["alone"] for a in ag]),
            "gap": np.array([a["components"]["da_rt_gap"] for a in ag]), "net": net}


def horizon_check(cells: list[dict], max_price: dict) -> dict:
    """The pre-registered test. Fails closed on a roster mismatch or a cell that is not cap-100 equivalent."""
    if not cells:
        raise CheckFailed("no cells")
    roster = list(cells[0]["header"]["roster"])
    for c in cells:
        D.check_roster(roster, list(c["header"]["roster"]), f"{c['run']} header vs {cells[0]['run']}")
        D.check_roster(roster, [a["agent"] for a in c["agents"]], f"{c['run']} agents")
        check_cell(c, max_price.get(c["run"]))
    n = len(roster)
    G, A, GAP = np.zeros(n), np.zeros(n), np.zeros(n)
    gap_pos, gap_neg = np.zeros(n, int), np.zeros(n, int)
    worst, worst_run = np.full(n, np.inf), [None] * n
    windows = {}
    for c in cells:
        w = window_rows(c)
        idx = [roster.index(a) for a in w["ids"]]
        G[idx] += w["gain"]
        A[idx] += w["alone"]
        GAP[idx] += w["gap"]
        gap_pos[idx] += w["gap"] > 0
        gap_neg[idx] += w["gap"] < 0
        for j, g in zip(idx, w["gain"]):
            if g < worst[j]:
                worst[j], worst_run[j] = g, c["run"]
        m = D.cell_metrics(c, RULE)
        windows[c["run"]] = {"settlement_harmed": m["settlement_harmed"], "settlement_harm_usd": m["settlement_harm_usd"],
                             "settlement_gate": m["settlement_harmed"] == 0, "harmed": m["harmed"],
                             "harm_usd": m["harm_usd"], "gap_total_usd": float(w["gap"].sum()),
                             "gap_cost_agents": int((w["gap"] > 0).sum()), "gap_gain_agents": int((w["gap"] < 0).sum())}
    tol = np.maximum(TOL_REL * np.abs(A), TOL_ABS)
    ok = G >= -tol - EPS_USD
    agents = [{"agent": a, "result_usd": float(G[j]), "alone_usd": float(A[j]), "tol_usd": float(tol[j]),
               "pass": bool(ok[j]), "worst_window_usd": float(worst[j]), "worst_window": worst_run[j],
               "gap_usd": float(GAP[j]), "gap_cost_windows": int(gap_pos[j]), "gap_gain_windows": int(gap_neg[j])}
              for j, a in enumerate(roster)]
    types = {}
    for r in agents:
        t = types.setdefault(r["agent"].split("_")[0], {"agents": 0, "failing": 0, "result_usd": 0.0,
                                                         "min_result_usd": np.inf, "gap_usd": 0.0})
        t["agents"] += 1
        t["failing"] += not r["pass"]
        t["result_usd"] += r["result_usd"]
        t["min_result_usd"] = min(t["min_result_usd"], r["result_usd"])
        t["gap_usd"] += r["gap_usd"]
    totals = [v["gap_total_usd"] for v in windows.values()]
    gates_ok = all(v["settlement_gate"] for v in windows.values())
    return {"cells": [c["run"] for c in cells], "pass": bool(ok.all()) and gates_ok,
            "agents_failing": int((~ok).sum()), "failing_shortfall_usd": float((-(G + tol))[~ok].sum()),
            "settlement_gate_all_windows": gates_ok, "windows": windows,
            "gap_sign": {"windows_cost": int(sum(t > 0 for t in totals)), "windows_gain": int(sum(t < 0 for t in totals)),
                         "mean_window_total_usd": float(np.mean(totals)), "sum_usd": float(np.sum(totals))},
            "by_type": types, "agents": agents}


def bystander(cell: dict, summary: dict) -> dict:
    """Report only: (a) operator payout vs. the cut, (b) rebound and peak from the first gate, (c) price impact."""
    h = summary["exchange_by_headroom_mw"]["100"]
    s = summary["series"]
    run_kw, self_kw = np.asarray(s["exchange_h100_rinfo_kw"], float), np.asarray(s["selfish_kw"], float)
    g0 = int(h["target_check_metered"]["pre_gate"]["steps"])
    cut_g = float(self_kw[g0:].max() - run_kw[g0:].max()) / 1000.0
    pay = float(np.sum(cell["rules"][RULE]["net"]))
    pre_gate_peak = int(run_kw.argmax()) < g0
    t = h["target_check_metered"]
    return {"operator_payout_usd": pay,
            "per_relief_mwh_usd": pay / cell["relief_mwh"] if cell["relief_mwh"] > 1e-9 else None,
            "per_mw_cut_summary_usd": pay / cell["mw_cut"] if cell["mw_cut"] > 1e-6 else None,
            "per_mw_cut_from_first_gate_usd": pay / cut_g if cut_g > 1e-6 else None,
            "peak_vs_selfish_mw": float(h["peak_vs_selfish_mw"]), "cut_from_first_gate_mw": cut_g,
            "summary_peak_before_first_gate": pre_gate_peak,
            "steps_over_target_outside_windows": t["steps_over"],
            "steps_over_beyond_band": t["steps_over_beyond_band"], "max_excess_mw": t["max_excess_mw"],
            "price_impact": "not modeled: prices are recorded and do not react to agents"}


def tool_sha() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)
    from bench.runinfo import run_header
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    head = {**run_header("bench/d58_horizon.py", {"cells": ",".join(CELLS), "rule": RULE, "cap": CAP_USD_MWH}),
            "tool_sha256_16": tool_sha()}
    try:
        cells = D.load_results(CELLS, D.OUT)
        summ = {r: json.loads((REPO / "runs" / r / "summary.json").read_text()) for r in CELLS}
        maxp = {r: summ[r]["exchange_by_headroom_mw"]["100"]["price_mode"]["max_peak_price_usd_mwh"] for r in RESETTLED}
        res = {"main": horizon_check(cells, maxp),
               "without_nov28": horizon_check([c for c in cells if c["run"] != SENSITIVITY_DROP], maxp),
               "max_round_price_resettled": maxp,
               "bystander": {c["run"]: bystander(c, summ[c["run"]]) for c in cells}}
    except CheckFailed as err:
        (out / "horizon.failed.json").write_text(json.dumps({"header": head, "failed": str(err)}, indent=1))
        print(f"CHECK FAILED: {err}", flush=True)
        return 2
    head["roster"] = cells[0]["header"]["roster"]
    (out / "horizon.json").write_text(json.dumps({"header": head, **res}, indent=1, default=float))
    m, s = res["main"], res["without_nov28"]
    print(f"pass {m['pass']}: {m['agents_failing']} agents failing, settlement gate {m['settlement_gate_all_windows']}; "
          f"without Nov 28: {s['agents_failing']} failing", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
