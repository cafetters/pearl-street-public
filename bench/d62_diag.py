"""
D62 post-hoc diagnostic (exploratory; docs/plans/d62_guarantee_report.md, "Post-hoc diagnostic"). Sets no threshold
and decides nothing. The 11 D58 windows are now the design set for a v3; nothing here runs on new windows.

  1. Coverage (gate 3) and cost (gate 4) with every agent-quarter eligible: the payout upper bound, and whether the
     guarantee covers our agents at all. Also exercises the ledger accounting and payout recomputation.
  2. The m = 0 median share: (a) per-step clipping vs. a measure netted over each priced span; (b) P* re-solved with
     each agent's final-round prox; (c) span shares by whether the gate's price rounds converged.
  3. m = 0 vs. acting-alone distributions under the netted measure.

Netted measure, per agent and span g (priced steps only): D_g = sum(P* - Q_g), delivered_g = clip(sum(N - Q_g), 0, D_g)
(spans with D_g <= 0 add nothing); per quarter, share = sum delivered_g / sum max(D_g, 0), 0/0 under 1 kWh as before.

Commands (one window per process, from the repo root):
  python -m bench.d62_diag window <run>     -> runs/d62/diag/<run>.json
  python -m bench.d62_diag summary          -> runs/d62/diag/summary.json
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

from agents.planner import solve
from bench import d58_margin as DM
from bench import d62_guarantee as T
from bench.d56_resettle import CheckFailed
from exchange import guarantee as G
from sim.reference import ModeledPaths, ref_battery

OUT = T.OUT / "diag"
H = T.H
PROX_EPS_USD_MWH = 0.05                  # configs: prox_deadzone_usd_mwh (the agents' secant prox)


class ProxPaths(ModeledPaths):
    """P* re-solved with the agent's final-round prox at each stage (Q* unchanged)."""

    def __init__(self, *a, prox_by_k0=None, **kw):
        super().__init__(*a, **kw)
        self.prox_by_k0 = prox_by_k0 or {}

    def _plan(self, k0, s_now, price, peak=None):
        prox = self.prox_by_k0.get(k0) if peak is not None else None
        sl = slice(k0, None)
        return solve(ref_battery(self.max_kw, self.usable, self.s0), k0, s_now, np.asarray(price, float)[sl],
                     self.avail[sl], self.base[sl], np.zeros(len(self.base) - k0), self.dt, self.wear,
                     peak=None if peak is None else np.asarray(peak, float)[sl],
                     prox=None if prox is None else (prox[0], prox[1][sl], prox[2]))


def final_prox(run: str, d: dict) -> dict:
    """call_id -> agent -> (rho, centre over the slice, eps): the prox each agent's final-round plan used. centre =
    base - (its last nomination for the call before the final price) - the final broadcast's shift."""
    pos = {t: i for i, t in enumerate(d["iso"])}
    calls = {g["call_id"] for g in d["gates"]}
    last_nom = defaultdict(dict)             # call -> agent -> (start index, kw)
    final = {}
    path = next((T.REPO / "runs" / run).glob("exchange_h*_rinfo/trace.jsonl"))
    with open(path, encoding="utf-8") as f:
        next(f)
        for line in f:
            m = json.loads(line)
            p = m["payload"]
            if m["msg_type"] == "nominate" and p["call_id"] in calls and p["call_id"] not in final:
                last_nom[p["call_id"]][m["from"]] = (pos[p["window"]["start_utc"]], p["kw"])
            elif m["msg_type"] == "peak_price" and p["call_id"] in calls and p["final"] and p["call_id"] not in final:
                final[p["call_id"]] = p
    eps = PROX_EPS_USD_MWH * H / 1000.0
    out = {}
    for cid, p in final.items():
        a0 = pos[p["window"]["start_utc"]]
        shift = np.full(d["T"], np.nan)
        shift[a0:a0 + len(p["shift_kw"])] = p["shift_kw"]
        out[cid] = {}
        for j, aid in enumerate(d["roster"]):
            sent = np.full(d["T"], np.nan)
            if aid in last_nom[cid]:
                s0, kw = last_nom[cid][aid]
                sent[s0:s0 + len(kw)] = kw
            centre = d["base"][j] - sent - shift          # NaN where no nomination or no shift: no prox there
            out[cid][aid] = (float(p["rho"]), centre, eps)
    return out


def measures(p, q_ref, P_net, net, gate_of, nspan) -> dict:
    """Per span: per-step clipped (shift, delivered) and netted (D, delivered) in kWh."""
    rows = []
    for g in range(nspan):
        m = (gate_of == g) & (p > 0)
        pm = np.where(m, p, 0.0)
        sh, got = G.response_kwh(pm, q_ref, P_net, net, H)
        Dg = float((P_net - q_ref)[m].sum()) * H
        Ng = float((net - q_ref)[m].sum()) * H
        rows.append({"step_shift": sh, "step_delivered": got, "net_D": Dg,
                     "net_delivered": float(min(max(Ng, 0.0), Dg)) if Dg > 0 else 0.0})
    return rows


def window(run: str) -> dict:
    d = T.load_run(run)
    res = T.compute(d)
    prox = final_prox(run, d)
    from bench.runinfo import run_header
    summ = json.loads((T.REPO / "runs" / run / "summary.json").read_text())
    log = summ["exchange_by_headroom_mw"][list(summ["exchange_by_headroom_mw"])[0]]["price_mode"]["log"]
    conv = {g["call_id"]: any(x.get("converged") for x in log if x["call_id"] == g["call_id"]) for g in d["gates"]}
    gates, T_ = d["gates"], d["T"]
    agents = []
    for j, aid in enumerate(d["roster"]):
        arr = res["arrays"][aid]
        q_ref, P_net = arr["q_ref"], arr["P_net"]
        args = (d["max_kw"][j], d["usable"][j], d["s0"][j])
        pp = ProxPaths(*args, d["avail"][j], d["base"][j], d["wear"], H,
                       prox_by_k0={g["deadline"]: prox[g["call_id"]][aid] for g in gates if g["call_id"] in prox})
        for gi, g in enumerate(gates):
            pp.stage(g["deadline"], g["fc"], g["peak"], arr["Q"][0] if gi == 0 else None)
        P_prox = pp.net(pp.P)
        agents.append({"agent": aid,
                       "m0": measures(d["p"], q_ref, P_net, d["N"][j], d["gate_of"], len(gates)),
                       "alone": measures(d["p"], q_ref, P_net, d["S"][j], d["gate_of"], len(gates)),
                       "m0_prox": measures(d["p"], q_ref, P_prox, d["N"][j], d["gate_of"], len(gates)),
                       "alone_prox": measures(d["p"], q_ref, P_prox, d["S"][j], d["gate_of"], len(gates))})
    return {"header": {**run_header("bench/d62_diag.py", {"run": run}), "roster": d["roster"]}, "run": run,
            "gates": [{"call_id": g["call_id"], "converged": conv[g["call_id"]]} for g in gates], "agents": agents}


def quarter_share(cells: dict, key: str, kind: str) -> dict:
    """(agent, quarter) -> share, from per-span rows. kind: 'step' (pre-registered) or 'net' (netted)."""
    acc = defaultdict(lambda: [0.0, 0.0])
    for w, c in cells.items():
        for a in c["agents"]:
            r = acc[(a["agent"], T.QUARTER[w])]
            for s in a[key]:
                if kind == "step":
                    r[0] += s["step_shift"]
                    r[1] += s["step_delivered"]
                else:
                    r[0] += max(s["net_D"], 0.0)
                    r[1] += s["net_delivered"]
    return {k: G.share(v[0], v[1]) for k, v in acc.items()}


def dist(sh: dict) -> dict:
    v = np.array([x for x in sh.values() if x is not None])
    return {"counted": int(len(v)), "excluded_0_0": int(len(sh) - len(v)),
            "quantiles_0_10_25_50_75_90_100": [round(float(np.quantile(v, q)), 3) for q in (0, .1, .25, .5, .75, .9, 1)]
            if len(v) else None}


def summary() -> dict:
    out = {"label": "POST HOC, exploratory: sets no threshold and decides nothing"}
    # 1. coverage and cost with every agent-quarter eligible
    d56, d62 = T.m0_cells()
    roster = list(d62[T.WINDOWS[0]]["header"]["roster"])
    rows = T.quarter_rows(d62)
    led = T.ledgers(rows, float("-inf"))                  # every agent-quarter eligible
    fleet = T.fleet_ledger(led)
    try:
        out["ledger_accounting"] = T.check_ledgers(led, fleet)
    except CheckFailed as err:
        out["ledger_accounting"] = {"pass": False, "failed": str(err)}
    gain, alone = defaultdict(float), defaultdict(float)
    for w in T.WINDOWS:
        for a in d56[w]["agents"]:
            alone[a["agent"]] += a["alone"]
        for a, g in DM.agent_results(d56[w]).items():
            gain[a] += g
    cov = []
    for a in roster:
        tol = max(T.TOL_REL * abs(alone[a]), T.TOL_ABS)
        total = gain[a] + led[a]["net_usd"]
        cov.append({"agent": a, "result_usd": gain[a], "d62_net_usd": led[a]["net_usd"], "total_usd": total,
                    "tol_usd": tol, "pass": bool(total >= -tol - 1e-6), "pass_without_d62": bool(gain[a] >= -tol - 1e-6)})
    pay0 = sum(T.payout(d56[w]) for w in T.WINDOWS)
    pay10 = sum(T.payout(DM.load(T.M10_RUN[w])) for w in T.WINDOWS)
    out["payout_recomputation"] = {"m0_usd": pay0, "m10_usd": pay10, "m0_preregistered": T.M0_PAYOUT_USD,
                                   "m10_preregistered": T.MARGIN10_PAYOUT_USD,
                                   "pass": bool(abs(pay0 - T.M0_PAYOUT_USD) <= T.PAYOUT_TOL_USD
                                                and abs(pay10 - T.MARGIN10_PAYOUT_USD) <= T.PAYOUT_TOL_USD)}
    out["coverage_all_eligible"] = {"pass": all(c["pass"] for c in cov), "failing": [c for c in cov if not c["pass"]],
                                    "failing_without_d62": sum(not c["pass_without_d62"] for c in cov),
                                    "agents_paid": sum(1 for a in roster if led[a]["net_usd"] > 0)}
    out["cost_all_eligible"] = {"m0_payout_usd": pay0, "d62_gross_usd": fleet["gross_paid_usd"],
                                "total_usd": pay0 + fleet["gross_paid_usd"], "margin10_payout_usd": pay10,
                                "below_margin10": bool(pay0 + fleet["gross_paid_usd"] < pay10),
                                "d62_net_usd": fleet["net_usd"], "clawed_back_usd": fleet["clawed_back_usd"],
                                "max_outstanding_usd": fleet["max_outstanding_usd"], "by_quarter": fleet["by_quarter"]}
    # 2-3. share measures
    cells = {}
    for w in T.WINDOWS:
        f = OUT / f"{T.M0_RUN[w]}.json"
        if not f.exists():
            raise CheckFailed(f"{T.M0_RUN[w]}: no diagnostic window")
        cells[w] = json.loads(f.read_text())
    m = {}
    for label, key, kind in (("m0_step", "m0", "step"), ("alone_step", "alone", "step"), ("m0_net", "m0", "net"),
                             ("alone_net", "alone", "net"), ("m0_step_prox", "m0_prox", "step"),
                             ("alone_step_prox", "alone_prox", "step"), ("m0_net_prox", "m0_prox", "net"),
                             ("alone_net_prox", "alone_prox", "net")):
        sh = quarter_share(cells, key, kind)
        m[label] = {**dist(sh), "_sh": sh}
    for v in ("step", "net", "step_prox", "net_prox"):
        sep = G.separation(m[f"m0_{v}"]["_sh"], m[f"alone_{v}"]["_sh"])
        m[f"separation_{v}"] = {k: sep.get(k) for k in ("pass", "lowest_responder", "highest_non_responder",
                                                         "excluded_0_0", "counted")}
        m[f"separation_{v}"]["overlap_counts"] = {k: len(x) for k, x in sep["overlap"].items()}
    for k in list(m):
        m[k].pop("_sh", None)
    out["shares"] = m
    # 2c. per-span shares (pre-registered per-step measure) by whether the gate converged
    by = defaultdict(list)
    for w, c in cells.items():
        for gi, g in enumerate(c["gates"]):
            for a in c["agents"]:
                s = a["m0"][gi]
                v = G.share(s["step_shift"], s["step_delivered"])
                if v is not None:
                    by["converged" if g["converged"] else "not_converged"].append(v)
    out["span_share_by_convergence"] = {
        k: {"spans": len(v), "median": round(float(np.median(v)), 3),
            "quartiles": [round(float(np.quantile(v, q)), 3) for q in (.25, .75)]} for k, v in by.items()}
    out["gates_converged"] = {w: [g["converged"] for g in c["gates"]] for w, c in cells.items()}
    return out


def main(argv=None):
    a = argv or sys.argv[1:]
    OUT.mkdir(parents=True, exist_ok=True)
    if a[0] == "window":
        (OUT / f"{a[1]}.json").write_text(json.dumps(window(a[1]), default=float))
        print(f"{a[1]}: ok", flush=True)
        return 0
    res = summary()
    (OUT / "summary.json").write_text(json.dumps(res, indent=1, default=float))
    print(json.dumps({k: v for k, v in res.items() if k not in ("cost_all_eligible",)}, indent=1, default=float)[:6000])
    c = res["cost_all_eligible"]
    print(json.dumps({k: v for k, v in c.items() if k != "by_quarter"}, indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
