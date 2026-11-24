"""M2b round 2 as revised by the owner (Nov 20; docs/plans/m2b_round2b_prereg.md): the operator's daily targets
rebuilt from the config's schedule; (f) do no harm at a 100 MW cut against the target within the measured band;
(g) the depth test at the with-deferral ceiling's cut; the payout split, report only. Synthetic inputs only."""
import csv
import json

import numpy as np
import pandas as pd
import pytest

from bench import m2b_judge as J

CFG = {"operator_id": "GRID_OP", "zone": "SYS", "layer_mw": 25, "price_cap_usd_mwh": 500, "post_hour_local": 9,
       "gate_lead_h": 1, "nomination_lead_h": 1, "price_publish_hour_local": 12, "nominations": True, "rounds": "info",
       "price_mode": True, "release_superseded_lock": True, "plan_to_nomination": True, "nomination_span": "horizon",
       "headroom_mw": [100]}


def _netload(d, fc_mw, ex_mw=None):
    ts = pd.date_range("2023-01-12 06:00", periods=len(fc_mw), freq="15min", tz="UTC")
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "netload.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_utc", "t_central", "recorded_kw", "operator_forecast_kw", "selfish_kw", "exchange_h100_rinfo_kw",
                    "exchange_h100_rinfo_in_window"])
        for i, t in enumerate(ts):
            w.writerow([t.strftime("%Y-%m-%dT%H:%M:%SZ"), t.tz_convert("America/Chicago").strftime("%Y-%m-%d %H:%M"),
                        0, fc_mw[i] * 1e3, 0, (ex_mw[i] if ex_mw is not None else 0) * 1e3, True])


def test_day_targets_follow_the_operators_gates(tmp_path):
    """Day 0 counts from its first gate (01:00 local, one hour after the slice starts); day 1 counts every step (its
    gate falls on day 0). A forecast spike at 00:30 on day 0 is not eligible; one at 00:30 on day 1 is."""
    fc = np.full(192, 2000.0)
    fc[2] = 2600.0            # day 0, 00:30 local: before the first gate
    fc[96 + 2] = 2500.0       # day 1, 00:30 local: eligible
    fc[50] = 2300.0
    _netload(tmp_path / "r", fc)
    t = J.day_targets(tmp_path / "r", CFG)
    assert t["2023-01-12"] == pytest.approx(2300.0 - 100.0)
    assert t["2023-01-13"] == pytest.approx(2500.0 - 100.0)


def test_no_harm_is_judged_against_the_target_or_the_off_twin_within_the_band():
    # target 2300; off at 2302; on within 6 MW of the larger
    assert J.no_harm(on_mw=2307.9, off_mw=2302.0, target_mw=2300.0)["pass"]
    assert not J.no_harm(on_mw=2308.0000001, off_mw=2302.0, target_mw=2300.0)["pass"]
    # batteries can't reach the target: off misses by 80 MW; on judged against off
    r = J.no_harm(on_mw=2384.0, off_mw=2380.0, target_mw=2300.0)
    assert r["pass"] and r["reference"] == "off"


def test_depth_is_judged_only_where_the_ceilings_differ_by_more_than_the_band():
    r = J.depth(on_mw=2230.0, off_mw=2231.0, gap_mw=22.0)
    assert r["judged"] and r["pass"]
    assert not J.depth(on_mw=2231.0, off_mw=2231.0, gap_mw=22.0)["pass"]           # equal is not lower
    small = J.depth(on_mw=2232.0, off_mw=2231.0, gap_mw=2.1)
    assert not small["judged"] and small["pass"] is None


def test_payout_split_reads_peak_settle(tmp_path):
    ex = tmp_path / "run" / "exchange_h100_rinfo"
    ex.mkdir(parents=True)
    msgs = [{"event": "header"},
            {"msg_type": "peak_settle", "to": "DC_01", "payload": {"net_usd": 5.0, "refund_usd": 9.0, "charge_usd": 4.0}},
            {"msg_type": "peak_settle", "to": "DC_01", "payload": {"net_usd": 1.0, "refund_usd": 2.0, "charge_usd": 1.0}},
            {"msg_type": "peak_settle", "to": "PVB_01", "payload": {"net_usd": 3.0, "refund_usd": 3.0, "charge_usd": 0.0}}]
    (ex / "trace.jsonl").write_text("\n".join(json.dumps(m) for m in msgs) + "\n")
    s = J.payout_split(tmp_path / "run")
    assert s["data_centers"] == {"net_usd": 6.0, "refund_usd": 11.0, "charge_usd": 5.0}
    assert s["others"]["net_usd"] == 3.0
