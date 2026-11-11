"""
Literature mapping, Nov 6 (docs/research/2026-11-06-literature-mapping.md; report only, decides nothing).

Re-settles saved arm C traces offline, behavior held fixed, under:
  today      today's 1p: p_k (N_metered - Q_k) on steps the last binding call priced above 0
  ri         the relief-increment rule (Nov 1 item 2; = the review's per-gate incremental settlement):
             p_1 (N_1 - Q_1) + sum_{i>=2} p_i [(N_i - Q_i) - (N_{i-1} - Q_{i-1})] + p_k (N_metered - N_k)
  frozen     the literal two-settlement reading: the reference is a forward position assigned once, at the first
             binding call that prices the step, and never updated:
             p_1 (N_1 - Q_1) + sum_{i>=2} p_i (N_i - N_{i-1}) + p_k (N_metered - N_k)
             (= ri + sum_{i>=2} p_i (Q_i - Q_{i-1}), checked per agent)
  max        D76's max rule with the relief cap, bench.item7_resettle.max_first_call_steps(cap="below")
and, on the design set only, delivery-scaled payment curves (review rec. 4) on today's 1p:
  per agent and window, at the last binding call's price on its priced steps, V_n = sum p (N_k - Q_k) K (nominated
  relief value), V_m = sum p (N_metered - Q_k) K (= today's pay), delta = V_m / V_n where V_n > 0;
  pay = f(delta) V_n, with sthlmflex f = 1 for delta >= 0.8, linear to 0 at 0.4; IntraFlex f = 1 for delta >= 0.95,
  1 - 0.03 (95 - 100 delta) down to delta = 0.63, 0 below. Where V_n <= 0 the agent is paid today's 1p.

Inputs:
  design   the 23 m = 0 cells of bench.nov01 (as bench.item7_resettle), read from the main checkout's runs/ (--repo).
  holdout  the 24 half-A runs of the max-rule holdout (m = 0, max rule in-run, 20b77d0), read from --holdout. At
           m = 0 our agents re-plan on the current price only, so re-settling these traces under another rule is
           exact for our agents (behavior held fixed); it is not for an agent that anticipates the rule.
Harm, tolerance and the settlement-caused split are bench.d56_resettle's (as item7): shortfall = e - pay; harmed if
shortfall - DA vs RT gap > max(1% |alone|, $10) + $1e-6.

Fail-closed checks per window (as item7, plus):
  - roster from each cell; agent order equals the roster; every roster agent has telemetry and a register line;
  - each call's nominations sum to its binding aggregate (<= 0.01 kW); none at or after its gate;
  - Q_i recomputed (sim.reference via bench.d56_resettle) equals in-run selfish_reference lines where present
    (<= 1e-6 kW);
  - design: today's 1p rebuilt equals the cell's 1p net per agent (<= $1e-6);
  - holdout: the max rule computed here equals each agent's in-run peak_settle (the cell's c.per_agent.in_run_usd)
    within $0.001 (the holdout's tolerance (c): trace prices are rounded to 6 decimals);
  - frozen - ri equals sum_{i>=2} p_i (Q_i - Q_{i-1}) K per agent (<= $1e-6);
  - design totals reproduce Nov 2's today $96,173 and ri $120,397 within $1 when all 23 windows run.

Writes only under --out (default runs/lit/ in this worktree). Reads the other checkouts, never writes there.
Run (from this worktree's root, one process at a time):  PYTHONPATH=. python -u -m bench.lit_settle design|holdout
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

from bench import d56_resettle as D
from bench import d58_margin as DM
from bench import nov01 as N
from bench.item7_resettle import max_first_call_steps

TOL_AGG_KW = 0.01
TOL_REF_KW = 1e-6
TOL_PAY_USD = 1e-6
TOL_INRUN_USD = 1e-3
HERE = Path(__file__).resolve().parents[1]
DEFAULT_REPO = HERE.parent / "pearl-street"
DEFAULT_HOLDOUT = HERE.parent / "pearl-street-holdout"
HOLDOUT_TAGS = ["jan04", "jan14", "feb08", "feb06", "mar12", "mar06", "apr08", "apr04", "may27", "may31", "jun16",
                "jun08", "jul11", "jul09", "aug05", "aug03", "sep15", "sep03", "oct09", "oct02", "nov26", "nov11",
                "dec05", "dec09"]
CheckFailed = D.CheckFailed


# ---------------------------------------------------------------------------------------------- pure rule functions
def rule_steps(pis, noms, qs, Nm) -> dict:
    """Per-step pay ($/MWh x kW; times K gives $) for one agent under today, ri, frozen and max. pis, noms, qs: one
    array per binding call in closing order (final price, NaN where the call doesn't price the step; binding
    nomination; 1p reference); Nm: metered site net. Also returns the arrays the curves and identities need."""
    T = len(Nm)
    ri, fr = np.zeros(T), np.zeros(T)
    prev_rel, prev_n = np.full(T, np.nan), np.full(T, np.nan)
    last_p, last_q, last_n = np.zeros(T), np.zeros(T), np.full(T, np.nan)
    q_update = np.zeros(T)                      # sum_{i>=2} p_i (Q_i - Q_{i-1}) per step
    prev_q = np.full(T, np.nan)
    for pc_, n_, q_ in zip(pis, noms, qs):
        cov = ~np.isnan(pc_)
        if (cov & (np.isnan(n_) | np.isnan(q_))).any():
            raise CheckFailed("a priced step has no nomination or no reference")
        pc = np.nan_to_num(pc_)
        rel = n_ - q_
        first = cov & np.isnan(prev_rel)
        later = cov & ~np.isnan(prev_rel)
        ri += np.where(first, pc * np.nan_to_num(rel), 0.0) + np.where(later, pc * np.nan_to_num(rel - prev_rel), 0.0)
        fr += np.where(first, pc * np.nan_to_num(rel), 0.0) + np.where(later, pc * np.nan_to_num(n_ - prev_n), 0.0)
        q_update += np.where(later, pc * np.nan_to_num(q_ - prev_q), 0.0)
        prev_rel = np.where(cov, rel, prev_rel)
        prev_n = np.where(cov, n_, prev_n)
        prev_q = np.where(cov, q_, prev_q)
        last_p = np.where(cov, pc, last_p)
        last_q = np.where(cov, q_, last_q)
        last_n = np.where(cov, n_, last_n)
    done = ~np.isnan(last_n)
    dev = np.where(done, last_p * np.where(done, Nm - last_n, 0.0), 0.0)
    ri, fr = ri + dev, fr + dev
    pr = done & (last_p > 1e-9)
    today = np.where(pr, last_p * np.where(pr, Nm - last_q, 0.0), 0.0)
    mx = max_first_call_steps(pis, noms, qs, Nm)[0]
    return {"today": today, "ri": ri, "frozen": fr, "max": mx, "q_update": q_update,
            "last_p": last_p, "last_q": last_q, "last_n": last_n, "priced": pr}


def curve_factor(delta: float, curve: str) -> float:
    """Share of the nominated relief value paid at delivery ratio delta (review Q4, JRC 2022 as quoted)."""
    if curve == "sthlmflex":
        if delta >= 0.8:
            return 1.0
        if delta <= 0.4:
            return 0.0
        return (delta - 0.4) / 0.4
    if curve == "intraflex":
        pts = 100.0 * delta
        if pts >= 95.0:
            return 1.0
        if pts < 63.0:
            return 0.0
        return 1.0 - 0.03 * (95.0 - pts)
    raise ValueError(f"unknown curve {curve!r}")


def curve_pay(st: dict, Nm, curve: str) -> tuple[float, float | None]:
    """($ paid, delta) for one agent and window under a delivery curve on today's 1p (see the module docstring)."""
    pr, p, q, n = st["priced"], st["last_p"], st["last_q"], st["last_n"]
    vn = float(p[pr] @ (n - q)[pr]) * D.K
    vm = float(p[pr] @ (Nm - q)[pr]) * D.K
    if vn <= 1e-9:
        return vm, None
    delta = vm / vn
    return curve_factor(delta, curve) * vn, delta


def score(pay, e, alone, gap, roster) -> dict:
    """bench.d56_resettle's harm and settlement-caused split (as bench.item7_resettle.window's score)."""
    tol = D.tolerance(alone)
    short = e - pay
    sh = (short - gap) > tol + D.EPS_USD
    hm = short > tol + D.EPS_USD
    return {"operator_payout_usd": float(pay.sum()),
            "settlement_harmed": int(sh.sum()), "settlement_harm_usd": float((short - gap)[sh].sum()),
            "settlement_beyond_tol_usd": float((short - gap - tol)[sh].sum()),
            "settlement_harmed_agents": [roster[i] for i in np.flatnonzero(sh)],
            "harmed": int(hm.sum()), "harm_usd": float(short[hm].sum())}


# ------------------------------------------------------------------------------------------------------- loading
def use_repo(root: Path):
    """Point bench.nov01 / d58_margin / d56_resettle's cell lookups at another checkout (read only)."""
    root = Path(root)
    N.REPO, N.OUT, N.CONFIGS, N.OVN = root, root / "runs" / "nov01", root / "configs" / "nov01", root / "runs" / "overnight"
    N.CELLS, N.FEAS_M5, N.FEAS_150 = N.OUT / "cells", N.OUT / "feas_m5.json", N.OUT / "feas_h150.json"
    DM.REPO, DM.OUT, DM.CONFIGS = root, root / "runs" / "d58m", root / "configs" / "d58m"
    DM.CELLS_OUT, DM.FEAS = DM.OUT / "cells", DM.OUT / "feasibility.json"
    D.REPO, D.OUT = root, root / "runs" / "d56"
    D.FEASIBILITY_FILES = [root / "runs" / "h2h" / "feasibility.json", root / "runs" / "d56" / "feasibility_confirm.json"]


def load_window(run_dir: Path, cfg_path: Path, roster: list[str], headroom: int) -> dict:
    """Arrays for one run (as bench.item7_resettle.window): final binding prices, binding nominations, 1p references
    recomputed at each gate and checked against in-run lines, metered site net."""
    from sim import physics
    from sim.data import load_config, load_slice
    from sim.forecasts import PriceForecast

    cfg = load_config(cfg_path)
    s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
    iso = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in s.ts_utc]
    pos = {t: i for i, t in enumerate(iso)}
    T = len(iso)
    lines = (run_dir / f"exchange_h{headroom}_rinfo" / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    head = json.loads(lines[0])
    if list(head.get("roster", roster)) != list(roster):
        raise CheckFailed(f"{run_dir.name}: trace roster differs from the cell's")
    final, binding, aggs, noms, sref, net, mandate, deadline = {}, [], {}, {}, {}, {}, {}, {}
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
            noms[(m.get("from") or m.get("sender"), p["call_id"])] = (m["t_utc"], p)
        elif typ == "selfish_reference":
            sref[(p["asset_id"].split("/")[0], p["call_id"])] = p
        elif typ == "telemetry":
            net.setdefault(p["asset_id"].split("/")[0], np.zeros(T))[pos[m["t_utc"]]] = p.get("net_kw", 0.0)
        elif typ == "register":
            mandate[p["agent_id"]] = p["mandate"]
    binding = [c for c in binding if c in final]
    for what, ids in (("telemetry", list(net)), ("register", list(mandate))):
        D.check_roster(roster, ids, f"{run_dir.name} {what}")

    def arr(win_start, vals):
        a = np.full(T, np.nan)
        k = pos[win_start]
        a[k:k + len(vals)] = vals
        return a

    idx = np.array([[str(x) for x in s.ids].index(a) for a in roster])
    fleet = physics.Fleet.from_slice(s, idx)
    a = s.agent
    pv, curt, load = np.nan_to_num(a["pv"][idx]), a["curt"][idx], a["load"][idx]
    avail, base = pv - curt, pv - curt - load
    wear = float(cfg["wear_usd_mwh"])
    max_kw = np.array([float(mandate[x]["max_kw"]) for x in roster])
    prices = PriceForecast(s.ts_utc, s.system["price_da"], cfg["slice_tz"], cfg["price_publish_hour_local"])
    summ = json.loads((run_dir / "summary.json").read_text())
    log = summ["exchange_by_headroom_mw"][str(headroom)]["price_mode"]["log"]
    plans, Q = {}, {}
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
                    raise CheckFailed(f"{run_dir.name}: 1p planner has no plan for {roster[j]}")
                plans[(key, j)] = q
            Qc[j] = plans[(key, j)]
        Q[c] = Qc

    checks = {"binding_calls": binding, "agg_max_err_kw": 0.0, "ref_max_err_kw": None, "ref_lines": 0,
              "late_nominations": 0}
    pi = {c: arr(final[c]["window"]["start_utc"], final[c]["price_usd_mwh"]) for c in binding}
    nom = {}
    for c in binding:
        tot = np.zeros(T)
        for j, aid in enumerate(roster):
            if (aid, c) not in noms:
                raise CheckFailed(f"{run_dir.name}: {aid} has no nomination for binding call {c}")
            t_n, pn = noms[(aid, c)]
            checks["late_nominations"] += int(t_n >= deadline[c])
            nom[(aid, c)] = arr(pn["window"]["start_utc"], pn["kw"])
            tot = tot + np.nan_to_num(nom[(aid, c)])
            if (aid, c) in sref:
                q = arr(sref[(aid, c)]["window"]["start_utc"], sref[(aid, c)]["net_kw"])
                ok = ~np.isnan(q)
                checks["ref_max_err_kw"] = max(checks["ref_max_err_kw"] or 0.0, float(np.abs(q[ok] - Q[c][j][ok]).max()))
                checks["ref_lines"] += 1
        agg = arr(aggs[c]["window"]["start_utc"], aggs[c]["kw"])
        ok = ~np.isnan(agg)
        checks["agg_max_err_kw"] = max(checks["agg_max_err_kw"], float(np.abs(tot[ok] - agg[ok]).max()))
    if checks["late_nominations"]:
        raise CheckFailed(f"{run_dir.name}: {checks['late_nominations']} nominations at or after their gate")
    if checks["agg_max_err_kw"] > TOL_AGG_KW:
        raise CheckFailed(f"{run_dir.name}: nominations vs binding aggregate {checks['agg_max_err_kw']:.4f} kW")
    if checks["ref_max_err_kw"] is not None and checks["ref_max_err_kw"] > TOL_REF_KW:
        raise CheckFailed(f"{run_dir.name}: 1p reference vs in-run selfish_reference {checks['ref_max_err_kw']:.6g} kW")
    return {"binding": binding, "pi": pi, "nom": nom, "Q": Q, "net": net, "checks": checks}


def settle_window(w: dict, roster: list[str], agents: list[dict], curves: bool) -> dict:
    """Every rule for every agent, the identities, and the scores."""
    if [x["agent"] for x in agents] != list(roster):
        raise CheckFailed("cell agents are not in roster order")
    K = D.K
    pays = {k: np.zeros(len(roster)) for k in ("today", "ri", "frozen", "max")}
    cpay = {c: np.zeros(len(roster)) for c in (("sthlmflex", "intraflex") if curves else ())}
    deltas, frozen_err = [], 0.0
    for j, aid in enumerate(roster):
        Nm = w["net"][aid]
        st = rule_steps([w["pi"][c] for c in w["binding"]], [w["nom"][(aid, c)] for c in w["binding"]],
                        [w["Q"][c][j] for c in w["binding"]], Nm)
        for k in pays:
            pays[k][j] = float(st[k].sum()) * K
        frozen_err = max(frozen_err, abs(pays["frozen"][j] - pays["ri"][j] - float(st["q_update"].sum()) * K))
        for c in cpay:
            cpay[c][j], d = curve_pay(st, Nm, c)
            if c == "sthlmflex":
                deltas.append(d)
    if frozen_err > TOL_PAY_USD:
        raise CheckFailed(f"frozen - ri identity, max error ${frozen_err:.6g}")
    e = np.array([x["e"] for x in agents])
    alone = np.array([x["alone"] for x in agents])
    gap = np.array([x["components"]["da_rt_gap"] for x in agents])
    out = {"rules": {k: score(v, e, alone, gap, roster) for k, v in pays.items()},
           "per_agent": [{"agent": aid, **{k: float(v[j]) for k, v in pays.items()}} for j, aid in enumerate(roster)],
           "frozen_identity_err_usd": frozen_err}
    if curves:
        out["curves"] = {c: score(v, e, alone, gap, roster) for c, v in cpay.items()}
        d = np.array([x for x in deltas if x is not None])
        out["delta"] = {"agents_with_relief": int(len(d)), "below_0_95": int((d < 0.95).sum()),
                        "below_0_8": int((d < 0.8).sum()), "above_1": int((d > 1.0 + 1e-9).sum()),
                        "min": float(d.min()) if len(d) else None, "median": float(np.median(d)) if len(d) else None,
                        "max": float(d.max()) if len(d) else None}
        for j, aid in enumerate(roster):
            out["per_agent"][j].update({c: float(v[j]) for c, v in cpay.items()})
    return out


# ------------------------------------------------------------------------------------------------------- commands
def design(repo: Path, out: Path) -> tuple:
    use_repo(repo)
    D.FEASIBILITY_FILES = [*D.FEASIBILITY_FILES,
                           *[f for f in [DM.FEAS, N.FEAS_M5, N.FEAS_150] if f.exists()],
                           *sorted((N.OVN / "feas").glob("*.json"))]
    rc, rows = 0, []
    for wname in N.D58_WINDOWS + N.OVN_WINDOWS:
        run, cell = N.window_cell(wname, 0.0)
        try:
            if cell is None:
                raise CheckFailed(f"{run}: no cell")
            roster = list(cell["roster"])
            w = load_window(repo / "runs" / run, N.cfg_path(run), roster, int(cell["headroom_mw"]))
            res = settle_window(w, roster, cell["agents"], curves=True)
            err = float(np.abs(np.array([x["today"] for x in res["per_agent"]]) - np.array(cell["rules"]["1p"]["net"])).max())
            if err > TOL_PAY_USD:
                raise CheckFailed(f"{run}: today's 1p rebuilt vs cell, max error ${err:.6g}")
            res["checks"] = {**w["checks"], "today_rebuild_max_err_usd": err}
        except (CheckFailed, FileNotFoundError, KeyError) as e:
            print(f"{wname} {run}: CHECK FAILED: {e}", flush=True)
            (out / f"{run}.failed.json").write_text(json.dumps({"run": run, "failed": str(e)}, indent=1))
            rc = 2
            continue
        res.update({"window": wname, "run": run, "feasible": bool(cell.get("feasible", True))})
        (out / f"{run}.json").write_text(json.dumps(res, indent=1))
        rows.append(res)
        print(_line(wname, res), flush=True)
    tot = _totals(rows)
    if rc == 0 and len(rows) == 23:
        for k, v in (("today", 96173.0), ("ri", 120397.0)):
            if abs(tot[k]["operator_payout_usd"] - v) > 1.0:
                print(f"CHECK FAILED: {k} total does not reproduce ${v:,.0f}", flush=True)
                rc = 2
    return rc, rows, tot


def holdout(hroot: Path, repo_cfg: Path, out: Path) -> tuple:
    rc, rows = 0, []
    for tag in HOLDOUT_TAGS:
        run = f"h2h_c_{tag}_h100_m0_max"
        try:
            obj = json.loads((hroot / "runs" / "holdout" / "cells" / f"{run}.json").read_text())
            cell = obj["cell"]
            roster = list(cell["roster"])
            w = load_window(hroot / "runs" / run, repo_cfg / "configs" / "holdout" / f"{run}.yaml", roster,
                            int(cell["headroom_mw"]))
            res = settle_window(w, roster, cell["agents"], curves=False)
            inrun = {x["agent"]: x["in_run_usd"] for x in obj["c"]["per_agent"]}
            D.check_roster(roster, list(inrun), f"{run} in-run peak_settle")
            err = max(abs(x["max"] - inrun[x["agent"]]) for x in res["per_agent"])
            if err > TOL_INRUN_USD:
                raise CheckFailed(f"{run}: max rule vs in-run peak_settle, max error ${err:.6g}")
            res["checks"] = {**w["checks"], "max_vs_inrun_max_err_usd": err}
        except (CheckFailed, FileNotFoundError, KeyError) as e:
            print(f"{tag} {run}: CHECK FAILED: {e}", flush=True)
            (out / f"{run}.failed.json").write_text(json.dumps({"run": run, "failed": str(e)}, indent=1))
            rc = 2
            continue
        res.update({"window": tag, "run": run, "feasible": bool(cell.get("feasible", True))})
        (out / f"{run}.json").write_text(json.dumps(res, indent=1))
        rows.append(res)
        print(_line(tag, res), flush=True)
    return rc, rows, _totals(rows)


def _line(wname, res) -> str:
    parts = [f"{k} ${v['operator_payout_usd']:,.0f} sh {v['settlement_harmed']}/${v['settlement_harm_usd']:,.0f}"
             for k, v in res["rules"].items()]
    parts += [f"{k} ${v['operator_payout_usd']:,.0f} sh {v['settlement_harmed']}/${v['settlement_harm_usd']:,.0f}"
              for k, v in res.get("curves", {}).items()]
    return f"{wname}: " + " | ".join(parts)


def _totals(rows) -> dict:
    tot = {}
    for r in rows:
        for k, v in [*r["rules"].items(), *r.get("curves", {}).items()]:
            t = tot.setdefault(k, {"operator_payout_usd": 0.0, "settlement_harmed": 0, "settlement_harm_usd": 0.0,
                                   "harmed": 0, "harm_usd": 0.0, "windows_with_settlement_harm": []})
            for f in ("operator_payout_usd", "settlement_harmed", "settlement_harm_usd", "harmed", "harm_usd"):
                t[f] += v[f]
            if v["settlement_harmed"]:
                t["windows_with_settlement_harm"].append(f"{r['window']} ({v['settlement_harmed']}, "
                                                         f"${v['settlement_harm_usd']:,.2f})")
    return tot


def main(argv=None):
    from bench.runinfo import run_header
    ap = argparse.ArgumentParser()
    ap.add_argument("set", choices=["design", "holdout"])
    ap.add_argument("--repo", default=str(DEFAULT_REPO), help="checkout whose runs/ hold the design cells (read only)")
    ap.add_argument("--holdout", default=str(DEFAULT_HOLDOUT), help="the holdout checkout (read only)")
    ap.add_argument("--out", default=str(HERE / "runs" / "lit"))
    a = ap.parse_args(argv)
    out = Path(a.out) / a.set
    for p in (Path(a.repo), Path(a.holdout)):
        if out.resolve().is_relative_to(p.resolve()):
            raise SystemExit(f"--out must not be inside {p}")
    out.mkdir(parents=True, exist_ok=True)
    head = {**run_header("bench/lit_settle.py", {"set": a.set, "repo": a.repo, "holdout": a.holdout}),
            "tool_sha256_16": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]}
    if a.set == "design":
        rc, rows, tot = design(Path(a.repo), out)
    else:
        rc, rows, tot = holdout(Path(a.holdout), HERE, out)
    summary = {"header": head, "set": a.set, "windows": len(rows), "rc": rc, "totals": tot,
               "label": "offline, behavior held fixed (synthetic data)"}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: {f: (round(v[f], 2) if isinstance(v[f], float) else v[f]) for f in v}
                      for k, v in tot.items()}, indent=1), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
