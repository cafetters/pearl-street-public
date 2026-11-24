"""M2b step 2: the LP ceiling with data-center deferral (bench/bench.py build(dc=...), bench/m0.py solve(dc=...)).
Pass item 2: the battery-plus-load ceiling's peak is at or below the battery-only one; the served schedule obeys the
deferral rules (sim/dc_defer.py); with no flexible workload the LP is unchanged (same peak and dual)."""
import numpy as np
import pytest

from bench import m0 as M0
from sim import dc_defer as DC
from tests.test_replay import synthetic_slice


@pytest.fixture(scope="module")
def bench():
    s = synthetic_slice()
    B = M0.load_bench(s)
    M0.use_slice(B, s)
    return s, B


def spec(T, g_kw):
    g = np.asarray(g_kw, float)
    return {"agents": [0], "generated": [g],
            "sessions": [[DC.Session(start=0, deadline=T, energy_kwh=float(g.sum() * 0.25), cap_kw=float(g.max() * 3 + 1))]]}


def test_no_flexible_work_changes_nothing(bench):
    s, B = bench
    free = M0.solve(B, peak=True)
    same = M0.solve(B, peak=True, dc=spec(B.T, np.zeros(B.T)))
    assert same["z"][0] == pytest.approx(free["z"][0], abs=1e-6)
    assert np.allclose(same["dual"], free["dual"], atol=1e-9)


def test_deferral_lowers_or_keeps_the_peak_and_obeys_the_rules(bench):
    s, B = bench
    hour = np.asarray(s.ts_utc.tz_convert("America/Chicago").hour)
    g = np.where((hour >= 17) & (hour < 21), 20.0, 0.0)                   # 20 kW of workload in the evening peak
    free = M0.solve(B, peak=True)
    with_dc = M0.solve(B, peak=True, dc=spec(B.T, g))
    assert with_dc["z"][0] <= free["z"][0] + 1e-6
    served = with_dc["dc_served"][0]
    assert DC.check(served, g, spec(B.T, g)["sessions"][0], 0.25) == []
    assert len(with_dc["dual"]) == B.T                                     # coupling rows still last


def test_network_rows_and_deferral_together_are_refused(bench):
    s, B = bench
    net = {"keys": ["k"], "g": np.zeros((1, B.N)), "sign": np.ones((1, B.T)), "rhs": np.ones((1, B.T))}
    with pytest.raises(ValueError):
        M0.solve(B, peak=True, dc=spec(B.T, np.zeros(B.T)), network=net)
