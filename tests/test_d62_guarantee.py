"""D62 guarantee (docs/plans/d62_guarantee_report.md): modeled paths, shortfall, response measure, ledger, the
exchange's messages and accrual, and the offline tool against the run. Synthetic data only."""
import json

import numpy as np
import pytest

from agents.planner import TERMINAL_USD_KWH, Battery, solve
from bench import d62_guarantee as T62
from bench.d56_resettle import CheckFailed
from exchange import guarantee as G
from exchange import messages as M
from sim import reference as R
from sim.reference import ModeledPaths, objective_bracket, selfish_plan, selfish_site_net

DT = 0.25
K = DT / 1000.0
N = 32


def flat(v=0.0, n=N):
    return np.full(n, float(v))


def two_peaks():
    """DA price with a cheap night and two evening-ish humps; one agent with no PV or load."""
    t = np.arange(N)
    return 20.0 + 15.0 * np.exp(-((t - 10) ** 2) / 6) + 18.0 * np.exp(-((t - 26) ** 2) / 6)


def paths(price, peak1, k1, peak2=None, k2=None, s0=50.0, kw=20.0, usable=80.0):
    mp = ModeledPaths(kw, usable, s0, flat(), flat(), 10.0, DT)
    q1 = selfish_plan(kw, usable, s0, price, flat(), flat(), 10.0, DT)
    mp.stage(k1, price, peak1, q1)
    if k2 is not None:
        mp.stage(k2, price, peak2)
    return mp, q1


def window_peak(lo, hi, v):
    p = flat()
    p[lo:hi] = v
    return p


# ---- sim/reference.py ----
def test_selfish_site_net_is_unchanged():
    """The pre-D62 body, inline: the refactor through selfish_plan gives the same array."""
    price, s0 = two_peaks(), 40.0
    b = Battery(P=20.0, CAP=80.0 / 0.8, ETA=R.ETA_REF, SD=R.SD_REF, POI=1e12, s_term=s0)
    pl = solve(b, 0, s0, price, flat(), flat(), np.zeros(N), DT, 10.0)
    old = flat() + pl.d - pl.c - pl.x
    assert np.array_equal(selfish_site_net(20.0, 80.0, s0, price, flat(), flat(), 10.0, DT), old)


def test_p_and_q_get_one_input_bundle(monkeypatch):
    """D64 structural check: 1p's Q and D62's P* and Q* stages are planned with identical power and energy inputs."""
    seen = []
    real = R.solve

    def spy(b, *a, **kw):
        seen.append(b)
        return real(b, *a, **kw)
    monkeypatch.setattr(R, "solve", spy)
    paths(two_peaks(), window_peak(8, 20, 80.0), 8, window_peak(20, N, 80.0), 20)
    assert len(seen) >= 4 and len({(b.P, b.CAP, b.ETA, b.SD, b.POI, b.s_term) for b in seen}) == 1


def test_one_gate_toy_rt_equal_to_forecast_has_no_shortfall():
    price = two_peaks()
    peak = window_peak(8, N, 0.0)
    peak[14:18] = 60.0
    mp, q1 = paths(price, peak, 8)
    assert np.allclose(mp.Q["s"], q1.s)                     # Q* is Q_1 with one gate
    q_ref = mp.net(mp.Q)                                    # 1p's reference on its steps is Q_1
    sp = G.spans([8], N)
    s = G.span_shortfall(price, peak, q_ref, mp.net(mp.P), mp.P["d"], mp.net(mp.Q), mp.Q["d"], mp.P["s"][-1],
                         mp.Q["s"][-1], 10.0, 0.0, sp, K)
    assert abs(mp.P["s"][-1] - mp.Q["s"][-1]) < 1e-6         # both hold the soft end target
    assert np.abs(mp.net(mp.P) - mp.net(mp.Q)).max() > 1.0   # the peak price moved P*
    assert sum(s) <= 1e-6


def test_two_gates_one_continuous_path_and_day_two_counted_once():
    price = two_peaks()
    p1, p2 = window_peak(8, 20, 0.0), window_peak(20, N, 0.0)
    p1[14:18], p2[21:24] = 50.0, 70.0
    mp, q1 = paths(price, p1, 8, p2, 20)
    for path in (mp.P, mp.Q):                    # physical SOC recursion across the stage boundary (no jump)
        s_prev = np.concatenate([[50.0], path["s"][:-1]])
        assert np.allclose(path["s"], s_prev + R.ETA_REF * path["c"] * DT - path["d"] * DT / R.ETA_REF, atol=1e-6)
    assert np.allclose(mp.P["s"][:8], q1.s[:8]) and np.allclose(mp.Q["s"][:20], q1.s[:20])
    sp = G.spans([8, 20], N)
    assert sp == [(8, 20), (20, N)]
    rt = price + 5.0 * np.sin(np.arange(N))
    q_ref = np.where(np.arange(N) < 20, mp.net(mp.Q), mp.net(mp.Q) + 1.0)    # 1p's Q_2 differs from Q* on span 2
    pk = p1 + p2
    args = (rt, pk, q_ref, mp.net(mp.P), mp.P["d"], mp.net(mp.Q), mp.Q["d"], mp.P["s"][-1], mp.Q["s"][-1], 10.0, 0.03)
    by_span = G.span_shortfall(*args, sp, K)
    whole = G.span_shortfall(*args, [(8, N)], K)            # one span over [d1, end): each step counted once
    assert sum(by_span) == pytest.approx(whole[0], abs=1e-9)


def test_stage_one_identity_in_the_owners_exact_form():
    """[v1 energy + wear + terminal](P) - p1 (net_P - net_Q1) K <= [v1 energy + wear + terminal](Q_1) on [d1, end);
    the peak price enters once, as the settlement payment."""
    price = two_peaks()
    peak = window_peak(8, N, 0.0)
    peak[14:18] = 60.0
    mp, q1 = paths(price, peak, 8)
    _, pl, k0, _ = mp.stage1
    sl = slice(k0, None)
    b_p = objective_bracket(price[sl], pl.c, pl.d, pl.x, pl.s[-1], 50.0, 10.0, DT)
    b_q = objective_bracket(price[sl], q1.c[sl], q1.d[sl], q1.x[sl], q1.s[-1], 50.0, 10.0, DT)
    settle = float(peak[sl] @ ((pl.d - pl.c - pl.x) - (q1.d[sl] - q1.c[sl] - q1.x[sl]))) * K
    assert b_p - settle <= b_q + 0.01
    assert mp.stage1_identity(peak) == pytest.approx(b_p - settle - b_q, abs=1e-12)
    # the bracket has no peak price in it: adding the peak to P's energy cost as well would double-count it
    assert objective_bracket(price[sl] + peak[sl], pl.c, pl.d, pl.x, pl.s[-1], 50.0, 10.0, DT) != pytest.approx(b_p)


def test_stage_one_identity_holds_when_the_end_target_term_binds():
    price = two_peaks()
    peak = window_peak(8, N, 0.0)
    peak[28:32] = 40_000.0                    # $40/kWh at the very end: P* sells below its soft end target
    mp, _ = paths(price, peak, 8, s0=90.0)    # starts full (s_max), so it can't charge ahead to cover the sale
    _, pl, _, _ = mp.stage1
    assert TERMINAL_USD_KWH * max(0.0, 90.0 - pl.s[-1]) > 1.0
    assert mp.stage1_identity(peak) <= 0.01


def test_stages_must_move_forward_and_only_the_first_takes_q1():
    price = two_peaks()
    mp, q1 = paths(price, flat(), 8)
    with pytest.raises(ValueError):
        mp.stage(8, price, flat())
    with pytest.raises(ValueError):
        mp.stage(12, price, flat(), q1)
    with pytest.raises(ValueError):
        ModeledPaths(20.0, 80.0, 50.0, flat(), flat(), 10.0, DT).stage(4, price, flat())


# ---- exchange/guarantee.py ----
def test_response_measure_and_share():
    p = np.array([0.0, 10.0, 10.0, 0.0])
    q = np.zeros(4)
    P = np.array([5.0, 4.0, 2.0, 9.0])
    shift, got = G.response_kwh(p, q, P, np.array([9.0, 8.0, -1.0, 9.0]), 1.0)
    assert (shift, got) == (6.0, 4.0)                 # unpriced steps don't count; delivery capped at the shift
    assert G.share(shift, got) == pytest.approx(4 / 6) and G.share(shift, got) <= 1
    assert G.share(0.999, 0.0) is None and G.eligible(None, 0.9)          # under 1 kWh: 0/0, eligible
    assert G.share(1.0, 0.0) == 0.0 and not G.eligible(0.0, 0.5)


def test_separation_leaves_0_0_out_and_fails_closed_on_overlap():
    ok = G.separation({1: 0.9, 2: 0.8, 3: None}, {1: 0.1, 2: None, 3: 0.3})
    assert ok["pass"] and ok["threshold"] == pytest.approx(0.55)
    assert ok["excluded_0_0"] == {"responders": 1, "non_responders": 1}
    bad = G.separation({1: 0.9, 2: 0.2}, {1: 0.1, 2: 0.4})
    assert not bad["pass"] and bad["threshold"] is None
    assert bad["overlap"] == {"responders": {"2": 0.2}, "non_responders": {"2": 0.4}}


def test_ledger_gains_count_losses_dont_and_clawback_never_exceeds_paid():
    l1 = G.ledger([(2023, 1, 100.0, True), (2023, 2, -30.0, True), (2023, 3, 50.0, False), (2023, 4, -200.0, True)])
    rows = l1["by_quarter"]
    assert [r["paid_usd"] for r in rows] == [100.0, 0.0, 0.0, 0.0]
    assert [r["clawed_back_usd"] for r in rows] == [0.0, 30.0, 0.0, 70.0]        # never more than was paid
    assert l1["net_usd"] == 0.0 and l1["max_outstanding_usd"] == 100.0
    l2 = G.ledger([(2023, 1, 100.0, True), (2023, 2, -30.0, False)])            # an ineligible gain still counts
    assert l2["net_usd"] == 70.0
    l3 = G.ledger([(2023, 4, 40.0, True), (2024, 1, -10.0, True)])              # the year resets
    assert l3["gross_paid_usd"] == 40.0 and l3["clawed_back_usd"] == 0.0


def test_spans_reject_bad_deadlines():
    for bad in ([], [5, 5], [8, 4], [40]):
        with pytest.raises(ValueError):
            G.spans(bad, N)


# ---- the run and the exchange ----
def _cfg(**kw):
    from tests.test_d58_margin import _cfg as base
    return base(**{"nomination_span": "horizon", **kw})         # D86 flip (Nov 6): D62 refuses any other span


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    from sim.run_m1 import run_variant
    from tests.test_replay import synthetic_slice
    d = tmp_path_factory.mktemp("d62")
    s = synthetic_slice()
    on = run_variant(_cfg(d62_guarantee=True), s, np.arange(2), d / "on", headroom_mw=0.03)
    off = run_variant(_cfg(), s, np.arange(2), d / "off", headroom_mw=0.03)
    sf = run_variant(_cfg(d62_guarantee=True), s, np.arange(2), d / "on")
    return {"on": on, "off": off, "sf": sf, "s": s, "dir": d}


def test_flag_off_run_is_unchanged(tmp_path, runs):
    from sim.run_m1 import run_variant
    r = run_variant(_cfg(d62_guarantee=False), runs["s"], np.arange(2), tmp_path, headroom_mw=0.03)
    assert r["log"] == runs["off"]["log"] and "guarantee" not in r["header"]["exchange"]
    # bus.log keeps nominate and telemetry as envelope only (fix 1), so compare the traces too
    body = lambda d: (d / "exchange_h0.03_rinfo" / "trace.jsonl").read_text().splitlines()[1:]    # noqa: E731
    assert body(tmp_path) == body(runs["dir"] / "off")
    for f in ("C", "D", "X", "SOC"):
        assert np.array_equal(r[f], runs["off"][f])


def test_flag_on_changes_nothing_but_adds_d62_messages(runs):
    strip = lambda log: [{k: v for k, v in m.items() if k != "msg_id"} for m in log
                         if m["msg_type"] not in T62.NEW_TYPES]
    assert strip(runs["on"]["log"]) == strip(runs["off"]["log"])
    for f in ("C", "D", "X", "SOC"):
        assert np.array_equal(runs["on"][f], runs["off"][f])
    types = [m["msg_type"] for m in runs["on"]["log"]]
    assert types.count("rt_price") == 1 and types.count("guarantee_accrual") == 2
    assert types.count("modeled_response") == types.count("selfish_reference") > 0      # one per asset per call
    assert runs["on"]["header"]["d62_guarantee"] is True


def test_trace_replays_exactly_with_the_flag_on(runs):
    from exchange.trace.replay import replay
    assert replay(runs["dir"] / "on" / "exchange_h0.03_rinfo" / "trace.jsonl")["match"]


def _synthetic_inputs(runs, run="test"):
    cfg = {**_cfg(d62_guarantee=True), "slice_tz": "America/Chicago"}
    log = runs["on"]["price_log"]
    d = runs["dir"] / "on"
    return T62.assemble(run, list(runs["on"]["header"]["roster"]), cfg, runs["s"],
                        d / "exchange_h0.03_rinfo" / "trace.jsonl", d / "selfish" / "trace.jsonl", log)


def test_offline_tool_equals_the_in_run_accrual(runs):
    d = _synthetic_inputs(runs)
    res = T62.compute(d)
    ck = T62.checks(d, res)
    assert ck["q_vs_in_run_reference_kw"] <= T62.PATH_TOL_KW
    m = T62.inrun_vs_tool(d, res)
    assert m["match"], m
    again = T62.strip(T62.compute(d))
    assert json.dumps(again, default=float) == json.dumps(T62.strip(res), default=float)


def test_a_missing_agent_in_the_accruals_fails_closed(runs):
    d = _synthetic_inputs(runs)
    res = T62.compute(d)
    d["accrual"].pop(d["roster"][0])
    with pytest.raises(CheckFailed):
        T62.inrun_vs_tool(d, res)


def test_accrual_is_invariant_to_the_agents_own_meter_except_the_response_measure(runs):
    """Replaying the run's exchange inputs with agent A's telemetry raised by 30 kW: A's shortfall is unchanged
    (no marginal price is zeroed), only its delivered kWh moves."""
    from exchange.core import Exchange
    from exchange.trace.logger import line_to_msg
    path = runs["dir"] / "on" / "exchange_h0.03_rinfo" / "trace.jsonl"
    lines = [json.loads(x) for x in path.read_text().splitlines()]

    def accruals(bump):
        ex = Exchange(**lines[0]["exchange"])
        out = {}
        for e in lines[1:]:
            if e["to"] != "exchange":
                continue
            m = line_to_msg(e)
            if bump and m["msg_type"] == "telemetry" and m["payload"]["asset_id"].startswith("A/"):
                m["payload"] = {**m["payload"], "net_kw": m["payload"]["net_kw"] + 30.0}
            for r in ex.handle(M.encode(m)):
                r = M.decode(r)
                if r["msg_type"] == "guarantee_accrual":
                    out[r["to"]] = r["payload"]
        return out
    a0, a1 = accruals(False), accruals(True)
    assert a1["A"]["shortfall_usd"] == a0["A"]["shortfall_usd"]
    assert a1["A"]["modeled_shift_kwh"] == a0["A"]["modeled_shift_kwh"]
    assert a1["B"] == a0["B"]


def _exchange_at_final_price(runs):
    """A fresh exchange fed the 'on' trace up to (not including) the first modeled_response."""
    from exchange.core import Exchange
    from exchange.trace.logger import line_to_msg
    path = runs["dir"] / "on" / "exchange_h0.03_rinfo" / "trace.jsonl"
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    ex = Exchange(**lines[0]["exchange"])
    first = None
    for e in lines[1:]:
        if e["to"] != "exchange":
            continue
        if e["msg_type"] == "modeled_response":
            first = line_to_msg(e)
            break
        ex.handle(M.encode(line_to_msg(e)))
    return ex, first


def _code(ex, msg):
    out = [M.decode(r) for r in ex.handle(M.encode(msg))]
    return out[0]["payload"]["code"] if out and out[0]["msg_type"] == "reject" else None


def test_exchange_rejects_bad_d62_messages(runs):
    ex, good = _exchange_at_final_price(runs)
    assert _code(ex, {**good, "sender": "GRID_OP"}) == "unauthorized_sender"
    assert _code(ex, {**good, "payload": {**good["payload"], "call_id": "nope"}}) == "bad_message"
    assert _code(ex, {**good, "payload": {**good["payload"], "p_net_kw": good["payload"]["p_net_kw"][:-1]}}) \
        == "bad_message"
    assert _code(ex, good) is None
    assert _code(ex, good) == "bad_message"                                   # never revised
    rt = M.make("rt_price", "metering", "exchange", good["t_utc"],
                {"window": {"start_utc": "2023-01-02T06:00:00Z", "end_utc": "2023-01-03T06:00:00Z"},
                 "rt_usd_mwh": [1.0] * 96, "end_energy_usd_kwh": 0.02}, "m:x", good["run_id"])
    assert _code(ex, {**rt, "sender": "clock"}) == "unauthorized_sender"
    assert _code(ex, rt) is None and _code(ex, rt) == "bad_message"         # once
    # a guarantee_accrual from anyone but the exchange is refused
    acc = M.make("guarantee_accrual", "A", "exchange", good["t_utc"], {"window": rt["payload"]["window"],
                 "calls": [], "shortfall_usd_by_span": [], "shortfall_usd": 0.0, "modeled_shift_kwh": 0.0,
                 "delivered_kwh": 0.0}, "A:x", good["run_id"])
    assert _code(ex, acc) == "unauthorized_sender"


def test_exchange_rejects_d62_messages_with_the_flag_off(runs):
    from exchange.core import Exchange
    _, good = _exchange_at_final_price(runs)
    hdr = json.loads((runs["dir"] / "off" / "exchange_h0.03_rinfo" / "trace.jsonl").read_text().splitlines()[0])
    assert _code(Exchange(**hdr["exchange"]), good) == "bad_message"
    with pytest.raises(ValueError, match="D62"):
        Exchange("r", "op", "metering", "clock", ["SYS"], guarantee=True)


def test_exchange_rejects_a_modeled_response_before_the_calls_final_price(runs):
    """Fed the run up to the binding call's first price round (its 1p references are in, no final price yet)."""
    from exchange.core import Exchange
    from exchange.trace.logger import line_to_msg
    _, good = _exchange_at_final_price(runs)
    lines = [json.loads(x) for x in (runs["dir"] / "on" / "exchange_h0.03_rinfo" / "trace.jsonl").read_text()
             .splitlines()]
    ex = Exchange(**lines[0]["exchange"])
    seen_ref = False
    for e in lines[1:]:
        if e["to"] != "exchange":
            continue
        seen_ref = seen_ref or e["msg_type"] == "selfish_reference"
        if seen_ref and e["msg_type"] == "tick" and e["payload"].get("price_round"):
            break
        ex.handle(M.encode(line_to_msg(e)))
    out = [M.decode(r) for r in ex.handle(M.encode(good))]
    assert out[0]["msg_type"] == "reject" and "no final price" in out[0]["payload"]["detail"]


def test_ledger_accounting_check():
    led = {"A": G.ledger([(2023, 1, 50.0, True), (2023, 3, -20.0, True)]),
           "B": G.ledger([(2023, 1, -5.0, True), (2023, 4, 12.0, False)])}
    fleet = T62.fleet_ledger(led)
    assert T62.check_ledgers(led, fleet)["pass"]
    assert fleet["gross_paid_usd"] == 50.0 and fleet["net_usd"] == 30.0
    with pytest.raises(CheckFailed):
        T62.check_ledgers(led, {**fleet, "gross_paid_usd": fleet["gross_paid_usd"] + 1.0})


def test_d62_needs_1p_in_the_run(tmp_path):
    from sim.run_m1 import run_variant
    from tests.test_replay import synthetic_slice
    with pytest.raises(ValueError, match="D62"):
        run_variant(_cfg(d62_guarantee=True, settlement_reference="recorded"), synthetic_slice(), np.arange(2),
                    tmp_path, headroom_mw=0.03)


# ---- the reproduction comparator ----
def test_reproduction_comparator_ignores_ids_and_new_types_but_catches_a_changed_message(runs, tmp_path):
    a = T62.trace_events(runs["dir"] / "on" / "exchange_h0.03_rinfo" / "trace.jsonl")
    b = T62.trace_events(runs["dir"] / "off" / "exchange_h0.03_rinfo" / "trace.jsonl")
    assert T62.json_diff(a, b, set()) == []
    for typ, key in (("telemetry", "net_kw"), ("nominate", "kw")):
        i = next(i for i, m in enumerate(b) if m["msg_type"] == typ)
        c = json.loads(json.dumps(b))
        v = c[i]["payload"][key]
        c[i]["payload"][key] = [x + 1.0 for x in v] if isinstance(v, list) else v + 1.0
        assert T62.json_diff(a, c, set()), typ
    s1 = {"x": 1, "messages_total": 5, "guarantee": {"y": 2}, "runtime_s": 3.0}
    s0 = {"x": 1, "messages_total": 4, "runtime_s": 9.0}
    assert T62.json_diff(s1, s0, T62.SUMMARY_IGNORE) == []
    assert T62.json_diff({**s1, "x": 2}, s0, T62.SUMMARY_IGNORE) == ["/x"]


def test_flag_on_summary_matches_outside_the_ignored_fields(runs):
    from sim.run_m1 import evaluate
    s, part = runs["s"], np.arange(2)
    rec = s.system["system_load"] * 1000.0

    def clean(ev):
        return json.loads(json.dumps({k: v for k, v in ev.items() if not k.startswith("_")}, default=float))
    on = clean(evaluate(_cfg(d62_guarantee=True), s, part, runs["on"], rec, None, runs["sf"]))
    off = clean(evaluate(_cfg(), s, part, runs["off"], rec, None, runs["sf"]))
    assert "guarantee" in on and on["messages_total"] > off["messages_total"]
    assert T62.json_diff(on, off, T62.SUMMARY_IGNORE) == []


def test_inrun_check_compares_each_stage_over_its_own_span_with_two_gates():
    """Pre-registration v2: with two binding calls, stage 1's message holds its own plan after d2, which stage 2
    re-plans. The check must pass on a correct run and still catch a difference inside a stage's own span."""
    price = two_peaks()
    p1, p2 = window_peak(8, 20, 0.0), window_peak(20, N, 0.0)
    p1[14:18], p2[21:24] = 50.0, 70.0
    mp = ModeledPaths(20.0, 80.0, 50.0, flat(), flat(), 10.0, DT)
    q1 = selfish_plan(20.0, 80.0, 50.0, price, flat(), flat(), 10.0, DT)
    st1 = {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in mp.stage(8, price, p1, q1).items()}
    st2 = {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in mp.stage(20, price, p2).items()}
    arr = {"P_net": mp.net(mp.P), "P_dis": mp.P["d"], "Q_net": mp.net(mp.Q), "Q_dis": mp.Q["d"]}
    tail = np.abs(np.asarray(st1["p_net_kw"])[12:] - arr["P_net"][20:]).max()
    assert tail > 1.0                                   # stage 1's tail after d2 differs from the final P* (by design)
    acc = {"shortfall_usd": 1.0, "shortfall_usd_by_span": [0.4, 0.6], "modeled_shift_kwh": 2.0, "delivered_kwh": 1.0}
    res = {"spans": [(8, 20), (20, N)], "arrays": {"A": arr},
           "agents": [{"agent": "A", "shortfall_usd": 1.0, "shortfall_by_span_usd": [0.4, 0.6],
                       "modeled_shift_kwh": 2.0, "delivered_kwh": 1.0}]}
    d = {"run": "toy", "roster": ["A"], "gates": [{"call_id": "c1"}, {"call_id": "c2"}],
         "modeled": {"c1": {"A": st1}, "c2": {"A": st2}}, "accrual": {"A": acc}}
    assert T62.inrun_vs_tool(d, res)["match"]
    bad = json.loads(json.dumps(st1))
    bad["q_net_kw"][3] += 0.01                          # inside stage 1's own span
    d["modeled"]["c1"]["A"] = bad
    assert not T62.inrun_vs_tool(d, res)["match"]
