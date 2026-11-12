"""M4 steps 6-7: the LLM chooser (plan test 7), spend caps (D94, plan test 8) and keys (plan test 9's run half)."""
import json

import numpy as np
import pytest

from agents import strategy as S
from agents.llm import spend as SP
from agents.llm.backends import FakeAdversarial, FakeBackend, Request
from agents.llm.chooser import LLMRun
from tests.test_m4_strategy import _view

FAKE_KEY = "sk-" + "ant-" + "api03-" + "Qw" * 20 + "AA"


def _run(tmp_path=None, **spec):
    base = {"chooser": "llm", "backend": "fake", "cap_usd_run": 5.0,
            "honest_models": [{"provider": "anthropic", "model": "claude-haiku-5-5", "weight": 1.0}]}
    base.update(spec)
    return LLMRun(base, roster=["A", "B"], attackers=set(), seed=1, run_id="r", power_kw={"A": 1.0, "B": 2.0})


# ---- price table ----

def test_price_table_rows_carry_source_and_date():
    for mid, row in SP.PRICES.items():
        assert row["source_url"] and row["checked"], mid       # verified Nov 12: tests/test_m4_prices.py
    assert SP.PRICES["anthropic/claude-haiku-5-5"]["in"] == 0.10
    assert SP.PRICES["anthropic/claude-haiku-5-5"]["long"] == {"in": 0.50, "out": 2.50}
    assert SP.PRICES["openai/gpt-6-luna"]["live"] is False      # priced, live refused until the owner confirms (D96)


def test_cost_uses_the_over_100k_tier():
    assert SP.cost_usd("anthropic/claude-haiku-5-5", 100_000, 0) == pytest.approx(0.01)
    assert SP.cost_usd("anthropic/claude-haiku-5-5", 100_001, 0) == pytest.approx(100_001 * 0.50 / 1e6)
    assert SP.cost_usd("anthropic/claude-haiku-5-5", 200_000, 1000) == pytest.approx(0.1 + 1000 * 2.5 / 1e6)


# ---- the run cap (D94) ----

def _req(user="x" * 350, model="claude-sonnet-5-5", max_tokens=1000):
    return Request(provider="anthropic", model=model, system="", user=user, max_tokens=max_tokens)


def test_cap_refuses_the_call_that_would_cross_it_before_it_is_sent():
    g = SP.SpendGuard(cap_usd_run=0.015, live=True)
    worst = SP.worst_case_usd(_req())
    assert worst == pytest.approx(130 * 2 / 1e6 + 1000 * 10 / 1e6)
    g.authorize(_req())
    g.record(_req(), type("R", (), {"input_tokens": 130, "output_tokens": 1000})(), hit=False)
    with pytest.raises(SP.CapReached):
        g.authorize(_req())
    assert g.cap_hit is True
    with pytest.raises(SP.CapReached):          # and stays hit
        g.authorize(_req(user="y"))


def test_a_live_model_with_no_price_is_refused_and_fake_runs_record_it_unpriced():
    r = Request(provider="openai", model="gpt-unlisted", system="", user="x")   # no price row
    with pytest.raises(SP.PriceMissing):
        SP.SpendGuard(cap_usd_run=5.0, live=True).authorize(r)
    g = SP.SpendGuard(cap_usd_run=5.0, live=False)
    g.authorize(r)
    assert g.record(r, type("R", (), {"input_tokens": 10, "output_tokens": 10})(), hit=False) is None


def test_a_cache_hit_costs_nothing():
    g = SP.SpendGuard(cap_usd_run=5.0, live=True)
    assert g.record(_req(), type("R", (), {"input_tokens": 10, "output_tokens": 10})(), hit=True) == 0.0
    assert g.spent_usd == 0.0


def test_phase_allocator_refuses_a_run_whose_cap_does_not_fit():
    al = SP.PhaseAllocator(cap_usd_phase=10.0, spent_usd=2.0)
    al.reserve("r1", 5.0)
    with pytest.raises(SP.CapReached):
        al.reserve("r2", 3.01)
    al.release("r1", spent_usd=1.0)        # reconciled from the call log
    al.reserve("r2", 7.0)
    assert al.committed_usd() == pytest.approx(2.0 + 1.0 + 7.0)


# ---- the chooser ----

def test_llm_chooser_returns_a_menu_label_and_logs_the_call():
    run = _run()
    d = run.chooser_for("A").decide(_view(), "start")
    assert d.label in ("normal", "cautious") and d.source == "llm" and d.error is None
    rec = run.call_records()[-1]
    assert rec["event"] == "call" and rec["agent"] == "A" and rec["model"] == "anthropic/claude-haiku-5-5"
    assert rec["tokens_in"] > 0 and rec["usd"] > 0 and rec["reason"] == "start" and rec["label"] == d.label
    assert "rationale" in rec


@pytest.mark.parametrize("mode,err", [("bad_json", "bad_json"), ("out_of_menu", None), ("number", "not_a_label"),
                                      ("refusal", "refusal"), ("error", "backend_error:timeout"),
                                      ("exception", "backend_exception:RuntimeError"), ("bold", None)])
def test_bad_answers_keep_the_previous_label(mode, err):
    run = _run()
    run.backend = FakeAdversarial(mode)
    inner = type("I", (), {"id": "A", "margin": 5.0, "posture": "normal", "iso": ["t"] * 192,
                           "base_export": -np.ones(192), "b": type("B", (), {"P": 1.0, "CAP": 2.0})(),
                           "receive": lambda self, r: None,
                           "act": lambda self, k, s, v, p: ([], (0, 0, 0))})()
    local = ["d0"] * 96 + ["d1"] * 96
    ag = S.StrategyAgent(inner, run.chooser_for("A"), S.daily_cadence(),
                         S.RunContext(local_dates=local, da_spread_threshold=None))
    ag.act(0, 0.0, 1, np.full(192, 30.0))
    assert inner.posture == "normal" and inner.margin == 5.0
    last = ag.decisions[-1]
    assert last["valid"] is False
    if err:
        assert last["error"] == err
    else:
        assert last["error"] == "label_not_allowed"


def test_the_rationale_never_reaches_a_later_prompt():
    run = _run()
    run.backend = FakeBackend(script=[json.dumps({"posture": "cautious", "rationale": "SECRET_NOTE_123"})] * 3)
    ch = run.chooser_for("A")
    ch.decide(_view(), "start")
    seen = []
    orig = run.backend.complete
    run.backend.complete = lambda req: (seen.append(req.system + req.user), orig(req))[1]
    ch.decide(_view(label_history=("cautious",), current_label="cautious"), "scheduled")
    assert seen and "SECRET_NOTE_123" not in seen[0]


def test_cap_reached_reverts_to_normal_and_marks_the_run():
    run = _run(cap_usd_run=0.0)
    d = run.chooser_for("A").decide(_view(current_label="cautious"), "scheduled")
    assert d.label == "normal" and d.source == "cap_reached"
    assert run.cap_hit() is True and run.summary(["d0"])["llm_cap_hit"] is True


def test_live_backend_without_a_key_fails_closed_with_no_call(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    run = _run(backend="live")
    d = run.chooser_for("A").decide(_view(), "start")
    assert d.label is None and d.error == "missing_key"
    assert run.call_records()[-1]["error"] == "missing_key"


def test_summary_rolls_up_dollars_per_simulated_day():
    run = _run()
    ch = run.chooser_for("A")
    ch.decide(_view(decision_day="d0"), "start")
    ch.decide(_view(decision_day="d1", da_spread=3.0), "scheduled")
    s = run.summary(["d0"] * 96 + ["d1"] * 96)
    assert s["llm_calls"] == 2 and s["simulated_days"] == 2
    assert s["usd_total"] == pytest.approx(sum(r["usd"] for r in run.call_records()))
    assert s["usd_per_simulated_day"] == pytest.approx(s["usd_total"] / 2)
    assert set(s["usd_by_day"]) == {"d0", "d1"} and "anthropic/claude-haiku-5-5" in s["by_model"]
    assert s["cache"] == {"mode": "off", "hits": 0, "misses": 2, "sha256_end": None}


def test_no_key_value_in_header_records_or_summary(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    run = _run()
    run.chooser_for("A").decide(_view(), "start")
    blob = json.dumps([run.header(), run.call_records(), run.summary(["d0"])], default=str)
    assert FAKE_KEY not in blob
