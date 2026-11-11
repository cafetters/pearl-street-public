"""nomination_span (owner, Nov 5): how far a full nomination call's window reaches (agents/operator.py)."""
import json

import numpy as np
import pandas as pd
import pytest

from agents.operator import GridOperator, parse_nomination_span
from exchange import messages as M
from sim.data import Slice

TS = pd.date_range("2023-01-02 06:00", periods=192, freq="15min", tz="UTC")    # two Central days, Jan 2 and 3
ISO = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in TS]
END = "2023-01-04T06:00:00Z"
DAY0_END = 96                                                                  # Jan 3 00:00 Central


def operator(**kw):
    args = dict(rounds="info", nominations=True, idle_net=np.zeros(192), price_mode=True)
    args.update(kw)
    return GridOperator("OP", ISO, END, TS.tz_convert("America/Chicago"), "run", "SYS", 1e5, 2.5e4, 500.0, **args)


def call_windows(op):
    """(deadline step, binding, window start step, window end step) for every call the operator sends."""
    out, fc = [], np.full(192, 1e6)
    for k in range(192):
        for raw in op.act(k, fc):
            m = M.decode(raw)
            if m["msg_type"] == "call_nominations":
                w = m["payload"]["window"]
                end = 192 if w["end_utc"] == END else ISO.index(w["end_utc"])
                out.append((ISO.index(m["payload"]["deadline_utc"]), m["payload"]["binding"], ISO.index(w["start_utc"]),
                            end))
    return out


def test_default_and_explicit_horizon_are_today_s_windows():
    default, explicit = call_windows(operator()), call_windows(operator(nomination_span="horizon"))
    assert default == explicit and default
    assert all(end == 192 for _, _, _, end in default)         # every call covers the rest of the horizon


def test_operating_day_ends_at_the_call_s_operating_day():
    op = operator(nomination_span="operating_day")
    wins = call_windows(op)
    deadlines_day0 = {d for day in op.days[:1] for _, d, _ in day["calls"]}
    for deadline, _, start, end in wins:
        assert start == deadline
        assert end == (DAY0_END if deadline in deadlines_day0 else 192)
    assert any(end == DAY0_END for *_, end in wins) and any(end == 192 for *_, end in wins)
    # day 1's calls go out on day 0 and still cover through day 1's end: every step has a binding call
    covered = set()
    for _, binding, start, end in wins:
        if binding:
            covered |= set(range(start, end))
    first_binding = min(d for d, binding, *_ in wins if binding)   # as under "horizon": nothing binds before it
    assert covered >= set(range(first_binding, 192))


def test_hours_span_and_clipping():
    wins = call_windows(operator(nomination_span=6))
    assert wins and all(end == min(start + 24, 192) for _, _, start, end in wins)
    wins = call_windows(operator(nomination_span=0.1))           # under one step: one step
    assert all(end == start + 1 for _, _, start, end in wins)


@pytest.mark.parametrize("bad", ["day", "Horizon", "", 0, -3, float("nan"), float("inf"), True, None, [24]])
def test_bad_values_are_refused(bad):
    with pytest.raises(ValueError):
        parse_nomination_span(bad)
    with pytest.raises(ValueError):
        operator(nomination_span=bad)


def test_run_m1_refuses_a_span_with_d62_and_defaults_to_operating_day():
    """D86's flip (Nov 6): absent means operating_day, so a D62 config must write horizon or it is refused."""
    from sim.run_m1 import nomination_span
    assert nomination_span({}) == "operating_day"
    assert nomination_span({"d62_guarantee": True, "nomination_span": "horizon"}) == "horizon"
    with pytest.raises(ValueError, match="d62_guarantee"):
        nomination_span({"d62_guarantee": True})
    assert nomination_span({"nomination_span": 12}) == 12
    for span in ("operating_day", 12):
        with pytest.raises(ValueError, match="d62_guarantee"):
            nomination_span({"nomination_span": span, "d62_guarantee": True})
    with pytest.raises(ValueError):
        nomination_span({"nomination_span": "week"})


@pytest.mark.parametrize("arm_a", [{"prep_clearing": True}, {"no_rebound": True},
                                   {"prep_clearing": True, "no_rebound": True, "firm_offers": True}])
def test_run_m1_refuses_a_span_for_arm_a(arm_a):
    from sim.run_m1 import nomination_span
    with pytest.raises(ValueError, match="arm A"):           # D86's flip: absent is operating_day, refused here
        nomination_span(arm_a)
    assert nomination_span({**arm_a, "nomination_span": "horizon"}) == "horizon"
    for span in ("operating_day", 12):
        with pytest.raises(ValueError, match="arm A"):
            nomination_span({**arm_a, "nomination_span": span})
    assert nomination_span({"price_mode": True, "nomination_span": "operating_day"}) == "operating_day"   # arm C


def test_run_variant_refuses_arm_a_before_any_work(tmp_path):
    from sim.run_m1 import run_variant
    from tests.test_replay import CFG_M11
    with pytest.raises(ValueError, match="arm A"):
        run_variant({**CFG_M11, "prep_clearing": True, "nomination_span": "operating_day"}, two_day_slice(),
                    np.arange(2), tmp_path / "a", headroom_mw=0.03)
    assert not (tmp_path / "a").exists()


def two_day_slice() -> Slice:
    from tests.test_replay import synthetic_slice
    s = synthetic_slice()
    rep = lambda a: np.concatenate([a, a], axis=-1)          # noqa: E731
    return Slice(ids=s.ids, ts_utc=TS, dt_h=s.dt_h, agent={k: rep(v) for k, v in s.agent.items()},
                 system={k: rep(v) for k, v in s.system.items()}, meta=s.meta)


def trace_lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_end_to_end_price_mode_operating_day(tmp_path):
    from sim.run_m1 import run_variant
    from tests.test_replay import CFG_M11
    cfg = {**CFG_M11, "rounds": "info", "price_mode": True, "plan_to_nomination": True,
           "release_superseded_lock": True, "price_rounds_per_gate": 5}
    s = two_day_slice()
    runs = {}
    for name, extra in (("absent", {}), ("horizon", {"nomination_span": "horizon"}),
                        ("day", {"nomination_span": "operating_day"})):
        out = tmp_path / name
        runs[name] = run_variant({**cfg, **extra}, s, np.arange(2), out, headroom_mw=0.03)
        runs[name]["_trace"] = trace_lines(out / "exchange_h0.03_rinfo" / "trace.jsonl")
    # D86's flip (Nov 6): absent is operating_day, the same run as the explicit setting (the config line in the
    # header is the only difference); explicit horizon is today's full-horizon run and its header has no span key
    assert runs["absent"]["_trace"][1:] == runs["day"]["_trace"][1:]
    assert json.loads(runs["absent"]["_trace"][0])["nomination_span"] == "operating_day"
    assert "nomination_span" not in json.loads(runs["horizon"]["_trace"][0])
    assert runs["horizon"]["_trace"][1:] != runs["day"]["_trace"][1:]
    hdr = json.loads(runs["day"]["_trace"][0])
    assert hdr["nomination_span"] == "operating_day"
    calls = [m["payload"] for m in runs["day"]["log"] if m["msg_type"] == "call_nominations" and m["sender"] == "exchange"]
    assert {c["window"]["end_utc"] for c in calls} == {ISO[DAY0_END], END}
    # bus.log keeps nominate and telemetry without payload (fix 1); the trace has every message in full
    noms = [json.loads(x)["payload"] for x in runs["day"]["_trace"][1:] if json.loads(x)["msg_type"] == "nominate"]
    by_call = {c["call_id"]: c for c in calls}
    assert noms and all(n["window"] == by_call[n["call_id"]]["window"] for n in noms)
    short = [n for n in noms if n["window"]["end_utc"] == ISO[DAY0_END]]
    assert short and all(len(n["kw"]) < 192 for n in short)
    assert any(m["msg_type"] == "peak_settle" for m in runs["day"]["log"])
