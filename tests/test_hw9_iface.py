"""HW9 outside-agent interface (iface/): buffered sends, sender binding, register pre-check, and the regression test
for the boundary-probe improvement (a nomination beyond the battery's power gets an exceeds_asset_power warning)."""
from types import SimpleNamespace

import numpy as np

from exchange import messages as M
from iface.external import ExternalAgent, Session
from iface.gateway import ClientSession, Gateway
from iface.mcp_stdio import Server, power_warnings, tool_list

ISO = ["2023-01-12T06:00:00Z", "2023-01-12T06:15:00Z", "2023-01-12T06:30:00Z", "2023-01-12T06:45:00Z"]
WIN = {"start_utc": ISO[1], "end_utc": "2023-01-12T07:00:00Z"}


def fake_ext():
    inner = SimpleNamespace(id="A_1", asset_id="A_1/bess", legal_entity="A_1 Owner LLC (simulated)", zone="SYS",
                            run_id="r", iso=ISO, T=4, end_iso="2023-01-12T07:00:00Z", dt_h=0.25,
                            b=SimpleNamespace(P=100.0, CAP=400.0, s_term=200.0, s_min=40.0, s_max=360.0),
                            base_export=np.array([10.0, 20.0, -5.0, 0.0]), local_dates=["2023-01-12"] * 4)
    s = Session()
    ext = ExternalAgent(inner, s)
    s.agent, s.state = ext, "paused"
    ext.calls["GRID_OP:1"] = {"call_id": "GRID_OP:1", "zone": "SYS", "window": WIN, "binding": True,
                              "deadline_utc": ISO[1]}
    srv = Server.__new__(Server)
    srv.session, srv.trace_path, srv.calls_after_finish = s, None, []
    srv.gateway, srv.require_credential, srv.agent_id = Gateway(), False, "A_1"     # M5: calls go through the gateway
    srv.gateway.attach(ext)
    srv.cs = ClientSession(ext.legal_entity, "A_1")
    return ext, srv


def test_tools_listed_with_schemas():
    names = [t["name"] for t in tool_list()]
    assert names == ["my_asset", "register", "subscribe", "read_inbox", "read_public", "nominate",
                     "declare_availability", "declare_outage", "advance", "get_settlement"]
    assert all(t["inputSchema"]["type"] == "object" and t["description"] for t in tool_list())


def test_sends_are_queued_with_the_bound_sender_and_validated():
    ext, srv = fake_ext()
    r, err = srv.call("subscribe", {"signals": ["call_nominations"], "zones": ["SYS"]})
    assert not err and r["status"] == "queued"
    assert len(ext.outbox) == 1
    m = M.decode(ext.outbox[0])
    assert m["sender"] == "A_1" and m["to"] == "exchange" and M.validate(m) is None
    r, err = srv.call("subscribe", {"signals": ["x"], "zones": ["SYS"], "sender": "B_2"})     # no sender argument
    assert err and r["code"] == "bad_arguments" and len(ext.outbox) == 1
    r, err = srv.call("no_such_tool", {})
    assert err and r["code"] == "unknown_tool"


def test_register_grants_demonstrated_power_and_refuses_mismatch():
    """M5 (Nov 19) replaces HW9's register_precheck: an over-claim is granted at the demonstrated power (D38, stated in
    the reply); another asset or entity is refused by the gateway and nothing is sent."""
    ext, srv = fake_ext()
    srv.gateway.limits["registration_cooldown_h"] = 0.0
    r, err = srv.call("register", {"asset_id": "B_2/bess", "legal_entity": "x", "zone": "SYS", "max_kw": 50.0})
    assert err and r["code"] == "not_your_asset" and ext.outbox == []
    r, err = srv.call("register", {"asset_id": "A_1/bess", "legal_entity": "A_1 Owner LLC (simulated)", "zone": "SYS",
                                   "max_kw": 150.0})
    assert not err and r["granted_kw"] == 100.0 and len(ext.outbox) == 1
    assert M.decode(ext.outbox[0])["payload"]["mandate"]["max_kw"] == 100.0


def test_advance_refused_before_register():
    ext, srv = fake_ext()
    r, err = srv.call("advance", {})
    assert err and r["code"] == "not_registered_yet"


def test_regression_overlimit_nomination_warns_but_is_sent():
    """Boundary probe (Nov 18): a 10x over-power nomination was accepted with ok: true and surfaced only as an
    imbalance charge. Now the reply carries exceeds_asset_power; the message is still sent (exchange rules unchanged)."""
    ext, srv = fake_ext()
    idle = [20.0, -5.0, 0.0]
    r, err = srv.call("nominate", {"call_id": "GRID_OP:1", "zone": "SYS", "window": WIN,
                                   "kw": [v + 10 * 100.0 for v in idle]})
    assert not err and r["ok"] and len(ext.outbox) == 1
    w = r["warnings"][0]
    assert w["code"] == "exceeds_asset_power" and w["steps"] == 3 and w["worst_implied_battery_kw"] == 1000.0
    r, err = srv.call("nominate", {"call_id": "GRID_OP:1", "zone": "SYS", "window": WIN,
                                   "kw": [idle[0] + 100.0, idle[1] - 100.0, idle[2]]})      # at the limit: no warning
    assert not err and "warnings" not in r


def test_regression_advance_flags_open_calls_not_nominated():
    """Cold run 1 (Nov 18): the agent waited for a nonzero price before nominating; the exchange prices a zone only
    from a complete set of nominations, so prices stayed 0 for everyone. A paused status now lists the open calls
    this agent has not nominated, with the reason; the list empties once it nominates."""
    ext, srv = fake_ext()
    st = srv.status()
    assert st["open_calls_you_have_not_nominated"] == [{"call_id": "GRID_OP:1", "binding": True,
                                                        "deadline_utc": ISO[1]}]
    assert "peak price stays 0" in st["attention"]
    srv.call("nominate", {"call_id": "GRID_OP:1", "zone": "SYS", "window": WIN, "kw": [20.0, -5.0, 0.0]})
    st = srv.status()
    assert "open_calls_you_have_not_nominated" not in st and "attention" not in st
    ext.rejected.add(next(iter(ext.sent)))            # a rejected nomination does not count
    assert srv.status()["open_calls_you_have_not_nominated"][0]["call_id"] == "GRID_OP:1"
    ext.t = ISO[1]                                    # at the deadline the call is no longer open
    assert "open_calls_you_have_not_nominated" not in srv.status()


def test_dispatch_follows_binding_nomination_until_rejected():
    ext, _ = fake_ext()
    ext.iface.call("nominate", {"call_id": "GRID_OP:1", "zone": "SYS", "window": WIN, "kw": [70.0, 45.0, 50.0]})
    ext.sent[next(iter(ext.sent))]["t_utc"] = ISO[0]          # sent before the deadline
    assert ext.nominated_kw(1) == 70.0 and ext.nominated_kw(0) is None
    ext.rejected.add(next(iter(ext.sent)))
    assert ext.nominated_kw(1) is None


def test_power_warnings_ignores_unknown_window():
    ext, _ = fake_ext()
    assert power_warnings(ext, {"window": {"start_utc": "1999-01-01T00:00:00Z"}, "kw": [1e9]}) == []
