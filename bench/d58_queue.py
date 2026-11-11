"""
D58 margin test queue, in the pre-registered order (docs/plans/d58_margin_report.md). One process at a time, from the
repo root: each run is `python -u -m sim.run_m1 configs/d58m/<run>.yaml > runs/d58m/logs/<run>.log`, then
`python -m bench.d58_margin cell <run>`.

- Keeps Windows awake for the whole queue (SetThreadExecutionState: continuous, system and display required, since
  this laptop uses S0 Modern Standby) and clears the request at exit.
- After each run, flags any gap over 5 minutes between consecutive wall-clock marks of the run (selfish start and
  end, exchange start, every price round, exchange end) as a possible sleep stall.
- Writes runs/d58m/queue_status.json after every run: run, start and end, exit codes, relief, stall flags, next run.
- A crash is recorded and the queue continues. Re-running the queue skips runs whose cell result exists, so it
  resumes where it stopped.

Order:
  1. m = 0 baseline reruns (Jan 12-13, Nov 28-29), then the baseline check. On a mismatch, or if either baseline
     run fails, the other nine m = 0 cells run on this build and become the comparator.
  2. Margins by the bisection rule (bench.d58_margin.next_margin), windows in WINDOWS order, with the determinism
     rerun right after the first margin cell; a margin stops at its first window below the relief floor.
  3. The fresh window at m = 0, then at each margin that passed, and its confirmation check.

Run:  python -u -m bench.d58_queue > runs/d58m/queue.log
"""
import argparse
import ctypes
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from bench import d58_margin as G
from bench.d56_resettle import CheckFailed

REPO = G.REPO
LOGS = G.OUT / "logs"
STATUS = G.OUT / "queue_status.json"
STALL_S = 300.0
ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002


def keep_awake(on: bool) -> bool:
    """Ask Windows not to sleep (on) or clear the request (off). False where unsupported."""
    if sys.platform != "win32":
        return False
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED if on else 0)
    return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def wall_marks(summ: dict) -> list[tuple[str, str]]:
    marks = []
    sf = summ.get("selfish", {}).get("wall_utc")
    if sf:
        marks += [("selfish start", sf["start"]), ("selfish end", sf["end"])]
    for h, ev in summ.get("exchange_by_headroom_mw", {}).items():
        w = ev.get("wall_utc")
        if w:
            marks.append((f"exchange h{h} start", w["start"]))
        marks += [(f"h{h} round {i + 1}", t) for i, t in enumerate((ev.get("price_mode") or {}).get("round_wall_utc", []))]
        if w:
            marks.append((f"exchange h{h} end", w["end"]))
    return marks


def stall_gaps(marks: list[tuple[str, str]], limit_s: float = STALL_S) -> list[dict]:
    """Consecutive wall-clock marks more than limit_s apart: possible sleep stalls."""
    out = []
    for (la, a), (lb, b) in zip(marks, marks[1:]):
        d = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
        if d > limit_s:
            out.append({"from": la, "to": lb, "at_utc": a, "seconds": d})
    return out


class Queue:
    def __init__(self, max_runs: int | None = None):
        self.max_runs = max_runs
        self.n_started = 0
        self.status = json.loads(STATUS.read_text()) if STATUS.exists() else {"runs": [], "verdicts": {},
                                                                                "notes": []}
        self.status["queue_started_utc"] = now()
        self.status["keep_awake"] = keep_awake(True)

    def save(self, **kw):
        self.status.update(kw)
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps(self.status, indent=1, default=float))

    def note(self, msg: str):
        print(f"[{now()}] {msg}", flush=True)
        self.status["notes"].append({"t_utc": now(), "note": msg})
        self.save()

    def done(self, run: str) -> bool:
        return (G.CELLS_OUT / f"{run}.json").exists()

    def tool(self, *args) -> int:
        with open(LOGS / "tools.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{now()}] bench.d58_margin {' '.join(args)}\n")
            f.flush()
            return subprocess.run([sys.executable, "-u", "-m", "bench.d58_margin", *args], stdout=f,
                                  stderr=subprocess.STDOUT, cwd=REPO).returncode

    def run(self, run: str, nxt: str | None = None) -> dict:
        if self.done(run):
            row = {"run": run, "skipped": "cell result exists"}
            self.status["runs"].append(row)
            self.save(running=None, next=nxt)
            return row
        if self.max_runs is not None and self.n_started >= self.max_runs:
            raise SystemExit(f"max runs ({self.max_runs}) reached before {run}")
        self.n_started += 1
        LOGS.mkdir(parents=True, exist_ok=True)
        start = now()
        self.save(running=run, running_since_utc=start, next=nxt)
        print(f"[{start}] start {run}", flush=True)
        with open(LOGS / f"{run}.log", "w", encoding="utf-8") as f:
            rc = subprocess.run([sys.executable, "-u", "-m", "sim.run_m1", str(G.CONFIGS / f"{run}.yaml")],
                                stdout=f, stderr=subprocess.STDOUT, cwd=REPO).returncode
        rc_cell = self.tool("cell", run) if rc == 0 else None
        row = {"run": run, "start_utc": start, "end_utc": now(), "exit_code": rc, "cell_exit_code": rc_cell}
        sf = REPO / "runs" / run / "summary.json"
        if rc == 0 and sf.exists():
            row["stalls"] = stall_gaps(wall_marks(json.loads(sf.read_text())))
            if row["stalls"]:
                print(f"  possible sleep stall(s): {row['stalls']}", flush=True)
        self.status["runs"].append(row)
        self.save(running=None, next=nxt)
        print(f"[{row['end_utc']}] end {run}: exit {rc}, cell {rc_cell}", flush=True)
        return row

    def relief(self, row: dict, w: str) -> dict | None:
        """The staged stop's relief check for one window (None if the run or its cell failed)."""
        try:
            r = row["run"]
            rel = G.relief(G.load(r), G.summary(r), G.comparator_cuts()[w])
        except (CheckFailed, FileNotFoundError, KeyError) as err:
            self.note(f"{row['run']}: relief not computed ({err})")
            return None
        row["relief"] = rel
        self.save()
        return rel


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-runs", type=int, default=None)
    a = ap.parse_args(argv)
    q = Queue(a.max_runs)
    try:
        q.note(f"queue start; keep-awake request {'set' if q.status['keep_awake'] else 'NOT set'}")
        # 1. m = 0 baseline
        rows = [q.run(G.run_name(w, 0.0), nxt) for w, nxt in
                zip(G.BASELINE, [G.run_name(G.BASELINE[1], 0.0), G.run_name("jan12", G.FIRST_MARGIN)])]
        ok = all(q.done(r["run"]) for r in rows) and q.tool("baseline") == 0
        match = ok and json.loads((G.OUT / "baseline.json").read_text())["match"]
        if not match:
            if not ok:     # a failed baseline run counts as a mismatch (pre-registered)
                (G.OUT / "baseline.json").write_text(json.dumps({"match": False, "reason": "baseline run failed"}))
            q.note("baseline mismatch: running the other nine m = 0 cells on this build as the comparator")
            for w in G.WINDOWS:
                if w not in G.BASELINE:
                    q.run(G.run_name(w, 0.0))
        q.save(baseline_match=bool(match))
        # 2. margins, bisected
        verdicts = {}
        while (m := G.next_margin(verdicts)) is not None:
            q.note(f"margin {m:g} $/MWh")
            for i, w in enumerate(G.WINDOWS):
                nxt = G.run_name(G.WINDOWS[i + 1], m) if i + 1 < len(G.WINDOWS) else "verdict"
                row = q.run(G.run_name(w, m), G.DETERMINISM[1] if (m, w) == (G.FIRST_MARGIN, "jan12") else nxt)
                if (m, w) == (G.FIRST_MARGIN, "jan12"):
                    q.run(G.DETERMINISM[1], nxt)
                    if q.done(G.DETERMINISM[0]) and q.done(G.DETERMINISM[1]) and q.tool("determinism") == 0:
                        det = json.loads((G.OUT / "determinism.json").read_text())
                        q.save(determinism_match=det["match"])
                        if not det["match"]:
                            q.note(f"determinism MISMATCH: {det['differences'][:5]}")
                    else:
                        q.note("determinism check could not run")
                rel = q.relief(row, w) if q.done(row["run"]) else None
                if rel is not None and not rel["pass"]:
                    q.note(f"margin {m:g}: {w} below the relief floor ({rel['ratio']:.1%}); stopping this margin")
                    break
            rc = q.tool("verdict", f"{m:g}")
            vf = G.OUT / f"verdict_{G.mtag(m)}.json"
            verdicts[m] = json.loads(vf.read_text())["verdict"] if rc == 0 and vf.exists() else "CHECK_FAILED"
            q.status["verdicts"][G.mtag(m)] = verdicts[m]
            q.note(f"margin {m:g}: {verdicts[m]}")
        if verdicts.get(G.FIRST_MARGIN) not in ("PASS", "FAIL_horizon", "FAIL_relief"):
            q.note("bisection stopped: the first margin has no PASS/FAIL verdict; owner decides")
        # 3. fresh window: its own m = 0 cut, then each margin that passed
        tag = G.fresh_tag()
        passed = [m for m, v in verdicts.items() if v == "PASS"]
        if passed:
            q.run(G.run_name(tag, 0.0), G.run_name(tag, passed[0]))
            for m in passed:
                q.run(G.run_name(tag, m), "confirm")
                q.tool("confirm", f"{m:g}")
        else:
            q.note("no margin passed: the fresh window is not run")
        q.tool("report")
        q.save(running=None, next=None, queue_done_utc=now())
        q.note("queue done")
        return 0
    finally:
        keep_awake(False)


if __name__ == "__main__":
    sys.exit(main())
