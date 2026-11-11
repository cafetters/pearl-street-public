"""D76 max rule: the in-run confirmation on the sealed holdout (docs/plans/max_rule_holdout_check.md).

Half A: m = 0 under settlement_rule max_first_call (with the relief cap). Half B: $5 under today's 1p. Pass, all five:
  (a) half A's settlement-caused harm is 0 in every window (bench.d56_resettle's tolerance and gap split, with each
      agent's settlement its in-run peak_settle net);
  (b) half A's total operator net payout (sum of peak_settle net_usd) over all windows is below half B's;
  (c) in every half-A window, each agent's in-run peak_settle net equals bench.item7_resettle.max_first_call_steps on
      the run's own trace within $0.001 per agent (owner, Nov 4: the trace's prices are rounded to 6 decimals, the
      exchange settles unrounded; the jan12 smoke run's largest error from that alone was $4.3e-6);
  (d) the roster is complete in every run of both halves (from each run header; fails if any agent is missing);
  (e) half A passes bench.d58_horizon.horizon_check, unchanged, fed half A's cells with rules["1p"]["net"] replaced by
      the max-rule settlement each run paid (owner, Nov 3), after a fail-closed reconciliation: that net equals each
      run's own peak_settle lines within $0.01 per agent.
Report only: half B's harm and horizon result, settlement paths, infeasible and D57 windows.

Check (c) reads only the trace. The loader (half A: bench.d56_resettle.load_cell with max_rule=True) reads the data.
"""
import json
from pathlib import Path

import numpy as np

from bench import d56_resettle as D
from bench import d58_horizon as H
from bench.item7_resettle import max_first_call_steps

TOL_C_USD = 1e-3          # (c), per agent (owner, Nov 4): above the trace's price-rounding error, below any rule error
TOL_RECON_USD = 0.01      # (e) reconciliation
TOL_AGG_KW = 0.01         # nominations vs the binding aggregate
CheckFailed = D.CheckFailed


# ---------------------------------------------------------------- (c): in-run vs offline, from the trace alone
def offline_vs_inrun(trace: Path) -> dict:
    """Per agent: in-run peak_settle net_usd vs max_first_call_steps on the trace's final binding prices, binding
    nominations (the last nominate per agent and call before the call's aggregate; a never-nominated step counts as
    nominating the reference, as in-run), selfish_reference lines and metered site net. Fails closed on a roster gap,
    a nomination at or after its gate, or nominations that don't add up to the binding aggregate."""
    lines = Path(trace).read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    if (header.get("exchange") or {}).get("settlement_rule") != "max_first_call":
        raise CheckFailed(f"{trace}: not a max_first_call run")
    roster = list(header["roster"])
    msgs = [json.loads(x) for x in lines[1:]]
    ts = sorted({m["t_utc"] for m in msgs if m["msg_type"] == "telemetry"})
    pos = {t: i for i, t in enumerate(ts)}
    T = len(ts)
    final, aggs, order, noms, sref, net, settle = {}, {}, [], {}, {}, {}, {}
    for m in msgs:
        typ, p = m["msg_type"], m["payload"]
        if typ == "peak_price" and p.get("final"):
            final[p["call_id"]] = p
        elif typ == "nomination_aggregate" and p.get("binding"):
            aggs[p["call_id"]] = (m["t_utc"], p)
            order.append(p["call_id"])
        elif typ == "nominate":
            noms[(m.get("sender") or m.get("from"), p["call_id"])] = (m["t_utc"], p)
        elif typ == "selfish_reference":
            sref[(p["asset_id"].split("/")[0], p["call_id"])] = p
        elif typ == "telemetry" and "net_kw" in p:
            net.setdefault(p["asset_id"].split("/")[0], np.zeros(T))[pos[m["t_utc"]]] = p["net_kw"]
        elif typ == "peak_settle":
            settle[m["to"]] = p
    for what, ids in (("telemetry", list(net)), ("peak_settle", list(settle))):
        D.check_roster(roster, ids, f"{trace} {what}")
    binding = [c for c in order if c in final]

    def arr(start, vals):
        if start not in pos or pos[start] + len(vals) > T:
            raise CheckFailed(f"{trace}: a window from {start} ({len(vals)} steps) is outside the metered steps")
        a = np.full(T, np.nan)
        a[pos[start]:pos[start] + len(vals)] = vals
        return a

    pis = [arr(final[c]["window"]["start_utc"], final[c]["price_usd_mwh"]) for c in binding]
    late, filled, agg_err = 0, 0, 0.0
    per_agent = []
    nom_arr = {}
    for c in binding:
        t_agg, ap = aggs[c]
        tot = np.zeros(T)
        for aid in roster:
            if (aid, c) in noms:
                t_n, pn = noms[(aid, c)]
                late += int(t_n >= t_agg)
                nom_arr[(aid, c)] = arr(pn["window"]["start_utc"], pn["kw"])
                tot = tot + np.nan_to_num(nom_arr[(aid, c)])
        agg = arr(ap["window"]["start_utc"], ap["kw"])
        ok = ~np.isnan(agg)
        agg_err = max(agg_err, float(np.abs(tot[ok] - agg[ok]).max()) if ok.any() else 0.0)
    if late:
        raise CheckFailed(f"{trace}: {late} nominations at or after their gate")
    if agg_err > TOL_AGG_KW:
        raise CheckFailed(f"{trace}: nominations vs binding aggregate {agg_err:.4f} kW")
    worst = 0.0
    for aid in roster:
        qs, ns = [], []
        for c in binding:
            if (aid, c) not in sref:
                raise CheckFailed(f"{trace}: no selfish_reference for {aid} on {c}")
            q = arr(sref[(aid, c)]["window"]["start_utc"], sref[(aid, c)]["net_kw"])
            n = nom_arr.get((aid, c), np.full(T, np.nan))
            gap = np.isnan(n) & ~np.isnan(q)
            filled += int(gap.sum())
            qs.append(q)
            ns.append(np.where(gap, q, n))                # never nominated: it nominated its reference
        want = float(max_first_call_steps(pis, ns, qs, net[aid])[0].sum()) * D.K
        got = float(settle[aid]["net_usd"])
        worst = max(worst, abs(got - want))
        per_agent.append({"agent": aid, "in_run_usd": got, "offline_usd": want})
    return {"trace": str(trace), "roster": roster, "binding_calls": binding, "max_err_usd": worst,
            "steps_filled_with_reference": filled, "agg_max_err_kw": agg_err, "pass": worst <= TOL_C_USD,
            "per_agent": per_agent, "payout_usd": float(sum(float(settle[a]["net_usd"]) for a in roster))}


def peak_settle_totals(trace: Path) -> dict[str, float]:
    """Each agent's peak_settle net_usd, read straight from the trace (the reconciliation's independent path)."""
    out = {}
    with open(trace, encoding="utf-8") as f:
        next(f)
        for line in f:
            m = json.loads(line)
            if m["msg_type"] == "peak_settle":
                out[m["to"]] = float(m["payload"]["net_usd"])
    return out


# ---------------------------------------------------------------- half A cells for (a) and (e)
def max_cell(cell: dict) -> dict:
    """A half-A cell (bench.d56_resettle.evaluate output of a max_rule=True load) with rules["1p"]["net"] replaced by
    the max-rule settlement the run paid (owner, Nov 3); everything else unchanged."""
    if cell.get("settled_rule") != "max_first_call" or "max_net" not in cell:
        raise CheckFailed(f"{cell.get('run')}: not a max-rule cell")
    rules = {**cell["rules"], "1p": {**cell["rules"]["1p"], "net": list(cell["max_net"])}}
    return {**cell, "rules": rules}


def reconcile(cell: dict, totals: dict[str, float]) -> float:
    """(e)'s reconciliation: the net fed to horizon_check vs the run's own peak_settle lines, per agent."""
    roster = cell["header"]["roster"]
    D.check_roster(roster, list(totals), f"{cell['run']} peak_settle lines")
    net = dict(zip([a["agent"] for a in cell["agents"]], cell["rules"]["1p"]["net"]))
    err = max(abs(net[a] - totals[a]) for a in roster)
    if err > TOL_RECON_USD:
        raise CheckFailed(f"{cell['run']}: reconciliation, max error ${err:.4f} > ${TOL_RECON_USD}")
    return err


# ---------------------------------------------------------------- the verdict (pure)
def verdict(a_cells: list[dict], b_cells: list[dict], c_results: dict[str, dict], a_totals: dict[str, dict],
            b_payout: dict[str, float], max_price: dict[str, float]) -> dict:
    """a_cells: max_cell() outputs, one per window; b_cells: today's-rule cells; c_results: offline_vs_inrun per half-A
    run; a_totals: peak_settle_totals per half-A run; b_payout: half B's operator net payout per run."""
    if not a_cells or len(a_cells) != len(b_cells):
        raise CheckFailed(f"half A has {len(a_cells)} windows, half B {len(b_cells)}")
    roster = list(a_cells[0]["header"]["roster"])
    for c in a_cells + b_cells:                                          # (d)
        D.check_roster(roster, list(c["header"]["roster"]), f"{c['run']} header")
        D.check_roster(roster, [a["agent"] for a in c["agents"]], f"{c['run']} agents")
    for r, res in c_results.items():
        D.check_roster(roster, res["roster"], f"{r} trace")
    a_runs = [c["run"] for c in a_cells]
    if set(c_results) != set(a_runs) or set(a_totals) != set(a_runs) or set(b_payout) != {c["run"] for c in b_cells}:
        raise CheckFailed("inputs don't cover the same runs")
    recon = {c["run"]: reconcile(c, a_totals[c["run"]]) for c in a_cells}
    a_metrics = {c["run"]: D.cell_metrics(c, "1p") for c in a_cells}
    b_metrics = {c["run"]: D.cell_metrics(c, "1p") for c in b_cells}
    a_payout = {r: float(sum(a_totals[r].values())) for r in a_runs}
    gate_a = all(m["settlement_harmed"] == 0 for m in a_metrics.values())
    gate_b = sum(a_payout.values()) < sum(b_payout.values())
    gate_c = all(res["max_err_usd"] <= TOL_C_USD for res in c_results.values())
    horizon = H.horizon_check(a_cells, max_price)
    gate_e = bool(horizon["pass"])
    return {"pass": gate_a and gate_b and gate_c and gate_e,
            "gates": {"a_settlement_harm_zero": gate_a, "b_payout_below_half_b": gate_b,
                      "c_in_run_equals_offline": gate_c, "d_roster_complete": True, "e_horizon": gate_e},
            "half_a": {r: {"settlement_harmed": a_metrics[r]["settlement_harmed"],
                           "settlement_harm_usd": a_metrics[r]["settlement_harm_usd"], "payout_usd": a_payout[r],
                           "c_max_err_usd": c_results[r]["max_err_usd"], "reconciliation_err_usd": recon[r],
                           "feasible": bool(next(c for c in a_cells if c["run"] == r).get("feasible", True))}
                       for r in a_runs},
            "half_b_report_only": {r: {"settlement_harmed": m["settlement_harmed"],
                                       "settlement_harm_usd": m["settlement_harm_usd"], "payout_usd": b_payout[r]}
                                   for r, m in b_metrics.items()},
            "payout_usd": {"half_a": sum(a_payout.values()), "half_b": sum(b_payout.values())},
            "horizon": {k: horizon[k] for k in ("pass", "agents_failing", "failing_shortfall_usd",
                                                "settlement_gate_all_windows")},
            "roster": roster}
