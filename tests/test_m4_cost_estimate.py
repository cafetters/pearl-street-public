"""M4 step 7: bench/llm_cost_estimate.py (owner, Nov 12)."""
import pytest

from bench import llm_cost_estimate as E

ONE = [{"provider": "anthropic", "model": "claude-haiku-5-5", "weight": 1.0}]


def test_expected_calls_follow_the_cadence_with_no_triggers():
    # start + scheduled evaluations over D-1 publication days
    assert E.expected_calls("daily", days=7, rate=0.0) == pytest.approx(7)
    assert E.expected_calls("weekly", days=30, rate=0.0) == pytest.approx(1 + 4)
    assert E.expected_calls("monthly", days=30, rate=0.0) == pytest.approx(1)
    assert E.expected_calls("triggers_only", days=30, rate=0.0) == pytest.approx(1)
    assert E.expected_calls("backoff", days=30, rate=0.0) == pytest.approx(1 + 4)      # days 1, 3, 7, 15


def test_triggers_add_calls_and_a_certain_trigger_means_a_call_every_day():
    assert E.expected_calls("triggers_only", days=30, rate=0.1) == pytest.approx(1 + 29 * 0.1)
    assert E.expected_calls("weekly", days=30, rate=1.0) == pytest.approx(30)
    assert E.expected_calls("weekly", days=30, rate=0.1) > E.expected_calls("weekly", days=30, rate=0.0)
    assert E.worst_calls("weekly", days=30) == 30 and E.worst_calls("daily", days=2) == 2


def test_dollars_scale_with_tokens_prices_and_agents():
    est = E.estimate(preset="daily", days=2, n_agents=10, honest=ONE, prompt_tokens=1000, out_tokens=100,
                     max_tokens=2048, rate=0.0)
    per_call = (1000 * 0.10 + 100 * 0.50) / 1e6
    assert est["expected_usd"] == pytest.approx(10 * 2 * per_call)
    worst_call = (1000 * 0.10 + 2048 * 0.50) / 1e6
    assert est["worst_usd"] == pytest.approx(10 * 2 * worst_call)
    assert est["by_model"]["anthropic/claude-haiku-5-5"]["agents"] == pytest.approx(10)


def test_an_unpriced_model_is_reported_not_summed():
    w = [{"provider": "anthropic", "model": "claude-haiku-5-5", "weight": 0.5},
         {"provider": "openai", "model": "gpt-unlisted", "weight": 0.5}]   # no price row
    est = E.estimate(preset="daily", days=2, n_agents=10, honest=w, prompt_tokens=1000, out_tokens=100,
                     max_tokens=2048, rate=0.0)
    assert est["by_model"]["openai/gpt-unlisted"]["expected_usd"] is None
    assert est["unpriced_models"] == ["openai/gpt-unlisted"]
    assert est["expected_usd"] == pytest.approx(5 * 2 * (1000 * 0.10 + 100 * 0.50) / 1e6)


def test_twin_multiplier_bounds():
    m = E.twin_multiplier(n_agents=80)
    assert m["worst"] == 2.0 and m["best"] == pytest.approx(1 + 1 / 80)


def test_table_covers_every_preset_and_horizon():
    t = E.table(n_agents=80, honest=E.AS.DEFAULT_HONEST, prompt_tokens=746, out_tokens=800, max_tokens=2048,
                rate=0.1, rate_label="assumed")
    assert {r["preset"] for r in t["rows"]} == set(E.CD.PRESETS)
    assert {r["days"] for r in t["rows"]} == {2, 7, 30, 365}
    assert t["trigger_rate"] == {"value": 0.1, "label": "assumed"}
    assert "markdown" in t and "| preset |" in t["markdown"]


def test_config_check_refuses_a_worst_case_above_the_run_cap():
    cfg = {"start": "2023-01-12", "end": "2023-01-13",
           "llm_strategy": {"chooser": "llm", "cap_usd_run": 0.001, "cadence": "daily", "honest_models": ONE}}
    r = E.check_config(cfg, n_agents=80, prompt_tokens=746)
    assert r["ok"] is False and r["worst_usd"] > 0.001
    cfg["llm_strategy"]["cap_usd_run"] = 5.0
    assert E.check_config(cfg, n_agents=80, prompt_tokens=746)["ok"] is True
    assert E.check_config({"start": "2023-01-12", "end": "2023-01-13"}, n_agents=80)["ok"] is True   # no LLM


def test_runtime_entry_is_used_when_present(tmp_path):
    import json
    p = tmp_path / "rt.json"
    p.write_text(json.dumps({"entries": [{"days": 2, "agents": 80, "runtime_s": 800, "peak_mem_mb": None,
                                          "source": "x"}]}))
    assert E.runtime_for(2, 80, p)["runtime_s"] == 800
    assert E.runtime_for(7, 80, p) is None
