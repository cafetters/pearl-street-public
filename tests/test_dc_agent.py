"""M2b step 3b (agent side): a data-center agent reschedules its own deferred workload each time it plans
(sim/dc_defer.schedule on its price, plus its peak price in arm C), with executed steps fixed, and its load forecast
(base_export) moves by generated - served. Without `dc` nothing changes."""
import numpy as np
import pytest

from sim import dc_defer as DC
from tests.test_m2a_agents import L, agent

G = np.zeros(L)
G[2:5] = 40.0                                                      # 40 kW of workload at steps 2-4
SESS = [DC.Session(start=0, deadline=12, energy_kwh=30.0, cap_kw=120.0)]


def test_no_dc_changes_nothing():
    a = agent()
    before = a.base_export.copy()
    a.act(0, 200.0, 1, np.full(L, 30.0))
    assert np.array_equal(a.base_export, before) and a.dc is None


def test_the_agent_serves_its_workload_at_the_cheap_steps_within_the_rules():
    a = agent(dc={"generated": G, "sessions": SESS})
    rec = a.base_export.copy()                                     # recorded load (generated workload included)
    price = np.full(L, 50.0)
    price[8:10] = 5.0                                              # cheap at steps 8-9, inside the deadline
    a.act(0, 200.0, 1, price)
    s = a.dc_served
    assert DC.check(s, G, SESS, 0.25) == []
    assert s[8:10].sum() == pytest.approx(G.sum())                 # all of it moved to the cheap steps
    assert np.allclose(a.base_export, rec + G - s)


def test_executed_steps_stay_fixed_on_a_later_plan():
    a = agent(dc={"generated": G, "sessions": SESS})
    a.act(0, 200.0, 1, np.full(L, 50.0))                            # flat: serves as recorded
    first = a.dc_served.copy()
    price = np.full(L, 50.0)
    price[8:10] = 5.0
    a.act(3, 200.0, 2, price)                                       # steps 0-2 already ran
    assert np.allclose(a.dc_served[:3], first[:3])
    assert DC.check(a.dc_served, G, SESS, 0.25) == []


def test_bound_steps_keep_the_workload_the_nomination_was_built_on():
    """Found in M2b's real-data runs (Nov 19): the agent re-optimized its deferral on steps its binding nomination
    had already locked, so its metered load left the nomination and it paid imbalance (jan06: $89k across data
    centers). On bound steps the workload stays at the schedule the nomination was built on; free steps move."""
    a = agent(dc={"generated": G, "sessions": SESS})
    a.act(0, 200.0, 1, np.full(L, 50.0))                            # flat: serves as recorded (steps 2-4)
    first = a.dc_served.copy()
    a.nom[1:6] = 0.0                                                # a binding call locked steps 1-5
    price = np.full(L, 50.0)
    price[8:10] = 5.0                                               # now steps 8-9 are cheap
    a.act(1, 200.0, 2, price)
    assert np.allclose(a.dc_served[1:6], first[1:6])               # bound: unchanged
    assert DC.check(a.dc_served, G, SESS, 0.25) == []
    a.nom[1:6] = np.nan                                             # released (a new binding call supersedes it)
    a.act(2, 200.0, 3, price)
    assert a.dc_served[8:10].sum() > 0                              # free again: the work can move
