"""D61 reneging attacker (attacks/renege.py), synthetic only: it nominates honestly, then runs its selfish plan on
bound steps. The exchange answers through the meter: imbalance beyond the band at the scarcity price, no 1p payment
for undelivered relief. Gain is measured against the paired honest run (same slice, config and seed).
M3 step 4 (owner, Nov 3): the tests run at the $5 margin (primary; D75's interim) and at m = 0, and both runs of a pair
set delivery_record: true (Q2)."""
import json

import numpy as np
import pytest

from exchange.trace.replay import replay
from sim.run_m1 import evaluate, run_variant
from tests.test_d58_margin import _cfg
from tests.test_replay import synthetic_slice


def _big_a():
    s = synthetic_slice()
    s.meta.loc[0, "bess_power_kw"] = 2000.0         # band = max(5% x 2000, 50) = 100 kW, far below a full response
    s.meta.loc[0, "bess_energy_kwh"] = 8000.0
    return s


MARGINS = [pytest.param(5.0, id="m5"), pytest.param(0.0, id="m0")]       # $5 primary, m = 0 also (step 4)


def _pair(tmp_path, s, h, margin=5.0, **atk):
    rec = s.system["system_load"] * 1000.0
    p_end = float(s.system["price_da"].mean()) / 1000.0
    out = {}
    base = {"peak_response_margin_usd_mwh": margin, "delivery_record": True}
    for name, cfg in (("hon", _cfg(run_name="hon", **base)),
                      ("atk", _cfg(run_name="atk", attackers=[{"type": "renege", "agent": "A", **atk}], **base))):
        sf = run_variant(cfg, s, np.arange(2), tmp_path / name)
        r = run_variant(cfg, s, np.arange(2), tmp_path / name, headroom_mw=h)
        ev = evaluate(cfg, s, np.arange(2), r, rec, None, sf)
        out[name] = {"r": r, "value": ev["_agent_value"] + r["SOC"][:, -1] * p_end,
                     "imb": {m["to"]: m["payload"] for m in r["log"] if m["msg_type"] == "imbalance"},
                     "ps": {m["to"]: m["payload"]["net_usd"] for m in r["log"] if m["msg_type"] == "peak_settle"},
                     "peak_mw": ev["peak_mw"], "path": next((tmp_path / name).glob("exchange_*/trace.jsonl"))}
    return out


@pytest.mark.parametrize("margin", MARGINS)
def test_reneging_beyond_the_band_pays_imbalance_and_gains_nothing(tmp_path, margin):
    o = _pair(tmp_path, _big_a(), 0.5, margin)
    hon, atk = o["hon"], o["atk"]
    ag = atk["r"]["agents"][0]
    assert type(ag).__name__ == "Reneger" and ag.log                      # the attack ran
    header = json.loads(atk["path"].read_text().splitlines()[0])
    assert header["attackers"] == [{"type": "renege", "agent": "A"}]
    assert any(e["selfish_req"] != e["honest_req"] for e in ag.log)       # it overrode the honest request
    assert hon["imb"]["A"]["deviation_kwh"] == 0.0
    assert atk["imb"]["A"]["deviation_kwh"] > 100.0 and atk["imb"]["A"]["charge_usd"] > 50.0   # the defense fired
    assert hon["ps"]["A"] > 0.0 and atk["ps"]["A"] == pytest.approx(0.0, abs=1e-6)            # nothing undelivered paid
    gain = atk["value"][0] - hon["value"][0]
    assert gain < 0.0
    assert atk["value"][1] == pytest.approx(hon["value"][1], abs=1e-9)    # the other agent is untouched here
    assert atk["peak_mw"] > hon["peak_mw"]                                # the relief it promised is lost
    assert not atk["r"]["refused"]                                        # only the meter shows the attack
    assert replay(atk["path"])["match"]


@pytest.mark.xfail(strict=True, reason="FINDING, not a pass (owner, Nov 2): D61's rule is gain <= 0, and inside the "
                                       "band reneging gains +$0.0006 at m = 0 (the 1p shortfall of "
                                       "docs/overnight/2026-11-01-day.md item 2). Pending the item 7 fix; when it "
                                       "lands this xfail turns into a failure and must be removed.")
def test_reneging_inside_the_band_gains_nothing(tmp_path):
    """A 100 kW battery's free band is 50 kW, so its whole response fits inside it: no imbalance. The pass rule stays
    gain <= 0 (owner, Nov 2). At m = 0 the honest response sits at break-even under 1p and loses a fraction of a cent,
    so reneging comes out slightly ahead: recorded as a finding pending the item 7 fix, not as a pass."""
    o = _pair(tmp_path, synthetic_slice(), 0.03, margin=0.0)
    assert o["atk"]["r"]["agents"][0].log
    assert o["atk"]["imb"]["A"]["deviation_kwh"] == 0.0
    assert o["atk"]["value"][0] - o["hon"]["value"][0] <= 0.0


def test_reneging_inside_the_band_gains_nothing_at_5(tmp_path):
    """Step 4's new test (owner, Nov 3): at the $5 margin, reneging inside the free band gains 0 or less."""
    o = _pair(tmp_path, synthetic_slice(), 0.03, margin=5.0)
    assert o["atk"]["imb"]["A"]["deviation_kwh"] == 0.0
    assert o["atk"]["value"][0] - o["hon"]["value"][0] <= 0.0


@pytest.mark.parametrize("margin", MARGINS)
def test_day_mode_reneges_only_on_its_day(tmp_path, margin):
    s = _big_a()
    other = _pair(tmp_path / "other", s, 0.5, margin, mode="day", day="2023-01-03")   # the slice is Jan 2 local only
    assert not other["atk"]["r"]["agents"][0].log
    assert other["atk"]["value"][0] == pytest.approx(other["hon"]["value"][0], abs=1e-9)
    same = _pair(tmp_path / "same", s, 0.5, margin, mode="day", day="2023-01-02")
    full = _pair(tmp_path / "full", s, 0.5, margin)
    assert same["atk"]["value"][0] == pytest.approx(full["atk"]["value"][0], abs=1e-9)


def test_bad_modes_are_refused():
    from attacks.renege import Reneger
    with pytest.raises(ValueError):
        Reneger(object(), mode="sometimes")
    with pytest.raises(ValueError):
        Reneger(object(), mode="day")


# ---------------------------------------------------------------- nominate at A, withdraw at a zero-price B (Nov 5)
class _Stub:
    """A stand-in honest agent: 4 steps; nominates 50 kW of export on every step of whatever call is open."""
    T, dt_h, wear = 4, 0.25, 0.0
    iso = ["2023-01-02T06:00:00Z", "2023-01-02T06:15:00Z", "2023-01-02T06:30:00Z", "2023-01-02T06:45:00Z"]
    avail = np.zeros(4)
    base_export = np.zeros(4)
    b = None

    def __init__(self):
        self.got, self.n = [], 0

    def _mask(self, w):
        m = np.zeros(4, bool)
        m[self.iso.index(w["start_utc"]):] = True
        return m

    def _msg(self, typ, t, payload):
        self.n += 1
        return json.dumps({"msg_type": typ, "t_utc": t, "payload": payload, "msg_id": f"A:{self.n}"})

    def receive(self, raw):
        self.got.append(raw)

    def between_price_rounds(self, k, soc, v, price):
        return [self._msg("nominate", self.iso[k], {"call_id": self.open, "window": W0, "kw": [50.0] * 4})]

    def act(self, k, soc, v, price):
        return [], (0.0, 25.0, 0.0)


W0 = {"start_utc": "2023-01-02T06:00:00Z", "end_utc": "2023-01-02T07:00:00Z"}


def _feed(z, typ, payload):
    z.receive(json.dumps({"msg_type": typ, "payload": payload}))


def test_zero_price_b_withdraws_only_where_a_priced_above_0_and_b_priced_0(monkeypatch):
    from types import SimpleNamespace

    from attacks import renege
    monkeypatch.setattr(renege, "solve", lambda *a, **k: SimpleNamespace(c=np.zeros(4 - a[1]), d=np.full(4 - a[1], 7.0),
                                                                          x=np.zeros(4 - a[1])))
    inner = _Stub()
    z = renege.ZeroPriceBReneger(inner)
    for cid in ("A", "B"):
        _feed(z, "call_nominations", {"call_id": cid, "binding": True})
    _feed(z, "peak_price", {"call_id": "A", "final": True, "window": W0, "price_usd_mwh": [10.0, 10.0, 0.0, 10.0]})
    inner.open = "A"
    assert json.loads(z.between_price_rounds(0, 0.0, 0, np.zeros(4))[0])["payload"]["kw"] == [50.0] * 4   # honest at A
    _feed(z, "peak_price", {"call_id": "B", "final": True, "window": W0, "price_usd_mwh": [10.0, 0.0, 0.0, 0.0]})
    inner.open = "B"
    kw = json.loads(z.between_price_rounds(0, 0.0, 0, np.zeros(4))[0])["payload"]["kw"]
    assert kw == [50.0, 7.0, 50.0, 7.0]          # steps 1 and 3: A > 0, B = 0 -> its selfish net (7 kW); 0 and 2 kept
    assert z.opportunity == {1, 3}
    assert z.act(1, 0.0, 0, np.zeros(4))[1] == (0.0, 7.0, 0.0)          # runs selfish where it withdrew
    assert z.act(2, 0.0, 0, np.zeros(4))[1] == (0.0, 25.0, 0.0)         # honest elsewhere


@pytest.mark.parametrize("margin", MARGINS)
def test_zero_price_b_without_an_opportunity_is_inert_and_gains_nothing(tmp_path, margin):
    """The synthetic slice's two binding calls price the same peak steps above 0, so there is no opportunity: the
    wrapper never acts and the cell equals its twin (D61 reports such a window as not exercised)."""
    o = _pair(tmp_path, _big_a(), 0.5, margin, mode="zero_b")
    ag = o["atk"]["r"]["agents"][0]
    assert type(ag).__name__ == "ZeroPriceBReneger" and not ag.opportunity and not ag.log
    assert o["atk"]["value"][0] - o["hon"]["value"][0] == pytest.approx(0.0, abs=1e-9)
