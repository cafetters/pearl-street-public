"""
Overnight Nov 1 (report only, decides nothing; pre-registered in docs/overnight/2026-11-01.md).

Item 1: arm C at a $5/MWh margin on 23 windows at 100 MW (the 11 D58 windows, then the 12 overnight Oct 31 windows),
1p settled in-run, D54 cap 100. Configs are copies of each window's $10 config with only run_name and the margin
changed. Report: the D58 horizon check (bench.d58_horizon.horizon_check, unchanged) across all 23 windows at m = 0,
$5 and $10, the 1p settlement gate, and operator payout, with share of the M0 ceiling and share of the
selfish-to-ceiling gap next to every row.
Item 3: the overnight windows at 150 MW, feasible cells only, m = 0 and $10 (configs: the 100 MW configs with only
run_name and headroom_mw changed). Report per cell. D62 fields are never read (sealed on the overnight windows).

Commands (one process at a time, from the repo root):
  python -m bench.nov01 configs        -> configs/nov01/*.yaml
  python -m bench.nov01 feas           -> runs/nov01/feas_m5.json, runs/nov01/feas_h150.json
  python -m bench.nov01 cell <run>     -> runs/nov01/cells/<run>.json  (bench.d56_resettle on the run)
  python -m bench.nov01 report         -> runs/nov01/report.json, runs/nov01/report.md
"""
import json
import re
import sys
from pathlib import Path

import numpy as np

from bench import d56_resettle as D
from bench import d58_horizon as H
from bench import d58_margin as DM
from bench.d56_resettle import CheckFailed
from sim.data import load_config

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "runs" / "nov01"
CELLS = OUT / "cells"
CONFIGS = REPO / "configs" / "nov01"
OVN = REPO / "runs" / "overnight"
D58_WINDOWS = list(DM.WINDOWS)                       # run order of the D58 margin test
OVN_WINDOWS = ["jan06", "jun29", "jul05", "nov14", "may29", "feb02", "sep18", "aug26", "oct04", "dec07", "mar08",
               "apr16"]                              # the overnight queue's run order
FEAS_M5 = OUT / "feas_m5.json"
FEAS_150 = OUT / "feas_h150.json"
CAP = 100.0


def mtag(m: float) -> str:
    return f"m{m:g}"


def run_name(w: str, m: float, h: int = 100) -> str:
    return f"h2h_c_{w}_h{h}_{mtag(m)}"


def src_config(w: str, m: float) -> Path:
    """The existing config a new one is copied from: D58 windows in configs/d58m, overnight in runs/overnight."""
    if w in D58_WINDOWS:
        return REPO / "configs" / "d58m" / f"{run_name(w, m)}.yaml"
    return OVN / "configs" / f"{run_name(w, m)}.yaml"


def copy_config(src: Path, dst: Path, note: str, **changes):
    """Copy src line by line, replacing only the top-level keys in `changes` (each must exist exactly once)."""
    lines = src.read_text(encoding="utf-8").splitlines()
    out, seen = [f"# Overnight Nov 1 (docs/overnight/2026-11-01.md): {note}"], set()
    for line in lines:
        k = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", line)
        if k and k.group(1) in changes:
            if k.group(1) in seen:
                raise ValueError(f"{src}: {k.group(1)} twice")
            seen.add(k.group(1))
            v = changes[k.group(1)]
            out.append(f"{k.group(1)}: {v}")
        else:
            out.append(line)
    if seen != set(changes):
        raise ValueError(f"{src}: keys not found {set(changes) - seen}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(out) + "\n", encoding="utf-8")
    a, b = load_config(str(src)), load_config(str(dst))
    diff = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
    if diff != set(changes):
        raise ValueError(f"{dst}: differs from {src} in {diff}")


def configs_cmd() -> int:
    for w in D58_WINDOWS + OVN_WINDOWS:
        src = src_config(w, 10.0)
        copy_config(src, CONFIGS / f"{run_name(w, 5.0)}.yaml", f"copy of {src.relative_to(REPO).as_posix()} with "
                    "only run_name and peak_response_margin_usd_mwh changed",
                    run_name=run_name(w, 5.0), peak_response_margin_usd_mwh=5.0)
    for w in OVN_WINDOWS:
        for m in (0.0, 10.0):
            src = src_config(w, m)
            copy_config(src, CONFIGS / f"{run_name(w, m, 150)}.yaml", f"copy of {src.relative_to(REPO).as_posix()} "
                        "with only run_name and headroom_mw changed", run_name=run_name(w, m, 150),
                        headroom_mw="[150]")
    print(f"configs written to {CONFIGS.relative_to(REPO)}")
    return 0


def feas_cmd() -> int:
    from bench import h2h_feasibility as F
    rel = lambda p: str(p.relative_to(REPO)).replace("\\", "/")
    F.main([rel(CONFIGS / f"{run_name(w, 5.0)}.yaml") for w in D58_WINDOWS + OVN_WINDOWS], FEAS_M5)
    F.main([rel(CONFIGS / f"{run_name(w, m, 150)}.yaml") for w in OVN_WINDOWS for m in (0.0, 10.0)], FEAS_150)
    return 0


def feasible_150() -> list[str]:
    """Item 3's run list in queue order: feasible 150 MW cells only."""
    cells = json.loads(FEAS_150.read_text())["cells"]
    return [run_name(w, m, 150) for w in OVN_WINDOWS for m in (0.0, 10.0) if cells[run_name(w, m, 150)]["feasible"]]


def cfg_path(run: str) -> Path:
    p = CONFIGS / f"{run}.yaml"
    if p.exists():
        return p
    for d in (REPO / "configs" / "d58m", OVN / "configs", REPO / "configs" / "h2h"):
        if (d / f"{run}.yaml").exists():
            return d / f"{run}.yaml"
    raise CheckFailed(f"{run}: no config")


def cell_cmd(run: str) -> int:
    from bench.runinfo import run_header
    CELLS.mkdir(parents=True, exist_ok=True)
    head = {**run_header("bench/nov01.py", {"run": run}), "tool_sha256_16": D.tool_sha()}
    files = D.FEASIBILITY_FILES
    extra = [DM.FEAS, FEAS_M5, FEAS_150] + sorted((OVN / "feas").glob("*.json"))
    try:
        D.FEASIBILITY_FILES = [*files, *[f for f in extra if f.exists()]]
        meta, arr = D.load_cell(run, cfg_path=cfg_path(run))
        res = D.evaluate(meta, arr)
    except CheckFailed as err:
        (CELLS / f"{run}.failed.json").write_text(json.dumps({"header": head, "failed": str(err)}, indent=1))
        print(f"{run}: CHECK FAILED: {err}", flush=True)
        return 2
    finally:
        D.FEASIBILITY_FILES = files
    head["roster"] = res["roster"]
    (CELLS / f"{run}.json").write_text(json.dumps({"header": head, **res}, indent=1, default=float))
    print(f"{run}: ok", flush=True)
    return 0


def summary(run: str) -> dict:
    return json.loads((REPO / "runs" / run / "summary.json").read_text())


def find_cell(run: str) -> dict | None:
    """Our own cell first, then the D58 test's, the overnight queue's and the D56 originals."""
    for d in (CELLS, DM.CELLS_OUT, OVN / "cells", D.OUT):
        f = d / f"{run}.json"
        if f.exists():
            (c,) = D.load_results([run], d)
            return c
    return None


def window_cell(w: str, m: float) -> tuple[str, dict | None]:
    """The cell for window w at margin m. m = 0 on the D58 windows is the D58 test's comparator (the originals)."""
    r = DM.M0_RUN[w] if (m == 0.0 and w in D58_WINDOWS) else run_name(w, m)
    return r, find_cell(r)


def row(run: str, cell: dict, h: int = 100) -> dict:
    s = summary(run)
    ev = s["exchange_by_headroom_mw"][str(h)]
    met = D.cell_metrics(cell, H.RULE)
    pm = ev["price_mode"]
    return {"run": run, "peak_mw": ev["peak_mw"], "share_of_m0_ceiling": ev["share_of_m0_ceiling"],
            "gap_share": ev["share_of_selfish_to_ceiling_gap"], "harmed": met["harmed"], "harm_usd": met["harm_usd"],
            "settlement_harmed": met["settlement_harmed"], "settlement_harm_usd": met["settlement_harm_usd"],
            "operator_payout_usd": float(np.sum(cell["rules"][H.RULE]["net"])),
            "max_round_price_usd_mwh": pm["max_peak_price_usd_mwh"], "rounds_total": pm["rounds_total"],
            "margin_usd_mwh": float(s["header"]["config"].get("peak_response_margin_usd_mwh", 0.0))}


def horizon(m: float) -> dict:
    """D58 horizon check across all 23 windows at margin m (fail closed on any missing cell or roster gap)."""
    cells, maxp, rows, missing = [], {}, {}, []
    for w in D58_WINDOWS + OVN_WINDOWS:
        r, c = window_cell(w, m)
        if c is None:
            missing.append(r)
            continue
        rows[w] = row(r, c)
        if rows[w]["margin_usd_mwh"] != m:
            raise CheckFailed(f"{r}: header margin {rows[w]['margin_usd_mwh']} is not {m}")
        cells.append(c)
        maxp[r] = rows[w]["max_round_price_usd_mwh"]
    out = {"margin_usd_mwh": m, "windows": rows, "missing": missing}
    if missing:
        out["horizon"] = None
        return out
    hz = H.horizon_check(cells, maxp)
    g = np.array([a["result_usd"] for a in hz["agents"]])
    out["horizon"] = {k: v for k, v in hz.items() if k != "agents"}
    out["failing_agents"] = [a for a in hz["agents"] if not a["pass"]]
    out["agents_below_zero"] = int((g < 0).sum())
    out["median_agent_usd"] = float(np.median(g))
    out["operator_payout_usd"] = float(sum(r["operator_payout_usd"] for r in rows.values()))
    out["d58_only"] = {k: v for k, v in H.horizon_check(cells[:len(D58_WINDOWS)],
                                                        maxp).items() if k not in ("agents", "windows")}
    out["d58_only"]["operator_payout_usd"] = float(sum(rows[w]["operator_payout_usd"] for w in D58_WINDOWS))
    return out


def report_cmd() -> int:
    from bench.runinfo import run_header
    OUT.mkdir(parents=True, exist_ok=True)
    res = {"header": {**run_header("bench/nov01.py", {"report": True}), "tool_sha256_16": D.tool_sha()},
           "item1": {}, "item3": {}}
    for m in (0.0, 5.0, 10.0):
        try:
            res["item1"][mtag(m)] = horizon(m)
        except CheckFailed as err:
            res["item1"][mtag(m)] = {"failed": str(err)}
    if FEAS_150.exists():
        cells150 = json.loads(FEAS_150.read_text())["cells"]
        for w in OVN_WINDOWS:
            for m in (0.0, 10.0):
                r = run_name(w, m, 150)
                ent = {"feasible": cells150[r]["feasible"], "max_short_mw": cells150[r]["max_short_mw"]}
                c = find_cell(r)
                if c is not None:
                    try:
                        ent.update(row(r, c, 150))
                    except (KeyError, FileNotFoundError) as err:
                        ent["error"] = repr(err)
                res["item3"][r] = ent
    (OUT / "report.json").write_text(json.dumps(res, indent=1, default=float))
    (OUT / "report.md").write_text(report_md(res), encoding="utf-8")
    print((OUT / "report.md").read_text(encoding="utf-8"))
    return 0


def _f(x, nd=1, pct=False):
    if x is None:
        return "n/a"
    return f"{100 * x:.{nd}f}%" if pct else f"{x:,.{nd}f}"


def report_md(res: dict) -> str:
    out = ["# Overnight Nov 1: results (report only)", "", "## Item 1: horizon check across 23 windows at 100 MW", "",
           "| Margin | Horizon | Failing agents | Beyond tolerance $ | Below zero | Median agent $ | 1p settlement gate "
           "| Operator payout $ | D58 11 only: horizon | D58 11: payout $ |", "|---|---|---|---|---|---|---|---|---|---|"]
    for m, v in res["item1"].items():
        if "failed" in v or v.get("horizon") is None:
            out.append(f"| {m} | {'CHECK FAILED: ' + v['failed'] if 'failed' in v else 'missing ' + str(len(v['missing']))}"
                       " | | | | | | | | |")
            continue
        hz = v["horizon"]
        out.append(f"| {m} | {'PASS' if hz['pass'] else 'FAIL'} | {len(v['failing_agents'])} | "
                   f"{_f(hz.get('failing_shortfall_usd'), 0)} | {v['agents_below_zero']} | {_f(v['median_agent_usd'], 0)} | "
                   f"{'pass' if hz['settlement_gate_all_windows'] else 'FAIL'} | {_f(v['operator_payout_usd'], 0)} | "
                   f"{'PASS' if v['d58_only']['pass'] else 'FAIL'} | {_f(v['d58_only']['operator_payout_usd'], 0)} |")
    out += ["", "Per window (share of M0 ceiling, gap share, harm, payout):", "",
            "| Window | m | Share of M0 ceiling | Gap share | Harmed | Harm $ | 1p settlement harm $ | Payout $ |",
            "|---|---|---|---|---|---|---|---|"]
    for m, v in res["item1"].items():
        for w, r in (v.get("windows") or {}).items():
            out.append(f"| {w} | {m} | {_f(r['share_of_m0_ceiling'], 1, True)} | {_f(r['gap_share'], 1, True)} | "
                       f"{r['harmed']} | {_f(r['harm_usd'], 0)} | {_f(r['settlement_harm_usd'], 0)} | "
                       f"{_f(r['operator_payout_usd'], 0)} |")
    out += ["", "## Item 3: overnight windows at 150 MW (feasible cells only run)", "",
            "| Cell | Feasible | Share of M0 ceiling | Gap share | Harmed | Harm $ | 1p settlement harm $ | Payout $ |",
            "|---|---|---|---|---|---|---|---|"]
    for r, e in res["item3"].items():
        out.append(f"| {r} | {e['feasible']} | {_f(e.get('share_of_m0_ceiling'), 1, True)} | "
                   f"{_f(e.get('gap_share'), 1, True)} | {e.get('harmed', 'n/a')} | {_f(e.get('harm_usd'), 0)} | "
                   f"{_f(e.get('settlement_harm_usd'), 0)} | {_f(e.get('operator_payout_usd'), 0)} |")
    return "\n".join(out) + "\n"


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    cmd = a[0] if a else ""
    if cmd == "configs":
        return configs_cmd()
    if cmd == "feas":
        return feas_cmd()
    if cmd == "cell":
        return cell_cmd(a[1])
    if cmd == "report":
        return report_cmd()
    if cmd == "list150":
        print("\n".join(feasible_150()))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
