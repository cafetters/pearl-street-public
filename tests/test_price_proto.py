"""Price-mode prototype (bench/price_mode_proto.py). Offline: a synthetic two-battery slice, no Engie files."""
import json

import numpy as np
import pandas as pd
import pytest

from bench import price_mode_proto as P
from bench.m0 import load_bench, use_slice
from sim.data import META_COLS, Slice

T = 16


def synth_slice():
    ts = pd.date_range("2023-01-02 06:00", periods=T, freq="15min", tz="UTC")
    price = np.full(T, 20.0)
    price[10:13] = 120.0
    pv = np.zeros((2, T))
    pv[:, 4:8] = [[30.0], [10.0]]
    agent = {"load": np.full((2, T), 5.0), "pv": pv, "curt": np.zeros((2, T)), "bess": np.zeros((2, T)),
             "soc": np.full((2, T), 50.0), "soh": np.full((2, T), 100.0), "pnet": np.zeros((2, T))}
    system = {"price_rt": price, "price_da": price, "system_load": np.linspace(1.0, 1.2, T),
              "net_disp": np.ones(T), "bg_load_scale": np.ones(T)}
    meta = pd.DataFrame({"entity_id": ["A", "B"], "entity_type": "pv_bess", "bess_power_kw": [50.0, 20.0],
                         "bess_energy_kwh": [100.0, 40.0], "eta_inverter": 0.98, "eta_battery_rt": 0.9,
                         "soc0": 50.0, "poi_limit_kw": 100.0, "self_discharge_per_h": 0.0, "soh0": 100.0,
                         "peak_kw": 50.0, "bus": [1, 2]})
    assert set(META_COLS) <= set(meta.columns)
    return Slice(ids=np.array(["A", "B"]), ts_utc=ts, dt_h=0.25, agent=agent, system=system, meta=meta)


def synth_models():
    s = synth_slice()
    B = load_bench(s)
    use_slice(B, s)
    qps, sfx, fun = P.capture_agents(B)
    return B, qps, sfx, fun


@pytest.fixture(scope="module")
def bench():
    return synth_models()


EPS = 0.05 * 0.25 / 1000.0                         # 0.05 $/MWh kink, in $/kW-step


def test_rho0_matches_build(bench):
    B, qps, sfx, fun = bench
    for q, f in zip(qps, fun):
        q.set_rho(0.0)
        v, obj, prox = q.solve(np.zeros(T))
        assert obj == pytest.approx(f, rel=1e-6, abs=1e-9)
        assert prox == 0.0 and q.m == 0


def test_selfish_plan_cycles(bench):
    B, _, sfx, _ = bench
    assert sfx["d"][:, 10:13].sum() > 1.0          # discharges into the price spike, so the test LP is not trivial


def test_breakpoints():
    """Secants run from b1 to R in steps of at most sqrt 2; the kink at zero is at most eps."""
    _, qps, _, _ = synth_models()
    for rho in (1e-6, 1e-3):
        for q in qps:
            q.set_rho(rho, EPS)
            r = q.b[2:] / q.b[1:-1]
            assert q.b[0] == 0.0 and q.b[-1] == pytest.approx(q.R)
            assert (r <= np.sqrt(2) + 1e-12).all() and (r > 1).all()
            assert q.deadzone <= EPS + 1e-15
            assert q.b[1] <= q.R / 256 + 1e-12


def test_prox_objective_is_the_secant_interpolant(bench):
    """At the solution, the objective is cost0 v + pi'x + PWL(x - p); the PWL term equals the secant interpolant
    (segments fill in order), lies at or above the exact square, and the solution beats the selfish plan on the
    same objective."""
    B, qps, sfx, _ = bench
    rng = np.random.default_rng(0)
    rho, pi = 1e-3, np.r_[np.zeros(10), np.full(3, 0.05), np.zeros(T - 13)]
    for i, q in enumerate(qps):
        p = rng.uniform(-10, 10, T)
        q.set_rho(rho, EPS)
        v, obj, prox = q.solve(pi, p)
        d = P.net_x(v, T) - p
        assert prox == pytest.approx(P.pwl_square(d, q.b, rho).sum(), rel=1e-6, abs=1e-9)
        assert prox >= rho / 2 * (d ** 2).sum() - 1e-9
        assert obj == pytest.approx(q.cost0 @ v + pi @ P.net_x(v, T) + prox, rel=1e-6, abs=1e-6)
        v0 = np.concatenate([sfx[k][i] for k in "cdsu"])
        full0 = q.cost0 @ v0 + pi @ P.net_x(v0, T) + P.pwl_square(P.net_x(v0, T) - p, q.b, rho).sum()
        assert obj <= full0 + 1e-6


def test_prox_at_selfish_with_no_price_keeps_selfish(bench):
    B, qps, sfx, _ = bench
    X0 = sfx["c"] - sfx["d"] + sfx["u"]
    for i, q in enumerate(qps):
        q.set_rho(1e-2, EPS)
        v, _, prox = q.solve(np.zeros(T), X0[i])
        assert np.allclose(P.net_x(v, T), X0[i], atol=1e-3)
        assert prox == pytest.approx(0.0, abs=1e-9)


def test_strong_prox_holds_the_plan_against_a_small_price(bench):
    """A large rho damps the response to the same price that moves the rho = 0 plan."""
    B, qps, sfx, _ = bench
    X0 = sfx["c"] - sfx["d"] + sfx["u"]
    pi = np.zeros(T)
    pi[11] = 0.5
    for i, q in enumerate(qps):
        q.set_rho(0.0)
        free = np.abs(P.net_x(q.solve(pi)[0], T) - X0[i]).sum()
        q.set_rho(1.0, EPS)
        held = np.abs(P.net_x(q.solve(pi, X0[i])[0], T) - X0[i]).sum()
        assert held < free


def test_peak_price_moves_discharge_onto_priced_steps(bench):
    B, qps, sfx, _ = bench
    pi = np.zeros(T)
    pi[2:4] = 1.0                                  # $1/kW-step = $4,000/MWh, far above the energy price
    for i, q in enumerate(qps):
        q.set_rho(0.0)
        v, _, _ = q.solve(pi)
        assert P.net_x(v, T)[2:4].sum() < (sfx["c"][i] - sfx["d"][i] + sfx["u"][i])[2:4].sum() - 1.0


def test_price_update_is_projected():
    pi = np.array([0.0, 1.0, 1.0])
    out = P.price_update(pi, agg=np.array([10.0, 0.0, -1e6]), target=np.zeros(3), alpha=0.1)
    assert out[0] == pytest.approx(1.0)            # rises over target
    assert out[1] == pytest.approx(1.0)            # holds at target
    assert out[2] == 0.0                           # falls below target, never negative


def test_classify_labels():
    k = np.arange(40)
    flat = np.zeros((40, 4))
    cycle = flat + np.where(k % 2, 30.0, -30.0)[:, None]
    assert P.classify(cycle, cycle.max(1), np.ones(40), True)[0] == "oscillating"
    assert P.classify(cycle, cycle.max(1), np.ones(40), True)[1] == pytest.approx(60.0)
    slide = flat + (100.0 - k)[:, None]
    assert P.classify(slide, slide.max(1), np.ones(40), True)[0] == "drifting"
    assert P.classify(slide, slide.max(1), k * 1.0, False)[0] == "diverging"
    assert P.classify(slide, slide.max(1), k * 1.0, True)[0] == "drifting"


def test_residual_counts_priced_steps_below_target():
    agg, target = np.array([5.0, -3.0, -3.0]), np.zeros(3)
    r = P.residual(agg, target, pi=np.array([0.0, 0.0, 1.0]))
    assert list(r) == [5.0, 0.0, -3.0]           # unpriced under-target is fine; priced under-target is off


def test_rb_update():
    kw = dict(mu=10, tau=2, lo=0.1, hi=10.0)
    assert P.rb_update(1.0, r=100.0, d=1.0, **kw) == 2.0     # residual dominates: raise
    assert P.rb_update(1.0, r=1.0, d=100.0, **kw) == 0.5     # plans swing: lower
    assert P.rb_update(1.0, r=5.0, d=1.0, **kw) == 1.0       # balanced within mu: hold
    assert P.rb_update(8.0, r=100.0, d=1.0, **kw) == 10.0    # bounded
    assert P.rb_update(0.15, r=1.0, d=100.0, **kw) == 0.1


def test_rounds_within():
    e = [100.0, 50.0, 4.0, 8.0, 0.5, 3.0, 0.9, 0.8]          # index 0 is the selfish round
    assert P.rounds_within(e, 5) == (2, 4)
    assert P.rounds_within(e, 1) == (4, 6)
    assert P.rounds_within(e, 0.1) == (None, None)
    assert P.rounds_within([9.0, 2.0, 3.0, 9.0], 5) == (1, None)   # left the band at the end: not settled
    assert P.rounds_within([9.0, 0.0, 0.0], 5) == (1, 1)


def test_da_spread_is_mean_daily_range():
    s = synth_slice()
    da = s.system["price_da"].copy()
    da[:] = 30.0
    da[3], da[-1] = 50.0, 10.0
    s.system["price_da"] = da
    day = s.ts_utc.tz_convert("America/Chicago").date
    ranges = [np.ptp(da[day == d]) for d in np.unique(day)]
    assert P.da_spread(s, "America/Chicago") == pytest.approx(np.mean(ranges))


def test_centre_shift_is_the_projected_mean_residual():
    """Gap / N where the step stays priced, 0 where unpriced and under target, clipped where the price hits 0."""
    pi_old = np.array([0.0, 1.0, 1.0, 0.0])
    agg, target, alpha, N = np.array([10.0, -2.0, -1e6, -5.0]), np.zeros(4), 0.1, 2
    pi_new = P.price_update(pi_old, agg, target, alpha)
    shift = P.centre_shift(pi_new, pi_old, alpha, N)
    assert shift == pytest.approx([5.0, -1.0, -5.0, 0.0])
    r = P.residual(agg, target, pi_new) / N            # same as the projected residual wherever pi stays positive
    assert shift[[0, 1, 3]] == pytest.approx(r[[0, 1, 3]])


def test_rb_scaled_dual_stops_the_runaway():
    kw = dict(mu=10, tau=2, lo=0.001, hi=10.0)
    # at 8 x rho0 the raw change still invites a raise; the scaled dual residual (8 x 2 kW) holds
    assert P.rb_update(8.0, 100.0, P.rb_dual_kw(8.0, 1.0, 2.0, scaled=False), **kw) == 10.0
    assert P.rb_update(8.0, 100.0, P.rb_dual_kw(8.0, 1.0, 2.0, scaled=True), **kw) == 8.0
    assert P.rb_dual_kw(1.0, 1.0, 2.0, scaled=True) == 2.0           # at rho0 the two rules agree
    assert P.rb_update(8.0, 1e4, P.rb_dual_kw(8.0, 1.0, 1.0, scaled=True), **kw) == 10.0   # the cap binds
    assert P.rb_update(1.0, 1.0, P.rb_dual_kw(1.0, 1.0, 100.0, scaled=True), **kw) == 0.5


def test_shifted_centre_needs_the_extra_reach():
    """A centre shifted past R is infeasible on the base breakpoints; with extra_kw covering it the agent solves and
    the prox term is still the secant interpolant."""
    B, qps, sfx, _ = synth_models()
    X0 = sfx["c"] - sfx["d"] + sfx["u"]
    rho = 1e-3
    for i, q in enumerate(qps):
        S = 1.5 * q.R
        q.set_rho(rho, EPS)
        with pytest.raises(RuntimeError):
            q.solve(np.zeros(T), X0[i] - S)
        q.extra_kw = 1.1 * S
        q.set_rho(rho, EPS)
        v, _, prox = q.solve(np.zeros(T), X0[i] - S)
        d = P.net_x(v, T) - (X0[i] - S)
        assert np.abs(d).max() > q.R
        assert prox == pytest.approx(P.pwl_square(d, q.b, rho).sum(), rel=1e-6, abs=1e-9)
        assert q.b[-1] == pytest.approx(q.R + q.extra_kw)


def test_first_streak():
    assert P.first_streak([True, False, True, True, True, False], 3) == 3
    assert P.first_streak([True, True, False, True], 3) is None


def test_d49_entry():
    row = {"settled_within_5mw": 18, "max_pi_usd_mwh": 22.0, "max_pi_star_usd_mwh": 20.6, "harmed_c_n": 0}
    t = P.d49_entry(row)
    assert t["settled_5mw"] and t["price_10pct"] and t["no_harm_c"] and t["pass"]
    v2_h100 = {**row, "max_pi_usd_mwh": 34.4}                        # v2: 67% over the dual
    assert not P.d49_entry(v2_h100)["price_10pct"] and not P.d49_entry(v2_h100)["pass"]
    assert not P.d49_entry({**row, "settled_within_5mw": None})["settled_5mw"]
    assert not P.d49_entry({**row, "settled_within_5mw": float("nan")})["settled_5mw"]
    assert not P.d49_entry({**row, "harmed_c_n": 50})["no_harm_c"]


def _saved_setting(sdir, label, ids, losses_c, tol=10.0):
    """A minimal saved setting: per-agent table and summary JSON as main() writes them."""
    a = pd.DataFrame({"agent_id": ids, "tolerance_usd": tol, "loss_a_usd": 0.0, "loss_b_usd": 0.0,
                      "loss_c_usd": losses_c})
    a.to_csv(sdir / f"agents_{label}.csv", index=False)
    n = {k: int((a[f"loss_{k}_usd"] > tol).sum()) for k in "abc"}
    (sdir / f"{label}.json").write_text(json.dumps({f"harmed_{k}_n": v for k, v in n.items()}))


def test_check_roster_passes_on_the_full_roster(tmp_path):
    roster = ["A", "B", "007"]
    _saved_setting(tmp_path, "h100_free-rb", roster, [0.0, 50.0, 0.0])
    assert P.check_roster(tmp_path, ["h100_free-rb"], roster) == {"h100_free-rb": {"agents": 3, "harmed_c_n": 1}}


def test_check_roster_fails_on_a_missing_agent(tmp_path):
    roster = ["A", "B", "C"]
    _saved_setting(tmp_path, "h100_free-rb", ["A", "B", "C"], [0.0, 0.0, 0.0])
    _saved_setting(tmp_path, "h150_free-rb", ["A", "B"], [0.0, 0.0])          # C missing: its harm is unseen
    with pytest.raises(SystemExit, match=r"h150_free-rb.*missing \['C'\]"):
        P.check_roster(tmp_path, ["h100_free-rb", "h150_free-rb"], roster)


def test_check_roster_fails_on_a_harm_count_the_table_does_not_support(tmp_path):
    roster = ["A", "B"]
    _saved_setting(tmp_path, "h100_free-rb", roster, [0.0, 50.0])
    (tmp_path / "h100_free-rb.json").write_text(json.dumps({"harmed_a_n": 0, "harmed_b_n": 0, "harmed_c_n": 0}))
    with pytest.raises(SystemExit, match=r"harmed \(c\) recount 1"):
        P.check_roster(tmp_path, ["h100_free-rb"], roster)
