"""exchange/proven.py, the pure rules of proven capacity (owner, Nov 19; plan step 2 with the owner's changes 1-2).
Every number here is a hand-built input [synthetic]."""
import pytest

from exchange import proven as P

CFG = P.Config(step_kw=50.0, grow=0.5, cut=0.5, clean_tol=0.05, n0=3, new_prior=(1.0, 1.0),
               existing_prior=(19.0, 1.0), starter_fallback_kw=100.0, budget_kw=1000.0, reserve_kw=5000.0)


def test_existing_assets_start_proven_at_demonstrated_power_with_a_strong_high_prior():
    s = P.initial(has_history=True, demonstrated_kw=2000.0, claim_kw=2000.0, nameplate_kw=None, type_median_kw=None,
                  cfg=CFG)
    assert s.proven_kw == 2000.0 and not s.new and s.trusted and P.rate(s) == 1.0      # rate 1 until a miss


def test_a_new_asset_starts_at_the_starter_cap_in_kw_never_a_fraction_of_its_claim():
    s = P.initial(False, 0.0, 5000.0, nameplate_kw=None, type_median_kw=250.0, cfg=CFG)
    assert s.new and s.proven_kw == 250.0 and P.rate(s) == pytest.approx(0.5)
    assert P.starter_kw(5000.0, 400.0, 250.0, 100.0) == 250.0             # min(claim, nameplate, type median)
    assert P.starter_kw(80.0, 400.0, 250.0, 100.0) == 80.0
    assert P.starter_kw(5000.0, 120.0, 250.0, 100.0) == 120.0
    assert P.starter_kw(5000.0, None, None, 100.0) == 100.0                 # type with no records: fixed fallback


def test_type_median_reads_only_assets_with_history_of_that_type():
    hist = {"a": ("bess", 100.0), "b": ("bess", 300.0), "c": ("bess", 200.0), "d": ("ev", 7.0)}
    assert P.type_median_kw(hist, "bess") == 200.0 and P.type_median_kw(hist, "ev") == 7.0
    assert P.type_median_kw(hist, "dc") is None


def test_counted_relief_is_rate_times_relief_capped_at_proven_plus_step_and_added_load_counts_in_full():
    s = P.State(proven_kw=200.0, alpha=3.0, beta=1.0, new=False, clean_calls=5)
    mx, mf = P.ceilings(ref_kw=0.0, idle_kw=0.0, s=s, cfg=CFG)
    assert (mx, mf) == (250.0, 200.0)
    assert P.counted(1000.0, mx, s) == pytest.approx(0.75 * 250.0)
    assert P.counted(100.0, mx, s) == pytest.approx(75.0)
    assert P.counted(-40.0, mx, s) == -40.0
    assert P.firm(1000.0, mf, s) == pytest.approx(0.75 * 200.0)
    # the cap is on the battery's own contribution: a reference charging at -200 kW leaves 450 kW of relief room
    assert P.ceilings(ref_kw=-200.0, idle_kw=0.0, s=s, cfg=CFG) == (450.0, 400.0)


def test_unproven_capacity_is_never_firm():
    new = P.State(proven_kw=200.0, alpha=1.0, beta=1.0, new=True, clean_calls=2)
    assert P.firm(150.0, 200.0, new) == 0.0 and P.counted(150.0, 250.0, new) == pytest.approx(75.0)


def test_slow_up_fast_down_and_probation_ends_after_n0_clean_calls():
    s = P.State(proven_kw=200.0, alpha=1.0, beta=1.0, new=True, clean_calls=0)
    for i in range(3):
        s = P.update(s, nominated_kwh=50.0, metered_kwh=49.0, metered_peak_kw=1000.0, cfg=CFG)  # clean (within 5%)
        assert s.proven_kw == pytest.approx(200.0 + 25.0 * (i + 1))
    assert not s.new and s.clean_calls == 3 and P.rate(s) > 0.5
    r0 = P.rate(s)
    s = P.update(s, nominated_kwh=50.0, metered_kwh=10.0, metered_peak_kw=60.0, cfg=CFG)          # a miss
    # owner, Nov 20: a miss never cuts what was seen delivered (1,000 kW metered on the clean calls); the rate falls
    assert s.proven_kw == pytest.approx(275.0) and P.rate(s) < r0


def test_growth_never_passes_the_metered_peak():
    s = P.State(proven_kw=1000.0, alpha=99.0, beta=1.0, new=False, clean_calls=0)
    assert P.update(s, 50.0, 50.0, metered_peak_kw=1000.0, cfg=CFG).proven_kw == 1000.0     # nothing shown beyond
    assert P.update(s, 50.0, 50.0, metered_peak_kw=1010.0, cfg=CFG).proven_kw == 1010.0
    assert P.update(s, 50.0, 50.0, metered_peak_kw=600.0, cfg=CFG).proven_kw == 1000.0      # clean: never cut


def test_portfolio_scale_holds_expected_shortfall_from_unproven_capacity_within_the_budget():
    assert P.portfolio_scale({"a": 400.0, "b": 400.0}, {"a": 0.5, "b": 0.5}, budget_kw=1000.0) == 1.0
    f = P.portfolio_scale({"a": 4000.0, "b": 4000.0}, {"a": 0.5, "b": 0.5}, budget_kw=1000.0)
    assert f == pytest.approx(0.25) and 0.25 * 8000.0 * 0.5 == pytest.approx(1000.0)


def test_contingency_caps_per_asset_and_per_legal_entity():
    out = P.contingency_scale({"a": 6000.0, "b": 2000.0, "c": 2000.0}, {"a": "E1", "b": "E2", "c": "E2"},
                              reserve_kw=3000.0)
    assert out["a"] == pytest.approx(0.5)                                  # asset cap
    assert out["b"] == pytest.approx(0.75) and out["c"] == pytest.approx(0.75)   # entity cap, pro rata
    assert sum(v * f for v, f in zip((2000.0, 2000.0), (out["b"], out["c"]))) == pytest.approx(3000.0)


def test_assets_with_history_count_at_rate_1_until_their_first_miss():
    """Owner, Nov 20 (option a): a trusted asset's rate is 1 until its first miss; then the strong prior's posterior,
    which includes every call so far."""
    s = P.initial(True, 1000.0, 1000.0, None, None, CFG)
    assert s.trusted and P.rate(s) == 1.0
    s = P.update(s, 50.0, 48.0, 1000.0, CFG)                 # clean (96%): still trusted
    assert s.trusted and P.rate(s) == 1.0
    s = P.update(s, 50.0, 10.0, 200.0, CFG)                  # a miss
    assert not s.trusted and P.rate(s) == pytest.approx((19.0 + 0.96 + 0.2) / 22.0)
    n = P.initial(False, 0.0, 500.0, None, 250.0, CFG)
    assert not n.trusted                                     # newcomers never start trusted


def test_a_miss_lowers_the_rate_but_never_cuts_what_was_seen_delivered():
    """Owner, Nov 20: proven capacity is the most an asset has been seen to deliver; a miss lowers its rate (the
    reputation), not that. Only a newcomer's untested starter amount is dropped on a miss, down to what it has shown."""
    ex = P.initial(True, 1000.0, 1000.0, None, None, CFG)                  # meter history: 1,000 kW seen
    ex = P.update(ex, 50.0, 30.0, metered_peak_kw=600.0, cfg=CFG)          # delivered 6 of 10 this time
    assert ex.proven_kw == 1000.0 and not ex.trusted and P.rate(ex) < 1.0
    nc = P.initial(False, 0.0, 5000.0, None, 1000.0, CFG)                  # starter 1,000 kW, never seen
    assert nc.proven_kw == 1000.0 and nc.seen_kw == 0.0
    nc = P.update(nc, 50.0, 10.0, metered_peak_kw=300.0, cfg=CFG)          # a miss: only 300 kW shown
    assert nc.proven_kw == 300.0 and nc.seen_kw == 300.0
    nc = P.update(nc, 50.0, 50.0, metered_peak_kw=900.0, cfg=CFG)          # clean: grows by grow x step = 25
    assert nc.proven_kw == 325.0 and nc.seen_kw == 900.0
    nc = P.update(nc, 50.0, 5.0, metered_peak_kw=100.0, cfg=CFG)           # a miss: 325 was all seen (900): kept
    assert nc.proven_kw == 325.0


def test_an_undiscounted_nomination_is_counted_bit_for_bit():
    """Nov 20 (80-agent check): ref + (v - ref) is not v in floating point, and the price loop amplifies the difference.
    counted_net returns exactly v when nothing is discounted."""
    v, ref = 0.1 + 0.2, -0.7000000000000001
    assert ref + (v - ref) != v                                       # the trap
    assert P.counted_net(v, ref, v - ref) == v                        # nothing discounted: exactly v
    assert P.counted_net(10.0, 0.0, 7.5) == 7.5                       # discounted: ref + counted relief
