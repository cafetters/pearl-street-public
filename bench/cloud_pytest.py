"""Full pytest as a launcher run (owner decision 4, Nov 12): `python -m bench.cloud_queue ... --module bench.cloud_pytest`.

    python -m bench.cloud_pytest <config.yaml>

Config keys used: run_name; pytest_args (default: -q -rs, the whole tests/ folder, no cache provider); pip_install
(optional list of exact requirement pins installed first, e.g. the `llm` extra so the adapter tests run instead of
skipping). Other keys (data_dir, start, end, m0_dir, ...) are there for the launcher's checks and are ignored.
Writes runs/<run_name>/: pytest.log, junit.xml, summary.json (counts, failed tests, skip reasons, exit, pins) and
trace.jsonl (one header line: config, seed, git commit). Exits with pytest's exit code (pip failing: 3).
"""
from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from bench.runinfo import run_header

DEFAULT_ARGS = ["tests", "-q", "-rs", "-p", "no:cacheprovider"]


def _junit(path: Path) -> dict:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    c = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    failed, skips = [], []
    for s in suites:
        for k in c:
            c[k] += int(s.get(k, 0))
        for tc in s.iter("testcase"):
            name = f"{tc.get('classname')}::{tc.get('name')}"
            if tc.find("failure") is not None or tc.find("error") is not None:
                failed.append(name)
            sk = tc.find("skipped")
            if sk is not None:
                skips.append(f"{name}: {sk.get('message', '')}")
    return {"passed": c["tests"] - c["failures"] - c["errors"] - c["skipped"], "failed": c["failures"],
            "errors": c["errors"], "skipped": c["skipped"], "failed_tests": failed, "skip_reasons": skips}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cfg = yaml.safe_load(Path(argv[0]).read_text(encoding="utf-8")) or {}
    out = Path("runs") / str(cfg["run_name"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "trace.jsonl").write_text(json.dumps({"header": run_header("bench/cloud_pytest.py", cfg)}) + "\n",
                                     encoding="utf-8")
    summary = {"run_name": cfg["run_name"], "pip_install": list(cfg.get("pip_install") or [])}
    with open(out / "pytest.log", "w", encoding="utf-8") as log:
        if summary["pip_install"]:
            p = subprocess.run([sys.executable, "-m", "pip", "install", "--no-input", "-q", *summary["pip_install"]],
                               stdout=log, stderr=subprocess.STDOUT)
            if p.returncode != 0:
                summary.update(exit=3, error="pip install failed")
                (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
                log.flush()
                print("PIP_INSTALL_FAILED", summary["pip_install"], flush=True)
                print((out / "pytest.log").read_text(encoding="utf-8", errors="replace")[-3000:], flush=True)
                return 3
        args = [str(a) for a in (cfg.get("pytest_args") or DEFAULT_ARGS)]
        rc = subprocess.run([sys.executable, "-m", "pytest", *args, f"--junitxml={out / 'junit.xml'}"],
                            stdout=log, stderr=subprocess.STDOUT).returncode
    summary["exit"] = rc
    summary["pytest_args"] = args
    if (out / "junit.xml").exists():
        summary.update(_junit(out / "junit.xml"))
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    # A failed launcher run keeps only run.log (this stdout), not the run folder: print what the report needs.
    print(json.dumps({k: summary.get(k) for k in ("exit", "passed", "failed", "errors", "skipped")}), flush=True)
    for t in summary.get("failed_tests", []):
        print(f"FAILED_TEST {t}")
    for s in summary.get("skip_reasons", []):
        print(f"SKIP {s}")
    log = (out / "pytest.log").read_text(encoding="utf-8", errors="replace").splitlines()
    i = next((k for k, ln in enumerate(log) if "short test summary info" in ln), max(0, len(log) - 80))
    print("\n".join(log[max(0, i - 120):][:400]), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
