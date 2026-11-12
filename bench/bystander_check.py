"""Bystander check (c-bystander; Maritime list item 1, literature mapping section 6): did another agent's messages push
an honest agent below acting alone?

For one attack cell and its honest twin, honest agents only (the attack header's roster minus its attackers), on the
acting-alone basis the run summary already reports: `net_harm.by_agent_usd` = max(acting alone - result, 0) per
agent (sim/run_m1.py). Zero tolerance:

  newly below acting alone   harm in the twin is $0 and harm in the attack cell is above $0 (zero tolerance)
  further below              harm in the twin is above $0 and harm in the attack cell is above it

D1 (owner, Nov 7; staged as D91, revising D88; set after D61's FAIL and after seeing the jan12 rehearsal, a design
window, and disclosed as such): "newly below" stays at zero tolerance; "further below" fails only beyond section 8's
tolerance, max(1% of |acting-alone value|, $10), per agent (`further_below_tol="section8"`, the judged setting). With
`further_below_tol="zero"` (D88 as first built, reported) any further-below amount fails. Every result reports the
zero-tolerance further-below count and $ next to the judged line. The acting-alone value of an agent below acting
alone is its attack-cell `agent_value_usd` plus its `by_agent_usd` (the summary rounds harm to cents, so it is exact
to $0.005).

The line PASSES when no honest agent is newly below acting alone and none is further below beyond the setting's
tolerance. Reported next to it, never judged here: the
twin-relative loss (honest agents whose value is below their twin value, and the sum), at zero tolerance, from
`agent_value_usd`, because Engie's goal is "one liar doesn't hurt the others".

Fails closed (exit 2): commits differ, a dirty tree, an attack header without attackers or a twin with any, rosters
that differ, any roster agent missing from either summary's by_agent_usd or agent_value_usd.

Set Nov 6, after D61's pre-registered FAIL (2d61911): proposed as a harm line for future attack runs (staged D88). It
cannot re-judge D61.

Run:  python -m bench.bystander_check <attack_dir> <twin_dir> [--out file.json] [--further-below-tol section8|zero]
"""
import argparse
import json
import sys
from pathlib import Path

NET_HARM_REL, NET_HARM_ABS = 0.01, 10.0       # section 8: max(1% of |acting alone|, $10), as sim/run_m1.py
# Staged D98 (owner, Nov 11): "unrounded" judges on the summary's signed, unrounded net_harm.shortfall_usd (written
# by sim/run_m1.py under harm_unrounded: true) with a $1e-6 float guard; "summary_cents" (default) is the pinned D88/D91
# basis, by_agent_usd rounded to cents, at zero.
BASES = ("summary_cents", "unrounded")
GUARD_USD = 1e-6
FURTHER_BELOW_TOLS = ("section8", "zero")


class CheckFailed(RuntimeError):
    pass


def _trace_header(run_dir: Path) -> dict:
    paths = sorted(run_dir.glob("exchange_h*/trace.jsonl"))
    if len(paths) != 1:
        raise CheckFailed(f"{run_dir}: expected one exchange trace, found {len(paths)}")
    with open(paths[0], encoding="utf-8") as f:
        return json.loads(next(f))


def load(run_dir) -> dict:
    run_dir = Path(run_dir)
    summ = json.loads((run_dir / "summary.json").read_text())
    evs = summ["exchange_by_headroom_mw"]
    if len(evs) != 1:
        raise CheckFailed(f"{run_dir}: expected one headroom, got {list(evs)}")
    ev = next(iter(evs.values()))
    header = _trace_header(run_dir)
    return {"dir": str(run_dir), "header": header, "roster": list(header.get("roster") or []),
            "attackers": [a["agent"] for a in header.get("attackers") or []],
            "harm": (ev.get("net_harm") or {}).get("by_agent_usd"), "value": ev.get("agent_value_usd"),
            "short": (ev.get("net_harm") or {}).get("shortfall_usd")}


def check(atk_dir, twin_dir, further_below_tol: str = "section8", basis: str = "summary_cents") -> dict:
    if further_below_tol not in FURTHER_BELOW_TOLS:
        raise ValueError(f"further_below_tol must be one of {FURTHER_BELOW_TOLS}, not {further_below_tol!r}")
    if basis not in BASES:
        raise ValueError(f"basis must be one of {BASES}, not {basis!r}")
    a, w = load(atk_dir), load(twin_dir)
    bad = []
    if a["header"].get("git_commit") != w["header"].get("git_commit"):
        bad.append("commits differ")
    for name, r in (("attack", a), ("twin", w)):
        c = str(r["header"].get("git_commit") or "")
        if not c or c.endswith("-dirty"):
            bad.append(f"{name}: dirty tree or no commit")
        if not r["roster"]:
            bad.append(f"{name}: no roster in the header")
        for what in ("harm", "value"):
            if r[what] is None:
                bad.append(f"{name}: summary has no {what}")
            else:
                missing = sorted(set(r["roster"]) - set(r[what]))
                if missing:
                    bad.append(f"{name}: roster agents missing from {what}: {missing[:10]}")
    if not a["attackers"]:
        bad.append("attack header has no attackers")
    if w["attackers"]:
        bad.append("twin header has attackers")
    if a["roster"] != w["roster"]:
        bad.append("rosters differ")
    if basis == "unrounded":
        for name, r in (("attack", a), ("twin", w)):
            if r["short"] is None:
                bad.append(f"{name}: summary has no net_harm.shortfall_usd (run with harm_unrounded: true)")
                continue
            missing = sorted(set(r["roster"]) - set(r["short"]))
            if missing:
                bad.append(f"{name}: roster agents missing from shortfall_usd: {missing[:10]}")
            off = [x for x in r["roster"] if x in r["short"] and r["harm"] and x in r["harm"]
                   and abs(round(max(r["short"][x], 0.0), 2) - r["harm"][x]) > 1e-9]
            if off:
                bad.append(f"{name}: shortfall_usd does not round to by_agent_usd for {off[:10]}")
    if bad:
        raise CheckFailed("; ".join(bad))
    if basis == "unrounded":
        g = GUARD_USD
        ha = {x: max(v, 0.0) for x, v in a["short"].items()}
        hw = {x: max(v, 0.0) for x, v in w["short"].items()}
        alone_of = {x: a["value"][x] + a["short"][x] for x in a["short"]}
    else:
        g, ha, hw = 0.0, a["harm"], w["harm"]
        alone_of = {x: a["value"][x] + a["harm"][x] for x in a["harm"]}
    honest = [x for x in a["roster"] if x not in a["attackers"]]
    newly = {x: {"attack_harm_usd": ha[x], "twin_harm_usd": hw[x]} for x in honest
             if ha[x] > g and hw[x] <= g}
    further = {}
    for x in honest:
        if hw[x] > g and ha[x] - hw[x] > g:
            alone = alone_of[x]
            tol = max(NET_HARM_REL * abs(alone), NET_HARM_ABS)
            d = ha[x] - hw[x]
            further[x] = {"attack_harm_usd": ha[x], "twin_harm_usd": hw[x], "further_usd": d,
                          "acting_alone_usd": alone, "tolerance_usd": tol, "beyond_tolerance": d > tol}
    judged = {x: f for x, f in further.items() if further_below_tol == "zero" or f["beyond_tolerance"]}
    already = [x for x in honest if hw[x] > g]
    better = [x for x in already if ha[x] < hw[x]]
    losses = {x: a["value"][x] - w["value"][x] for x in honest if a["value"][x] < w["value"][x]}
    return {"attack_dir": a["dir"], "twin_dir": w["dir"], "commit": a["header"].get("git_commit"),
            "attackers": a["attackers"], "honest_agents": len(honest),
            "further_below_tol": further_below_tol, "basis": basis, "guard_usd": g if basis == "unrounded" else 0.0,
            "verdict": "PASS" if not newly and not judged else "FAIL",
            "newly_below_acting_alone": newly,
            "further_below_acting_alone": further,
            "further_below_judged": judged,
            "zero_tolerance": {"further_below_count": len(further),
                               "further_below_usd": sum(f["further_usd"] for f in further.values()),
                               "verdict": "PASS" if not newly and not further else "FAIL"},
            "attackers_below_acting_alone": {x: ha[x] for x in a["attackers"] if ha[x] > g},
            "honest_below_acting_alone_in_twin": len(already),
            "of_which_better_in_attack": len(better),
            "reported_not_judged": {"twin_relative_loss": {"agents": len(losses),
                                                           "sum_usd": sum(losses.values()),
                                                           "largest_usd": min(losses.values()) if losses else 0.0}}}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("attack")
    ap.add_argument("twin")
    ap.add_argument("--out")
    ap.add_argument("--further-below-tol", choices=FURTHER_BELOW_TOLS, default="section8",
                    help="section8: D1's judged line (default); zero: D88 as first built (reported)")
    args = ap.parse_args(argv)
    try:
        res = check(args.attack, args.twin, args.further_below_tol)
    except CheckFailed as e:
        print(f"INTEGRITY_FAIL: {e}")
        return 2
    text = json.dumps(res, indent=1)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    print(text)
    return 0 if res["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
