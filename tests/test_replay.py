"""End to end on a synthetic day: the simulator loop writes a trace, and offline replay reproduces the exchange."""
import json

import numpy as np
import pandas as pd

from exchange.trace.replay import replay
from sim.data import META_COLS, Slice
from sim.run_m1 import run_variant

CFG = {"run_name": "test", "zone": "SYS", "operator_id": "GRID_OP", "metering_id": "metering", "clock_id": "clock",
       "layer_mw": 0.05, "price_cap_usd_mwh": 500.0, "post_hour_local": 9, "gate_lead_h": 1,
       "price_publish_hour_local": 12, "forecast_noise_sigma": 0.0, "seed": 1, "min_offer_kw": 5.0,
       "wear_usd_mwh": 10.0, "nominations": False, "rounds": 1}
CFG_M11 = {**CFG, "nominations": True, "rounds": 2, "nomination_lead_h": 1}


def synthetic_slice() -> Slice:
    ts = pd.date_range("2023-01-02 06:00", periods=96, freq="15min", tz="UTC")     # one Central day
    hour = np.asarray(ts.tz_convert("America/Chicago").hour)
    n = 2
    sys_load = 1000 + 300 * np.exp(-((hour - 19) ** 2) / 4)                         # MW, evening peak
    price = 20 + 10 * (hour < 6)                                                    # cheap evenings: selfish charges into the peak
    z = np.zeros((n, 96))
    meta = pd.DataFrame({"entity_id": ["A", "B"], "entity_type": "pv_bess", "bess_power_kw": [100.0, 60.0],
                         "bess_energy_kwh": [400.0, 240.0], "eta_inverter": 0.97, "eta_battery_rt": 0.9,
                         "soc0": 0.5, "poi_limit_kw": 1000.0, "self_discharge_per_h": 0.0, "soh0": 100.0,
                         "peak_kw": np.nan, "bus": [1, 2]})
    assert set(META_COLS) <= set(meta.columns)
    return Slice(ids=np.array(["A", "B"]), ts_utc=ts, dt_h=0.25,
                 agent={"load": z.copy(), "pv": z.copy(), "curt": z.copy(), "bess": z.copy(),
                        "soc": np.full((n, 96), 50.0), "soh": np.full((n, 96), 100.0), "pnet": z.copy()},
                 system={"price_rt": price.astype(float), "price_da": price.astype(float), "system_load": sys_load,
                         "net_disp": sys_load, "bg_load_scale": np.ones(96)},
                 meta=meta)


def test_trace_header_and_exact_replay(tmp_path):
    s = synthetic_slice()
    r = run_variant(CFG, s, np.arange(2), tmp_path, headroom_mw=0.1)
    path = tmp_path / "exchange_h0.1" / "trace.jsonl"
    lines = path.read_text().splitlines()
    header = json.loads(lines[0])
    assert header["event"] == "header" and header["seed"] == 1 and header["git_commit"]
    assert header["config"]["run_name"] == "test" and header["exchange"]["operator_id"] == "GRID_OP"
    types = {json.loads(l)["msg_type"] for l in lines[1:]}
    assert {"register", "post_need", "offer", "award", "telemetry", "settle", "tick"} <= types

    res = replay(path)
    assert res["match"], res
    assert res["summary"]["awards"] > 0 and res["summary"]["settlements"] == res["summary"]["awards"]

    # drop one exchange output: replay must notice
    first_out = next(i for i, l in enumerate(lines[1:], 1) if json.loads(l)["from"] == "exchange")
    broken = tmp_path / "broken.jsonl"
    broken.write_text("\n".join(lines[:first_out] + lines[first_out + 1:]) + "\n")
    assert not replay(broken)["match"]


def test_selfish_run_sends_no_exchange_traffic(tmp_path):
    r = run_variant(CFG, synthetic_slice(), np.arange(2), tmp_path)
    lines = [json.loads(l) for l in (tmp_path / "selfish" / "trace.jsonl").read_text().splitlines()[1:]]
    assert {l["msg_type"] for l in lines} == {"telemetry"}
    assert all(l["event"] == "dropped" for l in lines)


def test_m1_1_rounds_replay_and_only_the_total_reaches_the_operator(tmp_path):
    s = synthetic_slice()
    r = run_variant(CFG_M11, s, np.arange(2), tmp_path, headroom_mw=0.1)
    path = tmp_path / "exchange_h0.1_r2" / "trace.jsonl"
    lines = [json.loads(l) for l in path.read_text().splitlines()]
    assert lines[0]["exchange"]["require_nominations"] is True and lines[0]["rounds"] == 2
    events = lines[1:]
    types = {e["msg_type"] for e in events}
    assert {"call_nominations", "nominate", "nomination_aggregate", "imbalance"} <= types
    assert all("net_kw" in e["payload"] for e in events if e["msg_type"] == "telemetry")
    to_op = [e for e in events if e["to"] == "GRID_OP" or (isinstance(e["to"], list) and "GRID_OP" in e["to"])]
    assert "nominate" not in {e["msg_type"] for e in to_op}                 # never an individual schedule
    assert {e["msg_type"] for e in to_op} <= {"nomination_aggregate", "award", "settle"}
    calls = {e["msg_id"]: e["payload"] for e in events if e["msg_type"] == "call_nominations" and e["from"] == "GRID_OP"}
    assert sorted((c["round"], c["binding"]) for c in calls.values()) == [(1, False), (2, False), (3, True)]
    aggs = [e["payload"] for e in events if e["msg_type"] == "nomination_aggregate"]
    assert len(aggs) == 3 and all(a["n_agents"] == 2 for a in aggs)
    assert replay(path)["match"]


def test_info_rounds_charge_honest_agents_no_imbalance(tmp_path):
    cfg = {**CFG_M11, "rounds": "info"}
    run_variant(cfg, synthetic_slice(), np.arange(2), tmp_path, headroom_mw=0.1)
    path = tmp_path / "exchange_h0.1_rinfo" / "trace.jsonl"
    events = [json.loads(l) for l in path.read_text().splitlines()[1:]]
    imb = [e["payload"] for e in events if e["msg_type"] == "imbalance"]
    assert imb and sum(p["charge_usd"] for p in imb) == 0.0
    binding = [e["payload"] for e in events if e["msg_type"] == "call_nominations" and e["from"] == "GRID_OP" and e["payload"]["binding"]]
    assert len(binding) == 1 and binding[0]["window"]["end_utc"] == "2023-01-03T06:00:00Z"
    assert replay(path)["match"]
