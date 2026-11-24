"""M2b step 1: sim/dc_defer.py, data-center workload deferral rules (plan docs/plans/m2b_dc_plan.md, pass item 1).
A schedule `served` (kW per step) is valid when: it is never ahead of the work generated so far, each session's work is
served by its deadline, no step exceeds the session's power ceiling, and the window's total is unchanged. Checks are
unrounded with 1e-6 guards. The LP rows (for the ceiling and the agent) encode the same rules."""
import numpy as np
import pytest

from sim import dc_defer as DC

DT = 0.25


def day(T=12):
    g = np.array([0, 4, 4, 4, 0, 0, 0, 0, 0, 0, 0, 0], float)[:T]          # 12 kWh generated in steps 1-3
    sessions = [DC.Session(start=0, deadline=8, energy_kwh=float(g.sum() * DT), cap_kw=6.0)]
    return g, sessions


def test_the_recorded_schedule_is_valid():
    g, ss = day()
    assert DC.check(g.copy(), g, ss, DT) == []


def test_postponing_within_the_deadline_is_valid():
    g, ss = day()
    s = np.array([0, 0, 0, 6, 6, 0, 0, 0, 0, 0, 0, 0], float)              # 12 kW-steps served at steps 3-4
    assert DC.check(s, g, ss, DT) == []


def test_each_rule_is_enforced():
    g, ss = day()
    early = np.array([4, 4, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0], float)            # served before it was generated
    assert any(v["rule"] == "ahead_of_generation" for v in DC.check(early, g, ss, DT))
    late = np.array([0, 0, 0, 0, 0, 0, 0, 0, 6, 6, 0, 0], float)             # after the deadline (step 8)
    assert any(v["rule"] == "missed_deadline" for v in DC.check(late, g, ss, DT))
    over = np.array([0, 0, 0, 12, 0, 0, 0, 0, 0, 0, 0, 0], float)            # above the 6 kW ceiling
    assert any(v["rule"] == "over_ceiling" for v in DC.check(over, g, ss, DT))
    short = np.array([0, 0, 0, 6, 0, 0, 0, 0, 0, 0, 0, 0], float)            # half the work never served
    rules = {v["rule"] for v in DC.check(short, g, ss, DT)}
    assert "energy_changed" in rules and "missed_deadline" in rules


def test_guards_are_one_millionth():
    g, ss = day()
    s = g.copy()
    s[3] += 0.5e-6 / DT                                                     # 0.5e-6 kWh extra: inside the guard
    s[4] -= 0.5e-6 / DT
    assert DC.check(s, g, ss, DT) == []
    s2 = g.copy()
    s2[1] += 4e-6 / DT                                                      # 4e-6 kWh ahead of generation
    s2[3] -= 4e-6 / DT
    assert any(v["rule"] == "ahead_of_generation" for v in DC.check(s2, g, ss, DT))


def test_deadlines_past_the_window_are_clipped_and_reported():
    g = np.array([0, 0, 4, 4], float)
    ss = [DC.Session(start=0, deadline=10, energy_kwh=2.0, cap_kw=8.0)]
    clipped = DC.clip(ss, T=4)
    assert clipped[0].deadline == 4 and clipped[0].clipped


def test_lp_rows_encode_the_same_rules():
    """Rows A s <= b (cumulative cap, deadlines, total) plus bounds 0 <= s <= cap: every LP-feasible s passes check."""
    g, ss = day()
    A, b, lo, hi = DC.lp_rows(g, ss, DT)
    for s in (g.copy(), np.array([0, 0, 0, 6, 6, 0, 0, 0, 0, 0, 0, 0], float)):
        assert (A @ s <= b + 1e-9).all() and (s >= lo - 1e-12).all() and (s <= hi + 1e-12).all()
    early = np.array([4, 4, 4, 0, 0, 0, 0, 0, 0, 0, 0, 0], float)
    assert not (A @ early <= b + 1e-9).all()
    late = np.array([0, 0, 0, 0, 0, 0, 0, 0, 6, 6, 0, 0], float)
    assert not (A @ late <= b + 1e-9).all()


# ---- step 3a: the agent's own deferral schedule (cheapest allowed steps; past steps fixed) ----

def test_schedule_serves_at_the_cheapest_allowed_steps_and_obeys_the_rules():
    g, ss = day()
    price = np.array([50, 50, 50, 90, 90, 10, 10, 50, 50, 50, 50, 50], float)
    s = DC.schedule(price, g, ss, DT)
    assert DC.check(s, g, ss, DT) == []
    assert s[[5, 6]].sum() == pytest.approx(12.0)                         # all 12 kW-steps at the 10 $/MWh steps


def test_schedule_with_flat_prices_keeps_the_recorded_timing():
    g, ss = day()
    s = DC.schedule(np.full(12, 40.0), g, ss, DT)
    assert np.allclose(s, g)                                              # tie-break: serve as early as allowed


def test_schedule_keeps_executed_steps_fixed():
    g, ss = day()
    price = np.array([50, 50, 50, 90, 90, 10, 10, 50, 50, 50, 50, 50], float)
    s = DC.schedule(price, g, ss, DT, fixed=g[:2])                        # steps 0-1 already ran as recorded
    assert np.allclose(s[:2], g[:2]) and DC.check(s, g, ss, DT) == []


def test_site_room_caps_served_work_below_the_session_ceiling():
    """Found in M2b's real-data runs (Nov 19): the plan's ceiling is the site's capacity minus the load not being
    postponed (`room`), not only the session's power ceiling; serving at the session ceiling put up to ~214 MW of
    data-center load above site capacity at the acting-alone peak. With room, check flags it and the LP rows,
    the ceiling and schedule keep served work within it."""
    g, ss = day()
    room = np.full(12, 6.0)
    room[3:5] = 2.0                                                 # little room at steps 3-4
    s = np.array([0, 0, 0, 6, 6, 0, 0, 0, 0, 0, 0, 0], float)      # valid under the session ceiling alone
    assert DC.check(s, g, ss, DT) == []
    v = DC.check(s, g, ss, DT, room=room)
    assert [x["rule"] for x in v] == ["over_ceiling"] and v[0]["steps"] == [3, 4]
    assert np.allclose(DC.ceiling(ss, 12, room=room)[3:5], 2.0)
    price = np.full(12, 50.0)
    price[3:5] = 1.0                                                # cheap exactly where the room is small
    out = DC.schedule(price, g, ss, DT, room=room)
    assert DC.check(out, g, ss, DT, room=room) == [] and out[3:5].max() <= 2.0 + 1e-6
