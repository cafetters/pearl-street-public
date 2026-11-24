"""M2b peak check (owner, Nov 19): with deferral on, the exchange's peak is at most its deferral-off twin's + 1 MW
(D53's convergence band), per window, judged unrounded.

- measure "full": the summary's exchange `peak_mw` (the full-slice peak). Item 4's red test on round 1's windows.
- measure "controllable": the D57 block's `peak_mw` (`peak_steps: controllable`). Round 2's pass item (f); the
  full-slice peak is reported beside it.
Report only, both measures: acting alone with deferral against acting alone without (the herding finding).

Usage: python -m bench.m2b_peak_check --cloud runs/cloud --windows jan12 jan06 feb02 jun29 [--measure full]
       [--prefix m2b_] [--out file.json]. Exit 0 if every window passes, 1 otherwise (or if a summary is missing).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TOL_MW = 1.0


def _exchange(summary: dict) -> dict:
    ex = summary["exchange_by_headroom_mw"]
    if len(ex) != 1:
        raise ValueError(f"expected one headroom, found {sorted(ex)}")
    return next(iter(ex.values()))


def peaks(summary: dict) -> dict:
    """Exchange and acting-alone peaks (MW, unrounded), full slice and controllable (None when not run)."""
    ev, sf = _exchange(summary), summary["selfish"]
    return {"exchange_full_mw": ev["peak_mw"], "acting_alone_full_mw": sf["peak_mw"],
            "exchange_controllable_mw": (ev.get("controllable") or {}).get("peak_mw"),
            "acting_alone_controllable_mw": (sf.get("controllable") or {}).get("peak_mw")}


def check_window(on: dict, off: dict, measure: str = "full", tol: float = TOL_MW) -> dict:
    if measure not in ("full", "controllable"):
        raise ValueError(f"measure must be 'full' or 'controllable', not {measure!r}")
    p_on, p_off = peaks(on), peaks(off)
    key = f"exchange_{measure}_mw"
    if p_on[key] is None or p_off[key] is None:
        raise ValueError(f"no {measure} peak in the summaries (peak_steps: controllable not set?)")
    diff = p_on[key] - p_off[key]
    out = {"measure": measure, "on_mw": p_on[key], "off_mw": p_off[key], "on_minus_off_mw": diff,
           "tol_mw": tol, "pass": bool(diff <= tol), "on": p_on, "off": p_off,
           # report only: the herding finding, acting alone with deferral vs without
           "acting_alone_full_on_minus_off_mw": p_on["acting_alone_full_mw"] - p_off["acting_alone_full_mw"]}
    if p_on["acting_alone_controllable_mw"] is not None and p_off["acting_alone_controllable_mw"] is not None:
        out["acting_alone_controllable_on_minus_off_mw"] = (p_on["acting_alone_controllable_mw"]
                                                            - p_off["acting_alone_controllable_mw"])
    return out


def run(cloud: Path, windows, measure: str = "full", prefix: str = "m2b_", tol: float = TOL_MW) -> dict:
    rows, missing = {}, []
    for w in windows:
        f_on, f_off = cloud / f"{prefix}{w}_on" / "summary.json", cloud / f"{prefix}{w}_off" / "summary.json"
        if not (f_on.exists() and f_off.exists()):
            missing.append(w)
            continue
        rows[w] = check_window(json.loads(f_on.read_text()), json.loads(f_off.read_text()), measure, tol)
    return {"measure": measure, "tol_mw": tol, "windows": rows, "missing": missing,
            "pass": bool(rows) and not missing and all(r["pass"] for r in rows.values())}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cloud", type=Path, required=True)
    ap.add_argument("--windows", nargs="+", required=True)
    ap.add_argument("--measure", choices=("full", "controllable"), default="full")
    ap.add_argument("--prefix", default="m2b_")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    res = run(a.cloud, a.windows, a.measure, a.prefix)
    for w, r in res["windows"].items():
        print(f"{w}: on {r['on_mw']:.3f} MW, off {r['off_mw']:.3f} MW, on - off {r['on_minus_off_mw']:+.3f} MW "
              f"-> {'PASS' if r['pass'] else 'FAIL'}; acting alone on - off {r['acting_alone_full_on_minus_off_mw']:+.3f} MW")
    if res["missing"]:
        print("missing:", " ".join(res["missing"]))
    print("PASS" if res["pass"] else "FAIL")
    if a.out:
        a.out.write_text(json.dumps(res, indent=1))
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
