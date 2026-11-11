"""
Exchange-only timing: replay one trace's exchange inputs through the current exchange/core.py and through the version
at an older git ref, check both reproduce the trace exactly, and report seconds (nomination cache check, supplementary).

Run:  python -m bench.replay_timing <trace.jsonl> <old_ref> [--out result.json]
The old core is loaded from `git show <old_ref>:exchange/core.py` into a scratch module; it imports the current
exchange.messages, clearing, imbalance, guarantee, price_mode and settlement, so only use refs where those are unchanged.
"""
import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from exchange import core as new_core
from exchange import messages as M
from exchange.trace.logger import line_to_msg

REPO = Path(__file__).resolve().parents[1]


def load_old(ref: str):
    src = subprocess.run(["git", "show", f"{ref}:exchange/core.py"], cwd=REPO, capture_output=True, text=True,
                         check=True).stdout
    d = Path(tempfile.mkdtemp())
    p = d / "core_old.py"
    p.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("core_old", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def time_replay(core_mod, header, events) -> dict:
    import inspect
    cfg = dict(header["exchange"])
    if "attested_owners" in inspect.signature(core_mod.Exchange).parameters:
        cfg = {"attested_owners": False, **cfg}          # pre-D69 traces carry no owner attestation
    ex = core_mod.Exchange(**cfg)
    expected = [line_to_msg(e) for e in events if e["from"] == "exchange" and "reason" not in e]
    got = []
    t0 = time.perf_counter()
    for e in events:
        if e["to"] == "exchange" and e.get("event", "send") == "send":
            got += [M.decode(r) for r in ex.handle(M.encode(line_to_msg(e)))]
    secs = time.perf_counter() - t0
    return {"secs": secs, "match": got == expected, "messages_out": len(got)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("trace")
    ap.add_argument("old_ref")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    lines = [json.loads(s) for s in Path(a.trace).read_text(encoding="utf-8").splitlines() if s.strip()]
    header, events = lines[0], lines[1:]
    res = {"trace": a.trace, "old_ref": a.old_ref, "new_core": new_core.__file__,
           "new": time_replay(new_core, header, events)}
    res["old"] = time_replay(load_old(a.old_ref), header, events)
    res["speedup"] = res["old"]["secs"] / res["new"]["secs"] if res["new"]["secs"] else None
    txt = json.dumps(res, indent=2)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(txt + "\n", encoding="utf-8")
    print(txt)
    return 0 if res["new"]["match"] and res["old"]["match"] else 1


if __name__ == "__main__":
    sys.exit(main())
