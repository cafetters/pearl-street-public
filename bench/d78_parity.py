"""D78 default-on parity judge (owner, Nov 20: "Good to run these when ready"; declared in configs/parity_d78/README.md
before the run). Report only.

For each run under <new_root>/<name>/ (configs/parity_d78, `silent_reference: true`) and its reference
<ref_root>/<name>/ (flag off): every trace (exchange and selfish) must match line for line once these are set aside,
and nothing else:
- the header line and the run_id field (it carries the run name);
- in the new trace, the `selfish_reference` lines for non-binding calls (D79). Their count must be exactly
  (assets with a binding-call reference) x (non-binding calls that ran price rounds);
- metering message ids `metering:<n>`: D79's extra lines share the metering service's counter, so every later id
  shifts. Every `"metering:<digits>"` string becomes `"metering:N"`. The `metering:ownerN` and `metering:idleN` ids are
  kept.

With --idle-root <qn10_root> (both proposed defaults together, configs/parity_d78q/README.md): the new trace's
`idle_reference` lines (Q-N10's check) are also set aside, but must equal, line for line and without run_id, those of
the Q-N10 parity run <qn10_root>/<name>/; and no `over_power_nomination` reject may appear.

    python -m bench.d78_parity <new_root> <ref_root> <name> [<name> ...] [--idle-root <qn10_root>]
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_MID = re.compile(r'"metering:\d+"')


def _read(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        next(f)                                           # the header line
        return [json.loads(line) for line in f]


def _norm(msgs: list[dict]) -> list[str]:
    out = []
    for m in msgs:
        m = dict(m)
        m.pop("run_id", None)
        out.append(_MID.sub('"metering:N"', json.dumps(m, sort_keys=True)))
    return out


def strip_d79(msgs: list[dict]) -> tuple[list[dict], int, int]:
    """(messages without the non-binding selfish_reference lines, lines removed, lines expected)."""
    binding = {}
    for m in msgs:
        p = m.get("payload") or {}
        if m.get("msg_type") == "call_nominations" and "call_id" in p:
            binding[p["call_id"]] = bool(p.get("binding"))
    priced = {(m.get("payload") or {}).get("call_id") for m in msgs if m.get("msg_type") == "peak_price"}
    nonbinding_priced = {c for c in priced if binding.get(c) is False}
    assets = {m["payload"]["asset_id"] for m in msgs
              if m.get("msg_type") == "selfish_reference" and binding.get(m["payload"]["call_id"]) is True}
    kept = [m for m in msgs if not (m.get("msg_type") == "selfish_reference"
                                    and binding.get(m["payload"]["call_id"]) is False)]
    return kept, len(msgs) - len(kept), len(assets) * len(nonbinding_priced)


def _idle(msgs: list[dict]) -> tuple[list[dict], list[str]]:
    """(messages without idle_reference lines, those lines normalized)."""
    idle = [m for m in msgs if m.get("msg_type") == "idle_reference"]
    return [m for m in msgs if m.get("msg_type") != "idle_reference"], _norm(idle)


def judge(new: Path, ref: Path, idle_root: Path | None = None) -> dict:
    res = {"traces": {}, "d79_lines": {}, "idle_lines": {}, "over_power_rejects": 0}
    for t in sorted(ref.rglob("trace.jsonl")):
        rel = t.relative_to(ref)
        n = new / rel
        if not n.exists():
            res["traces"][str(rel)] = "missing"
            continue
        msgs = _read(n)
        res["over_power_rejects"] += sum(1 for m in msgs if m.get("msg_type") == "reject"
                                         and "over_power_nomination" in json.dumps(m.get("payload")))
        if idle_root is not None:
            msgs, got = _idle(msgs)
            q = idle_root / rel
            want = _idle(_read(q))[1] if q.exists() else None
            res["idle_lines"][str(rel)] = (f"identical ({len(got)})" if got == want else
                                           "qn10 trace missing" if want is None else
                                           f"differ: {len(got)} vs {len(want)} lines")
        kept, removed, expected = strip_d79(msgs)
        res["d79_lines"][str(rel)] = {"removed": removed, "expected": expected}
        a, b = _norm(_read(t)), _norm(kept)
        if removed != expected:
            res["traces"][str(rel)] = f"d79 lines {removed} vs expected {expected}"
        elif a == b:
            res["traces"][str(rel)] = "identical"
        else:
            first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
            res["traces"][str(rel)] = f"differ: {len(a)} vs {len(b)} lines, first at {first}"
    res["pass"] = (bool(res["traces"]) and all(v == "identical" for v in res["traces"].values())
                   and all(v.startswith("identical") for v in res["idle_lines"].values())
                   and res["over_power_rejects"] == 0)
    return res


if __name__ == "__main__":
    args = sys.argv[1:]
    idle_root = None
    if "--idle-root" in args:
        i = args.index("--idle-root")
        idle_root = Path(args[i + 1])
        del args[i:i + 2]
    new_root, ref_root, names = Path(args[0]), Path(args[1]), args[2:]
    rows = {n: judge(new_root / n, ref_root / n, None if idle_root is None else idle_root / n) for n in names}
    for n, r in rows.items():
        print("PASS" if r["pass"] else "FAIL", n, r["traces"], r["d79_lines"],
              *([r["idle_lines"], "over_power_rejects", r["over_power_rejects"]] if idle_root is not None else []))
    print(f"{sum(r['pass'] for r in rows.values())} of {len(rows)} PASS")
