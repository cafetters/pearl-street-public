"""D57 option 1 (owner, Nov 6): the headline peak, share of the M0 ceiling and feasibility over the controllable steps
(from the first binding call's window on), opt-in with `peak_steps: controllable`; the full slice stays the footnote.
No free dumping into the uncounted hours: the controllable M0 caps net load on pre-from steps at the recorded
baseline's maximum there, and runs report the pre-from peaks and pre_from_rebound. Defaults change nothing."""
import json

import numpy as np
import pytest
import yaml

from bench import m0 as M0


def _bench(tmp_path):
    """bench.bench loaded on a tiny synthetic slice (2 agents x 8 steps), as tests/test_adapter.py does."""
    from dataclasses import replace

    from sim import data as D
    from tests.test_adapter import make_data
    make_data(tmp_path, "2023-01-02 06:00", 96, ids=("A", "B"))
    full = D.load_slice(tmp_path, "2023-01-02", "2023-01-02")
    s = replace(full, ts_utc=full.ts_utc[:8], agent={k: v[:, :8] for k, v in full.agent.items()},
                system={k: v[:8] for k, v in full.system.items()})
    B = M0.load_bench(s)
    M0.use_slice(B, s)
    return B


def _record_lp(B, monkeypatch):
    seen = []
    orig = B.linprog

    def rec(c, A_ub=None, b_ub=None, A_eq=None, b_eq=None, bounds=None, method=None):
        seen.append({"c": np.array(c), "A_ub": A_ub.toarray(), "b_ub": np.array(b_ub), "A_eq": A_eq.toarray(),
                     "b_eq": np.array(b_eq), "bounds": np.array(bounds)})
        return orig(c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method=method)
    monkeypatch.setattr(B, "linprog", rec)
    return seen


# ---------------------------------------------------------------- bench.build
@pytest.mark.parametrize("mode", [{"peak": True}, {"cap_kw": 150000.0}])
def test_build_with_default_peak_from_is_the_same_lp(tmp_path, monkeypatch, mode):
    B = _bench(tmp_path)
    seen = _record_lp(B, monkeypatch)
    B.build([0, 1], 0, B.T, B.S0, B.S0, **mode)
    B.build([0, 1], 0, B.T, B.S0, B.S0, peak_from=0, pre_cap_kw=None, **mode)
    a, b = seen
    for k in a:
        np.testing.assert_array_equal(a[k], b[k])


def test_peak_from_drops_earlier_coupling_rows_and_a_spike_there_no_longer_moves_the_ceiling(tmp_path):
    B = _bench(tmp_path)
    base = B.SL.copy()
    cold = M0.solve(B, peak=True)["z"][0]
    ctrl = M0.solve(B, peak=True, peak_from=4)
    assert ctrl["dual"].shape == (B.T,) and np.all(ctrl["dual"][:4] == 0)
    B.SL = base.copy()
    B.SL[1] += 50000.0                                               # a spike before peak_from
    assert M0.solve(B, peak=True)["z"][0] > cold + 1000.0             # the cold ceiling sees it
    assert M0.solve(B, peak=True, peak_from=4)["z"][0] == pytest.approx(ctrl["z"][0], abs=1e-6)   # this one doesn't
    B.SL = base.copy()
    B.SL[6] += 50000.0                                               # a spike after it: both see it
    assert M0.solve(B, peak=True, peak_from=4)["z"][0] > ctrl["z"][0] + 1000.0


def test_pre_from_cap_stops_the_ceiling_dumping_load_into_the_uncounted_hours(tmp_path):
    """Owner's addition, red first: without the pre-from cap row the peak-min LP recharges the batteries on pre-from
    steps above the baseline there, to lower the controllable peak; with it, it can't."""
    B = _bench(tmp_path)
    B.PV = np.zeros_like(B.PV)                                       # no PV: net load = SL - sum(d - c)
    B.SL = np.array([1000.0] * 4 + [1250.0, 1300.0, 1250.0, 1250.0])   # pre-from flat at 1000; a spike at step 5
    pre_cap = float(B.SL[:4].max())                                  # the recorded baseline's max over pre-from steps
    free = M0.solve(B, peak=True, peak_from=4)
    capped = M0.solve(B, peak=True, peak_from=4, pre_cap_kw=pre_cap)
    nf = M0.net_load(B, **M0.dispatch(free["x"]))
    nc = M0.net_load(B, **M0.dispatch(capped["x"]))
    assert nf[:4].max() > pre_cap + 1.0                              # red: charged into the uncounted hours
    assert nc[:4].max() <= pre_cap + 1e-6                            # with the cap row: can't
    assert capped["z"][0] >= free["z"][0] - 1e-6                     # the controllable peak is no lower
    assert capped["z"][0] > free["z"][0] + 1.0                       # and here the dumping was worth something


def test_pre_cap_needs_a_controllable_start(tmp_path):
    B = _bench(tmp_path)
    with pytest.raises(ValueError):
        B.build([0, 1], 0, B.T, B.S0, B.S0, peak=True, pre_cap_kw=100.0)
    with pytest.raises(ValueError):
        B.build([0, 1], 0, B.T, B.S0, B.S0, peak=True, peak_from=B.T)


# ---------------------------------------------------------------- sim.run_m1
def test_controllable_from_is_the_first_binding_deadline_at_04_local_for_both_spans():
    from sim.run_m1 import controllable_from
    from tests.test_d58_margin import _cfg
    from tests.test_nomination_span import two_day_slice
    s = two_day_slice()
    for span in ("horizon", "operating_day"):
        k = controllable_from(_cfg(run_name="x", nomination_span=span), s)
        assert str(s.ts_utc[k].tz_convert("America/Chicago").time()) == "04:00:00" and k == 16
    assert controllable_from(_cfg(run_name="x", nominations=False, rounds=1), s) == 0


def test_peak_steps_values_and_refusals():
    from sim.run_m1 import peak_steps
    assert peak_steps({}) == "all" and peak_steps({"peak_steps": "controllable"}) == "controllable"
    with pytest.raises(ValueError):
        peak_steps({"peak_steps": "window"})
    with pytest.raises(ValueError):
        peak_steps({"peak_steps": "controllable", "warmup_days": 1})


def test_controllable_block_reports_the_pre_from_peaks_and_the_rebound_flag():
    from sim.clock import Clock
    from sim.run_m1 import TZ, controllable_block
    from tests.test_nomination_span import two_day_slice
    clock = Clock(two_day_slice().ts_utc, TZ)
    T = len(clock.iso)
    m0c = {"baseline_recorded": {"peak_mw": 3.0}, "ceiling_15min": {"peak_min_mw": 1.0}}
    selfish = np.full(T, 2000.0)
    ex = np.full(T, 1500.0)
    ex[3] = 3500.0                                                  # a pre-from step above the selfish one
    b = controllable_block(ex, selfish, clock, 16, m0c)
    assert b["peak_mw"] == 1.5 and b["pre_from_peak_mw"] == 3.5 and b["selfish_pre_from_peak_mw"] == 2.0
    assert b["pre_from_rebound"] is True and b["share_of_m0_ceiling"] == pytest.approx(0.75)
    assert b["share_of_selfish_to_ceiling_gap"] == pytest.approx(0.5)
    ex[3] = 2900.0                                                  # within 1 MW: no rebound
    assert controllable_block(ex, selfish, clock, 16, m0c)["pre_from_rebound"] is False


# ---------------------------------------------------------------- end to end: M0 --controllable and run_m1.main
@pytest.fixture
def repo(tmp_path, monkeypatch):
    """run_m1.main and bench.m0 on the synthetic two-day slice, with REPO pointed at tmp_path."""
    import sim.run_m1 as R
    from tests.test_d58_margin import _cfg
    from tests.test_nomination_span import two_day_slice
    s = two_day_slice()
    monkeypatch.setattr(R, "REPO", tmp_path)
    monkeypatch.setattr(R, "load_slice", lambda *a, **k: s)
    monkeypatch.setattr(M0, "REPO", tmp_path)
    monkeypatch.setattr(M0, "load_slice", lambda *a, **k: s)

    def write(name, **kw):
        cfg = _cfg(run_name=name, data_dir="../data", start="2023-01-02", end="2023-01-03",
                   slice_tz="America/Chicago", agents="all", headroom_mw=0.03, m0_dir=str(tmp_path / "m0"),
                   nomination_span="operating_day", **kw)
        p = tmp_path / f"{name}.yaml"
        p.write_text(yaml.safe_dump(cfg))
        return p
    return {"tmp": tmp_path, "write": write, "R": R, "s": s}


def _keys(d, prefix=""):
    out = set()
    if isinstance(d, dict):
        for k, v in d.items():
            out.add(k)
            out |= _keys(v)
    return out


def test_end_to_end_default_adds_nothing_and_controllable_reports_the_block(repo):
    R, tmp = repo["R"], repo["tmp"]
    M0.main_controllable(repo["write"]("m0cfg"))
    mc = json.loads((tmp / "m0" / "ceiling_controllable.json").read_text())
    assert mc["controllable"]["peak_from_step"] == 16 and mc["controllable"]["pre_from_cap_mw"] > 0
    assert mc["controllable"]["pre_from_peak_mw"]["peak_min"] <= mc["controllable"]["pre_from_cap_mw"] + 1e-6
    assert mc["controllable"]["pre_from_peak_mw"]["value"] <= mc["controllable"]["pre_from_cap_mw"] + 1e-6
    assert not (tmp / "m0" / "ceiling.json").exists() and not (tmp / "m0" / "run.json").exists()   # cold files never written
    R.main(repo["write"]("plain"))
    plain = json.loads((tmp / "runs" / "plain" / "summary.json").read_text())
    assert not ({"controllable", "peak_steps", "pre_from_rebound"} & _keys(plain))
    R.main(repo["write"]("ctrl", peak_steps="controllable"))
    ctrl = json.loads((tmp / "runs" / "ctrl" / "summary.json").read_text())
    ev, sf = ctrl["exchange_by_headroom_mw"]["0.03"], ctrl["selfish"]
    net = np.array(ctrl["series"]["exchange_h0.03_rinfo_kw"])
    b = ev["controllable"]
    assert b["peak_mw"] == pytest.approx(net[16:].max() / 1e3) and b["pre_from_peak_mw"] == pytest.approx(net[:16].max() / 1e3)
    assert b["pre_from_rebound"] == (b["pre_from_peak_mw"] > b["selfish_pre_from_peak_mw"] + 1.0)
    assert sf["controllable"]["peak_mw"] == pytest.approx(np.array(ctrl["series"]["selfish_kw"])[16:].max() / 1e3)
    assert ctrl["peak_steps"]["from_step"] == 16
    for k in ("peak_mw", "share_of_m0_ceiling", "share_of_selfish_to_ceiling_gap"):      # the footnote is unchanged
        assert ev[k] == plain["exchange_by_headroom_mw"]["0.03"][k]


def test_end_to_end_fails_closed_without_or_with_a_mismatched_controllable_ceiling(repo):
    R, tmp = repo["R"], repo["tmp"]
    with pytest.raises(ValueError, match="ceiling_controllable"):
        R.main(repo["write"]("ctrl", peak_steps="controllable"))
    M0.main_controllable(repo["write"]("m0cfg"))
    p = tmp / "m0" / "ceiling_controllable.json"
    mc = json.loads(p.read_text())
    mc["controllable"]["peak_from_utc"] = "2023-01-02T09:00:00Z"
    p.write_text(json.dumps(mc))
    with pytest.raises(ValueError, match="controllable steps"):
        R.main(repo["write"]("ctrl2", peak_steps="controllable"))


def test_feasibility_controllable_leaves_pre_from_steps_uncapped(repo, monkeypatch):
    import bench.h2h_feasibility as F
    monkeypatch.setattr(F, "load_slice", lambda *a, **k: repo["s"])
    seen = {}
    orig = F.feasible

    def rec(B, t, tol_kw=F.TOL_KW):
        seen.setdefault("t", []).append(np.array(t))
        return orig(B, t, tol_kw)
    monkeypatch.setattr(F, "feasible", rec)
    a = F.check(repo["write"]("f"))
    b = F.check(repo["write"]("f"), controllable=True)
    t0, t1 = seen["t"]
    assert np.all(np.isnan(t1[:16])) and np.array_equal(t0[16:], t1[16:], equal_nan=True)
    assert b["controllable_from_step"] == 16 and "controllable_from_step" not in a
