"""HW9 scripted (non-LLM) client for the outside-agent interface: smoke runs and the boundary probe.

  --transport stdio     spawn `python -m iface.mcp_stdio` and speak MCP over stdin/stdout (exchange variant only)
  --transport inproc    run sim/run_m1.py's main (selfish run, then the exchange run with the outside agent), driving
                        the outside agent from a thread through the same Server.call; main's summary then has every
                        agent's value against acting alone (net_harm, agents_worse_off_than_selfish)

Policies: `idle` nominates the battery-idle site net for every binding call (the battery rests in the window);
`overlimit` nominates the battery-idle net plus 10 x registered max_kw at every step (physically impossible).
Usage: python scripts/hw9_probe.py --config configs/hw9/hw9_smoke_jan12.yaml --policy idle --transport inproc
Simulation numbers are synthetic.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def policy_loop(call, policy: str, log: list):
    def c(name, args=None):
        r = call(name, args or {})
        log.append({"tool": name, "args": args or {}, "result": r})
        return r
    a = c("my_asset")
    c("register", {"asset_id": a["asset_id"], "legal_entity": a["legal_entity"], "zone": a["zone"],
                   "max_kw": a["power_kw"]})
    c("subscribe", {"signals": ["call_nominations", "peak_price"], "zones": [a["zone"]]})
    idle = a["battery_idle_site_net_kw"]
    t0 = a["window"]["start_utc"]
    nominated = set()
    while True:
        st = c("advance")
        msgs = c("read_inbox").get("messages", [])
        for m in msgs:
            if m["msg_type"] != "call_nominations" or not m["payload"]["binding"]:
                continue
            p = m["payload"]
            if p["call_id"] in nominated:
                continue
            asset = c("my_asset")
            steps = asset["battery_idle_site_net_kw"]
            # window steps from the 15-minute grid
            from datetime import datetime
            f = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))      # noqa: E731
            i0 = int((f(p["window"]["start_utc"]) - f(t0)).total_seconds() // 900)
            i1 = int((f(p["window"]["end_utc"]) - f(t0)).total_seconds() // 900)
            kw = [round(v, 3) for v in steps[i0:i1]]
            if policy == "overlimit":
                kw = [v + 10 * a["power_kw"] for v in kw]
            c("nominate", {"call_id": p["call_id"], "zone": p["zone"], "window": p["window"], "kw": kw})
            nominated.add(p["call_id"])
        if st.get("status") in ("finished", "error"):
            break
    c("get_settlement")
    return idle


def stdio_call_factory(cfg: str):
    import os
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.Popen([sys.executable, "-m", "iface.mcp_stdio", "--config", cfg], cwd=str(REPO), env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")
    n = [0]

    def rpc(method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            n[0] += 1
            msg["id"] = n[0]
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        if notify:
            return None
        return json.loads(proc.stdout.readline())

    init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "hw9_probe", "version": "0"}})
    rpc("notifications/initialized", notify=True)
    tools = rpc("tools/list")
    print("initialize:", init["result"]["serverInfo"], "tools:", [t["name"] for t in tools["result"]["tools"]])

    def call(name, args):
        r = rpc("tools/call", {"name": name, "arguments": args})
        return json.loads(r["result"]["content"][0]["text"])
    return call, proc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--policy", choices=["idle", "overlimit"], default="idle")
    ap.add_argument("--transport", choices=["stdio", "inproc"], default="inproc")
    a = ap.parse_args()
    log: list = []
    tic = time.time()
    if a.transport == "stdio":
        call, proc = stdio_call_factory(a.config)
        policy_loop(call, a.policy, log)
        proc.stdin.close()
        proc.wait(timeout=600)
        from sim.data import load_config
        out = REPO / "runs" / load_config(a.config)["run_name"]
    else:
        from iface.mcp_stdio import Server
        from sim import run_m1 as R
        from sim.data import load_config
        cfg = load_config(a.config)
        srv = Server(a.config, start_sim=False)
        err = []

        def drive():
            while srv.session.state == "starting":
                time.sleep(0.5)
            try:
                policy_loop(lambda n, x: srv.call(n, x)[0], a.policy, log)
            except Exception as e:      # noqa: BLE001
                import traceback; traceback.print_exc(); err.append(repr(e))
                srv.session.detach()
        th = threading.Thread(target=drive, daemon=True)
        th.start()
        try:
            R.main(a.config)
            srv.trace_path = next((REPO / "runs" / cfg["run_name"]).glob("exchange*/trace.jsonl"), None)
            srv.session.finish()
        except BaseException as e:
            srv.session.finish(repr(e))
            raise
        finally:
            th.join(timeout=60)
        out = REPO / "runs" / cfg["run_name"]
        if err:
            print("driver error:", err)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / f"probe_{a.policy}_{a.transport}.json", "w", encoding="utf-8") as f:
        json.dump({"policy": a.policy, "transport": a.transport, "wall_s": round(time.time() - tic, 1), "calls": log},
                  f, indent=1)
    print(f"done in {time.time() - tic:.0f} s; {len(log)} tool calls; log in {out}")


if __name__ == "__main__":
    main()
