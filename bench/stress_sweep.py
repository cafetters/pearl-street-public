"""
D10 stressed-grid calibration: AC power flow on the dev slice under three load-growth sweeps.

  A  zone agents' load x (1 + g), g = 0..40% step 5      (reported only, never recommended)
  B  A plus background loads at zone buses x (1 + g)
  C  one new flat load per zone at its highest-|PTDF| sign-correct agent bus, 0..60 MW step 10

Zones are M0's (bench/zone_check.py): agents with |PTDF| >= 0.05 on t35 or t40. One growth factor
covers the union, so agents in both zones grow once.

Steps
  1. Baseline gate: level 0 vs Engie's hourly power-flow results, variants tried in order. The
     held-slack variant (gens pro rata to max_p, ext_grid held at Engie's hourly slack_p_mw) has a
     looser raw gate and must also pass the delta validation (hour-over-hour changes on t35/t40).
     Stop if no variant passes.
  2. Zone report: each agent's PTDF sign, whether its discharge relieves or loads the element, and the
     zone's sign-correct battery capacity.
  3. Each level, anchored: Engie's loading (and vm) + our change from level 0. Hours >= 90% / 100% per
     Central day, worst-hour relief vs. capacity, other binding elements, voltage violations, steps
     with the gens at max_p.
  4. Rules (D31): bar = >= 4 h >= 90% across the slice on each of t35 and t40; feasible = worst-hour relief
     <= 70% of sign-correct capacity on both. Smallest passing level per sweep, then pick(B, C).
  5. Supply sensitivity at the pick: the same scenario with gens held at their level-0 total, so the
     ext_grid serves the growth.

Run:  python -m bench.stress_sweep configs/dev_2day.yaml      -> runs/stress/
"""
import json, sys, time

import numpy as np
import pandas as pd

from bench.runinfo import run_header
from bench.zone_check import LIMIT_PCT, PTDF_MIN, branches, cut_off, ptdf
from sim.data import REPO, load_config, load_flex, load_grid, load_pp_net, load_powerflow, load_slice
from sim.stress import NEW_LOAD_PF, SLACK_TOL_MW, ACGrid, Scenario, new_load_bus, relief_mw

OUT = REPO / "runs" / "stress"
ZONES = ["t35", "t40"]
M0_ZONE_SIZES = {"t35": 22, "t40": 53, "l43": 1, "l26": 1}
LINE_ZONES = ["l43", "l26"]                               # D9 thin-zone candidates, checked at level 0 only
GROWTH = [round(0.05 * i, 2) for i in range(9)]          # A, B
NEW_MW = [10.0 * i for i in range(7)]                    # C
GRID2D_T35_GROWTH = [round(0.05 * i, 2) for i in range(4)]   # 2D: t35 zone load growth (B style, t35 only)
GRID2D_T40_MW = [10.0 * i for i in range(9)]                 # 2D: new load at t40's C bus
FLEX_T35_MW = [10.0 * i for i in range(5)]                   # flexible-DC 2D: new data center at t35's C bus
FLEX_T40_MW = [10.0 * i for i in range(9)]                   # flexible-DC 2D: new data center at t40's C bus
NEW_DC_FLEX_SHARE = 0.25                                     # deferrable share of a new data center (assumption)
FLEX_RULE = ("capacity = sign-correct battery MW + PTDF-weighted, sign-correct flexible load at the worst-relief step: "
             "dc_flexible_kw (load that could be postponed right now) + ev_load_kw (unmanaged EV charging, deferrable "
             "up to max_defer_steps_ev), summed over the zone's relief-correct agents x |PTDF|; a new data center adds "
             f"{NEW_DC_FLEX_SHARE:.0%} of its MW x |PTDF| on a zone where its bus is relief-correct. Flexible load "
             "counts only what is running at that step; deadlines and rebound are not checked (upper bound)")
OVERLOAD_PCT = 100.0
BAR_HOURS = 4.0                                          # D31 bar: hours >= 90% across the slice, per element
RELIEF_CAP_SHARE = 0.7
GATE_VM_PU = 0.005
DELTA_MIN_R = 0.95
DELTA_SLOPE = (0.9, 1.1)
TZ = "America/Chicago"
VARIANTS = [                                              # tried in order until one passes the gate
    {"name": "unity_pf_ext_grid_slack", "agent_q_ratio": 0.0, "distributed_slack": False, "dispatch": "json",
     "split": "max", "gate_pp": 1.0, "anchored": False},
    {"name": "agent_q_bg_ratio", "agent_q_ratio": None, "distributed_slack": False, "dispatch": "json",
     "split": "max", "gate_pp": 1.0, "anchored": False},
    {"name": "gens_share_slack", "agent_q_ratio": 0.0, "distributed_slack": True, "dispatch": "json",
     "split": "max", "gate_pp": 1.0, "anchored": False},
    {"name": "pro_rata_held_slack", "agent_q_ratio": 0.0, "distributed_slack": False, "dispatch": "held_slack",
     "split": "max", "gate_pp": 3.0, "anchored": True},
    {"name": "headroom_held_slack", "agent_q_ratio": 0.0, "distributed_slack": False, "dispatch": "held_slack",
     "split": "headroom", "gate_pp": 3.0, "anchored": True},
]
# Fallback when every variant above fails: the held-slack run whose t35/t40 deltas correlate best with
# Engie's, with each element's change divided by its fitted delta slope (disclosed calibration factor).
CALIBRATED = "calibrated_delta"
DISPATCH_RULE = ("held slack: ext_grid held at Engie's hourly slack_p_mw (the hour's value for all four 15-min steps, "
                 f"to {SLACK_TOL_MW} MW); in-service gens cover the rest, including all growth, split pro rata to "
                 "max_p_mw (pro_rata) or as min_p plus a common share of (max_p - min_p) (headroom); gens clip at "
                 "[min_p, max_p], steps at max_p are flagged and the ext_grid then takes the excess")
ANCHOR_RULE = ("reported loading and vm = Engie's hourly value + (ours at this level - ours at level 0); the hour's "
               "offset (Engie - ours at level 0, on the hour) is applied to all four 15-min steps of that hour")
CALIBRATION_RULE = (f"calibrated_delta: for t35 and t40, our change from level 0 is divided by that element's fitted "
                    f"delta slope (ours regressed on Engie's hour-over-hour changes) before anchoring; gate: loading "
                    f"deltas r >= {DELTA_MIN_R} per element. Every bus's vm change is divided by one pooled vm delta "
                    f"slope; voltage is validated only if a factor fitted on day 1 gives r >= {DELTA_MIN_R} and slope in "
                    f"[{DELTA_SLOPE[0]}, {DELTA_SLOPE[1]}] on day 2. If not, vm is anchored uncalibrated, violation "
                    "counts are marked not validated and voltage leaves pick's tie-break. Raw level errors are "
                    "reported, not gated, since anchoring replaces raw levels")


# ---------- rules (D31) ----------

def hours_by_day(over: pd.Series, days) -> dict[str, float]:
    """Hours (15-min steps x 0.25) where `over` is True, per Central calendar day. Every day in `days` appears."""
    local = over.index.tz_convert(TZ).strftime("%Y-%m-%d")
    h = over.astype(float).groupby(local).sum() * 0.25
    return {d: float(h.get(d, 0.0)) for d in days}


def meets_bar(hours: dict[str, float], min_hours=BAR_HOURS) -> bool:
    """Every element (t35 and t40) has >= min_hours at or above the limit across the slice."""
    return all(h >= min_hours for h in hours.values())


def feasible(relief: dict[str, float], capacity: dict[str, float], share=RELIEF_CAP_SHARE) -> bool:
    """Worst-hour relief on every element is at most `share` of its zone's sign-correct battery capacity."""
    return all(relief[e] <= share * capacity[e] for e in relief)


def smallest_passing(levels: list[dict]):
    """Lowest level that meets both the bar and feasibility, or None."""
    ok = [x for x in levels if x["meets_bar"] and x["feasible"]]
    return min(ok, key=lambda x: x["level"]) if ok else None


def pick(cands: dict, voltage_validated: bool = True):
    """Choose between sweeps B and C (A never). cands: sweep -> smallest passing level dict or None.
    Both pass: prefer the one with no voltage violations (only if voltage is validated) and no new
    out-of-zone overload; if that does not separate them, C (one disclosed new load is a smaller
    assumption than broad growth)."""
    b, c = cands.get("B"), cands.get("C")
    if b is None and c is None:
        return None
    if b is None or c is None:
        return ("B", b) if c is None else ("C", c)
    clean = lambda x: (not voltage_validated or x["voltage"]["bus_steps_out"] == 0) and not x["new_overloads"]
    if clean(b) and not clean(c):
        return "B", b
    return "C", c


def added_load_mw(load_kw, in_zone, bg_p, bg_bus, zone_buses, bg_scale, g: float, new_mw: float) -> np.ndarray:
    """(T,) MW added by a scenario: g x (zone agents' load + background load at zone buses, scaled per step)
    plus a flat new load."""
    agents = np.asarray(load_kw)[np.asarray(in_zone, bool)].sum(0) / 1e3
    bg = (np.asarray(bg_p) * np.isin(bg_bus, list(zone_buses))).sum() * np.asarray(bg_scale)
    return g * (agents + bg) + new_mw


def d31_gaps(m: dict, min_hours=BAR_HOURS, share=RELIEF_CAP_SHARE) -> dict:
    """How far a level is from D31: hours short of the bar per element, relief/capacity above the share."""
    bar = {e: m["zones"][e]["hours_ge_90"] - min_hours for e in ZONES}
    feas = {e: m["zones"][e]["relief_over_capacity"] - share for e in ZONES}
    return {"bar_hours_short": {k: -x for k, x in bar.items() if x < 0},
            "feasibility_excess": {k: x for k, x in feas.items() if x > 0},
            "worst_bar_gap_h": min(bar.values()), "worst_feasibility_excess": max(feas.values())}


def pick_2d(levels: list[dict]):
    """Passing combination (bar and feasible) with the least peak added MW; ties -> less t35 addition
    (growth, or new MW in the flexible-DC sweep). None if none."""
    ok = [x for x in levels if x["meets_bar"] and x["feasible"]]
    return min(ok, key=lambda x: (x["added_mw_peak"], x.get("t35_growth", x.get("t35_new_mw", 0.0)))) if ok else None


def zone_flex_mw(rep_zone: dict, ids, flex: dict[str, np.ndarray], ts) -> dict[str, pd.Series]:
    """PTDF-weighted, sign-correct flexible load per step (MW), one series per flex kind (dc_flex, ev).
    Only the zone's relief-correct agents count: deferring their load lowers the element's flow."""
    pos = {a: i for i, a in enumerate(ids)}
    w = np.zeros(len(ids))
    for a in rep_zone["agents"]:
        if a["relief_correct"]:
            w[pos[a["id"]]] = abs(a["ptdf"])
    return {k: pd.Series(w @ arr / 1e3, index=ts) for k, arr in flex.items()}


def new_dc_flex_mw(rep_zone: dict, bus: int, mw: float, share=NEW_DC_FLEX_SHARE) -> float:
    """Deferrable share of a new data center at `bus`, x |PTDF|, if `bus` is a relief-correct agent bus of the zone."""
    a = next((x for x in rep_zone["agents"] if x["bus"] == bus), None)
    return share * mw * abs(a["ptdf"]) if a is not None and a["relief_correct"] else 0.0


def capacity_mw(bess_mw: float, extra: pd.Series | None, at_utc) -> float:
    """Zone capacity at one step: sign-correct battery MW plus any time-varying extra (flexible load) MW then."""
    return bess_mw + (0.0 if extra is None else float(extra.loc[pd.Timestamp(at_utc)]))


def delta_check(ours, engie, min_r=DELTA_MIN_R, slope=DELTA_SLOPE) -> dict:
    """Hour-over-hour changes: ours regressed on Engie's. Series, or DataFrames pooled over columns
    (columns where Engie never changes are dropped). Pass if r >= min_r and slope inside [lo, hi]."""
    a, b = pd.DataFrame(ours).diff().iloc[1:], pd.DataFrame(engie).diff().iloc[1:]
    keep = [i for i in range(b.shape[1]) if b.iloc[:, i].std() > 0]
    a, b = a.iloc[:, keep].to_numpy().ravel(), b.iloc[:, keep].to_numpy().ravel()
    r = float(np.corrcoef(a, b)[0, 1])
    k = float(np.polyfit(b, a, 1)[0])
    return {"n": len(a), "series": len(keep), "pearson_r": r, "slope": k,
            "pass": bool(r >= min_r and slope[0] <= k <= slope[1])}


def anchor(ours: pd.DataFrame, ours0: pd.DataFrame, engie: pd.DataFrame, scale: dict | None = None) -> pd.DataFrame:
    """Engie's value + our change from level 0, each column's change divided by scale[col] (default 1).
    engie is hourly, same columns where available; the hour's offset (engie - ours0 on the hour) holds for
    every step in that hour. Columns Engie lacks stay raw."""
    k = pd.Series(scale or {}, dtype=float).reindex(ours.columns).fillna(1.0)
    adj = ours0 + (ours - ours0) / k
    off = (engie - ours0.reindex(engie.index)).reindex(columns=ours.columns)
    off = off.reindex(ours.index, method="ffill").fillna(0.0)
    return adj + off


# ---------- setup ----------

def zones(g, s, elements=ZONES):
    """M0 zones for `elements`, plus each zone's buses (|PTDF| >= PTDF_MIN) and PTDF rows."""
    br = branches(g)
    buses = g.buses.bus.to_numpy()
    P = ptdf(br, buses, g.slack_bus)
    row = {k: i for i, k in enumerate(br.key)}
    out = {}
    for e in elements:
        p = pd.Series(P[row[e]], index=buses)
        down = cut_off(br, buses, g.slack_bus, e)
        bus_of = s.meta.set_index("entity_id").bus
        sens = pd.Series([p[b] for b in bus_of], index=bus_of.index)
        members = sens[(sens.abs() >= PTDF_MIN) | bus_of.isin(down)]
        if len(members) != M0_ZONE_SIZES[e]:
            raise ValueError(f"{e}: {len(members)} zone agents, M0 found {M0_ZONE_SIZES[e]}")
        out[e] = {"ptdf_bus": p, "agents": members, "buses": set(p.index[p.abs() >= PTDF_MIN]),
                  "rating_mva": float(br.iloc[row[e]].rating_mva)}
    m0 = REPO / "runs" / "m0" / "zone_check.json"
    check = "runs/m0/zone_check.json not found; sizes checked against M0 counts"
    if m0.exists():
        z = json.loads(m0.read_text())["zones"]
        for e in elements:
            if set(a["id"] for a in z[e]["agents"]) != set(out[e]["agents"].index):
                raise ValueError(f"{e}: zone members differ from runs/m0/zone_check.json")
        check = "matches runs/m0/zone_check.json"
    return out, check


def baseline_gate(res, eng_load: pd.DataFrame, eng_vm: pd.DataFrame, steps: np.ndarray, gate_pp: float) -> dict:
    """Our level-0 run at the on-the-hour steps vs. Engie's hourly results (raw, not anchored)."""
    ours = res.loading().iloc[steps]
    cols = [c for c in ours.columns if c in eng_load]
    err = np.abs(ours[cols].to_numpy() - eng_load[cols].to_numpy())
    vcols = [b for b in res.vm_pu.columns if b in eng_vm]
    verr = np.abs(res.vm_pu.iloc[steps][vcols].to_numpy() - eng_vm[vcols].to_numpy())
    zone = {e: float(err[:, cols.index(e)].max()) for e in ZONES}
    return {
        "hours": len(steps), "converged": bool(res.converged[steps].all()),
        "zone_max_abs_err_pp": zone, "all_elements_mae_pp": float(np.nanmean(err)),
        "all_elements_max_abs_err_pp": float(np.nanmax(err)),
        "worst_element": cols[int(np.nanargmax(np.nanmax(err, 0)))],
        "vm_max_abs_err_pu": float(np.nanmax(verr)), "vm_mae_pu": float(np.nanmean(verr)),
        "thresholds": {"zone_loading_pp": gate_pp, "vm_pu": GATE_VM_PU},
        "pass": bool(res.converged[steps].all() and max(zone.values()) <= gate_pp and np.nanmax(verr) <= GATE_VM_PU),
    }


def calibrated_gate(base, src: dict, eng_load: pd.DataFrame, eng_vm: pd.DataFrame, steps: np.ndarray) -> dict:
    """Gate for calibrated_delta, built on a held-slack level-0 run (base) and its gate entry (src).
    Loading factors = src's fitted t35/t40 delta slopes; gated on r (the in-sample calibrated slope is 1 by
    construction), with an out-of-sample check (fit on day 1, test on day 2) reported. Voltage factor = the
    pooled vm delta slope; voltage counts as validated only if the day-1 factor passes r and slope on day 2.
    The variant passes on loading; an unvalidated voltage is flagged, not fatal."""
    ours_h = base.loading().iloc[steps].reset_index(drop=True)
    eng = eng_load.reset_index(drop=True)
    k = {e: src["delta_validation"][e]["slope"] for e in ZONES}
    loading = {e: delta_check(ours_h[e] / k[e], eng[e]) for e in ZONES}
    day = pd.Series(eng_load.index.tz_convert(TZ).strftime("%Y-%m-%d"))
    d1, d2 = sorted(day.unique())[:2]
    oos = {}
    for e in ZONES:
        k1 = delta_check(ours_h[e][day == d1], eng[e][day == d1])["slope"]
        t = delta_check(ours_h[e][day == d2] / k1, eng[e][day == d2])
        oos[e] = {"fit_day": d1, "test_day": d2, "factor_day1": k1, "test_pearson_r": t["pearson_r"],
                  "test_slope": t["slope"]}
    vcols = [b for b in base.vm_pu.columns if b in eng_vm]
    ov = base.vm_pu.iloc[steps][vcols].reset_index(drop=True)
    ev = eng_vm[vcols].reset_index(drop=True)
    vm = delta_check(ov, ev)                                    # uncalibrated, pooled over buses
    kv = vm["slope"]
    kv1 = delta_check(ov[day == d1], ev[day == d1])["slope"]
    vm_oos = delta_check(ov[day == d2] / kv1, ev[day == d2])    # same bars as loading
    vm_oos = {"fit_day": d1, "test_day": d2, "factor_day1": kv1, "test_pearson_r": vm_oos["pearson_r"],
              "test_slope": vm_oos["slope"], "pass": vm_oos["pass"]}
    ok = all(loading[e]["pass"] for e in ZONES)
    return {"built_on": src["variant"], "calibration_factors": k, "vm_calibration_factor": kv,
            "loading_delta_after_calibration": loading, "out_of_sample_day2": oos,
            "vm_delta_pooled_uncalibrated": vm, "vm_delta_after_calibration": delta_check(ov / kv, ev),
            "vm_out_of_sample_day2": vm_oos, "voltage_validated": bool(vm_oos["pass"]),
            "raw_errors_reported_not_gated": {"zone_max_abs_err_pp": src["zone_max_abs_err_pp"],
                                              "vm_max_abs_err_pu": src["vm_max_abs_err_pu"]},
            "pass": bool(ok)}


def zone_report(z, s, base, elements=ZONES):
    """Per zone: flow direction, each agent's sign and role, sign-correct battery capacity (MW).
    Flow direction comes from the watched element's p (trafo hv side, line from side)."""
    meta = s.meta.set_index("entity_id")
    rep = {}
    for e in elements:
        p = base.trafo_hv[e].p_hv_mw[base.converged]
        sign = float(np.sign(p.median()))
        agents = []
        for a, v in z[e]["agents"].items():
            relief = v * sign < 0          # injection (discharge) lowers |flow|
            agents.append({"id": a, "type": meta.at[a, "entity_type"], "bus": int(meta.at[a, "bus"]),
                           "ptdf": round(float(v), 4), "ptdf_sign": "+" if v > 0 else "-",
                           "bess_power_kw": float(meta.at[a, "bess_power_kw"]),
                           "relief_correct": bool(relief), "discharge_increases_flow": bool(not relief)})
        cap = sum(x["bess_power_kw"] * abs(x["ptdf"]) for x in agents if x["relief_correct"]) / 1e3
        rep[e] = {"flow_direction": "hv->lv" if sign > 0 else "lv->hv",
                  "flow_reverses_in_slice": bool((np.sign(p) != sign).any()),
                  "sign_correct_bess_mw": cap,
                  "n_discharge_increases_flow": sum(x["discharge_increases_flow"] for x in agents),
                  "agents": agents}
    return rep


def level_metrics(res, load, vm, sc, days, rep, ratings, base_bind, base_over, vm_lo, vm_hi, gen_max_mw,
                  voltage_note=None, cap_extra: dict | None = None):
    """Metrics for one level. load and vm are the reported (anchored) frames; res gives Q and vm_hv for relief.
    cap_extra: zone -> MW series added to battery capacity at the worst-relief step (flexible load)."""
    ok = res.converged
    load, vm = load[ok], vm[ok]
    m = {"sweep": sc.sweep, "level": sc.level, "nonconverged_steps": int((~ok).sum()), "zones": {}}
    by_day, relief, cap = {}, {}, {}
    for e in ZONES:
        s = load[e]
        hv = res.trafo_hv[e][ok]
        s_mva = s.to_numpy() / 100 * ratings[e] * hv.vm_hv_pu.to_numpy()
        p = np.sqrt(np.maximum(s_mva ** 2 - hv.q_hv_mvar.to_numpy() ** 2, 0.0))
        r = relief_mw(p, hv.q_hv_mvar, ratings[e], hv.vm_hv_pu, LIMIT_PCT)
        by_day[e] = hours_by_day(s >= LIMIT_PCT, days)
        relief[e] = float(r.max())
        worst = s.index[int(np.argmax(r))]
        bess = rep[e]["sign_correct_bess_mw"]
        cap[e] = capacity_mw(bess, (cap_extra or {}).get(e), worst)
        m["zones"][e] = {
            "max_pct": float(s.max()), "max_at_utc": str(s.idxmax()),
            "hours_ge_90": float((s >= LIMIT_PCT).sum() * 0.25), "hours_ge_100": float((s >= OVERLOAD_PCT).sum() * 0.25),
            "hours_ge_90_by_day_central": by_day[e],
            "hours_ge_100_by_day_central": hours_by_day(s >= OVERLOAD_PCT, days),
            "worst_relief_mw": relief[e], "worst_relief_at_utc": str(worst),
            "sign_correct_bess_mw": bess, "flex_mw_at_worst": cap[e] - bess, "capacity_mw": cap[e],
            "relief_over_capacity": relief[e] / cap[e] if cap[e] > 0 else float("inf"),
        }
    others = {}
    for c in load.columns:
        if c in ZONES or not (load[c] >= LIMIT_PCT).any():
            continue
        others[c] = {"max_pct": float(load[c].max()), "hours_ge_90": float((load[c] >= LIMIT_PCT).sum() * 0.25),
                     "hours_ge_100": float((load[c] >= OVERLOAD_PCT).sum() * 0.25),
                     "status": "already binding at level 0" if c in base_bind else "new"}
    m["other_elements"] = others
    m["new_overloads"] = sorted(c for c, v in others.items() if v["hours_ge_100"] > 0 and c not in base_over)
    out = (vm < vm_lo) | (vm > vm_hi)
    m["voltage"] = {"bus_steps_out": int(out.to_numpy().sum()), "hours_with_any": float(out.any(axis=1).sum() * 0.25),
                    "buses_out": sorted(int(b) for b in out.columns[out.any()]),
                    "vm_min_pu": float(vm.min().min()), "vm_max_pu": float(vm.max().max()),
                    "validated": voltage_note is None, "note": voltage_note}
    gt = res.gen_total_mw[ok]
    m["dispatch"] = {"gen_at_max_steps": int((gt >= gen_max_mw - 1e-6).sum()),
                     "gen_at_limit_steps": int(res.gen_at_limit[ok].sum()),
                     "gen_total_mw_max": float(np.nanmax(gt)), "gen_max_p_total_mw": gen_max_mw,
                     "ext_p_mw_max": float(np.nanmax(res.ext_p_mw[ok]))}
    m["meets_bar"] = meets_bar({e: m["zones"][e]["hours_ge_90"] for e in ZONES})
    m["feasible"] = feasible(relief, cap)
    return m


def scenarios(z, rep):
    flow_sign = {e: 1.0 if rep[e]["flow_direction"] == "hv->lv" else -1.0 for e in ZONES}
    agent_bus = {e: pd.Series([z[e]["ptdf_bus"][b["bus"]] for b in rep[e]["agents"]],
                              index=[b["bus"] for b in rep[e]["agents"]]) for e in ZONES}
    new_bus = {e: new_load_bus(agent_bus[e], flow_sign[e]) for e in ZONES}
    sw = {"A": [Scenario("A", g, agent_growth=g) for g in GROWTH],
          "B": [Scenario("B", g, agent_growth=g, bg_growth=g) for g in GROWTH],
          "C": [Scenario("C", mw, new_load_mw={new_bus[e]: mw for e in ZONES}) for mw in NEW_MW]}
    return sw, new_bus


def summary_row(m, **extra):
    row = {"sweep": m["sweep"], "level": m["level"], **extra}
    for e in ZONES:
        ze = m["zones"][e]
        row |= {f"{e}_max_pct": ze["max_pct"], f"{e}_h_ge_90": ze["hours_ge_90"], f"{e}_h_ge_100": ze["hours_ge_100"],
                **{f"{e}_h_ge_90_{d}": h for d, h in ze["hours_ge_90_by_day_central"].items()},
                f"{e}_relief_mw": ze["worst_relief_mw"], f"{e}_relief_over_cap": ze["relief_over_capacity"]}
    return row | {"other_binding": " ".join(f"{c}({o['hours_ge_90']}h)" for c, o in m["other_elements"].items()),
                  "new_overloads": " ".join(m["new_overloads"]), "voltage_bus_steps_out": m["voltage"]["bus_steps_out"],
                  "voltage_note": m["voltage"]["note"] or "validated",
                  "vm_min_pu": m["voltage"]["vm_min_pu"], "vm_max_pu": m["voltage"]["vm_max_pu"],
                  "gen_at_max_steps": m["dispatch"]["gen_at_max_steps"], "nonconverged": m["nonconverged_steps"],
                  "meets_bar": m["meets_bar"], "feasible": m["feasible"]}


# ---------- main ----------

MODES = {"1d": OUT, "grid2d": OUT / "grid2d", "grid2d_flex": OUT / "grid2d_flex", "lines": OUT / "lines"}


def main(cfg_path, mode="1d"):
    tic = time.time()
    out = MODES[mode]
    cfg = load_config(cfg_path)
    g = load_grid(cfg["data_dir"])
    s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
    net = load_pp_net(cfg["data_dir"])
    ref = load_powerflow(cfg["data_dir"])
    ref = ref[(ref.index >= s.ts_utc[0]) & (ref.index <= s.ts_utc[-1])]
    steps = s.ts_utc.get_indexer(ref.index)
    if (steps < 0).any():
        raise ValueError("power-flow hours do not line up with the slice")
    eng_load = ref.filter(regex=r"^loading_pct_[lt]\d+$").rename(columns=lambda c: c.removeprefix("loading_pct_"))
    eng_vm = ref.filter(regex=r"^vm_pu_b\d+$").rename(columns=lambda c: int(c.removeprefix("vm_pu_b")))
    slack_target = ref.slack_p_mw.reindex(s.ts_utc, method="ffill").to_numpy()
    days = sorted(set(s.ts_utc.tz_convert(TZ).strftime("%Y-%m-%d")))

    z, zone_check = zones(g, s)
    in_zone = s.meta.entity_id.isin(set().union(*(set(z[e]["agents"].index) for e in ZONES))).to_numpy()
    zone_buses = set().union(*(z[e]["buses"] for e in ZONES))
    ratings = {e: z[e]["rating_mva"] for e in ZONES}
    bg_q_ratio = float(net.load.q_mvar.sum() / net.load.p_mw.sum())
    args = dict(pnet_kw=s.agent["pnet"], load_kw=s.agent["load"], in_zone=in_zone, zone_buses=zone_buses,
                bg_scale=s.system["bg_load_scale"], index=s.ts_utc)
    out.mkdir(parents=True, exist_ok=True)

    # 1. baseline gate (+ delta validation for the anchored variant)
    gate, chosen, held = [], None, []
    for v in VARIANTS:
        q = bg_q_ratio if v["agent_q_ratio"] is None else v["agent_q_ratio"]
        disp = {"slack_target_mw": slack_target} if v["dispatch"] == "held_slack" else {}
        grid0 = ACGrid(net, s.ids, s.meta.bus, agent_q_ratio=q, distributed_slack=v["distributed_slack"],
                       gen_split=v["split"])
        base = grid0.run_scenario(Scenario("A", 0.0), **args, **disp)
        entry = {"variant": v["name"], "dispatch": v["dispatch"], "split": v["split"], "anchored": v["anchored"],
                 "agent_q_ratio": q, **baseline_gate(base, eng_load, eng_vm, steps, v["gate_pp"])}
        if v["dispatch"] == "held_slack":
            on_hour = base.ext_p_mw[steps] - ref.slack_p_mw.to_numpy()
            entry["slack_hold_max_abs_err_mw"] = float(np.nanmax(np.abs(on_hour)))
            entry["gen_at_max_steps"] = int((base.gen_total_mw >= grid0.gen_max.sum() - 1e-6).sum())
        if v["anchored"]:
            ours_h = base.loading().iloc[steps]
            entry["delta_validation"] = {e: delta_check(ours_h[e].reset_index(drop=True),
                                                        eng_load[e].reset_index(drop=True)) for e in ZONES}
            entry["delta_validation_pass"] = all(d["pass"] for d in entry["delta_validation"].values())
            entry["pass"] = entry["pass"] and entry["delta_validation_pass"]
            held.append((v, q, disp, base, grid0.gen_max.sum(), entry))
        gate.append(entry)
        dv = entry.get("delta_validation")
        print(f"gate {v['name']}: zone max err {({k: round(x, 2) for k, x in entry['zone_max_abs_err_pp'].items()})}, "
              f"vm max err {entry['vm_max_abs_err_pu']:.4f}"
              + (f", delta {({e: (round(d['pearson_r'], 3), round(d['slope'], 3)) for e, d in dv.items()})}" if dv else "")
              + f", pass {entry['pass']}")
        if entry["pass"]:
            chosen = (v, q, disp, base, grid0.gen_max.sum(), None)
            break
    if not chosen and held:
        v, q, disp, base, gmax, src = max(held, key=lambda h: min(d["pearson_r"] for d in h[5]["delta_validation"].values()))
        entry = {"variant": CALIBRATED, **calibrated_gate(base, src, eng_load, eng_vm, steps)}
        gate.append(entry)
        print(f"gate {CALIBRATED} (on {src['variant']}): factors {({e: round(x, 3) for e, x in entry['calibration_factors'].items()})}, "
              f"loading r {({e: round(d['pearson_r'], 3) for e, d in entry['loading_delta_after_calibration'].items()})}, "
              f"vm factor {entry['vm_calibration_factor']:.3f}, vm day-2 r {entry['vm_out_of_sample_day2']['test_pearson_r']:.3f} "
              f"slope {entry['vm_out_of_sample_day2']['test_slope']:.3f}, voltage validated {entry['voltage_validated']}, "
              f"pass {entry['pass']}")
        if entry["pass"]:
            chosen = ({**v, "name": CALIBRATED}, q, disp, base, gmax,
                      {"loading": entry["calibration_factors"], "voltage_validated": entry["voltage_validated"],
                       "vm": entry["vm_calibration_factor"] if entry["voltage_validated"] else None,
                       "vm_fitted_not_applied": None if entry["voltage_validated"] else entry["vm_calibration_factor"]})
    (out / "baseline_validation.json").write_text(json.dumps(
        {"disclosed_assumptions": {"dispatch_rule": DISPATCH_RULE, "anchoring": ANCHOR_RULE,
                                   "delta_validation": f"hour-over-hour change in t35/t40 loading, ours regressed on "
                                                       f"Engie's over the slice hours: r >= {DELTA_MIN_R}, slope in "
                                                       f"[{DELTA_SLOPE[0]}, {DELTA_SLOPE[1]}]",
                                   "calibration": CALIBRATION_RULE,
                                   "supply_sensitivity": "at the pick only: gens held at their level-0 total per step, "
                                                         "ext_grid serves the growth"},
         "variants": gate, "chosen": chosen[0]["name"] if chosen else None,
         "calibration_factors": chosen[5] if chosen else None}, indent=2))
    levels_cfg = {"1d": {"growth": GROWTH, "new_mw": NEW_MW},
                  "grid2d": {"t35_growth": GRID2D_T35_GROWTH, "t40_new_mw": GRID2D_T40_MW},
                  "grid2d_flex": {"t35_new_dc_mw": FLEX_T35_MW, "t40_new_dc_mw": FLEX_T40_MW,
                                  "new_dc_flex_share": NEW_DC_FLEX_SHARE},
                  "lines": {"elements": LINE_ZONES, "level": 0}}[mode]
    header = run_header("bench/stress_sweep.py", {**cfg, **levels_cfg, "mode": mode,
                                                  "variant": chosen[0]["name"] if chosen else None,
                                                  "calibration_factors": chosen[5] if chosen else None})
    (out / "run.json").write_text(json.dumps(header, indent=2))
    if not chosen:
        raise SystemExit(f"baseline gate failed for every variant; see {out / 'baseline_validation.json'}")
    v, q, disp, base, gen_max_mw, factors = chosen
    base_load, base_vm = base.loading(), base.vm_pu

    def reported(res):
        if not v["anchored"]:
            return res.loading(), res.vm_pu
        lf = factors["loading"] if factors else None
        vf = {b: factors["vm"] for b in res.vm_pu.columns} if factors and factors["vm"] else None
        return anchor(res.loading(), base_load, eng_load, scale=lf), anchor(res.vm_pu, base_vm, eng_vm, scale=vf)

    # 2. zone report
    rep = zone_report(z, s, base)
    sw, new_bus = scenarios(z, rep)
    (out / "zones.json").write_text(json.dumps(
        {"zone_check": zone_check, "union_agents": int(in_zone.sum()), "zone_buses_for_B": sorted(int(b) for b in zone_buses),
         "new_load_bus_for_C": new_bus, "new_load_pf": NEW_LOAD_PF, "zones": rep}, indent=2))

    # 3. sweeps
    grid = ACGrid(net, s.ids, s.meta.bus, new_load_buses=sorted(set(new_bus.values())),
                  agent_q_ratio=q, distributed_slack=v["distributed_slack"], gen_split=v["split"])
    b_load, b_vm = reported(base)
    b_load = b_load[base.converged]
    base_bind = set(b_load.columns[(b_load >= LIMIT_PCT).any()])
    base_over = set(b_load.columns[(b_load >= OVERLOAD_PCT).any()])
    vm_lo = net.bus.min_vm_pu.reindex(base.vm_pu.columns).to_numpy()
    vm_hi = net.bus.max_vm_pu.reindex(base.vm_pu.columns).to_numpy()
    voltage_ok = factors is None or factors["voltage_validated"]
    vnote = None if voltage_ok else f"not validated (slope {factors['vm_fitted_not_applied']:.3f})"
    mkw = dict(days=days, rep=rep, ratings=ratings, base_bind=base_bind, base_over=base_over,
               vm_lo=vm_lo, vm_hi=vm_hi, gen_max_mw=gen_max_mw, voltage_note=vnote)
    if mode == "grid2d":
        return main_grid2d(out, header, tic, z, new_bus, grid, base, args, disp, reported, mkw, s, v, factors, voltage_ok)
    if mode == "lines":
        return main_lines(out, header, tic, g, cfg, grid, args, disp, reported, s, factors)
    if mode == "grid2d_flex":
        return main_grid2d_flex(out, header, tic, cfg, rep, new_bus, grid, base, args, disp, reported, mkw, s,
                                factors, voltage_ok)
    results, runs = {}, {}
    for name, levels in sw.items():
        results[name] = []
        for sc in levels:
            res = grid.run_scenario(sc, **args, **disp)
            runs[(name, sc.level)] = res
            results[name].append(level_metrics(res, *reported(res), sc, **mkw))
            r = results[name][-1]
            print(f"{name} {sc.level:>5}: t35 {r['zones']['t35']['max_pct']:.1f}% {r['zones']['t35']['hours_ge_90']}h, "
                  f"t40 {r['zones']['t40']['max_pct']:.1f}% {r['zones']['t40']['hours_ge_90']}h, "
                  f"gens at max {r['dispatch']['gen_at_max_steps']}")
    lvl0 = [runs[(k, 0.0)].loading().to_numpy() for k in sw]
    level0_identical = all(np.allclose(a, lvl0[0], equal_nan=True, atol=1e-6) for a in lvl0)
    gate_vs_sweep = np.allclose(lvl0[0], base.loading().to_numpy(), equal_nan=True, atol=1e-6)

    # 4. rules
    smallest = {k: smallest_passing(x) for k, x in results.items()}
    chosen_pick = pick({k: smallest[k] for k in ("B", "C")}, voltage_validated=voltage_ok)
    summary = {k: (None if x is None else x["level"]) for k, x in smallest.items()}

    # 5. supply sensitivity at the pick: gens at their level-0 total, ext_grid serves the growth
    supply = None
    if chosen_pick is not None:
        sc = next(x for x in sw[chosen_pick[0]] if x.level == chosen_pick[1]["level"])
        res = grid.run_scenario(sc, **args, gen_total_mw=base.gen_total_mw)
        runs[(f"{sc.sweep}_ext_grid_supply", sc.level)] = res
        alt = level_metrics(res, *reported(res), sc, **mkw)
        supply = {"scenario": {"sweep": sc.sweep, "level": sc.level},
                  "gens_serve_growth": chosen_pick[1], "ext_grid_serves_growth": alt,
                  "d31_holds_under_ext_grid_supply": bool(alt["meets_bar"] and alt["feasible"])}

    result = {
        "header": header,
        "method": {
            "power_flow": f"pandapower AC Newton-Raphson, variant {v['name']}, every 15-min step of the slice",
            "dispatch": DISPATCH_RULE if v["dispatch"] == "held_slack" else v["dispatch"],
            "gen_split": v["split"],
            "reported_values": ANCHOR_RULE if v["anchored"] else "raw",
            "calibration": {"rule": CALIBRATION_RULE, "factors": factors} if factors else None,
            "agents": "one load per agent at its bus, p = -p_net/1000",
            "hours": "15-min steps x 0.25; Central calendar days",
            "relief": "hv side, Q fixed, P from the reported loading: |P| - sqrt((0.9 sn vm_hv)^2 - Q^2)",
            "capacity": "sum bess_power_kw x |PTDF| over the zone's relief-correct agents (ignores SOC and energy)",
            "bar": f">= {BAR_HOURS} h >= {LIMIT_PCT:.0f}% across the slice, each of t35 and t40",
            "feasible": f"worst-hour relief <= {RELIEF_CAP_SHARE} x sign-correct capacity, t35 and t40",
            "pick": "B or C only; both pass -> the clean one (no new out-of-zone overload, and no voltage violations "
                    "if voltage is validated); tie -> C",
            "voltage": "validated" if voltage_ok else vnote,
        },
        "checks": {"level0_identical_across_sweeps": bool(level0_identical), "level0_matches_gate_run": bool(gate_vs_sweep)},
        "sweeps": results,
        "smallest_passing": summary,
        "pick": None if chosen_pick is None else {"sweep": chosen_pick[0], "level": chosen_pick[1]["level"]},
        "supply_sensitivity": supply,
        "runtime_s": time.time() - tic,
    }
    (out / "sweep.json").write_text(json.dumps(result, indent=2))

    rows = [summary_row(m, supply="gens") for x in results.values() for m in x]
    if supply:
        rows.append(summary_row(supply["ext_grid_serves_growth"], supply="ext_grid"))
    pd.DataFrame(rows).to_csv(out / "sweep_summary.csv", index=False)

    ever = sorted(set(ZONES).union(*(set(m["other_elements"]) for x in results.values() for m in x)))
    long = []
    for (name, level), res in runs.items():
        ld, vm = reported(res)
        d = ld[ever].copy()
        d["vm_min_pu"], d["vm_max_pu"] = vm.min(axis=1), vm.max(axis=1)
        d["gen_total_mw"], d["ext_p_mw"] = res.gen_total_mw, res.ext_p_mw
        d.insert(0, "t_central", d.index.tz_convert(TZ).strftime("%Y-%m-%d %H:%M"))
        d.insert(0, "t_utc", d.index.strftime("%Y-%m-%dT%H:%M:%SZ"))
        d.insert(0, "level", level)
        d.insert(0, "sweep", name)
        long.append(d)
    pd.concat(long).to_csv(out / "loading_steps.csv", index=False)

    print(f"checks: {result['checks']}")
    print(f"smallest passing: {summary}")
    print(f"pick: {result['pick']}   ({result['runtime_s']:.0f} s)")
    if supply:
        print(f"supply sensitivity: D31 holds with ext_grid serving growth: {supply['d31_holds_under_ext_grid_supply']}")


def main_grid2d(out, header, tic, z, new_bus, grid, base, args, disp, reported, mkw, s, v, factors, voltage_ok):
    """2D sweep: t35-only zone load growth (B style) x a new load at t40's C bus. Same gate, calibration
    and D31 rules as the 1D sweeps; pick = least peak added MW among passing combinations."""
    t35_agents = s.meta.entity_id.isin(set(z["t35"]["agents"].index)).to_numpy()
    t35_buses = z["t35"]["buses"]
    a35 = {**args, "in_zone": t35_agents, "zone_buses": t35_buses}
    bus40 = new_bus["t40"]
    results, runs, i = [], {}, 0
    for g in GRID2D_T35_GROWTH:
        for mw in GRID2D_T40_MW:
            sc = Scenario("D", float(i), agent_growth=g, bg_growth=g, new_load_mw={bus40: mw})
            res = grid.run_scenario(sc, **a35, **disp)
            m = level_metrics(res, *reported(res), sc, **mkw)
            add = added_load_mw(s.agent["load"], t35_agents, grid.bg_p, grid.bg_bus, t35_buses,
                                s.system["bg_load_scale"], g, mw)
            m |= {"t35_growth": g, "t40_new_mw": mw, "added_mw_peak": float(add.max()), "added_mw_mean": float(add.mean())}
            m["gaps"] = d31_gaps(m)
            results.append(m)
            runs[(g, mw)] = res
            r = m["zones"]
            print(f"t35 +{g:.0%} / t40 {mw:>4.0f} MW: t35 {r['t35']['hours_ge_90_by_day_central']} rc {r['t35']['relief_over_capacity']:.2f}, "
                  f"t40 {r['t40']['hours_ge_90_by_day_central']} rc {r['t40']['relief_over_capacity']:.2f}, "
                  f"bar {m['meets_bar']} feas {m['feasible']}")
            i += 1
    best = pick_2d(results)
    l0 = runs[(0.0, 0.0)].loading().to_numpy()
    level0_matches_gate = bool(np.allclose(l0, base.loading().to_numpy(), equal_nan=True, atol=1e-6))

    supply = None
    if best is not None:
        sc = Scenario("D", best["level"], agent_growth=best["t35_growth"], bg_growth=best["t35_growth"],
                      new_load_mw={bus40: best["t40_new_mw"]})
        res = grid.run_scenario(sc, **a35, gen_total_mw=base.gen_total_mw)
        alt = level_metrics(res, *reported(res), sc, **mkw)
        supply = {"combination": {"t35_growth": best["t35_growth"], "t40_new_mw": best["t40_new_mw"]},
                  "ext_grid_serves_growth": alt, "d31_holds_under_ext_grid_supply": bool(alt["meets_bar"] and alt["feasible"])}

    bar_ok = [x for x in results if x["meets_bar"]]
    feas_ok = [x for x in results if x["feasible"]]
    closest = {
        "meets_bar_least_feasibility_excess": None if not bar_ok else
            {k: min(bar_ok, key=lambda x: x["gaps"]["worst_feasibility_excess"])[k] for k in ("t35_growth", "t40_new_mw", "gaps")},
        "feasible_least_bar_shortfall": None if not feas_ok else
            {k: max(feas_ok, key=lambda x: x["gaps"]["worst_bar_gap_h"])[k] for k in ("t35_growth", "t40_new_mw", "gaps")},
    }
    result = {
        "header": header,
        "method": {"grid": "t35 growth: agents' load and background loads at t35 zone buses x (1 + g), t35 zone only; "
                           f"t40: one new flat load at bus {bus40} (pf {NEW_LOAD_PF})",
                   "added_load": "peak over the slice of g x (t35 zone agent load + t35 zone background load) + new MW",
                   "pick": "meets bar and feasible; least peak added MW; tie -> less t35 growth",
                   "calibration": {"rule": CALIBRATION_RULE, "factors": factors} if factors else None,
                   "voltage": "validated" if voltage_ok else "not validated"},
        "checks": {"level0_matches_gate_run": level0_matches_gate},
        "combinations": results,
        "pick": None if best is None else {k: best[k] for k in ("t35_growth", "t40_new_mw", "added_mw_peak")},
        "closest_if_none": closest if best is None else None,
        "supply_sensitivity": supply,
        "runtime_s": time.time() - tic,
    }
    (out / "sweep.json").write_text(json.dumps(result, indent=2))
    rows = [summary_row(m, t35_growth=m["t35_growth"], t40_new_mw=m["t40_new_mw"], added_mw_peak=m["added_mw_peak"],
                        worst_bar_gap_h=m["gaps"]["worst_bar_gap_h"],
                        worst_feasibility_excess=m["gaps"]["worst_feasibility_excess"]) for m in results]
    pd.DataFrame(rows).to_csv(out / "sweep_summary.csv", index=False)
    print(f"checks: {result['checks']}")
    print(f"pick: {result['pick']}   ({result['runtime_s']:.0f} s)")
    if best is None:
        print(f"closest: {json.dumps(closest)}")


def rescore(result: dict, cap_extra_fn=None) -> dict:
    """Apply the current D31 rules to a saved 1D sweep.json (no power flow): bar from saved hours_ge_90,
    feasibility from saved worst-hour relief against capacity at that step, then smallest_passing and pick.
    cap_extra_fn(sweep, level) -> {zone: MW series} adds flexible load to battery capacity."""
    voltage_ok = result["method"].get("voltage", "validated") == "validated"
    sweeps = {}
    for name, levels in result["sweeps"].items():
        sweeps[name] = []
        for m in levels:
            extra = cap_extra_fn(name, m["level"]) if cap_extra_fn else {}
            z = {}
            for e in ZONES:
                ze = m["zones"][e]
                cap = capacity_mw(ze["sign_correct_bess_mw"], extra.get(e), ze["worst_relief_at_utc"])
                z[e] = {**ze, "capacity_mw": cap, "flex_mw_at_worst": cap - ze["sign_correct_bess_mw"],
                        "relief_over_capacity": ze["worst_relief_mw"] / cap if cap > 0 else float("inf")}
            m = {**m, "zones": z, "meets_bar": meets_bar({e: z[e]["hours_ge_90"] for e in ZONES}),
                 "feasible": feasible({e: z[e]["worst_relief_mw"] for e in ZONES},
                                      {e: z[e]["capacity_mw"] for e in ZONES})}
            m["gaps"] = d31_gaps(m)
            sweeps[name].append(m)
    smallest = {k: smallest_passing(x) for k, x in sweeps.items()}
    chosen = pick({k: smallest[k] for k in ("B", "C")}, voltage_validated=voltage_ok)
    return {"sweeps": sweeps, "smallest_passing": {k: None if x is None else x["level"] for k, x in smallest.items()},
            "pick": None if chosen is None else {"sweep": chosen[0], "level": chosen[1]["level"]}}


def line_d31(load: pd.Series, flow: pd.DataFrame, rating_mva: float, bess_mw: float, extra: pd.Series) -> dict:
    """D31 for one element at one level: hours >= 90% across the slice, worst-step relief (from the reported
    loading, Q and vm held at the modelled values) against capacity (batteries + flexible load) at that step."""
    s_mva = load.to_numpy() / 100 * rating_mva * flow.vm_hv_pu.to_numpy()
    p = np.sqrt(np.maximum(s_mva ** 2 - flow.q_hv_mvar.to_numpy() ** 2, 0.0))
    r = relief_mw(p, flow.q_hv_mvar, rating_mva, flow.vm_hv_pu, LIMIT_PCT)
    worst = load.index[int(np.argmax(r))]
    cap = capacity_mw(bess_mw, extra, worst)
    hours = float((load >= LIMIT_PCT).sum() * 0.25)
    relief = float(r.max())
    return {"hours_ge_90": hours, "hours_ge_100": float((load >= OVERLOAD_PCT).sum() * 0.25),
            "max_pct": float(load.max()), "worst_relief_mw": relief, "worst_relief_at_utc": str(worst),
            "sign_correct_bess_mw": bess_mw, "flex_mw_at_worst": cap - bess_mw, "capacity_mw": cap,
            "relief_over_capacity": relief / cap if cap > 0 else float("inf"),
            "meets_bar": meets_bar({"e": hours}), "feasible": feasible({"e": relief}, {"e": cap})}


def base_flex(cfg, s, rep, elements=ZONES) -> tuple[dict[str, pd.Series], dict]:
    """Existing PTDF-weighted, sign-correct flexible load per zone (MW series), plus a summary for disclosure."""
    ids, fl = load_flex(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
    if not np.array_equal(ids, s.ids):
        raise ValueError("flex columns do not line up with the slice agents")
    parts = {e: zone_flex_mw(rep[e], ids, fl, s.ts_utc) for e in elements}
    tot = {e: parts[e]["dc_flex"] + parts[e]["ev"] for e in elements}
    summary = {e: {k: {"min_mw": float(v.min()), "mean_mw": float(v.mean()), "max_mw": float(v.max())}
                   for k, v in {**parts[e], "total": tot[e]}.items()} for e in elements}
    return tot, summary


def main_rescore(cfg_path, flex=False):
    cfg = load_config(cfg_path)
    src = json.loads((OUT / "sweep.json").read_text())
    fn, flex_info = None, None
    if flex:
        zj = json.loads((OUT / "zones.json").read_text())
        rep, new_bus = zj["zones"], {e: int(b) for e, b in zj["new_load_bus_for_C"].items()}
        s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
        tot, summary = base_flex(cfg, s, rep)

        def fn(sweep, level):
            new = {e: sum(new_dc_flex_mw(rep[e], b, level) for b in new_bus.values()) if sweep == "C" else 0.0
                   for e in ZONES}
            return {e: tot[e] + new[e] for e in ZONES}
        flex_info = {"rule": FLEX_RULE, "existing_flex_by_zone": summary, "new_load_bus_for_C": new_bus,
                     "new_dc_mw_per_mw_by_zone": {e: {str(b): new_dc_flex_mw(rep[e], b, 1.0) for b in new_bus.values()}
                                                  for e in ZONES}}
    res = rescore(src, fn)
    name = "sweep_rescored_flex.json" if flex else "sweep_rescored.json"
    mode = "rescore_flex" if flex else "rescore"
    out = {"header": run_header(f"bench/stress_sweep.py --{mode.replace('_', '-')}", {**cfg, "mode": mode}),
           "source": {"file": "runs/stress/sweep.json", "header": src["header"]},
           "rules": {"bar": f">= {BAR_HOURS} h >= {LIMIT_PCT:.0f}% across the slice, each of t35 and t40",
                     "feasible": f"worst-hour relief <= {RELIEF_CAP_SHARE} x capacity at that step, t35 and t40",
                     "capacity": FLEX_RULE if flex else "sign-correct battery MW only"},
           "flex": flex_info, **res}
    (OUT / name).write_text(json.dumps(out, indent=2))
    print(f"smallest passing: {res['smallest_passing']}  pick: {res['pick']}")


def main_lines(out, header, tic, g, cfg, grid, args, disp, reported, s, factors):
    """D31 at level 0 (no growth) for the thin-zone lines l43 and l26, with zone agents' batteries plus flexible
    load as capacity. Reported loading is anchored to Engie's (the loading factors only scale t35/t40)."""
    zl, check = zones(g, s, LINE_ZONES)
    res = grid.run_scenario(Scenario("A", 0.0), **args, **disp, watch=("t35", "t40", *LINE_ZONES))
    load = reported(res)[0][res.converged]
    rep = zone_report(zl, s, res, LINE_ZONES)
    tot, summary = base_flex(cfg, s, rep, LINE_ZONES)
    lines = {}
    for e in LINE_ZONES:
        d = line_d31(load[e], res.trafo_hv[e][res.converged], zl[e]["rating_mva"], rep[e]["sign_correct_bess_mw"], tot[e])
        lines[e] = {**d, "rating_mva": zl[e]["rating_mva"], "flow_direction": rep[e]["flow_direction"],
                    "agents": rep[e]["agents"], "flex_by_kind": summary[e]}
        print(f"{e}: {d['hours_ge_90']} h >= 90% (max {d['max_pct']:.1f}%), worst relief {d['worst_relief_mw']:.2f} MW, "
              f"capacity {d['capacity_mw']:.3f} MW ({d['sign_correct_bess_mw']:.3f} batt + {d['flex_mw_at_worst']:.3f} flex), "
              f"ratio {d['relief_over_capacity']:.1f}, bar {d['meets_bar']} feasible {d['feasible']}")
    result = {"header": header, "zone_check": check,
              "method": {"level": "0 (no growth)", "capacity": FLEX_RULE, "bar": f">= {BAR_HOURS} h >= {LIMIT_PCT:.0f}%",
                         "feasible": f"worst-step relief <= {RELIEF_CAP_SHARE} x capacity at that step",
                         "relief": "from side, Q fixed: |P| - sqrt((0.9 rating vm_from)^2 - Q^2)",
                         "calibration": {"rule": CALIBRATION_RULE, "factors": factors} if factors else None},
              "lines": lines, "runtime_s": time.time() - tic}
    (out / "lines.json").write_text(json.dumps(result, indent=2))


def main_grid2d_flex(out, header, tic, cfg, rep, new_bus, grid, base, args, disp, reported, mkw, s, factors, voltage_ok):
    """Per-zone flexible data centers: a new data center at t35's and at t40's C bus, each NEW_DC_FLEX_SHARE
    deferrable. Same gate, calibration and D31 rules; capacity adds existing and new flexible load.
    Pick = least added MW."""
    tot, summary = base_flex(cfg, s, rep)
    b35, b40 = new_bus["t35"], new_bus["t40"]
    results, runs, i = [], {}, 0
    for mw35 in FLEX_T35_MW:
        for mw40 in FLEX_T40_MW:
            sc = Scenario("E", float(i), new_load_mw={b35: mw35, b40: mw40})
            extra = {e: tot[e] + new_dc_flex_mw(rep[e], b35, mw35) + new_dc_flex_mw(rep[e], b40, mw40) for e in ZONES}
            res = grid.run_scenario(sc, **args, **disp)
            m = level_metrics(res, *reported(res), sc, **mkw, cap_extra=extra)
            m |= {"t35_new_mw": mw35, "t40_new_mw": mw40, "added_mw_peak": mw35 + mw40, "added_mw_mean": mw35 + mw40}
            m["gaps"] = d31_gaps(m)
            results.append(m)
            runs[(mw35, mw40)] = res
            r = m["zones"]
            print(f"t35 {mw35:>3.0f} MW / t40 {mw40:>3.0f} MW: t35 {r['t35']['hours_ge_90']}h "
                  f"rc {r['t35']['relief_over_capacity']:.2f}, t40 {r['t40']['hours_ge_90']}h "
                  f"rc {r['t40']['relief_over_capacity']:.2f}, bar {m['meets_bar']} feas {m['feasible']}")
            i += 1
    best = pick_2d(results)
    level0_matches_gate = bool(np.allclose(runs[(0.0, 0.0)].loading().to_numpy(), base.loading().to_numpy(),
                                           equal_nan=True, atol=1e-6))
    supply = None
    if best is not None:
        sc = Scenario("E", best["level"], new_load_mw={b35: best["t35_new_mw"], b40: best["t40_new_mw"]})
        extra = {e: tot[e] + new_dc_flex_mw(rep[e], b35, best["t35_new_mw"])
                 + new_dc_flex_mw(rep[e], b40, best["t40_new_mw"]) for e in ZONES}
        res = grid.run_scenario(sc, **args, gen_total_mw=base.gen_total_mw)
        alt = level_metrics(res, *reported(res), sc, **mkw, cap_extra=extra)
        supply = {"combination": {"t35_new_mw": best["t35_new_mw"], "t40_new_mw": best["t40_new_mw"]},
                  "ext_grid_serves_growth": alt,
                  "d31_holds_under_ext_grid_supply": bool(alt["meets_bar"] and alt["feasible"])}
    bar_ok = [x for x in results if x["meets_bar"]]
    feas_ok = [x for x in results if x["feasible"]]
    keys = ("t35_new_mw", "t40_new_mw", "gaps")
    closest = {
        "meets_bar_least_feasibility_excess": None if not bar_ok else
            {k: min(bar_ok, key=lambda x: x["gaps"]["worst_feasibility_excess"])[k] for k in keys},
        "feasible_least_bar_shortfall": None if not feas_ok else
            {k: max(feas_ok, key=lambda x: x["gaps"]["worst_bar_gap_h"])[k] for k in keys},
    }
    result = {
        "header": header,
        "method": {"grid": f"new flat data-center load at bus {b35} (t35) and bus {b40} (t40), pf {NEW_LOAD_PF}, "
                           f"{NEW_DC_FLEX_SHARE:.0%} deferrable",
                   "capacity": FLEX_RULE, "existing_flex_by_zone": summary,
                   "added_load": "t35 MW + t40 MW (flat)",
                   "pick": "meets bar and feasible; least added MW; tie -> less t35 MW",
                   "calibration": {"rule": CALIBRATION_RULE, "factors": factors} if factors else None,
                   "voltage": "validated" if voltage_ok else "not validated"},
        "checks": {"level0_matches_gate_run": level0_matches_gate},
        "combinations": results,
        "pick": None if best is None else {k: best[k] for k in ("t35_new_mw", "t40_new_mw", "added_mw_peak")},
        "closest_if_none": closest if best is None else None,
        "supply_sensitivity": supply,
        "runtime_s": time.time() - tic,
    }
    (out / "sweep.json").write_text(json.dumps(result, indent=2))
    rows = [summary_row(m, t35_new_mw=m["t35_new_mw"], t40_new_mw=m["t40_new_mw"],
                        t35_capacity_mw=m["zones"]["t35"]["capacity_mw"], t40_capacity_mw=m["zones"]["t40"]["capacity_mw"],
                        worst_bar_gap_h=m["gaps"]["worst_bar_gap_h"],
                        worst_feasibility_excess=m["gaps"]["worst_feasibility_excess"]) for m in results]
    pd.DataFrame(rows).to_csv(out / "sweep_summary.csv", index=False)
    print(f"checks: {result['checks']}")
    print(f"pick: {result['pick']}   ({result['runtime_s']:.0f} s)")
    if best is None:
        print(f"closest: {json.dumps(closest)}")


if __name__ == "__main__":
    known = {"--grid2d", "--grid2d-flex", "--lines", "--rescore", "--rescore-flex"}
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    paths = [a for a in sys.argv[1:] if not a.startswith("--")]
    if flags - known or len(flags) > 1:
        raise SystemExit(f"use at most one of {sorted(known)}; got {sorted(flags)}")
    cfg_path = paths[0] if paths else REPO / "configs" / "dev_2day.yaml"
    if flags & {"--rescore", "--rescore-flex"}:
        main_rescore(cfg_path, flex="--rescore-flex" in flags)
    else:
        main(cfg_path, mode=next((f[2:].replace("-", "_") for f in flags), "1d"))
