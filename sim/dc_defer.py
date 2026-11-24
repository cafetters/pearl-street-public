"""M2b: data-center workload deferral rules (docs/plans/m2b_dc_plan.md; owner, Nov 18 evening).

Per data-center agent, generated flexible workload g (kW per step, `dc_flexible_kw`) may be served later as s (kW per
step), under the daily sessions of `dc_sessions_2023` (local midnight to a deadline of 04:30 to 10:30 the next day, per data center; `charge_power_kw_max` is
the session's power ceiling):
  - never ahead of generation: cumulative served <= cumulative generated, every step;
  - each session's work served by its deadline: cumulative served at (deadline - 1) >= cumulative generated up to the
    session's last generating step (the end of its local day, or the window's end);
  - at most the ceiling of the session in force at each step;
  - the window's total unchanged: cumulative served at the end = cumulative generated at the end (deadlines past the
    window's end are clipped to it, and reported).
The agent's load becomes load - g + s. Nothing here reads files: sim/ passes the arrays and sessions in.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

GUARD_KWH = 1e-6
GUARD_KW = 1e-6


@dataclass(frozen=True)
class Session:
    start: int                  # first step of the session's day (inclusive), within the window
    deadline: int               # step by which its work is served (exclusive)
    energy_kwh: float           # required energy (the session table's; the check uses generation itself)
    cap_kw: float               # power ceiling while it is in force
    day_end: int | None = None  # last generating step + 1 (default: start + one day, clipped to the window)
    clipped: bool = False       # its deadline was past the window's end


def clip(sessions, T: int, steps_per_day: int = 96) -> list:
    out = []
    for s in sessions:
        de = min(s.day_end if s.day_end is not None else s.start + steps_per_day, T)
        out.append(replace(s, deadline=min(s.deadline, T), day_end=de, clipped=s.deadline > T))
    return out


def ceiling(sessions, T: int, room=None) -> np.ndarray:
    """kW cap per step: the largest ceiling among sessions in force (start <= t < deadline); 0 where none is.
    room (T,), optional (Nov 19): the site's capacity minus the load not being postponed; the cap is the lower of
    the two, so served work never puts the data center above its capacity."""
    cap = np.zeros(T)
    for s in clip(sessions, T):
        cap[s.start:s.deadline] = np.maximum(cap[s.start:s.deadline], s.cap_kw)
    if room is not None:
        cap = np.minimum(cap, np.maximum(np.asarray(room, float)[:T], 0.0))
    return cap


def check(served, generated, sessions, dt: float, room=None) -> list:
    """Violations of the deferral rules (empty when valid), unrounded with the 1e-6 guards."""
    s, g = np.asarray(served, float), np.asarray(generated, float)
    T = len(g)
    ss = clip(sessions, T)
    S, G = np.cumsum(s) * dt, np.cumsum(g) * dt
    out = []
    ahead = np.flatnonzero(S - G > GUARD_KWH)
    if len(ahead):
        out.append({"rule": "ahead_of_generation", "steps": ahead.tolist(), "max_kwh": float((S - G).max())})
    cap = ceiling(ss, T, room)
    over = np.flatnonzero(s - cap > GUARD_KW)
    if len(over):
        out.append({"rule": "over_ceiling", "steps": over.tolist(), "max_kw": float((s - cap)[over].max())})
    for i, x in enumerate(ss):
        need = G[x.day_end - 1] if x.day_end > 0 else 0.0
        got = S[x.deadline - 1] if x.deadline > 0 else 0.0
        if need - got > GUARD_KWH:
            out.append({"rule": "missed_deadline", "session": i, "short_kwh": float(need - got)})
    if abs(S[-1] - G[-1]) > GUARD_KWH:
        out.append({"rule": "energy_changed", "served_kwh": float(S[-1]), "generated_kwh": float(G[-1])})
    return out


def lp_rows(generated, sessions, dt: float, room=None):
    """A s <= b plus bounds lo <= s <= hi for one agent's served schedule s (T,), encoding check()'s rules:
    cumulative served <= cumulative generated (T rows), each session's deadline (one row each), and the window total
    (two rows: <= and >=)."""
    g = np.asarray(generated, float)
    T = len(g)
    ss = clip(sessions, T)
    G = np.cumsum(g) * dt
    L = np.tril(np.ones((T, T))) * dt                       # cumulative sum operator, kWh
    rows, rhs = [L], [G]
    for x in ss:
        if x.deadline > 0:
            rows.append(-L[x.deadline - 1][None, :])
            rhs.append(np.array([-(G[x.day_end - 1] if x.day_end > 0 else 0.0)]))
    rows += [-L[-1][None, :]]
    rhs += [np.array([-G[-1]])]
    return np.vstack(rows), np.concatenate(rhs), np.zeros(T), ceiling(ss, T, room)


def rows_from(generated, sessions, dt: float, k0: int, fixed, hold=None, room=None):
    """lp_rows over steps k0..T-1 only (the joint planner, Nov 19): executed steps 0..k0-1 (fixed, served kW) move to
    the right-hand side, rows with no remaining step are dropped, and steps held (hold kW, NaN = free; e.g. bound
    steps) get lo = hi. Returns (A sparse (m, T - k0), b, lo, hi)."""
    import scipy.sparse as sp
    A, b, lo, hi = lp_rows(generated, sessions, dt, room)
    f = np.asarray(fixed, float)[:k0]
    b = b - A[:, :k0] @ f
    A = A[:, k0:]
    keep = np.abs(A).sum(1) > 0
    lo, hi = lo[k0:].copy(), hi[k0:].copy()
    if hold is not None:
        h = np.asarray(hold, float)[k0:]
        m = np.isfinite(h)
        lo[m] = hi[m] = h[m]
    return sp.csr_matrix(A[keep]), b[keep], lo, np.maximum(hi, lo)


TIE_BREAK = 1e-6        # $/MWh per step later: among equal prices, serve as early as allowed (the recorded timing)


def schedule(price_usd_mwh, generated, sessions, dt: float, fixed=None, hold=None, room=None) -> np.ndarray:
    """The agent's own deferral: served kW per step minimizing price x energy under lp_rows' rules, with executed
    steps fixed (fixed = served kW for steps 0..len(fixed)-1). Ties go to the earliest step allowed, so flat prices
    reproduce the recorded timing. hold: kW per step to keep (NaN = free), e.g. steps a binding nomination locked.
    Raises if no schedule meets the rules (fail closed)."""
    from scipy.optimize import linprog
    g = np.asarray(generated, float)
    T = len(g)
    A, b, lo, hi = lp_rows(g, sessions, dt, room)
    lo, hi = lo.copy(), hi.copy()
    if fixed is not None:
        f = np.asarray(fixed, float)
        lo[:len(f)] = hi[:len(f)] = f
    if hold is not None:                                    # steps held at given kW (NaN = free), e.g. bound steps
        h = np.asarray(hold, float)
        m = np.isfinite(h)
        m[:len(fixed) if fixed is not None else 0] = False
        lo[m] = hi[m] = h[m]
    hi = np.maximum(hi, lo)
    c = (np.asarray(price_usd_mwh, float) + TIE_BREAK * np.arange(T)) * dt
    r = linprog(c, A_ub=A, b_ub=b, bounds=np.c_[lo, hi], method="highs")
    if r.status != 0:
        raise RuntimeError(f"no deferral schedule meets the rules: {r.message}")
    return r.x
