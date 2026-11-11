"""D56 offline re-settle (bench.d56_resettle): settlement rules, accounting, top-ups, rule 1p's reference, and the
pre-registered selection (E1 margin gate, E3 per-cell net payment, harm before payout). Synthetic data only."""
from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from bench import d56_resettle as D
from exchange.core import Exchange

ROSTER = ["A", "B", "C"]


def _cell(feasible=True):
    p = np.array([0.0, 20.0, 100.0, 50.0])
    N = np.array([[-100.0, -200.0, 300.0, 0.0],          # site net, export positive
                  [-1000.0, -1500.0, -1200.0, -900.0],
                  [50.0, 0.0, -400.0, -100.0]])
    R = np.array([[-100.0, -150.0, 200.0, 0.0],
                  [-1000.0, -1400.0, -1300.0, -900.0],
                  [50.0, 50.0, 100.0, -100.0]])
    B = np.array([[0.0, 50.0, 400.0, 0.0], [0.0, 0.0, 200.0, 0.0], [0.0, 0.0, 300.0, 0.0]])
    w = np.array([0.5, 0.3, 0.2])
    return {"N": N, "R": R, "S": R - 10.0, "Q": R + 5.0, "B": B, "w": w, "feasible": feasible}, p


def test_money_unit_one_mw_one_step_at_100():
    assert D.settle_ref([100.0], [[1000.0]], [[0.0]])[0] == pytest.approx(25.0)


def test_rule_1_matches_exchange_peak_settle():
    c, p = _cell()
    ts = ["2023-01-02T00:00:00Z", "2023-01-02T00:15:00Z", "2023-01-02T00:30:00Z", "2023-01-02T00:45:00Z"]
    ex = SimpleNamespace(
        peak_settled={t: (float(v), "call") for t, v in zip(ts, p)}, step_h=0.25, step=timedelta(minutes=15),
        agents={a: {"assets": [f"{a}/bess"]} for a in ROSTER},
        net_meter={f"{a}/bess": dict(zip(ts, c["N"][j])) for j, a in enumerate(ROSTER)},
        reference={f"{a}/bess": dict(zip(ts, c["R"][j])) for j, a in enumerate(ROSTER)},
        settlement_reference="recorded",
        _msg=lambda typ, to, now, payload: {"to": to, **payload}, _holds=lambda aid, t: True)
    ex._metered = lambda aid: Exchange._metered(ex, aid)
    msgs = Exchange._peak_settle(ex, ts[-1])
    got = {m["to"]: m["net_usd"] for m in msgs}
    want = D.rule_net("1", c, p)
    for j, a in enumerate(ROSTER):
        assert got[a] == pytest.approx(want[j], abs=1e-9)


@pytest.mark.parametrize("rule", D.BASE_RULES)
def test_accounting_every_rule(rule):
    c, p = _cell(feasible=False)
    D.check_accounting(rule, D.rule_net(rule, c, p), 0.0, D.operator_payout(rule, c, p))


def test_accounting_check_fails_closed():
    with pytest.raises(D.CheckFailed):
        D.check_accounting("1", np.array([1.0, 2.0]), 0.0, 3.5)


@pytest.mark.parametrize("rule", ["3e", "3c", "3d"])
def test_budget_balanced_rules_sum_to_zero(rule):
    c, p = _cell()
    assert D.rule_net(rule, c, p).sum() == pytest.approx(0.0, abs=1e-9)
    assert D.operator_payout(rule, c, p) == pytest.approx(0.0, abs=1e-9)


def test_floor_never_negative_and_zeroes_where_it_binds():
    c, p = _cell()
    one = D.rule_net("1", c, p)
    five = D.rule_net("5", c, p)
    assert (five >= 0).all() and (one < 0).any()
    assert list(D.zeroed_agents("5", c, p)) == list(one < 0)


def test_rule_2_settles_nothing_on_an_infeasible_target_and_zeroes_every_margin():
    c, p = _cell(feasible=False)
    assert not D.rule_net("2", c, p).any()
    assert D.zeroed_agents("2", c, p).all()
    c2, _ = _cell(feasible=True)
    assert D.rule_net("2", c2, p) == pytest.approx(D.rule_net("1", c2, p))
    assert not D.zeroed_agents("2", c2, p).any()


def test_full_top_up_has_a_notch_continuous_does_not():
    tol = np.array([10.0, 10.0])
    short = np.array([10.0 - 1e-6, 10.0 + 1e-6])
    full = short - D.top_up(short, tol, "f")
    cont = short - D.top_up(short, tol, "c")
    assert full[0] - full[1] == pytest.approx(10.0, abs=1e-5)      # crossing the tolerance pays the tolerance
    assert cont[0] - cont[1] == pytest.approx(0.0, abs=1e-5)
    assert (cont <= tol + 1e-12).all()


def _sel_cell(run, nets, payout_shift=0.0, e=(30.0, 30.0, 30.0)):
    agents = [{"agent": a, "alone": 100.0, "e": e[j], "max_kw": 100.0,
               "components": {k: 0.0 for k in D.COMPONENTS}} for j, a in enumerate(ROSTER)]
    rules = {r: {"net": list(v), "zeroed": [False] * 3} for r, v in nets.items()}
    return {"run": run, "agents": agents, "rules": rules, "mw_cut": 10.0, "relief_mwh": 1.0, "priced_steps": 4}


def test_selection_margin_gate_payout_per_cell_and_harm_before_payout():
    # "1": harms everyone; "1p": harms no one, pays more; "6": harms no one, pays less but negative in one cell;
    # "5": no harm, but zeroes a margin
    c1 = _sel_cell("x", {"1": [0, 0, 0], "1p": [30, 30, 30], "6": [25, 25, 25], "5": [30, 30, 30]})
    c2 = _sel_cell("y", {"1": [0, 0, 0], "1p": [30, 30, 30], "6": [-25, 0, 0], "5": [30, 30, 30]},
                   e=(-30.0, 0.0, 0.0))
    c1["rules"]["5"]["zeroed"] = [True, False, False]
    out = D.select_d56([c1, c2], names=["1", "1p", "6", "5"])
    t = {r["rule"]: r for r in out["table"]}
    assert not t["5"]["e1"] and not t["5"]["eligible"]
    assert t["6"]["payout_usd"] > 0 and not t["6"]["e3"]          # positive in total, negative in one cell
    assert out["winner"] == "1p"                                   # pays more than "1" but harms fewer agents
    assert out["ranking"] == ["1p", "1"]


def test_heldout_check():
    c = _sel_cell("z", {"1": [0, 0, 0], "1p": [30, 30, 30]})
    assert D.heldout_check([c], "1p")["pass"]


def test_roster_missing_agent_raises():
    with pytest.raises(D.CheckFailed):
        D.check_roster(ROSTER, ["A", "B"], "test")


def test_1p_reference_uses_only_registration_and_public_data():
    T = 8
    price = np.array([10.0, 10.0, 10.0, 10.0, 200.0, 200.0, 10.0, 10.0])
    base = np.full((1, T), -50.0)                       # site load 50 kW, no PV
    q = D.ref_1p([100.0], [400.0], [250.0], [price], [np.arange(T)], np.zeros((1, T)), base, wear=10.0)
    batt = q[0] - base[0]
    assert np.all(np.abs(batt) <= 100.0 + 1e-6)
    assert batt[4:6].min() > 0 and batt[:4].sum() <= 1e-6     # discharges into the high price, not before
    steps = [np.array([4, 5])]
    q2 = D.ref_1p([100.0], [400.0], [250.0], [price], steps, np.zeros((1, T)), base, wear=10.0)
    assert np.isnan(q2[0, :4]).all() and not np.isnan(q2[0, 4:6]).any()


def test_pumping_gain_counts_only_gains():
    p = np.array([10.0, 10.0])
    dp = np.array([1.0, 1.0])
    payoff = lambda q: np.array([q[0] - q[1], -q[0]])
    assert list(D.pumping_gain(payoff, p, dp)) == [1.0, 0.0]


def test_bench_1p_is_the_shared_run_reference():
    from sim.reference import selfish_site_net
    T = 8
    price = np.array([10.0, 10.0, 10.0, 10.0, 200.0, 200.0, 10.0, 10.0])
    base = np.full((1, T), -50.0)
    q = D.ref_1p([100.0], [400.0], [250.0], [price], [np.arange(T)], np.zeros((1, T)), base, wear=10.0)
    assert q[0] == pytest.approx(selfish_site_net(100.0, 400.0, 250.0, price, np.zeros(T), base[0], 10.0, D.H))
    assert D.SETTLED_RULE == {"recorded": "1", "selfish_1p": "1p"}


def _conf_cell(run, nets, e=(30.0, 30.0, 30.0), gap=0.0, settled="1p", err=0.0):
    c = _sel_cell(run, nets, e=e)
    for a in c["agents"]:
        a["components"]["da_rt_gap"] = gap
    c.update({"settled_rule": settled, "feasible": True,
              "checks": {"settle_err_usd": err, "roster_complete": True}})
    return c


def test_confirm_check_zero_cells_feasible_cells_payout_and_settle_match():
    ok = _conf_cell("tune", {"1": [0, 0, 0], "1p": [30, 30, 30]})
    zero_ok = _conf_cell("zero", {"1": [0, 0, 0], "1p": [30, 30, 30]})
    r = D.confirm_check([ok, zero_ok], [], ["zero"])
    assert r["pass"] and r["cells"]["zero"]["zero_required"]
    # one agent harmed by settlement where 0 is required, even though rule 1 is worse
    bad = _conf_cell("zero", {"1": [0, 0, 0], "1p": [30, 30, 0]})
    assert not D.confirm_check([bad], [], ["zero"])["cells"]["zero"]["c1_settlement_harm"]
    # elsewhere: no higher than rule 1 on the same trace
    worse = _conf_cell("tune", {"1": [30, 30, 30], "1p": [30, 30, 0]})
    assert not D.confirm_check([worse], [], [])["cells"]["tune"]["c1_settlement_harm"]
    same = _conf_cell("tune", {"1": [30, 30, 0], "1p": [30, 30, 0]})
    assert D.confirm_check([same], [], [])["cells"]["tune"]["c1_settlement_harm"]
    # gap-only harm is not settlement-caused
    gap = _conf_cell("zero", {"1": [0, 0, 0], "1p": [0, 0, 0]}, gap=30.0)
    assert D.confirm_check([gap], [], ["zero"])["cells"]["zero"]["c1_settlement_harm"]
    # agents paying the operator on net, a settle mismatch, or a run settled on another rule fail
    neg = _conf_cell("tune", {"1": [0, 0, 0], "1p": [30, 30, -61]})
    assert not D.confirm_check([neg], [], [])["cells"]["tune"]["c2_payout_nonneg"]
    off = _conf_cell("tune", {"1": [0, 0, 0], "1p": [30, 30, 30]}, err=2e-4)
    assert not D.confirm_check([off], [], [])["cells"]["tune"]["c3_peak_settle_matches"]
    rec = _conf_cell("tune", {"1": [0, 0, 0], "1p": [30, 30, 30]}, settled="1")
    assert not D.confirm_check([rec], [], [])["pass"]
    # a missing or failed cell fails the test
    r = D.confirm_check([ok], ["gone"], [])
    assert not r["pass"] and not r["cells"]["gone"]["pass"]


def test_continuous_top_up_leaves_no_one_harmed_in_metrics():
    c = _sel_cell("x", {"1": [0.0, 0.0, 0.0]}, e=(10.0 + 1 / 3, 57.1, 1e3 / 7))
    m = D.cell_metrics(c, "4c/1")
    assert m["harmed"] == 0 and m["zeroed_agents"] == 3
    assert D.cell_metrics(c, "1")["harmed"] == 3
