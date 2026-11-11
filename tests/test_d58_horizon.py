"""D58 horizon no-net-harm test (bench.d58_horizon): the summed tolerance, the per-window settlement gate, roster
and cap-100 equivalence checks. Synthetic data only."""
import numpy as np
import pytest

from bench import d58_horizon as H
from bench.d56_resettle import CheckFailed

ROSTER = ["COM_01", "PVB_01"]
RS = H.RESETTLED


def _cell(run, gain, alone=(-5000.0, -5000.0), gap=(0.0, 0.0), settled=None, cap=500.0):
    """A cell where agent j's result vs. acting alone is gain[j]: e = 0, 1p net = gain."""
    agents = [{"agent": a, "alone": alone[j], "e": 0.0, "max_kw": 100.0,
               "components": {"lost_arbitrage_net_of_credits": 0.0, "da_rt_gap": gap[j], "extra_curtailment": 0.0,
                              "imbalance_charges": 0.0, "end_energy": 0.0}} for j, a in enumerate(ROSTER)]
    return {"run": run, "header": {"roster": list(ROSTER)}, "agents": agents, "settled_rule": settled,
            "price_cap_usd_mwh": cap, "rules": {"1p": {"net": list(gain), "zeroed": [False, False]}},
            "mw_cut": 10.0, "relief_mwh": 1.0, "priced_steps": 4}


def _e_from_gap(cell):
    # a gap cost is part of e (the pre-settlement shortfall); keep result = net - e equal to the given gain
    for a, n in zip(cell["agents"], cell["rules"]["1p"]["net"]):
        a["e"] = a["components"]["da_rt_gap"]
    cell["rules"]["1p"]["net"] = [n + a["e"] for a, n in zip(cell["agents"], cell["rules"]["1p"]["net"])]
    return cell


OK_PRICE = {r: 20.0 for r in RS}


def test_one_ten_dollar_floor_over_the_horizon_not_per_window():
    # small acting-alone: 1% of |300| = 3, so tolerance $10 per window and $10 over the horizon
    cells = [_cell(RS[i], (-6.0, 0.0), alone=(-100.0, -100.0)) for i in range(3)]
    r = H.horizon_check(cells, OK_PRICE)
    assert not r["agents"][0]["pass"] and r["agents"][0]["tol_usd"] == pytest.approx(10.0)
    assert r["agents_failing"] == 1 and not r["pass"]


def test_tolerance_uses_absolute_summed_acting_alone():
    fail = [_cell(RS[i], (-60.0, 0.0)) for i in range(2)]          # |-10,000| x 1% = 100; -120 fails
    ok = [_cell(RS[i], (-40.0, 0.0)) for i in range(2)]            # -80 passes
    assert not H.horizon_check(fail, OK_PRICE)["agents"][0]["pass"]
    r = H.horizon_check(ok, OK_PRICE)
    assert r["agents"][0]["pass"] and r["agents"][0]["tol_usd"] == pytest.approx(100.0) and r["pass"]


def test_big_gain_cannot_hide_a_settlement_harm_window():
    good = _cell(RS[0], (10_000.0, 10_000.0))
    bad = _cell(RS[1], (-500.0, 0.0))                               # no gap, so all of it is settlement-caused
    r = H.horizon_check([good, bad], OK_PRICE)
    assert r["agents_failing"] == 0
    assert not r["windows"][RS[1]]["settlement_gate"] and not r["pass"]


def test_gap_only_harm_evens_out_over_the_horizon():
    lose = _e_from_gap(_cell(RS[0], (-500.0, 0.0), gap=(500.0, 0.0)))
    win = _e_from_gap(_cell(RS[1], (480.0, 0.0), gap=(-480.0, 0.0)))
    r = H.horizon_check([lose, win], OK_PRICE)
    assert r["windows"][RS[0]]["harmed"] == 1 and r["windows"][RS[0]]["settlement_gate"]
    assert r["pass"]
    a = r["agents"][0]
    assert a["worst_window"] == RS[0] and a["worst_window_usd"] == pytest.approx(-500.0)
    assert (a["gap_cost_windows"], a["gap_gain_windows"]) == (1, 1)
    assert r["gap_sign"] == {"windows_cost": 1, "windows_gain": 1, "mean_window_total_usd": pytest.approx(10.0),
                             "sum_usd": pytest.approx(20.0)}
    assert r["by_type"]["COM"]["agents"] == 1 and r["by_type"]["PVB"]["failing"] == 0


def test_missing_or_mismatched_roster_raises():
    c = _cell(RS[0], (0.0, 0.0))
    c["agents"] = c["agents"][:1]
    with pytest.raises(CheckFailed):
        H.horizon_check([c], OK_PRICE)
    d = _cell(RS[1], (0.0, 0.0))
    d["header"]["roster"] = ["COM_01", "PVB_02"]
    with pytest.raises(CheckFailed):
        H.horizon_check([_cell(RS[0], (0.0, 0.0)), d], OK_PRICE)


def test_cap_100_equivalence_fails_closed():
    with pytest.raises(CheckFailed):
        H.horizon_check([_cell(RS[0], (0.0, 0.0))], {RS[0]: 100.5})
    with pytest.raises(CheckFailed):
        H.horizon_check([_cell(RS[0], (0.0, 0.0))], {})
    assert H.horizon_check([_cell(RS[0], (0.0, 0.0))], {RS[0]: 100.0})["pass"]
    inrun = H.IN_RUN[0]
    with pytest.raises(CheckFailed):
        H.horizon_check([_cell(inrun, (0.0, 0.0), settled="1", cap=100.0)], {})
    with pytest.raises(CheckFailed):
        H.horizon_check([_cell(inrun, (0.0, 0.0), settled="1p", cap=500.0)], {})
    assert H.horizon_check([_cell(inrun, (0.0, 0.0), settled="1p", cap=100.0)], {})["pass"]


def test_cells_are_one_per_window_at_100_mw():
    assert len(H.CELLS) == 11 and len(set(H.CELLS)) == 11
    assert all("_h100" in r for r in H.CELLS) and "h2h_c_jan16_h100" not in H.CELLS
    windows = [r.split("_")[2] for r in H.CELLS]
    assert len(set(windows)) == 11
    assert H.SENSITIVITY_DROP in H.CELLS and np.isclose(H.CAP_USD_MWH, 100.0)


def test_site_rows_gap_per_mwh_and_timing_cosine():
    from bench.d58_sites import site_rows
    rt, da = np.array([50.0, 10.0]), np.array([30.0, 10.0])     # RT beats DA by $20 on step 0
    dn = np.array([[-1000.0, 1000.0],                           # charges 1 MW more when RT is high: costs
                   [-2000.0, 2000.0],                           # same timing, twice the size
                   [1000.0, -1000.0]])                          # the opposite timing: gains
    r = site_rows(dn, rt, da, ["A", "B", "C"])
    a, b, c = (r["agents"][k] for k in "ABC")
    assert a["gap_usd"] == pytest.approx(20.0 * 0.25) and b["gap_usd"] == pytest.approx(2 * a["gap_usd"])
    assert a["shifted_mwh"] == pytest.approx(0.5) and a["gap_per_mwh"] == pytest.approx(b["gap_per_mwh"])
    assert a["cos_with_fleet"] == pytest.approx(1.0) and c["cos_with_fleet"] == pytest.approx(-1.0)
    assert c["gap_usd"] == pytest.approx(-a["gap_usd"])
    assert r["fleet_gap_per_mwh"] == pytest.approx((5.0 + 10.0 - 5.0) / 2.0)
