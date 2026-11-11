"""
Silent attacker (D61 scripted misbehavior; M3 plan step 6).

Wraps an honest agent. From a set local day (`day`, YYYY-MM-DD in the slice's time zone; None = the whole run) it
sends no nominations: the honest agent still reads everything and plans as before, but every `nominate` it writes is
dropped. Mode `none` drops every message it would send from that day on.

Physically:
  - `selfish` (default): it runs its own selfish plan (the agent's LP from its actual SOC with no nomination, no peak
    price and no prox, re-planned each step), like the reneging wrapper.
  - `deviate` ("silent, then deviate"): it runs the honest agent's own plan, which keeps answering the published peak
    price as if it had nominated. On bound steps that departs from its 1p reference, beyond the band for a large
    battery, without any nomination at stake.

The exchange sees it only as missing nominations and through the meter. The defenses (D78/D79, under
`silent_reference`): the agent counts as nominating its 1p reference in the price loop, the binding aggregate and the
imbalance base on bound steps, and D65 counts every binding call it skipped as missed. `log` lists every dropped
message and every step where the physical request was overridden.
"""
import numpy as np

from agents.planner import solve
from exchange import messages as M


class Silent:
    def __init__(self, inner, mode: str = "selfish", day: str | None = None, local_dates=None,
                 drop: str = "nominations"):
        if mode not in ("selfish", "deviate"):
            raise ValueError(f"unknown silent mode {mode!r}")
        if drop not in ("nominations", "none"):
            raise ValueError(f"unknown silent drop {drop!r}")
        if day is not None and local_dates is None:
            raise ValueError("a silent day needs the slice's local dates")
        self._inner, self.mode, self.day, self.drop = inner, mode, day, drop
        self._local = local_dates
        self.log: list[dict] = []

    def __getattr__(self, name):               # everything else is the honest agent's
        return getattr(self._inner, name)

    def receive(self, raw: str):
        self._inner.receive(raw)

    def _silent(self, k: int) -> bool:
        return self.day is None or self._local[k] >= self.day

    def _filter(self, k: int, outs):
        if not self._silent(k):
            return outs
        keep = []
        for raw in outs:
            typ = M.decode(raw)["msg_type"]
            if self.drop == "none" or typ == "nominate":
                self.log.append({"k": k, "t_utc": self._inner.iso[k], "dropped": typ})
            else:
                keep.append(raw)
        return keep

    def act(self, k: int, soc: float, price_version: int, price: np.ndarray):
        outs, req = self._inner.act(k, soc, price_version, price)
        outs = self._filter(k, outs)
        if not self._silent(k) or self.mode == "deviate":
            return outs, req
        a = self._inner
        plan = solve(a.b, k, soc, np.asarray(price, float)[k:], a.avail[k:], a.base_export[k:], np.zeros(a.T - k),
                     a.dt_h, a.wear)
        if plan is None:                       # no selfish plan: keep the honest request
            return outs, req
        own = (float(plan.c[0]), float(plan.d[0]), float(plan.x[0]))
        if own != tuple(float(v) for v in req):
            self.log.append({"k": k, "t_utc": a.iso[k], "honest_req": [float(v) for v in req],
                             "selfish_req": list(own)})
        return outs, own

    def between_price_rounds(self, k: int, soc: float, price_version: int, price: np.ndarray) -> list[str]:
        return self._filter(k, self._inner.between_price_rounds(k, soc, price_version, price))

    def between_clearings(self, k: int, soc: float, price_version: int, price: np.ndarray) -> list[str]:
        return self._filter(k, self._inner.between_clearings(k, soc, price_version, price))
