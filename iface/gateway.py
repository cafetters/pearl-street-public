"""The agent interface's one dispatch core (M5). The stdio MCP server (iface/mcp_stdio.py), the red-team harness
(redteam/) and the race test all call Gateway.call(session, tool, args), so identity, limits and refusals cover every
client the same way.

Identity (D69; Q-N9):
- a client opens a session with a credential issued to a legal entity (iface/credentials.py); the session acts only
  for an agent the metering service attests to that entity (asset_limits owner and agent_id, D69 rule 6; in the
  simulator that is the battery's own facts);
- no tool takes a sender: every send is built with sender = the session's agent, checked by exchange.messages.validate,
  and reaches the exchange through the bus with origin = that agent (D69 rule 1).

Registration end to end:
- `legal_entity` comes from the credential (an explicit one must match), the asset must be the session's own;
- D38 (first line only; the exchange's register_power_check and the metering's reference_power_cap are the defense,
  D61): the gateway sends min(claimed, demonstrated power) and the reply states claimed_kw and granted_kw;
- D39: a refused attempt (here, or an exchange reject of the register) starts a cool-down in simulated time; a retry
  before retry_after_utc is refused; the agent may re-apply lower.

Limits (simulated terms, deterministic; the wall clock is a backstop only):
- max_arg_bytes on a tool call's arguments (the stdio transport also caps a raw line, max_line_bytes, before parsing);
- max_sends_per_step: sends per simulated step;
- max_calls_per_pause: the turn cap. Past it the pause is missed (owner item 5): the agent's queued sends for that
  pause are discarded, the code is pause_deadline_missed, the sim resumes, and the agent counts as silent for the gate
  (D78's silent_reference where the run sets it). pause_wall_s is the same rule on the wall clock.
Refusals are {"ok": false, "code": <constant>, ...structured numbers}; they never echo input (D69 rule 5).

Concurrency (Q-N5): one lock serializes every call, so two clients can't interleave a check and a commit. Direct
sessions (attach_direct) put sends straight onto a bus and flush it inside the lock: that is the hosted-exchange shape
the race test drives with threads.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

from agents.tool_iface import TOOLS as BASE_TOOLS
from exchange import messages as M

LIMITS = {
    "max_line_bytes": 262_144,
    "max_arg_bytes": 65_536,
    "max_sends_per_step": 8,
    "max_calls_per_pause": 60,
    "pause_wall_s": 1800.0,
    "registration_cooldown_h": 24.0,
    "grant_limits": True,
}
_WIN = {"type": "object", "properties": {"start_utc": {"type": "string"}, "end_utc": {"type": "string"}},
        "required": ["start_utc", "end_utc"], "additionalProperties": False}
_NONE = {"type": "object", "properties": {}, "additionalProperties": False}


def _schema(props, req):
    return {"type": "object", "properties": props, "required": req, "additionalProperties": False}


_BASE = {t["name"]: t["input_schema"] for t in BASE_TOOLS}
SCHEMAS = {
    "my_asset": _NONE,
    "register": _schema({"asset_id": {"type": "string"}, "max_kw": {"type": "number"},
                         "legal_entity": {"type": "string"}, "zone": {"type": "string"}}, ["asset_id", "max_kw"]),
    "subscribe": _BASE["subscribe"],
    "read_inbox": _NONE,
    "read_public": _NONE,
    "nominate": _BASE["nominate"],
    "declare_availability": _BASE["declare_availability"],
    "declare_outage": _BASE["declare_outage"],
    "advance": _NONE,
    "get_settlement": _NONE,
}
# arm A (offers) only, for direct sessions on a hosted exchange: the hold the Q-N5 race test exercises
OFFER_SCHEMA = _schema({"need_id": {"type": "string"}, "zone": {"type": "string"}, "window": _WIN,
                        "kw": {"type": "number"}, "floor_usd_mwh": {"type": "number"}}, ["need_id", "zone", "window",
                                                                                        "kw", "floor_usd_mwh"])
SEND_TOOLS = {"register", "subscribe", "nominate", "declare_availability", "declare_outage", "offer"}
OWN_ASSET_TOOLS = {"declare_availability", "declare_outage"}


def refuse(code: str, **refs) -> tuple[dict, bool]:
    return {"ok": False, "code": code, **refs}, True


def _args_ok(schema: dict, args) -> bool:
    return (isinstance(args, dict) and set(args) <= set(schema["properties"])
            and all(k in args for k in schema.get("required", ())))


def _positive_finite(v) -> bool:
    """A number > 0 that float() holds finitely (a 400-digit int overflows float; review finding 1)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:
        f = float(v)
    except (OverflowError, ValueError):
        return False
    return f > 0 and f == f and f != float("inf")


def _plus_hours(t: str, h: float) -> str:
    d = datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(hours=h)
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


class ClientSession:
    """One authenticated client bound to one agent of one legal entity."""

    def __init__(self, entity: str, agent_id: str):
        self.entity, self.agent_id = entity, agent_id

    def __repr__(self):
        return f"ClientSession({self.agent_id})"


class _Direct:
    """An agent endpoint on a hosted exchange (no simulator pauses): sends go onto the bus and are flushed at once."""

    def __init__(self, agent_id, entity, asset, power_kw, zone, bus, clock, run_id):
        self.id, self.legal_entity, self.asset_id, self.zone = agent_id, entity, asset, zone
        self.power_kw, self.bus, self.clock, self.run_id = float(power_kw), bus, clock, run_id
        self.inbox: list[str] = []
        self.n = 0
        self.sent: dict[str, dict] = {}

    def receive(self, raw):
        self.inbox.append(raw)


class Gateway:
    def __init__(self, limits: dict | None = None, credentials=None, serialize: bool = True):
        unknown = set(limits or {}) - set(LIMITS)
        if unknown:
            raise ValueError(f"unknown gateway limits {sorted(unknown)}")
        self.limits = {**LIMITS, **(limits or {})}
        self.credentials = credentials
        self.agents: dict = {}            # agent id -> ExternalAgent (simulator) or _Direct (hosted)
        self.lock = threading.RLock() if serialize else _NoLock()
        self.log: list[dict] = []         # refusals and grants, for the run's gateway log (no input echoed)
        self._calls: dict[str, tuple[int, int]] = {}      # agent -> (pause number, calls in it)
        self._sends: dict[str, tuple[object, int]] = {}   # agent -> (step, sends in it)
        self._cooldown: dict[str, str] = {}                 # agent -> retry_after_utc
        self._reg_ids: dict[str, list[str]] = {}            # agent -> register msg_ids sent
        self._reg_rejects_seen: dict[str, set] = {}

    # ---- wiring ----
    def attach(self, ext):
        """A simulator battery (iface.external.ExternalAgent); its pause session gets the wall-clock backstop."""
        self.agents[ext.id] = ext
        s = ext.session
        if hasattr(s, "wall_s"):
            s.wall_s = float(self.limits["pause_wall_s"])
            s.on_missed = lambda via="wall_clock", aid=ext.id: self.pause_missed(aid, via)

    def attach_direct(self, agent_id, entity, asset_id, power_kw, zone, bus, clock, run_id):
        d = _Direct(agent_id, entity, asset_id, power_kw, zone, bus, clock, run_id)
        bus.attach(agent_id, d)
        self.agents[agent_id] = d
        return d

    def open_session(self, token, agent_id: str | None = None):
        """A ClientSession for the entity the credential names, or a refusal dict."""
        if token is None or token == "":
            return {"ok": False, "code": "no_credential"}
        ent = self.credentials.entity(token) if self.credentials is not None else None
        if ent is None:
            return {"ok": False, "code": "bad_credential"}
        mine = sorted(a for a, x in self.agents.items() if x.legal_entity == ent)
        if agent_id is not None:
            if agent_id not in mine:
                return {"ok": False, "code": "not_your_agent"}
            return ClientSession(ent, agent_id)
        if not mine:
            return {"ok": False, "code": "no_agent_for_entity"}
        if len(mine) > 1:
            return {"ok": False, "code": "choose_agent", "agents": mine}
        return ClientSession(ent, mine[0])

    def tools(self, direct: bool = False) -> dict:
        return {**SCHEMAS, **({"offer": OFFER_SCHEMA} if direct else {})}

    def enroll_default(self, agent_id: str) -> tuple[dict, bool]:
        """The harness's default enrollment (owner, Nov 18, item 4): register a silent agent at its demonstrated
        power, bypassing the per-pause limits, and log it. The agent stays inert (it sends nothing else)."""
        with self.lock:
            ext = self.agents[agent_id]
            if any(m["msg_type"] == "register" for m in ext.sent.values()):
                return {"ok": True, "note": "already registered"}, False
            payload = {"agent_id": ext.id, "legal_entity": ext.legal_entity,
                       "assets": [{"asset_id": ext.asset_id, "zone": ext.zone, "type": "bess"}],
                       "mandate": {"max_kw": float(ext.inner.b.P), "price_floor": 0.0, "workload_limits": None}}
            r, err = self._sim_send(ext, "register", payload)
            if not err:
                self._reg_ids.setdefault(agent_id, []).append(r["msg_id"])
            self.log.append({"agent_id": agent_id, "code": "auto_registered", "inert": True})
            return r, err

    # ---- the pause deadline (owner item 5) ----
    def pause_missed(self, agent_id: str, via: str) -> int:
        """The agent missed its pause: discard its queued sends, log a constant code, resume the sim."""
        with self.lock:
            ext = self.agents[agent_id]
            n = len(ext.outbox)
            for raw in ext.outbox:
                try:
                    ext.sent.pop(M.decode(raw)["msg_id"], None)
                except Exception:
                    pass
            ext.outbox.clear()
            self.log.append({"agent_id": agent_id, "code": "pause_deadline_missed", "via": via, "discarded_sends": n,
                             "t_utc": getattr(ext, "t", None)})
            if getattr(ext.session, "n_pause", 0) == 1 and hasattr(ext, "inner"):
                # a missed registration pause: default enrollment (owner item 4), so one silent client can't stop
                # the run (rt-0001); the agent stays inert
                self.enroll_default(agent_id)
            s = ext.session
            if hasattr(s, "resume"):
                s.resume()
            return n

    # ---- dispatch ----
    def call(self, cs, name, args, raw_bytes: int | None = None) -> tuple[dict, bool]:
        with self.lock:
            return self._call(cs, name, args, raw_bytes)

    def _call(self, cs, name, args, raw_bytes):
        if not isinstance(cs, ClientSession):
            return refuse("no_credential")
        ext = self.agents.get(cs.agent_id)
        if ext is None:
            return refuse("not_your_agent")
        direct = isinstance(ext, _Direct)
        if not direct:            # every call in a pause counts, a malformed or refused one too (review finding 7)
            s = ext.session
            pause_no = getattr(s, "n_pause", 0)
            pn, n = self._calls.get(cs.agent_id, (pause_no, 0))
            n = n + 1 if pn == pause_no else 1
            self._calls[cs.agent_id] = (pause_no, n)
            if s.state == "paused" and n > self.limits["max_calls_per_pause"] and name != "advance":
                k = self.pause_missed(cs.agent_id, "turn_cap")
                return refuse("pause_deadline_missed", max_calls=int(self.limits["max_calls_per_pause"]),
                              discarded_sends=k)
        try:
            size = raw_bytes if raw_bytes is not None else len(json.dumps(args, default=str).encode("utf-8"))
        except (TypeError, ValueError, RecursionError):
            return refuse("bad_arguments")
        if size > self.limits["max_arg_bytes"]:
            return self._refuse(cs, "message_too_large", max_bytes=int(self.limits["max_arg_bytes"]), bytes=size)
        schemas = self.tools(direct)
        if not isinstance(name, str) or name not in schemas:
            return refuse("unknown_tool")
        if not _args_ok(schemas[name], args):
            return refuse("bad_arguments")
        if not direct and name not in ("advance", "get_settlement", "my_asset"):
            st_ = ext.session.state
            if st_ in ("running", "starting"):
                return refuse("sim_running", detail="call advance until it returns status paused")
            if st_ in ("finished", "error") and name in SEND_TOOLS:
                return refuse("run_over")
        if name in SEND_TOOLS:
            over = self._count_send(cs.agent_id, ext.clock() if direct else ext.k)
            if over is not None:
                return over
        if name in OWN_ASSET_TOOLS and args.get("asset_id") != ext.asset_id:
            return self._refuse(cs, "not_your_asset")
        if name == "register":
            return self._register(cs, ext, args, direct)
        if direct:
            return self._direct(cs, ext, name, args)
        return self._sim(cs, ext, name, args)

    def _count_send(self, agent_id, step):
        st, k = self._sends.get(agent_id, (step, 0))
        k = k + 1 if st == step else 1
        if k > self.limits["max_sends_per_step"]:
            refs = {"limit": "sends_per_step", "max": int(self.limits["max_sends_per_step"]), "retry": "after advance"}
            self.log.append({"agent_id": agent_id, "code": "rate_limited", **refs})
            return refuse("rate_limited", **refs)
        self._sends[agent_id] = (step, k)
        return None

    def count_send(self, agent_id: str) -> tuple[dict, bool] | None:
        """A send outside the tool table (a round's planted channel) counts against the same per-step limit."""
        with self.lock:
            ext = self.agents[agent_id]
            return self._count_send(agent_id, ext.clock() if isinstance(ext, _Direct) else ext.k)

    def _refuse(self, cs, code, **refs):
        self.log.append({"agent_id": cs.agent_id, "code": code, **refs})
        return refuse(code, **refs)

    def _register(self, cs, ext, a, direct):
        now = ext.clock() if direct else ext.t
        self._note_register_rejects(cs.agent_id, ext, now)
        until = self._cooldown.get(cs.agent_id)
        if until is not None and now < until:
            return self._refuse(cs, "registration_cooldown", retry_after_utc=until)
        live = [m for m in self._reg_ids.get(cs.agent_id, []) if m not in getattr(ext, "rejected", set())]
        if live:
            return refuse("already_registered")
        bad = None
        if a["asset_id"] != ext.asset_id:
            bad = "not_your_asset"
        elif "legal_entity" in a and a["legal_entity"] != cs.entity:
            bad = "not_your_entity"
        elif "zone" in a and a["zone"] != ext.zone:
            bad = "not_your_zone"
        elif not _positive_finite(a["max_kw"]):
            return refuse("bad_arguments")
        if bad:
            self._cooldown[cs.agent_id] = _plus_hours(now, self.limits["registration_cooldown_h"])
            return self._refuse(cs, bad)
        claimed = float(a["max_kw"])
        power = ext.power_kw if direct else float(ext.inner.b.P)
        granted = min(claimed, power) if self.limits["grant_limits"] else claimed
        payload = {"agent_id": ext.id, "legal_entity": cs.entity,
                   "assets": [{"asset_id": ext.asset_id, "zone": ext.zone, "type": "bess"}],
                   "mandate": {"max_kw": granted, "price_floor": 0.0, "workload_limits": None}}
        r, err = (self._direct_send(ext, "register", payload) if direct else self._sim_send(ext, "register", payload))
        if not err:
            self._reg_ids.setdefault(cs.agent_id, []).append(r["msg_id"])
            r.update(claimed_kw=claimed, granted_kw=granted)
            if granted < claimed:
                self.log.append({"agent_id": cs.agent_id, "code": "granted_below_claim", "claimed_kw": claimed,
                                 "granted_kw": granted})
        return r, err

    def _note_register_rejects(self, aid, ext, now):
        rej = getattr(ext, "rejected", set())
        seen = self._reg_rejects_seen.setdefault(aid, set())
        for mid in self._reg_ids.get(aid, []):
            if mid in rej and mid not in seen:
                seen.add(mid)
                self._cooldown[aid] = _plus_hours(now, self.limits["registration_cooldown_h"])

    # ---- simulator battery (buffered sends; the sim delivers them at advance) ----
    def _sim_send(self, ext, msg_type, payload):
        return ext.iface._send(msg_type, payload), False

    def _sim(self, cs, ext, name, args):
        if name == "my_asset":
            return {"ok": True, **ext.asset_facts()}, False
        if name == "read_inbox":
            out = []
            for raw in ext.inbox:
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    continue
            ext.inbox.clear()
            return {"ok": True, "messages": out}, False
        if name in ("advance", "get_settlement"):
            return {"ok": False, "code": "transport_only"}, True     # the server/harness owns the pause
        r = ext.iface.call(name, dict(args))
        if name == "nominate" and r.get("ok"):
            w = power_warnings(ext, args)
            if w:
                r["warnings"] = w
        return r, not r.get("ok", False)


    # ---- hosted exchange (direct sessions) ----
    def _direct_send(self, d, msg_type, payload):
        d.n += 1
        msg = M.make(msg_type, d.id, "exchange", d.clock(), payload, f"{d.id}:g{d.n}", d.run_id)
        if M.validate(msg) is not None:
            return refuse("bad_message")
        d.sent[msg["msg_id"]] = msg
        d.bus.send([M.encode(msg)], d.id)
        d.bus.flush()
        return {"ok": True, "msg_id": msg["msg_id"]}, False

    def _direct(self, cs, d, name, args):
        if name == "read_inbox":
            out = [json.loads(r) for r in d.inbox]
            d.inbox.clear()
            return {"ok": True, "messages": out}, False
        if name == "my_asset":
            return {"ok": True, "agent_id": d.id, "asset_id": d.asset_id, "zone": d.zone, "power_kw": d.power_kw}, False
        if name == "offer":
            return self._direct_send(d, "offer", {"need_id": args["need_id"], "mode": "scheduled",
                                                  "zone": args["zone"], "window": args["window"], "kw": args["kw"],
                                                  "floor_usd_mwh": args["floor_usd_mwh"], "standing": False})
        if name in ("subscribe", "nominate", "declare_availability", "declare_outage"):
            return self._direct_send(d, name, dict(args))
        return refuse("transport_only")


class _NoLock:
    """Only for the Q-N5 red test: the unserialized path that shows why the lock exists."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def power_warnings(ext, args: dict) -> list[dict]:
    """HW9 improvement (boundary probe, Nov 18): the exchange accepts any finite site-net nomination (it has no
    baseline to bound it by), so a physically impossible one was accepted silently and surfaced only as an imbalance
    charge at settlement. The interface knows the owner's battery-idle site net, so it warns at send time when the
    implied battery kW (nominated - battery-idle net) is beyond the battery's power. A warning, not a refusal: the
    message is still sent, and the exchange's rules are unchanged."""
    try:
        i0 = ext.iso.index(args["window"]["start_utc"])
    except (KeyError, TypeError, ValueError):
        return []
    idle, p = ext.inner.base_export, float(ext.inner.b.P)
    over = []
    for i, v in enumerate(args.get("kw") or []):
        if i0 + i < len(idle) and isinstance(v, (int, float)) and abs(v - idle[i0 + i]) > p * 1.001 + 1e-6:
            over.append(i)
    if not over:
        return []
    worst = max(over, key=lambda i: abs(args["kw"][i] - idle[i0 + i]))
    return [{"code": "exceeds_asset_power", "steps": len(over), "first_step_index": over[0],
             "worst_step_index": worst, "worst_implied_battery_kw": round(float(args["kw"][worst] - idle[i0 + worst]), 1),
             "power_kw": round(p, 1),
             "detail": "nominated site net minus your battery-idle site net needs more battery power than you have "
                       "at these steps. The message was sent: the exchange accepts any finite schedule, but your "
                       "battery is clipped to power_kw and the shortfall beyond the free band pays imbalance."}]
