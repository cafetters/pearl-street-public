"""Proven capacity (owner, Nov 19; Leaning): the owner's tests, run on the synthetic scenarios
(redteam/proven_scenarios.py) [sim, synthetic]. Parameters here are test points, not the chosen point (the
pre-registered experiment sets them)."""
import json
import math

import numpy as np
import pytest

from redteam.proven_scenarios import run

PC = {"step_kw": 125.0, "grow": 1.0, "cut": 0.5, "n0": 3, "new_prior": [1.0, 1.0], "existing_prior": [99.0, 1.0],
      "starter_fallback_kw": 100.0, "budget_kw": 100.0, "reserve_kw": 2000.0}


def test_existing_fleet_with_proven_capacity_matches_flag_off(tmp_path):
    """Owner change 1, with the owner's Nov 20 decision (option a): assets with meter history count at rate 1 until
    their first miss, so with no newcomers and no miss the market matches flag-off exactly (prices to 1e-9 $/MWh,
    settlements to 1e-6 $). The 80-agent real-data check and the M2a $5 and D61 v2 checks are the gate."""
    from bench import firmness as F
    from redteam.synth import BASE_CFG, HEADROOM_MW, synthetic_slice
    from sim.run_m1 import run_variant
    s = synthetic_slice(fleet=(("L", 100.0, 400.0), ("H1", 1000.0, 4000.0), ("H2", 500.0, 2000.0)))
    out = {}
    for name, pc in (("off", None), ("on", PC)):
        cfg = {**BASE_CFG, "run_name": f"fleet_{name}", "silent_reference": True, **({"proven_capacity": pc} if pc else {})}
        run_variant(cfg, s, np.arange(3), tmp_path / name, headroom_mw=HEADROOM_MW)
        log = F.read_trace(next((tmp_path / name).glob("exchange_*/trace.jsonl")))
        out[name] = (F.final_prices(log), F.peak_settle(log))
    (p0, s0), (p1, s1) = out["off"], out["on"]
    assert p0.keys() == p1.keys() and all(abs(p1[c] - p0[c]) <= 1e-9 for c in p0), (p0, p1)
    assert s0.keys() == s1.keys() and all(abs(s1[a] - s0[a]) <= 1e-6 for a in s0), (s0, s1)


def test_the_starter_type_comes_from_the_metering_never_the_register():
    """Owner change 2: an agent declaring a bigger type in its register gets no bigger starter."""
    from tests.test_m2a_exchange import H
    starters = {}
    for declared in ("bess", "data_center"):
        h = H(price_mode=True, silent_reference=True, settlement_reference="selfish_1p", proven_capacity=PC)
        for aid, kw in (("E1", 1000.0), ("E2", 500.0)):           # existing bess with history: median 750
            h.send("asset_limits", "meter", {"asset_id": f"{aid}/bess", "usable_kwh": 4 * kw, "power_kw": kw,
                                             "pv_kwh_per_day": 0.0, "owner": f"{aid} LLC", "agent_id": aid,
                                             "asset_type": "bess"})
        h.send("asset_limits", "meter", {"asset_id": "N/bess", "usable_kwh": 8000.0, "power_kw": 2000.0,
                                         "pv_kwh_per_day": 0.0, "owner": "N LLC", "agent_id": "N",
                                         "asset_type": "bess", "history_days": 0, "nameplate_kw": 2000.0})
        out = h.send("register", "N", {"agent_id": "N", "legal_entity": "N LLC",
                                       "assets": [{"asset_id": "N/bess", "zone": "SYS", "type": declared}],
                                       "mandate": {"max_kw": 2000.0, "price_floor": 0.0, "workload_limits": None}})[1]
        assert out == []
        starters[declared] = h.ex.proven_state["N"].proven_kw
    assert starters == {"bess": 750.0, "data_center": 750.0}


def test_an_honest_new_asset_grows_to_its_real_capacity_within_n_calls(tmp_path):
    """A 1,000 kW honest newcomer starts at the bess median 750 kW and grows by up to grow x step = 125 kW per clean
    call, so N = ceil(250 / 125) = 2 calls with a nomination."""
    r = run("honest_flood", PC, tmp_path, days=4, k=1, honest_kw=1000.0)
    n = math.ceil((1000.0 - 750.0) / (PC["grow"] * PC["step_kw"]))
    first = [e for e in r["events"] if e["agent_id"] == "N1"][0]
    assert first["proven_kw_before"] == 750.0
    assert r["honest_calls_to_real_kw"]["N1"] is not None and r["honest_calls_to_real_kw"]["N1"] <= n, r["events"]


def test_an_over_claimer_converges_to_its_real_capacity_and_gains_nothing_against_its_honest_twin(tmp_path):
    kw = {"giant_real_kw": 100.0, "giant_paper_kw": 500.0}
    atk = run("giant_liar", PC, tmp_path / "atk", days=4, **kw)
    twin = run("giant_liar", PC, tmp_path / "twin", days=4, honest_twin=True, **kw)
    assert atk["final"]["G"]["proven_kw"] <= 100.0 + 1e-6 and atk["final"]["G"]["rate"] < 0.5
    assert atk["settlement_usd"]["G"] <= twin["settlement_usd"]["G"] + 0.01


def test_a_sybil_split_under_the_starter_cap_stays_within_the_portfolio_budget(tmp_path):
    r = run("sybil", PC, tmp_path, days=3, m=10, sybil_real_kw=20.0, sybil_paper_kw=100.0)
    assert r["max_expected_shortfall_unproven_kw"] <= PC["budget_kw"] + 1e-6
    assert r["max_expected_shortfall_unproven_kw"] > 0.0          # the scenario exercised the budget


def test_uncovered_shortfall_never_exceeds_the_contingency_reserve_with_honest_newcomers(tmp_path):
    r = run("honest_flood", PC, tmp_path, days=3, k=4, honest_kw=300.0)
    assert r["peak_uncovered_mw"] * 1000.0 <= PC["reserve_kw"]
    assert r["firm_when_called"] is not None


def test_counted_zone_totals_add_up_exactly_like_flag_off():
    """Nov 20, the 80-agent check: with nothing discounted, the proven path's zone load and binding aggregate must be
    the flag-off sums bit for bit. Python 3.12's sum() adds floats with compensation, the flag-off loops with plain +=,
    so their last bits differ and the price loop amplifies that (call 8 moved 0.34%). Values chosen so the two
    summations disagree."""
    from types import SimpleNamespace
    from exchange.core import Exchange
    vals = [0.1] * 10 + [1e16, 1.0, -1e16]
    assert sum(vals) != _plain_sum(vals)            # the case this test needs
    ex = Exchange.__new__(Exchange)
    ex.agents = {f"A{i}": {"zones": {"z": "Z"}} for i in range(len(vals))}
    ex.inactive, ex.pending_prep = set(), {}
    by = {f"A{i}": -v for i, v in enumerate(vals)}           # site net; zone load is -net
    ex._nom_or_ref = lambda aid, call_id, t: by[aid]
    ex._counted_values = lambda zone, t, call_id: {a: {"counted": v} for a, v in by.items()}
    ex.proven_cfg = None
    off = ex._zone_load("Z", "t0", "C:1")
    ex.proven_cfg = SimpleNamespace()
    on = ex._zone_load("Z", "t0", "C:1")
    assert on == off, (on, off)


def _plain_sum(xs):
    t = 0.0
    for x in xs:
        t += x
    return t
