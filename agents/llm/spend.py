"""Prices, the fail-closed run cap and the phase allocator (D94).

Per run: before every call, the worst-case cost (estimated input tokens plus max_tokens of output, at the tier the
input falls in) must fit under cap - spent; otherwise no call is made and the run's cap is marked hit. Per phase: the
launcher reserves each run's whole cap against the phase cap before starting it, so the phase cap holds even if every
run spends its cap; spent is reconciled from each run's call log afterwards.

A model with no price row is refused for live calls (fail closed). Fake-backend runs record it as unpriced.
"""
from __future__ import annotations

PRICE_TABLE_VERSION = "prices-2026-11-12-verified"
_ANTHROPIC = "https://platform.claude.com/docs/en/about-claude/pricing"
_OPENAI = "https://developers.openai.com/api/docs/pricing"
# $ per million tokens, standard tier, base input and output (no batch, cache, fast-mode or regional multipliers).
# Each row was read from the provider's own pricing page on the date in `checked` (owner decision 6, Nov 12).
# `long`: the higher prices for a prompt over `long_above` input tokens (per request); None: one price at any length.
# `live`: False refuses live calls to the model (LiveRefused); the fake backend still prices it.
PRICES = {
    "anthropic/claude-haiku-5-5": {"in": 0.10, "out": 0.50, "long": {"in": 0.50, "out": 2.50}, "long_above": 100_000,
                                   "live": True, "source_url": _ANTHROPIC, "checked": "2026-11-12"},
    "anthropic/claude-sonnet-5-5": {"in": 2.00, "out": 10.00, "long": None, "live": True,
                                    "source_url": _ANTHROPIC, "checked": "2026-11-12"},
    "anthropic/claude-opus-5-5": {"in": 4.00, "out": 20.00, "long": None, "live": True,
                                  "source_url": _ANTHROPIC, "checked": "2026-11-12"},
    # Live calls refused until the owner confirms the OpenAI key route (D96; owner, Nov 12).
    "openai/gpt-6-luna": {"in": 0.10, "out": 0.50, "long": {"in": 0.20, "out": 0.75}, "long_above": 272_000,
                          "live": False, "source_url": _OPENAI, "checked": "2026-11-12"},
}


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


def cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = _rates(model_id, int(input_tokens))
    return (int(input_tokens) * pin + int(output_tokens) * pout) / 1e6


def worst_case_usd(req) -> float:
    return cost_usd(req.model_id, req.est_input_tokens(), req.max_tokens)


class SpendGuard:
    def __init__(self, cap_usd_run: float, live: bool):
        if not cap_usd_run >= 0:
            raise ValueError("cap_usd_run must be >= 0")
        self.cap, self.live = float(cap_usd_run), bool(live)
        self.spent_usd = 0.0
        self.cap_hit = False

    def authorize(self, req) -> None:
        if self.cap_hit:
            raise CapReached("run cap already reached")
        if self.live and not (PRICES.get(req.model_id) or {}).get("live", False):
            if req.model_id in PRICES:
                raise LiveRefused(req.model_id)
            raise PriceMissing(req.model_id)
        try:
            worst = worst_case_usd(req)
        except PriceMissing:
            if self.live:
                raise
            return                                  # fake backend: unpriced, recorded as such
        if self.spent_usd + worst > self.cap + 1e-12:
            self.cap_hit = True
            raise CapReached(f"worst case ${worst:.6f} over the remaining ${self.cap - self.spent_usd:.6f}")

    def record(self, req, resp, hit: bool):
        """$ for this call: 0 on a cache hit, None when the model has no price row (fake runs only)."""
        if hit:
            return 0.0
        try:
            usd = cost_usd(req.model_id, resp.input_tokens or 0, resp.output_tokens or 0)
        except PriceMissing:
            return None
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
