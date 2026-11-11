"""Stressed-grid scenarios (D10) and the AC power flow that scores them.

A scenario grows load in the t35/t40 zones three ways (bench/stress_sweep.py sweeps each):
  A  zone agents' load:            p_net' = p_net - g * load_kw
  B  A plus background loads at zone buses:   p_mw * bg_load_scale * (1 + g), q the same
  C  one new flat load per zone at a chosen bus ("new data center connects"), fixed power factor

The pandapower network comes in from sim/data.py; nothing here reads files.
Agents are added as one load each at their bus, p_mw = -p_net / 1000 (p_net > 0 is export).

Generator dispatch (disclosed assumption, see bench/stress_sweep.py):
  json          gens at their json p_mw, the ext_grid takes the balance
  held slack    gens share a total iterated until the ext_grid supplies a target (Engie's hourly
                slack_p_mw); growth is served by the gens. Gens clip at [min_p, max_p] (flagged)
  fixed total   gens share a given total (e.g. their level-0 total); the ext_grid serves any growth
The split is pro rata to max_p_mw ("max") or min_p plus a common share of (max_p - min_p) ("headroom").
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

NEW_LOAD_PF = 0.98        # lagging, disclosed
SLACK_TOL_MW = 0.1        # held-slack dispatch: stop when the ext_grid is this close to its target
MAX_DISPATCH_ITER = 8


@dataclass(frozen=True)
class Scenario:
    sweep: str                                   # "A", "B" or "C"
    level: float                                 # growth fraction (A, B) or MW per new load (C)
    agent_growth: float = 0.0
    bg_growth: float = 0.0
    new_load_mw: dict[int, float] = field(default_factory=dict)   # bus -> MW


def grow_agents(pnet_kw: np.ndarray, load_kw: np.ndarray, in_zone: np.ndarray, g: float) -> np.ndarray:
    """(N, T) p_net with zone agents' load scaled by 1 + g. in_zone is the (N,) union of both zones,
    so an agent in both zones grows once. Agents with load 0 (PVB plants) are unchanged."""
    return pnet_kw - g * load_kw * np.asarray(in_zone, bool)[:, None]


def bg_multiplier(load_bus: np.ndarray, zone_buses, g: float) -> np.ndarray:
    """(n_loads,) growth multiplier for background loads: 1 + g at zone buses, 1 elsewhere."""
    return np.where(np.isin(load_bus, list(zone_buses)), 1.0 + g, 1.0)


def new_load_bus(ptdf_by_bus: pd.Series, flow_sign: float) -> int:
    """Highest-|PTDF| bus where added load raises the element's |flow|.
    Load is negative injection, so it raises flow when PTDF * flow_sign < 0."""
    ok = ptdf_by_bus[ptdf_by_bus * flow_sign < 0]
    if ok.empty:
        raise ValueError("no bus where added load raises flow")
    return int(ok.abs().idxmax())


def relief_mw(p_mw, q_mvar, sn_mva: float, vm_pu, limit_pct: float) -> np.ndarray:
    """MW of real-power reduction that brings a trafo back to limit_pct (hv side, Q held fixed):
    loading = S / (sn * vm), so the limit is S_max = limit * sn * vm and relief = |P| - sqrt(S_max^2 - Q^2).
    0 when already under the limit; inf when Q alone breaks it."""
    p, q, vm = (np.asarray(a, float) for a in (p_mw, q_mvar, vm_pu))
    s_max = limit_pct / 100 * sn_mva * vm
    room = s_max ** 2 - q ** 2
    with np.errstate(invalid="ignore"):
        r = np.where(room > 0, np.abs(p) - np.sqrt(np.maximum(room, 0)), np.inf)
    return np.maximum(r, 0.0)


@dataclass
class PFResult:
    line_loading: pd.DataFrame       # (T, lines) %, columns "l<idx>"
    trafo_loading: pd.DataFrame      # (T, trafos) %, columns "t<idx>"
    vm_pu: pd.DataFrame              # (T, buses), columns = bus idx
    trafo_hv: dict[str, pd.DataFrame]   # watched key -> columns p_hv_mw, q_hv_mvar, vm_hv_pu (lines: from side)
    converged: np.ndarray            # (T,) bool
    gen_total_mw: np.ndarray | None = None    # (T,) in-service gens' total p
    ext_p_mw: np.ndarray | None = None        # (T,) ext_grid p
    gen_at_limit: np.ndarray | None = None    # (T,) bool: pro-rata dispatch clipped at max_p or min_p

    def loading(self) -> pd.DataFrame:
        return pd.concat([self.line_loading, self.trafo_loading], axis=1)


class ACGrid:
    """A copy of the base network with one load per agent and one (initially 0 MW) load per new-load bus.

    Options for the baseline gate:
      agent_q_ratio     agent q = ratio * p (0 = unity power factor)
      distributed_slack gens share the slack in proportion to their p_mw (else the ext_grid takes it all)
    """

    def __init__(self, net, agent_ids, agent_bus, new_load_buses=(), agent_q_ratio=0.0, distributed_slack=False,
                 gen_split="max"):
        import pandapower as pp
        self.pp = pp
        self.net = copy.deepcopy(net)
        n = self.net
        self.bg_idx = n.load.index[n.load.in_service].to_numpy()
        self.bg_p = n.load.loc[self.bg_idx, "p_mw"].to_numpy(float)
        self.bg_q = n.load.loc[self.bg_idx, "q_mvar"].to_numpy(float)
        self.bg_bus = n.load.loc[self.bg_idx, "bus"].to_numpy()
        self.agent_idx = np.array([pp.create_load(n, int(b), p_mw=0.0, q_mvar=0.0, name=f"agent:{a}")
                                   for a, b in zip(agent_ids, agent_bus)])
        self.new_idx = {int(b): pp.create_load(n, int(b), p_mw=0.0, q_mvar=0.0, name=f"new:{b}")
                        for b in new_load_buses}
        self.agent_q_ratio = agent_q_ratio
        self.distributed_slack = distributed_slack
        if distributed_slack:
            n.gen["slack_weight"] = n.gen.p_mw.where(n.gen.in_service, 0.0)
        self.gen_idx = n.gen.index[n.gen.in_service].to_numpy()
        lim = n.gen.reindex(columns=["max_p_mw", "min_p_mw"]).loc[self.gen_idx]   # columns absent if no gens
        self.gen_max = lim.max_p_mw.to_numpy(float)
        self.gen_min = lim.min_p_mw.to_numpy(float)
        if gen_split not in ("max", "headroom"):
            raise ValueError(f"unknown gen_split {gen_split!r}")
        self.gen_split = gen_split

    def _set_gens(self, total: float) -> bool:
        """Gens share a total, clipped to [min_p, max_p]. True if any clipped.
        max:      pro rata to max_p
        headroom: each at min_p plus the same share of its (max_p - min_p)"""
        if self.gen_split == "max":
            want = self.gen_max * total / self.gen_max.sum()
        else:
            room = self.gen_max - self.gen_min
            want = self.gen_min + room * (total - self.gen_min.sum()) / room.sum()
        p = np.clip(want, self.gen_min, self.gen_max)
        self.net.gen.loc[self.gen_idx, "p_mw"] = p
        return bool((np.abs(p - want) > 1e-9).any())

    def _solve(self, t, init, slack_target_mw, gen_total_mw, g_prev):
        """One step's power flow under the dispatch mode. Returns (gen total, clipped) or raises."""
        n, pp = self.net, self.pp
        if gen_total_mw is not None:
            clipped = self._set_gens(gen_total_mw[t])
            pp.runpp(n, init=init, numba=False)
            return float(n.gen.loc[self.gen_idx, "p_mw"].sum()), clipped
        if slack_target_mw is None:
            pp.runpp(n, init=init, numba=False, distributed_slack=self.distributed_slack)
            return float(n.gen.loc[self.gen_idx, "p_mw"].sum()), False
        g = g_prev if g_prev is not None else float(n.load.p_mw[n.load.in_service].sum()) - slack_target_mw[t]
        for _ in range(MAX_DISPATCH_ITER):
            clipped = self._set_gens(g)
            pp.runpp(n, init=init, numba=False)
            init = "results"
            err = float(n.res_ext_grid.p_mw.iat[0]) - slack_target_mw[t]
            if abs(err) < SLACK_TOL_MW or (clipped and (err > 0) == (g >= self.gen_max.sum())):
                break
            g += err
        return float(n.gen.loc[self.gen_idx, "p_mw"].sum()), clipped

    def run(self, bg_scale: np.ndarray, bg_mult: np.ndarray, agent_pnet_kw: np.ndarray,
            new_load_mw: dict[int, float] | None = None, watch=("t35", "t40"), index=None,
            slack_target_mw: np.ndarray | None = None, gen_total_mw: np.ndarray | None = None) -> PFResult:
        """AC power flow at every step. bg_scale (T,), bg_mult (n_bg,), agent_pnet_kw (N, T).
        slack_target_mw (T,) selects held-slack dispatch, gen_total_mw (T,) fixed-total dispatch."""
        n, pp = self.net, self.pp
        T = len(bg_scale)
        gt, ext, lim = np.full(T, np.nan), np.full(T, np.nan), np.zeros(T, bool)
        g_prev = None
        tan = np.tan(np.arccos(NEW_LOAD_PF))
        for b, i in self.new_idx.items():
            mw = (new_load_mw or {}).get(b, 0.0)
            n.load.loc[i, ["p_mw", "q_mvar"]] = [mw, mw * tan]
        lines, trafos, buses = n.line.index, n.trafo.index, n.bus.index
        ll = np.full((T, len(lines)), np.nan)
        tl = np.full((T, len(trafos)), np.nan)
        vm = np.full((T, len(buses)), np.nan)
        hv = {k: np.full((T, 3), np.nan) for k in watch}
        ok = np.zeros(T, bool)
        init = "auto"
        for t in range(T):
            m = bg_scale[t] * bg_mult
            n.load.loc[self.bg_idx, "p_mw"] = self.bg_p * m
            n.load.loc[self.bg_idx, "q_mvar"] = self.bg_q * m
            p = -agent_pnet_kw[:, t] / 1e3
            n.load.loc[self.agent_idx, "p_mw"] = p
            n.load.loc[self.agent_idx, "q_mvar"] = p * self.agent_q_ratio
            try:
                gt[t], lim[t] = self._solve(t, init, slack_target_mw, gen_total_mw, g_prev)
            except pp.LoadflowNotConverged:
                init, g_prev = "auto", None
                continue
            ok[t], init, g_prev = True, "results", gt[t]
            ext[t] = n.res_ext_grid.p_mw.iat[0]
            ll[t] = n.res_line.loading_percent.to_numpy()
            tl[t] = n.res_trafo.loading_percent.to_numpy()
            vm[t] = n.res_bus.vm_pu.to_numpy()
            for k in watch:
                if k[0] == "t":
                    hv[k][t] = n.res_trafo.loc[int(k[1:]), ["p_hv_mw", "q_hv_mvar", "vm_hv_pu"]].to_numpy(float)
                else:
                    hv[k][t] = n.res_line.loc[int(k[1:]), ["p_from_mw", "q_from_mvar", "vm_from_pu"]].to_numpy(float)
        return PFResult(
            line_loading=pd.DataFrame(ll, index=index, columns=[f"l{i}" for i in lines]),
            trafo_loading=pd.DataFrame(tl, index=index, columns=[f"t{i}" for i in trafos]),
            vm_pu=pd.DataFrame(vm, index=index, columns=list(buses)),
            trafo_hv={k: pd.DataFrame(v, index=index, columns=["p_hv_mw", "q_hv_mvar", "vm_hv_pu"])
                      for k, v in hv.items()},
            converged=ok, gen_total_mw=gt, ext_p_mw=ext, gen_at_limit=lim,
        )

    def run_scenario(self, sc: Scenario, pnet_kw, load_kw, in_zone, zone_buses, bg_scale, index=None,
                     watch=("t35", "t40"), slack_target_mw=None, gen_total_mw=None) -> PFResult:
        pnet = grow_agents(pnet_kw, load_kw, in_zone, sc.agent_growth)
        mult = bg_multiplier(self.bg_bus, zone_buses, sc.bg_growth)
        return self.run(bg_scale, mult, pnet, sc.new_load_mw, watch=watch, index=index,
                        slack_target_mw=slack_target_mw, gen_total_mw=gen_total_mw)
