"""One battery operated by an outside agent, inside sim/run_m1.py's loop (HW9; M5's MCP server pulled forward).

The run config names the battery under `attackers` with type "external" (run_m1's only per-agent wrapper hook; an
external agent is untrusted by design, D63, not malicious). attacks.wrap_attacker hands the honest BatteryAgent to
wrap_external, which returns an ExternalAgent bound to the server's Session.

- Messages: the outside agent queues them through ExternalIface (agents.tool_iface.ToolInterface with buffered
  sends: validate now, send when the sim resumes). run_m1 sends what ExternalAgent returns with origin = this agent's
  id, so the bus binds the sender (D69); there is no argument for the sender.
- Pauses: at registration (step 0), at any step where a new call_nominations, reject, imbalance or peak_settle reached
  the agent, and at price round 1 and the final price round of each call. Between pauses the latest nomination stands.
- Dispatch (no message carries it, so the rule is fixed and documented): during the window of a binding call the
  agent nominated (and the exchange did not reject), the battery follows the nomination, battery kW = nominated site
  net - battery-idle site net, clipped to +-power; the physics clips state of charge. Everywhere else it runs the
  honest agent's acting-alone plan (the reference planner at published day-ahead prices; it never sees the
  exchange's messages).
"""
from __future__ import annotations

import threading
import time

import numpy as np

from agents.tool_iface import ToolInterface
from exchange import messages as M

PAUSE_TYPES = {"call_nominations", "reject", "imbalance", "peak_settle"}
_SESSION: "Session | None" = None


class Session:
    """Hand-off between the sim thread (pause) and the server thread (advance). Exactly one side runs at a time."""

    def __init__(self):
        self.cv = threading.Condition()
        self.state = "starting"        # starting | running | paused | finished | error
        self.info: dict = {}
        self.n_pause = 0
        self._go = False
        self.detached = False          # stdin closed: the run finishes with the agent silent
        self.error: str | None = None
        self.agent: "ExternalAgent | None" = None
        self.wall_s: float | None = None    # M5: the gateway sets its pause_wall_s backstop
        self.on_missed = None

    def pause(self, info: dict):
        """Park the sim thread until the agent advances. M5 (owner item 5): with wall_s set (the gateway's
        pause_wall_s), a pause nobody advances ends at the deadline; on_missed (the gateway) then discards the agent's
        queued sends and logs pause_deadline_missed before the sim resumes. on_missed runs outside the condition's
        lock, so it can't deadlock with a client thread inside the gateway."""
        missed = False
        with self.cv:
            if self.detached:
                return
            self.state, self.info = "paused", info
            self.n_pause += 1
            self.cv.notify_all()
            deadline = None if not self.wall_s else time.monotonic() + self.wall_s
            while not self._go and not self.detached:
                if deadline is None:
                    self.cv.wait()
                    continue
                rem = deadline - time.monotonic()
                if rem <= 0:
                    missed = True
                    break
                self.cv.wait(rem)
            self._go = False
            self.state = "running"
        if missed and self.on_missed is not None:
            self.on_missed("wall_clock")

    def resume(self):
        """End the current pause (the gateway's turn cap). A no-op unless paused, so it never skips a later pause."""
        with self.cv:
            if self.state == "paused":
                self._go = True
                self.cv.notify_all()

    def finish(self, error: str | None = None):
        with self.cv:
            self.state, self.error = ("error", error) if error else ("finished", None)
            self.cv.notify_all()

    def wait_first_pause(self, timeout: float) -> str:
        with self.cv:
            self.cv.wait_for(lambda: self.state in ("paused", "finished", "error"), timeout)
            return self.state

    def advance(self, timeout: float) -> str:
        """Resume if paused, then wait for the next pause (or the end) at most `timeout` seconds."""
        with self.cv:
            n0 = self.n_pause
            if self.state == "paused":
                self._go = True
                self.cv.notify_all()
            self.cv.wait_for(lambda: (self.state == "paused" and self.n_pause > n0)
                             or self.state in ("finished", "error"), timeout)
            return self.state

    def detach(self):
        with self.cv:
            self.detached = True
            self._go = True
            self.cv.notify_all()


class InlineSession:
    """A pause handled in the sim's own thread (the red-team harness, M5): pause calls on_pause(agent_id, info), which
    runs the agent's whole turn through the gateway and returns. advance (or the gateway's turn cap) calls resume."""

    def __init__(self, on_pause):
        self.on_pause = on_pause
        self.state, self.info, self.n_pause = "starting", {}, 0
        self._go = False
        self.error: str | None = None
        self.agent: "ExternalAgent | None" = None

    def pause(self, info: dict):
        self.state, self.info, self._go = "paused", info, False
        self.n_pause += 1
        self.on_pause(self.agent.id, info)
        self.state = "running"

    def resume(self):
        if self.state == "paused":
            self._go = True

    def finish(self, error: str | None = None):
        self.state, self.error = ("error", error) if error else ("finished", None)


_FACTORY = None          # agent id -> session (M5: one session per external agent)
_GATEWAY = None


def set_session(s: "Session | None"):
    """HW9's single-agent server: every external agent shares this one session (there is one)."""
    global _SESSION, _FACTORY
    _SESSION = s
    _FACTORY = None if s is None else (lambda aid: s)


def set_interface(factory, gateway=None):
    """M5: a session per external agent from `factory(agent_id)`, each attached to `gateway`."""
    global _FACTORY, _GATEWAY
    _FACTORY, _GATEWAY = factory, gateway


def wrap_external(agent, spec: dict, cfg: dict):
    if _FACTORY is None:
        raise RuntimeError("attackers type 'external' needs an interface session (python -m iface.mcp_stdio)")
    s = _FACTORY(agent.id)
    ext = ExternalAgent(agent, s)
    s.agent = ext
    if _GATEWAY is not None:
        _GATEWAY.attach(ext)
    return ext


class ExternalIface(ToolInterface):
    """ToolInterface whose sends are queued for the next resume instead of flushed at once."""

    def __init__(self, ext: "ExternalAgent"):
        super().__init__(ext.id, ext.run_id, bus=None, clock=lambda: ext.t, public=ext.published_prices)
        self.ext = ext

    def _send(self, msg_type: str, payload: dict) -> dict:
        self._n += 1
        msg = M.make(msg_type, self.id, "exchange", self.clock(), payload, f"{self.id}:t{self._n}", self.run_id)
        if M.validate(msg) is not None:
            return {"ok": False, "code": "bad_message"}
        self.ext.outbox.append(M.encode(msg))
        self.ext.sent[msg["msg_id"]] = msg
        self.sent.append(msg_type)
        return {"ok": True, "msg_id": msg["msg_id"], "status": "queued",
                "note": "sent to the exchange when you call advance; a reject (if any) then appears in read_inbox"}


class ExternalAgent:
    def __init__(self, inner, session: Session):
        self.inner, self.session = inner, session
        self.id, self.asset_id, self.legal_entity = inner.id, inner.asset_id, inner.legal_entity
        self.zone, self.run_id, self.iso, self.T = inner.zone, inner.run_id, inner.iso, inner.T
        self.end_iso, self.dt_h = inner.end_iso, inner.dt_h
        self.margin_log: list = []
        self.margin_compare_secs = 0.0
        self.k, self.t, self.soc = 0, inner.iso[0], float(inner.b.s_term)
        self.price_version, self.price = 1, None
        self.inbox: list[str] = []         # undelivered to the outside agent (read_inbox drains)
        self.received: list[dict] = []     # everything the exchange sent this agent, decoded
        self.outbox: list[str] = []
        self.sent: dict[str, dict] = {}    # msg_id -> message the outside agent queued
        self.rejected: set[str] = set()
        self.calls: dict[str, dict] = {}
        self._events: list[str] = []
        self.registered_sent = False
        self.n_followed = 0                # steps dispatched to a binding nomination
        self._last_round_seen = None
        self.iface = ExternalIface(self)

    def __getattr__(self, name):           # anything else run_m1 reads (counters) comes from the honest agent
        return getattr(self.inner, name)

    # ---- from the bus ----
    def receive(self, raw: str):
        self.inbox.append(raw)
        try:
            m = M.decode(raw)
        except Exception:
            return
        if not isinstance(m, dict) or m.get("sender") != "exchange":
            return
        self.received.append(m)
        typ, p = m.get("msg_type"), m.get("payload") or {}
        if typ == "call_nominations":
            self.calls[p["call_id"]] = p
        if typ == "reject":
            self.rejected.add(p.get("ref_msg_id"))
        if typ in PAUSE_TYPES:
            self._events.append(typ)

    # ---- what the outside agent may read ----
    def published_prices(self) -> list[dict]:
        """Day-ahead prices published so far ($/MWh per step): days 1 .. price_version of the window."""
        if self.price is None:
            return []
        days = list(dict.fromkeys(self.inner.local_dates))[:self.price_version]
        return [{"t_utc": t, "usd_mwh": float(p)} for t, p, d in zip(self.iso, self.price, self.inner.local_dates)
                if d in days]

    def asset_facts(self) -> dict:
        b = self.inner.b
        return {"agent_id": self.id, "asset_id": self.asset_id, "legal_entity": self.legal_entity, "zone": self.zone,
                "power_kw": float(b.P), "capacity_kwh": float(b.CAP), "soc_min_kwh": float(b.s_min),
                "soc_max_kwh": float(b.s_max), "soc_now_kwh": float(self.soc), "step_minutes": round(self.dt_h * 60),
                "window": {"start_utc": self.iso[0], "end_utc": self.end_iso}, "now_utc": self.t,
                "battery_idle_site_net_kw": [round(float(v), 3) for v in self.inner.base_export],
                "battery_idle_note": "site net kW per step with the battery idle (PV - load; + = export, - = import)"}

    def nominated_kw(self, k: int) -> float | None:
        """The binding nomination in force at step k: the latest non-rejected nomination, sent before the deadline,
        for a binding call whose window holds step k."""
        t = self.iso[k]
        best = None
        for mid, msg in self.sent.items():
            if msg["msg_type"] != "nominate" or mid in self.rejected:
                continue
            p = msg["payload"]
            c = self.calls.get(p["call_id"])
            if not c or not c.get("binding") or msg["t_utc"] >= c["deadline_utc"]:
                continue
            w = c["window"]
            if not (w["start_utc"] <= t < w["end_utc"]):
                continue
            i0 = self.iso.index(w["start_utc"])
            if k - i0 < len(p["kw"]):
                best = float(p["kw"][k - i0])      # dict order = send order, so the last one wins
        return best

    def open_calls_not_nominated(self) -> list[dict]:
        """Calls still open (deadline after now) with no non-rejected nomination from this agent."""
        mine = {m["payload"]["call_id"] for mid, m in self.sent.items()
                if m["msg_type"] == "nominate" and mid not in self.rejected}
        return [{"call_id": c["call_id"], "binding": bool(c.get("binding")), "deadline_utc": c["deadline_utc"]}
                for c in self.calls.values() if c["deadline_utc"] > self.t and c["call_id"] not in mine]

    def _drain(self) -> list[str]:
        out, self.outbox = self.outbox, []
        return out

    # ---- the run_m1 agent protocol ----
    def start(self, k: int) -> list[str]:
        self.k, self.t = k, self.iso[k]
        self.session.pause({"step": k, "t_utc": self.t, "reason": "registration: call register (and subscribe), "
                            "then advance"})
        return self._drain()

    def act(self, k: int, soc: float, price_version: int, price):
        self.k, self.t, self.soc = k, self.iso[k], float(soc)
        self.price_version, self.price = price_version, np.asarray(price, float)
        if self._events:
            ev, self._events = sorted(set(self._events)), []
            self.session.pause({"step": k, "t_utc": self.t, "reason": "new exchange messages: " + ", ".join(ev)})
        _, (c, d, x) = self.inner.act(k, soc, price_version, price)     # the acting-alone plan; its messages dropped
        nom = self.nominated_kw(k)
        if nom is not None:
            b = float(np.clip(nom - self.inner.base_export[k], -self.inner.b.P, self.inner.b.P))
            c, d, x = max(-b, 0.0), max(b, 0.0), 0.0
            self.n_followed += 1
        return self._drain(), (c, d, x)

    def between_price_rounds(self, k: int, soc: float, price_version: int, price) -> list[str]:
        last = next((m for m in reversed(self.received) if m["msg_type"] == "peak_price"), None)
        if (last is not None and last["msg_id"] != self._last_round_seen
                and (last["payload"]["round"] == 1 or last["payload"]["final"])):
            self._last_round_seen = last["msg_id"]
            p = last["payload"]
            self.session.pause({"step": k, "t_utc": self.t,
                                "reason": f"price round {p['round']} for call {p['call_id']}"
                                          + (" (final: nominations after this one are not priced again)"
                                             if p["final"] else "")})
        return self._drain()

    def between_clearings(self, k, soc, price_version, price) -> list[str]:
        return self._drain()
