"""T3: where a misdeclaring attacker's extra peak settlement comes from, and what a wider D81 void would leave
(Maritime list item 2; literature mapping section 3). Offline, trace only: no data files, no simulation.

The exchange settles peak relief once, at the closing tick (exchange/core.py `on_tick`, `final`): first the D81 void
(`_void_declarations`), then today's 1p (`_peak_settle`) or the max rule (`_peak_settle_max`). This tool rebuilds
those three steps from one run's exchange trace:

  references   every `selfish_reference` (net_kw, net_kw_registered under declared_reference), never revised
  prices       each binding call's settled price per step = its last `peak_price` before its aggregate (the
               price in force when its gate closed), later binding calls replacing earlier ones (today's rule) or
               chained (max rule)
  nominations  the latest nominated kW per agent and step at each binding aggregate (max rule's call_nom)
  meters       `telemetry` net_kw (site net), meter_kw (battery meter) and soc_kwh, per asset and step
  declarations `declare_availability` per asset and local day (latest wins) with its filing time, and
               `declare_outage` windows; a filing D82 rejects at filing (its reject follows it directly) never counts;
               one D82 drops later (an outage made 1p infeasible) counts until the drop

Void rules:
  d81   today's rule: on each local day with a breach (battery meter above declared power, SOC below declared
        reserve, activity in a declared outage), every call's stored reference for that day's steps is replaced by
        its registered-limits reference (net_kw_registered)
  t3a   D87 (built Nov 6, T3-c): additionally, every stored reference whose days in force when it was filed (a
        declaration or forced outage filed at or before it, for any day: 1p plans the whole run) include a breached
        day is replaced by its net_kw_registered on all of its steps. No window condition (the Nov 6 correction)

Fail closed: with the rule the run itself used (t3a when its delivery records carry `voided_calls`, the D87 build;
d81 otherwise) the tool must reproduce the run's own `peak_settle` net per agent within $0.001, the days it voids must
equal the records' `voided_days`, and (D87 runs) the calls it voids whole must equal the records' `voided_calls`;
otherwise it exits 2 and reports nothing else. That is a consistency check between the in-run code and this tool, not
independent evidence: both implement the same rule.

Run:  python -m bench.t3_void <run_dir> [<run_dir> ...] [--out file.json]
      python -m bench.t3_void --pair <attack_dir> <twin_dir> --check <d61 check json> [--out file.json]
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from exchange.core import ENERGY_TOL_KWH
from exchange.records import ACTIVITY_TOL_KW

REPRO_TOL_USD = 1e-3        # fail closed: the d81 rebuild vs the run's own peak_settle, per agent
PRICED = 1e-9               # exchange/core.py: a settled price above this is priced
GAIN_GUARD_USD = 1e-6       # T3-a pass: attacker gain at or below this (D61's float guard)
HONEST_TOL_USD = 1e-9       # T3-a pass: every honest agent unchanged within this
UNSUPPORTED = ("cap_rebase", "deregister", "award", "settle", "rebound")


class CheckFailed(RuntimeError):
    pass


def _t(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _f(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def trace_path(run_dir: Path) -> Path:
    paths = sorted(Path(run_dir).glob("exchange_h*/trace.jsonl"))
    if len(paths) != 1:
        raise CheckFailed(f"{run_dir}: expected one exchange trace, found {len(paths)}")
    return paths[0]


def load(run_dir) -> dict:
    """One pass over the exchange trace, in order."""
    run_dir = Path(run_dir)
    st = {"dir": str(run_dir), "calls": {}, "agg_order": [], "refs": defaultdict(dict), "regs": defaultdict(dict),
          "ref_t": defaultdict(dict), "last_price": {}, "settled_price": {}, "call_nom": {}, "latest": defaultdict(dict),
          "net": defaultdict(dict), "meter": defaultdict(dict), "soc": defaultdict(dict), "settle": {},
          "settle_t": defaultdict(list), "decl": defaultdict(dict), "assets": {}, "records": {}, "counts": Counter(),
          "rejects": [], "final_t": None, "imbalance": defaultdict(float), "outages": defaultdict(list)}
    # first pass: a reject that directly follows the message it refers to refused that message (never stored); a
    # later infeasible_declaration reject (D82 amendment: an outage made 1p infeasible) drops a stored declaration
    refused, dropped, prev = set(), {}, None
    with open(trace_path(run_dir), encoding="utf-8") as f:
        next(f)
        for line in f:
            m = json.loads(line)
            if m.get("msg_type") == "reject":
                ref = m["payload"].get("ref_msg_id")
                if ref == prev:
                    refused.add(ref)
                elif m["payload"].get("code") == "infeasible_declaration":
                    dropped[ref] = m["t_utc"]
                continue
            prev = m.get("msg_id")
    st["refused"] = sorted(x for x in refused if x)
    st["dropped"] = dropped
    with open(trace_path(run_dir), encoding="utf-8") as f:
        header = json.loads(next(f))
        ex = header.get("exchange") or {}
        st["header"] = header
        st["tz"] = ZoneInfo(header["config"].get("slice_tz") or "America/Chicago")   # sim.run_m1.TZ when unset
        st["rule"] = ex.get("settlement_rule", "today")
        st["declared_reference"] = bool(ex.get("declared_reference"))
        step_min = int(ex.get("step_minutes", 15))
        st["step"] = timedelta(minutes=step_min)
        st["h"] = step_min / 60.0 / 1000.0
        for line in f:
            m = json.loads(line)
            typ = m.get("msg_type")
            if typ is None:
                continue
            p = m["payload"]
            st["counts"][typ] += 1
            if m.get("msg_id") in refused:
                continue
            if typ in UNSUPPORTED:
                raise CheckFailed(f"{run_dir}: {typ} messages: this tool does not rebuild that path")
            if typ == "call_nominations" and m["from"] == "exchange":
                w = p["window"]
                steps, t = [], _t(w["start_utc"])
                while t < _t(w["end_utc"]):
                    steps.append(_f(t))
                    t += st["step"]
                st["calls"][p["call_id"]] = {"binding": bool(p["binding"]), "steps": steps,
                                            "deadline": p["deadline_utc"]}
            elif typ == "register":
                st["assets"][p["agent_id"]] = [a["asset_id"] for a in p["assets"]]
            elif typ == "telemetry":
                a, t = p["asset_id"], m["t_utc"]
                st["meter"][a][t] = float(p["meter_kw"])
                if p.get("soc_kwh") is not None:
                    st["soc"][a][t] = float(p["soc_kwh"])
                if p.get("net_kw") is not None:
                    st["net"][a][t] = float(p["net_kw"])
            elif typ == "selfish_reference":
                c, a = p["call_id"], p["asset_id"]
                steps = st["calls"][c]["steps"]
                if a in st["refs"][c]:
                    raise CheckFailed(f"{run_dir}: two references for {a} on {c}")
                st["refs"][c][a] = dict(zip(steps, map(float, p["net_kw"])))
                st["ref_t"][c][a] = m["t_utc"]
                if p.get("net_kw_registered") is not None:
                    st["regs"][c][a] = dict(zip(steps, map(float, p["net_kw_registered"])))
            elif typ == "nominate":
                c = p["call_id"]
                st["latest"][m["from"]].update(zip(st["calls"][c]["steps"], map(float, p["kw"])))
            elif typ == "peak_price":
                st["last_price"][p["call_id"]] = (p["window"], p["price_usd_mwh"])
            elif typ == "nomination_aggregate":
                c = p["call_id"]
                if not p["binding"]:
                    continue
                st["agg_order"].append(c)
                w, price = st["last_price"].get(c, (None, None))
                steps = st["calls"][c]["steps"]
                if price is None:
                    st["settled_price"][c] = {t: 0.0 for t in steps}
                else:
                    if w["start_utc"] != steps[0] or len(price) != len(steps):
                        raise CheckFailed(f"{run_dir}: peak_price window of {c} is not the call's")
                    st["settled_price"][c] = dict(zip(steps, map(float, price)))
                if st["rule"] == "max_first_call":
                    st["call_nom"][c] = {aid: {t: lat[t] for t in steps if t in lat}
                                         for aid, lat in st["latest"].items()}
            elif typ == "declare_availability":
                st["decl"][p["asset_id"]].setdefault(p["date"], []).append({**p, "t_utc": m["t_utc"],
                                                                            "msg_id": m["msg_id"],
                                                                            "dropped_t": dropped.get(m["msg_id"])})
            elif typ == "declare_outage":
                w, steps, t = p["window"], [], _t(p["window"]["start_utc"])
                while t < _t(w["end_utc"]):
                    steps.append(_f(t))
                    t += st["step"]
                st["outages"][p["asset_id"]].append({"t_utc": m["t_utc"], "steps": set(steps)})
            elif typ == "peak_settle":
                st["settle"][m["to"]] = p
                st["settle_t"][m["to"]].append(m["t_utc"])
            elif typ == "imbalance":
                st["imbalance"][m["to"]] += float(p["charge_usd"])
            elif typ == "delivery_record":
                st["records"][m["to"]] = p
            elif typ == "reject":
                st["rejects"].append({"to": m["to"], **p})
            elif typ == "tick" and p.get("final"):
                st["final_t"] = m["t_utc"]
    st["roster"] = list(header.get("roster") or [])
    missing = sorted(set(st["roster"]) - set(st["settle"]))
    if not st["roster"] or missing:
        raise CheckFailed(f"{run_dir}: roster agents without peak_settle: {missing[:10]}")
    return st


def local_date(st, t: str) -> str:
    return _t(t).astimezone(st["tz"]).date().isoformat()


def days_in_force(st, asset: str, as_of: str) -> frozenset:
    """exchange/core.py `days_in_force`: local dates with a declaration (stored at as_of: filed at or before it and
    not dropped by then) or a forced outage filed at or before as_of."""
    days = {d for d, xs in st["decl"].get(asset, {}).items()
            if any(x["t_utc"] <= as_of and not (x["dropped_t"] and x["dropped_t"] <= as_of) for x in xs)}
    for o in st["outages"].get(asset, []):
        if o["t_utc"] <= as_of:
            days |= {local_date(st, t) for t in o["steps"]}
    return frozenset(days)


def breach_days(st, asset: str) -> list[str]:
    """exchange/core.py `_breach_steps` under declared_reference: the latest declaration per day still stored at the
    closing tick (dropped ones left out), and activity during a forced outage."""
    decl = {}
    for d, xs in st["decl"].get(asset, {}).items():
        kept = [x for x in xs if not x["dropped_t"]]
        if kept:
            decl[d] = kept[-1]
    down = set().union(*(o["steps"] for o in st["outages"].get(asset, []))) if st["outages"].get(asset) else set()
    days = set()
    for t, kw in st["meter"].get(asset, {}).items():
        x = decl.get(local_date(st, t))
        if t in down or (x is not None and x["planned_outage"]):
            if abs(kw) > ACTIVITY_TOL_KW:
                days.add(local_date(st, t))
        elif x is not None and abs(kw) > x["power_kw"] + ACTIVITY_TOL_KW:
            days.add(local_date(st, t))
    for t, soc in st["soc"].get(asset, {}).items():
        x = decl.get(local_date(st, t))
        if x is not None and soc < x["reserve_kwh"] - ENERGY_TOL_KWH:
            days.add(local_date(st, t))
    return sorted(days)


def voided_refs(st, rule: str) -> tuple[dict, dict]:
    """Stored references after the void rule (`d81`, `t3a` or `none`), and what each rule replaced."""
    refs = {c: {a: dict(v) for a, v in by.items()} for c, by in st["refs"].items()}
    info = {}
    if rule == "none" or not st["declared_reference"]:
        return refs, info
    for asset in sorted({a for by in st["regs"].values() for a in by}):
        days = breach_days(st, asset)
        if not days:
            continue
        rep = {"voided_days": days, "calls_whole": [], "steps_replaced": 0, "steps_changed": 0}
        for c, by in st["regs"].items():
            reg = by.get(asset)
            if reg is None:
                continue
            cur = refs[c][asset]
            whole = False
            if rule == "t3a":
                whole = bool(days_in_force(st, asset, st["ref_t"][c][asset]) & set(days))
            if whole:
                rep["calls_whole"].append(c)
            for t, v in reg.items():
                if whole or local_date(st, t) in days:
                    rep["steps_replaced"] += 1
                    rep["steps_changed"] += cur[t] != v
                    cur[t] = v
        info[asset] = rep
    return refs, info


def metered(st, aid) -> dict:
    out = defaultdict(float)
    for a in st["assets"][aid]:
        for t, v in st["net"].get(a, {}).items():
            out[t] += v
    return out


def settle_today(st, refs) -> dict:
    """exchange/core.py `_peak_settle` (selfish_1p): the last binding call to close settles each step."""
    by_call, price = {}, {}
    for c in st["agg_order"]:
        for t, p in st["settled_price"][c].items():
            by_call[t], price[t] = c, p
    priced = {t: p for t, p in price.items() if p > PRICED}
    h, out = st["h"], {}
    for aid in st["roster"]:
        met, ref = metered(st, aid), defaultdict(float)
        for a in st["assets"][aid]:
            for t in priced:
                r = refs.get(by_call[t], {}).get(a, {}).get(t)
                if r is None:
                    raise CheckFailed(f"no selfish_reference for {a} on {by_call[t]} at {t}")
                ref[t] += r
        charge = sum(p * -met.get(t, 0.0) * h for t, p in priced.items())
        refund = sum(p * -ref[t] * h for t, p in priced.items())
        out[aid] = {"net_usd": refund - charge, "by_step": {t: p * (met.get(t, 0.0) - ref[t]) * h
                                                            for t, p in priced.items()}}
    return out


def settle_max(st, refs) -> dict:
    """exchange/core.py `_peak_settle_max` (D76's max rule with the relief cap), every asset held all run."""
    chain = defaultdict(list)
    for c in st["agg_order"]:
        for t in st["settled_price"][c]:
            chain[t].append(c)
    cp = st["settled_price"]
    priced = sorted(t for t, cids in chain.items() if any(cp[c][t] > PRICED for c in cids))
    h, out = st["h"], {}
    for aid in st["roster"]:
        assets, met = st["assets"][aid], metered(st, aid)
        net, by_step = 0.0, {}
        for t in priced:
            cids = chain[t]
            calls = []
            for c in cids:
                rs = [refs.get(c, {}).get(x, {}).get(t) for x in assets]
                if any(r is None for r in rs):
                    raise CheckFailed(f"no selfish_reference for {aid} on {c} at {t} (D77 path not rebuilt)")
                calls.append((c, sum(rs)))
            noms = [st["call_nom"].get(c, {}).get(aid, {}).get(t) for c, _ in calls]
            terms, rels, prev = [], [], None
            for (c, q), nv in zip(calls, noms):
                rel = (q if nv is None else nv) - q
                terms.append((cp[c][t], rel if prev is None else rel - prev))
                rels.append(rel)
                prev = rel
            later, pay = float("-inf"), 0.0
            for p, inc in reversed(terms):
                later = max(later, p)
                pay += later * inc
            c_k, q_k = calls[-1]
            p_k = cp[c_k][t]
            n_k = q_k if noms[-1] is None else noms[-1]
            m_t = met.get(t, 0.0)
            pay += p_k * (m_t - n_k)
            p_1 = max(p for p, _ in terms)
            if p_1 > p_k + 1e-9:
                q_j = next((q for c, q in reversed(calls) if cp[c][t] > PRICED), calls[0][1])
                rel_met = m_t - q_j
                prem, nxt, lvl = 0.0, p_k, float("-inf")
                for (p, _), rel in zip(reversed(terms), reversed(rels)):
                    lvl = max(lvl, p)
                    prem += (lvl - nxt) * max(0.0, min(rel, rel_met))
                    nxt = lvl
                pay = p_k * (m_t - q_k) + prem
            net += pay * h
            by_step[t] = pay * h
        out[aid] = {"net_usd": net, "by_step": by_step}
    return out


def settle(st, refs) -> dict:
    return settle_max(st, refs) if st["rule"] == "max_first_call" else settle_today(st, refs)


def rebuild(run_dir) -> dict:
    """Fail-closed rebuild under the void rule the run used (t3a for a D87 build, whose records carry voided_calls;
    d81 otherwise), then both rules for comparison. Raises CheckFailed on any mismatch."""
    st = load(run_dir)
    in_run = "t3a" if any("voided_calls" in (r.get("this_run") or {}) for r in st["records"].values()) else "d81"
    refs81, void81 = voided_refs(st, "d81")
    s81 = settle(st, refs81)
    refs3, void3 = voided_refs(st, "t3a")
    s3 = settle(st, refs3)
    s_run, void_run = (s3, void3) if in_run == "t3a" else (s81, void81)
    worst = max(abs(s_run[a]["net_usd"] - float(st["settle"][a]["net_usd"])) for a in st["roster"])
    if worst > REPRO_TOL_USD:
        bad = sorted(st["roster"], key=lambda a: -abs(s_run[a]["net_usd"] - float(st["settle"][a]["net_usd"])))[:5]
        raise CheckFailed(f"{run_dir}: {in_run} rebuild vs peak_settle, max error ${worst:.6f} ({bad})")
    rec_days, rec_calls = {}, {}
    for aid, rec in st["records"].items():
        tr = rec.get("this_run") or {}
        if tr.get("voided_days"):
            rec_days[aid] = sorted(tr["voided_days"])
        if tr.get("voided_calls"):
            rec_calls[aid] = sorted(tr["voided_calls"])
    tool_days = {aid: sorted({d for a in st["assets"][aid] for d in void_run.get(a, {}).get("voided_days", [])})
                 for aid in st["roster"]}
    tool_days = {a: d for a, d in tool_days.items() if d}
    if st["declared_reference"] and st["records"] and rec_days != tool_days:
        raise CheckFailed(f"{run_dir}: voided days {tool_days} vs delivery records {rec_days}")
    if in_run == "t3a":
        tool_calls = {aid: sorted({c for a in st["assets"][aid] for c in void3.get(a, {}).get("calls_whole", [])})
                      for aid in st["roster"]}
        tool_calls = {a: c for a, c in tool_calls.items() if c}
        if tool_calls != rec_calls:
            raise CheckFailed(f"{run_dir}: voided calls {tool_calls} vs delivery records {rec_calls}")
    # suspect (b): any peak settlement paid before the closing tick (and so before the void)?
    early = {a: ts for a, ts in st["settle_t"].items() if any(t != st["final_t"] for t in ts) or len(ts) != 1}
    return {"run": Path(run_dir).name, "dir": str(run_dir), "rule": st["rule"], "void_rule_in_run": in_run,
            "voided_calls_records": rec_calls,
            "commit": st["header"].get("git_commit"), "attackers": st["header"].get("attackers") or [],
            "declared_reference": st["declared_reference"], "final_tick": st["final_t"],
            "repro_max_err_usd": worst, "voided_days_records": rec_days,
            "void_d81": void81, "void_t3a": void3,
            "peak_settle_in_run": {a: float(st["settle"][a]["net_usd"]) for a in st["roster"]},
            "net_d81": {a: s81[a]["net_usd"] for a in st["roster"]},
            "net_t3a": {a: s3[a]["net_usd"] for a in st["roster"]},
            "settle_before_final_tick": early, "imbalance_usd": dict(st["imbalance"]),
            "_st": st, "_s81": s81, "_s3": s3, "_refs81": refs81}


def by_day(st, by_step: dict) -> dict:
    out = defaultdict(float)
    for t, v in by_step.items():
        out[local_date(st, t)] += v
    return dict(out)


def pair(atk_dir, twin_dir, check_json=None) -> dict:
    """T3 for one attack cell against its honest twin."""
    a, w = rebuild(atk_dir), rebuild(twin_dir)
    if a["_st"]["roster"] != w["_st"]["roster"]:
        raise CheckFailed("rosters differ")
    attackers = [s["agent"] for s in a["attackers"]]
    if not attackers or w["attackers"]:
        raise CheckFailed("the attack run needs attackers and the twin none")
    chk = json.loads(Path(check_json).read_text()) if check_json else None
    roster = a["_st"]["roster"]
    res = {"attack": a["run"], "twin": w["run"], "rule": a["rule"], "commit": a["commit"],
           "repro_max_err_usd": {"attack": a["repro_max_err_usd"], "twin": w["repro_max_err_usd"]},
           "twin_changed_by_t3a_max_usd": max(abs(w["net_t3a"][x] - w["net_d81"][x]) for x in roster),
           "settle_before_final_tick": {"attack": a["settle_before_final_tick"], "twin": w["settle_before_final_tick"]},
           "attackers": {}}
    honest = [x for x in roster if x not in attackers]
    res["honest_max_change_t3a_usd"] = max(abs(a["net_t3a"][x] - a["net_d81"][x]) for x in honest)
    for aid in attackers:
        d_settle = a["net_d81"][aid] - w["net_d81"][aid]
        d_t3 = a["net_t3a"][aid] - a["net_d81"][aid]
        d_t3_twin = w["net_t3a"][aid] - w["net_d81"][aid]
        gain = chk["attackers"][aid]["gain_usd"] if chk else None
        nonpeak = (gain - (a["peak_settle_in_run"][aid] - w["peak_settle_in_run"][aid])) if gain is not None else 0.0
        st_a, st_w = a["_st"], w["_st"]
        day81 = by_day(st_a, a["_s81"][aid]["by_step"])
        dayw = by_day(st_w, w["_s81"][aid]["by_step"])
        day3 = by_day(st_a, a["_s3"][aid]["by_step"])
        voided = sorted({d for x in st_a["assets"][aid] for d in a["void_d81"].get(x, {}).get("voided_days", [])})
        res["attackers"][aid] = {
            "gain_in_run_usd": gain,
            "peak_settle_attack_minus_twin_usd": d_settle,
            "gain_minus_peak_settle_usd": (gain - d_settle) if gain is not None else None,
            "imbalance_attack_minus_twin_usd": a["imbalance_usd"].get(aid, 0.0) - w["imbalance_usd"].get(aid, 0.0),
            "voided_days": voided,
            "settle_by_day_d81_minus_twin_usd": {d: day81.get(d, 0.0) - dayw.get(d, 0.0)
                                                 for d in sorted(set(day81) | set(dayw))},
            "settle_by_day_t3a_minus_twin_usd": {d: day3.get(d, 0.0) - dayw.get(d, 0.0)
                                                 for d in sorted(set(day3) | set(dayw))},
            "t3a_change_usd": d_t3, "t3a_change_twin_usd": d_t3_twin,
            # judged: rebuilt attack against rebuilt twin (the trace's 6-decimal prices round the same way in both),
            # plus the part of the in-run gain that isn't peak settlement
            "gain_non_peak_usd": nonpeak,
            "gain_d81_rebuilt_usd": d_settle + nonpeak,
            "gain_t3a_usd": a["net_t3a"][aid] - w["net_t3a"][aid] + nonpeak,
            # information: the in-run gain plus the rebuilt T3-a change (mixes exact and 6-decimal prices; its error
            # is up to the rebuild's reproduction error)
            "gain_t3a_mixed_usd": (gain + d_t3 - d_t3_twin) if gain is not None else None,
            "calls_whole_t3a": {x: a["void_t3a"].get(x, {}).get("calls_whole", []) for x in st_a["assets"][aid]},
        }
    gains = [v["gain_t3a_usd"] for v in res["attackers"].values()]
    res["t3a_pass"] = {"attacker_gain_le_guard": all(g <= GAIN_GUARD_USD for g in gains),
                       "honest_unchanged": res["honest_max_change_t3a_usd"] <= HONEST_TOL_USD,
                       "twin_unchanged": res["twin_changed_by_t3a_max_usd"] <= HONEST_TOL_USD}
    res["t3a_pass"]["all"] = all(res["t3a_pass"].values())
    return res


def _public(r: dict) -> dict:
    return {k: v for k, v in r.items() if not k.startswith("_")}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("runs", nargs="*")
    ap.add_argument("--pair", nargs=2, metavar=("ATTACK", "TWIN"))
    ap.add_argument("--check", help="the cell's d61_check json (for the in-run gain)")
    ap.add_argument("--out")
    args = ap.parse_args(argv)
    try:
        if args.pair:
            out = pair(*args.pair, args.check)
        else:
            out = [_public(rebuild(r)) for r in args.runs]
    except CheckFailed as e:
        print(f"CHECK FAILED: {e}")
        return 2
    text = json.dumps(out, indent=1, default=str)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text)
    print(text if len(text) < 6000 else text[:6000] + "\n...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
