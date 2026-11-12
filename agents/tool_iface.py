"""Minimal agent interface for D61's LLM red team (brought forward from M5; plan step 2).

Typed tools, one per message an agent may send to the exchange, plus read_inbox and read_public. The interface is a
bus endpoint for one agent id: every send is built here with sender = that id (there is no argument for the sender),
checked with exchange.messages.validate before it leaves (a failure returns a constant code, never the text), and
queued on the bus, which binds sender to origin (D69). Attackers may send anything the exchange accepts (D61), so ids
such as call_id are plain arguments here; the honest LLM chooser never sees this interface.

The MCP server and its registration credential stay in M5 (D69); real red-team rounds need keys and their own
pre-registration. run_episode drives one attacker episode against any backend (the fake one in tests).
"""
from __future__ import annotations

import json

from exchange import messages as M

_WIN = {"type": "object", "properties": {"start_utc": {"type": "string"}, "end_utc": {"type": "string"}},
        "required": ["start_utc", "end_utc"], "additionalProperties": False}


def _tool(name, desc, props, req):
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": req, "additionalProperties": False}}


TOOLS = [
    _tool("register", "Register this agent and one battery asset with a mandate.",
          {"asset_id": {"type": "string"}, "legal_entity": {"type": "string"}, "zone": {"type": "string"},
           "max_kw": {"type": "number"}}, ["asset_id", "legal_entity", "zone", "max_kw"]),
    _tool("subscribe", "Subscribe to signals for zones.",
          {"signals": {"type": "array", "items": {"type": "string"}},
           "zones": {"type": "array", "items": {"type": "string"}}}, ["signals", "zones"]),
    _tool("nominate", "Nominate a per-step kW schedule for a call.",
          {"call_id": {"type": "string"}, "zone": {"type": "string"}, "window": _WIN,
           "kw": {"type": "array", "items": {"type": "number"}}}, ["call_id", "zone", "window", "kw"]),
    _tool("declare_availability", "Declare a day's availability for an asset.",
          {"asset_id": {"type": "string"}, "date": {"type": "string"}, "power_kw": {"type": "number"},
           "reserve_kwh": {"type": "number"}, "planned_outage": {"type": "boolean"}},
          ["asset_id", "date", "power_kw", "reserve_kwh", "planned_outage"]),
    _tool("declare_outage", "Declare a forced outage window for an asset.",
          {"asset_id": {"type": "string"}, "window": _WIN}, ["asset_id", "window"]),
    _tool("deregister", "Deregister this agent from a later date.",
          {"effective_date": {"type": "string"}}, ["effective_date"]),
    _tool("read_inbox", "Messages the exchange sent this agent since the last read.", {}, []),
    _tool("read_public", "Public data: published day-ahead prices so far.", {}, []),
]
_SCHEMA = {t["name"]: t["input_schema"] for t in TOOLS}


def _args_ok(name: str, args) -> bool:
    s = _SCHEMA[name]
    return isinstance(args, dict) and set(args) <= set(s["properties"]) and all(k in args for k in s["required"])


class ToolInterface:
    def __init__(self, agent_id: str, run_id: str, bus, clock, public):
        self.id, self.run_id, self.bus, self.clock, self.public = agent_id, run_id, bus, clock, public
        self.inbox: list[str] = []
        self._n = 0
        self.sent: list[str] = []

    def receive(self, raw: str):
        self.inbox.append(raw)

    def _send(self, msg_type: str, payload: dict) -> dict:
        self._n += 1
        msg = M.make(msg_type, self.id, "exchange", self.clock(), payload, f"{self.id}:t{self._n}", self.run_id)
        if M.validate(msg) is not None:
            return {"ok": False, "code": "bad_message"}
        raw = M.encode(msg)
        self.bus.send([raw], self.id)
        self.bus.flush()
        self.sent.append(msg_type)
        return {"ok": True, "msg_id": msg["msg_id"]}

    def call(self, name: str, args) -> dict:
        if name not in _SCHEMA:
            return {"ok": False, "code": "unknown_tool"}
        if not _args_ok(name, args):
            return {"ok": False, "code": "bad_arguments"}
        a = args
        if name == "read_inbox":
            out = []
            for raw in self.inbox:
                try:
                    out.append(M.decode(raw))
                except Exception:
                    continue
            self.inbox = []
            return {"ok": True, "messages": out}
        if name == "read_public":
            return {"ok": True, "public": self.public()}
        if name == "register":
            return self._send("register", {"agent_id": self.id, "legal_entity": a["legal_entity"],
                                           "assets": [{"asset_id": a["asset_id"], "zone": a["zone"], "type": "bess"}],
                                           "mandate": {"max_kw": a["max_kw"], "price_floor": 0.0,
                                                       "workload_limits": None}})
        if name == "deregister":
            return self._send("deregister", {"agent_id": self.id, "effective_date": a["effective_date"]})
        return self._send(name, dict(a))


EPISODE_SYSTEM = ("You operate one agent on an electricity flexibility exchange through tools. Each turn, reply with "
                  "one JSON object {\"tool\": <name>, \"args\": {...}}, or {\"tool\": \"done\", \"args\": {}} to stop. "
                  "Tools: " + ", ".join(t["name"] for t in TOOLS) + ".")


def run_episode(backend, iface: ToolInterface, model: str, max_turns: int = 50) -> dict:
    from agents.llm.backends import Request
    provider, _, mname = model.partition("/")
    last, log, bad = {"ok": True, "note": "episode start"}, [], 0
    for turn in range(1, max_turns + 1):
        req = Request(provider=provider, model=mname, system=EPISODE_SYSTEM, user=json.dumps(last, sort_keys=True),
                      kind="tool_turn", prompt_version="redteam-0")
        resp = backend.complete(req)
        try:
            act = json.loads(resp.text or "")
            name, args = act["tool"], act.get("args", {})
            if not isinstance(name, str):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            bad += 1
            last = {"ok": False, "code": "unparseable_reply"}
            log.append({"turn": turn, "bad": True})
            continue
        if name == "done":
            log.append({"turn": turn, "tool": "done"})
            return {"turns": turn, "ended": "done", "bad_outputs": bad, "log": log}
        last = iface.call(name, args)
        log.append({"turn": turn, "tool": name, "ok": last.get("ok"), "code": last.get("code")})
    return {"turns": max_turns, "ended": "max_turns", "bad_outputs": bad, "log": log}
