"""Run header shared by bench scripts: config, seed and git commit (CLAUDE.md rule 6)."""
import subprocess
from datetime import datetime, timezone

from sim.data import REPO


def git_commit() -> str:
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    return sha + ("-dirty" if dirty else "")


def run_header(script: str, config: dict, seed=None) -> dict:
    return {"script": script, "config": {k: str(v) for k, v in config.items()}, "seed": seed,
            "git_commit": git_commit(), "run_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
