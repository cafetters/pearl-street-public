"""
D56: offline re-settle of arm C runs under candidate settlement rules (docs/plans/d56_settlement_report.md).

Reads runs/<run>/summary.json, the exchange and selfish traces, runs/h2h/feasibility.json, and the slice's data
(prices, recorded schedules, battery metadata) through sim.data. No simulation runs: agents' behaviour is the run's,
and every rule is applied to the as-run settled price.

Run (one cell per process):
    python -m bench.d56_resettle cell h2h_c_jan16_h100 [--out runs/d56]
    python -m bench.d56_resettle select [--cells selection|heldout|all]   -> runs/d56/selection*.json, table*.md
"""
import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

from sim.reference import ETA_REF, SD_REF, selfish_site_net    # rule 1p, shared with the run (sim/run_m1.py)

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "runs" / "d56"
H = 0.25                                # step, hours
K = H / 1000.0                          # $ per (kW x $/MWh) in one step
TOL_REL, TOL_ABS = 0.01, 10.0           # section 8 (sim/run_m1.py NET_HARM_REL, NET_HARM_ABS)
CLIP_USD_MWH = 100.0                    # D54 placeholder, for the clip variant (report only)
NET_PAY_MIN = -1.0                      # E3: operator net payout >= -$1 in every cell
EPS_USD = 1e-6                          # float noise in "shortfall > tolerance" (a 4c top-up lands on the edge)
SETTLED_RULE = {"recorded": "1", "selfish_1p": "1p"}     # exchange settlement_reference -> the rule it settled on

SELECTION = ["h2h_c_jan02_h100", "h2h_c_jan02_h150", "h2h_c_jan12_h100", "h2h_c_jan12_h150",
             "h2h_c_jan16_h100", "h2h_c_jan16_h150"]
HELDOUT = ["h2h_c_feb12_h100", "h2h_c_jun19_h100", "h2h_c_jun19_h150", "h2h_c_sep21_h100", "h2h_c_jul01_h100",
           "h2h_c_aug28_h100", "h2h_c_jul14_h100", "h2h_c_nov28_h100"]
SENSITIVITY = ["h2h_c_jan02_h150_cap100", "h2h_c_jan02_h150_cap250", "h2h_c_jan12_h150_cap100",
               "h2h_c_jan12_h150_cap250"]
# confirmation test (pre-registered in the report; runs settled in-run on 1p at D54's 100 $/MWh cap)
CONFIRM_TUNING = ["h2h_c_jan02_h100_1p", "h2h_c_jan12_h100_1p", "h2h_c_jan16_h100_1p"]
CONFIRM_FRESH = ["h2h_c_jan31_h100_1p"]
CONFIRM_ZERO = ["h2h_c_jan16_h100_1p"] + CONFIRM_FRESH        # settlement-caused harm must be 0 here
FEASIBILITY_FILES = [REPO / "runs" / "h2h" / "feasibility.json", REPO / "runs" / "d56" / "feasibility_confirm.json"]
BASE_RULES = ["1", "1s", "1p", "2", "3e", "3c", "3d", "5", "6"]
TOPUP_BASES = ["1", "3d", "3c"]
CANDIDATES = ["1", "1p", "2", "3e", "3c", "3d"] + [f"4{m}/{b}" for b in TOPUP_BASES for m in "fc"] + ["5", "6"]
COMPONENTS = ("lost_arbitrage_net_of_credits", "da_rt_gap", "extra_curtailment", "imbalance_charges", "end_energy")


class CheckFailed(RuntimeError):
    """A fail-closed check: the cell stops and the failure is recorded."""


# ---------------------------------------------------------------- settlement rules (pure; arrays are agents x steps)
def money(p, kw) -> np.ndarray:
    """$ per agent: sum over steps of p [$/MWh] x kW x h / 1000. p is (T,), kw is (n, T)."""
    return (np.asarray(kw, float) * np.asarray(p, float)).sum(axis=-1) * K


def settle_ref(p, N, ref) -> np.ndarray:
    """Charge on metered site net, refund on a reference at the same price: net paid to each agent."""
    return money(p, np.asarray(N, float) - np.asarray(ref, float))


def charges(p, N) -> np.ndarray:
    """Peak charge per agent: the price on metered load (load = -site net)."""
    return money(p, -np.asarray(N, float))


def rule_net(rule: str, c: dict, p) -> np.ndarray:
    """Net settlement paid to each agent under a base rule. c: cell arrays (see cell_arrays)."""
    if rule == "1":
        return settle_ref(p, c["N"], c["R"])
    if rule == "1s":
        return settle_ref(p, c["N"], c["S"])
    if rule == "1p":
        return settle_ref(p, c["N"], c["Q"])
    if rule == "6":
        return settle_ref(p, c["N"], c["R"] - c["B"])
    if rule == "2":
        return settle_ref(p, c["N"], c["R"]) if c["feasible"] else np.zeros(len(c["N"]))
    if rule in ("3e", "3c"):
        ch = charges(p, c["N"])
        w = np.full(len(ch), 1.0 / len(ch)) if rule == "3e" else c["w"]
        return -ch + w * ch.sum()
    if rule == "3d":
        s = settle_ref(p, c["N"], c["R"])
        return s - c["w"] * s.sum()
    if rule == "5":
        return np.maximum(0.0, settle_ref(p, c["N"], c["R"]))
    raise ValueError(rule)


def operator_payout(rule: str, c: dict, p) -> float:
    """The operator's net payout computed from fleet aggregates (not from per-agent nets), for the accounting check."""
    agg = lambda x: np.asarray(x, float).sum(axis=0)
    one = lambda ref: float((agg(c["N"]) - agg(ref)) @ p) * K
    if rule == "1" or (rule == "2" and c["feasible"]):
        return one(c["R"])
    if rule == "2":
        return 0.0
    if rule == "1s":
        return one(c["S"])
    if rule == "1p":
        return one(c["Q"])
    if rule == "6":
        return one(c["R"] - c["B"])
    if rule in ("3e", "3c"):
        pool = float(-agg(c["N"]) @ p) * K
        returned = pool * (1.0 if rule == "3e" else float(np.sum(c["w"])))
        return returned - pool
    if rule == "3d":
        return one(c["R"]) * (1.0 - float(np.sum(c["w"])))
    if rule == "5":
        refund, charge = money(p, -np.asarray(c["R"], float)), charges(p, c["N"])
        return float(refund.sum() - np.minimum(charge, refund).sum())
    raise ValueError(rule)


def zeroed_agents(rule: str, c: dict, p, net_1=None) -> np.ndarray:
    """Agents whose margin a base rule zeroes on every priced step (bool per agent)."""
    n = len(c["N"])
    if rule == "2" and not c["feasible"]:
        return np.ones(n, bool)
    if rule == "5":
        s = settle_ref(p, c["N"], c["R"]) if net_1 is None else net_1
        return s < 0.0
    return np.zeros(n, bool)


def tolerance(alone, scale: float = 1.0) -> np.ndarray:
    return scale * np.maximum(TOL_REL * np.abs(np.asarray(alone, float)), TOL_ABS)


def top_up(short, tol, mode: str) -> np.ndarray:
    """Rule 4: 'f' makes an agent whole once it is harmed beyond tolerance; 'c' tops it up to the tolerance edge."""
    short, tol = np.asarray(short, float), np.asarray(tol, float)
    if mode == "f":
        return np.where(short > tol, short, 0.0)
    if mode == "c":
        return np.maximum(0.0, short - tol)
    raise ValueError(mode)


def candidate(name: str, nets: dict, e, alone, tol_scale: float = 1.0) -> dict:
    """Net, top-up and zeroed agents for a selection candidate, from stored base-rule nets and the pre-settlement
    shortfall e. nets[rule] = {"net": [...], "zeroed": [...]}."""
    tol = tolerance(alone, tol_scale)
    if name.startswith("4"):
        mode, base = name[1], name.split("/")[1]
        net = np.asarray(nets[base]["net"], float)
        top = top_up(np.asarray(e, float) - net, tol, mode)
        return {"net": net, "top": top, "zeroed": top > 0, "tol": tol}
    net = np.asarray(nets[name]["net"], float)
    return {"net": net, "top": np.zeros_like(net), "zeroed": np.asarray(nets[name]["zeroed"], bool), "tol": tol}


# ---------------------------------------------------------------- metrics and selection (pure)
def cell_metrics(cell: dict, name: str, tol_scale: float = 1.0) -> dict:
    """Harm, settlement-caused harm, components over harmed agents, payout and zeroed margins for one candidate."""
    ag = cell["agents"]
    e = np.array([a["e"] for a in ag])
    alone = np.array([a["alone"] for a in ag])
    gap = np.array([a["components"]["da_rt_gap"] for a in ag])
    r = candidate(name, cell["rules"], e, alone, tol_scale)
    short = e - r["net"] - r["top"]
    harmed = short > r["tol"] + EPS_USD
    settle_harmed = (short - gap) > r["tol"] + EPS_USD
    comp = {k: float(sum(a["components"][k] for a, h in zip(ag, harmed) if h)) for k in COMPONENTS}
    comp["peak_settlement"] = float(-r["net"][harmed].sum())
    comp["top_up"] = float(-r["top"][harmed].sum())
    payout = float(r["net"].sum() + r["top"].sum())
    cut = cell["mw_cut"]
    mw = np.array([a["max_kw"] for a in ag]) / 1000.0
    return {"rule": name, "harmed": int(harmed.sum()), "harm_usd": float(short[harmed].sum()),
            "settlement_harmed": int(settle_harmed.sum()),
            "settlement_harm_usd": float((short - gap)[settle_harmed].sum()),
            "gap_usd_harmed": float(gap[harmed].sum()), "components_harmed": comp,
            "payout_usd": payout, "top_up_usd": float(r["top"].sum()),
            "payout_per_mw_cut": payout / cut if cut > 1e-6 else None,
            "breakeven_usd_mwh": payout / cell["relief_mwh"] if cell["relief_mwh"] > 1e-9 else None,
            "zeroed_agents": int(r["zeroed"].sum()),
            "zeroed_agent_steps": int(r["zeroed"].sum()) * cell["priced_steps"],
            "zeroed_mw": float(mw[r["zeroed"]].sum())}


def select_d56(cells: list[dict], names=None, tol_scale: float = 1.0) -> dict:
    """The pre-registered selection: E1 (no zeroed margin), E3 (payout >= -$1 per cell), then rank on agent-cells
    harmed, total $ harm, total payout. E2 holds for every candidate by construction (see the report)."""
    names = list(names or CANDIDATES)
    rows = {n: [cell_metrics(c, n, tol_scale) for c in cells] for n in names}
    table = []
    for n in names:
        ms = rows[n]
        e1 = all(m["zeroed_agent_steps"] == 0 for m in ms)
        e3 = all(m["payout_usd"] >= NET_PAY_MIN for m in ms)
        table.append({"rule": n, "e1": e1, "e3": e3, "eligible": e1 and e3,
                      "agent_cells_harmed": sum(m["harmed"] for m in ms),
                      "harm_usd": sum(m["harm_usd"] for m in ms),
                      "payout_usd": sum(m["payout_usd"] for m in ms)})
    ranked = sorted((t for t in table if t["eligible"]),
                    key=lambda t: (t["agent_cells_harmed"], round(t["harm_usd"], 6), round(t["payout_usd"], 6)))
    return {"cells": [c["run"] for c in cells], "tol_scale": tol_scale, "table": table,
            "ranking": [t["rule"] for t in ranked], "winner": ranked[0]["rule"] if ranked else None,
            "per_cell": rows}


def heldout_check(cells: list[dict], winner: str) -> dict:
    """Pre-registered: the winner meets E1-E3 in every held-out cell, and its settlement-caused harm $ summed over the
    held-out cells is no higher than rule 1's."""
    w = [cell_metrics(c, winner) for c in cells]
    one = [cell_metrics(c, "1") for c in cells]
    e1 = all(m["zeroed_agent_steps"] == 0 for m in w)
    e3 = all(m["payout_usd"] >= NET_PAY_MIN for m in w)
    sw, s1 = sum(m["settlement_harm_usd"] for m in w), sum(m["settlement_harm_usd"] for m in one)
    return {"winner": winner, "cells": [c["run"] for c in cells], "e1": e1, "e3": e3,
            "settlement_harm_usd": sw, "settlement_harm_usd_rule1": s1, "not_higher": sw <= s1 + 1e-9,
            "pass": e1 and e3 and sw <= s1 + 1e-9}


def confirm_check(cells: list[dict], missing: list[str], zero: list[str], rule: str = "1p") -> dict:
    """Pre-registered confirmation test, per cell: (1) settlement-caused harm is 0 in the `zero` cells (Jan 16-17
    and the fresh window), elsewhere its $ is no higher than rule 1's on the same trace; (2) operator net payout >= 0;
    (3) in-run peak_settle equals this tool's figure within $0.0001 (a cell that fails it never loads); (4) roster
    complete. A missing or failed cell fails."""
    rows = {}
    for c in cells:
        w, one = cell_metrics(c, rule), cell_metrics(c, "1")
        if c["run"] in zero:
            c1 = w["settlement_harmed"] == 0
        else:
            c1 = w["settlement_harm_usd"] <= one["settlement_harm_usd"] + 1e-9
        c2 = w["payout_usd"] >= 0.0
        c3 = c.get("settled_rule") == rule and c["checks"]["settle_err_usd"] <= 1e-4
        c4 = bool(c["checks"]["roster_complete"])
        c1, c2, c3 = bool(c1), bool(c2), bool(c3)
        rows[c["run"]] = {"c1_settlement_harm": c1, "c2_payout_nonneg": c2, "c3_peak_settle_matches": c3,
                          "c4_roster": c4, "pass": c1 and c2 and c3 and c4, "zero_required": c["run"] in zero,
                          "feasible": c["feasible"], rule: w, "rule_1_same_trace": one,
                          "settle_err_usd": c["checks"]["settle_err_usd"]}
    for r in missing:
        rows[r] = {"pass": False, "failed": "no result (run or check failed)"}
    return {"rule": rule, "cells": rows, "pass": bool(rows) and all(v["pass"] for v in rows.values())}


def check_roster(roster: list[str], ids: list[str], what: str):
    missing, extra = sorted(set(roster) - set(ids)), sorted(set(ids) - set(roster))
    if missing or extra or len(ids) != len(roster):
        raise CheckFailed(f"{what}: roster mismatch, missing {missing}, extra {extra}")


def check_accounting(rule: str, nets, tops, payout: float, tol: float = 0.01):
    total = float(np.sum(nets) + np.sum(tops))
    if abs(total - payout) > tol:
        raise CheckFailed(f"accounting, rule {rule}: agents {total:.4f} vs operator {payout:.4f}")


# ---------------------------------------------------------------- rule 1p's reference
def ref_1p(max_kw, usable_kwh, s0_kwh, price_by_gate: list, gate_steps: list, avail, base_export, wear: float,
           dt_h: float = H) -> np.ndarray:
    """Selfish site net per agent and step, computed by the exchange from registration and public data only:
    power = max_kw, energy window 10-90% of usable / 0.8, start SOC (also the soft end target), the day-ahead prices
    published by each binding gate, and site net without the battery (recorded). Each gate's plan runs from the slice
    start; the reference takes it on the steps that gate settles. NaN where no gate settles."""
    n, T = np.shape(base_export)
    out = np.full((n, T), np.nan)
    plans: dict = {}
    for price, steps in zip(price_by_gate, gate_steps):
        key = np.asarray(price, float).tobytes()
        for j in range(n):
            if (key, j) not in plans:
                q = selfish_site_net(max_kw[j], usable_kwh[j], s0_kwh[j], price, avail[j], base_export[j], wear, dt_h)
                if q is None:
                    raise CheckFailed(f"1p planner: no plan for agent index {j}")
                plans[(key, j)] = q
            out[j, steps] = plans[(key, j)][steps]
    return out


# ---------------------------------------------------------------- price pumping (first-order)
def pumping_gain(payoff, p, dp) -> np.ndarray:
    """Per agent: sum over priced steps of the gain from raising that one step's price by dp_t, where it gains.
    payoff(p) -> per-agent $ (settlement + top-up)."""
    base = payoff(p)
    gain = np.zeros_like(base)
    for t in np.flatnonzero(dp > 0):
        q = p.copy()
        q[t] += dp[t]
        gain += np.maximum(0.0, payoff(q) - base)
    return gain


# ---------------------------------------------------------------- loading a cell (traces + data)
def _read(path: Path, iso_pos: dict, T: int, exchange: bool) -> dict:
    out = {"net": defaultdict(lambda: np.zeros(T)), "meter": defaultdict(lambda: np.zeros(T)), "soc_end": {},
           "imb": defaultdict(float), "settle": {}, "ref": defaultdict(lambda: np.full(T, np.nan)), "pp": [],
           "aggs": [], "mandate": {}, "other": defaultdict(int), "sref": defaultdict(dict)}
    with open(path, encoding="utf-8") as f:
        header = json.loads(next(f))
        for line in f:
            m = json.loads(line)
            p, typ = m["payload"], m["msg_type"]
            if typ == "telemetry":
                aid = p["asset_id"].split("/")[0]
                i = iso_pos[m["t_utc"]]
                out["net"][aid][i] = p.get("net_kw", 0.0)
                out["meter"][aid][i] = p["meter_kw"]
                out["soc_end"][aid] = p["soc_kwh"]
            elif not exchange:
                continue
            elif typ == "imbalance":
                out["imb"][m["to"]] += p["charge_usd"]
            elif typ == "peak_settle":
                out["settle"][m["to"]] = p
            elif typ == "reference_net":
                a0 = iso_pos[p["window"]["start_utc"]]
                out["ref"][p["asset_id"].split("/")[0]][a0:a0 + len(p["net_kw"])] = p["net_kw"]
            elif typ == "selfish_reference":      # D56 confirmation runs: rule 1p's in-run reference per call
                out["sref"][p["call_id"]][p["asset_id"].split("/")[0]] = (iso_pos[p["window"]["start_utc"]],
                                                                          p["net_kw"], m["t_utc"])
            elif typ == "peak_price":
                out["pp"].append((m["t_utc"], p))
            elif typ == "nomination_aggregate" and p["binding"]:
                out["aggs"].append((m["t_utc"], p))
            elif typ == "register":
                out["mandate"][p["agent_id"]] = p["mandate"]
            elif typ in ("settle", "rebound", "award", "reject"):
                out["other"][typ] += 1
    return header, out


def _sref_gap(sref: dict, gates: list, gate_of, priced, Q, roster) -> float:
    """Largest |in-run selfish_reference - tool Q| (kW) over priced steps, by the binding call that settled them."""
    worst = 0.0
    for gi, g in enumerate(gates):
        steps = np.flatnonzero((gate_of == gi) & priced)
        if not len(steps):
            continue
        got = sref.get(g["call_id"], {})
        check_roster(roster, list(got), f"selfish_reference for {g['call_id']}")
        for j, aid in enumerate(roster):
            a0, kw, _ = got[aid]
            worst = max(worst, float(np.abs(np.asarray(kw, float)[steps - a0] - Q[j, steps]).max()))
    return worst


def tool_sha() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def load_cell(run: str, cfg_path: Path | None = None, max_rule: bool = False) -> tuple[dict, dict]:
    """Per-cell arrays (for the rules) and per-agent data (for harm), with the fail-closed rebuild checks.
    max_rule (D76 holdout check, off by default): accept a run settled under settlement_rule max_first_call. Its
    settlement is the run's own peak_settle net per agent (meta["max_net"]); this loader doesn't rebuild it, and
    bench.max_rule_holdout checks it against the offline rule. Every other check is unchanged."""
    import yaml
    from sim import physics
    from sim.data import load_config, load_slice
    from sim.forecasts import PriceForecast

    R = REPO / "runs" / run
    summ = json.loads((R / "summary.json").read_text())
    hs = list(summ["exchange_by_headroom_mw"])
    if len(hs) != 1:
        raise CheckFailed(f"{run}: expected one headroom, got {hs}")
    h = hs[0]
    ev = summ["exchange_by_headroom_mw"][h]
    roster = list(summ["header"]["roster"])
    cfg_path = cfg_path or REPO / "configs" / "h2h" / f"{run}.yaml"
    cfg = load_config(cfg_path)
    raw = yaml.safe_load(cfg_path.read_text())
    if cfg["run_name"] != run:
        raise CheckFailed(f"{run}: config run_name {cfg['run_name']}")
    s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
    iso = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in s.ts_utc]
    pos = {t: i for i, t in enumerate(iso)}
    T = len(iso)
    sid = [str(x) for x in s.ids]
    idx = np.array([sid.index(a) for a in roster])
    ex_header, ex = _read(R / f"exchange_h{h}_rinfo" / "trace.jsonl", pos, T, True)
    _, sf = _read(R / "selfish" / "trace.jsonl", pos, T, False)
    if any(ex["other"].values()):
        raise CheckFailed(f"{run}: unexpected messages in arm C trace {dict(ex['other'])}")
    # the rule the run settled under (D56 confirmation runs settle on 1p); rebuild checks use that rule
    mode = (ex_header.get("exchange") or {}).get("settlement_reference", "recorded")
    if mode not in SETTLED_RULE:
        raise CheckFailed(f"{run}: unknown settlement_reference {mode!r}")
    settled = SETTLED_RULE[mode]
    rule = (ex_header.get("exchange") or {}).get("settlement_rule", "today")
    is_max = rule == "max_first_call" and max_rule
    if rule != "today" and not is_max:   # D76: today's rule is rebuilt here; a max-rule run needs max_rule=True
        raise CheckFailed(f"{run}: settlement_rule {rule!r} is not today's rule; this loader can't check it")
    for what, ids in (("exchange telemetry", list(ex["net"])), ("selfish telemetry", list(sf["net"])),
                      ("peak_settle", list(ex["settle"])), ("reference_net", list(ex["ref"])),
                      ("register", list(ex["mandate"])), ("exchange header", ex_header.get("roster", []))):
        check_roster(roster, ids, f"{run} {what}")

    # settled price, and which binding call settles each step (a later binding call replaces an earlier one)
    p = np.zeros(T)
    gate_of = np.full(T, -1)
    gates = []
    for t_agg, agg in sorted(ex["aggs"], key=lambda x: x[0]):
        last = [pp for _, pp in ex["pp"] if pp["call_id"] == agg["call_id"]]
        if not last:
            continue
        pp = last[-1]
        a0 = pos[pp["window"]["start_utc"]]
        sl = slice(a0, a0 + len(pp["price_usd_mwh"]))
        p[sl] = pp["price_usd_mwh"]
        gate_of[sl] = len(gates)
        gates.append({"call_id": agg["call_id"], "t_utc": t_agg})
    priced = p > 1e-9
    p = np.where(priced, p, 0.0)
    cap = float(raw.get("peak_price_cap_usd_mwh", cfg.get("peak_price_cap_usd_mwh", np.nan)))
    if not np.isfinite(cap) or p.max() > cap + 1e-6:
        raise CheckFailed(f"{run}: price cap {cap} vs max settled {p.max()}")
    # alpha in force at each gate's last price update: rho_next / n (exchange/core.py)
    log = ev["price_mode"]["log"]
    for g in gates:
        rows = [x for x in log if x["call_id"] == g["call_id"] and not x.get("closing")]
        g["alpha"] = rows[-1]["rho_next"] / len(roster) if rows else 0.0
        g["t_gate"] = rows[0]["t_utc"] if rows else g["t_utc"]

    a = s.agent
    fleet = physics.Fleet.from_slice(s, idx)
    rt, da = s.system["price_rt"], s.system["price_da"]
    wear = float(cfg["wear_usd_mwh"])
    p_end = float(da.mean()) / 1000.0
    N = np.array([ex["net"][x] for x in roster])
    S = np.array([sf["net"][x] for x in roster])
    Rf = np.array([ex["ref"][x] for x in roster])
    B = a["bess"][idx].astype(float)
    if np.isnan(Rf[:, priced]).any():
        raise CheckFailed(f"{run}: reference_net missing at priced steps")
    Rf = np.nan_to_num(Rf)
    max_kw = np.array([float(ex["mandate"][x]["max_kw"]) for x in roster])
    w = max_kw / max_kw.sum()

    # rule 1p: the exchange's own selfish reference at each binding gate
    pv, curt, load = np.nan_to_num(a["pv"][idx]), a["curt"][idx], a["load"][idx]
    prices = PriceForecast(s.ts_utc, da, cfg["slice_tz"], cfg["price_publish_hour_local"])
    by_gate, steps_by_gate, versions = [], [], []
    for gi, g in enumerate(gates):
        v, fc = prices.at(pos[g["t_gate"]])
        versions.append(v)
        by_gate.append(fc)
        steps_by_gate.append(np.flatnonzero((gate_of == gi) & priced))
    Q = ref_1p(max_kw, 0.8 * fleet.CAP, fleet.S0, by_gate, steps_by_gate, pv - curt, pv - curt - load, wear)
    Q = np.where(np.isnan(Q), 0.0, Q)

    feas = {}
    for f in FEASIBILITY_FILES:
        if f.exists():
            feas = {**json.loads(f.read_text())["cells"], **feas}
    if run not in feas:
        raise CheckFailed(f"{run}: not in {[str(f.relative_to(REPO)) for f in FEASIBILITY_FILES]}")
    c = {"N": N, "R": Rf, "S": S, "B": B, "Q": Q, "w": w, "feasible": bool(feas[run]["feasible"])}

    # per-agent harm data (sim/run_m1.py m2a_metrics and net_harm)
    agents, settle_err, rebuild_err, comp_err = [], 0.0, 0.0, 0.0
    net1 = settle_ref(p, N, Rf)
    net_s = net1 if settled == "1" else settle_ref(p, N, Q)       # what peak_settle paid
    if is_max:                   # the max rule: what peak_settle paid, as is (checked offline by the holdout tool)
        net_s = np.array([float(ex["settle"][x]["net_usd"]) for x in roster])
    by_agent = ev["net_harm"]["by_agent_usd"]
    for j, aid in enumerate(roster):
        mr, ms = ex["meter"][aid], sf["meter"][aid]
        val = float(rt @ N[j]) * K - wear * K * np.maximum(mr, 0).sum()
        sval = float(rt @ S[j]) * K - wear * K * np.maximum(ms, 0).sum()
        alone = sval + sf["soc_end"][aid] * p_end
        e = alone - (val - ex["imb"][aid] + ex["soc_end"][aid] * p_end)
        dn = mr - ms
        dx = -((N[j] - S[j]) - dn)
        dwear = wear * K * (np.maximum(mr, 0).sum() - np.maximum(ms, 0).sum())
        comp = {"lost_arbitrage_net_of_credits": -(float(da @ dn) * K - dwear),
                "da_rt_gap": -float((rt - da) @ dn) * K, "extra_curtailment": float(rt @ dx) * K,
                "imbalance_charges": ex["imb"][aid], "end_energy": (sf["soc_end"][aid] - ex["soc_end"][aid]) * p_end}
        comp_err = max(comp_err, abs(sum(comp.values()) - e))
        ps = ex["settle"][aid]
        settle_err = max(settle_err, abs(ps["net_usd"] - net_s[j]))
        rebuild_err = max(rebuild_err, abs(max(e - net_s[j], 0.0) - by_agent[aid]))
        agents.append({"agent": aid, "alone": alone, "e": e, "components": comp, "max_kw": float(max_kw[j]),
                       "w": float(w[j])})
    if settle_err > 1e-4:
        raise CheckFailed(f"{run}: rule {settled} vs peak_settle, max error ${settle_err:.6f}")
    if rebuild_err > 0.05:
        raise CheckFailed(f"{run}: rule {settled} harm vs summary by_agent_usd, max error ${rebuild_err:.4f}")
    if comp_err > 0.01:
        raise CheckFailed(f"{run}: components vs shortfall, max error ${comp_err:.4f}")
    tol = tolerance([x["alone"] for x in agents])
    short1 = np.array([x["e"] for x in agents]) - net_s
    harmed = short1 > tol
    want = ev["net_harm"]["components"]["harmed_agents"]
    keymap = {"lost_arbitrage_net_of_credits": "lost_arbitrage_net_of_credits", "da_rt_gap": "da_rt_gap",
              "extra_curtailment": "extra_curtailment", "imbalance_charges": "imbalance_charges",
              "end_energy": "end_energy"}
    for k, sk in keymap.items():
        got = sum(x["components"][k] for x, hh in zip(agents, harmed) if hh)
        if abs(got - want.get(sk, 0.0)) > 0.05:
            raise CheckFailed(f"{run}: harmed-agent component {k} {got:.2f} vs summary {want.get(sk):.2f}")
    got = float(-net_s[harmed].sum())
    if abs(got - want.get("peak_price_net", 0.0)) > 0.05:
        raise CheckFailed(f"{run}: harmed-agent peak_price_net {got:.2f} vs summary {want.get('peak_price_net'):.2f}")

    sel_load = -S.sum(0)
    run_load = -N.sum(0)
    meta = {"run": run, "headroom_mw": int(h), "window": f"{cfg['start']} to {cfg['end']}",
            "feasible": c["feasible"], "price_cap_usd_mwh": cap, "priced_steps": int(priced.sum()),
            "settled_max_usd_mwh": float(p.max()), "settled_mean_usd_mwh": float(p[priced].mean()) if priced.any() else 0.0,
            "mw_cut": -float(ev["peak_vs_selfish_mw"]),
            "relief_mwh": float(np.maximum(0.0, sel_load - run_load)[priced].sum() * H / 1000.0),
            "gates": [{**g, "price_version": v} for g, v in zip(gates, versions)],
            "roster": roster, "agents": agents,
            "checks": {"settle_err_usd": settle_err, "rebuild_err_usd": rebuild_err, "components_err_usd": comp_err,
                       "roster_complete": True}}
    if settled != "1":       # report only: the in-run reference vs this tool's Q at each settled step
        meta["settled_rule"] = settled
        meta["checks"]["in_run_reference_vs_tool_max_kw"] = _sref_gap(ex["sref"], gates, gate_of, priced, Q, roster)
    if is_max:
        meta["settled_rule"] = "max_first_call"
        meta["max_net"] = [float(v) for v in net_s]
    dp = np.zeros(T)
    for gi, g in enumerate(gates):
        m = (gate_of == gi) & priced
        dp[m] = np.minimum(g["alpha"] * 1000.0 / K, cap - p[m])
    return meta, {"c": c, "p": p, "priced": priced, "dp": dp}


def evaluate(meta: dict, arr: dict) -> dict:
    """Every base rule at the as-run price and clipped at CLIP_USD_MWH; accounting checks; pumping bounds; 1p vs 1s."""
    c, p, dp = arr["c"], arr["p"], arr["dp"]
    e = np.array([x["e"] for x in meta["agents"]])
    alone = np.array([x["alone"] for x in meta["agents"]])

    def block(price):
        out, net1 = {}, rule_net("1", c, price)
        for r in BASE_RULES:
            net = rule_net(r, c, price)
            check_accounting(r, net, 0.0, operator_payout(r, c, price))
            out[r] = {"net": [float(x) for x in net], "zeroed": [bool(x) for x in zeroed_agents(r, c, price, net1)]}
        for name in CANDIDATES:      # top-ups: accounting with the operator paying base + top-ups
            if name.startswith("4"):
                base = name.split("/")[1]
                r = candidate(name, out, e, alone)
                check_accounting(name, r["net"], r["top"], operator_payout(base, c, price) + float(r["top"].sum()))
        return out

    meta = dict(meta)
    meta["rules"] = block(p)
    clip = meta["price_cap_usd_mwh"] > CLIP_USD_MWH
    meta["clip"] = {"applied": bool(clip), "price_usd_mwh": CLIP_USD_MWH,
                    "rules": block(np.minimum(p, CLIP_USD_MWH)) if clip else None}
    pump = {}
    for name in CANDIDATES + ["1s"]:
        def payoff(price, name=name):
            if name.startswith("4"):
                base = name.split("/")[1]
                net = rule_net(base, c, price)
                return net + top_up(e - net, tolerance(alone), name[1])
            return rule_net(name, c, price)
        g = pumping_gain(payoff, p, dp)
        pump[name] = {"max_usd": float(g.max()), "sum_usd": float(g.sum()), "agents_gaining": int((g > 1e-9).sum())}
    meta["pumping_per_mw"] = pump
    d = np.array(meta["rules"]["1p"]["net"]) - np.array(meta["rules"]["1s"]["net"])
    meta["p1_vs_1s"] = {"sum_usd": float(d.sum()), "max_abs_usd": float(np.abs(d).max()),
                        "by_agent": {a["agent"]: float(x) for a, x in zip(meta["agents"], d)}}
    return meta


# ---------------------------------------------------------------- CLI
def run_cell(run: str, out: Path) -> int:
    from bench.runinfo import run_header
    out.mkdir(parents=True, exist_ok=True)
    head = {**run_header("bench/d56_resettle.py", {"run": run, "eta_ref": ETA_REF, "sd_ref": SD_REF,
                                                    "clip_usd_mwh": CLIP_USD_MWH}), "tool_sha256_16": tool_sha()}
    try:
        meta, arr = load_cell(run)
        res = evaluate(meta, arr)
    except CheckFailed as err:
        (out / f"{run}.failed.json").write_text(json.dumps({"header": head, "failed": str(err)}, indent=1))
        print(f"{run}: CHECK FAILED: {err}", flush=True)
        return 2
    head["roster"] = res["roster"]
    (out / f"{run}.json").write_text(json.dumps({"header": head, **res}, indent=1, default=float))
    print(f"{run}: ok ({res['priced_steps']} priced steps, feasible {res['feasible']})", flush=True)
    return 0


def load_results(runs: list[str], out: Path) -> list[dict]:
    cells = []
    for r in runs:
        f = out / f"{r}.json"
        if not f.exists():
            raise CheckFailed(f"{r}: no result (failed or not run)")
        d = json.loads(f.read_text())
        check_roster(d["header"]["roster"], [a["agent"] for a in d["agents"]], f"{r} result")
        cells.append(d)
    return cells


def clipped(cell: dict) -> dict:
    """The cell with its clip-at-100 rule nets in place of the as-run ones (report only)."""
    return {**cell, "rules": cell["clip"]["rules"]} if cell["clip"]["applied"] else cell


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("cell")
    a1.add_argument("run")
    a1.add_argument("--out", default=str(OUT))
    a2 = sub.add_parser("select")
    a2.add_argument("--out", default=str(OUT))
    a3 = sub.add_parser("heldout")
    a3.add_argument("--out", default=str(OUT))
    a4 = sub.add_parser("confirm")
    a4.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)
    if args.cmd == "cell":
        return run_cell(args.run, Path(args.out))
    out = Path(args.out)
    if args.cmd == "heldout":      # only after the verdict is committed (pre-registered order)
        winner = json.loads((out / "selection.json").read_text())["main"]["winner"]
        done = [r for r in HELDOUT if (out / f"{r}.json").exists()]
        res = heldout_check(load_results(done, out), winner) if winner else {"winner": None}
        res["missing_or_failed"] = sorted(set(HELDOUT) - set(done))
        res["pass"] = bool(res.get("pass")) and not res["missing_or_failed"]
        (out / "heldout.json").write_text(json.dumps(res, indent=1, default=float))
        print(json.dumps(res, indent=1, default=float))
        return 0
    if args.cmd == "confirm":      # the tuning cells and the fresh window are reported separately
        res = {}
        for part, runs in (("tuning", CONFIRM_TUNING), ("fresh", CONFIRM_FRESH)):
            done = [r for r in runs if (out / f"{r}.json").exists()]
            res[part] = confirm_check(load_results(done, out), sorted(set(runs) - set(done)), CONFIRM_ZERO)
        res["pass"] = res["tuning"]["pass"] and res["fresh"]["pass"]
        (out / "confirm.json").write_text(json.dumps(res, indent=1, default=float))
        print(json.dumps({k: (v["pass"] if isinstance(v, dict) else v) for k, v in res.items()}, indent=1))
        return 0
    cells = load_results(SELECTION, out)
    res = {"main": select_d56(cells)}
    for drop in SELECTION:
        res[f"without_{drop}"] = {k: v for k, v in select_d56([c for c in cells if c["run"] != drop]).items()
                                  if k != "per_cell"}
    for sc in (0.0, 2.0):
        res[f"tol_x{sc:g}"] = {k: v for k, v in select_d56(cells, tol_scale=sc).items() if k != "per_cell"}
    (out / "selection.json").write_text(json.dumps(res, indent=1, default=float))
    print(json.dumps({k: v["winner"] for k, v in res.items()}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
