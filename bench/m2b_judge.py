"""M2b judge (pass test: docs/plans/m2b_dc_plan.md, committed at 0c4099b before any run; owner's criteria (a)-(e),
Nov 18 overnight). Reads downloaded cloud runs and writes the verdict per item; it never reruns anything.

    python -m bench.m2b_judge --cloud runs/cloud --parity runs/m2b/q/parity.json --pytest runs/cloud/<pt run>
        [--out runs/m2b/verdict.json]

Items (all on unrounded values; rosters from run headers; any missing agent or field fails closed):
  (a) every design window's deferral-on run: zero deferral-rule violations for every data center, exchange and acting
      alone (summary dc_deferral.*.pass; at least one data center checked).
  (b) the same runs: section 8's net-harm check passes with a complete roster, and the D98 bystander line against the
      deferral-off twin passes (bystander_line: newly below = on above $1e-6 and off at most $1e-6; further below =
      off above $1e-6 and on - off above $1e-6, judged beyond max(1% of |acting alone|, $10), D91).
  (c) every ceiling run (m2b_ceil_*, 23 expected): both ceilings solved on every variant and with-deferral at or below
      battery-only + 1e-3 MW; for each design window, share of ceiling and gap share against both ceilings (reported).
  (d) parity: every parity config PASS in the given parity file (11 expected).
  (e) reported: the data centers' share of relief (runs and ceilings).
  Report only: the D100 (c) attack (m2b_<w>_att vs m2b_<w>_on): the attacker's gain, the 1p references compared, and
  the bystander line for everyone else.

Round 2 (--round 2 --configs <dir>; pre-registration docs/plans/m2b_round2b_prereg.md, owner Nov 20, revising
m2b_round2_prereg.md): runs m2b2_<w>_{on,off,att} at a 100 MW cut and m2b2_<w>_{don,doff} at the depth cut, on the 19
fresh windows; ceilings m2b2_ceil_<w> (23). Items (a) to (e) as above ((a) also on the depth runs; its ceiling is the
Scope's site room, in dc_block since 97c4b4c), plus:
  (f) do no harm, every window, 100 MW cut: the controllable-step peak (D57) with deferral on is at most the larger of
      the operator's target (day_targets, the highest day) and the off twin's controllable peak, + BAND_MW.
  (g) depth, at the cut the with-deferral ceiling sets: judged where the controllable ceilings differ by more than
      BAND_MW; there the controllable peak with deferral on is strictly below the off twin's. At least one window is
      judged, else (g) fails.
  Context and report only, per window: the off twin's section 8 result next to (b); on vs off at 100 MW within 1 MW
  (the superseded test); acting alone with deferral vs without (the herding finding); imbalance charges; the payout
  split between data centers and everyone else (peak_settle); pre_from_rebound.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bench.bystander_check import GUARD_USD, NET_HARM_ABS, NET_HARM_REL, CheckFailed, load
from bench import roster_cover as RC

WINDOWS = ("jan12", "jan06", "feb02", "jun29")
WINDOWS_R2 = ("apr16", "aug26", "aug28", "dec07", "feb12", "jan02", "jan16", "jan31", "jul01", "jul05", "jul14", "jun19",
              "mar08", "may29", "nov14", "nov28", "oct04", "sep18", "sep21")
PREFIX = {1: "m2b_", 2: "m2b2_"}
PASS_TEST = {1: "docs/plans/m2b_dc_plan.md (0c4099b)", 2: "docs/plans/m2b_round2b_prereg.md"}
BAND_MW = 6.0     # (f), (g): the market's measured precision (round2b pre-registration, from the Nov 20 gate runs)


def _netload(run_dir: Path):
    import csv
    rows = list(csv.DictReader(open(Path(run_dir) / "netload.csv", encoding="utf-8")))
    if not rows:
        raise CheckFailed(f"{run_dir}: empty netload.csv")
    return rows


def day_targets(run_dir: Path, cfg: dict) -> dict:
    """Per local day, the operator's system target (MW), as agents/operator.py _zone_target sets it: its forecast's
    maximum over the day's steps from the day's first gate, minus the run's headroom. From netload.csv
    (operator_forecast_kw) and the config's schedule."""
    import numpy as np
    import pandas as pd
    rows = _netload(run_dir)
    ts = pd.DatetimeIndex(pd.to_datetime([r["t_utc"] for r in rows], utc=True))
    fc = np.array([float(r["operator_forecast_kw"]) for r in rows]) / 1e3
    hr = cfg["headroom_mw"]
    hr = float(hr[0] if isinstance(hr, list) else hr)
    from sim.run_m1 import operator_schedule
    op = operator_schedule(cfg, ts)
    out = {}
    for d in op.days:
        st = d["steps"][d["steps"] >= d["gates"][0]]
        if len(st):
            out[rows[int(d["steps"][0])]["t_central"][:10]] = float(fc[st].max() - hr)
    return out


def no_harm(on_mw: float, off_mw: float, target_mw: float, band: float = BAND_MW) -> dict:
    ref, which = (target_mw, "target") if target_mw >= off_mw else (off_mw, "off")
    return {"on_mw": on_mw, "off_mw": off_mw, "target_mw": target_mw, "reference": which, "band_mw": band,
            "over_reference_mw": on_mw - ref, "pass": bool(on_mw <= ref + band)}


def depth(on_mw: float, off_mw: float, gap_mw: float, band: float = BAND_MW) -> dict:
    judged = bool(gap_mw > band)
    return {"on_mw": on_mw, "off_mw": off_mw, "on_minus_off_mw": on_mw - off_mw, "ceiling_gap_mw": gap_mw,
            "judged": judged, "pass": bool(on_mw < off_mw) if judged else None}


def payout_split(run_dir: Path) -> dict:
    """Report only: peak_settle summed for data centers (DC_*) and everyone else."""
    paths = sorted(Path(run_dir).glob("exchange_h*/trace.jsonl"))
    if len(paths) != 1:
        raise CheckFailed(f"{run_dir}: expected one exchange trace, found {len(paths)}")
    out = {g: {"net_usd": 0.0, "refund_usd": 0.0, "charge_usd": 0.0} for g in ("data_centers", "others")}
    with open(paths[0], encoding="utf-8") as f:
        for line in f:
            if '"peak_settle"' not in line:
                continue
            m = json.loads(line)
            if m.get("msg_type") != "peak_settle":
                continue
            g = out["data_centers" if str(m["to"]).startswith("DC_") else "others"]
            for k in g:
                g[k] += float(m["payload"][k])
    return out
N_CEILINGS = 23
N_PARITY = 11
LP_TOL_MW = 1e-3


def bystander_line(on_dir, off_dir, exclude=()) -> dict:
    """D98's line between a deferral-on run and its deferral-off twin (bench/bystander_check's unrounded logic,
    without its attacker requirement). exclude: agents left out (the attacker, in the report-only attack)."""
    a, w = load(on_dir), load(off_dir)
    bad = []
    if a["header"].get("git_commit") != w["header"].get("git_commit"):
        bad.append("commits differ")
    for name, r in (("on", a), ("off", w)):
        c = str(r["header"].get("git_commit") or "")
        if not c or c.endswith("-dirty"):
            bad.append(f"{name}: dirty tree or no commit")
        if not r["roster"]:
            bad.append(f"{name}: no roster in the header")
        for what in ("value", "short"):
            if r[what] is None:
                bad.append(f"{name}: summary has no {what} (harm_unrounded: true?)")
            elif set(r["roster"]) - set(r[what]):
                bad.append(f"{name}: roster agents missing from {what}: {sorted(set(r['roster']) - set(r[what]))[:10]}")
    if a["roster"] != w["roster"]:
        bad.append("rosters differ")
    if bad:
        raise CheckFailed("; ".join(bad))
    g = GUARD_USD
    ha = {x: max(v, 0.0) for x, v in a["short"].items()}
    hw = {x: max(v, 0.0) for x, v in w["short"].items()}
    who = [x for x in a["roster"] if x not in set(exclude)]
    newly = {x: {"on_harm_usd": ha[x], "off_harm_usd": hw[x]} for x in who if ha[x] > g and hw[x] <= g}
    further = {}
    for x in who:
        if hw[x] > g and ha[x] - hw[x] > g:
            alone = a["value"][x] + a["short"][x]
            tol = max(NET_HARM_REL * abs(alone), NET_HARM_ABS)
            d = ha[x] - hw[x]
            further[x] = {"on_harm_usd": ha[x], "off_harm_usd": hw[x], "further_usd": d, "acting_alone_usd": alone,
                          "tolerance_usd": tol, "beyond_tolerance": d > tol}
    judged = {x: f for x, f in further.items() if f["beyond_tolerance"]}
    return {"on": a["dir"], "off": w["dir"], "commit": a["header"].get("git_commit"), "agents": len(who),
            "guard_usd": g, "verdict": "PASS" if not newly and not judged else "FAIL",
            "newly_below_acting_alone": newly, "further_below_acting_alone": further, "further_below_judged": judged,
            "zero_tolerance_further_below": {"count": len(further),
                                             "usd": sum(f["further_usd"] for f in further.values())}}


def _ev(run_dir: Path) -> tuple[dict, dict]:
    s = json.loads((Path(run_dir) / "summary.json").read_text())
    RC.guard(s.get("header"), "bench.m2b_judge")     # D101: not built for exclusions
    evs = s["exchange_by_headroom_mw"]
    if len(evs) != 1:
        raise CheckFailed(f"{run_dir}: expected one headroom, got {list(evs)}")
    return s, next(iter(evs.values()))


def _shares(ev_peak_mw, selfish_mw, ceil_row) -> dict:
    out = {}
    rec = ceil_row["recorded_peak_mw"]
    for k in ("battery_only_peak_mw", "with_deferral_peak_mw"):
        c = ceil_row.get(k)
        out[k.replace("_peak_mw", "")] = None if c is None else {
            "ceiling_mw": c, "share_of_ceiling": (rec - ev_peak_mw) / (rec - c) if rec - c > 1e-9 else None,
            "gap_share": (selfish_mw - ev_peak_mw) / (selfish_mw - c) if selfish_mw - c > 1e-9 else None}
    return out


def _refs(run_dir: Path) -> list:
    """The 1p references (selfish_reference payloads) in a run's exchange trace."""
    paths = sorted(Path(run_dir).glob("exchange_h*/trace.jsonl"))
    if len(paths) != 1:
        raise CheckFailed(f"{run_dir}: expected one exchange trace, found {len(paths)}")
    refs = []
    with open(paths[0], encoding="utf-8") as f:
        next(f)
        for line in f:
            if '"selfish_reference"' in line:
                m = json.loads(line)
                if m.get("msg_type") == "selfish_reference":
                    refs.append(m["payload"])
    return refs


def judge(cloud: Path, parity_file: Path | None, pytest_dir: Path | None = None, rnd: int = 1,
          configs: Path | None = None) -> dict:
    if rnd not in PREFIX:
        raise ValueError(f"round must be 1 or 2, not {rnd!r}")
    from bench import m2b_peak_check as PC
    cloud, pre = Path(cloud), PREFIX[rnd]
    out = {"pass_test": PASS_TEST[rnd], "round": rnd, "items": {}, "windows": {}, "attack_report_only": {}}
    ceil = {}
    for d in sorted(cloud.glob(f"{pre}ceil_*")):
        if not (d / "DONE").exists():
            continue
        rows = json.loads((d / "summary.json").read_text())["variants"]
        ok = bool(rows) and all(r.get("battery_only_solved") and r.get("with_deferral_solved")
                                and r["with_deferral_peak_mw"] <= r["battery_only_peak_mw"] + LP_TOL_MW
                                for r in rows.values())
        ceil[d.name.removeprefix(f"{pre}ceil_")] = {
            "pass": ok, "cold": rows.get("cold"), "controllable": rows.get("controllable"),
            "dc_rule_violations_total": sum(r.get("dc_rule_violations_total", 0) for r in rows.values())}
    c_ok = len(ceil) == N_CEILINGS and all(v["pass"] for v in ceil.values())
    a_ok = b_ok = f_ok = g_ok = True
    g_judged = 0
    for w in (WINDOWS if rnd == 1 else WINDOWS_R2):
        on, off, att = cloud / f"{pre}{w}_on", cloud / f"{pre}{w}_off", cloud / f"{pre}{w}_att"
        f_w = g_w = rnd == 1
        try:
            s_on, ev = _ev(on)
            dd = ev.get("dc_deferral")
            if not dd:
                raise CheckFailed(f"{on}: no dc_deferral block (dc_deferral: true?)")
            a_w = bool(dd["exchange"]["pass"] and dd["acting_alone"]["pass"] and dd["exchange"]["agents"])
            nh = ev["net_harm"]
            line = bystander_line(on, off)
            b_w = bool(nh["pass"] and not nh["missing_from_check"] and line["verdict"] == "PASS")
            row = {"a_pass": a_w, "a_violations": {"exchange": dd["exchange"]["violations_total"],
                                                   "acting_alone": dd["acting_alone"]["violations_total"],
                                                   "data_centers_checked": len(dd["exchange"]["agents"]),
                                                   "missing": dd["exchange"]["missing_from_check"]},
                   "b_pass": b_w, "section8": {"pass": nh["pass"], "agents_harmed": nh["agents_harmed"],
                                               "total_net_harm_usd": nh["total_net_harm_usd"]},
                   "bystander_line": line, "share_of_relief": dd["share_of_relief"],
                   "peak_mw": ev["peak_mw"], "selfish_peak_mw": s_on["selfish"]["peak_mw"]}
            if rnd == 2:          # (f) and (g); the off twin's section 8 as context (it doesn't change (b))
                import yaml
                if configs is None:
                    raise CheckFailed("round 2 needs --configs (the run configs, for the operator's targets)")
                s_off, ev_off = _ev(off)
                pc = PC.check_window(s_on, s_off, "controllable")
                cfg_off = yaml.safe_load(open(Path(configs) / f"{pre}{w}_off.yaml", encoding="utf-8"))
                tg = day_targets(off, cfg_off)
                fh = no_harm(pc["on_mw"], pc["off_mw"], max(tg.values()))
                f_w = fh["pass"]
                s_don, ev_don = _ev(cloud / f"{pre}{w}_don")
                s_doff, _ = _ev(cloud / f"{pre}{w}_doff")
                pd_ = PC.check_window(s_don, s_doff, "controllable")
                cc = (ceil.get(w) or {}).get("controllable")
                if not cc:
                    raise CheckFailed(f"{w}: no controllable ceiling in {pre}ceil_{w}")
                gd = depth(pd_["on_mw"], pd_["off_mw"], cc["battery_only_peak_mw"] - cc["with_deferral_peak_mw"])
                g_w = gd["pass"] is not False
                g_judged += int(gd["judged"])
                dd_d = ev_don.get("dc_deferral") or {}
                a_w = a_w and bool(dd_d.get("exchange", {}).get("pass") and dd_d.get("acting_alone", {}).get("pass"))
                row["a_pass"] = a_w
                nh_off = ev_off["net_harm"]
                row.update({"f_pass": f_w, "f_no_harm": fh, "day_targets_mw": tg, "g_pass": gd["pass"], "g_depth": gd,
                            "depth_peak_check": pd_, "superseded_1mw_check_report_only": pc,
                            "payout_split_report_only": {"on": payout_split(on), "off": payout_split(off)},
                            "off_twin_section8": {"pass": nh_off["pass"], "agents_harmed": nh_off["agents_harmed"],
                                                  "total_net_harm_usd": nh_off["total_net_harm_usd"]},
                            "imbalance_charges_usd": {"on": ev["imbalance_all"]["charge_usd"],
                                                      "off": ev_off["imbalance_all"]["charge_usd"]},
                            "pre_from_rebound": ev["controllable"].get("pre_from_rebound")})
            if ceil.get(w, {}).get("cold"):
                row["shares_against_both_ceilings"] = _shares(ev["peak_mw"], s_on["selfish"]["peak_mw"], ceil[w]["cold"])
                row["ceiling_dc_share_of_relief"] = ceil[w]["cold"].get("dc_share_of_ceiling_relief")
            else:
                c_ok = False
                row["shares_against_both_ceilings"] = None
        except (CheckFailed, FileNotFoundError, KeyError, StopIteration, ValueError) as e:
            row, a_w, b_w, f_w, g_w = {"error": f"{type(e).__name__}: {str(e)[:300]}"}, False, False, False, False
        a_ok, b_ok, f_ok, g_ok = a_ok and a_w, b_ok and b_w, f_ok and f_w, g_ok and g_w
        out["windows"][w] = row
        try:                                   # D100 (c), report only
            _, ev_att = _ev(att)
            _, ev_on = _ev(on)
            tight = (ev_att.get("dc_deferral") or {}).get("declared_tighter") or []
            out["attack_report_only"][w] = {
                "attackers": tight,
                "gain_usd": {x: ev_att["agent_value_usd"][x] - ev_on["agent_value_usd"][x] for x in tight},
                "reference_1p_identical_to_honest_twin": _refs(att) == _refs(on),
                "bystander_line_vs_honest_twin": bystander_line(att, on, exclude=tight),
                "attack_dc_violations": ev_att["dc_deferral"]["exchange"]["violations_total"]}
        except (CheckFailed, FileNotFoundError, KeyError, StopIteration) as e:
            out["attack_report_only"][w] = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
    par = json.loads(Path(parity_file).read_text()) if parity_file and Path(parity_file).exists() else {}
    d_ok = len(par) == N_PARITY and all(v == "PASS" for v in par.values())
    out["items"] = {"a_zero_deferral_violations": a_ok, "b_no_net_harm_and_bystander_line": b_ok,
                    "c_ceiling_with_deferral": c_ok, "d_flag_off_parity": d_ok, "e_share_of_relief_reported": a_ok}
    if rnd == 2:
        out["items"]["f_no_harm_vs_target"] = f_ok
        out["items"]["g_depth_lowers_the_peak"] = bool(g_ok and g_judged > 0)
        out["depth_windows_judged"] = g_judged
    out["ceilings"] = {"count": len(ceil), "expected": N_CEILINGS,
                       "failing": [w for w, v in ceil.items() if not v["pass"]],
                       "by_window": {w: {k: (v["cold"] or {}).get(k) for k in
                                         ("recorded_peak_mw", "battery_only_peak_mw", "with_deferral_peak_mw",
                                          "dc_share_of_ceiling_relief")} | {
                                             "dc_rule_violations_total": v["dc_rule_violations_total"]}
                                     for w, v in ceil.items()}}
    out["parity"] = par
    if pytest_dir:
        ps = json.loads((Path(pytest_dir) / "summary.json").read_text())
        out["pytest"] = {k: ps.get(k) for k in ("passed", "failed", "errors", "skipped", "exit")}
    out["verdict"] = "PASS" if all(out["items"].values()) else "FAIL"
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cloud", required=True)
    ap.add_argument("--parity", required=True, help="JSON {config name: PASS|FAIL} from bench.parity_check")
    ap.add_argument("--pytest", default=None, help="the branch's cloud pytest run dir")
    ap.add_argument("--out", default=None)
    ap.add_argument("--round", type=int, default=1, choices=(1, 2))
    ap.add_argument("--configs", default=None, help="round 2: the run configs' folder (runs/m2b/q)")
    args = ap.parse_args(argv)
    res = judge(Path(args.cloud), Path(args.parity), Path(args.pytest) if args.pytest else None, rnd=args.round,
                configs=Path(args.configs) if args.configs else None)
    txt = json.dumps(res, indent=2, default=str)
    if args.out:
        Path(args.out).write_text(txt, encoding="utf-8")
    print(json.dumps({"verdict": res["verdict"], "items": res["items"]}, indent=2))
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
