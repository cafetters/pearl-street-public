"""bench/tolerances.py: the money tolerance for a recompute from a trace's logged prices is price rounding (half a unit
in the 6th decimal, $/MWh) x the price-weighted absolute energy, plus float noise. It holds on synthetic worst cases
(every logged price off by just under half a unit, all errors the same sign; exact halves; the max rule with ties,
near-ties and several calls) and is far below a $0.01 rule error at real energies."""
import json
from pathlib import Path

import numpy as np
import pytest

from bench import tolerances as TL
from bench.item7_resettle import max_first_call_steps

K = TL.STEP_H / 1000.0
D = TL.PRICE_ROUNDING_USD_MWH
REPO = Path(__file__).resolve().parents[1]


def test_constants_match_the_exchange_s_price_logging():
    src = (REPO / "exchange" / "core.py").read_text(encoding="utf-8")
    assert f"price = [round(new[pos[t]] / K, {TL.PRICE_DECIMALS})" in src     # logged $/MWh, 6 decimals
    assert TL.PRICE_DECIMALS == 6 and D == 5e-7
    assert "p_1 > p_k + 1e-9" in src and TL.HIT_EPS_USD_MWH == 1e-9            # the max rule's cap comparison


def _logged_and_true(rng, n, sign):
    """Logged prices on the 6-decimal grid and true prices just under half a unit away, on the side of `sign`."""
    logged = np.round(rng.uniform(0.0, 2000.0, n), TL.PRICE_DECIMALS)
    true = logged + np.sign(sign) * D * (1 - 1e-5)
    assert all(round(float(t), TL.PRICE_DECIMALS) == float(lg) for t, lg in zip(true, logged))
    return logged, true


def test_one_price_worst_case_same_sign_errors_stay_within_and_near_the_bound():
    rng = np.random.default_rng(0)
    x = rng.uniform(-500.0, 500.0, 192)                    # kW each price multiplies (today's 1p: N - Q)
    logged, true = _logged_and_true(rng, 192, x)           # every error the sign of its kW: nothing cancels
    err = abs(float(true @ x) * K - float(logged @ x) * K)
    e_kwh = TL.abs_kwh(x)
    tol = TL.money_tol_usd(e_kwh, float(np.abs(true * x).sum()) * K, 2 * len(x))
    assert err <= tol
    assert err >= 0.99 * TL.price_rounding_usd(e_kwh)      # tight: the rounding term is attained
    assert TL.price_rounding_usd(e_kwh) == pytest.approx(D * e_kwh / 1000.0)


def test_exact_halves_are_covered_with_the_float_term():
    """Python rounds a decimal half to the even float neighbour; the error is half a unit plus at most an ulp."""
    for k in (0, 1, 7, 123456, 999999999):
        for v in (k * 1e-6 + 5e-7, 1234.0 + k * 1e-6 + 5e-7):
            r = round(v, TL.PRICE_DECIMALS)
            assert abs(r - v) <= D + 4 * np.spacing(v)
            x = 300.0
            assert abs(r - v) * x * K <= TL.money_tol_usd(x * TL.STEP_H, v * x * K, 4)


def _random_case(rng, T, C, ties):
    """C binding calls over T steps, random coverage (the last call covers a suffix), prices with exact zeros and,
    if ties, earlier prices within a few rounding units of the settling price."""
    pis, noms, qs = [], [], []
    for c in range(C):
        start = int(rng.integers(0, T // 2)) if c else 0
        cov = np.zeros(T, bool)
        cov[start:] = True
        p = rng.uniform(0.0, 300.0, T)
        p[rng.random(T) < 0.2] = 0.0
        pis.append(np.where(cov, p, np.nan))
        q = rng.uniform(-200.0, 200.0, T)
        n = q + rng.uniform(-50.0, 150.0, T)
        noms.append(np.where(cov, n, np.nan))
        qs.append(np.where(cov, q, np.nan))
    if ties:
        last = pis[-1]
        for p in pis[:-1]:
            near = ~np.isnan(p) & (rng.random(T) < 0.5) & (last > 0)
            off = rng.choice([-2e-6, -1e-6, -4e-7, 0.0, 4e-7, 1e-6, 2e-6], near.sum())
            p[near] = np.maximum(0.0, last[near] + off)
    Nm = rng.uniform(-200.0, 200.0, T)
    return pis, noms, qs, Nm


@pytest.mark.parametrize("seed", range(40))
def test_max_rule_rounding_error_stays_within_the_bound(seed):
    """True prices (exact zeros kept zero, as the exchange's projection gives them) vs the same rounded to 6
    decimals: the max rule's recompute moves by no more than max_rule_tol_usd, per step and in total."""
    rng = np.random.default_rng(seed)
    T, C = 48, int(rng.integers(1, 5))
    pis, noms, qs, Nm = _random_case(rng, T, C, ties=seed % 2 == 0)
    if seed % 4 == 1:          # adversarial: every positive price just under half a unit from its logged value
        true = [np.where(p > 0, np.round(p, 6) + rng.choice([-1, 1], T) * D * (1 - 1e-5), p) for p in pis]
        true = [np.where(t < 0, 0.0, t) for t in true]
    else:
        true = pis
    logged = [np.where(np.isnan(t), np.nan, np.round(t, TL.PRICE_DECIMALS)) for t in true]
    s_true = max_first_call_steps(true, noms, qs, Nm)[0]
    s_log = max_first_call_steps(logged, noms, qs, Nm)[0]
    e, _ = TL.max_rule_price_kwh(logged, noms, qs, Nm)     # flagged steps rest on true zeros, as here
    per_step = np.abs(s_true - s_log) * K
    assert (per_step <= TL.PRICE_ROUNDING_USD_MWH * e / 1000.0 + 1e-12).all()
    tol, _ = TL.max_rule_tol_usd(logged, noms, qs, Nm)
    assert abs(float(s_true.sum() - s_log.sum())) * K <= tol


def test_the_cap_switch_near_its_threshold_is_covered():
    """Earlier call A within rounding of the settling call B: offline and in-run may disagree on the cap."""
    for p_b in (50.0, 50.000001):
        for d_a in (-1e-6, -5e-7, 0.0, 4e-7, 1e-6, 1.4e-6):
            true = [np.array([p_b + d_a]), np.array([p_b])]
            logged = [np.round(x, 6) for x in true]
            noms = [np.array([-20.0]), np.array([-100.0])]
            qs = [np.array([-100.0]), np.array([-60.0])]
            Nm = np.array([-150.0])
            diff = abs(float(max_first_call_steps(true, noms, qs, Nm)[0][0])
                       - float(max_first_call_steps(logged, noms, qs, Nm)[0][0])) * K
            tol, _ = TL.max_rule_tol_usd(logged, noms, qs, Nm)
            assert diff <= tol, (p_b, d_a)


def test_a_logged_zero_after_a_positive_price_on_a_capped_step_is_flagged():
    """A true price in (1e-9, delta) logs as 0 and moves the cap's reference to another call: outside the bound,
    so max_rule_price_kwh flags the step for the check to report."""
    nan = float("nan")
    true = [np.array([80.0]), np.array([3e-7])]
    logged = [np.array([80.0]), np.array([0.0])]
    noms, qs, Nm = [np.array([-20.0]), np.array([-100.0])], [np.array([-100.0]), np.array([-40.0])], np.array([-60.0])
    _, flag = TL.max_rule_price_kwh(logged, noms, qs, Nm)
    assert flag.tolist() == [True]
    diff = abs(float(max_first_call_steps(true, noms, qs, Nm)[0][0])
               - float(max_first_call_steps(logged, noms, qs, Nm)[0][0])) * K
    assert diff > TL.max_rule_tol_usd(logged, noms, qs, Nm)[0]
    _, flag = TL.max_rule_price_kwh([np.array([80.0]), np.array([nan])], noms, qs, Nm)
    assert not flag.any()


def test_a_one_cent_rule_error_fails_at_real_energies():
    """jan12's largest error ($4.3e-6) needs about 8.6 MWh of price-weighted energy; an agent's 1p settlement over two
    days at up to its 500 kW mandate is at most 96 MWh. The bound there is ~5e-5: a $0.01 error, or one step
    settled at the wrong price ($50/MWh x 100 kW x 0.25 h = $1.25), fails."""
    e_kwh = 500.0 * 192 * TL.STEP_H * 4                    # 4 x the 2-day mandate energy, generous
    tol = TL.money_tol_usd(e_kwh, gross_usd=2000.0 * e_kwh / 1000.0, n_terms=192 * 20)
    assert TL.price_rounding_usd(8600.0) == pytest.approx(4.3e-6)
    assert tol < 1e-3 and 0.01 > 10 * tol
    rng = np.random.default_rng(1)
    pis, noms, qs, Nm = _random_case(rng, 192, 3, ties=True)
    logged = [np.where(np.isnan(p), np.nan, np.round(p, 6)) for p in pis]
    in_run = float(max_first_call_steps(pis, noms, qs, Nm)[0].sum()) * K
    offline = float(max_first_call_steps(logged, noms, qs, Nm)[0].sum()) * K
    tol, _ = TL.max_rule_tol_usd(logged, noms, qs, Nm)
    assert abs(in_run - offline) <= tol < 0.01
    assert abs(in_run + 0.01 - offline) > tol
    wrong = float(max_first_call_steps(logged, noms, qs, Nm, cap=None)[0].sum()) * K   # the rule as first built
    assert abs(in_run - wrong) > tol


def test_logged_kw_are_exact_so_energy_has_float_noise_only():
    rng = np.random.default_rng(2)
    kw = rng.uniform(-1000.0, 1000.0, 1000) * np.pi
    back = np.array(json.loads(json.dumps(kw.tolist())))
    assert (back == kw).all()                              # json.dumps round-trips float64
    e = TL.abs_kwh(kw)
    s1 = float(kw.sum()) * TL.STEP_H
    s2 = sum(float(v) * TL.STEP_H for v in kw[::-1])
    tol = TL.energy_tol_kwh(e, len(kw) + 1)
    assert abs(s1 - s2) <= tol < 1e-6 * e
    assert TL.abs_kwh([1.0, np.nan, -2.0], [4.0]) == pytest.approx(7.0 * TL.STEP_H)


def test_gamma_is_higham_s_and_refuses_overflow():
    assert TL.gamma(0) == 0.0
    assert TL.gamma(10) == pytest.approx(10 * 2.0 ** -53, rel=1e-12)
    with pytest.raises(ValueError):
        TL.gamma(2 ** 53)
