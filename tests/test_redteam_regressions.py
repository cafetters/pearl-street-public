"""The red-team regression suite (D61 part 2): every catalog entry in attacks/catalog is replayed deterministically.

- attributable_gain entries: with the defenses off (the entry's exploit settings) the replay must reproduce the
  exploit (fidelity); with each known defense layer on alone it must be blocked, including the gateway grant off with
  the exchange's register_power_check on (owner, Nov 18, item 2).
- liveness entries: the run must finish.
All numbers synthetic [sim, synthetic]. The replays never call an LLM."""
import json
import threading
from pathlib import Path

import numpy as np
import pytest

from attacks.replay import CATALOG_DIR, load_entry
from redteam.rounds import DEFENSES, TOL_USD, attributable, replay_world

ENTRIES = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(CATALOG_DIR.glob("rt-*.json"))]
GAIN = [e for e in ENTRIES if e.get("check", "attributable_gain") == "attributable_gain"]


def _rc(e, name):
    return {"name": name, "seed": e["world"].get("seed", 1), "world": e["world"], "flags": e["world"].get("flags", {})}


def test_catalog_ids_are_sequential_and_entries_complete():
    ids = [e["id"] for e in ENTRIES]
    assert ids == [f"rt-{i:04d}" for i in range(len(ids))] and len(ids) >= 3
    for e in ENTRIES:
        assert {"id", "title", "status", "class", "attackers", "world", "actions"} <= set(e)
        assert e.get("check", "attributable_gain") in ("attributable_gain", "liveness")


@pytest.mark.parametrize("entry", GAIN, ids=[e["id"] for e in GAIN])
def test_exploit_reproduces_with_its_defenses_off(tmp_path, entry):
    rc = _rc(entry, "fid")
    lim = entry.get("exploit_gateway_limits") or {}
    w = replay_world(rc, entry["actions"], tmp_path, limits=lim, name="exploit")
    c = replay_world(rc, entry["control"]["actions"], tmp_path, limits=lim, name="control")
    a = attributable(w, c, entry["attackers"])
    for k, v in entry["triage"]["attributable_gain_usd"].items():
        assert a[k] == pytest.approx(v, abs=TOL_USD) and a[k] > 1.0


@pytest.mark.parametrize("defense", DEFENSES, ids=[d["name"] for d in DEFENSES])
@pytest.mark.parametrize("entry", GAIN, ids=[e["id"] for e in GAIN])
def test_each_defense_layer_alone_blocks_it(tmp_path, entry, defense):
    rc = _rc(entry, f"def_{defense['name']}")
    w = replay_world(rc, entry["actions"], tmp_path, flags=defense["flags"], limits=defense["gateway_limits"],
                     name="exploit")
    if defense["name"] == "register_power_check":
        # owner item 2: gateway grant off, exchange flag on: blocked at registration. Today that stops the run
        # (the roster rule, rt-0001's class), so "blocked" here is the reject, recorded honestly as a run stop
        assert w.get("error") and "roster agents not registered" in w["error"]
        return
    c = replay_world(rc, entry["control"]["actions"], tmp_path, flags=defense["flags"],
                     limits=defense["gateway_limits"], name="control")
    a = attributable(w, c, entry["attackers"])
    assert all(v <= TOL_USD for v in a.values()), a


def test_rt0000_runs_as_a_plain_config_attack(tmp_path):
    """The catalog entry as an ordinary attacks/ type (type: replay): no harness, the run config alone."""
    from redteam.synth import BASE_CFG, HEADROOM_MW, synthetic_slice
    from sim.run_m1 import run_variant
    s = synthetic_slice()
    out = {}
    for grant in (False, True):
        cfg = {**BASE_CFG, "run_name": f"replay_{grant}", "attackers": [
            {"agent": "A", "type": "replay", "catalog": "rt-0000", "gateway_limits": {"grant_limits": grant}}]}
        r = run_variant(cfg, s, np.arange(3), tmp_path / str(grant), headroom_mw=HEADROOM_MW)
        a = r["agents"][0]
        assert a.replay["entry"] == "rt-0000"
        reg = [m for m in r["log"] if m["msg_type"] == "register" and m["sender"] == "A"]
        out[grant] = reg[0]["payload"]["mandate"]["max_kw"]
        assert {m["msg_type"] for m in r["log"] if m["sender"] == "A"} >= {"register", "subscribe", "nominate"}
    assert out == {False: 6000.0, True: 2000.0}


def test_rt0001_harness_keeps_the_run_alive_by_default_enrollment(tmp_path):
    e = load_entry("rt-0001")
    w = replay_world(_rc(e, "rt1"), e["actions"], tmp_path, name="gate")
    assert not w.get("error")
    assert [x["agent_id"] for x in w["events"] if x["event"] == "auto_registered"] == ["A"]


def test_rt0001_is_still_open_in_the_simulator(tmp_path):
    """Documents the open issue (fix proposal staged; sim/run_m1.py not edited tonight): without the harness's default
    enrollment, an agent that sends no register stops the run. When this test fails, the fix has landed: update the
    entry's status."""
    from redteam.synth import BASE_CFG, HEADROOM_MW, synthetic_slice
    from sim.run_m1 import run_variant

    class NoRegister:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, k):
            return getattr(self.inner, k)

        def start(self, k):
            return [r for r in self.inner.start(k) if '"msg_type":"register"' not in r.replace(" ", "")]

    import attacks
    orig = attacks.wrap_attacker

    def wrap(spec, agent, cfg):
        return NoRegister(agent) if spec.get("type") == "test_no_register" else orig(spec, agent, cfg)
    import sim.run_m1 as R
    R.wrap_attacker = wrap
    try:
        cfg = {**BASE_CFG, "run_name": "rt1_open", "attackers": [{"agent": "A", "type": "test_no_register"}]}
        with pytest.raises((RuntimeError, KeyError)):
            run_variant(cfg, synthetic_slice(), np.arange(3), tmp_path, headroom_mw=HEADROOM_MW)
    finally:
        R.wrap_attacker = orig


def test_rt0002_never_advancing_session_is_ended_by_the_pause_deadline(tmp_path):
    e = load_entry("rt-0002")
    w = replay_world({**_rc(e, "rt2"), "turn_cap": 30}, e["actions"], tmp_path, name="gate",
                     never_advance=e["never_advance"])
    assert not w.get("error")
    missed = [x for x in w["gateway_log"] if x["code"] == "pause_deadline_missed"]
    assert len(missed) >= 3 and all(x["agent_id"] == "A" for x in missed)


def test_rt0002_threaded_server_wall_clock_backstop(tmp_path):
    """The stdio server's threaded pause: a client that registers and then never advances. The wall-clock backstop
    (pause_wall_s) ends every pause, so the run finishes instead of freezing."""
    from iface.mcp_stdio import Server
    from redteam.synth import BASE_CFG, HEADROOM_MW, synthetic_slice
    s = synthetic_slice()
    cfg = {**BASE_CFG, "run_name": "rt2_server", "headroom_mw": HEADROOM_MW,
           "attackers": [{"agent": "A", "type": "external"}],
           "gateway": {"limits": {"pause_wall_s": 0.2}}}
    srv = Server(cfg=cfg, out_dir=tmp_path, slice_fn=lambda: (s, np.arange(3)))
    r, err = srv.call("register", {"asset_id": "A/bess", "max_kw": 2000.0})
    assert not err and r["granted_kw"] == 2000.0
    srv.call("subscribe", {"signals": ["call_nominations", "peak_price"], "zones": ["SYS"]})
    srv.call("advance", {})                               # registration done; from here on it never advances
    srv.thread.join(180.0)
    assert not srv.thread.is_alive() and srv.session.state == "finished", srv.session.error
    assert srv.status()["pauses_missed"] >= 2
    res = json.loads((tmp_path / "run_result.json").read_text())
    assert any(x["code"] == "pause_deadline_missed" for x in res["gateway_log"])


@pytest.mark.parametrize("eid", ["rt-0001", "rt-0002"])
def test_liveness_entries_under_the_redteam_flags_harm_no_bystander(tmp_path, eid):
    """Owner item 5: with D78's silent_reference on (every configs/redteam file), a silent or never-advancing
    attacker leaves no honest agent below its twin. With it off, the same agent left B and C $1.10 and $0.66 below
    their twins (the HW9 item 5 price freeze) [sim, synthetic]; recorded in rt-0001's notes."""
    from redteam.rounds import regress
    flags = {"register_power_check": True, "reference_power_cap": True, "silent_reference": True}
    g = regress(load_entry(eid), {"flags": flags}, tmp_path)
    assert g["blocked"] and g["bystanders_vs_twin"] is not None
    assert all(h <= 1e-6 for h in g["bystanders_vs_twin"]["harm_usd"].values()), g
