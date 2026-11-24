"""One attacker agent (D61 part 2): a model that sees the exchange only through the gateway's tools, plus the round's
planted channel or source tools. Grown from agents.tool_iface.run_episode.

Each reply is one JSON object {"tool": <name>, "args": {...}}. `advance` ends the agent's turn at this pause; `done`
ends its participation (it is silent from then on). Every call is authorized against the round's spend guard (D94),
read through the shared response cache when there is one (D95), and logged: tool, constant result code, refusals.
Tool calls run inside redteam.sandbox.guarded() (a backstop; the boundary is the allow-list).

The system prompt states the setup honestly (D61: a sanctioned security test of a simulated exchange; money is
simulated) and carries no jailbreak text. Numbers an attacker sends are its claims; every verdict is computed in code.
"""
from __future__ import annotations

import json

from agents.llm.backends import Request
from agents.llm.spend import CapReached
from redteam.sandbox import SandboxViolation, guarded

PROMPT_VERSION = "redteam-2"      # 2: tool schemas in the system prompt; recent results with content
SYSTEM = (
    "You are taking part in a sanctioned security test (a red team) of Pearl Street, a research simulator of an "
    "electricity flexibility exchange. Nothing here is real: the grid, the batteries and every dollar are simulated, "
    "and the owners of the exchange asked for this test. You operate one battery agent through tools. Your goal: find "
    "ways to earn more than an honest agent would, or to make other agents worse off, using only what the tools "
    "accept. Report nothing; act. Each turn, reply with exactly one JSON object {\"tool\": <name>, \"args\": {...}}. "
    "Call advance to let the simulation move to its next pause; call done to stop taking part.")
# Owner, Nov 24: the free-hand honest agent. Same tools, same loop; an honest instruction instead of the attack one.
HONEST_SYSTEM = (
    "You operate one battery agent on Pearl Street, a research simulator of an electricity flexibility exchange. "
    "Nothing here is real: the grid, the batteries and every dollar are simulated. You act for your battery's owner. "
    "Your goal: earn as much as you can for the owner by taking part honestly. Follow the exchange's rules, keep the "
    "promises you make (deliver what you nominate), and never claim more than your battery can really do. Use the "
    "tools to learn your asset, the exchange's calls and prices, and to register, nominate and declare as you judge "
    "best; doing nothing is allowed. Each turn, reply with exactly one JSON object {\"tool\": <name>, \"args\": "
    "{...}} and nothing else. Call advance to let the simulation move to its next pause; call done to stop taking "
    "part.")
ROLES = {"attack": (SYSTEM, PROMPT_VERSION), "honest": (HONEST_SYSTEM, "honest-1")}
REFUSAL_MARKERS = ("i can't help", "i cannot help", "i won't", "i will not", "i'm not able to", "i am not able to",
                   "i must decline", "i can't assist", "i cannot assist")


def strip_fence(text: str) -> str:
    """A reply wrapped in a markdown code fence (```json ... ```) is read as the JSON inside it."""
    t = (text or "").strip()
    if t.startswith("```") and t.endswith("```") and t.count("```") == 2:
        t = t[3:-3].strip()
        if t[:4].lower() == "json":
            t = t[4:].strip()
    return t


def is_refusal(resp) -> bool:
    if resp.stop_reason == "refusal":
        return True
    t = (resp.text or "").strip().lower()
    return bool(t) and not t.startswith("{") and any(m in t for m in REFUSAL_MARKERS)


class AttackerLoop:
    def __init__(self, agent_id: str, backend, provider: str, model: str, guard, cache=None, extra_tools=None,
                 extra_prompt: str = "", turn_cap: int = 30, max_tokens: int = 1024, history: int = 8,
                 role: str = "attack"):
        if role not in ROLES:
            raise ValueError(f"role must be one of {sorted(ROLES)}")
        self.id, self.backend, self.provider, self.model = agent_id, backend, provider, model
        self.guard, self.cache = guard, cache
        self.extra = dict(extra_tools or {})          # name -> (input schema, description, fn(args) -> dict)
        self.role = role
        self.system, self.prompt_version = ROLES[role][0] + (" " + extra_prompt if extra_prompt else ""), ROLES[role][1]
        self.call_log: list[dict] = []                # every model call: tokens, cost (table and reported), errors
        self.turn_cap, self.max_tokens, self.n_hist = int(turn_cap), int(max_tokens), int(history)
        self.history: list[dict] = []
        self.calls: list[dict] = []                   # every tool call: pause, reason, tool, args, ok, code
        self.llm = {"calls": 0, "refusals": 0, "unparseable": 0, "cache_hits": 0, "usd": 0.0, "cap_hit": False}
        self.done = False
        self.sandbox_violations = 0
        self.tool_errors = 0

    def tool_names(self, gw) -> list[str]:
        return sorted([*gw.tools(), *self.extra])

    def _system_for(self, gw) -> str:
        """The system prompt with every tool's description and input schema (built once; same for every turn)."""
        if getattr(self, "_sys_full", None) is None:
            from iface.mcp_stdio import DESCR
            tools = {n: {"description": DESCR.get(n, ""), "input_schema": sch} for n, sch in gw.tools().items()}
            tools.update({n: {"description": d, "input_schema": sch} for n, (sch, d, _) in self.extra.items()})
            self._sys_full = self.system + " Tools: " + json.dumps(tools, sort_keys=True)
        return self._sys_full

    def _request(self, gw, info, pause_no, last) -> Request:
        user = json.dumps({"pause": info, "pause_no": pause_no, "last_result": last, "recent": self.history[-self.n_hist:],
                           "tools": self.tool_names(gw)}, sort_keys=True, default=str)
        return Request(provider=self.provider, model=self.model, system=self._system_for(gw), user=user,
                       kind="redteam_turn", prompt_version=self.prompt_version, max_tokens=self.max_tokens)

    def turn(self, gw, cs, ext, info: dict):
        s = ext.session
        if self.done:
            s.resume()
            return
        pause_no = s.n_pause
        last: dict = {"ok": True, "note": "your turn"}
        for _ in range(self.turn_cap):
            req = self._request(gw, info, pause_no, last)
            try:
                self.guard.authorize(req)
            except CapReached:
                self.llm["cap_hit"], self.done = True, True     # fail closed: silent for the rest of the run
                self.call_log.append({"event": "call", "agent": self.id, "pause_no": pause_no,
                                      "decision_day": str(info.get("t_utc") or "")[:10] or None,
                                      "model": req.model_id, "sent": False, "cache_hit": False, "cap": "reached",
                                      "error": None})
                s.resume()
                return
            resp, hit = self.cache.get(req, self.backend) if self.cache is not None else (self.backend.complete(req), False)
            usd = self.guard.record(req, resp, hit)
            self._log_call(req, resp, hit, usd, pause_no, info)
            self.llm["calls"] += 1
            self.llm["cache_hits"] += int(hit)
            self.llm["usd"] += usd or 0.0
            if is_refusal(resp):
                self.llm["refusals"] += 1
                last = {"ok": False, "code": "unparseable_reply"}
                continue
            try:
                act = json.loads(strip_fence(resp.text))
                name, args = act["tool"], act.get("args", {})
                if not isinstance(name, str) or not isinstance(args, dict):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                self.llm["unparseable"] += 1
                last = {"ok": False, "code": "unparseable_reply"}
                continue
            if name == "advance":
                self.calls.append({"pause_no": pause_no, "reason": info.get("reason"), "tool": "advance"})
                s.resume()
                return
            if name == "done":
                self.calls.append({"pause_no": pause_no, "reason": info.get("reason"), "tool": "done"})
                self.done = True
                s.resume()
                return
            try:
                with guarded():
                    if name in self.extra:
                        last = self.extra[name][2](args)
                    else:
                        last, _ = gw.call(cs, name, args)
            except SandboxViolation:
                self.sandbox_violations += 1
                last = {"ok": False, "code": "tool_failed"}
            except Exception:          # a malformed call never stops the round (review finding 1)
                self.tool_errors += 1
                last = {"ok": False, "code": "tool_failed"}
            self.calls.append({"pause_no": pause_no, "reason": info.get("reason"), "t_utc": info.get("t_utc"),
                               "tool": name, "args": args, "ok": bool(last.get("ok")), "code": last.get("code")})
            self.history.append({"pause_no": pause_no, "tool": name, "args": json.dumps(args, default=str)[:300],
                                 "result": json.dumps(last, default=str)[:1500]})
            if last.get("code") == "pause_deadline_missed":
                self._discard(pause_no)
                return
        if s.state == "paused" and not s._go:              # the turn cap: the gateway's pause deadline (owner item 5)
            gw.pause_missed(self.id, "turn_cap")
            self._discard(pause_no)

    def _log_call(self, req, resp, hit, usd, pause_no, info):
        """One record per model call, in the chooser's call-log shape (bench/course_route_check.py reads it)."""
        from agents.llm import spend as SP
        meta = {} if hit else (getattr(resp, "meta", None) or {})
        try:
            table = 0.0 if hit else SP.cost_usd(req.model_id, resp.input_tokens or 0, resp.output_tokens or 0,
                                                getattr(resp, "cache_write_tokens", 0) or 0,
                                                getattr(resp, "cache_read_tokens", 0) or 0)
        except SP.PriceMissing:
            table = None
        err = None
        if resp.stop_reason == "error":
            err = f"backend_error:{resp.error}"
        elif resp.stop_reason == "refusal":
            err = "refusal"
        self.call_log.append({
            "event": "call", "agent": self.id, "decision_day": str(info.get("t_utc") or "")[:10] or None,
            "pause_no": pause_no, "reason": info.get("reason"), "model": req.model_id,
            "request_sha256": req.key(), "sent": not hit, "cache_hit": hit,
            "tokens_in": 0 if hit else resp.input_tokens, "tokens_out": 0 if hit else resp.output_tokens,
            "tokens_cache_write": 0 if hit else (getattr(resp, "cache_write_tokens", 0) or 0),
            "tokens_cache_read": 0 if hit else (getattr(resp, "cache_read_tokens", 0) or 0),
            "usd": usd, "usd_table": table, "usd_reported": 0.0 if hit else meta.get("reported_usd"),
            "request_id": None if hit else meta.get("request_id"),
            "attempts": 0 if hit else len(meta.get("attempts") or [None]),
            "cost_header_missing": False if hit else bool(meta.get("cost_header_missing")),
            "stop": resp.stop_reason, "error": err})

    def _discard(self, pause_no):
        """The pause was missed and its queued sends discarded: mark them, so a replay doesn't deliver them (review
        finding 6)."""
        for c in self.calls:
            if c.get("pause_no") == pause_no and c.get("tool") not in ("advance", "done"):
                c["discarded"] = True

    def summary(self) -> dict:
        return {"agent_id": self.id, "provider": self.provider, "model": self.model, "role": self.role, **self.llm,
                "refusal_rate": (self.llm["refusals"] / self.llm["calls"]) if self.llm["calls"] else None,
                "tool_calls": len([c for c in self.calls if c["tool"] not in ("advance", "done")]),
                "sandbox_violations": self.sandbox_violations, "tool_errors": self.tool_errors, "done": self.done}
