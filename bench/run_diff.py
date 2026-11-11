"""
Exact comparison of two run folders (sim/run_m1.py output), for checks that a code change leaves a run unchanged
(nomination cache check, docs/plans/nomination_cache_check.md; D69 honest-behavior check).

What must match:
  - every trace (selfish/ and exchange_*/trace.jsonl): same files, same line count, every message line identical as
    text (so any floating-point difference counts), header lines equal after dropping HEADER_SKIP;
  - summary.json: equal at every path after dropping keys in SUMMARY_SKIP wherever they appear (NaN equals NaN,
    nothing else is tolerated);
  - netload.csv identical as text; any agent_log.jsonl identical as text.
Integrity: the new run's headers carry exactly `expect_commit` (no -dirty) when given, both runs have the same
roster, and every roster agent sends at least one message in each compared exchange trace (CLAUDE.md roster rule).
`ignore_line` (optional) drops lines from both traces before comparing, for a pre-registered exception.

Run:  python -m bench.run_diff <base_run_dir> <new_run_dir> [--commit SHA] [--out result.json]
Exit 0 on a full match, 1 otherwise.
"""
import argparse
import json
import sys
from pathlib import Path

HEADER_SKIP = {"git_commit", "run_at_utc"}
SUMMARY_SKIP = {"runtime_s", "round_secs", "run_at_utc", "run_name", "git_commit", "wall_utc", "round_wall_utc",
                "compare_solve_secs"}
MAX_DIFFS = 20


def _drop(obj, skip):
    if isinstance(obj, dict):
        return {k: _drop(v, skip) for k, v in obj.items() if k not in skip}
    if isinstance(obj, list):
        return [_drop(v, skip) for v in obj]
    return obj


def first_diff(a, b, path="") -> str | None:
    """Path of the first difference between two JSON values, or None. Floats must be bit-identical (NaN equals NaN,
    -0.0 differs from 0.0); 1 vs 1.0 is a difference."""
    if type(a) is not type(b):
        return f"{path or '/'}: type {type(a).__name__} vs {type(b).__name__}"
    if isinstance(a, dict):
        if list(a) != list(b):
            return f"{path or '/'}: keys {sorted(set(a) ^ set(b)) or 'order differs'}"
        for k in a:
            d = first_diff(a[k], b[k], f"{path}/{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path or '/'}: length {len(a)} vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = first_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    if isinstance(a, float):          # bit-exact: repr tells -0.0 from 0.0; NaN equals NaN
        return None if repr(a) == repr(b) else f"{path or '/'}: {a!r} vs {b!r}"
    return None if a == b else f"{path or '/'}: {a!r} vs {b!r}"


def _lines(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8").splitlines()


def compare_trace(base: Path, new: Path, expect_commit=None, ignore_line=None, header_skip=()) -> dict:
    a, b = _lines(base), _lines(new)
    ha, hb = json.loads(a[0]), json.loads(b[0])
    out = {"file": str(new), "diffs": [], "lines_base": len(a), "lines_new": len(b)}
    skip = HEADER_SKIP | set(header_skip)
    d = first_diff(_drop(ha, skip), _drop(hb, skip), "header")
    if d:
        out["diffs"].append(d)
    if expect_commit is not None and hb.get("git_commit") != expect_commit:
        out["diffs"].append(f"header/git_commit: {hb.get('git_commit')!r}, expected {expect_commit!r}")
    if ignore_line is not None:
        a = [a[0]] + [s for s in a[1:] if not ignore_line(json.loads(s))]
        b = [b[0]] + [s for s in b[1:] if not ignore_line(json.loads(s))]
        out["lines_compared"] = len(b)
    if len(a) != len(b):
        out["diffs"].append(f"line count {len(a)} vs {len(b)}")
    for i, (x, y) in enumerate(zip(a[1:], b[1:]), 1):
        if x != y:
            out["diffs"].append(f"line {i}: {first_diff(json.loads(x), json.loads(y)) or 'text differs'}")
            if len(out["diffs"]) >= MAX_DIFFS:
                break
    roster = hb.get("roster") or []
    if roster and "exchange" in hb:
        senders = {json.loads(s).get("from") for s in b[1:]}
        missing = sorted(set(roster) - senders)
        out["roster_size"], out["roster_missing"] = len(roster), missing
        if missing:
            out["diffs"].append(f"roster agents with no message in the trace: {missing[:10]}")
    if ha.get("roster") != hb.get("roster"):
        out["diffs"].append("rosters differ between the runs")
    return out


def diff_runs(base_dir, new_dir, expect_commit=None, ignore_line=None, header_skip=(), summary_adjust=None) -> dict:
    """header_skip: extra header keys to drop (anywhere in the header); summary_adjust(base, new) -> (base, new):
    a pre-registered correction applied to the parsed summaries before they are compared."""
    base_dir, new_dir = Path(base_dir), Path(new_dir)
    res = {"base": str(base_dir), "new": str(new_dir), "expect_commit": expect_commit, "checks": [], "diffs": []}
    ta = sorted(p.relative_to(base_dir).as_posix() for p in base_dir.glob("*/trace.jsonl"))
    tb = sorted(p.relative_to(new_dir).as_posix() for p in new_dir.glob("*/trace.jsonl"))
    if ta != tb or not ta:
        res["diffs"].append(f"trace files differ: {ta} vs {tb}")
    for rel in sorted(set(ta) & set(tb)):
        c = compare_trace(base_dir / rel, new_dir / rel, expect_commit, ignore_line, header_skip)
        res["checks"].append({k: v for k, v in c.items() if k != "diffs"} | {"diffs": len(c["diffs"])})
        res["diffs"] += [f"{rel}: {d}" for d in c["diffs"]]
    sa, sb = base_dir / "summary.json", new_dir / "summary.json"
    if not (sa.exists() and sb.exists()):
        res["diffs"].append("summary.json missing")
    else:
        ja = json.loads(sa.read_text(encoding="utf-8"))
        jb = json.loads(sb.read_text(encoding="utf-8"))
        if summary_adjust is not None:
            ja, jb = summary_adjust(ja, jb)
        d = first_diff(_drop(ja, SUMMARY_SKIP), _drop(jb, SUMMARY_SKIP), "summary")
        if d:
            res["diffs"].append(d)
        if expect_commit is not None and jb.get("header", {}).get("git_commit") != expect_commit:
            res["diffs"].append(f"summary/header/git_commit: {jb.get('header', {}).get('git_commit')!r}")
        res["checks"].append({"file": "summary.json", "skipped_keys": sorted(SUMMARY_SKIP)})
    for rel in ["netload.csv"] + sorted({p.relative_to(base_dir).as_posix() for p in base_dir.glob("*/agent_log.jsonl")}
                                        | {p.relative_to(new_dir).as_posix() for p in new_dir.glob("*/agent_log.jsonl")}):
        pa, pb = base_dir / rel, new_dir / rel
        if pa.exists() != pb.exists():
            res["diffs"].append(f"{rel}: present in only one run")
        elif pa.exists():
            same = pa.read_bytes() == pb.read_bytes()
            res["checks"].append({"file": rel, "identical": same})
            if not same:
                res["diffs"].append(f"{rel}: differs")
    res["match"] = not res["diffs"]
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("new")
    ap.add_argument("--commit")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    res = diff_runs(a.base, a.new, a.commit)
    txt = json.dumps(res, indent=2)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(txt + "\n", encoding="utf-8")
    print(txt)
    return 0 if res["match"] else 1


if __name__ == "__main__":
    sys.exit(main())
