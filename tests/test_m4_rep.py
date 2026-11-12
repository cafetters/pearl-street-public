"""M4 step 10: round_liar (REP-style liars; plan test 12) and the REP table (D24)."""
import json

import numpy as np
import pytest

from attacks import wrap_attacker
from attacks.round_liar import RoundLiar
from bench import m4_rep_table as R
from exchange import messages as M


class Inner:
    """An honest agent's price-round surface: nominates per call; c['binding'] marks binding calls."""

    def __init__(self):
        self.id, self.iso = "DC_08", [f"t{k}" for k in range(10)]
        self.calls = {"GRID_OP:2": {"binding": False}, "GRID_OP:4": {"binding": True}}
        self.honest = {"GRID_OP:2": [10.0, 20.0], "GRID_OP:4": [10.0, 20.0]}
        self.first = True

    def _nom(self, cid, kw):
        return M.encode(M.make("nominate", self.id, "exchange", "2023-01-12T06:45:00Z",
                               {"call_id": cid, "zone": "SYS", "window": {"start_utc": "a", "end_utc": "b"},
                                "kw": kw}, f"{self.id}:1", "r"))

    def act(self, k, soc, v, price):
        if self.first:      # the first nominations, made before any price round: the selfish plan
            self.first = False
            return [self._nom("GRID_OP:2", [4.0, 5.0]), self._nom("GRID_OP:4", [4.0, 5.0])], (0, 0, 0)
        return [], (0, 0, 0)

    def between_price_rounds(self, k, soc, v, price):
        return [self._nom(cid, kw) for cid, kw in self.honest.items()]

    def receive(self, raw):
        pass


def _kw(raws):
    return {M.decode(r)["payload"]["call_id"]: M.decode(r)["payload"]["kw"] for r in raws}


@pytest.mark.parametrize("mode,want", [("overstate", [16.0, 35.0]), ("understate", [4.0, 5.0])])
def test_round_liar_misreports_only_non_binding_rounds(mode, want):
    lia = RoundLiar(Inner(), mode=mode)
    first = _kw(lia.act(0, 0.0, 1, None)[0])
    assert first == {"GRID_OP:2": [4.0, 5.0], "GRID_OP:4": [4.0, 5.0]}        # honest outside price rounds
    got = _kw(lia.between_price_rounds(1, 0.0, 1, None))
    assert got["GRID_OP:2"] == want                       # overstate: selfish + 2 x (honest - selfish)
    assert got["GRID_OP:4"] == [10.0, 20.0]               # binding: honest
    assert lia.log and lia.log[-1]["call_id"] == "GRID_OP:2"


def test_wrap_attacker_knows_round_liar():
    a = wrap_attacker({"agent": "DC_08", "type": "round_liar", "mode": "understate"}, Inner(), {})
    assert isinstance(a, RoundLiar) and a.mode == "understate"
    with pytest.raises(ValueError):
        RoundLiar(Inner(), mode="sideways")


# ---- the REP table ----

def _summ(tmp_path, name, roster, peak, attackers=(), selfish_peak=120.0, m0_peak=80.0, start="2023-01-12"):
    d = tmp_path / name
    d.mkdir()
    head = {"roster": roster, "config": {"start": start, "end": "2023-01-13",
                                         "attackers": str([{"agent": a, "type": "round_liar"} for a in attackers])}}
    ex = {"controllable": {"peak_mw": peak, "share_of_m0_ceiling": 0.5, "share_of_selfish_to_ceiling_gap": 0.4},
          "net_harm": {"agents_harmed": 0, "total_net_harm_usd": 0.0, "missing_from_check": []},
          "agent_value_usd": {a: 1.0 for a in roster},
          "price_mode": {"rounds_total": 50, "rounds_at_cap": 1}}
    s = {"header": head, "selfish": {"controllable": {"peak_mw": selfish_peak, "share_of_m0_ceiling": 0.0}},
         "m0": {"ceiling_peak_mw": m0_peak}, "peak_steps": {"m0_controllable": {"ceiling_peak_mw": m0_peak}},
         "exchange_by_headroom_mw": {"100": ex}}
    (d / "summary.json").write_text(json.dumps(s))
    return d


def test_rep_table_rows(tmp_path):
    ro = ["A", "B"]
    h = _summ(tmp_path, "honest", ro, 100.0)
    lo = _summ(tmp_path, "liar_over_1", ro, 104.0, attackers=["A"])
    t = R.rep_table(honest=h, liars=[lo], arm_a=None)
    names = [r["row"] for r in t["rows"]]
    assert names == ["acting alone", "arm C honest (REP-style price)", "arm C with liars: liar_over_1",
                     "M0 ceiling"]
    liar = t["rows"][2]
    assert liar["peak_ctrl_mw"] == 104.0 and liar["liars"] == ["A"] and liar["rounds_total"] == 50
    assert t["rows"][0]["peak_ctrl_mw"] == 120.0 and t["rows"][3]["peak_ctrl_mw"] == 80.0
    assert "gap share" in t["markdown"]


def test_rep_table_fails_on_a_roster_or_window_mismatch(tmp_path):
    h = _summ(tmp_path, "honest", ["A", "B"], 100.0)
    with pytest.raises(R.CheckFailed):
        R.rep_table(honest=h, liars=[_summ(tmp_path, "l1", ["A"], 100.0)], arm_a=None)
    with pytest.raises(R.CheckFailed):
        R.rep_table(honest=h, liars=[_summ(tmp_path, "l2", ["A", "B"], 100.0, start="2023-01-06")], arm_a=None)
