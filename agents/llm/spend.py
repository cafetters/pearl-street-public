"""Prices, the fail-closed run cap and the phase allocator (D94).

Per run: before every call, the worst-case cost (estimated input tokens plus max_tokens of output, at the tier the
input falls in) must fit under cap - spent; otherwise no call is made and the run's cap is marked hit. Per phase: the
launcher reserves each run's whole cap against the phase cap before starting it, so the phase cap holds even if every
run spends its cap; spent is reconciled from each run's call log afterwards.

A model with no price row is refused for live calls (fail closed). Fake-backend runs record it as unpriced.

Course route (owner, Nov 23): a row with `course_only` may be called live only on the course route. Each call is
charged the larger of its table cost and the cost the gateway reported; the worst case when an attempt may have been
billed with no cost reported (Response.meta["cost_unknown"]); and a reported cost above the table cost by more than
OVER_TABLE_ABS_USD + OVER_TABLE_REL marks the cap hit, because the pre-call worst case can no longer be trusted.
"""
from __future__ import annotations

PRICE_TABLE_VERSION = "prices-2026-11-12-verified"
_ANTHROPIC = "https://platform.claude.com/docs/en/about-claude/pricing"
_OPENAI = "https://developers.openai.com/api/docs/pricing"
# $ per million tokens, standard tier, base input and output (no batch, cache, fast-mode or regional multipliers).
# Each row was read from the provider's own pricing page on the date in `checked` (owner decision 6, Nov 12).
# `long`: the higher prices for a prompt over `long_above` input tokens (per request); None: one price at any length.
# `live`: False refuses live calls to the model (LiveRefused); the fake backend still prices it.
# `course_only`: live calls only on the course route (the course's LLM gateway). `course_checked`: the gateway's
# reported cost matched this row on the date given (the probe calls of Nov 23: Haiku twice, luna once).
PRICES = {
    "anthropic/claude-haiku-5-5": {"in": 0.10, "out": 0.50, "long": {"in": 0.50, "out": 2.50}, "long_above": 100_000,
                                   "live": True, "source_url": _ANTHROPIC, "checked": "2026-11-12",
                                   "course_checked": "2026-11-23"},
    "anthropic/claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "long": None, "live": True,
                                    "source_url": _ANTHROPIC, "checked": "2026-11-12"},
    "anthropic/claude-opus-5-5": {"in": 4.00, "out": 20.00, "long": None, "live": True,
                                  "source_url": _ANTHROPIC, "checked": "2026-11-12"},
    # Live only through the course's LLM gateway (owner, Nov 23); there is no direct OpenAI key (D96).
    "openai/gpt-6-luna": {"in": 0.10, "out": 0.50, "long": {"in": 0.20, "out": 0.75}, "long_above": 272_000,
                          "live": True, "course_only": True, "source_url": _OPENAI, "checked": "2026-11-12",
                          "course_checked": "2026-11-23"},
}


ROUTES = ("direct", "course")
# Prompt caching on the course route: the gateway caches prompts by itself and bills a cache write at 2x the input price
# (a 1-hour cache) and a cache read at 0.1x. Inferred Nov 23 from four reported costs (Haiku and Sonnet, two writes and
# two reads of an 853-token prompt) that this formula reproduces to the reported precision. The worst case on the
# course route counts every input token as a cache write.
CACHE_WRITE_MULT = 2.0
CACHE_READ_MULT = 0.1
WORST_INPUT_MULT = {"direct": 1.0, "course": CACHE_WRITE_MULT}
OVER_TABLE_ABS_USD = 0.000002
OVER_TABLE_REL = 0.01


def live_allowed(model_id: str, route: str = "direct") -> bool:
    row = PRICES.get(model_id)
    return bool(row and row.get("live", False) and (route == "course" or not row.get("course_only", False)))


class CapReached(Exception):
    pass


class PriceMissing(Exception):
    pass


class LiveRefused(PriceMissing):
    """The model has a price row but live calls to it are not allowed yet (its row says live: False)."""


def _rates(model_id: str, input_tokens: int) -> tuple[float, float]:
    row = PRICES.get(model_id)
    if row is None:
        raise PriceMissing(model_id)
    tier = row["long"] if row.get("long") and input_tokens > row["long_above"] else None
    return (tier["in"], tier["out"]) if tier else (row["in"], row["out"])


def cost_usd(model_id: str, input_tokens: int, output_tokens: int, cache_write_tokens: int = 0,
             cache_read_tokens: int = 0) -> float:
    """Table cost. Input tokens not read from or written to a cache, plus cache writes and reads at their multiples of
    the input price; the long-context tier is chosen by the whole prompt (all three)."""
    tin, cw, cr = int(input_tokens), int(cache_write_tokens), int(cache_read_tokens)
    pin, pout = _rates(model_id, tin + cw + cr)
    return (tin * pin + cw * pin * CACHE_WRITE_MULT + cr * pin * CACHE_READ_MULT + int(output_tokens) * pout) / 1e6


def worst_case_usd(req, route: str = "direct") -> float:
    est = req.est_input_tokens()
    pin, pout = _rates(req.model_id, est)
    return (est * pin * WORST_INPUT_MULT[route] + req.max_tokens * pout) / 1e6


class SpendGuard:
    def __init__(self, cap_usd_run: float, live: bool, route: str = "direct"):
        if not cap_usd_run >= 0:
            raise ValueError("cap_usd_run must be >= 0")
        if route not in ROUTES:
            raise ValueError(f"route {route!r} not in {ROUTES}")
        self.cap, self.live, self.route = float(cap_usd_run), bool(live), route
        self.spent_usd = 0.0
        self.cap_hit = False
        self.over_table = False         # a reported cost above the table cost was seen (the cap is then hit)

    def authorize(self, req) -> None:
        if self.cap_hit:
            raise CapReached("run cap already reached")
        if self.live and not live_allowed(req.model_id, self.route):
            if req.model_id in PRICES:
                raise LiveRefused(req.model_id)
            raise PriceMissing(req.model_id)
        try:
            worst = worst_case_usd(req, self.route)
        except PriceMissing:
            if self.live:
                raise
            return                                  # fake backend: unpriced, recorded as such
        if self.spent_usd + worst > self.cap + 1e-12:
            self.cap_hit = True
            raise CapReached(f"worst case ${worst:.6f} over the remaining ${self.cap - self.spent_usd:.6f}")

    def record(self, req, resp, hit: bool):
        """$ for this call: 0 on a cache hit, None when the model has no price row (fake runs only). Otherwise the larger
        of the table cost and the reported cost (course route), and the worst case when an attempt may have been billed
        with no cost reported."""
        if hit:
            return 0.0
        meta = getattr(resp, "meta", None) or {}
        try:
            table = cost_usd(req.model_id, resp.input_tokens or 0, resp.output_tokens or 0,
                             getattr(resp, "cache_write_tokens", 0) or 0, getattr(resp, "cache_read_tokens", 0) or 0)
        except PriceMissing:
            return None
        usd = table
        reported = meta.get("reported_usd")
        if reported is not None:
            usd = max(usd, float(reported))
            if resp.stop_reason != "error" and float(reported) > table * (1 + OVER_TABLE_REL) + OVER_TABLE_ABS_USD:
                self.over_table = self.cap_hit = True
        if meta.get("cost_unknown"):
            usd = max(usd, worst_case_usd(req, self.route))
        self.spent_usd += usd
        return usd


class PhaseAllocator:
    def __init__(self, cap_usd_phase: float, spent_usd: float = 0.0):
        self.cap, self.spent = float(cap_usd_phase), float(spent_usd)
        self.reserved: dict[str, float] = {}

    def committed_usd(self) -> float:
        return self.spent + sum(self.reserved.values())

    def reserve(self, run: str, cap_usd_run: float) -> None:
        if self.committed_usd() + float(cap_usd_run) > self.cap + 1e-12:
            raise CapReached(f"phase cap ${self.cap:.2f}: committed ${self.committed_usd():.2f} + run cap "
                             f"${cap_usd_run:.2f} does not fit")
        self.reserved[run] = float(cap_usd_run)

    def release(self, run: str, spent_usd: float) -> None:
        self.reserved.pop(run, None)
        self.spent += float(spent_usd)
