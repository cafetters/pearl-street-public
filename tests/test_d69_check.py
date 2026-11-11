"""bench.d69_check on synthetic runs: the honest check tolerates only the attestation lines, the attested_owners header
key and the matching message counts; the attack check needs exactly the five refusals and nothing else changed."""
import json
import shutil

import numpy as np

from bench import d69_check as C


def _runs(tmp_path):
    from sim.run_m1 import run_variant
    from tests.test_d62_guarantee import _cfg
    from tests.test_replay import synthetic_slice
    return run_variant, _cfg, synthetic_slice


def _folder(tmp_path, name, cfg):
    """A run folder like sim.run_m1.main writes: traces plus a minimal summary and netload."""
    from sim.run_m1 import bus_refusals
    run_variant, _, synthetic_slice = _runs(tmp_path)
    d = tmp_path / name
    s = synthetic_slice()
    run_variant(cfg, s, np.arange(2), d)
    r = run_variant(cfg, s, np.arange(2), d, headroom_mw=0.03)
    n_reg = sum(1 for m in r["log"] if m["msg_type"] == "asset_limits")
    summ = {"header": {"git_commit": r["header"]["git_commit"], "config": {"run_name": cfg["run_name"]}},
            "exchange_by_headroom_mw": {"0.03": {"messages_total": len(r["log"]), "bus_refusals": bus_refusals(r["refused"]),
                                                 "messages_per_day": {"2023-01-02": {"asset_limits": n_reg,
                                                                                     "tick": 5}}}}}
    (d / "summary.json").write_text(json.dumps(summ))
    (d / "netload.csv").write_text("x\n1\n")
    return d, r["header"]["git_commit"]


def test_honest_check_accepts_only_the_d69_additions(tmp_path):
    _, cfg_fn, _ = _runs(tmp_path)
    new, commit = _folder(tmp_path, "new", cfg_fn())
    base = tmp_path / "base"
    shutil.copytree(new, base)
    for p in base.glob("*/trace.jsonl"):           # a pre-D69 twin: no attestations, no attested_owners key
        lines = [json.loads(l) for l in p.read_text().splitlines()]
        lines[0].get("exchange", {}).pop("attested_owners", None)
        keep = [lines[0]] + [l for l in lines[1:] if not C.is_attestation(l)]
        p.write_text("\n".join(json.dumps(l, separators=(",", ":"), sort_keys=True) for l in keep) + "\n")
    s = json.loads((base / "summary.json").read_text())
    h = s["exchange_by_headroom_mw"]["0.03"]
    h["messages_total"] -= 2
    del h["messages_per_day"]["2023-01-02"]["asset_limits"]
    del h["bus_refusals"]                           # pre-D69 summaries have no refusal count
    (base / "summary.json").write_text(json.dumps(s))
    res = C.honest(base, new, commit)
    assert res["match"], res["diffs"] and res["attestations"] == 2
    t = next(new.glob("exchange_*/trace.jsonl"))       # any other change fails
    t.write_text(t.read_text().replace('"meter_kw":', '"meter_kw": ', 1))
    assert not C.honest(base, new, commit)["match"]


def test_attack_check_needs_exactly_the_five_refusals(tmp_path):
    _, cfg_fn, _ = _runs(tmp_path)
    C.ATTACKER, C.VICTIM = "A", "B"
    try:
        hon, commit = _folder(tmp_path, "hon", cfg_fn(run_name="hon"))
        atk, _ = _folder(tmp_path, "atk", {**cfg_fn(run_name="atk"),
                                           "attackers": [{"type": "impersonate", "agent": "A", "victim": "B"}]})
        res = C.attack(hon, atk, commit)
        assert res["match"], res["diffs"]
        assert not C.attack(hon, hon, commit)["match"]          # no refusals: the attack did not run
    finally:
        C.ATTACKER, C.VICTIM = "PVB_01", "PVB_02"


def test_summary_counts_refusals_by_reason_outside_the_message_counts(tmp_path):
    from sim.run_m1 import evaluate, run_variant
    from tests.test_replay import synthetic_slice
    _, cfg_fn, _ = _runs(tmp_path)
    s = synthetic_slice()
    rec = s.system["system_load"] * 1000.0
    cfg = {**cfg_fn(run_name="atk"), "attackers": [{"type": "impersonate", "agent": "A", "victim": "B"}]}
    sf = run_variant(cfg, s, np.arange(2), tmp_path / "atk")
    r = run_variant(cfg, s, np.arange(2), tmp_path / "atk", headroom_mw=0.03)
    ev = evaluate(cfg, s, np.arange(2), r, rec, None, sf)
    assert ev["bus_refusals"] == {"total": 5, "by_reason": {"direct_delivery": 1, "sender_mismatch": 4}}
    assert ev["messages_total"] == len(r["log"]) and ev["messages_total"] == sum(
        sum(d.values()) for d in ev["messages_per_day"].values())
    assert not any(m.get("reason") for m in r["log"])
    honest = run_variant(cfg_fn(run_name="hon"), s, np.arange(2), tmp_path / "hon", headroom_mw=0.03)
    assert evaluate(cfg_fn(run_name="hon"), s, np.arange(2), honest, rec, None, sf)["bus_refusals"] ==         {"total": 0, "by_reason": {}}
