"""Q-N10 (owner, Nov 19, proven-capacity change 3): under `nomination_power_check` the exchange refuses a nomination
whose relief exceeds the asset's granted power: nominated site net above the battery-idle site net (sent by the
metering service per call, `idle_reference`) plus min(mandate max_kw, demonstrated power_kw), on any step. Constant
code `over_power_nomination`, structured refs only, never the nominated values. Off by default [sim, synthetic]."""
import json

import numpy as np
import pytest

from exchange import messages as M
from redteam.synth import BASE_CFG, HEADROOM_MW, synthetic_slice

FLEET = (("L", 100.0, 400.0), ("H1", 1000.0, 4000.0), ("H2", 500.0, 2000.0))


def _run(tmp_path, name, check: bool, attackers=()):
    from sim.run_m1 import run_variant
    cfg = {**BASE_CFG, "run_name": name, "nomination_power_check": check, "attackers": list(attackers)}
    run_variant(cfg, synthetic_slice(fleet=FLEET), np.arange(3), tmp_path / name, headroom_mw=HEADROOM_MW)
    lines = next((tmp_path / name).glob("exchange_*/trace.jsonl")).read_text(encoding="utf-8").splitlines()
    return json.loads(lines[0]), [json.loads(x) for x in lines[1:]]


def _rejects(msgs, code):
    return [m for m in msgs if m.get("msg_type") == "reject" and m["payload"]["code"] == code]


def test_the_code_is_a_constant_reject_code():
    assert "over_power_nomination" in M.REJECT_CODES
    assert M.REQUIRED["idle_reference"] == ("asset_id", "call_id", "window", "net_kw")


def test_an_over_nominator_is_refused_and_honest_agents_are_not(tmp_path):
    _, msgs = _run(tmp_path, "on", True, [{"agent": "L", "type": "over_nominate", "factor": 3.0}])
    rej = _rejects(msgs, "over_power_nomination")
    assert rej and {m["to"] for m in rej} == {"L"}
    for m in rej:                         # structured refs only: never the nominated values
        assert set(m["payload"]["refs"]) <= {"call_id", "max_kw", "steps_over"}
    # every nomination the exchange accepted from L is within idle + power on every step
    rejected = {m["payload"]["ref_msg_id"] for m in msgs if m.get("msg_type") == "reject"}
    idle = {}
    for m in msgs:
        if m.get("msg_type") == "idle_reference" and m["payload"]["asset_id"] == "L/bess":
            idle[m["payload"]["call_id"]] = m["payload"]["net_kw"]
    acc = [m for m in msgs if m.get("msg_type") == "nominate" and m.get("from") == "L" and m["msg_id"] not in rejected]
    for m in acc:
        base = idle[m["payload"]["call_id"]]
        assert all(v <= b + 100.0 + 0.01 for v, b in zip(m["payload"]["kw"], base))


def test_off_by_default_and_honest_runs_send_no_reject(tmp_path):
    h_off, off = _run(tmp_path, "off", False)
    h_on, on = _run(tmp_path, "on_honest", True)
    assert not _rejects(on, "over_power_nomination")
    assert "nomination_power_check" not in h_off["exchange"] and h_on["exchange"]["nomination_power_check"] is True
    # parity apart from the idle_reference lines the flag adds (declared before any parity run)
    strip = lambda ms: [(m.get("msg_id"), m.get("msg_type"), m.get("from"), m.get("to"), m.get("t_utc"),
                         json.dumps(m.get("payload"), sort_keys=True))
                        for m in ms if m.get("msg_type") != "idle_reference"]
    a, b = strip(off), strip(on)
    assert len(a) == len(b) and a == b


def test_unit_check_on_a_hand_built_exchange():
    from tests.test_exchange import code
    from tests.test_m2a_exchange import CALL_WIN, H, _steps
    n = len(_steps(CALL_WIN))
    off = H()
    assert off.register("A") == []
    cid0, _ = off.call()
    assert off.nominate("A", cid0, 900.0)[1] == []                    # off: today, unbounded (Q-N10)
    h = H(nomination_power_check=True)
    assert h.register("A") == [] and h.ex.config["nomination_power_check"] is True     # power 100 (attest default)
    cid, _ = h.call()
    assert h.send("idle_reference", "meter", {"asset_id": "A/bess", "call_id": cid, "window": CALL_WIN,
                                              "net_kw": [0.0] * n})[1] == []
    assert h.nominate("A", cid, 100.0)[1] == []                       # at the limit
    assert h.nominate("A", cid, -500.0)[1] == []                      # more import is never "relief"
    out = h.nominate("A", cid, [100.02] + [0.0] * (n - 1))[1]
    assert code(out) == "over_power_nomination" and "100.02" not in json.dumps(out)
    assert out[0]["payload"]["refs"] == {"call_id": cid, "max_kw": 100.0, "steps_over": 1}
    cid2, _ = h.call()
    assert code(h.nominate("A", cid2, 0.0)[1]) == "over_power_nomination"   # no idle reference: fails closed
    bad = h.send("idle_reference", "A", {"asset_id": "A/bess", "call_id": cid2, "window": CALL_WIN,
                                         "net_kw": [0.0] * n})[1]
    assert code(bad) == "unauthorized_sender"


def test_a_refused_agent_counts_at_its_reference_only_with_d78(tmp_path):
    """D107 with D109 (Nov 21): a nomination the check refuses leaves the agent silent. Without D78 the silent agent
    freezes its zone's price (no price forms; HW9 item 5); with D78 it counts at its 1p reference, so prices form, and
    its bound steps get the reference as their imbalance base. Characterizes today's behaviour (no code change)."""
    from sim.run_m1 import run_variant
    atk = [{"agent": "L", "type": "over_nominate", "factor": 3.0}]
    out = {}
    for d78 in (False, True):
        name = f"refused_d78_{d78}"
        cfg = {**BASE_CFG, "run_name": name, "nomination_power_check": True, "silent_reference": d78, "attackers": atk}
        run_variant(cfg, synthetic_slice(fleet=FLEET), np.arange(3), tmp_path / name, headroom_mw=HEADROOM_MW)
        lines = next((tmp_path / name).glob("exchange_*/trace.jsonl")).read_text(encoding="utf-8").splitlines()
        out[d78] = [json.loads(x) for x in lines[1:]]
    for msgs in out.values():
        rejected = {m["payload"]["ref_msg_id"] for m in _rejects(msgs, "over_power_nomination")}
        assert rejected and not [m for m in msgs if m.get("msg_type") == "nominate" and m.get("from") == "L"
                                 and m["msg_id"] not in rejected]                  # L is refused every time: silent
    price = lambda msgs: [max(m["payload"]["price_usd_mwh"]) for m in msgs
                          if m.get("msg_type") == "peak_price" and m["payload"]["final"]]
    assert price(out[False]) and all(p == 0.0 for p in price(out[False]))         # frozen: no price forms
    assert price(out[True]) and all(p > 0.0 for p in price(out[True]))            # D78: prices form
    imb = lambda msgs: [m for m in msgs if m.get("msg_type") == "imbalance" and m.get("to") == "L"]
    assert not imb(out[False]) and imb(out[True])                                  # D78: reference base for L
