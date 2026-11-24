"""M2b pass item (c) as a launcher job (owner, Nov 18 overnight): the LP ceiling with data-center deferral next to the
battery-only ceiling, for one window.

    python -m bench.m0_dc <config.yaml>

Runs bench/m0.py's main_dc (which writes ceiling_dc.json into the config's m0_dir) and writes runs/<run_name>/:
summary.json (the same output) and trace.jsonl (one header line: config, seed, git commit), so the cloud launcher
downloads it. Exit 0 when both ceilings solved on every variant, 1 otherwise (the summary is written either way).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from bench.runinfo import run_header
from sim.data import REPO, load_config

RUNS = REPO / "runs"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    from bench.m0 import main_dc
    cfg = load_config(argv[0])
    out = Path(RUNS) / str(cfg["run_name"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "trace.jsonl").write_text(json.dumps({"header": run_header("bench/m0_dc.py", cfg)}) + "\n",
                                     encoding="utf-8")
    res = main_dc(argv[0])
    (out / "summary.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    ok = all(v.get("battery_only_solved") and v.get("with_deferral_solved") for v in res["variants"].values())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
