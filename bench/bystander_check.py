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
            "harm": (ev.get("net_harm") or {}).get("by_agent_usd"), "value": ev.get("agent_value_usd")}


def check(atk_dir, twin_dir, further_below_tol: str = "section8") -> dict:
    if further_below_tol not in FURTHER_BELOW_TOLS:
        raise ValueError(f"further_below_tol must be one of {FURTHER_BELOW_TOLS}, not {further_below_tol!r}")
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
    if bad:
        raise CheckFailed("; ".join(bad))
    honest = [x for x in a["roster"] if x not in a["attackers"]]
    newly = {x: {"attack_harm_usd": a["harm"][x], "twin_harm_usd": w["harm"][x]} for x in honest
             if a["harm"][x] > 0.0 and w["harm"][x] <= 0.0}
    further = {}
    for x in honest:
        if w["harm"][x] > 0.0 and a["harm"][x] > w["harm"][x]:
            alone = a["value"][x] + a["harm"][x]
            tol = max(NET_HARM_REL * abs(alone), NET_HARM_ABS)
            d = a["harm"][x] - w["harm"][x]
            further[x] = {"attack_harm_usd": a["harm"][x], "twin_harm_usd": w["harm"][x], "further_usd": d,
                          "acting_alone_usd": alone, "tolerance_usd": tol, "beyond_tolerance": d > tol}
    judged = {x: f for x, f in further.items() if further_below_tol == "zero" or f["beyond_tolerance"]}
    already = [x for x in honest if w["harm"][x] > 0.0]
    better = [x for x in already if a["harm"][x] < w["harm"][x]]
    losses = {x: a["value"][x] - w["value"][x] for x in honest if a["value"][x] < w["value"][x]}
    return {"attack_dir": a["dir"], "twin_dir": w["dir"], "commit": a["header"].get("git_commit"),
            "attackers": a["attackers"], "honest_agents": len(honest),
            "further_below_tol": further_below_tol,
            "verdict": "PASS" if not newly and not judged else "FAIL",
            "newly_below_acting_alone": newly,
            "further_below_acting_alone": further,
            "further_below_judged": judged,
            "zero_tolerance": {"further_below_count": len(further),
                               "further_below_usd": sum(f["further_usd"] for f in further.values()),
                               "verdict": "PASS" if not newly and not further else "FAIL"},
            "attackers_below_acting_alone": {x: a["harm"][x] for x in a["attackers"] if a["harm"][x] > 0.0},
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
