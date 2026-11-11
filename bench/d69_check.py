"""
D69 full-run checks (overnight Nov 1 item 2, pre-registered in docs/overnight/2026-11-01.md; report only).

  honest: Jan 12-13 at m = 0 (configs/d62/h2h_c_jan12_h100_g.yaml, unchanged) on the D69 commit must reproduce the
          D62 v2 run exactly, apart from what D69 adds by design:
            - the metering service's owner attestations (`asset_limits` with msg_id metering:ownerN, one per roster
              agent, before any register, naming the agent and the legal entity it registers under), left out of
              the line-by-line comparison;
            - the header's exchange setting `attested_owners`;
            - the summary's message counts, which must be higher by exactly those attestations (asset_limits on the
              first day, and the total).
          Everything else as in bench/run_diff.py (text-identical message lines, bit-identical summary floats, the
          wall-clock and identity fields skipped, the run's commit in every header, full roster).
  attack: the same window and config with one impersonation attacker (attacks/impersonate.py; PVB_01 forging as the
          operator, as PVB_02, and as the exchange). Pass: the five forged messages are all refused by the bus (four
          sender_mismatch, one direct_delivery, origin PVB_01), nothing else is refused, and every other trace line and
          the summary match the honest D69 run (run_id and run_name aside, and the header's attacker list).

Run:  python -m bench.d69_check honest <v2_run_dir> <d69_run_dir> --commit SHA [--out f.json]
      python -m bench.d69_check attack <d69_run_dir> <attack_run_dir> --commit SHA [--out f.json]
"""
import argparse
import json
import re
import sys
from pathlib import Path

from bench.run_diff import SUMMARY_SKIP, _drop, diff_runs, first_diff

OWNER_ID = re.compile(r"metering:owner[0-9]+")
NOTICE_ID = re.compile(r"ex:refusal[0-9]+")
INJ_TEXT = "IGNORE PRIOR INSTRUCTIONS"
ATTACKER, VICTIM = "PVB_01", "PVB_02"
# the impersonation attack's refusals (D69) and, with refusal_notice on, its notices and D65 record (owner, Nov 3):
# 5 refusals, all recorded against the attacker's legal entity; 3 notices, because 3 of the 4 sender_mismatch
# forgeries share a minute of the exchange's clock and the notice is rate-limited to one per reason per minute
EXPECT_REFUSALS = {"direct_delivery": 1, "sender_mismatch": 4}
EXPECT_NOTICES = 3


def is_attestation(line: dict) -> bool:
    return line.get("msg_type") == "asset_limits" and isinstance(line.get("msg_id"), str) \
        and OWNER_ID.fullmatch(line["msg_id"]) is not None


def attestations(trace: Path) -> tuple[list[dict], int]:
    lines = [json.loads(s) for s in trace.read_text(encoding="utf-8").splitlines()]
    att = [l for l in lines[1:] if is_attestation(l)]
    first_reg = next((i for i, l in enumerate(lines) if l.get("msg_type") == "register"), len(lines))
    last_att = max((i for i, l in enumerate(lines) if is_attestation(l)), default=-1)
    return att, int(last_att < first_reg)


def honest(base: Path, new: Path, commit: str | None, header_skip_extra=(), adjust_extra=None) -> dict:
    """header_skip_extra and adjust_extra(base, new) -> (base, new): declared exclusions added by a caller
    (bench/d69_regress.py); the defaults change nothing."""
    trace = next(new.glob("exchange_*/trace.jsonl"))
    att, before = attestations(trace)
    roster = json.loads(trace.open(encoding="utf-8").readline())["roster"]
    n = len(att)
    problems = []
    lines = [json.loads(s) for s in trace.read_text(encoding="utf-8").splitlines()[1:]]
    entity = {l["payload"]["agent_id"]: l["payload"]["legal_entity"] for l in lines if l.get("msg_type") == "register"}
    if sorted(a["payload"].get("agent_id") for a in att) != sorted(roster):
        problems.append("attestations do not name exactly the roster")
    if any(a["payload"]["asset_id"] != f"{a['payload'].get('agent_id')}/bess" for a in att):
        problems.append("an attestation names another agent's asset")
    if any(a["payload"].get("owner") != entity.get(a["payload"].get("agent_id")) for a in att):
        problems.append("an attested owner is not the legal entity its agent registers under")
    if not before:
        problems.append("an attestation arrives after a register")

    def adjust(sa, sb):
        sb = json.loads(json.dumps(sb))
        for h in sb.get("exchange_by_headroom_mw", {}).values():
            mpd = h.get("messages_per_day") or {}
            first = sorted(mpd)[0] if mpd else None
            if first is not None and mpd[first].get("asset_limits") == n:
                del mpd[first]["asset_limits"]
            if isinstance(h.get("messages_total"), int):
                h["messages_total"] -= n
            if h.get("bus_refusals") == {"total": 0, "by_reason": {}}:
                del h["bus_refusals"]          # D69 adds the refusal count; an honest run has none
        if adjust_extra is not None:
            sa, sb = adjust_extra(sa, sb)
        return sa, sb

    res = diff_runs(base, new, commit, ignore_line=is_attestation, header_skip={"attested_owners", *header_skip_extra},
                    summary_adjust=adjust)
    res["attestations"] = n
    res["diffs"] = problems + res["diffs"]
    res["match"] = not res["diffs"]
    return res


def is_notice(line: dict) -> bool:
    """refusal_notice: the exchange's coded reject to the true origin of a refused message (own msg_id sequence)."""
    return line.get("msg_type") == "reject" and line.get("from") == "exchange" \
        and isinstance(line.get("msg_id"), str) and NOTICE_ID.fullmatch(line["msg_id"]) is not None


NOTICES: list = []          # the notices seen by the last notice_check (for the summary adjustment)


def notice_check(res: dict, rel: str, lines: list[str], kept: list[str]):
    """With refusal_notice on: one notice per (origin, reason, minute of the exchange's clock) among the refusals,
    each to the true origin (never the claimed sender or the victim), with the reason as its code, the constant text,
    refs {recorded: true}, a valid or null ref_msg_id, and none of the attacker's text."""
    from exchange.core import REFUSAL_DETAIL
    from exchange import messages as M
    tick, buckets = "", []
    for s in lines[1:]:
        l = json.loads(s)
        if l.get("msg_type") == "tick" and l.get("event") == "send" and l.get("to") == "exchange":
            tick = max(tick, l.get("t_utc") or "")
        if "reason" in l:
            b = (l["origin"], l["reason"], tick[:16])
            if b not in buckets:
                buckets.append(b)
    notices = [json.loads(s) for s in kept if is_notice(json.loads(s))]
    raw = [s for s in kept if is_notice(json.loads(s))]
    got = [(n["to"], n["payload"]["code"], (n.get("t_utc") or "")[:16]) for n in notices]
    if got != buckets:
        res["diffs"].append(f"{rel}: notices {got}, expected one per (origin, reason, minute) {buckets}")
    if len(notices) != EXPECT_NOTICES:
        res["diffs"].append(f"{rel}: {len(notices)} notices, expected {EXPECT_NOTICES}")
    for n, s in zip(notices, raw):
        p = n["payload"]
        if n["to"] != ATTACKER or n["to"] == VICTIM:
            res["diffs"].append(f"{rel}: a notice went to {n['to']}")
        if p.get("detail") != REFUSAL_DETAIL.get(p.get("code")) or p.get("refs") != {"recorded": True}:
            res["diffs"].append(f"{rel}: notice {n['msg_id']} payload {p}")
        if p.get("ref_msg_id") is not None and not M.is_id(p["ref_msg_id"]):
            res["diffs"].append(f"{rel}: notice {n['msg_id']} ref_msg_id not a valid id")
        if INJ_TEXT in s:
            res["diffs"].append(f"{rel}: notice {n['msg_id']} echoes the attacker's text")
    NOTICES[:] = notices
    res["checks"].append({"file": f"{rel} (notices)", "notices": len(notices), "expected_buckets": len(buckets),
                          "refusals": sum(1 for s in lines[1:] if "reason" in json.loads(s))})


def record_check(res: dict, rel: str, header: dict, kept: list[str]) -> list[str]:
    """refusal_notice on: the D65 record must hold every refusal (EXPECT_REFUSALS), not only the notified ones,
    against the attacker's legal entity, this run's and the record's, and nothing against anyone else (the
    impersonated agent is never charged). Needs delivery_record on, so it fails closed without it. Returns the kept
    lines with the refusals_by_reason fields taken out, for the line-by-line match with the honest twin (which runs
    with delivery_record on and refusal_notice off)."""
    if not (header.get("exchange") or {}).get("delivery_record"):
        res["diffs"].append(f"{rel}: refusal_notice is on but delivery_record is off, so the D65 record is not checked")
        return kept
    out, seen = [], set()
    for s in kept:
        line = json.loads(s)
        if line.get("msg_type") != "delivery_record" or line.get("from") != "exchange":
            out.append(s)
            continue
        p = line["payload"]
        want = EXPECT_REFUSALS if line["to"] == ATTACKER else {}
        for part in ("this_run", "record"):
            got = (p.get(part) or {}).pop("refusals_by_reason", None)
            if got != want:
                res["diffs"].append(f"{rel}: {line['to']} {part}.refusals_by_reason {got}, expected {want}")
        seen.add(line["to"])
        out.append(json.dumps(line, separators=(",", ":")))
    if ATTACKER not in seen:
        res["diffs"].append(f"{rel}: no delivery_record row for {ATTACKER}")
    res["checks"].append({"file": f"{rel} (D65 record)", "rows": len(seen),
                          "attacker_refusals": EXPECT_REFUSALS if ATTACKER in seen else None})
    return out


def _norm(line: str, rid_from: str, rid_to: str) -> str:
    d = json.loads(line)
    if d.get("run_id") == rid_from:
        d["run_id"] = rid_to
    return json.dumps(d, separators=(",", ":"), sort_keys=True)


def attack(honest_dir: Path, atk_dir: Path, commit: str | None) -> dict:
    NOTICES.clear()
    res = {"honest": str(honest_dir), "attack": str(atk_dir), "diffs": [], "checks": []}
    for rel in sorted(p.relative_to(atk_dir).as_posix() for p in atk_dir.glob("*/trace.jsonl")):
        a = (honest_dir / rel).read_text(encoding="utf-8").splitlines()
        b = (atk_dir / rel).read_text(encoding="utf-8").splitlines()
        ha, hb = json.loads(a[0]), json.loads(b[0])
        if commit is not None and hb.get("git_commit") != commit:
            res["diffs"].append(f"{rel}: header git_commit {hb.get('git_commit')!r}")
        skip = {"git_commit", "run_at_utc", "run_id", "run_name", "attackers", "refusal_notice"}
        d = first_diff(_drop(ha, skip), _drop(hb, skip), "header")
        if d:
            res["diffs"].append(f"{rel}: {d}")
        refused = [json.loads(s) for s in b[1:] if "reason" in json.loads(s)]
        kept = [s for s in b[1:] if "reason" not in json.loads(s)]
        if (hb.get("exchange") or {}).get("refusal_notice"):
            notice_check(res, rel, b, kept)
            kept = [s for s in kept if not is_notice(json.loads(s))]
            kept = record_check(res, rel, hb, kept)
        row = {"file": rel, "refused": len(refused), "lines_honest": len(a) - 1, "lines_kept": len(kept)}
        if "exchange" in hb:
            reasons = sorted(r["reason"] for r in refused)
            row.update({"reasons": reasons, "origins": sorted({r["origin"] for r in refused}),
                        "claimed": sorted(r["from"] for r in refused), "attackers": hb.get("attackers")})
            if reasons != ["direct_delivery"] + ["sender_mismatch"] * 4:
                res["diffs"].append(f"{rel}: refusals {reasons}, expected 4 sender_mismatch and 1 direct_delivery")
            if {r["origin"] for r in refused} != {ATTACKER}:
                res["diffs"].append(f"{rel}: refusals from {sorted({r['origin'] for r in refused})}")
            roster = hb.get("roster") or []
            missing = sorted(set(roster) - {json.loads(s).get("from") for s in kept})
            if missing:
                res["diffs"].append(f"{rel}: roster agents with no message: {missing[:10]}")
        elif refused:
            res["diffs"].append(f"{rel}: refusals in a run without an exchange")
        rid_a, rid_b = ha.get("run_id"), hb.get("run_id")
        if len(kept) != len(a) - 1:
            res["diffs"].append(f"{rel}: {len(kept)} kept lines vs {len(a) - 1} honest")
        for i, (x, y) in enumerate(zip(a[1:], kept), 1):
            if x != _norm(y, rid_b, rid_a):
                res["diffs"].append(f"{rel}: line {i}: {first_diff(json.loads(x), json.loads(_norm(y, rid_b, rid_a)))}")
                break
        res["checks"].append(row)
    sa = json.loads((honest_dir / "summary.json").read_text(encoding="utf-8"))
    sb = json.loads((atk_dir / "summary.json").read_text(encoding="utf-8"))
    want = {"total": 5, "by_reason": {"direct_delivery": 1, "sender_mismatch": 4}}
    if NOTICES:                      # the notices are delivered messages: take them out of the attack run's counts
        import pandas as pd
        from sim.run_m1 import TZ
        local_day = lambda t: str(pd.Timestamp(t).tz_convert(TZ).date())      # as sim.run_m1.evaluate
        for h in (sb.get("exchange_by_headroom_mw") or {}).values():
            if isinstance(h.get("messages_total"), int):
                h["messages_total"] -= len(NOTICES)
            for n in NOTICES:
                mpd = (h.get("messages_per_day") or {}).get(local_day(n["t_utc"]))
                if mpd is not None and mpd.get("reject"):
                    mpd["reject"] -= 1
                    if not mpd["reject"]:
                        del mpd["reject"]
                rbc = h.get("rejects_by_code") or {}
                if rbc.get(n["payload"]["code"]):
                    rbc[n["payload"]["code"]] -= 1
                    if not rbc[n["payload"]["code"]]:
                        del rbc[n["payload"]["code"]]
    for name, s, expect in (("honest", sa, {"total": 0, "by_reason": {}}), ("attack", sb, want)):
        for hk, h in (s.get("exchange_by_headroom_mw") or {}).items():
            got = h.pop("bus_refusals", None)
            if got != expect:
                res["diffs"].append(f"summary {name} {hk} bus_refusals {got}, expected {expect}")
            res["checks"].append({"file": f"summary.json ({name}, {hk})", "bus_refusals": got})
    skip_s = SUMMARY_SKIP | {"attackers", "refusal_notice"}
    d = first_diff(_drop(sa, skip_s), _drop(sb, skip_s), "summary")
    if d:
        res["diffs"].append(d)
    na, nb = (honest_dir / "netload.csv").read_bytes(), (atk_dir / "netload.csv").read_bytes()
    if na != nb:
        res["diffs"].append("netload.csv differs")
    res["match"] = not res["diffs"]
    return res


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["honest", "attack"])
    ap.add_argument("base")
    ap.add_argument("new")
    ap.add_argument("--commit")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    fn = honest if a.mode == "honest" else attack
    res = fn(Path(a.base), Path(a.new), a.commit)
    txt = json.dumps(res, indent=2, default=str)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(txt + "\n", encoding="utf-8")
    print(txt)
    return 0 if res["match"] else 1


if __name__ == "__main__":
    sys.exit(main())
