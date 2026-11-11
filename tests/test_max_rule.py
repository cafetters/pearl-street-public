"""D76's max rule in the exchange (settlement_rule: max_first_call, Leaning, off by default): the flag's checks and
header, one call equals today's 1p, the two-call identity, in-run peak_settle equal to the offline tool's
(bench/item7_resettle.max_first_call_steps) on the published prices within bench.tolerances' price-rounding bound per
agent, a step with no binding nomination, D77's late registrant, fail-closed references, and a full synthetic run that
replays. Every rule here is enforced in code."""
import json

import numpy as np
import pandas as pd
import pytest

from bench import tolerances as TL
from bench.item7_resettle import max_first_call_steps
from tests.test_price_mode import DEADLINE, K, P, ROUND_T, STEPS, WIN, _sref

END = "2023-01-02T07:30:00Z"
W2 = {"start_utc": STEPS[1], "end_utc": END}
T2 = "2023-01-02T07:05:00Z"
REF_A = {"A": (-100.0, -20.0), "B": (-10.0, -10.0)}
REF_B = {"A": (-50.0,), "B": (-5.0,)}
METER = {"A": (-70.0, -60.0), "B": (-75.0, -30.0)}


def _settle(h):
    return {m["to"]: m["payload"] for m in h.send("tick", "clock", {"final": True}, t=END)[1]
            if m["msg_type"] == "peak_settle"}


def _one_call(rule):
    h = P(settlement_reference="selfish_1p", settlement_rule=rule)
    cid = h.setup(binding=True)
    for aid in ("A", "B"):
        assert _sref(h, cid, aid=aid, kw=REF_A[aid]) == []
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    assert h.round(1)[0]["payload"]["price_usd_mwh"][0] > 0
    h.tick(DEADLINE)
    for aid in ("A", "B"):
        for t, v in zip(STEPS, METER[aid]):
            h.meter_net(aid, t, v)
    return _settle(h)


def _two_calls(rule, nom_b=(-45.0,), skip_b_nom=(), late_c=False):
    """Call A binds both steps (load 160 kW over a 100 kW target: its price rises); call B binds step 2 only, with
    less load (its price falls), so p_A > p_B on step 2. skip_b_nom: agents with no nomination for call B. late_c: C
    registers (first time) after A's gate and before B's, with a reference and a nomination for B only."""
    h = P(settlement_reference="selfish_1p", settlement_rule=rule)
    a = h.setup(binding=True)
    for aid in ("A", "B"):
        assert _sref(h, a, aid=aid, kw=REF_A[aid]) == []
        h.nominate(aid, a, [-80.0, -80.0], window=WIN)
    pa = h.round(1)[0]["payload"]["price_usd_mwh"]
    h.tick(DEADLINE)
    b, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": W2, "deadline_utc": STEPS[1],
                                             "binding": True}, t=DEADLINE)
    agents = ("A", "B")
    if late_c:
        h.attest("C", t=DEADLINE)
        assert h.send("register", "C", {"agent_id": "C", "legal_entity": "C LLC",
                                        "assets": [{"asset_id": "C/bess", "zone": "SYS", "type": "bess"}],
                                        "mandate": {"max_kw": 100.0, "price_floor": 0.0}}, t=DEADLINE)[1] == []
        agents = ("A", "B", "C")
    refs_b = {**REF_B, "C": (-40.0,)}
    for aid in agents:
        assert _sref(h, b, aid=aid, kw=refs_b[aid], window=W2, t=T2) == []
        if aid not in skip_b_nom:
            h.nominate(aid, b, list(nom_b), window=W2, t=DEADLINE)
    pb = h.round(1, t=T2)[0]["payload"]["price_usd_mwh"]
    h.tick(STEPS[1])
    meter = {**METER, "C": (-65.0, -35.0)}
    for aid in agents:
        for t, v in zip(STEPS, meter[aid]):
            h.meter_net(aid, t, v)
    return h, _settle(h), pa, pb


def test_settlement_rule_is_checked_and_enters_the_header_only_when_not_today():
    with pytest.raises(ValueError):
        P(settlement_reference="selfish_1p", settlement_rule="max")
    with pytest.raises(ValueError):                              # needs rule 1p
        P(settlement_rule="max_first_call")
    plain = P(settlement_reference="selfish_1p")
    assert plain.ex.settlement_rule == "today" and "settlement_rule" not in plain.ex.config
    assert P(settlement_reference="selfish_1p", settlement_rule="today").ex.config == plain.ex.config
    mx = P(settlement_reference="selfish_1p", settlement_rule="max_first_call")
    assert mx.ex.config["settlement_rule"] == "max_first_call"


def test_with_one_binding_call_the_max_rule_is_todays_1p():
    today, mx = _one_call("today"), _one_call("max_first_call")
    assert set(today) == set(mx) == {"A", "B"}
    for aid in today:
        assert mx[aid]["steps"] == today[aid]["steps"] and mx[aid]["window"] == today[aid]["window"]
        for k in ("charge_usd", "refund_usd", "net_usd"):
            assert mx[aid][k] == pytest.approx(today[aid][k], abs=1e-9)


def test_two_calls_first_call_relief_is_paid_at_the_higher_price():
    """Identity (bench/item7_resettle's): where one call prices a step, today's 1p; where two do with p_B < p_A,
    today's 1p plus (p_A - p_B) x max(0, min(N_A - Q_A, Nm - Q_B)) (the relief cap, owner Nov 4). Here both agents
    nominated more load than their reference at A (N_A - Q_A < 0): the rule as first built charged that at the spread,
    the cap floors the premium at 0."""
    _, today, pa, pb = _two_calls("today")
    _, mx, pa2, pb2 = _two_calls("max_first_call")
    assert pa == pa2 and pb == pb2
    assert pa[1] > pb[0] > 0                                    # the max term is live on step 2
    for aid in ("A", "B"):
        rel_a = -80.0 - REF_A[aid][1]
        extra = (pa[1] - pb[0]) * max(0.0, min(rel_a, METER[aid][1] - REF_B[aid][0])) * K
        assert rel_a < 0 and extra == 0.0
        assert mx[aid]["net_usd"] == pytest.approx(today[aid]["net_usd"] + extra, abs=1e-9)
        uncapped = today[aid]["net_usd"] + (pa[1] - pb[0]) * rel_a * K       # the rule as first built
        assert uncapped < mx[aid]["net_usd"] - 1e-9
        assert mx[aid]["charge_usd"] == pytest.approx(today[aid]["charge_usd"], abs=1e-9)
        assert mx[aid]["refund_usd"] == pytest.approx(mx[aid]["net_usd"] + mx[aid]["charge_usd"], abs=1e-12)


def _offline_args(h, pa, pb, aid, nom_a, nom_b, ref_b=None):
    nan = float("nan")
    pis = [np.array(pa, float), np.array([nan, pb[0]])]
    noms = [np.array(nom_a, float), np.array([nan, nom_b])]
    qs = [np.array(REF_A[aid], float), np.array([nan, (ref_b or REF_B)[aid][0]])]
    Nm = np.array([h.ex.net_meter[f"{aid}/bess"][t] for t in STEPS])
    return pis, noms, qs, Nm


def _offline(h, pa, pb, aid, nom_a, nom_b, ref_b=None):
    step = max_first_call_steps(*_offline_args(h, pa, pb, aid, nom_a, nom_b, ref_b))[0]
    return float(step.sum()) * K


def _tol(*args):
    """In-run (unrounded prices) vs a recompute from the logged prices (6 decimals): bench.tolerances' bound."""
    return TL.max_rule_tol_usd(*args)[0]


def _offline_tol(h, pa, pb, aid, nom_a, nom_b, ref_b=None):
    return _tol(*_offline_args(h, pa, pb, aid, nom_a, nom_b, ref_b))


def test_in_run_peak_settle_equals_the_offline_tool_within_the_rounding_bound_per_agent():
    h, mx, pa, pb = _two_calls("max_first_call")
    for aid in ("A", "B"):
        assert mx[aid]["net_usd"] == pytest.approx(_offline(h, pa, pb, aid, (-80.0, -80.0), -45.0),
                                                   abs=_offline_tol(h, pa, pb, aid, (-80.0, -80.0), -45.0))


def test_a_step_with_no_binding_nomination_counts_as_nominating_the_reference():
    """The binding nomination is the agent's latest (an earlier call's nomination carries over, as for imbalance), so
    a step has none only if the agent never nominated it: here C, registered after A's gate, sends none for B. B's
    price is then frozen (today's silent-agent case) but still settles; C is paid as if it nominated its reference."""
    h, mx, pa, pb = _two_calls("max_first_call", skip_b_nom=("C",), late_c=True)
    _, today, _, _ = _two_calls("today", skip_b_nom=("C",), late_c=True)
    assert h.ex.call_nom[h.ex.settle_chain[STEPS[1]][-1]].get("C", {}) == {}
    assert mx["C"]["steps"] == 1
    assert mx["C"]["net_usd"] == pytest.approx(pb[0] * (-35.0 - -40.0) * K, abs=1e-9)
    assert mx["C"]["net_usd"] == pytest.approx(today["C"]["net_usd"], abs=1e-9)
    for aid in ("A", "B"):
        assert mx[aid]["net_usd"] == pytest.approx(_offline(h, pa, pb, aid, (-80.0, -80.0), -45.0),
                                                   abs=_offline_tol(h, pa, pb, aid, (-80.0, -80.0), -45.0))


def test_d77_a_late_registrant_is_settled_only_on_the_calls_whose_gate_it_was_held_for():
    """C registers after call A's gate and before call B's: A drops out of C's chain, so on step 2 C is settled on
    B alone (today's 1p there), on step 1 not at all; A and B are settled as the offline tool on the same run (C's
    nomination moves call B's price, so a run without C is not the comparator)."""
    h, mx, pa, pb = _two_calls("max_first_call", late_c=True)
    _, today, _, _ = _two_calls("today", late_c=True)
    for aid in ("A", "B"):
        assert mx[aid]["net_usd"] == pytest.approx(_offline(h, pa, pb, aid, (-80.0, -80.0), -45.0),
                                                   abs=_offline_tol(h, pa, pb, aid, (-80.0, -80.0), -45.0))
    assert mx["C"]["steps"] == 1 == today["C"]["steps"]
    assert mx["C"]["net_usd"] == pytest.approx(pb[0] * (-35.0 - -40.0) * K, abs=1e-9)
    assert mx["C"]["net_usd"] == pytest.approx(today["C"]["net_usd"], abs=1e-9)


def test_a_missing_reference_still_fails_closed_under_the_max_rule():
    h = P(settlement_reference="selfish_1p", settlement_rule="max_first_call")
    cid = h.setup(binding=True)
    assert _sref(h, cid, aid="A") == []                                      # B has none
    for aid in ("A", "B"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    assert h.round(1)[0]["payload"]["price_usd_mwh"][0] > 0
    h.tick(DEADLINE)
    with pytest.raises(RuntimeError, match="no selfish_reference for B/bess"):
        h.send("tick", "clock", {"final": True}, t=END)


def test_max_rule_run_settles_as_the_offline_tool_and_replays(tmp_path):
    """A full synthetic run under the flag: the header carries it, the trace replays, and every agent's peak_settle
    equals bench/item7_resettle.max_first_call_steps on the trace's own prices, nominations, references and meter."""
    from exchange.trace.replay import replay
    from sim.run_m1 import run_variant
    from tests.test_replay import CFG_M11, synthetic_slice
    cfg = {**CFG_M11, "rounds": "info", "price_mode": True, "plan_to_nomination": True,
           "release_superseded_lock": True, "price_rounds_per_gate": 10, "settlement_reference": "selfish_1p",
           "settlement_rule": "max_first_call"}
    s = synthetic_slice()
    r = run_variant(cfg, s, np.arange(2), tmp_path, headroom_mw=0.03)
    path = tmp_path / "exchange_h0.03_rinfo" / "trace.jsonl"
    assert json.loads(path.read_text().splitlines()[0])["exchange"]["settlement_rule"] == "max_first_call"
    assert replay(path)["match"]
    # bus.log keeps nominate and telemetry without payload (fix 1); the trace has every message in full
    from exchange.trace.logger import line_to_msg
    log = [line_to_msg(json.loads(x)) for x in path.read_text().splitlines()[1:]]
    iso = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in s.ts_utc]
    pos, T = {t: i for i, t in enumerate(iso)}, len(iso)

    def arr(start, vals):
        a = np.full(T, np.nan)
        a[pos[start]:pos[start] + len(vals)] = vals
        return a

    binding = [m["payload"]["call_id"] for m in log if m["msg_type"] == "nomination_aggregate"
               and m["payload"]["binding"]]
    final = {m["payload"]["call_id"]: m["payload"] for m in log if m["msg_type"] == "peak_price"
             and m["payload"].get("final")}
    binding = [c for c in binding if c in final]
    assert binding
    noms, refs, net = {}, {}, {}
    for m in log:
        p = m["payload"]
        if m["msg_type"] == "nominate":
            noms[(m["sender"], p["call_id"])] = p
        elif m["msg_type"] == "selfish_reference":
            refs[(p["asset_id"].split("/")[0], p["call_id"])] = p
        elif m["msg_type"] == "telemetry" and "net_kw" in p:
            net.setdefault(p["asset_id"].split("/")[0], np.zeros(T))[pos[m["t_utc"]]] = p["net_kw"]
    settle = {m["to"]: m["payload"] for m in log if m["msg_type"] == "peak_settle"}
    assert set(settle) == set(net)
    for aid in net:
        pis = [arr(final[c]["window"]["start_utc"], final[c]["price_usd_mwh"]) for c in binding]
        ns = [arr(noms[(aid, c)]["window"]["start_utc"], noms[(aid, c)]["kw"]) for c in binding]
        qs = [arr(refs[(aid, c)]["window"]["start_utc"], refs[(aid, c)]["net_kw"]) for c in binding]
        want = float(max_first_call_steps(pis, ns, qs, net[aid])[0].sum()) * K
        assert settle[aid]["net_usd"] == pytest.approx(want, abs=_tol(pis, ns, qs, net[aid]))


def _two_calls_two_asset_late(rule):
    """Review finding 1 (Nov 3): C registers after A's gate with two assets, both excused on A (D77), with references
    and a nomination for B. Under today's rule C is settled on B alone; the max rule must do the same, not raise."""
    h = P(settlement_reference="selfish_1p", settlement_rule=rule)
    a = h.setup(binding=True)
    for aid in ("A", "B"):
        assert _sref(h, a, aid=aid, kw=REF_A[aid]) == []
        h.nominate(aid, a, [-80.0, -80.0], window=WIN)
    h.round(1)
    h.tick(DEADLINE)
    b, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": W2, "deadline_utc": STEPS[1],
                                             "binding": True}, t=DEADLINE)
    h.attest("C", t=DEADLINE)
    h.attest("C2", owner="C LLC", agent="C", t=DEADLINE)
    assert h.send("register", "C", {"agent_id": "C", "legal_entity": "C LLC",
                                    "assets": [{"asset_id": "C/bess", "zone": "SYS", "type": "bess"},
                                               {"asset_id": "C2/bess", "zone": "SYS", "type": "bess"}],
                                    "mandate": {"max_kw": 100.0, "price_floor": 0.0}}, t=DEADLINE)[1] == []
    for aid, kw in (("A", REF_B["A"]), ("B", REF_B["B"]), ("C", (-40.0,)), ("C2", (-20.0,))):
        assert _sref(h, b, aid=aid, kw=kw, window=W2, t=T2) == []
    for aid in ("A", "B", "C"):
        h.nominate(aid, b, [-45.0], window=W2, t=DEADLINE)
    pb = h.round(1, t=T2)[0]["payload"]["price_usd_mwh"]
    h.tick(STEPS[1])
    for aid, vals in {**METER, "C": (-65.0, -35.0), "C2": (-10.0, -15.0)}.items():
        for t, v in zip(STEPS, vals):
            h.meter_net(aid, t, v)
    return _settle(h), pb


def test_d77_a_late_registrant_with_two_assets_is_settled_as_today():
    mx, pb = _two_calls_two_asset_late("max_first_call")
    today, _ = _two_calls_two_asset_late("today")
    assert mx["C"]["steps"] == 1 == today["C"]["steps"]
    assert mx["C"]["net_usd"] == pytest.approx(pb[0] * ((-35.0 - 15.0) - (-40.0 - 20.0)) * K, abs=1e-9)
    for k in ("charge_usd", "refund_usd", "net_usd"):
        assert mx["C"][k] == pytest.approx(today["C"][k], abs=1e-9)


def _partial_miss(rule):
    """A partial miss on the settling call (delivery_record on). Call 3 (window Jan 3) gates on Jan 2 while A holds
    A/bess (reference sent). A has filed to leave from Jan 3. Y (same entity, a software swap) then takes A/bess over
    together with a brand-new asset Z/bess: A/bess has call 3's reference, Z/bess is excused (no holder at the gate).
    Today's rule settles Y on A/bess's reference with Z's metered load left out; the max rule must match."""
    from tests.test_price_mode import _p1
    h, cid = _p1(delivery_record=True, settlement_rule=rule)
    for aid, kw in (("A", (-100.0, -20.0)), ("B", (-10.0, -10.0))):
        assert _sref(h, cid, aid=aid, kw=kw) == []
    w3 = {"start_utc": "2023-01-03T08:00:00Z", "end_utc": "2023-01-03T08:30:00Z"}
    h.target(100.0, window=w3)
    c3, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": w3, "deadline_utc": "2023-01-02T09:00:00Z",
                                              "binding": True}, t="2023-01-02T06:30:00Z")
    for aid, kw in (("A", (-60.0, -60.0)), ("B", (-10.0, -10.0))):
        assert _sref(h, c3, aid=aid, kw=kw, window=w3, t="2023-01-02T06:40:00Z") == []
    for aid in ("A", "B"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
        h.nominate(aid, c3, [-90.0, -90.0], window=w3, t="2023-01-02T06:40:00Z")
    h.round(1)
    assert h.send("deregister", "A", {"agent_id": "A", "effective_date": "2023-01-03"}, t=ROUND_T)[1] == []
    h.tick(DEADLINE)                                                  # call 1 closes
    assert h.round(1, t="2023-01-02T08:00:00Z")[0]["payload"]["price_usd_mwh"][0] > 0     # call 3's round
    h.tick("2023-01-02T09:00:00Z")                                    # call 3 closes while A holds A/bess
    for aid in ("A", "B"):
        for t in STEPS:
            h.meter_net(aid, t, -70.0)
    h.tick("2023-01-03T06:00:00Z")                                    # A is out
    h.attest("A", owner="A LLC", agent="Y", t="2023-01-03T06:00:00Z")
    h.attest("Z", owner="A LLC", agent="Y", t="2023-01-03T06:00:00Z")
    assert h.send("register", "Y", {"agent_id": "Y", "legal_entity": "A LLC",
                                    "assets": [{"asset_id": "A/bess", "zone": "SYS", "type": "bess"},
                                               {"asset_id": "Z/bess", "zone": "SYS", "type": "bess"}],
                                    "mandate": {"max_kw": 100.0, "price_floor": 0.0}},
                  t="2023-01-03T06:00:00Z")[1] == []
    for t in ("2023-01-03T08:00:00Z", "2023-01-03T08:15:00Z"):
        h.meter_net("A", t, -50.0)
        h.meter_net("Z", t, -30.0)
        h.meter_net("B", t, -70.0)
    out = _settle_at(h, "2023-01-03T08:30:00Z")
    return h, out, c3


def _settle_at(h, t):
    return {m["to"]: m["payload"] for m in h.send("tick", "clock", {"final": True}, t=t)[1]
            if m["msg_type"] == "peak_settle"}


def test_d77_a_partial_miss_on_the_settling_call_is_settled_as_today():
    h, mx, c3 = _partial_miss("max_first_call")
    assert "Z/bess" in h.ex.unheld_at_gate and c3 in h.ex.unheld_at_gate["Z/bess"]
    _, today, _ = _partial_miss("today")
    assert today["Y"]["steps"] > 0 and mx["Y"]["steps"] == today["Y"]["steps"]
    for aid in today:
        for k in ("charge_usd", "refund_usd", "net_usd"):
            assert mx[aid][k] == pytest.approx(today[aid][k], abs=1e-9)


def test_run_summary_reports_the_prices_the_max_rule_paid():
    """Review finding 2: under the max rule, the summary's settled stats count every step a call priced and use the
    highest price among the calls that priced it (the price first-call relief is paid at), not only the last."""
    from sim.run_m1 import settled_prices
    h, _, pa, pb = _two_calls("max_first_call")
    got = settled_prices(h.ex)
    assert got[STEPS[0]] == pytest.approx(pa[0]) and got[STEPS[1]] == pytest.approx(max(pa[1], pb[0]))
    assert got[STEPS[1]] > h.ex.peak_settled[STEPS[1]][0]
    today, _, _, _ = _two_calls("today")
    assert settled_prices(today.ex) == {t: p for t, (p, _) in today.ex.peak_settled.items()}


def _partial_after_inherited_call(rule):
    """Re-review finding (Nov 3): a partly excused settling call after an earlier call whose references are all
    present because previous holders sent them. A (A/bess) and D (D/bess) share legal entity A LLC. Call 3 (window
    Jan 4) gates Jan 2 with both held. D leaves from Jan 3, A from Jan 4. Call 4 (same window) gates Jan 3: A/bess
    held (reference), D/bess unheld (excused). Y (A LLC) then takes both over. Y has no nomination for either call,
    so the earlier call adds nothing (N = Q) and the step is settled as today; it must not stop the settlement."""
    from tests.test_exchange import code
    from tests.test_price_mode import _p1
    h, cid = _p1(delivery_record=True, settlement_rule=rule)
    h.attest("D", owner="A LLC", agent="D")
    assert h.send("register", "D", {"agent_id": "D", "legal_entity": "A LLC",
                                    "assets": [{"asset_id": "D/bess", "zone": "SYS", "type": "bess"}],
                                    "mandate": {"max_kw": 100.0, "price_floor": 0.0}})[1] == []
    for aid, kw in (("A", (-100.0, -20.0)), ("B", (-10.0, -10.0)), ("D", (-10.0, -10.0))):
        assert _sref(h, cid, aid=aid, kw=kw) == []
    w = {"start_utc": "2023-01-04T08:00:00Z", "end_utc": "2023-01-04T08:30:00Z"}
    h.target(100.0, window=w)
    c3, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": w, "deadline_utc": "2023-01-02T09:00:00Z",
                                              "binding": True}, t="2023-01-02T06:30:00Z")
    for aid, kw in (("A", (-60.0, -60.0)), ("B", (-10.0, -10.0)), ("D", (-10.0, -10.0))):
        assert _sref(h, c3, aid=aid, kw=kw, window=w, t="2023-01-02T06:40:00Z") == []
    for aid in ("A", "B", "D"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
        h.nominate(aid, c3, [-90.0, -90.0], window=w, t="2023-01-02T06:40:00Z")
    h.round(1)
    assert h.send("deregister", "D", {"agent_id": "D", "effective_date": "2023-01-03"}, t=ROUND_T)[1] == []
    assert h.send("deregister", "A", {"agent_id": "A", "effective_date": "2023-01-04"}, t=ROUND_T)[1] == []
    h.tick(DEADLINE)
    assert h.round(1, t="2023-01-02T08:00:00Z")[0]["payload"]["price_usd_mwh"][0] > 0
    h.tick("2023-01-02T09:00:00Z")                                    # call 3 closes: all held
    for aid in ("A", "B", "D"):
        for t in STEPS:
            h.meter_net(aid, t, -70.0)
    h.tick("2023-01-03T06:00:00Z")                                    # D is out; A still holds A/bess
    c4, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": w, "deadline_utc": "2023-01-03T08:00:00Z",
                                              "binding": True}, t="2023-01-03T06:00:00Z")
    assert _sref(h, c4, aid="A", kw=(-70.0, -70.0), window=w, t="2023-01-03T06:30:00Z") == []
    assert code(_sref(h, c4, aid="D", kw=(-5.0, -5.0), window=w, t="2023-01-03T06:30:00Z")) == "bad_message"
    assert _sref(h, c4, aid="B", kw=(-10.0, -10.0), window=w, t="2023-01-03T06:30:00Z") == []
    h.nominate("B", c4, [-90.0, -90.0], window=w, t="2023-01-03T06:30:00Z")
    h.nominate("A", c4, [-200.0, -200.0], window=w, t="2023-01-03T06:30:00Z")
    assert h.round(1, t="2023-01-03T07:00:00Z")[0]["payload"]["price_usd_mwh"][0] > 0
    h.tick("2023-01-03T08:00:00Z")                                    # call 4 closes: D/bess unheld
    h.tick("2023-01-04T06:00:00Z")                                    # A is out
    h.attest("A", owner="A LLC", agent="Y", t="2023-01-04T06:00:00Z")
    h.attest("D", owner="A LLC", agent="Y", t="2023-01-04T06:00:00Z")
    assert h.send("register", "Y", {"agent_id": "Y", "legal_entity": "A LLC",
                                    "assets": [{"asset_id": "A/bess", "zone": "SYS", "type": "bess"},
                                               {"asset_id": "D/bess", "zone": "SYS", "type": "bess"}],
                                    "mandate": {"max_kw": 100.0, "price_floor": 0.0}},
                  t="2023-01-04T06:00:00Z")[1] == []
    assert c4 in h.ex.unheld_at_gate["D/bess"] and c3 not in h.ex.unheld_at_gate["D/bess"]
    if rule == "max_first_call":
        assert h.ex.settle_chain[w["start_utc"]] == [c3, c4]
    for t in ("2023-01-04T08:00:00Z", "2023-01-04T08:15:00Z"):
        h.meter_net("A", t, -50.0)
        h.meter_net("D", t, -30.0)
        h.meter_net("B", t, -70.0)
    return _settle_at(h, "2023-01-04T08:30:00Z")


def test_d77_a_partial_miss_after_a_call_with_inherited_references_is_settled_as_today():
    mx = _partial_after_inherited_call("max_first_call")
    today = _partial_after_inherited_call("today")
    assert today["Y"]["steps"] == 2 and mx["Y"]["steps"] == 2
    assert set(mx) == set(today)
    for aid in today:
        for k in ("charge_usd", "refund_usd", "net_usd"):
            assert mx[aid][k] == pytest.approx(today[aid][k], abs=1e-9)


def test_settle_paths_count_each_agent_step_by_the_path_that_settled_it():
    """Owner, Nov 3 (max-rule review 2b): the run summary counts agent-steps by settlement path."""
    h, _, _, _ = _two_calls("max_first_call")
    assert h.ex.settle_paths == {"max_rule": 4, "max_rule_after_d77_drop": 0, "today_via_d77": 0,
                                 "today_partly_excused": 0, "not_settled_d77": 0, "of_which_settling_price_zero": 0}
    h, _, _, pb = _two_calls("max_first_call", nom_b=(0.0,))           # call B's price falls to 0 on step 2
    assert pb == [0.0] and h.ex.settle_paths["of_which_settling_price_zero"] == 2
    h, mx, _, _ = _two_calls("max_first_call", late_c=True)            # C: step 1 not settled, step 2 on B alone
    assert h.ex.settle_paths["today_via_d77"] == 1 and h.ex.settle_paths["not_settled_d77"] == 1
    assert h.ex.settle_paths["max_rule"] == 4
    h, out, _ = _partial_miss("max_first_call")
    assert h.ex.settle_paths["today_partly_excused"] == out["Y"]["steps"] == 2
    assert P(settlement_reference="selfish_1p").ex.settle_paths == {}


def test_run_summary_counts_paths_only_under_the_max_rule(tmp_path):
    from sim.run_m1 import price_summary, run_variant
    from tests.test_replay import CFG_M11, synthetic_slice
    base = {**CFG_M11, "rounds": "info", "price_mode": True, "plan_to_nomination": True,
            "release_superseded_lock": True, "price_rounds_per_gate": 10, "settlement_reference": "selfish_1p"}
    s = synthetic_slice()
    for rule in ("today", "max_first_call"):
        cfg = {**base, "settlement_rule": rule}
        r = run_variant(cfg, s, np.arange(2), tmp_path / rule, headroom_mw=0.03)
        settled = price_summary(cfg, r, r["log"])["settled"]
        if rule == "today":
            assert "agent_steps_by_path" not in settled and "basis" not in settled
        else:
            paths = settled["agent_steps_by_path"]
            steps = sum(m["payload"]["steps"] for m in r["log"] if m["msg_type"] == "peak_settle")
            assert steps > 0 and paths["max_rule"] + paths["max_rule_after_d77_drop"] + paths["today_via_d77"] \
                + paths["today_partly_excused"] == steps


def _withdraw_at_zero_price_b(m2, rule="max_first_call", b_export=300.0):
    """Owner, Nov 4 (D76 zero cap): A nominates 80 kW of relief at call A (reference -100, nomination -20) on both
    steps, then withdraws it at call B (nomination -100 = its reference) on step 2, where B's other agent exports
    enough that call B prices step 2 at 0. A's meter reads -20 on step 1 and m2 on step 2."""
    h = P(settlement_reference="selfish_1p", settlement_rule=rule)
    a = h.setup(binding=True, target=10.0)
    for aid in ("A", "B"):
        assert _sref(h, a, aid=aid, kw=(-100.0, -100.0)) == []
        h.nominate(aid, a, [-20.0, -20.0], window=WIN)
    pa = h.round(1)[0]["payload"]["price_usd_mwh"]
    h.tick(DEADLINE)
    b, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": W2, "deadline_utc": STEPS[1],
                                             "binding": True}, t=DEADLINE)
    for aid in ("A", "B"):
        assert _sref(h, b, aid=aid, kw=(-100.0,), window=W2, t=T2) == []
    h.nominate("A", b, [-100.0], window=W2, t=DEADLINE)               # A withdraws its relief
    h.nominate("B", b, [b_export], window=W2, t=DEADLINE)               # B exports: the zone is under target
    pb = h.round(1, t=T2)[0]["payload"]["price_usd_mwh"]
    h.tick(STEPS[1])
    for t, v in zip(STEPS, (-20.0, m2)):
        h.meter_net("A", t, v)
    for t, v in zip(STEPS, (-20.0, b_export)):
        h.meter_net("B", t, v)
    return h, _settle(h), pa, pb


@pytest.mark.parametrize("m2", [-100.0, -60.0, -20.0, -140.0])
def test_relief_nominated_at_a_and_withdrawn_at_a_zero_price_b_is_paid_only_as_metered(m2):
    h, mx, pa, pb = _withdraw_at_zero_price_b(m2)
    assert pa[1] > 0 and pb == [0.0]
    step1 = pa[0] * (-20.0 - -100.0) * K                              # one call: today's 1p
    step2 = pa[1] * max(0.0, min(80.0, m2 - -100.0)) * K              # capped at metered relief vs Q_A, floored at 0
    assert mx["A"]["net_usd"] == pytest.approx(step1 + step2, abs=1e-9)
    nan = float("nan")
    args = ([np.array(pa, float), np.array([nan, 0.0])], [np.array([-20.0, -20.0]), np.array([nan, -100.0])],
            [np.array([-100.0, -100.0]), np.array([nan, -100.0])], np.array([-20.0, m2]))
    assert mx["A"]["net_usd"] == pytest.approx(float(max_first_call_steps(*args)[0].sum()) * K, abs=_tol(*args))
    uncapped = float(max_first_call_steps(*args, cap=None)[0].sum()) * K
    assert uncapped == pytest.approx(step1 + pa[1] * 80.0 * K, abs=1e-9)    # the rule as first built: nominated relief
    if m2 < -20.0:
        assert mx["A"]["net_usd"] < uncapped - 1e-9                   # the withdrawn, undelivered relief isn't paid


def _three_calls():
    """Review finding (Nov 4): three binding calls price step 2 (A, then B above A, then C at 0), and the 1p reference
    moves between A and B. Agent A adds 50 kW of relief at B and withdraws it at C, metering its reference (no relief
    delivered). Agent Z adds 50 kW at B, keeps it at C and delivers it. B (the bystander) sets the zone load."""
    h = P(settlement_reference="selfish_1p", settlement_rule="max_first_call")
    a = h.setup(binding=True, target=10.0)
    assert h.register("Z") == []
    refs_a = {"A": (-100.0, -100.0), "B": (-100.0, -100.0), "Z": (-20.0, -20.0)}
    for aid, kw in refs_a.items():
        assert _sref(h, a, aid=aid, kw=kw) == []
    for aid, kw in (("A", [-100.0, -100.0]), ("B", [-200.0, -200.0]), ("Z", [-20.0, -20.0])):
        h.nominate(aid, a, kw, window=WIN)
    pa = h.round(1)[0]["payload"]["price_usd_mwh"]
    h.tick(DEADLINE)
    prices, cids = [pa[1]], [a]
    plan = [("2023-01-02T07:10:00Z", {"A": -20.0, "B": -100.0, "Z": -100.0}, {"A": 30.0, "B": -200.0, "Z": -50.0}),
            (STEPS[1], {"A": -20.0, "B": -100.0, "Z": -100.0}, {"A": -20.0, "B": 3000.0, "Z": -50.0})]
    t0 = DEADLINE
    for deadline, refs, noms in plan:
        c, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": W2, "deadline_utc": deadline,
                                                 "binding": True}, t=t0)
        t1 = (pd.Timestamp(t0) + pd.Timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t2 = (pd.Timestamp(t0) + pd.Timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for aid in ("A", "B", "Z"):
            assert _sref(h, c, aid=aid, kw=(refs[aid],), window=W2, t=t1) == []
            h.nominate(aid, c, [noms[aid]], window=W2, t=t1)
        prices.append(h.round(1, t=t2)[0]["payload"]["price_usd_mwh"][0])
        cids.append(c)
        h.tick(deadline)
        t0 = deadline
    meter = {"A": (-100.0, -20.0), "B": (-200.0, 3000.0), "Z": (-20.0, -50.0)}
    for aid, vals in meter.items():
        for t, v in zip(STEPS, vals):
            h.meter_net(aid, t, v)
    return h, _settle(h), pa, prices, cids, refs_a, plan, meter


def test_three_calls_the_zero_cap_measures_relief_against_the_last_positively_priced_call():
    h, out, pa, prices, cids, refs_a, plan, meter = _three_calls()
    assert h.ex.settle_chain[STEPS[1]] == cids
    p_a, p_b, p_c = prices
    assert p_b > p_a > 0 and p_c == 0.0                              # C priced step 2 at 0
    nan = float("nan")
    for aid in ("A", "Z"):
        q = [refs_a[aid][1], plan[0][1][aid], plan[1][1][aid]]
        n = [{"A": -100.0, "Z": -20.0}[aid], plan[0][2][aid], plan[1][2][aid]]
        pis = [np.array([pa[0], p_a]), np.array([nan, p_b]), np.array([nan, p_c])]
        noms = [np.array([n[0], n[0]]), np.array([nan, n[1]]), np.array([nan, n[2]])]
        qs = [np.array([refs_a[aid][0], q[0]]), np.array([nan, q[1]]), np.array([nan, q[2]])]
        Nm = np.array(meter[aid])
        want = float(max_first_call_steps(pis, noms, qs, Nm)[0].sum()) * K
        tol = _tol(pis, noms, qs, Nm)
        assert out[aid]["net_usd"] == pytest.approx(want, abs=tol)
        step1 = pa[0] * (meter[aid][0] - refs_a[aid][0]) * K
        if aid == "A":       # 50 kW nominated at B, withdrawn at zero-price C, never delivered: step 2 pays 0
            assert out[aid]["net_usd"] == pytest.approx(step1, abs=tol)
        else:                # 50 kW nominated at B against the moved reference and delivered: paid at p_B
            assert out[aid]["net_usd"] == pytest.approx(step1 + p_b * 50.0 * K, abs=tol)


@pytest.mark.parametrize("m2", [-100.0, -60.0, -20.0, -140.0])
def test_relief_nominated_at_a_and_withdrawn_at_a_b_priced_just_above_0_is_paid_only_as_metered(m2):
    """Owner, Nov 4 (D76 relief cap): the max rule never pays for relief the meter didn't show, for any later call
    priced below the earlier one. A nominates 80 kW of relief at A and withdraws it at B, priced above 0 but below A.
    Step 2 pays today's 1p at B plus (p_A - p_B) x max(0, min(80, metered relief vs Q_B))."""
    h, mx, pa, pb = _withdraw_at_zero_price_b(m2, b_export=115.0)
    assert 0 < pb[0] < pa[1]
    step1 = pa[0] * (-20.0 - -100.0) * K
    step2 = (pb[0] * (m2 - -100.0) + (pa[1] - pb[0]) * max(0.0, min(80.0, m2 - -100.0))) * K
    nan = float("nan")
    args = ([np.array(pa, float), np.array([nan, pb[0]])], [np.array([-20.0, -20.0]), np.array([nan, -100.0])],
            [np.array([-100.0, -100.0]), np.array([nan, -100.0])], np.array([-20.0, m2]))
    assert mx["A"]["net_usd"] == pytest.approx(step1 + step2, abs=_tol(*args))
    assert mx["A"]["net_usd"] == pytest.approx(float(max_first_call_steps(*args)[0].sum()) * K, abs=_tol(*args))
    uncapped = float(max_first_call_steps(*args, cap=None)[0].sum()) * K
    zero_only = float(max_first_call_steps(*args, cap="zero")[0].sum()) * K
    assert uncapped == pytest.approx(zero_only, abs=1e-12)              # the zero cap didn't reach a priced B
    if m2 < -20.0:
        assert mx["A"]["net_usd"] < uncapped - 1e-9                   # the withdrawn, undelivered relief isn't paid


def _three_calls_falling(b_exports=(140.0, 190.0)):
    """Review finding (Nov 4): three binding calls price step 2 at falling prices (A > B > C > 0), all references -100.
    Agent A nominates its reference at A and 50 kW of relief at B, keeps it at C, and its meter shows 25 kW. Agent Z
    does the same and delivers all 50 kW. B (the bystander) exports to set the zone load."""
    h = P(settlement_reference="selfish_1p", settlement_rule="max_first_call")
    a = h.setup(binding=True, target=10.0)
    assert h.register("Z") == []
    for aid in ("A", "B", "Z"):
        assert _sref(h, a, aid=aid, kw=(-100.0, -100.0)) == []
    for aid, kw in (("A", [-100.0, -100.0]), ("B", [-200.0, -200.0]), ("Z", [-100.0, -100.0])):
        h.nominate(aid, a, kw, window=WIN)
    pa = h.round(1)[0]["payload"]["price_usd_mwh"]
    h.tick(DEADLINE)
    prices, t0 = [pa[1]], DEADLINE
    for deadline, export in (("2023-01-02T07:10:00Z", b_exports[0]), (STEPS[1], b_exports[1])):
        c, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": W2, "deadline_utc": deadline,
                                                 "binding": True}, t=t0)
        t1 = (pd.Timestamp(t0) + pd.Timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t2 = (pd.Timestamp(t0) + pd.Timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for aid, kw in (("A", -50.0), ("B", export), ("Z", -50.0)):
            assert _sref(h, c, aid=aid, kw=(-100.0,), window=W2, t=t1) == []
            h.nominate(aid, c, [kw], window=W2, t=t1)
        prices.append(h.round(1, t=t2)[0]["payload"]["price_usd_mwh"][0])
        h.tick(deadline)
        t0 = deadline
    for aid, vals in {"A": (-100.0, -75.0), "B": (-200.0, b_exports[1]), "Z": (-100.0, -50.0)}.items():
        for t, v in zip(STEPS, vals):
            h.meter_net(aid, t, v)
    return h, _settle(h), pa, prices


def test_three_falling_prices_each_calls_relief_is_capped_at_its_own_spread():
    h, out, pa, (p_a, p_b, p_c) = _three_calls_falling()
    assert p_a > p_b > p_c > 0
    nan = float("nan")
    for aid, m2 in (("A", -75.0), ("Z", -50.0)):
        met = m2 - -100.0                                               # metered relief against Q_C (= Q_j)
        want2 = p_c * met + (p_a - p_b) * 0.0 + (p_b - p_c) * max(0.0, min(50.0, met))
        args = ([np.array([pa[0], p_a]), np.array([nan, p_b]), np.array([nan, p_c])],
                [np.array([-100.0, -100.0]), np.array([nan, -50.0]), np.array([nan, -50.0])],
                [np.array([-100.0, -100.0]), np.array([nan, -100.0]), np.array([nan, -100.0])], np.array([-100.0, m2]))
        assert out[aid]["net_usd"] == pytest.approx(want2 * K, abs=_tol(*args))   # step 1: no relief, pays 0
        assert out[aid]["net_usd"] == pytest.approx(float(max_first_call_steps(*args)[0].sum()) * K, abs=_tol(*args))
        total_spread = p_c * met + min((p_b - p_c) * 50.0, (p_a - p_c) * met)   # the cap on the total spread
        if aid == "A":       # only 25 kW of B's 50 kW showed: paid 25 kW at B's spread, not more via A's price
            assert want2 < total_spread - 1e-9
        else:                # all 50 kW delivered: paid in full at B's spread
            assert want2 == pytest.approx(p_c * 50.0 + (p_b - p_c) * 50.0)
