"""What the simulator hands agents: the operator's load forecast and each battery agent's own-site and price data."""
import numpy as np
import pandas as pd

from sim.data import Slice


def operator_forecast(s: Slice, part: np.ndarray, sigma: float = 0.0, seed: int = 0) -> np.ndarray:
    """System net load (kW) if the participating batteries sat idle; everyone else at recorded dispatch.
    Optional multiplicative noise, seeded."""
    a = s.agent
    net = s.system["system_load"] * 1000.0 - (np.nan_to_num(a["pv"]) - a["curt"] + a["bess"]).sum(0)
    net = net + a["bess"][part].sum(0)
    if sigma:
        net = net * (1 + np.random.default_rng(seed).normal(0, sigma, net.shape))
    return net


class PriceForecast:
    """Day-ahead prices as agents would know them. Day D's prices are published at `publish_hour` local on D-1
    (the first day counts as published). Unpublished days repeat the last published day."""

    def __init__(self, ts_utc: pd.DatetimeIndex, price_da: np.ndarray, tz: str = "America/Chicago",
                 publish_hour: int = 12):
        local = ts_utc.tz_convert(tz)
        days = local.normalize()
        self.price = np.asarray(price_da, dtype=float)
        self.day_steps = [np.flatnonzero(days == d) for d in days.unique()]
        self.publish = [None] + [d - pd.Timedelta(days=1) + pd.Timedelta(hours=publish_hour)
                                 for d in days.unique()[1:]]
        self.ts_local = local
        self._cache: dict[int, np.ndarray] = {}

    def version(self, k: int) -> int:
        """How many days are published at step k."""
        t = self.ts_local[k]
        return 1 + sum(p <= t for p in self.publish[1:])

    def at(self, k: int) -> tuple[int, np.ndarray]:
        v = self.version(k)
        if v not in self._cache:
            fc = self.price.copy()
            last = self.day_steps[v - 1]
            for steps in self.day_steps[v:]:
                fc[steps] = np.resize(self.price[last], len(steps))
            self._cache[v] = fc
        return v, self._cache[v]
