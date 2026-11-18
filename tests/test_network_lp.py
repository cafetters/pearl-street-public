"""M2c step 6: the network-constrained LP ceiling at the 90% limit (bench/bench.py build(network=...), bench/m0.py).
Rows per monitored element and step: sign x sum_a PTDF_a x (d - c - u)_a / 1000 - slack <= room (MW), anchored on the
baseline loading (AC on Engie's grid; DC on the synthetic grid). Placed before the coupling rows, so m0.solve's dual
indexing is unchanged. Pass item 3: the ceiling solves, its peak is at or above the unconstrained one, slack is
reported per element."""
import numpy as np
import pytest

from bench import m0 as M0
from sim import synth_grid as S


@pytest.fixture(scope="module")
def bench():
    s = S.run_slice(days=1)
    B = M0.load_bench(s)
    M0.use_slice(B, s)
    return s, B


def synth_network(s, undersized=False, room_scale=1.0):
    """DC-anchored rows on the synthetic grid: base flows from the background load, batteries idle at base."""
    T = len(s.ts_utc)
    rows = S.zone_rows()
    f = S.flows_mw(S.baseline_injections(T, undersized))
    rating = S.branch_table().set_index("key").rating_mva
    g = np.array([[rows[k][a] for a in s.ids] for k in S.MONITORED])
    sign = np.sign(f[S.MONITORED].to_numpy().T)
    rhs = np.array([(0.9 * rating[k] - f[k].abs().to_numpy()) * room_scale for k in S.MONITORED])
    return {"keys": list(S.MONITORED), "g": g, "sign": sign, "rhs": rhs}


def network_flows(s, x):
    """Element |flow| (MW) per step for a solved dispatch (synthetic grid, DC)."""
    T = len(s.ts_utc)
    inj = S.baseline_injections(T)
    net_kw = x[:, 1] - x[:, 0] - x[:, 3]
    for i, a in enumerate(s.ids):
        inj[:, S.ASSET_BUS[a]] += net_kw[i] / 1000.0
    return S.flows_mw(inj).abs()


def test_network_rows_hold_and_the_peak_is_at_or_above_unconstrained(bench):
    s, B = bench
    free = M0.solve(B, peak=True)
    net = M0.solve(B, peak=True, network=synth_network(s))
    assert net["z"][0] >= free["z"][0] - 1e-6
    f = network_flows(s, net["x"])
    rating = S.branch_table().set_index("key").rating_mva
    for i, k in enumerate(S.MONITORED):
        over = f[k].to_numpy() - 0.9 * rating[k]
        assert (over <= net["network_slack_mw"][i] + 1e-6).all()
    assert len(net["dual"]) == B.T                                            # coupling rows still last


def test_non_binding_rows_change_nothing(bench):
    s, B = bench
    free = M0.solve(B, peak=True)
    loose = synth_network(s)
    loose["rhs"] = loose["rhs"] + 1e6
    net = M0.solve(B, peak=True, network=loose)
    assert net["z"][0] == pytest.approx(free["z"][0], abs=1e-6)
    assert np.allclose(net["dual"], free["dual"], atol=1e-9)
    assert float(np.abs(net["network_slack_mw"]).max()) == 0.0


def test_undersized_rows_stay_feasible_with_reported_slack(bench):
    s, B = bench
    net = M0.solve(B, peak=True, network=synth_network(s, undersized=True))
    by = net["network_slack_by_element_mw"]
    assert set(by) == set(S.MONITORED)
    assert by["l14"] > 0.0                                                    # B4 alone cannot relieve l14


DATA = __import__("pathlib").Path(__file__).resolve().parents[1].parent / "data"


@pytest.mark.skipif(not (DATA / "grid_topology_pandapower.json").exists(), reason="needs ../data with the grid files")
def test_real_grid_rows_jan12_are_anchored_on_the_ac_baseline():
    from sim.data import load_slice
    s = load_slice(DATA, "2023-01-12", "2023-01-13")
    nw = M0.network_rows_ac(s, DATA)
    assert nw["keys"] == ["l43"]                                  # the only element at or above 80% that window
    g, sign, rhs = nw["g"], nw["sign"], nw["rhs"]
    assert g.shape == (1, 80) and sign.shape == rhs.shape == (1, len(s.ts_utc))
    bess = s.agent["bess"]
    base = np.maximum(bess, 0) - np.maximum(-bess, 0) - s.agent["curt"]
    lhs = sign[0] * (g[0] @ base) / 1000.0
    assert (lhs <= rhs[0] + 1e-9).all()                           # the recorded dispatch is feasible (below 90%)
