"""D61 v2 queue: a copy of the pinned D61 queue (runs/d61/run_queue.py, sha256 1bd49d0f1ab43e34, never edited),
fixed for v2 (Nov 6, item 6; not merged, not run):
  - the cell-to-twin mapping is `twin_of()`, built from the attack lists, so every attack name maps (the pinned
    regex lacked renege_zero_b: the feb02 max-rule reruns compared a cell with itself);
  - paths: runs/d61v2, configs/d61v2, docs/plans/d61v2_attacks_check.md; WINDOWS empty until the owner confirms
    the v2 pre-registration (the queue refuses to run without them);
  - peakmem.py is imported from the pinned runs/d61/ only when the queue runs, so tests can import this file.
Everything below is the pinned file's text otherwise.

Original docstring (D61 attack-run queue and the smoke pair; pinned by sha256 in docs/plans/d61_attacks_check.md):

Runs in ../pearl-street-d61run with the shared venv's python from the worktree root (its own code imports first).
Launched hidden through WMI, not a child of Claude's shell, with no visible window (owner, Nov 4: Schedule).

--smoke (owner, Nov 4: after the holdout, before the backfill; HEAD = the build commit, which carries only the two
smoke configs):
  waits for the holdout queue to end (runs/holdout/queue_status.json in ../pearl-street-holdout has done_utc) and its
  verdict, then: M0 sep05 -> twin run -> the all-attackers run -> bench.d61_check on the pair.
  Writes runs/d61/smoke_status.json with `go`:
    - false on a simulation failure: a crash, a roster failure, an integrity failure, or the twin not behaving as an
      honest twin (check verdict INTEGRITY_FAIL or TWIN_FAIL). D61 isn't launched; the backfill still runs.
    - true otherwise, including a failure of the check tool alone (listed for the owner; fixed through the amendment
      protocol and re-run offline on the saved traces).
default (the D61 runs; HEAD = the pre-registration commit, the last commit that touched DOC):
  waits for the backfill queue to end (../pearl-street-holdout/runs/backfill/queue_status.json has `ended`), then runs
  the steps of steps() in order, one process at a time. Before every run: no config resolves d62_guarantee to true
  (directly or through compare_to), else the queue stops.

Both modes:
- Before every step the queue waits while any other heavy Python job or pytest is running (the holdout queue's gate),
  then HEAD must be the expected commit and the tree clean, else the queue stops.
- A running step is never stopped. After each simulation run the queue waits GAP_AFTER_RUN_S (60 s).
- After every run and every M0: its header shows that commit without -dirty, else the queue stops.
- A roster failure stops the queue. Any other failure is recorded and the queue moves on. Nothing waits for the owner.
- Every step runs under runs/holdout/peakmem.py (copied in): wall time, peak memory, lowest free RAM.
- Restartable: steps that finished with exit 0 are skipped.
Run (from the worktree root):  python -u -m bench.d61v2_run_queue [--smoke] [--dry-run]
"""
import ctypes
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
D = REPO / "runs" / "d61v2"
PINNED = REPO / "runs" / "d61"
LOGS = D / "logs"
DOC = "docs/plans/d61v2_attacks_check.md"
CFG = "configs/d61v2"
HOLDOUT = REPO.parent / "pearl-street-holdout" / "runs"
GATE_POLL_S = 60
GAP_AFTER_RUN_S = 60
SETTLE_S = 120
HEAVY = re.compile(r"pytest|sim\.run_m1|bench\.|runs[\\/][^ ]*\.py|-m runs\.")
# Not heavy (owner, Nov 5): the cloud session's driver (bench.cloud_queue) and its maritime.cmd calls run no local
# simulation. Only these two are excluded; a pytest or simulation started from ../pearl-street-cloud still counts.
NOT_HEAVY = re.compile(r"bench\.cloud_queue|maritime\.cmd", re.IGNORECASE)
GAIN_TOL_USD = 1e-6
WINDOWS: list[str] = []      # D61 v2: set from the pre-registration once the owner confirms the windows
GROUP1 = ["renege_full", "renege_day", "renege_zero_b", "silent_deviate"]
GROUP2 = ["silent", "outage_stop", "outage_run", "misdeclare_power", "misdeclare_reserve", "infeasible_declare"]
M0_ATTACKS = ["renege_full", "renege_day", "renege_zero_b"]
ALL_ATTACKS = list(dict.fromkeys(GROUP1 + GROUP2 + M0_ATTACKS))
# longest name first, so silent_deviate is never read as silent
# lookarounds, so two names in a row are both found (and refused)
_ATTACK_RE = re.compile(r"(?<=_)(" + "|".join(sorted(map(re.escape, ALL_ATTACKS), key=len, reverse=True))
                        + r")(?=_)")


def twin_of(cell: str) -> str:
    """The honest twin of an attack cell: the attack name replaced by `twin`. Fails closed on a cell with no known
    attack name or more than one, and on a twin name equal to the cell."""
    found = _ATTACK_RE.findall(cell)
    if len(found) != 1:
        raise ValueError(f"{cell}: expected one attack name from {ALL_ATTACKS}, found {found}")
    twin = _ATTACK_RE.sub("twin", cell)
    if twin == cell or "_twin_" not in twin:
        raise ValueError(f"{cell}: twin mapping failed")
    return twin


def run_measured(*a, **kw):
    sys.path.insert(0, str(PINNED))
    from peakmem import run_measured as rm
    return rm(*a, **kw)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True).stdout.strip()


def clean_at(commit: str) -> str | None:
    head, dirty = git("rev-parse", "HEAD"), git("status", "--porcelain")
    if head != commit:
        return f"HEAD {head[:7]} is not the expected commit {commit[:7]}"
    if dirty:
        return f"tree dirty: {dirty.splitlines()[:3]}"
    return None


def sha16(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def pins() -> dict:
    return {"bench/d61v2_run_queue.py": sha16(Path(__file__)), "runs/d61/peakmem.py": sha16(PINNED / "peakmem.py")}


def heavy_jobs() -> list[str]:
    cmd = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
           "ForEach-Object { [string]$_.ProcessId + ' ' + $_.CommandLine }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True).stdout
    jobs = []
    for ln in out.splitlines():
        pid, _, line = ln.strip().partition(" ")
        if pid.isdigit() and HEAVY.search(line) and "run_queue.py" not in line and not NOT_HEAVY.search(line):
            jobs.append(f"{pid} {line[:160]}")
    return jobs


def wait_for_quiet():
    while heavy_jobs():
        time.sleep(GATE_POLL_S)


def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def wait_for(path: Path, key: str, why: str):
    print(f"{stamp()} waiting for {why}", flush=True)
    while key not in _json(path):
        time.sleep(GATE_POLL_S)
    time.sleep(SETTLE_S)                     # a verdict step may start just after; then wait for it like any job
    wait_for_quiet()
    print(f"{stamp()} {why}: done; going on", flush=True)


def cfg(name: str) -> str:
    return f"{CFG}/{name}.yaml"


def run_step(name: str) -> tuple:
    return (f"run_{name}", ["-m", "sim.run_m1", cfg(name)], name, "summary.json")


def check_step(cell: str, twin: str) -> tuple:
    return (f"check_{cell}", ["-m", "bench.d61_check", f"runs/{cell}", f"runs/{twin}", "--out",
                              f"runs/d61/checks/{cell}.json"], None, None)


def smoke_steps() -> list[tuple]:
    twin, cell = "h2h_c_d61smoke_sep05_twin_m5", "h2h_c_d61smoke_sep05_all_m5"
    return [("m0_sep05", ["-m", "bench.m0", cfg("m0_sep05")], "m0_sep05", "run.json"),
            run_step(twin), run_step(cell), check_step(cell, twin)]


def main_steps() -> list[tuple]:
    """Step 7's order (owner, Nov 4): M0 and feasibility per window; group 1 (reneging at $5 and m = 0, silent then
    deviate), each window in order, a twin before its first cell; group 2 (the other attacks); the conditional
    max-rule reruns are added at the end by conditional_steps()."""
    out = []
    for w in WINDOWS:
        out += [(f"m0_{w}", ["-m", "bench.m0", cfg(f"m0_{w}")], f"m0_{w}", "run.json"),
                (f"feas_{w}", ["-m", "bench.h2h_feasibility", "--out", f"runs/d61/feas/{w}.json",
                               cfg(f"h2h_c_d61_{w}_twin_m5")], None, None)]
    for w in WINDOWS:
        for mk, names in (("m5", GROUP1), ("m0", M0_ATTACKS)):
            twin = f"h2h_c_d61_{w}_twin_{mk}"
            out.append(run_step(twin))
            for a in names:
                c = f"h2h_c_d61_{w}_{a}_{mk}"
                out += [run_step(c), check_step(c, twin)]
    for w in WINDOWS:
        twin = f"h2h_c_d61_{w}_twin_m5"
        for a in GROUP2:
            c = f"h2h_c_d61_{w}_{a}_m5"
            out += [run_step(c), check_step(c, twin)]
    return out


def conditional_steps() -> list[tuple]:
    """Pre-registered: every cell where any attacker's gain is above GAIN_TOL_USD under today's rule, and its twin
    once, rerun with settlement_rule: max_first_call (report only), after the last main run."""
    out, twins = [], []
    for f in sorted((D / "checks").glob("h2h_c_d61_*.json")):
        res = _json(f)
        gains = [a.get("gain_usd", 0.0) for a in (res.get("attackers") or {}).values()]
        if any(g > GAIN_TOL_USD for g in gains):
            cell = f.stem
            twin = twin_of(cell)
            if twin not in twins:
                twins.append(twin)
                out.append(run_step(f"{twin}_max"))
            out += [run_step(f"{cell}_max"), check_step(f"{cell}_max", f"{twin}_max")]
    return out


def d62_off(step) -> str | None:
    """No config, directly or through compare_to, resolves d62_guarantee to true."""
    import yaml
    seen, todo = set(), [a for a in step[1] if a.endswith(".yaml")]
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        c = yaml.safe_load((REPO / p).read_text()) or {}
        if c.get("d62_guarantee"):
            return f"{p} sets d62_guarantee"
        ct = c.get("compare_to")
        if ct and str(ct) != "none":
            todo.append(str(ct))
    return None


def header_commit(run: str, fname: str) -> str | None:
    h = _json(REPO / "runs" / run / fname)
    return (h.get("header") or h).get("git_commit") if h else None


def log_has(name: str, word: str) -> bool:
    f = LOGS / f"{name}.log"
    return f.exists() and word in f.read_text(encoding="utf-8", errors="replace")


def execute(steps, commit: str, st: dict, status: Path) -> str | None:
    save = lambda: status.write_text(json.dumps(st, indent=1))
    done = {s["step"] for s in st["steps"] if s.get("returncode") == 0}
    for name, args, run, hfile in steps:
        if name in done:
            continue
        wait_for_quiet()
        bad = clean_at(commit) or (d62_off((name, args)) if run else None)
        if bad:
            return f"integrity before {name}: {bad}"
        row = {"step": name, "start_utc": stamp()}
        st["steps"].append(row)
        save()
        print(f"{stamp()} start {name}", flush=True)
        (D / "checks").mkdir(parents=True, exist_ok=True)
        (D / "feas").mkdir(parents=True, exist_ok=True)
        rec = run_measured(args, LOGS / f"{name}.log", REPO, live=D / "live_memory.json")
        rc = rec["returncode"]
        row.update({"end_utc": rec["end_utc"], "returncode": rc,
                    **{k: rec[k] for k in ("wall_min", "peak_working_set_mb", "peak_commit_mb", "min_free_ram_mb")}})
        print(f"{stamp()} end {name}: exit {rc}, {rec['wall_min']} min, peak {rec['peak_working_set_mb']} MB, "
              f"lowest free RAM {rec['min_free_ram_mb']} MB", flush=True)
        if run is not None and hfile == "summary.json":
            (REPO / "runs" / run).mkdir(parents=True, exist_ok=True)
            (REPO / "runs" / run / "peak_memory.json").write_text(json.dumps(rec, indent=1))
        if run is not None and rc == 0 and header_commit(run, hfile) != commit:
            row["note"] = f"header commit {header_commit(run, hfile)}"
            save()
            return f"integrity: {run} header shows {header_commit(run, hfile)}, not {commit} clean"
        if rc != 0:
            if log_has(name, "roster"):
                row["note"] = "roster failure"
                save()
                return f"roster failure in {name}"
            row["note"] = "failed (recorded; queue moves on)"
        save()
        if hfile == "summary.json":
            print(f"{stamp()} gap {GAP_AFTER_RUN_S} s before the next step (a pytest may start now)", flush=True)
            time.sleep(GAP_AFTER_RUN_S)
    return None


def smoke_verdict(st: dict) -> dict:
    rows = {s["step"]: s for s in st["steps"]}
    sim_fail = [n for n, *_ in smoke_steps()[:3] if (rows.get(n) or {}).get("returncode") != 0]
    res = _json(D / "checks" / "h2h_c_d61smoke_sep05_all_m5.json")
    tool_rc = (rows.get("check_h2h_c_d61smoke_sep05_all_m5") or {}).get("returncode")
    v = res.get("verdict")
    if st.get("stopped"):
        sim_fail.append(st["stopped"])
    if v in ("INTEGRITY_FAIL", "TWIN_FAIL"):
        sim_fail.append(f"check: {v}: {res.get('integrity') or res.get('twin')}")
    tool_fail = None if (tool_rc == 0 and v in ("PASS", "FAIL")) else f"check tool: exit {tool_rc}, verdict {v}"
    return {"go": not sim_fail, "simulation_failures": sim_fail, "check_tool_failure": tool_fail if not sim_fail else None,
            "check_verdict": v}


def main(argv) -> int:
    if not WINDOWS:
        print("D61 v2: no windows set (the owner confirms the pre-registration first)")
        return 2
    smoke = "--smoke" in argv
    commit = git("rev-parse", "HEAD") if smoke else git("log", "-1", "--format=%H", "--", DOC)
    status = D / ("smoke_status.json" if smoke else "queue_status.json")
    steps = smoke_steps() if smoke else main_steps()
    if "--dry-run" in argv:
        missing = [a for _, args, *_ in steps for a in args if a.endswith(".yaml") and not (REPO / a).exists()]
        print(f"commit {commit[:12]}; integrity: {clean_at(commit) or 'ok'}; heavy now: {heavy_jobs() or 'none'}; "
              f"pins {pins()}")
        for i, (n, *_) in enumerate(steps, 1):
            print(f"  {i:3d} {n}")
        print(f"{len(steps)} steps; missing configs: {missing or 'none'}")
        return 0 if not missing and not clean_at(commit) else 1
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    st = _json(status) or {"steps": []}
    st.update({"commit": commit, "pins": pins(), "started_utc": st.get("started_utc") or stamp()})
    for k in ("stopped", "done_utc"):
        st.pop(k, None)
    status.write_text(json.dumps(st, indent=1))
    if smoke:
        wait_for(HOLDOUT / "holdout" / "queue_status.json", "done_utc", "the holdout queue to end")
    else:
        wait_for(HOLDOUT / "backfill" / "queue_status.json", "ended", "the backfill queue to end")
    stop = execute(steps, commit, st, status)
    if not smoke and not stop:
        stop = execute(conditional_steps(), commit, st, status)
    st["stopped"] = stop
    st["done_utc"] = stamp()
    if smoke:
        st["smoke"] = smoke_verdict(st)
    status.write_text(json.dumps(st, indent=1))
    print(f"{stamp()} queue ended: {stop or 'all steps run'}" + (f"; smoke: {st['smoke']}" if smoke else ""), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
