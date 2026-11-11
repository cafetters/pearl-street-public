"""D42: sequential clearing within a gate. Needs sharing a gate clear one per tick in operator priority order;
need_cleared carries the need id only and prices follow in one gate_prices message; between clearings only new
winners may revise, and only downward; offers are alternatives, so the mandate is re-checked at clearing. Agents
size every offer from known awards only and resize after each award."""
import json

import numpy as np
import pytest

from exchange import messages as M
from sim.run_m1 import run_variant
from tests.test_exchange import GATE, code
from tests.test_m2a_exchange import CALL_DEADLINE, H
from tests.test_m2a_regressions import DT, stack_market
from tests.test_replay import CFG_M11, synthetic_slice

W1 = {"start_utc": "2023-01-02T12:00:00Z", "end_utc": "2023-01-02T12:30:00Z"}
W2 = {"start_utc": "2023-01-02T13:00:00Z", "end_utc": "2023-01-02T13:30:00Z"}


def seq_market(agents=("A", "B"), max_kw=100.0, **flags):
    h = H(sequential_clearing=True, **flags)
    for a in agents:
        assert h.register(a, max_kw=max_kw) == []
    cid, _ = h.call()
    for a in agents:
        assert h.nominate(a, cid, 0.0)[1] == []
    h.tick(CALL_DEADLINE)
    return h


def post(h, window, kw=50.0, priority=None):
    p = {"zone": "SYS", "window": window, "kw_needed": kw, "mode": "scheduled", "price_cap": 50.0, "gate_utc": GATE}
    if priority is not None:
        p["priority"] = priority
    return h.send("post_need", "OP", p)[0]


def sub(h, n):
    return h.send("tick", "clock", {"substep": n}, t=GATE)[1]


def of_type(out, t):
    return [m for m in out if m["msg_type"] == t]


def test_needs_clear_one_per_tick_in_priority_order_and_prices_follow_the_last():
    h = seq_market()
    n_none = post(h, W1)                         # no priority: last
    n_tie = post(h, W2, priority=2)              # same priority, later window: after n_low
    n_low = post(h, W1, priority=2)
    n_high = post(h, W2, priority=1)
    for nid, w in ((n_none, W1), (n_tie, W2), (n_low, W1), (n_high, W2)):
        assert h.offer("A", nid, 50.0, 1.0, window=w)[1] == []
    order, outs = [], [h.tick(GATE)] + [sub(h, i) for i in (1, 2, 3)]
    for i, out in enumerate(outs):
        cleared = of_type(out, "need_cleared")
        assert len(cleared) == 1 and set(cleared[0]["payload"]) == {"need_id"}   # the id only, no price
        order.append(cleared[0]["payload"]["need_id"])
        award = of_type(out, "award")[0]["payload"]
        assert award["need_id"] == order[-1] and award["price_usd_mwh"] == pytest.approx(1.0)
        assert bool(of_type(out, "gate_prices")) == (i == 3)                  # once, after the last need
    assert order == [n_high, n_low, n_tie, n_none]
    gp = of_type(outs[-1], "gate_prices")[0]
    assert set(gp["payload"]["prices"]) == set(order) and "OP" in gp["to"] and "B" in gp["to"]
    assert of_type(sub(h, 4), "gate_prices") == []                            # nothing left: no repeat


def test_substeps_exist_only_with_sequential_clearing():
    h = H()
    assert code(h.send("tick", "clock", {"substep": 1}, t=GATE)[1]) == "bad_message"
    h = seq_market()
    assert code(h.send("tick", "clock", {"substep": -1}, t=GATE)[1]) == "bad_message"
    assert code(h.send("post_need", "OP", {"zone": "SYS", "window": W1, "kw_needed": 50.0, "mode": "scheduled",
                                           "price_cap": 50.0, "gate_utc": GATE, "priority": 1.5})[1]) == "bad_message"


def test_between_clearings_only_new_winners_may_revise_and_only_downward():
    h = seq_market()
    n1, n2 = post(h, W1, priority=1), post(h, W2, priority=2)
    oa1, _ = h.offer("A", n1, 50.0, 1.0)
    ob1, _ = h.offer("B", n1, 50.0, 5.0)
    oa2, _ = h.offer("A", n2, 80.0, 1.0, window=W2)
    ob2, _ = h.offer("B", n2, 80.0, 5.0, window=W2)
    out = h.tick(GATE)                                                         # A wins n1
    assert [m["to"][0] for m in of_type(out, "award")] == ["A"]
    rev = lambda aid, oid, **p: h.send("revise", aid, {"offer_id": oid, **p}, t=GATE)[1]
    assert code(rev("B", ob2, kw=60.0)) == "after_gate_closure"               # a loser may not revise
    assert code(rev("B", ob2, floor_usd_mwh=1.0)) == "after_gate_closure"
    assert code(rev("A", oa2, kw=90.0)) == "after_gate_closure"               # a winner may not go up
    assert rev("A", oa2, kw=40.0, floor_usd_mwh=2.0) == []                    # a winner's resize is accepted
    assert h.ex.offers[oa2]["kw"] == 40.0 and h.ex.offers[oa2]["floor_usd_mwh"] == 2.0
    awards = of_type(sub(h, 1), "award")
    assert {(m["to"][0], m["payload"]["accredited_kw"]) for m in awards} == {("A", 40.0), ("B", 10.0)}
    n3 = post(h, W1, priority=3)
    assert code(h.offer("B", n3, 10.0, 0.0, t=GATE)[1]) == "after_gate_closure"   # no new offers at the gate


def test_alternative_offers_pass_the_mandate_at_offer_time_and_are_blocked_at_clearing():
    h = seq_market(agents=("A",))
    n1, n2 = post(h, W1, kw=80.0, priority=1), post(h, W1, kw=80.0, priority=2)
    assert h.offer("A", n1, 80.0, 1.0)[1] == []
    assert h.offer("A", n2, 80.0, 1.0)[1] == []                    # overlapping alternatives: 160 kW, both accepted
    assert of_type(h.tick(GATE), "award")[0]["payload"]["accredited_kw"] == 80.0
    assert of_type(sub(h, 1), "award") == []                       # 80 + 80 > max_kw 100: skipped at clearing
    h = seq_market(agents=("A",), partial_prep=True, min_partial_kw=10.0)
    n1, n2 = post(h, W1, kw=80.0, priority=1), post(h, W1, kw=80.0, priority=2)
    h.offer("A", n1, 80.0, 1.0)
    h.offer("A", n2, 80.0, 1.0)
    h.tick(GATE)
    assert of_type(sub(h, 1), "award")[0]["payload"]["accredited_kw"] == pytest.approx(20.0)   # the room left


def test_com07_winner_resizes_its_next_offer_with_exact_combined_prep():
    """COM_07: two awards from one gate. After winning need 0 the agent resizes its offer on need 1 against the cap
    that now holds need 0's prep, so the awarded prep together is what both awards need, not a multiple of it."""
    ex, ag, offers, awards, revisions, rev_out, _ = stack_market(rival_on=None)
    assert [m["msg_type"] for m in revisions] == ["revise"] and not of_type(rev_out, "reject")
    mine = [a["payload"] for a in awards if a["to"][0] == "A"]
    assert len(mine) == 2
    stored = sum(p["accredited_kw"] * 2 * DT / 0.95 for p in mine)            # energy the two windows draw
    needed_charge = stored / 0.95                                             # AC kWh from the floor
    awarded_prep = sum(sum(p["prep"]["kw"]) * DT for p in mine if p.get("prep"))
    assert needed_charge - 1.0 <= awarded_prep <= needed_charge * 1.05 + 1.0


def test_an_agent_that_loses_the_first_need_offers_its_full_battery_on_the_second():
    ex, ag, offers, awards, revisions, *_ = stack_market(rival_on=0)
    assert revisions == []                                                    # a loss changes nothing
    assert [o["payload"]["kw"] for o in offers] == [100.0, 100.0]             # sized without assuming either wins
    mine = [a["payload"] for a in awards if a["to"][0] == "A"]
    assert [p["accredited_kw"] for p in mine] == [100.0]


def bug3_cfg(**extra):
    return {**CFG_M11, "rounds": "info", "nomination_span": "horizon", "firm_offers": True, "prep_clearing": True, "no_rebound": True,
            "imbalance_band_pct": 0.0, "imbalance_band_min_kw": 0.0, **extra}


def test_referee_collect_mode_records_failures_and_the_run_continues(tmp_path, monkeypatch):
    import agents.battery as B
    monkeypatch.setattr(B.BatteryAgent, "_allowed_net", lambda self: np.full(self.T, np.nan))   # bug 3 again
    sl = synthetic_slice()
    sl.agent["soc"][:] = 12.0
    r = run_variant(bug3_cfg(referee="collect"), sl, np.arange(2), tmp_path, headroom_mw=0.1)
    fails = r["referee_failures"]
    assert fails and {"agent", "new_awards", "failing_needs", "deficit_kwh", "award_kwh"} <= set(fails[0])
    with pytest.raises(RuntimeError, match="award not deliverable"):
        run_variant(bug3_cfg(referee="abort"), sl, np.arange(2), tmp_path / "abort", headroom_mw=0.1)


def test_sequential_synthetic_run_replays_with_substep_ticks(tmp_path):
    from exchange.trace.replay import replay
    cfg = bug3_cfg(sequential_clearing=True, partial_prep=True, daily_energy=True, risk_premium_usd_mwh=2.0,
                   rebound_hours=24, referee="collect")
    r = run_variant(cfg, synthetic_slice(), np.arange(2), tmp_path, headroom_mw=0.1)
    path = tmp_path / "exchange_h0.1_rinfo" / "trace.jsonl"
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert lines[0]["exchange"]["sequential_clearing"]
    types = {e["msg_type"] for e in lines[1:]}
    assert {"need_cleared", "gate_prices"} <= types
    needs = [e for e in lines[1:] if e["msg_type"] == "post_need" and e["to"] == "exchange"]
    assert needs and all(isinstance(e["payload"].get("priority"), int) for e in needs)
    assert r["referee_failures"] == []
    assert replay(path)["match"]
