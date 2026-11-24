"""Free-hand honest LLM agent (owner, Nov 24): the red-team loop with an honest instruction, per-call cost logs, a live
backend only for a pre-registered course-route round, and the cloud launcher's red-team form."""
import json

import pytest

from agents.llm import course_route as CR
from agents.llm.backends import Request, Response
from agents.llm.spend import SpendGuard
from redteam import attacker as A
from redteam import launch as L

KEY = "sk-" + "zz" + "-v1-" + "Rr" * 20


@pytest.fixture(autouse=True)
def _no_real_config(monkeypatch, tmp_path):
    for v in CR.NEUTRAL_ENV.values():
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv(CR.CONFIG_PATH_ENV, str(tmp_path / "none.json"))


def _course_env(monkeypatch):
    for k, v in {"base_url": "https://gw.example.invalid", "key": KEY, "cost_header": "x-tst-cost",
                 "request_id_header": "x-tst-rid"}.items():
        monkeypatch.setenv(CR.NEUTRAL_ENV[k], v)


def test_the_honest_role_swaps_only_the_instruction():
    h = A.AttackerLoop("X", None, "anthropic", "claude-haiku-5-5", SpendGuard(1.0, live=False), role="honest")
    a = A.AttackerLoop("X", None, "anthropic", "claude-haiku-5-5", SpendGuard(1.0, live=False))
    assert h.system.startswith(A.HONEST_SYSTEM) and h.prompt_version == "honest-1"
    assert a.system.startswith(A.SYSTEM) and a.prompt_version == A.PROMPT_VERSION
    assert "honestly" in A.HONEST_SYSTEM and "worse off" not in A.HONEST_SYSTEM
    with pytest.raises(ValueError):
        A.AttackerLoop("X", None, "anthropic", "m", SpendGuard(1.0, live=False), role="other")


@pytest.mark.parametrize("raw,want", [('```json\n{"tool": "advance"}\n```', '{"tool": "advance"}'),
                                      ('```\n{"tool": "done"}\n```', '{"tool": "done"}'),
                                      ('{"tool": "advance"}', '{"tool": "advance"}'),
                                      ("I can't help with that", "I can't help with that")])
def test_a_fenced_reply_is_read_as_its_json(raw, want):
    assert A.strip_fence(raw) == want


def test_every_model_call_is_logged_with_table_and_reported_cost():
    lp = A.AttackerLoop("X", None, "anthropic", "claude-haiku-5-5", SpendGuard(1.0, live=False), role="honest")
    req = Request(provider="anthropic", model="claude-haiku-5-5", system="s", user="u")
    resp = Response(text="{}", input_tokens=4, output_tokens=100, stop_reason="end_turn", cache_write_tokens=800,
                    meta={"reported_usd": 0.00021, "request_id": "r-1", "attempts": [{}]})
    lp._log_call(req, resp, False, 0.00021, 3, {"t_utc": "2023-01-12T15:00:00Z", "reason": "call_nominations"})
    lp._log_call(req, Response(text=None, input_tokens=0, output_tokens=0, stop_reason="error", error="timeout"),
                 False, 0.001, 3, {"t_utc": "2023-01-12T15:00:00Z"})
    r0, r1 = lp.call_log
    assert r0["usd_table"] == pytest.approx((4 * 0.1 + 800 * 0.2 + 100 * 0.5) / 1e6)
    assert r0["usd_reported"] == 0.00021 and r0["request_id"] == "r-1" and r0["decision_day"] == "2023-01-12"
    assert r0["error"] is None and r1["error"] == "backend_error:timeout"


def test_a_live_round_needs_its_preregistration_and_the_course_route(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="preregistration"):
        L.backend_factory({"backend": "live", "route": "course"})
    with pytest.raises(ValueError, match="preregistration"):
        L.backend_factory({"backend": "live", "route": "course", "preregistration": "docs/plans/none_such.md"})
    pre = "docs/plans/single_llm_prereg.md"                      # any committed document
    with pytest.raises(ValueError, match="course route"):
        L.backend_factory({"backend": "live", "route": "direct", "preregistration": pre})
    with pytest.raises(CR.RouteConfigMissing):                    # no config: fails before the round
        L.backend_factory({"backend": "live", "route": "course", "preregistration": pre})
    _course_env(monkeypatch)
    make = L.backend_factory({"backend": "live", "route": "course", "preregistration": pre})
    assert isinstance(make("PVB_33", {}), CR.CourseRoute)
    with pytest.raises(ValueError):
        L.backend_factory({"backend": "something"})


def test_the_launcher_reads_the_red_team_forms_route_and_cap(monkeypatch):
    from bench import cloud_queue as CQ
    cfg = {"run_name": "hf", "llm_route": "course", "llm_cap_usd_run": 2.0, "phase": {}}
    assert CQ.llm_reasons(cfg)                                    # the queue must be declared --llm
    assert CQ.check_phase([cfg], 2.0, 0.0) == pytest.approx(2.0)
    with pytest.raises(CQ.QueueError):
        CQ.check_phase([cfg], 1.5, 0.0)
    _course_env(monkeypatch)
    try:
        env = CQ.course_route_env([{"name": "hf", "cfg": cfg}])
        assert set(env) == set(CR.NEUTRAL_ENV.values())
    finally:
        CQ.forget_secret_values()


def test_call_logs_land_next_to_the_attack_worlds_trace(tmp_path):
    from redteam.rounds import _write_call_logs
    d = tmp_path / "attack" / "exchange_h100_rinfo"
    d.mkdir(parents=True)
    (d / "trace.jsonl").write_text(json.dumps({"event": "header", "roster": ["A", "PVB_33"]}) + "\n")
    lp = A.AttackerLoop("PVB_33", None, "anthropic", "claude-haiku-5-5", SpendGuard(1.0, live=False), role="honest")
    lp.call_log.append({"event": "call", "agent": "PVB_33", "note": f"key {KEY}"})
    _write_call_logs(tmp_path / "attack", {"PVB_33": lp}, {"run_name": "r"}, {"backend": "live", "route": "course"})
    rows = [json.loads(x) for x in (d / "llm_calls.jsonl").read_text().splitlines()]
    assert rows[0]["event"] == "header" and rows[0]["llm_strategy"]["chooser_by_agent"] == {"A": "code",
                                                                                            "PVB_33": "llm"}
    assert KEY not in (d / "llm_calls.jsonl").read_text()       # redacted by its shape


def test_call_logs_never_overwrite_the_victims_log(tmp_path):
    from redteam.rounds import _write_call_logs
    d = tmp_path / "attack" / "exchange_h100_rinfo"
    d.mkdir(parents=True)
    (d / "trace.jsonl").write_text(json.dumps({"event": "header", "roster": ["A", "PVB_33"]}) + "\n")
    (d / "llm_calls.jsonl").write_text("victims\n")
    lp = A.AttackerLoop("PVB_33", None, "anthropic", "claude-haiku-5-5", SpendGuard(1.0, live=False))
    lp.call_log.append({"event": "call", "agent": "PVB_33"})
    _write_call_logs(tmp_path / "attack", {"PVB_33": lp}, {"run_name": "r"}, {"backend": "fake"})
    assert (d / "llm_calls.jsonl").read_text() == "victims\n" and (d / "attacker_llm_calls.jsonl").is_file()


def test_a_cap_hit_is_written_to_the_call_log_and_fails_the_route_check(tmp_path):
    from bench.course_route_check import check, load_patterns
    from redteam.rounds import _write_call_logs

    class Sess:
        n_pause, state, _go = 1, "paused", False

        def resume(self):
            pass

    class Ext:
        session = Sess()

    lp = A.AttackerLoop("PVB_33", None, "anthropic", "claude-haiku-5-5", SpendGuard(0.0, live=False), role="honest")

    class GW:
        def tools(self):
            return {}

    lp.turn(GW(), None, Ext(), {"t_utc": "2023-01-12T15:00:00Z", "reason": "registration"})
    assert lp.done and lp.call_log[-1]["cap"] == "reached"
    d = tmp_path / "run" / "attack" / "exchange_h100_rinfo"
    d.mkdir(parents=True)
    (d / "trace.jsonl").write_text(json.dumps({"event": "header", "roster": ["A", "PVB_33"]}) + "\n")
    _write_call_logs(tmp_path / "run" / "attack", {"PVB_33": lp}, {"run_name": "r"},
                     {"backend": "live", "route": "course", "seed": 1})
    t = tmp_path / "terms.txt"
    t.write_text("Zq" + "neverhere\n")
    rep = check(tmp_path / "run", load_patterns(t))
    assert rep["verdict"] == "FAIL" and any("cap was reached" in f for f in rep["fails"])


def test_a_live_round_refuses_an_untracked_or_edited_preregistration(monkeypatch, tmp_path):
    _course_env(monkeypatch)
    p = L.REPO / "docs" / "plans" / "zz_untracked_prereg_test.md"
    p.write_text("not committed\n")
    try:
        with pytest.raises(ValueError, match="committed"):
            L.backend_factory({"backend": "live", "route": "course",
                               "preregistration": "docs/plans/zz_untracked_prereg_test.md"})
    finally:
        p.unlink()
    assert L._tracked("docs/plans/single_llm_prereg.md")


def test_each_worlds_agent_statuses_are_written_beside_it():
    import inspect
    from redteam import rounds as R
    from redteam import world as W
    assert '"roster_status": dict(r.get("roster_status") or {})' in inspect.getsource(W.run_world)
    assert '"roster_status": w["roster_status"]' in inspect.getsource(R._round_body)


def test_thresholds_read_as_text_by_yaml_are_numbers_or_refused_before_any_world():
    import yaml
    from redteam.rounds import run_round
    from redteam.triage import triage
    th = yaml.safe_load("t: {gain_usd: 1.0e9, harm_usd: 0.01}")["t"]
    assert isinstance(th["gain_usd"], str)                      # YAML 1.1: 1.0e9 is text
    w = {"value": {"A": 1.0, "B": 2.0}, "trace": None, "peak_mw": 1.0}
    import redteam.triage as T
    orig = T.roster_of
    T.roster_of = lambda _t: ["A", "B"]
    try:
        assert triage(w, w, ["A"], th)["candidate"] is False
    finally:
        T.roster_of = orig
    with pytest.raises(ValueError, match="thresholds must be numbers"):
        run_round({"type": "open_ended", "thresholds": {"gain_usd": "lots"}}, "unused", None)
