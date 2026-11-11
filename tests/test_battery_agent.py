"""Battery agent and its planner (agents/battery.py, agents/planner.py)."""
import numpy as np
import pytest

from agents.battery import BatteryAgent
from agents.planner import Battery, solve
from exchange import messages as M

DT = 0.25
B = Battery(P=100.0, CAP=400.0, ETA=0.95, SD=0.0, POI=1000.0, s_term=200.0)


def flat(L, v=0.0):
    return np.full(L, v)


def evening_price(L=16):
    p = flat(L, 20.0)
    p[12:14] = 80.0
    return p


def test_plan_respects_bounds_and_terminal():
    p = solve(B, 0, 200.0, evening_price(), flat(16), flat(16), flat(16), DT, 10.0)
    assert (p.c >= -1e-9).all() and (p.d <= B.P + 1e-9).all() and ((p.c + p.d) <= B.P + 1e-6).all()
    assert (p.s >= B.s_min - 1e-6).all() and (p.s <= B.s_max + 1e-6).all()
    assert p.s[-1] >= B.s_term - 1e-6


def test_award_is_honored():
    commit = flat(16)
    commit[4:6] = 80.0                                 # cheap hours, the plan would not discharge here
    p = solve(B, 0, 200.0, evening_price(), flat(16), flat(16), commit, DT, 10.0)
    assert ((p.d - p.c)[4:6] >= 80.0 - 1e-6).all() and p.sh.max() < 1e-6


def test_max_commit_is_feasible_and_stacks():
    mask = np.zeros(16, bool)
    mask[4:6] = True
    q1 = solve(B, 0, 200.0, evening_price(), flat(16), flat(16), flat(16), DT, 10.0, q_mask=mask).q
    assert q1 == pytest.approx(100.0)
    q2 = solve(B, 0, 200.0, evening_price(), flat(16), flat(16), 60.0 * mask, DT, 10.0, q_mask=mask).q
    assert q2 == pytest.approx(40.0)
    held = solve(B, 0, 200.0, evening_price(), flat(16), flat(16), (60.0 + q2) * mask, DT, 10.0)
    assert held.sh.max() < 1e-6


def make_agent(price_peak_at=(12, 14), L=16):
    iso = [f"2023-01-02T{6 + k // 4:02d}:{15 * (k % 4):02d}:00Z" for k in range(L)]
    end = "2023-01-02T10:00:00Z"
    ag = BatteryAgent("A", B, flat(L), flat(L), flat(L), iso, end, DT, "run", "SYS", participate=True,
                      min_offer_kw=10)
    price = flat(L, 20.0)
    price[price_peak_at[0]:price_peak_at[1]] = 80.0
    return ag, iso, price


def need_msg(iso, a, b, kw, nid, gate_k=2):
    return M.encode(M.make("post_need", "exchange", ["A"], iso[0],
                           {"zone": "SYS", "window": {"start_utc": iso[a], "end_utc": iso[b]}, "kw_needed": kw,
                            "mode": "scheduled", "price_cap": 500.0, "gate_utc": iso[gate_k], "need_id": nid}, "ex:1",
                           "run"))


def test_agent_registers_and_offers_through_messages():
    ag, iso, price = make_agent()
    start = [M.decode(r) for r in ag.start(0)]
    assert [m["msg_type"] for m in start] == ["register", "subscribe"]
    assert start[0]["payload"]["mandate"]["max_kw"] == B.P
    ag.receive(need_msg(iso, 4, 6, 70.0, "n1"))
    ag.receive(need_msg(iso, 4, 6, 70.0, "n2"))
    out, _ = ag.act(0, 200.0, 1, price)
    offers = [M.decode(r) for r in out]
    assert all(M.validate(o) is None and o["to"] == "exchange" for o in offers)
    assert [(o["payload"]["need_id"], o["payload"]["kw"]) for o in offers] == [("n1", 70.0), ("n2", 30.0)]
    assert offers[0]["payload"]["floor_usd_mwh"] > 0          # discharging at 20 $/MWh costs it the 80 $/MWh hours


def test_floor_is_zero_when_commitment_is_free():
    ag, iso, price = make_agent(price_peak_at=(4, 6))
    ag.start(0)
    ag.receive(need_msg(iso, 4, 6, 50.0, "n1"))
    out, _ = ag.act(0, 200.0, 1, price)
    assert M.decode(out[0])["payload"]["floor_usd_mwh"] == pytest.approx(0.0, abs=1e-3)


def test_award_changes_dispatch():
    ag, iso, price = make_agent()
    ag.start(0)
    award = M.make("award", "exchange", ["A", "OP"], iso[2],
                   {"offer_id": "A:3", "need_id": "n1", "window": {"start_utc": iso[4], "end_utc": iso[6]},
                    "accredited_kw": 70.0, "price_usd_mwh": 5.0, "cap_kw": None}, "ex:2", "run")
    ag.receive(M.encode(award))
    _, (c, d, x) = ag.act(2, 200.0, 1, price)
    assert ((ag.plan.d - ag.plan.c)[2:4] >= 70 - 1e-6).all()     # steps 4, 5 relative to plan start 2


def test_selfish_agent_sends_nothing():
    ag, iso, price = make_agent()
    ag.participate = False
    assert ag.start(0) == []
    ag.receive(need_msg(iso, 4, 6, 70.0, "n1"))
    out, _ = ag.act(0, 200.0, 1, price)
    assert out == []


def call_msg(iso, a, b, deadline_k, cid, binding=False):
    end = iso[b] if b < len(iso) else "2023-01-02T10:00:00Z"
    return M.encode(M.make("call_nominations", "exchange", ["A"], iso[0],
                           {"zone": "SYS", "window": {"start_utc": iso[a], "end_utc": end},
                            "deadline_utc": iso[deadline_k], "binding": binding, "call_id": cid,
                            "imbalance_usd_mwh": 500.0, "band_pct": 0.05, "band_min_kw": 50.0}, "ex:9", "run"))


def test_agent_nominates_its_planned_site_net():
    ag, iso, price = make_agent()
    ag.start(0)
    ag.receive(call_msg(iso, 4, 16, 4, "c1"))
    out, _ = ag.act(0, 200.0, 1, price)
    noms = [M.decode(r) for r in out if M.decode(r)["msg_type"] == "nominate"]
    assert len(noms) == 1 and M.validate(noms[0]) is None
    sched = np.array(noms[0]["payload"]["kw"])
    p = ag.plan
    assert len(sched) == 12
    np.testing.assert_allclose(sched, (p.d - p.c - p.x)[4:16], atol=1e-3)      # site net; no PV or load here
    out, _ = ag.act(1, float(p.s[0]), 1, price)
    assert [M.decode(r)["msg_type"] for r in out] == []                        # plan unchanged: no resend


def test_agent_renominates_when_an_award_moves_its_plan():
    ag, iso, price = make_agent()
    ag.start(0)
    ag.receive(call_msg(iso, 4, 16, 6, "c1"))
    ag.act(0, 200.0, 1, price)
    first = ag.calls["c1"]["sent"].copy()
    award = M.make("award", "exchange", ["A", "OP"], iso[1],
                   {"offer_id": "A:3", "need_id": "n1", "window": {"start_utc": iso[8], "end_utc": iso[10]},
                    "accredited_kw": 100.0, "price_usd_mwh": 5.0, "cap_kw": None}, "ex:2", "run")
    ag.receive(M.encode(award))
    out, _ = ag.act(1, float(ag.plan.s[0]), 1, price)
    noms = [M.decode(r) for r in out if M.decode(r)["msg_type"] == "nominate"]
    assert len(noms) == 1 and np.abs(np.array(noms[0]["payload"]["kw"]) - first).max() > 1.0
    assert np.array(noms[0]["payload"]["kw"])[4:6] == pytest.approx([100.0, 100.0], abs=1e-3)   # steps 8, 9


def test_locked_nomination_holds_against_a_price_change():
    ag, iso, price = make_agent()
    ag.start(0)
    ag.receive(call_msg(iso, 4, 16, 2, "c1", binding=True))
    ag.act(0, 200.0, 1, price)
    sent = ag.calls["c1"]["sent"].copy()
    ag.act(1, float(ag.plan.s[0]), 1, price)
    soc = float(ag.plan.s[0])
    flipped = price.copy()
    flipped[4:8] = 200.0                         # new prices after the lock would pay to discharge early
    ag.act(2, soc, 2, flipped)                   # deadline passed: locked
    assert not np.isnan(ag.nom[4:16]).any()
    p = ag.plan
    dev = np.abs((p.d - p.c - p.x)[2:14] - sent)
    assert dev.max() <= ag.band + 1e-6          # 200 $/MWh is below the 500 $/MWh imbalance price


def test_award_is_delivered_before_terminal_energy_is_protected():
    """PVB_48 in M1/M1.1 at 250 MW: energy was in the battery at the window start, but the planner paid the
    shortfall to protect the end-of-slice energy target. Here delivering the award leaves too little time to
    fully recharge (losses), so one of the two soft targets must give: it must be terminal energy, not the award."""
    b = Battery(P=100.0, CAP=400.0, ETA=0.95, SD=0.0, POI=1000.0, s_term=200.0)
    commit = np.array([100.0, 100.0, 0.0, 0.0])                    # award at the window start
    p = solve(b, 0, 200.0, np.full(4, 30.0), flat(4), flat(4), commit, DT, 10.0)
    assert ((p.d - p.c)[:2] >= 100.0 - 1e-6).all() and p.sh.max() < 1e-6
    assert p.s[-1] < b.s_term                                     # the terminal target is what gave way
