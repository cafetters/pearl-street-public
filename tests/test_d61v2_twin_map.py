"""D61 v2 queue (bench/d61v2_run_queue.py, item 6): the cell-to-twin mapping covers every attack name. The pinned D61
queue's regex lacked renege_zero_b, so its feb02 max-rule reruns compared a cell with itself (INTEGRITY_FAIL)."""
import re

import pytest

from bench import d61v2_run_queue as Q

WINDOWS = ["jan12", "jan06", "feb02", "jun29", "mar14"]          # D61's four plus an arbitrary new one
PINNED_RE = (r"_(renege_full|renege_day|silent_deviate|silent|outage_stop|outage_run|misdeclare_power|"
             r"misdeclare_reserve|infeasible_declare)_")                # runs/d61/run_queue.py, pinned


def test_every_attack_name_is_covered():
    assert set(Q.ALL_ATTACKS) == set(Q.GROUP1) | set(Q.GROUP2) | set(Q.M0_ATTACKS)
    assert "renege_zero_b" in Q.ALL_ATTACKS


@pytest.mark.parametrize("attack", sorted(set(Q.GROUP1) | set(Q.GROUP2) | set(Q.M0_ATTACKS)))
@pytest.mark.parametrize("mk", ["m5", "m0"])
@pytest.mark.parametrize("suffix", ["", "_max"])
def test_each_cell_maps_to_its_window_and_margin_twin(attack, mk, suffix):
    for w in WINDOWS:
        assert Q.twin_of(f"h2h_c_d61_{w}_{attack}_{mk}{suffix}") == f"h2h_c_d61_{w}_twin_{mk}{suffix}"


def test_the_pinned_regex_misses_renege_zero_b_and_the_fix_does_not():
    cell = "h2h_c_d61_feb02_renege_zero_b_m0"
    assert re.sub(PINNED_RE, "_twin_", cell) == cell                 # the bug: the "twin" is the cell itself
    assert Q.twin_of(cell) == "h2h_c_d61_feb02_twin_m0"


def test_silent_deviate_is_not_read_as_silent():
    assert Q.twin_of("h2h_c_d61_jan06_silent_deviate_m5") == "h2h_c_d61_jan06_twin_m5"


@pytest.mark.parametrize("bad", ["h2h_c_d61_jan12_twin_m5", "h2h_c_d61_jan12_renege_m5",
                                 "h2h_c_d61_jan12_silent_misdeclare_power_m5"])
def test_unknown_or_ambiguous_names_fail_closed(bad):
    with pytest.raises(ValueError):
        Q.twin_of(bad)


def test_main_steps_pair_each_cell_with_the_twin_the_mapping_gives(monkeypatch):
    monkeypatch.setattr(Q, "WINDOWS", WINDOWS)
    checks = [args for name, args, *_ in Q.main_steps() if name.startswith("check_")]
    assert checks
    for args in checks:
        cell, twin = args[2].split("/")[-1], args[3].split("/")[-1]
        assert Q.twin_of(cell) == twin


def test_the_queue_refuses_to_run_without_windows():
    assert Q.main(["--dry-run"]) == 2
