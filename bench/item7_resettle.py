"""
Nov 2 item 5 (report only, offline, existing traces): re-settle every m = 0 window of the 23-window set under the
relief-increment rule proposed in docs/overnight/2026-11-01-day.md (item 2), and compare with today's 1p.

Relief-increment rule, per agent and step, with c_1 .. c_k the binding calls whose final price covers the step:
    pay = p_1 (N_1 - Q_1) + sum_{i>=2} p_i [(N_i - Q_i) - (N_{i-1} - Q_{i-1})] + p_k (N_metered - N_k)
p_i is call i's final (settled) peak price, N_i the agent's binding nomination for call i, Q_i rule 1p's selfish
reference computed at call i's gate (bench/d56_resettle's planner and inputs, over the call's whole window). With one
binding call this is today's 1p exactly; today's 1p is p_k (N_metered - Q_k).

Behaviour is held fixed (the same run), as in the day session's offline counterfactual. That is exact only for
agents that do not anticipate later calls' prices; ours re-plan on the current price only. Cross-call arbitrage by an
anticipating agent is not modelled.

The windows and cells are bench/nov01's m = 0 set: the 11 D58 comparators (in-run 1p runs, or recorded-reference runs
re-settled under 1p offline) and the 12 overnight runs. Harm, tolerance and the settlement-caused split are
bench/d56_resettle's, unchanged.

Fail-closed checks per window:
  - today's 1p rebuilt from these arrays equals the cell's 1p net per agent (max error <= $1e-6);
  - each call's nominations sum to that call's binding nomination_aggregate (max error <= 0.01 kW per step);
  - where the run carries in-run selfish_reference lines, Q_i equals them over the call's window (<= 1e-6 kW);
  - roster complete in every input.

Run: python -m bench.item7_resettle [--out runs/nov02/item5]
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

from bench import d56_resettle as D
from bench import nov01 as N

TOL_PAY_USD = 1e-6
TOL_AGG_KW = 0.01
TOL_REF_KW = 1e-6


def max_first_call_steps(pis, noms, qs, Nm, cap="below"):
    """The max rule for one agent, per step ($/MWh x kW; times K gives $). pis, noms, qs: one array per binding call in
    closing order (settled price, NaN where the call doesn't price the step; binding nomination; 1p reference), Nm:
    metered site net. Each call's relief increment is paid at the highest price among it and the later calls that
    price the step; the metered deviation from the last nomination at the last call's price. Also returns the arrays
    the fail-closed identities use. exchange/core.py's settlement_rule max_first_call is tested against this.
    The step is today's 1p at the settling call, p_k (Nm - Q_k), plus a premium for relief priced higher at earlier
    calls, sum_i (L_i - p_k) inc_i with L_i the highest price among call i and the later calls.
    cap (owner, Nov 4, D76; "the max rule never pays for relief the meter didn't show"):
      "below" (the rule): the premium is sum_i (L_i - L_{i+1}) rel_i (L_{k+1} = p_k; each spread >= 0), and each
        call's relief is limited to max(0, min(rel_i, Nm - Q_j)), Q_j the reference of the last call that priced the
        step above 0: relief paid at a higher earlier price never exceeds the metered relief. Applied where an earlier
        call priced the step above the settling call (P1 > p_k + 1e-9). Two calls with p_B < p_A:
        (p_A - p_B) x max(0, min(N_A - Q_A, Nm - Q_j)), j = B if p_B > 0, else A. At p_k = 0 this is the zero cap.
      "zero": the same, only where the settling call priced the step at 0 (the first Nov 4 version).
      None: no cap (the rule as first built)."""
    T = len(Nm)
    prev_rel = np.full(T, np.nan)
    first_p, first_rel, first_q = np.full(T, np.nan), np.full(T, np.nan), np.full(T, np.nan)
    pos_q = np.full(T, np.nan)                       # reference of the last call pricing the step above 0
    last_p, last_q, last_n = np.zeros(T), np.zeros(T), np.full(T, np.nan)
    parts = []
    for pc_, n_, q_ in zip(pis, noms, qs):
        cov = ~np.isnan(pc_)
        rel = n_ - q_
        inc = np.where(np.isnan(prev_rel), rel, rel - prev_rel)
        parts.append((cov, pc_, inc, rel))
        new = cov & np.isnan(first_p)
        first_p, first_rel = np.where(new, pc_, first_p), np.where(new, rel, first_rel)
        first_q = np.where(new, q_, first_q)
        pos_q = np.where(cov & (np.nan_to_num(pc_) > 1e-9), q_, pos_q)
        prev_rel = np.where(cov, rel, prev_rel)
        last_p = np.where(cov, np.nan_to_num(pc_), last_p)
        last_q = np.where(cov, q_, last_q)
        last_n = np.where(cov, n_, last_n)
    step, later = np.zeros(T), np.full(T, np.nan)
    spreads = []                                     # (cov, L_i - L_{i+1}, rel_i) per call, for the relief cap
    next_l = last_p.copy()
    for cov, pc, inc, rel in reversed(parts):
        later = np.fmax(later, pc)
        step += np.where(cov, np.nan_to_num(later) * np.where(cov, inc, 0.0), 0.0)
        li = np.nan_to_num(later)
        spreads.append((cov, np.where(cov, li - next_l, 0.0), rel))
        next_l = np.where(cov, li, next_l)
    done = ~np.isnan(last_n)
    step += np.where(done, last_p * np.where(done, Nm - last_n, 0.0), 0.0)
    P1 = np.nan_to_num(later)
    if cap == "zero":
        hit = done & (last_p <= 1e-9)
    elif cap == "below":
        hit = done & (P1 > last_p + 1e-9)
    elif cap is None:
        hit = np.zeros(T, bool)
    else:
        raise ValueError(f"cap must be 'below', 'zero' or None, not {cap!r}")
    today = np.where(done, last_p * np.where(done, Nm - last_q, 0.0), 0.0)
    if cap == "zero":
        lim = P1 * np.where(hit, Nm - np.nan_to_num(pos_q), 0.0)
        step = np.where(hit, np.maximum(0.0, np.minimum(step, lim)), step)
    elif cap == "below":
        met = Nm - np.nan_to_num(pos_q)
        prem = np.zeros(T)
        for cov, w, rel in spreads:
            prem += np.where(cov, w * np.maximum(0.0, np.minimum(np.nan_to_num(rel), met)), 0.0)
        step = np.where(hit, today + prem, step)
    return step, first_p, first_rel, last_p, last_q, done, first_q, pos_q


def _feas_files():
    return [*D.FEASIBILITY_FILES, *[f for f in [N.DM.FEAS, N.FEAS_M5, N.FEAS_150] if f.exists()],
            *sorted((N.OVN / "feas").glob("*.json"))]


def window(run: str) -> dict:
    from sim import physics
    from sim.data import load_config, load_slice
    from sim.forecasts import PriceForecast

    cell = N.find_cell(run)
    if cell is None:
        raise D.CheckFailed(f"{run}: no cell")
    roster = list(cell["roster"])
    cfg = load_config(N.cfg_path(run))
    s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
    iso = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in s.ts_utc]
    pos = {t: i for i, t in enumerate(iso)}
    T = len(iso)
    h = cell["headroom_mw"]
    R = N.REPO / "runs" / run
    lines = (R / f"exchange_h{h}_rinfo" / "trace.jsonl").read_text(encoding="utf-8").splitlines()

    final, binding, aggs, noms, sref, net, mandate = {}, [], {}, {}, {}, {}, {}
    deadline = {}
    for raw in lines[1:]:
        m = json.loads(raw)
        typ, p = m["msg_type"], m["payload"]
        if typ == "peak_price" and p.get("final"):
            final[p["call_id"]] = p
        elif typ == "nomination_aggregate" and p.get("binding"):
            binding.append(p["call_id"])
            aggs[p["call_id"]] = p
            deadline[p["call_id"]] = m["t_utc"]
        elif typ == "nominate":
            noms[(m.get("from") or m.get("sender"), p["call_id"])] = (m["t_utc"], p)      # the last one before the gate is binding
        elif typ == "selfish_reference":
            sref[(p["asset_id"].split("/")[0], p["call_id"])] = p
        elif typ == "telemetry":
            net.setdefault(p["asset_id"].split("/")[0], np.zeros(T))[pos[m["t_utc"]]] = p.get("net_kw", 0.0)
        elif typ == "register":
            mandate[p["agent_id"]] = p["mandate"]
    binding = [c for c in binding if c in final]
    for what, ids in (("telemetry", list(net)), ("register", list(mandate))):
        D.check_roster(roster, ids, f"{run} {what}")

    def arr(win_start, vals):
        a = np.full(T, np.nan)
        k = pos[win_start]
        a[k:k + len(vals)] = vals
        return a

    # rule 1p's reference at each binding gate, over the whole slice (d56_resettle.load_cell's inputs)
    idx = np.array([[str(x) for x in s.ids].index(a) for a in roster])
    fleet = physics.Fleet.from_slice(s, idx)
    a = s.agent
    pv, curt, load = np.nan_to_num(a["pv"][idx]), a["curt"][idx], a["load"][idx]
    avail, base = pv - curt, pv - curt - load
    wear = float(cfg["wear_usd_mwh"])
    max_kw = np.array([float(mandate[x]["max_kw"]) for x in roster])
    prices = PriceForecast(s.ts_utc, s.system["price_da"], cfg["slice_tz"], cfg["price_publish_hour_local"])
    summ = json.loads((R / "summary.json").read_text())
    log = summ["exchange_by_headroom_mw"][str(h)]["price_mode"]["log"]
    plans = {}
    Q = {}
    for c in binding:
        rows = [x for x in log if x["call_id"] == c and not x.get("closing")]
        t_gate = rows[0]["t_utc"] if rows else deadline[c]
        _, fc = prices.at(pos[t_gate])
        key = np.asarray(fc, float).tobytes()
        Qc = np.zeros((len(roster), T))
        for j in range(len(roster)):
            if (key, j) not in plans:
                q = D.selfish_site_net(max_kw[j], 0.8 * fleet.CAP[j], fleet.S0[j], fc, avail[j], base[j], wear, D.H)
                if q is None:
                    raise D.CheckFailed(f"{run}: 1p planner has no plan for {roster[j]}")
                plans[(key, j)] = q
            Qc[j] = plans[(key, j)]
        Q[c] = Qc

    checks = {"binding_calls": binding, "agg_max_err_kw": 0.0, "ref_max_err_kw": None, "late_nominations": 0}
    K = D.K
    today, ri = np.zeros(len(roster)), np.zeros(len(roster))
    pi = {c: arr(final[c]["window"]["start_utc"], final[c]["price_usd_mwh"]) for c in binding}
    nom_arr = {}
    for c in binding:
        tot = np.zeros(T)
        for j, aid in enumerate(roster):
            if (aid, c) not in noms:
                raise D.CheckFailed(f"{run}: {aid} has no nomination for binding call {c}")
            t_n, pn = noms[(aid, c)]
            checks["late_nominations"] += int(t_n >= deadline[c])
            nom_arr[(aid, c)] = arr(pn["window"]["start_utc"], pn["kw"])
            tot = tot + np.nan_to_num(nom_arr[(aid, c)])
        agg = arr(aggs[c]["window"]["start_utc"], aggs[c]["kw"])
        ok = ~np.isnan(agg)
        checks["agg_max_err_kw"] = max(checks["agg_max_err_kw"], float(np.abs(tot[ok] - agg[ok]).max()))
        for j, aid in enumerate(roster):
            if (aid, c) in sref:
                q = arr(sref[(aid, c)]["window"]["start_utc"], sref[(aid, c)]["net_kw"])
                okq = ~np.isnan(q)
                err = float(np.abs(q[okq] - Q[c][j][okq]).max())
                checks["ref_max_err_kw"] = max(checks["ref_max_err_kw"] or 0.0, err)
    if checks["late_nominations"]:
        raise D.CheckFailed(f"{run}: {checks['late_nominations']} nominations at or after their gate")
    if checks["agg_max_err_kw"] > TOL_AGG_KW:
        raise D.CheckFailed(f"{run}: nominations vs binding aggregate {checks['agg_max_err_kw']:.4f} kW")
    if checks["ref_max_err_kw"] is not None and checks["ref_max_err_kw"] > TOL_REF_KW:
        raise D.CheckFailed(f"{run}: 1p reference vs in-run selfish_reference {checks['ref_max_err_kw']:.6g} kW")

    for j, aid in enumerate(roster):
        Nm = net[aid]
        prev_rel = np.full(T, np.nan)
        last_p, last_q, last_n = np.zeros(T), np.zeros(T), np.full(T, np.nan)
        for c in binding:
            cov = ~np.isnan(pi[c])
            pc = np.nan_to_num(pi[c])
            rel = nom_arr[(aid, c)] - Q[c][j]
            first = cov & np.isnan(prev_rel)
            later = cov & ~np.isnan(prev_rel)
            ri[j] += float(pc[first] @ rel[first]) * K
            ri[j] += float(pc[later] @ (rel - prev_rel)[later]) * K
            prev_rel = np.where(cov, rel, prev_rel)
            last_p = np.where(cov, pc, last_p)
            last_q = np.where(cov, Q[c][j], last_q)
            last_n = np.where(cov, nom_arr[(aid, c)], last_n)
        done = ~np.isnan(last_n)
        ri[j] += float(last_p[done] @ (Nm - last_n)[done]) * K
        pr = done & (last_p > 1e-9)
        today[j] = float(last_p[pr] @ (Nm - last_q)[pr]) * K

    # Nov 3, owner item 2 (research track, design set, report only): the max rule. As the relief-increment rule,
    # with each call's relief increment paid at the highest final price among that call and the later binding calls
    # that also price the step (with two calls: first-call relief at max(p_A, p_B)); the metered deviation from the
    # last nomination stays at the last call's price. Fail-closed identities, per step: where one call prices the
    # step it equals today's 1p; where two do, today's 1p + max(0, p_A - p_B)(N_A - Q_A).
    ncov = np.zeros(T, int)
    for c in binding:
        ncov += ~np.isnan(pi[c])
    checks["steps_3plus_calls"] = int((ncov >= 3).sum())
    checks["max_identity_err_usd"] = 0.0
    mx = np.zeros(len(roster))
    for j, aid in enumerate(roster):
        Nm = net[aid]
        step, first_p, first_rel, last_p, last_q, done, first_q, pos_q = max_first_call_steps(
            [pi[c] for c in binding], [nom_arr[(aid, c)] for c in binding], [Q[c][j] for c in binding], Nm)
        mx[j] = float(step.sum()) * K
        tstep = np.where(done & (last_p > 1e-9), last_p * np.where(done, Nm - last_q, 0.0), 0.0)
        want = tstep + np.where(ncov == 2, np.maximum(0.0, np.nan_to_num(first_p) - last_p)
                                * np.nan_to_num(first_rel), 0.0)
        # relief cap (owner, Nov 4): two calls, p_B < p_A: p_B (Nm - Q_B) + (p_A - p_B) max(0, min(N_A - Q_A, Nm - Q_j))
        z2 = (ncov == 2) & done & (np.nan_to_num(first_p) > last_p + 1e-9)
        want = np.where(z2, last_p * (Nm - last_q) + (np.nan_to_num(first_p) - last_p) * np.maximum(0.0, np.minimum(
            np.nan_to_num(first_rel), Nm - np.nan_to_num(pos_q))), want)
        sel = (ncov == 1) | (ncov == 2)
        if sel.any():
            checks["max_identity_err_usd"] = max(checks["max_identity_err_usd"],
                                                 float(np.abs(step - want)[sel].max()) * K)
    if checks["max_identity_err_usd"] > TOL_PAY_USD:
        raise D.CheckFailed(f"{run}: max rule identity, max error ${checks['max_identity_err_usd']:.6g}")

    cell_net = np.array(cell["rules"]["1p"]["net"])
    checks["today_rebuild_max_err_usd"] = float(np.abs(today - cell_net).max())
    if checks["today_rebuild_max_err_usd"] > TOL_PAY_USD:
        raise D.CheckFailed(f"{run}: today's 1p rebuilt vs cell, max error ${checks['today_rebuild_max_err_usd']:.6g}")

    ag = cell["agents"]
    e = np.array([x["e"] for x in ag])
    alone = np.array([x["alone"] for x in ag])
    gap = np.array([x["components"]["da_rt_gap"] for x in ag])
    tol = D.tolerance(alone)

    def score(pay):
        short = e - pay
        sh = (short - gap) > tol + D.EPS_USD
        hm = short > tol + D.EPS_USD
        return {"operator_payout_usd": float(pay.sum()),
                "settlement_harmed": int(sh.sum()), "settlement_harm_usd": float((short - gap)[sh].sum()),
                "settlement_harmed_agents": [roster[i] for i in np.flatnonzero(sh)],
                "harmed": int(hm.sum()), "harm_usd": float(short[hm].sum()),
                "agents_paid_less": int((pay < -1e-9).sum())}

    # step 6 (Nov 2): for every agent settlement-harmed under either rule, the relief-increment change
    # (RI - today) split on the steps both calls cover (two binding calls A, B): re-pricing of A's relief measured
    # against B's reference, (p_A - p_B)(N_A - Q_B), and re-pricing of the reference update, (p_A - p_B)(Q_B - Q_A);
    # "other" is the rest (steps only one call prices). By local day, with the relief-weighted price move.
    diag = []
    if len(binding) == 2:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(cfg["slice_tz"])
        day = np.array([t.astimezone(tz).date().isoformat() for t in s.ts_utc])
        A, B = binding
        both = ~np.isnan(pi[A]) & ~np.isnan(pi[B])
        dp = np.nan_to_num(pi[A]) - np.nan_to_num(pi[B])
        sh_any = set(score(cell_net)["settlement_harmed_agents"]) | set(score(ri)["settlement_harmed_agents"])
        for j, aid in enumerate(roster):
            if aid not in sh_any:
                continue
            NA, QA, QB = nom_arr[(aid, A)], Q[A][j], Q[B][j]
            rep = np.where(both, dp * (NA - QB), 0.0) * K
            upd = np.where(both, dp * (QB - QA), 0.0) * K
            relA = np.where(both, NA - QA, 0.0)
            w = np.abs(relA)
            diag.append({
                "agent": aid, "settlement_harm_today_usd": float(e[j] - cell_net[j] - gap[j]),
                "settlement_harm_ri_usd": float(e[j] - ri[j] - gap[j]), "tol_usd": float(tol[j]),
                "ri_minus_today_usd": float(ri[j] - cell_net[j]),
                "repricing_usd": float(rep.sum()), "reference_update_repriced_usd": float(upd.sum()),
                "other_usd": float(ri[j] - cell_net[j] - rep.sum() - upd.sum()),
                "by_local_day": {d: {"repricing_usd": float(rep[day == d].sum()),
                                     "reference_update_repriced_usd": float(upd[day == d].sum())}
                                 for d in sorted(set(day[both]))},
                "first_call_relief_mwh": float(relA.sum() * D.H / 1000.0),
                "relief_weighted_price_move_usd_mwh": float((dp * w)[both].sum() / w[both].sum()) if w[both].sum() else None,
                "ref_shift_kw_steps": float(np.abs(np.where(both, QB - QA, 0.0)).sum())})
    return {"run": run, "feasible": bool(cell.get("feasible", True)), "checks": checks, "harm_split": diag,
            "today_1p": score(cell_net), "relief_increment": score(ri), "max_first_call": score(mx),
            "per_agent": [{"agent": aid, "today_usd": float(cell_net[j]), "relief_increment_usd": float(ri[j]),
                           "max_first_call_usd": float(mx[j])}
                          for j, aid in enumerate(roster)]}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(N.REPO / "runs" / "nov02" / "item5"))
    ap.add_argument("--windows", nargs="*")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    files = D.FEASIBILITY_FILES
    D.FEASIBILITY_FILES = _feas_files()
    rc, totals = 0, {}
    try:
        for w in a.windows or (N.D58_WINDOWS + N.OVN_WINDOWS):
            run, _ = N.window_cell(w, 0.0)
            try:
                res = window(run)
            except (D.CheckFailed, FileNotFoundError, KeyError) as err:
                (out / f"{run}.failed.json").write_text(json.dumps({"run": run, "failed": str(err)}, indent=1))
                print(f"{w} {run}: CHECK FAILED: {err}", flush=True)
                rc = 2
                continue
            res["window"] = w
            (out / f"{run}.json").write_text(json.dumps(res, indent=1))
            t, r, x = res["today_1p"], res["relief_increment"], res["max_first_call"]
            for k, v in (("today", t), ("ri", r), ("max", x)):
                totals[k] = totals.get(k, 0.0) + v["operator_payout_usd"]
            print(f"{w} {run}: today ${t['operator_payout_usd']:,.0f} sh {t['settlement_harmed']}/"
                  f"${t['settlement_harm_usd']:,.0f} | RI ${r['operator_payout_usd']:,.0f} sh {r['settlement_harmed']}/"
                  f"${r['settlement_harm_usd']:,.0f} | max ${x['operator_payout_usd']:,.0f} sh {x['settlement_harmed']}/"
                  f"${x['settlement_harm_usd']:,.0f} | checks {res['checks']}", flush=True)
    finally:
        D.FEASIBILITY_FILES = files
    print("totals:", {k: round(v, 2) for k, v in totals.items()}, flush=True)
    if not a.windows and rc == 0:          # all 23 windows: Nov 2's totals must reproduce (item 5), else fail closed
        for k, v in (("today", 96173.0), ("ri", 120397.0)):
            if abs(totals[k] - v) > 1.0:
                print(f"CHECK FAILED: {k} total ${totals[k]:,.2f} does not reproduce ${v:,.0f}", flush=True)
                rc = 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
