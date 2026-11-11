"""No one is net harmed (PLAN.md section 8), checked against the run header's roster (CLAUDE.md)."""
import json

import numpy as np
import pytest

from sim.run_m1 import net_harm, run_variant
from tests.test_replay import CFG, synthetic_slice


def test_tolerance_is_max_of_one_percent_and_ten_dollars():
    ids = ["A", "B", "C", "D"]
    alone = np.array([5000.0, 5000.0, 100.0, -2000.0])
    with_ps = np.array([4951.0, 4949.0, 91.0, -2025.0])   # A: 49 < 50 ok; B: 51 > 50 harmed; C: 9 < 10 ok; D: 25 > 20 harmed
    r = net_harm(alone, with_ps, ids, ids, bystanders=3)
    assert r["agents_harmed"] == 2 and r["total_net_harm_usd"] == pytest.approx(51 + 25)
    assert r["by_agent_usd"] == {"A": 49.0, "B": 51.0, "C": 9.0, "D": 25.0}
    assert r["bystanders"]["count"] == 3 and not r["pass"]
    gains = net_harm(alone, alone + 1, ids, ids, 0)
    assert gains["agents_harmed"] == 0 and gains["total_net_harm_usd"] == 0 and gains["pass"]
    assert gains["by_agent_usd"]["A"] == 0.0


def test_fails_if_an_agent_on_the_roster_is_missing():
    r = net_harm(np.array([1.0]), np.array([1.0]), ["A"], ["A", "B"], 0)
    assert r["missing_from_check"] == ["B"] and not r["pass"]


def test_run_header_carries_the_roster(tmp_path):
    r = run_variant(CFG, synthetic_slice(), np.arange(2), tmp_path, headroom_mw=0.1)
    header = json.loads((tmp_path / "exchange_h0.1" / "trace.jsonl").read_text().splitlines()[0])
    assert header["roster"] == ["A", "B"] == r["header"]["roster"]
