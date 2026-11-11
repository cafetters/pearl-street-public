"""
Declaration attackers (D61 scripted misbehavior; M3 plan step 6). Each wraps an honest agent; every other message is
the honest agent's.

- `LateOutage` (late or false outage): at the first step of local day `day` after a binding call's deadline has
  passed (its steps are bound), it declares a forced outage over the rest of that call's window. Mode `stop` (a)
  then idles the battery over the outage; mode `run` (b) keeps running the honest plan (a false outage).
  Defenses: imbalance on bound steps is never excused (D80), D65 counts the outage late, and for (b) the battery
  meter shows activity during the declared outage, a breach that voids the day (D81).
- `Misdeclare`: for every local day whose day-ahead deadline hasn't passed, it declares less power (mode `power`:
  `frac` x registered power) or more reserve (mode `reserve`: `frac` x usable energy), then runs the honest plan at
  full power. Without the void rule the lowered 1p reference pays for relief the battery could give anyway. Defense:
  the battery meter breaches the declaration (output above declared power, SOC below declared reserve) and the day is
  void (D81); detection reads only the battery meter and SOC.
- `InfeasibleDeclare` (owner, Nov 4: "file declarations that make 1p infeasible"): at the first step it files the
  D64 check-in case, a forced outage over the first local day (1p's SOC stays at its start) and, for the next local
  day, power 0 with a reserve above the starting SOC; mode `reserve_first` files them the other way round. It keeps
  running the honest plan. Defense (D82 as amended): the outage stands, the reserve is rejected or voided with
  `infeasible_declaration`, and the run never stops.
`log` lists every declaration filed and every overridden request.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np


class _Wrapper:
    def __init__(self, inner):
        self._inner = inner
        self.log: list[dict] = []

    def __getattr__(self, name):               # everything else is the honest agent's
        return getattr(self._inner, name)

    def receive(self, raw: str):
        self._inner.receive(raw)

    def _declare(self, k: int, msg_type: str, payload: dict) -> str:
        self.log.append({"k": k, "t_utc": self._inner.iso[k], "msg_type": msg_type, **payload})
        return self._inner._msg(msg_type, self._inner.iso[k], payload)


def _local_dates(iso: list[str], tz: str) -> list[str]:
    z = ZoneInfo(tz)
    return [datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(z).date().isoformat() for t in iso]


def _deadline_utc(day: str, tz: str, publish_hour: int) -> str:
    """A local day's day-ahead deadline: the day before, at the price publish hour local (the exchange's rule)."""
    d = date.fromisoformat(day) - timedelta(days=1)
    t = datetime(d.year, d.month, d.day, publish_hour, tzinfo=ZoneInfo(tz))
    return t.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


class LateOutage(_Wrapper):
    def __init__(self, inner, mode: str, day: str, tz: str):
        if mode not in ("stop", "run"):
            raise ValueError(f"unknown outage mode {mode!r}")
        super().__init__(inner)
        self.mode, self.day = mode, day
        self._local = _local_dates(inner.iso, tz)
        self.window: tuple[int, int] | None = None     # outage steps [k0, k1)

    def act(self, k: int, soc: float, price_version: int, price: np.ndarray):
        outs, req = self._inner.act(k, soc, price_version, price)
        a = self._inner
        if self.window is None and self._local[k] == self.day:
            bound = [c for c in a.calls.values() if c["binding"] and c["deadline_utc"] <= a.iso[k] and c["mask"][k:].any()]
            if bound:
                k1 = max(int(np.flatnonzero(c["mask"])[-1]) for c in bound) + 1
                self.window = (k, k1)
                end = a.iso[k1] if k1 < a.T else a.end_iso
                outs = list(outs) + [self._declare(k, "declare_outage", {
                    "asset_id": a.asset_id, "window": {"start_utc": a.iso[k], "end_utc": end}})]
        if self.mode == "stop" and self.window is not None and self.window[0] <= k < self.window[1]:
            if tuple(float(v) for v in req) != (0.0, 0.0, 0.0):
                self.log.append({"k": k, "t_utc": a.iso[k], "honest_req": [float(v) for v in req],
                                 "outage_req": [0.0, 0.0, 0.0]})
            return outs, (0.0, 0.0, 0.0)
        return outs, req


class Misdeclare(_Wrapper):
    def __init__(self, inner, mode: str, tz: str, publish_hour: int, frac: float | None = None):
        if mode not in ("power", "reserve"):
            raise ValueError(f"unknown misdeclaration mode {mode!r}")
        super().__init__(inner)
        self.mode = mode
        self.frac = float(frac if frac is not None else (0.5 if mode == "power" else 0.9))
        days = sorted(set(_local_dates(inner.iso, tz)))
        self._todo = {d: _deadline_utc(d, tz, publish_hour) for d in days}

    def act(self, k: int, soc: float, price_version: int, price: np.ndarray):
        outs, req = self._inner.act(k, soc, price_version, price)
        a = self._inner
        usable = 0.8 * float(a.b.CAP)
        for d, deadline in sorted(self._todo.items()):
            if a.iso[k] < deadline:
                power = self.frac * float(a.b.P) if self.mode == "power" else float(a.b.P)
                reserve = self.frac * usable if self.mode == "reserve" else 0.0
                outs = list(outs) + [self._declare(k, "declare_availability", {
                    "asset_id": a.asset_id, "date": d, "power_kw": power, "reserve_kwh": reserve,
                    "planned_outage": False})]
                del self._todo[d]
        return outs, req


class InfeasibleDeclare(_Wrapper):
    def __init__(self, inner, tz: str, order: str = "outage_first"):
        if order not in ("outage_first", "reserve_first"):
            raise ValueError(f"unknown order {order!r}")
        super().__init__(inner)
        self.order = order
        self._local = _local_dates(inner.iso, tz)
        self._done = False

    def act(self, k: int, soc: float, price_version: int, price: np.ndarray):
        outs, req = self._inner.act(k, soc, price_version, price)
        if self._done:
            return outs, req
        self._done = True
        a = self._inner
        days = sorted(set(self._local))
        if len(days) < 2:
            return outs, req
        k1 = self._local.index(days[1])
        usable = 0.8 * float(a.b.CAP)
        outage = ("declare_outage", {"asset_id": a.asset_id, "window": {"start_utc": a.iso[0], "end_utc": a.iso[k1]}})
        reserve = ("declare_availability", {"asset_id": a.asset_id, "date": days[1], "power_kw": 0.0,
                                            "reserve_kwh": min(usable, max(float(soc) + 0.5 * (usable - float(soc)),
                                                                           float(soc) + 1.0)),
                                            "planned_outage": False})
        pair = [outage, reserve] if self.order == "outage_first" else [reserve, outage]
        return list(outs) + [self._declare(k, t, p) for t, p in pair], req
