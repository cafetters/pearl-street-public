"""bench/bystander_check (c-bystander, staged D88): honest agents only, acting-alone basis from the summary, zero
tolerance; fails closed on integrity. The real-trace check is the 8 D61 silent cells (runs/nov06/silent)."""
import json

import pytest

from bench.bystander_check import CheckFailed, check

ROSTER = ["A", "B", "C", "X"]


def _run(tmp_path, name, harm, value, attackers=(), commit="abc1234", roster=ROSTER):
    d = tmp_path / name
    (d / "exchange_h100_rinfo").mkdir(parents=True)
    header = {"git_commit": commit, "roster": list(roster), "attackers": [{"agent": a} for a in attackers]}
    (d / "exchange_h100_rinfo" / "trace.jsonl").write_text(json.dumps(header) + "\n")
    ev = {"net_harm": {"by_agent_usd": harm}, "agent_value_usd": value}
    (d / "summary.json").write_text(json.dumps({"exchange_by_headroom_mw": {"100": ev}}))
    return d


def test_an_honest_agent_pushed_below_acting_alone_fails_and_the_attacker_is_left_out(tmp_path):
    twin = _run(tmp_path, "twin", {"A": 0.0, "B": 2.0, "C": 0.0, "X": 0.0}, {"A": 10, "B": 5, "C": 7, "X": 3})
    atk = _run(tmp_path, "atk", {"A": 0.01, "B": 1.0, "C": 0.0, "X": 500.0}, {"A": 9, "B": 6, "C": 7, "X": -400},
               attackers=["X"])
    r = check(atk, twin)
    assert r["verdict"] == "FAIL"
    assert list(r["newly_below_acting_alone"]) == ["A"]                  # zero tolerance: 1 cent counts
    assert r["attackers_below_acting_alone"] == {"X": 500.0}
    assert r["honest_below_acting_alone_in_twin"] == 1 and r["of_which_better_in_attack"] == 1
    assert r["reported_not_judged"]["twin_relative_loss"] == {"agents": 1, "sum_usd": -1, "largest_usd": -1}


def test_losing_against_the_twin_while_staying_above_acting_alone_passes_but_is_reported(tmp_path):
    twin = _run(tmp_path, "twin", {"A": 0.0, "B": 0.0, "C": 0.0, "X": 0.0}, {"A": 10, "B": 5, "C": 7, "X": 3})
    atk = _run(tmp_path, "atk", {"A": 0.0, "B": 0.0, "C": 0.0, "X": 9.0}, {"A": 4, "B": 5, "C": 7, "X": -9},
               attackers=["X"])
    r = check(atk, twin)
    assert r["verdict"] == "PASS" and r["newly_below_acting_alone"] == {}
    assert r["reported_not_judged"]["twin_relative_loss"]["agents"] == 1


@pytest.mark.parametrize("bad", ["commit", "twin_attackers", "no_attackers", "missing_agent", "roster", "dirty"])
def test_fails_closed(tmp_path, bad):
    harm = {"A": 0.0, "B": 0.0, "C": 0.0, "X": 0.0}
    value = {"A": 1, "B": 1, "C": 1, "X": 1}
    twin = _run(tmp_path, "twin", harm, value, attackers=["X"] if bad == "twin_attackers" else (),
                commit="def5678" if bad == "commit" else "abc1234",
                roster=ROSTER[:3] if bad == "roster" else ROSTER)
    atk = _run(tmp_path, "atk", {k: v for k, v in harm.items() if not (bad == "missing_agent" and k == "B")}, value,
               attackers=() if bad == "no_attackers" else ["X"], commit="abc1234-dirty" if bad == "dirty" else "abc1234")
    with pytest.raises(CheckFailed):
        check(atk, twin)


# ---- D1 (owner, Nov 7; staged as D91, revising D88): "newly below" at zero tolerance; "further below" judged only
# beyond section 8's max(1% of |acting alone|, $10) per agent; the zero-tolerance count and $ always reported --------

def _d1_pair(tmp_path, a_harm_b, a_harm_c, twin_harm_b=11.88, value_b=-1000.0):
    """B is already below acting alone in the twin (harm 11.88); C is not. X is the attacker."""
    twin = _run(tmp_path, "twin", {"A": 0.0, "B": twin_harm_b, "C": 0.0, "X": 0.0},
                {"A": 10, "B": value_b, "C": 7, "X": 3})
    atk = _run(tmp_path, "atk", {"A": 0.0, "B": a_harm_b, "C": a_harm_c, "X": 9.0},
               {"A": 10, "B": value_b - (a_harm_b - twin_harm_b), "C": 7 - a_harm_c, "X": -9}, attackers=["X"])
    return atk, twin


def test_d1_already_below_and_21_cents_further_passes_judged_and_is_counted_under_zero(tmp_path):
    atk, twin = _d1_pair(tmp_path, a_harm_b=12.09, a_harm_c=0.0)
    r = check(atk, twin)                                                    # default: section 8 (judged)
    assert r["further_below_tol"] == "section8" and r["verdict"] == "PASS"
    assert r["newly_below_acting_alone"] == {} and r["further_below_judged"] == {}
    fb = r["further_below_acting_alone"]["B"]
    assert fb["further_usd"] == pytest.approx(0.21) and fb["tolerance_usd"] == pytest.approx(10.0)
    assert fb["beyond_tolerance"] is False
    z = r["zero_tolerance"]
    assert z["further_below_count"] == 1 and z["further_below_usd"] == pytest.approx(0.21) and z["verdict"] == "FAIL"
    r0 = check(atk, twin, further_below_tol="zero")                         # reported setting
    assert r0["verdict"] == "FAIL" and list(r0["further_below_judged"]) == ["B"]


def test_d1_newly_below_by_one_cent_fails_under_both_settings(tmp_path):
    atk, twin = _d1_pair(tmp_path, a_harm_b=11.88, a_harm_c=0.01)
    for tol in ("section8", "zero"):
        r = check(atk, twin, further_below_tol=tol)
        assert r["verdict"] == "FAIL" and list(r["newly_below_acting_alone"]) == ["C"]
        assert r["further_below_acting_alone"] == {}


def test_d1_further_below_beyond_the_tolerance_fails_and_the_1_percent_part_applies(tmp_path):
    # acting alone for B = value + harm = -1000 - 10.5 + 22.38 ... |alone| ~ 988: 1% ~ $9.9 < $10, so tol = $10
    atk, twin = _d1_pair(tmp_path, a_harm_b=11.88 + 10.5, a_harm_c=0.0)
    r = check(atk, twin)
    assert r["verdict"] == "FAIL" and list(r["further_below_judged"]) == ["B"]
    # |alone| ~ 5,000: tol = 1% = ~$50, so $10.50 further is within it
    atk, twin = _d1_pair(tmp_path / "big", a_harm_b=11.88 + 10.5, a_harm_c=0.0, value_b=-5000.0)
    r = check(atk, twin)
    fb = r["further_below_acting_alone"]["B"]
    assert fb["tolerance_usd"] == pytest.approx(0.01 * abs(fb["acting_alone_usd"]))
    assert fb["acting_alone_usd"] == pytest.approx(-5000.0 - 10.5 + 22.38)
    assert r["verdict"] == "PASS" and r["zero_tolerance"]["verdict"] == "FAIL"


def test_d1_unknown_setting_is_refused(tmp_path):
    atk, twin = _d1_pair(tmp_path, a_harm_b=11.88, a_harm_c=0.0)
    with pytest.raises(ValueError):
        check(atk, twin, further_below_tol="1pct")


# ---- unrounded basis (staged D98; owner, Nov 11 Part C 2e): judge on unrounded shortfalls with a $1e-6 guard --------
# shortfall = acting alone - value, signed and unrounded, written by sim/run_m1.py under `harm_unrounded: true`.

def _run_u(tmp_path, name, short, value, attackers=()):
    d = _run(tmp_path, name, {k: round(max(v, 0.0), 2) for k, v in short.items()}, value, attackers=attackers)
    s = json.loads((d / "summary.json").read_text())
    s["exchange_by_headroom_mw"]["100"]["net_harm"]["shortfall_usd"] = short
    (d / "summary.json").write_text(json.dumps(s))
    return d


def test_unrounded_basis_counts_a_sub_cent_newly_below_that_the_cents_basis_misses(tmp_path):
    # jun02 COM_13 shape: $2.89 above acting alone in the twin, $0.000028 below in the attack cell
    twin = _run_u(tmp_path, "twin", {"A": -2.89, "B": 0.0, "C": -1.0, "X": 0.0}, {"A": 1, "B": 1, "C": 1, "X": 1})
    atk = _run_u(tmp_path, "atk", {"A": 2.8e-5, "B": 0.0, "C": -1.0, "X": 9.0}, {"A": 1, "B": 1, "C": 1, "X": 1},
                 attackers=["X"])
    assert check(atk, twin)["verdict"] == "PASS"                               # cents: rounds to $0.00
    r = check(atk, twin, basis="unrounded")
    assert r["basis"] == "unrounded" and r["guard_usd"] == 1e-6
    assert r["verdict"] == "FAIL" and list(r["newly_below_acting_alone"]) == ["A"]


def test_unrounded_basis_turns_a_rounding_newly_below_into_further_below(tmp_path):
    # feb26 COM_12 shape: $0.001016 below in the twin (rounds to $0.00), $0.008190 below in the attack (rounds to $0.01)
    twin = _run_u(tmp_path, "twin", {"A": 0.001016, "B": 0.0, "C": 0.0, "X": 0.0},
                  {"A": -2570.779296, "B": 1, "C": 1, "X": 1})
    atk = _run_u(tmp_path, "atk", {"A": 0.008190, "B": 0.0, "C": 0.0, "X": 9.0},
                 {"A": -2570.786471, "B": 1, "C": 1, "X": 1}, attackers=["X"])
    assert check(atk, twin)["verdict"] == "FAIL"                               # cents: newly below
    r = check(atk, twin, basis="unrounded")
    assert r["verdict"] == "PASS" and r["newly_below_acting_alone"] == {}
    fb = r["further_below_acting_alone"]["A"]
    assert fb["further_usd"] == pytest.approx(0.007174) and fb["beyond_tolerance"] is False
    assert r["zero_tolerance"]["further_below_count"] == 1


def test_unrounded_guard_ignores_float_noise(tmp_path):
    twin = _run_u(tmp_path, "twin", {"A": 0.0, "B": 3e-7, "C": 0.0, "X": 0.0}, {"A": 1, "B": 1, "C": 1, "X": 1})
    atk = _run_u(tmp_path, "atk", {"A": 9e-7, "B": 3e-7 + 5e-7, "C": 0.0, "X": 9.0}, {"A": 1, "B": 1, "C": 1, "X": 1},
                 attackers=["X"])
    r = check(atk, twin, basis="unrounded")
    assert r["verdict"] == "PASS" and r["newly_below_acting_alone"] == {} and r["further_below_acting_alone"] == {}


def test_unrounded_basis_fails_closed_without_the_field(tmp_path):
    twin = _run(tmp_path, "twin", {"A": 0.0, "B": 0.0, "C": 0.0, "X": 0.0}, {"A": 1, "B": 1, "C": 1, "X": 1})
    atk = _run_u(tmp_path, "atk", {"A": 0.0, "B": 0.0, "C": 0.0, "X": 9.0}, {"A": 1, "B": 1, "C": 1, "X": 1},
                 attackers=["X"])
    with pytest.raises(CheckFailed):
        check(atk, twin, basis="unrounded")


def test_unrounded_shortfall_must_agree_with_the_rounded_harm(tmp_path):
    twin = _run_u(tmp_path, "twin", {"A": 0.0, "B": 0.0, "C": 0.0, "X": 0.0}, {"A": 1, "B": 1, "C": 1, "X": 1})
    atk = _run_u(tmp_path, "atk", {"A": 0.5, "B": 0.0, "C": 0.0, "X": 9.0}, {"A": 1, "B": 1, "C": 1, "X": 1},
                 attackers=["X"])
    s = json.loads((atk / "summary.json").read_text())
    s["exchange_by_headroom_mw"]["100"]["net_harm"]["by_agent_usd"]["A"] = 0.0     # inconsistent with 0.5
    (atk / "summary.json").write_text(json.dumps(s))
    with pytest.raises(CheckFailed):
        check(atk, twin, basis="unrounded")


def test_unknown_basis_is_refused(tmp_path):
    twin = _run(tmp_path, "twin", {"A": 0.0, "B": 0.0, "C": 0.0, "X": 0.0}, {"A": 1, "B": 1, "C": 1, "X": 1})
    atk = _run(tmp_path, "atk", {"A": 0.0, "B": 0.0, "C": 0.0, "X": 9.0}, {"A": 1, "B": 1, "C": 1, "X": 1},
               attackers=["X"])
    with pytest.raises(ValueError):
        check(atk, twin, basis="rounded_up")
