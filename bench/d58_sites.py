"""
D58 site-level diagnostic (report only, not a pass test): is an agent's DA vs. RT gap loss over the horizon a matter
of when it shifts, or of how much it shifts relative to what it earns?

For each of the D58 horizon cells (bench/d58_horizon.py) and each agent, from the run and selfish traces:
dn = battery meter (run) - battery meter (selfish), kW; shifted MWh = sum |dn| x dt; gap = -sum (RT - DA) x dn x dt.
Reported per agent and window: shifted MWh, gap, gap per shifted MWh against the fleet's in the same window, and
the cosine between the agent's dn and the fleet's total dn (1 = same timing as the fleet). Fails closed if the
rebuilt gap differs from runs/d56/<cell>.json by more than $0.01, or on a roster mismatch.

Run (one process, after any simulation has finished):
    python -m bench.d58_sites [--agents PVB_39,PVB_22,...] [--out runs/d58]
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

from bench import d56_resettle as D
from bench.d56_resettle import CheckFailed, K
from bench.d58_horizon import CELLS

REPO = Path(__file__).resolve().parents[1]
FOCUS = ["PVB_39", "PVB_22", "PVB_13", "DC_07", "PVB_25"]     # the two failing agents and the three nearest the line


def site_rows(dn: np.ndarray, rt: np.ndarray, da: np.ndarray, roster: list[str]) -> dict:
    """dn: agents x steps (kW). Per agent: shifted MWh, gap $, gap per shifted MWh, cosine with the fleet's dn."""
    shifted = np.abs(dn).sum(axis=1) * K                        # K = 0.25 h / 1000: kW-steps -> MWh
    gap = -(dn @ (rt - da)) * K
    fleet = dn.sum(axis=0)
    fn = np.linalg.norm(fleet)
    rows = {}
    for j, a in enumerate(roster):
        n = np.linalg.norm(dn[j])
        rows[a] = {"shifted_mwh": float(shifted[j]), "gap_usd": float(gap[j]),
                   "gap_per_mwh": float(gap[j] / shifted[j]) if shifted[j] > 1e-9 else None,
                   "cos_with_fleet": float(dn[j] @ fleet / (n * fn)) if n > 1e-9 and fn > 1e-9 else None}
    tot = float(shifted.sum())
    return {"agents": rows, "fleet_gap_per_mwh": float(gap.sum() / tot) if tot > 1e-9 else None,
            "fleet_shifted_mwh": tot}


def load_dn(run: str):
    from sim.data import load_config, load_slice
    R = REPO / "runs" / run
    summ = json.loads((R / "summary.json").read_text())
    roster = list(summ["header"]["roster"])
    (h,) = list(summ["exchange_by_headroom_mw"])
    cfg = load_config(REPO / "configs" / "h2h" / f"{run}.yaml")
    s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
    iso = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in s.ts_utc]
    pos = {t: i for i, t in enumerate(iso)}
    _, ex = D._read(R / f"exchange_h{h}_rinfo" / "trace.jsonl", pos, len(iso), True)
    _, sf = D._read(R / "selfish" / "trace.jsonl", pos, len(iso), False)
    D.check_roster(roster, list(ex["meter"]), f"{run} exchange telemetry")
    D.check_roster(roster, list(sf["meter"]), f"{run} selfish telemetry")
    dn = np.array([ex["meter"][a] - sf["meter"][a] for a in roster])
    return roster, dn, np.asarray(s.system["price_rt"], float), np.asarray(s.system["price_da"], float)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", default=",".join(FOCUS))
    ap.add_argument("--out", default=str(REPO / "runs" / "d58"))
    args = ap.parse_args(argv)
    from bench.runinfo import run_header
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    focus = args.agents.split(",")
    head = {**run_header("bench/d58_sites.py", {"cells": ",".join(CELLS), "agents": args.agents}),
            "tool_sha256_16": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]}
    res = {}
    try:
        for run in CELLS:
            roster, dn, rt, da = load_dn(run)
            r = site_rows(dn, rt, da, roster)
            stored = {a["agent"]: a["components"]["da_rt_gap"] for a in D.load_results([run], D.OUT)[0]["agents"]}
            D.check_roster(roster, list(stored), f"{run} d56 result")
            err = max(abs(r["agents"][a]["gap_usd"] - stored[a]) for a in roster)
            if err > 0.01:
                raise CheckFailed(f"{run}: rebuilt gap differs from runs/d56 by ${err:.4f}")
            per = [v["gap_per_mwh"] for v in r["agents"].values() if v["gap_per_mwh"] is not None]
            res[run] = {"fleet_gap_per_mwh": r["fleet_gap_per_mwh"], "fleet_shifted_mwh": r["fleet_shifted_mwh"],
                        "agent_gap_per_mwh_median": float(np.median(per)), "gap_rebuild_err_usd": err,
                        "focus": {a: r["agents"][a] for a in focus},
                        "shifted_mwh_rank": {a: int(sorted(roster, key=lambda x: -r["agents"][x]["shifted_mwh"]).index(a)) + 1
                                             for a in focus}}
            print(f"{run}: ok", flush=True)
    except CheckFailed as err:
        (out / "sites.failed.json").write_text(json.dumps({"header": head, "failed": str(err)}, indent=1))
        print(f"CHECK FAILED: {err}", flush=True)
        return 2
    head["roster"] = roster
    (out / "sites.json").write_text(json.dumps({"header": head, "cells": res}, indent=1, default=float))
    return 0


if __name__ == "__main__":
    sys.exit(main())
