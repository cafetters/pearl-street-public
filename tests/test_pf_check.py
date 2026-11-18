"""M2c step 5: the power-flow check of the final schedules (bench/pf_check.py). Pass item 2 on Engie's grid (zone
product off, D35): for every element and step, the exchange's AC loading is at most max(90%, acting-alone loading),
compared in MW (loading x rating) with a 1e-6 MW guard, unrounded. The check reads the roster from the run header and
fails on a missing agent. CLAUDE.md: a new check passes on a real saved trace before it is used."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bench import pf_check as PF

REPO = Path(__file__).resolve().parents[1]


def _ld(rows):
    return pd.DataFrame(rows, columns=["x", "y"])


def test_compare_counts_only_steps_the_exchange_makes_worse_above_the_limit():
    rating = pd.Series({"x": 100.0, "y": 10.0})
    alone = _ld([[80.0, 95.0], [92.0, 50.0], [50.0, 50.0]])
    ex = _ld([[89.9, 95.0], [93.0, 50.0], [91.0, 50.0]])
    c = PF.compare(ex, alone, rating)
    assert c["elements"]["x"]["worse_steps"] == 2             # 93 > 92 (alone above 90) and 91 > 90 (new)
    assert c["elements"]["x"]["worse_mw_max"] == pytest.approx(1.0)
    assert c["elements"]["y"]["worse_steps"] == 0             # 95 = alone 95: not worse
    assert c["violations"] == 2 and c["pass"] is False


def test_compare_guard_is_one_microwatt_on_unrounded_values():
    rating = pd.Series({"x": 100.0})
    alone = pd.DataFrame({"x": [90.0]})
    assert PF.compare(pd.DataFrame({"x": [90.0 + 0.5e-6]}), alone, rating)["pass"]          # 0.5e-6 MW over: guard
    assert not PF.compare(pd.DataFrame({"x": [90.0 + 2e-6]}), alone, rating)["pass"]        # 2e-6 MW: violation


def test_compare_refuses_unconverged_steps():
    rating = pd.Series({"x": 100.0})
    with pytest.raises(PF.CheckFailed):
        PF.compare(pd.DataFrame({"x": [np.nan]}), pd.DataFrame({"x": [10.0]}), rating)


def test_telemetry_pnet_reads_the_header_roster_and_fails_on_a_missing_agent(tmp_path):
    iso = ["2023-01-12T06:00:00Z", "2023-01-12T06:15:00Z"]
    lines = [{"event": "header", "roster": ["A", "B"]}]
    for t in iso:
        for a, v in (("A", 5.0), ("B", -3.0)):
            lines.append({"msg_type": "telemetry", "t_utc": t, "payload": {"asset_id": f"{a}/bess", "net_kw": v}})
    p = tmp_path / "trace.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    ids, pnet = PF.telemetry_pnet(p, iso)
    assert list(ids) == ["A", "B"] and pnet.tolist() == [[5.0, 5.0], [-3.0, -3.0]]
    lines.pop()                                                 # B's last reading
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    with pytest.raises(PF.CheckFailed):
        PF.telemetry_pnet(p, iso)


REAL = REPO.parent / "pearl-street" / "runs" / "cloud" / "h2h_c_bp48_jan12_twin_m5"


@pytest.mark.skipif(not (REAL / "summary.json").exists() or not (REPO.parent / "data" / "grid_topology_pandapower.json").exists(),
                    reason="needs the saved jan12 v2 twin run and ../data")
def test_real_saved_trace_jan12_twin():
    res = PF.check_run(REAL)
    assert res["roster_size"] == 80 and res["steps"] == 192
    assert res["converged_steps"] == {"exchange": 192, "acting_alone": 192}
    assert set(res) >= {"pass", "violations", "elements", "dispatch", "elapsed_s"}
    assert len(res["elements"]) == 245                          # every line and trafo


def test_run_m1_adds_the_network_block_only_when_pf_check_is_set(monkeypatch, tmp_path):
    from sim import run_m1
    calls = []
    monkeypatch.setattr(PF, "check_run", lambda d, data_dir=None: calls.append((d, data_dir)) or {"pass": True})
    assert run_m1.network_block({"data_dir": "x"}, tmp_path) is None and calls == []
    assert run_m1.network_block({"data_dir": "x", "pf_check": True}, tmp_path) == {"pass": True}
    assert calls == [(tmp_path, "x")]
