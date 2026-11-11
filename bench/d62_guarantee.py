"""
D62 guarantee test (docs/plans/d62_guarantee_report.md, pre-registered): the exchange pays the shortfall of a modeled
m = 0 price response P* against a modeled acting-alone path Q* (sim/reference.py ModeledPaths; exchange/guarantee.py),
quarterly with an annual true-up, to agents whose delivered share of P*'s modeled shift clears a threshold (D65).

P*, Q* and the accruals depend only on 1p's inputs, the settled peak prices and real-time prices, never on what an
agent sends, so they are computed here from saved m = 0 traces. Jan 12-13 and Nov 28-29 are confirmed in-run
(configs/d62, d62_guarantee: true): each must reproduce its m = 0 rerun exactly with the new messages removed and
msg_ids ignored, and its in-run accruals must equal this tool's on the same trace and on the old one.

Gates (all on the 11 windows of the D58 horizon test, 100 MW, 1p, cap 100):
  0. fail-closed checks (roster, stage-1 identity <= $0.01, tool double run, 1p reference vs in-run, accounting);
  1. in-run confirmation (inrun_<w>.json, both MATCH, else stop);
  2. separation: every m = 0 agent-quarter's share > every acting-alone agent-quarter's share (0/0 left out);
     threshold = midpoint;
  3. coverage: each agent's summed result vs acting alone + its D62 year-end net payment >= -max(1% x |alone|, $10);
  4. cost: m = 0 peak payout + D62 gross quarterly payments < the $10 margin's payout on the same windows.
Fresh window Sep 5-6 (after the verdict commit): separation at the threshold, 1p settlement harm 0, coverage.

Commands (one process at a time, from the repo root):
  python -m bench.d62_guarantee cell <run>        -> runs/d62/cells/<run>.json
  python -m bench.d62_guarantee inrun <window>    -> runs/d62/inrun_<window>.json   (jan12 | nov28)
  python -m bench.d62_guarantee verdict           -> runs/d62/verdict.json
  python -m bench.d62_guarantee fresh             -> runs/d62/confirm_sep05.json
  python -m bench.d62_guarantee report            -> runs/d62/report.md
"""
import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

from bench import d56_resettle as D
from bench import d58_margin as DM
from bench.d56_resettle import CheckFailed
from exchange import guarantee as G

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "runs" / "d62"
CELLS = OUT / "cells"
H = 0.25
K = H / 1000.0
WINDOWS = DM.WINDOWS
M0_RUN = DM.M0_RUN                                       # the D58 comparators (baseline MATCH, so the originals)
M10_RUN = {w: DM.run_name(w, 10.0) for w in WINDOWS}
FRESH = "sep05"
FRESH_RUN = "h2h_c_sep05_h100_m0"
QUARTER = {"jan02": 1, "jan12": 1, "jan16": 1, "jan31": 1, "feb12": 1, "jun19": 2, "jul01": 3, "jul14": 3,
           "aug28": 3, "sep21": 3, "nov28": 4, "sep05": 3}    # by window start (runs/d58m/guarantee_cost.py)
YEAR = 2023
INRUN = {"jan12": "h2h_c_jan12_h100_g", "nov28": "h2h_c_nov28_h100_g"}
INRUN_RERUN = {"jan12": "h2h_c_jan12_h100_m0", "nov28": "h2h_c_nov28_h100_m0"}   # same build (6156964), 1p, cap 100
NEW_TYPES = {"modeled_response", "rt_price", "guarantee_accrual"}
# summary fields allowed to differ in the in-run reproduction: D58's volatile list, message counts and the new block
SUMMARY_IGNORE = DM.VOLATILE | {"messages_total", "messages_per_day", "guarantee"}
IDENTITY_TOL_USD = 0.01                  # stage-1 identity, per agent
PATH_TOL_KW = 1e-6                       # in-run vs tool P*, Q*; tool Q vs in-run selfish_reference
ACCRUAL_TOL_USD = 1e-4                   # in-run vs tool accruals (the D56 peak_settle tolerance)
TOL_REL, TOL_ABS = D.TOL_REL, D.TOL_ABS
MARGIN10_PAYOUT_USD = 106_691.44         # the $10 margin's 1p payout over the 11 windows (D58 cells); recomputed
M0_PAYOUT_USD = 42_334.81                # m = 0 1p payout over the 11 windows (D58 comparator cells); recomputed
PAYOUT_TOL_USD = 0.01
LEDGER_TOL_USD = 0.01                    # accounting: agents' D62 payments = the operator's outlay


def tool_sha() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def config_path(run: str) -> Path:
    for d in ("h2h", "d58m", "d62"):
        f = REPO / "configs" / d / f"{run}.yaml"
        if f.exists():
            return f
    raise CheckFailed(f"{run}: no config in configs/h2h, configs/d58m or configs/d62")


# ---------------------------------------------------------------- reading a run
def read_trace(path: Path, pos: dict, T: int, exchange: bool) -> tuple[dict, dict]:
    out = {"net": defaultdict(lambda: np.zeros(T)), "mandate": {}, "calls": {}, "pp": [], "aggs": [],
           "sref": defaultdict(dict), "modeled": defaultdict(dict), "accrual": {}, "rt": None}
    with open(path, encoding="utf-8") as f:
        header = json.loads(next(f))
        for line in f:
            m = json.loads(line)
            p, typ = m["payload"], m["msg_type"]
            if typ == "telemetry":
                out["net"][p["asset_id"].split("/")[0]][pos[m["t_utc"]]] = p.get("net_kw", 0.0)
            elif not exchange:
                continue
            elif typ == "register":
                out["mandate"][p["agent_id"]] = p["mandate"]
            elif typ == "call_nominations" and m["from"] == "exchange" and p.get("binding") and "call_id" in p:
                out["calls"][p["call_id"]] = {"deadline_utc": p["deadline_utc"], "window": p["window"]}
            elif typ == "peak_price":
                out["pp"].append(p)
            elif typ == "nomination_aggregate" and p["binding"]:
                out["aggs"].append((m["t_utc"], p))
            elif typ == "selfish_reference":
                out["sref"][p["call_id"]][p["asset_id"].split("/")[0]] = (pos[p["window"]["start_utc"]], p["net_kw"])
            elif typ == "modeled_response":
                out["modeled"][p["call_id"]][p["asset_id"].split("/")[0]] = p
            elif typ == "guarantee_accrual":
                out["accrual"][m["to"]] = p
            elif typ == "rt_price":
                out["rt"] = p
    return header, out


def load_run(run: str) -> dict:
    """Everything D62 needs from one arm C run: 1p's inputs, the binding calls (deadline, DA version, final price),
    the settled price, metered site net of the run and of its selfish (acting-alone) run."""
    from sim.data import load_config, load_slice
    R = REPO / "runs" / run
    summ = json.loads((R / "summary.json").read_text())
    hs = list(summ["exchange_by_headroom_mw"])
    if len(hs) != 1:
        raise CheckFailed(f"{run}: expected one headroom, got {hs}")
    h = hs[0]
    cfg = load_config(config_path(run))
    if cfg["run_name"] != run:
        raise CheckFailed(f"{run}: config run_name {cfg['run_name']}")
    s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
    return assemble(run, list(summ["header"]["roster"]), cfg, s, R / f"exchange_h{h}_rinfo" / "trace.jsonl",
                    R / "selfish" / "trace.jsonl", summ["exchange_by_headroom_mw"][h]["price_mode"]["log"])


def assemble(run: str, roster: list[str], cfg: dict, s, ex_path: Path, sf_path: Path, log: list[dict]) -> dict:
    """load_run's work once the files are found: the traces, the slice's data and the run's price log."""
    from sim import physics
    from sim.forecasts import PriceForecast
    iso = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in s.ts_utc]
    pos = {t: i for i, t in enumerate(iso)}
    T = len(iso)
    sid = [str(x) for x in s.ids]
    idx = np.array([sid.index(a) for a in roster])
    ex_header, ex = read_trace(ex_path, pos, T, True)
    _, sf = read_trace(sf_path, pos, T, False)
    for what, ids in (("exchange telemetry", list(ex["net"])), ("selfish telemetry", list(sf["net"])),
                      ("register", list(ex["mandate"])), ("exchange header", ex_header.get("roster", []))):
        D.check_roster(roster, ids, f"{run} {what}")
    if (ex_header.get("exchange") or {}).get("settlement_reference", "recorded") == "recorded" and run not in (
            M0_RUN.values()):
        raise CheckFailed(f"{run}: not settled on 1p and not a D58 re-settled comparator")
    prices = PriceForecast(s.ts_utc, s.system["price_da"], cfg["slice_tz"], cfg["price_publish_hour_local"])
    p = np.zeros(T)
    gate_of = np.full(T, -1)
    gates = []
    for _, agg in sorted(ex["aggs"], key=lambda x: x[0]):
        last = [pp for pp in ex["pp"] if pp["call_id"] == agg["call_id"]]
        if not last:
            continue
        pp = last[-1]
        if not pp["final"]:
            raise CheckFailed(f"{run}: {agg['call_id']}'s last price is not final")
        call = ex["calls"][agg["call_id"]]
        a0 = pos[pp["window"]["start_utc"]]
        sl = slice(a0, a0 + len(pp["price_usd_mwh"]))
        p[sl] = pp["price_usd_mwh"]
        gate_of[sl] = len(gates)
        peak = np.zeros(T)
        peak[sl] = pp["price_usd_mwh"]
        rows = [x for x in log if x["call_id"] == agg["call_id"] and not x.get("closing")]
        t_gate = rows[0]["t_utc"]
        version, fc = prices.at(pos[t_gate])
        gates.append({"call_id": agg["call_id"], "deadline": pos[call["deadline_utc"]],
                      "deadline_utc": call["deadline_utc"], "t_gate": t_gate, "version": version, "fc": fc,
                      "peak": peak})
    if [g["deadline"] for g in gates] != sorted(g["deadline"] for g in gates):
        raise CheckFailed(f"{run}: binding calls out of deadline order")
    for gi, g in enumerate(gates):          # a call's span is exactly the steps it settles
        settles = np.flatnonzero(gate_of == gi)
        end = gates[gi + 1]["deadline"] if gi + 1 < len(gates) else T
        if len(settles) and (settles[0] < g["deadline"] or settles[-1] >= end):
            raise CheckFailed(f"{run}: {g['call_id']} settles steps outside its span")
    p = np.where(p > 1e-9, p, 0.0)
    a = s.agent
    fleet = physics.Fleet.from_slice(s, idx)
    pv, curt, load = np.nan_to_num(a["pv"][idx]), a["curt"][idx], a["load"][idx]
    return {"run": run, "roster": roster, "iso": iso, "T": T, "gates": gates, "p": p, "gate_of": gate_of,
            "max_kw": np.array([float(ex["mandate"][x]["max_kw"]) for x in roster]),
            "usable": 0.8 * fleet.CAP, "s0": fleet.S0, "avail": pv - curt, "base": pv - curt - load,
            "wear": float(cfg["wear_usd_mwh"]), "rt": np.asarray(s.system["price_rt"], float),
            "da": np.asarray(s.system["price_da"], float),
            "end_usd_kwh": float(s.system["price_da"].mean()) / 1000.0,
            "N": np.array([ex["net"][x] for x in roster]), "S": np.array([sf["net"][x] for x in roster]),
            "sref": ex["sref"], "modeled": ex["modeled"], "accrual": ex["accrual"], "rt_msg": ex["rt"],
            "header": ex_header}


# ---------------------------------------------------------------- computing one run
def compute(d: dict) -> dict:
    """Per agent: P*, Q*, 1p's Q_g on settled steps, span shortfalls, D65's measure for the run and its selfish run,
    and the stage-1 identity slack."""
    from sim.reference import ModeledPaths, selfish_plan
    gates, T, p = d["gates"], d["T"], d["p"]
    sp = G.spans([g["deadline"] for g in gates], T)
    priced = p > 0
    agents, arrays = [], {}
    for j, aid in enumerate(d["roster"]):
        args = (d["max_kw"][j], d["usable"][j], d["s0"][j])
        plans = []
        for g in gates:
            pl = selfish_plan(*args, g["fc"], d["avail"][j], d["base"][j], d["wear"], H)
            if pl is None:
                raise CheckFailed(f"{d['run']}: 1p planner, no plan for {aid}")
            plans.append(pl)
        q_ref = np.zeros(T)
        for gi, pl in enumerate(plans):
            m = (d["gate_of"] == gi) & priced
            q_ref[m] = (d["base"][j] + pl.d - pl.c - pl.x)[m]
        mp = ModeledPaths(*args, d["avail"][j], d["base"][j], d["wear"], H)
        for gi, g in enumerate(gates):
            mp.stage(g["deadline"], g["fc"], g["peak"], plans[0] if gi == 0 else None)
        P_net, Q_net = mp.net(mp.P), mp.net(mp.Q)
        P_dis, Q_dis = mp.P["d"], mp.Q["d"]
        ps, qs = float(mp.P["s"][-1]), float(mp.Q["s"][-1])
        short = G.span_shortfall(d["rt"], p, q_ref, P_net, P_dis, Q_net, Q_dis, ps, qs, d["wear"], d["end_usd_kwh"],
                                 sp, K)
        short_da = G.span_shortfall(d["da"], p, q_ref, P_net, P_dis, Q_net, Q_dis, ps, qs, d["wear"],
                                    d["end_usd_kwh"], sp, K)
        shift, got = G.response_kwh(p, q_ref, P_net, d["N"][j], H)
        _, got_alone = G.response_kwh(p, q_ref, P_net, d["S"][j], H)
        agents.append({"agent": aid, "shortfall_by_span_usd": short, "shortfall_usd": float(sum(short)),
                       "shortfall_at_final_da_usd": float(sum(short_da)),
                       "reference_mismatch_usd": float((p[priced] * (Q_net - q_ref)[priced]).sum()) * K,
                       "modeled_shift_kwh": shift, "delivered_kwh": got, "delivered_alone_kwh": got_alone,
                       "identity_slack_usd": mp.stage1_identity(gates[0]["peak"])})
        arrays[aid] = {"P_net": P_net, "P_dis": P_dis, "Q_net": Q_net, "Q_dis": Q_dis, "q_ref": q_ref, "Q": plans}
    return {"agents": agents, "arrays": arrays, "spans": sp}


def checks(d: dict, res: dict) -> dict:
    """Gate 0's per-run checks. Fails closed."""
    worst = max(a["identity_slack_usd"] for a in res["agents"])
    if worst > IDENTITY_TOL_USD:
        bad = [a["agent"] for a in res["agents"] if a["identity_slack_usd"] > IDENTITY_TOL_USD]
        raise CheckFailed(f"{d['run']}: stage-1 identity above ${IDENTITY_TOL_USD} for {bad[:5]} (worst {worst:.6f})")
    sref_gap = None
    if d["sref"]:            # in-run 1p: the tool's Q_g must equal the selfish_reference sent in-run
        sref_gap = 0.0
        for gi, g in enumerate(d["gates"]):
            got = d["sref"].get(g["call_id"], {})
            D.check_roster(d["roster"], list(got), f"{d['run']} selfish_reference {g['call_id']}")
            steps = np.flatnonzero((d["gate_of"] == gi) & (d["p"] > 0))
            for aid in d["roster"]:
                a0, kw = got[aid]
                pl = res["arrays"][aid]["Q"][gi]
                j = d["roster"].index(aid)
                q = d["base"][j] + pl.d - pl.c - pl.x
                sref_gap = max(sref_gap, float(np.abs(np.asarray(kw, float)[steps - a0] - q[steps]).max(initial=0.0)))
        if sref_gap > PATH_TOL_KW:
            raise CheckFailed(f"{d['run']}: tool 1p reference vs in-run selfish_reference {sref_gap} kW")
    return {"identity_worst_slack_usd": worst, "q_vs_in_run_reference_kw": sref_gap, "roster_complete": True}


def strip(res: dict) -> dict:
    return {"agents": res["agents"], "spans": [list(x) for x in res["spans"]]}


def cell(run: str) -> dict:
    """One run's D62 result, computed twice (determinism) with the checks; fails closed."""
    from bench.runinfo import run_header
    d = load_run(run)
    r1, r2 = compute(d), compute(d)
    if json.dumps(strip(r1), default=float) != json.dumps(strip(r2), default=float):
        raise CheckFailed(f"{run}: tool double run differs")
    ck = checks(d, r1)
    head = {**run_header("bench/d62_guarantee.py", {"run": run}), "tool_sha256_16": tool_sha(),
            "roster": d["roster"], "run_git_commit": d["header"].get("git_commit")}
    return {"header": head, "run": run, "calls": [{k: g[k] for k in ("call_id", "deadline_utc", "t_gate", "version")}
                                                  for g in d["gates"]],
            "checks": {**ck, "double_run_identical": True}, **strip(r1)}


def cell_cmd(run: str, out: Path) -> int:
    (out / "cells").mkdir(parents=True, exist_ok=True)
    try:
        res = cell(run)
    except CheckFailed as err:
        (out / "cells" / f"{run}.failed.json").write_text(json.dumps({"failed": str(err)}, indent=1))
        print(f"{run}: CHECK FAILED: {err}", flush=True)
        return 2
    (out / "cells" / f"{run}.json").write_text(json.dumps(res, indent=1, default=float))
    print(f"{run}: ok, worst identity slack ${res['checks']['identity_worst_slack_usd']:.6f}", flush=True)
    return 0


def load_cell(run: str, out: Path = OUT) -> dict:
    f = out / "cells" / f"{run}.json"
    if not f.exists():
        raise CheckFailed(f"{run}: no D62 cell result (failed or not run)")
    c = json.loads(f.read_text())
    D.check_roster(c["header"]["roster"], [a["agent"] for a in c["agents"]], f"{run} D62 cell")
    return c


# ---------------------------------------------------------------- in-run confirmation
def json_diff(a, b, ignore: set, names=("", ""), path="") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            if k in ignore:
                continue
            if k not in a or k not in b:
                out.append(f"{path}/{k} missing")
            else:
                out += json_diff(a[k], b[k], ignore, names, f"{path}/{k}")
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path} length {len(a)} vs {len(b)}"]
        return [x for i, (u, v) in enumerate(zip(a, b)) for x in json_diff(u, v, ignore, names, f"{path}[{i}]")]
    if isinstance(a, str) and isinstance(b, str):
        return [] if a.replace(names[0], names[1]) == b else [path]
    if isinstance(a, float) and isinstance(b, float) and a != a and b != b:
        return []
    return [] if a == b else [path]


def trace_events(path: Path) -> list[dict]:
    """A trace's messages without the header, without D62's three message types, and without msg_ids."""
    out = []
    with open(path, encoding="utf-8") as f:
        next(f)
        for line in f:
            m = json.loads(line)
            if m["msg_type"] in NEW_TYPES:
                continue
            m.pop("msg_id", None)
            out.append(m)
    return out


def reproduce(new: str, old: str) -> dict:
    """The new in-run trace vs. the old one: every message but D62's, ids ignored, must match exactly (run names
    swapped), in both the exchange and the selfish trace; the summaries must match outside SUMMARY_IGNORE."""
    R1, R0 = REPO / "runs" / new, REPO / "runs" / old
    diffs = {}
    for part in ("exchange_h100_rinfo", "selfish"):
        a, b = trace_events(R1 / part / "trace.jsonl"), trace_events(R0 / part / "trace.jsonl")
        diffs[part] = json_diff(a, b, set(), (new, old))[:50]
    s1, s0 = (json.loads((R / "summary.json").read_text()) for R in (R1, R0))
    diffs["summary"] = json_diff(s1, s0, SUMMARY_IGNORE, (new, old))[:50]
    return {"new": new, "old": old, "match": not any(diffs.values()), "differences": diffs,
            "ignored_summary_fields": sorted(SUMMARY_IGNORE), "ignored_message_types": sorted(NEW_TYPES)}


def inrun_vs_tool(d: dict, res: dict) -> dict:
    """In-run modeled_response stages and guarantee_accrual vs. this tool on the same trace. Each stage is compared
    over its own span [d_g, d_{g+1}) only: its steps after the next deadline are re-planned by the next stage, so the
    final P* and Q* hold that stage's values there (pre-registration v2; v1 compared the whole tail)."""
    worst_kw, worst_usd = 0.0, 0.0
    sp = res["spans"]
    for g, (a0, a1) in zip(d["gates"], sp):
        got = d["modeled"].get(g["call_id"], {})
        D.check_roster(d["roster"], list(got), f"{d['run']} modeled_response {g['call_id']}")
        for aid in d["roster"]:
            m, arr = got[aid], res["arrays"][aid]
            for key, ref in (("p_net_kw", arr["P_net"]), ("p_discharge_kw", arr["P_dis"]),
                             ("q_net_kw", arr["Q_net"]), ("q_discharge_kw", arr["Q_dis"])):
                mine = np.asarray(m[key], float)
                if len(mine) != len(ref) - a0:
                    raise CheckFailed(f"{d['run']}: {key} for {aid} on {g['call_id']} has {len(mine)} steps")
                worst_kw = max(worst_kw, float(np.abs(mine[:a1 - a0] - ref[a0:a1]).max()))
    D.check_roster(d["roster"], list(d["accrual"]), f"{d['run']} guarantee_accrual")
    worst_kwh = 0.0
    for a in res["agents"]:
        acc = d["accrual"][a["agent"]]
        worst_usd = max(worst_usd, abs(acc["shortfall_usd"] - a["shortfall_usd"]),
                        *(abs(x - y) for x, y in zip(acc["shortfall_usd_by_span"], a["shortfall_by_span_usd"])))
        worst_kwh = max(worst_kwh, abs(acc["modeled_shift_kwh"] - a["modeled_shift_kwh"]),
                        abs(acc["delivered_kwh"] - a["delivered_kwh"]))
    return {"paths_max_abs_kw": worst_kw, "accrual_max_abs_usd": worst_usd, "response_max_abs_kwh": worst_kwh,
            "match": bool(worst_kw <= PATH_TOL_KW and worst_usd <= ACCRUAL_TOL_USD and worst_kwh <= PATH_TOL_KW)}


def tool_vs_tool(a: dict, b: dict) -> dict:
    """The tool on the new in-run trace vs. the tool on an old trace of the same window (behaviour must be equal)."""
    D.check_roster([x["agent"] for x in a["agents"]], [x["agent"] for x in b["agents"]], "tool vs tool")
    rb = {x["agent"]: x for x in b["agents"]}
    worst = 0.0
    for x in a["agents"]:
        y = rb[x["agent"]]
        for k in ("shortfall_usd", "modeled_shift_kwh", "delivered_kwh", "delivered_alone_kwh"):
            worst = max(worst, abs(x[k] - y[k]))
    return {"max_abs": worst, "match": bool(worst <= ACCRUAL_TOL_USD)}


def inrun_cmd(w: str, out: Path) -> int:
    new = INRUN[w]
    res = {"window": w, "run": new, "rerun": INRUN_RERUN[w], "original": M0_RUN[w]}
    try:
        res["reproduction"] = reproduce(new, INRUN_RERUN[w])
        d = load_run(new)
        if not d["header"].get("d62_guarantee"):
            raise CheckFailed(f"{new}: header has no d62_guarantee")
        summ = json.loads((REPO / "runs" / new / "summary.json").read_text())
        total = summ["exchange_by_headroom_mw"]["100"]["guarantee"]["shortfall_usd_total"]
        acc = sum(a["shortfall_usd"] for a in d["accrual"].values())
        if abs(total - acc) > 0.01:       # accounting: the summary's guarantee total is the agents' accruals
            raise CheckFailed(f"{new}: summary guarantee total {total:.4f} vs accruals {acc:.4f}")
        r = compute(d)
        checks(d, r)
        res["inrun_vs_tool"] = inrun_vs_tool(d, r)
        mine = strip(r)
        res["tool_vs_rerun"] = tool_vs_tool(mine, strip(compute(load_run(INRUN_RERUN[w]))))
        res["tool_vs_original"] = tool_vs_tool(mine, strip(compute(load_run(M0_RUN[w]))))
    except CheckFailed as err:
        res["failed"] = str(err)
    res["match"] = bool("failed" not in res and res["reproduction"]["match"] and res["inrun_vs_tool"]["match"]
                        and res["tool_vs_rerun"]["match"] and res["tool_vs_original"]["match"])
    # a failed check is a wiring fault (stop, owner decides), a mismatch is behaviour drift (stop, go all in-run)
    res["status"] = "MATCH" if res["match"] else ("FAIL_check" if "failed" in res else "MISMATCH")
    out.mkdir(parents=True, exist_ok=True)
    (out / f"inrun_{w}.json").write_text(json.dumps(res, indent=1, default=float))
    print(f"{w}: {res['status']}" + {"MATCH": "", "FAIL_check": " (stop: a check failed; the owner decides)",
                                     "MISMATCH": " (stop: report and go all in-run)"}[res["status"]], flush=True)
    return 0 if res["match"] else 1


# ---------------------------------------------------------------- the verdict
def quarter_rows(cells: dict) -> dict:
    """agent -> quarter -> summed shortfall, modeled shift and delivered kWh (run and acting alone)."""
    rows = defaultdict(lambda: defaultdict(lambda: {"shortfall_usd": 0.0, "shift_kwh": 0.0, "delivered_kwh": 0.0,
                                                     "delivered_alone_kwh": 0.0, "windows": []}))
    for w, c in cells.items():
        for a in c["agents"]:
            r = rows[a["agent"]][QUARTER[w]]
            r["shortfall_usd"] += a["shortfall_usd"]
            r["shift_kwh"] += a["modeled_shift_kwh"]
            r["delivered_kwh"] += a["delivered_kwh"]
            r["delivered_alone_kwh"] += a["delivered_alone_kwh"]
            r["windows"].append(w)
    return rows


def shares(rows: dict, key: str) -> dict:
    return {(a, q): G.share(r["shift_kwh"], r[key]) for a, qs in rows.items() for q, r in qs.items()}


def ledgers(rows: dict, threshold: float) -> dict:
    out = {}
    for a, qs in rows.items():
        out[a] = G.ledger([(YEAR, q, r["shortfall_usd"], G.eligible(G.share(r["shift_kwh"], r["delivered_kwh"]),
                                                                     threshold)) for q, r in qs.items()])
    return out


def fleet_ledger(led: dict) -> dict:
    by_q = defaultdict(lambda: {"paid_usd": 0.0, "clawed_back_usd": 0.0, "outstanding_usd": 0.0, "agents_owed": 0,
                                "ineligible_agents": 0})
    for a, l in led.items():
        for r in l["by_quarter"]:
            b = by_q[r["quarter"]]
            b["paid_usd"] += r["paid_usd"]
            b["clawed_back_usd"] += r["clawed_back_usd"]
            b["outstanding_usd"] += r["balance_usd"]
            b["agents_owed"] += r["balance_usd"] > 0
            b["ineligible_agents"] += not r["eligible"]
    gross = sum(l["gross_paid_usd"] for l in led.values())
    clawed = sum(l["clawed_back_usd"] for l in led.values())
    return {"gross_paid_usd": gross, "clawed_back_usd": clawed, "net_usd": gross - clawed,
            "max_outstanding_usd": max((b["outstanding_usd"] for b in by_q.values()), default=0.0),
            "by_quarter": {q: by_q[q] for q in sorted(by_q)}}


def check_ledgers(led: dict, fleet: dict) -> dict:
    """Gate 0 accounting: each agent's paid minus clawed back equals its final balance, and the operator's outlay
    (fleet gross, clawed back, net) equals the sum over agents. Fails closed beyond LEDGER_TOL_USD."""
    worst = 0.0
    for a, l in led.items():
        final = l["by_quarter"][-1]["balance_usd"] if l["by_quarter"] else 0.0
        worst = max(worst, abs(l["gross_paid_usd"] - l["clawed_back_usd"] - final))
    for k in ("gross_paid_usd", "clawed_back_usd", "net_usd"):
        worst = max(worst, abs(fleet[k] - sum(l[k] for l in led.values())))
    if worst > LEDGER_TOL_USD:
        raise CheckFailed(f"D62 ledger accounting off by ${worst:.4f}")
    return {"max_abs_usd": worst, "pass": True}


def m0_cells() -> tuple[dict, dict]:
    """The 11 D58 comparator cells (gains vs. acting alone, payout) and their D62 cells."""
    d56 = {w: DM.comparator_cell(w)[0] for w in WINDOWS}
    d62 = {w: load_cell(M0_RUN[w]) for w in WINDOWS}
    return d56, d62


def payout(cell56: dict) -> float:
    return float(D.cell_metrics(cell56, "1p")["payout_usd"])


def judge() -> dict:
    """Gates 0-4 on the 11 windows. Gate 1 (in-run) must already be MATCH for both windows."""
    res = {"tool_sha256_16": tool_sha(), "windows": WINDOWS}
    inrun = {}
    for w in INRUN:
        f = OUT / f"inrun_{w}.json"
        inrun[w] = json.loads(f.read_text())["match"] if f.exists() else None
    res["gate1_inrun"] = inrun
    if not all(inrun.values()):
        return {**res, "verdict": "STOPPED_inrun", "note": "gate 1 not MATCH: offline numbers are not used"}
    d56, d62 = m0_cells()
    roster = list(d62[WINDOWS[0]]["header"]["roster"])
    for w in WINDOWS:
        D.check_roster(roster, d62[w]["header"]["roster"], f"{w} D62 cell")
        D.check_roster(roster, list(DM.agent_results(d56[w])), f"{w} D56 cell")
    res["gate0"] = {w: d62[w]["checks"] for w in WINDOWS}
    settle = {w: D.cell_metrics(d56[w], "1p")["settlement_harmed"] for w in WINDOWS}
    res["gate0_settlement_harm_agents"] = settle
    if any(settle.values()):
        return {**res, "verdict": "FAIL_check", "note": "1p settlement-caused harm in a window"}
    rows = quarter_rows(d62)
    sep = G.separation(shares(rows, "delivered_kwh"), shares(rows, "delivered_alone_kwh"))
    res["gate2_separation"] = sep
    if not sep["pass"]:
        return {**res, "verdict": "FAIL_separation", "threshold": None}
    thr = sep["threshold"]
    led = ledgers(rows, thr)
    res["ledger"] = fleet_ledger(led)
    res["gate0_accounting"] = check_ledgers(led, res["ledger"])
    # gate 3: coverage
    gain = defaultdict(float)
    alone = defaultdict(float)
    for w in WINDOWS:
        for a in d56[w]["agents"]:
            alone[a["agent"]] += a["alone"]
        for a, g in DM.agent_results(d56[w]).items():
            gain[a] += g
    cov = []
    for a in roster:
        tol = max(TOL_REL * abs(alone[a]), TOL_ABS)
        total = gain[a] + led[a]["net_usd"]
        cov.append({"agent": a, "result_usd": gain[a], "d62_net_usd": led[a]["net_usd"], "total_usd": total,
                    "tol_usd": tol, "pass": bool(total >= -tol - D.EPS_USD)})
    res["gate3_coverage"] = {"pass": all(c["pass"] for c in cov), "failing": [c for c in cov if not c["pass"]],
                             "agents": cov}
    # gate 4: cost
    pay0 = sum(payout(d56[w]) for w in WINDOWS)
    pay10 = sum(payout(DM.load(M10_RUN[w])) for w in WINDOWS)
    if abs(pay0 - M0_PAYOUT_USD) > PAYOUT_TOL_USD or abs(pay10 - MARGIN10_PAYOUT_USD) > PAYOUT_TOL_USD:
        raise CheckFailed(f"payouts {pay0:.2f} / {pay10:.2f} differ from the pre-registered {M0_PAYOUT_USD} / "
                          f"{MARGIN10_PAYOUT_USD}")
    gross = res["ledger"]["gross_paid_usd"]
    res["gate4_cost"] = {"m0_peak_payout_usd": pay0, "d62_gross_usd": gross, "total_usd": pay0 + gross,
                         "margin10_payout_usd": pay10, "pass": bool(pay0 + gross < pay10),
                         "d62_net_usd": res["ledger"]["net_usd"], "clawed_back_usd": res["ledger"]["clawed_back_usd"],
                         "max_outstanding_usd": res["ledger"]["max_outstanding_usd"],
                         "stand_in_estimate_net_usd": 1703.93}
    gates = [res["gate3_coverage"]["pass"], res["gate4_cost"]["pass"]]
    res["threshold"] = thr
    res["verdict"] = "PASS" if all(gates) else ("FAIL_coverage" if not gates[0] else "FAIL_cost")
    res["report_only"] = report_only(d56, d62, roster, thr)
    return res


def report_only(d56: dict, d62: dict, roster: list, thr: float) -> dict:
    out = {"scope": "separation and the threshold are validated on our reference agents only; our m = 0 agents plan "
                    "like P (D63's known gap), so the threshold is not yet evidence about outside agents",
           "true_up_over_longer_runs": "open item: answered by D61's month experiment with D66's warm-up"}
    comp = defaultdict(lambda: {"shortfall_usd": 0.0, "at_final_da_usd": 0.0, "reference_mismatch_usd": 0.0})
    for w in WINDOWS:
        for a in d62[w]["agents"]:
            c = comp[a["agent"]]
            c["shortfall_usd"] += a["shortfall_usd"]
            c["at_final_da_usd"] += a["shortfall_at_final_da_usd"]
            c["reference_mismatch_usd"] += a["reference_mismatch_usd"]
    out["components_by_agent"] = dict(comp)
    out["windows"] = {w: {"shortfall_usd": sum(a["shortfall_usd"] for a in d62[w]["agents"]),
                          "positive_shortfall_usd": sum(max(a["shortfall_usd"], 0.0) for a in d62[w]["agents"]),
                          "peak_payout_usd": payout(d56[w])} for w in WINDOWS}
    try:                    # $10-posture shares on the m10 traces (their own P*)
        m10 = {w: load_cell(M10_RUN[w]) for w in WINDOWS}
        sh = shares(quarter_rows(m10), "delivered_kwh")
        vals = [v for v in sh.values() if v is not None]
        out["m10_shares"] = {"counted": len(vals), "excluded_0_0": len(sh) - len(vals),
                             "min": min(vals) if vals else None, "below_threshold": sum(v < thr for v in vals)}
    except CheckFailed as err:
        out["m10_shares"] = {"missing": str(err)}
    no_nov = {w: d62[w] for w in WINDOWS if w != "nov28"}
    led = ledgers(quarter_rows(no_nov), thr)
    out["without_nov28"] = fleet_ledger(led)
    return out


def verdict_cmd(out: Path) -> int:
    try:
        res = judge()
    except CheckFailed as err:
        res = {"verdict": "FAIL_check", "failed": str(err)}
    out.mkdir(parents=True, exist_ok=True)
    (out / "verdict.json").write_text(json.dumps(res, indent=1, default=float))
    print(f"D62 verdict: {res['verdict']}", flush=True)
    return 0


def fresh_cmd(out: Path) -> int:
    """Gate 5 on Sep 5-6, after the verdict commit: separation at the threshold, 1p settlement harm 0, coverage."""
    v = json.loads((out / "verdict.json").read_text())
    if v.get("threshold") is None:
        raise CheckFailed("no threshold: the fresh window is not run")
    thr = v["threshold"]
    c56, c62 = DM.load(FRESH_RUN), load_cell(FRESH_RUN)
    sh = {a["agent"]: G.share(a["modeled_shift_kwh"], a["delivered_kwh"]) for a in c62["agents"]}
    sh_alone = {a["agent"]: G.share(a["modeled_shift_kwh"], a["delivered_alone_kwh"]) for a in c62["agents"]}
    resp = [v_ for v_ in sh.values() if v_ is not None]
    non = [v_ for v_ in sh_alone.values() if v_ is not None]
    sep_ok = all(x >= thr for x in resp) and all(x < thr for x in non)
    settle = D.cell_metrics(c56, "1p")["settlement_harmed"]
    gain = DM.agent_results(c56)
    alone = {a["agent"]: a["alone"] for a in c56["agents"]}
    cov = []
    for a in c62["agents"]:
        pay = max(a["shortfall_usd"], 0.0) if G.eligible(sh[a["agent"]], thr) else 0.0
        tol = max(TOL_REL * abs(alone[a["agent"]]), TOL_ABS)
        total = gain[a["agent"]] + pay
        cov.append({"agent": a["agent"], "result_usd": gain[a["agent"]], "d62_payment_usd": pay, "total_usd": total,
                    "tol_usd": tol, "pass": bool(total >= -tol - D.EPS_USD)})
    res = {"window": FRESH_RUN, "threshold": thr,
           "separation": {"pass": sep_ok, "responders_counted": len(resp), "non_responders_counted": len(non),
                          "excluded_0_0": {"responders": len(sh) - len(resp), "non_responders": len(sh_alone) - len(non)},
                          "lowest_responder": min(resp, default=None), "highest_non_responder": max(non, default=None)},
           "settlement_harm_agents": settle, "coverage": {"pass": all(c["pass"] for c in cov),
                                                          "failing": [c for c in cov if not c["pass"]]},
           "checks": c62["checks"]}
    res["confirm"] = "PASS" if (sep_ok and settle == 0 and res["coverage"]["pass"]) else "CONFIRM_FAIL"
    (out / "confirm_sep05.json").write_text(json.dumps(res, indent=1, default=float))
    print(f"Sep 5-6: {res['confirm']}", flush=True)
    return 0


def report_cmd(out: Path) -> int:
    v = json.loads((out / "verdict.json").read_text())
    lines = [f"# D62 guarantee test: {v['verdict']}", ""]
    if "gate4_cost" in v:
        c = v["gate4_cost"]
        lines += [f"- Threshold: {v['threshold']:.4f}",
                  f"- Coverage: {'PASS' if v['gate3_coverage']['pass'] else 'FAIL'} "
                  f"({len(v['gate3_coverage']['failing'])} agents failing)",
                  f"- Cost: m = 0 payout ${c['m0_peak_payout_usd']:,.0f} + D62 gross ${c['d62_gross_usd']:,.0f} = "
                  f"${c['total_usd']:,.0f} vs. $10 margin ${c['margin10_payout_usd']:,.0f}",
                  f"- D62 net ${c['d62_net_usd']:,.0f}, clawed back ${c['clawed_back_usd']:,.0f}, max outstanding "
                  f"${c['max_outstanding_usd']:,.0f} (stand-in estimate: net ${c['stand_in_estimate_net_usd']:,.0f})"]
    f = out / "confirm_sep05.json"
    if f.exists():
        lines.append(f"- Sep 5-6: {json.loads(f.read_text())['confirm']}")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["cell", "inrun", "verdict", "fresh", "report"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args(argv)
    out = Path(a.out)
    if a.cmd == "cell":
        return cell_cmd(a.arg, out)
    if a.cmd == "inrun":
        return inrun_cmd(a.arg, out)
    if a.cmd == "verdict":
        return verdict_cmd(out)
    if a.cmd == "fresh":
        return fresh_cmd(out)
    return report_cmd(out)


if __name__ == "__main__":
    sys.exit(main())
