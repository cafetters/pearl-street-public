"""M4 step 9: the ablation table (Fixed / Rule / LLM; claim 2 is LLM vs Rule) and the Q-N7 paired table."""
import json

import pytest

from bench import m4_ablation as A

ROSTER = ["A", "B", "C"]


def _run(tmp_path, name, chooser, values, peak=100.0, calls=None, backend="fake", model=None, roster=ROSTER,
         start="2023-01-12", end="2023-01-13"):
    d = tmp_path / name
    d.mkdir()
    cfg = {"start": start, "end": end, "run_name": name}
    if chooser:
        cfg["llm_strategy"] = str({"chooser": chooser, "backend": backend})
    ex = {"controllable": {"peak_mw": peak, "share_of_m0_ceiling": 0.9, "share_of_selfish_to_ceiling_gap": 0.5},
          "value_share_of_m0_ceiling": 0.4, "net_harm": {"agents_harmed": 1, "total_net_harm_usd": 10.0,
                                                          "missing_from_check": []},
          "agent_value_usd": dict(zip(roster, values))}
    if chooser == "llm":
        ex["llm"] = {"llm_calls": 6, "usd_total": 0.6, "usd_per_simulated_day": 0.3, "simulated_days": 2,
                     "label_counts_by_day": {"2023-01-12": {"normal": 2, "cautious": 1}},
                     "by_model": {model or "anthropic/claude-haiku-5-5": {"usd": 0.6}}}
    elif chooser:
        ex["llm"] = {"label_counts_by_day": {"2023-01-12": {"normal": 3}}}
    summ = {"header": {"roster": roster, "config": cfg, "git_commit": "abc"}, "exchange_by_headroom_mw": {"100": ex}}
    (d / "summary.json").write_text(json.dumps(summ))
    if calls:
        sub = d / "exchange_h100_rinfo"
        sub.mkdir()
        lines = [{"event": "header"}] + [{"event": "call", "agent": a, "usd": u, "model": "m"} for a, u in calls]
        (sub / "llm_calls.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
    return d


def test_ablation_table_three_arms_and_claim_2(tmp_path):
    f = _run(tmp_path, "fixed", "fixed", [1.0, 2.0, 3.0])
    r = _run(tmp_path, "rule", "rule", [2.0, 2.0, 3.0], peak=99.0)
    llm = _run(tmp_path, "llm", "llm", [4.0, 1.0, 3.0], peak=98.0, calls=[("A", 0.2), ("B", 0.3), ("C", 0.1)])
    t = A.ablation([f, r, llm])
    arms = {x["arm"]: x for x in t["arms"]}
    assert set(arms) == {"fixed", "rule", "llm"}
    assert arms["llm"]["usd_per_simulated_day"] == 0.3 and arms["llm"]["llm_calls"] == 6
    assert arms["rule"]["peak_ctrl_mw"] == 99.0 and arms["llm"]["gap_share_ctrl"] == 0.5
    c2 = t["claim_2_llm_vs_rule"]
    assert c2["total_value_added_usd_per_day"] == pytest.approx((4 + 1 + 3 - 2 - 2 - 3) / 2)
    per = {x["agent"]: x for x in t["per_agent"]}
    assert per["A"]["llm_usd_per_day"] == pytest.approx(0.1) and per["A"]["value_added_over_rule_usd_per_day"] == 1.0
    assert per["B"]["value_added_over_rule_usd_per_day"] == pytest.approx(-0.5)
    assert t["label"].startswith("synthetic")
    assert "| arm |" in t["markdown"] and "gap share" in t["markdown"]


def test_a_missing_roster_agent_fails(tmp_path):
    f = _run(tmp_path, "fixed", "fixed", [1.0, 2.0, 3.0])
    r = _run(tmp_path, "rule", "rule", [1.0, 2.0], roster=["A", "B"])
    with pytest.raises(A.CheckFailed):
        A.ablation([f, r])
    bad = _run(tmp_path, "bad", "rule", [1.0, 2.0, 3.0])
    s = json.loads((bad / "summary.json").read_text())
    del s["exchange_by_headroom_mw"]["100"]["agent_value_usd"]["C"]
    (bad / "summary.json").write_text(json.dumps(s))
    with pytest.raises(A.CheckFailed):
        A.ablation([f, bad])


def test_arms_must_share_the_window(tmp_path):
    f = _run(tmp_path, "fixed", "fixed", [1.0, 2.0, 3.0])
    r = _run(tmp_path, "rule", "rule", [1.0, 2.0, 3.0], start="2023-01-06", end="2023-01-07")
    with pytest.raises(A.CheckFailed):
        A.ablation([f, r])


def test_a_run_without_llm_strategy_at_5_is_the_fixed_arm(tmp_path):
    f = _run(tmp_path, "plain", None, [1.0, 2.0, 3.0])
    t = A.ablation([f])
    assert t["arms"][0]["arm"] == "fixed (no llm_strategy block; normal = $5)"


def test_qn7_pairs_all_x_vs_all_y(tmp_path):
    x = _run(tmp_path, "haiku", "llm", [1.0, 2.0, 3.0], model="anthropic/claude-haiku-5-5")
    y = _run(tmp_path, "sonnet", "llm", [1.5, 2.0, 3.5], model="anthropic/claude-sonnet-5-5")
    t = A.qn7([x, y])
    assert [r["model"] for r in t["arms"]] == ["anthropic/claude-haiku-5-5", "anthropic/claude-sonnet-5-5"]
    assert t["value_difference_usd_per_day"] == pytest.approx((1.5 + 2 + 3.5 - 1 - 2 - 3) / 2)
