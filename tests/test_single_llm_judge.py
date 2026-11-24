"""Single-LLM-agent judge (owner, Nov 24) on synthetic run folders shaped like a downloaded cloud run."""
import copy
import json

import pytest

from bench import bystander_check as BC
from bench import single_llm_judge as J

X = "A2"
ROSTER = ["A1", "A2", "A3"]
COMMIT = "abc123def456"
TERM = "Zq" + "judgeblocked"
CFG = {"run_name": "t", "start": "2023-07-17", "end": "2023-07-26", "seed": 1, "headroom_mw": [100],
       "harm_unrounded": True,
       "llm_strategy": {"chooser": "rule", "cadence": "daily", "bold_allowed": True}}


def _cfg(name, per_agent=None, **llm):
    c = copy.deepcopy(CFG)
    c["run_name"] = name
    if per_agent:
        c["llm_strategy"]["per_agent"] = per_agent
    c["llm_strategy"].update(llm)
    return {k: str(v) for k, v in c.items()}


def _mk(tmp, name, kind="twin", x_labels=None, others=None, values=None, short=None, commit=COMMIT, cfg=None,
        cap_hit=False, extra_value=0.0, calls=None, fixed=None):
    d = tmp / name
    v = d / "exchange_h100_rinfo"
    v.mkdir(parents=True)
    others = others or {"2023-07-17": "normal", "2023-07-18": "cautious"}
    x_labels = x_labels or dict(others)
    labels = {a: dict(others) for a in ROSTER}
    labels[X] = dict(x_labels)
    values = dict(values or {"A1": 100.0, "A2": 50.0, "A3": 30.0})
    values[X] += extra_value
    short = short or {a: -5.0 for a in ROSTER}
    ev = {"agent_value_usd": values,
          "net_harm": {"by_agent_usd": {a: round(max(s, 0.0), 2) for a, s in short.items()}, "shortfall_usd": short},
          "peak_mw": 1.0,
          "llm": {"labels_by_agent": labels, "llm_cap_hit": cap_hit, "llm_calls": len(calls or [])}}
    s = {"header": {"git_commit": commit, "roster": ROSTER}, "exchange_by_headroom_mw": {"100": ev}}
    (d / "summary.json").write_text(json.dumps(s))
    cba = {a: "rule" for a in ROSTER}
    if kind != "twin":
        cba[X] = "llm" if kind == "llm" else "fixed"
    per = {X: cba[X]} if kind != "twin" else None
    extra = {}
    if kind == "llm":
        extra = {"route": "course", "backend": "live", "honest_models": [{"provider": "anthropic", "model": "m"}]}
    if kind == "control":
        extra = {"fixed_label": fixed}
    th = {"event": "header", "git_commit": commit, "seed": 1, "roster": ROSTER, "run_at_utc": name,
          "config": cfg or _cfg(name, per, **extra),
          "llm_strategy": {"chooser_by_agent": cba, "t4_threshold_usd_mwh": 20.0, "backend": extra.get("backend"),
                           "route": extra.get("route")}}
    (v / "trace.jsonl").write_text(json.dumps(th) + "\n" + json.dumps({"event": "tick", "id": f"{name}-x", "v": 1})
                                   + "\n")
    if kind == "llm":
        head = {"event": "header", "run_id": name, "config": {"a": 1}, "seed": 1, "git_commit": commit,
                "llm_strategy": {"backend": "live", "route": "course", "chooser_by_agent": cba}}
        rows = calls if calls is not None else [
            {"event": "call", "agent": X, "decision_day": "2023-07-17", "model": "anthropic/m", "sent": True,
             "cache_hit": False, "tokens_in": 4, "tokens_out": 50, "usd_table": 1e-4, "usd_reported": 1e-4,
             "error": None, "request_sha256": "ab" * 32}]
        (v / "llm_calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [head] + rows))
    return d


def _patterns(tmp):
    from bench.course_route_check import load_patterns
    t = tmp / "terms.txt"
    t.write_text(TERM + "\n")
    return load_patterns(t)


def _set(tmp, **over):
    twin = _mk(tmp, "twin")
    cp = _mk(tmp, "twin_copy")
    llm = _mk(tmp, "llm_haiku", "llm", x_labels={"2023-07-17": "bold", "2023-07-18": "normal"}, extra_value=3.0,
              **over.get("llm", {}))
    ctrl = {lab: _mk(tmp, f"ctrl_{lab}", "control", x_labels={"2023-07-17": lab, "2023-07-18": lab}, fixed=lab)
            for lab in ("normal", "cautious", "bold")}
    return twin, cp, [llm], ctrl


def _judge(tmp, sets=None):
    twin, cp, llms, ctrl = sets or _set(tmp)
    return J.judge(X, COMMIT, twin, cp, llms, ctrl, patterns=_patterns(tmp))


def test_a_consistent_set_passes_and_reports_the_effect_on_others(tmp_path):
    rep = _judge(tmp_path)
    assert rep["verdict"] == "PASS", rep["gates"]
    r = rep["reported_not_gated"]["llm_haiku"]
    assert r["x"]["value_minus_twin_usd"] == pytest.approx(3.0) and r["others_section8"]["verdict"] == "PASS"
    assert r["others_section8"]["treated"] == [X] and r["others_section8"]["honest_agents"] == 2
    assert set(r["label_differences"]) == {X}


def test_a_config_change_outside_the_allowed_keys_fails_integrity(tmp_path):
    twin, cp, llms, ctrl = _set(tmp_path)
    tr = llms[0] / "exchange_h100_rinfo" / "trace.jsonl"
    lines = tr.read_text().splitlines()
    h = json.loads(lines[0])
    h["config"]["seed"] = "2"
    h["config"]["headroom_mw"] = "[150]"
    tr.write_text("\n".join([json.dumps(h)] + lines[1:]) + "\n")
    rep = _judge(tmp_path, (twin, cp, llms, ctrl))
    assert rep["verdict"] == "FAIL" and any("headroom_mw" in f for f in rep["gates"]["integrity"])


def test_a_dirty_or_other_commit_fails_integrity(tmp_path):
    twin, cp, llms, ctrl = _set(tmp_path)
    bad = _mk(tmp_path, "llm_dirty", "llm", commit=COMMIT + "-dirty")
    rep = _judge(tmp_path, (twin, cp, [bad], ctrl))
    assert any("pre-registered" in f for f in rep["gates"]["integrity"])


def test_a_twin_copy_that_differs_fails_determinism_but_run_names_and_clock_do_not(tmp_path):
    assert _judge(tmp_path)["gates"]["determinism"] == []                  # names and run_at_utc differ already
    twin, cp, llms, ctrl = _set(tmp_path / "b")
    s = json.loads((cp / "summary.json").read_text())
    s["exchange_by_headroom_mw"]["100"]["peak_mw"] = 1.5
    (cp / "summary.json").write_text(json.dumps(s))
    rep = _judge(tmp_path / "b", (twin, cp, llms, ctrl))
    assert rep["verdict"] == "FAIL" and rep["gates"]["determinism"]


def test_equal_x_labels_must_give_equal_exchange_values(tmp_path):
    twin, cp, _, ctrl = _set(tmp_path)
    same = _mk(tmp_path, "llm_same", "llm", extra_value=0.0)
    assert _judge(tmp_path, (twin, cp, [same], ctrl))["verdict"] == "PASS"
    moved = _mk(tmp_path, "llm_moved", "llm", extra_value=1.0)
    rep = _judge(tmp_path, (twin, cp, [moved], ctrl))
    assert any("labels equal the twin" in f for f in rep["gates"]["determinism"])


def test_health_fails_on_a_cap_hit_a_bold_rule_agent_an_unfixed_control_or_a_bad_call_log(tmp_path):
    twin, cp, llms, ctrl = _set(tmp_path)
    capped = _mk(tmp_path, "llm_cap", "llm", cap_hit=True)
    boldrule = _mk(tmp_path, "llm_bold", "llm", others={"2023-07-17": "bold", "2023-07-18": "normal"})
    loose = _mk(tmp_path, "ctrl_loose", "control", x_labels={"2023-07-17": "normal", "2023-07-18": "cautious"},
                fixed="normal")
    errs = _mk(tmp_path, "llm_err", "llm", calls=[{"event": "call", "agent": X, "sent": True, "cache_hit": False,
                                                    "usd_table": 1e-4, "usd_reported": 1e-4,
                                                    "error": "backend_error:timeout", "request_sha256": "cd" * 32}])
    rep = J.judge(X, COMMIT, twin, cp, [capped, boldrule, errs], {"normal": loose}, patterns=_patterns(tmp_path))
    h = " | ".join(rep["gates"]["health"])
    assert "cap was hit" in h and "Rule agents applied bold" in h and "fixed label" in h and "course_route_check" in h


def test_a_leaked_term_in_an_llm_run_fails_health(tmp_path):
    twin, cp, llms, ctrl = _set(tmp_path)
    (llms[0] / "exchange_h100_rinfo" / "note.txt").write_text(TERM.upper())
    rep = _judge(tmp_path, (twin, cp, llms, ctrl))
    assert any("leak" in f for f in rep["gates"]["health"])


def test_others_newly_below_acting_alone_are_reported_not_gated(tmp_path):
    twin, cp, _, ctrl = _set(tmp_path)
    hurt = _mk(tmp_path, "llm_hurt", "llm", x_labels={"2023-07-17": "bold", "2023-07-18": "bold"},
               short={"A1": 2.0, "A2": -5.0, "A3": -5.0})
    rep = _judge(tmp_path, (twin, cp, [hurt], ctrl))
    assert rep["verdict"] == "PASS"
    assert "A1" in rep["reported_not_gated"]["llm_hurt"]["others_section8"]["newly_below_acting_alone"]


def test_the_bystander_check_default_path_is_unchanged(tmp_path):
    twin, cp, llms, _ = _set(tmp_path)
    with pytest.raises(BC.CheckFailed, match="attack header has no attackers"):
        BC.check(llms[0], twin, basis="unrounded")
    with pytest.raises(BC.CheckFailed, match="treated agents not on the roster"):
        BC.check(llms[0], twin, basis="unrounded", treated=["ZZ"])


def test_a_replay_must_match_its_source_on_exchange_values_and_labels(tmp_path):
    twin, cp, llms, ctrl = _set(tmp_path)
    xl = {"2023-07-17": "bold", "2023-07-18": "normal"}
    good = _mk(tmp_path, "replay_ok", "llm", x_labels=xl, extra_value=3.0, calls=[])
    rep = J.judge(X, COMMIT, twin, cp, llms, ctrl, {good: llms[0]}, patterns=_patterns(tmp_path))
    assert rep["gates"]["determinism"] == []
    bad = _mk(tmp_path, "replay_bad", "llm", x_labels={"2023-07-17": "normal", "2023-07-18": "normal"},
              extra_value=3.0, calls=[])
    rep = J.judge(X, COMMIT, twin, cp, llms, ctrl, {bad: llms[0]}, patterns=_patterns(tmp_path))
    assert any("replay differs" in f for f in rep["gates"]["determinism"])
