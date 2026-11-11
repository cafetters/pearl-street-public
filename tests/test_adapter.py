"""Tests for the data adapter (sim/data.py) and the bench loader it feeds. Offline: synthetic parquet in tmp_path."""
import os
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sim import data as D

REAL_DATA = Path(__file__).resolve().parents[2] / "data"
PREP_NPZ_KEYS = {"ids", "ts", "price_rt", "price_da", "system_load", "net_disp",
                 "load", "pv", "curt", "bess", "soc", "soh", "pnet"}


def make_data(d: Path, start_utc: str, steps: int, ids=("B", "A", "C"), drop=None, dup=False, gap=None,
              sys_drop=None):
    """Write tiny parquet files with the dataset's layout. pnet = 1000 * agent index (sorted) + global step."""
    ts = pd.date_range(start_utc, periods=steps, freq="15min", tz="UTC")
    order = {e: i for i, e in enumerate(sorted(ids))}
    rows = []
    for k, t in enumerate(ts):
        for e in ids:
            i = order[e]
            rows.append({"timestamp": t, "entity_id": e, "entity_type": "pv_bess" if e == "C" else "community",
                         "p_net_kw": 1000.0 * i + k, "load_kw": None if e == "C" else 10.0 + k,
                         "pv_ac_kw": 5.0, "pv_curtailed_kw": 0.0, "bess_power_true_kw": float(k % 4),
                         "bess_soc_true_percent": 50.0 + k, "bess_soh_percent": 99.0})
    df = pd.DataFrame(rows)
    if gap is not None:
        df = df[df.timestamp != ts[gap]]
    if drop is not None:
        df = df[~((df.timestamp == ts[drop[0]]) & (df.entity_id == drop[1]))]
    if dup:
        df = pd.concat([df, df[df.timestamp == ts[100]].iloc[[0]]])
    df.set_index(["timestamp", "entity_id"]).to_parquet(d / D.TS_FILE)

    sys_ts = ts if sys_drop is None else ts.delete(sys_drop)
    pd.DataFrame({"price_usd_mwh": np.arange(len(sys_ts), dtype=float), "price_da_usd_mwh": 1.0,
                  "system_load_mw": 100.0, "net_load_dispatched_mw": 90.0, "bg_load_scale": 1.0},
                 index=pd.Index(sys_ts, name="timestamp")).to_parquet(d / D.SYS_FILE)

    meta = pd.DataFrame({"entity_id": list(ids), "entity_type": "community", "bess_power_kw": 100.0,
                         "bess_energy_kwh": 200.0, "eta_inverter": 0.97, "eta_battery_rt": 0.9, "soc0": 0.5,
                         "poi_limit_kw": 1000.0, "self_discharge_per_h": 0.0, "soh0": 100.0, "peak_kw": 100.0,
                         "bus": range(len(ids))})
    meta.to_parquet(d / D.META_FILE)
    return ts


def test_central_slice_shape_and_range(tmp_path):
    make_data(tmp_path, "2023-01-01 06:00", 4 * 96)
    s = D.load_slice(tmp_path, "2023-01-02", "2023-01-03")
    assert s.shape == (3, 192)
    assert list(s.ids) == ["A", "B", "C"]
    assert str(s.ts_utc.tz) == "UTC"
    assert s.ts_utc[0] == pd.Timestamp("2023-01-02 06:00", tz="UTC")
    assert s.ts_utc[-1] == pd.Timestamp("2023-01-04 05:45", tz="UTC")
    # rows land in the right (agent, step): slice starts at global step 96
    assert s.agent["pnet"][1, 0] == 1000 + 96
    assert s.agent["pnet"][2, -1] == 2000 + 96 + 191
    assert list(s.meta.entity_id) == ["A", "B", "C"]
    assert all(v.shape == (3, 192) for v in s.agent.values())
    assert all(v.shape == (192,) for v in s.system.values())


def test_dst_day_has_92_steps(tmp_path):
    make_data(tmp_path, "2023-03-11 06:00", 4 * 96)
    s = D.load_slice(tmp_path, "2023-03-12", "2023-03-12")
    assert s.shape[1] == 92
    assert s.ts_utc[0] == pd.Timestamp("2023-03-12 06:00", tz="UTC")
    assert s.ts_utc[-1] == pd.Timestamp("2023-03-13 04:45", tz="UTC")


@pytest.mark.parametrize("kwargs, match", [
    ({"drop": (100, "B")}, "missing rows"),
    ({"dup": True}, "duplicate"),
    ({"gap": 120}, "contiguous"),
    ({"sys_drop": 150}, "line up"),
])
def test_bad_input_raises(tmp_path, kwargs, match):
    make_data(tmp_path, "2023-01-01 06:00", 4 * 96, **kwargs)
    with pytest.raises(ValueError, match=match):
        D.load_slice(tmp_path, "2023-01-02", "2023-01-03")


def test_empty_range_raises(tmp_path):
    make_data(tmp_path, "2023-01-01 06:00", 96)
    with pytest.raises(ValueError, match="no agent rows"):
        D.load_slice(tmp_path, "2022-06-01", "2022-06-02")


def test_null_load_becomes_zero(tmp_path):
    make_data(tmp_path, "2023-01-01 06:00", 4 * 96)
    s = D.load_slice(tmp_path, "2023-01-02", "2023-01-03")
    assert not np.isnan(s.agent["load"]).any()
    assert (s.agent["load"][2] == 0).all()          # C is the pv_bess plant
    assert s.agent["load"][0, 0] == 10 + 96


def test_to_hourly(tmp_path):
    make_data(tmp_path, "2023-01-01 06:00", 4 * 96)
    s = D.load_slice(tmp_path, "2023-01-02", "2023-01-03")
    h = D.to_hourly(s)
    assert h.shape == (3, 48) and h.dt_h == 1.0
    assert (h.ts_utc == s.ts_utc[::4]).all()
    np.testing.assert_allclose(h.agent["pnet"][0, 0], s.agent["pnet"][0, :4].mean())
    np.testing.assert_allclose(h.agent["bess"][0], 1.5)               # mean of 0, 1, 2, 3
    np.testing.assert_array_equal(h.agent["soc"], s.agent["soc"][:, ::4])
    np.testing.assert_allclose(h.system["price_rt"][1], s.system["price_rt"][4:8].mean())
    partial = replace(s, ts_utc=s.ts_utc[:6], agent={k: v[:, :6] for k, v in s.agent.items()},
                      system={k: v[:6] for k, v in s.system.items()})
    with pytest.raises(ValueError):
        D.to_hourly(partial)


def test_write_bench_inputs_matches_prep_format(tmp_path):
    make_data(tmp_path, "2023-01-01 06:00", 4 * 96)
    s = D.load_slice(tmp_path, "2023-01-02", "2023-01-03")
    out = tmp_path / "out"
    out.mkdir()
    D.write_bench_inputs(s, out)
    with np.load(out / "data.npz") as z:
        assert set(z.files) == PREP_NPZ_KEYS
        np.testing.assert_array_equal(z["pnet"], s.agent["pnet"])
        np.testing.assert_array_equal(z["price_rt"], s.system["price_rt"])
        assert pd.Timestamp(z["ts"][0]) == pd.Timestamp("2023-01-02 06:00")
    meta = pd.read_csv(out / "meta.csv")
    assert list(meta.columns) == ["entity_id", *D.META_COLS]
    assert list(meta.entity_id) == ["A", "B", "C"]


def test_bench_loader_and_dual_capture(tmp_path):
    from bench import m0

    # 2 agents x 8 steps, cut from a full local day
    make_data(tmp_path, "2023-01-02 06:00", 96, ids=("A", "B"))
    full = D.load_slice(tmp_path, "2023-01-02", "2023-01-02")
    s = replace(full, ts_utc=full.ts_utc[:8], agent={k: v[:, :8] for k, v in full.agent.items()},
                system={k: v[:8] for k, v in full.system.items()})
    cwd = os.getcwd()
    B = m0.load_bench(s)
    assert os.getcwd() == cwd
    assert (B.N, B.T) == (2, 8)
    res = m0.solve(B, peak=True)
    assert res["dual"].shape == (8,)
    assert res["dual"].sum() == pytest.approx(1.0, abs=1e-6)          # d(peak)/d(Z) = 1 spread over binding steps
    plain = m0.solve(B, peak=True, dual=False)
    assert plain["obj_recomputed"] == pytest.approx(res["obj"], rel=1e-9)
    assert B.linprog is not None and not isinstance(B.linprog, m0.Capture)


def test_load_flex_order_and_nulls(tmp_path):
    ts = pd.date_range("2023-01-02 06:00", periods=192, freq="15min", tz="UTC")
    rows = [{"timestamp": t, "entity_id": e, "dc_flexible_kw": (100.0 + k if e == "DC" else None),
             "ev_load_kw": (None if e == "DC" else 2.0 * k)} for k, t in enumerate(ts) for e in ("PV", "DC")]
    pd.DataFrame(rows).set_index(["timestamp", "entity_id"]).to_parquet(tmp_path / D.TS_FILE)
    ids, f = D.load_flex(tmp_path, "2023-01-02", "2023-01-03")
    assert list(ids) == ["DC", "PV"] and f["dc_flex"].shape == (2, 192)
    assert f["dc_flex"][0, 5] == 105.0 and (f["dc_flex"][1] == 0).all()     # null -> 0
    assert f["ev"][1, 7] == 14.0 and (f["ev"][0] == 0).all()
    pd.DataFrame(rows[:-1]).set_index(["timestamp", "entity_id"]).to_parquet(tmp_path / D.TS_FILE)
    with pytest.raises(ValueError, match="flex columns"):
        D.load_flex(tmp_path, "2023-01-02", "2023-01-03")


@pytest.mark.skipif(not (REAL_DATA / D.TS_FILE).exists(), reason="Engie data not present in ../data")
def test_real_dev_slice():
    s = D.load_slice(REAL_DATA, "2023-01-02", "2023-01-03")
    assert s.shape == (80, 192)
    assert s.ts_utc[0] == pd.Timestamp("2023-01-02 06:00", tz="UTC")
    assert s.ts_utc[-1] == pd.Timestamp("2023-01-04 05:45", tz="UTC")
    a = s.agent
    assert np.abs(a["pv"] - a["curt"] + a["bess"] - a["load"] - a["pnet"]).max() < 0.01
    assert not any(np.isnan(v).any() for v in a.values())
