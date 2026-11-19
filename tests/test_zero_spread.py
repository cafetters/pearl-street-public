"""Arm C with a zero day-ahead spread (bug found in M2c, Nov 18). rho0 comes from the first day's DA spread (D53), so a
flat day gave rho0 = alpha = 0 and the centre shift divided by zero. Guard: a missing, zero, negative or non-finite
spread uses PM.SPREAD_FLOOR_USD_MWH (disclosed) and the price log says so; a positive spread is unchanged.
No 2023 day in Engie's data has a zero DA spread (smallest $5.78, Dec 16), so no recorded run is affected."""
import math

import pytest

from exchange import price_mode as PM
from tests.test_m2a_exchange import H
from tests.test_price_mode import K, ROUND_T, WIN


class P0(H):
    def __init__(self, spread, **kw):
        super().__init__(price_mode=True, price_spread_usd_mwh=spread, **kw)

    def setup(self):
        for aid in ("A", "B"):
            self.register(aid)
        self.target(100.0, window=WIN)
        cid, _ = self.call(binding=False, window=WIN, deadline="2023-01-02T07:00:00Z")
        return cid


@pytest.mark.parametrize("spread", [0.0, -3.0, math.nan, math.inf])
def test_a_bad_spread_uses_the_floor_and_logs_it(spread):
    h = P0(spread)
    cid = h.setup()
    for aid in ("A", "B"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    out = h.send("tick", "clock", {"price_round": 1}, t=ROUND_T)[1]
    assert out and out[0]["msg_type"] == "peak_price"                     # no ZeroDivisionError
    assert h.ex.rho0 == pytest.approx(PM.rho0_public(PM.SPREAD_FLOOR_USD_MWH, 0.25, 100.0))
    row = h.ex.price_log[0]
    assert row["rho0_spread_floor"]["used_usd_mwh"] == PM.SPREAD_FLOOR_USD_MWH
    assert row["rho0_spread_floor"]["given_usd_mwh"] == (None if not math.isfinite(spread) else spread)


def test_a_positive_spread_is_unchanged_and_not_logged():
    h = P0(40.0)
    cid = h.setup()
    for aid in ("A", "B"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    h.send("tick", "clock", {"price_round": 1}, t=ROUND_T)
    assert h.ex.rho0 == pytest.approx(40.0 * K / 100.0)
    assert "rho0_spread_floor" not in h.ex.price_log[0]


def test_floor_is_positive():
    assert PM.SPREAD_FLOOR_USD_MWH > 0
