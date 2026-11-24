"""Triage of one red-team world against its honest twin (D61 part 2): a validator, so it reads the roster from both
runs' trace headers and fails closed if any roster agent is missing from either side's values (CLAUDE.md rule).

Gain: an attacker's full value (energy, wear, every settlement, end SOC at the mean DA price) minus its honest twin's.
Harm: for every other agent, the twin's value minus the attack world's. A world is a candidate when any attacker's gain
exceeds `gain_usd` or any other agent's harm exceeds `harm_usd` (screening thresholds from the round config), or when
the attack world did not finish (liveness). Every number is computed here; nothing comes from a model. A candidate is
not a verdict: it is confirmed by a deterministic replay (redteam/rounds.py) and classified by the owner.
These checks are new: none may enter a pre-registration before it passes on a real saved trace (CLAUDE.md).
"""
from __future__ import annotations

import json
import math

THRESHOLDS = {"gain_usd": 0.01, "harm_usd": 0.01}


class TriageError(ValueError):
    pass


def roster_of(trace_path) -> list[str]:
    with open(trace_path, encoding="utf-8") as f:
        h = json.loads(f.readline())
    if h.get("event") != "header" or "roster" not in h:
        raise TriageError("trace has no header roster")
    return [str(a) for a in h["roster"]]


def triage(attack: dict, twin: dict, attackers: list[str], thresholds: dict | None = None) -> dict:
    th = {k: float(v) for k, v in {**THRESHOLDS, **(thresholds or {})}.items()}     # YAML may read 1.0e9 as text
    if attack.get("error"):
        return {"candidate": True, "reasons": ["run_did_not_finish"], "liveness": False, "error": attack["error"],
                "label": "[sim, synthetic]"}
    roster = roster_of(attack["trace"])
    if roster != roster_of(twin["trace"]):
        raise TriageError("attack and twin rosters differ")
    for side, w in (("attack", attack), ("twin", twin)):
        missing = [a for a in roster if a not in w["value"] or not math.isfinite(w["value"][a])]
        if missing:
            raise TriageError(f"{side} values missing roster agents {missing}")
    unknown = [a for a in attackers if a not in roster]
    if unknown:
        raise TriageError(f"attackers not in the roster: {unknown}")
    gain = {a: attack["value"][a] - twin["value"][a] for a in attackers}
    harm = {b: twin["value"][b] - attack["value"][b] for b in roster if b not in attackers}
    reasons = [f"gain:{a}" for a, g in gain.items() if g > th["gain_usd"]]
    reasons += [f"harm:{b}" for b, h in harm.items() if h > th["harm_usd"]]
    return {"candidate": bool(reasons), "reasons": reasons, "liveness": True, "roster": roster,
            "gain_usd": gain, "harm_usd": harm, "peak_mw": {"attack": attack["peak_mw"], "twin": twin["peak_mw"]},
            "thresholds": th, "label": "[sim, synthetic]"}


def from_run_dir(run_dir) -> dict:
    """Triage inputs from a saved sim/run_m1.py output folder (summary.json plus one exchange trace): the summary's
    per-agent value (`agent_value_usd`, the value bench/d61_check.py judges) and the trace path. Used to run triage
    on real saved traces (the CLAUDE.md rule for new checks)."""
    from pathlib import Path
    run_dir = Path(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    (_, ev), = summary["exchange_by_headroom_mw"].items()
    traces = sorted(run_dir.glob("exchange_h*/trace.jsonl"))
    if len(traces) != 1:
        raise TriageError(f"{run_dir}: expected one exchange trace, found {len(traces)}")
    vals = ev.get("agent_value_usd")
    if not isinstance(vals, dict):
        raise TriageError(f"{run_dir}: summary has no agent_value_usd")
    return {"trace": str(traces[0]), "value": {str(k): float(v) for k, v in vals.items()},
            "peak_mw": float(ev.get("peak_mw", float("nan")))}
