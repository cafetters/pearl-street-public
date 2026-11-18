"""Grid library for M2c (and M0's zone check): branch table, DC PTDF, bus injections, zones, monitored elements,
binding hours. Moved here from bench/zone_check.py (which re-exports these names so M0 reproduces).

Nothing here reads files: callers pass a sim.data.Grid or a pandapower network. exchange/ never imports this module;
sim/ turns its output into plain arrays (PTDF rows, ratings, caps) for the exchange.
"""
from __future__ import annotations

from collections import deque

import numpy as np
import pandas as pd

LIMIT_PCT = 90.0           # D10 operating limit
PTDF_MIN = 0.05            # D9 zone membership: |PTDF| at or above this
BASE_MVA = 100.0
TZ = "America/Chicago"


def _x_line(x_ohm_per_km, length_km, parallel, vn_kv):
    return np.asarray(x_ohm_per_km) * np.asarray(length_km) / np.asarray(parallel) / (np.asarray(vn_kv) ** 2 / BASE_MVA)


def _x_trafo(vk_percent, vkr_percent, sn_mva, parallel):
    return (np.sqrt(np.asarray(vk_percent) ** 2 - np.asarray(vkr_percent) ** 2) / 100 * BASE_MVA
            / np.asarray(sn_mva) / np.asarray(parallel))


def branches(g) -> pd.DataFrame:
    """Lines and trafos of a sim.data.Grid as one table: key, frm, to, x (p.u. on BASE_MVA), rating_mva, kv."""
    vn = g.buses.set_index("bus").vn_kv
    ln, tr = g.lines, g.trafos
    x_line = _x_line(ln.x_ohm_per_km, ln.length_km, ln.parallel, vn.loc[ln.from_bus].to_numpy())
    x_tr = _x_trafo(tr.vk_percent, tr.vkr_percent, tr.sn_mva, tr.parallel)
    return pd.concat([
        pd.DataFrame({"key": "l" + ln.line.astype(str), "frm": ln.from_bus, "to": ln.to_bus,
                      "x": np.asarray(x_line), "rating_mva": ln.rating_mva, "kv": vn.loc[ln.from_bus].to_numpy()}),
        pd.DataFrame({"key": "t" + tr.trafo.astype(str), "frm": tr.hv_bus, "to": tr.lv_bus,
                      "x": np.asarray(x_tr), "rating_mva": tr.sn_mva, "kv": vn.loc[tr.hv_bus].to_numpy()}),
    ], ignore_index=True)


def branches_from_pp(net) -> pd.DataFrame:
    """The same table from a pandapower network (lines first, then trafos, in index order). Keys are the element
    names when every element is named, else l<idx> / t<idx>. Line rating = sqrt(3) x vn x max_i_ka."""
    vn = net.bus.vn_kv
    ln, tr = net.line, net.trafo

    def keys(df, p):
        names = df.get("name")
        if names is not None and names.notna().all() and (names != "").all():
            return names.astype(str).to_numpy()
        return np.array([f"{p}{i}" for i in df.index])

    lkv = vn.loc[ln.from_bus].to_numpy()
    return pd.concat([
        pd.DataFrame({"key": keys(ln, "l"), "frm": ln.from_bus.to_numpy(), "to": ln.to_bus.to_numpy(),
                      "x": _x_line(ln.x_ohm_per_km, ln.length_km, ln.parallel, lkv),
                      "rating_mva": np.sqrt(3) * lkv * ln.max_i_ka.to_numpy(), "kv": lkv}),
        pd.DataFrame({"key": keys(tr, "t"), "frm": tr.hv_bus.to_numpy(), "to": tr.lv_bus.to_numpy(),
                      "x": _x_trafo(tr.vk_percent, tr.vkr_percent, tr.sn_mva, tr.parallel),
                      "rating_mva": tr.sn_mva.to_numpy(float), "kv": vn.loc[tr.hv_bus].to_numpy()}),
    ], ignore_index=True)


def ptdf(br: pd.DataFrame, buses: np.ndarray, slack: int) -> np.ndarray:
    """(branches x buses): MW on each branch (from -> to) per MW injected at a bus and withdrawn at slack."""
    pos = {b: i for i, b in enumerate(buses)}
    A = np.zeros((len(br), len(buses)))
    A[np.arange(len(br)), [pos[b] for b in br.frm]] = 1
    A[np.arange(len(br)), [pos[b] for b in br.to]] = -1
    bdiag = 1 / br.x.to_numpy()
    Bbus = A.T @ (bdiag[:, None] * A)
    keep = np.array([b != slack for b in buses])
    X = np.zeros((len(buses), len(buses)))
    X[np.ix_(keep, keep)] = np.linalg.inv(Bbus[np.ix_(keep, keep)])
    return (bdiag[:, None] * A) @ X


def cut_off(br: pd.DataFrame, buses: np.ndarray, slack: int, skip: str) -> list[int]:
    """Buses no longer connected to the slack bus once branch `skip` is removed."""
    adj = {b: [] for b in buses}
    for k, f, t in zip(br.key, br.frm, br.to):
        if k != skip:
            adj[f].append(t)
            adj[t].append(f)
    seen, q = {slack}, deque([slack])
    while q:
        for v in adj[q.popleft()]:
            if v not in seen:
                seen.add(v)
                q.append(v)
    return sorted(int(b) for b in set(buses) - seen)


def injections(g, buses: np.ndarray, pnet_kw: np.ndarray, agent_bus: np.ndarray, scale: np.ndarray):
    """Bus injections, MW, (hours x buses): agents' p_net + gens at p_mw - background loads x bg_load_scale.
    The slack bus takes the balance (its PTDF column is zero)."""
    pos = {b: i for i, b in enumerate(buses)}
    load = np.zeros(len(buses)); gen = np.zeros(len(buses)); ag = np.zeros((len(buses), len(agent_bus)))
    np.add.at(load, [pos[b] for b in g.loads.bus], g.loads.p_mw.to_numpy())
    np.add.at(gen, [pos[b] for b in g.gens.bus], g.gens.p_mw.to_numpy())
    ag[[pos[b] for b in agent_bus], np.arange(len(agent_bus))] = 1
    return gen[None, :] - scale[:, None] * load[None, :] + (pnet_kw.T / 1e3) @ ag.T


def zone_rows(P: np.ndarray, br: pd.DataFrame, buses: np.ndarray, asset_bus: dict, elements) -> dict:
    """{element: {asset: PTDF of the asset's bus on that element}}. Every asset appears in every row (zone
    membership is |PTDF| >= PTDF_MIN, applied by the caller): zones overlap, so pricing uses rows, not membership."""
    row = {k: i for i, k in enumerate(br.key)}
    col = {b: i for i, b in enumerate(buses)}
    return {e: {a: float(P[row[e], col[b]]) for a, b in asset_bus.items()} for e in elements}


def monitored(loading: pd.DataFrame, threshold: float = LIMIT_PCT) -> list:
    """Elements (columns of a T x elements loading table, %) whose max loading is at or above `threshold`."""
    return [k for k in loading.columns if float(loading[k].max()) >= threshold]


def binding(series: pd.Series) -> dict:
    """Hours at or above the limit and at or above 100% for an hourly loading series (%)."""
    local = series.index.tz_convert(TZ)
    over = series >= LIMIT_PCT
    return {
        "max_pct": float(series.max()), "max_at_utc": str(series.idxmax()),
        "hours_ge_90": int(over.sum()), "hours_ge_100": int((series >= 100).sum()),
        "hours_ge_90_by_month_central": {int(k): int(v) for k, v in over.groupby(local.month).sum().items() if v},
        "hours_ge_90_by_hour_central": {int(k): int(v) for k, v in over.groupby(local.hour).sum().items() if v},
    }
