"""
D58 risk-margin test (docs/plans/d58_margin_report.md, pre-registered): does a fleet-wide forecast-risk margin m in the
agents' arm C re-plan (agents/battery.py, peak_response_margin_usd_mwh) bring every agent within the D58 horizon
tolerance without giving up more than a quarter of the peak cut in any window?

PASS for a margin m needs all of (on the 11 windows of the 922d7fd horizon test, 100 MW, 1p in-run, cap 100):
  1. horizon gate: each agent's result vs. acting alone, summed over the 11 windows, >= -max(1% x |summed alone|, $10)
     (bench.d58_horizon.horizon_check, unchanged);
  2. settlement gate: 1p settlement-caused harm 0 in every window;
  3. relief floor: in every window, the cut from the first gate vs. the selfish run >= 75% of the m = 0 cut there.
Bisected run order: $3 first; $3 PASS -> $1; FAIL on horizon -> $10; FAIL on relief -> $1; at most two margins.
Within a margin: Jan 12-13, Nov 28-29, then the other nine in 922d7fd's CELLS order; a margin stops at its first
window below the relief floor. Every figure is reported next to the run's share of the M0 ceiling.

Commands (one process at a time, from the repo root):
  python -m bench.d58_margin fresh                -> runs/d58m/fresh_window.json   (recorded data only)
  python -m bench.d58_margin configs              -> configs/d58m/*.yaml, runs/d58m/feasibility.json
  python -m bench.d58_margin cell <run>           -> runs/d58m/cells/<run>.json    (bench.d56_resettle on the run)
  python -m bench.d58_margin baseline             -> runs/d58m/baseline.json       (m = 0 reruns vs. the originals)
  python -m bench.d58_margin determinism          -> runs/d58m/determinism.json
  python -m bench.d58_margin verdict <m>          -> runs/d58m/verdict_m<m>.json
  python -m bench.d58_margin confirm <m>          -> runs/d58m/confirm_m<m>.json   (fresh window)
  python -m bench.d58_margin report               -> runs/d58m/report.json, runs/d58m/report.md
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

from bench import d56_resettle as D
from bench import d58_horizon as H
from bench.d56_resettle import CheckFailed

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "runs" / "d58m"
CELLS_OUT = OUT / "cells"
CONFIGS = REPO / "configs" / "d58m"
FEAS = OUT / "feasibility.json"

RULE = "1p"
CAP_USD_MWH = 100.0
RELIEF_FLOOR = 0.75
MARGINS = (1.0, 3.0, 10.0)
FIRST_MARGIN = 3.0
# run order inside a margin: Jan 12-13, Nov 28-29, then the other nine in bench.d58_horizon.CELLS order
WINDOWS = ["jan12", "nov28", "jan02", "jan16", "jan31", "feb12", "jun19", "sep21", "jul01", "aug28", "jul14"]
IN_RUN_1P = {"jan02", "jan12", "jan16", "jan31"}
M0_RUN = {w: f"h2h_c_{w}_h100_1p" if w in IN_RUN_1P else f"h2h_c_{w}_h100" for w in WINDOWS}
# m = 0 cut from the first gate vs. the selfish run, MW (runs/d58/horizon.json "bystander", computed at 922d7fd)
M0_CUT_MW = {"jan02": 139.3096000000001, "jan12": 180.32619999999972, "jan16": 41.91219999999972,
             "jan31": 102.83119999999995, "feb12": 246.17820000000017, "jun19": 89.98789999999991,
             "sep21": 94.8265, "jul01": 93.7756000000001, "aug28": 85.58030000000028, "jul14": 81.27589999999991,
             "nov28": 114.9786000000001}
# m = 0 baseline check on the new build: Jan 12-13 (in-run 1p) and the re-settled cell with the highest max round
# price, Nov 28-29 (51.09 $/MWh; Feb 12 22.6, Jun 19 32.7, Sep 21 49.5, Jul 1 42.9, Aug 28 24.2, Jul 14 40.2)
BASELINE = ["jan12", "nov28"]
MATCH_TOL = 1e-6                 # $ and MW: float noise only
DETERMINISM = ("h2h_c_jan12_h100_m3", "h2h_c_jan12_h100_m3_det")
# fields allowed to differ between exact-match runs: wall clock, timing, commit and header, and (for the m = 0
# baseline) the fields this build added
VOLATILE = {"header", "runtime_s", "wall_utc", "round_secs", "round_wall_utc", "compare_solve_secs", "run_at_utc",
            "git_commit", "tool_sha256_16"}
TAKEN = ["2023-01-02", "2023-01-12", "2023-01-16", "2023-01-31", "2023-02-12", "2023-06-19", "2023-09-21",
         "2023-07-01", "2023-08-28", "2023-07-14", "2023-11-28"]
FRESH_WINDOW = "2023-09-05"      # breadth rule (runs/d58m/fresh_window.json, computed before any run): Sep 5-6


def mtag(m: float) -> str:
    return f"m{m:g}"


def run_name(w: str, m: float) -> str:
    return f"h2h_c_{w}_h100_{mtag(m)}"


def next_margin(verdicts: dict) -> float | None:
    """The pre-registered bisection. verdicts: margin -> PASS, FAIL_horizon, FAIL_relief, FAIL_settlement or
    INCOMPLETE. Returns the next margin to run, or None when the queue of margins is done (or needs the owner)."""
    if FIRST_MARGIN not in verdicts:
        return FIRST_MARGIN
    second = {"PASS": 1.0, "FAIL_horizon": 10.0, "FAIL_relief": 1.0}.get(verdicts[FIRST_MARGIN])
    if second is None or second in verdicts:
        return None
    return second


# ---------------------------------------------------------------- the fresh window (breadth rule)
def daily_peaks(rec_net_kw: np.ndarray, local_days) -> dict:
    out = {}
    for d in sorted(set(local_days)):
        out[d] = float(rec_net_kw[np.asarray(local_days) == d].max()) / 1000.0
    return out


def pick_fresh(peaks: dict, taken: list[str]) -> tuple[str | None, list[dict]]:
    """Breadth rule (docs/overnight/2026-10-26.md, "Window rule"): days ranked by recorded peak; a window is the peak
    day and the next; both outside Jan 1-14; the day before and after exist; it starts at least 7 days from every
    window already taken."""
    import datetime as dt
    days = sorted(peaks)
    have = set(days)
    taken_d = [dt.date.fromisoformat(t) for t in taken]
    year = dt.date.fromisoformat(days[0]).year           # the data's year (2023)
    ranked = []
    for d in sorted(days, key=lambda x: -peaks[x]):
        day = dt.date.fromisoformat(d)
        why = []
        nxt, prev, after = day + dt.timedelta(days=1), day - dt.timedelta(days=1), day + dt.timedelta(days=2)
        if any(x.year == year and x.month == 1 and x.day <= 14 for x in (day, nxt)):
            why.append("inside Jan 1-14")
        if str(prev) not in have or str(nxt) not in have or str(after) not in have:
            why.append("day before or after missing")
        near = [str(t) for t in taken_d if abs((day - t).days) < 7]
        if near:
            why.append(f"within 7 days of {', '.join(near)}")
        ranked.append({"day": d, "max_mw": round(peaks[d], 1), "excluded": why})
        if not why:
            return d, ranked
    return None, ranked


def fresh_cmd(out: Path) -> int:
    from sim.clock import Clock
    from sim.data import load_config, load_slice
    from sim.run_m1 import TZ
    cfg = load_config(REPO / "configs" / "h2h" / "h2h_c_jan12_h100_1p.yaml")
    s = load_slice(cfg["data_dir"], "2023-01-01", "2023-12-31", cfg["slice_tz"])
    a = s.agent
    rec = s.system["system_load"] * 1000.0 - (np.nan_to_num(a["pv"]) - a["curt"] + a["bess"]).sum(0)
    local = Clock(s.ts_utc, TZ).local
    peaks = daily_peaks(rec, [str(d) for d in local.date])
    check = {"2023-01-02": 2745.5, "2023-01-16": 2666.0}             # M0's recorded peaks
    for d, v in check.items():
        if round(peaks[d], 1) != v:
            raise CheckFailed(f"recorded peak {d}: {peaks[d]:.1f} MW, M0 has {v}")
    if round(max(peaks["2023-01-12"], peaks["2023-01-13"]), 1) != 2441.2:
        raise CheckFailed("Jan 12-13 recorded peak does not match M0's 2,441.2 MW")
    pick, ranked = pick_fresh(peaks, TAKEN)
    out.mkdir(parents=True, exist_ok=True)
    rec_ = {"rule": "docs/overnight/2026-10-26.md window rule (all of 2023), after the 11 D58 horizon windows",
            "taken": TAKEN, "pick": pick, "check": "reproduces M0's recorded peaks (Jan 2, Jan 16, Jan 12-13)",
            "ranked_until_pick": ranked}
    (out / "fresh_window.json").write_text(json.dumps(rec_, indent=1))
    print(f"fresh window starts {pick}", flush=True)
    return 0


# ---------------------------------------------------------------- configs
def config_text(src: Path, run: str, overrides: dict, note: str, keep_comments: bool = True) -> str:
    """Copy of src with run_name and the given keys set (replaced in place, else appended); comments kept unless
    keep_comments is False (the fresh window, whose source comments describe another window)."""
    lines = src.read_text(encoding="utf-8").splitlines()
    if not keep_comments:
        lines = [ln for ln in lines if not ln.lstrip().startswith("#")]
    keys = {"run_name": run, **overrides}
    done = set()
    out = [f"# D58 margin test (pre-registered, docs/plans/d58_margin_report.md). {note}",
           f"# Copy of {src.relative_to(REPO).as_posix()} with only {', '.join(keys)} set."]
    for ln in lines:
        k = ln.split(":", 1)[0].strip() if ":" in ln and not ln.lstrip().startswith("#") else None
        if k in keys and not ln.startswith(" "):
            v = keys[k]
            out.append(f"{k}: {v}")
            done.add(k)
        else:
            out.append(ln)
    out += [f"{k}: {v}" for k, v in keys.items() if k not in done]
    return "\n".join(out) + "\n"


def source_config(w: str) -> Path:
    return REPO / "configs" / "h2h" / f"{M0_RUN[w]}.yaml"


def all_configs(fresh: str | None) -> dict[str, str]:
    """run name -> config text. Every cell settles on 1p in-run at the 100 cap."""
    out = {}
    base = {"peak_price_cap_usd_mwh": 100, "settlement_reference": "selfish_1p"}
    for w in WINDOWS:
        for m in (0.0, *MARGINS):
            r = run_name(w, m)
            out[r] = config_text(source_config(w), r, {**base, "peak_response_margin_usd_mwh": m},
                                 f"Window {w}, margin {m:g} $/MWh.")
    det = DETERMINISM[1]
    out[det] = config_text(source_config("jan12"), det, {**base, "peak_response_margin_usd_mwh": 3.0},
                           "Determinism rerun of h2h_c_jan12_h100_m3 (only the run name differs).")
    if fresh:
        import datetime as dt
        d0 = dt.date.fromisoformat(fresh)
        tag = d0.strftime("%b%d").lower()
        m0dir = f"runs/m0_{tag}"
        for m in (0.0, *MARGINS):
            r = run_name(tag, m)
            out[r] = config_text(source_config("jan31"), r,
                                 {**base, "peak_response_margin_usd_mwh": m, "start": str(d0),
                                  "end": str(d0 + dt.timedelta(days=1)), "m0_dir": m0dir},
                                 f"Fresh window {d0} (breadth rule), margin {m:g} $/MWh.", keep_comments=False)
        out[f"m0_{tag}"] = (f"# D58 margin test fresh window ({d0}, breadth rule): M0 ceiling -> {m0dir}/.\n"
                            f"data_dir: ../data\nstart: {d0}\nend: {d0 + dt.timedelta(days=1)}\n"
                            f"slice_tz: America/Chicago\nm0_dir: {m0dir}\n")
    return out


def fresh_tag() -> str:
    import datetime as dt
    return dt.date.fromisoformat(FRESH_WINDOW).strftime("%b%d").lower()


def configs_cmd() -> int:
    f = OUT / "fresh_window.json"
    if f.exists() and json.loads(f.read_text())["pick"] != FRESH_WINDOW:
        raise CheckFailed(f"{f} picks {json.loads(f.read_text())['pick']}, the tool has {FRESH_WINDOW}")
    CONFIGS.mkdir(parents=True, exist_ok=True)
    texts = all_configs(FRESH_WINDOW)
    for r, t in texts.items():
        (CONFIGS / f"{r}.yaml").write_text(t, encoding="utf-8")
    # feasibility does not depend on the margin: each window's entry is the original cell's
    feas = {}
    for f in D.FEASIBILITY_FILES:
        if f.exists():
            feas = {**json.loads(f.read_text())["cells"], **feas}
    cells = {}
    for w in WINDOWS:
        src = feas[M0_RUN[w] if M0_RUN[w] in feas else f"h2h_c_{w}_h100"]
        for r in [run_name(w, m) for m in (0.0, *MARGINS)] + ([DETERMINISM[1]] if w == "jan12" else []):
            cells[r] = {**src, "run_name": r, "config": f"configs/d58m/{r}.yaml", "from": src["run_name"]}
    tag = fresh_tag()
    ff = OUT / "feasibility_fresh.json"
    if ff.exists():
        src = json.loads(ff.read_text())["cells"][run_name(tag, 0.0)]
        for m in (0.0, *MARGINS):
            r = run_name(tag, m)
            cells[r] = {**src, "run_name": r, "config": f"configs/d58m/{r}.yaml", "from": src["run_name"]}
    OUT.mkdir(parents=True, exist_ok=True)
    FEAS.write_text(json.dumps({"note": "per-window feasibility copied from the original cells (margin-independent)",
                                "cells": cells}, indent=1))
    print(f"{len(texts)} configs, {len(cells)} feasibility rows", flush=True)
    return 0


# ---------------------------------------------------------------- one cell
def cell_cmd(run: str, out: Path) -> int:
    from bench.runinfo import run_header
    out.mkdir(parents=True, exist_ok=True)
    head = {**run_header("bench/d58_margin.py", {"run": run}), "tool_sha256_16": tool_sha()}
    files = D.FEASIBILITY_FILES
    try:
        D.FEASIBILITY_FILES = [*files, FEAS]
        meta, arr = D.load_cell(run, cfg_path=CONFIGS / f"{run}.yaml")
        res = D.evaluate(meta, arr)
    except CheckFailed as err:
        (out / f"{run}.failed.json").write_text(json.dumps({"header": head, "failed": str(err)}, indent=1))
        print(f"{run}: CHECK FAILED: {err}", flush=True)
        return 2
    finally:
        D.FEASIBILITY_FILES = files
    head["roster"] = res["roster"]
    (out / f"{run}.json").write_text(json.dumps({"header": head, **res}, indent=1, default=float))
    print(f"{run}: ok", flush=True)
    return 0


def summary(run: str) -> dict:
    return json.loads((REPO / "runs" / run / "summary.json").read_text())


def load(run: str, comparator: bool = False) -> dict:
    """A cell result: the original m = 0 cells live in runs/d56, everything this test runs in runs/d58m/cells."""
    f = (D.OUT if comparator else CELLS_OUT) / f"{run}.json"
    if not f.exists():
        raise CheckFailed(f"{run}: no cell result")
    (c,) = D.load_results([run], f.parent)
    return c


def margin_of(summ: dict) -> float:
    return float(summ["header"]["config"].get("peak_response_margin_usd_mwh", 0.0))


def check_margin_cell(cell: dict, summ: dict, m: float, roster: list[str]):
    """Fail closed: settled on 1p in-run at the 100 cap, the margin it ran at, and the run header's full roster."""
    if cell.get("settled_rule") != RULE or cell.get("price_cap_usd_mwh") != CAP_USD_MWH:
        raise CheckFailed(f"{cell['run']}: not settled in-run on {RULE} at the {CAP_USD_MWH:g} cap")
    if margin_of(summ) != m:
        raise CheckFailed(f"{cell['run']}: header margin {margin_of(summ)} is not {m}")
    D.check_roster(roster, list(summ["header"]["roster"]), f"{cell['run']} run header")
    D.check_roster(roster, [a["agent"] for a in cell["agents"]], f"{cell['run']} agents")


def cut_mw(cell: dict, summ: dict) -> float:
    return H.bystander(cell, summ)["cut_from_first_gate_mw"]


def relief(cell: dict, summ: dict, base_cut: float) -> dict:
    c = cut_mw(cell, summ)
    return {"cut_mw": c, "m0_cut_mw": base_cut, "ratio": c / base_cut if base_cut > 1e-9 else None,
            "pass": bool(c >= RELIEF_FLOOR * base_cut - 1e-9)}


# ---------------------------------------------------------------- the m = 0 comparators
def comparator_cuts() -> dict:
    """The m = 0 cut per window: the pre-registered originals, or the new-build m = 0 reruns if the baseline check
    failed (pre-registered mismatch rule)."""
    f = OUT / "baseline.json"
    if f.exists() and not json.loads(f.read_text())["match"]:
        return {w: cut_mw(load(run_name(w, 0.0)), summary(run_name(w, 0.0))) for w in WINDOWS}
    return dict(M0_CUT_MW)


def comparator_cell(w: str) -> tuple[dict, dict]:
    f = OUT / "baseline.json"
    if f.exists() and not json.loads(f.read_text())["match"]:
        return load(run_name(w, 0.0)), summary(run_name(w, 0.0))
    return load(M0_RUN[w], comparator=True), summary(M0_RUN[w])


def agent_results(cell: dict) -> dict:
    w = H.window_rows(cell)
    return dict(zip(w["ids"], w["gain"]))


def baseline_compare(orig: tuple[dict, dict], new: tuple[dict, dict]) -> dict:
    """Peak, cut from the first gate, and every agent's result vs. acting alone must match within MATCH_TOL."""
    (oc, os_), (nc, ns) = orig, new
    D.check_roster(list(os_["header"]["roster"]), list(ns["header"]["roster"]), f"{nc['run']} vs {oc['run']} roster")
    op, np_ = os_["exchange_by_headroom_mw"]["100"]["peak_mw"], ns["exchange_by_headroom_mw"]["100"]["peak_mw"]
    ocut, ncut = cut_mw(oc, os_), cut_mw(nc, ns)
    og, ng = agent_results(oc), agent_results(nc)
    worst = max(abs(og[a] - ng[a]) for a in og)
    ok = abs(op - np_) <= MATCH_TOL and abs(ocut - ncut) <= MATCH_TOL and worst <= MATCH_TOL
    return {"original": oc["run"], "rerun": nc["run"], "peak_mw": [op, np_], "cut_mw": [ocut, ncut],
            "max_agent_diff_usd": worst, "match": bool(ok)}


def baseline_cmd(out: Path) -> int:
    rows = {w: baseline_compare((load(M0_RUN[w], True), summary(M0_RUN[w])),
                                (load(run_name(w, 0.0)), summary(run_name(w, 0.0)))) for w in BASELINE}
    res = {"windows": rows, "match": all(r["match"] for r in rows.values()),
           "rule": "if either mismatches, all 11 m = 0 cells are re-run on the new build and used as the comparator"}
    out.mkdir(parents=True, exist_ok=True)
    (out / "baseline.json").write_text(json.dumps(res, indent=1, default=float))
    print(f"baseline match {res['match']}", flush=True)
    return 0


# ---------------------------------------------------------------- determinism
def diff(a, b, path="", names=("", "")) -> list[str]:
    """Paths where two JSON values differ, ignoring VOLATILE keys and the run names themselves."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k in VOLATILE:
                continue
            if k not in a or k not in b:
                out.append(f"{path}/{k} missing")
            else:
                out += diff(a[k], b[k], f"{path}/{k}", names)
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path} length {len(a)} vs {len(b)}"]
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in diff(x, y, f"{path}[{i}]", names)]
    if isinstance(a, str) and isinstance(b, str):
        return [] if a.replace(names[1], names[0]) == b.replace(names[1], names[0]) else [path]
    if isinstance(a, float) and isinstance(b, float) and a != a and b != b:      # NaN on both sides is a match
        return []
    return [] if a == b else [path]


def determinism_cmd(out: Path) -> int:
    r0, r1 = DETERMINISM
    d = diff(summary(r0), summary(r1), "summary", (r0, r1)) + diff(load(r0), load(r1), "cell", (r0, r1))
    res = {"runs": [r0, r1], "match": not d, "differences": d[:50], "ignored": sorted(VOLATILE)}
    out.mkdir(parents=True, exist_ok=True)
    (out / "determinism.json").write_text(json.dumps(res, indent=1))
    print(f"determinism match {res['match']}", flush=True)
    return 0


# ---------------------------------------------------------------- verdict
def window_report(w: str, cell: dict, summ: dict, base: tuple[dict, dict], base_cut: float) -> dict:
    """Everything reported for one window, with the shares of the M0 ceiling next to the money."""
    ev = summ["exchange_by_headroom_mw"]["100"]
    pm = ev["price_mode"]
    by = H.bystander(cell, summ)
    met = D.cell_metrics(cell, RULE)
    bcell, bsumm = base
    bev = bsumm["exchange_by_headroom_mw"]["100"]
    bby = H.bystander(bcell, bsumm)
    bmet = D.cell_metrics(bcell, RULE)
    return {"window": w, "run": cell["run"],
            "share_of_m0_ceiling": ev["share_of_m0_ceiling"], "value_share_of_m0_ceiling": ev["value_share_of_m0_ceiling"],
            "gap_share": ev["share_of_selfish_to_ceiling_gap"],
            "m0": {"share_of_m0_ceiling": bev["share_of_m0_ceiling"], "value_share_of_m0_ceiling": bev["value_share_of_m0_ceiling"],
                   "gap_share": bev["share_of_selfish_to_ceiling_gap"], "harmed": bmet["harmed"],
                   "harm_usd": bmet["harm_usd"], "operator_payout_usd": bby["operator_payout_usd"]},
            "relief": relief(cell, summ, base_cut),
            "harmed": met["harmed"], "harm_usd": met["harm_usd"],
            "settlement_harmed": met["settlement_harmed"], "settlement_harm_usd": met["settlement_harm_usd"],
            "operator_payout_usd": by["operator_payout_usd"],
            "extra_payout_vs_m0_usd": by["operator_payout_usd"] - bby["operator_payout_usd"],
            "per_relief_mwh_usd": by["per_relief_mwh_usd"], "per_mw_cut_from_first_gate_usd": by["per_mw_cut_from_first_gate_usd"],
            "max_round_price_usd_mwh": pm["max_peak_price_usd_mwh"], "settled": pm["settled"],
            "rounds_at_cap": pm["rounds_at_cap"], "rounds_total": pm["rounds_total"],
            "margin": pm.get("margin"), "summary_peak_before_first_gate": by["summary_peak_before_first_gate"]}


def classify(relief_fail: bool, complete: bool, settlement_ok: bool | None, horizon_ok: bool | None) -> str:
    """Pre-registered order: a window below the relief floor fails the margin (the staged stop, judged on the windows
    run); otherwise the harm gates need all 11 windows; the settlement gate comes before the horizon gate."""
    if relief_fail:
        return "FAIL_relief"
    if not complete:
        return "INCOMPLETE"
    if not settlement_ok:
        return "FAIL_settlement"
    return "PASS" if horizon_ok else "FAIL_horizon"


def judge(m: float, runs_done: dict) -> dict:
    """runs_done: window -> True if the run and its cell exist. The verdict for margin m, fail closed."""
    cuts = comparator_cuts()
    rows, cells, maxp = {}, [], {}
    roster = None
    for w in WINDOWS:
        if not runs_done.get(w):
            continue
        r = run_name(w, m)
        cell, summ = load(r), summary(r)
        roster = roster or list(summ["header"]["roster"])
        check_margin_cell(cell, summ, m, roster)
        rows[w] = window_report(w, cell, summ, comparator_cell(w), cuts[w])
        cells.append(cell)
        maxp[r] = rows[w]["max_round_price_usd_mwh"]
    low = [w for w, x in rows.items() if not x["relief"]["pass"]]
    res = {"margin_usd_mwh": m, "windows_run": list(rows), "relief_fail_windows": low, "windows": rows}
    if low or len(rows) < len(WINDOWS):
        res["verdict"] = classify(bool(low), False, None, None)
        res["missing"] = [w for w in WINDOWS if w not in rows]
        return res
    for c in cells:                                     # horizon_check's own cap test: max round price <= 100
        if not maxp[c["run"]] <= CAP_USD_MWH:
            raise CheckFailed(f"{c['run']}: round price {maxp[c['run']]} above the cap")
    hz = H.horizon_check(cells, maxp)
    res["horizon"] = hz
    res["without_nov28"] = {k: v for k, v in H.horizon_check([c for c in cells if not c["run"].startswith("h2h_c_nov28")],
                                                             maxp).items() if k != "agents"}
    g = np.array([a["result_usd"] for a in hz["agents"]])
    res["agents_below_zero"] = int((g < 0).sum())
    res["median_agent_usd"] = float(np.median(g))
    res["verdict"] = classify(False, True, hz["settlement_gate_all_windows"], hz["pass"])
    return res


def verdict_cmd(m: float, out: Path) -> int:
    from bench.runinfo import run_header
    done = {w: (CELLS_OUT / f"{run_name(w, m)}.json").exists() for w in WINDOWS}
    head = {**run_header("bench/d58_margin.py", {"verdict": m}), "tool_sha256_16": tool_sha()}
    try:
        res = judge(m, done)
    except CheckFailed as err:
        (out / f"verdict_{mtag(m)}.failed.json").write_text(json.dumps({"header": head, "failed": str(err)}))
        print(f"CHECK FAILED: {err}", flush=True)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    (out / f"verdict_{mtag(m)}.json").write_text(json.dumps({"header": head, **res}, indent=1, default=float))
    print(f"margin {m:g}: {res['verdict']}", flush=True)
    return 0


# ---------------------------------------------------------------- fresh-window confirmation
def confirm(m: float) -> dict:
    """Fresh window, reported separately: 1p settlement-caused harm 0 and relief >= 75% of its own m = 0 cut.
    Per-agent results vs. acting alone are reported, not gated."""
    tag = fresh_tag()
    r0, r = run_name(tag, 0.0), run_name(tag, m)
    c0, s0, c, s = load(r0), summary(r0), load(r), summary(r)
    roster = list(s0["header"]["roster"])
    check_margin_cell(c, s, m, roster)
    check_margin_cell(c0, s0, 0.0, roster)
    row = window_report(tag, c, s, (c0, s0), cut_mw(c0, s0))
    ok = row["settlement_harmed"] == 0 and row["relief"]["pass"]
    return {"margin_usd_mwh": m, "window": tag, "pass": bool(ok), "row": row,
            "agents_vs_alone_usd": agent_results(c)}


# ---------------------------------------------------------------- report
def fmt(x, nd=1, pct=False):
    if x is None:
        return "n/a"
    return f"{100 * x:.{nd}f}%" if pct else f"{x:,.{nd}f}"


def report_md(verdicts: dict) -> str:
    """Summary tables, led by share of the M0 ceiling; every harm and payout figure sits next to it."""
    out = ["# D58 margin test: results", ""]
    for m, v in verdicts.items():
        out += [f"## m = ${m:g}/MWh: {v['verdict']}", "",
                "| Window | Share of M0 ceiling (m=0) | Value share (m=0) | Gap share (m=0) | Cut MW (m=0) | Relief | "
                "Harmed (m=0) | Harm $ (m=0) | Settlement harm | Payout $ (m=0) | Extra payout $ | Max round price |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for w, x in v["windows"].items():
            b = x["m0"]
            out.append(
                f"| {w} | {fmt(x['share_of_m0_ceiling'], pct=True)} ({fmt(b['share_of_m0_ceiling'], pct=True)}) "
                f"| {fmt(x['value_share_of_m0_ceiling'], pct=True)} ({fmt(b['value_share_of_m0_ceiling'], pct=True)}) "
                f"| {fmt(x['gap_share'], pct=True)} ({fmt(b['gap_share'], pct=True)}) "
                f"| {fmt(x['relief']['cut_mw'])} ({fmt(x['relief']['m0_cut_mw'])}) | {fmt(x['relief']['ratio'], pct=True)} "
                f"| {x['harmed']} ({b['harmed']}) | {fmt(x['harm_usd'], 0)} ({fmt(b['harm_usd'], 0)}) "
                f"| {x['settlement_harmed']} | {fmt(x['operator_payout_usd'], 0)} ({fmt(b['operator_payout_usd'], 0)}) "
                f"| {fmt(x['extra_payout_vs_m0_usd'], 0)} | {fmt(x['max_round_price_usd_mwh'])} |")
        if "horizon" in v:
            hz = v["horizon"]
            out += ["", f"Horizon: {hz['agents_failing']} agents beyond tolerance (${fmt(hz['failing_shortfall_usd'], 0)} "
                        f"beyond in total); settlement gate {hz['settlement_gate_all_windows']}; "
                        f"{v['agents_below_zero']} agents below zero; median agent ${fmt(v['median_agent_usd'], 0)}; "
                        f"without Nov 28-29: {v['without_nov28']['agents_failing']} failing."]
        out.append("")
    return "\n".join(out)


def report_cmd(out: Path) -> int:
    verdicts = {}
    for m in MARGINS:
        f = out / f"verdict_{mtag(m)}.json"
        if f.exists():
            verdicts[m] = json.loads(f.read_text())
    (out / "report.md").write_text(report_md(verdicts), encoding="utf-8")
    print(f"report: {len(verdicts)} margins", flush=True)
    return 0


def tool_sha() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fresh", "configs", "cell", "baseline", "determinism", "verdict", "confirm",
                                    "report"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    out = Path(a.out)
    if a.cmd == "fresh":
        return fresh_cmd(out)
    if a.cmd == "configs":
        return configs_cmd()
    if a.cmd == "cell":
        return cell_cmd(a.arg, out / "cells")
    if a.cmd == "baseline":
        return baseline_cmd(out)
    if a.cmd == "determinism":
        return determinism_cmd(out)
    if a.cmd == "verdict":
        return verdict_cmd(float(a.arg), out)
    if a.cmd == "confirm":
        res = confirm(float(a.arg))
        (out / f"confirm_{mtag(float(a.arg))}.json").write_text(json.dumps(res, indent=1, default=float))
        print(f"confirm {a.arg}: pass {res['pass']}", flush=True)
        return 0
    return report_cmd(out)


if __name__ == "__main__":
    sys.exit(main())
