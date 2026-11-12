"""Price table (owner decision 6, Nov 12): each row carries the provider's pricing-page URL and the date it was checked;
gpt-6-luna is priced but refused for live calls until the owner confirms the OpenAI key route (D96)."""
import pytest

from agents.llm import spend as SP


class _Req:
    def __init__(self, model_id, tin, max_tokens=0):
        self.model_id, self._tin, self.max_tokens = model_id, tin, max_tokens

    def est_input_tokens(self):
        return self._tin


def test_every_row_names_its_pricing_page_and_check_date():
    for mid, row in SP.PRICES.items():
        assert row["source_url"].startswith("https://"), mid
        assert row["checked"] == "2026-11-12", mid
        assert not row.get("verify_before_pilot"), mid


@pytest.mark.parametrize("mid,pin,pout", [("anthropic/claude-haiku-5-5", 0.10, 0.50),
                                          ("anthropic/claude-sonnet-5-5", 2.00, 10.00),
                                          ("anthropic/claude-opus-5-5", 4.00, 20.00),
                                          ("openai/gpt-6-luna", 0.10, 0.50)])
def test_base_prices_match_the_pages(mid, pin, pout):
    assert SP.cost_usd(mid, 10_000, 0) == pytest.approx(10_000 * pin / 1e6)      # below every long-context tier
    assert SP.cost_usd(mid, 0, 1_000_000) == pytest.approx(pout)


def test_long_context_tiers_use_each_rows_own_threshold():
    assert SP.cost_usd("anthropic/claude-haiku-5-5", 100_000, 0) == pytest.approx(100_000 * 0.10 / 1e6)
    assert SP.cost_usd("anthropic/claude-haiku-5-5", 100_001, 0) == pytest.approx(100_001 * 0.50 / 1e6)
    assert SP.cost_usd("openai/gpt-6-luna", 272_000, 0) == pytest.approx(272_000 * 0.10 / 1e6)
    assert SP.cost_usd("openai/gpt-6-luna", 272_001, 1000) == pytest.approx((272_001 * 0.20 + 1000 * 0.75) / 1e6)
    assert SP.cost_usd("anthropic/claude-sonnet-5-5", 900_000, 0) == pytest.approx(900_000 * 2.00 / 1e6)


def test_luna_is_refused_live_but_runs_on_the_fake_backend():
    with pytest.raises(SP.LiveRefused):
        SP.SpendGuard(5.0, live=True).authorize(_Req("openai/gpt-6-luna", 1000, 512))
    assert issubclass(SP.LiveRefused, SP.PriceMissing)       # every existing fail-closed path still catches it
    SP.SpendGuard(5.0, live=False).authorize(_Req("openai/gpt-6-luna", 1000, 512))
    SP.SpendGuard(5.0, live=True).authorize(_Req("anthropic/claude-haiku-5-5", 1000, 512))
