"""sim/run_m1.net_harm: `shortfall_usd` (signed, unrounded) is written only when asked (staged D98), so existing
summaries stay byte-identical."""
import numpy as np

from sim.run_m1 import net_harm


def test_default_net_harm_has_no_shortfall_field():
    nh = net_harm(np.array([10.0, -5.0]), np.array([9.99997, -4.0]), ["A", "B"], ["A", "B"], 0)
    assert "shortfall_usd" not in nh and nh["by_agent_usd"] == {"A": 0.0, "B": 0.0}


def test_unrounded_shortfall_is_signed_and_exact():
    nh = net_harm(np.array([10.0, -5.0]), np.array([9.99997, -4.0]), ["A", "B"], ["A", "B"], 0, unrounded=True)
    assert nh["shortfall_usd"]["A"] == 10.0 - 9.99997 and nh["shortfall_usd"]["B"] == -1.0
    assert nh["by_agent_usd"] == {"A": 0.0, "B": 0.0}
