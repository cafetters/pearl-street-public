"""M2c step 4b: the battery agent prices its own zone component (D99, Leaning). It keeps its PTDF rows from
zone_rows, the latest lambda and flow direction per element from zone_price, and plans on the peak price plus
sum_k lambda_k x (-direction_k x row_k). With no zone rows nothing changes (flag-off identical)."""
import numpy as np
import pytest

from exchange import messages as M
from tests.test_m2a_agents import ISO, END, L, agent

WIN = {"start_utc": ISO[0], "end_utc": END}


def _msg(typ, payload, to="A"):
    return M.encode(M.make(typ, "exchange", to, ISO[0], payload, f"ex:{typ}", "run"))


def zoned(lam, direction=1.0, row=-0.5, **kw):
    a = agent(price_mode=True, **kw)
    a.receive(_msg("zone_rows", {"rows": {"A/bess": {"z1": row}}}))
    a.receive(_msg("zone_price", {"element": "z1", "call_id": "c1", "window": WIN, "lambda_usd_mwh": list(lam),
                                  "direction": [direction] * L, "shift_kw": [0.0] * L, "round": 1, "final": False}))
    a._read_inbox()
    return a


def test_component_adds_to_the_peak_price_sign_aware():
    lam = np.linspace(0.0, 30.0, L)
    a = zoned(lam)
    assert np.allclose(a.zone_add, lam * (-1.0 * -0.5))         # charged on load: discharge relieves
    b = zoned(lam, direction=-1.0)
    assert np.allclose(b.zone_add, lam * (-(-1.0) * -0.5))     # reversed flow: load relieves, so it is paid
    assert np.allclose(a.effective_peak(), a.peak + a.zone_add)


def test_rows_for_another_asset_are_ignored_and_no_rows_means_no_component():
    a = agent(price_mode=True)
    a.receive(_msg("zone_rows", {"rows": {"B/bess": {"z1": -1.0}}}))
    a._read_inbox()
    assert a.zone_rows == {}
    assert a.effective_peak() is a.peak                          # flag-off identical: the same array object


def test_zone_settle_is_booked():
    a = agent(price_mode=True)
    a.receive(_msg("zone_settle", {"window": WIN, "by_element_usd": {"z1": 1.25}, "net_usd": 1.25}))
    a._read_inbox()
    assert a.zone_net_usd == pytest.approx(1.25)


def test_zone_shift_moves_the_prox_centre():
    a = agent(price_mode=True)
    a.receive(_msg("zone_rows", {"rows": {"A/bess": {"z1": -0.5}}}))
    a.receive(_msg("zone_price", {"element": "z1", "call_id": "c1", "window": WIN, "lambda_usd_mwh": [1.0] * L,
                                  "direction": [1.0] * L, "shift_kw": [10.0] * L, "round": 1, "final": False}))
    a._read_inbox()
    assert np.allclose(a.zone_shift, (-1.0 * -0.5) * 10.0)      # 5 kW less load at every step
