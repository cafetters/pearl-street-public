"""Q-N10 parity judge (owner, Nov 19; declared in configs/parity_qn10/README.md before the run). Report only.

For each run under <new_root>/<name>/ and its reference <ref_root>/<name>/: every trace (exchange and selfish) must
match line for line, after removing the `idle_reference` lines the flag adds, with the header line and the run_id
field (which carries the run name) excluded; and no `over_power_nomination` reject may appear.

    python -m bench.qn10_parity <new_root> <ref_root> <name> [<name> ...]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _lines(path: Path) -> list[str]:
    out = []
    with open(path, encoding="utf-8") as f:
        next(f)                                           # the header line
        for line in f:
            m = json.loads(line)
            if m.get("msg_type") == "idle_reference":
                continue
            m.pop("run_id", None)
            out.append(json.dumps(m, sort_keys=True))
    return out


def judge(new: Path, ref: Path) -> dict:
    res = {"traces": {}, "over_power_rejects": 0}
    for t in sorted(ref.rglob("trace.jsonl")):
        rel = t.relative_to(ref)
        n = new / rel
        if not n.exists():
            res["traces"][str(rel)] = "missing"
            continue
        a, b = _lines(t), _lines(n)
        res["over_power_rejects"] += sum(1 for x in b if '"over_power_nomination"' in x)
        if a == b:
            res["traces"][str(rel)] = "identical"
        else:
            first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
            res["traces"][str(rel)] = f"differ: {len(a)} vs {len(b)} lines, first at {first}"
    res["pass"] = (bool(res["traces"]) and all(v == "identical" for v in res["traces"].values())
                   and res["over_power_rejects"] == 0)
    return res


if __name__ == "__main__":
    new_root, ref_root, names = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
    rows = {n: judge(new_root / n, ref_root / n) for n in names}
    for n, r in rows.items():
        print("PASS" if r["pass"] else "FAIL", n, r["traces"], "over_power_rejects", r["over_power_rejects"])
    print(f"{sum(r['pass'] for r in rows.values())} of {len(rows)} PASS")
