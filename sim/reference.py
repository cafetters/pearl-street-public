"""D56 rule 1p: the selfish reference schedule the metering service sends the exchange for each binding call.

One agent's selfish site net over the horizon from registration and public data only: power = registered max_kw,
energy window 10-90% of usable / 0.8 (the planner's convention), start SOC (also the soft end target), the day-ahead
prices published by the gate's time, and site net without the battery from recorded data (recorded curtailment, since
curtailment is agent-controlled). The planner is the agents' own LP with no awards, nominations, peak price or prox.
Nothing an agent sends during the run enters it. The run (sim/run_m1.py) and the offline tool (bench/d56_resettle.py)
both call this, so in-run settlement and the offline check use the same arithmetic.

D62 (Leaning; docs/plans/d62_guarantee_report.md): ModeledPaths builds, from the same inputs, one continuous modeled
m = 0 price response P* and one continuous acting-alone path Q*. The run and bench/d62_guarantee.py both use it.
"""
import numpy as np

from agents.planner import TERMINAL_USD_KWH, Battery, Plan, solve

ETA_REF, SD_REF = 0.95, 0.0             # not registered, so fixed constants (set before any D56 result)


def ref_battery(max_kw: float, usable_kwh: float, s_term_kwh: float) -> Battery:
    """The one input bundle 1p's Q and D62's P* and Q* are planned with (D64's declarations will enter here)."""
    return Battery(P=float(max_kw), CAP=float(usable_kwh) / 0.8, ETA=ETA_REF, SD=SD_REF, POI=1e12,
                   s_term=float(s_term_kwh))


def selfish_plan(max_kw: float, usable_kwh: float, s0_kwh: float, price, avail, base_export, wear: float,
                 dt_h: float, p_max=None, s_floor=None) -> Plan | None:
    """1p's selfish plan from the slice start (the Plan, so its SOC path is available). p_max, s_floor (D80, under
    declared_reference only): per-step power cap and SOC floor from the agent's accepted declarations, NaN where none;
    None leaves the plan exactly as before."""
    base_export = np.asarray(base_export, float)
    args = (ref_battery(max_kw, usable_kwh, s0_kwh), 0, float(s0_kwh), np.asarray(price, float),
            np.asarray(avail, float), base_export, np.zeros(len(base_export)), dt_h, wear)
    if p_max is None and s_floor is None:
        return solve(*args)
    try:            # D82: within declared limits, a planner failure means 1p has no plan (filing check, backstop)
        return solve(*args, p_max=p_max, s_floor=s_floor)
    except RuntimeError as e:
        if str(e).startswith("battery plan failed"):
            return None
        raise


def selfish_site_net(max_kw: float, usable_kwh: float, s0_kwh: float, price, avail, base_export, wear: float,
                     dt_h: float, p_max=None, s_floor=None) -> np.ndarray | None:
    """Site net (kW, export positive) per step of the selfish plan, or None if the planner finds none."""
    pl = selfish_plan(max_kw, usable_kwh, s0_kwh, price, avail, base_export, wear, dt_h, p_max, s_floor)
    return None if pl is None else np.asarray(base_export, float) + pl.d - pl.c - pl.x


def objective_bracket(price, c, d, x, s_end: float, s_term: float, wear: float, dt_h: float) -> float:
    """The planner's objective without any peak price: forecast price x battery net load x K + wear x K x discharge
    + the soft end-target term (TERMINAL_USD_KWH x energy below s_term). For D62's stage-1 identity check."""
    K = dt_h / 1000.0
    return (float(np.asarray(price, float) @ (np.asarray(c) - np.asarray(d) + np.asarray(x))) * K
            + wear * K * float(np.sum(d)) + TERMINAL_USD_KWH * max(0.0, s_term - s_end))


class ModeledPaths:
    """D62: one asset's continuous modeled response P* and acting-alone path Q* over the slice.

    Both start as Q_1, 1p's selfish plan at the first binding call. At each binding call's deadline k0 (stage):
    P* re-plans from its own SOC at k0 with that call's DA price version plus its final peak price; Q* keeps Q_1 at
    the first stage and re-plans from its own SOC with the DA version and no peak price after that. Steps before a
    stage are held. No margin, no prox, no awards: 1p's planner and inputs only."""

    def __init__(self, max_kw: float, usable_kwh: float, s0_kwh: float, avail, base_export, wear: float, dt_h: float):
        self.max_kw, self.usable, self.s0 = float(max_kw), float(usable_kwh), float(s0_kwh)
        self.avail, self.base = np.asarray(avail, float), np.asarray(base_export, float)
        self.wear, self.dt = float(wear), float(dt_h)
        T = len(self.base)
        self.P = {k: np.zeros(T) for k in "cdxs"}
        self.Q = {k: np.zeros(T) for k in "cdxs"}
        self.stages = 0
        self.last_k0 = -1
        self.stage1 = None                       # (Q_1 plan, P*'s stage-1 plan, k0, price) for the identity check

    def _soc_before(self, path: dict, k: int) -> float:
        return self.s0 if k == 0 else float(path["s"][k - 1])

    def _plan(self, k0: int, s_now: float, price, peak=None) -> Plan:
        sl = slice(k0, None)
        return solve(ref_battery(self.max_kw, self.usable, self.s0), k0, s_now, np.asarray(price, float)[sl],
                     self.avail[sl], self.base[sl], np.zeros(len(self.base) - k0), self.dt, self.wear,
                     peak=None if peak is None else np.asarray(peak, float)[sl])

    @staticmethod
    def _write(path: dict, pl: Plan, k0: int):
        for k in "cdxs":
            path[k][k0:] = getattr(pl, k)

    def stage(self, k0: int, price, peak, q1: Plan | None = None) -> dict:
        """One binding call's stage at its deadline k0. price: the call's DA version over the slice; peak: its final
        peak price over the slice ($/MWh, 0 off its window). q1: 1p's plan at the first call (first stage only).
        Returns the stage from k0 to the slice end: site net and discharge of P* and Q*, and their end SOC."""
        if not 0 <= k0 < len(self.base) or k0 <= self.last_k0:
            raise ValueError(f"stage at step {k0} after {self.last_k0}")
        if self.stages == 0:
            if q1 is None:
                raise ValueError("the first stage needs 1p's plan Q_1")
            for path in (self.P, self.Q):
                self._write(path, q1, 0)
        elif q1 is not None:
            raise ValueError("only the first stage takes Q_1")
        if self.stages:
            self._write(self.Q, self._plan(k0, self._soc_before(self.Q, k0), price), k0)
        pl = self._plan(k0, self._soc_before(self.P, k0), price, peak)
        if self.stages == 0:
            self.stage1 = (q1, pl, k0, np.asarray(price, float))
        self._write(self.P, pl, k0)
        self.stages += 1
        self.last_k0 = k0
        sl = slice(k0, None)
        return {"p_net_kw": self.net(self.P)[sl], "p_discharge_kw": self.P["d"][sl].copy(),
                "p_soc_end_kwh": float(self.P["s"][-1]), "q_net_kw": self.net(self.Q)[sl],
                "q_discharge_kw": self.Q["d"][sl].copy(), "q_soc_end_kwh": float(self.Q["s"][-1])}

    def net(self, path: dict) -> np.ndarray:
        return self.base + path["d"] - path["c"] - path["x"]

    def stage1_identity(self, peak) -> float:
        """Stage-1 identity on [d1, end): [bracket](P) - p1 (net_P - net_Q1) K - [bracket](Q_1), which must be <= 0
        (P's stage-1 LP minimises bracket - p1 (net - base) K and Q_1's tail is feasible for it). peak: call 1's final
        price over the slice, the array P's stage-1 LP used. Returns the slack in $ (<= 0 when the wiring is right)."""
        q1, pl, k0, price = self.stage1
        sl = slice(k0, None)
        K = self.dt / 1000.0
        qc, qd, qx = q1.c[sl], q1.d[sl], q1.x[sl]
        b_p = objective_bracket(price[sl], pl.c, pl.d, pl.x, float(pl.s[-1]), self.s0, self.wear, self.dt)
        b_q = objective_bracket(price[sl], qc, qd, qx, float(q1.s[-1]), self.s0, self.wear, self.dt)
        net_p = self.base[sl] + pl.d - pl.c - pl.x
        net_q = self.base[sl] + qd - qc - qx
        settle = float(np.asarray(peak, float)[sl] @ (net_p - net_q)) * K
        return b_p - settle - b_q
