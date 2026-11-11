"""D82 (owner, Nov 4, D64 check-in): under declared_reference the exchange checks each declaration when it is filed
and rejects it (`infeasible_declaration`) if, with the asset's accepted declarations, 1p's planner would have no
feasible plan. The check is the metering service's 1p model handed to the exchange (it reads no data); flag on and no
check installed rejects every declaration (fails closed). Backstop: if 1p still has no plan within an agent's
declarations, the metering service sends the registered-limits reference with `declarations_void: true`, the exchange
logs the breach (D83's `infeasible_declaration_calls`) and the run continues; nobody else's settlement changes.
D83: the persistent D65 record carries `above_power_steps` and `infeasible_declaration_calls` under the flag only."""
import numpy as np
import pandas as pd
import pytest

from sim.data import META_COLS, Slice
from tests.test_d64_declared_reference import DECL_T, REF, REG, _declare, _p, _sref_reg, _telemetry, _today_run
from tests.test_exchange import code
from tests.test_price_mode import STEPS, WIN

DAY2 = "2023-01-03"                     # the second local day of the two-day slice below


# ---------------------------------------------------------------- the filing check, at the exchange
def test_an_infeasible_declaration_is_rejected_and_nothing_is_recorded():
    h = _p()
    h.setup(binding=True)
    assert _declare(h, "A", power=50.0, reserve=40.0) == []
    seen = []

    def check(asset, as_of):           # sees the declaration being filed, as if accepted
        seen.append(h.ex.declared_limits(asset, ["2023-01-02T12:00:00Z"], as_of))
        return False
    h.ex.declaration_check = check
    assert code(_declare(h, "A", power=10.0, reserve=90.0)) == "infeasible_declaration"
    assert seen == [([10.0], [90.0])]
    assert h.ex.declared["A/bess"]["2023-01-02"]["power_kw"] == 50.0       # the earlier declaration stands
    assert h.ex.declared["A/bess"]["2023-01-02"]["reserve_kwh"] == 40.0
    assert "A/bess" not in h.ex.declarations_voided


def test_a_forced_outage_is_never_rejected_and_voids_the_conflicting_voluntary_declaration_instead():
    """D82 amendment: the check fails with the outage in; the outage is kept, and the earlier voluntary declaration
    is voided (removed from then on) with infeasible_declaration sent to the agent for that declaration."""
    h = _p()
    h.setup(binding=True)
    msg_ids = []
    h.ex.declaration_check = lambda asset, as_of: not (h.ex.outages.get(asset) and h.ex.declared.get(asset))
    out = h.send("declare_availability", "A", {"asset_id": "A/bess", "date": "2023-01-02", "power_kw": 50.0,
                                                "reserve_kwh": 90.0, "planned_outage": False}, t=DECL_T)
    msg_ids.append(out[0])
    assert out[1] == []
    res = h.send("declare_outage", "A", {"asset_id": "A/bess", "window": WIN}, t="2023-01-02T06:40:00Z")[1]
    assert [(m["msg_type"], m["to"], m["payload"]["code"], m["payload"]["ref_msg_id"]) for m in res] ==         [("reject", "A", "infeasible_declaration", msg_ids[0])]
    assert len(h.ex.outages["A/bess"]) == 1 and "A/bess" not in h.ex.declared
    assert [x["date"] for x in h.ex.declarations_voided["A/bess"]] == ["2023-01-02"]


def test_either_filing_order_gives_the_same_accepted_set():
    def run(order):
        h = _p()
        h.setup(binding=True)
        h.ex.declaration_check = lambda asset, as_of: not (h.ex.outages.get(asset) and h.ex.declared.get(asset))
        rej = []
        for what in order:
            if what == "reserve":
                rej += [m["payload"]["code"] for m in _declare(h, "A", power=50.0, reserve=90.0)]
            else:
                rej += [m["payload"]["code"] for m in h.send("declare_outage", "A", {"asset_id": "A/bess",
                                                             "window": WIN}, t=DECL_T)[1]]
        return rej, h.ex.declared.get("A/bess"), [sorted(o["steps"]) for o in h.ex.outages.get("A/bess", [])]
    assert run(["outage", "reserve"]) == run(["reserve", "outage"]) ==         (["infeasible_declaration"], None, [sorted(STEPS)])


def test_flag_on_without_a_check_rejects_every_declaration_and_flag_off_never_calls_it():
    h = _p()
    h.setup(binding=True)
    h.ex.declaration_check = None
    assert code(_declare(h)) == "infeasible_declaration"
    assert "A/bess" not in h.ex.declared
    h0 = _p(False)
    h0.setup(binding=True)

    def boom(asset, as_of):
        raise AssertionError("flag off: no check")
    h0.ex.declaration_check = boom
    assert _declare(h0) == []
    assert h0.send("declare_outage", "A", {"asset_id": "A/bess", "window": WIN}, t="2023-01-02T06:40:00Z")[1] == []


# ---------------------------------------------------------------- the backstop message
def test_declarations_void_needs_the_flag_and_the_registered_reference():
    h = _p()
    cid = h.setup(binding=True)
    bad = h.send("selfish_reference", "meter", {"asset_id": "A/bess", "call_id": cid, "window": WIN,
                                                "net_kw": list(REF["A"]), "net_kw_registered": list(REG["A"]),
                                                "declarations_void": True})[1]
    assert code(bad) == "bad_message"                                   # void must send the registered reference
    assert h.send("selfish_reference", "meter", {"asset_id": "A/bess", "call_id": cid, "window": WIN,
                                                 "net_kw": list(REG["A"]), "net_kw_registered": list(REG["A"]),
                                                 "declarations_void": True})[1] == []
    assert h.ex.infeasible_calls == {"A/bess": {cid}}
    h0 = _p(False)
    cid0 = h0.setup(binding=True)
    assert code(h0.send("selfish_reference", "meter", {"asset_id": "A/bess", "call_id": cid0, "window": WIN,
                                                       "net_kw": list(REG["A"]), "declarations_void": True})[1]) \
        == "bad_message"


# ---------------------------------------------------------------- D83: the persistent record
def test_the_persistent_record_carries_the_power_breach_and_the_infeasible_calls_under_the_flag_only():
    h, _, _, _ = _today_run(meter_a=(80.0, 0.0))                       # 80 kW out against 50 declared
    rec = {m["to"]: m["payload"] for m in h.final_out if m["msg_type"] == "delivery_record"}
    assert rec["A"]["record"]["above_power_steps"] == 1
    assert rec["A"]["record"]["infeasible_declaration_calls"] == 0
    assert rec["A"]["this_run"]["infeasible_declaration_calls"] == 0
    assert rec["B"]["record"]["above_power_steps"] == 0
    # flag off: the record is unchanged (no new keys)
    h0 = _p(False)
    cid = h0.setup(binding=True)
    _declare(h0)
    for aid in ("A", "B"):
        h0.send("selfish_reference", "meter", {"asset_id": f"{aid}/bess", "call_id": cid, "window": WIN,
                                               "net_kw": list(REF[aid])})
        h0.nominate(aid, cid, [-50.0, -50.0], window=WIN)
    h0.round(1)
    out = h0.send("tick", "clock", {"final": True}, t="2023-01-02T12:30:00Z")[1]
    rows = [m["payload"] for m in out if m["msg_type"] == "delivery_record"]
    assert rows and all("above_power_steps" not in r["record"] and "infeasible_declaration_calls" not in r["record"]
                        for r in rows)


# ---------------------------------------------------------------- full runs, two local days
def two_day_slice() -> Slice:
    """tests.test_replay.synthetic_slice over two Central days (Jan 2 and Jan 3), so a day-2 declaration can be filed
    before its day-ahead deadline (Jan 2, 18:00 UTC)."""
    n_t = 192
    ts = pd.date_range("2023-01-02 06:00", periods=n_t, freq="15min", tz="UTC")
    hour = np.asarray(ts.tz_convert("America/Chicago").hour)
    sys_load = 1000 + 300 * np.exp(-((hour - 19) ** 2) / 4)
    price = 20 + 10 * (hour < 6)
    z = np.zeros((2, n_t))
    meta = pd.DataFrame({"entity_id": ["A", "B"], "entity_type": "pv_bess", "bess_power_kw": [100.0, 60.0],
                         "bess_energy_kwh": [400.0, 240.0], "eta_inverter": 0.97, "eta_battery_rt": 0.9,
                         "soc0": 0.5, "poi_limit_kw": 1000.0, "self_discharge_per_h": 0.0, "soh0": 100.0,
                         "peak_kw": np.nan, "bus": [1, 2]})
    assert set(META_COLS) <= set(meta.columns)
    return Slice(ids=np.array(["A", "B"]), ts_utc=ts, dt_h=0.25,
                 agent={"load": z.copy(), "pv": z.copy(), "curt": z.copy(), "bess": z.copy(),
                        "soc": np.full((2, n_t), 50.0), "soh": np.full((2, n_t), 100.0), "pnet": z.copy()},
                 system={"price_rt": price.astype(float), "price_da": price.astype(float), "system_load": sys_load,
                         "net_disp": sys_load, "bg_load_scale": np.ones(n_t)},
                 meta=meta)


class _Files:
    """Test-only wrapper: at step 0 the agent files the given declarations, in order (and keeps running)."""

    def __init__(self, inner, make):
        self._inner, self.make = inner, make

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def receive(self, raw):
        self._inner.receive(raw)

    def act(self, k, soc, price_version, price):
        outs, req = self._inner.act(k, soc, price_version, price)
        if k == 0:
            outs = list(outs) + self.make(self._inner)
        return outs, req


def _outage_day1(a):
    return a._msg("declare_outage", a.iso[0], {"asset_id": a.asset_id,
                                               "window": {"start_utc": a.iso[0], "end_utc": a.iso[96]}})


def _reserve_day2(a):             # power 0 and a reserve (300 kWh) above the starting SOC (200 kWh)
    return a._msg("declare_availability", a.iso[0], {"asset_id": a.asset_id, "date": DAY2, "power_kw": 0.0,
                                                     "reserve_kwh": 300.0, "planned_outage": False})


CFG = dict(delivery_record=True, declared_reference=True, silent_reference=True)


def _run(tmp_path, name, make, monkeypatch):
    import sim.run_m1 as R
    from tests.test_d58_margin import _cfg
    if make is None:
        return R.run_variant(_cfg(run_name=name, **CFG), two_day_slice(), np.arange(2), tmp_path / name,
                             headroom_mw=0.03)
    monkeypatch.setattr(R, "wrap_attacker", lambda spec, ag, cfg: _Files(ag, make))
    cfg = _cfg(run_name=name, **CFG, attackers=[{"type": "test_declare", "agent": "A"}])
    return R.run_variant(cfg, two_day_slice(), np.arange(2), tmp_path / name, headroom_mw=0.03)


def _rejects(r):
    ids = {m["msg_id"]: m["msg_type"] for m in r["log"] if m["sender"] == "A"}
    return [(ids.get(m["payload"]["ref_msg_id"]), m["payload"]["code"]) for m in r["log"]
            if m["msg_type"] == "reject" and m["to"] == "A" and m["payload"]["ref_msg_id"] in ids]


def test_the_two_declaration_case_keeps_the_outage_and_voids_the_reserve_in_either_order(tmp_path, monkeypatch):
    """The case from the D64 check-in: a forced outage over day 1 keeps 1p's SOC at its start (200 kWh); day 2 then
    declares power 0 with a 300 kWh reserve. Each alone is feasible; together they are not. In either filing order the
    outage is kept and the reserve declaration is voided, with infeasible_declaration for it (D82 as amended); every
    reference is then the same, the run completes, and A's battery running during the accepted outage is a breach."""
    runs = {o: _run(tmp_path, o, mk, monkeypatch) for o, mk in (
        ("outage_first", lambda a: [_outage_day1(a), _reserve_day2(a)]),
        ("reserve_first", lambda a: [_reserve_day2(a), _outage_day1(a)]))}
    refs = {}
    for o, r in runs.items():
        assert _rejects(r) == [("declare_availability", "infeasible_declaration")]
        refs[o] = [(p["asset_id"], p["call_id"], p["net_kw"]) for p in
                   (m["payload"] for m in r["log"] if m["msg_type"] == "selfish_reference")]
        assert refs[o] and not any(m["payload"].get("declarations_void") for m in r["log"]
                                   if m["msg_type"] == "selfish_reference")
        row = [m["payload"] for m in r["log"] if m["msg_type"] == "delivery_record" and m["to"] == "A"][-1]
        assert row["record"]["forced_outages"] == 1 and row["record"]["outage_activity_steps"] > 0
    assert refs["outage_first"] == refs["reserve_first"]


def test_the_backstop_voids_one_agents_declarations_and_nobody_elses_settlement_changes(tmp_path, monkeypatch):
    """The filing check is bypassed (monkeypatched to accept everything) so the infeasible pair is accepted. 1p then
    has no plan within A's declarations at every reference: the run must not stop; each of A's references is its
    registered-limits one with declarations_void, and the D65 row counts the calls. Comparator: the same run with
    A's declarations never filed. Every message to B (settlement, imbalance, record) and every price are the same."""
    import sim.run_m1 as R
    monkeypatch.setattr(R, "make_declaration_check", lambda *a, **k: (lambda asset, as_of: True))
    r = _run(tmp_path, "bypass", lambda a: [_outage_day1(a), _reserve_day2(a)], monkeypatch)
    r0 = _run(tmp_path, "none", None, monkeypatch)
    assert _rejects(r) == []
    refs_a = [m["payload"] for m in r["log"] if m["msg_type"] == "selfish_reference" and m["payload"]["asset_id"] == "A/bess"]
    refs_a0 = [m["payload"] for m in r0["log"] if m["msg_type"] == "selfish_reference" and m["payload"]["asset_id"] == "A/bess"]
    assert refs_a and all(p.get("declarations_void") is True for p in refs_a)
    assert [p["net_kw"] for p in refs_a] == [p["net_kw"] for p in refs_a0]
    to_b = lambda run: [(m["msg_type"], m["payload"]) for m in run["log"] if m["to"] == "B"
                        and m["msg_type"] in ("peak_settle", "imbalance", "delivery_record", "settle")]
    assert to_b(r) and to_b(r) == to_b(r0)
    prices = lambda run: [m["payload"] for m in run["log"] if m["msg_type"] == "peak_price"]
    assert prices(r) == prices(r0)
    row = [m["payload"] for m in r["log"] if m["msg_type"] == "delivery_record" and m["to"] == "A"][-1]
    assert row["this_run"]["infeasible_declaration_calls"] == len(refs_a)
    assert row["record"]["infeasible_declaration_calls"] == len(refs_a)


# ---------------------------------------------------------------- D64 check-in item 2: what detection reads
@pytest.mark.parametrize("with_site_net", [True, False])
def test_misdeclaration_is_detected_from_the_battery_meter_and_soc_alone(with_site_net):
    """Owner question (Nov 4): is misdeclaration detected using only meters the simulator computes truthfully? The
    breaches come from the battery meter (meter_kw) and SOC, which only the metering service may send and which
    sim/run_m1.py computes from physics. Dropping the site net meter (net_kw) changes nothing."""
    h = _p()
    h.setup(binding=True)
    assert _declare(h, "A", power=50.0, reserve=60.0) == []                     # less power, more reserve
    for t, kw, soc in ((STEPS[0], 80.0, 100.0), (STEPS[1], 20.0, 40.0)):      # 80 kW > 50; 40 kWh < 60
        p = {"asset_id": "A/bess", "soc_kwh": soc, "meter_kw": kw, **({"net_kw": -100.0} if with_site_net else {})}
        assert h.send("telemetry", "meter", p, t=t)[1] == []
    br = h.ex._breach_steps("A/bess")
    assert br == {"outage_activity": set(), "above_power": {STEPS[0]}, "below_reserve": {STEPS[1]}}
