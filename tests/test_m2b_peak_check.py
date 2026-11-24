"""bench.m2b_peak_check: deferral on vs its off twin, exchange peak within 1 MW, unrounded; acting alone reported."""
import json

import pytest

from bench import m2b_peak_check as PC


def _summary(ex_full, alone_full, ex_c=None, alone_c=None):
    ev = {"peak_mw": ex_full}
    sf = {"peak_mw": alone_full}
    if ex_c is not None:
        ev["controllable"] = {"peak_mw": ex_c}
        sf["controllable"] = {"peak_mw": alone_c}
    return {"exchange_by_headroom_mw": {"100": ev}, "selfish": sf}


def test_within_the_band_passes_and_above_fails_unrounded():
    off = _summary(2000.0, 2100.0)
    assert PC.check_window(_summary(2001.0, 2300.0), off)["pass"]
    r = PC.check_window(_summary(2001.0000001, 2300.0), off)
    assert not r["pass"] and r["on_minus_off_mw"] > 1.0
    assert r["acting_alone_full_on_minus_off_mw"] == pytest.approx(200.0)


def test_controllable_measure_reads_the_d57_block():
    on, off = _summary(2500.0, 2600.0, 2000.5, 2550.0), _summary(2000.0, 2100.0, 2000.0, 2050.0)
    r = PC.check_window(on, off, "controllable")
    assert r["pass"] and r["on_mw"] == 2000.5 and r["on"]["exchange_full_mw"] == 2500.0
    assert r["acting_alone_controllable_on_minus_off_mw"] == pytest.approx(500.0)
    assert not PC.check_window(on, off, "full")["pass"]
    with pytest.raises(ValueError):
        PC.check_window(_summary(1.0, 1.0), _summary(1.0, 1.0), "controllable")


def test_a_missing_window_fails_the_run(tmp_path):
    for k, s in (("on", _summary(10.0, 10.0)), ("off", _summary(10.0, 10.0))):
        (tmp_path / f"m2b_a_{k}").mkdir()
        (tmp_path / f"m2b_a_{k}" / "summary.json").write_text(json.dumps(s))
    assert PC.run(tmp_path, ["a"])["pass"]
    res = PC.run(tmp_path, ["a", "b"])
    assert not res["pass"] and res["missing"] == ["b"]
    assert PC.main(["--cloud", str(tmp_path), "--windows", "a", "b"]) == 1
