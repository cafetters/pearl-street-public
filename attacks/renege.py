"""
Reneging attacker (D61 scripted misbehavior, first item; plan draft runs/d61/plan_draft.md, owner unreviewed).

Wraps an honest agent. Every message is the honest agent's, so its binding nominations are honest. Physically, on
every step that a locked binding nomination covers, it runs the battery to its own selfish plan instead: the
agent's LP from its actual state of charge with no nomination, no peak price and no prox, re-planned each step.
Mode "day" reneges only on one local date (`day`, YYYY-MM-DD in the agent's slice time zone).

The exchange never sees the attack except through the meter. The defenses that should answer it are all in the
exchange:
  - imbalance beyond the D33 band at the scarcity price (D48);
  - 1p settling on the metered net (undelivered relief isn't paid);
  - the D65 record's share within band.
`log` lists every step where the attack overrode the honest request.
"""
import json

import numpy as np

from agents.planner import solve


class Reneger:
    def __init__(self, inner, mode: str = "full", day: str | None = None, local_dates=None):
        if mode not in ("full", "day"):
            raise ValueError(f"unknown reneging mode {mode!r}")
        if mode == "day" and (day is None or local_dates is None):
            raise ValueError("mode 'day' needs the day and the slice's local dates")
        self._inner, self.mode, self.day = inner, mode, day
        self._local = local_dates
        self.log: list[dict] = []

    def __getattr__(self, name):               # everything else is the honest agent's
        return getattr(self._inner, name)

    def receive(self, raw: str):
        self._inner.receive(raw)

    def _reneges(self, k: int) -> bool:
        if np.isnan(self._inner.nom[k]):       # no locked binding nomination on this step
            return False
        return self.mode == "full" or self._local[k] == self.day

    def act(self, k: int, soc: float, price_version: int, price: np.ndarray):
        outs, req = self._inner.act(k, soc, price_version, price)
        if not self._reneges(k):
            return outs, req
        a = self._inner
        plan = solve(a.b, k, soc, np.asarray(price, float)[k:], a.avail[k:], a.base_export[k:], np.zeros(a.T - k),
                     a.dt_h, a.wear)
        if plan is None:                       # no selfish plan: keep the honest request
            return outs, req
        own = (float(plan.c[0]), float(plan.d[0]), float(plan.x[0]))
        self.log.append({"k": k, "t_utc": a.iso[k], "nominated_kw": float(a.nom[k]),
                         "honest_req": [float(v) for v in req], "selfish_req": list(own)})
        return outs, own


class ZeroPriceBReneger:
    """Owner, Nov 4-5 (D61 step 6; D76's zero-cap case): nominate at A, withdraw at a zero-price B.

    Honest until a later binding call's final price round. On the steps that call (B) prices at $0 while an earlier
    binding call (A) priced them above $0 (the opportunity), it answers B's final round with its selfish site net
    instead of its honest response, so its binding nomination there withdraws the relief nominated at A; on those
    steps it then runs its selfish plan, as nominated (no imbalance). Every other message is the honest agent's.
    Under today's 1p the step settles at B's price ($0), so the withdrawn relief earns nothing; under the max rule
    the relief cap pays only relief the meter shows. `log` lists each withdrawal and each overridden step;
    `opportunity` the steps."""

    def __init__(self, inner):
        self._inner = inner
        self.log: list[dict] = []
        self.binding: dict[str, bool] = {}
        self.final: dict[str, np.ndarray] = {}           # call_id -> final price per step (NaN off its window)
        self.order: list[str] = []                         # binding calls in the order their final price came
        self.opportunity: set[int] = set()
        self._pending: tuple | None = None                 # (call_id, steps) to withdraw at the next round answer

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def receive(self, raw: str):
        self._inner.receive(raw)
        try:
            m = json.loads(raw)
        except ValueError:
            return
        p = m.get("payload") or {}
        if m.get("msg_type") == "call_nominations":
            self.binding[p["call_id"]] = bool(p["binding"])
        elif m.get("msg_type") == "peak_price" and p.get("final") and self.binding.get(p["call_id"]):
            a = self._inner
            mask = a._mask(p["window"])
            arr = np.full(a.T, np.nan)
            arr[mask] = np.asarray(p["price_usd_mwh"], float)[:mask.sum()]
            earlier = [self.final[c] for c in self.order if c != p["call_id"]]
            hit = []
            for j in np.flatnonzero(mask):
                if arr[j] == 0.0 and any(e[j] > 0.0 for e in earlier if not np.isnan(e[j])):
                    hit.append(int(j))
            self.final[p["call_id"]] = arr
            if p["call_id"] not in self.order:
                self.order.append(p["call_id"])
            if hit:
                self._pending = (p["call_id"], hit)

    def _selfish_net(self, k: int, soc: float, price) -> np.ndarray | None:
        a = self._inner
        plan = solve(a.b, k, soc, np.asarray(price, float)[k:], a.avail[k:], a.base_export[k:], np.zeros(a.T - k),
                     a.dt_h, a.wear)
        if plan is None:
            return None
        net = np.full(a.T, np.nan)
        net[k:] = a.base_export[k:] + plan.d - plan.c - plan.x
        return net

    def between_price_rounds(self, k: int, soc: float, price_version: int, price: np.ndarray) -> list[str]:
        outs = self._inner.between_price_rounds(k, soc, price_version, price)
        if self._pending is None:
            return outs
        cid, hit = self._pending
        self._pending = None
        own = self._selfish_net(k, soc, price)
        if own is None:
            return outs
        a = self._inner
        new = []
        for raw in outs:
            m = json.loads(raw)
            if m.get("msg_type") == "nominate" and m["payload"].get("call_id") == cid:
                idx = np.flatnonzero(a._mask(m["payload"]["window"]))
                kw = list(m["payload"]["kw"])
                done = []
                for i, j in enumerate(idx[:len(kw)]):
                    if j in hit and not np.isnan(own[j]):
                        kw[i] = float(round(own[j], 3))
                        done.append(int(j))
                if done:
                    m["payload"]["kw"] = kw
                    self.opportunity.update(done)
                    self.log.append({"k": k, "t_utc": a.iso[k], "call_id": cid, "withdrawn_steps": done})
                    raw = a._msg("nominate", m["t_utc"], m["payload"])
            new.append(raw)
        return new

    def act(self, k: int, soc: float, price_version: int, price: np.ndarray):
        outs, req = self._inner.act(k, soc, price_version, price)
        if k not in self.opportunity:
            return outs, req
        a = self._inner
        plan = solve(a.b, k, soc, np.asarray(price, float)[k:], a.avail[k:], a.base_export[k:], np.zeros(a.T - k),
                     a.dt_h, a.wear)
        if plan is None:
            return outs, req
        own = (float(plan.c[0]), float(plan.d[0]), float(plan.x[0]))
        self.log.append({"k": k, "t_utc": a.iso[k], "honest_req": [float(v) for v in req], "selfish_req": list(own)})
        return outs, own
