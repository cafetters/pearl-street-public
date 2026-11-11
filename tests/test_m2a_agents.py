"""M2a agent, operator and summary rules: firm offers from the gate, declared prep, the daily budget, the risk
premium, planning under the rebound cap, zone targets, and net harm by component."""
import json

import numpy as np
import pandas as pd
import pytest

from agents.battery import BatteryAgent
from agents.operator import GridOperator
from agents.planner import Battery, solve
from exchange import messages as M
from sim.run_m1 import COMPONENTS, harm_components, net_harm, run_variant
from tests.test_replay import CFG_M11, synthetic_slice

DT = 0.25
L = 16
ISO = [f"2023-01-02T{6 + k // 4:02d}:{15 * (k % 4):02d}:00Z" for k in range(L)]
END = "2023-01-02T10:00:00Z"


def agent(soc_cap=400.0, **kw):
    b = Battery(P=100.0, CAP=soc_cap, ETA=0.95, SD=0.0, POI=1000.0, s_term=40.0)
    z = np.zeros(L)
    return BatteryAgent("A", b, z, z, z, ISO, END, DT, "run", "SYS", participate=True, min_offer_kw=10,
                        local_dates=["2023-01-02"] * L, prep_hours=2.0, **kw)


def need(a, b, kw, gate_k, nid="n1"):
    return M.encode(M.make("post_need", "exchange", ["A"], ISO[0],
                           {"zone": "SYS", "window": {"start_utc": ISO[a], "end_utc": ISO[b] if b < L else END},
                            "kw_needed": kw, "mode": "scheduled", "price_cap": 500.0, "gate_utc": ISO[gate_k],
                            "need_id": nid}, "ex:1", "run"))


def flat_price(v=20.0):
    return np.full(L, v)


def offer_of(out):
    return [M.decode(r)["payload"] for r in out if M.decode(r)["msg_type"] == "offer"][0]


def test_firm_offer_is_sized_from_the_energy_reachable_after_the_gate():
    """PVB_48: at bid time the battery is nearly empty and its plan doesn't charge before the gate. An offer sized
    from bid time assumes that pre-gate charging; a firm offer counts only what it can reach after the gate."""
    price = flat_price(20.0)
    price[:4] = 200.0                     # charging before the gate is dear, so the plan won't
    for firm in (False, True):
        ag = agent(firm_offers=firm)
        ag.start(0)
        ag.receive(need(6, 8, 100.0, gate_k=5))
        out, _ = ag.act(0, 40.0, 1, price)          # SOC at the floor (10% of 400)
        q = offer_of(out)["kw"]
        if firm:
            q_firm = q
        else:
            q_bid = q
    assert q_firm < q_bid
    b = agent().b
    commit = np.zeros(L)
    commit[6:8] = q_firm
    p = solve(b, 5, 40.0, price[5:], np.zeros(L - 5), np.zeros(L - 5), commit[5:], DT, 10.0)
    assert p.sh.max() < 1e-6                          # deliverable from the gate


def test_prep_is_the_load_added_outside_the_window_and_zero_inside():
    ag = agent(firm_offers=True, send_prep=True)
    ag.start(0)
    ag.latest_sent[:] = 0.0                       # nominated idle: any charging is load added beyond the nomination
    ag.receive(need(10, 12, 100.0, gate_k=4))
    out, _ = ag.act(0, 40.0, 1, flat_price())
    p = offer_of(out)
    a = ISO.index(p["prep"]["start_utc"])
    prep = np.zeros(L)
    prep[a:a + len(p["prep"]["kw"])] = p["prep"]["kw"]
    assert a >= 4 and (prep >= 0).all() and prep[10:12].sum() == 0
    # enough charging to deliver: stored energy for 2 steps of 100 kW is 2 x 100 x 0.25 / 0.95 kWh
    assert prep.sum() * DT * 0.95 >= 2 * 100 * DT / 0.95 - 0.05          # prep is rounded to 0.1 kW per step


def test_daily_budget_caps_the_offer():
    ag = agent(daily_budget={"2023-01-02": 30.0})          # 30 kWh: 60 kW over a 2-step (0.5 h) window
    ag.start(0)
    ag.receive(need(6, 8, 100.0, gate_k=2))
    out, _ = ag.act(0, 200.0, 1, flat_price())
    assert offer_of(out)["kw"] == pytest.approx(60.0)
    start = [M.decode(r) for r in agent(daily_budget={"2023-01-02": 30.0}).start(0)]
    assert start[0]["payload"]["mandate"]["daily_kwh"] == {"2023-01-02": 30.0}


def test_premium_is_added_to_the_floor():
    floors = []
    for prem in (0.0, 4.25):
        ag = agent(premium=prem)
        ag.start(0)
        ag.receive(need(6, 8, 50.0, gate_k=2))
        out, _ = ag.act(0, 200.0, 1, flat_price())
        floors.append(offer_of(out)["floor_usd_mwh"])
    assert floors[1] - floors[0] == pytest.approx(4.25, abs=1e-3)


def test_planner_keeps_load_under_the_rebound_cap_before_terminal_energy():
    """After an award the cap holds load at the pre-award nomination outside the window; recharging above it is
    priced above the terminal penalty, so the plan stays under the cap and ends lower instead."""
    b = Battery(P=100.0, CAP=400.0, ETA=0.95, SD=0.0, POI=1000.0, s_term=200.0)
    commit = np.zeros(L)
    commit[4:6] = 100.0
    cap = np.zeros(L)
    cap[4:6] = np.nan                             # nominated idle everywhere outside the window: no recharge allowed
    p = solve(b, 0, 200.0, flat_price(), np.zeros(L), np.zeros(L), commit, DT, 10.0, load_cap=cap, cap_band=0.0)
    load = p.c - p.d
    assert (load[:4] <= 1e-6).all() and (load[6:] <= 1e-6).all() and p.s[-1] < b.s_term


def test_agent_builds_its_cap_like_the_exchange():
    ag = agent(no_rebound=True)
    ag.start(0)
    ag.latest_sent[:] = -10.0                      # nominated 10 kW of load everywhere
    award = {"offer_id": "A:3", "need_id": "n1", "window": {"start_utc": ISO[6], "end_utc": ISO[8]},
             "accredited_kw": 50.0, "price_usd_mwh": 5.0, "cap_kw": None,
             "prep": {"start_utc": ISO[3], "kw": [20.0, 20.0]}}
    ag._add_to_cap(ISO[2], award)
    cap = ag.load_cap
    assert np.isnan(cap[:2]).all()                 # before the award
    assert cap[3] == pytest.approx(30.0) and cap[4] == pytest.approx(30.0) and cap[5] == pytest.approx(10.0)
    assert np.isnan(cap[6:8]).all()                # inside the window
    assert cap[8] == pytest.approx(10.0) and np.isnan(cap[8 + 8:]).all()      # until window end + 2 h (8 steps)


def two_day_op(prep):
    ts = pd.date_range("2023-01-02 06:00", periods=192, freq="15min", tz="UTC")
    iso = list(ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
    op = GridOperator("OP", iso, "2023-01-04T06:00:00Z", ts.tz_convert("America/Chicago"), "run", "SYS",
                      headroom_kw=50, layer_kw=25, price_cap=500, post_hour=9, gate_lead_steps=4, rounds="info",
                      nominations=True, nom_lead_steps=4, idle_net=np.full(192, 5.0), prep_clearing=prep)
    return op, iso


def test_zone_target_covers_the_full_horizon_in_the_zone_frame():
    op, iso = two_day_op(prep=True)
    fc = np.full(192, 1000.0)
    fc[60], fc[150] = 1100.0, 1200.0
    out = [M.decode(r) for r in op.act(0, fc)]
    zt = [m for m in out if m["msg_type"] == "zone_target"]
    assert len(zt) == 1 and M.validate(zt[0]) is None
    p = zt[0]["payload"]
    assert p["window"] == {"start_utc": iso[4], "end_utc": "2023-01-04T06:00:00Z"}     # first gate to horizon end
    tk = np.array(p["target_kw"])
    # system target minus the base (idle forecast + participants' idle net): day 1 1100-50, day 2 1200-50
    assert tk[0] == pytest.approx(1050 - (1000 + 5)) and tk[150 - 4] == pytest.approx(1150 - (1200 + 5))
    calls = [m for m in out if m["msg_type"] == "call_nominations"]
    assert calls[0]["payload"]["window"]["end_utc"] == "2023-01-04T06:00:00Z"          # full-horizon calls
    off, _ = two_day_op(prep=False)
    assert not [m for m in map(M.decode, off.act(0, fc)) if m["msg_type"] == "zone_target"]


def test_harm_components_add_up_to_each_total():
    rows = []
    for aid, vals in (("A", (10, 5, 0, 30, 0, 0, 0, 2)), ("B", (-20, 1, 0, 0, 0, 0, 0, 0))):
        r = dict(zip(COMPONENTS, map(float, vals)))
        r.update(agent=aid, total=float(sum(vals)), end_kwh=0.0)
        rows.append(r)
    alone = np.array([1000.0, 1000.0])
    nh = net_harm(alone, alone - np.array([47.0, -19.0]), ["A", "B"], ["A", "B"], 0)
    c = harm_components(rows, nh, alone)
    assert c["harmed_agents"]["total"] == pytest.approx(47.0)
    assert sum(c["harmed_agents"][k] for k in COMPONENTS) == pytest.approx(c["harmed_agents"]["total"])
    with pytest.raises(AssertionError):           # a component table that doesn't match the harm is an error
        harm_components(rows, net_harm(alone, alone - np.array([40.0, -19.0]), ["A", "B"], ["A", "B"], 0), alone)


def test_m2a_synthetic_run_replays_and_records_every_new_message(tmp_path):
    from exchange.trace.replay import replay
    cfg = {**CFG_M11, "rounds": "info", "nomination_span": "horizon", "firm_offers": True, "prep_clearing": True, "no_rebound": True,
           "daily_energy": True, "risk_premium_usd_mwh": 2.0, "rebound_hours": 24}
    r = run_variant(cfg, synthetic_slice(), np.arange(2), tmp_path, headroom_mw=0.1)
    path = tmp_path / "exchange_h0.1_rinfo" / "trace.jsonl"
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    hdr = lines[0]
    assert hdr["exchange"]["prep_clearing"] and hdr["m2a"]["risk_premium_usd_mwh"] == 2.0
    types = {e["msg_type"] for e in lines[1:]}
    assert {"asset_limits", "zone_target", "clearing_report", "rebound"} <= types
    assert not [e for e in lines[1:] if e["msg_type"] == "reject" and e["payload"]["code"] == "over_demonstrated_limit"]
    assert replay(path)["match"]


def test_daily_budget_is_clipped_at_the_demonstrated_limit_and_reports_the_cut():
    from sim.run_m1 import daily_budgets
    budgets, cut = daily_budgets(usable=100.0, pv_by_date={"2023-01-02": 40.0, "2023-01-03": 200.0}, demo_pv=50.0,
                                 factor=0.5)
    assert budgets == {"2023-01-02": 120.0, "2023-01-03": 150.0}       # 0.5 x 200 = 100 clipped to demonstrated 50
    assert cut == {"2023-01-03": 50.0}



def test_prep_is_measured_against_the_nomination_and_the_award_is_deliverable_under_the_cap():
    """Bug 2 (M2a run 1): prep was measured against a freshly solved plan, while the exchange caps against the latest
    nomination. Now prep is the load beyond the nomination, so after the award the cap (nomination + prep) leaves
    room to deliver, even with the grid-first penalty order."""
    ag = agent(firm_offers=True, send_prep=True, no_rebound=True, rebound_priority="grid")
    ag.start(0)
    nominated = np.zeros(L)
    nominated[4:6] = -50.0                        # it already nominated 50 kW of charging at steps 4-5
    ag.latest_sent[:] = nominated
    ag.receive(need(10, 12, 100.0, gate_k=4))
    out, _ = ag.act(0, 40.0, 1, flat_price())
    p = offer_of(out)
    a = ISO.index(p["prep"]["start_utc"])
    prep = np.zeros(L)
    prep[a:a + len(p["prep"]["kw"])] = p["prep"]["kw"]
    # declared prep is only the charging beyond the nominated 50 kW at steps 4-5 (and anything elsewhere)
    held_extra_energy = prep.sum() * DT
    needed = 2 * p["kw"] * DT / 0.95 / 0.95 - 2 * 50 * DT       # AC kWh to charge for delivery, minus nominated
    assert held_extra_energy >= needed - 0.1
    award = {"offer_id": "A:3", "need_id": "n1", "window": {"start_utc": ISO[10], "end_utc": ISO[12]},
             "accredited_kw": p["kw"], "price_usd_mwh": 5.0, "cap_kw": None, "prep": p["prep"]}
    ag.receive(M.encode(M.make("award", "exchange", ["A", "OP"], ISO[4], award, "ex:2", "run")))
    ag.act(4, 40.0, 1, flat_price())
    assert ag.plan.sh.max() < 1e-3                 # deliverable within nomination + awarded prep
    load = ag.plan.c - ag.plan.d
    cap = ag.load_cap[4:]
    ok = ~np.isnan(cap)
    assert (load[ok] <= cap[ok] + ag.band + 1e-3).all()


def test_rebound_priority_orders_cap_against_delivery():
    from agents.planner import CAP_PRIORITY_USD_MWH, SHORTFALL_USD_MWH, TERMINAL_USD_KWH
    assert CAP_PRIORITY_USD_MWH["grid"] > SHORTFALL_USD_MWH > CAP_PRIORITY_USD_MWH["delivery"] > TERMINAL_USD_KWH * 1000
    b = Battery(P=100.0, CAP=400.0, ETA=0.95, SD=0.0, POI=1000.0, s_term=40.0)
    commit = np.zeros(L)
    commit[8:10] = 100.0
    cap = np.zeros(L)
    cap[8:10] = np.nan                            # no charging allowed anywhere outside the window
    for prio, expect_short in (("grid", True), ("delivery", False)):
        p = solve(b, 0, 40.0, flat_price(), np.zeros(L), np.zeros(L), commit, DT, 10.0, load_cap=cap, cap_band=0.0,
                  cap_price=CAP_PRIORITY_USD_MWH[prio])
        assert (p.sh.sum() > 1.0) == expect_short


def test_run_stops_if_a_roster_agent_is_not_registered(tmp_path, monkeypatch):
    import sim.run_m1 as R
    real = R.daily_budgets
    monkeypatch.setattr(R, "daily_budgets", lambda *a, **k: ({d: v + 1.0 for d, v in real(*a, **k)[0].items()}, {}))
    cfg = {**CFG_M11, "rounds": "info", "daily_energy": True}
    with pytest.raises(RuntimeError, match="not registered"):
        run_variant(cfg, synthetic_slice(), np.arange(2), tmp_path, headroom_mw=0.1)


def test_first_nomination_goes_out_before_the_bid_so_prep_is_measured_against_it():
    """M2a run 2: on day 1 the agent bid before its first nomination, so prep had no reference and came out 0; the
    exchange then froze a nomination without the charging, and the award failed under the grid-first order."""
    ag = agent(firm_offers=True, send_prep=True, no_rebound=True)
    ag.start(0)
    call = M.encode(M.make("call_nominations", "exchange", ["A"], ISO[0],
                           {"zone": "SYS", "window": {"start_utc": ISO[0], "end_utc": END}, "deadline_utc": ISO[4],
                            "binding": False, "call_id": "c1", "imbalance_usd_mwh": 500.0, "band_pct": 0.05,
                            "band_min_kw": 50.0}, "ex:9", "run"))
    ag.receive(call)
    ag.receive(need(10, 12, 100.0, gate_k=4))
    out, _ = ag.act(0, 40.0, 1, flat_price())
    kinds = [M.decode(r)["msg_type"] for r in out]
    assert kinds.index("nominate") < kinds.index("offer")
    p = offer_of(out)
    assert "prep" in p and sum(p["prep"]["kw"]) > 0


def test_referee_walk_charges_within_the_cap_and_discharges_in_windows():
    from sim.run_m1 import referee_walk
    z = np.zeros(L)
    commit = np.zeros(L)
    commit[8:10] = 100.0                                         # 2 steps x 100 kW: 52.6 kWh from storage
    capped = np.zeros(L)                                         # allowed net 0: no charging beyond the band
    ok = np.full(L, np.nan)                                      # uncapped
    args = dict(P=100.0, s_min=40.0, s_max=360.0, eta=0.95, sd=0.0, dt=0.25, base_export=z, commit=commit, end=10)
    assert referee_walk(50.0, 0, allowed_net=capped, band=0.0, **args)          # 10 kWh usable: short
    assert referee_walk(50.0, 0, allowed_net=ok, band=0.0, **args) == {}        # may charge freely before the window
    assert referee_walk(50.0, 0, allowed_net=capped, band=50.0, **args) == {}   # the band alone allows enough
    prep_cap = capped.copy()
    prep_cap[2:4] = -100.0                                       # awarded prep: 100 kW of extra load at steps 2-3
    assert referee_walk(50.0, 0, allowed_net=prep_cap, band=0.0, **args) == {}


def test_referee_stops_the_run_on_bug_3(tmp_path, monkeypatch):
    """Bug 3 (M2a run 2): the agent bid before its first nomination, so prep had no reference and was zero; the
    award then needed charging the frozen nomination didn't contain. The referee stops at that first award."""
    import agents.battery as B
    monkeypatch.setattr(B.BatteryAgent, "_allowed_net", lambda self: np.full(self.T, np.nan))   # no reference
    sl = synthetic_slice()
    sl.agent["soc"][:] = 12.0                       # nearly empty: any award needs charging
    cfg = {**CFG_M11, "rounds": "info", "nomination_span": "horizon", "firm_offers": True, "prep_clearing": True, "no_rebound": True,
           "imbalance_band_pct": 0.0, "imbalance_band_min_kw": 0.0}   # no free band, so the cap is exact
    with pytest.raises(RuntimeError, match="award not deliverable"):
        run_variant(cfg, sl, np.arange(2), tmp_path, headroom_mw=0.1)
    monkeypatch.undo()
    run_variant(cfg, sl, np.arange(2), tmp_path / "fixed", headroom_mw=0.1)      # with the fix: no abort


def test_gap_share_is_the_part_of_the_selfish_to_ceiling_gap_closed():
    from sim.run_m1 import gap_share
    m0 = {"ceiling_15min": {"peak_min_mw": 2400.0}}
    assert gap_share(2600.0, 2500.0, m0) == pytest.approx(0.5)
    assert gap_share(2600.0, 2620.0, m0) == pytest.approx(-0.1)       # above selfish: negative
    assert gap_share(2600.0, 2500.0, None) is None
