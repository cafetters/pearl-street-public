"""
D69 regression on the 23 windows at the final m3-foundation commit (owner, Nov 2; pre-registered in
docs/overnight/2026-11-02.md before any run). Report only; the merge decision follows the pre-registered rule.

Per window: bench.d69_check honest (every trace line text-identical apart from the 80 owner attestations; header
equal apart from attested_owners, git_commit and run_at_utc; summary bit-identical apart from run_diff.SUMMARY_SKIP,
message counts higher by exactly the attestations, and a zero bus_refusals; netload.csv identical; the run's commit in
every header; full roster), plus the owner's two declared exclusions, each applied only when its guard holds:
  1. price_cap_usd_mwh in a trace header: excused only if it appears at the same places in both headers and every
     differing value is numerically equal (500 vs 500.0). Any other difference there still fails.
  2. the summary header's git_commit_from: excused only if the old run has exactly "trace header" there and the new
     run has no such key.
Verdict: MATCH (nothing excused), MATCH_DECLARED (only the declared exclusions were used), or FAIL.

Run: python -m bench.d69_regress <old_run_dir> <new_run_dir> --commit SHA --out f.json
"""
import argparse
import json
import sys
from pathlib import Path

from bench import d69_check as C

CAP_KEY = "price_cap_usd_mwh"
SRC_KEY = "git_commit_from"


def _leaves(obj, key, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield f"{path}/{k}", v
            else:
                yield from _leaves(v, key, f"{path}/{k}")


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def cap_excusable(old_header: dict, new_header: dict) -> tuple[bool, list]:
    a, b = dict(_leaves(old_header, CAP_KEY)), dict(_leaves(new_header, CAP_KEY))
    if not a and not b:
        return False, []
    same = lambda x, y: type(x) is type(y) and x == y          # 500 == 500.0 in Python, so compare types too
    ok = set(a) == set(b) and all(same(a[k], b[k]) or (_num(a[k]) is not None and _num(a[k]) == _num(b[k]))
                                  for k in a)
    differs = sorted(k for k in a if k in b and not same(a[k], b[k]))
    return ok and bool(differs), differs


def regress(old: Path, new: Path, commit: str | None) -> dict:
    excused, cap_rows, cap_bad = [], [], False
    for t_old in sorted(old.glob("*/trace.jsonl")):
        t_new = new / t_old.relative_to(old)
        if not t_new.exists():
            continue
        ha = json.loads(t_old.open(encoding="utf-8").readline())
        hb = json.loads(t_new.open(encoding="utf-8").readline())
        a, b = dict(_leaves(ha, CAP_KEY)), dict(_leaves(hb, CAP_KEY))
        ok, differs = cap_excusable(ha, hb)
        if ok:
            cap_rows.append({"file": t_old.relative_to(old).as_posix(), "exclusion": "header price cap type",
                             "paths": differs, "old": [a[p] for p in differs], "new": [b[p] for p in differs]})
        elif a.keys() != b.keys() or any(type(a[k]) is not type(b[k]) or a[k] != b[k] for k in a):
            cap_bad = True                   # a cap difference that is not only int vs float: nothing is excused
    skip = {CAP_KEY} if cap_rows and not cap_bad else set()
    if skip:
        excused += cap_rows

    def adjust(sa, sb):
        ha, hb = sa.get("header") or {}, sb.get("header") or {}
        if ha.get(SRC_KEY) == "trace header" and SRC_KEY not in hb:
            sa = json.loads(json.dumps(sa))
            del sa["header"][SRC_KEY]
            excused.append({"file": "summary.json", "exclusion": "commit-source note", "old": "trace header"})
        return sa, sb

    res = C.honest(old, new, commit, header_skip_extra=skip, adjust_extra=adjust)
    res["excused"] = excused
    res["verdict"] = ("MATCH" if not excused else "MATCH_DECLARED") if res["match"] else "FAIL"
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--commit")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    res = regress(Path(a.old), Path(a.new), a.commit)
    txt = json.dumps(res, indent=1, default=str)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(txt)
    print(json.dumps({k: res[k] for k in ("verdict", "attestations", "excused")}, default=str))
    print(json.dumps(res["diffs"][:5], default=str))
    return 0 if res["match"] else 1


if __name__ == "__main__":
    sys.exit(main())
