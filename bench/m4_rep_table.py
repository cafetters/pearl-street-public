"""M4 REP table (D24; Appendix A: arm C's per-step peak price is the REP-style shared price, the Lagrangian update).

Rows per window: acting alone, arm A (offers; optional), arm C honest, arm C with liars (attacks/round_liar.py, one
row per run), and the M0 ceiling. Each row: controllable peak, share of the M0 ceiling and of the selfish-to-ceiling
gap, harm, and rounds to converge. Report only. Every run's roster comes from its header; the check fails if the
rosters or windows differ, or if any roster agent is missing from a run's per-agent values.

    python -m bench.m4_rep_table --honest <run> --liars <run> [<run> ...] [--arm-a <run>] [--out t.json]
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path


class CheckFailed(Exception):
    pass


def _load(d) -> dict:
    d = Path(d)
    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    head = s.get("header") or {}
    roster = list(head.get("roster") or [])
    if not roster:
        raise CheckFailed(f"{d.name}: no roster in the header")
    exs = s.get("exchange_by_headroom_mw") or {}
    if len(exs) != 1:
        raise CheckFailed(f"{d.name}: expected one headroom")
    ex = next(iter(exs.values()))
    missing = [a for a in roster if a not in (ex.get("agent_value_usd") or {})]
    if missing:
        raise CheckFailed(f"{d.name}: roster agents missing from agent_value_usd: {missing}")
    cfg = head.get("config") or {}
    att = cfg.get("attackers")
    try:
        att = ast.literal_eval(att) if isinstance(att, str) and att not in ("", "None") else (att or [])
    except (ValueError, SyntaxError):
        att = []
    return {"name": d.name, "s": s, "ex": ex, "roster": roster, "window": (cfg.get("start"), cfg.get("end")),
            "liars": [a.get("agent") for a in att if isinstance(a, dict) and a.get("type") == "round_liar"]}


def _row(label, peak, share, gap, harm=None, net=None, rounds=None, at_cap=None, liars=None):
    return {"row": label, "peak_ctrl_mw": peak, "share_ctrl": share, "gap_share_ctrl": gap, "agents_harmed": harm,
            "total_net_harm_usd": net, "rounds_total": rounds, "rounds_at_cap": at_cap, "liars": liars or []}


def _ex_row(label, r):
    ex = r["ex"]
    c, nh, pm = ex.get("controllable") or {}, ex.get("net_harm") or {}, ex.get("price_mode") or {}
    return _row(label, c.get("peak_mw", ex.get("peak_mw")), c.get("share_of_m0_ceiling"),
                c.get("share_of_selfish_to_ceiling_gap"), nh.get("agents_harmed"), nh.get("total_net_harm_usd"),
                pm.get("rounds_total"), pm.get("rounds_at_cap"), r["liars"])


def _f(x, nd=3):
    return "-" if x is None else (f"{x:,.{nd}f}" if isinstance(x, float) else str(x))


def rep_table(honest, liars, arm_a=None) -> dict:
    h = _load(honest)
    others = [_load(x) for x in liars] + ([_load(arm_a)] if arm_a else [])
    for o in others:
        if sorted(o["roster"]) != sorted(h["roster"]):
            raise CheckFailed(f"{o['name']}: roster differs from {h['name']}")
        if o["window"] != h["window"]:
            raise CheckFailed(f"{o['name']}: window {o['window']} differs from {h['window']}")
    s = h["s"]
    sc = (s.get("selfish") or {}).get("controllable") or {}
    rows = [_row("acting alone", sc.get("peak_mw", (s.get("selfish") or {}).get("peak_mw")),
                 sc.get("share_of_m0_ceiling"), 0.0)]
    if arm_a:
        rows.append(_ex_row("arm A (offers)", others[-1]))
    rows.append(_ex_row("arm C honest (REP-style price)", h))
    for r in others[:len(liars)]:
        rows.append(_ex_row(f"arm C with liars: {r['name']}", r))
    m0c = ((s.get("peak_steps") or {}).get("m0_controllable") or {}).get("ceiling_peak_mw")
    rows.append(_row("M0 ceiling", m0c if m0c is not None else (s.get("m0") or {}).get("ceiling_peak_mw"), 1.0, 1.0))
    head = "| row | peak ctrl MW | share of M0 ceiling | gap share | harmed | net harm $ | rounds | at cap | liars |"
    md = [head, "|" + "---|" * 9]
    for r in rows:
        md.append(f"| {r['row']} | {_f(r['peak_ctrl_mw'], 1)} | {_f(r['share_ctrl'])} | {_f(r['gap_share_ctrl'])} | "
                  f"{_f(r['agents_harmed'])} | {_f(r['total_net_harm_usd'], 2)} | {_f(r['rounds_total'])} | "
                  f"{_f(r['rounds_at_cap'])} | {' '.join(r['liars'])} |")
    return {"window": h["window"], "roster_size": len(h["roster"]), "rows": rows, "markdown": "\n".join(md),
            "label": "report only (D24 comparison)"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--honest", required=True)
    ap.add_argument("--liars", nargs="*", default=[])
    ap.add_argument("--arm-a")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    try:
        t = rep_table(a.honest, a.liars, a.arm_a)
    except CheckFailed as e:
        print(f"FAIL: {e}")
        return 1
    print(t["markdown"])
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(t, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
