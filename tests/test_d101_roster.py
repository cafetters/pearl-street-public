"""D101 (owner, Nov 19; built Nov 23): a rejected or missing registration leaves that agent out of the market and the
run goes on. The agent stays on the roster with a status; its battery acts alone; the exchange gets nothing from or
about it. Rule 10: a check covers an excluded agent only by reporting it as excluded. Synthetic slices only."""
import json

import numpy as np
import pytest

import sim.run_m1 as R
from bench import roster_cover as RC
from redteam.synth import BASE_CFG, HEADROOM_MW, synthetic_slice


class _Wrap:
    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, k):
        return getattr(self.inner, k)


class NoRegister(_Wrap):
    """Sends everything but its register."""
    def start(self, k):
        return [r for r in self.inner.start(k) if json.loads(r)["msg_type"] != "register"]


class WrongOwner(_Wrap):
    """Registers under a legal entity the metering service did not attest (D69 rule 6: refused)."""
    def start(self, k):
        out = []
        for r in self.inner.start(k):
            m = json.loads(r)
            if m["msg_type"] == "register":
                m["payload"]["legal_entity"] = "Someone Else LLC (simulated)"
                r = json.dumps(m)
            out.append(r)
        return out


def _run(tmp_path, kind, monkeypatch, name):
    orig = R.wrap_attacker

    def wrap(spec, agent, cfg):
        if spec.get("type") == "test_no_register":
            return NoRegister(agent)
        if spec.get("type") == "test_wrong_owner":
            return WrongOwner(agent)
        return orig(spec, agent, cfg)
    monkeypatch.setattr(R, "wrap_attacker", wrap)
    cfg = {**BASE_CFG, "run_name": name, "attackers": [{"agent": "B", "type": kind}]}
    return R.run_variant(cfg, synthetic_slice(), np.arange(3), tmp_path / name, headroom_mw=HEADROOM_MW)


def _from(r, aid):
    """What the agent sent after its step-0 start (register and subscribe go out before the exclusion)."""
    t0 = r["log"][0]["t_utc"]
    return [m for m in r["log"] if str(m.get("sender")) == aid
            and not (m["t_utc"] == t0 and m["msg_type"] in ("register", "subscribe"))]


def _about(r, asset):
    """What the metering service told the exchange about the asset after step 0's paperwork (asset_limits and
    reference_net go out before anyone registers, so before the exclusion is known)."""
    return [m for m in r["log"] if m["sender"] == "metering" and (m.get("payload") or {}).get("asset_id") == asset
            and m["msg_type"] in ("telemetry", "selfish_reference", "modeled_response", "idle_reference")]


def test_never_registered_agent_is_left_out_and_the_run_finishes(tmp_path, monkeypatch):
    r = _run(tmp_path, "test_no_register", monkeypatch, "silent")
    assert r["roster_status"] == {"A": "active", "B": "excluded: never registered", "C": "active"}
    assert r["roster_excluded_reason"] == {}
    assert _from(r, "B") == [] and _about(r, "B/bess") == []
    assert not any(m["msg_type"] in ("settle", "peak_settle", "imbalance") and m["payload"].get("agent_id") == "B"
                   for m in r["log"])
    assert any(m["msg_type"] == "nominate" and m["sender"] == "A" for m in r["log"])      # the others still trade


def test_rejected_registration_is_left_out_with_its_code(tmp_path, monkeypatch):
    r = _run(tmp_path, "test_wrong_owner", monkeypatch, "rejected")
    assert r["roster_status"]["B"] == "excluded: registration rejected"
    code = r["roster_excluded_reason"]["B"]
    rej = [m["payload"]["code"] for m in r["log"] if m["msg_type"] == "reject" and m["to"] == "B"]
    assert rej and code == rej[0]
    assert _from(r, "B") == [] and _about(r, "B/bess") == []
    assert r["roster_status"]["A"] == r["roster_status"]["C"] == "active"


def test_excluded_agent_acts_alone(tmp_path, monkeypatch):
    r = _run(tmp_path, "test_no_register", monkeypatch, "alone_x")
    cfg = {**BASE_CFG, "run_name": "alone_s"}
    sf = R.run_variant(cfg, synthetic_slice(), np.arange(3), tmp_path / "alone_s")
    for key in ("C", "D", "X", "SOC"):
        assert np.array_equal(r[key][1], sf[key][1]), key


def test_net_harm_reports_excluded_agents():
    nh = R.net_harm(np.zeros(3), np.zeros(3), ["A", "B", "C"], ["A", "B", "C"], 0,
                    excluded={"B": "excluded: never registered"})
    assert nh["excluded_agents"] == {"B": "excluded: never registered"} and nh["pass"]
    assert "excluded_agents" not in R.net_harm(np.zeros(1), np.zeros(1), ["A"], ["A"], 0)


def test_roster_cover():
    h = {"roster": ["A", "B", "C"]}
    assert RC.excluded(h) == {}                                   # a run before D101: all active
    assert RC.coverage(h, ["A", "B"])["missing"] == ["C"]
    h2 = {**h, "roster_status": {"A": "active", "B": "excluded: never registered", "C": "active"}}
    cov = RC.coverage(h2, ["A", "C"])
    assert cov == {"excluded": {"B": "excluded: never registered"}, "missing": []}
    assert RC.coverage(h2, ["A"])["missing"] == ["C"]
    with pytest.raises(RC.RosterError):
        RC.require_all_active(h2, "some check")
    RC.require_all_active(h, "some check")
    for bad in ({**h, "roster_status": {"A": "active", "B": "gone", "C": "active"}},
                {**h, "roster_status": {"A": "active", "B": "active"}},
                {**h, "roster_status": {"A": "active", "B": "active", "C": "active", "D": "active"}},
                {"roster": []}):
        with pytest.raises(RC.RosterError):
            RC.statuses(bad)


def test_bystander_check_reports_and_matches_exclusions(tmp_path):
    from bench import bystander_check as B

    def write(d, attackers, status):
        d.mkdir()
        hdr = {"git_commit": "abc", "roster": ["A", "B"], "attackers": attackers}
        (d / "exchange_h1").mkdir()
        (d / "exchange_h1" / "trace.jsonl").write_text(json.dumps(hdr) + "\n")
        ev = {"net_harm": {"by_agent_usd": {"A": 0.0, "B": 0.0}}, "agent_value_usd": {"A": 1.0, "B": 1.0}}
        (d / "summary.json").write_text(json.dumps({"header": {"roster": ["A", "B"], "roster_status": status},
                                                    "exchange_by_headroom_mw": {"1": ev}}))
    st = {"A": "active", "B": "excluded: never registered"}
    write(tmp_path / "atk", [{"agent": "A"}], st)
    write(tmp_path / "twin", [], st)
    out = B.check(tmp_path / "atk", tmp_path / "twin")
    assert out["excluded_agents"] == {"B": "excluded: never registered"} and out["verdict"] == "PASS"
    write(tmp_path / "twin2", [], {"A": "active", "B": "active"})
    with pytest.raises(B.CheckFailed):
        B.check(tmp_path / "atk", tmp_path / "twin2")


def test_guard_fails_closed_on_summary_and_trace_headers(tmp_path):
    h = {"roster": ["A", "B"]}
    RC.guard(h, "t")                                              # before D101: passes
    RC.guard({**h, "roster_status": {"A": "active", "B": "active"}}, "t")
    with pytest.raises(RC.RosterError):
        RC.guard({**h, "roster_status": {"A": "active", "B": "excluded: never registered"}}, "t")
    with pytest.raises(RC.RosterError):
        RC.guard({**h, "data_centers_excluded": ["B"]}, "t")      # a trace header with data centers off
    run = tmp_path / "run"
    (run / "exchange_h100_rinfo").mkdir(parents=True)
    trace = run / "exchange_h100_rinfo" / "trace.jsonl"
    trace.write_text(json.dumps(h) + "\n")
    (run / "summary.json").write_text(json.dumps({"header": {**h, "roster_status": {"A": "active", "B": "active"}}}))
    RC.guard(h, "t", trace)
    (run / "summary.json").write_text(json.dumps(
        {"header": {**h, "roster_status": {"A": "active", "B": "excluded: registration rejected"}}}))
    with pytest.raises(RC.RosterError):                           # registration exclusions live only in the summary
        RC.guard(h, "t", trace)
    with pytest.raises(RC.RosterError):
        RC.guard(h, "t", run)
    (run / "summary.json").write_text(json.dumps({"variants": []}))   # a summary without a header: nothing to check
    RC.guard(h, "t", trace)


def test_validators_not_built_for_exclusions_fail_closed(tmp_path):
    from bench import m4_rep_table, m4_trigger_rates
    run = tmp_path / "run"
    (run / "exchange_h100_rinfo").mkdir(parents=True)
    trace = run / "exchange_h100_rinfo" / "trace.jsonl"
    trace.write_text(json.dumps({"roster": ["A", "B"], "data_centers_excluded": ["B"]}) + "\n")
    (run / "summary.json").write_text(json.dumps(
        {"header": {"roster": ["A", "B"], "roster_status": {"A": "active", "B": "excluded: data centers off (setting)"}}}))
    with pytest.raises(RC.RosterError):
        m4_rep_table._load(run)                                   # reads the summary header
    with pytest.raises(RC.RosterError):
        m4_trigger_rates.count_events(trace)                      # reads the trace header
