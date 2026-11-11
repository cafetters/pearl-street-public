"""
M0: perfect-foresight ceiling on the dev slice, the dual of its peak cap, and the hourly-vs-15-min loss.

Reuses the LP in bench/bench.py unchanged:
  peak-min : fleet minimizes the system net-load peak over the slice
  value    : fleet maximizes revenue subject to net load <= peak-min peak x 1.01 (bench/README.md rule)
Both start and end at the dataset's initial SOC, as bench.py does.

The dual of the cap constraint is read from the linprog result that build() otherwise throws away.
The hourly LP solves on hourly means, then its schedule is replayed at 15 min and scored there.

Run:  python -m bench.m0 configs/dev_2day.yaml      -> runs/m0/   (config m0_dir picks another folder)
      python -m bench.m0 <config> --controllable      -> <m0_dir>/ceiling_controllable.json (D57; ceiling.json is
                                                        never touched)

--controllable (D57 option 1, owner, Nov 6): the peak and cap rows cover only the steps from the first binding call's
window on (sim.run_m1.controllable_from: the earliest binding deadline, from the config). On every earlier step system
net load may not exceed the recorded baseline's maximum over those same steps (pre_from_cap_mw), so the uncounted
hours cannot absorb shifted load. The recorded baseline peak and the ceiling are reported over the controllable steps.
"""
import importlib, json, os, sys, tempfile, time
from pathlib import Path

import numpy as np
import pandas as pd

from bench.runinfo import run_header
from sim.data import REPO, Slice, load_config, load_slice, to_hourly, write_bench_inputs

OUT = REPO / "runs" / "m0"     # default; a config's m0_dir (relative to the repo) overrides it


def m0_dir(cfg) -> Path:
    return REPO / cfg.get("m0_dir", "runs/m0")
CAP_MARGIN = 1.01      # cap = peak-min peak x 1.01, as in bench/README.md
REG_RTOL = 1e-6        # wrapper vs. plain build() objectives must agree to this
TOL_KW = 1e-3


def load_bench(s: Slice):
    """Import bench/bench.py against a slice. It reads data.npz and meta.csv from the cwd at import,
    so write them to a temp dir outside the repo, import there, then come back."""
    cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        write_bench_inputs(s, tmp)
        os.chdir(tmp)
        try:
            sys.modules.pop("bench.bench", None)
            B = importlib.import_module("bench.bench")
            B.d.close()    # every array is read at import; release the npz so the temp dir can go
        finally:
            os.chdir(cwd)
    return B


def use_slice(B, s: Slice):
    """Point bench.bench's time-series globals at a slice. build() reads them at call time."""
    B.DT, B.K = s.dt_h, s.dt_h / 1000.0
    B.price = s.system["price_rt"]
    B.SL = s.system["system_load"] * 1000.0
    B.PV = np.nan_to_num(s.agent["pv"])
    B.LOAD = s.agent["load"]
    B.N, B.T = s.shape


class Capture:
    """Stands in for scipy's linprog inside bench.bench and keeps the last result."""
    def __init__(self, fn):
        self.fn, self.last = fn, None

    def __call__(self, *args, **kwargs):
        self.last = self.fn(*args, **kwargs)
        return self.last


def objective(B, x, z, peak):
    """build()'s objective recomputed from its solution: -(battery revenue) + wear, plus Z or slack."""
    c, d, u = x[:, 0], x[:, 1], x[:, 3]
    lp = (B.price[:B.T] * B.K * (c - d + u)).sum() + B.DEG * B.K * d.sum()
    return float(1e-6 * lp + z[0]) if peak else float(lp + z.sum())


def solve(B, peak=False, cap_kw=None, dual=True, peak_from=0, pre_cap_kw=None):
    """One LP over all agents and the whole slice. dual=False runs plain build() with scipy's linprog.
    peak_from, pre_cap_kw: D57's controllable ceiling (bench.bench.build); the defaults give the LP as before."""
    orig = B.linprog
    cap = Capture(orig) if dual else None
    B.linprog = cap or orig
    tic = time.time()
    try:
        kw = {} if (not peak_from and pre_cap_kw is None) else {"peak_from": peak_from, "pre_cap_kw": pre_cap_kw}
        x, z = B.build(list(range(B.N)), 0, B.T, B.S0, B.S0, cap_kw=cap_kw, peak=peak, **kw)
    finally:
        B.linprog = orig
    out = {"x": x, "z": z, "secs": time.time() - tic, "obj_recomputed": objective(B, x, z, peak)}
    if dual:
        out["obj"] = float(cap.last.fun)
        if peak_from:                                     # D57: coupling rows only from peak_from; zeros before
            out["dual"] = np.concatenate([np.zeros(peak_from), -cap.last.ineqlin.marginals[-(B.T - peak_from):]])
        else:
            out["dual"] = -cap.last.ineqlin.marginals[-B.T:]   # coupling rows are appended last; $ per kW-step
    return out


def dispatch(x):
    return {"c": x[:, 0], "d": x[:, 1], "u": x[:, 3]}


def net_load(B, c, d, u):
    """System net load, kW (bench.py docstring: system_load - sum(PV_ac - curtail + BESS))."""
    return B.SL - (B.PV - u + d - c).sum(0)


def value(B, c, d, u):
    """Fleet revenue at RT prices, net of wear, $."""
    return float((B.price * B.K * (B.PV - B.LOAD + d - c - u)).sum() - B.DEG * B.K * d.sum())


def replay(B, xh, steps=4):
    """Hold an hourly schedule constant inside each hour and check it at 15-min resolution."""
    c, d, u = (np.repeat(v, steps, axis=1) for v in (xh[:, 0], xh[:, 1], xh[:, 3]))
    clipped = int((u > B.PV + TOL_KW).sum())
    u = np.minimum(u, B.PV)
    pnet = B.PV - u + d - c - B.LOAD
    poi = int((pnet > B.POI[:, None] + TOL_KW).sum())
    keep = 1 - B.SD * B.DT
    soc = np.empty_like(c)
    prev = B.S0.copy()
    for t in range(c.shape[1]):
        prev = keep * prev + B.ETA * B.DT * c[:, t] - B.DT / B.ETA * d[:, t]
        soc[:, t] = prev
    lo, hi = 0.1 * B.CAP[:, None], 0.9 * B.CAP[:, None]
    soc_bad = int(((soc < lo - TOL_KW) | (soc > hi + TOL_KW)).sum())
    shortfall = float(np.maximum(B.S0 - soc[:, -1], 0).sum())
    checks = {"curtail_clipped_steps": clipped, "poi_export_violations": poi, "soc_bound_violations": soc_bad,
              "end_soc_shortfall_kwh": shortfall}
    return {"c": c, "d": d, "u": u}, checks


def main(cfg_path):
    cfg = load_config(cfg_path)
    s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
    B = load_bench(s)
    use_slice(B, s)

    bess = s.agent["bess"]
    base = {"c": np.maximum(-bess, 0), "d": np.maximum(bess, 0), "u": s.agent["curt"]}
    base_net, base_val = net_load(B, **base), value(B, **base)

    # 15-min ceiling
    pk = solve(B, peak=True)
    cap_kw = float(pk["z"][0]) * CAP_MARGIN
    vl = solve(B, cap_kw=cap_kw)
    pk_net, vl_net = net_load(B, **dispatch(pk["x"])), net_load(B, **dispatch(vl["x"]))
    vl_val = value(B, **dispatch(vl["x"]))

    # Regression: the dual-capturing wrapper must not change the LP
    regression = {}
    for name, kw, res in [("peak_min", {"peak": True}, pk), ("value", {"cap_kw": cap_kw}, vl)]:
        plain = solve(B, dual=False, **kw)
        a, b = res["obj"], plain["obj_recomputed"]
        rel = abs(a - b) / max(1.0, abs(a))
        regression[name] = {"wrapper_objective": a, "plain_build_objective": b, "abs_diff": abs(a - b),
                            "rel_diff": rel, "rtol": REG_RTOL, "pass": rel <= REG_RTOL}

    # Hourly LP, replayed at 15 min
    h = to_hourly(s)
    use_slice(B, h)
    hpk = solve(B, peak=True)
    hvl = solve(B, cap_kw=cap_kw)
    use_slice(B, s)
    rp_pk, chk_pk = replay(B, hpk["x"])
    rp_vl, chk_vl = replay(B, hvl["x"])
    rp_peak = float(net_load(B, **rp_pk).max())
    rp_val = value(B, **rp_vl)
    rp_vl_net = net_load(B, **rp_vl)
    chk_vl["steps_over_cap"] = int((rp_vl_net > cap_kw + TOL_KW).sum())
    chk_vl["max_over_cap_mw"] = float(max(rp_vl_net.max() - cap_kw, 0)) / 1e3

    base_peak, ceil_peak = float(base_net.max()), float(pk_net.max())
    cap_dual = vl["dual"]
    at_cap = vl_net >= cap_kw - TOL_KW
    ts = s.ts_utc
    result = {
        "slice": {"start_utc": str(ts[0]), "end_utc": str(ts[-1]), "steps": len(ts), "agents": len(s.ids),
                  "start_central": str(ts[0].tz_convert("America/Chicago"))},
        "baseline_recorded": {"peak_mw": base_peak / 1e3, "peak_step_utc": str(ts[base_net.argmax()]),
                              "value_usd": base_val},
        "ceiling_15min": {
            "peak_min_mw": ceil_peak / 1e3, "peak_min_step_utc": str(ts[pk_net.argmax()]),
            "peak_reduction_mw": (base_peak - ceil_peak) / 1e3,
            "cap_mw": cap_kw / 1e3, "value_usd": vl_val, "value_gain_usd": vl_val - base_val,
            "value_run_peak_mw": float(vl_net.max()) / 1e3, "value_run_slack_kw_steps": float(vl["z"].sum()),
            "solve_s": {"peak_min": pk["secs"], "value": vl["secs"]},
        },
        "cap_dual": {
            "units": "$/kW-step = value of 1 kW more cap at that step; $/MWh = that / (dt_h/1000)",
            "steps_at_cap": int(at_cap.sum()), "steps_with_positive_dual": int((cap_dual > 1e-9).sum()),
            "max_usd_mwh": float(cap_dual.max() / B.K), "sum_usd_per_kw": float(cap_dual.sum()),
            "steps_at_slack_penalty": int((cap_dual >= 1 - 1e-9).sum()),
        },
        "hourly": {
            "lp_peak_mw_on_hourly_means": float(hpk["z"][0]) / 1e3,
            "replay_peak_mw": rp_peak / 1e3, "replay_value_usd": rp_val,
            "replay_checks_peak_min": chk_pk, "replay_checks_value": chk_vl,
            "loss": {
                "peak_mw": (rp_peak - ceil_peak) / 1e3,
                "peak_reduction_retained": (base_peak - rp_peak) / (base_peak - ceil_peak),
                "value_usd": vl_val - rp_val,
                "value_gain_retained": (rp_val - base_val) / (vl_val - base_val),
                "replay_feasible_at_15min": not any(v > 0.01 for v in (*chk_pk.values(), *chk_vl.values())),
            },
            "solve_s": {"peak_min": hpk["secs"], "value": hvl["secs"]},
        },
        "regression": regression,
    }

    out_dir = m0_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run.json").write_text(json.dumps(run_header("bench/m0.py", cfg), indent=2))
    (out_dir / "ceiling.json").write_text(json.dumps(result, indent=2))
    pd.DataFrame({
        "t_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "t_central": ts.tz_convert("America/Chicago").strftime("%Y-%m-%d %H:%M"),
        "net_load_baseline_kw": base_net, "net_load_peakmin_kw": pk_net, "net_load_value_kw": vl_net,
        "at_cap": at_cap, "cap_dual_usd_per_kw_step": cap_dual, "cap_dual_usd_mwh": cap_dual / B.K,
        "peakmin_dual": pk["dual"],
    }).to_csv(out_dir / "dual_15min.csv", index=False)

    print(json.dumps({k: result[k] for k in ("baseline_recorded", "ceiling_15min", "cap_dual")}, indent=2))
    print(json.dumps(result["hourly"]["loss"], indent=2))
    print("regression:", {k: v["pass"] for k, v in regression.items()})
    if not all(v["pass"] for v in regression.values()):
        raise SystemExit("regression check failed: wrapper and plain build() objectives differ")


def _ratio(a, b):
    """a / b, or None when b is 0 (a slice where the ceiling gains nothing)."""
    return a / b if b else None


def main_controllable(cfg_path):
    """D57 option 1: the controllable ceiling. Writes ceiling_controllable.json, dual_15min_controllable.csv and
    run_controllable.json next to the window's M0 files; the cold files are never touched."""
    from sim.run_m1 import controllable_from
    cfg = load_config(cfg_path)
    s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
    pf = controllable_from(cfg, s)
    steps_h = round(1.0 / s.dt_h)
    if pf <= 0:
        raise SystemExit("no binding call in this config: there is no controllable start")
    if pf % steps_h:
        raise SystemExit(f"the controllable start (step {pf}) is not on the hour: the hourly LP cannot follow it")
    B = load_bench(s)
    use_slice(B, s)
    bess = s.agent["bess"]
    base = {"c": np.maximum(-bess, 0), "d": np.maximum(bess, 0), "u": s.agent["curt"]}
    base_net, base_val = net_load(B, **base), value(B, **base)
    pre_cap = float(base_net[:pf].max())
    kw = {"peak_from": pf, "pre_cap_kw": pre_cap}

    pk = solve(B, peak=True, **kw)
    cap_kw = float(pk["z"][0]) * CAP_MARGIN
    vl = solve(B, cap_kw=cap_kw, **kw)
    pk_net, vl_net = net_load(B, **dispatch(pk["x"])), net_load(B, **dispatch(vl["x"]))
    vl_val = value(B, **dispatch(vl["x"]))
    regression = {}
    for name, extra, res in [("peak_min", {"peak": True}, pk), ("value", {"cap_kw": cap_kw}, vl)]:
        plain = solve(B, dual=False, **extra, **kw)
        a, b = res["obj"], plain["obj_recomputed"]
        rel = abs(a - b) / max(1.0, abs(a))
        regression[name] = {"wrapper_objective": a, "plain_build_objective": b, "abs_diff": abs(a - b),
                            "rel_diff": rel, "rtol": REG_RTOL, "pass": rel <= REG_RTOL}

    h = to_hourly(s)
    use_slice(B, h)
    hkw = {"peak_from": pf // steps_h, "pre_cap_kw": pre_cap}
    hpk = solve(B, peak=True, **hkw)
    hvl = solve(B, cap_kw=cap_kw, **hkw)
    use_slice(B, s)
    rp_pk, chk_pk = replay(B, hpk["x"])
    rp_vl, chk_vl = replay(B, hvl["x"])
    rp_peak = float(net_load(B, **rp_pk)[pf:].max())
    rp_val = value(B, **rp_vl)
    rp_vl_net = net_load(B, **rp_vl)
    chk_vl["steps_over_cap"] = int((rp_vl_net[pf:] > cap_kw + TOL_KW).sum())
    chk_vl["max_over_cap_mw"] = float(max(rp_vl_net[pf:].max() - cap_kw, 0)) / 1e3
    chk_vl["pre_from_steps_over_pre_cap"] = int((rp_vl_net[:pf] > pre_cap + TOL_KW).sum())

    base_peak, ceil_peak = float(base_net[pf:].max()), float(pk_net[pf:].max())
    cap_dual = vl["dual"]
    at_cap = np.zeros(len(vl_net), bool)
    at_cap[pf:] = vl_net[pf:] >= cap_kw - TOL_KW
    ts = s.ts_utc
    k_b, k_p = pf + int(base_net[pf:].argmax()), pf + int(pk_net[pf:].argmax())
    result = {
        "slice": {"start_utc": str(ts[0]), "end_utc": str(ts[-1]), "steps": len(ts), "agents": len(s.ids),
                  "start_central": str(ts[0].tz_convert("America/Chicago"))},
        "controllable": {"peak_from_step": pf, "peak_from_utc": ts[pf].strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "peak_from_central": str(ts[pf].tz_convert("America/Chicago")),
                         "pre_from_cap_mw": pre_cap / 1e3,
                         "pre_from_peak_mw": {"recorded": pre_cap / 1e3,
                                              "peak_min": float(pk_net[:pf].max()) / 1e3,
                                              "value": float(vl_net[:pf].max()) / 1e3},
                         "rule": "D57 option 1: peak and cap rows from the first binding call's window on; earlier "
                                 "steps capped at the recorded baseline's maximum over them"},
        "baseline_recorded": {"peak_mw": base_peak / 1e3, "peak_step_utc": str(ts[k_b]), "value_usd": base_val,
                              "full_slice_peak_mw": float(base_net.max()) / 1e3},
        "ceiling_15min": {
            "peak_min_mw": ceil_peak / 1e3, "peak_min_step_utc": str(ts[k_p]),
            "peak_reduction_mw": (base_peak - ceil_peak) / 1e3,
            "cap_mw": cap_kw / 1e3, "value_usd": vl_val, "value_gain_usd": vl_val - base_val,
            "value_run_peak_mw": float(vl_net[pf:].max()) / 1e3, "value_run_slack_kw_steps": float(vl["z"].sum()),
            "solve_s": {"peak_min": pk["secs"], "value": vl["secs"]},
        },
        "cap_dual": {
            "units": "$/kW-step = value of 1 kW more cap at that step; $/MWh = that / (dt_h/1000)",
            "steps_at_cap": int(at_cap.sum()), "steps_with_positive_dual": int((cap_dual > 1e-9).sum()),
            "max_usd_mwh": float(cap_dual.max() / B.K), "sum_usd_per_kw": float(cap_dual.sum()),
            "steps_at_slack_penalty": int((cap_dual >= 1 - 1e-9).sum()),
        },
        "hourly": {
            "lp_peak_mw_on_hourly_means": float(hpk["z"][0]) / 1e3,
            "replay_peak_mw": rp_peak / 1e3, "replay_value_usd": rp_val,
            "replay_checks_peak_min": chk_pk, "replay_checks_value": chk_vl,
            "loss": {
                "peak_mw": (rp_peak - ceil_peak) / 1e3,
                "peak_reduction_retained": _ratio(base_peak - rp_peak, base_peak - ceil_peak),
                "value_usd": vl_val - rp_val,
                "value_gain_retained": _ratio(rp_val - base_val, vl_val - base_val),
                "replay_feasible_at_15min": not any(v > 0.01 for v in (*chk_pk.values(), *chk_vl.values())),
            },
            "solve_s": {"peak_min": hpk["secs"], "value": hvl["secs"]},
        },
        "regression": regression,
    }
    out_dir = m0_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run_controllable.json").write_text(json.dumps(run_header("bench/m0.py --controllable", cfg),
                                                              indent=2))
    (out_dir / "ceiling_controllable.json").write_text(json.dumps(result, indent=2))
    pd.DataFrame({
        "t_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "t_central": ts.tz_convert("America/Chicago").strftime("%Y-%m-%d %H:%M"),
        "controllable": np.arange(len(ts)) >= pf,
        "net_load_baseline_kw": base_net, "net_load_peakmin_kw": pk_net, "net_load_value_kw": vl_net,
        "at_cap": at_cap, "cap_dual_usd_per_kw_step": cap_dual, "cap_dual_usd_mwh": cap_dual / B.K,
        "peakmin_dual": pk["dual"],
    }).to_csv(out_dir / "dual_15min_controllable.csv", index=False)
    print(json.dumps({k: result[k] for k in ("controllable", "baseline_recorded", "ceiling_15min")}, indent=2))
    print("regression:", {k: v["pass"] for k, v in regression.items()})
    if not all(v["pass"] for v in regression.values()):
        raise SystemExit("regression check failed: wrapper and plain build() objectives differ")
    return result


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--controllable"]
    path = args[0] if args else REPO / "configs" / "dev_2day.yaml"
    if "--controllable" in sys.argv[1:]:
        main_controllable(path)
    else:
        main(path)
