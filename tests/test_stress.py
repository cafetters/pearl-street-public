"""Tests for the D10 stress scenarios (sim/stress.py) and the D31 rules (bench/stress_sweep.py). Offline."""
import numpy as np
import pandas as pd
import pytest

from bench import stress_sweep as S
from sim import stress as X


def test_grow_agents_only_zone_and_once():
    pnet = np.array([[-100.0, -200.0], [-50.0, -50.0], [300.0, 400.0]])
    load = np.array([[100.0, 200.0], [50.0, 50.0], [0.0, 0.0]])    # third agent is a PVB plant
    in_zone = np.array([True, False, True])                         # union of both zones: one flag each
    out = X.grow_agents(pnet, load, in_zone, 0.2)
    assert np.allclose(out[0], [-120.0, -240.0])
    assert np.allclose(out[1], pnet[1])
    assert np.allclose(out[2], pnet[2])


def test_bg_multiplier_only_zone_buses():
    m = X.bg_multiplier(np.array([1, 2, 3, 2]), {2, 9}, 0.15)
    assert np.allclose(m, [1.0, 1.15, 1.0, 1.15])


def test_new_load_bus_skips_wrong_sign():
    ptdf = pd.Series({10: 0.6, 11: -0.4, 12: -0.2})    # flow hv->lv: load raises flow where PTDF < 0
    assert X.new_load_bus(ptdf, 1.0) == 11
    assert X.new_load_bus(ptdf, -1.0) == 10
    with pytest.raises(ValueError):
        X.new_load_bus(pd.Series({10: 0.6}), 1.0)


def test_hours_by_day_central():
    ts = pd.date_range("2023-01-02 06:00", periods=192, freq="15min", tz="UTC")   # Jan 2-3 Central
    over = pd.Series(False, index=ts)
    over.iloc[0:12] = True          # 3 h on Jan 2
    over.iloc[100:104] = True       # 1 h on Jan 3
    h = S.hours_by_day(over, ["2023-01-02", "2023-01-03", "2023-01-04"])
    assert h == {"2023-01-02": 3.0, "2023-01-03": 1.0, "2023-01-04": 0.0}


def test_meets_bar_needs_4h_across_slice_on_both_zones():
    assert S.meets_bar({"t35": 4.0, "t40": 9.25})                        # boundary: exactly 4 h passes
    assert not S.meets_bar({"t35": 23.0, "t40": 3.75})                   # one zone short fails
    assert S.BAR_HOURS == 4.0
    # all on one day is fine now (the old per-day rule would have failed this)
    by_day = {"t35": {"d1": 4.0, "d2": 0.0}, "t40": {"d1": 0.5, "d2": 3.5}}
    assert S.meets_bar({e: sum(d.values()) for e, d in by_day.items()})


def test_relief_matches_hand_calc():
    # sn 400, vm 1.0, limit 90% -> S_max 360. P 380, Q 100: relief = 380 - sqrt(360^2 - 100^2)
    r = X.relief_mw([380.0], [100.0], 400.0, [1.0], 90.0)
    assert r[0] == pytest.approx(380 - np.sqrt(360 ** 2 - 100 ** 2))
    assert X.relief_mw([200.0], [50.0], 400.0, [1.0], 90.0)[0] == 0.0          # under the limit
    assert np.isinf(X.relief_mw([10.0], [370.0], 400.0, [1.0], 90.0)[0])        # Q alone breaks it


def test_feasible_boundary():
    cap = {"t35": 100.0, "t40": 50.0}
    assert S.feasible({"t35": 70.0, "t40": 35.0}, cap)
    assert not S.feasible({"t35": 70.0, "t40": 35.01}, cap)
    assert not S.feasible({"t35": float("inf"), "t40": 0.0}, cap)


def lvl(level, bar=True, feas=True, vio=0, new=()):
    return {"level": level, "meets_bar": bar, "feasible": feas,
            "voltage": {"bus_steps_out": vio}, "new_overloads": list(new)}


def test_smallest_passing():
    assert S.smallest_passing([lvl(0, bar=False), lvl(0.1, feas=False), lvl(0.2), lvl(0.3)])["level"] == 0.2
    assert S.smallest_passing([lvl(0, bar=False), lvl(0.1, feas=False)]) is None


def test_pick_order():
    assert S.pick({"B": None, "C": None}) is None
    assert S.pick({"A": lvl(0.05), "B": None, "C": None}) is None          # A is never picked
    assert S.pick({"B": lvl(0.2), "C": None})[0] == "B"
    assert S.pick({"B": None, "C": lvl(30)})[0] == "C"
    assert S.pick({"B": lvl(0.2), "C": lvl(30)})[0] == "C"                 # tie -> C
    assert S.pick({"B": lvl(0.2), "C": lvl(30, vio=4)})[0] == "B"          # only B clean
    assert S.pick({"B": lvl(0.2), "C": lvl(30, new=["l7"])})[0] == "B"
    assert S.pick({"B": lvl(0.2, vio=1), "C": lvl(30)})[0] == "C"
    assert S.pick({"B": lvl(0.2, vio=1), "C": lvl(30, vio=1)})[0] == "C"   # neither clean -> C


def test_added_load_mw():
    load = np.array([[1000.0, 2000.0], [500.0, 500.0], [9000.0, 9000.0]])     # kW; third agent outside the zone
    add = S.added_load_mw(load, [True, True, False], np.array([10.0, 20.0]), np.array([5, 6]), {5},
                          np.array([1.0, 0.5]), 0.1, 30.0)
    assert np.allclose(add, [0.1 * (1.5 + 10.0) + 30, 0.1 * (2.5 + 5.0) + 30])


def zlvl(h35, h40, rc35, rc40):
    return {"zones": {"t35": {"hours_ge_90": h35, "relief_over_capacity": rc35},
                      "t40": {"hours_ge_90": h40, "relief_over_capacity": rc40}}}


def test_d31_gaps():
    g = S.d31_gaps(zlvl(23.0, 1.0, 1.9, 0.4))
    assert g["bar_hours_short"] == {"t40": 3.0} and g["worst_bar_gap_h"] == -3.0
    assert g["feasibility_excess"] == {"t35": pytest.approx(1.2)} and g["worst_feasibility_excess"] == pytest.approx(1.2)
    ok = S.d31_gaps(zlvl(4.0, 4.0, 0.7, 0.1))
    assert ok["bar_hours_short"] == {} and ok["feasibility_excess"] == {}


def test_pick_2d():
    c = lambda g, mw, peak, bar=True, feas=True: {"t35_growth": g, "t40_new_mw": mw, "added_mw_peak": peak,
                                                   "meets_bar": bar, "feasible": feas}
    assert S.pick_2d([c(0.05, 40, 90.0), c(0.0, 70, 70.0), c(0.1, 0, 60.0, feas=False)])["t40_new_mw"] == 70
    assert S.pick_2d([c(0.05, 20, 70.0), c(0.0, 70, 70.0)])["t35_growth"] == 0.0       # tie -> less t35 growth
    assert S.pick_2d([c(0.0, 0, 0.0, bar=False), c(0.1, 0, 60.0, feas=False)]) is None


def test_rescore_applies_current_rules():
    def lv(sweep, level, h35, h40, r35, r40):
        z = lambda h, r: {"hours_ge_90": h, "worst_relief_mw": r, "sign_correct_bess_mw": 10.0,
                          "relief_over_capacity": r / 10.0, "worst_relief_at_utc": "2023-01-02 06:00:00+00:00"}
        return {"sweep": sweep, "level": level, "zones": {"t35": z(h35, r35), "t40": z(h40, r40)},
                "voltage": {"bus_steps_out": 0}, "new_overloads": [], "meets_bar": False, "feasible": False}
    saved = {"method": {"voltage": "validated"}, "sweeps": {
        "A": [lv("A", 0.1, 5, 5, 1, 1)],                                  # passes, but A is never picked
        "B": [lv("B", 0.0, 1, 0, 1, 0), lv("B", 0.1, 4, 4, 7, 7), lv("B", 0.2, 9, 6, 8, 2)],
        "C": [lv("C", 10.0, 4, 3.75, 2, 2)]}}
    r = S.rescore(saved)
    assert r["smallest_passing"] == {"A": 0.1, "B": 0.1, "C": None} and r["pick"] == {"sweep": "B", "level": 0.1}
    b = r["sweeps"]["B"]
    assert [x["meets_bar"] for x in b] == [False, True, True] and [x["feasible"] for x in b] == [True, True, False]
    assert r["sweeps"]["C"][0]["gaps"]["bar_hours_short"] == {"t40": 0.25}


REP = {"agents": [{"id": "A", "bus": 1, "ptdf": -0.5, "relief_correct": True},
                  {"id": "B", "bus": 2, "ptdf": 0.2, "relief_correct": False},
                  {"id": "C", "bus": 3, "ptdf": -0.1, "relief_correct": True}]}


def test_zone_flex_mw_sign_correct_and_weighted():
    ts = pd.date_range("2023-01-02 06:00", periods=2, freq="15min", tz="UTC")
    flex = {"dc_flex": np.array([[1000.0, 0.0], [5000.0, 5000.0], [0.0, 0.0], [9000.0, 9000.0]]),
            "ev": np.array([[0.0, 200.0], [0.0, 0.0], [2000.0, 4000.0], [0.0, 0.0]])}
    f = S.zone_flex_mw(REP, ["A", "B", "C", "D"], flex, ts)                 # B wrong sign, D not in zone
    assert np.allclose(f["dc_flex"], [0.5, 0.0]) and np.allclose(f["ev"], [0.2, 0.1 + 0.4])


def test_new_dc_flex_mw():
    assert S.new_dc_flex_mw(REP, 1, 40.0) == pytest.approx(0.25 * 40 * 0.5)
    assert S.new_dc_flex_mw(REP, 2, 40.0) == 0.0                              # bus of a wrong-sign agent
    assert S.new_dc_flex_mw(REP, 99, 40.0) == 0.0                             # not a zone bus
    assert S.new_dc_flex_mw(REP, 1, 40.0, share=0.5) == pytest.approx(10.0)


def test_capacity_at_worst_step_and_rescore_with_flex():
    ts = pd.date_range("2023-01-02 06:00", periods=4, freq="15min", tz="UTC")
    extra = pd.Series([1.0, 2.0, 3.0, 4.0], index=ts)
    assert S.capacity_mw(10.0, extra, "2023-01-02 06:30:00+00:00") == 13.0
    assert S.capacity_mw(10.0, None, ts[2]) == 10.0
    zone = lambda h, r: {"hours_ge_90": h, "worst_relief_mw": r, "sign_correct_bess_mw": 10.0,
                         "relief_over_capacity": r / 10.0, "worst_relief_at_utc": str(ts[3])}
    lvl_ = lambda s, level, r: {"sweep": s, "level": level, "zones": {"t35": zone(5, r), "t40": zone(5, r)},
                                "voltage": {"bus_steps_out": 0}, "new_overloads": []}
    saved = {"method": {"voltage": "validated"}, "sweeps": {"B": [lvl_("B", 0.1, 9.0)], "C": [lvl_("C", 10.0, 9.0)]}}
    assert S.rescore(saved)["pick"] is None                                   # 9 > 0.7 x 10
    r = S.rescore(saved, lambda sweep, level: {e: extra for e in S.ZONES})   # capacity 14 at the worst step
    assert r["pick"] == {"sweep": "C", "level": 10.0}                         # both pass, tie -> C
    assert r["sweeps"]["B"][0]["zones"]["t35"]["capacity_mw"] == 14.0
    assert r["sweeps"]["B"][0]["zones"]["t35"]["relief_over_capacity"] == pytest.approx(9 / 14)


def test_line_d31():
    ts = pd.date_range("2023-01-02 06:00", periods=24, freq="15min", tz="UTC")
    load = pd.Series(80.0, index=ts)
    load.iloc[4:20] = 95.0                                                   # 4 h at 95%
    flow = pd.DataFrame({"p_hv_mw": 0.0, "q_hv_mvar": 0.0, "vm_hv_pu": 1.0}, index=ts)
    extra = pd.Series(0.5, index=ts)
    d = S.line_d31(load, flow, 100.0, 0.03, extra)                           # Q = 0: relief = 95 - 90 = 5 MW
    assert d["hours_ge_90"] == 4.0 and d["meets_bar"]
    assert d["worst_relief_mw"] == pytest.approx(5.0) and d["capacity_mw"] == pytest.approx(0.53)
    assert d["relief_over_capacity"] == pytest.approx(5.0 / 0.53) and not d["feasible"]
    assert S.line_d31(load, flow, 100.0, 7.0, extra)["feasible"]             # 5 <= 0.7 x 7.5


def test_acgrid_watches_a_line():
    pp = pytest.importorskip("pandapower")
    net = pp.create_empty_network()
    b0, b1 = pp.create_bus(net, 110), pp.create_bus(net, 110)
    pp.create_ext_grid(net, b0)
    pp.create_line_from_parameters(net, b0, b1, length_km=10, r_ohm_per_km=0.1, x_ohm_per_km=0.4,
                                   c_nf_per_km=0, max_i_ka=0.5)
    pp.create_load(net, b1, p_mw=5.0, q_mvar=1.0)
    g = X.ACGrid(net, [], [])
    res = g.run(np.array([1.0]), np.array([1.0]), np.zeros((0, 1)), watch=("l0",))
    f = res.trafo_hv["l0"].iloc[0]
    assert f.p_hv_mw > 5.0 and f.q_hv_mvar > 1.0 and f.vm_hv_pu == pytest.approx(1.0)   # from side = ext grid bus


def test_pick_ignores_unvalidated_voltage():
    both = {"B": lvl(0.2), "C": lvl(30, vio=4)}
    assert S.pick(both)[0] == "B"                                          # voltage separates them
    assert S.pick(both, voltage_validated=False)[0] == "C"                 # it doesn't count -> tie -> C
    assert S.pick({"B": lvl(0.2), "C": lvl(30, vio=4, new=["l7"])}, voltage_validated=False)[0] == "B"


def test_delta_check():
    e = pd.Series([60.0, 62.0, 61.0, 65.0, 64.0, 70.0, 68.0])
    assert S.delta_check(e + 5.0, e)["pass"]                                  # offset only: same deltas
    d = S.delta_check(60 + 1.2 * (e - 60), e)                                 # deltas 20% too big
    assert d["slope"] == pytest.approx(1.2) and not d["pass"]
    noisy = e + pd.Series([0.0, 3.0, -3.0, 3.0, -3.0, 3.0, -3.0])
    assert not S.delta_check(noisy, e)["pass"]                               # r too low


def test_delta_check_pooled_drops_constant_columns():
    e = pd.DataFrame({1: [1.00, 1.01, 0.99, 1.02], 2: [1.04] * 4, 3: [0.98, 0.97, 0.99, 0.96]})
    d = S.delta_check(e + 0.01, e)
    assert d["series"] == 2 and d["n"] == 6 and d["pass"]                    # bus 2 (PV setpoint) dropped


def test_anchor_scales_change():
    ts = pd.date_range("2023-01-02 06:00", periods=4, freq="15min", tz="UTC")
    ours0 = pd.DataFrame({"t35": 50.0, "l1": 10.0}, index=ts)
    ours = ours0 + pd.DataFrame({"t35": 4.3, "l1": 2.0}, index=ts)
    engie = pd.DataFrame({"t35": [60.0], "l1": [12.0]}, index=ts[:1])
    a = S.anchor(ours, ours0, engie, scale={"t35": 0.86})
    assert np.allclose(a.t35, 60.0 + 4.3 / 0.86)
    assert np.allclose(a.l1, 14.0)                                            # unscaled column


def test_calibrated_gate():
    ts = pd.date_range("2023-01-02 06:00", periods=48, freq="h", tz="UTC")     # Jan 2-3 Central, hourly
    w = np.sin(np.arange(48) / 3) * 10
    eng = pd.DataFrame({"t35": 70 + w, "t40": 60 + w[::-1]}, index=ts)
    ours = pd.DataFrame({"t35": 65 + 0.86 * w, "t40": 50 + 1.0 * w[::-1]}, index=ts)
    vm = pd.DataFrame({1: 1.0 + w / 1000, 2: 1.04}, index=ts)
    res = X.PFResult(ours[[]], ours, vm, {}, np.ones(48, bool))
    src = {"variant": "v", "zone_max_abs_err_pp": {}, "vm_max_abs_err_pu": 0.0,
           "delta_validation": {"t35": {"slope": 0.86}, "t40": {"slope": 1.0}}}
    eng_vm = 1.0 + (vm - 1.0) / 0.8                                           # our vm changes 20% too small
    g = S.calibrated_gate(res, src, eng, eng_vm, np.arange(48))
    assert g["pass"] and g["calibration_factors"] == {"t35": 0.86, "t40": 1.0}
    assert g["loading_delta_after_calibration"]["t35"]["slope"] == pytest.approx(1.0)
    assert g["out_of_sample_day2"]["t35"]["test_slope"] == pytest.approx(1.0)
    assert g["vm_calibration_factor"] == pytest.approx(0.8) and g["voltage_validated"]
    assert g["vm_out_of_sample_day2"]["test_slope"] == pytest.approx(1.0)
    noise = pd.DataFrame(np.random.default_rng(0).normal(0, 0.01, (48, 2)), index=ts, columns=[1, 2])
    bad = S.calibrated_gate(res, src, eng, 1.0 + noise, np.arange(48))
    assert bad["pass"] and not bad["voltage_validated"]                       # loading passes, voltage flagged


def test_headroom_split():
    pp = pytest.importorskip("pandapower")
    net, b1 = tiny_net(pp, gen_max=5.0)
    pp.create_gen(net, b1, p_mw=0.0, vm_pu=1.0, min_p_mw=2.0, max_p_mw=4.0)
    net.gen.loc[0, "min_p_mw"] = 1.0
    g = X.ACGrid(net, [], [], gen_split="headroom")                           # rooms 4 and 2
    assert not g._set_gens(6.0)                                               # min 3 + 3 shared 2:1
    assert np.allclose(g.net.gen.p_mw, [3.0, 3.0])
    assert g._set_gens(10.0) and np.allclose(g.net.gen.p_mw, [5.0, 4.0])     # clipped at max
    with pytest.raises(ValueError):
        X.ACGrid(net, [], [], gen_split="cost")


def test_anchor_adds_change_to_engie():
    ts = pd.date_range("2023-01-02 06:00", periods=8, freq="15min", tz="UTC")
    ours0 = pd.DataFrame({"t35": np.arange(8.0) + 50, "l1": 10.0}, index=ts)
    ours = ours0 + pd.DataFrame({"t35": 3.0, "l1": 1.0}, index=ts)
    engie = pd.DataFrame({"t35": [60.0, 70.0]}, index=ts[::4])               # hourly; Engie has no l1
    a = S.anchor(ours, ours0, engie)
    assert a.t35.iloc[0] == pytest.approx(63.0) and a.t35.iloc[4] == pytest.approx(73.0)
    assert a.t35.iloc[1] == pytest.approx(ours.t35.iloc[1] + 10.0)           # hour's offset (60 - 50) held
    assert np.allclose(a.l1, ours.l1)                                         # raw where Engie has no column
    assert np.allclose(S.anchor(ours0, ours0, engie).t35.iloc[::4], engie.t35)


def tiny_net(pp, gen_max=None):
    net = pp.create_empty_network()
    b0, b1 = pp.create_bus(net, 110), pp.create_bus(net, 20)
    pp.create_ext_grid(net, b0)
    pp.create_transformer_from_parameters(net, b0, b1, sn_mva=10, vn_hv_kv=110, vn_lv_kv=20, vk_percent=10,
                                          vkr_percent=0.5, pfe_kw=0, i0_percent=0)
    pp.create_load(net, b1, p_mw=2.0, q_mvar=0.5)
    if gen_max is not None:
        pp.create_gen(net, b1, p_mw=0.0, vm_pu=1.0, min_p_mw=0.0, max_p_mw=gen_max)
    return net, b1


def test_held_slack_and_fixed_total_dispatch():
    pp = pytest.importorskip("pandapower")
    net, b1 = tiny_net(pp, gen_max=5.0)
    g = X.ACGrid(net, ["A"], [b1])
    pnet, one = np.array([[-1000.0]]), np.array([1.0])                       # 2 + 1 = 3 MW at bus 1
    res = g.run(one, one, pnet, watch=("t0",), slack_target_mw=np.array([1.0]))
    assert res.ext_p_mw[0] == pytest.approx(1.0, abs=X.SLACK_TOL_MW)
    assert res.gen_total_mw[0] == pytest.approx(2.0, abs=0.2) and not res.gen_at_limit[0]
    big = g.run(one, one, np.array([[-6000.0]]), watch=("t0",), slack_target_mw=np.array([1.0]))
    assert big.gen_at_limit[0] and big.gen_total_mw[0] == pytest.approx(5.0)  # needs 7, clips at max
    assert big.ext_p_mw[0] > 2.9                                              # ext_grid takes the excess
    fixed = g.run(one, one, np.array([[-6000.0]]), watch=("t0",), gen_total_mw=np.array([2.0]))
    assert fixed.gen_total_mw[0] == pytest.approx(2.0) and fixed.ext_p_mw[0] > 5.9


def test_acgrid_tiny_net():
    pp = pytest.importorskip("pandapower")
    net = pp.create_empty_network()
    b0, b1 = pp.create_bus(net, 110), pp.create_bus(net, 20)
    pp.create_ext_grid(net, b0)
    pp.create_transformer_from_parameters(net, b0, b1, sn_mva=10, vn_hv_kv=110, vn_lv_kv=20, vk_percent=10,
                                          vkr_percent=0.5, pfe_kw=0, i0_percent=0)
    pp.create_load(net, b1, p_mw=2.0, q_mvar=0.5)                     # background load
    g = X.ACGrid(net, ["A"], [b1], new_load_buses=[b1])
    pnet = np.array([[-1000.0, -1000.0]])                             # agent imports 1 MW
    res = g.run(np.array([1.0, 0.5]), np.array([1.0]), pnet, {b1: 3.0}, watch=("t0",))
    assert res.converged.all()
    q_new = 3.0 * np.tan(np.arccos(X.NEW_LOAD_PF))
    lv = g.net.res_trafo.p_lv_mw.iat[0]                               # last step: bg at 0.5
    assert lv == pytest.approx(-(1.0 + 1.0 + 3.0), abs=1e-6)          # all of bus 1's load comes through t0
    assert g.net.res_trafo.q_lv_mvar.iat[0] == pytest.approx(-(0.25 + q_new), abs=1e-6)
    assert res.trafo_loading["t0"].iloc[0] > res.trafo_loading["t0"].iloc[1]
    assert (res.trafo_hv["t0"].p_hv_mw > 5.0).all()                   # losses on top of the load
