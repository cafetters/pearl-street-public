"""M4 run plumbing (plan tests 1 and 11): the optional llm_strategy block in sim/run_m1.py."""
import json

import numpy as np
import pytest


def _cfg(**kw):
    from tests.test_replay import CFG_M11
    return {**CFG_M11, "rounds": "info", "price_mode": True, "plan_to_nomination": True,
            "release_superseded_lock": True, "price_rounds_per_gate": 10, "settlement_reference": "selfish_1p",
            "peak_response_margin_usd_mwh": 5.0, **kw}


def _strat(**kw):
    return {"chooser": "fixed", "cadence": "daily", "t4_threshold_usd_mwh": 20.0, **kw}


def _body(d):
    return (d / "exchange_h0.03_rinfo" / "trace.jsonl").read_text().splitlines()[1:]


def _header(d):
    return json.loads((d / "exchange_h0.03_rinfo" / "trace.jsonl").read_text().splitlines()[0])


def test_flag_off_writes_no_llm_fields(tmp_path):
    from sim.run_m1 import run_variant
    from tests.test_replay import synthetic_slice
    r = run_variant(_cfg(), synthetic_slice(), np.arange(2), tmp_path / "a", headroom_mw=0.03)
    assert "llm_strategy" not in r["header"] and "llm" not in r
    assert not (tmp_path / "a" / "exchange_h0.03_rinfo" / "llm_calls.jsonl").exists()


def test_fixed_normal_at_the_config_margin_gives_an_identical_body(tmp_path):
    from sim.run_m1 import run_variant
    from tests.test_replay import synthetic_slice
    s = synthetic_slice()
    r1 = run_variant(_cfg(), s, np.arange(2), tmp_path / "a", headroom_mw=0.03)
    r2 = run_variant(_cfg(llm_strategy=_strat()), s, np.arange(2), tmp_path / "b", headroom_mw=0.03)
    assert _body(tmp_path / "a") == _body(tmp_path / "b")
    for f in ("C", "D", "X", "SOC"):
        assert np.array_equal(r1[f], r2[f])
    assert r2["llm"]["decisions"] > 0


def test_header_and_summary_name_every_roster_agent(tmp_path):
    from sim.run_m1 import run_variant
    from tests.test_replay import synthetic_slice
    r = run_variant(_cfg(llm_strategy=_strat(chooser="rule")), synthetic_slice(), np.arange(2), tmp_path,
                    headroom_mw=0.03)
    h = _header(tmp_path)["llm_strategy"]
    for k in ("chooser_by_agent", "cadence", "menu_version", "label_table_sha256", "posture_margin_usd_mwh",
              "t4_threshold_usd_mwh", "bold_allowed", "reset", "backend"):
        assert k in h, k
    roster = r["header"]["roster"]
    assert sorted(h["chooser_by_agent"]) == sorted(roster)
    assert set(r["llm"]["labels_by_agent"]) == set(roster)
    side = (tmp_path / "exchange_h0.03_rinfo" / "llm_calls.jsonl").read_text().splitlines()
    head = json.loads(side[0])
    assert head["event"] == "header" and head["run_id"] == r["header"]["run_id"]
    assert head["git_commit"] == r["header"]["git_commit"] and head["seed"] == r["header"]["seed"]
    assert any(json.loads(ln)["event"] == "decision" for ln in side[1:])


def test_a_bad_llm_strategy_block_is_refused_before_the_run(tmp_path):
    from sim.run_m1 import run_variant
    from tests.test_replay import synthetic_slice
    with pytest.raises(ValueError):
        run_variant(_cfg(llm_strategy=_strat(chooser="oracle")), synthetic_slice(), np.arange(2), tmp_path,
                    headroom_mw=0.03)
    with pytest.raises(ValueError):
        run_variant(_cfg(llm_strategy=_strat(unknown_key=1)), synthetic_slice(), np.arange(2), tmp_path,
                    headroom_mw=0.03)


def _llm(tmp_path, **kw):
    return _strat(chooser="llm", cap_usd_run=1.0, **kw)


def test_llm_run_on_the_fake_backend_logs_calls_and_dollars_per_day(tmp_path):
    from sim.run_m1 import run_variant
    from tests.test_replay import synthetic_slice
    r = run_variant(_cfg(llm_strategy=_llm(tmp_path, backend="fake")), synthetic_slice(), np.arange(2), tmp_path,
                    headroom_mw=0.03)
    s = r["llm"]
    assert s["llm_calls"] >= 2 and s["usd_total"] > 0 and s["usd_per_simulated_day"] > 0
    assert s["llm_cap_hit"] is False
    side = [json.loads(ln) for ln in (tmp_path / "exchange_h0.03_rinfo" / "llm_calls.jsonl").read_text().splitlines()]
    assert sum(1 for x in side if x["event"] == "call") == s["llm_calls"]
    h = _header(tmp_path)["llm_strategy"]
    assert set(h["models"]["draws"]) == set(r["header"]["roster"]) and h["cap_usd_run"] == 1.0


def test_twin_and_attack_runs_give_honest_agents_the_same_labels_through_one_cache(tmp_path):
    from sim.run_m1 import run_variant
    from tests.test_replay import synthetic_slice
    s = synthetic_slice()
    cache = tmp_path / "window_cache.jsonl"
    twin = run_variant(_cfg(llm_strategy=_llm(tmp_path, backend="fake_random:1",
                                              cache={"path": str(cache), "mode": "record"})),
                       s, np.arange(2), tmp_path / "twin", headroom_mw=0.03)
    ids = twin["header"]["roster"]
    atk_cfg = _cfg(llm_strategy=_llm(tmp_path, backend="fake_random:2", cache={"path": str(cache),
                                                                             "mode": "read_through"}),
                   attackers=[{"agent": ids[1], "type": "silent"}])
    atk = run_variant(atk_cfg, s, np.arange(2), tmp_path / "atk", headroom_mw=0.03)
    honest = ids[0]
    assert twin["llm"]["labels_by_agent"][honest] == atk["llm"]["labels_by_agent"][honest]
    assert atk["llm"]["cache"]["hits"] >= 1
