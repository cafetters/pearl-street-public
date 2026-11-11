"""D80/D81 behind one flag, declared_reference (off by default; owner, Nov 3, M3 plan step 5): accepted declarations
enter 1p's reference (power cap, reserve floor, outages), and on a local day with a breach the day's declaration is
void and settlement re-reads the reference from the registered limits at the closing tick. The flag needs
delivery_record and 1p, and fails closed with D62's guarantee; there is no separate key for either half. Imbalance on
bound steps is never excused by a declared outage. Every rule here is enforced in code."""
import numpy as np
import pytest

from bench.item7_resettle import max_first_call_steps
from tests.test_exchange import code
from tests.test_price_mode import DEADLINE, K, P, ROUND_T, STEPS, WIN, _sref

DECL_T = "2023-01-01T06:00:00Z"            # before Jan 2's day-ahead prices publish (Jan 1, 12:00 local)
END = "2023-01-02T07:30:00Z"
FINAL = "2023-01-02T12:30:00Z"
REF = {"A": (-60.0, -20.0), "B": (-10.0, -40.0)}        # declared-limits reference (A declared 50 kW)
REG = {"A": (-100.0, -20.0), "B": (-10.0, -40.0)}       # registered-limits reference
NOM = {"A": [-120.0, -30.0], "B": [-10.0, -40.0]}


def _p(flag=True, **kw):
    """The harness has no 1p model, so under the flag it gets a check that accepts every declaration (D82: without
    one the exchange rejects them all; tests/test_d82_declaration_check.py covers the check itself)."""
    h = P(settlement_reference="selfish_1p", delivery_record=True,
          **({"declared_reference": True} if flag else {}), **kw)
    if flag:
        h.ex.declaration_check = lambda asset, as_of: True
    return h


def _declare(h, aid="A", power=50.0, reserve=40.0, outage=False, date="2023-01-02", t=DECL_T):
    return h.send("declare_availability", aid, {"asset_id": f"{aid}/bess", "date": date, "power_kw": power,
                                                 "reserve_kwh": reserve, "planned_outage": outage}, t=t)[1]


def _sref_reg(h, cid, aid, kw, reg, window=WIN, t=ROUND_T):
    return h.send("selfish_reference", "meter", {"asset_id": f"{aid}/bess", "call_id": cid, "window": window,
                                                 "net_kw": list(kw), "net_kw_registered": list(reg)}, t=t)[1]


def _telemetry(h, aid, t, meter_kw, net_kw, soc=100.0):
    h.send("telemetry", "meter", {"asset_id": f"{aid}/bess", "soc_kwh": soc, "meter_kw": meter_kw, "net_kw": net_kw},
           t=t)


# ---------------------------------------------------------------- the flag: one key, fail-closed combinations
def test_flag_needs_delivery_record_and_1p_and_fails_with_the_d62_guarantee():
    with pytest.raises(ValueError):
        P(settlement_reference="selfish_1p", declared_reference=True)                       # no delivery_record
    with pytest.raises(ValueError):
        P(delivery_record=True, declared_reference=True)                                    # recorded reference
    with pytest.raises(ValueError):
        _p(guarantee=True)
    assert _p().ex.config["declared_reference"] is True
    assert "declared_reference" not in _p(False).ex.config


@pytest.mark.parametrize("cfg_extra", [
    {"declared_reference": True},                                               # no delivery_record
    {"declared_reference": True, "delivery_record": True, "d62_guarantee": True},
    {"delivery_record": True, "declarations_into_1p": True},                     # no separate key for either half
    {"delivery_record": True, "void_rule": True},
    {"delivery_record": True, "declared_reference": True, "void_rule": False},
])
def test_the_run_config_path_fails_closed(tmp_path, cfg_extra):
    from sim.run_m1 import run_variant
    from tests.test_d58_margin import _cfg
    from tests.test_replay import synthetic_slice
    with pytest.raises(ValueError):
        run_variant(_cfg(run_name="x", **cfg_extra), synthetic_slice(), np.arange(2), tmp_path, headroom_mw=0.03)


@pytest.mark.parametrize("flag", [False, True])
def test_both_halves_follow_the_one_flag(flag):
    """(declarations active, void active) is (on, on) or (off, off): never one without the other."""
    h = _p(flag)
    cid = h.setup(binding=True)
    assert _declare(h) == []
    pmax, _ = h.ex.declared_limits("A/bess", STEPS, "2023-01-02T06:00:00Z")
    declarations_active = not np.isnan(pmax).all()
    for aid in ("A", "B"):
        out = _sref_reg(h, cid, aid, REF[aid], REG[aid])
        assert (out == []) == flag                                     # the registered reference needs the flag
        if not flag:
            assert _sref(h, cid, aid=aid, kw=REF[aid]) == []
        h.nominate(aid, cid, NOM[aid], window=WIN)
    h.round(1)
    h.tick(DEADLINE)
    for t in STEPS:
        _telemetry(h, "A", t, 80.0, -100.0)                          # 80 kW out against 50 declared: a breach
        _telemetry(h, "B", t, 0.0, -10.0)
    h.tick(END)
    h.send("tick", "clock", {"final": True}, t=FINAL)
    void_active = bool(h.ex.voided)
    assert declarations_active == void_active == flag


# ---------------------------------------------------------------- D80: accepted declarations only
def test_declared_limits_come_from_accepted_declarations_only():
    h = _p()
    h.setup(binding=True)
    assert _declare(h, "A", power=50.0, reserve=40.0) == []
    assert code(_declare(h, "B", power=10.0, t="2023-01-02T06:00:00Z")) == "after_gate_closure"   # rejected: late
    assert h.ex.declared_limits("A/bess", STEPS, "2023-01-02T06:00:00Z") == ([50.0, 50.0], [40.0, 40.0])
    pb, fb = h.ex.declared_limits("B/bess", STEPS, "2023-01-02T06:00:00Z")
    assert np.isnan(pb).all() and np.isnan(fb).all()              # the rejected declaration changes nothing


def test_a_forced_outage_caps_power_at_0_only_for_references_computed_after_it():
    h = _p()
    h.setup(binding=True)
    assert h.send("declare_outage", "A", {"asset_id": "A/bess", "window": WIN}, t="2023-01-02T06:40:00Z")[1] == []
    before = h.ex.declared_limits("A/bess", STEPS, "2023-01-02T06:30:00Z")
    after = h.ex.declared_limits("A/bess", STEPS, "2023-01-02T06:45:00Z")
    assert np.isnan(before[0]).all()
    assert after[0] == [0.0, 0.0] and np.isnan(after[1]).all()
    assert _declare(h, "B", outage=True, date="2023-01-03") == []      # a planned outage day: power 0
    assert h.ex.declared_limits("B/bess", ["2023-01-03T12:00:00Z"], "2023-01-02T06:45:00Z")[0] == [0.0]


# ---------------------------------------------------------------- D81: the void rule, today's 1p
def _today_run(meter_a):
    h = _p()
    cid = h.setup(binding=True)
    assert _declare(h) == []
    for aid in ("A", "B"):
        assert _sref_reg(h, cid, aid, REF[aid], REG[aid]) == []
        h.nominate(aid, cid, NOM[aid], window=WIN)
    p = h.round(1)[0]["payload"]["price_usd_mwh"]
    h.tick(DEADLINE)
    for t, kw in zip(STEPS, meter_a):
        _telemetry(h, "A", t, kw, -100.0)
        _telemetry(h, "B", t, 0.0, -10.0)
    h.tick(END)
    out = h.send("tick", "clock", {"final": True}, t=FINAL)[1]
    settle = {m["to"]: m["payload"]["net_usd"] for m in out if m["msg_type"] == "peak_settle"}
    rec = {m["to"]: m["payload"]["this_run"] for m in out if m["msg_type"] == "delivery_record"}
    h.final_out = out
    return h, p, settle, rec


def test_a_breach_voids_the_days_declaration_and_settlement_rereads_the_registered_reference():
    _, p, settle, rec = _today_run(meter_a=(80.0, 0.0))        # 80 kW out against 50 declared, on step 1
    _, p0, settle0, rec0 = _today_run(meter_a=(40.0, 0.0))     # within the declaration
    assert p == p0
    q = lambda ref: sum(pi * (-100.0 - qi) * K for pi, qi in zip(p, ref))
    assert settle0["A"] == pytest.approx(q(REF["A"]), abs=1e-9)
    assert settle["A"] == pytest.approx(q(REG["A"]), abs=1e-9)
    assert settle["B"] == pytest.approx(settle0["B"], abs=1e-12)          # B had no breach: untouched
    assert rec["A"]["voided_days"] == ["2023-01-02"] and rec["A"]["above_power_steps"] == 1
    assert rec0["A"]["voided_days"] == [] and rec0["A"]["above_power_steps"] == 0


def test_an_agent_that_deregisters_after_a_breach_is_still_voided():
    """Review finding (Nov 4): deregistration removes the agent's assets from asset_owner, but its held steps are
    still settled, so the void rule must still re-read them."""
    h = _p()
    cid = h.setup(binding=True)
    assert _declare(h) == []
    for aid in ("A", "B"):
        assert _sref_reg(h, cid, aid, REF[aid], REG[aid]) == []
        h.nominate(aid, cid, NOM[aid], window=WIN)
    p = h.round(1)[0]["payload"]["price_usd_mwh"]
    h.tick(DEADLINE)
    for t, kw in zip(STEPS, (80.0, 0.0)):                         # 80 kW out against 50 declared: a breach
        _telemetry(h, "A", t, kw, -100.0)
        _telemetry(h, "B", t, 0.0, -10.0)
    h.tick(END)
    assert h.send("deregister", "A", {"agent_id": "A", "effective_date": "2023-01-03"},
                  t="2023-01-02T08:00:00Z")[1] == []
    h.tick("2023-01-03T06:15:00Z")                                # the deregistration takes effect
    assert "A/bess" not in h.ex.asset_owner
    out = h.send("tick", "clock", {"final": True}, t="2023-01-03T07:00:00Z")[1]
    settle = {m["to"]: m["payload"]["net_usd"] for m in out if m["msg_type"] == "peak_settle"}
    assert h.ex.voided == {"A/bess": ["2023-01-02"]}
    assert settle["A"] == pytest.approx(sum(pi * (-100.0 - qi) * K for pi, qi in zip(p, REG["A"])), abs=1e-9)


def test_flag_off_refuses_the_registered_reference():
    h = _p(False)
    cid = h.setup(binding=True)
    assert code(_sref_reg(h, cid, "A", REF["A"], REG["A"])) == "bad_message"


# ---------------------------------------------------------------- D81 with the max rule: every call's reference
def test_with_the_max_rule_the_reread_replaces_every_calls_reference_and_matches_the_offline_tool():
    from tests.test_max_rule import T2, W2
    h = _p(settlement_rule="max_first_call")
    a = h.setup(binding=True)
    assert _declare(h) == []
    for aid in ("A", "B"):
        assert _sref_reg(h, a, aid, REF[aid], REG[aid]) == []
        h.nominate(aid, a, [-80.0, -80.0], window=WIN)
    pa = h.round(1)[0]["payload"]["price_usd_mwh"]
    h.tick(DEADLINE)
    b, _ = h.send("call_nominations", "OP", {"zone": "SYS", "window": W2, "deadline_utc": STEPS[1],
                                             "binding": True}, t=DEADLINE)
    ref_b, reg_b = {"A": (-30.0,), "B": (-5.0,)}, {"A": (-50.0,), "B": (-5.0,)}
    for aid in ("A", "B"):
        assert _sref_reg(h, b, aid, ref_b[aid], reg_b[aid], window=W2, t=T2) == []
        h.nominate(aid, b, [-45.0], window=W2, t=DEADLINE)
    pb = h.round(1, t=T2)[0]["payload"]["price_usd_mwh"]
    h.tick(STEPS[1])
    meter = {"A": (-70.0, -60.0), "B": (-75.0, -30.0)}
    for aid in ("A", "B"):
        for t, v in zip(STEPS, meter[aid]):
            _telemetry(h, aid, t, 80.0 if aid == "A" else 0.0, v)     # A breaches its 50 kW declaration
    out = h.send("tick", "clock", {"final": True}, t=FINAL)[1]
    settle = {m["to"]: m["payload"]["net_usd"] for m in out if m["msg_type"] == "peak_settle"}
    assert h.ex.voided == {"A/bess": ["2023-01-02"]}
    assert [h.ex.call_reference[c]["A/bess"][t] for c, t in ((a, STEPS[0]), (a, STEPS[1]), (b, STEPS[1]))] == \
        [REG["A"][0], REG["A"][1], reg_b["A"][0]]                      # every call's reference was re-read
    nan = float("nan")
    for aid, refs_a, refs_b in (("A", REG["A"], reg_b["A"]), ("B", REF["B"], ref_b["B"])):
        step = max_first_call_steps([np.array(pa, float), np.array([nan, pb[0]])],
                                    [np.array([-80.0, -80.0]), np.array([nan, -45.0])],
                                    [np.array(refs_a, float), np.array([nan, refs_b[0]])],
                                    np.array(meter[aid], float))[0]
        assert settle[aid] == pytest.approx(float(step.sum()) * K, abs=1e-6)


# ---------------------------------------------------------------- imbalance is never excused by a declared outage
def _imbalance(declare_outage):
    h = _p()
    cid = h.setup(binding=True)
    for aid in ("A", "B"):
        _sref_reg(h, cid, aid, REF[aid], REG[aid])
        h.nominate(aid, cid, NOM[aid], window=WIN)
    h.round(1)
    h.tick(DEADLINE)
    if declare_outage:          # after the gate: the steps are already bound
        assert h.send("declare_outage", "A", {"asset_id": "A/bess", "window": WIN}, t=DEADLINE)[1] == []
    for t in STEPS:
        _telemetry(h, "A", t, 0.0, -10.0)                              # A stops delivering: 110 and 20 kW off
        _telemetry(h, "B", t, 0.0, -10.0)
    return {m["to"]: m["payload"] for m in h.tick(END) if m["msg_type"] == "imbalance"}


def test_a_declared_outage_never_excuses_imbalance_on_bound_steps():
    with_outage, without = _imbalance(True), _imbalance(False)
    assert without["A"]["charge_usd"] > 0.0
    assert with_outage["A"] == without["A"]


# ---------------------------------------------------------------- full runs
def test_flag_off_is_byte_identical_and_flag_on_with_no_declarations_settles_the_same(tmp_path):
    import json

    from exchange.trace.replay import replay
    from sim.run_m1 import run_variant
    from tests.test_d58_margin import _cfg
    from tests.test_replay import synthetic_slice
    s = synthetic_slice()
    r0 = run_variant(_cfg(run_name="a", delivery_record=True), s, np.arange(2), tmp_path / "a", headroom_mw=0.03)
    r1 = run_variant(_cfg(run_name="a", delivery_record=True, declared_reference=False), s, np.arange(2),
                     tmp_path / "b", headroom_mw=0.03)
    assert r0["log"] == r1["log"]
    # bus.log keeps nominate and telemetry as envelope only (fix 1), so compare the traces too
    body = lambda d: (d / "exchange_h0.03_rinfo" / "trace.jsonl").read_text().splitlines()[1:]    # noqa: E731
    assert body(tmp_path / "a") == body(tmp_path / "b")
    r2 = run_variant(_cfg(run_name="a", delivery_record=True, declared_reference=True), s, np.arange(2),
                     tmp_path / "c", headroom_mw=0.03)
    pick = lambda r, typ: [(m["to"], m["payload"]) for m in r["log"] if m["msg_type"] == typ]
    for typ in ("peak_settle", "imbalance", "peak_price"):
        assert pick(r2, typ) == pick(r0, typ)
    refs = [m["payload"] for m in r2["log"] if m["msg_type"] == "selfish_reference"]
    assert refs and all(x["net_kw_registered"] == x["net_kw"] for x in refs)     # no declarations: the same plan
    path = next((tmp_path / "c").glob("exchange_*/trace.jsonl"))
    assert json.loads(path.read_text().splitlines()[0])["exchange"]["declared_reference"] is True
    assert replay(path)["match"]


class _Outage:
    """Test-only wrapper: at step k_at the agent declares a forced outage over steps k0.. (and keeps running)."""

    def __init__(self, inner, k_at, k0, end_utc):
        self._inner, self.k_at, self.k0, self.end_utc = inner, k_at, k0, end_utc

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def receive(self, raw):
        self._inner.receive(raw)

    def act(self, k, soc, price_version, price):
        outs, req = self._inner.act(k, soc, price_version, price)
        if k == self.k_at:
            a = self._inner
            win = {"start_utc": a.iso[self.k0], "end_utc": self.end_utc}
            outs = list(outs) + [a._msg("declare_outage", a.iso[k], {"asset_id": a.asset_id, "window": win})]
        return outs, req


def test_a_forced_outage_changes_the_reference_from_the_next_gate(tmp_path, monkeypatch):
    import sim.run_m1 as R
    from sim.run_m1 import run_variant
    from tests.test_d58_margin import _cfg
    from tests.test_replay import synthetic_slice
    s = synthetic_slice()
    iso = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in s.ts_utc]
    base_cfg = dict(delivery_record=True, declared_reference=True, silent_reference=True)
    # when references are computed: from a run without the outage; declare one step before the last one
    r0 = run_variant(_cfg(run_name="p", **base_cfg), s, np.arange(2), tmp_path / "probe", headroom_mw=0.03)
    ref_t = sorted({(m["t_utc"], m["payload"]["window"]["start_utc"]) for m in r0["log"]
                    if m["msg_type"] == "selfish_reference"})
    assert len({t for t, _ in ref_t}) >= 2
    t_last, w_last = ref_t[-1]
    k_at, k0 = iso.index(t_last) - 1, iso.index(w_last)
    end = (s.ts_utc[-1] + np.timedelta64(15, "m")).strftime("%Y-%m-%dT%H:%M:%SZ")      # the slice's end
    monkeypatch.setattr(R, "wrap_attacker", lambda spec, ag, cfg: _Outage(ag, k_at, k0, end))
    cfg = _cfg(run_name="o", **base_cfg, attackers=[{"type": "test_outage", "agent": "A"}])
    r = run_variant(cfg, s, np.arange(2), tmp_path / "outage", headroom_mw=0.03)
    t_decl = iso[k_at]
    a = s.agent
    base = (np.nan_to_num(a["pv"][0]) - a["curt"][0] - a["load"][0])
    refs = [(m["t_utc"], m["payload"]) for m in r["log"]
            if m["msg_type"] == "selfish_reference" and m["payload"]["asset_id"] == "A/bess"]
    later = [(t, p) for t, p in refs if t > t_decl]
    earlier = [(t, p) for t, p in refs if t <= t_decl]
    assert later and earlier
    for _, p in earlier:
        assert p["net_kw"] == p["net_kw_registered"]
    hit = 0
    for _, p in later:
        i0 = iso.index(p["window"]["start_utc"])
        for j, v in enumerate(p["net_kw"]):
            if i0 + j >= k0:
                assert v == pytest.approx(base[i0 + j], abs=1e-6)        # battery at 0 on the outage steps
                hit += 1
    assert hit


def test_planner_limits_all_nan_give_exactly_the_unconstrained_plan_and_bind_where_set():
    from sim.reference import selfish_plan
    T = 96
    hour = (np.arange(T) // 4 + 0) % 24
    price = 20.0 + 30.0 * (hour >= 17) * (hour < 21)
    args = (100.0, 320.0, 200.0, price, np.zeros(T), np.zeros(T), 10.0, 0.25)
    free = selfish_plan(*args)
    nan = np.full(T, np.nan)
    same = selfish_plan(*args, p_max=nan, s_floor=nan)
    for k in ("c", "d", "s", "x"):
        assert np.array_equal(getattr(free, k), getattr(same, k))
    assert free.d.max() > 50.0                                    # it discharges into the evening price
    capped = selfish_plan(*args, p_max=np.full(T, 50.0), s_floor=np.full(T, 150.0))
    assert capped.d.max() <= 50.0 + 1e-6 and capped.c.max() <= 50.0 + 1e-6
    assert capped.s.min() >= 150.0 - 1e-6
    out = np.where(np.arange(T) >= 60, 0.0, np.nan)               # a forced outage from step 60
    o = selfish_plan(*args, p_max=out)
    assert np.abs(o.c[60:]).max() <= 1e-9 and np.abs(o.d[60:]).max() <= 1e-9
