"""D58 risk-margin test: the agents' peak-response margin m (agents/planner.py anchor, agents/battery.py, sim/run_m1.py).

A plan made against a peak price pays (m/2) per MWh of distance from the agent's own selfish plan, so a shift (both
legs) pays about m per MWh moved. m = 0 must leave everything exactly as today; a non-participant never uses it."""
import json

import numpy as np
import pytest

from agents.battery import BatteryAgent
from agents.planner import Battery, solve

DT = 0.25
B = Battery(P=100.0, CAP=400.0, ETA=0.95, SD=0.0, POI=1000.0, s_term=200.0)
L = 16
ISO = [f"2023-01-02T{6 + k // 4:02d}:{15 * (k % 4):02d}:00Z" for k in range(L)]
END = "2023-01-02T10:00:00Z"


def flat(v=0.0, n=L):
    return np.full(n, v)


def evening_price():
    p = flat(20.0)
    p[12:14] = 80.0
    return p


def morning_peak(v=200.0):
    pk = flat()
    pk[4:6] = v
    return pk


def agent(margin=0.0, participate=True):
    ag = BatteryAgent("A", B, flat(), flat(), flat(), ISO, END, DT, "run", "SYS", participate=participate,
                      min_offer_kw=10, price_mode=True, plan_to_nomination=True,
                      peak_response_margin_usd_mwh=margin)
    ag.peak = morning_peak()
    return ag


def net(p, base=None):
    return (flat() if base is None else base)[p.k0:] + p.d - p.c - p.x


# ---- planner: m = 0 is today's LP ----
def test_solve_without_anchor_or_with_m0_is_unchanged():
    args = (B, 0, 200.0, evening_price(), flat(), flat(), flat(), DT, 10.0)
    centre = np.where(np.arange(L) < 8, 0.0, np.nan)
    for kw in ({}, {"peak": morning_peak()}, {"peak": morning_peak(), "prox": (1e-3, centre, 1e-5)}):
        base = solve(*args, **kw)
        for anchor in (None, (flat(), 0.0), (np.full(L, np.nan), 3.0)):
            p = solve(*args, **kw, anchor=anchor)
            for f in ("c", "d", "s", "x", "sh"):
                assert np.array_equal(getattr(p, f), getattr(base, f))
            assert p.cost == base.cost


# ---- planner: the threshold ----
@pytest.mark.parametrize("m", [1.0, 3.0, 10.0])
def test_shift_threshold_is_lost_arbitrage_plus_half_m_on_each_leg(m):
    """Toy: two steps at day-ahead price 50 $/MWh, wear w, efficiency eta, terminal SOC = start (so a discharge at step
    1 must be recharged at step 0: d = eta^2 c). A peak price p sits on step 1 only. The selfish anchor is idle.
    Discharging 1 MWh at step 1 earns 50 + p; recharging it costs 50 / eta^2; wear costs w; the margin costs m/2 on
    the discharge leg (1 MWh off the anchor) and m/2 on the charge leg (1/eta^2 MWh off the anchor). So the agent
    shifts iff
        p > (50 / eta^2 - 50 + w) + (m / 2) x (1 + 1 / eta^2)
    i.e. lost arbitrage + m/2 x (1 + 1/eta^2), about lost arbitrage + m. Checked on both sides, and against the LP's
    own threshold found by bisection. That agrees within HiGHS's dual feasibility tolerance (1e-7 $ per kW-step of
    reduced cost = 1e-7 / K = 4e-4 $/MWh at K = 2.5e-4), so the bisection is held to 1e-3 $/MWh."""
    eta, w, da = 0.9, 10.0, 50.0
    b = Battery(P=100.0, CAP=1000.0, ETA=eta, SD=0.0, POI=1e9, s_term=500.0)
    lost = da / eta ** 2 - da + w
    p_star = lost + m / 2 * (1 + 1 / eta ** 2)

    def discharged(p):
        pl = solve(b, 0, 500.0, flat(da, 2), flat(0, 2), flat(0, 2), flat(0, 2), DT, w,
                   peak=np.array([0.0, p]), anchor=(flat(0, 2), m))
        return pl.d[1]

    assert discharged(p_star + 1e-3) > 1.0
    assert discharged(p_star - 1e-3) < 1e-6
    lo, hi = p_star - 5.0, p_star + 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if discharged(mid) > 1e-6 else (mid, hi)
    assert hi == pytest.approx(p_star, abs=1e-3)
    # without the margin the threshold is the lost arbitrage alone
    pl = solve(b, 0, 500.0, flat(da, 2), flat(0, 2), flat(0, 2), flat(0, 2), DT, w, peak=np.array([0.0, lost + 1e-3]))
    assert pl.d[1] > 1.0


# ---- agent ----
def test_a_non_participant_ignores_the_margin():
    a3, a0 = agent(3.0, participate=False), agent(0.0, participate=False)
    p3, p0 = a3._solve(0, 200.0, evening_price(), a3.commit), a0._solve(0, 200.0, evening_price(), a0.commit)
    assert np.array_equal(net(p3), net(p0))
    assert not a3._anchors and not a3.margin_log


def test_m0_agent_plans_exactly_as_before_and_logs_nothing():
    a0 = agent(0.0)
    p = a0._solve(0, 200.0, evening_price(), a0.commit)
    ref = solve(B, 0, 200.0, evening_price(), flat(), flat(), flat(), DT, 10.0, peak=morning_peak())
    assert np.array_equal(net(p), net(ref))
    assert not a0._anchors and not a0.margin_log and a0.posture == "normal"


def test_cached_anchor_equals_an_uncached_selfish_solve():
    a3 = agent(3.0)
    a3._solve(0, 200.0, evening_price(), a3.commit)
    a3._solve(0, 200.0, evening_price(), a3.commit)
    assert len(a3._anchors) == 1
    (cached,) = a3._anchors.values()
    fresh = solve(B, 0, 200.0, evening_price(), flat(), flat(), flat(), DT, 10.0)
    assert np.array_equal(cached, net(fresh))
    a3._solve(0, 190.0, evening_price(), a3.commit)                 # a different SOC is a different anchor
    assert len(a3._anchors) == 2


def test_margin_damps_the_response_and_logs_when_it_binds():
    a0, a3 = agent(0.0), agent(3.0)
    p0 = a0._solve(0, 200.0, evening_price(), a0.commit)
    p3 = a3._solve(0, 200.0, evening_price(), a3.commit)
    anchor = net(solve(B, 0, 200.0, evening_price(), flat(), flat(), flat(), DT, 10.0))
    shift0, shift3 = np.abs(net(p0) - anchor).sum(), np.abs(net(p3) - anchor).sum()
    assert shift0 > 0 and shift3 <= shift0 + 1e-6
    (e,) = a3.margin_log
    assert e["posture"] == "cautious" and e["m_usd_mwh"] == 3.0 and e["reason"]
    assert e["shifted_mwh"] == pytest.approx(shift3 * DT / 1000)
    assert e["shifted_m0_mwh"] == pytest.approx(shift0 * DT / 1000)
    assert e["declined_mwh"] == pytest.approx(e["shifted_m0_mwh"] - e["shifted_mwh"])
    assert not a0.margin_log


def test_a_margin_that_stops_the_shift_entirely_is_still_logged():
    big = agent(1e4)
    p = big._solve(0, 200.0, evening_price(), big.commit)
    anchor = net(solve(B, 0, 200.0, evening_price(), flat(), flat(), flat(), DT, 10.0))
    assert np.abs(net(p) - anchor).max() < 1e-6                     # no shift, so the margin term costs nothing
    (e,) = big.margin_log
    assert e["shifted_mwh"] == pytest.approx(0.0, abs=1e-9) and e["declined_mwh"] > 0


def test_no_peak_price_means_no_anchor_and_no_log():
    a3 = agent(3.0)
    a3.peak = flat()
    a3._solve(0, 200.0, evening_price(), a3.commit)
    assert not a3._anchors and not a3.margin_log


# ---- the run ----
def _cfg(**kw):
    from tests.test_replay import CFG_M11
    return {**CFG_M11, "rounds": "info", "price_mode": True, "plan_to_nomination": True,
            "release_superseded_lock": True, "price_rounds_per_gate": 10, "settlement_reference": "selfish_1p", **kw}


def test_m0_run_is_identical_to_a_run_without_the_key(tmp_path):
    from sim.run_m1 import run_variant
    from tests.test_replay import synthetic_slice
    s = synthetic_slice()
    r1 = run_variant(_cfg(), s, np.arange(2), tmp_path / "a", headroom_mw=0.03)
    r2 = run_variant(_cfg(peak_response_margin_usd_mwh=0.0), s, np.arange(2), tmp_path / "b", headroom_mw=0.03)
    assert r1["log"] == r2["log"]
    # bus.log keeps nominate and telemetry as envelope only (fix 1), so compare the traces too
    body = lambda d: (d / "exchange_h0.03_rinfo" / "trace.jsonl").read_text().splitlines()[1:]    # noqa: E731
    assert body(tmp_path / "a") == body(tmp_path / "b")
    for f in ("C", "D", "X", "SOC"):
        assert np.array_equal(r1[f], r2[f])
    assert not r2["margin_log"] and not (tmp_path / "b" / "exchange_h0.03_rinfo" / "agent_log.jsonl").exists()


def test_run_records_the_margin_round_wall_clock_and_the_agent_log(tmp_path):
    from exchange.trace.replay import replay
    from sim.run_m1 import evaluate, run_variant
    from tests.test_replay import synthetic_slice
    s = synthetic_slice()
    cfg = _cfg(peak_response_margin_usd_mwh=3.0)
    sf = run_variant(cfg, s, np.arange(2), tmp_path)
    r = run_variant(cfg, s, np.arange(2), tmp_path, headroom_mw=0.03)
    path = tmp_path / "exchange_h0.03_rinfo" / "trace.jsonl"
    hdr = json.loads(path.read_text().splitlines()[0])
    assert hdr["peak_response_margin_usd_mwh"] == 3.0
    assert replay(path)["match"]
    assert not sf["margin_log"]                                     # the selfish run never uses the margin
    ev = evaluate(cfg, s, np.arange(2), r, s.system["system_load"] * 1000.0, None, sf)
    pm = ev["price_mode"]
    assert len(pm["round_wall_utc"]) == pm["rounds_total"] > 0
    assert pm["margin"]["peak_response_margin_usd_mwh"] == 3.0
    assert pm["margin"]["log_entries"] == len(r["margin_log"])
    log_path = tmp_path / "exchange_h0.03_rinfo" / "agent_log.jsonl"
    assert r["margin_log"]                                          # m = 3 binds in this synthetic run
    rows = [json.loads(x) for x in log_path.read_text().splitlines()]
    assert len(rows) == len(r["margin_log"]) and {x["agent"] for x in rows} <= {"A", "B"}
    assert all(x["declined_mwh"] >= -1e-9 and x["posture"] == "cautious" for x in rows)
    finals = [x for x in rows if x["final_round"]]
    assert finals and all(x["in_price_round"] for x in finals)
    by_gate = pm["margin"]["final_round_by_gate"]
    assert sum(g["agents_bound"] for g in by_gate.values()) == len(finals)
    assert sum(g["declined_mwh"] for g in by_gate.values()) == pytest.approx(sum(x["declined_mwh"] for x in finals))
    assert ev["wall_utc"]["start"] <= ev["wall_utc"]["end"]


def test_negative_margin_is_rejected(tmp_path):
    from sim.run_m1 import run_variant
    from tests.test_replay import synthetic_slice
    with pytest.raises(ValueError, match="peak_response_margin"):
        run_variant(_cfg(peak_response_margin_usd_mwh=-1.0), synthetic_slice(), np.arange(2), tmp_path,
                    headroom_mw=0.03)
