"""D49 head-to-head rule in bench.h2h_report: B is adopted only if it wins every cell (more of the gap closed, net
harm per MW cut not higher), and a cell with a run stopped by the referee, or with an incomplete or mismatched roster,
is not a B win. D53 (arm C vs. A) and the D55 tie band below."""
from bench.h2h_report import HEADROOMS, WINDOWS, decide


ROSTER = ["PVB_01", "PVB_02", "COM_01"]


def _row(arm, win, h, gap, harm_per_mw, peak_mw=2400.0):
    return {"arm": arm, "window": win, "h": h, "gap_share": gap, "harm_per_mw_cut": harm_per_mw, "peak_mw": peak_mw,
            "roster": list(ROSTER), "roster_complete": True}


def _rows(b_gap=0.20, b_harm=1.0):
    rows = {}
    for win in WINDOWS:
        for h in HEADROOMS:
            rows[("a", win, h)] = _row("a", win, h, 0.10, 2.0)
            rows[("b", win, h)] = _row("b", win, h, b_gap, b_harm)
    return rows


def test_b_adopted_when_it_wins_every_cell():
    ok, cells = decide(_rows())
    assert ok and len(cells) == len(WINDOWS) * len(HEADROOMS)


def test_b_not_adopted_if_gap_not_better_in_one_cell():
    rows = _rows()
    rows[("b", "jan12", 150)]["gap_share"] = 0.10
    assert not decide(rows)[0]


def test_b_not_adopted_if_harm_per_mw_higher_in_one_cell():
    rows = _rows()
    rows[("b", "jan02", 100)]["harm_per_mw_cut"] = 2.5
    assert not decide(rows)[0]


def test_no_cut_counts_as_infinite_harm():
    rows = _rows()
    rows[("b", "jan02", 150)]["harm_per_mw_cut"] = None
    assert not decide(rows)[0]
    rows = _rows()
    for win in WINDOWS:
        for h in HEADROOMS:
            rows[("a", win, h)]["harm_per_mw_cut"] = None
    assert decide(rows)[0]


def test_missing_run_blocks_adoption():
    rows = _rows()
    del rows[("a", "jan12", 100)]
    ok, cells = decide(rows)
    assert not ok and any("missing run" in c for c in cells)


def test_referee_stop_is_not_a_b_win():
    rows = _rows()
    rows[("b", "jan02", 100)] = {"arm": "b", "window": "jan02", "h": 100, "stopped": "award not deliverable"}
    ok, cells = decide(rows)
    assert not ok and any("B stopped by the referee" in c for c in cells)
    rows = _rows()
    rows[("a", "jan02", 100)] = {"arm": "a", "window": "jan02", "h": 100, "stopped": "award not deliverable"}
    ok, cells = decide(rows)
    assert not ok and any("owner's call" in c for c in cells)


# ---- D53: arm C vs. arm A over feasible cells ----
from bench.h2h_report import decide_d53


def _d53(c_gap=0.30, c_harm_mw=1.0, a_harm_mw=2.0, a_harmed=(14, 8000.0), c_harmed=(3, 500.0)):
    rows = {}
    for win in WINDOWS:
        for h in HEADROOMS:
            rows[("a", win, h)] = {**_row("a", win, h, 0.10, a_harm_mw), "harmed": a_harmed[0], "harm_usd": a_harmed[1]}
            rows[("c", win, h)] = {**_row("c", win, h, c_gap, c_harm_mw), "harmed": c_harmed[0], "harm_usd": c_harmed[1]}
    return rows


FEAS = {f"h2h_c_{w}_h{h}": {"feasible": h == 100, "max_short_mw": 0.0 if h == 100 else 85.5}
        for w in WINDOWS for h in HEADROOMS}


def test_d53_judges_only_feasible_cells():
    rows = _d53()
    for win in WINDOWS:                                    # C loses badly where the target is infeasible
        rows[("c", win, 150)]["gap_share"] = -1.0
    ok, cells = decide_d53(rows, FEAS)
    assert ok and sum("infeasible target" in c for c in cells) == 2


def test_d53_where_a_has_no_cut_compares_total_net_harm():
    rows = _d53(a_harm_mw=None)
    assert decide_d53(rows, FEAS)[0]                       # C less harm in agents and $
    rows = _d53(a_harm_mw=None, c_harmed=(20, 500.0))
    assert not decide_d53(rows, FEAS)[0]                   # more agents harmed than A
    rows = _d53(a_harm_mw=None, c_harmed=(3, 9000.0))
    assert not decide_d53(rows, FEAS)[0]                   # more $ than A


def test_d53_needs_more_gap_and_no_more_harm_per_mw_in_each_feasible_cell():
    assert not decide_d53(_d53(c_gap=0.05), FEAS)[0]
    assert not decide_d53(_d53(c_harm_mw=3.0), FEAS)[0]
    assert not decide_d53(_d53(c_harm_mw=None), FEAS)[0]   # C cut nothing while A did: infinite harm per MW


def test_d53_without_feasibility_or_with_no_feasible_cell_adopts_nothing():
    assert not decide_d53(_d53(), {})[0]
    none = {k: {**v, "feasible": False, "max_short_mw": 1.0} for k, v in FEAS.items()}
    assert not decide_d53(_d53(), none)[0]


# ---- Roster from the run header (CLAUDE.md): an incomplete or mismatched roster is never a win ----
import pytest

from bench.h2h_report import roster_problem


@pytest.mark.parametrize("arm", ["a", "c"])
def test_d53_cell_with_an_incomplete_roster_is_not_a_c_win(arm):
    rows = _d53()
    rows[(arm, "jan02", 100)]["roster_complete"] = False
    ok, cells = decide_d53(rows, FEAS)
    assert not ok and any("roster incomplete" in c for c in cells)


def test_d53_cell_with_different_rosters_is_not_a_c_win():
    rows = _d53()
    rows[("c", "jan12", 100)]["roster"] = ROSTER[:2] + ["PVB_99"]
    ok, cells = decide_d53(rows, FEAS)
    assert not ok and any("rosters differ" in c for c in cells)


def test_d49_cell_with_an_incomplete_or_different_roster_is_not_a_b_win():
    rows = _rows()
    rows[("b", "jan02", 150)]["roster_complete"] = False
    assert not decide(rows)[0]
    rows = _rows()
    rows[("a", "jan12", 100)]["roster"] = ROSTER[:2]
    assert not decide(rows)[0]


def test_roster_check_fails_closed_without_roster_fields():
    a = _row("a", "jan02", 100, 0.1, 2.0)
    bare = {"arm": "c", "window": "jan02", "h": 100, "gap_share": 0.3, "harm_per_mw_cut": 1.0}
    assert roster_problem(a, bare) == "C roster incomplete"
    assert roster_problem(a, {**bare, "roster": [], "roster_complete": True}) == "C roster incomplete"
    assert roster_problem(a, {**a, "arm": "c", "roster": list(reversed(ROSTER))}) is None   # same set, any order


# ---- D55 tie band (future comparisons only; D53's pre-registered verdict uses no band) ----
def _tied(c_harm_mw, peak_gap_mw):
    rows = _d53(c_gap=0.099, c_harm_mw=c_harm_mw)                     # C a hair behind A on gap share
    for win in WINDOWS:
        for h in HEADROOMS:
            rows[("c", win, h)]["peak_mw"] = rows[("a", win, h)]["peak_mw"] + peak_gap_mw
    return rows


def test_tie_band_judges_peaks_within_1_mw_on_harm_alone():
    assert not decide_d53(_tied(1.0, 0.8), FEAS)[0]                    # as pre-registered: not better on gap
    ok, cells = decide_d53(_tied(1.0, 0.8), FEAS, tie_band_mw=1.0)
    assert ok and all("tie, peaks within 1 MW" in c for c in cells if "infeasible" not in c)
    assert not decide_d53(_tied(3.0, 0.8), FEAS, tie_band_mw=1.0)[0]   # tie, but more harm per MW cut than A


def test_tie_band_does_not_apply_outside_1_mw():
    assert not decide_d53(_tied(1.0, 1.2), FEAS, tie_band_mw=1.0)[0]
