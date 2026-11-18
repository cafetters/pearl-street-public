"""M2c step 4: the zone product inside arm C (D99, Leaning). Registration sends each asset its PTDF rows; each price
round also moves lambda per element and sends zone_price to the element's members; the round limit without
convergence is defined and logged; at a binding call's deadline the zone prices settle under 1p (D56) as zone_settle.
Protection (owner, Nov 18): a schedule equal to its reference settles exactly $0 on every component; an agent in no
zone is never charged. Flag off: no zone message at all. Judged on unrounded values (D98)."""
import pytest

from exchange.core import Exchange
from tests.test_exchange import code
from tests.test_price_mode import DEADLINE, K, ROUND_T, STEPS, WIN, P, _sref

ZONES = [{"key": "z1", "rows": {"A/bess": -1.0, "B/bess": -0.5, "C/bess": -0.01}, "limit_mw": 0.1,
          "base_mw": {STEPS[0]: 0.2, STEPS[1]: 0.05}, "cap_usd_mwh": 300.0}]


class Z(P):
    def __init__(self, zones=ZONES, **kw):
        kw.setdefault("settlement_reference", "selfish_1p")
        super().__init__(grid_zones=zones, **kw)

    def setup(self, target=1e9, binding=True, **kw):          # a target the system price never binds: zones only
        out = {}
        for aid in ("A", "B", "C"):
            out[aid] = self.register(aid)
        self.reg_out = out
        self.target(target, window=WIN)
        cid, _ = self.call(binding=binding, window=WIN, deadline=DEADLINE)
        return cid


def _zone_msgs(out, typ):
    return [m for m in out if m["msg_type"] == typ]


def test_zones_need_price_mode_and_1p():
    with pytest.raises(ValueError):
        P(grid_zones=ZONES)                                        # recorded reference: no 1p
    with pytest.raises(ValueError):
        Exchange("run", "OP", "meter", "clock", ["SYS"], grid_zones=ZONES)          # not price mode


def test_registration_sends_each_member_its_rows_and_far_assets_nothing():
    h = Z()
    h.setup()
    rows = {aid: _zone_msgs(out, "zone_rows") for aid, out in h.reg_out.items()}
    assert rows["A"][0]["payload"]["rows"] == {"A/bess": {"z1": -1.0}}
    assert rows["B"][0]["payload"]["rows"] == {"B/bess": {"z1": -0.5}}
    assert rows["C"] == []                                      # |PTDF| 0.01 < 0.05: in no zone


def test_a_price_round_moves_lambda_and_tells_members_only():
    h = Z()
    cid = h.setup()
    for aid in ("A", "B", "C"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)        # 80 kW load each: flows 0.32 and 0.17 MW > 0.1
    out = h.round(1)
    zp = _zone_msgs(out, "zone_price")
    assert sorted(m["to"] if isinstance(m["to"], str) else tuple(m["to"]) for m in zp) == [("A", "B")]
    p = zp[0]["payload"]
    assert p["element"] == "z1" and p["call_id"] == cid and p["round"] == 1
    assert all(v > 0 for v in p["lambda_usd_mwh"]) and p["direction"] == [1.0, 1.0]
    assert max(p["lambda_usd_mwh"]) <= 300.0


def test_round_limit_without_convergence_uses_the_last_prices_and_is_logged():
    h = Z(price_rounds=2, price_tol_excess_kw=1.0)              # zone excess (220 kW) never within 1 kW
    cid = h.setup()
    for r in (1, 2):
        for aid in ("A", "B", "C"):
            h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
        out = h.round(r)
    last = _zone_msgs(out, "zone_price")[0]["payload"]
    assert last["final"]
    row = [x for x in h.ex.price_log if x.get("round") == 2 and not x.get("closing")][-1]
    assert row["final"] and not row.get("converged") and row["round_limit"] is True
    assert row["zone_max_excess_kw"]["z1"] > 1.0
    in_force = [h.ex.zonebook.lam["z1"][t] / K for t in STEPS]
    assert in_force == pytest.approx(last["lambda_usd_mwh"])   # the prices in force are the last ones sent


def _settled(h, cid, metered):
    for aid, kw in (("A", (-80.0, -80.0)), ("B", (-80.0, -80.0)), ("C", (-80.0, -80.0))):
        assert _sref(h, cid, aid=aid, kw=kw) == []
    for aid in ("A", "B", "C"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    h.round(1)
    h.tick(DEADLINE)
    for aid, (m0, m1) in metered.items():
        h.meter_net(aid, STEPS[0], m0)
        h.meter_net(aid, STEPS[1], m1)
    out = h.send("tick", "clock", {"final": True}, t="2023-01-02T07:30:00Z")[1]
    return {m["to"]: m["payload"] for m in out if m["msg_type"] == "zone_settle"}


def test_protection_reference_schedule_settles_zero_and_no_zone_agent_is_never_charged():
    h = Z()
    cid = h.setup()
    zs = _settled(h, cid, {"A": (-80.0, -80.0), "B": (-40.0, -40.0), "C": (-200.0, -200.0)})
    assert zs["A"]["net_usd"] == 0.0 and zs["A"]["by_element_usd"] == {"z1": 0.0}     # exactly, unrounded
    lam = [h.ex.zone_settled[t]["z1"][0] for t in STEPS]        # $/kW-step settled at the binding deadline
    assert zs["B"]["net_usd"] == pytest.approx(sum(l_ * 0.5 * (80.0 - 40.0) for l_ in lam))   # relieved: paid
    assert zs["B"]["net_usd"] > 0
    assert "C" not in zs                                        # in no zone: no zone settlement at all


def test_zone_payment_never_exceeds_the_cap():
    h = Z(zones=[dict(ZONES[0], cap_usd_mwh=5.0)])
    cid = h.setup()
    zs = _settled(h, cid, {"A": (0.0, 0.0), "B": (0.0, 0.0), "C": (0.0, 0.0)})
    for aid, row in (("A", 1.0), ("B", 0.5)):
        assert abs(zs[aid]["net_usd"]) <= 5.0 * K * row * 80.0 * 2 + 1e-12


def test_flag_off_sends_no_zone_message():
    h = P(settlement_reference="selfish_1p")
    cid = h.setup(binding=True)
    for aid in ("A", "B"):
        h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
    out = h.round(1)
    assert not [m for m in out if m["msg_type"].startswith("zone_")]
    assert h.ex.zonebook is None


def test_zones_have_their_own_tight_tolerance():
    h = Z(price_tol_excess_kw=1e9, zone_tol_excess_kw=1.0, price_rounds=3)
    cid = h.setup()
    for r in (1, 2, 3):
        for aid in ("A", "B", "C"):
            h.nominate(aid, cid, [-80.0, -80.0], window=WIN)
        h.round(r)
    rows = [x for x in h.ex.price_log if not x.get("closing")]
    assert not any(x.get("converged") for x in rows)            # 220 kW over the zone limit: never converged
    assert rows[-1]["round_limit"] is True
