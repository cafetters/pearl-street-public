"""Run one simulated world with outside agents driven in-process (the red-team harness's engine).

run_world: the run config names each attacker under `attackers: [{agent, type: external}]`; every external agent gets
an InlineSession whose pause runs its controller's turn through one shared Gateway. The harness enrolls each attacker's
legal entity (it holds the credential; the attacker never sees it). After the registration pause, an attacker that sent
no register is auto-registered at its demonstrated power and logged `auto_registered` and `inert` (owner, Nov 18,
item 4): a roster agent that never registers would otherwise stop the run (catalog rt-0001).

Values per agent are the run's full value (sim/run_m1.evaluate's _agent_value: energy, wear, every settlement) plus end
state of charge at the mean day-ahead price, as in the D61 tests. All synthetic unless the slice is real.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from iface.credentials import CredentialStore
from iface.external import InlineSession, set_interface
from iface.gateway import Gateway


def run_world(cfg: dict, s, out_dir: Path, controllers: dict, headroom_mw: float, limits: dict | None = None,
              on_gateway=None) -> dict:
    from sim.run_m1 import evaluate, run_variant
    gw = Gateway(limits=limits, credentials=CredentialStore())
    if on_gateway is not None:          # the round's channel tools need the world's gateway
        on_gateway(gw)
    sessions: dict = {}
    events: list[dict] = []

    def on_pause(aid, info):
        ext = gw.agents[aid]
        if aid not in sessions:
            sessions[aid] = gw.open_session(gw.credentials.issue(ext.legal_entity), aid)     # enrollment
        ctl = controllers.get(aid)
        if ctl is not None:
            ctl.turn(gw, sessions[aid], ext, info)
        if ext.session.n_pause == 1 and not any(m["msg_type"] == "register" for m in ext.sent.values()):
            r, err = gw.enroll_default(aid)          # owner item 4: auto-registered, inert
            events.append({"agent_id": aid, "event": "auto_registered", "inert": True, "ok": not err})
        if ext.session.state == "paused" and not ext.session._go and ctl is None:
            ext.session.resume()

    set_interface(lambda aid: InlineSession(on_pause), gw)
    part = np.arange(len(s.ids))
    try:
        sf = run_variant(cfg, s, part, out_dir)
        r = run_variant(cfg, s, part, out_dir, headroom_mw=headroom_mw)
    finally:
        set_interface(None, None)
    ev = evaluate(cfg, s, part, r, s.system["system_load"] * 1000.0, None, sf)
    p_end = float(s.system["price_da"].mean()) / 1000.0
    value = ev["_agent_value"] + r["SOC"][:, -1] * p_end
    ids = [str(x) for x in s.ids]
    trace = next(Path(out_dir).glob("exchange_*/trace.jsonl"))
    return {"ids": ids, "value": {a: float(v) for a, v in zip(ids, value)}, "peak_mw": float(ev["peak_mw"]),
            "peak_settle": {m["to"]: m["payload"]["net_usd"] for m in r["log"] if m["msg_type"] == "peak_settle"},
            "imbalance": {m["to"]: m["payload"]["charge_usd"] for m in r["log"] if m["msg_type"] == "imbalance"},
            "rejects": {a: sum(1 for m in r["log"] if m["msg_type"] == "reject" and m["to"] == a) for a in ids},
            "gateway_log": list(gw.log), "events": events, "trace": str(trace), "refused": r["refused"],
            "roster": ids, "roster_status": dict(r.get("roster_status") or {}),            # D101
            "bus_refusals": len(r.get("refused") or []) if isinstance(r.get("refused"), list) else r.get("refused")}
