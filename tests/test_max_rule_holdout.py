"""bench/max_rule_holdout.py (D76 holdout check, docs/plans/max_rule_holdout_check.md): check (c) on a synthetic
max-rule run's trace, the reconciliation for (e), and the verdict with a forced pass and a forced failure of each gate.
The loader on real data (bench.d56_resettle.load_cell with max_rule=True) is exercised by the smoke run."""
import json

import numpy as np
import pytest

from bench import max_rule_holdout as MH
from bench.d56_resettle import CheckFailed


@pytest.fixture(scope="module")
def traces(tmp_path_factory):
    from sim.run_m1 import run_variant
    from tests.test_replay import CFG_M11, synthetic_slice
    base = {**CFG_M11, "rounds": "info", "price_mode": True, "plan_to_nomination": True,
            "release_superseded_lock": True, "price_rounds_per_gate": 10, "settlement_reference": "selfish_1p"}
    s = synthetic_slice()
    out = {}
    for rule in ("max_first_call", "today"):
        d = tmp_path_factory.mktemp(rule)
        run_variant({**base, "settlement_rule": rule}, s, np.arange(2), d, headroom_mw=0.03)
        out[rule] = d / "exchange_h0.03_rinfo" / "trace.jsonl"
    return out


def test_check_c_passes_on_a_max_rule_trace_and_reads_the_roster(traces):
    res = MH.offline_vs_inrun(traces["max_first_call"])
    assert res["pass"] and res["max_err_usd"] <= MH.TOL_C_USD
    assert res["binding_calls"] and len(res["per_agent"]) == len(res["roster"]) == 2
    totals = MH.peak_settle_totals(traces["max_first_call"])
    assert res["payout_usd"] == pytest.approx(sum(totals.values()))


def test_check_c_refuses_a_today_trace(traces):
    with pytest.raises(CheckFailed, match="not a max_first_call run"):
        MH.offline_vs_inrun(traces["today"])


def _tamper(src, dst, fn):
    lines = src.read_text(encoding="utf-8").splitlines()
    out = [lines[0]]
    for raw in lines[1:]:
        m = json.loads(raw)
        out.append(json.dumps(fn(m)) if fn(m) is not None else None)
    dst.write_text("\n".join(x for x in out if x is not None) + "\n", encoding="utf-8")
    return dst


def test_check_c_fails_on_a_wrong_payment_and_on_a_missing_agent(traces, tmp_path):
    def bump(m):
        if m["msg_type"] == "peak_settle":
            m = {**m, "payload": {**m["payload"], "net_usd": m["payload"]["net_usd"] + 0.01}}
        return m
    res = MH.offline_vs_inrun(_tamper(traces["max_first_call"], tmp_path / "bump.jsonl", bump))
    assert not res["pass"] and res["max_err_usd"] == pytest.approx(0.01, abs=1e-6)
    first = json.loads(traces["max_first_call"].read_text().splitlines()[0])["roster"][0]

    def drop(m):
        return None if (m["msg_type"] == "peak_settle" and m["to"] == first) else m
    with pytest.raises(CheckFailed, match="peak_settle"):
        MH.offline_vs_inrun(_tamper(traces["max_first_call"], tmp_path / "drop.jsonl", drop))


# ---------------------------------------------------------------- the verdict, on fabricated cells
ROSTER = ["PVB_01", "PVB_02"]


def _cell(run, nets, e, gap=(0.0, 0.0), alone=(1000.0, 1000.0), max_rule=True):
    comps = lambda g: {"lost_arbitrage_net_of_credits": 0.0, "da_rt_gap": g, "extra_curtailment": 0.0,
                       "imbalance_charges": 0.0, "end_energy": 0.0}
    c = {"run": run, "header": {"roster": list(ROSTER)}, "feasible": True, "mw_cut": 1.0, "relief_mwh": 1.0,
         "priced_steps": 4,
         "agents": [{"agent": a, "e": e[j], "alone": alone[j], "components": comps(gap[j]), "max_kw": 100.0}
                    for j, a in enumerate(ROSTER)],
         "rules": {"1p": {"net": [0.0, 0.0], "zeroed": [False, False]}}}
    if max_rule:
        c = MH.max_cell({**c, "settled_rule": "max_first_call", "max_net": list(nets)})
    else:
        c["rules"]["1p"]["net"] = list(nets)
    return c


def _inputs(a_nets=((5.0, 5.0), (5.0, 5.0)), a_e=((5.0, 5.0), (5.0, 5.0)), gaps=((0.0, 0.0), (0.0, 0.0)),
            b_pay=(20.0, 20.0), c_err=0.0, recon_off=0.0):
    a = [_cell(f"A{i}", a_nets[i], a_e[i], gaps[i]) for i in range(2)]
    b = [_cell(f"B{i}", (10.0, 10.0), (10.0, 10.0), max_rule=False) for i in range(2)]
    c = {f"A{i}": {"roster": list(ROSTER), "max_err_usd": c_err} for i in range(2)}
    tot = {f"A{i}": {a_: v + (recon_off if i == 0 and j == 0 else 0.0) for j, (a_, v) in enumerate(zip(ROSTER, a_nets[i]))}
           for i in range(2)}
    return a, b, c, tot, {f"B{i}": b_pay[i] for i in range(2)}, {f"A{i}": 50.0 for i in range(2)}


def test_verdict_passes_when_every_gate_holds():
    v = MH.verdict(*_inputs())
    assert v["pass"] and all(v["gates"].values())
    assert v["payout_usd"] == {"half_a": 20.0, "half_b": 40.0}


def test_gate_a_fails_on_settlement_caused_harm():
    v = MH.verdict(*_inputs(a_e=((50.0, 5.0), (5.0, 5.0))))           # $45 short, no gap: beyond the $10 tolerance
    assert not v["pass"] and not v["gates"]["a_settlement_harm_zero"]


def test_gate_b_fails_when_half_a_pays_no_less():
    v = MH.verdict(*_inputs(b_pay=(10.0, 10.0)))
    assert not v["pass"] and not v["gates"]["b_payout_below_half_b"]


def test_gate_c_tolerance_is_a_tenth_of_a_cent_per_agent():
    # owner, Nov 4: the trace's prices are rounded to 6 decimals, so price rounding alone gives errors of a few $1e-6
    assert MH.TOL_C_USD == 1e-3
    v = MH.verdict(*_inputs(c_err=4.3e-6))                  # the jan12 smoke run's largest rounding error
    assert v["pass"] and v["gates"]["c_in_run_equals_offline"]
    v = MH.verdict(*_inputs(c_err=1.1e-3))
    assert not v["pass"] and not v["gates"]["c_in_run_equals_offline"]


def test_gate_d_fails_closed_on_a_missing_agent():
    a, b, c, tot, bp, mp = _inputs()
    b[1] = {**b[1], "agents": b[1]["agents"][:1]}
    with pytest.raises(CheckFailed):
        MH.verdict(a, b, c, tot, bp, mp)


def test_gate_e_fails_on_horizon_harm_that_is_all_gap():
    # $45 short in both windows, all of it DA vs RT gap: no settlement-caused harm, but the horizon check fails
    v = MH.verdict(*_inputs(a_e=((50.0, 5.0), (50.0, 5.0)), gaps=((45.0, 0.0), (45.0, 0.0))))
    assert v["gates"]["a_settlement_harm_zero"] and not v["gates"]["e_horizon"] and not v["pass"]


def test_reconciliation_stops_the_check_beyond_1_cent():
    MH.verdict(*_inputs(recon_off=0.009))
    with pytest.raises(CheckFailed, match="reconciliation"):
        MH.verdict(*_inputs(recon_off=0.011))


def test_max_cell_refuses_a_cell_that_is_not_max_rule():
    with pytest.raises(CheckFailed):
        MH.max_cell(_cell("X", (1.0, 1.0), (1.0, 1.0), max_rule=False))
