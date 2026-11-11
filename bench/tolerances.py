"""
Tolerances for checks that recompute money or energy from a saved trace, derived from the trace's logging precision.

What the trace loses (exchange/core.py, exchange/trace/logger.py; logging precision is not changed here):
  - peak_price's `price_usd_mwh` is logged as round(pi / K, 6): $/MWh to PRICE_DECIMALS = 6 decimals, with
    K = step_h / 1000 (kW x $/MWh x K = $ in one step). The exchange settles at the unrounded pi / K, so each logged
    price is off by at most PRICE_ROUNDING_USD_MWH = 0.5e-6 $/MWh (round half to even never exceeds half a unit).
    `shift_kw` is rounded the same way (6 decimals, kW), but no settlement uses it.
  - Everything else a recompute reads (telemetry and meter kW, nominations, selfish_reference, peak_settle $) is
    written with json.dumps, whose float repr round-trips float64 exactly: no logging loss. Values rounded before
    they are sent (an agent's schedule to 3 decimals, the operator's target_kw, room) are the values the exchange
    used, so they are exact too.

Money bound (price rounding x energy). A recompute that is linear in the logged prices, money = sum_j p_j x_j K with
x_j the kW each price multiplies, differs from the in-run value by
    |sum_j (p~_j - p_j) x_j K| <= delta x sum_j |x_j| K = delta x E_MWh,   delta = PRICE_ROUNDING_USD_MWH,
where E_MWh = sum_j |x_j| K is the price-weighted absolute energy: every (price, kW) product counts, by absolute value
(the rounding errors of different steps and calls have independent signs, so nothing may cancel). In kWh,
E_kWh = 1000 x E_MWh, so the bound is 5e-10 $ per kWh. Today's 1p rule: x = metered - reference at each priced step,
E = sum |N - Q| x step_h. The max rule (bench.item7_resettle.max_first_call_steps): see max_rule_price_kwh.

Float term. The two sides are also computed in float64, in different orders. Each sum of n products carries at most
gamma(n) x gross, gamma(n) = n u / (1 - n u), u = 2^-53 (Higham, Accuracy and Stability of Numerical Algorithms,
eq. 3.4), and gross = sum of the absolute values of the terms actually summed (the in-run peak_settle adds charge and
refund separately, so gross is not bounded by the net). Two independent sums: 2 gamma(n) x gross.

Tolerance = delta x E_MWh + 2 gamma(n) x gross. A new tolerance must hold on a real saved trace before a
pre-registration uses it: max_rule_tol_usd was checked on the jan12 smoke run's trace (h2h_c_jan12_h100_m0_max at
9bcc0d1, 80 agents, 2 binding calls; Nov 4): largest error $4.34e-6 (PVB_12, bound $1.54e-4), largest error / bound
0.040 (PVB_09), bounds $4.2e-7 to $2.0e-4 per agent (the holdout's fixed TOL_C_USD is $1e-3), 80 flagged agent-steps
(settling price logged 0 after a positive one; all within bound, as true zeros give).

Energy. Logged kW are exact, so an energy recompute has no logging term: only the float term 2 gamma(n) x sum |kWh|.

Where the bound does not apply: a rule that compares a price with a threshold (the max rule's `> p_k + 1e-9` and
`> 1e-9`) can take another branch offline when a logged price sits within rounding of that threshold.
max_rule_price_kwh covers the `p_1 > p_k + 1e-9` switch (it is continuous up to the 1e-9; see there) and flags the
steps where a logged price of exactly 0 could hide a true price in (1e-9, delta), which would change which call's
reference caps the relief. A check using it must report those steps.
"""
import numpy as np

PRICE_DECIMALS = 6                                         # exchange/core.py: round(new / K, 6), $/MWh
PRICE_ROUNDING_USD_MWH = 0.5 * 10.0 ** -PRICE_DECIMALS     # max |logged - settled| per logged price
STEP_H = 0.25                                              # step, hours (every run so far)
U = 2.0 ** -53                                             # float64 unit roundoff
HIT_EPS_USD_MWH = 1e-9                                     # the max rule's comparison slack (exchange/core.py)

# D61 (docs/plans/d61_attacks_check.md; owner, Nov 4). The check compares in-run values, not a trace recompute:
# `agent_value_usd` is computed in float64 by sim/run_m1.py and written to summary.json at full precision (JSON
# floats round-trip), so no logging term applies. Attack and twin compute each value with the same code in the same
# order; unchanged inputs give bit-identical values. GAIN_GUARD_USD is the pre-registered float guard: it exceeds the
# float term 2 gamma(n) x gross for n = 1,920 products (2 days x 96 steps x 10 terms) at any gross below $2.3M per agent
# (the smoke pair's largest |agent value| is $68,914).
GAIN_GUARD_USD = 1e-6
# Harm against the honest twin: a flat $10 per agent, section 8's absolute floor without its 1% of |value| part (the
# twin removes forecast noise). A policy tolerance, not a precision bound; every smaller loss is reported.
HARM_TOL_USD = 10.0


def gamma(n: int) -> float:
    """Higham's gamma_n = n u / (1 - n u): the relative error bound of a float64 sum of n products."""
    n = max(int(n), 0)
    if n * U >= 1:
        raise ValueError(f"gamma({n}) undefined")
    return n * U / (1.0 - n * U)


def abs_kwh(*kw, dt_h: float = STEP_H) -> float:
    """sum |kW| x dt_h over every given array (NaN counts as 0): the price-weighted absolute energy of the kW series
    the logged prices multiply, one array per (price, kW) product."""
    return float(sum(np.nansum(np.abs(np.asarray(x, float))) for x in kw)) * dt_h


def price_rounding_usd(energy_kwh: float) -> float:
    """The most a recompute from logged prices can differ from settlement at unrounded prices: delta x E_MWh."""
    return PRICE_ROUNDING_USD_MWH * abs(float(energy_kwh)) / 1000.0


def float_usd(gross_usd: float, n_terms: int) -> float:
    """Float64 noise between two independent sums of n_terms products with absolute total gross_usd."""
    return 2.0 * gamma(n_terms) * abs(float(gross_usd))


def money_tol_usd(energy_kwh: float, gross_usd: float = 0.0, n_terms: int = 0) -> float:
    """Tolerance for one agent's money recomputed from a trace: price rounding x price-weighted absolute energy
    (kWh, abs_kwh or max_rule_price_kwh), plus float noise on gross_usd over n_terms products."""
    return price_rounding_usd(energy_kwh) + float_usd(gross_usd, n_terms)


def energy_tol_kwh(energy_kwh: float, n_terms: int) -> float:
    """Tolerance for energy recomputed from a trace's kW: logged kW are exact, so float noise only."""
    return 2.0 * gamma(n_terms) * abs(float(energy_kwh))


def max_rule_price_kwh(pis, noms, qs, Nm, dt_h: float = STEP_H):
    """Per step, the price-weighted absolute energy (kWh) of the max rule (bench.item7_resettle.max_first_call_steps,
    cap="below", the rule exchange/core.py _peak_settle_max settles), so that sum over steps of
    money_tol_usd(...) bounds one agent's recompute error from rounded prices. Same inputs as max_first_call_steps:
    one array per binding call in closing order (price $/MWh, NaN where the call doesn't cover the step; binding
    nomination; reference), and metered net Nm.

    Derivation, at a step covered by calls 1..k (k the settling call), each price moved by at most delta:
      L_i = max(p_i, ..., p_k) moves by at most delta (max is 1-Lipschitz), a spread L_i - L_{i+1} by at most 2 delta.
      No cap (p_1 <= p_k + 1e-9): step = sum_i L_i inc_i + p_k (Nm - n_k)  ->  A = sum_i |inc_i| + |Nm - n_k|.
      Cap (p_1 > p_k + 1e-9): step = p_k (Nm - q_k) + sum_i (L_i - L_{i+1}) r_i, r_i = max(0, min(rel_i, Nm - Q_j))
        >= 0  ->  B = |Nm - q_k| + 2 sum_i r_i.
      Switch: offline and in-run can disagree on the branch only if max_{i<k} p_i - p_k is within 2 delta of 1e-9
        (logged). Both branches equal p_k (Nm - q_k) at p_1 = p_k and move from it by at most (p_1 - p_k) sum_i |inc_i|
        and (p_1 - p_k) max_i r_i, with p_1 - p_k <= 1e-9 + 2 delta on either side. So the error is at most
        delta |Nm - q_k| + (2 delta + 1e-9) sum r_i + 1e-9 sum |inc_i| <= 2 delta (A + B) (as 1e-9 < delta).
      So E = A (no cap), B (cap), 2 (A + B) where the branch is ambiguous; times dt_h for kWh.
    Q_j is the reference of the last call priced above 1e-9. A logged 0 is taken as a true 0; if it hid a true price
    in (1e-9, delta), Q_j could be another call's. Returns (kWh per step, mask of capped or ambiguous steps where a
    covering call's logged price is 0 after an earlier positive one), so a check can report how many steps rest on
    that."""
    Nm = np.asarray(Nm, float)
    T = len(Nm)
    d = PRICE_ROUNDING_USD_MWH
    prev_rel = np.full(T, np.nan)
    inc_abs = np.zeros(T)
    last_p, last_q, last_n, pos_q = np.zeros(T), np.zeros(T), np.full(T, np.nan), np.full(T, np.nan)
    seen_pos = np.zeros(T, bool)
    zero_after_pos = np.zeros(T, bool)
    parts = []
    for pc, n, q in zip(pis, noms, qs):
        pc, n, q = (np.asarray(x, float) for x in (pc, n, q))
        cov = ~np.isnan(pc)
        rel = n - q
        inc = np.where(np.isnan(prev_rel), rel, rel - prev_rel)
        inc_abs += np.where(cov, np.abs(np.nan_to_num(inc)), 0.0)
        p0 = np.nan_to_num(pc)
        zero_after_pos |= cov & seen_pos & (p0 <= 0.0)
        seen_pos |= cov & (p0 > HIT_EPS_USD_MWH)
        pos_q = np.where(cov & (p0 > HIT_EPS_USD_MWH), q, pos_q)
        prev_rel = np.where(cov, rel, prev_rel)
        last_p = np.where(cov, p0, last_p)
        last_q = np.where(cov, q, last_q)
        last_n = np.where(cov, n, last_n)
        parts.append((cov, p0, rel))
    done = ~np.isnan(last_n)
    met = Nm - np.nan_to_num(pos_q)
    r_sum = np.zeros(T)
    max_earlier = np.full(T, -np.inf)
    later = np.zeros(T, bool)
    for cov, p0, rel in reversed(parts):
        r_sum += np.where(cov, np.maximum(0.0, np.minimum(np.nan_to_num(rel), met)), 0.0)
        max_earlier = np.where(cov & later, np.maximum(max_earlier, p0), max_earlier)
        later |= cov
    A = inc_abs + np.abs(np.nan_to_num(Nm - last_n))
    B = np.abs(np.nan_to_num(Nm - last_q)) + 2.0 * r_sum
    gap = max_earlier - last_p - HIT_EPS_USD_MWH                 # in-run caps where this is > 0 (true prices)
    hit = gap > 0
    ambiguous = np.abs(gap) <= 2 * d + HIT_EPS_USD_MWH           # + 1e-9: float slack on 6-decimal values
    E = np.where(ambiguous, 2.0 * (A + B), np.where(hit, B, A))
    E = np.where(done, E, 0.0)
    return E * dt_h, done & (hit | ambiguous) & zero_after_pos


def max_rule_tol_usd(pis, noms, qs, Nm, dt_h: float = STEP_H) -> tuple[float, int]:
    """One agent's tolerance for in-run max-rule peak_settle net vs max_first_call_steps on the logged prices:
    money_tol_usd over max_rule_price_kwh, with the float term on an upper bound of the gross both sides sum.
    Per step every product either side forms (later x inc, p_k x (m - n_k), the cap's spreads x relief, the charge)
    is at most P1 x 4 (|m| + sum_c (|n_c| + |q_c|)) in $/MWh x kW (|inc_i| <= |rel_i| + |rel_{i-1}|, each relief
    <= |n| + |q|), P1 the largest covering price (+ delta), from at most 3 + 4 x calls products per step.
    Returns (tolerance $, steps flagged by max_rule_price_kwh: a logged 0 after a positive price on a capped step)."""
    e, flag = max_rule_price_kwh(pis, noms, qs, Nm, dt_h)
    Nm = np.asarray(Nm, float)
    p1 = np.zeros(len(Nm))
    mass = np.abs(np.nan_to_num(Nm))
    for pc, n, q in zip(pis, noms, qs):
        pc = np.asarray(pc, float)
        cov = ~np.isnan(pc)
        p1 = np.where(cov, np.maximum(p1, np.abs(np.nan_to_num(pc))), p1)
        mass = mass + np.where(cov, np.abs(np.nan_to_num(np.asarray(n, float)))
                               + np.abs(np.nan_to_num(np.asarray(q, float))), 0.0)
    gross = float(((p1 + PRICE_ROUNDING_USD_MWH) * 4.0 * mass).sum()) * dt_h / 1000.0
    n_terms = len(Nm) * (3 + 4 * len(pis))
    return money_tol_usd(float(e.sum()), gross, n_terms), int(flag.sum())
