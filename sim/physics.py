"""
Battery and site physics for participating agents (vectorized over agents).

Same model as bench/bench.py and M0: usable capacity = E x mean SOH, one-way efficiency =
eta_inverter x sqrt(eta_battery_rt), SOC 10-90 %, self-discharge per hour, AC power <= rating,
export at the point of interconnection <= poi_limit_kw.
Load and PV stay at recorded values. Curtailment starts at the recorded value; the inverter curtails more
only when export would break the POI limit.
"""
from dataclasses import dataclass

import numpy as np

from sim.data import Slice


@dataclass(frozen=True)
class Fleet:
    ids: np.ndarray
    P: np.ndarray       # kW
    CAP: np.ndarray     # kWh usable
    ETA: np.ndarray     # one-way
    SD: np.ndarray      # self-discharge per hour
    POI: np.ndarray     # kW export limit
    S0: np.ndarray      # kWh at slice start

    @classmethod
    def from_slice(cls, s: Slice, idx: np.ndarray) -> "Fleet":
        m = s.meta.iloc[idx]
        cap = m.bess_energy_kwh.to_numpy() * s.agent["soh"][idx].mean(1) / 100.0
        return cls(ids=s.ids[idx], P=m.bess_power_kw.to_numpy(), CAP=cap,
                   ETA=m.eta_inverter.to_numpy() * np.sqrt(m.eta_battery_rt.to_numpy()),
                   SD=m.self_discharge_per_h.to_numpy(), POI=m.poi_limit_kw.to_numpy(),
                   S0=s.agent["soc"][idx, 0] / 100.0 * cap)

    @property
    def s_min(self):
        return 0.1 * self.CAP

    @property
    def s_max(self):
        return 0.9 * self.CAP


def step(f: Fleet, soc, c_req, d_req, pv, curt, load, dt_h):
    """Apply one step of requested charge/discharge (kW >= 0). Returns actual c, d, extra curtailment, new SOC."""
    net = np.clip(d_req, 0, f.P) - np.clip(c_req, 0, f.P)
    c, d = np.maximum(-net, 0.0), np.maximum(net, 0.0)
    base = (1 - f.SD * dt_h) * soc
    d = np.minimum(d, np.maximum(base - f.s_min, 0.0) * f.ETA / dt_h)
    c = np.minimum(c, np.maximum(f.s_max - base, 0.0) / (f.ETA * dt_h))
    over = np.maximum(pv - curt + d - c - load - f.POI, 0.0)
    extra = np.minimum(over, np.maximum(pv - curt, 0.0))
    d = np.maximum(d - (over - extra), 0.0)
    soc_new = base + f.ETA * dt_h * c - dt_h / f.ETA * d
    return c, d, extra, soc_new
