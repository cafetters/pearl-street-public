"""D87 (T3-c, owner-approved Nov 6): the D81 void reaches every reference computed with a breached declaration.

The metering service plans 1p over the whole run (sim/run_m1.py), so a declaration for any day enters every reference
computed after it, whatever the call's window. On a breach on local days B, every stored reference whose days in force
at filing (Exchange.call_ref_days) meet B is replaced on all its steps by its registered-limits reference; the others
keep D81's B-day re-read. Only peak settlement changes: prices formed are never recomputed, and a silent agent's (D78)
imbalance base stays the reference as bound at the gate.

Also (owner, Nov 6): the recorded days in force equal what the metering service's declared_limits used for each
reference, including a declaration that shares the reference's t_utc and a declaration D82 rejects at filing (it
counts on neither side)."""
import math

import numpy as np
import pytest

from tests.test_d64_declared_reference import _declare, _p, _sref_reg, _telemetry
from tests.test_price_mode import K

# one call across local midnight (America/Chicago): step 0 is Jan 2 23:45 local, step 1 is Jan 3 00:00 local
WIN = {"start_utc": "2023-01-03T05:45:00Z", "end_utc": "2023-01-03T06:15:00Z"}
STEPS = ["2023-01-03T05:45:00Z", "2023-01-03T06:00:00Z"]
DAY1, DAY2 = "2023-01-02", "2023-01-03"
ROUND_T = "2023-01-03T05:30:00Z"
DEADLINE = "2023-01-03T05:45:00Z"
END = "2023-01-03T06:15:00Z"
FINAL = "2023-01-03T07:00:00Z"
DECL_T = "2023-01-02T06:00:00Z"             # before Jan 3's day-ahead prices publish (Jan 2, 12:00 local)
# A's 1p reference with its day-2 declaration (50 kW) in force: more load on day 1 too (the whole-run plan moved)
REF = {"A": (-140.0, -20.0), "B": (-10.0, -40.0)}
REG = {"A": (-100.0, -50.0), "B": (-10.0, -40.0)}         # registered limits: what the twin's reference is
NOM = {"A": [-120.0, -120.0], "B": [-10.0, -10.0]}
METER_NET = {"A": -100.0, "B": -10.0}
ALL_STEPS = [f"2023-01-0{d}T{h:02d}:{m:02d}:00Z" for d in (2, 3, 4) for h in range(24) for m in (0, 15, 30, 45)]


def _metering_view(ex, asset, as_of):
    """The local dates the metering service's 1p planner got a limit for: declared_limits over the run's steps."""
    pmax, floor = ex.declared_limits(asset, ALL_STEPS, as_of)
    return frozenset(ex._local_date(t) for t, p, f in zip(ALL_STEPS, pmax, floor)
                     if not (math.isnan(p) and math.isnan(f)))


def _sref_checked(h, cid, aid, kw, reg, window, t, seen):
    """Send a reference after noting what the metering service would have planned with at t, then check the
    exchange's record of it (owner's change 1)."""
    view = _metering_view(h.ex, f"{aid}/bess", t)
    assert _sref_reg(h, cid, aid, kw, reg, window=window, t=t) == []
    assert h.ex.call_ref_days[cid][f"{aid}/bess"] == view
    seen.append(view)


def _run(declare=True, breach=True, flag_kw=None, silent=False, decl_t=DECL_T, ref_t=ROUND_T, widen=True):
    """declare: A files a day-2 declaration (attack) or none (twin). breach: A's battery runs 80 kW on day 2.
    widen=False: today's D81 alone (test only, no config can do this), for the red comparison."""
    h = _p(**(flag_kw or {}))
    if not widen:
        h.ex.days_in_force = lambda asset, as_of: frozenset()
    cid = h.setup(binding=True, window=WIN, deadline=DEADLINE)
    seen = []
    if declare and decl_t <= ref_t:
        assert _declare(h, date=DAY2, power=50.0, reserve=0.0, t=decl_t) == []
    for aid in ("A", "B"):
        ref = REF[aid] if declare else REG[aid]
        if widen:
            _sref_checked(h, cid, aid, ref, REG[aid], WIN, ref_t, seen)
        else:
            assert _sref_reg(h, cid, aid, ref, REG[aid], window=WIN, t=ref_t) == []
        if not (silent and aid == "A"):
            h.nominate(aid, cid, NOM[aid], window=WIN)
    if declare and decl_t > ref_t:     # filed after the reference (and before day 2's deadline)
        assert _declare(h, date=DAY2, power=50.0, reserve=0.0, t=decl_t) == []
    rnd = h.round(1, t=ROUND_T)
    p = rnd[0]["payload"]["price_usd_mwh"]
    h.tick(DEADLINE)
    for i, t in enumerate(STEPS):
        _telemetry(h, "A", t, 80.0 if (breach and i == 1) else 0.0, METER_NET["A"])
        _telemetry(h, "B", t, 0.0, METER_NET["B"])
    end_out = h.tick(END)
    out = h.send("tick", "clock", {"final": True}, t=FINAL)[1]
    return {"h": h, "cid": cid, "p": p, "seen": seen,
            "settle": {m["to"]: m["payload"]["net_usd"] for m in out if m["msg_type"] == "peak_settle"},
            "imb": {m["to"]: m["payload"] for m in end_out + out if m["msg_type"] == "imbalance"},
            "rec": {m["to"]: m["payload"]["this_run"] for m in out if m["msg_type"] == "delivery_record"},
            "prices": [m["payload"] for m in rnd if m["msg_type"] == "peak_price"]}


# ---------------------------------------------------------------- the rule
def test_after_a_breach_the_exchange_settles_the_attacker_like_its_twin():
    atk, twin = _run(), _run(declare=False)
    assert atk["p"] == twin["p"]                                     # references never move the price
    assert atk["settle"]["A"] == pytest.approx(twin["settle"]["A"], abs=1e-9)
    assert atk["settle"]["B"] == twin["settle"]["B"]
    ref = atk["h"].ex.call_reference[atk["cid"]]["A/bess"]
    assert (ref[STEPS[0]], ref[STEPS[1]]) == REG["A"]                # replaced on every step, day 1 included
    assert atk["rec"]["A"]["voided_days"] == [DAY2] and atk["rec"]["A"]["voided_calls"] == [atk["cid"]]
    assert atk["rec"]["B"]["voided_calls"] == []


def test_without_the_widening_the_day1_gain_stays_red_comparison():
    old, twin = _run(widen=False), _run(declare=False)
    gain = old["settle"]["A"] - twin["settle"]["A"]
    assert gain == pytest.approx(old["p"][0] * (REG["A"][0] - REF["A"][0]) * K, abs=1e-12) and gain > 0


def test_a_reference_filed_before_the_declaration_keeps_only_d81s_day_reread():
    r = _run(decl_t="2023-01-02T07:00:00Z", ref_t="2023-01-02T06:30:00Z")
    assert r["seen"] == [frozenset(), frozenset()]                  # nothing was in force when it was filed
    ref = r["h"].ex.call_reference[r["cid"]]["A/bess"]
    assert (ref[STEPS[0]], ref[STEPS[1]]) == (REF["A"][0], REG["A"][1])   # day 1 untouched, day 2 re-read
    assert r["rec"]["A"]["voided_days"] == [DAY2] and r["rec"]["A"]["voided_calls"] == []


def test_a_day1_only_call_is_replaced_whole_when_a_day2_declaration_was_in_force():
    """operating_day's shape: the call's window holds only day 1, the breach is on day 2. 1p planned the whole run
    with the day-2 declaration, so the day-1 reference carries it and is replaced."""
    win1 = {"start_utc": "2023-01-03T05:30:00Z", "end_utc": "2023-01-03T06:00:00Z"}      # Jan 2 23:30 and 23:45
    steps1 = ["2023-01-03T05:30:00Z", "2023-01-03T05:45:00Z"]
    h = _p()
    cid = h.setup(binding=True, window=win1, deadline="2023-01-03T05:30:00Z")
    assert _declare(h, date=DAY2, power=50.0, reserve=0.0, t=DECL_T) == []
    seen = []
    for aid, ref, reg in (("A", (-140.0, -130.0), (-100.0, -100.0)), ("B", (-10.0, -10.0), (-10.0, -10.0))):
        _sref_checked(h, cid, aid, ref, reg, win1, "2023-01-03T05:15:00Z", seen)
        h.nominate(aid, cid, [-120.0, -120.0] if aid == "A" else [-10.0, -10.0], window=win1)
    assert seen[0] == frozenset({DAY2})
    h.round(1, t="2023-01-03T05:15:00Z")
    h.tick("2023-01-03T05:30:00Z")
    for t in steps1 + ["2023-01-03T06:00:00Z"]:
        _telemetry(h, "A", t, 80.0 if t == "2023-01-03T06:00:00Z" else 0.0, -100.0)     # the breach is on day 2
        _telemetry(h, "B", t, 0.0, -10.0)
    h.tick("2023-01-03T06:15:00Z")
    h.send("tick", "clock", {"final": True}, t=FINAL)
    assert h.ex.voided == {"A/bess": [DAY2]}
    assert [h.ex.call_reference[cid]["A/bess"][t] for t in steps1] == [-100.0, -100.0]
    assert h.ex.voided_calls == {"A/bess": [cid]}


# ---------------------------------------------------------------- the record equals the metering service's view
def test_days_in_force_equal_what_declared_limits_used_including_a_shared_t_utc():
    r = _run(decl_t=DECL_T, ref_t=DECL_T)                            # declaration and reference at the same t_utc
    assert r["seen"] == [frozenset({DAY2}), frozenset()]
    assert r["rec"]["A"]["voided_calls"] == [r["cid"]]


def test_a_declaration_rejected_by_d82_counts_on_neither_side():
    h = _p()
    cid = h.setup(binding=True, window=WIN, deadline=DEADLINE)
    assert _declare(h, date=DAY2, power=50.0, reserve=0.0, t=DECL_T) == []
    h.ex.declaration_check = lambda asset, as_of: False             # the next filing has no feasible 1p plan
    rej = _declare(h, date=DAY1, power=10.0, reserve=0.0, t="2023-01-01T06:15:00Z")
    assert [m["payload"]["code"] for m in rej] == ["infeasible_declaration"]
    seen = []
    _sref_checked(h, cid, "A", REF["A"], REG["A"], WIN, ROUND_T, seen)
    assert seen == [frozenset({DAY2})]                                # DAY1's rejected filing is on neither side


def test_a_declaration_d82_later_drops_still_counts_for_references_filed_while_it_was_in_force():
    h = _p()
    cid = h.setup(binding=True, window=WIN, deadline=DEADLINE)
    assert _declare(h, date=DAY2, power=50.0, reserve=0.0, t=DECL_T) == []
    seen = []
    for aid in ("A", "B"):
        _sref_checked(h, cid, aid, REF[aid], REG[aid], WIN, ROUND_T, seen)
        h.nominate(aid, cid, NOM[aid], window=WIN)
    h.round(1, t=ROUND_T)
    h.ex.declaration_check = lambda asset, as_of: False              # a forced outage leaves 1p infeasible
    out = h.send("declare_outage", "A", {"asset_id": "A/bess", "window": {"start_utc": STEPS[1], "end_utc": END}},
                 t="2023-01-03T05:40:00Z")[1]
    assert [m["payload"]["code"] for m in out] == ["infeasible_declaration"]
    assert "A/bess" not in h.ex.declared                              # the day-2 declaration is gone from now on
    h.tick(DEADLINE)
    for i, t in enumerate(STEPS):
        _telemetry(h, "A", t, 80.0 if i == 1 else 0.0, METER_NET["A"])   # active during the outage: a day-2 breach
        _telemetry(h, "B", t, 0.0, METER_NET["B"])
    h.tick(END)
    h.send("tick", "clock", {"final": True}, t=FINAL)
    assert h.ex.voided == {"A/bess": [DAY2]} and h.ex.voided_calls == {"A/bess": [cid]}
    ref = h.ex.call_reference[cid]["A/bess"]
    assert (ref[STEPS[0]], ref[STEPS[1]]) == REG["A"]


# ---------------------------------------------------------------- what the widening never touches
def test_silent_agent_imbalance_base_and_prices_are_unchanged_by_the_widening():
    """Owner's change 3: under D78 a silent agent's imbalance base is its reference as bound at the binding aggregate,
    and imbalance is charged before the closing-tick void. The widening changes peak settlement only."""
    on = _run(silent=True, flag_kw={"silent_reference": True})
    off = _run(silent=True, flag_kw={"silent_reference": True}, widen=False)
    assert on["prices"] == off["prices"]
    assert on["imb"]["A"] == off["imb"]["A"] and on["imb"]["A"]["charge_usd"] > 0.0
    assert on["imb"]["B"] == off["imb"]["B"]
    assert on["settle"]["A"] != off["settle"]["A"]                   # settlement is what the widening changes
    base = on["h"].ex.baseline["A"]
    assert [base[t][0] for t in STEPS] == list(REF["A"])             # the bound base is the declared reference


def test_honest_agents_are_byte_identical_with_and_without_the_widening():
    on, off = _run(), _run(widen=False)
    assert on["settle"]["B"] == off["settle"]["B"]
    assert on["imb"]["B"] == off["imb"]["B"]


def test_flag_off_records_have_no_voided_calls():
    from tests.test_price_mode import _sref
    h = _p(False)                                                    # delivery_record on, declared_reference off
    cid = h.setup(binding=True, window=WIN, deadline=DEADLINE)
    for aid in ("A", "B"):
        assert _sref(h, cid, aid, kw=REG[aid], window=WIN, t=ROUND_T) == []
        h.nominate(aid, cid, NOM[aid], window=WIN)
    h.round(1, t=ROUND_T)
    h.tick(DEADLINE)
    h.tick(END)
    out = h.send("tick", "clock", {"final": True}, t=FINAL)[1]
    recs = [m["payload"]["this_run"] for m in out if m["msg_type"] == "delivery_record"]
    assert recs and all("voided_calls" not in r and "voided_days" not in r for r in recs)
    assert h.ex.call_ref_days == {}


# ---------------------------------------------------------------- the max rule
def test_max_rule_attacker_settles_like_its_twin_and_matches_the_offline_rule():
    from bench.item7_resettle import max_first_call_steps
    w2 = {"start_utc": STEPS[1], "end_utc": END}                    # a second binding call over day 2 only
    t2 = "2023-01-03T05:50:00Z"

    def run(declare):
        h = _p(settlement_rule="max_first_call")
        a = h.setup(binding=True, window=WIN, deadline=DEADLINE)
        if declare:
            assert _declare(h, date=DAY2, power=50.0, reserve=0.0, t=DECL_T) == []
        for aid in ("A", "B"):
            assert _sref_reg(h, a, aid, REF[aid] if declare else REG[aid], REG[aid], window=WIN, t=ROUND_T) == []
            h.nominate(aid, a, NOM[aid], window=WIN)
        pa = h.round(1, t=ROUND_T)[0]["payload"]["price_usd_mwh"]
        h.tick(DEADLINE)
        b, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": w2, "deadline_utc": STEPS[1],
                                                 "binding": True}, t=DEADLINE)
        for aid, q, qr in (("A", (-30.0,) if declare else (-50.0,), (-50.0,)), ("B", (-40.0,), (-40.0,))):
            assert _sref_reg(h, b, aid, q, qr, window=w2, t=t2) == []
            h.nominate(aid, b, [-110.0] if aid == "A" else [-10.0], window=w2, t=t2)
        pb = h.round(1, t=t2)[0]["payload"]["price_usd_mwh"]
        h.tick(STEPS[1])
        for i, t in enumerate(STEPS):
            _telemetry(h, "A", t, 80.0 if i == 1 else 0.0, METER_NET["A"])
            _telemetry(h, "B", t, 0.0, METER_NET["B"])
        h.tick(END)
        out = h.send("tick", "clock", {"final": True}, t=FINAL)[1]
        return h, a, b, pa, pb, {m["to"]: m["payload"]["net_usd"] for m in out if m["msg_type"] == "peak_settle"}

    h, a, b, pa, pb, atk = run(True)
    _, _, _, pa0, pb0, twin = run(False)
    assert (pa, pb) == (pa0, pb0)
    assert atk["A"] == pytest.approx(twin["A"], abs=1e-9) and atk["B"] == twin["B"]
    assert sorted(h.ex.voided_calls["A/bess"]) == sorted([a, b])
    nan = float("nan")
    step = max_first_call_steps([np.array(pa, float), np.array([nan, pb[0]])],
                                [np.array(NOM["A"]), np.array([nan, -110.0])],
                                [np.array(REG["A"], float), np.array([nan, -50.0])],
                                np.array([METER_NET["A"]] * 2, float))[0]
    assert atk["A"] == pytest.approx(float(step.sum()) * K, abs=1e-6)


# ---------------------------------------------------------------- full runs: the record equals the metering service's view
@pytest.mark.parametrize("attack", [{"type": "misdeclare", "mode": "power"},
                                    {"type": "infeasible_declare", "order": "outage_first"}])
def test_in_a_full_run_every_reference_records_exactly_the_days_the_metering_service_planned_with(tmp_path,
                                                                                                  monkeypatch, attack):
    """Owner's change 1, through sim/run_m1.py's metering path: for every selfish_reference, call_ref_days equals the
    days declared_limits gave the metering service for that asset and time. The infeasible_declare run includes a
    declaration D82 rejects at filing, which must count on neither side."""
    from exchange.core import Exchange
    from tests.test_d61_wrappers import MISDECLARE_H, _big_a, _evening_spike, _pair
    from tests.test_d82_declaration_check import two_day_slice
    used, checked, bad = {}, [], []
    orig_dl, orig_sr = Exchange.declared_limits, Exchange._on_selfish_reference

    def dl(self, asset, steps, as_of):
        pmax, floor = orig_dl(self, asset, steps, as_of)
        used[(id(self), asset, as_of)] = frozenset(self._local_date(t) for t, p, f in zip(steps, pmax, floor)
                                                   if not (math.isnan(p) and math.isnan(f)))
        return pmax, floor

    def sr(self, msg):
        out = orig_sr(self, msg)
        if self.declared_reference:
            p = msg["payload"]
            got = self.call_ref_days[p["call_id"]][p["asset_id"]]
            want = used.get((id(self), p["asset_id"], msg["t_utc"]))
            checked.append(got)
            if want != got:
                bad.append((p["call_id"], p["asset_id"], msg["t_utc"], sorted(got), want))
        return out

    monkeypatch.setattr(Exchange, "declared_limits", dl)
    monkeypatch.setattr(Exchange, "_on_selfish_reference", sr)
    if attack["type"] == "misdeclare":
        o = _pair(tmp_path, _evening_spike(_big_a(two_day_slice())), MISDECLARE_H, attack, tag="cons")
        assert o["atk"]["rec"]["A"]["this_run"]["voided_calls"]          # the widening fired in-run
        from bench.t3_void import rebuild
        res = rebuild(o["atk"]["path"].parent.parent)                     # the offline tool reads the D87 trace
        assert res["void_rule_in_run"] == "t3a" and res["repro_max_err_usd"] <= 1e-3
        assert res["voided_calls_records"] == {"A": sorted(o["atk"]["rec"]["A"]["this_run"]["voided_calls"])}
    else:
        o = _pair(tmp_path, two_day_slice(), 0.03, attack, tag="cons")
        assert [r["code"] for r in o["atk"]["rej"]] == ["infeasible_declaration"]
    assert checked and not bad, bad[:3]
    assert any(checked)                                                 # some reference carried a declaration
    assert o["gain"] <= 1e-6
