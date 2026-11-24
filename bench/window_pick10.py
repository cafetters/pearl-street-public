"""The 10-day window rule for the single-LLM-agent runs (owner, Nov 24): D61 v2's window rule
(runs/nov06/v2_windows/v2_pick.py), extended to 10-day stretches over the whole year.

Rule. Among all 10-day stretches (d, ..., d+9) of 2023 whose day before and day after exist in the data, keep those
sharing no day with any excluded day, and pick the one with the highest recorded system peak (the highest daily peak of
its ten days). Ties: the higher first-day peak, then the earlier start. Recorded peak as in v2_pick.py
(bench.d58_margin.daily_peaks on the recorded system net load, checked against two M0 values).

Excluded days (written out here, so the pick does not depend on git-ignored files):
  - the 24 earlier 2-day windows and the 24 holdout 2-day windows (v2_pick.py's USED and HOLDOUT_USED);
  - the spans: D66 warm-up Jan 18-31, backfill Feb 10-23, fallback Feb 1-14;
  - the 23 warm-up days of the Nov 6 cloud report;
  - D61 v2's four windows (dec11, jan09, feb26, jun02; 2 days each);
  - red-team round 1's window Sep 1-2 (owner, Nov 23, item 7a);
  - Nov 16-25, kept for the red-team round's 10-day run (owner, Nov 24);
  - every day inside the start..end of any committed config (configs/**/*.yaml).
Then, as a fail-closed check, no YAML config on disk in any worktree (configs/ and runs/) may overlap the pick.
Only recorded daily peaks are read; nothing is run.

usage: python -m bench.window_pick10 [--out runs/single_llm/window_pick10.json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import yaml

from bench import d58_margin as DM
from bench.d56_resettle import CheckFailed

LENGTH = 10
USED = ["2023-01-02", "2023-01-12", "2023-01-16", "2023-01-31", "2023-02-12", "2023-06-19", "2023-07-01",
        "2023-07-14", "2023-08-28", "2023-09-05", "2023-09-21", "2023-11-28",
        "2023-01-06", "2023-02-02", "2023-03-08", "2023-04-16", "2023-05-29", "2023-06-29", "2023-07-05",
        "2023-08-26", "2023-09-18", "2023-10-04", "2023-11-14", "2023-12-07"]
HOLDOUT_USED = ["2023-01-18", "2023-01-04", "2023-02-08", "2023-02-06", "2023-03-12", "2023-03-06", "2023-04-08",
                "2023-04-04", "2023-05-27", "2023-05-31", "2023-06-16", "2023-06-08", "2023-07-11", "2023-07-09",
                "2023-08-05", "2023-08-03", "2023-09-15", "2023-09-03", "2023-10-02", "2023-10-09", "2023-11-26",
                "2023-11-11", "2023-12-05", "2023-12-09"]
D61V2 = ["2023-12-11", "2023-01-09", "2023-02-26", "2023-06-02"]
REDTEAM_2DAY = ["2023-09-01"]
SPANS = [("2023-01-18", "2023-01-31", "D66 warm-up"), ("2023-02-10", "2023-02-23", "backfill"),
         ("2023-02-01", "2023-02-14", "fallback spans"), ("2023-11-16", "2023-11-25", "kept for the red-team 10-day run")]
WARMUP_DAYS = ["2023-01-01", "2023-01-05", "2023-01-11", "2023-01-15", "2023-01-30", "2023-02-01", "2023-02-11",
               "2023-03-07", "2023-04-15", "2023-05-28", "2023-06-18", "2023-06-28", "2023-06-30", "2023-07-04",
               "2023-07-13", "2023-08-25", "2023-08-27", "2023-09-17", "2023-09-20", "2023-10-03", "2023-11-13",
               "2023-11-27", "2023-12-06"]
M0_CHECK = {"2023-01-02": 2745.5, "2023-01-16": 2666.0}
REPO = DM.REPO


def D(x) -> dt.date:
    return x if isinstance(x, dt.date) else dt.date.fromisoformat(str(x)[:10])


def _span(a, b):
    x, out = D(a), []
    while x <= D(b):
        out.append(x)
        x += dt.timedelta(days=1)
    return out


def config_spans(paths) -> list[tuple[str, dt.date, dt.date]]:
    out = []
    for p in paths:
        try:
            c = yaml.safe_load(Path(p).read_text(encoding="utf-8"))
            if isinstance(c, dict) and c.get("start") and c.get("end"):
                out.append((str(p), D(c["start"]), D(c["end"])))
        except Exception:                                   # noqa: BLE001  not a run config
            continue
    return out


def excluded_days(repo: Path) -> dict:
    why: dict = {}
    for u in USED + HOLDOUT_USED + D61V2 + REDTEAM_2DAY:
        tag = ("earlier window" if u in USED else "holdout window" if u in HOLDOUT_USED
               else "D61 v2 window" if u in D61V2 else "red-team round 1 window")
        for x in _span(u, D(u) + dt.timedelta(days=1)):
            why.setdefault(x, tag)
    for a, b, tag in SPANS:
        for x in _span(a, b):
            why.setdefault(x, tag)
    for u in WARMUP_DAYS:
        why.setdefault(D(u), "warm-up day")
    for p, a, b in config_spans(sorted((repo / "configs").rglob("*.yaml"))):
        for x in _span(a, b):
            why.setdefault(x, f"committed config {Path(p).relative_to(repo).as_posix()}")
    return why


def recorded_peaks() -> dict:
    from sim.clock import Clock
    from sim.data import load_config, load_slice
    from sim.run_m1 import TZ
    cfg = load_config(REPO / "configs" / "h2h" / "h2h_c_jan12_h100_1p.yaml")
    s = load_slice(cfg["data_dir"], "2023-01-01", "2023-12-31", cfg["slice_tz"])
    a = s.agent
    rec = s.system["system_load"] * 1000.0 - (np.nan_to_num(a["pv"]) - a["curt"] + a["bess"]).sum(0)
    peaks = DM.daily_peaks(rec, [str(d) for d in Clock(s.ts_utc, TZ).local.date])
    for d, v in M0_CHECK.items():
        if round(peaks[d], 1) != v:
            raise CheckFailed(f"recorded peak {d}: {peaks[d]:.1f}, M0 has {v}")
    return peaks


def candidates(peaks: dict, taken: set) -> list[dict]:
    have = {D(d) for d in peaks}
    out = []
    for d in sorted(have):
        days = [d + dt.timedelta(days=i) for i in range(LENGTH)]
        if not ({d - dt.timedelta(days=1), d + dt.timedelta(days=LENGTH)} | set(days)) <= have:
            continue
        if set(days) & taken:
            continue
        out.append({"start": str(d), "end": str(days[-1]), "window_peak_mw": round(max(peaks[str(x)] for x in days), 1),
                    "first_day_peak_mw": round(peaks[str(d)], 1),
                    "_key": (max(peaks[str(x)] for x in days), peaks[str(d)], -d.toordinal())})
    return sorted(out, key=lambda c: c["_key"], reverse=True)


def disk_overlaps(start: dt.date, end: dt.date, roots) -> list[str]:
    paths = [p for r in roots for sub in ("configs", "runs") if (r / sub).is_dir() for p in (r / sub).rglob("*.yaml")]
    return sorted({f"{p} ({a}..{b})" for p, a, b in config_spans(paths) if a <= end and b >= start})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="runs/single_llm/window_pick10.json")
    a = ap.parse_args(argv)
    why = excluded_days(REPO)
    peaks = recorded_peaks()
    cands = candidates(peaks, set(why))
    if not cands:
        raise CheckFailed("no clean 10-day stretch")
    pick = cands[0]
    roots = sorted(p for p in REPO.parent.iterdir() if p.is_dir() and p.name.startswith("pearl-street"))
    overlaps = disk_overlaps(D(pick["start"]), D(pick["end"]), roots)
    fall = [c for c in cands if D(c["start"]).month in (9, 10, 11)]
    res = {"rule": __doc__, "length_days": LENGTH, "days_excluded": len(why),
           "pick": {k: v for k, v in pick.items() if k != "_key"},
           "next": [{k: v for k, v in c.items() if k != "_key"} for c in cands[1:6]],
           "best_fall": {k: v for k, v in fall[0].items() if k != "_key"} if fall else None,
           "disk_overlaps": overlaps, "worktrees_scanned": [r.name for r in roots]}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(f"pick {pick['start']}..{pick['end']} peak {pick['window_peak_mw']} MW; next "
          f"{[(c['start'], c['window_peak_mw']) for c in res['next']]}; best fall {res['best_fall']}")
    if overlaps:
        print(f"FAIL: configs on disk overlap the pick: {overlaps[:10]}")
        return 1
    print(f"no config on disk overlaps it ({len(roots)} worktrees scanned); wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
