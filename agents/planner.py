"""
Single-battery LP an agent plans with, from the current step to the end of the horizon.

Variables per step: c (charge kW), d (discharge kW), s (energy kWh), x (extra curtailment kW), sh (award shortfall kW).
Objective (minimize): forecast price x (c - d + x) x K + wear x K x d + penalties.
Same battery model as the simulator (sim/physics.py) and the bench LP.
"""
from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp
from scipy.optimize import linprog

TERMINAL_USD_KWH = 10.0       # keeps the end-of-horizon energy target soft
# Keeps award constraints soft so the plan always solves. It must beat the terminal penalty even after losses
# (delivering 1 kWh takes 1/eta kWh of storage), so agents deliver awards before protecting terminal energy.
SHORTFALL_USD_MWH = 3 * TERMINAL_USD_KWH * 1000.0
assert SHORTFALL_USD_MWH / 1000.0 > TERMINAL_USD_KWH / 0.5    # holds for any one-way efficiency above 50 %
# M2a: load above the D34 rebound cap. Both orders put the cap above terminal energy; they differ on cap vs. award
# delivery (config rebound_priority). "grid" (default): the agent misses an award rather than break its cap.
# "delivery": it breaks its cap (and pays the rebound charge) rather than miss an award. Settlement is the same.
CAP_PRIORITY_USD_MWH = {"grid": 2 * SHORTFALL_USD_MWH, "delivery": SHORTFALL_USD_MWH / 1.2}
assert all(v / 1000.0 > TERMINAL_USD_KWH / 0.5 for v in CAP_PRIORITY_USD_MWH.values())
assert CAP_PRIORITY_USD_MWH["grid"] > SHORTFALL_USD_MWH > CAP_PRIORITY_USD_MWH["delivery"]
CAP_USD_MWH = CAP_PRIORITY_USD_MWH["grid"]
PREP_USD_MWH = 1.0            # small: prefer plans that add as little load as possible outside the window
Q_WEIGHT = 1e6                # max_commit: maximize q first, then revenue


def prox_breakpoints(R: float, rho: float, eps: float = 0.0) -> np.ndarray:
    """Secant breakpoints for (rho/2) delta^2 on |delta| <= R (prototype v3, AgentProx.set_rho): 0, b1, then x sqrt 2
    up to R, so the secants overstate the square by at most ~4.3% past b1. b1 = min(R/256, 2 eps / rho) keeps the
    kink at delta = 0 (slope rho b1 / 2) at or below eps."""
    b1 = min(R / 256, 2 * eps / rho) if eps > 0 else R / 256
    bp = [0.0, b1]
    while bp[-1] * np.sqrt(2.0) < R:
        bp.append(bp[-1] * np.sqrt(2.0))
    bp.append(R)
    return np.array(bp)


@dataclass(frozen=True)
class Battery:
    P: float
    CAP: float
    ETA: float
    SD: float
    POI: float
    s_term: float           # energy to hold at the end of the horizon (the slice's starting SOC, as in M0)

    @property
    def s_min(self):
        return 0.1 * self.CAP

    @property
    def s_max(self):
        return 0.9 * self.CAP


@dataclass
class Plan:
    k0: int
    c: np.ndarray
    d: np.ndarray
    s: np.ndarray
    x: np.ndarray
    sh: np.ndarray
    cost: float             # objective without penalties: -(forecast revenue from the battery) + wear
    q: float = 0.0


def solve(b: Battery, k0: int, s_now: float, price, avail, base_export, commit, dt_h: float, wear: float,
          q_mask=None, nom=None, band: float = 0.0, imb_price: float = 0.0, ref_net=None, load_cap=None,
          cap_band: float = 0.0, cap_price: float = CAP_USD_MWH, peak=None, prox=None, anchor=None,
          p_max=None, s_floor=None) -> Plan | None:
    """Plan steps k0..end. price, avail (pv - curt), base_export (pv - curt - load) and commit (awarded kW, d - c
    must reach it) are arrays over the horizon. With q_mask, also maximize a uniform extra commitment q over the
    masked steps, with no shortfall or terminal slack allowed. Returns None if that is infeasible.
    nom: locked nomination of site net kW per step (NaN where none). Deviation beyond `band` costs imb_price $/MWh.
    ref_net (M2a): a reference site net per step (NaN where none); load added above it costs PREP_USD_MWH, so
    the plan adds load outside the window only where it must. load_cap (M2a, D34): the most net load allowed per
    step (NaN where uncapped); load above cap + cap_band costs cap_price (see CAP_PRIORITY_USD_MWH).
    peak (D53): a peak price per step, $/MWh, added to the price on the battery's net load (c - d + x).
    prox (D53): (rho $/kW^2-step, centre kW per step with NaN where none, eps $/kW-step): adds (rho/2)(c - d + x -
    centre)^2 as piecewise-linear secants (prox_breakpoints). Neither is in Plan.cost, so offer floors stay energy.
    anchor (D58 margin): (site net kW per step with NaN where none, m $/MWh): adds (m/2) |site net - anchor| per MWh,
    so a shift, which moves both legs, pays about m per MWh moved. Not in Plan.cost. None or m = 0 adds nothing.
    Extra variables go last, so without them the LP is exactly the M1 one."""
    L = len(price)
    K = dt_h / 1000.0
    hard = q_mask is not None
    nq = 1 if hard else 0
    n = 5 * L + 1 + nq            # blocks c, d, s, x, sh, then terminal slack z, then q
    I = sp.identity(L, format="csr")
    Z = sp.csr_matrix((L, L))
    zc = sp.csr_matrix((L, 1 + nq))
    keep = 1 - b.SD * dt_h
    D = sp.diags([np.ones(L), -keep * np.ones(L - 1)], [0, -1], format="csr")
    Aeq = sp.hstack([-b.ETA * dt_h * I, dt_h / b.ETA * I, D, Z, Z, zc], format="csr")
    beq = np.zeros(L)
    beq[0] = keep * s_now

    rows = [sp.hstack([I, I, Z, Z, Z, zc]),                       # c + d <= P
            sp.hstack([-I, I, Z, -I, Z, zc])]                     # export <= POI
    # D80 (declared_reference, 1p's reference only): per-step power cap (kW) and SOC floor (kWh), NaN where none
    pcap = np.full(L, b.P) if p_max is None else np.fmin(np.full(L, b.P), np.asarray(p_max, float)[:L])
    rhs = [pcap, b.POI - base_export]
    need = commit > 0 if not hard else (commit > 0) | q_mask
    if need.any():
        sel = sp.identity(L, format="csr")[np.flatnonzero(need)]
        qcol = sp.csr_matrix(q_mask[need].astype(float).reshape(-1, 1)) if hard else None
        blk = [sel, -sel, sp.csr_matrix(sel.shape), sp.csr_matrix(sel.shape), -sel, sp.csr_matrix((sel.shape[0], 1))]
        if hard:
            blk.append(qcol)
        rows.append(sp.hstack(blk))                               # c - d - sh + q <= -commit
        rhs.append(-commit[need])
    term = sp.csr_matrix(([-1.0, -1.0], ([0, 0], [3 * L - 1, 5 * L])), shape=(1, n))
    rows.append(term)                                             # s_end + z >= s_term
    rhs.append(np.array([-b.s_term]))
    Aub = sp.vstack(rows, format="csr")
    bub = np.concatenate(rhs)
    locked = np.flatnonzero(~np.isnan(nom)) if nom is not None else np.array([], int)
    m = len(locked)
    if m:   # ep, em >= |site net - nom| beyond the band; site net = base_export + d - c - x
        sel = sp.identity(L, format="csr")[locked]
        z0 = sp.csr_matrix((m, L))
        net_ctrl = sp.hstack([-sel, sel, z0, -sel, z0, sp.csr_matrix((m, 1 + nq))])
        Im = sp.identity(m, format="csr")
        Zm = sp.csr_matrix((m, m))
        Aeq = sp.hstack([Aeq, sp.csr_matrix((Aeq.shape[0], 2 * m))], format="csr")
        Aub = sp.vstack([sp.hstack([Aub, sp.csr_matrix((Aub.shape[0], 2 * m))]),
                         sp.hstack([net_ctrl, -Im, Zm]), sp.hstack([-net_ctrl, Zm, -Im])], format="csr")
        target = nom[locked] - base_export[locked]
        bub = np.concatenate([bub, band + target, band - target])
    extra = []      # (steps, rhs, price): -(d - c - x) - slack <= rhs, slack >= 0 at `price`
    if ref_net is not None:
        idx = np.flatnonzero(~np.isnan(ref_net))
        extra.append((idx, base_export[idx] - ref_net[idx], PREP_USD_MWH))     # load added above the reference
    if load_cap is not None:
        idx = np.flatnonzero(~np.isnan(load_cap))
        extra.append((idx, load_cap[idx] + cap_band + base_export[idx], cap_price))
    extra = [e for e in extra if len(e[0])]
    n_extra = sum(len(e[0]) for e in extra)
    if n_extra:
        Aeq = sp.hstack([Aeq, sp.csr_matrix((Aeq.shape[0], n_extra))], format="csr")
        Aub = sp.hstack([Aub, sp.csr_matrix((Aub.shape[0], n_extra))], format="csr")
        n_before = Aub.shape[1] - n_extra
        blocks, rhs_x, at = [], [], 0
        for idx, rhs_e, _ in extra:
            k_ = len(idx)
            sel = sp.identity(L, format="csr")[idx]
            z0 = sp.csr_matrix((k_, L))
            neg_net = sp.hstack([sel, -sel, z0, sel, z0, sp.csr_matrix((k_, n_before - 5 * L))])   # -(d - c - x)
            slack = sp.hstack([sp.csr_matrix((k_, at)), -sp.identity(k_, format="csr"),
                               sp.csr_matrix((k_, n_extra - at - k_))])
            blocks.append(sp.hstack([neg_net, slack]))
            rhs_x.append(rhs_e)
            at += k_
        Aub = sp.vstack([Aub] + blocks, format="csr")
        bub = np.concatenate([bub] + rhs_x)

    price = np.asarray(price, float)
    pe = price if peak is None else price + np.asarray(peak, float)
    cost = np.concatenate([pe * K, (wear - pe) * K, np.zeros(L), pe * K,
                           np.full(L, SHORTFALL_USD_MWH * K), [TERMINAL_USD_KWH]])
    if s_floor is None:
        smin = np.full(L, b.s_min)
    else:                                     # NaN (no declared reserve): the usual floor
        fl = np.asarray(s_floor, float)[:L]
        smin = np.where(np.isnan(fl), b.s_min, np.clip(fl, b.s_min, b.s_max))
    lb = np.concatenate([np.zeros(L), np.zeros(L), smin, np.zeros(L), np.zeros(L), [0.0]])
    ub = np.concatenate([pcap, pcap, np.full(L, b.s_max), np.maximum(avail, 0.0),
                         np.zeros(L) if hard else np.full(L, np.inf), [0.0 if hard else np.inf]])
    if hard:
        cost = np.append(cost, -Q_WEIGHT)
        lb, ub = np.append(lb, 0.0), np.append(ub, b.P)
    if m:
        cost = np.concatenate([cost, np.full(2 * m, imb_price * K)])
        lb, ub = np.concatenate([lb, np.zeros(2 * m)]), np.concatenate([ub, np.full(2 * m, np.inf)])
    for idx, _, price_e in extra:
        cost = np.concatenate([cost, np.full(len(idx), price_e * K)])
        lb, ub = np.concatenate([lb, np.zeros(len(idx))]), np.concatenate([ub, np.full(len(idx), np.inf)])
    if prox is not None:      # D53: secant segments p_j, n_j per proxed step, delta = sum p - sum n
        rho, centre, eps = prox
        centre = np.asarray(centre, float)
        ip = np.flatnonzero(~np.isnan(centre))
        if rho > 0 and len(ip):
            hi = b.P + float(np.max(np.maximum(avail, 0.0), initial=0.0))      # c - d + x lies in [-P, P + avail]
            R = max(float((hi - centre[ip]).max()), float((centre[ip] + b.P).max()), 0.0) + 1.0
            bp = prox_breakpoints(R, rho, eps)
            w, slope = np.diff(bp), rho / 2 * (bp[:-1] + bp[1:])
            m_s, n_p = len(w), len(ip)
            n_seg = 2 * m_s * n_p
            n_now = Aub.shape[1]
            Aub = sp.hstack([Aub, sp.csr_matrix((Aub.shape[0], n_seg))], format="csr")
            sel = sp.identity(L, format="csr")[ip]
            z0 = sp.csr_matrix((n_p, L))
            net_load = sp.hstack([sel, -sel, z0, sel, sp.csr_matrix((n_p, n_now - 4 * L))])    # c - d + x
            seg = sp.hstack([-sp.identity(n_p, format="csr")] * m_s + [sp.identity(n_p, format="csr")] * m_s)
            Aeq = sp.vstack([sp.hstack([Aeq, sp.csr_matrix((Aeq.shape[0], n_seg))]), sp.hstack([net_load, seg])],
                            format="csr")
            beq = np.concatenate([beq, centre[ip]])
            cost = np.concatenate([cost, np.repeat(slope, n_p), np.repeat(slope, n_p)])
            lb = np.concatenate([lb, np.zeros(n_seg)])
            ub = np.concatenate([ub, np.repeat(w, n_p), np.repeat(w, n_p)])
    if anchor is not None and anchor[1] > 0:      # D58: u - v = (d - c - x) - (anchor - base_export), u, v >= 0
        a_net = np.asarray(anchor[0], float)
        ia = np.flatnonzero(~np.isnan(a_net))
        if len(ia):
            n_a, n_now = len(ia), Aub.shape[1]
            Aub = sp.hstack([Aub, sp.csr_matrix((Aub.shape[0], 2 * n_a))], format="csr")
            sel = sp.identity(L, format="csr")[ia]
            z0 = sp.csr_matrix((n_a, L))
            net_ctrl = sp.hstack([-sel, sel, z0, -sel, sp.csr_matrix((n_a, n_now - 4 * L))])      # d - c - x
            Ia = sp.identity(n_a, format="csr")
            Aeq = sp.vstack([sp.hstack([Aeq, sp.csr_matrix((Aeq.shape[0], 2 * n_a))]),
                             sp.hstack([net_ctrl, -Ia, Ia])], format="csr")
            beq = np.concatenate([beq, a_net[ia] - base_export[ia]])
            cost = np.concatenate([cost, np.full(2 * n_a, anchor[1] / 2 * K)])
            lb, ub = np.concatenate([lb, np.zeros(2 * n_a)]), np.concatenate([ub, np.full(2 * n_a, np.inf)])
    r = linprog(cost, A_ub=Aub, b_ub=bub, A_eq=Aeq, b_eq=beq, bounds=np.c_[lb, ub], method="highs")
    if r.status != 0:
        if hard:
            return None
        raise RuntimeError(f"battery plan failed: {r.message}")
    v = r.x
    c, d, s, x, sh = (v[i * L:(i + 1) * L] for i in range(5))
    base_cost = float(price @ (c - d + x) * K + wear * K * d.sum())
    if m:     # imbalance is real money, so floors include it
        base_cost += float(imb_price * K * v[5 * L + 1 + nq:5 * L + 1 + nq + 2 * m].sum())
    return Plan(k0, c, d, s, x, sh, base_cost, float(v[5 * L + 1]) if hard else 0.0)
