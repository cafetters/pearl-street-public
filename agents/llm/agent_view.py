"""AgentView: the only thing a chooser, a cadence trigger or the prompt builder may read (D59 firewall, D70).

Built by code from the agent's own fields and an allow-listed digest of what the exchange sent it:
- numbers are coerced to float or int; IDs (call_id, msg_id, ref_msg_id) are never kept;
- reject `detail`, `refs`, extra payload keys and any other free text are never kept; a reject code is kept only if
  it is one of exchange.messages.REJECT_CODES;
- messages not sent by `exchange` are ignored (D69 rule 2), so a forged sender never reaches the view;
- never: other agents' data, real-time prices, zone_target, the acting-alone run or the run config.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from exchange import messages as M

VIEW_VERSION = "view-1"
_DIGEST_TYPES = {"imbalance", "reject", "peak_settle", "peak_price", "call_nominations"}


def _num(x) -> float:
    v = float(x)
    if not math.isfinite(v):
        raise ValueError("non-finite number")
    return v


@dataclass(frozen=True)
class AgentView:
    decision_day: str                         # local date the labels will apply to (the newly published DA day)
    reason: str                               # start | scheduled | trigger:<names>
    power_kw: float
    energy_kwh: float
    bold_allowed: bool
    current_label: str
    label_history: tuple                      # own previous labels (menu values only)
    da_prices_hourly: tuple                   # published DA prices for decision_day, hourly mean, $/MWh
    da_spread: float                          # max - min of decision_day's published DA prices, $/MWh
    da_spread_threshold: float | None         # T4: 75th percentile of trailing 30 days of published DA spreads
    net_load_forecast_hourly: tuple           # own load - own PV forecast for decision_day, hourly mean kW
    peak_price_max: float | None              # last peak_price broadcast's maximum, $/MWh
    peak_price_steps: int                     # steps priced above zero in that broadcast
    imbalance_usd_since_last_call: float      # T3
    rejects_since_eval: int                   # T2
    reject_codes: tuple                       # codes from REJECT_CODES only
    binding_calls_since_eval: int             # T6
    peak_settle_net_usd: float | None         # T1 (arrives at the closing tick only)
    peak_settle_refund_usd: float | None
    site_net_forecast_kwh_yesterday: float | None  # T5: own site net load (load - PV) forecast vs metered, the
    site_net_metered_kwh_yesterday: float | None   # day before; equal when agents plan on the recorded series


@dataclass
class InboxDigest:
    """Allow-listed, typed running counters from the exchange's messages to one agent."""
    imbalance_usd_since_last_call: float = 0.0
    rejects_since_eval: int = 0
    reject_codes: list = field(default_factory=list)
    binding_calls_since_eval: int = 0
    peak_settle_net_usd: float | None = None
    peak_settle_refund_usd: float | None = None
    peak_price_max: float | None = None
    peak_price_steps: int = 0
    dropped: int = 0                          # messages ignored (wrong sender, bad fields)

    def observe(self, raw: str) -> None:
        try:
            m = M.decode(raw)
        except Exception:
            self.dropped += 1
            return
        if m.get("sender") != "exchange" or m.get("msg_type") not in _DIGEST_TYPES:
            if m.get("msg_type") in _DIGEST_TYPES:
                self.dropped += 1
            return
        p, typ = m.get("payload") or {}, m["msg_type"]
        try:
            if typ == "imbalance":
                self.imbalance_usd_since_last_call += max(_num(p.get("charge_usd", 0.0)), 0.0)
            elif typ == "reject":
                self.rejects_since_eval += 1
                code = p.get("code")
                if isinstance(code, str) and code in M.REJECT_CODES:
                    self.reject_codes.append(code)
            elif typ == "peak_settle":
                self.peak_settle_net_usd = _num(p["net_usd"])
                self.peak_settle_refund_usd = _num(p["refund_usd"])
            elif typ == "peak_price":
                prices = [_num(v) for v in (p.get("price_usd_mwh") or [])]
                self.peak_price_max = max(prices) if prices else 0.0
                self.peak_price_steps = int(sum(1 for v in prices if v > 1e-9))
            elif typ == "call_nominations":
                if p.get("binding") is True:
                    self.binding_calls_since_eval += 1
        except (KeyError, TypeError, ValueError):
            self.dropped += 1

    def reset_eval(self) -> None:
        self.rejects_since_eval, self.reject_codes, self.binding_calls_since_eval = 0, [], 0
        self.peak_settle_net_usd = self.peak_settle_refund_usd = None

    def reset_call(self) -> None:
        self.imbalance_usd_since_last_call = 0.0


def hourly(values, n_per_hour: int = 4) -> tuple:
    v = [float(x) for x in values]
    return tuple(round(sum(v[i:i + n_per_hour]) / len(v[i:i + n_per_hour]), 3) for i in range(0, len(v), n_per_hour))
