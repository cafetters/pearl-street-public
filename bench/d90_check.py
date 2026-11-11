"""D90's infeasible_declare verdict for one run folder (docs/plans/d90_check_plan.md, approved by the owner Nov 7).

    python -m bench.d90_check <attack_run_dir> [--out file.json] [--data-dir DIR] [--code-root DIR]

D90 (Decided, Nov 6): an infeasible_declare cell is judged only where the filed set is infeasible for 1p when filed;
then D82's `infeasible_declaration` must fire. Otherwise the cell is not exercised.

Everything is read from the run folder (the trace header's config, roster and attackers; the exchange trace), plus
one data load: the run's own slice through sim.data.load_slice. exchange/ reads no data; this is a bench tool.

Per header attacker of type infeasible_declare:
  1. its filings in trace order (declare_outage, declare_availability), its register and asset_limits;
  2. replay in filing order: re-solve 1p's selfish plan (sim.reference.selfish_plan, the metering service's function)
     with the first filing, then the first two. Limits are built as runs/nov06/infeasible/d82_recheck.py builds them:
     an outage sets power 0 over its window; an availability sets that local day's power cap and SOC floor;
  3. infeasible_at_filing: the full filed set has no plan; made_infeasible_by: the filing that made it so;
  4. the in-run D82 result: a `reject` to the attacker with payload code `infeasible_declaration`, at filing (its
     ref_msg_id is a filing and it went out at that filing's time) or later (a D82 drop);
  5. verdict: NOT_EXERCISED (feasible at filing, D82 silent), PASS (infeasible, D82 fired), FAIL (infeasible, D82
     silent), DISAGREE (D82 fired but the replay finds the set feasible: fails closed in the judge).
The run's verdict: DISAGREE if any attacker disagrees, else FAIL if any fails, else PASS if any passes, else
NOT_EXERCISED.

Fails closed (exit 2, verdict REFUSED): no infeasible_declare attacker in the header; filings other than one outage
plus one availability; an attacker missing from the header roster; a dirty or missing commit in the header; a data
window that isn't the trace's clock (telemetry steps); the code's commit differs from the header's (commit guard).

Two options beyond the plan (both needed to run the plan's real-trace checks; disclosed in the Nov 8 report):
  --data-dir   the data folder, when the header's (e.g. a Maritime sandbox's /home/user/data) isn't on this machine;
  --code-root  a clean checkout of the run's commit whose sim/ and agents/ are imported for the replay (default: this
               checkout). The commit guard compares that checkout's HEAD with the header's commit.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ATTACK_TYPE = "infeasible_declare"
CODE = "infeasible_declaration"
FILING_TYPES = ("declare_outage", "declare_availability")


class Refused(Exception):
    """The check can't give a verdict on this run folder (fails closed)."""


def read_run(run_dir) -> tuple[dict, list[dict]]:
    run_dir = Path(run_dir)
    paths = sorted(run_dir.glob("exchange_h*/trace.jsonl"))
    if len(paths) != 1:
        raise Refused(f"{run_dir}: expected one exchange trace, found {len(paths)}")
    lines = paths[0].read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    log = [json.loads(x) for x in lines[1:]]
    log = [{**m, "sender": m.get("sender", m.get("from"))} for m in log if "msg_type" in m]      # trace: "from"
    return header, log


def filings(log, aid) -> list[dict]:
    return [m for m in log if m["msg_type"] in FILING_TYPES and m["sender"] == aid]


def d82_rejects(log, aid) -> list[dict]:
    return [m for m in log if m["msg_type"] == "reject" and m.get("to") == aid
            and (m.get("payload") or {}).get("code") == CODE]


def limits(fs, iso, loc):
    """(msg_id, power cap, SOC floor) after each filing in turn, NaN where none (as d82_recheck.py builds them)."""
    import numpy as np
    T = len(iso)
    pmax, floor = np.full(T, np.nan), np.full(T, np.nan)
    out = []
    for m in fs:
        p = m["payload"]
        if m["msg_type"] == "declare_outage":
            w = p["window"]
            for k, t in enumerate(iso):
                if w["start_utc"] <= t < w["end_utc"]:
                    pmax[k] = 0.0
        else:
            day = [k for k in range(T) if loc[k] == p["date"]]
            pmax[day] = float(p["power_kw"])
            floor[day] = float(p["reserve_kwh"])
        out.append((m.get("msg_id"), pmax.copy(), floor.copy()))
    return out


def _check_header(header, log, iso) -> list[str]:
    specs = [s for s in (header.get("attackers") or []) if s.get("type") == ATTACK_TYPE]
    if not specs:
        raise Refused("no infeasible_declare attacker in the header")
    roster = set(header.get("roster") or [])
    missing = [s["agent"] for s in (header.get("attackers") or []) if s["agent"] not in roster]
    if missing:
        raise Refused(f"attackers missing from the header roster: {missing}")
    c = str(header.get("git_commit") or "")
    if not c or c.endswith("-dirty"):
        raise Refused(f"header commit missing or dirty: {c!r}")
    clock = sorted({m["t_utc"] for m in log if m["msg_type"] == "telemetry"})
    if clock != list(iso):
        raise Refused(f"the data window isn't the trace's clock: {len(iso)} data steps "
                      f"({iso[0] if iso else None} to {iso[-1] if iso else None}) vs {len(clock)} telemetry steps "
                      f"({clock[0] if clock else None} to {clock[-1] if clock else None})")
    return [s["agent"] for s in specs]


def judge(header, log, iso, loc, plan_fn) -> dict:
    """plan_fn(agent, power cap, SOC floor) -> True when 1p has a plan within those limits."""
    out = {}
    for aid in _check_header(header, log, iso):
        fs = filings(log, aid)
        kinds = sorted(m["msg_type"] for m in fs)
        if kinds != sorted(FILING_TYPES):
            raise Refused(f"{aid}: expected one outage plus one availability filing, found {kinds}")
        after = [[mid, bool(plan_fn(aid, p, f))] for mid, p, f in limits(fs, iso, loc)]
        infeasible = not after[-1][1]
        by = next((mid for mid, ok in after if not ok), None)
        rej = d82_rejects(log, aid)
        at = {m.get("msg_id"): m["t_utc"] for m in fs}
        at_filing = [r for r in rej if (r["payload"].get("ref_msg_id") in at
                                         and r["t_utc"] == at[r["payload"]["ref_msg_id"]])]
        fired = bool(rej)
        verdict = (("PASS" if fired else "FAIL") if infeasible else ("DISAGREE" if fired else "NOT_EXERCISED"))
        out[aid] = {"verdict": verdict, "filings": [m.get("msg_id") for m in fs], "feasible_after": after,
                    "infeasible_at_filing": infeasible, "made_infeasible_by": by, "d82_fired": fired,
                    "d82_at_filing": bool(at_filing), "d82_later_drop": len(rej) > len(at_filing),
                    "d82_messages": [r["payload"] for r in rej][:4]}
    vs = [a["verdict"] for a in out.values()]
    verdict = next((v for v in ("DISAGREE", "FAIL", "PASS") if v in vs), "NOT_EXERCISED")
    return {"verdict": verdict, "attackers": out}


def guard_commit(header, code_commit) -> None:
    h = str(header.get("git_commit") or "")
    if not code_commit or code_commit.endswith("-dirty") or code_commit != h:
        raise Refused(f"commit guard: the code's commit {code_commit!r} is not the run's commit {h!r}")


# The dirty check (owner, Nov 11, decision 2): a modified tracked file or an untracked file under any of these marks
# the code root -dirty (git-ignored files don't count), and so does a failing `git status` (fail closed).
DIRTY_PATHS = ("bench", "sim", "agents", "exchange", "attacks")


def code_commit(code_root: Path) -> str | None:
    def git(*a):
        return subprocess.run(["git", "-C", str(code_root), *a], capture_output=True, text=True)
    r = git("rev-parse", "HEAD")
    if r.returncode:
        return None
    d = git("status", "--porcelain", "--untracked-files=all", "--", *DIRTY_PATHS)
    dirty = d.returncode != 0 or bool(d.stdout.strip())
    return r.stdout.strip() + ("-dirty" if dirty else "")


def planner(header, log, data_dir: Path, code_root: Path):
    """The run's slice and 1p's planner, imported from code_root (iso steps, local dates, plan_fn)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    if str(code_root) not in sys.path[:1]:
        sys.path.insert(0, str(code_root))
    import numpy as np
    import sim
    if Path(sim.__file__).resolve().parents[1] != code_root.resolve():
        raise Refused(f"sim imported from {Path(sim.__file__).parent}, not from the code root {code_root}")
    from sim import physics
    from sim.data import load_slice
    from sim.reference import selfish_plan
    cfg = header.get("config") or {}
    for k in ("start", "end", "slice_tz", "wear_usd_mwh"):
        if k not in cfg:
            raise Refused(f"header config has no {k}")
    try:
        step_min = float((header.get("exchange") or {})["step_minutes"])
    except (KeyError, TypeError, ValueError):
        raise Refused("header has no exchange.step_minutes")
    s = load_slice(data_dir, cfg["start"], cfg["end"], cfg["slice_tz"])
    iso = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in s.ts_utc]
    tz = ZoneInfo(cfg["slice_tz"])
    loc = [datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(tz).date().isoformat() for t in iso]
    ids = [str(x) for x in s.ids]
    fleet = physics.Fleet.from_slice(s, np.arange(len(ids)))
    a = s.agent
    T = len(iso)

    def plan_fn(aid, pmax, floor):
        j = ids.index(aid)
        lim = next(m["payload"] for m in log if m["msg_type"] == "asset_limits" and m["payload"]["agent_id"] == aid)
        reg = next(m["payload"] for m in log if m["msg_type"] == "register" and m["payload"]["agent_id"] == aid)
        pv, curt, load = np.nan_to_num(a["pv"][j]), a["curt"][j], a["load"][j]
        args = (float(reg["mandate"]["max_kw"]), float(lim["usable_kwh"]), float(fleet.S0[j]), np.zeros(T),
                pv - curt, pv - curt - load, float(cfg["wear_usd_mwh"]), step_min / 60.0)
        return selfish_plan(*args, pmax, floor) is not None

    return iso, loc, plan_fn


def check_run(run_dir, data_dir=None, code_root=None) -> dict:
    header, log = read_run(run_dir)
    code_root = Path(code_root or ROOT)
    _check_header(header, log, sorted({m["t_utc"] for m in log if m["msg_type"] == "telemetry"}))   # cheap first
    guard_commit(header, code_commit(code_root))
    data_dir = Path(data_dir or (header.get("config") or {}).get("data_dir") or "")
    if not data_dir.is_dir():
        raise Refused(f"data folder {data_dir} not found (give --data-dir)")
    iso, loc, plan_fn = planner(header, log, data_dir, code_root)
    res = judge(header, log, iso, loc, plan_fn)
    return {"run_dir": str(run_dir), "commit": header.get("git_commit"), "code_root": str(code_root), **res}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    opts = {}
    for k in ("--out", "--data-dir", "--code-root"):
        if k in argv:
            i = argv.index(k)
            opts[k], argv = argv[i + 1], argv[:i] + argv[i + 2:]
    if len(argv) != 1:
        print("usage: python -m bench.d90_check <attack_run_dir> [--out f] [--data-dir d] [--code-root d]",
              file=sys.stderr)
        return 2
    try:
        res, rc = check_run(argv[0], opts.get("--data-dir"), opts.get("--code-root")), 0
    except Refused as e:
        res, rc = {"run_dir": argv[0], "verdict": "REFUSED", "reason": str(e)}, 2
    text = json.dumps(res, indent=1, default=str)
    if "--out" in opts:
        Path(opts["--out"]).write_text(text, encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("verdict", "reason") if k in res}))
    return rc


if __name__ == "__main__":
    sys.exit(main())
