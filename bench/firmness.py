"""Firmness measures from a run's bus log (M5 firmness item, owner Nov 19; plan steps (a) and (g)). Report only.

Everything is read from the trace (read_trace: every message in full; the bus log slims telemetry): each agent's latest nomination per call and step (`nominate`, site net kW,
+ = export), the metering service's `telemetry` (`net_kw` per asset and step), `peak_price` rounds and `peak_settle`.

- price_by_call: the final round's peak price per call ($/MWh, the max over the window when it is a list);
- relief_kw: per agent and step in call windows, metered site net minus the same agent's metered net in a twin run
  is not available here, so relief is measured against the agent's own nomination baseline where needed by callers;
- gap_by_step: nominated zone site net minus metered zone site net per call-window step (kW; + = promised export not
  delivered): the relief the operator counted that never arrived.
"""
from __future__ import annotations

import json
from collections import defaultdict


def read_trace(path) -> list[dict]:
    """The trace's messages (header and dropped lines skipped), with `sender` set from `from`."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = json.loads(line)
            if "msg_type" not in m or m.get("event") == "dropped":
                continue
            m.setdefault("sender", m.get("from"))
            out.append(m)
    return out


def latest_nominations(log) -> dict:
    """agent -> call_id -> {t_utc: kW}, the last accepted nomination each agent sent for each call (send order)."""
    out: dict = defaultdict(dict)
    refused = {m["payload"]["ref_msg_id"] for m in log if m["msg_type"] == "reject"}
    for m in log:
        if m["msg_type"] != "nominate" or m["msg_id"] in refused:
            continue
        p = m["payload"]
        steps = window_steps(p["window"], len(p["kw"]))
        out[m["sender"]][p["call_id"]] = dict(zip(steps, (float(v) for v in p["kw"])))
    return out


def window_steps(window, n, minutes: int = 15) -> list[str]:
    from datetime import datetime, timedelta
    t0 = datetime.fromisoformat(window["start_utc"].replace("Z", "+00:00"))
    return [(t0 + timedelta(minutes=minutes * i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(n)]


def metered(log) -> dict:
    """agent -> t_utc -> metered site net kW (+ = export), from telemetry (asset ids are '<agent>/...')."""
    out: dict = defaultdict(dict)
    for m in log:
        if m["msg_type"] == "telemetry" and "net_kw" in m["payload"]:
            aid = m["payload"]["asset_id"].split("/")[0]
            out[aid][m["t_utc"]] = out[aid].get(m["t_utc"], 0.0) + float(m["payload"]["net_kw"])
    return out


def final_prices(log) -> dict:
    out = {}
    for m in log:
        if m["msg_type"] == "peak_price" and m["payload"].get("final"):
            v = m["payload"]["price_usd_mwh"]
            out[m["payload"]["call_id"]] = max(v) if isinstance(v, list) else float(v)
    return out


def peak_settle(log) -> dict:
    out: dict = defaultdict(float)
    for m in log:
        if m["msg_type"] == "peak_settle":
            out[m["to"]] += float(m["payload"]["net_usd"])
    return dict(out)


def gap_by_step(log) -> dict:
    """t_utc -> nominated minus metered zone site net (kW) over every agent that nominated that step."""
    nom, met = latest_nominations(log), metered(log)
    tot: dict = defaultdict(float)
    for aid, calls in nom.items():
        seen = {}
        for c in calls.values():
            seen.update(c)
        for t, v in seen.items():
            tot[t] += v - met.get(aid, {}).get(t, 0.0)
    return dict(tot)
