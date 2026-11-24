"""M2c: the power-flow check of final schedules (pass item 2; D35: on Engie's grid the zone product is off).

For every line and transformer and every step, the exchange's AC loading may be at most max(90%, acting-alone
loading). The comparison is in MW (loading % x rating / 100), unrounded, with a 1e-6 MW guard (D98's style). A step
whose power flow did not converge fails the check (fail closed). Arm C has no awards, so the check runs on schedules:
each agent's metered site net from the trace telemetry (exchange run and acting-alone run), roster from the header.

AC model: sim/stress.py's ACGrid with held-slack dispatch (the ext_grid held at Engie's hourly slack_p_mw; the
dispatch M0b's calibrated runs used). Both runs use the same model, so the comparison is like for like; absolute
levels are our model's (M0b reports its offsets to Engie's own results).

    python -m bench.pf_check <run folder> [--out <json>]     (run folder: holds selfish/ and exchange_h*/ traces)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from sim import grid as G
from bench import roster_cover as RC

GUARD_MW = 1e-6


class CheckFailed(Exception):
    pass


def compare(ld_ex: pd.DataFrame, ld_alone: pd.DataFrame, rating_mva: pd.Series, limit_pct: float = G.LIMIT_PCT,
            guard_mw: float = GUARD_MW) -> dict:
    """Per element: steps at or above the limit and 100% (exchange and alone), max loading, and the steps where the
    exchange is worse than max(limit, alone) by more than guard_mw. pass = no such step anywhere."""
    if ld_ex.isna().any().any() or ld_alone.isna().any().any():
        raise CheckFailed("a power flow did not converge at some step: the check fails closed")
    els, total = {}, 0
    for k in ld_ex.columns:
        ex, al = ld_ex[k].to_numpy(float), ld_alone[k].to_numpy(float)
        over_mw = (ex - np.maximum(limit_pct, al)) / 100.0 * float(rating_mva[k])
        worse = over_mw > guard_mw
        els[k] = {"steps_ge_90": int((ex >= limit_pct).sum()), "steps_ge_100": int((ex >= 100.0).sum()),
                  "alone_steps_ge_90": int((al >= limit_pct).sum()), "max_pct": float(ex.max()),
                  "alone_max_pct": float(al.max()), "worse_steps": int(worse.sum()),
                  "worse_mw_max": float(over_mw[worse].max()) if worse.any() else 0.0}
        total += int(worse.sum())
    return {"limit_pct": limit_pct, "guard_mw": guard_mw, "violations": total, "pass": total == 0, "elements": els}


def telemetry_pnet(trace: Path, iso: list) -> tuple[np.ndarray, np.ndarray]:
    """(roster ids, (N, T) metered site net kW) from a trace's telemetry. Roster from the header; every roster agent
    must have a reading at every step, or the check fails."""
    pos = {t: i for i, t in enumerate(iso)}
    with open(trace, encoding="utf-8") as f:
        head = json.loads(next(f))
        RC.guard(head, "bench.pf_check", trace)     # D101: not built for exclusions
        roster = list(head.get("roster") or [])
        if not roster:
            raise CheckFailed(f"{trace}: no roster in the header")
        row = {a: i for i, a in enumerate(roster)}
        out = np.full((len(roster), len(iso)), np.nan)
        for ln in f:
            m = json.loads(ln)
            if m.get("msg_type") != "telemetry":
                continue
            p = m["payload"]
            a = p["asset_id"].split("/")[0]
            if a in row and m["t_utc"] in pos:
                out[row[a], pos[m["t_utc"]]] = float(p["net_kw"])
    missing = [roster[i] for i in np.flatnonzero(np.isnan(out).any(1))]
    if missing:
        raise CheckFailed(f"{trace}: agents missing telemetry at some step: {missing[:5]}")
    return np.array(roster), out


def ac_loading(s, ids: np.ndarray, pnet_kw: np.ndarray, net, slack_mw: np.ndarray) -> tuple[pd.DataFrame, np.ndarray]:
    """AC loading (%) of every line and trafo per step for the given fleet schedule (ids in slice order)."""
    from sim.stress import ACGrid
    bus = s.meta.set_index("entity_id").bus.loc[list(ids)].to_numpy()
    grid = ACGrid(net, list(ids), bus)
    res = grid.run(s.system["bg_load_scale"], np.ones(len(grid.bg_idx)), pnet_kw, index=s.ts_utc,
                   slack_target_mw=slack_mw)
    return res.loading(), res.converged


def ratings(net) -> pd.Series:
    """MVA rating per loading column (l<idx>, t<idx>): lines sqrt(3) x vn x max_i_ka x parallel, trafos sn_mva."""
    vn = net.bus.vn_kv
    ln, tr = net.line, net.trafo
    par = ln["parallel"] if "parallel" in ln else 1
    r = {f"l{i}": float(np.sqrt(3) * vn[ln.from_bus[i]] * ln.max_i_ka[i] * (par[i] if hasattr(par, "__getitem__") else 1))
         for i in ln.index}
    r.update({f"t{i}": float(tr.sn_mva[i]) for i in tr.index})
    return pd.Series(r)


def check_run(run_dir, data_dir=None) -> dict:
    from sim.data import REPO, load_powerflow, load_pp_net, load_slice, local_range
    run_dir = Path(run_dir)
    ex_traces = sorted(run_dir.glob("exchange_h*/trace.jsonl"))
    if len(ex_traces) != 1:
        raise CheckFailed(f"{run_dir}: expected one exchange trace, found {len(ex_traces)}")
    head = json.loads(ex_traces[0].open(encoding="utf-8").readline())
    RC.guard(head, "bench.pf_check", run_dir)     # D101: not built for exclusions
    cfg = head["config"]
    data_dir = Path(data_dir) if data_dir else (REPO / "../data").resolve()
    s = load_slice(data_dir, cfg["start"], cfg["end"], cfg.get("slice_tz", "America/Chicago"))
    iso = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in s.ts_utc]
    ids_ex, p_ex = telemetry_pnet(ex_traces[0], iso)
    ids_al, p_al = telemetry_pnet(run_dir / "selfish" / "trace.jsonl", iso)
    if list(ids_ex) != list(ids_al):
        raise CheckFailed("exchange and acting-alone rosters differ")
    if sorted(ids_ex) != sorted(s.ids):
        raise CheckFailed("the roster is not the slice's fleet")
    net = load_pp_net(data_dir)
    a, b = local_range(cfg["start"], cfg["end"], cfg.get("slice_tz", "America/Chicago"))
    pf = load_powerflow(data_dir, ["slack_p_mw"])
    slack = pf[(pf.index >= a) & (pf.index < b)].slack_p_mw.reindex(s.ts_utc, method="ffill").to_numpy()
    t0 = time.time()
    ld_ex, ok_ex = ac_loading(s, ids_ex, p_ex, net, slack)
    ld_al, ok_al = ac_loading(s, ids_al, p_al, net, slack)
    res = compare(ld_ex, ld_al, ratings(net))
    res.update({"run": run_dir.name, "roster_size": len(ids_ex), "steps": len(iso),
                "converged_steps": {"exchange": int(ok_ex.sum()), "acting_alone": int(ok_al.sum())},
                "dispatch": "held slack (Engie hourly slack_p_mw), sim/stress.py ACGrid",
                "elapsed_s": round(time.time() - t0, 1), "commit": head.get("git_commit")})
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir")
    ap.add_argument("--out")
    ap.add_argument("--data-dir")
    a = ap.parse_args(argv)
    res = check_run(a.run_dir, a.data_dir)
    out = Path(a.out) if a.out else Path(a.run_dir) / "network_check.json"
    out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    worst = sorted(((v["worse_steps"], k) for k, v in res["elements"].items()), reverse=True)[:3]
    print(json.dumps({k: res[k] for k in ("run", "pass", "violations", "converged_steps", "elapsed_s")}), worst)
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
