"""Owner item 5 (Nov 19): with several outside sessions, one session that never advances must not cost another
session the nominations it queued in the same pause [sim, synthetic].

Threaded per-agent Sessions (the hosted shape), two client threads through one Gateway, the sim in its own thread.
The quiet client registers, then never advances; the pause deadline (pause_wall_s) ends each of its pauses. The
active client reads its inbox at every pause and nominates its battery-idle net for each new call. Both pause orders
are covered: the quiet agent paused before the active one (A quiet) and after it (B quiet)."""
import threading

import numpy as np
import pytest

from iface.credentials import CredentialStore
from iface.external import Session, set_interface
from iface.gateway import Gateway
from redteam.synth import BASE_CFG, HEADROOM_MW, synthetic_slice

WALL_S = 0.3


def _idx(start: str, t: str) -> int:
    from datetime import datetime
    f = lambda x: datetime.fromisoformat(x.replace("Z", "+00:00"))
    return int(round((f(t) - f(start)).total_seconds() / 900))


def _wait_pause(sess, handled: int, timeout: float = 60.0) -> str:
    with sess.cv:
        sess.cv.wait_for(lambda: (sess.state == "paused" and sess.n_pause > handled)
                         or sess.state in ("finished", "error"), timeout)
        return sess.state


def _client(gw, sess, aid, quiet: bool, out: dict):
    if _wait_pause(sess, 0) != "paused":
        return
    cs = gw.open_session(gw.credentials.issue(gw.agents[aid].legal_entity), aid)
    facts, _ = gw.call(cs, "my_asset", {})
    gw.call(cs, "register", {"asset_id": facts["asset_id"], "max_kw": facts["power_kw"]})
    gw.call(cs, "subscribe", {"signals": ["call_nominations", "peak_price"], "zones": ["SYS"]})
    handled = sess.n_pause
    sess.resume()
    if quiet:
        return                                   # never advances again
    calls, done, noms = {}, set(), []
    while _wait_pause(sess, handled) == "paused":
        handled = sess.n_pause
        inbox, _ = gw.call(cs, "read_inbox", {})
        for m in inbox.get("messages", []):
            if m.get("msg_type") == "call_nominations":
                calls[m["payload"]["call_id"]] = m["payload"]
        facts, _ = gw.call(cs, "my_asset", {})
        idle, start = facts["battery_idle_site_net_kw"], facts["window"]["start_utc"]
        for cid, c in calls.items():
            if cid in done or c["deadline_utc"] <= facts["now_utc"]:
                continue
            i0, i1 = _idx(start, c["window"]["start_utc"]), _idx(start, c["window"]["end_utc"])
            r, err = gw.call(cs, "nominate", {"call_id": cid, "zone": c["zone"], "window": c["window"],
                                              "kw": [float(v) for v in idle[i0:i1]]})
            if not err:
                noms.append({"msg_id": r["msg_id"], "t_utc": facts["now_utc"]})
            done.add(cid)
        sess.resume()
    out[aid] = noms


def _run(tmp_path, quiet: str | None):
    s = synthetic_slice()
    cfg = {**BASE_CFG, "run_name": f"multi_{quiet}", "silent_reference": True,
           "attackers": [{"agent": "A", "type": "external"}, {"agent": "B", "type": "external"}]}
    gw = Gateway(limits={"pause_wall_s": WALL_S}, credentials=CredentialStore())
    sessions = {"A": Session(), "B": Session()}
    set_interface(lambda aid: sessions[aid], gw)
    res, noms = {}, {}

    def sim():
        from sim.run_m1 import run_variant
        try:
            res["r"] = run_variant(cfg, s, np.arange(3), tmp_path, headroom_mw=HEADROOM_MW)
        except BaseException as e:      # noqa: BLE001 (reported by the assertion below)
            res["error"] = f"{type(e).__name__}: {e}"
        finally:
            for x in sessions.values():
                x.finish(res.get("error"))

    th = [threading.Thread(target=sim, daemon=True)]
    th += [threading.Thread(target=_client, args=(gw, sessions[a], a, a == quiet, noms), daemon=True)
           for a in ("A", "B")]
    try:
        for t in th:
            t.start()
        th[0].join(240.0)
    finally:
        set_interface(None, None)
    assert not th[0].is_alive(), "the run froze"
    assert "error" not in res, res.get("error")
    for t in th[1:]:
        t.join(10.0)
    return res["r"], gw, noms


@pytest.mark.parametrize("quiet,active", [("A", "B"), ("B", "A")])
def test_a_quiet_session_does_not_cost_another_its_nominations_in_the_same_pause(tmp_path, quiet, active):
    r, gw, noms = _run(tmp_path, quiet)
    missed = [x for x in gw.log if x.get("code") == "pause_deadline_missed"]
    assert missed and {x["agent_id"] for x in missed} == {quiet}         # only the quiet agent is ended
    assert all(x["discarded_sends"] == 0 for x in missed)                # it queued nothing; nothing else dropped

    mine = noms[active]
    assert mine, "the active client nominated nothing"
    log = r["log"]
    landed = {m["msg_id"] for m in log if m["msg_type"] == "nominate" and m["sender"] == active}
    rejected = {m["payload"].get("ref_msg_id") for m in log if m["msg_type"] == "reject" and m["to"] == active}
    ids = {n["msg_id"] for n in mine}
    assert ids <= landed, f"nominations lost: {sorted(ids - landed)}"
    assert not ids & rejected
    # the scenario itself: some of them were queued at a step where the quiet session missed its pause
    same = {n["t_utc"] for n in mine} & {x["t_utc"] for x in missed}
    assert same, "no pause shared by a miss and a nomination; the test did not exercise item 5"
    assert gw.agents[active].n_followed > 0
    assert not [m for m in log if m["msg_type"] == "nominate" and m["sender"] == quiet]


def test_the_active_session_dispatches_as_when_both_advance(tmp_path):
    """Its battery follows the same nominations as in the run where the other session also advances (no barrier
    cost beyond wall time)."""
    _, gw_q, _ = _run(tmp_path / "quiet", "A")
    _, gw_t, _ = _run(tmp_path / "twin", None)
    assert gw_q.agents["B"].n_followed == gw_t.agents["B"].n_followed > 0
