"""Silent harm diagnosis (Maritime list item 1; literature mapping sections 2 and 6). Offline, report only.

Question: when one agent goes silent, what does the exchange do differently that costs specific honest agents money?
For one silent cell (`silent` or `silent_deviate`) and its honest twin, from the traces and summaries (`74b1ae0`):

  (a) T2-a split: each honest agent's value change against the twin, split into peak settlement (`peak_settle`
      net), imbalance (`imbalance` charges), and real-time energy net of wear (energy = sum rt x site net x step from
      the trace's telemetry and the data's real-time price; wear = wear_usd_mwh x discharge). The parts must add up
      to the summary's agent_value_usd change within $0.01 (fail closed).
  (b) Other suspects, ruled in or out: the operator's messages (zone targets, call windows and deadlines, re-forecasts),
      the binding aggregates, every price round's prices, every honest agent's nominations, every 1p reference; the
      first message where silent and twin differ, and the silent agent's stand-in there (its 1p reference for that
      call, D78) against the twin's nomination from the same agent.
  (d) T2-c offline: pool the silent agent's imbalance charges and pay it to the honest agents in proportion to their
      binding energy (the delivery record's this-run `weight_kwh`, D65). Who is still short of the twin?

Run:  python -m bench.silent_diag <cell_dir> <twin_dir> [--deviate <silent_deviate_dir>] [--out file.json]
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

SPLIT_TOL_USD = 0.01


class CheckFailed(RuntimeError):
    pass


def _trace_path(run_dir: Path) -> Path:
    paths = sorted(Path(run_dir).glob("exchange_h*/trace.jsonl"))
    if len(paths) != 1:
        raise CheckFailed(f"{run_dir}: expected one exchange trace, found {len(paths)}")
    return paths[0]


def load(run_dir) -> dict:
    run_dir = Path(run_dir)
    summ = json.loads((run_dir / "summary.json").read_text())
    ev = next(iter(summ["exchange_by_headroom_mw"].values()))
    r = {"dir": str(run_dir), "value": ev["agent_value_usd"], "harm": ev["net_harm"]["by_agent_usd"],
         "net": defaultdict(dict), "meter": defaultdict(dict), "imb": defaultdict(float), "peak": {},
         "op": [], "agg": {}, "pp": {}, "pp_final": {}, "noms": defaultdict(list), "sref": {}, "records": {}}
    with open(_trace_path(run_dir), encoding="utf-8") as f:
        r["header"] = json.loads(next(f))
        for line in f:
            m = json.loads(line)
            typ = m.get("msg_type")
            if typ is None:
                continue
            p, frm, t = m["payload"], m["from"], m["t_utc"]
            if typ == "telemetry":
                aid = p["asset_id"].split("/")[0]
                r["net"][aid][t] = float(p.get("net_kw") or 0.0)
                r["meter"][aid][t] = float(p["meter_kw"])
            elif typ == "imbalance":
                r["imb"][m["to"]] += float(p["charge_usd"])
            elif typ == "peak_settle":
                r["peak"][m["to"]] = float(p["net_usd"])
            elif frm == r["header"].get("exchange", {}).get("operator_id", "GRID_OP"):
                r["op"].append((typ, t, json.dumps(p, sort_keys=True)))
            elif typ == "nomination_aggregate":
                r["agg"][p["call_id"]] = (t, p["binding"], p["kw"])
            elif typ == "peak_price":
                r["pp"][(p["call_id"], p["round"])] = (t, p["price_usd_mwh"], p["shift_kw"], p["rho"])
                if p["final"]:
                    r["pp_final"][p["call_id"]] = json.dumps(p, sort_keys=True)
            elif typ == "nominate":
                r["noms"][frm].append((t, p["call_id"], p["kw"]))
            elif typ == "selfish_reference":
                r["sref"][(p["call_id"], p["asset_id"].split("/")[0])] = (t, p["net_kw"])
            elif typ == "delivery_record":
                r["records"][m["to"]] = p.get("this_run") or {}
    r["roster"] = list(r["header"].get("roster") or [])
    r["attackers"] = [a["agent"] for a in r["header"].get("attackers") or []]
    return r


def rt_prices(header) -> dict:
    """Real-time price per UTC step over the run's slice (data, read only)."""
    from sim.data import SYS_FILE, _read_range, local_range
    cfg = header["config"]
    data_dir = Path(cfg["data_dir"])
    if not data_dir.exists():
        data_dir = Path(__file__).resolve().parents[2] / "data"
    t0, t1 = local_range(cfg["start"], cfg["end"], cfg["slice_tz"])
    df = _read_range(data_dir / SYS_FILE, ["timestamp", "price_usd_mwh"], t0, t1).sort_values("timestamp")
    return {ts.strftime("%Y-%m-%dT%H:%M:%SZ"): float(v) for ts, v in zip(df.timestamp, df.price_usd_mwh)}


def split(cell, twin, rt) -> dict:
    """(a) T2-a per honest agent."""
    cfg = cell["header"]["config"]
    wear = float(cfg["wear_usd_mwh"])
    K = 0.25 / 1000.0
    out, worst = {}, 0.0
    honest = [x for x in cell["roster"] if x not in cell["attackers"]]
    for aid in honest:
        parts = {}
        for name, r in (("cell", cell), ("twin", twin)):
            e = sum(rt[t] * K * v for t, v in r["net"][aid].items())
            w = wear * K * sum(max(v, 0.0) for v in r["meter"][aid].values())
            parts[name] = {"energy_net_of_wear": e - w, "peak": r["peak"].get(aid, 0.0), "imbalance": r["imb"].get(aid, 0.0)}
        d = {k: parts["cell"][k] - parts["twin"][k] for k in parts["cell"]}
        dv = cell["value"][aid] - twin["value"][aid]
        resid = dv - (d["energy_net_of_wear"] + d["peak"] - d["imbalance"])
        worst = max(worst, abs(resid))
        meter_steps = sum(1 for t in cell["meter"][aid] if abs(cell["meter"][aid][t] - twin["meter"][aid].get(t, 0.0)) > 1e-6)
        out[aid] = {"value_change_usd": dv, "peak_settle_usd": d["peak"], "imbalance_usd": -d["imbalance"],
                    "energy_net_of_wear_usd": d["energy_net_of_wear"], "residual_usd": resid,
                    "meter_steps_differ": meter_steps}
    if worst > SPLIT_TOL_USD:
        raise CheckFailed(f"value split doesn't add up: worst residual ${worst:.4f}")
    return out


def suspects(cell, twin) -> dict:
    """(b) every channel, silent vs twin."""
    sa = cell["attackers"][0]
    res = {}
    res["operator_messages"] = {"cell": len(cell["op"]), "twin": len(twin["op"]),
                                "differ": sum(a != b for a, b in zip(cell["op"], twin["op"])) + abs(len(cell["op"]) - len(twin["op"])),
                                "types": sorted({x[0] for x in cell["op"]})}
    first_op = next((i for i, (a, b) in enumerate(zip(cell["op"], twin["op"])) if a != b), None)
    res["operator_messages"]["first_differ"] = (cell["op"][first_op][:2] if first_op is not None else None)
    aggs = {}
    for c in sorted(set(cell["agg"]) | set(twin["agg"]), key=lambda c: int(c.split(":")[1])):
        a, b = cell["agg"].get(c), twin["agg"].get(c)
        aggs[c] = {"binding": (a or b)[1], "max_abs_diff_kw": float(np.abs(np.array(a[2]) - np.array(b[2])).max())
                   if a and b else None}
    res["aggregates"] = aggs
    # price rounds, in time order
    keys = sorted(set(cell["pp"]) | set(twin["pp"]), key=lambda k: ((cell["pp"].get(k) or twin["pp"].get(k))[0], k[1]))
    rounds, first = [], None
    for k in keys:
        a, b = cell["pp"].get(k), twin["pp"].get(k)
        if a is None or b is None:
            rounds.append({"call": k[0], "round": k[1], "only_in": "cell" if b is None else "twin"})
            if first is None:
                first = rounds[-1]
            continue
        dp = float(np.abs(np.array(a[1]) - np.array(b[1])).max())
        if dp > 0 or a[2] != b[2] or a[3] != b[3]:
            rounds.append({"call": k[0], "round": k[1], "t_utc": a[0], "max_abs_price_diff_usd_mwh": dp})
            if first is None:
                first = {**rounds[-1], "_a": a, "_b": b}
    res["price_rounds"] = {"total_cell": len(cell["pp"]), "total_twin": len(twin["pp"]), "differ": len(rounds),
                           "first_differ": {k: v for k, v in (first or {}).items() if not k.startswith("_")}}
    # honest nominations
    nd, nfirst = 0, None
    for aid in cell["roster"]:
        if aid == sa:
            continue
        a, b = cell["noms"].get(aid, []), twin["noms"].get(aid, [])
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                nd += 1
                if nfirst is None or x[0] < nfirst[0]:
                    nfirst = (x[0], aid, x[1])
        nd += abs(len(a) - len(b))
    res["honest_nominations"] = {"differ": nd, "first_differ": nfirst}
    srd = [k for k in set(cell["sref"]) | set(twin["sref"]) if cell["sref"].get(k, (0, None))[1] != twin["sref"].get(k, (0, None))[1]]
    res["references_differ"] = sorted(srd)[:10]
    res["references_differ_count"] = len(srd)
    res["silent_agent_nominations"] = {"cell": len(cell["noms"].get(sa, [])), "twin": len(twin["noms"].get(sa, []))}
    # the silent agent's stand-in at the first differing round: its 1p reference for that call vs its twin nomination
    if first and "_a" in first:
        c, t_round = first["call"], first["_a"][0]
        ref = cell["sref"].get((c, sa))
        noms = [n for n in twin["noms"].get(sa, []) if n[0] <= t_round]
        latest_twin = noms[-1] if noms else None
        pa, pb = np.array(first["_a"][1]), np.array(first["_b"][1])
        idx = np.flatnonzero(np.abs(pa - pb) > 0)
        st = {"call": c, "round": first["round"], "t_utc": t_round, "steps_price_differs": int(len(idx))}
        if ref is not None:
            q = np.array(ref[1])
            st["stand_in_reference_kw_at_those_steps"] = {"min": float(q[idx].min()), "max": float(q[idx].max())} if len(idx) else None
        if latest_twin is not None and ref is not None and latest_twin[1] == c:
            n = np.array(latest_twin[2])
            diff = q - n
            st["twin_nomination_call"] = latest_twin[1]
            st["stand_in_minus_twin_nomination_kw"] = {"max_abs": float(np.abs(diff).max()),
                                                       "at_price_diff_steps_mean": float(diff[idx].mean()) if len(idx) else None,
                                                       "steps_differ": int((np.abs(diff) > 1e-6).sum())}
        elif latest_twin is not None:
            st["twin_latest_nomination_call"] = latest_twin[1]
        res["first_divergence_stand_in"] = st
    return res


def pool(cell, twin) -> dict:
    """(d) T2-c: the silent agent's imbalance charges, paid to honest agents in proportion to binding energy."""
    sa = cell["attackers"][0]
    honest = [x for x in cell["roster"] if x not in cell["attackers"]]
    pot = cell["imb"].get(sa, 0.0)
    w = {x: float(cell["records"].get(x, {}).get("weight_kwh") or 0.0) for x in honest}
    W = sum(w.values())
    pay = {x: pot * w[x] / W if W > 0 else 0.0 for x in honest}
    loss = {x: twin["value"][x] - cell["value"][x] for x in honest if cell["value"][x] < twin["value"][x]}
    short = {x: loss[x] - pay[x] for x in loss if pay[x] < loss[x]}
    return {"pool_usd": pot, "binding_energy_kwh_honest": W, "losers": len(loss), "total_loss_usd": sum(loss.values()),
            "made_whole": len(loss) - len(short), "still_short": len(short), "still_short_usd": sum(short.values()),
            "still_short_agents": {x: round(v, 4) for x, v in sorted(short.items(), key=lambda kv: -kv[1])[:20]}}


def diagnose(cell_dir, twin_dir, deviate_dir=None) -> dict:
    cell, twin = load(cell_dir), load(twin_dir)
    if cell["header"].get("git_commit") != twin["header"].get("git_commit") or cell["roster"] != twin["roster"]:
        raise CheckFailed("commit or roster differs")
    if len(cell["attackers"]) != 1 or twin["attackers"]:
        raise CheckFailed("need one attacker in the cell and none in the twin")
    rt = rt_prices(cell["header"])
    out = {"cell": Path(cell_dir).name, "twin": Path(twin_dir).name, "commit": cell["header"].get("git_commit"),
           "attacker": cell["attackers"][0], "split": split(cell, twin, rt), "suspects": suspects(cell, twin),
           "pool": pool(cell, twin)}
    sp = out["split"]
    out["split_totals"] = {k: sum(v[k] for v in sp.values()) for k in
                           ("value_change_usd", "peak_settle_usd", "imbalance_usd", "energy_net_of_wear_usd")}
    out["split_max_abs"] = {k: max(abs(v[k]) for v in sp.values()) for k in
                            ("peak_settle_usd", "imbalance_usd", "energy_net_of_wear_usd")}
    out["t2a_energy_and_imbalance_zero_within_0.01"] = (out["split_max_abs"]["imbalance_usd"] <= SPLIT_TOL_USD
                                                        and out["split_max_abs"]["energy_net_of_wear_usd"] <= SPLIT_TOL_USD)
    out["honest_meters_differ_agents"] = sum(1 for v in sp.values() if v["meter_steps_differ"])
    if deviate_dir:
        dev = load(deviate_dir)
        out["final_prices_equal_to_deviate"] = {c: dev["pp_final"].get(c) == p for c, p in cell["pp_final"].items()}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cell")
    ap.add_argument("twin")
    ap.add_argument("--deviate")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    try:
        res = diagnose(args.cell, args.twin, args.deviate)
    except CheckFailed as e:
        print(f"CHECK FAILED: {e}")
        return 2
    text = json.dumps(res, indent=1, default=str)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    print(json.dumps({k: v for k, v in res.items() if k != "split"}, indent=1, default=str)[:5000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
