"""D61 pass check for one attack cell against its honest twin (M3 plan step 7; pre-registered in
docs/plans/d61_attacks_check.md before any D61 run).

    python -m bench.d61_check <attack_run_dir> <twin_run_dir> [--out file.json]

A run dir is a sim/run_m1.py output folder (summary.json plus exchange_h<H>*/trace.jsonl). Everything is read from
the two folders; nothing is recomputed from data.

Integrity (fails closed: verdict INTEGRITY_FAIL):
  - both headers name the same git commit, and neither was run from a dirty tree;
  - the two trace headers differ only in keys named `run_name`, `run_id`, `run_at_utc` or `attackers`, at any level
    (the header repeats `run_id` under `exchange`, and `run_name` and `attackers` under `config`); everything else in
    the header, the config included, is equal. Summaries are not compared: an attack is supposed to change them;
  - `d62_guarantee` resolves to false in both;
  - the attack header's `attackers` is non-empty and the twin's is absent;
  - roster: each run's roster comes from its header, and every roster agent has a value in its summary
    (`agent_value_usd`); the two rosters are equal (CLAUDE.md: a missing agent fails the check).
Twin behaviour (TWIN_FAIL, a simulation failure): the twin has no attackers, files no declarations and gets no
`infeasible_declaration`, and every would-be attacker nominated every binding call (D65 `binding_calls_missed` 0).
Judged (D61, at the cell's margin):
  - gain: each attacker's value minus its value in the twin; above GAIN_TOL_USD counts as a gain;
  - harm: every other agent, twin value minus attack value, against a flat HARM_TOL_USD ($10) per agent (owner,
    Nov 4: the twin removes forecast noise, so section 8's 1% of |value| part is dropped);
  - the named defense for the attack type fires (DEFENSES).
Zero-price-B reneging (owner, Nov 5): a window with no opportunity in the twin's trace (a step an earlier binding call
priced above $0 and a later one priced at $0) gives verdict NOT_EXERCISED (reported, not judged); with an opportunity,
an attack that didn't act fails its defense part. Per declaring attacker (item 6): whether its declarations moved any
final price before the void took effect, and by how much (price_moved_before_void).
Reported, zero or not: gain and harm per agent, harm count and $ beyond tolerance, every defense counter, operator
payout against the twin, the attacker's D65 rows. Reported, not judged (owner, Nov 4): the total loss to all other
agents against the twin at zero tolerance (how many agents have attack value below twin value, and the sum of those
negative differences) and the change in operator payout (attack minus twin). A defense that didn't fire because the attack never acted (no
dropped nomination, no declaration filed, no meter difference from the twin) is reported `inert`, and counts as not
firing.
"""
import json
import sys
from pathlib import Path

from bench.tolerances import GAIN_GUARD_USD, HARM_TOL_USD     # condition 3 (owner, Nov 5): the tolerances' home

GAIN_TOL_USD = GAIN_GUARD_USD       # float guard (pre-registered): above it counts as a gain
HEADER_KEYS_ALLOWED_TO_DIFFER = ("run_name", "run_id", "run_at_utc", "attackers")     # at any level of the header
METER_TOL_KW = 1e-6
# Proposed amendment (Nov 6, item 6; staged D89; not merged): an attack cell and its twin may have run on two
# machines (laptop and Maritime sandbox), whose run headers record different absolute data folders. Only this
# header path may then differ, only when both values are absolute paths; anything else still fails closed.
DATA_DIR_PATH = "config.data_dir"


def _trace(run_dir: Path) -> Path:
    paths = sorted(run_dir.glob("exchange_h*/trace.jsonl"))
    if len(paths) != 1:
        raise ValueError(f"{run_dir}: expected one exchange trace, found {len(paths)}")
    return paths[0]


def load_run(run_dir) -> dict:
    """Header, per-agent values and the exchange trace's messages of one run folder."""
    run_dir = Path(run_dir)
    summary = json.loads((run_dir / "summary.json").read_text())
    (h, ev), = summary["exchange_by_headroom_mw"].items()
    lines = _trace(run_dir).read_text().splitlines()
    header = json.loads(lines[0])
    log = [json.loads(x) for x in lines[1:]]
    log = [{**m, "sender": m.get("sender", m.get("from"))} for m in log if "msg_type" in m]   # trace: "from"
    return {"dir": str(run_dir), "header": header, "summary_header": summary["header"], "ev": ev,
            "values": ev.get("agent_value_usd"), "log": log, "roster": list(header.get("roster") or [])}


def _cfg(run) -> dict:
    """The run's config as the header records it (every value as a string, bench/runinfo.run_header)."""
    return dict(run["header"].get("config") or {})


def header_diffs(a, b, path: str = "") -> list[str]:
    """Paths where two trace headers differ, skipping keys in HEADER_KEYS_ALLOWED_TO_DIFFER at any level."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            if k in HEADER_KEYS_ALLOWED_TO_DIFFER:
                continue
            p = f"{path}.{k}" if path else str(k)
            out += [p] if (k in a) != (k in b) else header_diffs(a[k], b[k], p)
        return out
    return [] if a == b else [path]


def _absolute(v) -> bool:
    """An absolute path on Windows (drive and root) or POSIX; never a relative path, an empty value or a non-string."""
    from pathlib import PurePosixPath, PureWindowsPath
    return isinstance(v, str) and bool(v) and (PureWindowsPath(v).is_absolute() or PurePosixPath(v).is_absolute())


def integrity(atk: dict, twin: dict) -> list[str]:
    bad = []
    ha, ht = atk["header"], twin["header"]
    if ha.get("git_commit") != ht.get("git_commit"):
        bad.append(f"commits differ: {ha.get('git_commit')} vs {ht.get('git_commit')}")
    for name, h in (("attack", ha), ("twin", ht)):
        if str(h.get("git_commit", "")).endswith("-dirty") or not h.get("git_commit"):
            bad.append(f"{name} run from a dirty tree or with no commit")
    diff = header_diffs(ha, ht)
    if (DATA_DIR_PATH in diff and _absolute((ha.get("config") or {}).get("data_dir"))
            and _absolute((ht.get("config") or {}).get("data_dir"))):
        diff = [d for d in diff if d != DATA_DIR_PATH]      # two machines: allowed (D89, proposed)
    if diff:
        bad.append(f"headers differ beyond {'/'.join(HEADER_KEYS_ALLOWED_TO_DIFFER)}: {diff[:10]}")
    ca, ct = _cfg(atk), _cfg(twin)
    for name, c in (("attack", ca), ("twin", ct)):
        if str(c.get("d62_guarantee", "False")).strip().lower() not in ("false", "0", "no", "none", ""):
            bad.append(f"{name}: d62_guarantee resolves to true")
    if not ha.get("attackers"):
        bad.append("attack header has no attackers")
    if ht.get("attackers"):
        bad.append("twin header has attackers")
    for name, r in (("attack", atk), ("twin", twin)):
        if not r["roster"]:
            bad.append(f"{name}: no roster in the header")
        elif r["values"] is None:
            bad.append(f"{name}: summary has no agent_value_usd")
        else:
            missing = sorted(set(r["roster"]) - set(r["values"]))
            if missing:
                bad.append(f"{name}: roster agents missing from the check: {missing[:10]}")
    if atk["roster"] != twin["roster"]:
        bad.append("rosters differ")
    return bad


def _rows(run, to=None, typ="delivery_record"):
    return [m for m in run["log"] if m["msg_type"] == typ and (to is None or m["to"] == to)]


def _last_record(run, aid) -> dict:
    rows = _rows(run, aid)
    return rows[-1]["payload"] if rows else {}


def twin_behaviour(twin: dict, attackers: list[str]) -> list[str]:
    bad = []
    if twin["header"].get("attackers"):
        bad.append("twin has attackers")
    decl = [m for m in twin["log"] if m["msg_type"] in ("declare_availability", "declare_outage")]
    if decl:
        bad.append(f"twin filed {len(decl)} declarations")
    if any(m["msg_type"] == "reject" and m["payload"].get("code") == "infeasible_declaration" for m in twin["log"]):
        bad.append("twin got infeasible_declaration")
    for aid in attackers:
        missed = (_last_record(twin, aid).get("this_run") or {}).get("binding_calls_missed")
        if missed is None:
            bad.append(f"twin: no delivery record for {aid}")
        elif missed:
            bad.append(f"twin: {aid} missed {missed} binding calls")
    return bad


def _meter(run, asset_prefix):
    return {m["t_utc"]: m["payload"]["meter_kw"] for m in run["log"]
            if m["msg_type"] == "telemetry" and m["payload"]["asset_id"].startswith(asset_prefix + "/")}


def _moves(run) -> bool:
    by = {}
    for m in run["log"]:
        if m["msg_type"] == "peak_price":
            by.setdefault(m["payload"]["call_id"], set()).add(tuple(m["payload"]["price_usd_mwh"]))
    return any(len(v) > 1 for v in by.values())


def _binding_finals(run) -> list[tuple[str, str, dict]]:
    """(call_id, t_utc, {step t_utc: final price}) for each binding call, in the order its final price went out."""
    binding = {m["payload"]["call_id"]: m["payload"]["binding"] for m in run["log"]
               if m["msg_type"] == "call_nominations" and "call_id" in m["payload"]}
    steps = sorted({m["t_utc"] for m in run["log"] if m["msg_type"] == "telemetry"})
    out = []
    for m in run["log"]:
        p = m["payload"]
        if m["msg_type"] == "peak_price" and p.get("final") and binding.get(p["call_id"]):
            w = p["window"]
            ts = [t for t in steps if w["start_utc"] <= t < w["end_utc"]]
            out.append((p["call_id"], m["t_utc"], dict(zip(ts, p["price_usd_mwh"]))))
    return out


def opportunity_steps(twin) -> list[str]:
    """Owner, Nov 5 (zero-price-B variant), from the honest twin's trace: a step that an earlier binding call priced
    above $0 and a later binding call priced at $0."""
    seen_pos, hit = set(), set()
    for _, _, prices in _binding_finals(twin):
        for t, v in prices.items():
            if v == 0.0 and t in seen_pos:
                hit.add(t)
        seen_pos |= {t for t, v in prices.items() if v > 0.0}
    return sorted(hit)


def price_moved_before_void(atk, twin, aid, tz) -> dict | None:
    """Owner, Nov 5 (item 6): did the attacker's declarations move any price before the void took effect, and by how
    much? Binding calls whose final price went out after the attacker's first declaration and before the end of its
    last voided local day (the slice's end if none was voided): steps where the attack's final price differs from the
    twin's, and the largest difference ($/MWh). In a one-attacker cell every difference is the attacker's."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    decl = [m["t_utc"] for m in atk["log"] if m["sender"] == aid
            and m["msg_type"] in ("declare_availability", "declare_outage")]
    if not decl:
        return None
    voided = (_last_record(atk, aid).get("this_run") or {}).get("voided_days") or []
    if voided:
        day = datetime.fromisoformat(max(voided)) + timedelta(days=1)
        end = day.replace(tzinfo=ZoneInfo(tz)).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        end = "9999"
    tw = {cid: pr for cid, _, pr in _binding_finals(twin)}
    calls, steps, mx = [], 0, 0.0
    for cid, t, pr in _binding_finals(atk):
        if not (min(decl) <= t < end) or cid not in tw:
            continue
        d = [abs(v - tw[cid].get(s, v)) for s, v in pr.items()]
        n = sum(1 for x in d if x > 0.0)
        calls.append({"call_id": cid, "final_utc": t, "steps_moved": n, "max_abs_usd_mwh": max(d, default=0.0)})
        steps += n
        mx = max(mx, max(d, default=0.0))
    return {"until_utc": end, "calls": calls, "steps_moved": steps, "max_abs_usd_mwh": mx}


def _peak_net(run, aid) -> float:
    return sum(m["payload"]["net_usd"] for m in _rows(run, aid, "peak_settle"))


def counters(atk: dict, twin: dict, aid: str) -> dict:
    rec = _last_record(atk, aid)
    this, record = rec.get("this_run") or {}, rec.get("record") or {}
    imb_rows = [m["payload"] for m in _rows(atk, aid, "imbalance")]          # one per binding call: summed
    imb = {k: sum(r.get(k, 0.0) for r in imb_rows) for k in ("deviation_kwh", "charge_usd")}
    sent = [m for m in atk["log"] if m["sender"] == aid]
    sent_twin = [m for m in twin["log"] if m["sender"] == aid]
    ma, mt = _meter(atk, aid), _meter(twin, aid)
    return {
        "nominations_sent": sum(m["msg_type"] == "nominate" for m in sent),
        "nominations_sent_twin": sum(m["msg_type"] == "nominate" for m in sent_twin),
        "declarations_filed": sum(m["msg_type"] in ("declare_availability", "declare_outage") for m in sent),
        "infeasible_declaration_rejects": sum(1 for m in _rows(atk, aid, "reject")
                                              if m["payload"].get("code") == "infeasible_declaration"),
        "meter_steps_differ_from_twin": sum(1 for t in ma if abs(ma[t] - mt.get(t, 0.0)) > METER_TOL_KW),
        "imbalance_deviation_kwh": imb.get("deviation_kwh", 0.0),
        "imbalance_charge_usd": imb.get("charge_usd", 0.0),
        "binding_calls_missed": this.get("binding_calls_missed"),
        "late_outages": record.get("late_outages"),
        "outage_activity_steps": record.get("outage_activity_steps"),
        "below_reserve_steps": record.get("below_reserve_steps"),
        "above_power_steps": record.get("above_power_steps"),
        "infeasible_declaration_calls": record.get("infeasible_declaration_calls"),
        "voided_days": this.get("voided_days"),
        "price_moves": _moves(atk),
        "peak_settle_usd": _peak_net(atk, aid),
        "peak_settle_usd_twin": _peak_net(twin, aid),
        "price_moved_before_void": price_moved_before_void(atk, twin, aid,
                                                           _cfg(atk).get("slice_tz") or "America/Chicago"),
    }


def _acted(kind: str, c: dict) -> bool:
    if kind == "silent":
        return c["nominations_sent"] < c["nominations_sent_twin"]
    if kind in ("outage", "misdeclare", "infeasible_declare"):
        return c["declarations_filed"] > 0
    return c["meter_steps_differ_from_twin"] > 0                    # renege


# The named defense per attack (pre-registered). Each takes the attacker's counters and its spec.
DEFENSES = {
    "renege": ("imbalance beyond the band at the scarcity price, or nothing paid for relief not metered "
               "(the attacker's D65 within-band share falls)",
               lambda c, s: c["imbalance_charge_usd"] > 0.0 or c["meter_steps_differ_from_twin"] > 0),
    "silent": ("D78/D79: the price keeps moving and D65 counts every skipped binding call as missed",
               lambda c, s: c["price_moves"] and (c["binding_calls_missed"] or 0) > 0),
    "silent_deviate": ("D78: imbalance charged against the 1p reference on bound steps",
                       lambda c, s: c["imbalance_deviation_kwh"] > 0.0 and (c["binding_calls_missed"] or 0) > 0),
    "outage_stop": ("D80: imbalance on bound steps is never excused; D65 counts the outage late",
                    lambda c, s: (c["late_outages"] or 0) > 0 and c["imbalance_charge_usd"] > 0.0),
    "outage_run": ("D81: battery activity during the declared outage is a breach and voids the day; counted late",
                   lambda c, s: (c["late_outages"] or 0) > 0 and (c["outage_activity_steps"] or 0) > 0
                   and bool(c["voided_days"])),
    "misdeclare_power": ("D81: output above declared power is a breach and voids the day",
                         lambda c, s: (c["above_power_steps"] or 0) > 0 and bool(c["voided_days"])),
    "misdeclare_reserve": ("D81: SOC below declared reserve is a breach and voids the day",
                           lambda c, s: (c["below_reserve_steps"] or 0) > 0 and bool(c["voided_days"])),
    "infeasible_declare": ("D82: the reserve is rejected or voided with infeasible_declaration and the run completes",
                           lambda c, s: c["infeasible_declaration_rejects"] > 0),
    "renege_zero_b": ("today's 1p settles each step at its last binding call's price, so relief withdrawn at a $0 B "
                      "earns nothing: the attacker's peak settlement is no more than the twin's",
                      lambda c, s: c["peak_settle_usd"] <= c["peak_settle_usd_twin"] + GAIN_TOL_USD),
}


def defense_key(spec: dict) -> str:
    kind = spec["type"]
    if kind == "silent":
        return "silent_deviate" if spec.get("mode") == "deviate" else "silent"
    if kind == "outage":
        return f"outage_{spec['mode']}"
    if kind == "misdeclare":
        return f"misdeclare_{spec['mode']}"
    if kind == "renege" and spec.get("mode") == "zero_b":
        return "renege_zero_b"
    return kind


def check_pair(atk: dict, twin: dict) -> dict:
    out = {"attack_dir": atk["dir"], "twin_dir": twin["dir"], "commit": atk["header"].get("git_commit")}
    bad = integrity(atk, twin)
    if bad:
        return {**out, "verdict": "INTEGRITY_FAIL", "integrity": bad}
    specs = list(atk["header"]["attackers"])
    attackers = [s["agent"] for s in specs]
    tw = twin_behaviour(twin, attackers)
    if tw:
        return {**out, "verdict": "TWIN_FAIL", "twin": tw}
    va, vt = atk["values"], twin["values"]
    opp = opportunity_steps(twin)
    zero_b = [s for s in specs if defense_key(s) == "renege_zero_b"]
    per_attacker = {}
    for s in specs:
        aid = s["agent"]
        c = counters(atk, twin, aid)
        key = defense_key(s)
        what, fired = DEFENSES[key]
        acted = _acted(s["type"], c)
        per_attacker[aid] = {"spec": s, "gain_usd": va[aid] - vt[aid], "defense": key, "defense_text": what,
                             "acted": acted, "defense_fired": bool(acted and fired(c, s)),
                             "status": "fired" if acted and fired(c, s) else ("inert" if not acted else "not fired"),
                             "counters": c, "d65": _last_record(atk, aid)}
    harm, beyond = {}, {}
    for aid in atk["roster"]:
        if aid in attackers:
            continue
        short = vt[aid] - va[aid]
        harm[aid] = short
        if short > HARM_TOL_USD:
            beyond[aid] = short - HARM_TOL_USD
    losses = {aid: -short for aid, short in harm.items() if short > 0.0}      # attack below twin, no tolerance
    pay = lambda r: ((r["ev"].get("price_mode") or {}).get("operator_net_payout_usd"))
    gains = {a: p["gain_usd"] for a, p in per_attacker.items()}
    ok_gain = all(g <= GAIN_TOL_USD for g in gains.values())
    ok_harm = not beyond
    ok_def = all(p["defense_fired"] for p in per_attacker.values())
    verdict = "PASS" if ok_gain and ok_harm and ok_def else "FAIL"
    if zero_b and not opp:          # owner, Nov 5: no opportunity in the window -> reported, not judged
        verdict = "NOT_EXERCISED"
    return {**out, "verdict": verdict, "opportunity_steps": len(opp) if zero_b else None,
            "pass_parts": {"gain": ok_gain, "no_harm": ok_harm, "defenses": ok_def, "roster": True},
            "attackers": per_attacker,
            "harm": {"agents_beyond_tolerance": len(beyond), "usd_beyond_tolerance": sum(beyond.values()),
                     "by_agent_beyond_usd": beyond, "max_short_usd": max(harm.values()) if harm else 0.0},
            "reported_not_judged": {"losses_vs_twin": {"agents": len(losses), "sum_negative_differences_usd":
                                                       sum(losses.values())},
                                    "operator_payout_change_usd": (pay(atk) - pay(twin)) if pay(atk) is not None
                                    and pay(twin) is not None else None},
            "operator_payout_usd": {"attack": pay(atk), "twin": pay(twin),
                                    "diff": (pay(atk) - pay(twin)) if pay(atk) is not None and pay(twin) is not None
                                    else None}}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    out = None
    if "--out" in argv:
        i = argv.index("--out")
        out, argv = argv[i + 1], argv[:i] + argv[i + 2:]
    res = check_pair(load_run(argv[0]), load_run(argv[1]))
    text = json.dumps(res, indent=1, default=str)
    if out:
        Path(out).write_text(text)
    print(json.dumps({k: res[k] for k in ("verdict",) if k in res}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
