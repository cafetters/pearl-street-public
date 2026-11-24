"""Judge for the single-LLM-agent runs (owner, Nov 24; docs/plans/single_llm_prereg.md).

One all-Rule twin, its determinism copy, one or more LLM runs (agent X on the LLM chooser, everyone else on Rule),
fixed-label controls (X on a fixed label, everyone else on Rule) and optionally a cache-only replay of one LLM run.

Gates, in verdict order (fail closed; PASS needs all of them in every run):
  1. integrity: every trace header has the expected commit (not dirty), the same start, end and seed; configs differ
     from the twin's only in the allowed keys; rosters and D101 statuses are equal; the twin's choosers are all Rule;
     an LLM run's are Rule except X on the LLM; a control's are Rule except X fixed; t4_threshold_usd_mwh is equal;
  2. run health: bench/course_route_check.py PASS on each LLM run (cost reconciled on every call, no backend error,
     no missing cost header, no leak of a blocked term or the key); the cap was never hit (summary llm_cap_hit); every
     label X applied is on the menu; no Rule agent ever applied `bold`; a control's X kept its fixed label every day;
  3. determinism: the twin matches its copy (summary and trace body, ignoring volatile keys and run names, as
     bench/d58_margin.py); an LLM run whose X labels equal the twin's X labels matches the twin on the exchange values;
     a replay matches its source LLM run.
Reported, not gated: for each LLM run and control against the twin, bench/bystander_check.py's line on the unrounded
basis with X as the treated agent (newly below acting alone, further below beyond section 8's tolerance, at zero
tolerance too, and the twin-relative loss); label differences for every agent; X's value minus the twin's per day and
X against acting alone; calls, tokens, cache use, refusals and dollars per day.

usage: python -m bench.single_llm_judge --x PVB_33 --commit <sha> --twin D --twin-copy D [--llm D ...]
                                        [--control LABEL=D ...] [--replay D=SOURCE_D] [--out report.json]
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

from bench import bystander_check as BC
from bench import course_route_check as CRC
from bench import roster_cover as RC
from bench.d58_margin import VOLATILE

MENU = ("normal", "cautious", "bold")
CONFIG_FREE = {"run_name", "llm_strategy"}           # top-level config keys that may differ from the twin's
LLM_FREE = {"per_agent", "honest_models", "route", "backend", "cache", "fixed_label", "cap_usd_run",
            "cap_usd_phase", "phase"}                # llm_strategy keys that may differ
EXCHANGE_IGNORE = VOLATILE | {"llm", "llm_strategy"}


def diff(a, b, ignore=VOLATILE, path="", names=("", "")) -> list[str]:
    """bench/d58_margin.py's diff with a chosen set of ignored keys."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k in ignore:
                continue
            if k not in a or k not in b:
                out.append(f"{path}/{k} missing")
            else:
                out += diff(a[k], b[k], ignore, f"{path}/{k}", names)
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path} length {len(a)} vs {len(b)}"]
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in diff(x, y, ignore, f"{path}[{i}]", names)]
    if isinstance(a, str) and isinstance(b, str):
        return [] if a.replace(names[1], names[0]) == b.replace(names[1], names[0]) else [path]
    if isinstance(a, float) and isinstance(b, float) and a != a and b != b:
        return []
    return [] if a == b else [path]


def _lit(v):
    if isinstance(v, str):
        try:
            return ast.literal_eval(v)
        except (ValueError, SyntaxError):
            return v
    return v


def load(run_dir) -> dict:
    d = Path(run_dir)
    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    evs = s["exchange_by_headroom_mw"]
    if len(evs) != 1:
        raise BC.CheckFailed(f"{d}: expected one headroom, got {list(evs)}")
    traces = sorted(d.glob("exchange_h*/trace.jsonl"))
    if len(traces) != 1:
        raise BC.CheckFailed(f"{d}: expected one exchange trace, found {len(traces)}")
    with open(traces[0], encoding="utf-8") as f:
        th = json.loads(f.readline())
    cfg = {k: _lit(v) for k, v in (th.get("config") or {}).items()}
    return {"dir": d, "name": cfg.get("run_name", d.name), "summary": s, "ev": next(iter(evs.values())),
            "th": th, "trace": traces[0], "cfg": cfg, "strat": th.get("llm_strategy") or {},
            "sh": s.get("header") or {}}


def labels(r) -> dict:
    return ((r["ev"].get("llm") or {}).get("labels_by_agent")) or {}


def integrity(r, twin, kind: str, x: str, commit: str, fixed: str | None = None) -> list[str]:
    bad, n = [], r["name"]
    c = str(r["th"].get("git_commit") or "")
    if not c or c.endswith("-dirty") or not (c.startswith(commit) or commit.startswith(c)):
        bad.append(f"{n}: commit {c!r} is not the pre-registered {commit!r} (or the tree was dirty)")
    for k in ("start", "end"):
        if r["cfg"].get(k) != twin["cfg"].get(k):
            bad.append(f"{n}: {k} differs from the twin")
    if r["th"].get("seed") != twin["th"].get("seed"):
        bad.append(f"{n}: seed differs from the twin")
    keys = (set(r["cfg"]) | set(twin["cfg"])) - CONFIG_FREE
    off = sorted(k for k in keys if r["cfg"].get(k) != twin["cfg"].get(k))
    if off:
        bad.append(f"{n}: config differs from the twin in {off}")
    ls, lt = r["cfg"].get("llm_strategy") or {}, twin["cfg"].get("llm_strategy") or {}
    off = sorted(k for k in (set(ls) | set(lt)) - LLM_FREE if ls.get(k) != lt.get(k))
    if off:
        bad.append(f"{n}: llm_strategy differs from the twin in {off}")
    if r["th"].get("roster") != twin["th"].get("roster"):
        bad.append(f"{n}: roster differs from the twin")
    try:
        if RC.statuses({**r["sh"], "roster": r["th"].get("roster")}) != RC.statuses(
                {**twin["sh"], "roster": twin["th"].get("roster")}):
            bad.append(f"{n}: agents out of the market (D101) differ from the twin")
    except RC.RosterError as e:
        bad.append(f"{n}: {e}")
    if r["strat"].get("t4_threshold_usd_mwh") != twin["strat"].get("t4_threshold_usd_mwh"):
        bad.append(f"{n}: t4_threshold_usd_mwh differs from the twin")
    cba = r["strat"].get("chooser_by_agent") or {}
    roster = [str(a) for a in r["th"].get("roster") or []]
    if x not in roster:
        bad.append(f"{n}: X {x} is not on the roster")
    want = {"twin": "rule", "llm": "llm", "control": "fixed"}[kind]
    wrong = sorted(a for a in roster if cba.get(a) != (want if a == x else "rule"))
    if wrong:
        bad.append(f"{n}: choosers not as expected for a {kind} run (X {want}, others rule): {wrong[:10]}")
    if kind == "control" and (r["cfg"].get("llm_strategy") or {}).get("fixed_label") != fixed:
        bad.append(f"{n}: fixed_label is not {fixed!r}")
    return bad


def health(r, kind: str, x: str, patterns, fixed: str | None = None) -> tuple[list[str], dict]:
    bad, n, info = [], r["name"], {}
    llm = r["ev"].get("llm") or {}
    if llm.get("llm_cap_hit"):
        bad.append(f"{n}: the LLM cap was hit")
    lab = labels(r)
    xl = lab.get(x) or {}
    if kind != "twin" and not xl:
        bad.append(f"{n}: no labels for X")
    if any(v not in MENU for v in xl.values()):
        bad.append(f"{n}: X applied a label off the menu: {sorted(set(xl.values()) - set(MENU))}")
    bold_rule = sorted(a for a, days in lab.items() if a != x and "bold" in days.values())
    if bold_rule:
        bad.append(f"{n}: Rule agents applied bold: {bold_rule[:10]}")
    if kind == "control" and any(v != fixed for v in xl.values()):
        bad.append(f"{n}: X did not keep its fixed label {fixed!r}")
    if kind == "llm":
        rep = CRC.check(r["dir"], patterns)
        info["course_route_check"] = {"verdict": rep["verdict"], "fails": rep["fails"],
                                      "variants": rep["variants"]}
        if rep["verdict"] != "PASS":
            bad += [f"{n}: course_route_check: {f}" for f in rep["fails"]]
    return bad, info


def trace_diff(a: Path, b: Path, names) -> list[str]:
    out = []
    with open(a, encoding="utf-8") as fa, open(b, encoding="utf-8") as fb:
        for i, (la, lb) in enumerate(zip(fa, fb)):
            d = diff(json.loads(la), json.loads(lb), VOLATILE, f"trace[{i}]", names)
            out += d
            if len(out) > 50:
                return out
    with open(a, encoding="utf-8") as fa, open(b, encoding="utf-8") as fb:
        if sum(1 for _ in fa) != sum(1 for _ in fb):
            out.append("trace line counts differ")
    return out


def reported(r, twin, x: str) -> dict:
    out = {}
    for tol in ("section8", "zero"):
        try:
            out[f"others_{tol}"] = BC.check(r["dir"], twin["dir"], further_below_tol=tol, basis="unrounded",
                                            treated=[x])
        except BC.CheckFailed as e:
            out[f"others_{tol}"] = {"error": str(e)}
    lr, lt = labels(r), labels(twin)
    out["label_differences"] = {a: {d: (lt.get(a, {}).get(d), v) for d, v in days.items() if lt.get(a, {}).get(d) != v}
                                for a, days in lr.items() if days != lt.get(a)}
    vr, vt = r["ev"].get("agent_value_usd") or {}, twin["ev"].get("agent_value_usd") or {}
    days = max(len((lr.get(x) or {})), 1)
    short = ((r["ev"].get("net_harm") or {}).get("shortfall_usd") or {}).get(x)
    out["x"] = {"value_usd": vr.get(x), "twin_value_usd": vt.get(x),
                "value_minus_twin_usd": (vr.get(x) - vt.get(x)) if x in vr and x in vt else None,
                "value_minus_twin_usd_per_day": ((vr.get(x) - vt.get(x)) / days) if x in vr and x in vt else None,
                "shortfall_vs_acting_alone_usd": short, "labels": lr.get(x)}
    llm = r["ev"].get("llm") or {}
    out["llm"] = {k: llm.get(k) for k in ("llm_calls", "tokens_in", "tokens_out", "tokens_cache_write",
                                           "tokens_cache_read", "usd_reported_total", "usd_per_simulated_day",
                                           "errors", "reasons")}
    return out


def judge(x, commit, twin_dir, copy_dir, llm_dirs=(), controls=None, replays=None, patterns=None) -> dict:
    controls, replays = dict(controls or {}), dict(replays or {})
    twin, copy = load(twin_dir), load(copy_dir)
    llms = [load(d) for d in llm_dirs]
    ctrls = {lab: load(d) for lab, d in controls.items()}
    reps = {load(d)["name"]: (load(d), load(src)) for d, src in replays.items()}
    gates = {"integrity": [], "health": [], "determinism": []}
    info = {}
    for r, kind, fixed in ([(twin, "twin", None), (copy, "twin", None)] + [(r, "llm", None) for r in llms]
                           + [(r, "control", lab) for lab, r in ctrls.items()]):
        gates["integrity"] += integrity(r, twin, kind, x, commit, fixed)
        b, i = health(r, kind, x, patterns or [], fixed)
        gates["health"] += b
        if i:
            info[r["name"]] = i
    names = (twin["name"], copy["name"])
    gates["determinism"] += diff(twin["summary"], copy["summary"], VOLATILE, "summary", names)[:50]
    gates["determinism"] += trace_diff(twin["trace"], copy["trace"], names)
    cross = {}
    for r in llms:
        same = (labels(r).get(x) or {}) == (labels(twin).get(x) or {})
        d = diff(r["ev"], twin["ev"], EXCHANGE_IGNORE, "exchange", (twin["name"], r["name"])) if same else None
        cross[r["name"]] = {"x_labels_equal_twin": same, "differences": (d or [])[:20]}
        if same and d:
            gates["determinism"].append(f"{r['name']}: X's labels equal the twin's but the exchange values differ: "
                                        f"{d[:5]}")
    for n, (rep, src) in reps.items():
        d = diff(rep["ev"], src["ev"], EXCHANGE_IGNORE, "exchange", (src["name"], rep["name"]))
        if labels(rep) != labels(src):
            d = d + ["labels_by_agent"]
        if d:
            gates["determinism"].append(f"{n}: replay differs from {src['name']}: {d[:5]}")
    verdict = "PASS" if not any(gates.values()) else "FAIL"
    rep_block = {r["name"]: reported(r, twin, x) for r in llms + list(ctrls.values())}
    return {"verdict": verdict, "x": x, "commit": commit, "gates": gates, "cross_check": cross,
            "checks": info, "reported_not_gated": rep_block,
            "runs": {"twin": twin["name"], "twin_copy": copy["name"], "llm": [r["name"] for r in llms],
                     "controls": {k: v["name"] for k, v in ctrls.items()}, "replays": list(reps)}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--x", required=True)
    ap.add_argument("--commit", required=True)
    ap.add_argument("--twin", required=True)
    ap.add_argument("--twin-copy", required=True)
    ap.add_argument("--llm", nargs="*", default=[])
    ap.add_argument("--control", nargs="*", default=[], help="LABEL=DIR")
    ap.add_argument("--replay", nargs="*", default=[], help="DIR=SOURCE_DIR")
    ap.add_argument("--terms", help="blocked-terms file (default: the one outside the repo)")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    patterns = CRC.load_patterns(Path(a.terms) if a.terms else CRC.default_terms_path(), CRC_key())
    try:
        rep = judge(a.x, a.commit, a.twin, a.twin_copy, a.llm, dict(c.split("=", 1) for c in a.control),
                    dict(r.split("=", 1) for r in a.replay), patterns)
    except BC.CheckFailed as e:
        print(f"FAIL (closed): {e}")
        return 2
    if a.out:
        Path(a.out).write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    print(f"{rep['verdict']}: X {a.x}, commit {a.commit}")
    for g, fails in rep["gates"].items():
        print(f"  {g}: {'ok' if not fails else f'{len(fails)} failure(s)'}")
        for f in fails[:10]:
            print(f"    - {f}")
    for n, r in rep["reported_not_gated"].items():
        o = r.get("others_section8") or {}
        print(f"  {n}: others newly below {len(o.get('newly_below_acting_alone') or {})}, further below judged "
              f"{len(o.get('further_below_judged') or {})}, twin-relative loss "
              f"{(o.get('reported_not_judged') or {}).get('twin_relative_loss')}; X minus twin "
              f"{r['x']['value_minus_twin_usd']}")
    return 0 if rep["verdict"] == "PASS" else 1


def CRC_key() -> list[str]:
    import os
    from agents.llm.course_route import NEUTRAL_ENV, RouteConfigMissing, load_config
    try:
        return [os.environ.get(load_config().key_var) or ""]
    except RouteConfigMissing:
        return [os.environ.get(NEUTRAL_ENV["key"]) or ""]


if __name__ == "__main__":
    sys.exit(main())
