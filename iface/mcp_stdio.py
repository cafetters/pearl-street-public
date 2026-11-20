"""Minimal stdio MCP server: one outside agent operates one battery in a Pearl Street run (HW9; M5's server, pulled
forward). Usage: python -m iface.mcp_stdio --config configs/hw9/<run>.yaml

The config names the battery under `attackers: [{agent: <id>, type: external}]`. The server answers `initialize`
at once, then runs the exchange variant of sim/run_m1.py in a worker thread; the sim pauses at each gate (see
iface/external.py) until the agent calls `advance`. Tool calls only touch state while the sim thread is parked.
Every JSON-RPC request and response is appended to runs/<run_name>/mcp_log.jsonl; the trace (header: config, seed,
git commit) is runs/<run_name>/<variant>/trace.jsonl. Protocol: newline-delimited JSON-RPC 2.0 on stdin/stdout;
everything the simulator prints goes to runs/<run_name>/server.log, never to stdout.

M5 (Nov 19): every tool call goes through iface/gateway.py (credential, identity, limits, refusals). A config with
`gateway.credential_sha256` requires the client's PEARL_STREET_CREDENTIAL (read once at start, redacted from
mcp_log.jsonl); a config without a `gateway` block keeps HW9's local-process access (no credential), and the reply
says so. A raw JSON-RPC line longer than the gateway's max_line_bytes is refused before parsing.
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
    "register": "Send `register` for your battery. Must be the first thing you do, at the start pause. Give the "
                "asset_id from my_asset and max_kw. Your legal entity comes from your credential. The interface "
                "grants at most your battery's demonstrated power_kw and says so (claimed_kw, granted_kw); the "
                "exchange also rejects a max_kw above it. A refused attempt starts a cool-down (retry_after_utc). "
                "Queued: delivered when you call advance.",
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
from iface.gateway import SCHEMAS, Gateway, ClientSession, power_warnings  # noqa: F401 (HW9 tests)

SETTLE_TYPES = ("peak_settle", "imbalance", "settle", "guarantee_accrual", "delivery_record")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def tool_list() -> list[dict]:
    names = ["my_asset", "register", "subscribe", "read_inbox", "read_public", "nominate", "declare_availability",
             "declare_outage", "advance", "get_settlement"]
    return [{"name": n, "description": DESCR[n], "inputSchema": SCHEMAS[n]} for n in names]


class Server:
    def __init__(self, cfg_path: str | None = None, out_dir: Path | None = None, start_sim: bool = True,
                 cfg: dict | None = None, slice_fn=None):
        """slice_fn (M5 seam): a callable returning (slice, participants) instead of reading cfg's data window, so
        tests and the harness can serve a synthetic slice. cfg: a config dict instead of a path."""
        from iface.credentials import ENV_VAR, CredentialStore
        from iface.external import Session, set_interface
        from sim.data import REPO, load_config
        self.cfg = cfg if cfg is not None else load_config(cfg_path)
        ext = [a for a in self.cfg.get("attackers") or [] if a.get("type") == "external"]
        if len(ext) != 1:
            raise ValueError("the config must name exactly one attackers entry with type external")
        self.agent_id = str(ext[0]["agent"])
        self.out_dir = out_dir or (REPO / "runs" / self.cfg["run_name"])
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.slice_fn = slice_fn
        g = self.cfg.get("gateway")
        self.require_credential = bool(g and g.get("credential_sha256"))
        self.gateway = Gateway(limits=(g or {}).get("limits"),
                               credentials=CredentialStore((g or {}).get("credential_sha256")))
        self.token = os.environ.get(ENV_VAR) if self.require_credential else None    # read once, never logged
        self.cs = None
        self.session = Session()
        set_interface(lambda aid: self.session, self.gateway)
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
            if self.slice_fn is not None:
                s, part = self.slice_fn()
            else:
                s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
                part = R.participants(s, cfg["agents"])
            fl = R.m2a_flags(cfg)
            prev = R.previous_day(cfg) if fl["daily_energy"] and self.slice_fn is None else None
            h = cfg["headroom_mw"][0] if isinstance(cfg["headroom_mw"], list) else cfg["headroom_mw"]
            r = R.run_variant(cfg, s, part, self.out_dir, h, prev=prev)
            self.trace_path = self.out_dir / r["variant"] / "trace.jsonl"
            with open(self.out_dir / "run_result.json", "w", encoding="utf-8") as f:
                json.dump({"variant": r["variant"], "trace": str(self.trace_path), "runtime_s": r["runtime_s"],
                           "refused": r["refused"], "external_agent": self.agent_id,
                           "steps_following_nomination": getattr(self.session.agent, "n_followed", None),
                           "gateway_log": self.gateway.log}, f, indent=1)
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
        missed = [x for x in self.gateway.log if x.get("code") == "pause_deadline_missed"]
        if missed:
            out["pauses_missed"] = len(missed)
        return out

    def client(self):
        """The gateway session for this process's client: from the credential when the config requires one, else
        HW9's local-process access bound to the configured agent."""
        if self.cs is None and self.session.agent is not None:
            if self.require_credential:
                cs = self.gateway.open_session(self.token, self.agent_id)
                if not isinstance(cs, ClientSession):
                    return cs
                self.cs = cs
            else:
                self.cs = ClientSession(self.session.agent.legal_entity, self.agent_id)
        return self.cs

    def call(self, name: str, args: dict) -> tuple[dict, bool]:
        s = self.session
        if name not in DESCR:
            return {"ok": False, "code": "unknown_tool"}, True
        if s.state in ("starting",):
            s.wait_first_pause(120.0)
        if s.state == "error" and s.agent is None:
            return {"ok": False, "code": "run_failed", "detail": s.error}, True
        cs = self.client()
        if not isinstance(cs, ClientSession):
            return (cs or {"ok": False, "code": "sim_running"}), True
        if name == "advance":
            if args:
                return {"ok": False, "code": "bad_arguments"}, True
            if s.agent is not None and not any(m["msg_type"] == "register" for m in s.agent.sent.values()):
                return {"ok": False, "code": "not_registered_yet",
                        "detail": "call register (and subscribe) before advance: the run needs every battery "
                                  "registered at step 0"}, True
            if s.state in ("finished", "error"):
                return {"ok": True, **self.status()}, False
            s.advance(ADVANCE_WAIT_S)
            return {"ok": True, **self.status()}, False
        if name == "get_settlement":
            return self.settlement(), False
        r, err = self.gateway.call(cs, name, args)
        if r.get("code") in ("sim_running", "run_over"):
            r = {**r, **self.status()}
        return r, err

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


def make_server(cfg_path: str) -> "Server":
    """The run's server. A config with `synthetic_world: true` serves redteam.synth's synthetic slice (smoke tests of
    the stdio transport only; never Engie data)."""
    from sim.data import load_config
    cfg = load_config(cfg_path)
    if cfg.get("synthetic_world"):
        import numpy as np
        from redteam.synth import synthetic_slice
        s = synthetic_slice()
        return Server(cfg=cfg, slice_fn=lambda: (s, np.arange(len(s.ids))))
    return Server(cfg_path)


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

    def record(direction, obj):      # the credential never reaches the log (M5)
        mcp_log.write(redact_text(json.dumps({"wall_utc": now_utc(), "dir": direction, "msg": obj})) + "\n")
        mcp_log.flush()

    def reply(obj):
        record("out", obj)
        proto.write(json.dumps(obj) + "\n")
        proto.flush()

    from iface.credentials import redact_text
    from iface.gateway import LIMITS
    g0 = load_config(cfg_path).get("gateway") or {}
    max_line = int((g0.get("limits") or {}).get("max_line_bytes", LIMITS["max_line_bytes"]))
    server = None
    stdin = sys.stdin.buffer
    while True:
        raw = stdin.readline(max_line + 1)       # M5: never buffer more than the cap of one line
        if not raw:
            break
        if len(raw) > max_line:      # size on raw bytes, before parsing; nothing of the line is echoed
            n = len(raw)
            while not raw.endswith(b"\n"):      # drain the rest of the oversized line
                raw = stdin.readline(max_line + 1)
                if not raw:
                    break
                n += len(raw)
            record("in_oversize", {"bytes": n})
            reply({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "message_too_large",
                                                          "data": {"max_bytes": max_line}}})
            continue
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
                    "serverInfo": {"name": "pearl-street", "version": "0.3-m5"},
                    "instructions": "Pearl Street flexibility exchange: you operate one battery. Read SKILL.md. "
                                    "Start with my_asset, register, subscribe, advance."}})
                if server is None:
                    server = make_server(cfg_path)
            elif method == "ping":
                reply({"jsonrpc": "2.0", "id": rid, "result": {}})
            elif method == "tools/list":
                reply({"jsonrpc": "2.0", "id": rid, "result": {"tools": tool_list()}})
            elif method == "tools/call":
                if server is None:
                    server = make_server(cfg_path)
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
