"""M2b item 4 (owner, Nov 19): in the exchange, a data center plans its deferral jointly with its battery against the
exchange's peak price (`dc_joint: true`), so served workload is part of its site net: nominated, priced, settled under
1p, under the same lock, imbalance, POI, prox and margin as battery relief. Acting alone and the 1p reference keep the
selfish deferral (dc_defer.schedule). Red test: with separate deferral, both data centers herd into the first cheap
step after an expensive evening and raise the participants' peak far above the deferral-off twin."""
import numpy as np
import pytest

from agents.planner import Battery, solve
from sim import dc_defer as DC
from sim.run_m1 import dc_spec_from_config, run_variant
from tests.test_replay import CFG_M11, synthetic_slice

CFG = {**CFG_M11, "rounds": "info", "price_mode": True, "plan_to_nomination": True, "release_superseded_lock": True,
       "price_rounds_per_gate": 6, "settlement_reference": "selfish_1p"}
TOL_KW = 1.0
RUN = {**CFG, "nomination_span": "horizon"}       # as every M2b config
B = Battery(P=100.0, CAP=400.0, ETA=0.95, SD=0.0, POI=1000.0, s_term=200.0)


def _lp(L=16, gen_at=(4, 5), kw=40.0, cap=200.0, room=None):
    g = np.zeros(L)
    g[list(gen_at)] = kw
    ss = [DC.Session(start=0, deadline=L, energy_kwh=float(g.sum() * 0.25), cap_kw=cap)]
    price = np.full(L, 20.0)
    return g, ss, price


def _args(L, price, base_export):
    return (B, 0, 200.0, price, np.zeros(L), base_export, np.zeros(L), 0.25, 10.0)


# ---- planner ----

def test_no_workload_gives_the_battery_only_plan():
    L = 16
    g, ss, price = _lp(L, gen_at=(), kw=0.0)
    price[8:12] = 60.0
    bx = np.full(L, -50.0)
    p0 = solve(*_args(L, price, bx))
    p1 = solve(*_args(L, price, bx), dc=DC.rows_from(g, ss, 0.25, 0, np.zeros(0)))
    assert np.allclose(p0.c, p1.c) and np.allclose(p0.d, p1.d) and np.allclose(p0.x, p1.x)
    assert p0.u is None and np.allclose(p1.u, 0.0)


def test_the_joint_plan_obeys_the_deferral_rules_with_history_and_holds():
    L = 16
    g, ss, price = _lp(L)
    price[6:9] = 5.0
    hold = np.full(L, np.nan)
    hold[10] = 0.0
    fixed = np.zeros(3)
    k0 = 3
    p = solve(B, k0, 200.0, price[k0:], np.zeros(L - k0), (np.full(L, -50.0) + g)[k0:], np.zeros(L - k0), 0.25, 10.0,
              dc=DC.rows_from(g, ss, 0.25, k0, fixed, hold=hold))
    served = np.concatenate([fixed, p.u])
    assert DC.check(served, g, ss, 0.25) == []
    assert served[10] == pytest.approx(0.0, abs=1e-9)
    assert served[6:9].sum() == pytest.approx(g.sum(), abs=1e-6)          # all of it in the cheap steps


def test_a_peak_price_moves_served_work_off_its_step():
    L = 16
    g, ss, price = _lp(L)
    bx = np.full(L, -50.0) + g
    rows = DC.rows_from(g, ss, 0.25, 0, np.zeros(0))
    p = solve(*_args(L, price, bx), dc=rows)
    t = int(np.argmax(p.u))
    peak = np.zeros(L)
    peak[t] = 200.0
    q = solve(*_args(L, price, bx), dc=rows, peak=peak)
    assert q.u[t] < p.u[t] - 1.0
    assert DC.check(q.u, g, ss, 0.25) == []


def test_prox_damps_the_move():
    """With a prox centred on the last plan's site net, a price change moves less served work than without it."""
    L = 16
    g, ss, price = _lp(L, cap=80.0)
    bx = np.full(L, -50.0) + g
    rows = DC.rows_from(g, ss, 0.25, 0, np.zeros(0))
    p = solve(*_args(L, price, bx), dc=rows)
    net_load = p.c - p.d + p.x + p.u                     # what the prox centre measures (bx excludes the workload)
    peak = np.zeros(L)
    peak[np.argmax(p.u)] = 30.0
    free = solve(*_args(L, price, bx), dc=rows, peak=peak)
    damped = solve(*_args(L, price, bx), dc=rows, peak=peak, prox=(5.0, net_load, 0.0))
    moved = lambda q: float(np.abs(q.u - p.u).sum())
    assert moved(damped) < moved(free)


def test_the_joint_planner_does_not_size_offers():
    L = 16
    g, ss, price = _lp(L)
    with pytest.raises(ValueError):
        solve(*_args(L, price, np.zeros(L)), q_mask=np.ones(L, bool), dc=DC.rows_from(g, ss, 0.25, 0, np.zeros(0)))


# ---- runs ----

def _evening(s, kw):
    hour = np.asarray(s.ts_utc.tz_convert("America/Chicago").hour)
    return np.where((hour >= 17) & (hour < 21), kw, 0.0)


def _herd_slice():
    """Both agents are data centers with 30 kW of evening workload; the DA evening (17-21 local) costs 80 $/MWh, so
    acting alone each serves it at its 200 kW ceiling in the first cheap step after 21:00. The background load is flat,
    so the participants' load sets the system peak and the exchange's target binds (as data centers do on real data;
    with the synthetic slice's 300 MW evening background, no step is ever priced)."""
    s = synthetic_slice()
    hour = np.asarray(s.ts_utc.tz_convert("America/Chicago").hour)
    for k in ("price_da", "price_rt"):
        s.system[k] = np.where((hour >= 17) & (hour < 21), 80.0, s.system[k]).astype(float)
    for i in range(2):
        s.agent["load"][i] = s.agent["load"][i] + _evening(s, 30.0)
    s.system["system_load"] = s.system["net_disp"] = 1000.0 + s.agent["load"].sum(0) / 1000.0     # MW, flat background
    return s


def _spec(s):
    T = len(s.ts_utc)
    g = _evening(s, 30.0)
    return {"generated": {a: g.copy() for a in ("A", "B")},
            "sessions": {a: [DC.Session(start=0, deadline=T, energy_kwh=float(g.sum() * 0.25), cap_kw=200.0)]
                         for a in ("A", "B")}}


def _zone_peak(r, load):
    return float((load + r["C"] - r["D"] + r["X"]).sum(0).max())


@pytest.fixture(scope="module")
def herd(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dcj")
    s = _herd_slice()
    part = np.arange(2)
    sp_ = _spec(s)
    off = run_variant(RUN, s, part, tmp / "off", headroom_mw=0.03)
    sep = run_variant(RUN, s, part, tmp / "sep", headroom_mw=0.03, dc_spec=sp_)
    joint = run_variant({**RUN, "dc_joint": True}, s, part, tmp / "joint", headroom_mw=0.03, dc_spec=sp_)
    alone = run_variant(RUN, s, part, tmp / "alone", dc_spec=sp_)
    alone_j = run_variant({**RUN, "dc_joint": True}, s, part, tmp / "alone_j", dc_spec=sp_)
    return s, sp_, off, sep, joint, alone, alone_j


def test_separate_deferral_herds_above_the_off_twin(herd):
    """The finding the change answers (round 1): kept as a regression of the separate planner."""
    s, sp_, off, sep, joint, alone, alone_j = herd
    assert _zone_peak(sep, sep["load_eff"]) > _zone_peak(off, s.agent["load"]) + 100.0


def test_joint_deferral_does_not_raise_the_peak_above_the_off_twin(herd):
    s, sp_, off, sep, joint, alone, alone_j = herd
    assert _zone_peak(joint, joint["load_eff"]) <= _zone_peak(off, s.agent["load"]) + TOL_KW


def test_joint_deferral_obeys_the_rules_and_meters_its_schedule(herd):
    s, sp_, off, sep, joint, alone, alone_j = herd
    for a, i in (("A", 0), ("B", 1)):
        assert DC.check(joint["dc_served"][a], sp_["generated"][a], sp_["sessions"][a], 0.25) == []
        assert np.allclose(joint["load_eff"][i], s.agent["load"][i] - sp_["generated"][a] + joint["dc_served"][a])


def test_joint_deferral_is_nominated(herd):
    """The binding nomination includes served workload: metered site net stays within the free band of it."""
    s, sp_, off, sep, joint, alone, alone_j = herd
    imb = [m for m in joint["log"] if m["msg_type"] == "imbalance"]
    assert imb                                                    # binding calls were settled
    assert sum(float(m["payload"]["charge_usd"]) for m in imb) == pytest.approx(0.0, abs=1e-6)


def test_acting_alone_and_the_1p_reference_keep_selfish_deferral(herd):
    s, sp_, off, sep, joint, alone, alone_j = herd
    for a in ("A", "B"):
        assert np.array_equal(alone["dc_served"][a], alone_j["dc_served"][a])
    ref = lambda r: [(m["payload"]["asset_id"], m["payload"]["net_kw"]) for m in r["log"]
                     if m["msg_type"] == "selfish_reference"]
    assert ref(joint) and ref(joint) == ref(sep)


def test_dc_joint_needs_deferral_and_price_mode(tmp_path):
    with pytest.raises(ValueError):
        dc_spec_from_config({"dc_joint": True}, None, None)
    s = _herd_slice()
    with pytest.raises(ValueError):
        run_variant({**CFG, "price_mode": False, "dc_joint": True}, s, np.arange(2), tmp_path, headroom_mw=0.03,
                    dc_spec=_spec(s))
