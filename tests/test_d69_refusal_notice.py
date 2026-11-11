"""D69 rule 1 amendment (owner, Nov 2; flag refusal_notice, off by default). Synthetic only.

With the flag on, the exchange answers the true origin of every refused message with a coded reject (fixed text, no
echo, at most one per origin, reason and minute of the exchange's clock), never the claimed sender, and counts the
refusal against the origin's legal entity in the D65 record. With it off nothing changes."""
import json

import numpy as np
import pytest

from exchange import messages as M
from exchange.core import REFUSAL_DETAIL, Exchange
from exchange.trace.replay import replay
from sim.bus import Bus
from tests.test_d69_identity import DAY, DL, INJ, T0, Inbox, ListLog


SYNTH_NOTICES = 3      # notices the synthetic impersonation run gets (its 5 refusals fall in 3 (reason, minute) buckets)


def _world(notice: bool, record: bool = False):
    ex = Exchange("run", "OP", "meter", "clock", ["SYS"], require_nominations=True, scarcity_usd_mwh=500.0,
                  refusal_notice=notice, delivery_record=record)
    log = ListLog()
    bus = Bus(log, ex)
    eps = {k: Inbox() for k in ("VICTIM", "ATK", "OP")}
    for k, v in eps.items():
        bus.attach(k, v)
    n = [0]

    def send(mt, sender, payload, origin=None, to="exchange", t=T0, msg_id=None, raw=None):
        n[0] += 1
        r = raw if raw is not None else M.encode(M.make(mt, sender, to, t, payload,
                                                        msg_id or f"{origin or sender}:{n[0]}", "run"))
        bus.send([r], origin or sender)
        bus.flush()

    for aid in ("VICTIM", "ATK"):
        send("asset_limits", "meter", {"asset_id": f"{aid}/bess", "usable_kwh": 100.0, "power_kw": 100.0,
                                       "pv_kwh_per_day": 0.0, "owner": f"{aid} LLC", "agent_id": aid})
        send("register", aid, {"agent_id": aid, "legal_entity": f"{aid} LLC",
                               "assets": [{"asset_id": f"{aid}/bess", "zone": "SYS", "type": "bess"}],
                               "mandate": {"max_kw": 100.0, "price_floor": 0.0, "workload_limits": None}})
        send("subscribe", aid, {"signals": ["call_nominations"], "zones": ["SYS"]})
    send("tick", "clock", {}, t=T0)
    send("call_nominations", "OP", {"zone": "SYS", "window": DAY, "deadline_utc": DL, "binding": True})
    cid = next(m for m in eps["VICTIM"].inbox if m["msg_type"] == "call_nominations")["payload"]["call_id"]
    return {"ex": ex, "eps": eps, "send": send, "cid": cid, "log": log}


def _rejects(w, who):
    return [m for m in w["eps"][who].inbox if m["msg_type"] == "reject"]


def _forge_nomination(w, msg_id=None):
    w["send"]("nominate", "VICTIM", {"call_id": w["cid"], "zone": "SYS", "window": DAY, "kw": [-100.0, -100.0]},
              origin="ATK", msg_id=msg_id)


def test_flag_off_changes_nothing():
    w = _world(False)
    _forge_nomination(w)
    assert _rejects(w, "ATK") == [] and _rejects(w, "VICTIM") == []
    assert "refusal_notice" not in w["ex"].config


def test_notice_goes_to_the_true_origin_only_with_fixed_text_and_no_echo():
    w = _world(True)
    _forge_nomination(w, msg_id="ATK:42")
    (r,) = _rejects(w, "ATK")
    assert _rejects(w, "VICTIM") == []                              # never the claimed sender
    assert r["sender"] == "exchange" and r["to"] == "ATK" and r["msg_id"] == "ex:refusal1"
    assert r["payload"] == {"ref_msg_id": "ATK:42", "code": "sender_mismatch",
                            "detail": REFUSAL_DETAIL["sender_mismatch"], "refs": {"recorded": True}}
    assert w["ex"].config["refusal_notice"] is True
    # an invalid msg_id (free text) is not echoed
    w["send"]("tick", "clock", {}, t="2023-01-02T06:15:00Z")
    _forge_nomination(w, msg_id=INJ)
    r2 = _rejects(w, "ATK")[-1]
    assert r2["payload"]["ref_msg_id"] is None and INJ not in json.dumps(r2)


def test_rate_limited_to_one_per_origin_reason_and_minute_of_the_exchange_clock():
    w = _world(True)
    for _ in range(3):
        _forge_nomination(w)
    assert len(_rejects(w, "ATK")) == 1                             # same reason, same minute: one notice
    w["send"]("peak_price", "ATK", {}, to="VICTIM")                 # another reason in the same minute: its own notice
    assert [r["payload"]["code"] for r in _rejects(w, "ATK")] == ["sender_mismatch", "direct_delivery"]
    w["send"]("tick", "clock", {}, t="2023-01-02T06:15:00Z")
    _forge_nomination(w)
    assert len(_rejects(w, "ATK")) == 3                             # a later minute of the clock: one more
    assert w["ex"].refusal_counts["ATK"] == {"sender_mismatch": 4, "direct_delivery": 1}   # every refusal counted


def test_a_forward_dated_forgery_does_not_reset_the_rate_limit():
    """The minute is the exchange's own clock, never the t_utc the sender writes."""
    w = _world(True)
    _forge_nomination(w)
    w["send"]("nominate", "VICTIM", {"call_id": w["cid"], "zone": "SYS", "window": DAY, "kw": [0.0, 0.0]},
              origin="ATK", t="2099-01-01T00:00:00Z")
    assert len(_rejects(w, "ATK")) == 1


def test_malformed_message_gets_a_malformed_notice():
    w = _world(True)
    w["send"](None, None, None, origin="ATK", raw="{not json")
    (r,) = _rejects(w, "ATK")
    assert r["payload"]["code"] == "malformed" and r["payload"]["ref_msg_id"] is None


def test_refusals_by_reason_enter_the_d65_record_per_legal_entity():
    w = _world(True, record=True)
    _forge_nomination(w)
    _forge_nomination(w)
    w["send"]("peak_price", "ATK", {}, to="VICTIM")
    for aid in ("VICTIM", "ATK"):
        for t in ("2023-01-02T12:00:00Z", "2023-01-02T12:15:00Z"):
            w["send"]("telemetry", "meter", {"asset_id": f"{aid}/bess", "soc_kwh": 5.0, "meter_kw": 0.0,
                                             "net_kw": 0.0}, t=t)
    w["send"]("tick", "clock", {"final": True}, t="2023-01-02T12:30:00Z")
    rec = {}
    for who in ("VICTIM", "ATK"):
        rec[who] = next(m for m in w["eps"][who].inbox if m["msg_type"] == "delivery_record")["payload"]
    assert rec["ATK"]["this_run"]["refusals_by_reason"] == {"direct_delivery": 1, "sender_mismatch": 2}
    assert rec["ATK"]["record"]["refusals_by_reason"] == {"direct_delivery": 1, "sender_mismatch": 2}
    assert rec["VICTIM"]["this_run"]["refusals_by_reason"] == {}    # the impersonated entity is not charged
    assert w["ex"].ledger.refusals_view("ATK LLC") == {"direct_delivery": 1, "sender_mismatch": 2}


def test_full_run_attack_check_and_replay_with_the_flag(tmp_path):
    """A synthetic full run with the impersonation attacker, refusal_notice on and delivery_record on in both runs
    (owner, Nov 3): the attack check expects the 5 refusals, the set number of notices (one per (reason, minute)
    bucket, all to the attacker) and all 5 refusals in the attacker's D65 record and nobody else's; the honest lines
    are unchanged; and the trace replays exactly (the replay feeds refusals to the exchange when the flag is on)."""
    from bench import d69_check as C
    from tests.test_d69_check import _folder, _runs
    _, cfg_fn, _ = _runs(tmp_path)
    C.ATTACKER, C.VICTIM, C.EXPECT_NOTICES = "A", "B", SYNTH_NOTICES
    try:
        hon, commit = _folder(tmp_path, "hon", {**cfg_fn(run_name="hon"), "delivery_record": True})
        atk, _ = _folder(tmp_path, "atk", {**cfg_fn(run_name="atk"), "refusal_notice": True, "delivery_record": True,
                                           "attackers": [{"type": "impersonate", "agent": "A", "victim": "B"}]})
        res = C.attack(hon, atk, commit)
        assert res["match"], res["diffs"]
        row = next(r for r in res["checks"] if "notices" in r)
        assert row["refusals"] == 5 and row["notices"] == row["expected_buckets"] == SYNTH_NOTICES
        rec = next(r for r in res["checks"] if r["file"].endswith("(D65 record)"))
        assert rec["attacker_refusals"] == {"direct_delivery": 1, "sender_mismatch": 4} and rec["rows"] >= 2
        trace = next(atk.glob("exchange_*/trace.jsonl"))
        assert replay(trace)["match"]
        lines = [json.loads(s) for s in trace.read_text().splitlines()[1:]]
        assert not [l for l in lines if C.is_notice(l) and l["to"] != "A"]

        C.EXPECT_NOTICES = SYNTH_NOTICES + 1                       # a wrong notice count fails
        assert any("notices, expected" in d for d in C.attack(hon, atk, commit)["diffs"])
        C.EXPECT_NOTICES = SYNTH_NOTICES
        bad = tmp_path / "bad"                                     # a refusal charged to the victim fails
        import shutil
        shutil.copytree(atk, bad)
        tb = next(bad.glob("exchange_*/trace.jsonl"))
        out = []
        for s in tb.read_text().splitlines():
            l = json.loads(s)
            if l.get("msg_type") == "delivery_record" and l.get("to") == "B":
                l["payload"]["this_run"]["refusals_by_reason"] = {"sender_mismatch": 1}
                s = json.dumps(l, separators=(",", ":"), sort_keys=True)
            out.append(s)
        tb.write_text("\n".join(out) + "\n")
        assert any("B this_run.refusals_by_reason" in d for d in C.attack(hon, bad, commit)["diffs"])

        off = tmp_path / "off"                                     # notices on without the D65 record: fails closed
        atk2, _ = _folder(tmp_path, "atk2", {**cfg_fn(run_name="atk"), "refusal_notice": True,
                                             "attackers": [{"type": "impersonate", "agent": "A", "victim": "B"}]})
        assert any("delivery_record is off" in d for d in C.attack(hon, atk2, commit)["diffs"])
    finally:
        C.ATTACKER, C.VICTIM, C.EXPECT_NOTICES = "PVB_01", "PVB_02", 3


def test_honest_run_with_the_flag_is_identical_apart_from_the_header(tmp_path):
    from sim.run_m1 import run_variant
    from tests.test_d69_check import _runs
    from tests.test_replay import synthetic_slice
    _, cfg_fn, _ = _runs(tmp_path)
    s = synthetic_slice()
    run_variant(cfg_fn(run_name="x"), s, np.arange(2), tmp_path / "off", headroom_mw=0.03)
    run_variant({**cfg_fn(run_name="x"), "refusal_notice": True}, s, np.arange(2), tmp_path / "on",
                headroom_mw=0.03)
    a = next((tmp_path / "off").glob("exchange_*/trace.jsonl")).read_text().splitlines()
    b = next((tmp_path / "on").glob("exchange_*/trace.jsonl")).read_text().splitlines()
    assert a[1:] == b[1:]
    ha, hb = json.loads(a[0]), json.loads(b[0])
    assert hb["exchange"].pop("refusal_notice") is True
    for h in (ha, hb):
        for k in ("run_at_utc",):
            h.pop(k, None)
        h.get("config", {}).pop("refusal_notice", None)
    assert ha == hb
