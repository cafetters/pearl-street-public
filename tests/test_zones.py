"""M2c step 3: exchange/zones.py, the arm C zone product (D99, Leaning): per-element prices lambda_k, sign-aware
per-asset components, per-zone caps (D68), joint clearing of overlapping elements, protection, undersized case.
Judged on unrounded values (D98). The PTDF rows come from the synthetic grid (sim/synth_grid.py)."""
import pytest

from exchange import zones as ZN
from sim import synth_grid as S

STEP_H = 0.25
K = STEP_H / 1000.0
STEPS = ["t0", "t1"]


def book(cap=200.0, base=None, alpha=1e-5):
    rows = S.zone_rows()
    rating = S.branch_table().set_index("key").rating_mva
    base = base or {k: {t: (1.1 * 0.9 * rating[k] if k != "l14" else 0.5) for t in STEPS} for k in S.MONITORED}
    els = [ZN.Element(key=k, rows=rows[k], limit_mw=0.9 * float(rating[k]), base_mw=base[k], cap_usd_mwh=cap)
           for k in S.MONITORED]
    return ZN.ZoneBook(els, step_h=STEP_H, alpha=alpha)


def idle():
    return {a: {t: 0.0 for t in STEPS} for a in S.ASSET_BUS}


def test_rows_below_ptdf_min_are_dropped_so_far_assets_get_no_component():
    b = book()
    assert b.elements["l14"].rows == {"B4": pytest.approx(-1.0)}       # B1-B3 have |PTDF| < 0.05 on l14
    assert set(b.elements["tA"].rows) == set(S.ASSET_BUS)


def test_lambda_rises_only_on_overloaded_elements_and_never_passes_its_cap_or_zero():
    b = book(cap=50.0, alpha=1.0)
    for _ in range(200):
        b.update(STEPS, idle())
    for k in ("tA", "tB"):                                                # base flow is 110% of the limit
        assert all(b.lam[k][t] == pytest.approx(50.0 * K) for t in STEPS)
    assert all(b.lam["l14"][t] == 0.0 for t in STEPS)                    # l14 is under its limit
    b2 = book(cap=50.0, alpha=1.0, base={k: {t: 0.0 for t in STEPS} for k in S.MONITORED})
    b2.update(STEPS, idle())
    assert all(v == 0.0 for k in b2.lam for v in b2.lam[k].values())


def test_component_is_sign_aware_and_overlapping_zones_add():
    b = book(cap=50.0, alpha=1.0)
    for _ in range(200):
        b.update(STEPS, idle())
    # Base flows are positive (loads pull power hv -> lv). One more kW of an asset's load moves the element's flow by
    # -PTDF/1000 MW, so its |flow| by -direction x PTDF: the component on load is lambda x (-direction x PTDF).
    for a in S.ASSET_BUS:
        c = b.component(a, "t0")
        expect = sum(b.lam[k]["t0"] * (-b.direction(k, "t0") * e.rows.get(a, 0.0)) for k, e in b.elements.items())
        assert c == pytest.approx(expect)
        assert c >= 0.0                                   # every asset here relieves tA/tB by discharging
    c3 = b.component("B3", "t0")
    only_a = b.lam["tA"]["t0"] * abs(b.elements["tA"].rows["B3"])
    only_b = b.lam["tB"]["t0"] * abs(b.elements["tB"].rows["B3"])
    assert abs(c3) == pytest.approx(only_a + only_b)                      # B3 sits in both transformer zones


def test_relief_direction_is_paid_and_worsening_is_charged():
    b = book(cap=50.0, alpha=1.0)
    for _ in range(200):
        b.update(STEPS, idle())
    a = "B1"
    ref = {t: 1000.0 for t in STEPS}                     # reference load, kW
    less = {t: 500.0 for t in STEPS}                     # relieving: less load than the reference
    more = {t: 1500.0 for t in STEPS}
    assert b.settle_component(a, ref, less) > 0          # paid (net to the agent, $)
    assert b.settle_component(a, ref, more) < 0          # charged


def test_protection_schedule_equal_to_reference_settles_zero_and_far_assets_settle_zero():
    b = book(cap=50.0, alpha=1.0)
    for _ in range(200):
        b.update(STEPS, idle())
    ref = {t: 1234.5678 for t in STEPS}
    for a in S.ASSET_BUS:
        assert b.settle_component(a, ref, dict(ref)) == 0.0              # exactly zero, unrounded
    assert b.settle_component("NOT_AN_ASSET", ref, {t: 0.0 for t in STEPS}) == 0.0
    assert b.component("NOT_AN_ASSET", "t0") == 0.0


def test_no_zone_payment_exceeds_the_cap():
    cap = 30.0
    b = book(cap=cap, alpha=1.0)
    for _ in range(500):
        b.update(STEPS, idle())
    for a in S.ASSET_BUS:
        ref = {t: 2000.0 for t in STEPS}
        met = {t: 0.0 for t in STEPS}
        paid = b.settle_component(a, ref, met)
        bound = sum(cap * sum(abs(r.get(a, 0.0)) for r in (e.rows for e in b.elements.values()))
                    * abs(ref[t] - met[t]) / 1000.0 * STEP_H for t in STEPS)
        assert abs(paid) <= bound + 1e-9


def test_undersized_case_reports_the_remaining_violation_per_element_and_step():
    rating = S.branch_table().set_index("key").rating_mva
    base = {k: {t: 0.0 for t in STEPS} for k in S.MONITORED}
    base["l14"] = {"t0": 5.0, "t1": 1.0}                 # 5 MW pulled through l14 at t0: B4's 1.5 MW cannot fix it
    b = book(cap=40.0, alpha=1.0, base=base)
    full = idle()
    full["B4"] = {"t0": 1500.0, "t1": 0.0}               # B4 discharges fully at t0 (injection, kW)
    for _ in range(300):
        b.update(STEPS, full)
    assert b.lam["l14"]["t0"] == pytest.approx(40.0 * K)                 # reaches its cap
    v = b.violations(STEPS, full)
    limit = 0.9 * float(rating["l14"])
    assert v["l14"]["t0"] == pytest.approx(5.0 - 1.5 - limit)           # unrounded remaining MW over the limit
    assert v["l14"]["t1"] == 0.0 and all(x == 0.0 for k in ("tA", "tB") for x in v[k].values())


def test_max_excess_feeds_convergence():
    b = book(cap=50.0, alpha=1.0)
    ex = b.max_excess_kw(STEPS, idle())
    rating = S.branch_table().set_index("key").rating_mva
    assert ex == pytest.approx(max(0.1 * 0.9 * float(rating[k]) for k in ("tA", "tB")) * 1000.0)
    b0 = book(base={k: {t: 0.0 for t in STEPS} for k in S.MONITORED})
    assert b0.max_excess_kw(STEPS, idle()) == 0.0


def test_adaptive_step_doubles_while_overloaded_and_halves_after_overshoot():
    b = book(cap=1e6, alpha=1e-9)
    b.adapt_tau = 2.0
    a0 = b.alpha
    for _ in range(3):
        b.update(STEPS, idle())                           # base 10% over on tA/tB: still overloaded every round
    assert b.alpha_k["tA"] == pytest.approx(a0 * 2 ** 2)  # doubled after each repeat of a persistent overload
    relieve = {a: {t: 3000.0 for t in STEPS} for a in S.ASSET_BUS}        # 3 MW each: tA, tB well under the limit
    b.update(STEPS, relieve)
    assert b.alpha_k["tA"] == pytest.approx(a0 * 2 ** 1)  # overshoot with lambda > 0: halved
    assert all(v == pytest.approx(a0) for k, v in b.alpha_k.items() if k == "l14")   # never overloaded: unchanged


def test_fixed_step_without_adapt_tau():
    b = book(cap=1e6, alpha=1e-9)
    for _ in range(3):
        b.update(STEPS, idle())
    assert all(v == 1e-9 for v in b.alpha_k.values())


def test_admm_centre_shift_moves_each_overloaded_element_exactly_to_its_limit():
    """alpha_k = rho / ||row_k||^2 and the member shifts (-dir x row_i) x dlam / (alpha_k ||row_k||^2): if every member
    moves its injection by its shift, the element's |flow| falls by exactly its excess (unprojected steps)."""
    b = book(cap=1e9, alpha=0.0)
    rho = 1e-7
    b.set_rho(rho)
    for k in b.elements:
        assert b.alpha_k[k] == pytest.approx(rho / b.norm2(k))
    inj0 = idle()
    b.update(STEPS, inj0)
    for k in ("tA", "tB"):
        for t in STEPS:
            e = b.excess_mw(k, t, inj0)
            moved = {a: {t: b.member_shift_kw(a, k, t)} for a in b.elements[k].rows}
            after = abs(b.flow_mw(k, t, moved))
            assert after == pytest.approx(abs(b.flow_mw(k, t, inj0)) - e)                     # exactly at the limit
            assert after == pytest.approx(b.elements[k].limit_mw)
