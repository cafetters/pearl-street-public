"""Battery and site physics (sim/physics.py)."""
import numpy as np
import pytest

from sim.physics import Fleet, step


def fleet(P=100.0, CAP=400.0, ETA=0.95, SD=0.0, POI=1000.0, S0=200.0):
    a = lambda v: np.array([v], float)
    return Fleet(ids=np.array(["A"]), P=a(P), CAP=a(CAP), ETA=a(ETA), SD=a(SD), POI=a(POI), S0=a(S0))


def run(f, soc, c, d, pv=0.0, curt=0.0, load=0.0, dt=0.25):
    a = lambda v: np.array([v], float)
    return [x[0] for x in step(f, a(soc), a(c), a(d), a(pv), a(curt), a(load), dt)]


def test_power_is_clipped_and_netted():
    c, d, x, s = run(fleet(), 200, 0, 500)
    assert d == 100 and c == 0
    c, d, x, s = run(fleet(), 200, 30, 80)          # both requested: net 50 discharge
    assert (c, d) == (0, 50)


def test_soc_bounds():
    f = fleet()
    c, d, x, s = run(f, 41, 0, 100)                  # only 1 kWh above 10 %
    assert s == pytest.approx(40) and d == pytest.approx(1 * 0.95 / 0.25)
    c, d, x, s = run(f, 359, 100, 0)
    assert s == pytest.approx(360)


def test_poi_forces_extra_curtailment_then_less_discharge():
    f = fleet(POI=100)
    c, d, x, s = run(f, 200, 0, 100, pv=50)          # export would be 150
    assert x == pytest.approx(50) and d == pytest.approx(100)
    c, d, x, s = run(f, 200, 0, 100, pv=50, load=-0.0, curt=0.0)
    f2 = fleet(POI=60)
    c, d, x, s = run(f2, 200, 0, 100, pv=10)         # curtail all 10 PV, then cut discharge to 60
    assert x == pytest.approx(10) and d == pytest.approx(60)


def test_self_discharge():
    c, d, x, s = run(fleet(SD=0.04), 200, 0, 0, dt=0.25)
    assert s == pytest.approx(200 * (1 - 0.01))
