"""
M0 zone check: what sits downstream of l43, l26, t35, t38 and t40, and when each one binds.

Downstream two ways:
  topological : buses cut off from the slack bus when the element is removed (empty if the element is meshed)
  electrical  : agents whose injection moves >= PTDF_MIN of each kW onto the element (DC PTDF, slack = ext_grid bus)

Binding comes from the hourly loading_pct_* columns of the power-flow results (90% = D10 limit).
All lines in the dataset are a placeholder 1.0 km, so the PTDF is checked against those same results
on the dev-slice hours.

Run:  python -m bench.zone_check configs/dev_2day.yaml      -> runs/m0/
"""
import json, sys
from collections import deque

import numpy as np
import pandas as pd

from bench.runinfo import run_header
from sim.data import REPO, Grid, load_config, load_grid, load_powerflow, load_slice, local_range

OUT = REPO / "runs" / "m0"
ELEMENTS = ["l43", "l26", "t35", "t38", "t40"]
PTDF_MIN = 0.05
LIMIT_PCT = 90.0           # D10 operating limit
CORR_MIN = 0.9             # PTDF validation flag
BASE_MVA = 100.0
TZ = "America/Chicago"


def branches(g: Grid) -> pd.DataFrame:
    """Lines and trafos as one table: key, from, to, x (p.u. on BASE_MVA), rating (MVA)."""
    vn = g.buses.set_index("bus").vn_kv
    ln = g.lines
    x_line = ln.x_ohm_per_km * ln.length_km / ln.parallel / (vn.loc[ln.from_bus].to_numpy() ** 2 / BASE_MVA)
    tr = g.trafos
    x_tr = np.sqrt(tr.vk_percent ** 2 - tr.vkr_percent ** 2) / 100 * BASE_MVA / tr.sn_mva / tr.parallel
    return pd.concat([
        pd.DataFrame({"key": "l" + ln.line.astype(str), "frm": ln.from_bus, "to": ln.to_bus,
                      "x": x_line.to_numpy(), "rating_mva": ln.rating_mva, "kv": vn.loc[ln.from_bus].to_numpy()}),
        pd.DataFrame({"key": "t" + tr.trafo.astype(str), "frm": tr.hv_bus, "to": tr.lv_bus,
                      "x": x_tr.to_numpy(), "rating_mva": tr.sn_mva, "kv": vn.loc[tr.hv_bus].to_numpy()}),
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


def injections(g: Grid, buses: np.ndarray, pnet_kw: np.ndarray, agent_bus: np.ndarray, scale: np.ndarray):
    """Bus injections, MW, (hours x buses): agents' p_net + gens at p_mw - background loads x bg_load_scale.
    The slack bus takes the balance (its PTDF column is zero)."""
    pos = {b: i for i, b in enumerate(buses)}
    load = np.zeros(len(buses)); gen = np.zeros(len(buses)); ag = np.zeros((len(buses), len(agent_bus)))
    np.add.at(load, [pos[b] for b in g.loads.bus], g.loads.p_mw.to_numpy())
    np.add.at(gen, [pos[b] for b in g.gens.bus], g.gens.p_mw.to_numpy())
    ag[[pos[b] for b in agent_bus], np.arange(len(agent_bus))] = 1
    return gen[None, :] - scale[:, None] * load[None, :] + (pnet_kw.T / 1e3) @ ag.T


def binding(series: pd.Series) -> dict:
    local = series.index.tz_convert(TZ)
    over = series >= LIMIT_PCT
    return {
        "max_pct": float(series.max()), "max_at_utc": str(series.idxmax()),
        "hours_ge_90": int(over.sum()), "hours_ge_100": int((series >= 100).sum()),
        "hours_ge_90_by_month_central": {int(k): int(v) for k, v in over.groupby(local.month).sum().items() if v},
        "hours_ge_90_by_hour_central": {int(k): int(v) for k, v in over.groupby(local.hour).sum().items() if v},
    }


def main(cfg_path):
    cfg = load_config(cfg_path)
    g = load_grid(cfg["data_dir"])
    s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
    t0, t1 = local_range(cfg["start"], cfg["end"], cfg["slice_tz"])

    br = branches(g)
    buses = g.buses.bus.to_numpy()
    P = ptdf(br, buses, g.slack_bus)
    row = {k: i for i, k in enumerate(br.key)}
    col = {b: i for i, b in enumerate(buses)}
    agents = g.agents.set_index("entity_id")

    cols = [f"loading_pct_{e}" for e in ELEMENTS]
    pf = load_powerflow(cfg["data_dir"], cols + ["slack_p_mw"])
    pf_slice = pf[(pf.index >= t0) & (pf.index < t1)]

    # PTDF validation on the slice hours: the agents' p_net at each power-flow timestamp
    step = s.ts_utc.get_indexer(pf_slice.index)
    if (step < 0).any():
        raise ValueError("power-flow hours do not line up with the slice")
    agent_bus = s.meta.bus.to_numpy()
    inj = injections(g, buses, s.agent["pnet"][:, step], agent_bus, s.system["bg_load_scale"][step])
    flows = inj @ P.T                                    # hours x branches, MW
    slack_pred = -inj.sum(1)
    slack_corr = float(np.corrcoef(slack_pred, pf_slice.slack_p_mw)[0, 1])

    zones, hours = {}, []
    for e in ELEMENTS:
        b = br.iloc[row[e]]
        down = cut_off(br, buses, g.slack_bus, e)
        sens = pd.Series(P[row[e], [col[x] for x in agents.bus]], index=agents.index)
        members = sens[(sens.abs() >= PTDF_MIN) | agents.bus.isin(down)]
        members = members.reindex(members.abs().sort_values(ascending=False).index)
        series = pf[f"loading_pct_{e}"]
        sl = pf_slice[f"loading_pct_{e}"]
        pred = np.abs(flows[:, row[e]]) / b.rating_mva * 100
        corr = float(np.corrcoef(pred, sl)[0, 1]) if sl.std() > 0 and pred.std() > 0 else float("nan")
        zones[e] = {
            "element": {"type": "line" if e[0] == "l" else "trafo", "from_bus": int(b.frm), "to_bus": int(b.to),
                        "kv": float(b.kv), "rating_mva": float(b.rating_mva)},
            "topological_downstream": {"buses": down,
                                       "agents": sorted(agents.index[agents.bus.isin(down)].tolist()),
                                       "meshed": not down},
            "agents": [{"id": i, "type": agents.at[i, "entity_type"], "bus": int(agents.at[i, "bus"]),
                        "ptdf": round(float(v), 4), "topological": bool(agents.at[i, "bus"] in down)}
                       for i, v in members.items()],
            "binding_year": binding(series),
            "binding_slice": {"max_pct": float(sl.max()), "max_at_utc": str(sl.idxmax()),
                              "hours_ge_90": int((sl >= LIMIT_PCT).sum())},
            "ptdf_validation": {"hours": len(sl), "pearson_r": corr, "mean_abs_err_pct": float(np.abs(pred - sl).mean()),
                                "flag_below_0.9": not corr >= CORR_MIN},
        }
        over = series[series >= LIMIT_PCT]
        hours.append(pd.DataFrame({"element": e, "t_utc": over.index.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   "t_central": over.index.tz_convert(TZ).strftime("%Y-%m-%d %H:%M"),
                                   "loading_pct": over.to_numpy()}))

    result = {
        "header": run_header("bench/zone_check.py", cfg),
        "method": {
            "ptdf": f"DC, base {BASE_MVA:.0f} MVA, slack bus {g.slack_bus}, line x = x_ohm_per_km*length/parallel on "
                    "from-bus kV, trafo x = sqrt(vk^2-vkr^2) on sn_mva",
            "zone_agents": f"|PTDF| >= {PTDF_MIN} or topologically downstream, sorted by |PTDF|",
            "binding": f"hourly loading_pct >= {LIMIT_PCT:.0f} (D10) and >= 100",
            "validation_injections": "agents p_net_kw at the hour's step; background loads p_mw x bg_load_scale; "
                                     "gen p_mw as in the pandapower json (sgen all out of service); slack takes the rest",
        },
        "validation_slack_p_mw_pearson_r": slack_corr,
        "zones": zones,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "zone_check.json").write_text(json.dumps(result, indent=2))
    pd.concat(hours).to_csv(OUT / "zone_check_hours.csv", index=False)

    for e, z in zones.items():
        v = z["ptdf_validation"]
        print(f"{e}: downstream {z['topological_downstream']['buses'] or 'meshed'}, {len(z['agents'])} agents, "
              f"max {z['binding_year']['max_pct']:.1f}%, {z['binding_year']['hours_ge_90']} h >= 90%, "
              f"PTDF r = {v['pearson_r']:.3f}{'  FLAG' if v['flag_below_0.9'] else ''}")
    print(f"slack r = {slack_corr:.3f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else REPO / "configs" / "dev_2day.yaml")
