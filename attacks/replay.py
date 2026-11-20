"""Regression attacks from the red-team catalog (D61 part 2): a deterministic replay of a confirmed exploit's recorded
tool calls through the agent interface's gateway. No LLM is called and no code is generated: an entry in
attacks/catalog/rt-NNNN.json holds the calls (tool, args, pause number), and this module plays them back.

Actions are pinned to their world (the same slice, config and pause sequence). Two symbols keep them honest across a
change of fleet facts: "$asset_id" (the agent's own asset) and {"$power_x": f} (f times the agent's demonstrated power,
used for register claims). Everything else is literal.

Use: as a backend for redteam.attacker.AttackerLoop (ReplayBackend), or as a plain run-config attack:
    attackers: [{agent: A, type: replay, catalog: rt-0000}]
which wraps the honest agent in an ExternalAgent with its own gateway (cfg `gateway.limits`, then the spec's
`gateway_limits`) and an inline session that plays the entry's actions.
"""
from __future__ import annotations

import json
from pathlib import Path

from agents.llm.backends import Response

CATALOG_DIR = Path(__file__).resolve().parent / "catalog"
SEND_TOOLS = ("register", "subscribe", "nominate", "declare_availability", "declare_outage")


def load_entry(entry_id: str, catalog_dir: Path | None = None) -> dict:
    p = (catalog_dir or CATALOG_DIR) / f"{entry_id}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def symbolize(calls: list[dict], asset_id: str, power_kw: float) -> list[dict]:
    """The sends among an attacker's recorded tool calls, as replayable actions."""
    out = []
    for c in calls:
        if c.get("tool") not in SEND_TOOLS or c.get("discarded") or not c.get("ok", True):
            continue          # reads, refused calls and sends a missed pause discarded never reached the exchange
        args = json.loads(json.dumps(c.get("args") or {}))
        for k, v in list(args.items()):
            if v == asset_id:
                args[k] = "$asset_id"
        if c["tool"] == "register" and isinstance(args.get("max_kw"), (int, float)) and power_kw > 0:
            args["max_kw"] = {"$power_x": round(float(args["max_kw"]) / power_kw, 9)}
        out.append({"pause_no": int(c["pause_no"]), "tool": c["tool"], "args": args})
    return out


def resolve(args, asset: dict):
    if isinstance(args, dict):
        if set(args) == {"$power_x"}:
            return float(args["$power_x"]) * float(asset["power_kw"])
        return {k: resolve(v, asset) for k, v in args.items()}
    if isinstance(args, list):
        return [resolve(v, asset) for v in args]
    if args == "$asset_id":
        return asset["asset_id"]
    return args


class ReplayBackend:
    """Plays an entry's actions: at pause N it asks for my_asset, then sends that pause's actions, then advances."""
    name = "replay"

    def __init__(self, actions: list[dict], never_advance_from: int | None = None):
        self.never_from = never_advance_from     # liveness entries (rt-0002): from this pause on, never advance
        self.by_pause: dict[int, list[dict]] = {}
        for a in actions:
            self.by_pause.setdefault(int(a["pause_no"]), []).append(a)
        self.queue: list = []
        self.last_pause = None
        self.asset: dict | None = None
        self.calls = 0

    def complete(self, req) -> Response:
        self.calls += 1
        obs = json.loads(req.user)
        last = obs.get("last_result") or {}
        if "asset_id" in last and "power_kw" in last:
            self.asset = last
        if obs.get("pause_no") != self.last_pause:
            self.last_pause = obs.get("pause_no")
            todo = self.by_pause.get(int(obs.get("pause_no") or 0), [])
            self.queue = ([{"tool": "my_asset", "args": {}}] if todo else []) + list(todo)
        if not self.queue:
            stuck = self.never_from is not None and int(obs.get("pause_no") or 0) >= self.never_from
            text = json.dumps({"tool": "my_asset" if stuck else "advance", "args": {}})
        else:
            a = self.queue.pop(0)
            text = json.dumps({"tool": a["tool"], "args": resolve(a["args"], self.asset or {})})
        return Response(text=text, input_tokens=0, output_tokens=0, stop_reason="end_turn", provider=req.provider,
                        model=req.model)


def wrap_replay(agent, spec: dict, cfg: dict):
    """A run-config attack: the honest agent becomes an outside agent that replays a catalog entry."""
    from agents.llm.spend import SpendGuard
    from iface.credentials import CredentialStore
    from iface.external import ExternalAgent, InlineSession
    from iface.gateway import Gateway
    from redteam.attacker import AttackerLoop

    entry = load_entry(str(spec["catalog"]))
    limits = {**((cfg.get("gateway") or {}).get("limits") or {}), **(spec.get("gateway_limits") or {})}
    gw = Gateway(limits=limits, credentials=CredentialStore())
    acts = entry["actions"][str(spec.get("from_agent", agent.id))]      # the recorded attacker whose calls to play
    backend = ReplayBackend(acts, (entry.get("never_advance") or {}).get(agent.id))
    loop = AttackerLoop(agent.id, backend, "replay", entry["id"], SpendGuard(0.0, live=False),
                        turn_cap=int(spec.get("turn_cap", 60)))
    state = {}

    def on_pause(aid, info):
        ext = gw.agents[aid]
        if "cs" not in state:
            state["cs"] = gw.open_session(gw.credentials.issue(ext.legal_entity), aid)
        loop.turn(gw, state["cs"], ext, info)
        if ext.session.n_pause == 1 and not any(m["msg_type"] == "register" for m in ext.sent.values()):
            gw.enroll_default(aid)

    s = InlineSession(on_pause)
    ext = ExternalAgent(agent, s)
    s.agent = ext
    gw.attach(ext)
    ext.replay = {"entry": entry["id"], "loop": loop, "gateway": gw}
    return ext
