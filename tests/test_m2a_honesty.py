"""After the D42 full run: D46 (an announced later binding call releases the earlier lock for the steps it takes
over), D47 (the D34 cap is re-based at each binding deadline; added load only within zone room, pro rata) and D48
(honest agents plan to their nomination; the free band is for forecast error)."""
import json

import numpy as np
import pytest

from agents.battery import BatteryAgent
from agents.planner import Battery
from exchange import messages as M
from sim.run_m1 import run_variant
from tests.test_exchange import GATE
from tests.test_m2a_exchange import CALL_DEADLINE, H, _steps
from tests.test_replay import CFG_M11, synthetic_slice

DT = 0.25
L = 16
ISO = [f"2023-01-02T{6 + k // 4:02d}:{15 * (k % 4):02d}:00Z" for k in range(L)]
END = "2023-01-02T10:00:00Z"


# ---- D46 / D48: the agent ----
def agent(**flags):
    b = Battery(P=100.0, CAP=400.0, ETA=0.95, SD=0.0, POI=1000.0, s_term=40.0)
    z = np.zeros(L)
    return BatteryAgent("A", b, z, z, z, ISO, END, DT, "run", "SYS", participate=True, min_offer_kw=10, **flags)


def call(cid, k_deadline, binding):
    return M.encode(M.make("call_nominations", "exchange", ["A"], ISO[0],
                           {"call_id": cid, "zone": "SYS", "window": {"start_utc": ISO[k_deadline], "end_utc": END},
                            "deadline_utc": ISO[k_deadline], "binding": binding, "imbalance_usd_mwh": 500.0,
                            "band_pct": 0.05, "band_min_kw": 50.0}, f"ex:{cid}", "run"))


def noms(out, cid):
    return [M.decode(r)["payload"] for r in out if M.decode(r)["msg_type"] == "nominate"
            and M.decode(r)["payload"]["call_id"] == cid]


P1 = np.full(L, 20.0)
P1[:2], P1[10], P1[14:] = 50.0, 5.0, 100.0     # stale prices: charge at step 10, sell at 14-15 (none before step 2)
P2 = P1.copy()
P2[10], P2[6] = 60.0, 5.0              # published prices: step 10 is dear now, step 6 cheap


def stale_then_published(**flags):
    """Binding call #1 locks a schedule planned on stale prices; prices then publish. Returns the agent and the
    plan after publication, before and after binding call #2 is announced, plus #2's nomination."""
    ag = agent(**flags)
    ag.receive(call("c1", 1, True))
    ag.act(0, 40.0, 1, P1)
    ag.act(1, 40.0, 1, P1)                                        # #1 locks at its deadline
    assert ag.nom[10] == pytest.approx(-100.0)                    # locked: charging at the stale-cheap step
    ag.act(2, 40.0, 2, P2)                                        # prices publish
    before = ag.plan
    ag.receive(call("c2", 4, True))
    out, _ = ag.act(3, 40.0, 2, P2)
    return ag, before, ag.plan, noms(out, "c2")


def test_announced_binding_call_releases_the_old_lock_for_the_steps_it_takes_over():
    ag, before, after, n2 = stale_then_published(release_superseded_lock=True, plan_to_nomination=True)
    j = lambda p, k: k - p.k0
    assert before.c[j(before, 10)] == pytest.approx(100.0)        # until #2 is announced, #1 still binds
    assert after.c[j(after, 10)] == pytest.approx(0.0, abs=1e-6)  # then the agent plans on published prices
    assert after.c[j(after, 6)] > 50.0
    kw = n2[-1]["kw"]                                             # #2's window starts at step 4
    assert kw[10 - 4] >= -1e-6 and kw[6 - 4] < -50.0              # no charging left at the now-dear step


def test_without_the_release_the_stale_schedule_carries_into_the_next_binding_call():
    ag, before, after, n2 = stale_then_published(plan_to_nomination=True)
    assert n2[-1]["kw"][10 - 4] == pytest.approx(-100.0)          # the M2a run 3 / D42 full-run pattern


def test_planning_to_the_nomination_leaves_the_free_band_unused():
    ag = agent()                                                  # band treated as free
    ag.receive(call("c1", 1, True))
    ag.act(0, 40.0, 1, P1)
    ag.act(1, 40.0, 1, P1)
    ag.act(2, 40.0, 2, P2)
    p = ag.plan
    net = p.d - p.c
    dev = np.abs(net - ag.nom[p.k0:])
    assert np.nanmax(dev) == pytest.approx(50.0)                  # rides the 50 kW band edge
    ag = agent(plan_to_nomination=True)
    ag.receive(call("c1", 1, True))
    ag.act(0, 40.0, 1, P1)
    ag.act(1, 40.0, 1, P1)
    ag.act(2, 40.0, 2, P2)
    p = ag.plan
    assert np.nanmax(np.abs(p.d - p.c - ag.nom[p.k0:])) == pytest.approx(0.0, abs=1e-6)


# ---- D47: the exchange ----
HORIZON = {"start_utc": "2023-01-02T06:30:00Z", "end_utc": "2023-01-03T14:00:00Z"}
BWIN = {"start_utc": "2023-01-02T07:30:00Z", "end_utc": "2023-01-03T14:00:00Z"}
T8, T9, T10 = "2023-01-02T08:00:00Z", "2023-01-02T09:00:00Z", "2023-01-02T10:00:00Z"


def rebase_market(target_at_9=150.0, a_at_9=-100.0, b_at_9=-100.0):
    """A and B win; A's award carries 40 kW of prep at 08:00. Binding call: A and B both nominate extra load at 09:00
    (over their caps), and A nominates less load at 10:00. Zone room at 09:00 is target - 200."""
    h = H(prep_clearing=True, no_rebound=True, rebound_hours=24.0, cap_rebase=True)
    for a in ("A", "B", "C"):
        h.register(a)
    cid, _ = h.call(window=HORIZON)
    for a in ("A", "B", "C"):
        h.nominate(a, cid, 0.0, window=HORIZON)
    h.tick(CALL_DEADLINE)
    steps = _steps(HORIZON)
    h.send("zone_target", "OP", {"zone": "SYS", "window": HORIZON,
                                 "target_kw": [target_at_9 if t == T9 else 1000.0 for t in steps]})
    nid, _ = h.need(kw=200)
    h.offer_prep("A", nid, 100, 1.0, {"start_utc": T8, "kw": [40.0]})
    h.offer_prep("B", nid, 100, 1.0, None)
    h.tick(GATE)
    bind, _ = h.call(binding=True, window=BWIN, deadline="2023-01-02T07:30:00Z")
    bsteps = _steps(BWIN)
    a_net = [{T8: -40.0, T9: a_at_9, T10: 20.0}.get(t, 0.0) for t in bsteps]   # includes its awarded prep at 08:00
    b_net = [b_at_9 if t == T9 else 0.0 for t in bsteps]
    h.nominate("A", bind, a_net, window=BWIN, t="2023-01-02T07:15:00Z")
    h.nominate("B", bind, b_net, window=BWIN, t="2023-01-02T07:15:00Z")
    h.nominate("C", bind, 0.0, window=BWIN, t="2023-01-02T07:15:00Z")
    out = h.tick("2023-01-02T07:30:00Z")
    rb = {m["to"]: m["payload"] for m in out if m["msg_type"] == "cap_rebase"}
    return h, rb, bsteps


def cap_load(h, aid, t):
    cap = h.ex.caps[aid]
    return -cap["base"][t] + cap["prep"].get(t, 0.0)


def test_cap_rebase_raises_caps_within_room_pro_rata_and_never_lowers_them():
    h, rb, bsteps = rebase_market(target_at_9=150.0)                # room for 150 of the 200 kW asked
    assert set(rb) == {"A", "B"}                                     # C has no cap
    assert cap_load(h, "A", T9) == pytest.approx(75.0) and cap_load(h, "B", T9) == pytest.approx(75.0)
    assert cap_load(h, "A", T10) == pytest.approx(0.0)              # less load: the cap stays (no discharge duty)
    assert rb["A"]["base_kw"][bsteps.index(T10)] == pytest.approx(0.0)        # reported unchanged
    assert cap_load(h, "A", T8) == pytest.approx(40.0)              # prep counted once, not twice
    i9 = bsteps.index(T9)
    assert rb["A"]["nomination_kw"][i9] == pytest.approx(-75.0)     # refused load clipped from the binding schedule
    assert h.ex.baseline["A"][T9][0] == pytest.approx(-75.0)
    assert rb["A"]["base_kw"][i9] == pytest.approx(-75.0)


def test_added_load_is_kept_out_when_the_zone_has_no_room_and_fully_accepted_when_it_has():
    h, rb, _ = rebase_market(target_at_9=100.0)                      # zone at 200 vs target 100: room for 100
    assert cap_load(h, "A", T9) + cap_load(h, "B", T9) == pytest.approx(100.0)
    h, rb, _ = rebase_market(target_at_9=0.0)                        # no room at all
    assert cap_load(h, "A", T9) == pytest.approx(0.0)
    h, rb, _ = rebase_market(target_at_9=1000.0)                     # plenty of room
    assert cap_load(h, "A", T9) == pytest.approx(100.0)


def test_a_negligible_increase_is_rebased_as_nominated():
    """M2a h100 crash: added load of 1e-12 kW at a step had no pro-rata scale."""
    h, rb, _ = rebase_market(target_at_9=0.0, a_at_9=-1e-12, b_at_9=0.0)
    assert cap_load(h, "A", T9) == pytest.approx(0.0, abs=1e-9)


def test_rebound_and_imbalance_settle_against_the_rebased_cap_and_clipped_schedule():
    h, rb, _ = rebase_market(target_at_9=150.0)
    h.meter_net("A", T9, -75.0)                                      # at the re-based cap and clipped schedule
    h.meter_net("B", T9, -200.0)                                     # 125 over its 75 kW cap, 75 beyond the band
    out = h.send("tick", "clock", {"final": True}, t="2023-01-03T14:00:00Z")[1]
    reb = {m["to"]: m["payload"] for m in out if m["msg_type"] == "rebound"}
    imb = {m["to"]: m["payload"] for m in out if m["msg_type"] == "imbalance"}
    assert reb["A"]["excess_kwh"] == pytest.approx(0.0) and imb["A"]["deviation_kwh"] == pytest.approx(0.0)
    # B is 75 kW beyond the band over both its clipped schedule and its cap: charged once (D34), here as imbalance
    assert reb["B"]["excess_kwh"] + imb["B"]["deviation_kwh"] == pytest.approx(75 * 0.25)


def test_agent_mirrors_the_rebased_cap():
    b = Battery(P=100.0, CAP=400.0, ETA=0.95, SD=0.0, POI=1000.0, s_term=40.0)
    z = np.zeros(L)
    ag = BatteryAgent("A", b, z, z, z, ISO, END, DT, "run", "SYS", participate=True, no_rebound=True,
                      cap_rebase=True, prep_hours=2.0)
    ag.latest_sent[:] = 0.0
    ag._add_to_cap(ISO[1], {"window": {"start_utc": ISO[8], "end_utc": ISO[10]},
                            "prep": {"start_utc": ISO[4], "kw": [30.0]}})
    base = [None] * (L - 2)
    base[5 - 2], base[4 - 2] = -60.0, 30.0                          # 60 kW more load at step 5; prep step unchanged
    ag.receive(M.encode(M.make("cap_rebase", "exchange", "A", ISO[2],
                               {"call_id": "b", "window": {"start_utc": ISO[2], "end_utc": END},
                                "base_kw": base, "nomination_kw": [None] * (L - 2)}, "ex:9", "run")))
    ag._read_inbox()
    assert ag.load_cap[5] == pytest.approx(60.0) and ag.load_cap[4] == pytest.approx(0.0)
    assert ag.load_cap[3] == pytest.approx(0.0) and np.isnan(ag.load_cap[8])     # award window: uncapped


# ---- the whole loop ----
def test_synthetic_run_with_d46_to_d48_replays_and_the_referee_finds_nothing(tmp_path):
    from exchange.trace.replay import replay
    cfg = {**CFG_M11, "rounds": "info", "nomination_span": "horizon", "firm_offers": True, "prep_clearing": True, "no_rebound": True,
           "partial_prep": True, "sequential_clearing": True, "daily_energy": True, "risk_premium_usd_mwh": 2.0,
           "rebound_hours": 24, "referee": "collect", "release_superseded_lock": True, "cap_rebase": True,
           "plan_to_nomination": True}
    r = run_variant(cfg, synthetic_slice(), np.arange(2), tmp_path, headroom_mw=0.1)
    path = tmp_path / "exchange_h0.1_rinfo" / "trace.jsonl"
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert lines[0]["exchange"]["cap_rebase"]
    assert r["referee_failures"] == []
    assert r["band_edge"]["binding_nomination"]["agent_steps"] == 0          # no planned band use
    assert replay(path)["match"]
