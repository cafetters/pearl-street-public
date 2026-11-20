"""M5 gateway (iface/gateway.py): the one dispatch core behind the stdio MCP server and the red-team harness.

Identity (D69, Q-N9): a credential per legal entity; a session acts only for an agent the metering service attests to
that entity; no tool takes a sender. Registration end to end: legal_entity from the credential, the asset must be the
session's own, D38's grant (min(claimed, demonstrated power), stated in the reply), D39's cool-down after a refused
attempt. Limits: argument bytes, sends per simulated step, tool calls per pause, the pause deadline (owner item 5).
Refusals are constant codes with structured numbers and never echo input (D69 rule 5). All values synthetic."""
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from exchange import messages as M
from iface.credentials import CredentialStore
from iface.external import ExternalAgent, Session
from iface.gateway import Gateway, ClientSession

ISO = ["2023-01-12T06:00:00Z", "2023-01-12T06:15:00Z", "2023-01-12T06:30:00Z", "2023-01-12T06:45:00Z"]
WIN = {"start_utc": ISO[1], "end_utc": "2023-01-12T07:00:00Z"}


def _inner(aid, power=100.0):
    return SimpleNamespace(id=aid, asset_id=f"{aid}/bess", legal_entity=f"{aid} Owner LLC (simulated)", zone="SYS",
                           run_id="r", iso=ISO, T=4, end_iso="2023-01-12T07:00:00Z", dt_h=0.25,
                           b=SimpleNamespace(P=power, CAP=400.0, s_term=200.0, s_min=40.0, s_max=360.0),
                           base_export=np.array([10.0, 20.0, -5.0, 0.0]), local_dates=["2023-01-12"] * 4)


def setup(limits=None, agents=("A_1", "B_2")):
    store = CredentialStore()
    gw = Gateway(limits=limits or {}, credentials=store)
    toks, exts = {}, {}
    for aid in agents:
        s = Session()
        ext = ExternalAgent(_inner(aid), s)
        s.agent, s.state = ext, "paused"
        ext.calls["GRID_OP:1"] = {"call_id": "GRID_OP:1", "zone": "SYS", "window": WIN, "binding": True,
                                  "deadline_utc": ISO[2]}
        gw.attach(ext)
        toks[aid] = store.issue(ext.legal_entity)
        exts[aid] = ext
    return gw, toks, exts


def _reg(gw, cs, **kw):
    return gw.call(cs, "register", {"asset_id": cs.agent_id + "/bess", "max_kw": 100.0, **kw})


# ---- identity ----
def test_no_or_bad_credential_is_refused_with_a_constant_code():
    gw, toks, _ = setup()
    assert gw.open_session(None)["code"] == "no_credential"
    bad = gw.open_session(toks["A_1"] + "x")
    assert bad == {"ok": False, "code": "bad_credential"}
    r, err = gw.call(None, "my_asset", {})
    assert err and r["code"] == "no_credential"


def test_a_credential_binds_its_own_entitys_agent_only():
    gw, toks, _ = setup()
    cs = gw.open_session(toks["A_1"])
    assert isinstance(cs, ClientSession) and cs.agent_id == "A_1" and cs.entity == "A_1 Owner LLC (simulated)"
    other = gw.open_session(toks["A_1"], agent_id="B_2")         # not attested to A_1's entity
    assert other["code"] == "not_your_agent"
    orphan = CredentialStore()
    gw2 = Gateway(limits={}, credentials=orphan)
    assert gw2.open_session(orphan.issue("Nobody LLC"))["code"] == "no_agent_for_entity"


def test_no_tool_takes_a_sender_and_sends_carry_the_bound_agent():
    gw, toks, exts = setup()
    cs = gw.open_session(toks["A_1"])
    r, err = gw.call(cs, "subscribe", {"signals": ["call_nominations"], "zones": ["SYS"], "sender": "B_2"})
    assert err and r["code"] == "bad_arguments" and exts["A_1"].outbox == []
    r, err = gw.call(cs, "subscribe", {"signals": ["call_nominations"], "zones": ["SYS"]})
    assert not err
    m = M.decode(exts["A_1"].outbox[0])
    assert m["sender"] == "A_1" and M.validate(m) is None and exts["B_2"].outbox == []


# ---- registration end to end ----
def test_register_takes_the_entity_from_the_credential_and_grants_demonstrated_power():
    gw, toks, exts = setup()
    cs = gw.open_session(toks["A_1"])
    r, err = _reg(gw, cs, max_kw=300.0)
    assert not err and r["claimed_kw"] == 300.0 and r["granted_kw"] == 100.0 and r["status"] == "queued"
    m = M.decode(exts["A_1"].outbox[0])
    assert m["payload"]["legal_entity"] == "A_1 Owner LLC (simulated)"
    assert m["payload"]["mandate"]["max_kw"] == 100.0
    r, err = _reg(gw, cs)
    assert err and r["code"] == "already_registered"


def test_register_refuses_another_entitys_asset_or_entity_and_sends_nothing():
    gw, toks, exts = setup(limits={"registration_cooldown_h": 0.0})      # no cool-down here (tested below)
    cs = gw.open_session(toks["A_1"])
    r, err = gw.call(cs, "register", {"asset_id": "B_2/bess", "max_kw": 50.0})
    assert err and r["code"] == "not_your_asset" and exts["A_1"].outbox == []
    r, err = gw.call(cs, "register", {"asset_id": "A_1/bess", "max_kw": 50.0, "legal_entity": "B_2 Owner LLC"})
    assert err and r["code"] == "not_your_entity"
    r, err = gw.call(cs, "register", {"asset_id": "A_1/bess", "max_kw": -5.0})
    assert err and r["code"] == "bad_arguments"


def test_grant_off_sends_the_claim_as_is():
    """grant_limits: false models the interface without D38's first line (the red team's fake exploit, rt-0000)."""
    gw, toks, exts = setup(limits={"grant_limits": False})
    cs = gw.open_session(toks["A_1"])
    r, err = _reg(gw, cs, max_kw=300.0)
    assert not err and r["granted_kw"] == 300.0
    assert M.decode(exts["A_1"].outbox[0])["payload"]["mandate"]["max_kw"] == 300.0


def test_cooldown_after_a_refused_attempt():
    gw, toks, exts = setup(limits={"registration_cooldown_h": 0.5})
    cs = gw.open_session(toks["A_1"])
    gw.call(cs, "register", {"asset_id": "B_2/bess", "max_kw": 50.0})            # refused: starts the cool-down
    r, err = _reg(gw, cs)
    assert err and r["code"] == "registration_cooldown" and r["retry_after_utc"] == ISO[2]
    exts["A_1"].t = ISO[2]                                                         # simulated time moves on
    r, err = _reg(gw, cs, max_kw=80.0)                                             # may re-apply lower
    assert not err and r["granted_kw"] == 80.0


def test_an_exchange_reject_of_the_register_also_starts_the_cooldown():
    gw, toks, exts = setup(limits={"registration_cooldown_h": 0.5})
    cs = gw.open_session(toks["A_1"])
    r, _ = _reg(gw, cs)
    rej = M.make("reject", "exchange", "A_1", ISO[0], {"ref_msg_id": r["msg_id"], "code": "unauthorized_sender",
                                                        "detail": "x", "refs": {}}, "exchange:9", "r")
    exts["A_1"].receive(M.encode(rej))
    exts["A_1"].outbox.clear()
    r, err = _reg(gw, cs)
    assert err and r["code"] == "registration_cooldown"


def test_declarations_only_for_the_sessions_own_asset():
    gw, toks, exts = setup()
    cs = gw.open_session(toks["A_1"])
    r, err = gw.call(cs, "declare_outage", {"asset_id": "B_2/bess", "window": WIN})
    assert err and r["code"] == "not_your_asset" and exts["A_1"].outbox == []


# ---- limits ----
def test_oversized_arguments_are_refused_without_echo():
    gw, toks, exts = setup(limits={"max_arg_bytes": 2000})
    cs = gw.open_session(toks["A_1"])
    marker = "IGNORE-PREVIOUS-" + "z" * 3000
    r, err = gw.call(cs, "subscribe", {"signals": [marker], "zones": ["SYS"]})
    assert err and r["code"] == "message_too_large" and r["max_bytes"] == 2000
    assert "IGNORE" not in json.dumps(r) and exts["A_1"].outbox == []


def test_refusals_never_echo_input():
    gw, toks, _ = setup()
    cs = gw.open_session(toks["A_1"])
    for name, args in [("nominate", {"call_id": "<inject>", "zone": "SYS", "window": WIN, "kw": [1, 2, 3],
                                     "note": "<inject>"}),
                       ("no_such_<inject>", {}), ("register", {"asset_id": "<inject>", "max_kw": 1.0})]:
        r, err = gw.call(cs, name, args)
        assert err and "<inject>" not in json.dumps(r)


def test_sends_per_simulated_step_are_rate_limited():
    gw, toks, exts = setup(limits={"max_sends_per_step": 3})
    cs = gw.open_session(toks["A_1"])
    sub = {"signals": ["call_nominations"], "zones": ["SYS"]}
    for _ in range(3):
        assert not gw.call(cs, "subscribe", sub)[1]
    r, err = gw.call(cs, "subscribe", sub)
    assert err and r["code"] == "rate_limited" and r["limit"] == "sends_per_step" and r["max"] == 3
    assert len(exts["A_1"].outbox) == 3
    exts["A_1"].k += 1                                       # next simulated step: the count starts again
    assert not gw.call(cs, "subscribe", sub)[1]


def test_tool_calls_per_pause_cap_is_the_pause_deadline():
    """Owner item 5: the turn cap. Past it the pause is missed: queued sends are discarded, the code is constant, and
    the sim resumes (the agent counts as silent for that gate)."""
    gw, toks, exts = setup(limits={"max_calls_per_pause": 4})
    cs = gw.open_session(toks["A_1"])
    ext = exts["A_1"]
    gw.call(cs, "subscribe", {"signals": ["call_nominations"], "zones": ["SYS"]})
    for _ in range(3):
        assert not gw.call(cs, "my_asset", {})[1]
    r, err = gw.call(cs, "my_asset", {})
    assert err and r["code"] == "pause_deadline_missed" and r["discarded_sends"] == 1
    assert ext.outbox == [] and not ext.sent and ext.session._go
    assert gw.log[-1]["code"] == "pause_deadline_missed" and gw.log[-1]["agent_id"] == "A_1"


def test_wall_clock_backstop_resumes_a_pause_nobody_advances():
    """Owner item 5: the wall-clock backstop (config) for a client that goes quiet mid-pause."""
    gw, toks, exts = setup(limits={"pause_wall_s": 0.2})
    ext = exts["A_1"]
    ext.session.state = "running"
    ext.outbox.append(M.encode(M.make("subscribe", "A_1", "exchange", ISO[0], {"signals": [], "zones": []},
                                      "A_1:t9", "r")))
    t0 = time.time()
    th = threading.Thread(target=ext.session.pause, args=({"step": 1, "reason": "test"},))
    th.start()
    th.join(5.0)
    assert not th.is_alive() and time.time() - t0 < 3.0
    missed = [x for x in gw.log if x["code"] == "pause_deadline_missed"]
    assert len(missed) == 1 and missed[0]["via"] == "wall_clock" and missed[0]["discarded_sends"] == 1
    # it was the registration pause, so the agent is enrolled by default (owner item 4; rt-0001), inert
    assert gw.log[-1]["code"] == "auto_registered"
    assert [M.decode(r)["msg_type"] for r in ext.outbox] == ["register"]


def test_channel_sends_count_against_the_same_per_step_limit():
    gw, toks, exts = setup(limits={"max_sends_per_step": 2})
    cs = gw.open_session(toks["A_1"])
    assert not gw.call(cs, "subscribe", {"signals": ["call_nominations"], "zones": ["SYS"]})[1]
    assert gw.count_send("A_1") is None                       # a planted-channel note: the second send this step
    r, err = gw.count_send("A_1")
    assert err and r["code"] == "rate_limited" and r["limit"] == "sends_per_step"
    r, err = gw.call(cs, "subscribe", {"signals": ["call_nominations"], "zones": ["SYS"]})
    assert err and r["code"] == "rate_limited"


def test_a_claim_float_cannot_hold_is_bad_arguments_not_a_crash():
    """Review finding 1: a 400-digit integer claim passed the type checks and crashed float()."""
    gw, toks, exts = setup()
    cs = gw.open_session(toks["A_1"])
    r, err = gw.call(cs, "register", {"asset_id": "A_1/bess", "max_kw": 10 ** 400})
    assert err and r == {"ok": False, "code": "bad_arguments"} and exts["A_1"].outbox == []


def test_malformed_calls_count_toward_the_pause_cap():
    """Review finding 7: refused calls (bad arguments, unknown tools, oversized) count too, so a client can't hold a
    pause open with them until the wall clock."""
    gw, toks, exts = setup(limits={"max_calls_per_pause": 3})
    cs = gw.open_session(toks["A_1"])
    for _ in range(3):
        assert gw.call(cs, "nominate", {"extra": 1})[0]["code"] == "bad_arguments"
    r, err = gw.call(cs, "no_such_tool", {})
    assert err and r["code"] == "pause_deadline_missed"
