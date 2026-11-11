"""D61 attack wrappers, M3 plan step 6 (synthetic, red-first): silent and "silent, then deviate" (attacks/silent.py),
late or false outage, misdeclaration and infeasible declarations (attacks/declare.py). Each attack cell is paired with
its honest twin: the same slice, config and flags, minus `attackers`. Gain is the attacker's value against the twin
(as tests/test_d61_renege.py). The D64 flags (`silent_reference`, `declared_reference`) are on unless a test says
otherwise; `delivery_record` is on in both runs of a pair."""
import json

import numpy as np
import pytest

from exchange.trace.replay import replay
from sim.run_m1 import evaluate, run_variant
from tests.test_d58_margin import _cfg
from tests.test_d82_declaration_check import two_day_slice
from tests.test_replay import synthetic_slice

FLAGS = {"delivery_record": True, "silent_reference": True, "declared_reference": True}


def _big_a(s):
    s.meta.loc[0, "bess_power_kw"] = 2000.0         # band = max(5% x 2000, 50) = 100 kW, far below a full response
    s.meta.loc[0, "bess_energy_kwh"] = 8000.0
    return s


MISDECLARE_H = 3.0     # MW: more than the two batteries can cut, so the evening peak stays priced


def _evening_spike(s, usd_mwh=80.0):
    """Condition 1 (Nov 5): on two_day_slice's flat prices the 1p reference never discharges at the peak, so a
    misdeclaration has nothing to lower and the honest battery never nears its declared limits (the test was inert).
    A day-ahead and real-time price spike over the evening peak (17:00 to 21:00 local) makes 1p discharge there."""
    hour = np.asarray(s.ts_utc.tz_convert("America/Chicago").hour)
    bump = usd_mwh * ((hour >= 17) & (hour < 21))
    s.system["price_da"] = s.system["price_da"] + bump
    s.system["price_rt"] = s.system["price_rt"] + bump
    return s


def _pair(tmp_path, s, h, attack: dict, margin=5.0, flags=FLAGS, tag="x"):
    rec = s.system["system_load"] * 1000.0
    p_end = float(s.system["price_da"].mean()) / 1000.0
    out = {}
    base = {"peak_response_margin_usd_mwh": margin, **flags}
    for name, cfg in ((f"hon_{tag}", _cfg(run_name="hon", **base)),
                      (f"atk_{tag}", _cfg(run_name="atk", attackers=[{"agent": "A", **attack}], **base))):
        sf = run_variant(cfg, s, np.arange(2), tmp_path / name)
        r = run_variant(cfg, s, np.arange(2), tmp_path / name, headroom_mw=h)
        ev = evaluate(cfg, s, np.arange(2), r, rec, None, sf)
        log = r["log"]
        out[name[:3]] = {
            "r": r, "value": ev["_agent_value"] + r["SOC"][:, -1] * p_end,
            "imb": {m["to"]: m["payload"] for m in log if m["msg_type"] == "imbalance"},
            "ps": {m["to"]: m["payload"]["net_usd"] for m in log if m["msg_type"] == "peak_settle"},
            "rec": {m["to"]: m["payload"] for m in log if m["msg_type"] == "delivery_record"},
            "rej": [m["payload"] for m in log if m["msg_type"] == "reject" and m["to"] == "A"],
            "prices": [(m["payload"]["call_id"], tuple(m["payload"]["price_usd_mwh"])) for m in log
                       if m["msg_type"] == "peak_price"],
            "path": next((tmp_path / name).glob("exchange_*/trace.jsonl"))}
    out["gain"] = float(out["atk"]["value"][0] - out["hon"]["value"][0])
    return out


def _header_attackers(o):
    return json.loads(o["atk"]["path"].read_text().splitlines()[0])["attackers"]


def _moves(prices) -> bool:
    by = {}
    for cid, p in prices:
        by.setdefault(cid, []).append(p)
    return any(len(set(v)) > 1 for v in by.values())


# ---------------------------------------------------------------- silent
def test_silent_keeps_the_price_moving_counts_missed_calls_and_gains_nothing(tmp_path):
    o = _pair(tmp_path, synthetic_slice(), 0.03, {"type": "silent"}, tag="on")
    off = _pair(tmp_path, synthetic_slice(), 0.03, {"type": "silent"},
                flags={"delivery_record": True}, tag="off")
    ag = o["atk"]["r"]["agents"][0]
    assert type(ag).__name__ == "Silent" and any(e.get("dropped") == "nominate" for e in ag.log)
    assert _header_attackers(o) == [{"agent": "A", "type": "silent"}]
    assert _moves(o["atk"]["prices"])                        # D78: the price keeps moving
    assert not _moves(off["atk"]["prices"])                  # without the rule: frozen (the documented case)
    assert o["atk"]["rec"]["A"]["this_run"]["binding_calls_missed"] > 0
    assert o["hon"]["rec"]["A"]["this_run"]["binding_calls_missed"] == 0
    assert o["gain"] <= 1e-6
    assert replay(o["atk"]["path"])["match"]


def test_silent_then_deviate_is_charged_imbalance_against_its_reference_and_gains_nothing(tmp_path):
    o = _pair(tmp_path, _big_a(synthetic_slice()), 0.5, {"type": "silent", "mode": "deviate"}, tag="on")
    off = _pair(tmp_path, _big_a(synthetic_slice()), 0.5, {"type": "silent", "mode": "deviate"},
                flags={"delivery_record": True}, tag="off")
    assert o["atk"]["imb"]["A"]["deviation_kwh"] > 100.0 and o["atk"]["imb"]["A"]["charge_usd"] > 0.0
    assert off["atk"]["imb"].get("A", {}).get("deviation_kwh", 0.0) == 0.0     # without the rule: none (documented)
    assert o["gain"] <= 1e-6


# ---------------------------------------------------------------- late or false outage
@pytest.mark.parametrize("mode", ["stop", "run"])
def test_a_late_outage_is_charged_counted_late_and_gains_nothing(tmp_path, mode):
    s = _big_a(synthetic_slice())
    o = _pair(tmp_path, s, 0.5, {"type": "outage", "mode": mode, "day": "2023-01-02"}, tag=mode)
    ag = o["atk"]["r"]["agents"][0]
    assert ag.window is not None                                         # it declared after a binding gate
    rec = o["atk"]["rec"]["A"]["record"]
    assert rec["forced_outages"] == 1 and rec["late_outages"] == 1
    assert o["atk"]["imb"]["A"]["charge_usd"] > 0.0 or mode == "run"     # (a): bound steps still charged
    if mode == "run":                                                    # (b): the meter shows the battery running
        assert rec["outage_activity_steps"] > 0
        assert o["atk"]["rec"]["A"]["this_run"]["voided_days"] == ["2023-01-02"]
    assert o["gain"] <= 1e-6


# ---------------------------------------------------------------- misdeclaration
@pytest.mark.parametrize("mode", ["power", "reserve"])
def test_misdeclaration_gains_without_the_void_rule_and_nothing_with_it(tmp_path, monkeypatch, mode):
    from exchange.core import Exchange
    s = _evening_spike(_big_a(two_day_slice()))
    o = _pair(tmp_path, s, MISDECLARE_H, {"type": "misdeclare", "mode": mode}, tag="void")
    br = o["atk"]["rec"]["A"]["this_run"]
    assert br["voided_days"] == ["2023-01-03"]                           # day 2 was declared, breached and voided
    assert (br["above_power_steps"] if mode == "power" else o["atk"]["rec"]["A"]["record"]["below_reserve_steps"]) > 0
    assert o["gain"] <= 1e-6
    # red: the same attack with the void rule switched off (test only; no config can do this)
    monkeypatch.setattr(Exchange, "_void_declarations", lambda self: setattr(self, "void_done", True))
    red = _pair(tmp_path, s, MISDECLARE_H, {"type": "misdeclare", "mode": mode}, tag="novoid")
    assert red["atk"]["rec"]["A"]["this_run"]["voided_days"] == []
    assert red["gain"] > 1e-6


# ---------------------------------------------------------------- infeasible declarations (owner, Nov 4)
@pytest.mark.parametrize("order", ["outage_first", "reserve_first"])
def test_infeasible_declarations_are_refused_the_run_completes_and_gain_nothing(tmp_path, order):
    o = _pair(tmp_path, two_day_slice(), 0.03, {"type": "infeasible_declare", "order": order}, tag=order)
    assert [r["code"] for r in o["atk"]["rej"]] == ["infeasible_declaration"]
    assert o["atk"]["rec"]["A"]["record"]["forced_outages"] == 1
    assert o["gain"] <= 1e-6
