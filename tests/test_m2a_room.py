"""D50 (published zone room, arm B of the head-to-head), D51 (pass check ignores exceedance within the aggregate
free band) and per-window M0 ceilings."""
import json

import numpy as np
import pytest

from exchange import messages as M
from tests.test_exchange import GATE, T0
from tests.test_m2a_agents import DT, ISO, L, agent, flat_price, need, offer_of
from tests.test_m2a_exchange import CALL_DEADLINE, CALL_WIN, H, WIN, _steps


def room_market(publish_room=True):
    h = H(prep_clearing=True, sequential_clearing=True, publish_room=publish_room)
    for a in ("A", "B", "C"):
        assert h.register(a) == []
    cid, _ = h.call()
    for a in ("A", "B", "C"):
        h.nominate(a, cid, -10.0)                       # each agent nominates 10 kW of load: zone load 30 kW
    h.tick(CALL_DEADLINE)
    steps = _steps(CALL_WIN)
    h.send("zone_target", "OP", {"zone": "SYS", "window": CALL_WIN, "target_kw": [100.0] * len(steps)})
    return h, steps


def post(h, kw=50.0):
    return h.send("post_need", "OP", {"zone": "SYS", "window": WIN, "kw_needed": kw, "mode": "scheduled",
                                      "price_cap": 50.0, "gate_utc": GATE, "priority": 1})


def rooms(out):
    return [m for m in out if m["msg_type"] == "zone_room"]


def test_room_is_target_minus_zone_load_sent_to_subscribers_and_deduplicated():
    h, steps = room_market()
    _, out = post(h)
    r = rooms(out)
    assert len(r) == 1 and sorted(r[0]["to"]) == ["A", "B", "C"]                # subscribers only, not the operator
    assert r[0]["payload"]["window"]["start_utc"] == steps[0]
    assert r[0]["payload"]["room_kw"] == pytest.approx([70.0] * len(steps))     # 100 - 30
    assert "target_kw" not in r[0]["payload"]
    _, out = post(h)                                                            # nothing moved: no new zone_room
    assert rooms(out) == []


def test_room_is_republished_after_a_clearing_with_awarded_prep_taken_out():
    h, steps = room_market()
    nid, _ = post(h)
    t8 = steps[steps.index("2023-01-02T08:00:00Z")]
    h.offer_prep("A", nid, 50, 1.0, {"start_utc": t8, "kw": [40.0]})
    out = h.tick(GATE)
    r = rooms(out)
    assert len(r) == 1
    pos = [t for t in steps if t >= GATE]
    room = dict(zip(pos, r[0]["payload"]["room_kw"]))
    assert room[t8] == pytest.approx(30.0) and room["2023-01-02T09:00:00Z"] == pytest.approx(70.0)


def test_no_room_messages_with_the_flag_off():
    h, _ = room_market(publish_room=False)
    nid, out = post(h)
    h.offer_prep("A", nid, 50, 1.0, None)
    assert rooms(out) == [] and rooms(h.tick(GATE)) == []


def room_msg(room):
    return M.encode(M.make("zone_room", "exchange", ["A"], ISO[0],
                           {"zone": "SYS", "window": {"start_utc": ISO[4], "end_utc": "2023-01-02T10:00:00Z"},
                            "room_kw": list(room[4:])}, "ex:r", "run"))


def test_agent_keeps_its_prep_within_the_published_room():
    price = flat_price()
    price[4:6] = 5.0                                   # cheapest charging at steps 4-5, where the zone has no room
    room = np.full(L, 500.0)
    room[4:7] = 0.0

    def offer(use_room):
        ag = agent(firm_offers=True, send_prep=True, use_room=use_room)
        ag.start(0)
        ag.latest_sent[:] = 0.0
        ag.receive(room_msg(room))
        ag.receive(need(10, 12, 100.0, gate_k=4))
        out, _ = ag.act(0, 40.0, 1, price)
        p = offer_of(out)
        prep = np.zeros(L)
        a = ISO.index(p["prep"]["start_utc"])
        prep[a:a + len(p["prep"]["kw"])] = p["prep"]["kw"]
        return p, prep

    blind, prep_blind = offer(False)
    seen, prep_seen = offer(True)
    assert prep_blind[4:6].sum() > 50.0                # without the room it charges in the cheap, full steps
    assert prep_seen[4:7].sum() == pytest.approx(0.0, abs=0.2)
    assert prep_seen.sum() * DT * 0.95 >= 2 * seen["kw"] * DT / 0.95 - 0.05     # still enough to deliver
    assert seen["floor_usd_mwh"] > blind["floor_usd_mwh"]                         # dearer charging is priced in


def test_d51_counts_only_exceedance_beyond_the_aggregate_band():
    from sim.run_m1 import over_counts
    excess = np.array([-5.0, 0.5, 200.0, 4000.0, -np.inf])
    assert over_counts(excess, 1000.0) == (3, 1)


def test_m0_folder_comes_from_the_config():
    import sim.run_m1 as R
    assert R.m0_dir({}) == R.REPO / "runs" / "m0"
    assert R.m0_dir({"m0_dir": "runs/m0_jan12"}) == R.REPO / "runs" / "m0_jan12"
