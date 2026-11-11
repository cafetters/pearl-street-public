"""
Day Nov 1 item 2 (report only, offline, existing traces): where the m = 0 1p settlement-caused harm on jan06, nov14 and
dec07 comes from, and why a margin removes it.

For each agent the cell tool (bench/d56_resettle.py) counts as settlement-harmed, the settlement-caused harm is
    H = lost_arbitrage_net_of_credits - net_1p      (extra_curtailment, imbalance, end_energy are 0 in these cells)
with dn = battery meter (exchange run) - battery meter (selfish run), N/S/Q the exchange-run, selfish-run and 1p
reference site nets, p the settled peak price, da the day-ahead price, K = h/1000. Since N - S = dn here,
    H = ref_error + priced_response + unpriced_response, where
    ref_error         = sum over priced steps of p (Q - S) K           (1p pays on Q, the agent gave up S)
    priced_response   = sum over priced steps of -(da + p) dn K + extra wear
    unpriced_response = sum over unpriced steps of -da dn K + extra wear   (rebound outside the priced steps)
An agent that re-optimised against da + p from the same model as its selfish plan has priced + unpriced <= 0.
ref_error is split further with Q_real, the same plan as Q (same gate, same price version, same horizon) but with
the asset's real efficiency, self-discharge and POI limit instead of 1p's fixed constants:
    ref_model  = sum p (Q - Q_real) K      (1p's ETA 0.95, SD 0, no POI limit)
    ref_timing = sum p (Q_real - S) K      (one plan from the slice start on the gate's prices vs the selfish run's
                                            rolling plans on later price versions)

Run: python -m bench.item7_diag [--out runs/day/item7]
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

from agents.planner import Battery, solve
from bench import d56_resettle as D
from bench import nov01 as N

WINDOWS = ["jan06", "nov14", "dec07"]
MARGINS = ["m0", "m5", "m10"]


def _cell_dir(run):
    for d in (N.CELLS, N.OVN / "cells"):
        if (d / f"{run}.json").exists():
            return d
    raise D.CheckFailed(f"{run}: no cell")


def diagnose(run: str) -> dict:
    from sim import physics
    from sim.data import load_config, load_slice
    from sim.forecasts import PriceForecast

    files = D.FEASIBILITY_FILES
    D.FEASIBILITY_FILES = [*files, *[f for f in [N.DM.FEAS, N.FEAS_M5, N.FEAS_150] if f.exists()],
                           *sorted((N.OVN / "feas").glob("*.json"))]
    try:
        meta, arr = D.load_cell(run, cfg_path=N.cfg_path(run))
    finally:
        D.FEASIBILITY_FILES = files
    cell = json.loads((_cell_dir(run) / f"{run}.json").read_text())
    c, p, priced = arr["c"], arr["p"], arr["priced"]
    roster = meta["roster"]
    cfg = load_config(N.cfg_path(run))
    s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
    iso = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in s.ts_utc]
    pos = {t: i for i, t in enumerate(iso)}
    T = len(iso)
    R = N.REPO / "runs" / run
    h = meta["headroom_mw"]
    _, ex = D._read(R / f"exchange_h{h}_rinfo" / "trace.jsonl", pos, T, True)
    _, sf = D._read(R / "selfish" / "trace.jsonl", pos, T, False)
    sid = [str(x) for x in s.ids]
    idx = np.array([sid.index(a) for a in roster])
    fleet = physics.Fleet.from_slice(s, idx)
    da = s.system["price_da"]
    wear = float(cfg["wear_usd_mwh"])
    K = D.K
    a = s.agent
    pv, curt, load = np.nan_to_num(a["pv"][idx]), a["curt"][idx], a["load"][idx]
    avail, base = pv - curt, pv - curt - load

    # which gate settles each priced step, and its price version (as load_cell)
    gate_of = np.full(T, -1)
    gates = meta["gates"]
    for gi, g in enumerate(gates):
        last = [pp for _, pp in ex["pp"] if pp["call_id"] == g["call_id"]][-1]
        a0 = pos[last["window"]["start_utc"]]
        gate_of[a0:a0 + len(last["price_usd_mwh"])] = gi
    prices = PriceForecast(s.ts_utc, da, cfg["slice_tz"], cfg["price_publish_hour_local"])
    fc_by_gate = [prices.at(pos[g["t_gate"]])[1] for g in gates]
    fc_t = np.array(da, float).copy()          # steps no gate settles: executed on the published price
    for gi, fc in enumerate(fc_by_gate):
        fc_t[gate_of == gi] = np.asarray(fc, float)[gate_of == gi]

    ag = cell["agents"]
    e = np.array([x["e"] for x in ag])
    alone = np.array([x["alone"] for x in ag])
    gap = np.array([x["components"]["da_rt_gap"] for x in ag])
    net = np.array(cell["rules"]["1p"]["net"])
    tol = D.tolerance(alone)
    short = e - net
    sh = (short - gap) > tol + D.EPS_USD
    max_kw = np.array([x["max_kw"] for x in ag])

    rows = []
    for j in np.flatnonzero(sh) if sh.any() else []:
        aid = roster[j]
        mr, ms = ex["meter"][aid], sf["meter"][aid]
        dn = mr - ms
        dw = wear * K * (np.maximum(mr, 0) - np.maximum(ms, 0))
        Nj, Sj, Qj = c["N"][j], c["S"][j], c["Q"][j]
        # Q_real: same gate plans as Q with the asset's real ETA, SD and POI
        Qr = np.zeros(T)
        for gi, fc in enumerate(fc_by_gate):
            steps = np.flatnonzero((gate_of == gi) & priced)
            if not len(steps):
                continue
            b = Battery(P=float(max_kw[j]), CAP=float(fleet.CAP[j]), ETA=float(fleet.ETA[j]), SD=float(fleet.SD[j]),
                        POI=float(fleet.POI[j]), s_term=float(fleet.S0[j]))
            pl = solve(b, 0, float(fleet.S0[j]), np.asarray(fc, float), avail[j], base[j], np.zeros(T), D.H, wear)
            Qr[steps] = (base[j] + pl.d - pl.c - pl.x)[steps]
        pr, up = priced, ~priced
        ref_err = float(p[pr] @ (Qj - Sj)[pr]) * K
        ref_model = float(p[pr] @ (Qj - Qr)[pr]) * K
        priced_resp = float(-(da[pr] + p[pr]) @ dn[pr]) * K + float(dw[pr].sum())
        unpriced_resp = float(-da[up] @ dn[up]) * K + float(dw[up].sum())
        H = float(ag[j]["components"]["lost_arbitrage_net_of_credits"] - net[j])
        # where in the priced steps: added import (dn < 0: the agent charges more than alone in a priced step)
        inc = pr & (dn < -1e-6)
        dec = pr & (dn > 1e-6)
        rows.append({
            "agent": aid, "max_kw": float(max_kw[j]), "eta_real": float(fleet.ETA[j]), "sd_real": float(fleet.SD[j]),
            "poi_kw": float(fleet.POI[j]),
            "settlement_harm_usd": float((short - gap)[j]), "tol_usd": float(tol[j]), "H_usd": H,
            "net_1p_usd": float(net[j]), "lost_arb_usd": float(ag[j]["components"]["lost_arbitrage_net_of_credits"]),
            "ref_error_usd": ref_err, "ref_model_usd": ref_model, "ref_timing_usd": ref_err - ref_model,
            "priced_response_usd": priced_resp, "unpriced_response_usd": unpriced_resp,
            "identity_err_usd": H - (ref_err + priced_resp + unpriced_resp),
            "priced_steps_more_import": int(inc.sum()),
            "priced_more_import_cost_usd": float(-(da[inc] + p[inc]) @ dn[inc]) * K,
            "priced_steps_less_import": int(dec.sum()),
            "priced_less_import_gain_usd": float((da[dec] + p[dec]) @ dn[dec]) * K,
            "mwh_shifted_out_of_priced": float(dn[pr].sum() * D.H / 1000.0),
            "mwh_shifted_unpriced": float(dn[up].sum() * D.H / 1000.0),
            "steps_ref_above_selfish": int((pr & (Qj - Sj > 1e-3)).sum()),
            "steps_ref_below_selfish": int((pr & (Qj - Sj < -1e-3)).sum()),
            "price_at_cap_steps": int((pr & (p >= meta["price_cap_usd_mwh"] - 1e-6)).sum()),
            # the day-ahead prices the agent nominated against: the settling gate's published version
            "forecast_error_usd": float((fc_t - da) @ dn) * K,
            "deficit_at_gate_prices_usd": float(-(fc_t[pr] + p[pr]) @ dn[pr]) * K + float(-fc_t[up] @ dn[up]) * K
                                          + float(dw.sum()),
        })
    tot = lambda k: float(sum(r[k] for r in rows))
    out = {"run": run, "priced_steps": int(priced.sum()), "settled_max_usd_mwh": float(p.max()),
           "settled_mean_usd_mwh": float(p[priced].mean()) if priced.any() else 0.0,
           "price_cap_usd_mwh": meta["price_cap_usd_mwh"], "settlement_harmed": int(sh.sum()),
           "harmed_beyond_tol": int((short > tol + D.EPS_USD).sum()),
           "totals": {k: tot(k) for k in ("settlement_harm_usd", "H_usd", "ref_error_usd", "ref_model_usd",
                                          "ref_timing_usd", "priced_response_usd", "unpriced_response_usd",
                                          "priced_more_import_cost_usd", "priced_less_import_gain_usd",
                                          "forecast_error_usd", "deficit_at_gate_prices_usd")} if rows else {},
           "agents": rows}
    # fleet-wide (all agents): the same decomposition summed, for the margin comparison
    allH = {"ref_error_usd": 0.0, "ref_model_usd": None, "priced_response_usd": 0.0, "unpriced_response_usd": 0.0}
    for j, aid in enumerate(roster):
        mr, ms = ex["meter"][aid], sf["meter"][aid]
        dn = mr - ms
        dw = wear * K * (np.maximum(mr, 0) - np.maximum(ms, 0))
        allH["ref_error_usd"] += float(p[priced] @ (c["Q"][j] - c["S"][j])[priced]) * K
        allH["priced_response_usd"] += float(-(da[priced] + p[priced]) @ dn[priced]) * K + float(dw[priced].sum())
        allH["unpriced_response_usd"] += float(-da[~priced] @ dn[~priced]) * K + float(dw[~priced].sum())
    out["fleet"] = allH
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(N.REPO / "runs" / "day" / "item7"))
    ap.add_argument("--runs", nargs="*")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    runs = a.runs or [f"h2h_c_{w}_h100_{m}" for w in WINDOWS for m in MARGINS]
    for r in runs:
        try:
            res = diagnose(r)
        except (D.CheckFailed, FileNotFoundError) as err:
            print(f"{r}: {err}")
            continue
        res["two_settlement"] = two_settlement(r)
        try:
            res["per_agent"] = per_agent(r)
        except D.CheckFailed as err:
            res["per_agent"] = {"error": str(err)}
        (out / f"{r}.json").write_text(json.dumps(res, indent=1))
        print(r, json.dumps({k: res[k] for k in ("settlement_harmed", "harmed_beyond_tol", "totals", "fleet")}))
    return 0



# ---------------------------------------------------------------- counterfactual: settle each binding call on its own
def two_settlement(run: str) -> dict:
    """Same run, same behaviour, settled call by call: the first binding call covering a step pays its price on that
    call's nomination against its 1p reference; each later binding call pays its price on the change in nomination;
    the metered deviation from the last nomination pays the last call's price (as today). With one binding call this
    is today's 1p exactly. At m = 0 the agent's incentive at each call is unchanged (the earlier payments are fixed by
    then), so the run's behaviour is what this rule would produce. Uses only nominate, selfish_reference, peak_price
    and telemetry lines (no D62 field)."""
    import datetime as dt
    cell = json.loads((_cell_dir(run) / f"{run}.json").read_text())
    roster = cell["roster"]
    R = N.REPO / "runs" / run
    h = cell["headroom_mw"]
    lines = (R / f"exchange_h{h}_rinfo" / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    head = json.loads(lines[0])
    t0 = None
    final, binding, noms, sref, net = {}, [], {}, {}, {}
    for s in lines[1:]:
        m = json.loads(s)
        typ, p = m["msg_type"], m["payload"]
        if typ == "peak_price" and p["final"]:
            final[p["call_id"]] = p
        elif typ == "nomination_aggregate" and p["binding"]:
            binding.append(p["call_id"])
        elif typ == "nominate":
            noms[(m["from"], p["call_id"])] = p          # the last one before the gate is the binding one
        elif typ == "selfish_reference":
            sref[(p["asset_id"].split("/")[0], p["call_id"])] = p
        elif typ == "telemetry":
            t0 = t0 or m["t_utc"]
            net.setdefault(p["asset_id"].split("/")[0], {})[m["t_utc"]] = p.get("net_kw", 0.0)
    ts = lambda x: dt.datetime.fromisoformat(x.replace("Z", "+00:00"))
    step = lambda x: int((ts(x) - ts(t0)).total_seconds() // 900)
    T = max(len(v) for v in net.values())
    K = D.K

    def arr(win_start, vals):
        a = np.full(T, np.nan)
        k = step(win_start)
        a[k:k + len(vals)] = vals
        return a

    pi = {c: arr(final[c]["window"]["start_utc"], final[c]["price_usd_mwh"]) for c in binding}
    pay_now, pay_two, pay_two_q = np.zeros(len(roster)), np.zeros(len(roster)), np.zeros(len(roster))
    pay_hold = np.zeros(len(roster))
    drop = {"mwh_weighted_drop_usd_mwh": 0.0, "mwh": 0.0}
    for j, aid in enumerate(roster):
        Nm = np.array([net[aid][k] for k in sorted(net[aid])])
        last_pi, last_q, last_nom = np.zeros(T), np.zeros(T), np.full(T, np.nan)
        first_pi, first_nom, first_q = np.zeros(T), np.full(T, np.nan), np.zeros(T)
        for c in binding:
            pc = np.nan_to_num(pi[c])
            cov = ~np.isnan(pi[c])
            nom = arr(noms[(aid, c)]["window"]["start_utc"], noms[(aid, c)]["kw"])
            q = arr(sref[(aid, c)]["window"]["start_utc"], sref[(aid, c)]["net_kw"])
            first = cov & np.isnan(last_nom)
            later = cov & ~np.isnan(last_nom)
            pay_two[j] += float(pc[first] @ (nom - q)[first]) * K
            pay_two[j] += float(pc[later] @ (nom - last_nom)[later]) * K
            first_pi = np.where(first, pc, first_pi)
            first_nom = np.where(first, nom, first_nom)
            first_q = np.where(first, q, first_q)
            last_pi = np.where(cov, pc, last_pi)
            last_q = np.where(cov, q, last_q)
            last_nom = np.where(cov, nom, last_nom)
        done = ~np.isnan(last_nom)
        pay_two[j] += float(last_pi[done] @ (Nm - last_nom)[done]) * K
        pay_now[j] = float(last_pi[done] @ (Nm - last_q)[done]) * K
        # variant: every call's leg against the last call's reference (the best-informed 1p plan; exogenous to the
        # agent, so incentives are unchanged): today's pay + (first price - last price) x (first nomination - Q_last)
        pay_two_q[j] = pay_now[j] + float((first_pi - last_pi)[done] @ (first_nom - last_q)[done]) * K
        # variant: response committed at the first call is held at that call's price, against that call's own
        # reference; later calls settle at their price as today (today's pay + (pi_first - pi_last)(N_first - Q_first))
        pay_hold[j] = pay_now[j] + float((first_pi - last_pi)[done] @ (first_nom - first_q)[done]) * K
        sh = (first_nom - last_q)[done]
        dp_ = (first_pi - last_pi)[done]
        pos_ = sh > 0
        drop["mwh_weighted_drop_usd_mwh"] += float(dp_[pos_] @ sh[pos_]) * D.H / 1000.0
        drop["mwh"] += float(sh[pos_].sum()) * D.H / 1000.0
    net_cell = np.array(cell["rules"]["1p"]["net"])
    ag = cell["agents"]
    e = np.array([x["e"] for x in ag])
    alone = np.array([x["alone"] for x in ag])
    gap = np.array([x["components"]["da_rt_gap"] for x in ag])
    tol = D.tolerance(alone)
    res = {"run": run, "binding_calls": binding,
           "rebuild_max_err_usd": float(np.abs(pay_now - net_cell).max())}
    res["first_call_relief_mwh"] = drop["mwh"]
    res["price_drop_on_first_call_relief_usd_mwh"] = drop["mwh_weighted_drop_usd_mwh"] / drop["mwh"] if drop["mwh"] else None
    for name, pay in (("today_1p", pay_now), ("call_by_call", pay_two), ("call_by_call_last_ref", pay_two_q),
                      ("hold_first_price", pay_hold)):
        short = e - pay
        sh = (short - gap) > tol + D.EPS_USD
        hm = short > tol + D.EPS_USD
        res[name] = {"agents": [roster[i] for i in np.flatnonzero(sh)], "settlement_harmed": int(sh.sum()), "settlement_harm_usd": float((short - gap)[sh].sum()),
                     "harmed": int(hm.sum()), "harm_usd": float(short[hm].sum()),
                     "operator_payout_usd": float(pay.sum())}
    return res



# ---------------------------------------------------------------- per harmed agent: steps, settlement term, cause
def per_agent(run: str) -> dict:
    """For each settlement-harmed agent under today's 1p: the hold-first-price correction (pi_A - pi_B)(N_A - Q_A) on
    the steps both binding calls cover, split into pure re-pricing (pi_A - pi_B)(N_A - Q_B) and re-pricing of the
    reference update (pi_A - pi_B)(Q_B - Q_A), by local day, and the steps that carry it. Two binding calls (A, B)
    with B's window inside A's, as in all three windows."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    cell = json.loads((_cell_dir(run) / f"{run}.json").read_text())
    roster = cell["roster"]
    lines = (N.REPO / "runs" / run / f"exchange_h{cell['headroom_mw']}_rinfo" / "trace.jsonl").read_text(
        encoding="utf-8").splitlines()
    tz = ZoneInfo(json.loads(lines[0])["exchange"]["tz"])
    final, binding, noms, sref = {}, [], {}, {}
    for s_ in lines[1:]:
        m = json.loads(s_)
        typ, p = m["msg_type"], m["payload"]
        if typ == "peak_price" and p["final"]:
            final[p["call_id"]] = p
        elif typ == "nomination_aggregate" and p["binding"]:
            binding.append(p["call_id"])
        elif typ == "nominate":
            noms[(m["from"], p["call_id"])] = p
        elif typ == "selfish_reference":
            sref[(p["asset_id"].split("/")[0], p["call_id"])] = p
    if len(binding) != 2:
        raise D.CheckFailed(f"{run}: {len(binding)} binding calls; this view needs two")
    A, B = binding
    pa, pb = np.array(final[A]["price_usd_mwh"]), np.array(final[B]["price_usd_mwh"])
    off = len(pa) - len(pb)
    if final[A]["window"]["end_utc"] != final[B]["window"]["end_utc"] or off < 0:
        raise D.CheckFailed(f"{run}: B's window is not the tail of A's")
    t0 = dt.datetime.fromisoformat(final[B]["window"]["start_utc"].replace("Z", "+00:00"))
    when = [t0 + dt.timedelta(minutes=15 * i) for i in range(len(pb))]
    day = np.array([w.astimezone(tz).date().isoformat() for w in when])
    pa = pa[off:]
    K = D.K
    ag = cell["agents"]
    e = np.array([x["e"] for x in ag])
    alone = np.array([x["alone"] for x in ag])
    gap = np.array([x["components"]["da_rt_gap"] for x in ag])
    net = np.array(cell["rules"]["1p"]["net"])
    tol = D.tolerance(alone)
    sh = (e - net - gap) > tol + D.EPS_USD
    rows = []
    for j in np.flatnonzero(sh):
        aid = roster[j]
        NA = np.array(noms[(aid, A)]["kw"][off:])
        QA = np.array(sref[(aid, A)]["net_kw"][off:])
        QB = np.array(sref[(aid, B)]["net_kw"])
        rep = (pa - pb) * (NA - QB) * K
        upd = (pa - pb) * (QB - QA) * K
        tot = rep + upd
        big = np.flatnonzero(tot > 0.01)
        order = big[np.argsort(-tot[big])]
        cum, top = 0.0, []
        for i in order:                         # the fewest steps that carry 80% of the positive correction
            top.append(int(i))
            cum += tot[i]
            if cum >= 0.8 * tot[tot > 0].sum():
                break
        rows.append({
            "agent": aid, "settlement_harm_usd": float(e[j] - net[j] - gap[j]), "tol_usd": float(tol[j]),
            "today_net_1p_usd": float(net[j]), "hold_correction_usd": float(tot.sum()),
            "repricing_usd": float(rep.sum()), "reference_update_repriced_usd": float(upd.sum()),
            "by_local_day_usd": {d: float(tot[day == d].sum()) for d in sorted(set(day))},
            "steps_carrying_80pct": len(top),
            "first_step_utc": when[min(top)].strftime("%Y-%m-%dT%H:%MZ") if top else None,
            "last_step_utc": when[max(top)].strftime("%Y-%m-%dT%H:%MZ") if top else None,
            "local_days_of_those_steps": sorted({day[i] for i in top}),
            "mean_price_drop_on_those_steps_usd_mwh": float(np.mean((pa - pb)[top])) if top else None,
            "cleared_by_hold": bool(e[j] - net[j] - tot.sum() - gap[j] <= tol[j] + D.EPS_USD)})
    return {"run": run, "calls": [A, B], "B_start_utc": final[B]["window"]["start_utc"], "agents": rows}


if __name__ == "__main__":
    sys.exit(main())
