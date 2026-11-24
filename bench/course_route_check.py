"""Course-route run check (owner, Nov 23): the check a pre-registration of LLM runs on the course's LLM gateway uses.

On a run folder (local `runs/<name>` or a downloaded `runs/cloud/<name>`), for every `llm_calls.jsonl` found with the
`trace.jsonl` next to it, PASS needs all of:
  - header: the call log's header has config, seed and git_commit, backend live and route course;
  - roster coverage (CLAUDE.md): the roster and each agent's status come from the run summary's header when it has
    roster_status (D101; bench/roster_cover.py), else from the trace header (a run before D101: all active), plus the
    trace header's data_centers_excluded; every active roster agent has a chooser in the call log's chooser_by_agent,
    and every excluded one is reported as excluded; no chooser names an agent outside the roster; at least one agent is
    an LLM agent; non-LLM agents made no calls;
  - calls: at least one call was sent to the gateway; no call has a backend error (a model refusal is counted and
    reported, not a failure: the agent keeps its previous label; owner, Nov 24); the cap was never reached;
  - cost: every sent call has a reported cost, within OVER_TABLE_ABS_USD + OVER_TABLE_REL of its table cost;
  - leaks: no file under the folder holds a blocked term (the blocked-terms file outside the repo; a literal term in
    any case, an `re:` term as written) or the course key's value. Hits are reported by file and term number only.
It also reports tokens per call and dollars per simulated day by model, for pricing later runs. Exit 0 on PASS, 1 on FAIL.

usage: python -m bench.course_route_check <run_dir> [--terms PATH] [--out report.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from agents.llm.spend import OVER_TABLE_ABS_USD, OVER_TABLE_REL
from bench import roster_cover as RC


def default_terms_path() -> Path | None:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "pearl-street" / "public_blocked_terms.txt" if base else None


def load_patterns(path: Path | None, extra_literals=()) -> list[re.Pattern]:
    terms = []
    if path is not None and path.is_file():
        terms = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
    pats = [re.compile(t[3:]) if t.startswith("re:") else re.compile(re.escape(t), re.IGNORECASE) for t in terms]
    pats += [re.compile(re.escape(v), re.IGNORECASE) for v in extra_literals if v]
    return pats


def leak_hits(run_dir: Path, patterns: list[re.Pattern]) -> list[str]:
    hits = []
    for f in sorted(p for p in run_dir.rglob("*") if p.is_file()):
        rel = f.relative_to(run_dir).as_posix()
        body = f.read_bytes().decode("utf-8", errors="replace")
        for i, pat in enumerate(patterns, 1):
            if pat.search(rel) or pat.search(body):
                hits.append(f"{rel}: term #{i}")
    return hits


def _lines(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _summary_header(d: Path) -> dict:
    for p in [d, *d.parents][:3]:
        f = p / "summary.json"
        if f.is_file():
            return (json.loads(f.read_text(encoding="utf-8")) or {}).get("header") or {}
    return {}


def check_variant(sidecar: Path) -> dict:
    fails, out = [], {"call_log": sidecar.as_posix()}
    rows = _lines(sidecar)
    head = rows[0] if rows and rows[0].get("event") == "header" else None
    if head is None:
        return {**out, "fails": ["call log has no header line"]}
    for k in ("config", "seed", "git_commit"):
        if head.get(k) is None or head.get(k) == "":
            fails.append(f"header has no {k}")
    ls = head.get("llm_strategy") or {}
    if ls.get("backend") != "live" or ls.get("route") != "course":
        fails.append(f"backend {ls.get('backend')!r} route {ls.get('route')!r}: not a live course-route run")
    trace = sidecar.parent / "trace.jsonl"
    if not trace.is_file():
        return {**out, "fails": fails + ["no trace.jsonl next to the call log"]}
    with open(trace, encoding="utf-8") as f:
        th = json.loads(f.readline())
    sh = _summary_header(sidecar.parent)
    hdr = sh if sh.get("roster_status") is not None else th
    try:
        status = RC.statuses(hdr)
    except RC.RosterError as e:
        return {**out, "fails": fails + [f"roster: {e}"]}
    roster = list(status)
    excluded = sorted(set(RC.excluded(hdr)) | {str(a) for a in th.get("data_centers_excluded") or []})
    cba = {str(a): str(c) for a, c in (ls.get("chooser_by_agent") or {}).items()}
    uncovered = [a for a in roster if a not in cba and a not in excluded]
    outside = sorted(set(cba) - set(roster))
    llm_agents = sorted(a for a in roster if cba.get(a) == "llm")
    non_llm = sorted(a for a in roster if a in cba and cba[a] != "llm")
    if uncovered:
        fails.append(f"roster agents with no chooser and not excluded: {uncovered}")
    if outside:
        fails.append(f"choosers for agents outside the roster: {outside}")
    if not llm_agents:
        fails.append("no LLM agent")
    calls = [r for r in rows if r.get("event") == "call"]
    sent = [r for r in calls if r.get("sent") and not r.get("cache_hit")]
    callers = sorted({str(r.get("agent")) for r in calls} - set(llm_agents))
    if callers:
        fails.append(f"calls by non-LLM agents: {callers}")
    if not sent:
        fails.append("no call was sent")
    errs, refusals = defaultdict(int), 0
    for r in calls:
        if r.get("error") == "refusal":
            refusals += 1
        elif r.get("error"):
            errs[str(r["error"])] += 1
    if errs:
        fails.append(f"calls with errors: {dict(errs)}")
    if any(r.get("cap") == "reached" for r in calls):
        fails.append("the run cap was reached")
    missing = sum(1 for r in sent if r.get("usd_reported") is None)
    if missing:
        fails.append(f"{missing} sent call(s) with no reported cost")
    off = [r.get("request_sha256", "")[:12] for r in sent if r.get("usd_reported") is not None and (
        r.get("usd_table") is None
        or abs(r["usd_reported"] - r["usd_table"]) > OVER_TABLE_ABS_USD + OVER_TABLE_REL * r["usd_table"])]
    if off:
        fails.append(f"reported cost off the table cost on {len(off)} call(s): {off}")
    by_model = defaultdict(lambda: {"calls": 0, "tokens_in": 0, "tokens_cache_write": 0, "tokens_cache_read": 0,
                                    "tokens_out": 0, "usd_reported": 0.0})
    for r in sent:
        m = by_model[r.get("model")]
        m["calls"] += 1
        m["tokens_in"] += r.get("tokens_in") or 0
        m["tokens_cache_write"] += r.get("tokens_cache_write") or 0
        m["tokens_cache_read"] += r.get("tokens_cache_read") or 0
        m["tokens_out"] += r.get("tokens_out") or 0
        m["usd_reported"] += r.get("usd_reported") or 0.0
    days = sorted({r.get("decision_day") for r in calls if r.get("decision_day")})
    for m in by_model.values():
        prompt = m["tokens_in"] + m["tokens_cache_write"] + m["tokens_cache_read"]
        m["tokens_in_per_call"] = prompt / m["calls"]
        m["cache_read_share"] = m["tokens_cache_read"] / max(prompt, 1)
        m["tokens_out_per_call"] = m["tokens_out"] / m["calls"]
    usd = sum(r.get("usd_reported") or 0.0 for r in sent)
    return {**out, "fails": fails, "roster": len(roster), "llm_agents": llm_agents, "non_llm_agents": len(non_llm),
            "excluded": excluded, "calls": len(calls), "sent": len(sent), "refusals": refusals, "usd_reported": usd,
            "decision_days": len(days), "usd_per_decision_day": usd / len(days) if days else None,
            "by_model": dict(by_model)}


def check(run_dir: Path, patterns: list[re.Pattern]) -> dict:
    sidecars = sorted(run_dir.rglob("llm_calls.jsonl"))
    variants = [check_variant(s) for s in sidecars]
    leaks = leak_hits(run_dir, patterns)
    fails = ([] if sidecars else ["no llm_calls.jsonl under the run folder"]) + \
            [f"{v['call_log']}: {f}" for v in variants for f in v["fails"]] + [f"leak: {h}" for h in leaks]
    if not patterns:
        fails.append("no blocked terms loaded: the leak scan would prove nothing")
    return {"run_dir": run_dir.as_posix(), "verdict": "FAIL" if fails else "PASS", "fails": fails,
            "terms_scanned": len(patterns), "variants": variants}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir")
    ap.add_argument("--terms", help="blocked-terms file (default: the one outside the repo)")
    ap.add_argument("--out", help="write the report as JSON here")
    a = ap.parse_args(argv)
    from agents.llm.course_route import NEUTRAL_ENV, RouteConfigMissing, load_config
    key = []
    try:
        cfg = load_config()
        key = [os.environ.get(cfg.key_var) or ""]
    except RouteConfigMissing:
        key = [os.environ.get(NEUTRAL_ENV["key"]) or ""]
    rep = check(Path(a.run_dir), load_patterns(Path(a.terms) if a.terms else default_terms_path(), key))
    if a.out:
        Path(a.out).write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(f"{rep['verdict']}: {a.run_dir} ({len(rep['variants'])} call log(s), {rep['terms_scanned']} terms scanned)")
    for f in rep["fails"]:
        print("  -", f)
    for v in rep["variants"]:
        for mid, m in (v.get("by_model") or {}).items():
            print(f"  {mid}: {m['calls']} calls, {m['tokens_in_per_call']:.0f} prompt ({m['cache_read_share']:.0%} "
                  f"from cache) / {m['tokens_out_per_call']:.0f} out tokens per call, ${m['usd_reported']:.6f}")
    return 0 if rep["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
