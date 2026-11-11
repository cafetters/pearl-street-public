"""M1.1 (D32): nomination calls, zone aggregation, award eligibility and imbalance in the exchange."""
import pytest

from exchange.core import Exchange
from exchange.imbalance import band_kw, imbalance
from tests.test_exchange import GATE, STEPS, T0, WIN, Harness, code

DAY = {"start_utc": "2023-01-02T12:00:00Z", "end_utc": "2023-01-02T12:30:00Z"}     # same 2 steps as WIN
DEADLINE = "2023-01-02T06:30:00Z"


class NomHarness(Harness):
    def __init__(self, require=True, scarcity=500.0):
        super().__init__()
        self.ex = Exchange("run", "OP", "meter", "clock", ["SYS"], require_nominations=require,
                           scarcity_usd_mwh=scarcity)

    def call(self, binding=False, window=DAY, deadline=DEADLINE, sender="OP"):
        return self.send("call_nominations", sender, {"zone": "SYS", "window": window, "deadline_utc": deadline,
                                                      "binding": binding})

    def nominate(self, aid, call_id, kw, window=DAY, t=T0):
        return self.send("nominate", aid, {"call_id": call_id, "zone": "SYS", "window": window, "kw": kw}, t=t)

    def meter_net(self, aid, t, net):
        return self.send("telemetry", "meter", {"asset_id": f"{aid}/bess", "soc_kwh": 0.0, "meter_kw": 0.0,
                                                "net_kw": net}, t=t)[1]


@pytest.fixture
def h():
    h = NomHarness()
    for a in ("A", "B", "C"):
        h.register(a, max_kw=100.0)
    return h


def test_call_is_broadcast_with_the_rules(h):
    cid, out = h.call()
    assert out[0]["to"] == ["A", "B", "C"] and out[0]["payload"]["call_id"] == cid
    p = out[0]["payload"]
    assert (p["imbalance_usd_mwh"], p["band_pct"], p["band_min_kw"]) == (500.0, 0.05, 50.0)
    assert code(h.call(sender="A")[1]) == "unauthorized_sender"
    assert code(h.call(deadline="2023-01-02T13:00:00Z")[1]) == "bad_message"         # deadline after window start


def test_nominate_checks(h):
    cid, _ = h.call()
    assert h.nominate("A", cid, [10.0, 20.0])[1] == []
    assert code(h.nominate("Z", cid, [1.0, 1.0])[1]) == "not_registered"
    assert code(h.nominate("A", "no-such-call", [1.0, 1.0])[1]) == "bad_message"
    assert code(h.nominate("A", cid, [1.0])[1]) == "bad_message"                     # wrong length
    assert code(h.nominate("A", cid, [1.0, float("nan")])[1]) == "bad_message"
    assert code(h.nominate("A", cid, [1.0, 1.0], window=WIN | {"end_utc": "2023-01-02T12:15:00Z"})[1]) == "bad_message"
    assert code(h.nominate("A", cid, [1.0, 1.0], t=DEADLINE)[1]) == "after_gate_closure"


def test_operator_gets_only_the_zone_total(h):
    cid, _ = h.call()
    h.nominate("A", cid, [10.0, 20.0])
    h.nominate("B", cid, [-5.0, 5.0])
    h.nominate("A", cid, [11.0, 21.0])            # a later nomination replaces the earlier one
    out = h.tick(DEADLINE)
    assert len(out) == 1 and out[0]["msg_type"] == "nomination_aggregate" and out[0]["to"] == "OP"
    assert out[0]["payload"]["kw"] == [6.0, 26.0] and out[0]["payload"]["n_agents"] == 2
    assert code(h.nominate("C", cid, [1.0, 1.0], t=DEADLINE)[1]) == "after_gate_closure"


def test_no_nomination_no_award(h):
    cid, _ = h.call(deadline=GATE)
    nid, _ = h.need(kw=150)
    h.nominate("A", cid, [0.0, 0.0])
    h.offer("A", nid, 100, 5)
    h.offer("B", nid, 100, 1)                    # cheaper, but never nominated
    awards = [m for m in h.tick(GATE) if m["msg_type"] == "award"]
    assert [(a["to"][0], a["payload"]["accredited_kw"]) for a in awards] == [("A", 100.0)]


def test_nominations_not_required_in_m1_mode():
    h = NomHarness(require=False, scarcity=None)
    h.register("B")
    nid, _ = h.need(kw=50)
    h.offer("B", nid, 50, 1)
    assert [m["msg_type"] for m in h.tick(GATE)] == ["award"]


def test_imbalance_band_and_charge(h):
    cid, _ = h.call(binding=True)
    h.nominate("A", cid, [100.0, 100.0])
    h.nominate("B", cid, [0.0, 0.0])
    h.tick(DEADLINE)
    h.meter_net("A", STEPS[0], 30.0)             # 70 kW off: 20 beyond the 50 kW band
    h.meter_net("A", STEPS[1], 140.0)            # 40 kW off: inside the band
    h.meter_net("B", STEPS[0], 0.0)              # B's second reading is missing: counts as 0 kW, on nomination
    h.meter_net("C", STEPS[0], 999.0)            # C never nominated: not charged
    assert [m for m in h.tick(STEPS[1]) if m["msg_type"] == "imbalance"] == []     # window not over
    out = {m["to"]: m["payload"] for m in h.tick(DAY["end_utc"]) if m["msg_type"] == "imbalance"}
    assert set(out) == {"A", "B"}
    assert out["A"]["band_kw"] == 50.0
    assert out["A"]["deviation_kwh"] == pytest.approx(20 * 0.25)
    assert out["A"]["raw_deviation_kwh"] == pytest.approx((70 + 40) * 0.25)
    assert out["A"]["charge_usd"] == pytest.approx(500 * 20 * 0.25 / 1000)
    assert out["B"]["charge_usd"] == 0.0
    assert [m for m in h.tick("2023-01-02T13:00:00Z") if m["msg_type"] == "imbalance"] == []    # once


def test_non_binding_calls_charge_nothing(h):
    cid, _ = h.call(binding=False)
    h.nominate("A", cid, [100.0, 100.0])
    h.tick(DEADLINE)
    h.meter_net("A", STEPS[0], -500.0)
    assert [m for m in h.tick(DAY["end_utc"]) if m["msg_type"] == "imbalance"] == []


def test_band_and_imbalance_functions():
    assert band_kw(9779.0, 0.05, 50.0) == pytest.approx(488.95)
    assert band_kw(500.0, 0.05, 50.0) == 50.0
    r = imbalance({"a": 10.0, "b": 10.0}, {"a": -100.0}, ["a", "b", "c"], 0.25, 50.0, 1000.0)
    assert r["raw_deviation_kwh"] == pytest.approx((110 + 10) * 0.25)
    assert r["deviation_kwh"] == pytest.approx(60 * 0.25)
    assert r["charge_usd"] == pytest.approx(60 * 0.25)


def test_later_binding_nomination_takes_over_and_each_step_is_charged_once(h):
    w1 = {"start_utc": "2023-01-02T12:00:00Z", "end_utc": "2023-01-02T12:30:00Z"}
    w2 = {"start_utc": "2023-01-02T12:15:00Z", "end_utc": "2023-01-02T12:30:00Z"}
    c1, _ = h.call(binding=True, window=w1, deadline="2023-01-02T06:30:00Z")
    h.nominate("A", c1, [0.0, 0.0])
    h.tick("2023-01-02T06:30:00Z")
    # an award made later moves A's second step; the next binding call includes it
    c2, _ = h.call(binding=True, window=w2, deadline="2023-01-02T08:00:00Z")
    h.nominate("A", c2, [200.0], window=w2, t="2023-01-02T07:00:00Z")
    h.tick("2023-01-02T08:00:00Z")
    h.meter_net("A", STEPS[0], 0.0)
    h.meter_net("A", STEPS[1], 200.0)              # delivering the award: matches the binding nomination in force
    out = [m for m in h.tick(DAY["end_utc"]) if m["msg_type"] == "imbalance"]
    assert sum(m["payload"]["charge_usd"] for m in out) == 0.0
    assert sum(m["payload"]["raw_deviation_kwh"] for m in out) == 0.0
    assert {m["payload"]["call_id"] for m in out} == {c1, c2}       # step 1 under c1, step 2 under c2
