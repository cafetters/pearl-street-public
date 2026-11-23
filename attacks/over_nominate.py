"""Over-nominator (M5 firmness item, owner Nov 19, plan step (a)): promises relief it can't deliver.

Wraps an honest agent. Every nomination it sends (price rounds and binding) adds `extra_kw` of export on every step
of the window, i.e. relief beyond what its battery can give; the battery itself runs the honest agent's plan, so the
meter shows only the honest response. The exchange sees the lie in the zone total (a lower load), so arm C's price can
fall and honest agents respond less; the operator counts relief that never arrives.

`extra_kw` defaults to (factor - 1) x the battery's power (factor 3: the agent claims three times its power). The
nominations are not bounded by power today (Q-N10). `log` lists every inflated nomination.
"""
from __future__ import annotations

from exchange import messages as M


class OverNominator:
    def __init__(self, inner, factor: float = 3.0, extra_kw: float | None = None):
        self._inner = inner
        self.extra_kw = float(extra_kw) if extra_kw is not None else (float(factor) - 1.0) * float(inner.b.P)
        self.log: list[dict] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def receive(self, raw: str):
        self._inner.receive(raw)

    def _inflate(self, k, outs):
        res = []
        for raw in outs:
            m = M.decode(raw)
            if m["msg_type"] != "nominate":
                res.append(raw)
                continue
            p = m["payload"]
            kw = [round(float(v) + self.extra_kw, 3) for v in p["kw"]]
            res.append(M.encode({**m, "payload": {**p, "kw": kw}}))
            self.log.append({"k": k, "call_id": p["call_id"], "extra_kw": self.extra_kw, "steps": len(kw)})
        return res

    def start(self, k):
        return self._inflate(k, self._inner.start(k))

    def act(self, k, soc, price_version, price):
        outs, req = self._inner.act(k, soc, price_version, price)
        return self._inflate(k, outs), req

    def between_price_rounds(self, k, soc, price_version, price):
        return self._inflate(k, self._inner.between_price_rounds(k, soc, price_version, price))

    def between_clearings(self, k, soc, price_version, price):
        return self._inflate(k, self._inner.between_clearings(k, soc, price_version, price))
