"""M5 (owner, Nov 18, plan item 1): demonstrated, not claimed, power limits in the exchange and the metering service.

- register_power_check (exchange; the Exchange class default is off, runs default it on since D-a/D102): a register whose mandate max_kw is above the metering service's
  demonstrated power_kw for its assets is rejected with over_demonstrated_limit (constant text, structured refs).
- reference_power_cap (metering; on by default since D-a/D102): rule 1p's acting-alone reference uses min(registered max_kw,
  demonstrated power_kw), so an over-registration can't widen the reference even if the exchange let it through.
Owner, Nov 19 (D-a/D102): both on by default in runs; a config may set them false. The parity runs showed no
trace change."""
import numpy as np

from exchange.trace.replay import exchange_config
from sim.metering_cap import reference_kw
from tests.test_exchange import Harness, code


def _reg(h, aid, max_kw):
    return h.send("register", aid, {"agent_id": aid, "legal_entity": f"{aid} LLC",
                                    "assets": [{"asset_id": f"{aid}/bess", "zone": "SYS", "type": "bess"}],
                                    "mandate": {"max_kw": max_kw, "price_floor": 0.0, "workload_limits": None}})[1]


def test_default_off_accepts_over_registration_as_today():
    h = Harness()
    h.attest("A", power=100.0)
    assert _reg(h, "A", 300.0) == []                       # today's gap: the exchange never checks max_kw
    assert "register_power_check" not in h.ex.config       # header unchanged when off


def test_flag_on_rejects_max_kw_above_demonstrated_power():
    h = Harness()
    h.ex = type(h.ex)("run", "OP", "meter", "clock", ["SYS"], register_power_check=True)
    h.attest("A", power=100.0)
    out = _reg(h, "A", 300.0)
    assert code(out) == "over_demonstrated_limit"
    p = out[0]["payload"]
    assert p["refs"]["demonstrated_kw"] == 100.0 and p["refs"]["max_kw"] == 300.0
    assert "A" not in h.ex.agents
    assert h.ex.config["register_power_check"] is True
    h.attest("B", power=100.0)
    assert _reg(h, "B", 100.0) == []                       # at the limit: accepted
    h.attest("C", power=0.0, pv=0.0)
    assert code(_reg(h, "C", 1.0)) == "over_demonstrated_limit"


def test_flag_on_without_limits_on_record_rejects():
    h = Harness()
    h.ex = type(h.ex)("run", "OP", "meter", "clock", ["SYS"], register_power_check=True, attested_owners=False)
    assert code(_reg(h, "A", 50.0)) == "over_demonstrated_limit"


def test_replay_reads_the_flag_from_the_header():
    cfg = exchange_config({"exchange": {"run_id": "r", "operator_id": "OP", "metering_id": "m", "clock_id": "c",
                                        "zones": ["SYS"], "register_power_check": True}})
    assert cfg["register_power_check"] is True


def test_metering_reference_cap():
    log = [{"msg_type": "register", "sender": "A", "payload": {"mandate": {"max_kw": 300.0}}},
           {"msg_type": "register", "sender": "B", "payload": {"mandate": {"max_kw": 50.0}}}]
    demo = {"A": 100.0, "B": 60.0}
    assert reference_kw(log, {}, demo) == {"A": 100.0, "B": 50.0}                       # default on (D-a)
    assert reference_kw(log, {"reference_power_cap": True}, demo) == {"A": 100.0, "B": 50.0}
    assert reference_kw(log, {"reference_power_cap": False}, demo) == {"A": 300.0, "B": 50.0}
    assert np.isclose(reference_kw(log, {"reference_power_cap": True}, {"A": np.float64(99.5), "B": 60.0})["A"], 99.5)


def test_honest_run_identical_with_both_flags_on(tmp_path):
    """Synthetic parity [sim, synthetic]: honest agents register at their demonstrated power, so both flags change
    nothing but the header. (The real-data parity runs are the owner's gate for proposing default on.)"""
    import json
    from sim.run_m1 import run_variant
    from tests.test_d58_margin import _cfg
    from tests.test_replay import synthetic_slice
    s = synthetic_slice()
    run_variant(_cfg(run_name="off", register_power_check=False, reference_power_cap=False), s, np.arange(2),
                tmp_path / "off", headroom_mw=0.03)
    run_variant(_cfg(run_name="off", register_power_check=True, reference_power_cap=True), s, np.arange(2),
                tmp_path / "on", headroom_mw=0.03)
    off, on = ((tmp_path / d).glob("exchange_*/trace.jsonl") for d in ("off", "on"))
    lo, ln = next(off).read_text().splitlines(), next(on).read_text().splitlines()
    ho, hn = json.loads(lo[0]), json.loads(ln[0])
    assert "register_power_check" not in ho["exchange"] and hn["exchange"]["register_power_check"] is True
    assert str(hn["config"]["reference_power_cap"]) == "True"      # the header stringifies config values
    assert lo[1:] == ln[1:]
    assert any(json.loads(x).get("msg_type") == "peak_settle" for x in ln[1:])
