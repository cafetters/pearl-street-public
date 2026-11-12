"""M4 step 2: the minimal agent interface for D61's red team (plan test 13). Typed tools, one per message an agent may
send, plus read_inbox and read_public; every send goes through the bus with the sender bound (D69) and is checked by
exchange.messages.validate first."""
import json

import pytest

from agents.tool_iface import TOOLS, ToolInterface, run_episode
from agents.llm.backends import FakeBackend
from exchange import messages as M
from sim.bus import Bus


class Log:
    def __init__(self):
        self.rows = []

    def log(self, raw, ev):
        self.rows.append((ev, raw))

    def refuse(self, raw, msg, origin, reason):
        self.rows.append(("refused", reason))


class EchoExchange:
    """Stands in for the exchange: validates like it and answers a reject or nothing."""
    refusal_notice = False

    def __init__(self):
        self.got = []

    def handle(self, raw):
        m = M.decode(raw)
        self.got.append(m)
        if M.validate(m) is not None:
            return [M.encode(M.make("reject", "exchange", m["sender"], m["t_utc"],
                                    {"ref_msg_id": m["msg_id"], "code": "bad_message", "detail": "x", "refs": {}},
                                    "ex:1", m["run_id"]))]
        return []


def _setup():
    ex = EchoExchange()
    bus = Bus(Log(), ex)
    it = ToolInterface("ATK_1", "run1", bus, clock=lambda: "2023-01-12T10:00:00Z",
                       public=lambda: {"da_usd_mwh": [30.0, 40.0]})
    bus.attach("ATK_1", it)
    return ex, bus, it


def test_tools_map_one_to_one_onto_agent_message_types():
    names = {t["name"] for t in TOOLS}
    assert {"register", "subscribe", "nominate", "declare_availability", "declare_outage", "deregister",
            "read_inbox", "read_public"} == names
    for t in TOOLS:
        assert t["input_schema"]["type"] == "object"
        assert t["input_schema"].get("additionalProperties") is False


def test_every_send_carries_the_bound_sender_and_reaches_the_exchange():
    ex, bus, it = _setup()
    r = it.call("subscribe", {"signals": ["call_nominations"], "zones": ["SYS"]})
    assert r["ok"] is True
    assert ex.got[-1]["sender"] == "ATK_1" and ex.got[-1]["msg_type"] == "subscribe"


def test_there_is_no_way_to_set_another_sender():
    ex, bus, it = _setup()
    r = it.call("subscribe", {"signals": ["x"], "zones": ["SYS"], "sender": "GRID_OP"})
    assert r == {"ok": False, "code": "bad_arguments"}
    assert not ex.got
    # a crafted raw message pushed from this endpoint with another sender is refused by the bus (D69 rule 1)
    forged = M.encode(M.make("subscribe", "GRID_OP", "exchange", "2023-01-12T10:00:00Z",
                             {"signals": ["x"], "zones": ["SYS"]}, "GRID_OP:9", "run1"))
    bus.send([forged], "ATK_1")
    bus.flush()
    assert ("refused", "sender_mismatch") in bus.logger.rows and not ex.got


def test_out_of_schema_arguments_are_refused_before_sending():
    ex, bus, it = _setup()
    r = it.call("nominate", {"call_id": "GRID_OP:4", "zone": "SYS",
                             "window": {"start_utc": "2023-01-12T10:00:00Z", "end_utc": "2023-01-12T11:00:00Z"},
                             "kw": ["lots"]})
    assert r == {"ok": False, "code": "bad_message"} and not ex.got
    assert it.call("no_such_tool", {}) == {"ok": False, "code": "unknown_tool"}


def test_read_tools_return_the_inbox_and_public_data():
    ex, bus, it = _setup()
    it.receive(M.encode(M.make("imbalance", "exchange", "ATK_1", "2023-01-12T10:00:00Z",
                               {"call_id": "c", "window": {"start_utc": "a", "end_utc": "b"}, "deviation_kwh": 0.0,
                                "charge_usd": 0.0}, "ex:3", "run1")))
    got = it.call("read_inbox", {})
    assert got["ok"] and len(got["messages"]) == 1 and got["messages"][0]["msg_type"] == "imbalance"
    assert it.call("read_inbox", {})["messages"] == []
    assert it.call("read_public", {})["public"] == {"da_usd_mwh": [30.0, 40.0]}


def test_a_scripted_fake_attacker_episode_completes():
    ex, bus, it = _setup()
    script = [{"tool": "subscribe", "args": {"signals": ["call_nominations"], "zones": ["SYS"]}},
              {"tool": "read_public", "args": {}},
              {"tool": "nominate", "args": {"call_id": "GRID_OP:4", "zone": "SYS",
                                            "window": {"start_utc": "2023-01-12T10:00:00Z",
                                                       "end_utc": "2023-01-12T10:30:00Z"}, "kw": [1.0, 2.0]}},
              {"tool": "done", "args": {}}]
    be = FakeBackend(script=[json.dumps(s) for s in script])
    ep = run_episode(be, it, model="anthropic/claude-sonnet-5-5", max_turns=10)
    assert ep["turns"] == 4 and ep["ended"] == "done"
    assert [m["msg_type"] for m in ex.got] == ["subscribe", "nominate"]
    assert all(m["sender"] == "ATK_1" for m in ex.got)


def test_an_episode_stops_at_max_turns_and_on_bad_output():
    ex, bus, it = _setup()
    be = FakeBackend(script=["not json"] * 5)
    ep = run_episode(be, it, model="anthropic/claude-sonnet-5-5", max_turns=3)
    assert ep["turns"] == 3 and ep["ended"] == "max_turns" and ep["bad_outputs"] == 3
