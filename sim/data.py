"""Data adapter: reads the Engie dataset in ../data and hands out per-agent arrays in UTC.

This is the only place that reads Engie files (CLAUDE.md, PLAN.md section 3). Everything else gets
arrays from here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import yaml

REPO = Path(__file__).resolve().parents[1]
STEP = pd.Timedelta("15min")

TS_FILE = "agents_timeseries_2023_15min.parquet"
SYS_FILE = "system_prices_and_load_2023_15min.parquet"
META_FILE = "agents_metadata.parquet"
PF_FILE = "grid_powerflow_results_2023.parquet"
GRID_FILE = "grid_topology_pandapower.json"
LINES_FILE = "grid_topology_lines.parquet"

# Same columns, same names as bench/prep.py, so the bench LPs can run on a slice unchanged.
AGENT_COLS = {
    "load": "load_kw", "pv": "pv_ac_kw", "curt": "pv_curtailed_kw", "bess": "bess_power_true_kw",
    "soc": "bess_soc_true_percent", "soh": "bess_soh_percent", "pnet": "p_net_kw",
}
SYSTEM_COLS = {
    "price_rt": "price_usd_mwh", "price_da": "price_da_usd_mwh", "system_load": "system_load_mw",
    "net_disp": "net_load_dispatched_mw", "bg_load_scale": "bg_load_scale",
}
META_COLS = ["entity_type", "bess_power_kw", "bess_energy_kwh", "eta_inverter", "eta_battery_rt", "soc0",
             "poi_limit_kw", "self_discharge_per_h", "soh0", "peak_kw"]
BENCH_SYSTEM_KEYS = ["price_rt", "price_da", "system_load", "net_disp"]


@dataclass(frozen=True)
class Slice:
    ids: np.ndarray                  # (N,) entity ids, sorted
    ts_utc: pd.DatetimeIndex         # (T,) interval starts, tz=UTC
    dt_h: float                      # step length in hours
    agent: dict[str, np.ndarray]     # name -> (N, T) float64
    system: dict[str, np.ndarray]    # name -> (T,) float64
    meta: pd.DataFrame               # one row per id, same order: entity_id, META_COLS, bus

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.ids), len(self.ts_utc)


@dataclass(frozen=True)
class Grid:
    buses: pd.DataFrame      # bus, vn_kv
    lines: pd.DataFrame      # line, from_bus, to_bus, length_km, x_ohm_per_km, parallel, rating_mva
    trafos: pd.DataFrame     # trafo, hv_bus, lv_bus, sn_mva, vk_percent, vkr_percent, parallel
    loads: pd.DataFrame      # bus, p_mw (background loads, before bg_load_scale)
    gens: pd.DataFrame       # bus, p_mw (gen and sgen)
    slack_bus: int
    agents: pd.DataFrame     # entity_id, entity_type, bus


def load_config(path: str | Path) -> dict:
    """Read a run config. data_dir is resolved against the repo root."""
    cfg = yaml.safe_load(Path(path).read_text())
    cfg["data_dir"] = str((REPO / cfg["data_dir"]).resolve())
    return cfg


def local_range(start, end, tz: str = "America/Chicago") -> tuple[pd.Timestamp, pd.Timestamp]:
    """Local calendar days [start, end] (end inclusive) -> UTC [t0, t1)."""
    t0 = pd.Timestamp(start).tz_localize(tz)
    t1 = (pd.Timestamp(end) + pd.Timedelta(days=1)).tz_localize(tz)
    return t0.tz_convert("UTC"), t1.tz_convert("UTC")


def _read_range(path: Path, columns: list[str], t0: pd.Timestamp, t1: pd.Timestamp) -> pd.DataFrame:
    tbl = pq.read_table(path, columns=columns, filters=[("timestamp", ">=", t0), ("timestamp", "<", t1)])
    return tbl.to_pandas(ignore_metadata=True)


def load_slice(data_dir: str | Path, start, end, tz: str = "America/Chicago") -> Slice:
    """Per-agent and system arrays for local days [start, end], 15-min steps, UTC timestamps."""
    data_dir = Path(data_dir)
    t0, t1 = local_range(start, end, tz)
    expected = pd.date_range(t0, t1, freq=STEP, inclusive="left").as_unit("ns")

    df = _read_range(data_dir / TS_FILE, ["timestamp", "entity_id", *AGENT_COLS.values()], t0, t1)
    if df.empty:
        raise ValueError(f"no agent rows between {t0} and {t1}")
    if df.duplicated(["timestamp", "entity_id"]).any():
        raise ValueError("duplicate (timestamp, entity_id) rows")
    ts = pd.DatetimeIndex(df.timestamp.unique()).as_unit("ns").sort_values()
    if not ts.equals(expected):
        raise ValueError(f"timestamps are not contiguous 15-min steps over [{t0}, {t1})")
    ids = np.array(sorted(df.entity_id.unique()))
    n, T = len(ids), len(expected)
    if len(df) != n * T:
        raise ValueError(f"missing rows: {len(df)} rows for {n} agents x {T} steps")
    df = df.sort_values(["entity_id", "timestamp"])
    agent = {k: df[c].to_numpy(np.float64).reshape(n, T) for k, c in AGENT_COLS.items()}
    agent["load"] = np.nan_to_num(agent["load"])   # load is null for pv_bess plants (prep.py: coalesce(load, 0))

    sysdf = _read_range(data_dir / SYS_FILE, ["timestamp", *SYSTEM_COLS.values()], t0, t1).sort_values("timestamp")
    if not pd.DatetimeIndex(sysdf.timestamp).as_unit("ns").equals(expected):
        raise ValueError("system table does not line up with the agent table")
    system = {k: sysdf[c].to_numpy(np.float64) for k, c in SYSTEM_COLS.items()}

    m = pd.read_parquet(data_dir / META_FILE).set_index("entity_id")
    missing = set(ids) - set(m.index)
    if missing:
        raise ValueError(f"agents missing from metadata: {sorted(missing)}")
    meta = m.loc[ids, [*META_COLS, "bus"]].reset_index()
    return Slice(ids=ids, ts_utc=expected, dt_h=0.25, agent=agent, system=system, meta=meta)


FLEX_COLS = {"dc_flex": "dc_flexible_kw", "ev": "ev_load_kw"}


def load_flex(data_dir: str | Path, start, end, tz: str = "America/Chicago") -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Per-agent flexible load for local days [start, end], (N, T) kW, same id and step order as load_slice:
    dc_flex = dc_flexible_kw (load that could be postponed right now), ev = ev_load_kw (unmanaged home EV
    charging, deferrable up to max_defer_steps_ev). Null (agents without that asset) -> 0."""
    t0, t1 = local_range(start, end, tz)
    T = len(pd.date_range(t0, t1, freq=STEP, inclusive="left"))
    df = _read_range(Path(data_dir) / TS_FILE, ["timestamp", "entity_id", *FLEX_COLS.values()], t0, t1)
    ids = np.array(sorted(df.entity_id.unique()))
    if len(df) != len(ids) * T or df.duplicated(["timestamp", "entity_id"]).any():
        raise ValueError(f"flex columns: {len(df)} rows for {len(ids)} agents x {T} steps")
    df = df.sort_values(["entity_id", "timestamp"])
    return ids, {k: np.nan_to_num(df[c].to_numpy(np.float64)).reshape(len(ids), T) for k, c in FLEX_COLS.items()}


def to_hourly(s: Slice) -> Slice:
    """Hourly means of powers and prices. soc takes the first step of each hour."""
    n, T = s.shape
    if s.dt_h != 0.25 or T % 4 or s.ts_utc[0].minute:
        raise ValueError("to_hourly needs a 15-min slice of whole hours starting on the hour")
    agent = {k: (v[:, ::4] if k == "soc" else v.reshape(n, T // 4, 4).mean(2)) for k, v in s.agent.items()}
    system = {k: v.reshape(T // 4, 4).mean(1) for k, v in s.system.items()}
    return replace(s, ts_utc=s.ts_utc[::4], dt_h=1.0, agent=agent, system=system)


def write_bench_inputs(s: Slice, out_dir: str | Path) -> None:
    """Write data.npz and meta.csv in exactly the format bench/prep.py produces."""
    out_dir = Path(out_dir)
    s.meta[["entity_id", *META_COLS]].to_csv(out_dir / "meta.csv", index=False)
    ts = s.ts_utc.tz_convert("UTC").tz_localize(None).to_numpy("datetime64[ns]").astype(np.int64)
    np.savez_compressed(out_dir / "data.npz", ids=s.ids, ts=ts,
                        **{k: s.system[k] for k in BENCH_SYSTEM_KEYS}, **s.agent)


def _pp_table(net: dict, name: str) -> pd.DataFrame:
    t = net[name]
    t = t["_object"] if isinstance(t, dict) else t
    t = json.loads(t) if isinstance(t, str) else t
    return pd.DataFrame(t["data"], index=t["index"], columns=t["columns"])


def load_grid(data_dir: str | Path) -> Grid:
    """Topology from the pandapower json (same ids as the parquet tables), plus the agent -> bus map."""
    data_dir = Path(data_dir)
    net = json.loads((data_dir / GRID_FILE).read_text())["_object"]
    bus = _pp_table(net, "bus").rename_axis("bus").reset_index()
    line = _pp_table(net, "line").rename_axis("line").reset_index()
    trafo = _pp_table(net, "trafo").rename_axis("trafo").reset_index()
    line = line[line.in_service].merge(pd.read_parquet(data_dir / LINES_FILE)[["line", "rating_mva"]], on="line")
    trafo = trafo[trafo.in_service]
    load = _pp_table(net, "load")
    gens = pd.concat([_pp_table(net, "gen"), _pp_table(net, "sgen")])
    ext = _pp_table(net, "ext_grid")
    if len(ext) != 1:
        raise ValueError(f"expected one ext_grid, found {len(ext)}")
    agents = pd.read_parquet(data_dir / META_FILE, columns=["entity_id", "entity_type", "bus"])
    return Grid(
        buses=bus[["bus", "vn_kv"]],
        lines=line[["line", "from_bus", "to_bus", "length_km", "x_ohm_per_km", "parallel", "rating_mva"]],
        trafos=trafo[["trafo", "hv_bus", "lv_bus", "sn_mva", "vk_percent", "vkr_percent", "parallel"]],
        loads=load.loc[load.in_service, ["bus", "p_mw"]].reset_index(drop=True),
        gens=gens.loc[gens.in_service, ["bus", "p_mw"]].reset_index(drop=True),
        slack_bus=int(ext.bus.iloc[0]),
        agents=agents.sort_values("entity_id").reset_index(drop=True),
    )


def load_pp_net(data_dir: str | Path):
    """The grid as a pandapower network (the file needs pandapower >= 3.3)."""
    import pandapower as pp
    return pp.from_json(str(Path(data_dir) / GRID_FILE))


def load_powerflow(data_dir: str | Path, columns: list[str] | None = None) -> pd.DataFrame:
    """Hourly power-flow results for the given columns (all if None), indexed by UTC timestamp."""
    df = pd.read_parquet(Path(data_dir) / PF_FILE, columns=columns)
    return df.sort_index()
