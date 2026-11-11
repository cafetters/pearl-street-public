"""Price mode (D53, arm C): the price rules ported from prototype v3, the exchange's price rounds, the settled price
and peak settlement, the planner's peak adder and prox, the operator's round schedule, and an end-to-end synthetic
run that replays. Every rule here is enforced in code (rule 5)."""
import json

import numpy as np
import pandas as pd
import pytest

from agents.operator import GridOperator
from agents.planner import Battery, prox_breakpoints, solve
from exchange import price_mode as PM
from tests.test_exchange import T0, code
from tests.test_m2a_exchange import H

K = 0.25 / 1000.0
WIN = {"start_utc": "2023-01-02T07:00:00Z", "end_utc": "2023-01-02T07:30:00Z"}      # 2 steps
STEPS = ["2023-01-02T07:00:00Z", "2023-01-02T07:15:00Z"]
DEADLINE = "2023-01-02T07:00:00Z"
ROUND_T = "2023-01-02T06:45:00Z"


# ---- ported rules match the prototype ----
def test_ported_rules_match_the_prototype():
    proto = pytest.importorskip("bench.price_mode_proto")
    rng = np.random.default_rng(1)
    for _ in range(20):
        pi, agg, tgt = rng.uniform(0, 0.02, 12), rng.uniform(0, 200, 12), rng.uniform(50, 150, 12)
        pi[rng.random(12) < 0.3] = 0.0
        alpha = rng.uniform(1e-6, 1e-4)
        assert np.allclose(PM.price_update(list(pi), list(agg), list(tgt), alpha), proto.price_update(pi, agg, tgt, alpha))
        assert np.allclose(PM.residual(list(agg), list(tgt), list(pi)), proto.residual(agg, tgt, pi))
        new = proto.price_update(pi, agg, tgt, alpha)
        assert np.allclose(PM.centre_shift(list(new), list(pi), alpha, 80), proto.centre_shift(new, pi, alpha, 80))
        rho, rho0, r, d = rng.uniform(1e-6, 1e-4), rng.uniform(1e-6, 1e-4), rng.uniform(0, 500), rng.uniform(0, 500)
        assert PM.rb_dual_kw(rho, rho0, d) == pytest.approx(proto.rb_dual_kw(rho, rho0, d, True))
        assert PM.rb_update(rho, r, d, 10, 2, rho0 / 1000, rho0 * 10) == pytest.approx(
            proto.rb_update(rho, r, d, 10, 2, rho0 / 1000, rho0 * 10))


def test_price_is_capped_and_never_negative():
    assert PM.price_update([0.1, 0.0], [1e9, -1e9], [0.0, 0.0], 1.0, cap=0.125) == [0.125, 0.0]


def test_converged_needs_a_full_streak_within_both_tolerances():
    ok, bad_dual = (500.0, 3.0), (500.0, 9.0)
    assert not PM.converged([ok, ok], 1000.0, 6.0, 3)
    assert PM.converged([bad_dual, ok, ok, ok], 1000.0, 6.0, 3)
    assert not PM.converged([ok, ok, bad_dual], 1000.0, 6.0, 3)
    assert not PM.converged([ok, (1500.0, 1.0), ok], 1000.0, 6.0, 3)


def test_rho0_from_public_data():
    assert PM.rho0_public(40.0, 0.25, 100.0) == pytest.approx(40.0 * K / 100.0)


# ---- the exchange's price rounds ----
class P(H):
    def __init__(self, **kw):
        super().__init__(price_mode=True, price_spread_usd_mwh=40.0, **kw)

    def setup(self, target=100.0, binding=False, window=WIN, deadline=DEADLINE):
        for aid in ("A", "B"):
            self.register(aid)
        self.target(target, window=window)
        cid, out = self.call(binding=binding, window=window, deadline=deadline)
        return cid

    def round(self, r, t=ROUND_T):
        return self.send("tick", "clock", {"price_round": r}, t=t)[1]


def test_price_rises_over_target_falls_under_and_shift_is_load_positive():
    h = P()
    cid = h.setup()
    h.nominate("A", cid, [-80.0, -30.0], window=WIN)          # nominations are site net, + = export
    h.nominate("B", cid, [-60.0, -30.0], window=WIN)          # zone load 140 (40 over target), then 60 (under)
    out = h.round(1)
    p = out[0]["payload"]
    assert out[0]["msg_type"] == "peak_price" and sorted(out[0]["to"]) == ["A", "B"]
    alpha = 40.0 * K / 100.0 / 2                              # rho0 / N
    assert p["price_usd_mwh"] == pytest.approx([alpha * 40.0 / K, 0.0])
    assert p["shift_kw"] == pytest.approx([20.0, 0.0])        # the gap / N: each centre moves to 20 kW less load
    assert not p["final"]
    h.nominate("A", cid, [-50.0, -30.0], window=WIN)          # now 80 kW at a priced step: 20 under target
    h.nominate("B", cid, [-30.0, -30.0], window=WIN)
    p2 = h.round(2)[0]["payload"]
    assert 0.0 <= p2["price_usd_mwh"][0] < p["price_usd_mwh"][0]
    assert p2["shift_kw"][0] < 0                              # a priced step under target pulls centres back up
    assert p2["price_usd_mwh"][1] == 0.0


def test_round_cap_marks_the_last_broadcast_final_and_further_rounds_are_rejected():
    h = P(price_rounds=2)
    cid = h.setup()
    for aid in ("A", "B"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    assert not h.round(1)[0]["payload"]["final"]
    assert h.round(2)[0]["payload"]["final"]
    assert code(h.round(3)) == "bad_message"


def test_rounds_must_run_in_order_and_only_in_price_mode():
    h = P()
    cid = h.setup()
    for aid in ("A", "B"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    assert code(h.round(2)) == "bad_message"
    plain = H()
    assert code(plain.send("tick", "clock", {"price_round": 1}, t=ROUND_T)[1]) == "bad_message"


def test_convergence_stops_early_with_the_price_unchanged():
    h = P(price_streak=2, price_tol_excess_kw=1.0)
    cid = h.setup()
    for aid in ("A", "B"):
        h.nominate(aid, cid, [-40.0, -40.0], window=WIN)      # 80 kW: under target and unpriced
    assert h.round(1)[0]["payload"]["price_usd_mwh"] == [0.0, 0.0]
    assert not h.round(2)[0]["payload"]["final"]              # first measured round: no plan change, streak 1
    p = h.round(3)[0]["payload"]
    assert p["final"] and p["price_usd_mwh"] == [0.0, 0.0] and p["shift_kw"] == [0.0, 0.0]


def test_nominations_close_at_the_deadline_after_price_rounds():
    h = P()
    cid = h.setup()
    for aid in ("A", "B"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    h.round(1)
    assert code(h.nominate("A", cid, [-70.0, -70.0], window=WIN, t=DEADLINE)[1]) == "after_gate_closure"


def test_settled_price_is_the_last_broadcast_and_peak_settle_refunds_on_the_reference():
    h = P()
    cid = h.setup(binding=True)
    h.send("reference_net", "meter", {"asset_id": "A/bess", "window": WIN, "net_kw": [-100.0, -20.0]})
    for aid in ("A", "B"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    last = h.round(1)[0]["payload"]["price_usd_mwh"]
    h.tick(DEADLINE)                                          # aggregation: the binding price is settled
    assert [h.ex.peak_settled[t][0] for t in STEPS] == pytest.approx(last)
    h.meter_net("A", STEPS[0], -70.0)
    h.meter_net("A", STEPS[1], -80.0)
    out = [m for m in h.send("tick", "clock", {"final": True}, t="2023-01-02T07:30:00Z")[1]
           if m["msg_type"] == "peak_settle"]
    a = next(m["payload"] for m in out if m["to"] == "A")
    hrs = 0.25 / 1000.0
    priced = [(p, i) for i, p in enumerate(last) if p > 0]
    assert a["charge_usd"] == pytest.approx(sum(p * [70.0, 80.0][i] * hrs for p, i in priced))
    assert a["refund_usd"] == pytest.approx(sum(p * [100.0, 20.0][i] * hrs for p, i in priced))
    assert a["net_usd"] == pytest.approx(a["refund_usd"] - a["charge_usd"])
    b = next(m["payload"] for m in out if m["to"] == "B")     # no reading, no reference: both count as 0
    assert b["charge_usd"] == 0.0 and b["refund_usd"] == 0.0


def test_a_later_binding_call_replaces_the_settled_price():
    h = P()
    cid = h.setup(binding=True)
    for aid in ("A", "B"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    first = h.round(1)[0]["payload"]["price_usd_mwh"]
    h.tick(DEADLINE)
    w2 = {"start_utc": "2023-01-02T07:15:00Z", "end_utc": "2023-01-02T07:30:00Z"}
    cid2, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": w2, "deadline_utc": STEPS[1],
                                                "binding": True}, t=DEADLINE)
    for aid in ("A", "B"):
        h.nominate(aid, cid2, [-90.0], window=w2, t=DEADLINE)
    second = h.round(1, t="2023-01-02T07:05:00Z")[0]["payload"]["price_usd_mwh"]
    h.tick(STEPS[1])
    assert h.ex.peak_settled[STEPS[0]] == (pytest.approx(first[0]), cid)
    assert h.ex.peak_settled[STEPS[1]] == (pytest.approx(second[0]), cid2)
    assert second[0] > first[1]


def test_reference_net_comes_only_from_metering():
    h = P()
    out = h.send("reference_net", "A", {"asset_id": "A/bess", "window": WIN, "net_kw": [0.0, 0.0]})[1]
    assert code(out) == "unauthorized_sender"
    assert code(h.send("reference_net", "meter", {"asset_id": "A/bess", "window": WIN, "net_kw": [0.0]})[1]) \
        == "bad_message"


# ---- planner ----
def _toy(L=48):
    rng = np.random.default_rng(0)
    price = 40 + 30 * np.sin(np.arange(L) / 6) + rng.normal(0, 3, L)
    b = Battery(P=500.0, CAP=2000.0, ETA=0.95, SD=0.0, POI=1e9, s_term=1000.0)
    avail = np.maximum(0, 300 * np.sin(np.arange(L) / 20))
    return b, price, avail, avail - 400.0, np.zeros(L)


def test_planner_without_peak_or_prox_is_unchanged():
    b, price, avail, base, commit = _toy()
    p0 = solve(b, 0, 1000.0, price, avail, base, commit, 0.25, 10.0)
    p1 = solve(b, 0, 1000.0, price, avail, base, commit, 0.25, 10.0, peak=np.zeros(len(price)), prox=None)
    assert np.allclose(p0.c, p1.c) and np.allclose(p0.d, p1.d) and p0.cost == pytest.approx(p1.cost)


def test_peak_price_moves_load_out_of_priced_steps_and_stays_out_of_cost():
    b, price, avail, base, commit = _toy()
    p0 = solve(b, 0, 1000.0, price, avail, base, commit, 0.25, 10.0)
    peak = np.zeros(len(price))
    hot = slice(30, 36)
    peak[hot] = 300.0
    p1 = solve(b, 0, 1000.0, price, avail, base, commit, 0.25, 10.0, peak=peak)
    load = lambda p: (p.c - p.d + p.x)[hot].sum()
    assert load(p1) < load(p0)
    assert p1.cost == pytest.approx(float(price @ (p1.c - p1.d + p1.x) * K + 10.0 * K * p1.d.sum()))


def test_prox_holds_the_plan_near_its_centre():
    b, price, avail, base, commit = _toy()
    p0 = solve(b, 0, 1000.0, price, avail, base, commit, 0.25, 10.0)
    x0 = p0.c - p0.d + p0.x
    peak = np.zeros(len(price))
    peak[30:36] = 300.0
    move = lambda rho: np.abs(np.diff([x0, (lambda p: p.c - p.d + p.x)(
        solve(b, 0, 1000.0, price, avail, base, commit, 0.25, 10.0, peak=peak, prox=(rho, x0, 0.05 * K)))], axis=0)).sum()
    assert move(1e-1) < 0.1 * move(1e-8)


def test_secants_overstate_the_square_by_at_most_4_3_percent_with_a_small_kink():
    R, rho, eps = 1500.0, 1e-5, 0.05 * K
    bp = prox_breakpoints(R, rho, eps)
    assert bp[0] == 0.0 and bp[-1] == R and np.all(np.diff(bp) > 0)
    assert rho / 2 * (bp[0] + bp[1]) <= eps + 1e-15           # slope of the first secant
    d = np.linspace(bp[1], R, 5000)
    pwl, sq = np.interp(d, bp, rho / 2 * bp ** 2), rho / 2 * d ** 2
    assert np.all(pwl >= sq - 1e-12) and np.max(pwl / sq) <= 1.0433


# ---- operator schedule ----
def test_price_rounds_run_at_every_day1_call_and_only_binding_calls_after():
    ts = pd.date_range("2023-01-02 06:00", periods=192, freq="15min", tz="UTC")
    iso = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in ts]
    op = GridOperator("OP", iso, "2023-01-04T06:00:00Z", ts.tz_convert("America/Chicago"), "run", "SYS", 1e5, 2.5e4,
                      500.0, rounds="info", nominations=True, idle_net=np.zeros(192), price_mode=True)
    steps = op.price_round_steps()
    assert steps == {3: (4, False), 7: (8, False), 15: (16, True), 59: (60, True)}


# ---- end to end ----
def test_price_mode_run_settles_replays_and_its_harm_components_add_up(tmp_path):
    from exchange.trace.replay import replay
    from sim.run_m1 import COMPONENTS, evaluate, harm_components, net_harm, run_variant
    from tests.test_replay import CFG_M11, synthetic_slice
    cfg = {**CFG_M11, "rounds": "info", "price_mode": True, "plan_to_nomination": True,
           "release_superseded_lock": True, "price_rounds_per_gate": 10}
    s = synthetic_slice()
    sf = run_variant(cfg, s, np.arange(2), tmp_path)
    r = run_variant(cfg, s, np.arange(2), tmp_path, headroom_mw=0.03)
    path = tmp_path / "exchange_h0.03_rinfo" / "trace.jsonl"
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert lines[0]["exchange"]["price_mode"] and lines[0]["m2a"]["price_mode"]
    assert replay(path)["match"]
    log = r["log"]
    types = {m["msg_type"] for m in log}
    assert {"peak_price", "peak_settle", "reference_net"} <= types and "post_need" not in types
    by_call = {}
    for m in log:
        if m["msg_type"] == "peak_price":
            by_call.setdefault(m["payload"]["call_id"], []).append(m["payload"])
    assert all(len(v) <= 10 and v[-1]["final"] for v in by_call.values())
    binding = [m["payload"]["call_id"] for m in log if m["msg_type"] == "nomination_aggregate"
               and m["payload"]["binding"]][-1]
    last = by_call[binding][-1]
    w0 = last["window"]["start_utc"]
    assert r["peak_settled"][w0][0] == pytest.approx(last["price_usd_mwh"][0])
    rec_net = s.system["system_load"] * 1000.0
    ev = evaluate(cfg, s, np.arange(2), r, rec_net, None, sf)
    selfish = evaluate(cfg, s, np.arange(2), sf, rec_net, None, sf)
    p_end = float(s.system["price_da"].mean()) / 1000.0
    alone = selfish["_agent_value"] + sf["SOC"][:, -1] * p_end
    with_ps = ev["_agent_value"] + r["SOC"][:, -1] * p_end
    nh = net_harm(alone, with_ps, ["A", "B"], r["header"]["roster"], 0)
    rows = ev["_components"]
    for row, a_, w_ in zip(rows, alone, with_ps):
        assert row["total"] == pytest.approx(a_ - w_, abs=0.02)
    assert "peak_price_net" in COMPONENTS
    peak_net = {m["to"]: m["payload"]["net_usd"] for m in log if m["msg_type"] == "peak_settle"}
    assert [row["peak_price_net"] for row in rows] == pytest.approx([-peak_net["A"], -peak_net["B"]])
    harm_components(rows, nh, alone)                          # raises if the parts don't add up
    pm = ev["price_mode"]
    assert pm["operator_net_payout_usd"] == pytest.approx(sum(peak_net.values()))
    assert pm["rounds_total"] == sum(len(v) for v in by_call.values())


# ---- D53 feasibility of the operator's target ----
def test_a_target_1_mw_below_the_lowest_reachable_peak_is_feasible_and_1_5_mw_below_is_not():
    from bench.h2h_feasibility import feasible
    from bench.m0 import load_bench, solve as central
    from bench.m0 import use_slice
    from tests.test_replay import synthetic_slice
    s = synthetic_slice()
    B = load_bench(s)
    use_slice(B, s)
    floor = float(central(B, peak=True)["z"][0])               # lowest reachable peak, kW
    target = np.full(B.T, floor)
    assert feasible(B, target - 1000.0)["feasible"]
    r = feasible(B, target - 1500.0)
    assert not r["feasible"] and r["max_short_mw"] == pytest.approx(0.5, abs=1e-3)
    target[:4] = np.nan                                        # before the first gate: no cap
    assert feasible(B, target - 1000.0)["feasible"]


def test_peak_price_cap_defaults_to_the_d54_placeholder_only_in_price_mode():
    from sim.run_m1 import PEAK_PRICE_CAP_USD_MWH, peak_price_cap
    assert PEAK_PRICE_CAP_USD_MWH == 100.0
    assert peak_price_cap({"price_cap_usd_mwh": 500}, True) == 100.0
    assert peak_price_cap({"price_cap_usd_mwh": 500, "peak_price_cap_usd_mwh": 500}, True) == 500.0   # recorded runs
    assert peak_price_cap({"price_cap_usd_mwh": 500}, False) == 500                                   # arm A unchanged


# ---- D56 rule 1p: the selfish reference, sent by the metering service per binding call ----
def _p1(**kw):
    h = P(settlement_reference="selfish_1p", **kw)
    cid = h.setup(binding=True)
    return h, cid


def _sref(h, cid, aid="A", kw=(-100.0, -20.0), window=WIN, sender="meter", t=ROUND_T):
    return h.send("selfish_reference", sender, {"asset_id": f"{aid}/bess", "call_id": cid, "window": window,
                                                "net_kw": list(kw)}, t=t)[1]


def test_settlement_reference_must_be_a_known_mode():
    with pytest.raises(ValueError):
        P(settlement_reference="declared")
    assert P().ex.config["settlement_reference"] == "recorded"


def test_selfish_reference_comes_only_from_metering_and_only_in_1p_mode():
    h, cid = _p1()
    assert code(_sref(h, cid, sender="A")) == "unauthorized_sender"
    plain = P()
    c2 = plain.setup(binding=True)
    assert code(_sref(plain, c2)) == "bad_message"


def test_selfish_reference_needs_a_binding_call_its_window_finite_values_and_is_never_revised():
    h = P(settlement_reference="selfish_1p")
    open_call = h.setup(binding=False)
    assert code(_sref(h, open_call)) == "bad_message"                         # not a binding call
    assert code(_sref(h, "nope")) == "bad_message"
    h, cid = _p1()
    assert code(_sref(h, cid, aid="Z")) == "bad_message"                      # unknown asset
    assert code(_sref(h, cid, kw=(-1.0,))) == "bad_message"                   # one value per step
    assert code(_sref(h, cid, kw=(-1.0, float("nan")))) == "bad_message"
    w = {"start_utc": STEPS[1], "end_utc": "2023-01-02T07:30:00Z"}
    assert code(_sref(h, cid, window=w, kw=(-1.0,))) == "bad_message"         # not the call's window
    assert _sref(h, cid) == []
    assert code(_sref(h, cid, kw=(-1.0, -1.0))) == "bad_message"              # no revisions
    assert h.ex.call_reference[cid]["A/bess"] == {STEPS[0]: -100.0, STEPS[1]: -20.0}


def test_selfish_reference_closes_at_the_call_deadline():
    h, cid = _p1()
    assert code(_sref(h, cid, t=DEADLINE)) == "after_gate_closure"


def test_1p_settles_each_step_on_the_reference_of_the_call_that_settled_it():
    h, cid = _p1()
    for aid, kw in (("A", (-100.0, -20.0)), ("B", (-10.0, -10.0))):
        assert _sref(h, cid, aid=aid, kw=kw) == []
    h.send("reference_net", "meter", {"asset_id": "A/bess", "window": WIN, "net_kw": [-999.0, -999.0]})   # unused
    for aid in ("A", "B"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    first = h.round(1)[0]["payload"]["price_usd_mwh"]
    h.tick(DEADLINE)
    w2 = {"start_utc": STEPS[1], "end_utc": "2023-01-02T07:30:00Z"}
    cid2, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": w2, "deadline_utc": STEPS[1],
                                                "binding": True}, t=DEADLINE)
    for aid, kw in (("A", (-50.0,)), ("B", (-5.0,))):
        assert _sref(h, cid2, aid=aid, kw=kw, window=w2, t="2023-01-02T07:05:00Z") == []
    for aid in ("A", "B"):
        h.nominate(aid, cid2, [-90.0], window=w2, t=DEADLINE)
    second = h.round(1, t="2023-01-02T07:05:00Z")[0]["payload"]["price_usd_mwh"]
    h.tick(STEPS[1])
    for aid in ("A", "B"):
        h.meter_net(aid, STEPS[0], -70.0)
        h.meter_net(aid, STEPS[1], -80.0)
    out = {m["to"]: m["payload"] for m in h.send("tick", "clock", {"final": True}, t="2023-01-02T07:30:00Z")[1]
           if m["msg_type"] == "peak_settle"}
    p = [first[0], second[0]]
    assert p[0] > 0 and p[1] > 0
    for aid, ref in (("A", (-100.0, -50.0)), ("B", (-10.0, -5.0))):
        assert out[aid]["refund_usd"] == pytest.approx(sum(pi * -r * K for pi, r in zip(p, ref)))
        assert out[aid]["charge_usd"] == pytest.approx(sum(pi * x * K for pi, x in zip(p, (70.0, 80.0))))


def test_1p_settlement_fails_closed_without_a_reference_at_a_priced_step():
    h, cid = _p1()
    assert _sref(h, cid, aid="A") == []                                      # B has none
    for aid in ("A", "B"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    assert h.round(1)[0]["payload"]["price_usd_mwh"][0] > 0
    h.tick(DEADLINE)
    with pytest.raises(RuntimeError, match="no selfish_reference for B/bess"):
        h.send("tick", "clock", {"final": True}, t="2023-01-02T07:30:00Z")


def _1p_with_late_registrant(late: bool, flag: bool, register_t=None):
    """A and B registered with 1p references; one binding call, priced. With late, C registers (attested, first time
    in the run) only after the call's gate has closed by the exchange's clock, so it has no 1p reference for that
    call and never could; C's sender time is register_t (default the gate)."""
    h, cid = _p1(delivery_record=flag)
    for aid, kw in (("A", (-100.0, -20.0)), ("B", (-10.0, -10.0))):
        assert _sref(h, cid, aid=aid, kw=kw) == []
    for aid in ("A", "B"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    assert h.round(1)[0]["payload"]["price_usd_mwh"][0] > 0
    h.tick(DEADLINE)
    if late:
        h.attest("C", t=DEADLINE)
        assert h.send("register", "C", {"agent_id": "C", "legal_entity": "C LLC",
                                        "assets": [{"asset_id": "C/bess", "zone": "SYS", "type": "bess"}],
                                        "mandate": {"max_kw": 100.0, "price_floor": 0.0}},
                      t=register_t or DEADLINE)[1] == []
    for aid in ("A", "B") + (("C",) if late else ()):
        for t in STEPS:
            h.meter_net(aid, t, -70.0)
    return {m["to"]: m["payload"] for m in h.send("tick", "clock", {"final": True}, t="2023-01-02T07:30:00Z")[1]
            if m["msg_type"] == "peak_settle"}


@pytest.mark.parametrize("flag", [False, True])
def test_1p_an_agent_registered_after_a_gate_is_not_settled_on_that_call_and_blocks_no_one(flag):
    """Owner, Nov 3 (item 4). An agent registering for the first time after a binding call's gate has no 1p
    reference for that call and never could (references close at the gate). It is not settled on that call's steps,
    and everyone else is settled exactly as without it, with delivery_record off or on. Before the fix the whole
    settlement raised "D56: no selfish_reference" for every agent."""
    base = _1p_with_late_registrant(False, flag)
    late = _1p_with_late_registrant(True, flag)
    assert late["C"]["steps"] == 0 and late["C"]["charge_usd"] == 0.0 and late["C"]["refund_usd"] == 0.0
    assert {k: v for k, v in late.items() if k != "C"} == base
    backdated = _1p_with_late_registrant(True, flag, register_t="2023-01-02T06:00:00Z")   # sender time: before the gate
    assert backdated == late                                   # the exchange's clock decides, not the sender's t_utc


def _1p_swap_with_a_gate_while_unheld(take_over: bool):
    """delivery_record on. A and B hold their assets with 1p references for call 1 (Jan 2). A deregisters from Jan 3.
    Call 2 (Jan 3) gates while A/bess has no holder, so no reference for A/bess on call 2 can exist (the metering
    service's reference for an unheld asset is refused). With take_over, Y (first registration in the run) takes
    A/bess over after call 2's gate, by the exchange's clock."""
    h, cid = _p1(delivery_record=True)
    for aid, kw in (("A", (-100.0, -20.0)), ("B", (-10.0, -10.0))):
        assert _sref(h, cid, aid=aid, kw=kw) == []
    for aid in ("A", "B"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    assert h.round(1)[0]["payload"]["price_usd_mwh"][0] > 0
    assert h.send("deregister", "A", {"agent_id": "A", "effective_date": "2023-01-03"}, t=ROUND_T)[1] == []
    h.tick(DEADLINE)
    for aid in ("A", "B"):
        for t in STEPS:
            h.meter_net(aid, t, -70.0)
    h.tick("2023-01-03T06:00:00Z")                                    # Jan 3 00:00 Central: A is out
    w2 = {"start_utc": "2023-01-03T08:00:00Z", "end_utc": "2023-01-03T08:30:00Z"}
    d2 = "2023-01-03T07:00:00Z"
    h.target(100.0, window=w2)
    cid2, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": w2, "deadline_utc": d2, "binding": True},
                     t="2023-01-03T06:00:00Z")
    assert code(_sref(h, cid2, aid="A", window=w2, t="2023-01-03T06:30:00Z")) == "bad_message"   # unheld asset
    assert _sref(h, cid2, aid="B", kw=(-10.0, -10.0), window=w2, t="2023-01-03T06:30:00Z") == []
    h.nominate("B", cid2, [-200.0, -200.0], window=w2, t="2023-01-03T06:30:00Z")
    assert h.round(1, t="2023-01-03T06:45:00Z")[0]["payload"]["price_usd_mwh"][0] > 0
    h.tick(d2)
    if take_over:
        h.attest("A", owner="A LLC", agent="Y", t=d2)               # a software swap (D73)
        assert h.send("register", "Y", {"agent_id": "Y", "legal_entity": "A LLC",
                                        "assets": [{"asset_id": "A/bess", "zone": "SYS", "type": "bess"}],
                                        "mandate": {"max_kw": 100.0, "price_floor": 0.0}}, t=d2)[1] == []
    for t in ("2023-01-03T08:00:00Z", "2023-01-03T08:15:00Z"):
        h.meter_net("B", t, -70.0)
        if take_over:
            h.meter_net("A", t, -70.0)
    return {m["to"]: m["payload"] for m in h.send("tick", "clock", {"final": True}, t="2023-01-03T08:30:00Z")[1]
            if m["msg_type"] == "peak_settle"}


def test_1p_an_asset_taken_over_after_a_gate_it_passed_unheld_is_not_settled_on_that_call():
    """Review finding (Nov 3): the exemption follows the asset, not its first registration. Y takes over an asset
    that had no holder at call 2's gate: it is not settled on call 2, and A, B are settled exactly as without Y."""
    base = _1p_swap_with_a_gate_while_unheld(False)
    late = _1p_swap_with_a_gate_while_unheld(True)
    assert late["Y"]["steps"] == 0 and late["Y"]["charge_usd"] == 0.0 and late["Y"]["refund_usd"] == 0.0
    assert {k: v for k, v in late.items() if k != "Y"} == base
    assert base["A"]["steps"] == 2 and base["B"]["steps"] == 4


def test_1p_an_agent_registered_before_the_gate_without_a_reference_still_fails_closed():
    """The fix excludes only agents registered after the gate: one registered before it with no reference still
    stops the settlement (D56 fails closed)."""
    h, cid = _p1()
    assert _sref(h, cid, aid="A") == []
    h.attest("C")
    h.send("register", "C", {"agent_id": "C", "legal_entity": "C LLC",
                             "assets": [{"asset_id": "C/bess", "zone": "SYS", "type": "bess"}],
                             "mandate": {"max_kw": 100.0, "price_floor": 0.0}}, t=ROUND_T)
    assert _sref(h, cid, aid="B") == []
    for aid in ("A", "B", "C"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    assert h.round(1)[0]["payload"]["price_usd_mwh"][0] > 0
    h.tick(DEADLINE)
    with pytest.raises(RuntimeError, match="no selfish_reference for C/bess"):
        h.send("tick", "clock", {"final": True}, t="2023-01-02T07:30:00Z")


def test_1p_run_sends_the_metering_reference_settles_on_it_and_replays(tmp_path):
    from exchange.trace.replay import replay
    from sim import physics
    from sim.forecasts import PriceForecast
    from sim.reference import selfish_site_net
    from sim.run_m1 import run_variant
    from tests.test_replay import CFG_M11, synthetic_slice
    cfg = {**CFG_M11, "rounds": "info", "price_mode": True, "plan_to_nomination": True,
           "release_superseded_lock": True, "price_rounds_per_gate": 10, "settlement_reference": "selfish_1p"}
    s = synthetic_slice()
    part = np.arange(2)
    r = run_variant(cfg, s, part, tmp_path, headroom_mw=0.03)
    path = tmp_path / "exchange_h0.03_rinfo" / "trace.jsonl"
    assert json.loads(path.read_text().splitlines()[0])["exchange"]["settlement_reference"] == "selfish_1p"
    assert replay(path)["match"]
    # bus.log keeps nominate and telemetry without payload (fix 1); the trace has every message in full
    from exchange.trace.logger import line_to_msg
    log = [line_to_msg(json.loads(x)) for x in path.read_text().splitlines()[1:]]
    iso = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in s.ts_utc]
    binding = {m["payload"]["call_id"] for m in log if m["msg_type"] == "nomination_aggregate"
               and m["payload"]["binding"]}
    refs = [m for m in log if m["msg_type"] == "selfish_reference"]
    assert refs and {m["payload"]["call_id"] for m in refs} <= binding
    assert all(m["sender"] == "metering" for m in refs) and not [m for m in log if m["msg_type"] == "reject"]
    # each reference is the shared 1p plan from registration and public data, priced as published at its tick
    fleet = physics.Fleet.from_slice(s, part)
    a = s.agent
    pv, curt, load = np.nan_to_num(a["pv"][part]), a["curt"][part], a["load"][part]
    prices = PriceForecast(s.ts_utc, s.system["price_da"], "America/Chicago", cfg["price_publish_hour_local"])
    reg = {m["sender"]: m["payload"]["mandate"]["max_kw"] for m in log if m["msg_type"] == "register"}
    ids = [str(x) for x in s.ids[part]]
    q_by = {}
    for m in refs:
        j = ids.index(m["payload"]["asset_id"].split("/")[0])
        q = selfish_site_net(reg[ids[j]], 0.8 * fleet.CAP[j], fleet.S0[j], prices.at(iso.index(m["t_utc"]))[1],
                             pv[j] - curt[j], pv[j] - curt[j] - load[j], cfg["wear_usd_mwh"], 0.25)
        a0 = iso.index(m["payload"]["window"]["start_utc"])
        assert m["payload"]["net_kw"] == pytest.approx(list(q[a0:a0 + len(m["payload"]["net_kw"])]))
        q_by.setdefault(m["payload"]["call_id"], {})[ids[j]] = dict(zip(iso[a0:], m["payload"]["net_kw"]))
    net = {}
    for m in log:
        if m["msg_type"] == "telemetry" and "net_kw" in m["payload"]:
            net.setdefault(m["payload"]["asset_id"].split("/")[0], {})[m["t_utc"]] = m["payload"]["net_kw"]
    settle = {m["to"]: m["payload"] for m in log if m["msg_type"] == "peak_settle"}
    assert set(settle) == set(ids) and any(p > 1e-9 for p, _ in r["peak_settled"].values())
    for aid in ids:
        want = sum(p * (net[aid].get(t, 0.0) - q_by[cid][aid][t]) * K
                   for t, (p, cid) in r["peak_settled"].items() if p > 1e-9)
        assert settle[aid]["net_usd"] == pytest.approx(want, abs=1e-9)
