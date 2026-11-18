"""Minimal stdio MCP server: one outside agent operates one battery in a Pearl Street run (HW9; M5's server, pulled
forward). Usage: python -m iface.mcp_stdio --config configs/hw9/<run>.yaml

The config names the battery under `attackers: [{agent: <id>, type: external}]`. The server answers `initialize`
at once, then runs the exchange variant of sim/run_m1.py in a worker thread; the sim pauses at each gate (see
iface/external.py) until the agent calls `advance`. Tool calls only touch state while the sim thread is parked.
Every JSON-RPC request and response is appended to runs/<run_name>/mcp_log.jsonl; the trace (header: config, seed,
git commit) is runs/<run_name>/<variant>/trace.jsonl. Protocol: newline-delimited JSON-RPC 2.0 on stdin/stdout;
everything the simulator prints goes to runs/<run_name>/server.log, never to stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

PROTOCOL = "2025-06-18"
ADVANCE_WAIT_S = 60.0

DESCR = {
    "my_asset": "Your battery's facts as its owner knows them: agent_id, asset_id, legal_entity, zone, power_kw, "
                "capacity and state-of-charge limits (kWh), current state of charge, the window, the current "
                "simulated time, and battery_idle_site_net_kw (one value per step: the site's net kW with the "
                "battery idle; + export, - import). Read-only.",
    "register": "Send `register` for your agent and battery. Must be the first thing you do, at the start pause. "
                "Use the asset_id, legal_entity and zone from my_asset; max_kw must not exceed power_kw. The "
                "interface checks these against my_asset before sending, because a rejected registration stops "
                "the whole simulated run. Queued: delivered when you call advance.",
    "subscribe": "Send `subscribe`. Without it you receive no calls or prices. Use signals "
                 "[\"call_nominations\", \"peak_price\"] and zones [your zone]. Queued until advance.",
    "nominate": "Send `nominate`: your planned SITE NET kW (not battery kW) for every step of a call's window, "
                "one number per step (+ export, - import). call_id, zone and window must be copied exactly from "
                "the call_nominations message. Your latest nomination before the call's deadline counts. For a "
                "binding call your battery is dispatched to it (battery kW = nominated - battery_idle_site_net_kw, "
                "clipped to +-power_kw) and deviation beyond the free band pays imbalance. Queued until advance.",
    "declare_availability": "Send `declare_availability` for one local date (YYYY-MM-DD): power_kw, reserve_kwh, "
                            "planned_outage. Optional; queued until advance.",
    "declare_outage": "Send `declare_outage` for a forced outage window {start_utc, end_utc}. Optional; queued "
                      "until advance.",
    "read_inbox": "Messages the exchange sent you since your last read_inbox (verbatim JSON envelopes: msg_type, "
                  "t_utc, payload). Includes call_nominations, peak_price rounds, rejects, imbalance, peak_settle.",
    "read_public": "Day-ahead energy prices published so far ($/MWh per 15-minute step, UTC).",
    "advance": "Resume the simulation until the next pause (a new call, a reject, the first and final price round "
               "of a call, a settlement) or the end of the run. Waits at most 60 s; if it returns status "
               "'running', call it again. Returns status, the simulated time and why it paused.",
    "get_settlement": "Your settlement from the exchange: its peak_settle (refund on your acting-alone reference "
                      "minus charge on your metered net load, at the settled peak price), imbalance charges, and "
                      "after the run ends the metering service's acting-alone reference per binding call and your "
                      "metered site net per step. Verbatim; nothing is computed by the interface.",
}
EXTRA_TOOLS = {
    "my_asset": {"type": "object", "properties": {}, "additionalProperties": False},
    "advance": {"type": "object", "properties": {}, "additionalProperties": False},
    "get_settlement": {"type": "object", "properties": {}, "additionalProperties": False},
}
SEND_TOOLS = ("register", "subscribe", "nominate", "declare_availability", "declare_outage")
# HW9 cold run 1 (Nov 18): the outside agent waited for a nonzero price before nominating, but the exchange computes
# the zone load only where every registered agent in the zone has a nomination (exchange/core.py _zone_load; D78's
# silent_reference is off in this config), so every price stayed 0 for all 80 agents: a deadlock. The interface now
# says so at every pause while an open call lacks this agent's nomination.
PRICE_NEEDS_YOU = ("The exchange can only form a zone peak price from a complete set of nominations: while you have "
                   "not nominated an open call, its peak price stays 0 for every agent in the zone. Nominate your "
                   "planned site net for each open call (your best plan, for example battery_idle_site_net_kw plus "
                   "your intended battery kW); you can re-nominate in later price rounds before the deadline.")
SETTLE_TYPES = ("peak_settle", "imbalance", "settle", "guarantee_accrual", "delivery_record")


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


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def tool_list() -> list[dict]:
    from agents.tool_iface import TOOLS
    base = {t["name"]: t["input_schema"] for t in TOOLS}
    names = ["my_asset", "register", "subscribe", "read_inbox", "read_public", "nominate", "declare_availability",
             "declare_outage", "advance", "get_settlement"]
    return [{"name": n, "description": DESCR[n], "inputSchema": EXTRA_TOOLS.get(n) or base[n]} for n in names]


class Server:
    def __init__(self, cfg_path: str, out_dir: Path | None = None, start_sim: bool = True):
        from iface.external import Session, set_session
        from sim.data import REPO, load_config
        self.cfg = load_config(cfg_path)
        ext = [a for a in self.cfg.get("attackers") or [] if a.get("type") == "external"]
        if len(ext) != 1:
            raise ValueError("the config must name exactly one attackers entry with type external")
        self.agent_id = str(ext[0]["agent"])
        self.out_dir = out_dir or (REPO / "runs" / self.cfg["run_name"])
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.session = Session()
        set_session(self.session)
        self.thread = None
        self.trace_path: Path | None = None
        self.calls_after_finish: list[str] = []
        if start_sim:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    # ---- the sim thread ----
    def _run(self):
        try:
            from sim import run_m1 as R
            from sim.data import load_slice
            cfg = self.cfg
            s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
            part = R.participants(s, cfg["agents"])
            fl = R.m2a_flags(cfg)
            prev = R.previous_day(cfg) if fl["daily_energy"] else None
            h = cfg["headroom_mw"][0] if isinstance(cfg["headroom_mw"], list) else cfg["headroom_mw"]
            r = R.run_variant(cfg, s, part, self.out_dir, h, prev=prev)
            self.trace_path = self.out_dir / r["variant"] / "trace.jsonl"
            with open(self.out_dir / "run_result.json", "w", encoding="utf-8") as f:
                json.dump({"variant": r["variant"], "trace": str(self.trace_path), "runtime_s": r["runtime_s"],
                           "refused": r["refused"], "external_agent": self.agent_id,
                           "steps_following_nomination": getattr(self.session.agent, "n_followed", None)}, f, indent=1)
            self.session.finish()
        except BaseException as e:      # reported to the agent on its next call
            traceback.print_exc()
            self.session.finish(f"{type(e).__name__}: {e}")

    # ---- tools ----
    def status(self) -> dict:
        s = self.session
        out = {"status": s.state, **({"error": s.error} if s.error else {})}
        if s.state == "paused":
            out.update(s.info)
        if s.agent is not None:
            out["unread_messages"] = len(s.agent.inbox)
            if s.state == "paused":
                missing = s.agent.open_calls_not_nominated()
                if missing:
                    out["open_calls_you_have_not_nominated"] = missing
                    out["attention"] = PRICE_NEEDS_YOU
        return out

    def call(self, name: str, args: dict) -> tuple[dict, bool]:
        s = self.session
        if name not in DESCR:
            return {"ok": False, "code": "unknown_tool"}, True
        if s.state in ("starting",):
            s.wait_first_pause(120.0)
        if name == "advance":
            if s.agent is not None and not any(m["msg_type"] == "register" for m in s.agent.sent.values()):
                return {"ok": False, "code": "not_registered_yet",
                        "detail": "call register (and subscribe) before advance: the run needs every battery "
                                  "registered at step 0"}, True
            if s.state in ("finished", "error"):
                return {"ok": True, **self.status()}, False
            s.advance(ADVANCE_WAIT_S)
            return {"ok": True, **self.status()}, False
        if s.state == "running":
            return {"ok": False, "code": "sim_running", "detail": "call advance until it returns status paused"}, True
        if s.state == "error" and s.agent is None:
            return {"ok": False, "code": "run_failed", "detail": s.error}, True
        ext = s.agent
        if name == "my_asset":
            return {"ok": True, **ext.asset_facts()}, False
        if name == "get_settlement":
            return self.settlement(), False
        if s.state in ("finished", "error") and name in SEND_TOOLS:
            return {"ok": False, "code": "run_over", **self.status()}, True
        if name == "register" and isinstance(args, dict):
            facts = ext.asset_facts()
            bad = [k for k in ("asset_id", "legal_entity", "zone") if k in args and args[k] != facts[k]]
            mk = args.get("max_kw")
            if isinstance(mk, (int, float)) and not (0 < mk <= facts["power_kw"]):
                bad.append("max_kw")
            if bad:
                return {"ok": False, "code": "register_precheck",
                        "detail": f"{bad} must match my_asset (max_kw in (0, power_kw]); nothing was sent"}, True
        if name == "read_inbox":
            if args:
                return {"ok": False, "code": "bad_arguments"}, True
            out = []
            for raw in ext.inbox:
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    continue
            ext.inbox.clear()
            return {"ok": True, "messages": out}, False
        r = ext.iface.call(name, args if isinstance(args, dict) else {})
        if name == "nominate" and r.get("ok"):
            w = power_warnings(ext, args)
            if w:
                r["warnings"] = w
        return r, not r.get("ok", False)

    def settlement(self) -> dict:
        ext = self.session.agent
        out = {"ok": True, "run_status": self.session.state,
               **{t: [m["payload"] for m in ext.received if m["msg_type"] == t] for t in SETTLE_TYPES}}
        if self.session.state == "finished" and self.trace_path and self.trace_path.exists():
            refs, meter = [], []
            with open(self.trace_path, encoding="utf-8") as f:
                for line in f:
                    if ext.asset_id not in line:
                        continue
                    d = json.loads(line)
                    p = d.get("payload") or {}
                    if p.get("asset_id") != ext.asset_id:
                        continue
                    if d.get("msg_type") == "selfish_reference":
                        refs.append({"call_id": p["call_id"], "window": p["window"], "net_kw": p["net_kw"]})
                    elif d.get("msg_type") == "telemetry" and "net_kw" in p:
                        meter.append({"t_utc": d["t_utc"], "net_kw": p["net_kw"]})
            out["acting_alone_reference"] = refs
            out["metered_site_net_kw"] = meter
            out["note"] = ("peak_settle.net_usd = refund_usd - charge_usd: the settled peak price times (metered site "
                           "net - acting-alone reference net) over the priced steps. imbalance.charge_usd is paid on "
                           "deviation beyond the free band. Synthetic simulation dollars.")
        else:
            out["note"] = "the run has not finished: metering data and the acting-alone reference come at the end"
        self.calls_after_finish.append(self.session.state)
        return out


def serve(cfg_path: str):
    # the protocol owns the real stdout; anything else written to fd 1 or sys.stdout goes to server.log
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", newline="\n")
    from sim.data import REPO, load_config
    run_dir = REPO / "runs" / load_config(cfg_path)["run_name"]
    run_dir.mkdir(parents=True, exist_ok=True)
    log = open(run_dir / "server.log", "a", encoding="utf-8")
    os.dup2(log.fileno(), 1)
    sys.stdout = log
    sys.stderr = log
    mcp_log = open(run_dir / "mcp_log.jsonl", "a", encoding="utf-8")

    def record(direction, obj):
        mcp_log.write(json.dumps({"wall_utc": now_utc(), "dir": direction, "msg": obj}) + "\n")
        mcp_log.flush()

    def reply(obj):
        record("out", obj)
        proto.write(json.dumps(obj) + "\n")
        proto.flush()

    server = None
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            record("in_unparsed", line)
            reply({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        record("in", req)
        method, rid = req.get("method"), req.get("id")
        if rid is None:           # a notification
            continue
        try:
            if method == "initialize":
                ver = (req.get("params") or {}).get("protocolVersion") or PROTOCOL
                reply({"jsonrpc": "2.0", "id": rid, "result": {
                    "protocolVersion": ver, "capabilities": {"tools": {}},
                    "serverInfo": {"name": "pearl-street", "version": "0.2-hw9"},
                    "instructions": "Pearl Street flexibility exchange: you operate one battery. Read SKILL.md. "
                                    "Start with my_asset, register, subscribe, advance."}})
                if server is None:
                    server = Server(cfg_path)
            elif method == "ping":
                reply({"jsonrpc": "2.0", "id": rid, "result": {}})
            elif method == "tools/list":
                reply({"jsonrpc": "2.0", "id": rid, "result": {"tools": tool_list()}})
            elif method == "tools/call":
                if server is None:
                    server = Server(cfg_path)
                p = req.get("params") or {}
                res, is_err = server.call(p.get("name"), p.get("arguments") or {})
                reply({"jsonrpc": "2.0", "id": rid, "result": {
                    "content": [{"type": "text", "text": json.dumps(res)}], "isError": bool(is_err)}})
            else:
                reply({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}})
        except Exception as e:
            traceback.print_exc()
            reply({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}})
    record("eof", None)
    if server is not None:       # the client went away: the run finishes with the agent silent, the trace closes
        server.session.detach()
        if server.thread is not None:
            server.thread.join()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    serve(ap.parse_args().config)
