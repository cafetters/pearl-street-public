"""M2b step 3c: run plumbing for data-center deferral (run_variant(dc_spec=...)). The data-center agent's metered load
is load - generated + served (its own schedule); the acting-alone run defers the same way; the exchange's 1p reference
(acting alone) includes the agent's own deferral; evaluate() uses the effective load. No flexible workload: the run is
identical to one without dc_spec. Synthetic slice only (no data needed)."""
import json

import numpy as np
import pytest

from sim import dc_defer as DC
from sim.run_m1 import evaluate, run_variant
from tests.test_replay import CFG_M11, synthetic_slice

CFG = {**CFG_M11, "rounds": "info", "price_mode": True, "plan_to_nomination": True, "release_superseded_lock": True,
       "price_rounds_per_gate": 6, "settlement_reference": "selfish_1p"}


def spec(s, g_a):
    T = len(s.ts_utc)
    return {"generated": {"A": np.asarray(g_a, float)},
            "sessions": {"A": [DC.Session(start=0, deadline=T, energy_kwh=float(np.sum(g_a) * 0.25), cap_kw=200.0)]}}


def _evening(s, kw):
    hour = np.asarray(s.ts_utc.tz_convert("America/Chicago").hour)
    return np.where((hour >= 17) & (hour < 21), kw, 0.0)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dc")
    s = synthetic_slice()
    s.agent["load"][0] = s.agent["load"][0] + _evening(s, 30.0)        # A's recorded load includes its workload
    part = np.arange(2)
    sp_ = spec(s, _evening(s, 30.0))
    plain = run_variant(CFG, s, part, tmp / "plain", headroom_mw=0.03)
    zero = run_variant(CFG, s, part, tmp / "zero", headroom_mw=0.03, dc_spec=spec(s, np.zeros(len(s.ts_utc))))
    sf = run_variant(CFG, s, part, tmp / "dc_alone", dc_spec=sp_)
    r = run_variant(CFG, s, part, tmp / "dc", headroom_mw=0.03, dc_spec=sp_)
    return s, part, sp_, plain, zero, sf, r


def test_no_flexible_work_is_identical_to_no_dc_spec(runs):
    s, part, sp_, plain, zero, sf, r = runs
    strip = lambda log: [json.dumps({k: v for k, v in m.items() if k != "run_id"}, sort_keys=True) for m in log]
    assert strip(plain["log"]) == strip(zero["log"])


def test_metered_load_follows_the_agents_schedule_and_obeys_the_rules(runs):
    s, part, sp_, plain, zero, sf, r = runs
    served = r["dc_served"]["A"]
    g = sp_["generated"]["A"]
    assert DC.check(served, g, sp_["sessions"]["A"], 0.25) == []
    assert np.allclose(r["load_eff"][0], s.agent["load"][0] - g + served)
    assert np.allclose(r["load_eff"][1], s.agent["load"][1])
    assert DC.check(sf["dc_served"]["A"], g, sp_["sessions"]["A"], 0.25) == []      # acting alone defers too


def _ref(run, asset="A/bess"):
    return [m["payload"] for m in run["log"] if m["msg_type"] == "selfish_reference" and m["payload"]["asset_id"] == asset]


def test_flat_day_ahead_prices_keep_the_recorded_timing_in_the_reference(runs):
    """Acting alone on flat evening DA prices, the data center keeps its recorded timing (ties go early), so the 1p
    reference equals the one without deferral (the step-3c test assumed otherwise; Nov 19)."""
    s, part, sp_, plain, zero, sf, r = runs
    assert np.allclose(sf["dc_served"]["A"], sp_["generated"]["A"])
    assert _ref(r) and _ref(r)[0]["net_kw"] == _ref(plain)[0]["net_kw"]


def _priced_slice():
    """The synthetic slice with an expensive DA evening (17-21 local), so acting alone defers work out of it."""
    s = synthetic_slice()
    hour = np.asarray(s.ts_utc.tz_convert("America/Chicago").hour)
    for k in ("price_da", "price_rt"):
        s.system[k] = np.where((hour >= 17) & (hour < 21), 80.0, s.system[k]).astype(float)
    s.agent["load"][0] = s.agent["load"][0] + _evening(s, 30.0)
    return s


def _tight(s):
    """A declaration tighter than the session allows: everything served by 21:00 local (the true deadline is the
    window's end)."""
    hour = np.asarray(s.ts_utc.tz_convert("America/Chicago").hour)
    d = int(np.flatnonzero(hour >= 21)[0])
    return [DC.Session(start=0, deadline=d, energy_kwh=30.0, cap_kw=200.0)]


@pytest.fixture(scope="module")
def priced(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dcp")
    s = _priced_slice()
    part = np.arange(2)
    sp_ = spec(s, _evening(s, 30.0))
    plain = run_variant(CFG, s, part, tmp / "plain", headroom_mw=0.03)
    sf = run_variant(CFG, s, part, tmp / "alone", dc_spec=sp_)
    r = run_variant(CFG, s, part, tmp / "dc", headroom_mw=0.03, dc_spec=sp_)
    att = run_variant(CFG, s, part, tmp / "att", headroom_mw=0.03, dc_spec={**sp_, "declared": {"A": _tight(s)}})
    return s, part, sp_, plain, sf, r, att


def test_the_1p_reference_includes_the_agents_own_deferral(priced):
    s, part, sp_, plain, sf, r, att = priced
    assert not np.allclose(sf["dc_served"]["A"], sp_["generated"]["A"])        # acting alone defers here
    assert _ref(r) and _ref(plain) and _ref(r)[0]["net_kw"] != _ref(plain)[0]["net_kw"]


def test_acting_alone_and_the_1p_reference_use_the_same_deferral_model(priced):
    """D100 (a): the reference's deferral at every gate is the schedule the acting-alone run executes (one day,
    every DA price known from the start, so the forecasts are the same)."""
    s, part, sp_, plain, sf, r, att = priced
    gates = r["dc_ref_served"]["A"]
    assert gates
    for g in gates:
        assert np.allclose(g["served_kw"], sf["dc_served"]["A"], atol=1e-6)


def test_the_reference_reads_the_simulators_sessions_not_the_agents_declaration(priced):
    """D100 (b): a data center declaring a tighter deadline changes its own schedule, never the 1p reference; the
    meter uses the simulator's generated workload, and the true rules hold."""
    s, part, sp_, plain, sf, r, att = priced
    a, h = att["dc_ref_served"]["A"], r["dc_ref_served"]["A"]
    assert len(a) == len(h) and all(np.allclose(x["served_kw"], y["served_kw"]) for x, y in zip(a, h))
    assert _ref(att)[0]["net_kw"] == _ref(r)[0]["net_kw"]
    assert not np.allclose(att["dc_served"]["A"], r["dc_served"]["A"])          # it followed its declaration
    g = sp_["generated"]["A"]
    assert np.allclose(att["load_eff"][0], s.agent["load"][0] - g + att["dc_served"]["A"])
    assert DC.check(att["dc_served"]["A"], g, sp_["sessions"]["A"], 0.25) == []


def test_dc_block_checks_every_data_center_in_the_roster(priced):
    """Pass item (a): zero deferral-rule violations per data center, unrounded, roster from the header; a data
    center in the roster with no check fails."""
    from sim.run_m1 import dc_block
    s, part, sp_, plain, sf, r, att = priced
    b = dc_block(r, sp_, r["header"]["roster"], 0.25)
    assert b["violations_total"] == 0 and b["missing_from_check"] == [] and b["pass"]
    assert set(b["agents"]) == {"A"}
    assert abs(b["agents"]["A"]["served_kwh"] - b["agents"]["A"]["generated_kwh"]) <= 1e-6
    sp2 = {**sp_, "generated": {**sp_["generated"], "B": np.zeros(len(s.ts_utc))},
           "sessions": {**sp_["sessions"], "B": []}}
    b2 = dc_block(r, sp2, r["header"]["roster"], 0.25)
    assert b2["missing_from_check"] == ["B"] and not b2["pass"]


def test_evaluate_uses_the_effective_load(runs):
    s, part, sp_, plain, zero, sf, r = runs
    rec = s.system["system_load"] * 1000.0
    ev = evaluate(CFG, s, part, r, rec, None, sf)
    a = s.agent
    net = rec + (a["pv"][part] - a["curt"][part] + a["bess"][part]).sum(0) \
        - (a["pv"][part] - a["curt"][part] - r["X"] + r["D"] - r["C"]).sum(0) + (r["load_eff"] - a["load"][part]).sum(0)
    assert np.allclose(ev["_net"], net)


def test_harm_components_sum_with_deferral(tmp_path_factory):
    """Found on Maritime (Nov 19, m2b_jan12_on): with deferral, the harm components missed the data center's
    workload timing (its load bought at other steps), so their sum disagreed with its net harm and the run stopped.
    A workload_timing component closes it: the parts add up to each agent's total. Flat DA (acting alone keeps the
    recorded timing) and RT rising every step (the exchange's move changes the energy bill)."""
    from sim.run_m1 import harm_components, net_harm
    s = synthetic_slice()
    hour = np.asarray(s.ts_utc.tz_convert("America/Chicago").hour)
    s.system["price_rt"] = s.system["price_rt"] + 0.5 * np.arange(len(hour))       # every move changes the bill
    s.agent["load"][0] = s.agent["load"][0] + _evening(s, 30.0)
    part = np.arange(2)
    sp_ = spec(s, _evening(s, 30.0))
    cfg = {**CFG, "harm_components": True}
    tmp = tmp_path_factory.mktemp("dch")
    sf2 = run_variant(cfg, s, part, tmp / "alone", dc_spec=sp_)
    r2 = run_variant(cfg, s, part, tmp / "dc", headroom_mw=0.03, dc_spec=sp_)
    assert not np.allclose(r2["dc_served"]["A"], sf2["dc_served"]["A"])
    rec = s.system["system_load"] * 1000.0
    se, ev = evaluate(cfg, s, part, sf2, rec, None, sf2), evaluate(cfg, s, part, r2, rec, None, sf2)
    p_end = float(s.system["price_da"].mean()) / 1000.0
    alone = se["_agent_value"] + sf2["SOC"][:, -1] * p_end
    with_ps = ev["_agent_value"] + r2["SOC"][:, -1] * p_end
    ids = [ag.id for ag in r2["agents"]]
    nh = net_harm(alone, with_ps, ids, r2["header"]["roster"], 0)
    rows = {x["agent"]: x for x in ev["_components"]}
    assert rows["A"]["total"] == pytest.approx(alone[0] - with_ps[0], abs=1e-6)
    out = harm_components(ev["_components"], nh, alone)
    assert out["all_agents"]["workload_timing"] != 0.0
