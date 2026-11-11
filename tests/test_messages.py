"""Message schema (exchange/messages.py)."""
import pytest

from exchange import messages as M

T0 = "2023-01-02T06:00:00Z"
WIN = {"start_utc": "2023-01-02T12:00:00Z", "end_utc": "2023-01-02T13:00:00Z"}

GOOD = {
    "register": {"agent_id": "A", "legal_entity": "A LLC", "assets": [{"asset_id": "A/bess", "zone": "SYS", "type": "bess"}],
                 "mandate": {"max_kw": 1.0, "price_floor": 0.0}},
    "subscribe": {"signals": ["post_need"], "zones": ["SYS"]},
    "post_need": {"zone": "SYS", "window": WIN, "kw_needed": 1.0, "mode": "scheduled", "price_cap": 1.0,
                  "gate_utc": T0},
    "offer": {"need_id": "n", "mode": "scheduled", "zone": "SYS", "window": WIN, "kw": 1.0, "floor_usd_mwh": 0.0,
              "standing": False},
    "revise": {"offer_id": "o"},
    "award": {"offer_id": "o", "need_id": "n", "window": WIN, "accredited_kw": 1.0, "price_usd_mwh": 1.0,
              "cap_kw": None},
    "telemetry": {"asset_id": "a", "soc_kwh": 1.0, "meter_kw": 1.0},
    "suspend": {"offer_id": "o", "reason": "r"},
    "settle": {"award_id": "a", "delivered_kw": 1.0, "credit_usd": 1.0, "charge_usd": 0.0, "new_delivery_record": 1.0},
    "directive": {"action": "x"},
    "tick": {},
    "reject": {"ref_msg_id": "m", "code": "bad_message", "detail": "d", "refs": {}},
    "call_nominations": {"zone": "SYS", "window": WIN, "deadline_utc": T0, "binding": False},
    "nominate": {"call_id": "c", "zone": "SYS", "window": WIN, "kw": [1.0, 2.0, 3.0, 4.0]},
    "nomination_aggregate": {"call_id": "c", "zone": "SYS", "window": WIN, "kw": [1.0], "n_agents": 1},
    "imbalance": {"call_id": "c", "window": WIN, "deviation_kwh": 0.0, "charge_usd": 0.0},
    "zone_target": {"zone": "SYS", "window": WIN, "target_kw": [1.0, 1.0, 1.0, 1.0]},
    "asset_limits": {"asset_id": "a", "usable_kwh": 1.0, "power_kw": 1.0, "pv_kwh_per_day": 0.0},
    "clearing_report": {"need_id": "n", "price_usd_mwh": 1.0, "accepted": [], "skipped": []},
    "rebound": {"window": WIN, "excess_kwh": 0.0, "charge_usd": 0.0},
    "need_cleared": {"need_id": "n"},
    "gate_prices": {"gate_utc": T0, "prices": {"n": 1.0}},
    "cap_rebase": {"call_id": "c", "window": WIN, "base_kw": [1.0, None], "nomination_kw": [1.0, 2.0]},
    "zone_room": {"zone": "SYS", "window": WIN, "room_kw": [1.0, None, 2.0, 0.0]},
    "peak_price": {"zone": "SYS", "call_id": "c", "window": WIN, "price_usd_mwh": [0.0, 1.0, 2.0, 0.0], "rho": 1e-6,
                   "shift_kw": [0.0, 1.0, -1.0, 0.0], "round": 1, "final": False},
    "reference_net": {"asset_id": "a", "window": WIN, "net_kw": [1.0, -2.0, 0.0, 3.0]},
    "peak_settle": {"window": WIN, "steps": 2, "charge_usd": 1.0, "refund_usd": 0.5, "net_usd": -0.5},
    "selfish_reference": {"asset_id": "a", "call_id": "c", "window": WIN, "net_kw": [1.0, -2.0, 0.0, 3.0]},
    "modeled_response": {"asset_id": "a", "call_id": "c", "window": WIN, "p_net_kw": [1.0, -2.0, 0.0, 3.0],
                         "p_discharge_kw": [1.0, 0.0, 0.0, 3.0], "p_soc_end_kwh": 5.0,
                         "q_net_kw": [0.0, -2.0, 0.0, 2.0], "q_discharge_kw": [0.0, 0.0, 0.0, 2.0],
                         "q_soc_end_kwh": 5.0},
    "rt_price": {"window": WIN, "rt_usd_mwh": [20.0, 30.0, 25.0, 40.0], "end_energy_usd_kwh": 0.03},
    "guarantee_accrual": {"window": WIN, "calls": ["c"], "shortfall_usd_by_span": [1.5], "shortfall_usd": 1.5,
                          "modeled_shift_kwh": 10.0, "delivered_kwh": 9.0},
    "deregister": {"agent_id": "A", "effective_date": "2023-01-04"},
    "declare_availability": {"asset_id": "a", "date": "2023-01-04", "power_kw": 1.0, "reserve_kwh": 0.0,
                             "planned_outage": False},
    "declare_outage": {"asset_id": "a", "window": WIN},
    "delivery_record": {"assets": ["a"], "this_run": {}, "record": {}},
}


def test_every_type_has_a_good_example():
    assert set(GOOD) == set(M.REQUIRED)


@pytest.mark.parametrize("msg_type", sorted(M.REQUIRED))
def test_valid_message_passes_and_round_trips(msg_type):
    m = M.make(msg_type, "A", "exchange", T0, GOOD[msg_type], "A:1", "run")
    assert M.validate(m) is None
    assert M.decode(M.encode(m)) == m


@pytest.mark.parametrize("msg_type", [t for t in sorted(M.REQUIRED) if M.REQUIRED[t]])
def test_missing_payload_field_fails(msg_type):
    p = dict(GOOD[msg_type])
    p.pop(M.REQUIRED[msg_type][0])
    assert "missing" in M.validate(M.make(msg_type, "A", "exchange", T0, p, "A:1", "run"))


@pytest.mark.parametrize("key", M.ENVELOPE)
def test_missing_envelope_field_fails(key):
    m = M.make("tick", "clock", "exchange", T0, {}, "c:1", "run")
    del m[key]
    assert M.validate(m) is not None


def test_unknown_type_bad_window_and_bad_to_fail():
    assert "unknown msg_type" in M.validate(M.make("haggle", "A", "exchange", T0, {}, "A:1", "run"))
    bad = dict(GOOD["offer"], window={"start_utc": "x"})
    assert M.problem(M.make("offer", "A", "exchange", T0, bad, "A:1", "run"))[1] == {"field": "payload.window"}
    assert M.validate(M.make("tick", "clock", 5, T0, {}, "c:1", "run")) is not None
    assert M.validate(M.make("tick", "clock", ["A", "B"], T0, {}, "c:1", "run")) is None
