"""M4 step 11: launcher additions (plan test 14): the LLM guard, the cost estimate before every --llm queue, phase-cap
allocation (D94), staged shared-cache inputs (D95), redaction of provider key shapes, and --secret-env."""
import secrets
import sys
from pathlib import Path

import pytest

from bench import cloud_queue as Q

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_cloud_queue as T  # noqa: E402  the fake Maritime harness

ONE = [{"provider": "anthropic", "model": "claude-haiku-5-5", "weight": 1.0}]


def test_llm_guard():
    assert Q.llm_reasons({"run_name": "x"}) == []
    assert Q.llm_reasons({"llm_strategy": {"chooser": "rule"}}) == []           # code-only arm: no --llm needed
    assert Q.llm_reasons({"llm_strategy": {"chooser": "llm"}}) == ["llm_strategy.chooser=llm"]
    assert Q.llm_reasons({"llm_strategy": {"chooser": "rule", "per_agent": {"A": "llm"}}}) == \
        ["llm_strategy.per_agent=llm"]
    assert Q.llm_reasons({"llm_model": "x"}) == ["llm_model"]                 # any other llm key, as before


def test_estimate_refuses_a_worst_case_above_the_run_cap():
    cfg = {"run_name": "a", "start": "2023-01-12", "end": "2023-01-13",
           "llm_strategy": {"chooser": "llm", "cap_usd_run": 0.0001, "honest_models": ONE}}
    with pytest.raises(Q.QueueError):
        Q.llm_estimates([cfg], n_agents=80, log=lambda s: None)
    cfg["llm_strategy"]["cap_usd_run"] = 5.0
    lines = []
    est = Q.llm_estimates([cfg], n_agents=80, log=lines.append)
    assert est["a"]["ok"] and any("worst" in s for s in lines)


def test_phase_cap_is_allocated_before_launch():
    cfgs = [{"run_name": f"r{i}", "llm_strategy": {"chooser": "llm", "cap_usd_run": 4.0}} for i in range(3)]
    with pytest.raises(Q.QueueError):
        Q.check_phase(cfgs, phase_cap=10.0, phase_spent=0.0)
    assert Q.check_phase(cfgs, phase_cap=12.0, phase_spent=0.0) == pytest.approx(12.0)
    with pytest.raises(Q.QueueError):
        Q.check_phase(cfgs, phase_cap=12.0, phase_spent=0.5)
    with pytest.raises(Q.QueueError):     # an LLM run with no cap never launches
        Q.check_phase([{"run_name": "x", "llm_strategy": {"chooser": "llm"}}], phase_cap=12.0, phase_spent=0.0)


def test_cache_inputs_are_staged(tmp_path):
    name = "h2h_c_x_jan12_silent_m5"
    rec = {"llm_strategy": {"chooser": "llm", "cache": {"mode": "record",
                                                        "path": f"runs/{name}/llm_cache.jsonl"}}}
    assert Q.llm_cache_context(rec, name, tmp_path) == []
    with pytest.raises(Q.QueueError):      # a recorded cache must sit inside the run folder, or it is never downloaded
        Q.llm_cache_context({"llm_strategy": {"chooser": "llm", "cache": {"mode": "record", "path": "runs/c.jsonl"}}},
                            name, tmp_path)
    twin = "h2h_c_x_jan12_twin_m5"
    rt = {"llm_strategy": {"chooser": "llm", "cache": {"mode": "read_through",
                                                       "path": f"runs/{twin}/llm_cache.jsonl"}}}
    with pytest.raises(Q.QueueError):      # the twin's cache must already be downloaded
        Q.llm_cache_context(rt, name, tmp_path)
    (tmp_path / twin).mkdir()
    (tmp_path / twin / "llm_cache.jsonl").write_text("")
    assert Q.llm_cache_context(rt, name, tmp_path) == [(tmp_path / twin / "llm_cache.jsonl",
                                                       f"{twin}/llm_cache.jsonl")]


def test_a_read_through_run_and_its_recording_twin_cannot_share_a_queue():
    twin = "t1"
    cfgs = [{"run_name": twin, "llm_strategy": {"cache": {"mode": "record", "path": f"runs/{twin}/llm_cache.jsonl"}}},
            {"run_name": "a1", "llm_strategy": {"cache": {"mode": "read_through",
                                                          "path": f"runs/{twin}/llm_cache.jsonl"}}}]
    with pytest.raises(Q.QueueError):
        Q.check_cache_stages(cfgs)
    Q.check_cache_stages(cfgs[:1])
    Q.check_cache_stages(cfgs[1:])


def test_redact_covers_provider_key_shapes():
    k1 = "sk-" + "ant-" + "api03-" + "Ab" * 20
    k2 = "sk-" + "proj-" + "Cd" * 22
    out = Q.redact(f"error {k1} and {k2} done")
    assert k1 not in out and k2 not in out and out.startswith("error ")


# ---- --secret-env (owner decision 6, Nov 12): env map in the data-plane POST /commands, confirmed with a dummy ----

def _dummy():
    return "psdummy-" + secrets.token_hex(12)       # non-secret, random per test; never a literal key in the repo


def test_secret_env_checks_fail_closed(monkeypatch):
    assert Q.check_secret_env([]) == {}
    monkeypatch.delenv("PS_DUMMY_SECRET", raising=False)
    with pytest.raises(Q.QueueError):                       # not set locally
        Q.check_secret_env(["PS_DUMMY_SECRET"])
    monkeypatch.setenv("PS_DUMMY_SECRET", "")
    with pytest.raises(Q.QueueError):                       # empty
        Q.check_secret_env(["PS_DUMMY_SECRET"])
    with pytest.raises(Q.QueueError):                       # not an env var name
        Q.check_secret_env(["bad name"])
    monkeypatch.setenv("PS_DUMMY_SECRET", "a&b")
    with pytest.raises(Q.QueueError):                       # would pass through cmd.exe
        Q.check_secret_env(["PS_DUMMY_SECRET"])
    v = _dummy()
    monkeypatch.setenv("PS_DUMMY_SECRET", v)
    try:
        assert Q.check_secret_env(["PS_DUMMY_SECRET"]) == {"PS_DUMMY_SECRET": v}
        assert v not in Q.redact(f"error text with {v} inside")    # every launcher error and log line is redacted
    finally:
        Q.forget_secret_values()


def test_secret_env_reaches_only_the_run_stage_and_no_file_or_log(monkeypatch):
    v = _dummy()
    monkeypatch.setenv("PS_DUMMY_SECRET", v)
    try:
        with T.Env() as env:
            rc = env.main(*T.runs_args(env, ["r1"]), *T.BASE, "--concurrency", "1", "--budget", "1",
                          "--queue-id", "q-sec", "--secret-env", "PS_DUMMY_SECRET")
            rep = env.report("q-sec")
            assert rc == 0, rep
            # the value goes only in the body of one data-plane POST (HTTPS), never on a maritime.cmd command line
            assert env.cloud.http_env == [(["bash", f"{Q.JOB}/stage.sh", "run"], {"PS_DUMMY_SECRET": v}, True)]
            run_sh = env.cloud.sbx["sbx-fake0001"]["files"][f"{Q.JOB}/run.sh"].decode()
            assert "PS_DUMMY_SECRET" in run_sh and v not in run_sh        # presence check by name only
            for c in env.cloud.calls:                                    # no CLI call (or logged HTTP line) carries it
                assert v not in " ".join(map(str, c)), c
            for f in env.tmp.rglob("*"):
                if f.is_file():
                    assert v.encode() not in f.read_bytes(), f
            assert rep["secret_env"] == ["PS_DUMMY_SECRET"]
    finally:
        Q.forget_secret_values()


def test_a_run_whose_output_carries_a_secret_value_fails(monkeypatch):
    v = _dummy()
    monkeypatch.setenv("PS_DUMMY_SECRET", v)
    try:
        with T.Env(T.FakeCloud(run_log_extra=f"oops {v}\n".encode())) as env:
            rc = env.main(*T.runs_args(env, ["r1"]), *T.BASE, "--concurrency", "1", "--budget", "1",
                          "--queue-id", "q-leak", "--secret-env", "PS_DUMMY_SECRET")
            rep = env.report("q-leak")
            assert rc != 0 and rep["runs"][0]["status"] != "ok"
            assert "secret" in " ".join(rep["runs"][0].get("errors") or [])
            assert not (env.tmp / "runs" / "cloud" / "r1").exists()            # never placed as a result
            for f in env.tmp.rglob("*"):
                if f.is_file():
                    assert v.encode() not in f.read_bytes(), f
    finally:
        Q.forget_secret_values()


def test_llm_queues_install_the_pinned_sdks_and_code_only_queues_do_not():
    llm = ("{chooser: llm, backend: fake, cap_usd_run: 5.0, honest_models: "
           "[{provider: anthropic, model: claude-haiku-5-5, weight: 1.0}]}")
    with T.Env() as env:
        rc = env.main("--run", env.config("r1", llm_strategy=llm), env.m0(), *T.BASE, "--concurrency", "1",
                      "--budget", "1", "--queue-id", "q-llm", "--llm", "--phase-cap-usd", "10")
        assert rc == 0, env.report("q-llm")
        setup = env.cloud.sbx["sbx-fake0001"]["files"][f"{Q.JOB}/setup.sh"].decode()
        for pin in Q.LLM_PINS:
            assert pin in setup
        assert setup.index("pip install --no-input -q -e .") < setup.index(Q.LLM_PINS[0])
    with T.Env() as env:
        assert env.main(*T.runs_args(env, ["r1"]), *T.BASE, "--concurrency", "1", "--budget", "1",
                        "--queue-id", "q-co") == 0
        setup = env.cloud.sbx["sbx-fake0001"]["files"][f"{Q.JOB}/setup.sh"].decode()
        assert not any(p in setup for p in Q.LLM_PINS)


def test_secret_env_refuses_cli_polls(monkeypatch):
    monkeypatch.setenv("PS_DUMMY_SECRET", _dummy())
    try:
        with T.Env() as env:
            rc = env.main(*T.runs_args(env, ["r1"]), *T.BASE, "--concurrency", "1", "--budget", "1",
                          "--queue-id", "q-secli", "--secret-env", "PS_DUMMY_SECRET", "--cli-polls")
            assert rc != 0 and not env.cloud.sbx                 # refused before any sandbox
    finally:
        Q.forget_secret_values()
