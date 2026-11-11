"""Grid-operator agent (agents/operator.py)."""
import numpy as np
import pandas as pd
import pytest

from agents.operator import GridOperator, layers
from exchange import messages as M


def test_layers_cover_the_forecast_above_target():
    fc = np.array([0, 10, 40, 95, 100, 60, 30, 0], float)
    steps = np.arange(len(fc))
    out = layers(fc, steps, headroom_kw=75, layer_kw=25)
    target = fc.max() - 75
    need = np.zeros(len(fc))
    for a, b, kw in out:
        need[a:b] += kw
    over = np.maximum(fc - target, 0)
    assert need[4] == pytest.approx(75)                         # the peak gets the full headroom
    assert (need >= over - 1e-9).all() and (need < over + 25).all()
    assert all(b > a for a, b, _ in out)


def test_layers_split_non_contiguous_windows():
    fc = np.array([100, 0, 100], float)
    assert layers(fc, np.arange(3), 10, 10) == [(0, 1, 10.0), (2, 3, 10.0)]


def test_post_and_gate_times():
    ts = pd.date_range("2023-01-02 06:00", periods=192, freq="15min", tz="UTC")
    iso = list(ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
    op = GridOperator("OP", iso, "2023-01-04T06:00:00Z", ts.tz_convert("America/Chicago"), "run", "SYS",
                      headroom_kw=50, layer_kw=25, price_cap=500, post_hour=9, gate_lead_steps=4)
    (p1, g1, d1), (p2, g2, d2) = op.schedule
    assert (p1, g1) == (0, 4)                                    # day 1: first step
    assert ts.tz_convert("America/Chicago")[p2].strftime("%m-%d %H:%M") == "01-02 09:00"   # day 2: 09:00 the day before
    assert g2 == p2 + 4
    fc = np.zeros(192)
    fc[2] = 1000                                                 # before day 1's gate: must not be in a window
    fc[50] = 900
    out = [M.decode(r) for r in op.act(0, fc)]
    assert out and all(m["payload"]["window"]["start_utc"] >= m["payload"]["gate_utc"] for m in out)
    assert all(M.validate(m) is None and m["to"] == "exchange" for m in out)
    assert op.act(1, fc) == []


def two_day_operator(rounds, nominations=True):
    ts = pd.date_range("2023-01-02 06:00", periods=192, freq="15min", tz="UTC")
    iso = list(ts.strftime("%Y-%m-%dT%H:%M:%SZ"))
    op = GridOperator("OP", iso, "2023-01-04T06:00:00Z", ts.tz_convert("America/Chicago"), "run", "SYS",
                      headroom_kw=50, layer_kw=25, price_cap=500, post_hour=9, gate_lead_steps=4, rounds=rounds,
                      nominations=nominations, nom_lead_steps=4, idle_net=np.zeros(192))
    return op, iso


def test_round_schedule():
    op, _ = two_day_operator(rounds=3)
    d = op.days[0]
    assert d["gates"] == [4, 12, 20]
    assert d["calls"] == [(0, 4, False), (4, 8, False), (12, 16, False), (20, 24, True)]
    m1, _ = two_day_operator(rounds=1, nominations=False)
    assert m1.days[0]["calls"] == [] and m1.days[0]["gates"] == [4]
    with pytest.raises(ValueError):
        two_day_operator(rounds=2, nominations=False)


def test_reforecast_and_residual_round():
    op, iso = two_day_operator(rounds=2)
    fc = np.full(192, 1000.0)
    fc[60] = 1100.0                               # idle forecast peak on day 1: target = 1050
    out = [M.decode(r) for r in op.act(0, fc)]
    assert {m["msg_type"] for m in out} == {"post_need", "call_nominations"}
    assert all(m["payload"]["round"] == 1 for m in out if m["msg_type"] == "post_need")
    call2 = [M.decode(r) for r in op.act(4, fc) if M.decode(r)["msg_type"] == "call_nominations"]
    assert len(call2) == 1 and call2[0]["payload"]["deadline_utc"] == iso[8]
    cid = call2[0]["msg_id"]
    win = call2[0]["payload"]["window"]
    n = 96 - 8
    kw = np.zeros(n)
    kw[30 - 8] = -80.0                             # the herd charges 80 kW at step 30
    op.receive(M.encode(M.make("nomination_aggregate", "exchange", "OP", iso[8],
                               {"call_id": cid, "zone": "SYS", "window": win, "kw": list(kw), "n_agents": 3,
                                "binding": False}, "ex:5", "run")))
    refc = op.reforecast(fc, {"window": win, "kw": list(kw)})
    assert refc[30] == pytest.approx(1080.0) and refc[60] == pytest.approx(1100.0)
    needs = [M.decode(r) for r in op.act(8, fc) if M.decode(r)["msg_type"] == "post_need"]
    assert needs and all(m["payload"]["round"] == 2 and m["payload"]["gate_utc"] == iso[12] for m in needs)
    starts = {m["payload"]["window"]["start_utc"] for m in needs}
    assert iso[30] in starts and iso[60] in starts              # residual covers the herd and the old peak


def test_info_schedule_waits_for_price_informed_nominations():
    op, iso = two_day_operator(rounds="info")
    d1, d2 = op.days
    # day 1: prices known at the start, so one residual round is enough
    assert d1["gates"] == [4, 12] and d1["calls"] == [(0, 4, False), (4, 8, False), (12, 16, True)]
    # day 2: posted 09:00 Jan 2 (step 36); prices publish 12:00 (step 48); the last residual call closes at 13:00
    assert d2["post"] == 36 and d2["gates"] == [40, 48, 56]
    assert d2["calls"] == [(36, 40, False), (40, 44, False), (48, 52, False), (56, 60, True)]
    assert d2["calls"][-2][1] > 48                      # last residual round uses nominations made after 12:00
    with pytest.raises(ValueError):
        two_day_operator(rounds="sometimes")


def test_binding_call_covers_the_rest_of_the_horizon():
    op, iso = two_day_operator(rounds="info")
    fc = np.full(192, 1000.0)
    op.act(0, fc)
    calls = [M.decode(r) for r in op.act(12, fc) if M.decode(r)["msg_type"] == "call_nominations"]
    assert len(calls) == 1 and calls[0]["payload"]["binding"]
    assert calls[0]["payload"]["window"] == {"start_utc": iso[16], "end_utc": "2023-01-04T06:00:00Z"}
