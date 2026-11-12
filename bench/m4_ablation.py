"""M4 ablation table (headline claim 2: LLM vs Rule; Fixed alongside) and the Q-N7 paired table.

Reads finished run folders (summary.json; llm_calls.jsonl for per-agent LLM cost). Per arm: controllable peak, share
of the M0 ceiling and of the selfish-to-ceiling gap, value share, net harm, LLM calls and $ per simulated day, label mix.
Per agent (owner, Nov 12; open question 7): LLM $ per simulated day next to $ value added over Rule per simulated day.
Per-model splits from the weighted draw are report only (D93); Q-N7 compares paired runs (all honest agents on X vs
all on Y, same window, same chooser).

Every check takes its roster from each run's header and fails if an agent is missing (CLAUDE.md rule 10), and all
arms must be the same window. Tables built from fake-backend runs are labeled synthetic.

    python -m bench.m4_ablation <run_dir> [<run_dir> ...] [--qn7] [--out table.json]
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path


class CheckFailed(Exception):
    pass


def _spec(cfg: dict) -> dict | None:
    s = cfg.get("llm_strategy")
    if s in (None, "", "None"):
        return None
    if isinstance(s, dict):
        return s
    try:
        return ast.literal_eval(s)
    except (ValueError, SyntaxError) as e:
        raise CheckFailed(f"unreadable llm_strategy in the header config: {e}") from None


def load(run_dir) -> dict:
    d = Path(run_dir)
    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    head = s.get("header") or {}
    roster = list(head.get("roster") or [])
    if not roster:
        raise CheckFailed(f"{d.name}: header has no roster")
    exs = s.get("exchange_by_headroom_mw") or {}
    if len(exs) != 1:
        raise CheckFailed(f"{d.name}: expected one headroom, found {sorted(exs)}")
    ex = next(iter(exs.values()))
    vals = ex.get("agent_value_usd") or {}
    missing = [a for a in roster if a not in vals]
    if missing:
        raise CheckFailed(f"{d.name}: roster agents missing from agent_value_usd: {missing}")
    cfg = head.get("config") or {}
    spec = _spec(cfg)
    arm = "fixed (no llm_strategy block; normal = $5)" if spec is None else str(spec.get("chooser", "fixed"))
    per_cost = defaultdict(float)
    for side in d.glob("exchange_*/llm_calls.jsonl"):
        for ln in side.read_text(encoding="utf-8").splitlines()[1:]:
            r = json.loads(ln)
            if r.get("event") == "call":
                per_cost[r["agent"]] += r.get("usd") or 0.0
    days = (dt.date.fromisoformat(str(cfg["end"])) - dt.date.fromisoformat(str(cfg["start"]))).days + 1
    return {"dir": d.name, "arm": arm, "spec": spec, "roster": roster, "window": (str(cfg["start"]), str(cfg["end"])),
            "days": days, "ex": ex, "values": vals, "llm_cost_by_agent": dict(per_cost),
            "backend": None if spec is None else str(spec.get("backend", "fake" if arm == "llm" else "none")),
            "commit": head.get("git_commit")}


def _row(r: dict) -> dict:
    ex, llm = r["ex"], r["ex"].get("llm") or {}
    c, nh = ex.get("controllable") or {}, ex.get("net_harm") or {}
    labels = defaultdict(int)
    for day in (llm.get("label_counts_by_day") or {}).values():
        for lab, n in day.items():
            labels[lab] += n
    models = sorted((llm.get("by_model") or {}).keys())
    return {"arm": r["arm"], "run": r["dir"], "window": "..".join(r["window"]), "days": r["days"],
            "peak_ctrl_mw": c.get("peak_mw", ex.get("peak_mw")),
            "share_ctrl": c.get("share_of_m0_ceiling", ex.get("share_of_m0_ceiling")),
            "gap_share_ctrl": c.get("share_of_selfish_to_ceiling_gap", ex.get("share_of_selfish_to_ceiling_gap")),
            "value_share": ex.get("value_share_of_m0_ceiling"), "agents_harmed": nh.get("agents_harmed"),
            "total_net_harm_usd": nh.get("total_net_harm_usd"), "llm_calls": llm.get("llm_calls", 0),
            "usd_per_simulated_day": llm.get("usd_per_simulated_day", 0.0), "label_mix": dict(labels),
            "model": models[0] if len(models) == 1 else (models or None), "backend": r["backend"],
            "total_value_usd": sum(r["values"].values())}


def _same(runs: list[dict]) -> None:
    w = {r["window"] for r in runs}
    if len(w) != 1:
        raise CheckFailed(f"arms are on different windows: {sorted(w)}")
    rs = {tuple(sorted(r["roster"])) for r in runs}
    if len(rs) != 1:
        raise CheckFailed("arms have different rosters")


def _fmt(x, nd=3):
    if x is None:
        return "-"
    return f"{x:,.{nd}f}" if isinstance(x, float) else str(x)


def _md(rows: list[dict]) -> str:
    head = ("| arm | run | peak ctrl MW | share of M0 ceiling | gap share | value share | harmed | net harm $ | "
            "LLM calls | LLM $/sim day | labels |")
    out = [head, "|" + "---|" * 11]
    for r in rows:
        out.append(f"| {r['arm']} | {r['run']} | {_fmt(r['peak_ctrl_mw'], 1)} | {_fmt(r['share_ctrl'])} | "
                   f"{_fmt(r['gap_share_ctrl'])} | {_fmt(r['value_share'])} | {_fmt(r['agents_harmed'])} | "
                   f"{_fmt(r['total_net_harm_usd'], 2)} | {r['llm_calls']} | {_fmt(r['usd_per_simulated_day'], 4)} | "
                   f"{json.dumps(r['label_mix'])} |")
    return "\n".join(out)


def _synthetic(runs) -> bool:
    return any(r["backend"] not in (None, "live", "none") for r in runs)


def ablation(run_dirs) -> dict:
    runs = [load(d) for d in run_dirs]
    _same(runs)
    rows = [_row(r) for r in runs]
    by = {r["arm"]: r for r in runs}
    out = {"arms": rows, "window": rows[0]["window"], "roster_size": len(runs[0]["roster"]),
           "label": ("code-only arms (no LLM arm)" if "llm" not in by else
                     "synthetic: fake-backend LLM arm, mechanics only, not claim 2" if _synthetic(runs)
                     else "real-key runs"),
           "markdown": _md(rows)}
    if "llm" in by and "rule" in by:
        llm, rule = by["llm"], by["rule"]
        days = llm["days"]
        per = []
        for a in llm["roster"]:
            per.append({"agent": a, "llm_usd_per_day": llm["llm_cost_by_agent"].get(a, 0.0) / days,
                        "value_added_over_rule_usd_per_day": (llm["values"][a] - rule["values"][a]) / days})
        out["per_agent"] = per
        out["claim_2_llm_vs_rule"] = {
            "total_value_added_usd_per_day": sum(p["value_added_over_rule_usd_per_day"] for p in per),
            "llm_usd_per_day": sum(p["llm_usd_per_day"] for p in per),
            "peak_ctrl_mw_llm_minus_rule": (_row(llm)["peak_ctrl_mw"] or 0) - (_row(rule)["peak_ctrl_mw"] or 0),
            "note": "LLM vs Rule on the same window (design 2.4); a tie is reported as a tie"}
    return out


def qn7(run_dirs) -> dict:
    """Q-N7: paired LLM runs, all honest agents on one model each, same window and chooser."""
    runs = [load(d) for d in run_dirs]
    _same(runs)
    rows = [_row(r) for r in runs]
    for r in rows:
        if not isinstance(r["model"], str):
            raise CheckFailed(f"{r['run']}: Q-N7 needs every honest agent on one model, found {r['model']}")
    out = {"arms": rows, "label": "synthetic" if _synthetic(runs) else "real-key runs", "markdown": _md(rows)}
    if len(runs) == 2:
        out["value_difference_usd_per_day"] = (sum(runs[1]["values"].values()) - sum(runs[0]["values"].values())) \
            / runs[0]["days"]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--qn7", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    try:
        t = qn7(a.runs) if a.qn7 else ablation(a.runs)
    except CheckFailed as e:
        print(f"FAIL: {e}")
        return 1
    print(t["label"])
    print(t["markdown"])
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(t, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
