"""
Price mode (D53, arm C): the per-step peak price rules, ported from prototype v3 (bench/price_mode_proto.py) to plain
Python so the exchange keeps no numeric dependencies. Prices are held in $/kW-step like the prototype ($/MWh = pi / K,
K = step hours / 1000); rho is $/kW^2-step, alpha $/kW-step per kW. Load is positive (zone load vs. its target).
"""
import math

PRICED = 1e-6            # $/kW-step: a price above this counts as positive


def price_update(pi, agg, target, alpha, cap=math.inf):
    """Projected damped dual ascent: rises where the aggregate is over target, falls where under, never < 0, and
    never above cap (the zone price cap)."""
    return [min(cap, max(0.0, p + alpha * (a - t))) for p, a, t in zip(pi, agg, target)]


def residual(agg, target, pi):
    """Projected primal residual per step: the excess where unpriced (only over-target counts), the full gap where
    priced (a priced step below target is also off the fixed point)."""
    return [a - t if p > PRICED else max(a - t, 0.0) for a, t, p in zip(agg, target, pi)]


def l2(v) -> float:
    return math.sqrt(sum(x * x for x in v))


def rb_update(rho, r, d, mu, tau, lo, hi):
    """Residual balancing: raise rho when the primal residual r is more than mu x the dual side d, lower it in the
    opposite case, within [lo, hi]."""
    if r > mu * d:
        rho *= tau
    elif d > mu * r:
        rho /= tau
    return float(min(max(rho, lo), hi))


def rb_dual_kw(rho, rho0, d):
    """The dual side residual balancing weighs the primal residual against, kW: the dual residual s = rho d over the
    fixed starting rho0 (v3's scaled rule), so a raise of rho also raises this side."""
    return rho / rho0 * d


def centre_shift(pi_new, pi_old, alpha, n):
    """Textbook exchange-ADMM shift of each agent's prox centre, kW per step (load-positive): the mean projected
    residual behind the price step, (pi_new - pi_old) / (alpha N)."""
    return [(a - b) / (alpha * n) for a, b in zip(pi_new, pi_old)]


def rho0_public(spread_usd_mwh, step_h, median_power_kw):
    """Starting rho from public data: mean daily day-ahead spread x K / median registered power."""
    return spread_usd_mwh * step_h / 1000.0 / median_power_kw


def converged(history, tol_excess_kw, tol_dual_usd_mwh, streak) -> bool:
    """history: (max excess kW, dual residual $/MWh) per measured round. True once the last `streak` rounds are all
    within both tolerances (v3's target+dual rule)."""
    if len(history) < streak:
        return False
    return all(e <= tol_excess_kw and s <= tol_dual_usd_mwh for e, s in history[-streak:])
