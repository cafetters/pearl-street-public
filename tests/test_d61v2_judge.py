"""bench/d61v2_judge.py: the thin D61 v2 judge (owner, Nov 7, D3/D4: gaps 1, 2 and 6). It combines d61_check (gain,
roster, defenses), bystander_check (D1: further below judged at section 8's tolerance, zero tolerance reported), D90
(infeasible_declare) and D87's voided_calls (misdeclare), and writes the draft's headline table.

Unit tests combine hand-built component results. The real-trace test re-judges the rehearsal cells and compares with
runs/nov07/v2r/judge.py's verdicts (it skips when they are absent)."""
import copy
import json
from pathlib import Path

import pytest

from bench import d61v2_judge as J

CFG5 = {"peak_response_margin_usd_mwh": "5.0", "settlement_rule": None, "declared_reference": "True"}


def _att(defense, fired=True, gain=-100.0, voided_calls=("GRID_OP:2",), counters=None, spec=None):
    c = {"above_power_steps": 3 if defense == "misdeclare_power" else 0,
         "below_reserve_steps": 3 if defense == "misdeclare_reserve" else 0, "voided_days": ["2023-01-13"],
         **(counters or {})}
    return {"spec": spec or {"agent": "DC_08", "type": defense.split("_")[0]}, "gain_usd": gain, "defense": defense,
            "defense_fired": fired, "status": "fired" if fired else "not fired", "counters": c,
            "d65": {"this_run": {"voided_calls": list(voided_calls) if voided_calls is not None else None}}}


def _c61(attackers, verdict="PASS", opp=None, harm10=0, losses=(3, -4.5), payout=(100.0, 120.0)):
    return {"verdict": verdict, "commit": "a" * 40, "attackers": attackers, "opportunity_steps": opp,
            "harm": {"agents_beyond_tolerance": harm10},
            "reported_not_judged": {"losses_vs_twin": {"agents": losses[0], "sum_negative_differences_usd": losses[1]}},
            "operator_payout_usd": {"attack": payout[0], "twin": payout[1], "diff": payout[0] - payout[1]}}


def _by(verdict="PASS", newly=0, judged=0, zero_n=0, zero_usd=0.0):
    return {"verdict": verdict, "further_below_tol": "section8",
            "newly_below_acting_alone": {f"N{i}": {} for i in range(newly)},
            "further_below_judged": {f"F{i}": {} for i in range(judged)},
            "zero_tolerance": {"further_below_count": zero_n, "further_below_usd": zero_usd,
                               "verdict": "FAIL" if (newly or zero_n) else "PASS"}}


def _ev(peak=2321.4, rebound=False, full=2400.0):
    return {"peak_mw": full, "controllable": {"peak_mw": peak, "pre_from_rebound": rebound, "pre_from_peak_mw": 2000.0,
                                              "share_of_m0_ceiling": 0.9}}


def _combine(attackers, c=None, by=None, d90=None, cfg=CFG5, **kw):
    return J.combine(c or _c61(attackers), by or _by(), d90, _ev(**kw), _ev(), cfg)


def test_a_misdeclare_cell_with_voided_calls_passes():
    r = _combine({"DC_08": _att("misdeclare_power")})
    assert r["verdict"] == "PASS" and r["judged"] is True
    assert r["attackers"]["DC_08"]["voided_calls"] == ["GRID_OP:2"]


@pytest.mark.parametrize("vc", [(), None])
def test_gap1_a_misdeclare_breach_without_voided_calls_fails_its_defense(vc):
    r = _combine({"DC_08": _att("misdeclare_reserve", voided_calls=vc)})
    a = r["attackers"]["DC_08"]
    assert r["verdict"] == "FAIL" and a["verdict"] == "FAIL" and a["parts"]["defense"] is False
    assert "voided_calls" in a["why"]


def test_misdeclare_outside_declared_reference_is_refused():
    with pytest.raises(J.Refused, match="declared_reference"):
        _combine({"DC_08": _att("misdeclare_power")}, cfg={**CFG5, "declared_reference": "False"})


@pytest.mark.parametrize("d90v, expect", [("NOT_EXERCISED", "NOT_EXERCISED"), ("PASS", "PASS"), ("FAIL", "FAIL"),
                                          ("DISAGREE", "FAIL")])
def test_infeasible_declare_is_judged_by_d90(d90v, expect):
    att = {"DC_08": _att("infeasible_declare", fired=(d90v != "NOT_EXERCISED"))}
    d90 = {"verdict": d90v, "attackers": {"DC_08": {"verdict": d90v, "infeasible_at_filing": d90v != "NOT_EXERCISED"}}}
    r = _combine(att, d90=d90)
    assert r["verdict"] == expect and r["attackers"]["DC_08"]["d90"] == d90v


def test_infeasible_declare_without_a_d90_result_is_incomplete():
    assert _combine({"DC_08": _att("infeasible_declare")}, d90=None)["verdict"] == "INCOMPLETE"


def test_renege_zero_b_with_no_opportunity_is_not_exercised_and_with_one_is_judged():
    att = {"DC_08": _att("renege_zero_b", fired=False)}
    assert _combine(att, c=_c61(att, opp=0))["verdict"] == "NOT_EXERCISED"
    assert _combine(att, c=_c61(att, opp=4))["verdict"] == "FAIL"


def test_a_gain_above_the_guard_fails():
    assert _combine({"DC_08": _att("silent", gain=2e-6)})["verdict"] == "FAIL"
    assert _combine({"DC_08": _att("silent", gain=1e-6)})["verdict"] == "PASS"


def test_d1_bystander_line_judged_at_section8_with_zero_tolerance_reported():
    att = {"DC_08": _att("silent")}
    r = _combine(att, by=_by("PASS", zero_n=1, zero_usd=0.21))
    assert r["verdict"] == "PASS"
    assert r["bystander"]["zero_tolerance_further_below"] == {"n": 1, "usd": 0.21, "verdict": "FAIL"}
    assert _combine(att, by=_by("FAIL", judged=1))["verdict"] == "FAIL"
    assert _combine(att, by=_by("FAIL", newly=1))["verdict"] == "FAIL"


def test_bystander_not_on_section8_is_refused():
    with pytest.raises(J.Refused, match="section8"):
        _combine({"DC_08": _att("silent")}, by={**_by(), "further_below_tol": "zero"})


def test_gap6_a_multi_attacker_cell_is_judged_per_attacker():
    att = {"DC_08": _att("renege_zero_b", fired=False, spec={"agent": "DC_08", "type": "renege", "mode": "zero_b"}),
           "PVB_48": _att("silent", spec={"agent": "PVB_48", "type": "silent"}),
           "DC_03": _att("misdeclare_reserve", spec={"agent": "DC_03", "type": "misdeclare", "mode": "reserve"})}
    r = _combine(att, c=_c61(att, verdict="NOT_EXERCISED", opp=0))
    v = {a: x["verdict"] for a, x in r["attackers"].items()}
    assert v == {"DC_08": "NOT_EXERCISED", "PVB_48": "PASS", "DC_03": "PASS"} and r["verdict"] == "PASS"
    att["DC_03"]["gain_usd"] = 5.88
    r = _combine(att, c=_c61(att, verdict="NOT_EXERCISED", opp=0))
    assert r["verdict"] == "FAIL" and r["attackers"]["DC_03"]["verdict"] == "FAIL"
    assert r["attackers"]["PVB_48"]["verdict"] == "PASS"


def test_simulation_failures_pass_through():
    for v in ("INTEGRITY_FAIL", "TWIN_FAIL"):
        r = J.combine({"verdict": v, "integrity": ["x"]}, None, None, _ev(), _ev(), CFG5)
        assert r["verdict"] == v


def test_m0_and_max_rule_cells_are_reported_not_judged():
    att = {"DC_08": _att("silent")}
    assert _combine(att, cfg={**CFG5, "peak_response_margin_usd_mwh": "0.0"})["judged"] is False
    assert _combine(att, cfg={**CFG5, "settlement_rule": "max_first_call"})["judged"] is False
    assert _combine(att, cfg={**CFG5, "settlement_rule": "today"})["judged"] is True


def test_gap2_the_headline_row():
    att = {"DC_08": _att("silent")}
    r = _combine(att, c=_c61(att, harm10=2, losses=(7, -4.2), payout=(561.3, 100.0)), peak=2325.2, rebound=True)
    h = r["headline"]
    assert h["twin_rel_loss"] == {"n": 7, "usd": -4.2}
    assert h["peak_ctrl_mw"] == 2325.2 and h["peak_ctrl_twin_mw"] == 2321.4
    assert h["pre_from_rebound"] is True and h["pre_from_rebound_twin"] is False
    assert h["operator_payout_vs_twin_usd"] == pytest.approx(461.3)
    assert h["d61_harm10_agents"] == 2
    assert h["peak_full_mw"] == 2400.0 and h["peak_full_twin_mw"] == 2400.0


def test_twin_for_maps_smoke_and_attack_cells():
    assert J.twin_for("h2h_c_d61v2smoke_sep05_all_m5") == "h2h_c_d61v2smoke_sep05_twin_m5"
    assert J.twin_for("h2h_c_d61v2_dec11_silent_deviate_m5") == "h2h_c_d61v2_dec11_twin_m5"
    assert J.twin_for("h2h_c_v2r_jan12_renege_zero_b_m0_max") == "h2h_c_v2r_jan12_twin_m0_max"
    with pytest.raises(ValueError):
        J.twin_for("h2h_c_d61v2_dec11_twin_m5")


def test_the_table_has_a_row_per_cell_and_every_headline_column():
    att = {"DC_08": _att("silent")}
    rows = [{"cell": "a_silent_m5", **_combine(att)}, {"cell": "b_silent_m5", **_combine(att, by=_by("FAIL", newly=1))}]
    md = J.table(rows)
    lines = md.strip().splitlines()
    assert len(lines) == 4
    for col in ("twin-rel. loss", "ctrl peak", "rebound", "operator payout", "D61 $10", "zero-tol further below",
                "voided_calls", "D90"):
        assert col in lines[0], col


# ---- real traces: the rehearsal cells against runs/nov07/v2r/judge.py (report only) --------------------------------

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT.parent / "pearl-street"
REH = MAIN / "runs" / "nov07" / "v2r" / "judge.json"
RUNS = MAIN / "runs" / "cloud"


D471 = ROOT.parent / "pearl-street-d471"          # a clean checkout of d471a72, the rehearsal's commit (for D90)
DATA = ROOT.parent / "data"


@pytest.mark.skipif(not REH.exists() or not D471.exists(), reason="needs the rehearsal judge's output and d471a72")
def test_real_trace_matches_the_rehearsal_judge_except_where_d1_changes_it():
    old = {r["cell"]: r for r in json.loads(REH.read_text()) if "parts" in r}
    assert old
    diffs = []
    for cell, o in old.items():
        new = J.judge_cell(RUNS / cell, RUNS / J.twin_for(cell), d90_code_root=D471, data_dir=DATA)
        if new["verdict"] == o["v2_verdict"]:
            continue
        diffs.append(cell)
        # the only allowed difference: a FAIL on the bystander line alone, at zero tolerance, that D1 passes
        assert o["v2_verdict"] == "FAIL" and new["verdict"] == "PASS", cell
        assert not o["parts"]["no_bystander_harm"] and all(v for k, v in o["parts"].items() if k != "no_bystander_harm")
        assert new["bystander"]["verdict"] == "PASS" and new["bystander"]["zero_tolerance_further_below"]["n"] > 0
        assert new["bystander"]["newly_below_n"] == 0
