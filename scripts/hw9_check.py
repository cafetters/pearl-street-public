"""HW9 cold-start checks (pre-registered in docs/hw9/success_checks.md before the cold run). Synthetic numbers.

Usage: python scripts/hw9_check.py --run runs/<run_name> --answer <file with the agent's final answer, verbatim>
Writes <run>/check.json and prints it. The roster comes from the trace header; the outside agent is the header's
attackers entry with type external, and must be in the roster.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

TOL_ABS, TOL_REL = 0.50, 0.01


def tol(x: float) -> float:
    return max(TOL_ABS, TOL_REL * abs(x))


def load_trace(run: Path):
    paths = sorted(run.glob("exchange*/trace.jsonl"))
    if len(paths) != 1:
        raise SystemExit(f"expected one exchange trace under {run}, found {paths}")
    with open(paths[0], encoding="utf-8") as f:
        header = json.loads(f.readline())
        lines = [json.loads(x) for x in f]
    return paths[0], header, lines


def dollar_figures(text: str) -> list[float]:
    """Every figure written with a $ sign (e.g. $1,234.56, -$12, $-12, −$3.4), as a signed float."""
    out = []
    for m in re.finditer(r"([-−–]?)\s?\$\s?([-−–]?)\s?([0-9][0-9,]*(?:\.[0-9]+)?)", text):
        v = float(m.group(3).replace(",", ""))
        out.append(-v if (m.group(1) or m.group(2)) else v)
    return out


def verdict(text: str) -> str:
    """YES / NO / UNDETERMINED: did the agent say participating beat acting alone?"""
    t = text.lower().replace("’", "'")
    neg = re.search(r"(did not|didn't|does not|doesn't|not)\s+(beat|outperform)|underperform|worse than acting alone"
                    r"|participating (lost|was worse)|\bno\b[,.:]", t)
    pos = re.search(r"\byes\b[,.:]|participating beat|did beat|\bbeat acting alone|outperformed acting alone"
                    r"|better than acting alone", t)
    if pos and not neg:
        return "YES"
    if neg and not pos:
        return "NO"
    if pos and neg:     # the earlier statement is the answer
        return "YES" if pos.start() < neg.start() else "NO"
    return "UNDETERMINED"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--answer", required=True)
    a = ap.parse_args()
    run = Path(a.run)
    trace_path, header, lines = load_trace(run)
    roster = header["roster"]
    ext = [x for x in header.get("attackers") or [] if x.get("type") == "external"]
    me = str(ext[0]["agent"]) if len(ext) == 1 else None
    res: dict = {"trace": str(trace_path), "git_commit": header["git_commit"], "roster_size": len(roster),
                 "external_agent": me, "external_in_roster": me in roster}
    rejected = {x["payload"]["ref_msg_id"] for x in lines if x.get("msg_type") == "reject"}
    regs = {x["from"]: x["msg_id"] for x in lines if x.get("msg_type") == "register" and x.get("event") == "send"}
    missing = [r for r in roster if r not in regs or regs[r] in rejected]
    res["roster_registered_missing"] = missing

    # 1. registered
    res["c1_registered"] = bool(me in regs and regs[me] not in rejected)
    # 2. at least one accepted binding nomination
    calls = {x["payload"]["call_id"]: x["payload"] for x in lines if x.get("msg_type") == "call_nominations"
             and x.get("from") == "exchange"}
    ok_noms = [x["msg_id"] for x in lines if x.get("msg_type") == "nominate" and x.get("from") == me
               and x.get("event") == "send" and calls.get(x["payload"]["call_id"], {}).get("binding")
               and x["t_utc"] < calls[x["payload"]["call_id"]]["deadline_utc"] and x["msg_id"] not in rejected]
    res["accepted_binding_nominations"] = len(ok_noms)
    res["c2_binding_nomination"] = len(ok_noms) >= 1
    # 3. settlement retrieved after the run finished
    got = False
    log_path = run / "mcp_log.jsonl"
    reqs: dict = {}
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            m = e.get("msg")
            if not isinstance(m, dict):
                continue
            if e["dir"] == "in" and m.get("method") == "tools/call":
                reqs[m.get("id")] = (m.get("params") or {}).get("name")
            elif e["dir"] == "out" and reqs.get(m.get("id")) == "get_settlement":
                body = json.loads(m["result"]["content"][0]["text"])
                got = got or body.get("run_status") == "finished"
    res["c3_settlement_retrieved"] = got
    # 4. numbers match the trace; 5. verdict matches the sign
    ps = [x["payload"] for x in lines if x.get("msg_type") == "peak_settle" and x.get("to") == me]
    imb = [x["payload"] for x in lines if x.get("msg_type") == "imbalance" and x.get("to") == me]
    sums = {"peak_net_usd": sum(p["net_usd"] for p in ps), "peak_refund_usd": sum(p["refund_usd"] for p in ps),
            "peak_charge_usd": sum(p["charge_usd"] for p in ps), "imbalance_charge_usd": sum(p["charge_usd"] for p in imb)}
    S = sums["peak_net_usd"] - sums["imbalance_charge_usd"]
    sums["settlement_total_S_usd"] = S
    res["trace_sums_usd_synthetic"] = sums
    text = Path(a.answer).read_text(encoding="utf-8")
    figs = dollar_figures(text)
    res["answer_dollar_figures"] = [{"value": v, "nearest_trace_sum": min(sums, key=lambda k: abs(sums[k] - v)),
                                     "abs_diff": round(min(abs(s - v) for s in sums.values()), 4)} for v in figs]
    res["c4_total_matches"] = any(abs(abs(v) - abs(S)) <= tol(S) for v in figs)     # sign: check 5
    v = verdict(text)
    res["agent_verdict"] = v
    res["expected_verdict_from_sign_of_S"] = "YES" if S > 0 else "NO"
    res["c5_verdict_matches"] = v == res["expected_verdict_from_sign_of_S"]
    res["pass_all"] = bool(res["external_in_roster"] and not missing and all(
        res[k] for k in ("c1_registered", "c2_binding_nomination", "c3_settlement_retrieved", "c4_total_matches",
                         "c5_verdict_matches")))
    (run / "check.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res, indent=1))
    return 0 if res["pass_all"] else 1


if __name__ == "__main__":
    sys.exit(main())
