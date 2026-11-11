"""The restated M2a gate (owner, Nov 3; bench.m2a_block.restated_gate): synthetic rows only."""
import pytest

from bench.d56_resettle import CheckFailed
from bench.m2a_block import restated_gate


def _row(w, **kw):
    r = {"window": w, "margin": 5.0, "feasible": True, "d57_peak_before_first_gate": False, "settlement_harmed_1p": 0,
         "peak_below_selfish": True, "d51_band_rule": True, "roster_complete": True}
    r.update(kw)
    return r


def test_passes_when_every_check_holds_and_leaves_out_only_infeasible_and_d57_windows_from_part_iii():
    rows = [_row("a"), _row("b", feasible=False, d51_band_rule=False),
            _row("c", d57_peak_before_first_gate=True, peak_below_selfish=False)]
    g = restated_gate(rows, True)
    assert g["pass"] and g["excluded_infeasible"] == ["b"] and g["excluded_d57"] == ["c"] and g["judged_for_iii"] == 1


@pytest.mark.parametrize("bad, check", [
    ({"settlement_harmed_1p": 1, "feasible": False}, "settlement_harm"),        # (i) counts every window
    ({"peak_below_selfish": False}, "peak_below_selfish"),
    ({"d51_band_rule": False}, "d51_band_rule"),
    ({"roster_complete": False, "feasible": False}, "roster"),                  # roster counts every window
])
def test_each_check_fails_the_gate(bad, check):
    g = restated_gate([_row("a"), _row("b", **bad)], True)
    assert not g["pass"] and g["fails"][check] == ["b"]


def test_horizon_failure_fails_the_gate():
    g = restated_gate([_row("a")], False)
    assert not g["pass"] and g["fails"]["horizon"] == ["all windows"]


def test_fails_closed_on_missing_empty_or_mixed_margins():
    with pytest.raises(CheckFailed):
        restated_gate([], True)
    with pytest.raises(CheckFailed):
        restated_gate([_row("a"), {"window": "b", "margin": 5.0, "missing": True}], True)
    with pytest.raises(CheckFailed):
        restated_gate([_row("a"), _row("b", margin=10.0)], True)
