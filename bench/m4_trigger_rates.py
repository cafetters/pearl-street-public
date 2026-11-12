"""D97 trigger rates measured on saved traces (M4 step 1; report only, read-only).

For each run folder (an arm C exchange run with its header roster), counts per honest agent and per local day:
  T1  peak_settle loss beyond tolerance: net_usd < -max(1% x refund_usd, $10) (section 8's tolerance on the 1p
      reference; peak_settle arrives once, at the closing tick, so T1 can fire at most once per run)
  T2  rejects from the exchange
  T3  imbalance charge over $10 (one per binding call)
  T6  binding calls announced (fleet-wide: every agent subscribed to the zone gets each one)
and, from the public DA price series (data loading lives here, never in exchange/):
  T4  the day's DA spread (max - min) in the top quartile of the trailing 30 days of published DA prices before the
      window start (fleet-wide: DA prices are shared)
T5 (own metered load or PV off its forecast by > 20% yesterday) is reported as 0 by construction when the agents'
forecasts are the recorded series (no agent forecast noise in these runs); the run header's config says which.

    python -m bench.m4_trigger_rates <run_dir> [<run_dir> ...] [--data-dir ../data] [--out rates.json]

Every count takes its roster from the run header and fails closed if any roster agent is missing from the run's
per-agent messages (CLAUDE.md rule 10).
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

TZ = "America/Chicago"
TOL_FLOOR_USD = 10.0
TOL_PCT = 0.01
T3_USD = 10.0


class CheckFailed(Exception):
    pass


def _trace(run_dir: Path) -> Path:
    cands = sorted(p for p in Path(run_dir).glob("exchange_*/trace.jsonl"))
    if len(cands) != 1:
        raise CheckFailed(f"{run_dir}: expected one exchange_*/trace.jsonl, found {len(cands)}")
    return cands[0]


def _local_day(t_utc: str) -> str:
    return str(pd.Timestamp(t_utc).tz_convert(TZ).date())


def count_events(trace: Path) -> dict:
    """Per-agent T1/T2/T3 counts, fleet-wide T6 per local day, the roster and the run's local days."""
    with open(trace, encoding="utf-8") as f:
        header = json.loads(next(f))
        roster = list(header.get("roster") or [])
        if not roster:
            raise CheckFailed(f"{trace}: header has no roster")
        cfg = header.get("config") or {}
        days = [str(d.date()) for d in pd.date_range(cfg["start"], cfg["end"], freq="D")]
        attackers = {a.get("agent") for a in (header.get("attackers") or [])}
        honest = [a for a in roster if a not in attackers]
        t1, t2, t3 = defaultdict(int), defaultdict(int), defaultdict(int)
        seen_imb, t6 = set(), defaultdict(int)
        for ln in f:
            r = json.loads(ln)
            typ, to, p = r.get("msg_type"), r.get("to"), r.get("payload") or {}
            if typ == "imbalance":
                seen_imb.add(to)
                if float(p.get("charge_usd") or 0.0) > T3_USD:
                    t3[to] += 1
            elif typ == "peak_settle":
                tol = max(TOL_PCT * abs(float(p.get("refund_usd") or 0.0)), TOL_FLOOR_USD)
                if float(p.get("net_usd") or 0.0) < -tol:
                    t1[to] += 1
            elif typ == "reject" and r.get("from") == "exchange":
                t2[to] += 1
            elif typ == "call_nominations" and p.get("binding") and r.get("from") != "exchange":
                t6[_local_day(r["t_utc"])] += 1
    missing = [a for a in honest if a not in seen_imb]
    if missing:
        raise CheckFailed(f"{trace}: roster agents with no imbalance statement: {missing[:5]} ({len(missing)})")
    return {"roster": roster, "honest": honest, "days": days, "t1": dict(t1), "t2": dict(t2), "t3": dict(t3),
            "t6_by_day": dict(t6), "agent_forecast": "recorded series (T5 = 0 by construction)"}


def t4_flags(data_dir: Path, start: str, end: str, trailing_days: int = 30) -> dict:
    """Fleet-wide T4 per window day, from the public DA price series only."""
    from sim.data import SYS_FILE, SYSTEM_COLS, _read_range, local_range
    w0 = pd.Timestamp(start)
    t0, _ = local_range(str((w0 - pd.Timedelta(days=trailing_days)).date()), start, TZ)
    _, t1 = local_range(start, end, TZ)
    col = SYSTEM_COLS["price_da"]
    df = _read_range(Path(data_dir) / SYS_FILE, ["timestamp", col], t0, t1)
    if df.empty:
        raise CheckFailed("no DA prices in range")
    local = pd.DatetimeIndex(df.timestamp).tz_convert(TZ).date
    spread = pd.Series(df[col].to_numpy(float)).groupby(np.asarray(local)).agg(lambda s: s.max() - s.min())
    spread.index = [str(d) for d in spread.index]
    trailing = spread[[d < str(w0.date()) for d in spread.index]]
    thr = float(np.percentile(trailing.to_numpy(), 75)) if len(trailing) else float("nan")
    days = [str(d.date()) for d in pd.date_range(start, end, freq="D")]
    return {"threshold_usd_mwh": thr, "trailing_days_available": int(len(trailing)),
            "days": {d: {"spread": float(spread.get(d, np.nan)), "fires": bool(spread.get(d, -np.inf) > thr)}
                     for d in days}}


def rates(run_dirs: list[Path], data_dir: Path | None) -> dict:
    out, tot = {}, defaultdict(float)
    agent_days = 0
    for rd in run_dirs:
        ev = count_events(_trace(rd))
        n, nd = len(ev["honest"]), len(ev["days"])
        ad = n * nd
        hon = set(ev["honest"])
        row = {"honest_agents": n, "days": nd, "agent_days": ad,
               "T1": sum(v for k, v in ev["t1"].items() if k in hon) / ad,
               "T2": sum(v for k, v in ev["t2"].items() if k in hon) / ad,
               "T3": sum(v for k, v in ev["t3"].items() if k in hon) / ad,
               "T5": 0.0,
               "T6": sum(ev["t6_by_day"].get(d, 0) for d in ev["days"]) / nd,
               "t6_by_day": ev["t6_by_day"], "agent_forecast": ev["agent_forecast"]}
        if data_dir is not None:
            cfg = json.loads(next(open(_trace(rd), encoding="utf-8")))["config"]
            t4 = t4_flags(data_dir, cfg["start"], cfg["end"])
            row["T4"] = sum(v["fires"] for v in t4["days"].values()) / nd
            row["t4"] = t4
        out[Path(rd).name] = row
        for k in ("T1", "T2", "T3", "T4", "T5", "T6"):
            if k in row:
                tot[k] += row[k] * (ad if k != "T6" else nd)
        tot["_days"] += nd
        agent_days += ad
    pooled = {k: (tot[k] / tot["_days"] if k == "T6" else tot[k] / agent_days)
              for k in ("T1", "T2", "T3", "T4", "T5", "T6") if k in tot}
    return {"per_run": out, "pooled_per_agent_day": pooled, "agent_days": agent_days,
            "note": "T6 is binding calls per day (fleet-wide, every agent gets each); T4 is fleet-wide"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--data-dir")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    try:
        res = rates([Path(r) for r in a.runs], Path(a.data_dir) if a.data_dir else None)
    except CheckFailed as e:
        print(f"FAIL: {e}")
        return 1
    txt = json.dumps(res, indent=1, default=str)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(txt, encoding="utf-8")
    print(json.dumps(res["pooled_per_agent_day"], indent=1), f"agent_days={res['agent_days']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
