"""Message bus: routes JSON between the exchange and agents, and logs every message to the trace.

D69 (rules 1 and 3): every message is sent from a named origin endpoint, and the bus binds `sender` to it.
  - A message whose `sender` is not its origin, or that isn't a JSON object with a string sender, is not delivered;
    it is logged as `dropped` with a reason and its true origin (TraceLogger.refuse). No reply is sent.
  - Only the exchange delivers to other endpoints. Any other origin may address only "exchange", unless the run
    plants a direct channel among a set of endpoints (`planted_channels`, D61's collusion rounds; none by default).
Refused messages go to `self.refused`, not `self.log`, so run summaries never count them as delivered traffic.

Memory (fix 1, Nov 5): `self.log` is what the run summary reads; the trace has every message in full. For the message
types in `slim`, the log keeps the envelope (msg_id, run_id, sender, to, t_utc, msg_type) without the payload, so a
reader that needs the payload fails loudly instead of reading a stale or empty one. sim/run_m1.py slims
`nominate` and `telemetry` (counted per day, never read), and keeps `nominate` in full when the D34 referee runs.
The default keeps every message in full.
"""
import sys
from collections import deque

from exchange import messages as M


class Bus:
    def __init__(self, logger, exchange=None, planted_channels=(), slim=()):
        self.logger, self.exchange = logger, exchange
        self.slim = frozenset(slim)       # message types logged as envelope only (fix 1)
        self.endpoints = {}
        self.queue: deque[tuple[str, str]] = deque()      # (origin endpoint, raw message)
        self.log: list[dict] = []        # every routed message, decoded (for the run summary; `slim` types envelope only)
        self.refused: list[dict] = []    # D69: messages refused for their sender, recipient or form
        self.channels = [frozenset(c) for c in planted_channels]
        if any(len(c) < 2 for c in self.channels):
            raise ValueError("a planted channel needs at least two endpoints")

    def attach(self, endpoint_id: str, endpoint):
        self.endpoints[endpoint_id] = endpoint

    def send(self, raws, origin: str):
        """Queue messages sent by endpoint `origin` (an agent, the operator, the metering service or the clock).
        Only the bus itself queues the exchange's replies."""
        if not isinstance(origin, str) or origin == "exchange":
            raise ValueError("origin must name the sending endpoint, and may not be the exchange")
        self.queue.extend((origin, r) for r in raws)

    def _direct_ok(self, origin, to) -> bool:
        recipients = to if isinstance(to, list) else [to]
        return all(isinstance(r, str) and any(origin in c and r in c for c in self.channels) for r in recipients)

    def _refuse(self, origin, raw, msg, reason):
        self.logger.refuse(raw, msg, origin, reason)
        self.refused.append({"origin": origin, "reason": reason,
                             "msg_type": msg.get("msg_type") if isinstance(msg, dict) else None})
        if self.exchange is not None and getattr(self.exchange, "refusal_notice", False):
            # D69 rule 1 amendment (refusal_notice, off by default): the exchange answers the true origin
            self.queue.extend(("exchange", r) for r in self.exchange.bus_refusal(origin, reason, msg))

    def flush(self):
        while self.queue:
            origin, raw = self.queue.popleft()
            try:
                msg = M.decode(raw)
            except (ValueError, TypeError, RecursionError):      # hostile input never stops the bus
                msg = None
            if not (isinstance(msg, dict) and set(M.ENVELOPE) <= set(msg) and isinstance(msg["sender"], str)):
                self._refuse(origin, raw, None, "malformed")
                continue
            if msg["sender"] != origin:
                self._refuse(origin, raw, msg, "sender_mismatch")
                continue
            to = msg["to"]
            if origin != "exchange" and to != "exchange" and not self._direct_ok(origin, to):
                self._refuse(origin, raw, msg, "direct_delivery")
                continue
            if isinstance(to, list):
                self.logger.log(raw, "broadcast")
                for r in to:
                    if r in self.endpoints:
                        self.endpoints[r].receive(raw)
            elif to == "exchange" and self.exchange is not None:
                self.logger.log(raw, "send")
                self.queue.extend(("exchange", r) for r in self.exchange.handle(raw))
            elif isinstance(to, str) and to in self.endpoints:
                self.logger.log(raw, "send")
                self.endpoints[to].receive(raw)
            else:
                self.logger.log(raw, "dropped")
            if msg["msg_type"] in self.slim:      # the envelope without the payload; repeated strings shared
                msg = {k: (sys.intern(v) if isinstance(v, str) and k != "msg_id" else v)
                       for k, v in msg.items() if k != "payload"}
            self.log.append(msg)
