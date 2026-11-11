"""Regression tests from the M2a run 3 referee abort (COM_07, GRID_OP:16). Written before the fix; they fail until it
lands. They test invariants, not a particular implementation."""
import numpy as np
import pytest

from agents.battery import BatteryAgent
from agents.planner import Battery
from exchange import messages as M
from exchange.core import Exchange
from sim.physics import Fleet
from sim.run_m1 import referee_awards

DT = 0.25
L = 24
ISO = [f"2023-01-02T{6 + k // 4:02d}:{15 * (k % 4):02d}:00Z" for k in range(L)]
END = "2023-01-02T12:00:00Z"


def fleet_one(P=100.0, CAP=1000.0, ETA=0.95):
    a = lambda v: np.array([v], float)
    return Fleet(ids=np.array(["A"]), P=a(P), CAP=a(CAP), ETA=a(ETA), SD=a(0.0), POI=a(1e6), S0=a(300.0))


def msg(msg_type, sender, to, k, payload, mid):
    return M.make(msg_type, sender, to, ISO[k], payload, mid, "run")


def test_referee_judges_deliverability_against_the_d34_cap_not_the_latest_nomination():
    """COM_07: award 1's prep (charging the exchange allowed) ran before award 2's window, and the agent re-nominated
    after award 1. The exchange caps against the nomination frozen at the first award minus all awarded prep; the
    referee used the latest nomination plus award 2's prep only, so it under-counted the energy and stopped the run."""
    f = fleet_one()
    T = L
    base = np.zeros(T)                                             # no PV or load: site net = battery net
    idle = [0.0] * T
    later = [0.0] * T
    for t in range(4, 12):
        later[t] = 100.0                                           # after award 1 it nominated discharging 4-11
    log = [msg("nominate", "A", "exchange", 0, {"call_id": "c", "zone": "SYS",
                                                "window": {"start_utc": ISO[0], "end_utc": END}, "kw": idle}, "A:1")]
    award1 = msg("award", "exchange", ["A", "OP"], 1, {
        "offer_id": "A:2", "need_id": "n1", "window": {"start_utc": ISO[20], "end_utc": ISO[21]},
        "accredited_kw": 50.0, "price_usd_mwh": 1.0, "cap_kw": None,
        "prep": {"start_utc": ISO[2], "kw": [100.0] * 8}}, "ex:1")      # 200 kWh of allowed charging, steps 2-9
    log += [award1, msg("nominate", "A", "exchange", 1, {"call_id": "c2", "zone": "SYS",
                                                         "window": {"start_utc": ISO[1], "end_utc": END},
                                                         "kw": later[1:]}, "A:3")]
    award2 = msg("award", "exchange", ["A", "OP"], 3, {
        "offer_id": "A:4", "need_id": "n2", "window": {"start_utc": ISO[14], "end_utc": ISO[18]},
        "accredited_kw": 100.0, "price_usd_mwh": 1.0, "cap_kw": None}, "ex:2")   # 100 kWh, no prep of its own
    log.append(award2)
    soc = np.array([200.0])                                        # 100 kWh above the 10% floor at the gate
    # under the D34 cap (frozen idle nomination, + award 1's charging) about 290 kWh is reachable: deliverable
    referee_awards([award2], log, 3, soc, f, {"A": 0}, base[None, :], ISO, DT)


def stack_market(rival_on=0, a_kw=None):
    """Agent A bids on two needs at the same gate (D42: sequential clearing, offers sized from known awards). A rival
    undercuts it on need `rival_on` (None: no rival). Returns the exchange, agent, its first offers and all awards."""
    ex = Exchange("run", "OP", "meter", "clock", ["SYS"], require_nominations=True, scarcity_usd_mwh=500.0,
                  prep_clearing=True, no_rebound=True, sequential_clearing=True)
    b = Battery(P=100.0, CAP=1000.0, ETA=0.95, SD=0.0, POI=1e6, s_term=100.0)
    z = np.zeros(L)
    ag = BatteryAgent("A", b, z, z, z, ISO, END, DT, "run", "SYS", participate=True, min_offer_kw=10,
                      firm_offers=True, send_prep=True, no_rebound=True, prep_hours=2.0, independent_offers=True)
    send = lambda m: [M.decode(r) for r in ex.handle(M.encode(m))]
    for aid in ("A", "R"):
        send(msg("asset_limits", "meter", "exchange", 0, {"asset_id": f"{aid}/bess", "usable_kwh": 1000.0,
                                                          "power_kw": 100.0, "pv_kwh_per_day": 0.0, "owner": aid,
                                                          "agent_id": aid},
                 f"meter:{aid}"))
        send(msg("register", aid, "exchange", 0, {"agent_id": aid, "legal_entity": aid,
                                                  "assets": [{"asset_id": f"{aid}/bess", "zone": "SYS", "type": "bess"}],
                                                  "mandate": {"max_kw": 100.0, "price_floor": 0.0}}, f"{aid}:r"))
        send(msg("subscribe", aid, "exchange", 0, {"signals": [], "zones": ["SYS"]}, f"{aid}:s"))
    out = send(msg("call_nominations", "OP", "exchange", 0, {"zone": "SYS", "window": {"start_utc": ISO[1], "end_utc": END},
                                                            "deadline_utc": ISO[1], "binding": False}, "OP:c"))
    for m in out:
        ag.receive(M.encode(m))
    send(msg("zone_target", "OP", "exchange", 0, {"zone": "SYS", "window": {"start_utc": ISO[1], "end_utc": END},
                                                  "target_kw": [1e6] * (L - 1)}, "OP:t"))
    needs, wins = [], [(8, 10), (16, 18)]
    for i, (a, b_) in enumerate(wins):
        out = send(msg("post_need", "OP", "exchange", 0, {"zone": "SYS", "window": {"start_utc": ISO[a], "end_utc": ISO[b_]},
                                                          "kw_needed": 100.0, "mode": "scheduled", "price_cap": 500.0,
                                                          "gate_utc": ISO[1], "priority": i + 1}, f"OP:n{i}"))
        needs.append(out[0]["payload"]["need_id"])
        ag.receive(M.encode(out[0]))
    price = np.full(L, 20.0)
    outs, _ = ag.act(0, 100.0, 1, price)                           # nearly empty (floor 100): every offer needs charging
    offers = [M.decode(r) for r in outs if M.decode(r)["msg_type"] == "offer"]
    for m in [M.decode(r) for r in outs]:
        send(m)
    send(msg("nominate", "R", "exchange", 0, {"call_id": "OP:c", "zone": "SYS",
                                              "window": {"start_utc": ISO[1], "end_utc": END}, "kw": [0.0] * (L - 1)}, "R:n"))
    if rival_on is not None:
        a, b_ = wins[rival_on]
        send(msg("offer", "R", "exchange", 0, {"need_id": needs[rival_on], "mode": "scheduled", "zone": "SYS",
                                               "window": {"start_utc": ISO[a], "end_utc": ISO[b_]}, "kw": 100.0,
                                               "floor_usd_mwh": 0.0, "standing": False}, "R:o"))
    out = send(msg("tick", "clock", "exchange", 1, {}, "clk:1"))       # clears need 0 (priority 1)
    awards = [m for m in out if m["msg_type"] == "award"]
    for m in out:
        if isinstance(m["to"], list) and "A" in m["to"] or m["to"] == "A":
            ag.receive(M.encode(m))
    revisions = [M.decode(r) for r in ag.between_clearings(1, 100.0, 1, price)]
    rev_out = [o for r in revisions for o in send(r)]
    out = send(msg("tick", "clock", "exchange", 1, {"substep": 1}, "clk:1.1"))   # clears need 1
    awards += [m for m in out if m["msg_type"] == "award"]
    return ex, ag, offers, awards, revisions, rev_out, out


def test_prep_of_a_stacked_offer_covers_only_what_its_own_award_needs():
    """COM_07's award 1 carried 2,104 kWh of prep for 129 kWh of delivery: its offer was stacked after offers that
    lost, and its prep included their charging. The awarded prep must not be much more than the award needs."""
    ex, ag, offers, awards, *_ = stack_market(rival_on=0)
    mine = [a for a in awards if a["to"][0] == "A"]
    assert len(mine) == 1                                          # only A's second offer won
    need_kwh = mine[0]["payload"]["accredited_kw"] * 2 * DT / 0.95  # stored energy for 2 steps
    reachable_without = 0.0                                        # SOC starts at the floor
    needed_charge = (need_kwh - reachable_without) / 0.95          # AC kWh of charging
    allowed_prep = sum(ex.caps["A"]["prep"].values()) * DT
    assert allowed_prep <= needed_charge * 1.2 + 1.0
