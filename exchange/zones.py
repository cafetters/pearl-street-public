"""Arm C zone product (M2c; D99, Leaning): per-element price components for transformer and line zones.

Plain Python, no numeric dependencies and no file reads (exchange/ rule): sim/ builds the elements from the grid
(sim/grid.py) and passes them in.

Units follow exchange/price_mode.py: prices in $/kW-step (K = step hours / 1000; $/MWh = value / K), load positive.

Each monitored element k has
  rows[a]   PTDF of asset a's bus on k (MW of flow per MW injected); rows with |PTDF| < ptdf_min are dropped, so a
            far asset never gets a component (D9's membership threshold)
  limit_mw  0.9 x rating (D10)
  base_mw   per step: the flow from everything that is not an exchange asset (signed, from -> to)
  cap       the operator's per-zone cap c_k (D68), $/MWh

flow_k,t = base_k,t + sum_a rows[a] x injection_a,t / 1000   (injection = -load, kW)
lambda_k,t <- min(c_k K, max(0, lambda_k,t + alpha x (|flow_k,t| - limit_k) x 1000))   (projected dual ascent)
component_a,t = sum_k lambda_k,t x (-direction_k,t x rows_k[a])                      ($/kW-step on load)

direction_k,t is the sign of the element's flow at the last update. One more kW of load at asset a moves |flow_k| by
-direction x PTDF / 1000 MW, so a positive component charges load that worsens the overload and pays load that
relieves it (sign-aware). Overlapping zones simply add. Settlement is D56's 1p on each component:
component x (reference load - metered load), so a schedule equal to its reference settles exactly $0.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PTDF_MIN = 0.05


@dataclass
class Element:
    key: str
    rows: dict                    # asset -> PTDF
    limit_mw: float
    base_mw: dict                 # step -> MW
    cap_usd_mwh: float
    ptdf_min: float = PTDF_MIN

    def __post_init__(self):
        self.rows = {a: float(v) for a, v in self.rows.items() if abs(v) >= self.ptdf_min}
        if not self.cap_usd_mwh >= 0:
            raise ValueError(f"{self.key}: zone cap must be >= 0 (D68)")
        if not self.limit_mw > 0:
            raise ValueError(f"{self.key}: limit must be > 0")


@dataclass
class ZoneBook:
    elements_in: list
    step_h: float
    alpha: float                                   # $/kW-step per kW of excess flow (base step)
    adapt_tau: float | None = None                 # residual-balancing-style step per element; None: fixed step
    alpha_range: tuple = (1e-3, 1e6)               # adaptive steps stay within base x these factors
    elements: dict = field(init=False)
    lam: dict = field(init=False)                  # key -> step -> $/kW-step
    _dir: dict = field(init=False)                 # key -> step -> sign of the flow at the last update

    def __post_init__(self):
        self.elements = {e.key: e for e in self.elements_in}
        if len(self.elements) != len(self.elements_in):
            raise ValueError("duplicate zone element")
        self.lam = {k: {} for k in self.elements}
        self._dir = {k: {} for k in self.elements}
        self.K = self.step_h / 1000.0
        self.alpha_k: dict = {}                     # per element, set from the base step at the first update
        self._was_over: dict = {k: False for k in self.elements}
        self.dlam: dict = {k: {} for k in self.elements}   # last round's lambda change per step (for the shift)

    def norm2(self, k: str) -> float:
        """||row_k||^2 over the element's members (the weighted analogue of arm C's N)."""
        return sum(r * r for r in self.elements[k].rows.values())

    def set_rho(self, rho: float) -> None:
        """ADMM's step for a weighted coupling row: alpha_k = rho / ||row_k||^2 (arm C's alpha = rho / N when every
        row weight is 1)."""
        self.alpha = float(rho)
        for k in self.elements:
            n2 = self.norm2(k)
            self.alpha_k[k] = self.alpha / n2 if n2 > 0 else 0.0

    def shift_unit_kw(self, k: str, t: str) -> float:
        """kW per unit of (-direction x row): the last lambda change over (alpha_k ||row_k||^2), the zone analogue of
        arm C's centre shift (pi_new - pi_old) / (alpha N)."""
        a, n2 = self.alpha_k.get(k, 0.0), self.norm2(k)
        return self.dlam[k].get(t, 0.0) / (a * n2) if a > 0 and n2 > 0 else 0.0

    def member_shift_kw(self, asset: str, k: str, t: str) -> float:
        """The asset's prox-centre shift for element k at t, kW of more injection (less load): moving every member by
        its shift moves |flow| by the excess the last step priced (exactly, where lambda was not projected)."""
        r = self.elements[k].rows.get(asset, 0.0)
        return -self._dir[k].get(t, 0.0) * r * self.shift_unit_kw(k, t)

    def set_base_alpha(self, alpha: float) -> None:
        """The base step; elements already stepping keep their own (adaptive) step."""
        self.alpha = float(alpha)
        for k in self.elements:
            self.alpha_k.setdefault(k, self.alpha)

    # --- flows -------------------------------------------------------------------------------------------------
    def flow_mw(self, k: str, t: str, inj_kw: dict) -> float:
        e = self.elements[k]
        return e.base_mw.get(t, 0.0) + sum(r * inj_kw.get(a, {}).get(t, 0.0) for a, r in e.rows.items()) / 1000.0

    def direction(self, k: str, t: str) -> float:
        return self._dir[k].get(t, 0.0)

    def excess_mw(self, k: str, t: str, inj_kw: dict) -> float:
        return abs(self.flow_mw(k, t, inj_kw)) - self.elements[k].limit_mw

    # --- prices ------------------------------------------------------------------------------------------------
    def update(self, steps, inj_kw: dict) -> dict:
        """One joint round over every element and step; returns {key: max excess kW} for the price log.
        With adapt_tau, each element's step doubles (x tau) while its overload persists from the last round, and
        halves (/ tau) once it is under its limit everywhere while still priced (an overshoot), within alpha_range
        of the base step: the zone analogue of arm C's residual balancing."""
        out = {}
        for k, e in self.elements.items():
            a = self.alpha_k.setdefault(k, self.alpha)
            cap = e.cap_usd_mwh * self.K
            ex = {}
            for t in steps:
                f = self.flow_mw(k, t, inj_kw)
                self._dir[k][t] = 1.0 if f > 0 else (-1.0 if f < 0 else 0.0)
                ex[t] = abs(f) - e.limit_mw
            worst = max((x * 1000.0 for x in ex.values()), default=0.0)
            over = worst > 0.0
            if self.adapt_tau:
                lo, hi = self.alpha * self.alpha_range[0], self.alpha * self.alpha_range[1]
                if over and self._was_over[k]:
                    a = min(hi, a * self.adapt_tau)
                elif not over and any(self.lam[k].get(t, 0.0) > 0.0 for t in steps):
                    a = max(lo, a / self.adapt_tau)
                self.alpha_k[k] = a
            self._was_over[k] = over
            for t in steps:
                old = self.lam[k].get(t, 0.0)
                self.lam[k][t] = min(cap, max(0.0, old + a * ex[t] * 1000.0))
                self.dlam[k][t] = self.lam[k][t] - old
            out[k] = worst
        return out

    def component(self, asset: str, t: str) -> float:
        """$/kW-step on the asset's load at step t, summed over elements (0 for an asset in no zone)."""
        return sum(self.lam[k].get(t, 0.0) * (-self._dir[k].get(t, 0.0) * e.rows[asset])
                   for k, e in self.elements.items() if asset in e.rows)

    def components_by_element(self, asset: str, t: str) -> dict:
        return {k: self.lam[k].get(t, 0.0) * (-self._dir[k].get(t, 0.0) * e.rows[asset])
                for k, e in self.elements.items() if asset in e.rows}

    def at_cap(self, k: str, t: str) -> bool:
        return self.lam[k].get(t, 0.0) >= self.elements[k].cap_usd_mwh * self.K - 1e-15

    # --- settlement (D56 1p on each component) -----------------------------------------------------------------
    def settle_component(self, asset: str, ref_kw: dict, metered_kw: dict) -> float:
        """Net $ to the asset's holder from every zone component: component x (reference - metered), load kW.
        Exactly 0 when the metered schedule equals the reference, and 0 for an asset in no zone."""
        total = 0.0
        for t in ref_kw:
            c = self.component(asset, t)
            if c:
                total += c * (ref_kw[t] - metered_kw.get(t, 0.0))
        return total

    # --- reports -----------------------------------------------------------------------------------------------
    def violations(self, steps, inj_kw: dict) -> dict:
        """Remaining MW over each element's limit per step (0 where within), unrounded."""
        return {k: {t: max(0.0, self.excess_mw(k, t, inj_kw)) for t in steps} for k in self.elements}

    def max_excess_kw(self, steps, inj_kw: dict) -> float:
        return max((max(0.0, self.excess_mw(k, t, inj_kw)) * 1000.0 for k in self.elements for t in steps),
                   default=0.0)
