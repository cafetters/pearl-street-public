"""bench/m4_trigger_rates.py on a tiny synthetic trace (it also ran on the four saved v2r twin traces, Nov 12)."""
import json

import pytest

from bench import m4_trigger_rates as R


def _trace(tmp_path, roster, msgs):
    d = tmp_path / "run" / "exchange_h100_rinfo"
    d.mkdir(parents=True)
    head = {"event": "header", "roster": roster, "config": {"start": "2023-01-12", "end": "2023-01-13"}}
    (d / "trace.jsonl").write_text("\n".join(json.dumps(m) for m in [head, *msgs]) + "\n")
    return tmp_path / "run"


def _imb(to, usd):
    return {"msg_type": "imbalance", "from": "exchange", "to": to, "t_utc": "2023-01-13T06:00:00Z",
            "payload": {"charge_usd": usd}}


def test_counts_per_agent_day(tmp_path):
    msgs = [_imb("A", 12.0), _imb("B", 0.0),
            {"msg_type": "call_nominations", "from": "GRID_OP", "to": "exchange", "t_utc": "2023-01-12T15:00:00Z",
             "payload": {"binding": True}},
            {"msg_type": "peak_settle", "from": "exchange", "to": "B", "t_utc": "2023-01-14T06:00:00Z",
             "payload": {"net_usd": -50.0, "refund_usd": 100.0}}]
    res = R.rates([_trace(tmp_path, ["A", "B"], msgs)], None)
    p = res["pooled_per_agent_day"]
    assert res["agent_days"] == 4
    assert p["T3"] == pytest.approx(0.25) and p["T1"] == pytest.approx(0.25) and p["T2"] == 0.0
    assert p["T6"] == pytest.approx(0.5)


def test_fails_closed_on_a_missing_roster_agent(tmp_path):
    with pytest.raises(R.CheckFailed):
        R.rates([_trace(tmp_path, ["A", "B"], [_imb("A", 0.0)])], None)
