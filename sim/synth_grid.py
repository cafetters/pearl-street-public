"""The M2c synthetic grid (D35: zone machinery is unit-tested on a small synthetic grid, not on Engie's).

All numbers here are made up and disclosed; nothing comes from Engie's data.

    bus 0 (110 kV, slack) --tA--> bus 1 (20 kV) --l14--> bus 4
                          --tB--> bus 2 (20 kV)
                                  bus 1 --l13--> bus 3 <--l23-- bus 2

- tA and tB are the two transformer zones. Bus 3 is fed from both, so asset B3 sits in both zones (overlap).
- l14 is the thin zone: radial, one asset (B4).
- Background load has a daily evening peak (17:00-21:00, an hour's ramp each side) that pushes tA and tB above 90%
  for at least 4 h a day. The batteries' sign-correct capacity covers the relief (D31's 0.70 share).
- `undersized=True` triples bus 4's peak load, so l14's relief need exceeds B4's capacity (the undersized case:
  the zone price should reach its cap and the remaining violation is reported).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from sim import grid as G

SLACK_BUS = 0
STEPS_PER_DAY = 96
VN = {0: 110.0, 1: 20.0, 2: 20.0, 3: 20.0, 4: 20.0}
TRAFOS = {"tA": (0, 1, 14.0), "tB": (0, 2, 14.0)}                       # name: (hv bus, lv bus, sn_mva)
TRAFO_VK, TRAFO_VKR = 10.0, 0.0
LINES = {"l13": (1, 3, 0.4), "l23": (2, 3, 0.4), "l14": (1, 4, 0.09)}   # name: (from, to, max_i_ka)
LINE_X_OHM_PER_KM = 0.4
MONITORED = ["tA", "tB", "l14"]
THIN = "l14"
ASSET_BUS = {"B1": 1, "B2": 2, "B3": 3, "B4": 4}
ASSET_KW = {"B1": 4000.0, "B2": 4000.0, "B3": 6000.0, "B4": 1500.0}     # battery power; energy = 4 h of it
ASSET_HOURS = 4.0
BASE_LOAD_MW = {1: 6.0, 2: 6.0, 3: 4.0, 4: 1.0}
PEAK_ADD_MW = {1: 3.0, 2: 3.0, 3: 3.0, 4: 2.0}
PEAK_HOURS = (17, 21)
UNDERSIZED_BUS4_FACTOR = 3.0


def branch_table() -> pd.DataFrame:
    """Same columns as sim.grid.branches_from_pp(pandapower_net()), built without pandapower."""
    rows = []
    for k, (f, t, imax) in LINES.items():
        rows.append({"key": k, "frm": f, "to": t, "x": G._x_line(LINE_X_OHM_PER_KM, 1.0, 1, VN[f]),
                     "rating_mva": float(np.sqrt(3) * VN[f] * imax), "kv": VN[f]})
    for k, (hv, lv, sn) in TRAFOS.items():
        rows.append({"key": k, "frm": hv, "to": lv, "x": G._x_trafo(TRAFO_VK, TRAFO_VKR, sn, 1),
                     "rating_mva": sn, "kv": VN[hv]})
    df = pd.DataFrame(rows)
    df["x"] = df.x.astype(float)
    return df


def buses() -> np.ndarray:
    return np.array(sorted(VN))


def ptdf_matrix() -> np.ndarray:
    return G.ptdf(branch_table(), buses(), SLACK_BUS)


def pandapower_net():
    import pandapower as pp
    net = pp.create_empty_network(sn_mva=G.BASE_MVA)
    for b in sorted(VN):
        pp.create_bus(net, vn_kv=VN[b], index=b, name=f"bus{b}")
    pp.create_ext_grid(net, SLACK_BUS, vm_pu=1.0)
    for k, (f, t, imax) in LINES.items():
        pp.create_line_from_parameters(net, f, t, length_km=1.0, r_ohm_per_km=0.0, x_ohm_per_km=LINE_X_OHM_PER_KM,
                                       c_nf_per_km=0.0, max_i_ka=imax, name=k)
    for k, (hv, lv, sn) in TRAFOS.items():
        pp.create_transformer_from_parameters(net, hv, lv, sn_mva=sn, vn_hv_kv=VN[hv], vn_lv_kv=VN[lv],
                                              vkr_percent=TRAFO_VKR, vk_percent=TRAFO_VK, pfe_kw=0.0,
                                              i0_percent=0.0, name=k)
    return net


def load_profile_mw(T: int, undersized: bool = False) -> np.ndarray:
    """(T, buses) background load, MW. Peak block PEAK_HOURS with a one-hour linear ramp on each side."""
    h = (np.arange(T) % STEPS_PER_DAY) / 4.0
    a, b = PEAK_HOURS
    shape = np.clip(np.minimum(h - (a - 1), (b + 1) - h), 0.0, 1.0)
    out = np.zeros((T, len(VN)))
    for bus, base in BASE_LOAD_MW.items():
        add = PEAK_ADD_MW[bus] * (UNDERSIZED_BUS4_FACTOR if (undersized and bus == 4) else 1.0)
        out[:, bus] = base + add * shape
    return out


def baseline_injections(T: int, undersized: bool = False) -> np.ndarray:
    """(T, buses) MW injections with every battery idle: minus the background load (slack column balances)."""
    return -load_profile_mw(T, undersized)


def flows_mw(inj: np.ndarray) -> pd.DataFrame:
    br = branch_table()
    return pd.DataFrame(inj @ ptdf_matrix().T, columns=br.key)


def dc_loading(inj: np.ndarray) -> pd.DataFrame:
    br = branch_table()
    return flows_mw(inj).abs() / br.rating_mva.to_numpy()[None, :] * 100


def relief_needed_mw(inj: np.ndarray) -> dict:
    """Worst-step MW over 90% of rating, per monitored element."""
    rating = branch_table().set_index("key").rating_mva
    f = flows_mw(inj).abs()
    return {k: float(max(0.0, (f[k] - G.LIMIT_PCT / 100 * rating[k]).max())) for k in MONITORED}


def zone_rows() -> dict:
    return G.zone_rows(ptdf_matrix(), branch_table(), buses(), ASSET_BUS, MONITORED)


def sign_correct_capacity_mw() -> dict:
    """Per element: sum over assets of power x |PTDF| for assets whose discharge (more injection) lowers |flow| in
    the baseline peak direction."""
    rows = zone_rows()
    f = flows_mw(baseline_injections(STEPS_PER_DAY))
    out = {}
    for k in MONITORED:
        direction = np.sign(f[k].iloc[int(f[k].abs().idxmax())])
        out[k] = float(sum(ASSET_KW[a] / 1e3 * abs(p) for a, p in rows[k].items() if p * direction < 0))
    return out


# ---- a run on the synthetic grid (bench/m2c_synth.py, tests/test_m2c_run.py) ----
START_UTC = "2023-01-02 06:00"          # local (Central) midnight
ZONE_CAP_USD_MWH = 300.0                # D68: disclosed made-up operator cap for synthetic tests


PRICE_SHAPES = ("midday", "cheap_peak")


def run_slice(days: int = 2, prices: str = "cheap_peak"):
    """A sim.data.Slice with the four batteries (no load or PV of their own; background load is in the base flows).
    prices "cheap_peak": day-ahead and real-time prices are low in the evening peak, so acting alone the batteries
    charge into the congestion (a stress case). "midday": prices 5 $/MWh higher from 11:00 to 14:00 (local), away from
    the evening congestion, so acting alone the batteries cycle around midday and the evening overload is the
    background load's, which their sign-correct capacity covers (the designed case). Arm C needs a nonzero day-ahead
    spread (its starting rho is spread-based), so the designed case is not flat.)"""
    if prices not in PRICE_SHAPES:
        raise ValueError(f"prices must be one of {PRICE_SHAPES}")
    from sim.data import META_COLS, Slice
    T = STEPS_PER_DAY * days
    ts = pd.date_range(START_UTC, periods=T, freq="15min", tz="UTC")
    hour = np.asarray(ts.tz_convert("America/Chicago").hour)
    if prices == "cheap_peak":
        price = (40.0 - 25.0 * ((hour >= PEAK_HOURS[0]) & (hour < PEAK_HOURS[1]))).astype(float)
    else:
        price = (40.0 + 5.0 * ((hour >= 11) & (hour < 14))).astype(float)
    ids = np.array(sorted(ASSET_BUS))
    n = len(ids)
    z = np.zeros((n, T))
    sys_load = load_profile_mw(T).sum(axis=1)
    meta = pd.DataFrame({"entity_id": ids, "entity_type": "pv_bess",
                         "bess_power_kw": [ASSET_KW[a] for a in ids],
                         "bess_energy_kwh": [ASSET_KW[a] * ASSET_HOURS for a in ids], "eta_inverter": 0.97,
                         "eta_battery_rt": 0.9, "soc0": 0.5, "poi_limit_kw": 1e6, "self_discharge_per_h": 0.0,
                         "soh0": 100.0, "peak_kw": np.nan, "bus": [ASSET_BUS[a] for a in ids]})
    assert set(META_COLS) <= set(meta.columns)
    return Slice(ids=ids, ts_utc=ts, dt_h=0.25,
                 agent={"load": z.copy(), "pv": z.copy(), "curt": z.copy(), "bess": z.copy(),
                        "soc": np.full((n, T), 50.0), "soh": np.full((n, T), 100.0), "pnet": z.copy()},
                 system={"price_rt": price, "price_da": price.copy(), "system_load": sys_load, "net_disp": sys_load,
                         "bg_load_scale": np.ones(T)},
                 meta=meta)


def iso(ts) -> list:
    return [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in ts]


def grid_zones(s, cap_usd_mwh: float = ZONE_CAP_USD_MWH, undersized: bool = False) -> list:
    """The Exchange's grid_zones for a run slice: rows keyed by asset id (<agent>/bess), limit 0.9 x rating, base
    flow from the background load per step, and the operator's cap."""
    T = len(s.ts_utc)
    f = flows_mw(baseline_injections(T, undersized))
    rating = branch_table().set_index("key").rating_mva
    rows = zone_rows()
    keys = iso(s.ts_utc)
    return [{"key": k, "rows": {f"{a}/bess": v for a, v in rows[k].items()},
             "limit_mw": G.LIMIT_PCT / 100 * float(rating[k]),
             "base_mw": dict(zip(keys, f[k].to_numpy(float).tolist())), "cap_usd_mwh": float(cap_usd_mwh)}
            for k in MONITORED]


def injections_from_run(r, ids, undersized: bool = False) -> np.ndarray:
    """(T, buses) MW injections for a run's final schedule: background load plus each battery's site net
    (discharge - charge, kW; no load or PV of their own)."""
    net_kw = np.asarray(r["D"]) - np.asarray(r["C"])
    T = net_kw.shape[1]
    inj = baseline_injections(T, undersized)
    for i, a in enumerate(ids):
        inj[:, ASSET_BUS[a]] += net_kw[i] / 1000.0
    return inj


def ac_loading(net_kw: np.ndarray, ids, undersized: bool = False) -> pd.DataFrame:
    """AC loading (%) of the monitored elements per step with pandapower: background load at each bus plus each
    battery's site net (kW, + = export) at its bus. Raises if a step does not converge (fail closed)."""
    import pandapower as pp
    net = pandapower_net()
    T = net_kw.shape[1]
    load = load_profile_mw(T, undersized)
    li = {b: pp.create_load(net, b, p_mw=0.0) for b in BASE_LOAD_MW}
    gi = {a: pp.create_sgen(net, ASSET_BUS[a], p_mw=0.0) for a in ids}
    out = np.zeros((T, len(MONITORED)))
    lines = {k: i for i, k in zip(net.line.index, net.line.name)}
    trafos = {k: i for i, k in zip(net.trafo.index, net.trafo.name)}
    for t in range(T):
        for b, i in li.items():
            net.load.at[i, "p_mw"] = load[t, b]
        for j, a in enumerate(ids):
            net.sgen.at[gi[a], "p_mw"] = net_kw[j, t] / 1000.0
        pp.runpp(net, numba=False)
        for c, k in enumerate(MONITORED):
            out[t, c] = (net.res_line.loading_percent[lines[k]] if k in lines
                         else net.res_trafo.loading_percent[trafos[k]])
    return pd.DataFrame(out, columns=MONITORED)
