"""The four proven-capacity scenarios (owner, Nov 19, plan step 6) and their measures. All synthetic.

Each scenario is an existing fleet (meter history: they start proven at demonstrated power) plus newcomers (no meter
history; the metering service attests their paperwork, `newcomers: {id: {paper_kw}}`). A liar's paper exceeds its
battery, and it nominates its paper (`over_nominate`, extra = paper - real); its battery runs the honest plan.

- no_newcomers: the existing fleet only (control);
- honest_flood: k honest newcomers whose paper is their real power;
- giant_liar: one newcomer with paper `paper_kw` and a small battery;
- sybil: m small liars, each its own legal entity, each with paper at or under the starter cap.

`run` returns the measures from the trace and proven.json (bench/firmness.py reads the same files):
peak uncovered shortfall (MW) and its duration (steps), the fleet's metered peak cut vs acting alone (MW; no M0 on a
synthetic slice, so the share of the ceiling is reported as this cut), honest newcomers' calls to reach their real
power, each liar's settlement gain vs its honest twin (1p peak settlement plus imbalance charges; the twin is the
same agent nominating honestly), the max expected shortfall from unproven capacity, and the firm-when-called rate.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from redteam.synth import BASE_CFG, HEADROOM_MW, synthetic_slice

EXISTING = (("H1", 1000.0, 4000.0), ("H2", 500.0, 2000.0))
EXISTING_MW = tuple((f"H{i + 1}", 1000.0, 4000.0) for i in range(10))     # the sweep's existing fleet: 10 x 1 MW
SCENARIOS = ("no_newcomers", "honest_flood", "giant_liar", "sybil")


def build(name: str, k: int = 4, honest_kw: float = 300.0, giant_real_kw: float = 100.0, giant_paper_kw: float = 5000.0,
          m: int = 10, sybil_real_kw: float = 20.0, sybil_paper_kw: float = 100.0, existing=EXISTING):
    """(fleet, newcomers {id: {paper_kw}}, liars {id: extra_kw}) for a scenario. `existing`: the fleet with meter
    history ((id, power kW, energy kWh) tuples); the sweep uses a MW-scale one."""
    fleet, nc, liars = list(existing), {}, {}
    if name == "honest_flood":
        for i in range(k):
            fleet.append((f"N{i + 1}", honest_kw, 4 * honest_kw))
            nc[f"N{i + 1}"] = {"paper_kw": honest_kw}
    elif name == "giant_liar":
        fleet.append(("G", giant_real_kw, 4 * giant_real_kw))
        nc["G"] = {"paper_kw": giant_paper_kw}
        liars["G"] = giant_paper_kw - giant_real_kw
    elif name == "sybil":
        for i in range(m):
            a = f"S{i + 1}"
            fleet.append((a, sybil_real_kw, 4 * sybil_real_kw))
            nc[a] = {"paper_kw": sybil_paper_kw}
            liars[a] = sybil_paper_kw - sybil_real_kw
    elif name != "no_newcomers":
        raise ValueError(f"scenario must be one of {SCENARIOS}")
    return tuple(fleet), nc, liars


def config(name: str, proven: dict | None, days: int, liars: dict, newcomers: dict, honest_twin: bool = False,
           extra: dict | None = None) -> dict:
    atk = [] if honest_twin else [{"agent": a, "type": "over_nominate", "extra_kw": x} for a, x in liars.items()]
    return {**BASE_CFG, "run_name": f"pc_{name}" + ("_twin" if honest_twin else ""), "silent_reference": True,
            **({"proven_capacity": proven} if proven else {}), "newcomers": newcomers, "attackers": atk,
            **(extra or {})}


def run(name: str, proven: dict | None, out: Path, days: int = 10, honest_twin: bool = False, extra=None, **kw) -> dict:
    from bench import firmness as F
    from sim.run_m1 import run_variant
    fleet, nc, liars = build(name, **kw)
    s = synthetic_slice(fleet=fleet, days=days)
    cfg = config(name, proven, days, liars, nc, honest_twin, extra)
    out = Path(out)
    run_variant(cfg, s, np.arange(len(fleet)), out, headroom_mw=HEADROOM_MW)
    var = next(out.glob("exchange_*"))
    log = F.read_trace(var / "trace.jsonl")
    pr = json.loads((var / "proven.json").read_text(encoding="utf-8")) if (var / "proven.json").exists() else None
    imb: dict = {}
    for m_ in log:
        if m_["msg_type"] == "imbalance":
            imb[m_["to"]] = imb.get(m_["to"], 0.0) + float(m_["payload"]["charge_usd"])
    ps = F.peak_settle(log)
    money = {a: ps.get(a, 0.0) - imb.get(a, 0.0) for a, *_ in fleet}
    res = {"scenario": name, "days": days, "fleet": [list(f) for f in fleet], "newcomers": nc, "liars": liars,
           "settlement_usd": money, "final_price": F.final_prices(log), "label": "[sim, synthetic]"}
    if pr is not None:
        steps = pr["steps"].values()
        unc = [v["uncovered_kw"] for v in steps]
        firm = sum(v["firm_kw"] for v in steps)
        res.update({
            "peak_uncovered_mw": max(unc, default=0.0) / 1000.0,
            "uncovered_steps": sum(1 for u in unc if u > 1e-6),
            "max_expected_shortfall_unproven_kw": max((v["expected_shortfall_unproven_kw"] for v in steps), default=0.0),
            "firm_when_called": (sum(v["delivered_firm_kw"] for v in steps) / firm) if firm > 0 else None,
            "final": pr["final"], "events": pr["events"]})
        real = {a: p for a, p, _ in fleet}
        reach = {}
        for a in nc:
            if a in liars:
                continue
            ev = [e for e in pr["events"] if e["agent_id"] == a and e["nominated_kwh"] > 0]
            reach[a] = next((i + 1 for i, e in enumerate(ev) if e["proven_kw_after"] >= real[a] - 1e-6), None)
        res["honest_calls_to_real_kw"] = reach
        miss = [e for e in pr["events"] if e["first_miss_t_utc"]]
        res["detect_steps"] = [{"agent_id": e["agent_id"], "call_id": e["call_id"], "meter_steps": 0,
                                "record_steps": _steps_between(e["first_miss_t_utc"], e["record_t_utc"])}
                               for e in miss]
    return res


def _steps_between(a: str, b: str, minutes: int = 15) -> int:
    from datetime import datetime
    f = lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))       # noqa: E731
    return int(round((f(b) - f(a)).total_seconds() / (60 * minutes)))
