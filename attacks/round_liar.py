"""REP-style liar (M4's REP comparison, D24; report only).

Wraps an honest agent. In non-binding price rounds it misreports its response to the posted peak price; everything
else, including every binding nomination, is the honest agent's own:
  - overstate: selfish + 2 x (honest - selfish), i.e. twice the shift it actually plans;
  - understate: its selfish schedule (claims not to respond at all).
"Selfish" is the first schedule the agent nominated for that call, made before any price round priced its window.
The honest agent keeps planning on what it honestly computed (its own record of what it sent stays honest); only the
exchange sees the lie. It tests whether free talk in non-binding rounds (never settled; D61's "free signaling channel")
moves the price.
"""
from __future__ import annotations

from exchange import messages as M


class RoundLiar:
    MODES = ("overstate", "understate")

    def __init__(self, inner, mode: str = "overstate"):
        if mode not in self.MODES:
            raise ValueError(f"unknown round_liar mode {mode!r}")
        self._inner, self.mode = inner, mode
        self._selfish: dict[str, list[float]] = {}
        self.log: list[dict] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def receive(self, raw: str):
        self._inner.receive(raw)

    def _remember(self, outs):
        for raw in outs:
            m = M.decode(raw)
            if m["msg_type"] == "nominate":
                self._selfish.setdefault(m["payload"]["call_id"], list(m["payload"]["kw"]))

    def act(self, k, soc, price_version, price):
        outs, req = self._inner.act(k, soc, price_version, price)
        self._remember(outs)
        return outs, req

    def between_price_rounds(self, k, soc, price_version, price):
        outs = self._inner.between_price_rounds(k, soc, price_version, price)
        res = []
        for raw in outs:
            m = M.decode(raw)
            p = m["payload"]
            cid = p.get("call_id")
            call = (self._inner.calls or {}).get(cid) or {}
            if m["msg_type"] != "nominate" or call.get("binding", True) or cid not in self._selfish:
                res.append(raw)
                continue
            honest, base = [float(v) for v in p["kw"]], self._selfish[cid]
            if len(base) != len(honest):
                res.append(raw)
                continue
            lie = ([b + 2.0 * (h - b) for h, b in zip(honest, base)] if self.mode == "overstate" else list(base))
            p2 = {**p, "kw": [round(v, 3) for v in lie]}
            res.append(M.encode({**m, "payload": p2}))
            self.log.append({"k": k, "call_id": cid, "mode": self.mode,
                             "honest_sum_kw": sum(honest), "sent_sum_kw": sum(lie)})
        return res
