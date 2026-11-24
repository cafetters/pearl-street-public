"""D111 (owner, Nov 23): data_centers: off | battery | deferral. battery (or no key) is today's behaviour; off keeps
data centers in the simulation but out of the market (D101's fourth status, acting alone); deferral is M2b's round-2
design, labelled demo only. Synthetic slices only."""
import dataclasses
import json

import numpy as np
import pytest

import sim.run_m1 as R
from tests.test_dc_run import CFG, _evening, spec
from tests.test_replay import synthetic_slice


def _dc_slice(aid="A"):
    s = synthetic_slice()
    meta = s.meta.copy()
    meta.loc[meta["entity_id"] == aid, "entity_type"] = "datacenter"
    return dataclasses.replace(s, meta=meta)


def _strip(log):
    return [json.dumps({k: v for k, v in m.items() if k != "run_id"}, sort_keys=True) for m in log]


def test_setting_values():
    assert R.data_centers_setting(CFG) is CFG                       # no key: unchanged
    d = R.data_centers_setting({**CFG, "data_centers": "deferral"})
    assert d["dc_deferral"] is True and d["dc_joint"] is True
    assert "dc_deferral" not in R.data_centers_setting({**CFG, "data_centers": "battery"})
    import yaml                                                     # an unquoted `off` in a YAML config is false
    assert R.data_centers_setting({**CFG, **yaml.safe_load("data_centers: off")})["data_centers"] == "off"
    for bad in ({**CFG, "data_centers": "on"}, {**CFG, "data_centers": True},
                {**CFG, "data_centers": "deferral", "price_mode": False},
                {**CFG, "data_centers": "deferral", "dc_deferral": False},
                {**CFG, "data_centers": "off", "dc_deferral": True},
                {**CFG, "data_centers": "battery", "dc_joint": True}):
        with pytest.raises(ValueError):
            R.data_centers_setting(bad)


def test_data_center_ids():
    assert R.data_center_ids(_dc_slice(), np.arange(2)) == {"A"}
    assert R.data_center_ids(synthetic_slice(), np.arange(2)) == set()


def test_battery_is_todays_run(tmp_path):
    s = synthetic_slice()
    plain = R.run_variant(CFG, s, np.arange(2), tmp_path / "plain", headroom_mw=0.03)
    bat = R.run_variant({**CFG, "data_centers": "battery"}, s, np.arange(2), tmp_path / "bat", headroom_mw=0.03)
    assert _strip(plain["log"]) == _strip(bat["log"])
    assert bat["header"]["data_centers"] == "battery" and "data_centers" not in plain["header"]
    assert set(bat["roster_status"].values()) == {"active"}


def test_off_keeps_data_centers_out_of_the_market(tmp_path):
    s = _dc_slice("A")
    r = R.run_variant({**CFG, "data_centers": "off"}, s, np.arange(2), tmp_path / "off", headroom_mw=0.03)
    assert r["roster_status"] == {"A": R.DC_OFF_STATUS, "B": "active"}
    assert r["header"]["data_centers_excluded"] == ["A"] and r["header"]["roster"] == ["A", "B"]
    assert not [m for m in r["log"] if m["sender"] == "A"]                         # A sends nothing, not even register
    assert not [m for m in r["log"] if m["sender"] == "metering" and (m.get("payload") or {}).get("asset_id") == "A/bess"]
    assert any(m["msg_type"] == "nominate" and m["sender"] == "B" for m in r["log"])
    sf = R.run_variant({**CFG, "data_centers": "off"}, s, np.arange(2), tmp_path / "off_alone")   # acting alone
    assert "data_centers_excluded" not in sf["header"] and sf["roster_status"] == {}
    for key in ("C", "D", "X", "SOC"):
        assert np.array_equal(r[key][0], sf[key][0]), key
    op = r["operator"]                    # A's load is background: the operator's idle net is B's alone
    assert np.allclose(op.idle_net, (s.agent["pv"][1] - s.agent["curt"][1] - s.agent["load"][1]))


def test_deferral_is_labelled_demo_only(tmp_path):
    s = synthetic_slice()
    s.agent["load"][0] = s.agent["load"][0] + _evening(s, 30.0)
    cfg = {**CFG, "data_centers": "deferral"}
    r = R.run_variant(cfg, s, np.arange(2), tmp_path / "dfr", headroom_mw=0.03, dc_spec=spec(s, _evening(s, 30.0)))
    assert r["header"]["demo_only"] == R.DEMO_ONLY and r["header"]["data_centers"] == "deferral"
    assert "DEMO ONLY" in R.DEMO_ONLY and "optimistic" in R.DEMO_ONLY
    line1 = json.loads((tmp_path / "dfr" / r["variant"] / "trace.jsonl").read_text().splitlines()[0])
    assert line1["demo_only"] == R.DEMO_ONLY
    plain = R.run_variant(CFG, s, np.arange(2), tmp_path / "plain2", headroom_mw=0.03)
    assert "demo_only" not in plain["header"]
