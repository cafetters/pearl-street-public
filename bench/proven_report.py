"""Tables for the proven-capacity sweep report (docs/plans/proven_capacity_experiment_report.md). Report only; reads
the 12 shards' summary.json and proven.json under <root>/pcsweep_NN/ and prints Markdown. [sim, synthetic]

    python -m bench.proven_report runs/cloud [--commit 81cb416]

Section A is the pre-registered measures and the trade-off curve (docs/plans/proven_capacity_experiment.md).
Section B is measures chosen after seeing the results; the report labels them so.
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

B = (1000, 5000, 20000)
R = (5000, 20000, 50000)
S = (2, 6, 20)
EXISTING = tuple(f"H{i}" for i in range(1, 11))


def load(root: Path, commit: str | None = None) -> dict:
    rows, commits = {}, set()
    for f in sorted(root.glob("pcsweep_*/summary.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        commits.add(d["header"].get("git_commit", "?"))
        for r in d["rows"]:
            r["_dir"] = f.parent / r["name"]
            rows[r["name"]] = r
    if len(rows) != 162:
        raise SystemExit(f"expected 162 points, found {len(rows)}")
    if commit and not all(c.startswith(commit) for c in commits):
        raise SystemExit(f"commits {commits} are not {commit}")
    return rows


def name(sc, b, r, s, twin=False) -> str:
    return f"{sc}_b{b}_r{r}_s{s}" + ("_twin" if twin else "")


def proven(row) -> dict:
    return json.loads(next(Path(row["_dir"]).glob("exchange_*/proven.json")).read_text(encoding="utf-8"))


def calls_to_full(row) -> tuple:
    """(median, max, not reached) of honest newcomers' binding calls to full proven capacity; not reached counts as
    above every reached value for the median."""
    v = list(row["honest_calls_to_real_kw"].values())
    ok = [x for x in v if x is not None]
    med = statistics.median([x if x is not None else float("inf") for x in v]) if v else None
    return med, (max(ok) if ok else None), len(v) - len(ok)


def gain(rows, sc, b, r, s) -> float:
    a, t = rows[name(sc, b, r, s)], rows[name(sc, b, r, s, True)]
    return sum(a["settlement_usd"][x] - t["settlement_usd"][x] for x in a["liars"])


def implied_imbalance_price(row) -> float | None:
    """$/MWh: the liars' imbalance charges over their charged deviation energy (sanity check of the gain's size)."""
    usd = kwh = 0.0
    with open(next(Path(row["_dir"]).glob("exchange_*/trace.jsonl")), encoding="utf-8") as f:
        next(f)
        for line in f:
            if '"imbalance"' not in line:
                continue
            m = json.loads(line)
            if m.get("msg_type") == "imbalance" and m.get("to") in row["liars"]:
                usd += float(m["payload"]["charge_usd"])
                kwh += abs(float(m["payload"]["deviation_kwh"]))
    return usd / kwh * 1000.0 if kwh > 0 else None


def fmt(x, nd=2) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float) and x == float("inf"):
        return "not reached"
    return f"{x:,.{nd}f}" if isinstance(x, float) else str(x)


def section_a(rows) -> list[str]:
    out = ["## A. Pre-registered measures, per grid point", "",
           "| B MW | R MW | strength | honest calls to full (median / max / not reached) | peak uncovered MW: giant / "
           "sybil | steps with any uncovered: giant / sybil | max expected shortfall kW: giant / sybil / honest | "
           "firm-when-called: none / honest / giant / sybil | attacker gain vs twin $: giant / sybil | record "
           "detection steps (min-max) |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for b in B:
        for r in R:
            for s in S:
                hf, gl, sy, nn = (rows[name(x, b, r, s)] for x in ("honest_flood", "giant_liar", "sybil", "no_newcomers"))
                med, mx, nr = calls_to_full(hf)
                det = [d["record_steps"] for x in (gl, sy) for d in x["detect_steps"]]
                out.append(
                    f"| {b // 1000} | {r // 1000} | {s} | {fmt(med, 0)} / {fmt(mx)} / {nr} "
                    f"| {fmt(gl['peak_uncovered_mw'], 3)} / {fmt(sy['peak_uncovered_mw'], 3)} "
                    f"| {gl['uncovered_steps']} / {sy['uncovered_steps']} "
                    f"| {fmt(float(gl['max_expected_shortfall_unproven_kw']), 0)} / "
                    f"{fmt(float(sy['max_expected_shortfall_unproven_kw']), 0)} / "
                    f"{fmt(float(hf['max_expected_shortfall_unproven_kw']), 0)} "
                    f"| {fmt(nn['firm_when_called'], 3)} / {fmt(hf['firm_when_called'], 3)} / "
                    f"{fmt(gl['firm_when_called'], 3)} / {fmt(sy['firm_when_called'], 3)} "
                    f"| {fmt(gain(rows, 'giant_liar', b, r, s), 0)} / {fmt(gain(rows, 'sybil', b, r, s), 0)} "
                    f"| {min(det) if det else 'n/a'}-{max(det) if det else 'n/a'} |")
    out += ["", "Detection on the meter: 0 steps at every point (the simulator meters each step in that step).", ""]
    # twins: control that the honest twins have no uncovered shortfall
    tw = [rows[name(sc, b, r, s, True)] for sc in ("giant_liar", "sybil") for b in B for r in R for s in S]
    out.append(f"Honest twins (54): peak uncovered max {max(t['peak_uncovered_mw'] for t in tw):.3f} MW, steps with "
               f"any uncovered max {max(t['uncovered_steps'] for t in tw)}.")
    gl = rows[name("giant_liar", 1000, 5000, 2)]
    sy = rows[name("sybil", 1000, 5000, 2)]
    out.append(f"Implied imbalance price on the liars' deviations (b1 r5 s2): giant {fmt(implied_imbalance_price(gl))} "
               f"$/MWh, sybil {fmt(implied_imbalance_price(sy))} $/MWh.")
    return out


def section_curve(rows) -> list[str]:
    out = ["## Trade-off curve (pre-registered): one point per (B, R, strength)", "",
           "| B MW | R MW | strength | x: honest median calls to full | y: peak uncovered MW (max of giant, sybil) | "
           "attacker gain $ (giant / sybil) | firm-when-called (min over the four scenarios) |", "|---|---|---|---|---|---|---|"]
    pts = {}
    for b in B:
        for r in R:
            for s in S:
                hf = rows[name("honest_flood", b, r, s)]
                x = calls_to_full(hf)[0]
                y = max(rows[name("giant_liar", b, r, s)]["peak_uncovered_mw"], rows[name("sybil", b, r, s)]["peak_uncovered_mw"])
                fw = [rows[name(sc, b, r, s)]["firm_when_called"] for sc in ("no_newcomers", "honest_flood", "giant_liar", "sybil")]
                fw = [v for v in fw if v is not None]
                pts.setdefault((x, round(y, 6)), []).append((b, r, s))
                out.append(f"| {b // 1000} | {r // 1000} | {s} | {fmt(x, 0)} | {y:.3f} | "
                           f"{fmt(gain(rows, 'giant_liar', b, r, s), 0)} / {fmt(gain(rows, 'sybil', b, r, s), 0)} | "
                           f"{min(fw):.3f} |")
    out += ["", f"Distinct (x, y) points: {len(pts)}."]
    for (x, y), g in sorted(pts.items()):
        out.append(f"- x = {fmt(x, 0)}, y = {y:.3f} MW: {len(g)} grid points (B MW in "
                   f"{sorted({b // 1000 for b, _, _ in g})}, R MW in {sorted({r // 1000 for _, r, _ in g})}, "
                   f"strength in {sorted({s for _, _, s in g})})")
    return out


def section_b(rows) -> list[str]:
    out = ["## B. Chosen after seeing the results (not pre-registered)", "",
           "| B MW | R MW | strength | uncovered MWh: giant / sybil | steps > 100 kW: giant / sybil | last call's peak "
           "uncovered kW: giant / sybil | honest newcomers' final rate (median) | liars' final proven kW / rate (giant; "
           "sybil S1) |", "|---|---|---|---|---|---|---|---|"]
    for b in B:
        for r in R:
            for s in S:
                gl, sy, hf = (rows[name(x, b, r, s)] for x in ("giant_liar", "sybil", "honest_flood"))
                cells = []
                for row in (gl, sy):
                    st = proven(row)["steps"]
                    ks = sorted(st)
                    u = [st[t]["uncovered_kw"] for t in ks]
                    last_call = st[ks[-1]]["call_id"]
                    cells.append((sum(u) * 0.25 / 1000.0, sum(v > 100.0 for v in u),
                                  max(st[t]["uncovered_kw"] for t in ks if st[t]["call_id"] == last_call)))
                fr = statistics.median(v["rate"] for a, v in hf["final"].items() if a in hf["newcomers"])
                g, s1 = gl["final"]["G"], sy["final"]["S1"]
                out.append(f"| {b // 1000} | {r // 1000} | {s} | {cells[0][0]:.1f} / {cells[1][0]:.1f} | "
                           f"{cells[0][1]} / {cells[1][1]} | {cells[0][2]:.0f} / {cells[1][2]:.0f} | {fr:.3f} | "
                           f"{g['proven_kw']:.0f} / {g['rate']:.3f}; {s1['proven_kw']:.0f} / {s1['rate']:.3f} |")
    return out


def main(argv) -> None:
    root = Path(argv[0])
    commit = argv[argv.index("--commit") + 1] if "--commit" in argv else None
    rows = load(root, commit)
    print("\n".join(section_a(rows) + [""] + section_curve(rows) + [""] + section_b(rows)))


if __name__ == "__main__":
    main(sys.argv[1:])
