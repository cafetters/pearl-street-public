"""Synthetic tests for bench/lit_settle.py (literature mapping, Nov 6). Plain asserts; also runnable without pytest:
PYTHONPATH=. python tests/test_lit_settle.py"""
import numpy as np

from bench import lit_settle as L

NAN = np.nan


def _two_calls(pA, pB, relA, QA, QB, NB=None, Nm=None):
    """Step 0 unpriced, step 1 priced by both calls. Returns rule_steps on step 1 only."""
    NA = QA + relA
    NB = NA if NB is None else NB
    Nm = NB if Nm is None else Nm
    pis = [np.array([NAN, pA]), np.array([NAN, pB])]
    noms = [np.array([0.0, NA]), np.array([0.0, NB])]
    qs = [np.array([0.0, QA]), np.array([0.0, QB])]
    st = L.rule_steps(pis, noms, qs, np.array([0.0, Nm]))
    return {k: float(st[k][1]) for k in ("today", "ri", "frozen", "max", "q_update")}, st


def test_one_call_all_rules_agree():
    pis, noms, qs = [np.array([NAN, 10.0, 20.0])], [np.array([5.0, 150.0, 80.0])], [np.array([5.0, 100.0, 60.0])]
    st = L.rule_steps(pis, noms, qs, np.array([5.0, 150.0, 80.0]))
    for k in ("ri", "frozen", "max"):
        assert np.allclose(st[k], st["today"]), k
    assert np.allclose(st["today"], [0.0, 500.0, 400.0])


def test_price_falls_today_reprices_down_ri_keeps_first_price():
    s, _ = _two_calls(pA=10.0, pB=6.0, relA=100.0, QA=0.0, QB=0.0)
    assert s["today"] == 600.0          # A's relief re-priced at p_B
    assert s["ri"] == 1000.0            # incremental: A's relief stays at p_A
    assert s["frozen"] == 1000.0
    assert s["max"] == 1000.0           # max(p_A, p_B), relief capped by metered relief (100)


def test_price_rises_ri_keeps_lower_first_price_max_pays_later():
    s, _ = _two_calls(pA=6.0, pB=10.0, relA=100.0, QA=0.0, QB=0.0)
    assert s["today"] == 1000.0
    assert s["ri"] == 600.0             # forward price risk: sold at p_A, price rose
    assert s["max"] == 1000.0


def test_reference_update_separates_ri_from_frozen():
    # Q moves from 0 to -50 between calls (the reference update); the nomination doesn't move.
    s, _ = _two_calls(pA=10.0, pB=6.0, relA=100.0, QA=0.0, QB=-50.0)
    assert s["today"] == 6.0 * 150.0
    assert s["ri"] == 10.0 * 100.0 + 6.0 * 50.0
    assert s["frozen"] == 10.0 * 100.0
    assert s["frozen"] - s["ri"] == s["q_update"] == 6.0 * -50.0


def test_metered_deviation_settles_at_last_price_in_ri_and_frozen():
    s, _ = _two_calls(pA=10.0, pB=6.0, relA=100.0, QA=0.0, QB=0.0, Nm=80.0)
    assert s["ri"] == 10.0 * 100.0 + 6.0 * (80.0 - 100.0)
    assert s["frozen"] == s["ri"]
    assert s["today"] == 6.0 * 80.0


def test_priced_step_without_nomination_fails_closed():
    try:
        L.rule_steps([np.array([10.0])], [np.array([NAN])], [np.array([0.0])], np.array([0.0]))
    except L.CheckFailed:
        return
    raise AssertionError("expected CheckFailed")


def test_curve_factors_at_the_quoted_points():
    f = L.curve_factor
    assert f(1.2, "sthlmflex") == 1.0 and f(0.8, "sthlmflex") == 1.0
    assert abs(f(0.6, "sthlmflex") - 0.5) < 1e-12 and f(0.4, "sthlmflex") == 0.0 and f(0.1, "sthlmflex") == 0.0
    assert f(0.95, "intraflex") == 1.0
    assert abs(f(0.80, "intraflex") - 0.55) < 1e-12
    assert abs(f(0.63, "intraflex") - 0.04) < 1e-12
    assert f(0.62, "intraflex") == 0.0


def test_curve_pay_full_delivery_pays_nominated_value_and_no_relief_pays_today():
    _, st = _two_calls(pA=10.0, pB=6.0, relA=100.0, QA=0.0, QB=0.0)
    pay, d = L.curve_pay(st, np.array([0.0, 100.0]), "sthlmflex")
    assert abs(d - 1.0) < 1e-12 and abs(pay - 6.0 * 100.0 * L.D.K) < 1e-12
    pay, d = L.curve_pay(st, np.array([0.0, 50.0]), "intraflex")     # delta 0.5: below 63%, nothing
    assert abs(d - 0.5) < 1e-12 and pay == 0.0
    _, st0 = _two_calls(pA=10.0, pB=6.0, relA=-20.0, QA=0.0, QB=0.0)  # nominated relief negative
    pay, d = L.curve_pay(st0, np.array([0.0, -20.0]), "sthlmflex")
    assert d is None and abs(pay - 6.0 * -20.0 * L.D.K) < 1e-12


def test_score_counts_settlement_harm_beyond_tolerance_only():
    e = np.array([100.0, 100.0])
    alone = np.array([-1000.0, -1000.0])         # tolerance max(1% x 1000, 10) = 10
    gap = np.array([0.0, 50.0])
    pay = np.array([85.0, 30.0])                 # shortfall 15 (> 10) and 70 (70 - 50 = 20 > 10)
    s = L.score(pay, e, alone, gap, ["A", "B"])
    assert s["settlement_harmed"] == 2 and abs(s["settlement_harm_usd"] - 35.0) < 1e-9
    s = L.score(np.array([95.0, 45.0]), e, alone, gap, ["A", "B"])
    assert s["settlement_harmed"] == 0


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
