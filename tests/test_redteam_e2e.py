"""The red-team harness end to end on the fake backend (the M5 acceptance test) [sim, synthetic].

Round 1 (today's exchange: both M5 flags off, the gateway's grant off) finds the planted fake exploit
(over-registration), triage flags it, a deterministic replay confirms it, the ablation control gives the attributable
gain, each defense layer is tried, and the entry goes to a (temporary) catalog. Round 2 (production settings) runs
the regression gate on that entry first, which must show it blocked, with refusal logging (a refusing fake), the
spend reserved and released against the phase cap (D94), the D93 draw and the shared cache (D95)."""
import json

import pytest

from agents.llm.spend import CapReached, PhaseAllocator
from redteam.fake import FakeAttacker
from redteam.rounds import run_round

MODELS = [{"provider": "anthropic", "model": "claude-sonnet-5-5", "weight": 0.9},
          {"provider": "anthropic", "model": "claude-opus-5-5", "weight": 0.1}]


def test_two_rounds_find_confirm_catalog_and_regress(tmp_path):
    cat = tmp_path / "catalog"
    phase = PhaseAllocator(2.0)
    rc = {"name": "e2e_open", "type": "open_ended", "attackers": ["A"], "seed": 1, "attacker_models": MODELS,
          "gateway_limits": {"grant_limits": False}, "flags": {}, "cap_usd_round": 1.0,
          "cache": {"mode": "record", "path": str(tmp_path / "r1" / "llm_cache.jsonl")}}
    r1 = run_round(rc, tmp_path / "r1", lambda a, d: FakeAttacker("over_register"), phase=phase, catalog_dir=cat)
    assert r1["regression_gate"] == []                                  # empty catalog
    assert r1["header"]["git_commit"] and r1["header"]["seed"] == 1
    assert r1["draws"]["A"]["model"] in ("claude-sonnet-5-5", "claude-opus-5-5") and 0 <= r1["draws"]["A"]["u"] < 1
    tri = r1["triage"]
    assert tri["candidate"] and tri["reasons"] == ["gain:A"] and tri["roster"] == ["A", "B", "C"]
    assert r1["replay_reproduced"] is True
    (e,) = r1["confirmed"]
    assert e["id"] == "rt-0000" and (cat / "rt-0000.json").exists()
    assert e["class"] == "exploit" and e["class_rule"].startswith("D-d")          # the code rule, not a judgment
    assert e["triage"]["attributable_gain_usd"]["A"] == pytest.approx(400.0, abs=0.01)
    blocked = {d["name"]: (d["blocked"], d["blocked_by"]) for d in e["defenses"]}
    assert blocked == {"gateway_grant": (True, "no_gain_no_harm"), "reference_power_cap": (True, "no_gain_no_harm"),
                       "register_power_check": (True, "run_stopped_at_registration")}
    assert e["found_by"]["A"]["backend"] == "fake_attacker:over_register"
    assert r1["spend"]["spent_usd"] > 0 and not r1["spend"]["cap_hit"]          # fake calls priced at the D94 table
    assert r1["cache"]["misses"] > 0 and r1["cache"]["sha256"]
    assert r1["refusal_rate_by_model"][f"anthropic/{r1['draws']['A']['model']}"]["rate"] == 0.0
    assert phase.reserved == {} and phase.spent == pytest.approx(r1["spend"]["spent_usd"])

    rc2 = {**rc, "name": "e2e_prod", "gateway_limits": {}, "cache": {"mode": "off"},
           "flags": {"register_power_check": True, "reference_power_cap": True, "silent_reference": True}}
    r2 = run_round(rc2, tmp_path / "r2", lambda a, d: FakeAttacker("refuser"), phase=phase, catalog_dir=cat,
                   write_catalog=False)
    (g,) = r2["regression_gate"]
    assert g["id"] == "rt-0000" and g["blocked"] is True and g["attributable_gain_usd"]["A"] <= 0.01
    m = f"anthropic/{r2['draws']['A']['model']}"
    assert r2["refusal_rate_by_model"][m]["rate"] == 1.0 and r2["attackers"]["A"]["refusals"] == 30
    assert json.loads((tmp_path / "r2" / "round_report.json").read_text())["round"] == "e2e_prod"


def test_a_round_that_does_not_fit_the_phase_cap_is_refused(tmp_path):
    phase = PhaseAllocator(0.5)
    rc = {"name": "too_big", "type": "open_ended", "attackers": ["A"], "seed": 1, "attacker_models": MODELS,
          "cap_usd_round": 1.0}
    with pytest.raises(CapReached):
        run_round(rc, tmp_path, lambda a, d: FakeAttacker("honest_like"), phase=phase, catalog_dir=tmp_path / "c")


def test_the_round_cap_fails_closed(tmp_path):
    """D94 per round: a tiny cap stops the attacker's calls; it goes silent for the rest of the run."""
    rc = {"name": "tiny", "type": "open_ended", "attackers": ["A"], "seed": 1, "attacker_models": MODELS,
          "cap_usd_round": 0.002, "flags": {"silent_reference": True}}
    r = run_round(rc, tmp_path, lambda a, d: FakeAttacker("honest_like"), catalog_dir=tmp_path / "c",
                  write_catalog=False)
    assert r["spend"]["cap_hit"] and r["attackers"]["A"]["cap_hit"] and r["spend"]["spent_usd"] <= 0.002
