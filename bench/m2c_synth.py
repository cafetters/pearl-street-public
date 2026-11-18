"""M2c on the synthetic grid (sim/synth_grid.py; made-up, disclosed numbers; D35): the zone product end to end, report.

Cases (arm C, 1p, zones on; 2 days; price_rounds_per_gate 20 unless --rounds):
  designed     prices "midday": acting alone the batteries idle through the evening; the overload is the background
               load's and the batteries' sign-correct capacity covers it (pass item 2, synthetic half)
  stress       prices "cheap_peak": acting alone the batteries charge into the congestion (report)
  undersized   designed prices, bus 4's peak load tripled: l14 needs more relief than B4 has (pass item 1's
               undersized case: lambda reaches its cap, remaining violation per element and step, no one net harmed,
               no zone payment above the cap)
  attacks      thin zone l14, its only asset B4: renege (full) and silent (withholding), each against the designed run
               as its honest twin (report only): B4's gain, l14's price path, how far c_k bounds it

Each case reports AC loading (pandapower) against acting alone, steps over 90% per element, rounds per gate
(converged or round limit), agents' value against acting alone (net harm), zone settlement per agent, and the bound
check: no agent's zone settlement above sum over its elements of cap x K x |PTDF| x |reference - metered|.

    python -m bench.m2c_synth [--rounds 20] [--out runs/m2c/synth]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from bench.runinfo import run_header
from sim import synth_grid as S
from sim.run_m1 import evaluate, net_harm, run_variant

BASE_CFG = {"run_name": "m2c_synth", "zone": "SYS", "operator_id": "GRID_OP", "metering_id": "metering",
            "clock_id": "clock", "layer_mw": 0.05, "price_cap_usd_mwh": 500.0, "post_hour_local": 9,
            "gate_lead_h": 1, "price_publish_hour_local": 12, "forecast_noise_sigma": 0.0, "seed": 1,
            "min_offer_kw": 5.0, "wear_usd_mwh": 10.0, "nominations": True, "rounds": "info",
            "nomination_lead_h": 1, "price_mode": True, "plan_to_nomination": True, "release_superseded_lock": True,
            "price_rounds_per_gate": 20, "settlement_reference": "selfish_1p"}
HEADROOM_MW = 0.5
ATTACKS = {"renege_full": {"type": "renege", "mode": "full"}, "silent": {"type": "silent"}}


def _ac_check(run, ids, undersized=False):
    net_kw = np.asarray(run["D"]) - np.asarray(run["C"])
    ac = S.ac_loading(net_kw, ids, undersized)
    rating = S.branch_table().set_index("key").rating_mva
    over = {k: [i for i in range(len(ac)) if ac[k].iat[i] > 90.0 and (ac[k].iat[i] - 90.0) / 100 * rating[k] > 1e-6]
            for k in S.MONITORED}
    return ac, over


def _case(name, s, part, tmp, rounds, undersized=False, attackers=None, alone=None):
    cfg = {**BASE_CFG, "run_name": f"m2c_synth_{name}", "price_rounds_per_gate": rounds}
    if attackers:
        cfg["attackers"] = attackers
    t0 = time.time()
    sf = alone if alone is not None else run_variant(cfg, s, part, tmp / name / "alone")
    r = run_variant(cfg, s, part, tmp / name, headroom_mw=HEADROOM_MW,
                    grid_zones=S.grid_zones(s, undersized=undersized))
    secs = time.time() - t0
    rec = s.system["system_load"] * 1000.0
    ev, eva = evaluate(cfg, s, part, r, rec, None, sf), evaluate(cfg, s, part, sf, rec, None, sf)
    pe = float(s.system["price_da"].mean()) / 1000.0
    v_alone = eva["_agent_value"] + sf["SOC"][:, -1] * pe
    v_with = ev["_agent_value"] + r["SOC"][:, -1] * pe
    nh = net_harm(v_alone, v_with, list(s.ids), r["header"]["roster"], 0)
    ac_al, over_al = _ac_check(sf, s.ids, undersized)
    ac, over = _ac_check(r, s.ids, undersized)
    rating = S.branch_table().set_index("key").rating_mva
    iso = S.iso(s.ts_utc)
    remaining = {k: {iso[i]: float((ac[k].iat[i] - 90.0) / 100 * rating[k]) for i in over[k]} for k in S.MONITORED}
    lam_path = {}
    for m in r["log"]:
        if m["msg_type"] == "zone_price":
            p = m["payload"]
            lam_path.setdefault(p["element"], {}).setdefault(p["call_id"], []).append(max(p["lambda_usd_mwh"]))
    cap = S.ZONE_CAP_USD_MWH
    zs = ev.get("zones", {})
    bound_ok = all(lam <= cap + 1e-6 for el in lam_path.values() for path in el.values() for lam in path)
    return {"case": name, "secs": round(secs, 1), "rounds_per_gate": rounds, "attackers": attackers or [],
            "ac_max_pct": {k: float(ac[k].max()) for k in S.MONITORED},
            "alone_ac_max_pct": {k: float(ac_al[k].max()) for k in S.MONITORED},
            "steps_over_90": {k: len(v) for k, v in over.items()},
            "alone_steps_over_90": {k: len(v) for k, v in over_al.items()},
            "remaining_violation_mw": remaining,
            "zero_violations": all(not v for v in over.values()),
            "gates": zs.get("gates"), "unhandled_non_convergence": zs.get("unhandled_non_convergence"),
            "zone_settle_usd_by_agent": zs.get("zone_settle_usd_by_agent"),
            "lambda_max_by_round": lam_path, "lambda_never_above_cap": bound_ok, "cap_usd_mwh": cap,
            "net_harm": {k: nh.get(k) for k in ("agents_harmed", "total_net_harm_usd")},
            "value_vs_alone_usd": {a: float(w - x) for a, w, x in zip(s.ids, v_with, v_alone)},
            "_value_with": {a: float(w) for a, w in zip(s.ids, v_with)}, "_alone": sf}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--out", default="runs/m2c/synth")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="m2c_synth_"))
    res = {"header": run_header("bench/m2c_synth.py", {"rounds": a.rounds, **BASE_CFG}), "cases": {}}
    s_mid = S.run_slice(days=2, prices="midday")
    s_chp = S.run_slice(days=2, prices="cheap_peak")
    part = np.arange(len(s_mid.ids))
    des = _case("designed", s_mid, part, tmp, a.rounds)
    res["cases"]["designed"] = des
    res["cases"]["stress"] = _case("stress", s_chp, part, tmp, a.rounds)
    res["cases"]["undersized"] = _case("undersized", s_mid, part, tmp, a.rounds, undersized=True)
    for name, spec in ATTACKS.items():
        c = _case(f"attack_{name}", s_mid, part, tmp, a.rounds, attackers=[{"agent": "B4", **spec}],
                  alone=des["_alone"])
        c["attacker_gain_vs_twin_usd"] = c["_value_with"]["B4"] - des["_value_with"]["B4"]
        res["cases"][f"attack_{name}"] = c
    for c in res["cases"].values():
        c.pop("_alone", None)
        c.pop("_value_with", None)
    (out / "report.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    lines = ["| case | AC max % (tA, tB, l14) | alone | steps > 90% (tA, tB, l14) | alone | gates: rounds, converged/limit "
             "| net harm (n, $) | lambda <= cap | B4 gain vs twin $ |", "|---|---|---|---|---|---|---|---|---|"]
    for n, c in res["cases"].items():
        g = c["gates"] or []
        gs = ", ".join(f"{x['rounds']}{'C' if x['converged'] else ('L' if x['round_limit'] else '?')}" for x in g)
        f = lambda d: ", ".join(f"{d[k]:.2f}" if isinstance(d[k], float) else str(d[k]) for k in S.MONITORED)
        lines.append(f"| {n} | {f(c['ac_max_pct'])} | {f(c['alone_ac_max_pct'])} | {f(c['steps_over_90'])} | "
                     f"{f(c['alone_steps_over_90'])} | {gs} | {c['net_harm']['agents_harmed']}, "
                     f"{c['net_harm']['total_net_harm_usd']} | {c['lambda_never_above_cap']} | "
                     f"{c.get('attacker_gain_vs_twin_usd', '-')} |")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
