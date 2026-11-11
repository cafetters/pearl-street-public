"""
D69 regression on breadth (day Nov 1 item 3, pre-registered in docs/overnight/2026-11-01-day.md on main; report only).

Every one of the 23 windows at m = 0, run on the D69 commit, against its existing m = 0 run:
  MATCH                           bench.d69_check honest passes (the exact check used for Jan 12-13).
  MATCH_EXCEPT_PRE_D69_ADDITIONS  for comparators older than the branch base 4460374 (the 11 D58 windows): every
                                  non-attestation trace line text-identical; every header leaf of the old header
                                  equal in the new one; every summary leaf of the old summary bit-equal in the new
                                  one (same skips and attestation count adjustment as d69_check); netload identical;
                                  commit and roster checks pass; and every leaf only the new run has also exists in a
                                  run made at 4460374 (so later pre-D69 code added it, not D69).
  FAIL                            anything else; the first differences are listed.
Leaves are compared by path (dict keys; list items by index); for the "exists at 4460374" test list indices are
generalised, so /log[*]/x matches any item. Only key paths of the 4460374 runs are read, never their values.

Run: python -m bench.d69_breadth <old_run> <new_run> --commit SHA --ref <4460374 run> [--ref ...] [--out f.json]
"""
import argparse
import json
import re
import sys
from pathlib import Path

from bench import d69_check as C
from bench.run_diff import SUMMARY_SKIP, _drop

BASE_COMMIT = "4460374f034c4b327eac59265c3ed8c6b6a9a126"
HEADER_SKIP = {"git_commit", "run_at_utc", "attested_owners"}
MAX = 20


def leaves(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaves(v, f"{path}/{k}")
    elif isinstance(obj, list) and obj and all(isinstance(x, (dict, list)) for x in obj):
        for i, v in enumerate(obj):
            yield from leaves(v, f"{path}[{i}]")
    else:
        yield path, obj


def general(path: str) -> str:
    return re.sub(r"\[\d+\]", "[*]", path)


def same(a, b) -> bool:
    return type(a) is type(b) and json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True) \
        if not isinstance(a, float) else (type(b) is float and repr(a) == repr(b))


def compare_leaves(old: dict, new: dict, ref_paths: set, what: str) -> tuple[list, list]:
    lo, ln = dict(leaves(old)), dict(leaves(new))
    problems, added = [], []
    for p, v in lo.items():
        if p not in ln:
            problems.append(f"{what}{p}: in the old run only")
        elif not same(v, ln[p]):
            problems.append(f"{what}{p}: {v!r} vs {ln[p]!r}")
    for p in ln:
        if p not in lo:
            if general(p) in ref_paths:
                added.append(general(p))
            else:
                problems.append(f"{what}{p}: in the new run only, and not in any 4460374 run")
    return problems, sorted(set(added))


def ref_paths(refs: list[Path]) -> tuple[set, set, list]:
    hp, sp, used = set(), set(), []
    for r in refs:
        heads = [json.loads(t.open(encoding="utf-8").readline()) for t in sorted(r.glob("*/trace.jsonl"))]
        if not heads or any(h.get("git_commit") != BASE_COMMIT for h in heads):
            continue                         # only runs made at the branch base count
        used.append(str(r))
        for h in heads:
            hp |= {general(p) for p, _ in leaves(_drop(h, HEADER_SKIP))}
        sp |= {general(p) for p, _ in leaves(_drop(json.loads((r / "summary.json").read_text(encoding="utf-8")),
                                                   SUMMARY_SKIP))}
    return hp, sp, used


def classify(old: Path, new: Path, commit: str, refs: list[Path]) -> dict:
    exact = C.honest(old, new, commit)
    out = {"old": str(old), "new": str(new), "attestations": exact.get("attestations"),
           "exact_diffs": exact["diffs"][:MAX]}
    if exact["match"]:
        out["verdict"] = "MATCH"
        return out
    hp, sp, used = ref_paths(refs)
    out["refs_used"] = used
    problems, added = [], {}
    if not used:
        problems.append("no comparator run made at 4460374")
    # commit, roster and attestation checks from the exact check are never excused
    problems += [d for d in exact["diffs"] if "git_commit" in d or "roster" in d or "attestation" in d]
    for rel in sorted(p.relative_to(new).as_posix() for p in new.glob("*/trace.jsonl")):
        a = (old / rel).read_text(encoding="utf-8").splitlines() if (old / rel).exists() else None
        if a is None:
            problems.append(f"{rel}: not in the old run")
            continue
        b = (new / rel).read_text(encoding="utf-8").splitlines()
        hb = json.loads(b[0])
        kept = [s for s in b[1:] if not C.is_attestation(json.loads(s))]
        if len(kept) != len(a) - 1:
            problems.append(f"{rel}: {len(kept)} non-attestation lines vs {len(a) - 1}")
        n = sum(x != y for x, y in zip(a[1:], kept))
        if n:
            i = next(i for i, (x, y) in enumerate(zip(a[1:], kept), 1) if x != y)
            problems.append(f"{rel}: {n} message lines differ, first at line {i}")
        p, ad = compare_leaves(_drop(json.loads(a[0]), HEADER_SKIP), _drop(hb, HEADER_SKIP), hp, f"{rel} header")
        problems += p
        if ad:
            added[f"{rel} header"] = ad
    sa = json.loads((old / "summary.json").read_text(encoding="utf-8"))
    sb = json.loads((new / "summary.json").read_text(encoding="utf-8"))
    n_att = exact.get("attestations") or 0
    for h in sb.get("exchange_by_headroom_mw", {}).values():         # the same adjustment as d69_check honest
        mpd = h.get("messages_per_day") or {}
        first = sorted(mpd)[0] if mpd else None
        if first is not None and mpd[first].get("asset_limits") == n_att:
            del mpd[first]["asset_limits"]
        if isinstance(h.get("messages_total"), int):
            h["messages_total"] -= n_att
        if h.get("bus_refusals") == {"total": 0, "by_reason": {}}:
            del h["bus_refusals"]
    p, ad = compare_leaves(_drop(sa, SUMMARY_SKIP), _drop(sb, SUMMARY_SKIP), sp, "summary")
    problems += p
    if ad:
        added["summary"] = ad
    if (old / "netload.csv").read_bytes() != (new / "netload.csv").read_bytes():
        problems.append("netload.csv differs")
    out["added_by_pre_d69_code"] = added
    out["problems"] = problems[:MAX]
    out["n_problems"] = len(problems)
    out["verdict"] = "FAIL" if problems else "MATCH_EXCEPT_PRE_D69_ADDITIONS"
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--commit", required=True)
    ap.add_argument("--ref", action="append", default=[])
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    res = classify(Path(a.old), Path(a.new), a.commit, [Path(r) for r in a.ref])
    txt = json.dumps(res, indent=2, default=str)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(txt + "\n", encoding="utf-8")
    print(txt)
    return 0 if res["verdict"] != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main())
