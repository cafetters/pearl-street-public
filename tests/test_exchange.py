"""Exchange rules (exchange/core.py, clearing.py, settlement.py). Every rule here is enforced in code (rule 5)."""
import pytest

from exchange import messages as M
from exchange.clearing import clear
from exchange.core import Exchange

T0 = "2023-01-02T06:00:00Z"
GATE = "2023-01-02T07:00:00Z"
WIN = {"start_utc": "2023-01-02T12:00:00Z", "end_utc": "2023-01-02T12:30:00Z"}      # 2 steps
STEPS = ["2023-01-02T12:00:00Z", "2023-01-02T12:15:00Z"]
WIN2 = {"start_utc": "2023-01-02T12:15:00Z", "end_utc": "2023-01-02T12:45:00Z"}     # overlaps WIN at 12:15


class Harness:
    def __init__(self):
        self.ex = Exchange("run", "OP", "meter", "clock", ["SYS"])
        self.n = 0

    def send(self, msg_type, sender, payload, t=T0, to="exchange"):
        self.n += 1
        m = M.make(msg_type, sender, to, t, payload, f"{sender}:{self.n}", "run")
        return m["msg_id"], [M.decode(r) for r in self.ex.handle(M.encode(m))]

    def attest(self, aid, usable=200.0, pv=40.0, power=100.0, owner=None, agent=None, t=T0):
        """D69 rule 6: the metering service attests the asset's owner (legal entity) and operating agent, with its
        demonstrated limits. Defaults match register: agent aid, entity f"{aid} LLC"."""
        return self.send("asset_limits", "meter", {"asset_id": f"{aid}/bess", "usable_kwh": usable, "power_kw": power,
                                                    "pv_kwh_per_day": pv, "owner": owner or f"{aid} LLC",
                                                    "agent_id": agent or aid}, t=t)[1]

    def register(self, aid, max_kw=100.0, floor=0.0):
        if f"{aid}/bess" not in self.ex.attested:
            self.attest(aid)
        self.send("register", aid, {"agent_id": aid, "legal_entity": f"{aid} LLC",
                                    "assets": [{"asset_id": f"{aid}/bess", "zone": "SYS", "type": "bess"}],
                                    "mandate": {"max_kw": max_kw, "price_floor": floor, "workload_limits": None}})
        return self.send("subscribe", aid, {"signals": ["post_need"], "zones": ["SYS"]})[1]

    def need(self, kw=150.0, window=WIN, cap=50.0):
        return self.send("post_need", "OP", {"zone": "SYS", "window": window, "kw_needed": kw, "mode": "scheduled",
                                             "price_cap": cap, "gate_utc": GATE})

    def offer(self, aid, need_id, kw, floor, window=WIN, t=T0):
        return self.send("offer", aid, {"need_id": need_id, "mode": "scheduled", "zone": "SYS", "window": window,
                                        "kw": kw, "floor_usd_mwh": floor, "standing": False}, t=t)

    def tick(self, t):
        return self.send("tick", "clock", {}, t=t)[1]

    def meter(self, aid, t, kw):
        return self.send("telemetry", "meter", {"asset_id": f"{aid}/bess", "soc_kwh": 0.0, "meter_kw": kw}, t=t)[1]


def code(out):
    assert len(out) == 1 and out[0]["msg_type"] == "reject", out
    return out[0]["payload"]["code"]


@pytest.fixture
def h():
    h = Harness()
    for a in ("A", "B", "C"):
        assert h.register(a) == []
    return h


def test_need_is_broadcast_to_subscribers(h):
    nid, out = h.need()
    assert len(out) == 1 and out[0]["msg_type"] == "post_need"
    assert out[0]["to"] == ["A", "B", "C"] and out[0]["payload"]["need_id"] == nid


def test_unauthorized_senders(h):
    _, out = h.send("post_need", "A", {"zone": "SYS", "window": WIN, "kw_needed": 1.0, "mode": "scheduled",
                                       "price_cap": 1.0, "gate_utc": GATE})
    assert code(out) == "unauthorized_sender"
    assert code(h.send("telemetry", "A", {"asset_id": "A/bess", "soc_kwh": 0, "meter_kw": 0})[1]) == "unauthorized_sender"
    assert code(h.send("tick", "A", {})[1]) == "unauthorized_sender"
    assert code(h.send("award", "A", {"offer_id": "x", "need_id": "y", "window": WIN, "accredited_kw": 1,
                                      "price_usd_mwh": 1, "cap_kw": None})[1]) == "unauthorized_sender"
    _, out = h.send("register", "D", {"agent_id": "E", "legal_entity": "x",
                                      "assets": [{"asset_id": "E/bess", "zone": "SYS", "type": "bess"}],
                                      "mandate": {"max_kw": 1.0, "price_floor": 0.0}})
    assert code(out) == "unauthorized_sender"


def test_not_registered(h):
    nid, _ = h.need()
    assert code(h.offer("Z", nid, 10, 1)[1]) == "not_registered"
    assert code(h.send("subscribe", "Z", {"signals": [], "zones": ["SYS"]})[1]) == "not_registered"


def test_outside_mandate_floor_and_power():
    h = Harness()
    h.register("A", max_kw=100.0, floor=5.0)
    nid, _ = h.need()
    assert code(h.offer("A", nid, 50, 4.0)[1]) == "outside_mandate"       # floor below mandate
    assert code(h.offer("A", nid, 120, 6.0)[1]) == "outside_mandate"      # above max_kw
    assert h.offer("A", nid, 60, 6.0)[1] == []


def test_outside_mandate_counts_overlapping_offers(h):
    n1, _ = h.need()
    n2, _ = h.need(window=WIN2)
    n3, _ = h.need(window={"start_utc": "2023-01-02T14:00:00Z", "end_utc": "2023-01-02T14:15:00Z"})
    assert h.offer("A", n1, 60, 1)[1] == []
    assert code(h.offer("A", n2, 60, 1, window=WIN2)[1]) == "outside_mandate"    # 120 kW at 12:15
    assert h.offer("A", n2, 40, 1, window=WIN2)[1] == []
    assert h.offer("A", n3, 100, 1, window={"start_utc": "2023-01-02T14:00:00Z",
                                            "end_utc": "2023-01-02T14:15:00Z"})[1] == []   # no overlap


def test_after_gate_closure(h):
    nid, _ = h.need()
    oid, out = h.offer("A", nid, 50, 1)
    assert out == []
    assert code(h.offer("B", nid, 50, 1, t=GATE)[1]) == "after_gate_closure"
    assert code(h.send("revise", "A", {"offer_id": oid, "kw": 40}, t=GATE)[1]) == "after_gate_closure"
    h.tick(GATE)
    assert code(h.offer("B", nid, 50, 1, t="2023-01-02T06:30:00Z")[1]) == "after_gate_closure"   # cleared


def test_revise_and_withdraw_before_gate(h):
    nid, _ = h.need(kw=100)
    oa, _ = h.offer("A", nid, 50, 10)
    ob, _ = h.offer("B", nid, 50, 20)
    assert h.send("revise", "A", {"offer_id": oa, "kw": 80, "floor_usd_mwh": 30})[1] == []
    assert h.send("revise", "B", {"offer_id": ob, "withdraw": True})[1] == []
    assert code(h.send("revise", "C", {"offer_id": oa, "kw": 1})[1]) == "unauthorized_sender"
    awards = h.tick(GATE)
    assert [(a["payload"]["offer_id"], a["payload"]["accredited_kw"], a["payload"]["price_usd_mwh"])
            for a in awards] == [(oa, 80.0, 30.0)]


def test_bad_messages(h):
    out = [M.decode(r) for r in h.ex.handle("not json")]
    assert code(out) == "bad_message"
    assert code(h.send("haggle", "A", {})[1]) == "bad_message"
    assert code(h.send("subscribe", "A", {"zones": ["SYS"]})[1]) == "bad_message"
    assert code(h.send("tick", "clock", {}, to="A")[1]) == "bad_message"
    assert code(h.offer("A", "no-such-need", 10, 1)[1]) == "bad_message"


def test_clear_uniform_price_partial_fill_cap_and_ties():
    offers = [{"offer_id": "a", "kw": 10, "floor_usd_mwh": 10.0, "seq": 1},
              {"offer_id": "b", "kw": 10, "floor_usd_mwh": 5.0, "seq": 3},
              {"offer_id": "c", "kw": 10, "floor_usd_mwh": 5.0, "seq": 2},
              {"offer_id": "d", "kw": 10, "floor_usd_mwh": 60.0, "seq": 4}]
    acc, price = clear(25, 50.0, offers)
    assert [(o["offer_id"], q) for o, q in acc] == [("c", 10), ("b", 10), ("a", 5)]
    assert price == 10.0
    acc, price = clear(100, 50.0, offers)          # short: everything under the cap, d is above it
    assert [o["offer_id"] for o, _ in acc] == ["c", "b", "a"] and price == 10.0
    assert clear(10, 1.0, offers) == ([], None)


def test_exchange_clears_at_uniform_price(h):
    nid, _ = h.need(kw=150, cap=50)
    h.offer("A", nid, 100, 20)
    h.offer("B", nid, 100, 10)
    h.offer("C", nid, 100, 70)          # above the cap
    awards = h.tick(GATE)
    got = sorted((a["to"][0], a["payload"]["accredited_kw"], a["payload"]["price_usd_mwh"]) for a in awards)
    assert got == [("A", 50.0, 20.0), ("B", 100.0, 20.0)]
    assert all(a["to"][1] == "OP" for a in awards)


def test_settlement_full_shortfall_and_pro_rata(h):
    n1, _ = h.need(kw=100, cap=50)
    n2, _ = h.need(kw=40, cap=50)
    h.offer("A", n1, 60, 10)            # A: 60 + 40 stacked on the same window
    h.offer("A", n2, 40, 10)
    h.offer("B", n1, 40, 10)
    h.tick(GATE)
    for t in STEPS:
        h.meter("A", t, 50.0)           # half of its 100 kW awarded
        h.meter("B", t, 40.0)           # all of it
    assert h.tick("2023-01-02T12:15:00Z") == []          # window not over yet
    settles = h.tick(WIN["end_utc"])
    by = {(s["to"][0], s["payload"]["need_id"]): s["payload"] for s in settles}
    assert by[("B", n1)]["delivered_kw"] == pytest.approx(40) and by[("B", n1)]["charge_usd"] == 0
    assert by[("A", n1)]["delivered_kw"] == pytest.approx(30)    # 60 x 50/100
    assert by[("A", n2)]["delivered_kw"] == pytest.approx(20)    # 40 x 50/100
    hours = 0.5
    assert by[("A", n1)]["credit_usd"] == pytest.approx(10 * 30 * hours / 1000)
    assert by[("A", n1)]["charge_usd"] == pytest.approx(50 * 30 * hours / 1000)    # shortfall at the cap
    assert by[("A", n2)]["new_delivery_record"] == pytest.approx(0.5)
    assert h.tick("2023-01-02T13:00:00Z") == []          # settled once


def test_missing_meter_reading_counts_as_zero(h):
    nid, _ = h.need(kw=100)
    h.offer("A", nid, 100, 1)
    h.tick(GATE)
    h.meter("A", STEPS[0], 100.0)
    s = h.tick(WIN["end_utc"])[0]["payload"]
    assert s["delivered_kw"] == pytest.approx(50)


def test_deterministic():
    def run():
        h = Harness()
        h.register("A")
        h.register("B")
        nid, _ = h.need(kw=120)
        h.offer("A", nid, 100, 5)
        h.offer("B", nid, 100, 7)
        out = h.tick(GATE)
        for t in STEPS:
            h.meter("A", t, 100.0)
            h.meter("B", t, 10.0)
        return out + h.tick(WIN["end_utc"])
    assert run() == run()
