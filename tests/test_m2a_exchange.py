"""M2a exchange rules (D34 revised, D37): zone targets, prep-aware clearing, demonstrated limits and the daily
energy mandate, and the rebound cap. Every rule here is enforced in code (rule 5)."""
import pytest

from exchange import messages as M
from exchange.core import Exchange
from tests.test_exchange import GATE, T0, Harness, code

WIN = {"start_utc": "2023-01-02T12:00:00Z", "end_utc": "2023-01-02T12:30:00Z"}
CALL_WIN = {"start_utc": "2023-01-02T06:30:00Z", "end_utc": "2023-01-02T14:00:00Z"}     # 30 steps
CALL_DEADLINE = "2023-01-02T06:30:00Z"


def t_at(i, base="2023-01-02T06:30:00Z"):
    from exchange.core import fmt_t, parse_t
    from datetime import timedelta
    return fmt_t(parse_t(base) + i * timedelta(minutes=15))


class H(Harness):
    def __init__(self, **flags):
        super().__init__()
        self.ex = Exchange("run", "OP", "meter", "clock", ["SYS"], require_nominations=True, scarcity_usd_mwh=500.0,
                           **flags)

    def limits(self, aid, usable=200.0, pv=40.0):
        return self.attest(aid, usable=usable, pv=pv)

    def register(self, aid, max_kw=100.0, floor=0.0, daily=None):
        mandate = {"max_kw": max_kw, "price_floor": floor, "workload_limits": None}
        if daily is not None:
            mandate["daily_kwh"] = daily
        if f"{aid}/bess" not in self.ex.attested:
            self.attest(aid)
        _, out = self.send("register", aid, {"agent_id": aid, "legal_entity": f"{aid} LLC",
                                             "assets": [{"asset_id": f"{aid}/bess", "zone": "SYS", "type": "bess"}],
                                             "mandate": mandate})
        if out:
            return out
        return self.send("subscribe", aid, {"signals": ["post_need"], "zones": ["SYS"]})[1]

    def target(self, kw, window=CALL_WIN, sender="OP"):
        n = len(_steps(window))
        return self.send("zone_target", sender, {"zone": "SYS", "window": window, "target_kw": [kw] * n})[1]

    def call(self, binding=False, window=CALL_WIN, deadline=CALL_DEADLINE):
        return self.send("call_nominations", "OP", {"zone": "SYS", "window": window, "deadline_utc": deadline,
                                                    "binding": binding})

    def nominate(self, aid, cid, net, window=CALL_WIN, t=T0):
        n = len(_steps(window))
        vals = net if isinstance(net, list) else [net] * n
        return self.send("nominate", aid, {"call_id": cid, "zone": "SYS", "window": window, "kw": vals}, t=t)

    def offer_prep(self, aid, nid, kw, floor, prep=None, window=WIN, t=T0):
        p = {"need_id": nid, "mode": "scheduled", "zone": "SYS", "window": window, "kw": kw, "floor_usd_mwh": floor,
             "standing": False}
        if prep is not None:
            p["prep"] = prep
        return self.send("offer", aid, p, t=t)

    def meter_net(self, aid, t, net):
        return self.send("telemetry", "meter", {"asset_id": f"{aid}/bess", "soc_kwh": 0.0, "meter_kw": 0.0,
                                                "net_kw": net}, t=t)[1]


def _steps(window):
    from exchange.core import fmt_t, parse_t
    from datetime import timedelta
    t, out = parse_t(window["start_utc"]), []
    while t < parse_t(window["end_utc"]):
        out.append(fmt_t(t))
        t += timedelta(minutes=15)
    return out


def market(**flags):
    h = H(prep_clearing=True, **flags)
    for a in ("A", "B", "C"):
        assert h.register(a) == []
    cid, _ = h.call()
    for a in ("A", "B", "C"):
        assert h.nominate(a, cid, 0.0)[1] == []          # zone load 0 everywhere
    h.tick(CALL_DEADLINE)
    return h


def report(out):
    return next(m["payload"] for m in out if m["msg_type"] == "clearing_report")


# ---- zone targets and limits are only accepted from the right sender ----
def test_only_operator_sends_targets_and_only_metering_sends_limits():
    h = H()
    assert code(h.target(50.0, sender="A")) == "unauthorized_sender"
    assert h.target(50.0) == []
    bad = h.send("zone_target", "OP", {"zone": "SYS", "window": CALL_WIN, "target_kw": [1.0]})[1]
    assert code(bad) == "bad_message"
    assert code(h.send("asset_limits", "A", {"asset_id": "A/bess", "usable_kwh": 1, "power_kw": 1,
                                             "pv_kwh_per_day": 1})[1]) == "unauthorized_sender"


# ---- demonstrated limits and the daily energy mandate ----
def test_registration_rejects_daily_kwh_above_demonstrated_limits():
    h0 = H(daily_energy=True, attested_owners=False)      # pre-D69: limits could be missing at registration
    _, out = h0.send("register", "A", {"agent_id": "A", "legal_entity": "A LLC",
                                       "assets": [{"asset_id": "A/bess", "zone": "SYS", "type": "bess"}],
                                       "mandate": {"max_kw": 100.0, "price_floor": 0.0,
                                                   "daily_kwh": {"2023-01-02": 100.0}}})
    assert code(out) == "over_demonstrated_limit"                                              # no limits yet
    h = H(daily_energy=True)
    h.limits("A", usable=200.0, pv=40.0)                                                       # demonstrated 240
    assert code(h.register("A", daily={"2023-01-02": 241.0})) == "over_demonstrated_limit"
    assert h.register("A", daily={"2023-01-02": 240.0, "2023-01-03": 100.0}) == []


def test_daily_energy_counts_each_step_on_its_local_date():
    h = H(daily_energy=True)
    h.limits("A", usable=200.0, pv=0.0)
    h.register("A", daily={"2023-01-01": 30.0, "2023-01-02": 30.0})
    # 05:45 to 06:15 UTC is 23:45 Jan 1 to 00:15 Jan 2 Central: one step on each date
    cross = {"start_utc": "2023-01-02T05:45:00Z", "end_utc": "2023-01-02T06:15:00Z"}
    nid, _ = h.send("post_need", "OP", {"zone": "SYS", "window": cross, "kw_needed": 200.0, "mode": "scheduled",
                                        "price_cap": 50.0, "gate_utc": "2023-01-02T05:45:00Z"},
                    t="2023-01-02T05:00:00Z")
    # 100 kW x 0.25 h = 25 kWh on each date: fits a 30 kWh budget per date, though the window holds 50 kWh
    assert h.offer_prep("A", nid, 100.0, 1.0, window=cross, t="2023-01-02T05:00:00Z")[1] == []
    nid2, _ = h.send("post_need", "OP", {"zone": "SYS", "window": cross, "kw_needed": 200.0, "mode": "scheduled",
                                         "price_cap": 50.0, "gate_utc": "2023-01-02T05:45:00Z"},
                     t="2023-01-02T05:00:00Z")
    assert code(h.offer_prep("A", nid2, 40.0, 1.0, window=cross, t="2023-01-02T05:00:00Z")[1]) == "outside_mandate"


# ---- prep validation ----
def test_prep_must_start_at_or_after_gate_be_nonnegative_and_zero_in_window():
    h = market()
    nid, _ = h.need(kw=100)
    assert code(h.offer_prep("A", nid, 50, 1, {"start_utc": "2023-01-02T06:45:00Z", "kw": [10.0]})[1]) == "bad_message"
    assert code(h.offer_prep("A", nid, 50, 1, {"start_utc": GATE, "kw": [-1.0]})[1]) == "bad_message"
    assert code(h.offer_prep("A", nid, 50, 1, {"start_utc": "2023-01-02T12:00:00Z", "kw": [5.0]})[1]) == "bad_message"
    assert h.offer_prep("A", nid, 50, 1, {"start_utc": GATE, "kw": [0.0, 10.0]})[1] == []


# ---- prep-aware clearing ----
def test_cheaper_offer_whose_prep_breaks_the_target_loses():
    h = market()
    h.target(50.0)
    nid, _ = h.need(kw=100)
    h.offer_prep("A", nid, 100, 1.0, {"start_utc": "2023-01-02T08:00:00Z", "kw": [60.0]})    # cheap, too much prep
    h.offer_prep("B", nid, 100, 5.0, {"start_utc": "2023-01-02T08:00:00Z", "kw": [30.0]})
    out = h.tick(GATE)
    awards = [m for m in out if m["msg_type"] == "award"]
    assert [(a["to"][0], a["payload"]["price_usd_mwh"]) for a in awards] == [("B", 5.0)]
    rep = report(out)
    assert [r for _, r in rep["skipped"]] == ["over_target"] and rep["skipped"][0][0].startswith("A:")
    assert rep["price_usd_mwh"] == 5.0 and rep["price_without_prep_check"] == 1.0
    assert awards[0]["payload"]["prep"] == {"start_utc": "2023-01-02T08:00:00Z", "kw": [30.0]}


def test_accepted_prep_counts_against_later_offers_and_partial_fills_scale_prep():
    h = market()
    h.target(50.0)
    nid, _ = h.need(kw=150)
    h.offer_prep("A", nid, 100, 1.0, {"start_utc": "2023-01-02T08:00:00Z", "kw": [30.0]})
    h.offer_prep("B", nid, 100, 2.0, {"start_utc": "2023-01-02T08:00:00Z", "kw": [50.0]})   # even half (25) breaks 50
    h.offer_prep("C", nid, 100, 3.0, {"start_utc": "2023-01-02T08:00:00Z", "kw": [20.0]})   # half filled: prep 10
    out = h.tick(GATE)
    got = {a["to"][0]: a["payload"] for a in out if a["msg_type"] == "award"}
    assert set(got) == {"A", "C"} and got["C"]["accredited_kw"] == 50.0
    assert got["C"]["prep"]["kw"] == [10.0]
    assert [r for _, r in report(out)["skipped"]] == ["over_target"]


def test_skip_reasons_missing_target_and_missing_nominations():
    h = market()
    h.target(50.0, window={"start_utc": "2023-01-02T06:30:00Z", "end_utc": "2023-01-02T08:00:00Z"})
    nid, _ = h.need(kw=100)
    h.offer_prep("A", nid, 100, 1.0, {"start_utc": "2023-01-02T08:00:00Z", "kw": [10.0]})   # no target at 08:00
    out = h.tick(GATE)
    assert [r for _, r in report(out)["skipped"]] == ["missing_target"]
    h2 = market()
    late = {"start_utc": "2023-01-02T14:00:00Z", "end_utc": "2023-01-02T15:00:00Z"}
    h2.target(50.0, window={"start_utc": "2023-01-02T06:30:00Z", "end_utc": "2023-01-02T15:00:00Z"})
    nid, _ = h2.need(kw=100)
    h2.offer_prep("A", nid, 100, 1.0, {"start_utc": late["start_utc"], "kw": [10.0]})        # nobody nominated 14:00
    out = h2.tick(GATE)
    assert [r for _, r in report(out)["skipped"]] == ["missing_nominations"]


def test_overnight_prep_into_day_two_clears_when_targets_cover_the_horizon_and_not_when_over():
    """A day-1 offer whose prep runs overnight into day 2: not skipped for missing targets once the operator has
    sent targets for the full nominated horizon; skipped if it would push day-2 nominated load over target."""
    horizon = {"start_utc": "2023-01-02T06:30:00Z", "end_utc": "2023-01-03T12:00:00Z"}
    overnight = "2023-01-03T09:00:00Z"                     # 03:00 Jan 3 Central
    for day2_load, expect in ((0.0, "award"), (45.0, "over_target")):
        h = H(prep_clearing=True)
        for a in ("A", "B", "C"):
            h.register(a)
        cid, _ = h.call(window=horizon)
        steps = _steps(horizon)
        for a in ("A", "B", "C"):
            net = [-(day2_load / 3) if t >= "2023-01-03T06:00:00Z" else 0.0 for t in steps]
            h.nominate(a, cid, net, window=horizon)
        h.tick(CALL_DEADLINE)
        h.target(50.0, window=horizon)
        nid, _ = h.need(kw=100)
        h.offer_prep("A", nid, 100, 1.0, {"start_utc": overnight, "kw": [10.0]})
        out = h.tick(GATE)
        if expect == "award":
            assert [m["msg_type"] for m in out if m["msg_type"] == "award"] == ["award"]
            assert report(out)["skipped"] == []
        else:
            assert [r for _, r in report(out)["skipped"]] == ["over_target"]


def test_awarded_prep_counts_once_before_and_after_renomination():
    h = market()
    h.target(1000.0)
    nid, _ = h.need(kw=100)
    h.offer_prep("A", nid, 100, 1.0, {"start_utc": "2023-01-02T08:00:00Z", "kw": [30.0]})
    h.tick(GATE)
    t = "2023-01-02T08:00:00Z"
    assert h.ex._zone_load("SYS", t) == pytest.approx(30.0)          # pending prep counted
    cid, _ = h.call(window={"start_utc": "2023-01-02T07:30:00Z", "end_utc": "2023-01-02T14:00:00Z"},
                    deadline="2023-01-02T07:30:00Z")
    n = len(_steps({"start_utc": "2023-01-02T07:30:00Z", "end_utc": "2023-01-02T14:00:00Z"}))
    net = [-30.0 if i == 2 else 0.0 for i in range(n)]               # the new nomination includes the prep
    h.nominate("A", cid, net, window={"start_utc": "2023-01-02T07:30:00Z", "end_utc": "2023-01-02T14:00:00Z"},
               t="2023-01-02T07:15:00Z")
    assert h.ex._zone_load("SYS", t) == pytest.approx(30.0)          # once, not 60


# ---- rebound cap (D34 revised) ----
def test_rebound_cap_scope_band_and_single_charge():
    h = H(prep_clearing=True, no_rebound=True, rebound_hours=24.0)
    for a in ("A", "B", "C"):
        h.register(a)
    horizon = {"start_utc": "2023-01-02T06:30:00Z", "end_utc": "2023-01-03T14:00:00Z"}
    cid, _ = h.call(window=horizon)
    for a in ("A", "B", "C"):
        h.nominate(a, cid, 0.0, window=horizon)
    h.tick(CALL_DEADLINE)
    h.target(1000.0, window=horizon)
    nid, _ = h.need(kw=100)
    h.offer_prep("A", nid, 100, 1.0, {"start_utc": "2023-01-02T08:00:00Z", "kw": [40.0]})
    h.tick(GATE)
    bind, _ = h.call(binding=True, window={"start_utc": "2023-01-02T07:30:00Z", "end_utc": "2023-01-03T14:00:00Z"},
                     deadline="2023-01-02T07:30:00Z")
    bsteps = _steps({"start_utc": "2023-01-02T07:30:00Z", "end_utc": "2023-01-03T14:00:00Z"})
    # the binding nomination already includes the extra 09:00 load, so it can't be imbalance there: only the cap
    # catches it (a binding nomination can't legitimize rebound)
    bnet = [-150.0 if t == "2023-01-02T09:00:00Z" else 0.0 for t in bsteps]
    h.nominate("A", bind, bnet, window={"start_utc": "2023-01-02T07:30:00Z",
                                        "end_utc": "2023-01-03T14:00:00Z"}, t="2023-01-02T07:15:00Z")
    h.tick("2023-01-02T07:30:00Z")
    h.meter_net("A", "2023-01-02T08:00:00Z", -40.0)       # declared prep: load 40 = cap 40, no charge
    h.meter_net("A", "2023-01-02T09:00:00Z", -150.0)      # 150 kW over the cap, 100 beyond the 50 kW band
    h.meter_net("A", "2023-01-02T12:00:00Z", -500.0)      # inside the award window: not rebound
    h.meter_net("A", "2023-01-03T13:00:00Z", -500.0)      # past window end + 24 h: uncapped (imbalance only)
    out = h.send("tick", "clock", {"final": True}, t="2023-01-03T14:00:00Z")[1]
    reb = [m["payload"] for m in out if m["msg_type"] == "rebound"]
    imb = {m["to"]: m["payload"] for m in out if m["msg_type"] == "imbalance"}
    assert len(reb) == 1 and reb[0]["excess_kwh"] == pytest.approx(100 * 0.25)
    assert reb[0]["charge_usd"] == pytest.approx(500 * 100 * 0.25 / 1000)
    assert reb[0]["window"]["end_utc"] == "2023-01-03T12:30:00Z"
    # 12:00 (in the window) and 13:00 Jan 3 (past the cap span) are over the binding nomination only: imbalance
    assert imb["A"]["deviation_kwh"] == pytest.approx(2 * (500 - 50) * 0.25)


def test_a_step_over_both_nomination_and_cap_is_charged_once_for_the_larger_excess():
    h = H(prep_clearing=True, no_rebound=True)
    for a in ("A", "B", "C"):
        h.register(a)
    horizon = {"start_utc": "2023-01-02T06:30:00Z", "end_utc": "2023-01-02T14:00:00Z"}
    cid, _ = h.call(window=horizon)
    for a in ("A", "B", "C"):
        h.nominate(a, cid, 0.0, window=horizon)
    h.tick(CALL_DEADLINE)
    h.target(1000.0, window=horizon)
    nid, _ = h.need(kw=100)
    h.offer_prep("A", nid, 100, 1.0, None)
    h.tick(GATE)
    bwin = {"start_utc": "2023-01-02T07:30:00Z", "end_utc": "2023-01-02T14:00:00Z"}
    bind, _ = h.call(binding=True, window=bwin, deadline="2023-01-02T07:30:00Z")
    bnet = [-100.0 if t == "2023-01-02T09:00:00Z" else 0.0 for t in _steps(bwin)]   # nominated 100 kW load at 09:00
    h.nominate("A", bind, bnet, window=bwin, t="2023-01-02T07:15:00Z")
    h.tick("2023-01-02T07:30:00Z")
    h.meter_net("A", "2023-01-02T09:00:00Z", -300.0)
    # vs binding: 300 - 100 - 50 = 150 kW; vs cap (pre-award nomination 0): 300 - 0 - 50 = 250 kW -> rebound only
    out = h.send("tick", "clock", {"final": True}, t="2023-01-02T14:00:00Z")[1]
    reb = [m["payload"] for m in out if m["msg_type"] == "rebound"]
    imb = [m["payload"] for m in out if m["msg_type"] == "imbalance" and m["to"] == "A"]
    assert reb[0]["excess_kwh"] == pytest.approx(250 * 0.25)
    assert imb[0]["deviation_kwh"] == pytest.approx(0.0)


def test_offer_whose_prep_does_not_fit_is_accepted_in_part():
    h = H(prep_clearing=True, partial_prep=True, min_partial_kw=10.0)
    for a in ("A", "B", "C"):
        h.register(a)
    cid, _ = h.call()
    for a in ("A", "B", "C"):
        h.nominate(a, cid, 0.0)
    h.tick(CALL_DEADLINE)
    h.target(30.0)
    nid, _ = h.need(kw=100)
    h.offer_prep("A", nid, 100, 1.0, {"start_utc": "2023-01-02T08:00:00Z", "kw": [60.0]})   # only half fits
    out = h.tick(GATE)
    aw = [m["payload"] for m in out if m["msg_type"] == "award"]
    assert len(aw) == 1 and aw[0]["accredited_kw"] == pytest.approx(50.0)
    assert aw[0]["prep"]["kw"] == [pytest.approx(30.0)]
    rep = report(out)
    assert rep["partial"] == [[aw[0]["offer_id"], pytest.approx(50.0)]] and rep["skipped"] == []
    h2 = H(prep_clearing=True, partial_prep=True, min_partial_kw=60.0)        # the part that fits is too small
    for a in ("A", "B", "C"):
        h2.register(a)
    cid, _ = h2.call()
    for a in ("A", "B", "C"):
        h2.nominate(a, cid, 0.0)
    h2.tick(CALL_DEADLINE)
    h2.target(30.0)
    nid, _ = h2.need(kw=100)
    h2.offer_prep("A", nid, 100, 1.0, {"start_utc": "2023-01-02T08:00:00Z", "kw": [60.0]})
    out = h2.tick(GATE)
    assert not [m for m in out if m["msg_type"] == "award"] and [r for _, r in report(out)["skipped"]] == ["over_target"]


def test_registration_accepts_a_budget_at_exactly_the_demonstrated_limit():
    """Bug 1 (M2a run 1): budgets clipped exactly at the limit were rounded up and rejected."""
    h = H(daily_energy=True)
    h.limits("A", usable=18765.884742790975 - 40.0, pv=40.0)
    assert h.register("A", daily={"2023-01-02": 18765.884742790975}) == []
    h.limits("B", usable=100.0, pv=0.0)
    assert code(h.register("B", daily={"2023-01-02": 100.02})) == "over_demonstrated_limit"
