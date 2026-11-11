"""
Feasibility of the operator's targets for the head-to-head cells (D53). A cell counts as feasible if its target is no
more than 1 MW below the central cap LP's lowest reachable peak: the fleet cap LP (all participants, perfect
foresight, bench.m0.solve) at the operator's system target + 1 MW per step needs no slack. Infeasible cells still run;
the report gives them their own row.

The target is rebuilt exactly as in a run: the operator's idle-battery forecast (sim.forecasts.operator_forecast),
each day's peak from its first gate minus the headroom (agents/operator.py).

Run:  python -m bench.h2h_feasibility      -> runs/h2h/feasibility.json
      python -m bench.h2h_feasibility --out runs/d56/feasibility_confirm.json configs/h2h/<cell>.yaml ...
      --controllable (D57 option 1): no cap before the first binding call's window (sim.run_m1.controllable_from);
      default unchanged
"""
import json
import sys
from pathlib import Path

import numpy as np

from agents.operator import GridOperator
from bench.m0 import load_bench, solve, use_slice
from bench.runinfo import run_header
from sim.clock import Clock
from sim.data import REPO, load_config, load_slice
from sim.forecasts import operator_forecast
from sim.run_m1 import TZ, controllable_from, participants, settings

OUT = REPO / "runs" / "h2h"
TOL_KW = 1000.0          # the 1 MW convergence band
SLACK_TOL = 1.0          # kW-steps of cap slack still counted as feasible (as the prototype's central_ref)
NO_CAP_KW = 1e12         # steps without a target (before the first gate)


def system_target(cfg, s, part, headroom_mw) -> np.ndarray:
    """The operator's system net-load target per step (kW, NaN before the first gate), as in sim.run_m1."""
    clock = Clock(s.ts_utc, TZ)
    dt = clock.dt_h
    nominations, rounds = settings(cfg)
    a = s.agent
    pv, curt, load = np.nan_to_num(a["pv"][part]), a["curt"][part], a["load"][part]
    op = GridOperator(cfg["operator_id"], clock.iso, clock.end_iso(len(clock.iso) - 1), clock.local, "feasibility",
                      cfg["zone"], headroom_mw * 1000.0, cfg["layer_mw"] * 1000.0, cfg["price_cap_usd_mwh"],
                      cfg["post_hour_local"], round(cfg["gate_lead_h"] / dt), rounds=rounds, nominations=nominations,
                      nom_lead_steps=round(cfg.get("nomination_lead_h", 1) / dt), idle_net=(pv - curt - load).sum(0),
                      info_hour=cfg["price_publish_hour_local"], price_mode=True)
    op._zone_target(0, operator_forecast(s, part, cfg["forecast_noise_sigma"], cfg["seed"]))
    return op.system_target()


def feasible(B, target_kw, tol_kw: float = TOL_KW) -> dict:
    """Central cap LP at target + tol per step (no cap where the target is NaN). Feasible if it needs no slack."""
    cap = np.where(np.isnan(target_kw), NO_CAP_KW, target_kw + tol_kw)
    r = solve(B, cap_kw=cap, dual=False)
    z = np.asarray(r["z"], float)
    return {"feasible": bool(z.sum() <= SLACK_TOL), "slack_kw_steps": float(z.sum()),
            "steps_short": int((z > 1e-3).sum()), "max_short_mw": float(z.max(initial=0.0)) / 1e3}


def check(cfg_path, controllable: bool = False) -> dict:
    cfg = load_config(cfg_path)
    s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
    part = participants(s, cfg["agents"])
    if len(part) != len(s.ids):
        raise ValueError("the central LP covers every agent; feasibility needs agents: all")
    B = load_bench(s)
    use_slice(B, s)
    h = cfg["headroom_mw"][0] if isinstance(cfg["headroom_mw"], list) else cfg["headroom_mw"]
    t = system_target(cfg, s, part, h)
    extra = {}
    if controllable:              # D57: only the controllable steps count; earlier steps are uncapped
        pf = controllable_from(cfg, s)
        t = t.copy()
        t[:pf] = np.nan
        extra = {"controllable_from_step": pf, "controllable_from_utc": str(s.ts_utc[pf])}
    day = s.ts_utc.tz_convert(TZ).date
    targets = {str(d): float(np.nanmax(t[day == d])) / 1e3 for d in np.unique(day) if not np.isnan(t[day == d]).all()}
    return {"config": str(cfg_path), "run_name": cfg["run_name"], "headroom_mw": h, "day_targets_mw": targets,
            **extra, **feasible(B, t)}


def main(paths, out_path=None, controllable: bool = False):
    paths = paths or sorted(str(p.relative_to(REPO)) for p in (REPO / "configs" / "h2h").glob("h2h_c_*.yaml"))
    cells = {}
    for p in paths:
        r = check(p, controllable)
        cells[r["run_name"]] = r
        print(f"{r['run_name']}: {'feasible' if r['feasible'] else 'INFEASIBLE'} (targets {r['day_targets_mw']}, "
              f"slack {r['slack_kw_steps']:.0f} kW-steps, max short {r['max_short_mw']:.1f} MW)", flush=True)
    out_path = Path(out_path) if out_path else OUT / "feasibility.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    hdr = {"configs": paths, "tol_kw": TOL_KW, **({"controllable": True} if controllable else {})}
    out = {"header": run_header("bench/h2h_feasibility.py", hdr, None),
           "rule": "feasible if the central cap LP at the operator's target + 1 MW per step needs no slack (D53)",
           "cells": cells}
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    args = sys.argv[1:]
    dest = None
    ctrl = "--controllable" in args
    args = [a for a in args if a != "--controllable"]
    if "--out" in args:
        i = args.index("--out")
        dest, args = args[i + 1], args[:i] + args[i + 2:]
    main(args, dest, ctrl)
