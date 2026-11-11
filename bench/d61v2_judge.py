"""D61 v2 judge: one thin tool, pinned with the v2 pre-registration (owner, Nov 7, decisions D3 and D4), that applies
the draft's pass rule (docs/plans/d61v2_attacks_check.md) to each attack cell against its honest twin.

    python -m bench.d61v2_judge --runs <runs dir> --out-dir <dir> [--cells <cell> ...] [--glob 'h2h_c_d61v2_*']
                                [--pair <cell> <twin> ...] [--d90-code-root <checkout>] [--data-dir <data>]

Each piece keeps its own tool and tests; this file only combines them:
  - bench.d61_check (pinned D61 tool, unchanged): integrity and roster (INTEGRITY_FAIL / TWIN_FAIL are simulation
    failures, passed through), each attacker's gain against GAIN_GUARD_USD ($1e-6), each attacker's named defense, and
    the zero-price-B opportunity;
  - bench.bystander_check with further_below_tol="section8" (D1, staged D91, revising D88): no honest agent newly below
    acting alone (zero tolerance) and none further below beyond max(1% of |acting alone|, $10). The zero-tolerance
    further-below count and $ are reported next to it in every row;
  - bench.d90_check (D90), for infeasible_declare attackers: NOT_EXERCISED when the filed set was feasible at filing;
    otherwise D82 must have fired (DISAGREE fails);
  - gap 1 (D87): a misdeclare attacker under declared_reference with a breach (above_power_steps or
    below_reserve_steps above 0) must have a non-empty `voided_calls` in its D65 row, besides d61_check's defense.
    That requirement lives here, not in bench/d61_check.py, so D61 stays re-checkable as judged.
Gap 6: every attacker is judged on its own. An attacker is NOT_EXERCISED (renege_zero_b with no opportunity in the
twin; infeasible_declare feasible at filing), INCOMPLETE (no D90 result), PASS (no gain and its defense fired) or FAIL.
The cell is NOT_EXERCISED when every attacker is, INCOMPLETE when any judged attacker is, PASS when every judged
attacker passes and the bystander line passes, else FAIL.
Judged cells: margin $5 under today's rule. m = 0 cells and max-rule reruns carry the same verdict, reported only.
Gap 2, the headline table, every cell: twin-relative loss (n, $; zero tolerance; d61_check's losses_vs_twin), the
controllable-steps peak against the twin's, pre_from_rebound, operator payout against the twin, D61's $10 line count;
and voided_days, voided_calls, the D90 result and the full-slice peak (footnote).
Writes <out-dir>/cells/<cell>.json, <out-dir>/judge.json (with this tool's and its components' sha256 and the git
commit) and <out-dir>/judge.md.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from bench import bystander_check as B
from bench import d61_check as C
from bench.d61v2_run_queue import twin_of
from bench.tolerances import GAIN_GUARD_USD

ROOT = Path(__file__).resolve().parents[1]
SIM_FAILURES = ("INTEGRITY_FAIL", "TWIN_FAIL")
MISDECLARE = ("misdeclare_power", "misdeclare_reserve")
# The dirty check (owner, Nov 11, decision 2): a modified tracked file or an untracked file under any of these marks
# the commit -dirty (git-ignored files don't count), and so does a failing `git status` (fail closed).
DIRTY_PATHS = ("bench", "sim", "agents", "exchange", "attacks")
COMPONENTS = ("bench/d61v2_judge.py", "bench/d61_check.py", "bench/bystander_check.py", "bench/d90_check.py",
              "bench/d61v2_run_queue.py", "bench/tolerances.py")


class Refused(Exception):
    """The judge can't apply the v2 rule to this cell (fails closed)."""


def _true(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


def twin_for(cell: str) -> str:
    """The twin of a cell: smoke cells named `_all_` map to `_twin_`; every other cell through twin_of()."""
    if "_all_" in cell:
        return cell.replace("_all_", "_twin_", 1)
    return twin_of(cell)


def judged_cell(cfg: dict) -> bool:
    """D61 v2 judges $5 cells under today's rule only."""
    try:
        m = float(cfg.get("peak_response_margin_usd_mwh"))
    except (TypeError, ValueError):
        return False
    return m == 5.0 and cfg.get("settlement_rule") in (None, "", "today", "None")


def _attacker(aid, p, c, d90, cfg) -> dict:
    key = p["defense"]
    cnt = p.get("counters") or {}
    vc = ((p.get("d65") or {}).get("this_run") or {}).get("voided_calls")
    fired, not_ex, why, d90v = p["defense_fired"], False, [], None
    if not fired:
        why.append(f"named defense {p.get('status')}")
    if key in MISDECLARE:
        if not _true(cfg.get("declared_reference")):
            raise Refused(f"{aid}: {key} outside declared_reference")
        breach = (cnt.get("above_power_steps") or 0) > 0 or (cnt.get("below_reserve_steps") or 0) > 0
        if breach and not vc:
            fired = False
            why.append("breach without voided_calls (D87)")
    if key == "infeasible_declare":
        d90v = ((d90 or {}).get("attackers") or {}).get(aid, {}).get("verdict")
        why = []
        if d90v is None:
            fired = None
            why.append("no D90 result")
        elif d90v == "NOT_EXERCISED":
            not_ex = True
        else:
            fired = d90v == "PASS"
            if not fired:
                why.append(f"D90 {d90v}")
    if key == "renege_zero_b" and c.get("opportunity_steps") == 0:
        not_ex = True
    gain_ok = p["gain_usd"] <= GAIN_GUARD_USD
    if not gain_ok:
        why.append(f"gain ${p['gain_usd']:.6f} above the ${GAIN_GUARD_USD} guard")
    verdict = ("NOT_EXERCISED" if not_ex else "INCOMPLETE" if fired is None else
               "PASS" if gain_ok and fired else "FAIL")
    return {"verdict": verdict, "defense": key, "defense_status": p.get("status"), "gain_usd": p["gain_usd"],
            "parts": {"gain": gain_ok, "defense": fired}, "why": "; ".join(why) if verdict == "FAIL" else "",
            "voided_days": cnt.get("voided_days"), "voided_calls": vc, "d90": d90v, "counters": cnt}


def combine(c: dict, by: dict | None, d90: dict | None, ea: dict, et: dict, cfg: dict) -> dict:
    """The v2 verdict of one cell from its components' results (d61_check, bystander_check, d90_check) and the two
    summaries' exchange blocks."""
    judged = judged_cell(cfg)
    if c["verdict"] in SIM_FAILURES:
        return {"verdict": c["verdict"], "judged": judged, "detail": c.get("integrity") or c.get("twin")}
    if by.get("further_below_tol") != "section8":
        raise Refused(f"bystander_check must run with further_below_tol section8, not {by.get('further_below_tol')}")
    atts = {aid: _attacker(aid, p, c, d90, cfg) for aid, p in c["attackers"].items()}
    vs = [a["verdict"] for a in atts.values() if a["verdict"] != "NOT_EXERCISED"]
    by_ok = by["verdict"] == "PASS"
    verdict = ("NOT_EXERCISED" if not vs else "INCOMPLETE" if "INCOMPLETE" in vs else
               "PASS" if all(v == "PASS" for v in vs) and by_ok else "FAIL")
    why = [f"{aid}: {a['why']}" for aid, a in atts.items() if a["verdict"] == "FAIL"]
    if not by_ok and vs:
        why.append(f"bystander: {len(by['newly_below_acting_alone'])} newly below, "
                   f"{len(by['further_below_judged'])} further below beyond section 8's tolerance")
    z = by["zero_tolerance"]
    ca, ct = ea.get("controllable") or {}, et.get("controllable") or {}
    loss = c["reported_not_judged"]["losses_vs_twin"]
    return {
        "verdict": verdict, "judged": judged, "why": "; ".join(why) if verdict == "FAIL" else "",
        "attackers": atts,
        "bystander": {"verdict": by["verdict"], "newly_below_n": len(by["newly_below_acting_alone"]),
                      "further_below_judged_n": len(by["further_below_judged"]),
                      "zero_tolerance_further_below": {"n": z["further_below_count"], "usd": z["further_below_usd"],
                                                       "verdict": z["verdict"]}},
        "headline": {"twin_rel_loss": {"n": loss["agents"], "usd": loss["sum_negative_differences_usd"]},
                     "peak_ctrl_mw": ca.get("peak_mw"), "peak_ctrl_twin_mw": ct.get("peak_mw"),
                     "pre_from_rebound": ca.get("pre_from_rebound"), "pre_from_rebound_twin": ct.get("pre_from_rebound"),
                     "pre_from_peak_mw": ca.get("pre_from_peak_mw"),
                     "share_ctrl": ca.get("share_of_m0_ceiling"), "share_ctrl_twin": ct.get("share_of_m0_ceiling"),
                     "operator_payout_vs_twin_usd": c["operator_payout_usd"]["diff"],
                     "d61_harm10_agents": c["harm"]["agents_beyond_tolerance"],
                     "peak_full_mw": ea.get("peak_mw"), "peak_full_twin_mw": et.get("peak_mw")},
    }


def run_d90(atk_dir, code_root=None, data_dir=None) -> dict:
    """bench.d90_check in its own process (it imports sim from the run commit's checkout)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "d90.json"
        cmd = [sys.executable, "-m", "bench.d90_check", str(atk_dir), "--out", str(out)]
        cmd += ["--code-root", str(code_root)] if code_root else []
        cmd += ["--data-dir", str(data_dir)] if data_dir else []
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        res = json.loads(out.read_text()) if out.exists() else {"verdict": "ERROR", "reason": r.stderr[-1000:]}
    return res


def judge_cell(atk_dir, twin_dir, d90_code_root=None, data_dir=None) -> dict:
    atk_dir, twin_dir = Path(atk_dir), Path(twin_dir)
    a, t = C.load_run(atk_dir), C.load_run(twin_dir)
    cfg = C._cfg(a)
    base = {"cell": atk_dir.name, "twin": twin_dir.name, "commit": a["header"].get("git_commit")}
    c = C.check_pair(a, t)
    if c["verdict"] in SIM_FAILURES:
        return {**base, **combine(c, None, None, a["ev"], t["ev"], cfg)}
    try:
        by = B.check(atk_dir, twin_dir, further_below_tol="section8")
    except B.CheckFailed as e:
        return {**base, "verdict": "INTEGRITY_FAIL", "judged": judged_cell(cfg), "detail": [f"bystander_check: {e}"]}
    d90 = None
    if any(s.get("type") == "infeasible_declare" for s in a["header"].get("attackers") or []):
        d90 = run_d90(atk_dir, d90_code_root, data_dir)
    out = {**base, **combine(c, by, d90, a["ev"], t["ev"], cfg)}
    if d90 is not None:
        out["d90_result"] = {k: d90.get(k) for k in ("verdict", "reason", "attackers", "commit", "code_root")}
    return out


def _f(x, n=2):
    if x is None:
        return "n/a"
    return f"{x:,.{n}f}" if isinstance(x, (int, float)) and not isinstance(x, bool) else str(x)


def table(rows: list[dict]) -> str:
    hdr = ["Cell", "Verdict", "Attacker: verdict (gain $, defense)", "Bystander (D1)",
           "zero-tol further below (n, $)", "voided_calls", "D90", "twin-rel. loss (n, $)",
           "ctrl peak MW cell / twin", "rebound", "operator payout vs twin $", "D61 $10 n"]
    lines = ["| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
    for r in rows:
        v = r["verdict"] + ("" if r.get("judged") else " (reported, not judged)")
        if "headline" not in r:
            lines.append(f"| {r['cell']} | {v} | {r.get('detail')} |" + " |" * (len(hdr) - 3))
            continue
        atk = "; ".join(f"{aid}: {a['verdict']} ({_f(a['gain_usd'], 6)}, {a['defense_status']})"
                        for aid, a in r["attackers"].items())
        b, z, h = r["bystander"], r["bystander"]["zero_tolerance_further_below"], r["headline"]
        vc = "; ".join(f"{aid}: {_f(a['voided_calls'])}" for aid, a in r["attackers"].items()
                       if a["defense"] in MISDECLARE or a["voided_calls"])
        d90 = "; ".join(f"{aid}: {a['d90']}" for aid, a in r["attackers"].items() if a["defense"] == "infeasible_declare")
        lines.append(" | ".join([
            f"| {r['cell']}", v, atk,
            f"{b['verdict']} (newly {b['newly_below_n']}, further judged {b['further_below_judged_n']})",
            f"{z['n']}, {_f(z['usd'])}", vc or "-", d90 or "-",
            f"{h['twin_rel_loss']['n']}, {_f(h['twin_rel_loss']['usd'])}",
            f"{_f(h['peak_ctrl_mw'], 1)} / {_f(h['peak_ctrl_twin_mw'], 1)}", _f(h["pre_from_rebound"]),
            _f(h["operator_payout_vs_twin_usd"]), f"{h['d61_harm10_agents']} |"]))
    return "\n".join(lines) + "\n"


def _sha256(rel: str) -> str | None:
    """sha256 of the file with CRLF read as LF: the git blob's hash (`git show <commit>:<path> | sha256sum`), the same
    on any checkout whatever core.autocrlf does to the working tree."""
    p = ROOT / rel
    return hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest() if p.exists() else None


def tool_header() -> dict:
    r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    d = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all", "--", *DIRTY_PATHS],
                       capture_output=True, text=True)
    dirty = d.returncode != 0 or bool(d.stdout.strip())
    return {"tool": "bench/d61v2_judge.py", "git_commit": r.stdout.strip() + ("-dirty" if dirty else ""),
            "sha256_lf": {k: _sha256(k) for k in COMPONENTS}}


def cells_in(runs: Path, glob: str) -> list[tuple[str, str]]:
    out = []
    for d in sorted(runs.glob(glob)):
        if "_twin_" in d.name or not (d / "summary.json").exists():
            continue
        try:
            tw = twin_for(d.name)
        except ValueError:
            continue
        if (runs / tw / "summary.json").exists():
            out.append((d.name, tw))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--cells", nargs="*")
    ap.add_argument("--glob", default="h2h_c_d61v2_*")
    ap.add_argument("--pair", nargs=2, action="append", metavar=("CELL", "TWIN"))
    ap.add_argument("--d90-code-root")
    ap.add_argument("--data-dir")
    a = ap.parse_args(argv)
    runs, out = Path(a.runs), Path(a.out_dir)
    pairs = ([(c, twin_for(c)) for c in a.cells] if a.cells else cells_in(runs, a.glob) if not a.pair else [])
    pairs += [tuple(p) for p in a.pair or []]
    rows = []
    (out / "cells").mkdir(parents=True, exist_ok=True)
    for cell, tw in pairs:
        try:
            r = judge_cell(runs / cell, runs / tw, a.d90_code_root, a.data_dir)
        except Refused as e:
            r = {"cell": cell, "twin": tw, "verdict": "REFUSED", "judged": False, "detail": str(e)}
        (out / "cells" / f"{cell}.json").write_text(json.dumps(r, indent=1, default=str), encoding="utf-8")
        rows.append(r)
    (out / "judge.json").write_text(json.dumps({"header": tool_header(), "rows": rows}, indent=1, default=str),
                                    encoding="utf-8")
    md = table(rows)
    (out / "judge.md").write_text(md, encoding="utf-8")
    print(md)
    from collections import Counter
    print("judged:", dict(Counter(r["verdict"] for r in rows if r.get("judged"))),
          " reported:", dict(Counter(r["verdict"] for r in rows if not r.get("judged"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
