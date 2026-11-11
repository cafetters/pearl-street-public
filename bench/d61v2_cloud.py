"""D61 v2 on Maritime (gap 3; owner, Nov 7, D4): from generated configs (bench.d61v2_make_configs), their M0 folders
and a commit, build bench.cloud_queue's arguments (PLAN section 6 cloud rules), launch the queue (or print it with
--print-only), then judge every downloaded attack cell against its twin with bench.d61v2_judge.

    python -u -m bench.d61v2_cloud --configs <dir> --commit <sha> --queue-id <id> --concurrency <n> --budget <n>
        --measured-runtime-s <s> [--max-awake <n>] [--m0-root <checkout>] [--glob '*.yaml'] [--names <run> ...]
        [--include-max] [--print-only | --judge-only] [--judge-out <dir>] [--d90-code-root <dir>] [--data-dir <dir>]

Before anything launches (fails closed, exit 2): every config resolves d62_guarantee to false, reads data from
../data (the cloud's data folder), has an M0 folder under --m0-root with ceiling.json, and with ceiling_controllable.json
when it sets peak_steps: controllable; concurrency at most 10 (no ramp approved) and at most --max-awake; a budget at
least the number of runs. bench.cloud_queue then applies the rest of the cloud rules (spend, time limits, cleanup).
Order (the draft's): each window's $5 twin and its cells, window by window (the v2 windows in the draft's order, any
other window by start date); then the m = 0 twins and cells; then, with --include-max, the conditional reruns.
The launcher runs in this process's foreground: run this driver in the foreground or as a background task of a
Claude Code session, never as a detached process (PLAN section 6).
"""
import argparse
import sys
from pathlib import Path

import yaml

from bench import d61v2_judge as J
from bench import d61v2_make_configs as G

REPO = Path(__file__).resolve().parents[1]
MAX_CONCURRENCY = 10                 # PLAN section 6 (Nov 6): no ramp above 10 approved
ATTACK_ORDER = list(G.attacks("x"))


class Refused(Exception):
    pass


def twin_for(name: str) -> str:
    return J.twin_for(name)


def _false(v) -> bool:
    return str(v).strip().lower() in ("false", "0", "no", "none", "")


def _key(cfg: dict):
    name = cfg["run_name"]
    parts = name.split("_")
    tag = parts[3] if len(parts) > 3 else ""
    v2 = [w[0] for w in G.WINDOWS]
    rank = (0, v2.index(tag), "") if tag in v2 else (1, 0, str(cfg.get("start")))
    is_max = name.endswith("_max")
    m5 = float(cfg.get("peak_response_margin_usd_mwh") or 0.0) == 5.0
    group = 2 if is_max else (0 if m5 else 1)
    twin = "_twin_" in name
    att = next((i for i, a in enumerate(ATTACK_ORDER) if f"_{a}_" in name), len(ATTACK_ORDER))
    return (group, 0 if m5 else 1, rank, not twin, att, name)


def select(configs: Path, include_max: bool = False, names=None, glob: str = "*.yaml") -> list[tuple[Path, dict]]:
    items = []
    for p in sorted(Path(configs).glob(glob)):
        if p.name.startswith("m0_"):
            continue
        cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not include_max and str(cfg.get("run_name", "")).endswith("_max") and not names:
            continue
        items.append((p, cfg))
    if names:
        have = {c["run_name"]: (p, c) for p, c in items}
        missing = [n for n in names if n not in have]
        if missing:
            raise Refused(f"configs not found: {missing}")
        items = [have[n] for n in names]
    return sorted(items, key=lambda pc: _key(pc[1]))


def plan(items, m0_root: Path) -> list[tuple[str, str]]:
    out = []
    for p, cfg in items:
        if not _false(cfg.get("d62_guarantee", False)):
            raise Refused(f"{p.name}: d62_guarantee resolves to true")
        if str(cfg.get("data_dir")).rstrip("/\\") != "../data":
            raise Refused(f"{p.name}: data_dir must be ../data")
        m0 = Path(m0_root) / str(cfg.get("m0_dir", ""))
        if not (m0 / "ceiling.json").exists():
            raise Refused(f"{p.name}: {m0} has no ceiling.json")
        if cfg.get("peak_steps") == "controllable" and not (m0 / "ceiling_controllable.json").exists():
            raise Refused(f"{p.name}: {m0} has no ceiling_controllable.json (run bench.m0 --controllable)")
        out.append((str(p.resolve()), str(m0.resolve())))
    return out


def queue_args(pairs, commit: str, concurrency: int, budget: int, runtime_s: float, max_awake: int,
               queue_id: str) -> list[str]:
    if concurrency > MAX_CONCURRENCY:
        raise Refused(f"concurrency {concurrency} above {MAX_CONCURRENCY} (PLAN section 6: no ramp approved)")
    if concurrency > max_awake:
        raise Refused(f"concurrency {concurrency} above --max-awake {max_awake}")
    if budget < len(pairs):
        raise Refused(f"budget {budget} below the {len(pairs)} runs")
    args = []
    for cfg, m0 in pairs:
        args += ["--run", cfg, m0]
    return args + ["--commit", commit, "--concurrency", str(concurrency), "--budget", str(budget),
                   "--measured-runtime-s", f"{runtime_s:g}", "--max-awake", str(max_awake), "--queue-id", queue_id]


def judge_pairs(items) -> list[tuple[str, str]]:
    names = {c["run_name"] for _, c in items}
    out = []
    for _, c in items:
        n = c["run_name"]
        if "_twin_" in n:
            continue
        tw = twin_for(n)
        if tw in names:
            out.append((n, tw))
    return out


def launch(args) -> int:
    import subprocess
    return subprocess.run([sys.executable, "-u", "-m", "bench.cloud_queue", *args], cwd=REPO).returncode


def judge(pairs, runs: Path, out: Path, d90_code_root=None, data_dir=None) -> int:
    argv = ["--runs", str(runs), "--out-dir", str(out)]
    for c, t in pairs:
        argv += ["--pair", c, t]
    argv += ["--d90-code-root", str(d90_code_root)] if d90_code_root else []
    argv += ["--data-dir", str(data_dir)] if data_dir else []
    return J.main(argv)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--configs", required=True)
    ap.add_argument("--commit", required=True)
    ap.add_argument("--queue-id", required=True)
    ap.add_argument("--concurrency", type=int, required=True)
    ap.add_argument("--budget", type=int, required=True)
    ap.add_argument("--measured-runtime-s", type=float, required=True)
    ap.add_argument("--max-awake", type=int, default=MAX_CONCURRENCY)
    ap.add_argument("--m0-root", default=str(REPO))
    ap.add_argument("--glob", default="*.yaml")
    ap.add_argument("--names", nargs="*")
    ap.add_argument("--include-max", action="store_true")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--print-only", action="store_true")
    mode.add_argument("--judge-only", action="store_true")
    ap.add_argument("--runs", default=str(REPO / "runs" / "cloud"))
    ap.add_argument("--judge-out")
    ap.add_argument("--d90-code-root")
    ap.add_argument("--data-dir")
    a = ap.parse_args(argv)
    try:
        items = select(Path(a.configs), a.include_max, a.names, a.glob)
        if not items:
            raise Refused(f"no configs in {a.configs} matching {a.glob}")
        args = queue_args(plan(items, Path(a.m0_root)), a.commit, a.concurrency, a.budget, a.measured_runtime_s,
                          a.max_awake, a.queue_id)
    except Refused as e:
        print(f"REFUSED: {e}")
        return 2
    pairs = judge_pairs(items)
    judge_out = Path(a.judge_out or Path(a.runs) / "_judge" / a.queue_id)
    if a.print_only:
        print("python -u -m bench.cloud_queue " + " ".join(args))
        print(f"then judge {len(pairs)} cells -> {judge_out}: " + ", ".join(c for c, _ in pairs))
        return 0
    if not a.judge_only:
        rc = launch(args)
        if rc:
            print(f"queue exit {rc}: not judged")
            return rc
    return judge(pairs, Path(a.runs), judge_out, a.d90_code_root, a.data_dir)


if __name__ == "__main__":
    sys.exit(main())
