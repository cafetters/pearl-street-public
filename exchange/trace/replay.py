"""
Offline replay: feed a trace's exchange inputs into a fresh exchange and check it says exactly what the trace says.

Run:  python -m exchange.trace.replay runs/m1/exchange_h150/trace.jsonl
Reads only the trace file.
"""
import json, sys
from collections import Counter
from pathlib import Path

from exchange import messages as M
from exchange.core import Exchange
from exchange.trace.logger import line_to_msg


def exchange_config(header: dict) -> dict:
    """Exchange settings from a trace header. Traces written before D69 have no attested_owners key and sent no
    owner attestation, so they replay with it off."""
    return {"attested_owners": False, **header["exchange"]}


def replay(path: str | Path) -> dict:
    lines = [json.loads(s) for s in Path(path).read_text(encoding="utf-8").splitlines() if s.strip()]
    header, events = lines[0], lines[1:]
    if header.get("event") != "header" or "exchange" not in header:
        raise ValueError("trace has no exchange header (selfish runs have no exchange to replay)")
    ex = Exchange(**exchange_config(header))
    expected = [line_to_msg(e) for e in events if e["from"] == "exchange" and "reason" not in e]   # D69: skip refusals
    got = []
    for e in events:
        if e["to"] == "exchange" and e.get("event") == "send":     # the bus delivered it (D69: drops never arrive)
            got += [M.decode(r) for r in ex.handle(M.encode(line_to_msg(e)))]
        elif "reason" in e and getattr(ex, "refusal_notice", False):      # refusal_notice: the bus told the exchange
            msg = line_to_msg(e) if e.get("msg_id") is not None and not e.get("unloggable") else None
            got += [M.decode(r) for r in ex.bus_refusal(e["origin"], e["reason"], msg)]
    first_diff = next((i for i, (a, b) in enumerate(zip(got, expected)) if a != b), None)
    if first_diff is None and len(got) != len(expected):
        first_diff = min(len(got), len(expected))
    return {"match": first_diff is None, "messages_in": sum(e["to"] == "exchange" for e in events),
            "messages_out": len(expected), "replayed_out": len(got), "first_mismatch_index": first_diff,
            "summary": summarize(events)}


def summarize(events: list[dict]) -> dict:
    by_type = Counter(e["msg_type"] for e in events)
    needs = {e["msg_id"]: e["payload"] for e in events if e["msg_type"] == "post_need" and e["from"] != "exchange"}
    awards = [e["payload"] for e in events if e["msg_type"] == "award"]
    settles = [e["payload"] for e in events if e["msg_type"] == "settle"]
    prices = sorted({a["price_usd_mwh"] for a in awards})
    return {
        "messages_by_type": dict(by_type),
        "rejects_by_code": dict(Counter(e["payload"]["code"] for e in events if e["msg_type"] == "reject")),
        "needs": len(needs), "kw_needed_total": sum(p["kw_needed"] for p in needs.values()),
        "awards": len(awards), "awarded_kw_total": sum(a["accredited_kw"] for a in awards),
        "clearing_price_min": prices[0] if prices else None, "clearing_price_max": prices[-1] if prices else None,
        "settlements": len(settles), "credit_usd": sum(s["credit_usd"] for s in settles),
        "charge_usd": sum(s["charge_usd"] for s in settles),
    }


if __name__ == "__main__":
    res = replay(sys.argv[1])
    print(json.dumps(res, indent=2))
    sys.exit(0 if res["match"] else 1)
