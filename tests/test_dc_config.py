"""M2b: the dc_deferral config (sim/run_m1.py dc_spec_from_config), the data centers' share of relief (pass item (e),
dc_relief_share) and the ceiling with deferral (bench/m0.py --dc, pass item (c)). Real-data tests skip without
../data (the Maritime snapshot pearl-base-full has it)."""
from pathlib import Path

import numpy as np
import pytest

from sim import dc_defer as DC

DATA = Path(__file__).resolve().parents[1].parent / "data"
HAS = (DATA / "dc_sessions_2023.parquet").exists()
CFG = {"data_dir": str(DATA), "start": "2023-01-12", "end": "2023-01-13", "slice_tz": "America/Chicago"}


def test_flag_off_builds_nothing_and_a_declaration_needs_the_flag():
    from sim.run_m1 import dc_spec_from_config
    assert dc_spec_from_config({}, None, []) is None
    with pytest.raises(ValueError):
        dc_spec_from_config({"dc_declare_tighter": ["DC_01"]}, None, [])
    with pytest.raises(ValueError):
        dc_spec_from_config({"dc_deferral": True}, None, [], n_warm=1)


def test_relief_share_at_the_acting_alone_peak():
    from sim.run_m1 import dc_relief_share
    sel = np.array([1000.0, 3000.0, 2000.0])
    ex = np.array([1000.0, 2000.0, 2000.0])          # 1,000 kW relief at step 1
    le_s = np.array([[10.0, 400.0, 10.0], [5.0, 5.0, 5.0]])
    le_e = np.array([[10.0, 150.0, 260.0], [5.0, 5.0, 5.0]])   # the data center (row 0) moved 250 kW off step 1
    out = dc_relief_share(sel, ex, le_s, le_e, [0])
    assert out["step"] == 1 and out["relief_mw"] == pytest.approx(1.0)
    assert out["dc_part_mw"] == pytest.approx(0.25) and out["dc_share"] == pytest.approx(0.25)
    assert dc_relief_share(sel, sel, le_s, le_s, [0])["dc_share"] is None


@pytest.mark.skipif(not HAS, reason="needs ../data with the session table")
def test_config_reads_the_simulators_sessions_and_tightens_only_the_declaration():
    from sim.data import load_slice
    from sim.run_m1 import DC_TIGHT_STEPS, dc_spec_from_config
    s = load_slice(CFG["data_dir"], CFG["start"], CFG["end"], CFG["slice_tz"])
    part = np.arange(len(s.ids))
    sp = dc_spec_from_config({**CFG, "dc_deferral": True, "dc_declare_tighter": ["DC_03"]}, s, part)
    assert sorted(sp["generated"]) == [f"DC_{i:02d}" for i in range(1, 11)]
    assert list(sp["declared"]) == ["DC_03"]
    for t, x in zip(sp["declared"]["DC_03"], sp["sessions"]["DC_03"]):
        assert t.deadline == x.start + DC_TIGHT_STEPS < x.deadline
    g = sp["generated"]["DC_03"]
    assert DC.check(g, g, sp["declared"]["DC_03"], 0.25) == []      # the recorded timing meets the declaration too
    with pytest.raises(ValueError):
        dc_spec_from_config({**CFG, "dc_deferral": True, "dc_declare_tighter": ["PVB_01"]}, s, part)


@pytest.mark.skipif(not HAS, reason="needs ../data with the session table")
def test_ceiling_with_deferral_solves_and_is_at_or_below_battery_only(tmp_path):
    import yaml

    from bench import m0
    cfg = {**CFG, "run_name": "t", "m0_dir": str(tmp_path / "m0"), "agents": "all"}
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(cfg))
    out = m0.main_dc(p)
    v = out["variants"]["cold"]
    assert v["battery_only_solved"] and v["with_deferral_solved"]
    assert v["with_deferral_le_battery_only"] and v["dc_rule_violations_total"] == 0
    assert 0.0 <= v["dc_share_of_ceiling_relief"] <= 1.0
    assert (tmp_path / "m0" / "ceiling_dc.json").exists()


@pytest.mark.skipif(not HAS, reason="needs ../data with the session table")
def test_config_carries_the_site_room_for_every_data_center():
    from sim.data import load_slice
    from sim.run_m1 import dc_spec_from_config
    s = load_slice(CFG["data_dir"], CFG["start"], CFG["end"], CFG["slice_tz"])
    sp = dc_spec_from_config({**CFG, "dc_deferral": True}, s, np.arange(len(s.ids)))
    assert sorted(sp["room"]) == sorted(sp["generated"])
    assert all(len(sp["room"][a]) == len(s.ts_utc) for a in sp["room"])
