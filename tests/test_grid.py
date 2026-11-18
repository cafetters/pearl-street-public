"""M2c step 2: sim/grid.py (PTDF, branches, zones, monitored elements) and the synthetic grid (sim/synth_grid.py).
Plan pass item 1: PTDF matches pandapower's DC solve; zone membership (overlap, thin zone); bench/zone_check keeps
its names so M0 reproduces."""
import numpy as np
import pandas as pd
import pytest

from sim import grid as G
from sim import synth_grid as S

pp = pytest.importorskip("pandapower")


def test_ptdf_matches_pandapower_dc_on_the_synthetic_grid():
    net = S.pandapower_net()
    br = G.branches_from_pp(net)
    buses = net.bus.index.to_numpy()
    P = G.ptdf(br, buses, S.SLACK_BUS)

    def flows(n):
        pp.rundcpp(n)
        return np.concatenate([n.res_line.p_from_mw.to_numpy(), n.res_trafo.p_hv_mw.to_numpy()])

    base = flows(net)
    for j, b in enumerate(buses):
        if b == S.SLACK_BUS:
            assert np.allclose(P[:, j], 0)
            continue
        n2 = S.pandapower_net()
        pp.create_sgen(n2, int(b), p_mw=1.0)
        got = flows(n2) - base
        assert np.allclose(got, P[:, j], atol=1e-6), (b, got, P[:, j])


def test_zones_overlap_and_the_thin_zone_has_one_asset():
    net = S.pandapower_net()
    br = G.branches_from_pp(net)
    P = G.ptdf(br, net.bus.index.to_numpy(), S.SLACK_BUS)
    z = G.zone_rows(P, br, net.bus.index.to_numpy(), S.ASSET_BUS, S.MONITORED)
    members = {k: sorted(a for a, v in row.items() if abs(v) >= G.PTDF_MIN) for k, row in z.items()}
    assert "B3" in members["tA"] and "B3" in members["tB"]                 # one asset in both transformer zones
    assert members[S.THIN] == ["B4"]                                         # the thin zone has one asset
    assert set(z) == set(S.MONITORED)


def test_monitored_picks_elements_at_or_above_the_threshold():
    ld = pd.DataFrame({"a": [50.0, 91.0], "b": [10.0, 20.0], "c": [85.0, 85.0]})
    assert G.monitored(ld, threshold=85.0) == ["a", "c"]
    assert G.monitored(ld, threshold=90.0) == ["a"]


def test_binding_counts_steps_and_hours_at_the_limit():
    s = pd.Series([89.999999, 90.0, 95.0, 100.0], index=pd.date_range("2023-01-12 06:00", periods=4, freq="15min",
                                                                         tz="UTC"))
    b = G.binding(s)
    assert b["hours_ge_90"] == 3 and b["hours_ge_100"] == 1 and b["max_pct"] == 100.0


def test_zone_check_keeps_its_names_for_m0():
    from bench import zone_check as Z
    assert Z.ptdf is G.ptdf and Z.branches is G.branches and Z.injections is G.injections
    assert Z.LIMIT_PCT == G.LIMIT_PCT == 90.0 and Z.PTDF_MIN == G.PTDF_MIN == 0.05


def test_synthetic_baseline_pushes_both_transformers_over_90_for_4_hours_and_relief_fits():
    T = S.STEPS_PER_DAY * 2
    ld = S.dc_loading(S.baseline_injections(T))
    for k in ("tA", "tB"):
        assert (ld[k] >= 90.0).sum() * 0.25 >= 4.0 * 2, k                  # at least 4 h per day
    need = S.relief_needed_mw(S.baseline_injections(T))
    cap = S.sign_correct_capacity_mw()
    for k in S.MONITORED:
        assert need[k] <= 0.7 * cap[k], (k, need[k], cap[k])                # D31 feasibility, normal case
    under = S.relief_needed_mw(S.baseline_injections(T, undersized=True))
    assert any(under[k] > S.sign_correct_capacity_mw()[k] for k in S.MONITORED)   # the undersized case
