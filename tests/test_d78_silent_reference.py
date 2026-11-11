"""D78/D79 silent agents (flag silent_reference, off by default; owner, Nov 3, M3 plan step 5). A step with no nomination
from an active agent counts as the agent having nominated its 1p reference there (N = Q): in the price loop, the
binding aggregate and the imbalance base on bound steps; the metering service sends references for every call that
runs price rounds. Every rule here is enforced in code."""
import numpy as np
import pytest

from tests.test_price_mode import DEADLINE, P, ROUND_T, STEPS, WIN, _sref

END = "2023-01-02T07:30:00Z"
REF = {"A": (-100.0, -20.0), "B": (-10.0, -40.0)}     # 1p reference site net per step (+ = export)
NOM_A = [-120.0, -30.0]                                # A's nomination: zone load 130 then 70 with B at its reference


def _h(flag=True, **kw):
    return P(settlement_reference="selfish_1p", silent_reference=flag, **kw) if flag else \
        P(settlement_reference="selfish_1p", **kw)


def _price(flag, b_nominates_ref, binding=False):
    h = _h(flag)
    cid = h.setup(binding=binding)
    for aid in ("A", "B"):
        if flag or binding:
            assert _sref(h, cid, aid=aid, kw=REF[aid]) == []
    h.nominate("A", cid, NOM_A, window=WIN)
    if b_nominates_ref:
        h.nominate("B", cid, list(REF["B"]), window=WIN)
    return h, cid, h.round(1)[0]["payload"]["price_usd_mwh"]


def test_flag_needs_price_mode_and_1p_and_enters_the_header_only_when_on():
    with pytest.raises(ValueError):
        P(silent_reference=True)                                   # recorded reference
    assert P(settlement_reference="selfish_1p", silent_reference=True).ex.config["silent_reference"] is True
    assert "silent_reference" not in P(settlement_reference="selfish_1p").ex.config


def test_flag_off_a_silent_agent_freezes_the_price_the_documented_case():
    _, _, p = _price(False, b_nominates_ref=False)
    assert p == [0.0, 0.0]                         # D53 as built: _zone_load is None, the price keeps its old value


def test_one_silent_agent_no_longer_freezes_the_price():
    _, _, p = _price(True, b_nominates_ref=False)
    assert p[0] > 0.0 and p[1] == 0.0              # 130 kW over a 100 kW target prices step 1; 70 kW doesn't


def test_the_price_matches_the_agent_having_nominated_its_reference():
    _, _, silent = _price(True, b_nominates_ref=False)
    _, _, twin = _price(True, b_nominates_ref=True)
    assert silent == twin


def test_sign_the_references_export_positive_net_maps_to_the_nominations_load_sign():
    h, cid, _ = _price(True, b_nominates_ref=False)
    for i, t in enumerate(STEPS):                  # REF["B"] is nonzero on both steps, so a flipped sign would show
        assert h.ex._zone_load("SYS", t, cid) == pytest.approx(-NOM_A[i] - REF["B"][i])
        assert h.ex._zone_load("SYS", t, cid) != pytest.approx(-NOM_A[i] + REF["B"][i])
    assert h.ex._zone_load("SYS", STEPS[0]) is None          # without the call there is no reference: as before


def test_non_binding_references_are_refused_with_the_flag_off():
    h = _h(False)
    cid = h.setup(binding=False)
    assert _sref(h, cid, aid="A", kw=REF["A"])[0]["payload"]["code"] == "bad_message"


def _imbalance_run(flag, b_nominates_ref, meter_b=(-75.0, -100.0), drop_ref_b=False, holds=None):
    h = _h(flag)
    cid = h.setup(binding=True)
    for aid in ("A", "B"):
        if not (aid == "B" and drop_ref_b):
            assert _sref(h, cid, aid=aid, kw=REF[aid]) == []
    h.nominate("A", cid, NOM_A, window=WIN)
    if b_nominates_ref:
        h.nominate("B", cid, list(REF["B"]), window=WIN)
    h.round(1)
    if holds is not None:
        h.ex._holds = holds
    h.tick(DEADLINE)
    for t, a, b in zip(STEPS, (-120.0, -30.0), meter_b):
        h.meter_net("A", t, a)
        h.meter_net("B", t, b)
    out = h.tick(END)                              # imbalance is charged once the call's window has ended
    return h, {m["to"]: m["payload"] for m in out if m["msg_type"] == "imbalance"}


def test_a_silent_agent_that_deviates_on_bound_steps_pays_the_same_imbalance_as_its_twin():
    # B's reference is -10, -40; it meters -75, -100: 65 and 60 kW off, beyond the 50 kW band (max(5% x 100, 50))
    _, silent = _imbalance_run(True, b_nominates_ref=False)
    _, twin = _imbalance_run(True, b_nominates_ref=True)
    assert twin["B"]["charge_usd"] > 0.0
    assert silent["B"]["charge_usd"] == pytest.approx(twin["B"]["charge_usd"], abs=1e-6)
    assert silent["B"]["deviation_kwh"] == pytest.approx(twin["B"]["deviation_kwh"], abs=1e-9)
    assert silent["A"] == twin["A"]


def test_flag_off_a_silent_agent_is_not_charged_imbalance_the_documented_case():
    _, off = _imbalance_run(False, b_nominates_ref=False)
    assert "B" not in off                          # no nomination, no base: going silent then deviating is free


def test_no_base_without_a_reference_d77_or_outside_tenure():
    h, out = _imbalance_run(True, b_nominates_ref=False, drop_ref_b=True)     # no reference for B (a D77 step)
    assert "B" not in out and not h.ex.baseline.get("B")

    def holds(aid, t):                             # B's tenure ends before step 2
        return not (aid == "B" and t == STEPS[1])
    h2, _ = _imbalance_run(True, b_nominates_ref=False, holds=holds)
    assert STEPS[1] not in h2.ex.baseline["B"] and STEPS[0] in h2.ex.baseline["B"]


def test_the_binding_aggregate_counts_the_reference():
    h = _h(True)
    cid = h.setup(binding=True)
    for aid in ("A", "B"):
        _sref(h, cid, aid=aid, kw=REF[aid])
    h.nominate("A", cid, NOM_A, window=WIN)
    h.round(1)
    agg = [m for m in h.tick(DEADLINE) if m["msg_type"] == "nomination_aggregate"]
    assert agg and agg[0]["payload"]["kw"] == pytest.approx([NOM_A[0] + REF["B"][0], NOM_A[1] + REF["B"][1]])
    assert agg[0]["payload"]["n_agents"] == 1       # still counts nominating agents only


def test_d65_still_counts_the_binding_call_as_missed():
    h = P(settlement_reference="selfish_1p", silent_reference=True, delivery_record=True)
    cid = h.setup(binding=True)
    for aid in ("A", "B"):
        _sref(h, cid, aid=aid, kw=REF[aid])
    h.nominate("A", cid, NOM_A, window=WIN)
    h.round(1)
    h.tick(DEADLINE)
    for t, b in zip(STEPS, (-10.0, -40.0)):
        h.meter_net("A", t, -100.0)
        h.meter_net("B", t, b)
    out = h.tick(END) + h.send("tick", "clock", {"final": True}, t="2023-01-02T12:30:00Z")[1]
    rec = {m["to"]: m["payload"] for m in out if m["msg_type"] == "delivery_record"}
    assert rec["B"]["this_run"]["binding_calls_missed"] == 1
    assert rec["A"]["this_run"]["binding_calls_missed"] == 0
    # B metered exactly its reference: the reference base is not a promise, so it earns no delivery credit
    assert rec["B"]["this_run"]["binding_steps"] == 0 and rec["B"]["this_run"]["steps_within_band"] == 0
    assert rec["A"]["this_run"]["binding_steps"] == 2


def test_full_run_sends_references_for_non_binding_priced_calls_and_replays(tmp_path):
    """Every agent nominates here, so the flag changes nothing but the extra references: settlement, imbalance and
    prices equal the flag-off run's, and the trace replays."""
    import json

    from exchange.trace.replay import replay
    from sim.run_m1 import run_variant
    from tests.test_d58_margin import _cfg
    from tests.test_replay import synthetic_slice
    s = synthetic_slice()
    runs = {}
    for flag in (False, True):
        cfg = _cfg(run_name=f"f{int(flag)}", **({"silent_reference": True} if flag else {}))
        runs[flag] = run_variant(cfg, s, np.arange(2), tmp_path / str(flag), headroom_mw=0.03)
    pick = lambda r, typ: [(m["to"], m["payload"]) for m in r["log"] if m["msg_type"] == typ]
    for typ in ("peak_settle", "imbalance", "peak_price"):
        assert pick(runs[True], typ) == pick(runs[False], typ)
    ref_calls = lambda r: {m["payload"]["call_id"] for m in r["log"] if m["msg_type"] == "selfish_reference"}
    assert ref_calls(runs[True]) > ref_calls(runs[False])          # non-binding priced calls got references too
    path = next((tmp_path / "True").glob("exchange_*/trace.jsonl"))
    assert json.loads(path.read_text().splitlines()[0])["exchange"]["silent_reference"] is True
    assert replay(path)["match"]


def test_flag_off_is_byte_identical_to_a_run_without_the_key(tmp_path):
    from sim.run_m1 import run_variant
    from tests.test_d58_margin import _cfg
    from tests.test_replay import synthetic_slice
    s = synthetic_slice()
    r1 = run_variant(_cfg(run_name="a"), s, np.arange(2), tmp_path / "a", headroom_mw=0.03)
    r2 = run_variant(_cfg(run_name="a", silent_reference=False), s, np.arange(2), tmp_path / "b", headroom_mw=0.03)
    assert r1["log"] == r2["log"]
