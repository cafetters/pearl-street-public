"""D64 (enrollment, availability declarations) and D65's delivery record, behind `delivery_record` (off by default).
Synthetic only. Each rule is enforced in exchange/core.py and exchange/records.py."""
import pytest

from exchange import messages as M
from exchange import records as REC
from exchange.core import Exchange
from tests.test_exchange import Harness, code

T0 = "2023-01-02T06:00:00Z"                   # 00:00 Central, Jan 2
NOON_JAN2 = "2023-01-02T18:00:00Z"            # 12:00 Central: Jan 3's day-ahead prices publish
CALL_WIN = {"start_utc": "2023-01-02T12:00:00Z", "end_utc": "2023-01-02T12:30:00Z"}


class R(Harness):
    def __init__(self, **kw):
        super().__init__()
        self.ex = Exchange("run", "OP", "meter", "clock", ["SYS"], require_nominations=True,
                           scarcity_usd_mwh=500.0, delivery_record=True, **kw)


def test_record_messages_need_the_flag_and_settings_enter_the_header_only_when_on():
    h = Harness()
    h.register("A")
    assert code(h.send("deregister", "A", {"agent_id": "A", "effective_date": "2023-01-04"})[1]) == "bad_message"
    assert "delivery_record" not in h.ex.config
    r = R(max_outage_days_per_season=2)
    assert r.ex.config["delivery_record"] is True and r.ex.config["max_outage_days_per_season"] == 2


def test_deregistration_takes_effect_on_a_later_day_filed_before_its_day_ahead_prices():
    h = R()
    h.register("A")
    dereg = lambda d, t: h.send("deregister", "A", {"agent_id": "A", "effective_date": d}, t=t)[1]
    assert code(dereg("2023-01-02", T0)) == "bad_message"                       # today: no daily opt-out
    assert code(dereg("2023-01-03", NOON_JAN2)) == "after_gate_closure"         # Jan 3 prices already out
    assert code(h.send("deregister", "A", {"agent_id": "B", "effective_date": "2023-01-04"})[1]) == "unauthorized_sender"
    assert dereg("2023-01-03", "2023-01-02T17:59:00Z") == []
    h.tick("2023-01-03T05:45:00Z")
    assert "A" not in h.ex.inactive
    h.tick("2023-01-03T06:00:00Z")                                              # Jan 3 00:00 Central
    assert "A" in h.ex.inactive and "A" not in h.ex.subs["SYS"] and "A/bess" not in h.ex.asset_owner
    assert code(h.send("subscribe", "A", {"signals": [], "zones": ["SYS"]}, t="2023-01-03T06:15:00Z")[1]) \
        == "not_registered"


def test_availability_declared_per_day_before_day_ahead_prices_within_registered_power_and_reserve():
    h = R(max_outage_days_per_season=1)
    h.register("A", max_kw=100.0)                       # attested with usable 200 kWh
    decl = lambda d, t=T0, **kw: h.send("declare_availability", "A", {
        "asset_id": "A/bess", "date": d, "power_kw": kw.get("power", 80.0), "reserve_kwh": kw.get("reserve", 20.0),
        "planned_outage": kw.get("outage", False)}, t=t)[1]
    assert decl("2023-01-03") == []
    assert code(decl("2023-01-03", t=NOON_JAN2)) == "after_gate_closure"
    assert code(decl("2023-01-04", power=120.0)) == "outside_mandate"            # above registered power
    assert code(decl("2023-01-04", reserve=250.0)) == "over_demonstrated_limit"  # above demonstrated usable energy
    assert decl("2023-01-04", outage=True) == []
    assert code(decl("2023-01-05", outage=True)) == "outside_mandate"            # season cap 1 day (DJF)
    assert decl("2023-03-01", outage=True) == []                                 # another season
    h.register("B")
    assert code(h.send("declare_availability", "B", {"asset_id": "A/bess", "date": "2023-01-06", "power_kw": 1.0,
                                                     "reserve_kwh": 0.0, "planned_outage": False})[1]) \
        == "unauthorized_sender"


def test_forced_outage_is_late_after_the_day_ahead_deadline():
    h = R()
    h.register("A")
    win3 = {"start_utc": "2023-01-03T12:00:00Z", "end_utc": "2023-01-03T13:00:00Z"}
    assert h.send("declare_outage", "A", {"asset_id": "A/bess", "window": win3}, t="2023-01-02T17:00:00Z")[1] == []
    assert h.send("declare_outage", "A", {"asset_id": "A/bess", "window": win3}, t="2023-01-03T11:00:00Z")[1] == []
    assert [o["late"] for o in h.ex.outages["A/bess"]] == [False, True]


def test_nomination_delivery_weighs_each_step_by_the_larger_of_nomination_and_band():
    r = REC.nomination_delivery({"t1": 100.0, "t2": 10.0, "t3": 0.0}, {"t1": 100.0, "t2": 200.0, "t3": 0.0}, 50.0, 0.25)
    assert r["binding_steps"] == 3 and r["steps_within_band"] == 2
    assert r["weight_kwh"] == pytest.approx((100 + 50 + 50) * 0.25)               # 10 kW and 0 kW steps weigh 50 kW
    assert r["share_within_band"] == pytest.approx(150 / 200)
    z = REC.nomination_delivery({"t": 0.0}, {"t": 0.0}, 50.0, 0.25)               # all-zero nominations: same unit
    assert z["weight_kwh"] == pytest.approx(12.5) and z["share_within_band"] == 1.0
    miss = REC.nomination_delivery({"t": 0.0}, {"t": 80.0}, 50.0, 0.25)
    assert miss["share_within_band"] == 0.0                                       # no step-count fallback either way
    assert REC.nomination_delivery({}, {}, 50.0, 0.25)["share_within_band"] is None
    assert set(r) == {"binding_steps", "steps_within_band", "weight_kwh", "weight_kwh_within_band", "share_within_band"}


def _run_record(h, meter_kw, soc_kwh, outage=False):
    h.register("A")
    h.send("subscribe", "A", {"signals": ["call_nominations"], "zones": ["SYS"]})
    h.send("declare_availability", "A", {"asset_id": "A/bess", "date": "2023-01-02", "power_kw": 50.0,
                                         "reserve_kwh": 40.0, "planned_outage": outage}, t="2023-01-01T06:00:00Z")
    cid, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": CALL_WIN, "deadline_utc": "2023-01-02T07:00:00Z",
                                               "binding": True})
    h.send("nominate", "A", {"call_id": cid, "zone": "SYS", "window": CALL_WIN, "kw": [-100.0, -20.0]})
    h.tick("2023-01-02T07:00:00Z")
    for t, kw, net, soc in zip(["2023-01-02T12:00:00Z", "2023-01-02T12:15:00Z"], meter_kw, [-100.0, 100.0], soc_kwh):
        h.send("telemetry", "meter", {"asset_id": "A/bess", "soc_kwh": soc, "meter_kw": kw, "net_kw": net}, t=t)
    out = h.send("tick", "clock", {"final": True}, t="2023-01-02T12:30:00Z")[1]
    return [m for m in out if m["msg_type"] == "delivery_record"]


def test_closing_tick_sends_each_agent_its_record_per_asset_and_legal_entity():
    h = R()
    (rec,) = _run_record(h, meter_kw=[0.0, 0.0], soc_kwh=[100.0, 30.0])
    p = rec["payload"]
    assert rec["to"] == "A" and p["assets"] == ["A/bess"]
    assert p["this_run"]["binding_steps"] == 2 and p["this_run"]["steps_within_band"] == 1
    assert p["this_run"]["share_within_band"] == pytest.approx(100.0 / 150.0)   # 100 kW kept; 20 kW (weighs 50) missed
    assert p["record"]["below_reserve_steps"] == 1 and p["record"]["days_declared"] == 1
    assert p["record"]["mean_declared_power_share"] == pytest.approx(0.5)
    assert p["record"]["response"] is None                                       # D65 response measure: hook only
    assert M.problem(rec) is None


def test_battery_activity_during_a_declared_outage_is_counted():
    h = R()
    (rec,) = _run_record(h, meter_kw=[30.0, 0.0], soc_kwh=[100.0, 100.0], outage=True)
    assert rec["payload"]["record"]["outage_activity_steps"] == 1


def test_record_follows_asset_and_legal_entity_not_the_agent():
    h = R()
    h.register("A")
    led = h.ex.ledger
    led.add(("A/bess", "A LLC"), binding_steps=4, steps_within_band=4, weight_kwh=10.0, weight_kwh_within_band=10.0)
    h.send("deregister", "A", {"agent_id": "A", "effective_date": "2023-01-03"})
    h.tick("2023-01-03T06:00:00Z")
    assert h.attest("A", owner="A LLC", agent="A2", t="2023-01-03T06:10:00Z") == []   # software swap, same entity
    _, out = h.send("register", "A2", {"agent_id": "A2", "legal_entity": "A LLC",
                                       "assets": [{"asset_id": "A/bess", "zone": "SYS", "type": "bess"}],
                                       "mandate": {"max_kw": 100.0, "price_floor": 0.0}}, t="2023-01-03T06:15:00Z")
    assert out == []
    led.add(("A/bess", "A LLC"), binding_steps=4, steps_within_band=0, weight_kwh=10.0, weight_kwh_within_band=0.0)
    assert led.view(("A/bess", "A LLC"))["share_within_band"] == pytest.approx(0.5)


def test_binding_call_with_no_nomination_counts_as_missed():
    h = R()
    h.register("A")
    h.send("subscribe", "A", {"signals": ["call_nominations"], "zones": ["SYS"]})
    h.send("call_nominations", "OP", {"zone": "SYS", "window": CALL_WIN, "deadline_utc": "2023-01-02T07:00:00Z",
                                      "binding": True})
    h.tick("2023-01-02T07:00:00Z")
    out = h.send("tick", "clock", {"final": True}, t="2023-01-02T12:30:00Z")[1]
    (rec,) = [m for m in out if m["msg_type"] == "delivery_record"]
    assert rec["payload"]["this_run"]["binding_calls_missed"] == 1


def test_impossible_dates_are_refused_not_raised():
    h = R()
    h.register("A")
    assert code(h.send("deregister", "A", {"agent_id": "A", "effective_date": "2023-02-30"})[1]) == "bad_message"
    assert code(h.send("declare_availability", "A", {"asset_id": "A/bess", "date": "2023-13-01", "power_kw": 1.0,
                                                     "reserve_kwh": 0.0, "planned_outage": False})[1]) == "bad_message"


def test_after_re_registration_per_asset_counts_go_to_the_new_owner_once_and_prices_do_not_freeze():
    h = R()
    h.register("A")
    h.send("declare_availability", "A", {"asset_id": "A/bess", "date": "2023-01-03", "power_kw": 50.0,
                                         "reserve_kwh": 10.0, "planned_outage": True})
    h.send("deregister", "A", {"agent_id": "A", "effective_date": "2023-01-03"})
    h.tick("2023-01-03T06:00:00Z")
    h.attest("A", owner="A LLC", agent="A2", t="2023-01-03T06:05:00Z")
    h.send("register", "A2", {"agent_id": "A2", "legal_entity": "A LLC",
                              "assets": [{"asset_id": "A/bess", "zone": "SYS", "type": "bess"}],
                              "mandate": {"max_kw": 100.0, "price_floor": 0.0}}, t="2023-01-03T06:10:00Z")
    win = {"start_utc": "2023-01-03T12:00:00Z", "end_utc": "2023-01-03T12:30:00Z"}
    cid, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": win, "deadline_utc": "2023-01-03T07:00:00Z",
                                               "binding": False}, t="2023-01-03T06:15:00Z")
    h.send("nominate", "A2", {"call_id": cid, "zone": "SYS", "window": win, "kw": [0.0, 0.0]}, t="2023-01-03T06:20:00Z")
    assert h.ex._zone_load("SYS", "2023-01-03T12:00:00Z") is not None        # inactive A no longer freezes the zone
    for t in ("2023-01-03T12:00:00Z", "2023-01-03T12:15:00Z"):
        h.send("telemetry", "meter", {"asset_id": "A/bess", "soc_kwh": 5.0, "meter_kw": 30.0, "net_kw": 0.0}, t=t)
    out = h.send("tick", "clock", {"final": True}, t="2023-01-03T12:30:00Z")[1]
    (rec,) = [m for m in out if m["msg_type"] == "delivery_record"]
    r = rec["payload"]["record"]
    assert rec["to"] == "A2"
    assert (r["days_declared"], r["planned_outage_days"], r["outage_activity_steps"], r["below_reserve_steps"]) \
        == (1, 1, 2, 2)


def test_asset_that_changes_hands_mid_run_is_billed_once_in_imbalance_and_peak_settlement():
    """A deregisters from Jan 3 (00:00 Central = 06:00 UTC); the same asset re-registers for the same entity under
    agent A2 at 06:00. A binding window and priced steps span the switch. Each step is billed to the agent that held
    the asset at that step, exactly once."""
    h = R()
    h.register("A")
    h.send("subscribe", "A", {"signals": ["call_nominations"], "zones": ["SYS"]})
    win = {"start_utc": "2023-01-03T05:30:00Z", "end_utc": "2023-01-03T06:30:00Z"}
    steps = ["2023-01-03T05:30:00Z", "2023-01-03T05:45:00Z", "2023-01-03T06:00:00Z", "2023-01-03T06:15:00Z"]
    cid, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": win, "deadline_utc": "2023-01-02T08:00:00Z",
                                               "binding": True})
    h.send("nominate", "A", {"call_id": cid, "zone": "SYS", "window": win, "kw": [0.0] * 4})
    h.send("deregister", "A", {"agent_id": "A", "effective_date": "2023-01-03"}, t="2023-01-02T09:00:00Z")
    h.tick("2023-01-02T08:00:00Z")                                      # binding: A's nominations are in force
    for t in steps[:2]:                                                 # A's steps: 300 kW load, far outside its band
        h.send("telemetry", "meter", {"asset_id": "A/bess", "soc_kwh": 50.0, "meter_kw": 0.0, "net_kw": -300.0}, t=t)
    h.tick("2023-01-03T06:00:00Z")                                      # A leaves; the asset is free
    assert h.attest("A", owner="A LLC", agent="A2", t="2023-01-03T06:00:00Z") == []
    assert h.send("register", "A2", {"agent_id": "A2", "legal_entity": "A LLC",
                                     "assets": [{"asset_id": "A/bess", "zone": "SYS", "type": "bess"}],
                                     "mandate": {"max_kw": 100.0, "price_floor": 0.0}}, t="2023-01-03T06:00:00Z")[1] == []
    for t in steps[2:]:                                                 # A2's steps: 500 kW load
        h.send("telemetry", "meter", {"asset_id": "A/bess", "soc_kwh": 50.0, "meter_kw": 0.0, "net_kw": -500.0}, t=t)
    out = h.send("tick", "clock", {"final": True}, t="2023-01-03T06:30:00Z")[1]
    imb = {m["to"]: m["payload"] for m in out if m["msg_type"] == "imbalance"}
    assert set(imb) == {"A"}                     # A2 has no binding nomination; A only for its own two steps
    assert imb["A"]["deviation_kwh"] == pytest.approx(2 * (300.0 - 50.0) * 0.25)
    # peak settlement over the same four steps at 100 $/MWh, refunded on a zero reference
    h.ex.peak_done = False
    h.ex.peak_settled = {t: (100.0, cid) for t in steps}
    ps = {m["to"]: m["payload"] for m in h.ex._peak_settle("2023-01-03T06:30:00Z")}
    assert ps["A"]["steps"] == 2 and ps["A2"]["steps"] == 2
    assert ps["A"]["charge_usd"] == pytest.approx(100.0 * 300.0 * 0.25 / 1000.0 * 2)
    assert ps["A2"]["charge_usd"] == pytest.approx(100.0 * 500.0 * 0.25 / 1000.0 * 2)
    metered_total = 100.0 * (2 * 300.0 + 2 * 500.0) * 0.25 / 1000.0
    assert ps["A"]["charge_usd"] + ps["A2"]["charge_usd"] == pytest.approx(metered_total)    # once, not twice


def _swap_run(register_a2_at="2023-01-03T06:00:00Z", register_a_at=T0):
    """The hand-over scenario: A (registered at register_a_at) nominates on a binding window that spans its
    deregistration; A2 takes the asset over. Returns the closing tick's messages and the exchange."""
    h = R()
    h.attest("A")
    assert h.send("register", "A", {"agent_id": "A", "legal_entity": "A LLC",
                                    "assets": [{"asset_id": "A/bess", "zone": "SYS", "type": "bess"}],
                                    "mandate": {"max_kw": 100.0, "price_floor": 0.0}}, t=register_a_at)[1] == []
    h.send("subscribe", "A", {"signals": ["call_nominations"], "zones": ["SYS"]})
    win = {"start_utc": "2023-01-03T05:30:00Z", "end_utc": "2023-01-03T06:30:00Z"}
    steps = ["2023-01-03T05:30:00Z", "2023-01-03T05:45:00Z", "2023-01-03T06:00:00Z", "2023-01-03T06:15:00Z"]
    cid, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": win, "deadline_utc": "2023-01-02T08:00:00Z",
                                               "binding": True})
    h.send("nominate", "A", {"call_id": cid, "zone": "SYS", "window": win, "kw": [0.0] * 4})
    h.send("deregister", "A", {"agent_id": "A", "effective_date": "2023-01-03"}, t="2023-01-02T09:00:00Z")
    h.tick("2023-01-02T08:00:00Z")
    for t in steps[:2]:
        h.send("telemetry", "meter", {"asset_id": "A/bess", "soc_kwh": 50.0, "meter_kw": 0.0, "net_kw": -300.0}, t=t)
    h.tick("2023-01-03T06:00:00Z")
    h.attest("A", owner="A LLC", agent="A2", t="2023-01-03T06:00:00Z")
    assert h.send("register", "A2", {"agent_id": "A2", "legal_entity": "A LLC",
                                     "assets": [{"asset_id": "A/bess", "zone": "SYS", "type": "bess"}],
                                     "mandate": {"max_kw": 100.0, "price_floor": 0.0}}, t=register_a2_at)[1] == []
    for t in steps[2:]:
        h.send("telemetry", "meter", {"asset_id": "A/bess", "soc_kwh": 50.0, "meter_kw": 0.0, "net_kw": -500.0}, t=t)
    out = h.send("tick", "clock", {"final": True}, t="2023-01-03T06:30:00Z")[1]
    h.ex.peak_done = False
    h.ex.peak_settled = {t: (100.0, cid) for t in steps}
    ps = {m["to"]: m["payload"] for m in h.ex._peak_settle("2023-01-03T06:30:00Z")}
    return h, out, ps


def test_tenure_ignores_the_time_a_sender_writes_on_its_register():
    """Review finding: tenure must not start at the register's t_utc. A future-dated register still pays for its
    steps; a backdated re-registration can't overlap the previous holder."""
    _, out, ps = _swap_run(register_a_at="2099-01-01T00:00:00Z")
    imb = {m["to"]: m["payload"] for m in out if m["msg_type"] == "imbalance"}
    assert imb["A"]["deviation_kwh"] == pytest.approx(2 * (300.0 - 50.0) * 0.25) and ps["A"]["steps"] == 2
    _, _, ps = _swap_run(register_a2_at="2023-01-02T00:00:00Z")
    assert ps["A"]["steps"] == 2 and ps["A2"]["steps"] == 2


def test_after_a_swap_the_new_agent_is_not_charged_a_miss_for_calls_before_its_tenure():
    h, out, _ = _swap_run()
    rec = {m["to"]: m["payload"] for m in out if m["msg_type"] == "delivery_record"}
    assert rec["A2"]["this_run"]["binding_calls_missed"] == 0
    assert h.ex.ledger.view(("A/bess", "A LLC"))["binding_calls_missed"] == 0    # A answered it; the entity's record


def test_d64_filings_cannot_be_backdated_past_the_clock():
    h = R()
    h.register("A")
    h.tick("2023-01-02T18:30:00Z")                             # Jan 3's day-ahead prices are out
    assert code(h.send("deregister", "A", {"agent_id": "A", "effective_date": "2023-01-03"},
                       t="2023-01-02T09:00:00Z")[1]) == "after_gate_closure"
    assert code(h.send("declare_availability", "A", {"asset_id": "A/bess", "date": "2023-01-03", "power_kw": 50.0,
                                                     "reserve_kwh": 0.0, "planned_outage": False},
                       t="2023-01-02T09:00:00Z")[1]) == "after_gate_closure"
    win3 = {"start_utc": "2023-01-03T12:00:00Z", "end_utc": "2023-01-03T13:00:00Z"}
    h.send("declare_outage", "A", {"asset_id": "A/bess", "window": win3}, t="2023-01-02T09:00:00Z")
    assert h.ex.outages["A/bess"][-1]["late"] is True


def test_award_settlement_credits_only_the_holders_own_delivery(monkeypatch):
    import exchange.core as core
    h, _, _ = _swap_run()
    seen = {}

    class Stop(Exception):
        pass

    def capture(award, mine, meter, *a, **k):
        seen["meter"] = dict(meter)
        raise Stop

    monkeypatch.setattr(core, "settle_award", capture)
    h.ex.needs["n1"] = {"_step_list": [], "price_cap": 50.0}
    with pytest.raises(Stop):
        h.ex._settle({"agent_id": "A", "need_id": "n1", "award_id": "w1"}, "2023-01-03T06:30:00Z")
    assert sorted(seen["meter"]) == ["2023-01-03T05:30:00Z", "2023-01-03T05:45:00Z"]


def _late_first_registration(flag: bool, register_t="2023-01-02T00:00:00Z"):
    """A holds A/bess from the start. B registers its own asset B/bess for the first time mid-run, after the clock's
    06:00 Jan 3 tick (its register is backdated to before the run). A binding call (deadline 08:00 Jan 2) and four
    priced steps span B's registration; the metering service sends B's reference over all four steps after B
    registers, as a recorded reference would."""
    h = R() if flag else Harness()
    if not flag:
        h.ex = Exchange("run", "OP", "meter", "clock", ["SYS"], require_nominations=True, scarcity_usd_mwh=500.0)
    h.register("A")
    win = {"start_utc": "2023-01-03T05:30:00Z", "end_utc": "2023-01-03T06:30:00Z"}
    steps = ["2023-01-03T05:30:00Z", "2023-01-03T05:45:00Z", "2023-01-03T06:00:00Z", "2023-01-03T06:15:00Z"]
    cid, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": win, "deadline_utc": "2023-01-02T08:00:00Z",
                                               "binding": True})
    h.send("nominate", "A", {"call_id": cid, "zone": "SYS", "window": win, "kw": [0.0] * 4})
    h.tick("2023-01-02T08:00:00Z")
    early = [h.send("telemetry", "meter", {"asset_id": "B/bess", "soc_kwh": 5.0, "meter_kw": 0.0, "net_kw": -100.0},
                    t=t)[1] for t in steps[:2]]
    h.tick("2023-01-03T06:00:00Z")
    h.attest("B", t="2023-01-03T06:00:00Z")
    assert h.send("register", "B", {"agent_id": "B", "legal_entity": "B LLC",
                                    "assets": [{"asset_id": "B/bess", "zone": "SYS", "type": "bess"}],
                                    "mandate": {"max_kw": 100.0, "price_floor": 0.0}}, t=register_t)[1] == []
    h.send("reference_net", "meter", {"asset_id": "B/bess", "window": win, "net_kw": [-100.0] * 4},
           t="2023-01-03T06:00:00Z")
    for t in steps:
        h.send("telemetry", "meter", {"asset_id": "A/bess", "soc_kwh": 5.0, "meter_kw": 0.0, "net_kw": 0.0}, t=t)
    for t in steps[2:]:
        h.send("telemetry", "meter", {"asset_id": "B/bess", "soc_kwh": 5.0, "meter_kw": 0.0, "net_kw": -100.0}, t=t)
    out = h.send("tick", "clock", {"final": True}, t="2023-01-03T06:30:00Z")[1]
    h.ex.peak_done = False
    h.ex.peak_settled = {t: (100.0, cid) for t in steps}
    ps = {m["to"]: m["payload"] for m in h.ex._peak_settle("2023-01-03T06:30:00Z")}
    return h, early, out, ps


def test_asset_registered_for_the_first_time_mid_run_is_not_billed_before_it_registers():
    """Owner, Nov 2: with tenure on (delivery_record), B answers only for steps from its registration, by the
    exchange's clock: no meter reading before then (refused as an unknown asset), no peak charge or refund on the two
    earlier priced steps although its reference covers them, and the binding call before it registered is not a miss.
    Before this fix tenure started at "" for a never-held asset, so B was settled on all four steps."""
    h, early, out, ps = _late_first_registration(flag=True)
    assert all(code(r) == "bad_message" for r in early)                     # unknown asset: nothing stored
    assert ps["B"]["steps"] == 2
    assert ps["B"]["refund_usd"] == pytest.approx(100.0 * 100.0 * 0.25 / 1000.0 * 2)
    assert ps["B"]["charge_usd"] == pytest.approx(100.0 * 100.0 * 0.25 / 1000.0 * 2)
    assert ps["A"]["steps"] == 4                                             # A held its own asset throughout
    assert not [m for m in out if m["msg_type"] == "imbalance" and m["to"] == "B"]
    rec = {m["to"]: m["payload"] for m in out if m["msg_type"] == "delivery_record"}
    assert rec["B"]["this_run"]["binding_calls_missed"] == 0
    assert h.ex.tenure_from["B"] == "2023-01-03T06:00:00Z"                  # the clock, not B's backdated t_utc


def test_tenure_is_inactive_without_delivery_record_so_honest_runs_cannot_change():
    """Owner, Nov 2: tenure billing sits behind delivery_record. With it off every priced step is the agent's, exactly
    as before D64 (a mid-run first registration is settled on its whole reference window, as before), and a sender's
    t_utc can't move anything."""
    h, _, _, ps = _late_first_registration(flag=False)
    assert ps["B"]["steps"] == 4 and ps["A"]["steps"] == 4
    assert all(h.ex._holds("B", t) for t in ("2000-01-01T00:00:00Z", "2099-01-01T00:00:00Z"))
    _, _, _, ps_future = _late_first_registration(flag=False, register_t="2099-01-01T00:00:00Z")
    assert ps_future == ps
