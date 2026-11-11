"""
M1: first end-to-end loop on the dev slice.

Two kinds of run with the same agents, planner, information and physics:
  selfish      : battery agents plan on day-ahead prices alone; no exchange, no operator
  exchange_h<H>: the grid operator posts layered needs (target = forecast peak - H MW), agents bid through the
                 exchange, clear at a uniform price on plain kW, and are settled on meter readings
Everyone's load and PV stay at recorded values, and non-participants keep their recorded dispatch;
only the participants' battery dispatch changes.

M1.1 (D32) adds nominations (config `nominations`, default on): agents publish planned site net schedules, the
operator sees only the zone total, re-forecasts, and posts residual needs for `rounds` rounds (default 2).
Deviation from the binding nomination beyond a free band pays the zone's scarcity price. M1 configs set
`nominations: false` and `rounds: 1`, which reproduces M1 exactly.

M2a (D34 revised, D37) adds, each behind its own flag (all off by default, so M1 and M1.1 configs reproduce
exactly): firm_offers, prep_clearing, no_rebound, daily_energy, and risk_premium_usd_mwh (0, a number, or "auto" =
mean |RT - DA| over the slice). With any of them on, the summary adds net harm by component (the parts add up to
the total exactly, end-of-slice stored energy included), skipped offers and their price effect, the metered and
nominated target checks, the value share of the M0 ceiling, and the M2a pass block.

Run:  python -m sim.run_m1 configs/m1_full.yaml      -> runs/<run_name>/
      python -m sim.run_m1 configs/m2a_full.yaml --ablation-one <flag>   (150 MW with one M2a flag off; own folder,
                                                                          so several can run at once)
      python -m sim.run_m1 configs/m2a_full.yaml --ablation-merge        (table into summary.json; all-on = main run)
"""
import json, sys, time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from agents.battery import BatteryAgent
from agents.operator import GridOperator, parse_nomination_span
from agents.planner import Battery
from bench.runinfo import run_header
from exchange import messages as M
from exchange.core import Exchange
from exchange.trace.logger import TraceLogger
from sim import physics
from attacks import wrap_attacker
from sim.bus import Bus
from sim.clock import Clock
from sim.data import REPO, Slice, load_config, load_slice
from sim.forecasts import PriceForecast, operator_forecast
from sim.reference import ModeledPaths, selfish_plan, selfish_site_net

TZ = "America/Chicago"
# fix 1 (memory): message types bus.log keeps as envelope only (sim/bus.py). The run summary counts them per day
# and never reads their payload; the trace keeps them in full.
BUS_LOG_SLIM = frozenset({"nominate", "telemetry"})
NET_HARM_REL, NET_HARM_ABS = 0.01, 10.0     # tolerance: max(1% of the acting-alone result, $10) (PLAN.md section 8)
M2A_FLAGS = ("firm_offers", "prep_clearing", "partial_prep", "no_rebound", "daily_energy", "risk_premium",
             "sequential_clearing", "release_superseded_lock", "cap_rebase", "plan_to_nomination", "publish_room",
             "price_mode")
PEAK_PRICE_CAP_USD_MWH = 100.0      # D54: arm C's peak price cap, placeholder for the operator's avoided cost (Q-N1)


def peak_price_cap(cfg, price_mode: bool) -> float:
    """$/MWh cap on the D53 peak price: `peak_price_cap_usd_mwh` if set, else the D54 placeholder in price mode. Off
    price mode the exchange records the zone cap but never uses it, as before D54."""
    if price_mode:
        return float(cfg.get("peak_price_cap_usd_mwh", PEAK_PRICE_CAP_USD_MWH))
    return cfg.get("peak_price_cap_usd_mwh", cfg["price_cap_usd_mwh"])


def summary_header(cfg, roster: list[str], trace_header: dict) -> dict:
    """Summary header (config, seed, commit, roster). If the git call fails here (it has under memory pressure), the
    commit falls back to the run's own trace header, written at the start of the run."""
    h = {**run_header("sim/run_m1.py", cfg, cfg["seed"]), "roster": roster}
    if not h["git_commit"].removesuffix("-dirty"):
        h["git_commit"] = trace_header.get("git_commit", "")
        h["git_commit_from"] = "trace header"
    return h


def participants(s: Slice, agents) -> np.ndarray:
    if agents == "all":
        return np.arange(len(s.ids))
    pos = {e: i for i, e in enumerate(s.ids)}
    missing = [a for a in agents if a not in pos]
    if missing:
        raise ValueError(f"unknown agents {missing}")
    return np.array([pos[a] for a in agents])


def settings(cfg, rounds=None):
    """rounds: a count, or "info" (default): residual rounds run until one closes after price-informed nominations."""
    nominations = cfg.get("nominations", True)
    rounds = rounds if rounds is not None else cfg.get("rounds", "info")
    if not nominations and rounds != 1:
        raise ValueError("rounds other than 1 need nominations")
    return nominations, rounds


def m2a_flags(cfg, off=()) -> dict:
    f = {k: bool(cfg.get(k, False)) for k in ("firm_offers", "prep_clearing", "partial_prep", "no_rebound",
                                               "daily_energy", "sequential_clearing", "release_superseded_lock",
                                               "cap_rebase", "plan_to_nomination", "publish_room",
                                               "price_mode")}
    prem = cfg.get("risk_premium_usd_mwh", 0) or 0
    f["risk_premium"] = prem != 0
    for k in off:
        f[k] = False
    f["premium_setting"] = prem if f["risk_premium"] else 0
    return f


def first_day_da_spread(s: Slice) -> float:
    """$/MWh: max - min day-ahead price over the slice's first local day, public before the first gate (D53 rho0)."""
    day = s.ts_utc.tz_convert(TZ).date
    da = s.system["price_da"][day == day[0]]
    return float(da.max() - da.min())


def risk_premium(setting, s: Slice) -> float:
    """$/MWh. "auto": mean |RT - DA| over the run's own slice (in-sample, disclosed)."""
    if setting == "auto":
        return float(np.abs(s.system["price_rt"] - s.system["price_da"]).mean())
    return float(setting or 0.0)


def demonstrated_pv(prev: Slice | None, part, P) -> np.ndarray:
    """Per participant, kWh/day of PV it has been metered producing that the battery could absorb (min(pv -
    curtailment, battery power) per step), on the day before the slice. Zero without that day."""
    if prev is None:
        return np.zeros(len(part))
    avail = np.maximum(np.nan_to_num(prev.agent["pv"][part]) - prev.agent["curt"][part], 0.0)
    return (np.minimum(avail, P[:, None]) * prev.dt_h).sum(1)


def daily_budgets(usable: float, pv_by_date: dict, demo_pv: float, factor: float) -> tuple[dict, dict]:
    """Per local date: usable storage + conservative PV forecast, never above usable + demonstrated PV (the Jan 1
    metered limit). Returns (budgets, cut) where cut holds the kWh removed by the demonstrated limit, if any."""
    budgets, cut = {}, {}
    for d, pv_kwh in pv_by_date.items():
        own = usable + factor * pv_kwh
        budgets[d] = usable + min(factor * pv_kwh, demo_pv)
        if own - budgets[d] > 1e-6:
            cut[d] = round(own - budgets[d], 1)
    return budgets, cut


def referee_walk(soc0: float, k: int, P: float, s_min: float, s_max: float, eta: float, sd: float, dt: float,
                 base_export, allowed_net, commit, band: float, end: int, tol_kwh: float = 1.0):
    """Greedy feasibility walk from step k to `end` under the rules the exchange enforces: outside award windows,
    charge as much as the cap allows (site net may not fall below allowed_net - band; NaN = uncapped), power and SOC
    permitting; inside, discharge the committed kW. Charging as early as possible maximizes stored energy, so if
    this walk can't deliver, nothing within the rules can. Returns {step: deficit kWh} where the floor is breached."""
    soc, short = soc0, {}
    for t in range(k, end):
        soc *= 1 - sd * dt
        if commit[t] > 1e-9:
            soc -= commit[t] * dt / eta
            if soc < s_min - 1e-9:
                short[t] = s_min - soc
                soc = s_min
        else:
            room = P if np.isnan(allowed_net[t]) else min(P, max(base_export[t] - allowed_net[t] + band, 0.0))
            soc += min(room, max(s_max - soc, 0.0) / (eta * dt)) * dt * eta
    if sum(short.values()) <= tol_kwh:
        return {}
    return short


def referee_awards(new_awards, log, k, soc, fleet, idx_of, base_export, iso, dt, collect=None, band_pct=0.05,
                   band_min_kw=50.0):
    """D34 referee: for every agent with a new award, all its awards so far must be deliverable under the cap the
    exchange enforces: the nomination frozen at its first award (log order), re-based by any cap_rebase (D47), minus
    all its awarded prep. A failure
    aborts the run, or with `collect` (a list) is recorded and the run continues."""
    pos = {t: i for i, t in enumerate(iso)}
    T = len(iso)
    for aid in sorted({m["to"][0] for m in new_awards}):
        j = idx_of[aid]
        frozen, first_award, prep, commit = np.full(T, np.nan), None, np.zeros(T), np.zeros(T)
        windows = {}
        for n in log:
            if n["msg_type"] == "award" and n["to"][0] == aid:
                first_award = first_award or n
                p = n["payload"]
                w0, w1 = pos[p["window"]["start_utc"]], pos.get(p["window"]["end_utc"], T)
                commit[w0:w1] += p["accredited_kw"]
                windows[p["need_id"]] = (w0, w1, p["accredited_kw"])
                if p.get("prep"):
                    a = pos[p["prep"]["start_utc"]]
                    v = np.asarray(p["prep"]["kw"], float)[:T - a]
                    prep[a:a + len(v)] += v
            elif first_award is None and n["msg_type"] == "nominate" and n["sender"] == aid:
                a = pos[n["payload"]["window"]["start_utc"]]
                frozen[a:a + len(n["payload"]["kw"])] = n["payload"]["kw"]
            elif n["msg_type"] == "cap_rebase" and n["to"] == aid:          # D47: the re-based cap base
                a = pos[n["payload"]["window"]["start_utc"]]
                b = np.array([np.nan if v is None else v for v in n["payload"]["base_kw"]], float)[:T - a]
                seg = frozen[a:a + len(b)]
                frozen[a:a + len(b)] = np.where(np.isnan(b), seg, b)
        allowed = frozen - prep
        end = max(w1 for _, w1, _ in windows.values())
        band = max(band_pct * fleet.P[j], band_min_kw)
        short = referee_walk(float(soc[j]), k, float(fleet.P[j]), float(fleet.s_min[j]), float(fleet.s_max[j]),
                             float(fleet.ETA[j]), float(fleet.SD[j]), dt, base_export[j], allowed, commit, band, end)
        if not short:
            continue
        failing = sorted(nid for nid, (w0, w1, _) in windows.items() if any(w0 <= t < w1 for t in short))
        new = [m["payload"]["need_id"] for m in new_awards if m["to"][0] == aid]
        detail = {"agent": aid, "at_utc": iso[k], "new_awards": new, "failing_needs": failing,
                  "deficit_kwh": round(float(sum(short.values())), 1),
                  "award_kwh": round(float(sum(kw * (w1 - w0) * dt for w0, w1, kw in windows.values())), 1)}
        if collect is None:
            raise RuntimeError(f"award not deliverable, run stopped: agent {aid}, need(s) {new}, awards "
                               f"{detail['award_kwh']} kWh, {detail['deficit_kwh']} kWh short under the D34 cap")
        collect.append(detail)


def band_edge(ex, from_t: str | None = None, rel: float = 0.01) -> dict:
    """M3 "band riding" measure (D48): agent-steps whose extra load is within `rel` of the free band's edge, against
    the binding nomination in force and against the D34 cap, from the exchange's own records."""
    from exchange.core import band_kw
    out = {}
    for ref in ("binding_nomination", "rebound_cap"):
        agents, steps, checked = set(), 0, 0
        for aid, a in ex.agents.items():
            band = band_kw(a["mandate"]["max_kw"], ex.band_pct, ex.band_min_kw)
            metered: dict[str, float] = {}
            for asset in a["assets"]:
                for t, v in ex.net_meter.get(asset, {}).items():
                    metered[t] = metered.get(t, 0.0) + v
            if ref == "binding_nomination":
                lim = {t: -v for t, (v, _) in ex.baseline.get(aid, {}).items()}
            else:
                cap = ex.caps.get(aid)
                lim = {} if cap is None else {t: -b + cap["prep"].get(t, 0.0) for t, b in cap["base"].items()
                                              if cap["from"] <= t < cap["until"] and t not in cap["windows"]}
            for t, l in lim.items():
                if t not in metered or (from_t is not None and t < from_t):
                    continue
                checked += 1
                extra = -metered[t] - l
                if (1 - rel) * band - 1e-6 <= extra <= band + 1e-6:
                    steps += 1
                    agents.add(aid)
        out[ref] = {"agents": len(agents), "agent_steps": steps, "agent_steps_checked": checked}
    out["rule"] = f"extra load within {rel:.0%} of the free band edge, max(band_pct x P, band_min_kw)"
    return out


def make_declaration_check(ex, bus, agents, usable, s0, avail, base_x, wear: float, dt: float, iso: list[str]):
    """D82: the metering service's 1p feasibility check, handed to the exchange (which reads no data). check(asset,
    as_of) is True when 1p's planner has a plan over the run within the asset's declarations accepted by as_of (the
    one being filed included). The same planner and inputs as the reference (sim/reference.selfish_plan); feasibility
    doesn't depend on prices, so it plans at price 0."""
    j_of = {ag.asset_id: j for j, ag in enumerate(agents)}

    def check(asset: str, as_of: str) -> bool:
        j = j_of.get(asset)
        regs = [m for m in bus.log if m["msg_type"] == "register" and j is not None and m["sender"] == agents[j].id]
        if not regs:
            return False
        pmax, floor = ex.declared_limits(asset, iso, as_of)
        return selfish_plan(float(regs[-1]["payload"]["mandate"]["max_kw"]), usable[j], s0[j], np.zeros(len(iso)),
                            avail[j], base_x[j], wear, dt, np.asarray(pmax, float), np.asarray(floor, float)) is not None
    return check


DEFAULT_NOMINATION_SPAN = "operating_day"     # D86: flipped from "horizon" on Nov 6 (owner, scheduled)


def nomination_span(cfg) -> str | float:
    """The operator's nomination_span (owner, Nov 5): "operating_day" when absent (D86's flip, Nov 6; "horizon" before
    it, so arm A, D62 and parity reference configs now write `nomination_span: horizon`). Any other span is refused
    with D62 and
    with arm A (owner, Nov 5). D62: its modeled_response runs from a binding call's deadline to the slice end, and the
    exchange requires it to match the call's window. Arm A (prep_clearing or no_rebound): D37's prep and D34's cap
    reach a need's window end + rebound_hours, past the operating day; with no pre-award nomination there, clearing
    would skip the offer (missing_nominations) and the cap would silently stop."""
    span = cfg.get("nomination_span", DEFAULT_NOMINATION_SPAN)
    kind, _ = parse_nomination_span(span)
    if kind != "horizon" and cfg.get("d62_guarantee"):
        raise ValueError("nomination_span other than 'horizon' is not built for d62_guarantee: D62's modeled paths "
                         "run to the slice end")
    fl = m2a_flags(cfg)
    if kind != "horizon" and (fl["prep_clearing"] or fl["no_rebound"]):
        raise ValueError("nomination_span other than 'horizon' is refused for arm A (prep_clearing or no_rebound): "
                         "prep and the D34 cap reach past the span, where no pre-award nomination exists "
                         "(owner, Nov 5)")
    return span


def peak_steps(cfg) -> str:
    """peak_steps (D57 option 1, owner, Nov 6): "all" (default; today's summary, byte for byte) or "controllable": each
    variant also reports a `controllable` block, its peak, share of the M0 ceiling and gap share over the steps from the
    first binding call's window on, which is the headline for runs that set it. Not built with warm-up (deferred)."""
    v = cfg.get("peak_steps", "all")
    if v not in ("all", "controllable"):
        raise ValueError(f"peak_steps must be 'all' or 'controllable', not {v!r}")
    if v == "controllable" and cfg.get("warmup_days", 0):
        raise ValueError("peak_steps: controllable is not built with warmup_days (D57 defers warm-up)")
    return v


def controllable_from(cfg, s: Slice) -> int:
    """D57: the first step any binding nomination can move, the earliest binding call's deadline (its window starts
    there). Fixed by the operator's schedule at construction, from the config alone (nomination_span moves only a
    window's end). 0 when the config has no binding call."""
    nominations, rounds = settings(cfg)
    if not nominations:
        return 0
    clock = Clock(s.ts_utc, TZ)
    dt, T = clock.dt_h, len(clock.iso)
    fl = m2a_flags(cfg)
    op = GridOperator(cfg["operator_id"], clock.iso, clock.end_iso(T - 1), clock.local, "controllable_from",
                      cfg["zone"], 0.0, cfg["layer_mw"] * 1000.0, cfg["price_cap_usd_mwh"], cfg["post_hour_local"],
                      round(cfg["gate_lead_h"] / dt), rounds=rounds, nominations=nominations,
                      nom_lead_steps=round(cfg.get("nomination_lead_h", 1) / dt),
                      info_hour=cfg["price_publish_hour_local"], prep_clearing=fl["prep_clearing"],
                      priorities=fl["sequential_clearing"], price_mode=fl["price_mode"],
                      nomination_span=nomination_span(cfg))
    deadlines = [d for day in op.days for _, d, binding in day["calls"] if binding and d < op.T]
    return min(deadlines) if deadlines else 0


def controllable_block(net, selfish_net, clock, pf: int, m0c) -> dict:
    """D57: peak, share of the controllable M0 ceiling and (for an exchange run) gap share over steps pf.., plus the
    pre-from peaks and pre_from_rebound (exchange pre-from peak above the selfish one by more than 1 MW): the uncounted
    hours must not absorb shifted load."""
    k = pf + int(np.argmax(net[pf:]))
    out = {"from_utc": clock.iso[pf], "from_central": str(clock.local[pf]),
           "peak_mw": float(net[k]) / 1e3, "peak_utc": clock.iso[k], "peak_central": str(clock.local[k]),
           "share_of_m0_ceiling": share(float(net[k]), m0c), "pre_from_peak_mw": float(np.max(net[:pf])) / 1e3}
    if selfish_net is not None:
        sp_mw = float(np.max(selfish_net[pf:])) / 1e3
        out["selfish_peak_mw"] = sp_mw
        out["peak_vs_selfish_mw"] = out["peak_mw"] - sp_mw
        out["share_of_selfish_to_ceiling_gap"] = gap_share(sp_mw, out["peak_mw"], m0c)
        out["selfish_pre_from_peak_mw"] = float(np.max(selfish_net[:pf])) / 1e3
        out["pre_from_rebound"] = bool(out["pre_from_peak_mw"] > out["selfish_pre_from_peak_mw"] + 1.0)
    return out


def warmup_days(cfg) -> int:
    """warmup_days (owner, Nov 6; default 0 = today's runs): the run starts this many local days before `start` and
    runs the full mechanism on them; every reported metric covers only start..end, and the exchange settles money
    only from start on (settle_from_utc). Arm C only: refused with arm A (prep, the D34 cap and daily energy carry
    state the window view doesn't crop) and with D62 (its guarantee accrues from the slice start)."""
    n = cfg.get("warmup_days", 0)
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError(f"warmup_days must be a whole number of days >= 0, not {n!r}")
    if n:
        fl = m2a_flags(cfg)
        if not fl["price_mode"]:
            raise ValueError("warmup_days is built for arm C (price_mode) only")
        if any(fl[k] for k in ("prep_clearing", "no_rebound", "daily_energy")):
            raise ValueError("warmup_days is refused for arm A (prep_clearing, no_rebound, daily_energy)")
        if cfg.get("d62_guarantee"):
            raise ValueError("warmup_days is refused with d62_guarantee")
        if cfg.get("delivery_record") or cfg.get("declared_reference"):
            raise ValueError("warmup_days is refused with delivery_record (D65 counts binding calls over the run)")
    return n


class WindowOperator:
    """The operator seen from the reported window only (warm-up): arrays cut at w0, gate and step indices shifted.
    A gate before the window comes out negative (it ran during warm-up)."""

    def __init__(self, op, fc_full, w0: int):
        self.op, self.fc_full, self.w0 = op, fc_full, w0
        self.days = [{**d, "gates": [g - w0 for g in d["gates"]], "steps": d["steps"][d["steps"] >= w0] - w0}
                     for d in op.days if (np.asarray(d["steps"]) >= w0).any()]

    def system_target(self):
        return self.op.system_target()[self.w0:]

    def reforecast(self, forecast, agg):        # `forecast` is the window's; the full one gives the same window
        return self.op.reforecast(self.fc_full, agg)[self.w0:]


def window_slice(s: Slice, w0: int) -> Slice:
    return Slice(s.ids, s.ts_utc[w0:], s.dt_h, {k: v[:, w0:] for k, v in s.agent.items()},
                 {k: v[w0:] for k, v in s.system.items()}, s.meta)


def window_result(r: dict, w0: int, iso: list[str]) -> dict:
    """A run_variant result cut to the reported window (warm-up): arrays from step w0, messages sent at or after the
    window start, the price rounds of calls whose window reaches into it, settled prices on window steps, and each
    battery's stored energy at the window start (SOC_start, valued in harm like the energy left at the end)."""
    start = iso[w0]
    out = dict(r)
    for k in ("C", "D", "X", "SOC"):
        out[k] = r[k][:, w0:]
    out["SOC_start"] = r["SOC"][:, w0 - 1].copy()
    out["fc_op"] = r["fc_op"][w0:]
    out["log"] = [m for m in r["log"] if m["t_utc"] >= start]
    if r.get("operator") is not None:
        out["operator"] = WindowOperator(r["operator"], r["fc_op"], w0)
    calls = {m["payload"]["call_id"] for m in r["log"] if m["msg_type"] == "call_nominations"
             and m["sender"] == "exchange" and m["payload"]["window"]["end_utc"] > start}
    if r.get("price_log") is not None:
        out["price_log"] = [x for x in r["price_log"] if x["call_id"] in calls]
    for k in ("peak_settled", "settled_prices"):
        if r.get(k) is not None:
            out[k] = {t: v for t, v in r[k].items() if t >= start}
    out["margin_log"] = [e for e in (r.get("margin_log") or [])
                         if (e["call_id"] in calls if "call_id" in e else e.get("t_utc", start) >= start)]
    out["window_start_utc"] = start
    return out


def run_variant(cfg, s: Slice, part, out_dir: Path, headroom_mw=None, rounds=None, off=(), prev: Slice | None = None,
                suffix: str = "", window_start_utc: str | None = None) -> dict:
    """window_start_utc (warm-up, set by main from warmup_days): the reported window starts there; the exchange
    settles money from there on, and the header records it. None: the window is the slice (today)."""
    tic = time.time()
    wall_start = datetime.now(timezone.utc).isoformat(timespec="seconds")
    span = nomination_span(cfg)
    for key in ("declarations_into_1p", "void_rule"):     # D80/D81: one flag, declared_reference, for both halves
        if key in cfg:
            raise ValueError(f"unknown config key {key!r}: declarations into 1p and the void rule are one flag, "
                             "declared_reference")
    clock = Clock(s.ts_utc, TZ)
    iso, T, dt = clock.iso, len(s.ts_utc), clock.dt_h
    end_iso = clock.end_iso(T - 1)
    fleet = physics.Fleet.from_slice(s, part)
    prices = PriceForecast(s.ts_utc, s.system["price_da"], TZ, cfg["price_publish_hour_local"])
    nominations, rounds = settings(cfg, rounds)
    variant = "selfish" if headroom_mw is None else f"exchange_h{headroom_mw:g}" + (f"_r{rounds}" if nominations else "")
    variant += suffix
    run_id = f"{cfg['run_name']}-{variant}"
    fl = m2a_flags(cfg, off)
    premium = risk_premium(fl["premium_setting"], s)
    m2a = any(fl[k] for k in M2A_FLAGS)
    v2 = m2a or bool(cfg.get("harm_components", False))     # M2a summary: components, end energy, target checks

    ex = None if headroom_mw is None else Exchange(
        run_id, cfg["operator_id"], cfg["metering_id"], cfg["clock_id"], [cfg["zone"]], round(dt * 60),
        require_nominations=nominations, scarcity_usd_mwh=cfg["price_cap_usd_mwh"] if nominations else None,
        band_pct=cfg.get("imbalance_band_pct", 0.05), band_min_kw=cfg.get("imbalance_band_min_kw", 50.0),
        prep_clearing=fl["prep_clearing"], no_rebound=fl["no_rebound"], rebound_hours=cfg.get("rebound_hours", 24.0),
        daily_energy=fl["daily_energy"], tz=TZ, partial_prep=fl["partial_prep"],
        min_partial_kw=cfg.get("min_offer_kw", 50.0), sequential_clearing=fl["sequential_clearing"],
        cap_rebase=fl["cap_rebase"] and fl["no_rebound"], publish_room=fl["publish_room"],
        price_mode=fl["price_mode"], price_rounds=int(cfg.get("price_rounds_per_gate", 20)),
        price_spread_usd_mwh=first_day_da_spread(s) if fl["price_mode"] else None,
        price_cap_usd_mwh=peak_price_cap(cfg, fl["price_mode"]),
        price_rb_mu=cfg.get("price_rb_mu", 10.0), price_rb_tau=cfg.get("price_rb_tau", 2.0),
        price_rho_floor=cfg.get("price_rho_floor", 1000.0), price_rho_cap=cfg.get("price_rho_cap", 10.0),
        price_tol_excess_kw=cfg.get("price_tol_excess_mw", 1.0) * 1000.0,
        price_tol_dual_usd_mwh=cfg.get("price_tol_dual_usd_mwh", 6.0), price_streak=cfg.get("price_streak", 3),
        settlement_reference=cfg.get("settlement_reference", "recorded"),
        settlement_rule=cfg.get("settlement_rule", "today"),         # D76 max rule (Leaning; header only when not today)
        refusal_notice=bool(cfg.get("refusal_notice", False)),        # D69 rule 1 amendment (off by default)
        delivery_record=bool(cfg.get("delivery_record", False)),      # D64/D65 (off by default; D61 runs set it)
        silent_reference=bool(cfg.get("silent_reference", False)),    # D78/D79 (off by default)
        declared_reference=bool(cfg.get("declared_reference", False)),  # D80/D81 (off by default; one flag)
        **({"settle_from_utc": window_start_utc} if window_start_utc is not None else {}),   # warm-up (off)
        **({"guarantee": True, "guarantee_wear_usd_mwh": float(cfg["wear_usd_mwh"])}
           if headroom_mw is not None and cfg.get("d62_guarantee") else {}))
    settle_1p = ex is not None and fl["price_mode"] and ex.settlement_reference == "selfish_1p"   # D56
    d62 = ex is not None and bool(cfg.get("d62_guarantee", False))     # D62 (Leaning), off; Exchange checks it needs 1p
    margin = float(cfg.get("peak_response_margin_usd_mwh", 0.0))
    if margin < 0:
        raise ValueError("peak_response_margin_usd_mwh must be >= 0")
    header = {**run_header("sim/run_m1.py", cfg, cfg["seed"]), "run_id": run_id, "variant": variant,
              "headroom_mw": headroom_mw, "rounds": rounds if headroom_mw is not None else None,
              "roster": [str(s.ids[i]) for i in part]}
    if m2a:
        header["m2a"] = {**{k: fl[k] for k in M2A_FLAGS}, "risk_premium_usd_mwh": premium, "off": list(off)}
    if ex:
        header["exchange"] = ex.config
    if fl["price_mode"]:      # D58 test: the agents' forecast-risk margin (0 = today's behaviour)
        header["peak_response_margin_usd_mwh"] = margin
    if d62:
        header["d62_guarantee"] = True
    if span != "horizon":     # only when set, so runs without it keep their header unchanged
        header["nomination_span"] = span
    if window_start_utc is not None:      # warm-up: only when set
        header["warmup"] = {"warmup_days": warmup_days(cfg), "window_start_utc": window_start_utc}
    attackers = list(cfg.get("attackers") or [])           # D61: wrappers around honest agents, named in the header
    if attackers and ex is not None:
        header["attackers"] = attackers
    logger = TraceLogger(out_dir / variant / "trace.jsonl", header)
    referee = ex is not None and fl["prep_clearing"] and fl["no_rebound"]
    # fix 1 (memory): the summary only counts these types; the D34 referee reads pre-award nominations in full
    slim = BUS_LOG_SLIM - ({"nominate"} if referee else set())
    bus = Bus(logger, ex, planted_channels=cfg.get("planted_channels") or (), slim=slim)     # D69 rules 1 and 3

    def meter_send(raws):
        bus.send(raws, cfg["metering_id"])

    def clock_send(raws):
        bus.send(raws, cfg["clock_id"])

    a = s.agent
    pv, curt, load = np.nan_to_num(a["pv"][part]), a["curt"][part], a["load"][part]
    local_dates = [str(d) for d in clock.local.date]
    usable = 0.8 * fleet.CAP
    demo_pv = demonstrated_pv(prev, part, fleet.P)
    agents, clipping = [], {}
    for j, i in enumerate(part):
        b = Battery(P=fleet.P[j], CAP=fleet.CAP[j], ETA=fleet.ETA[j], SD=fleet.SD[j], POI=fleet.POI[j],
                    s_term=fleet.S0[j])
        budget = None
        if fl["daily_energy"]:     # usable storage + conservative PV forecast, never above what was demonstrated
            pv_ok = np.minimum(np.maximum(pv[j] - curt[j], 0.0), fleet.P[j]) * dt
            pv_by_date = {d: float(pv_ok[np.array(local_dates) == d].sum()) for d in sorted(set(local_dates))}
            budget, cut = daily_budgets(float(usable[j]), pv_by_date, float(demo_pv[j]),
                                        cfg.get("pv_conservative_factor", 0.5))
            if cut:
                clipping[str(s.ids[i])] = cut
        ag = BatteryAgent(s.ids[i], b, pv[j], curt[j], load[j], iso, end_iso, dt, run_id, cfg["zone"],
                          participate=ex is not None, wear=cfg["wear_usd_mwh"], min_offer_kw=cfg["min_offer_kw"],
                          firm_offers=fl["firm_offers"], send_prep=fl["prep_clearing"], no_rebound=fl["no_rebound"],
                          daily_budget=budget, local_dates=local_dates, premium=premium,
                          prep_hours=cfg.get("rebound_hours", 24.0),
                          rebound_priority=cfg.get("rebound_priority", "grid"),
                          independent_offers=fl["sequential_clearing"],
                          release_superseded_lock=fl["release_superseded_lock"],
                          cap_rebase=fl["cap_rebase"] and fl["no_rebound"],
                          plan_to_nomination=fl["plan_to_nomination"], use_room=fl["publish_room"],
                          price_mode=fl["price_mode"], prox_eps_usd_mwh=cfg.get("prox_deadzone_usd_mwh", 0.05),
                          peak_response_margin_usd_mwh=margin)
        if ex is not None:
            for spec in attackers:
                if spec.get("agent") == ag.id:
                    ag = wrap_attacker(spec, ag, cfg)
        agents.append(ag)
        bus.attach(ag.id, ag)
    op, fc_op = None, operator_forecast(s, part, cfg["forecast_noise_sigma"], cfg["seed"])
    if ex:
        op = GridOperator(cfg["operator_id"], iso, end_iso, clock.local, run_id, cfg["zone"], headroom_mw * 1000.0,
                          cfg["layer_mw"] * 1000.0, cfg["price_cap_usd_mwh"], cfg["post_hour_local"],
                          round(cfg["gate_lead_h"] / dt), rounds=rounds, nominations=nominations,
                          nom_lead_steps=round(cfg.get("nomination_lead_h", 1) / dt),
                          idle_net=(pv - curt - load).sum(0), info_hour=cfg["price_publish_hour_local"],
                          prep_clearing=fl["prep_clearing"], priorities=fl["sequential_clearing"],
                          price_mode=fl["price_mode"], nomination_span=span)
        bus.attach(op.id, op)

    n = len(part)
    soc = fleet.S0.copy()
    idx_of = {ag.id: j for j, ag in enumerate(agents)}
    referee_mode = cfg.get("referee", "abort")
    failures = [] if referee_mode == "collect" else None
    base_x = pv - curt - load
    if ex is not None and ex.declared_reference:     # D82: the exchange checks each declaration with 1p's model
        ex.declaration_check = make_declaration_check(ex, bus, agents, usable, fleet.S0, pv - curt, base_x,
                                                      float(cfg["wear_usd_mwh"]), dt, iso)
    round_steps = op.price_round_steps() if (op and fl["price_mode"]) else {}
    round_secs, round_wall = [], []     # round_wall: wall-clock UTC per price round, to spot sleep stalls
    paths, q1 = None, {}                # D62: each agent's modeled paths, and 1p's plan at the first binding call

    def judge(n_before):
        new = [m for m in bus.log[n_before:] if m["msg_type"] == "award"]
        if referee and new:
            try:
                referee_awards(new, bus.log, k, soc, fleet, idx_of, base_x, iso, dt, collect=failures,
                               band_pct=cfg.get("imbalance_band_pct", 0.05),
                               band_min_kw=cfg.get("imbalance_band_min_kw", 50.0))
            except RuntimeError:
                logger.close()
                raise
    C, D, X, SOC = (np.zeros((n, T)) for _ in range(4))
    n_meter = 0
    for k in range(T):
        t = iso[k]
        if k == 0:
            if ex:     # demonstrated limits and the attested owner (D69 rule 6), before anyone registers
                for j, ag in enumerate(agents):
                    if fl["daily_energy"]:
                        n_meter += 1
                        mid = f"{cfg['metering_id']}:{n_meter}"
                    else:          # own id sequence: every other message keeps the id it had before D69
                        mid = f"{cfg['metering_id']}:owner{j + 1}"
                    meter_send([M.encode(M.make("asset_limits", cfg["metering_id"], "exchange", t, {
                        "asset_id": ag.asset_id, "usable_kwh": float(usable[j]), "power_kw": float(fleet.P[j]),
                        "pv_kwh_per_day": float(demo_pv[j]), "owner": ag.legal_entity, "agent_id": ag.id},
                        mid, run_id))])
                bus.flush()
            if ex and fl["price_mode"]:      # D53 refund reference: each agent's recorded site net (dataset schedule)
                rec_site = base_x + a["bess"][part]
                for j, ag in enumerate(agents):
                    n_meter += 1
                    meter_send([M.encode(M.make("reference_net", cfg["metering_id"], "exchange", t, {
                        "asset_id": ag.asset_id, "window": {"start_utc": iso[0], "end_utc": end_iso},
                        "net_kw": [float(v) for v in rec_site[j]]}, f"{cfg['metering_id']}:{n_meter}", run_id))])
                bus.flush()
            for ag in agents:
                bus.send(ag.start(0), ag.id)
            bus.flush()
            if ex:      # every roster agent must be in the market, or the run is not worth finishing (CLAUDE.md)
                regs = {m["msg_id"]: m["sender"] for m in bus.log if m["msg_type"] == "register"}
                out = [regs[m["payload"]["ref_msg_id"]] for m in bus.log
                       if m["msg_type"] == "reject" and m["payload"]["ref_msg_id"] in regs]
                if out:
                    logger.close()
                    raise RuntimeError(f"roster agents not registered, run stopped: {sorted(out)}")
        if ex:
            n_before = len(bus.log)
            clock_send([M.encode(M.make("tick", cfg["clock_id"], "exchange", t, {}, f"clock:{k}", run_id))])
            bus.flush()
            judge(n_before)
            if fl["sequential_clearing"]:      # D42: one need per sub-step; winners resize between clearings
                at_gate = sum(1 for m in bus.log if m["msg_type"] == "post_need"
                              and m["sender"] == cfg["operator_id"] and m["payload"]["gate_utc"] == t)
                vv, pp_ = prices.at(k)
                for sub in range(1, at_gate):
                    for j, ag in enumerate(agents):
                        bus.send(ag.between_clearings(k, soc[j], vv, pp_), ag.id)
                    bus.flush()
                    n_before = len(bus.log)
                    clock_send([M.encode(M.make("tick", cfg["clock_id"], "exchange", t, {"substep": sub},
                                              f"clock:{k}.{sub}", run_id))])
                    bus.flush()
                    judge(n_before)
        if op:
            bus.send(op.act(k, fc_op), op.id)
            bus.flush()
        v, pf = prices.at(k)
        req = np.zeros((3, n))
        for j, ag in enumerate(agents):
            outs, req[:, j] = ag.act(k, soc[j], v, pf)
            bus.send(outs, ag.id)
        bus.flush()
        if k in round_steps:       # D53: price rounds before the call's deadline; step k is already dispatched
            binding_gate = round_steps[k][1]
            if settle_1p and (binding_gate or ex.silent_reference):
                # D56 rule 1p: before the binding call's first round, the metering service sends each asset's selfish
                # reference for the call's window, from registration, recorded data and the prices published now.
                # D79 (silent_reference): the same for every call that runs price rounds, binding or not
                call = [m["payload"] for m in bus.log if m["msg_type"] == "call_nominations"
                        and m["sender"] == "exchange" and m["payload"]["binding"] == binding_gate
                        and m["payload"]["deadline_utc"] == iso[round_steps[k][0]]][-1]
                w = call["window"]
                a0 = iso.index(w["start_utc"])
                a1 = T if w["end_utc"] == end_iso else iso.index(w["end_utc"])
                reg_kw = {m["sender"]: float(m["payload"]["mandate"]["max_kw"]) for m in bus.log
                          if m["msg_type"] == "register"}
                for j, ag in enumerate(agents):
                    if d62 and binding_gate:    # the same LP as selfish_site_net, kept as a Plan for D62's paths
                        pl = selfish_plan(reg_kw[ag.id], usable[j], fleet.S0[j], pf, pv[j] - curt[j], base_x[j],
                                          float(cfg["wear_usd_mwh"]), dt)
                        q = None if pl is None else base_x[j] + pl.d - pl.c - pl.x
                        if paths is None:
                            q1[j] = pl
                    else:
                        q = selfish_site_net(reg_kw[ag.id], usable[j], fleet.S0[j], pf, pv[j] - curt[j], base_x[j],
                                             float(cfg["wear_usd_mwh"]), dt)
                    if q is None:
                        logger.close()
                        raise RuntimeError(f"D56: no selfish reference plan for {ag.id} at {t}")
                    extra = {}
                    if ex.declared_reference:
                        # D80: the agent's accepted declarations (as of now) cap power and floor SOC in 1p's planner;
                        # D81: the registered-limits reference goes along, for the void rule at the closing tick
                        pmax, floor = ex.declared_limits(ag.asset_id, iso, t)
                        q_reg = q
                        if not (np.all(np.isnan(pmax)) and np.all(np.isnan(floor))):
                            q = selfish_site_net(reg_kw[ag.id], usable[j], fleet.S0[j], pf, pv[j] - curt[j],
                                                 base_x[j], float(cfg["wear_usd_mwh"]), dt, np.asarray(pmax, float),
                                                 np.asarray(floor, float))
                        extra = {"net_kw_registered": [float(x) for x in q_reg[a0:a1]]}
                        if q is None:   # D82 backstop: never stop the run for one agent; void its declarations here
                            q = q_reg
                            extra["declarations_void"] = True
                    n_meter += 1
                    meter_send([M.encode(M.make("selfish_reference", cfg["metering_id"], "exchange", t, {
                        "asset_id": ag.asset_id, "call_id": call["call_id"], "window": w,
                        "net_kw": [float(x) for x in q[a0:a1]], **extra}, f"{cfg['metering_id']}:{n_meter}",
                        run_id))])
                n_before = len(bus.log)
                bus.flush()
                bad = [m["payload"] for m in bus.log[n_before:] if m["msg_type"] == "reject"]
                if bad:        # settlement would fail closed later; stop now
                    logger.close()
                    raise RuntimeError(f"D56: selfish_reference rejected: {bad[:3]}")
            tg = time.time()
            for rr in range(1, int(cfg.get("price_rounds_per_gate", 20)) + 1):
                tr = time.time()
                n_before = len(bus.log)
                clock_send([M.encode(M.make("tick", cfg["clock_id"], "exchange", t, {"price_round": rr},
                                          f"clock:{k}.p{rr}", run_id))])
                bus.flush()
                sent = [m for m in bus.log[n_before:] if m["msg_type"] == "peak_price"]
                if not sent:
                    raise RuntimeError(f"price round {rr} at step {k} sent no peak_price")
                for j, ag in enumerate(agents):
                    bus.send(ag.between_price_rounds(k, soc[j], v, pf), ag.id)
                bus.flush()
                round_secs.append(time.time() - tr)
                round_wall.append(datetime.now(timezone.utc).isoformat(timespec="seconds"))
                print(f"    round {rr} at {t}: wall {round_wall[-1]}", flush=True)
                if sent[-1]["payload"]["final"]:
                    break
            print(f"  price rounds at {t}: {rr} rounds, {time.time() - tg:.1f} s", flush=True)
            if d62 and round_steps[k][1]:
                # D62: after the binding call's final price, the metering service sends each asset's stage of the
                # modeled response P* and acting-alone path Q*, from the call's deadline to the slice end
                fin = sent[-1]["payload"]
                if not fin["final"]:
                    raise RuntimeError(f"D62: the price rounds at step {k} ended without a final price")
                k0 = round_steps[k][0]
                peak = np.zeros(T)
                w0 = iso.index(fin["window"]["start_utc"])
                peak[w0:w0 + len(fin["price_usd_mwh"])] = fin["price_usd_mwh"]
                first = paths is None
                if first:
                    reg_kw = {m["sender"]: float(m["payload"]["mandate"]["max_kw"]) for m in bus.log
                              if m["msg_type"] == "register"}
                    paths = [ModeledPaths(reg_kw[ag.id], usable[j], fleet.S0[j], pv[j] - curt[j], base_x[j],
                                          float(cfg["wear_usd_mwh"]), dt) for j, ag in enumerate(agents)]
                for j, ag in enumerate(agents):
                    st = paths[j].stage(k0, pf, peak, q1[j] if first else None)
                    n_meter += 1
                    meter_send([M.encode(M.make("modeled_response", cfg["metering_id"], "exchange", t, {
                        "asset_id": ag.asset_id, "call_id": fin["call_id"],
                        "window": {"start_utc": iso[k0], "end_utc": end_iso},
                        **{key: ([float(x) for x in v] if isinstance(v, np.ndarray) else v) for key, v in st.items()}},
                        f"{cfg['metering_id']}:{n_meter}", run_id))])
                n_before = len(bus.log)
                bus.flush()
                bad = [m["payload"] for m in bus.log[n_before:] if m["msg_type"] == "reject"]
                if bad:
                    logger.close()
                    raise RuntimeError(f"D62: modeled_response rejected: {bad[:3]}")
        c, d, extra, soc = physics.step(fleet, soc, req[0], req[1], pv[:, k], curt[:, k] + req[2], load[:, k], dt)
        C[:, k], D[:, k], X[:, k], SOC[:, k] = c, d, req[2] + extra, soc
        site_net = pv[:, k] - curt[:, k] - X[:, k] + d - c - load[:, k]
        for j, ag in enumerate(agents):
            n_meter += 1
            payload = {"asset_id": ag.asset_id, "soc_kwh": float(soc[j]), "meter_kw": float(d[j] - c[j])}
            if nominations:
                payload["net_kw"] = float(site_net[j])
            meter_send([M.encode(M.make("telemetry", cfg["metering_id"], "exchange", t, payload,
                                      f"{cfg['metering_id']}:{n_meter}", run_id))])
        bus.flush()
    if d62:      # D62: the metering service relays the real-time prices the guarantee is valued at
        n_meter += 1
        meter_send([M.encode(M.make("rt_price", cfg["metering_id"], "exchange", end_iso, {
            "window": {"start_utc": iso[0], "end_utc": end_iso},
            "rt_usd_mwh": [float(v) for v in s.system["price_rt"]],
            "end_energy_usd_kwh": float(s.system["price_da"].mean()) / 1000.0}, f"{cfg['metering_id']}:{n_meter}",
            run_id))])
        bus.flush()
    if ex:   # settle windows that end with the slice (and, with the rebound cap, everything still open)
        last = {"final": True} if (fl["no_rebound"] or fl["price_mode"]) else {}
        clock_send([M.encode(M.make("tick", cfg["clock_id"], "exchange", end_iso, last, f"clock:{T}", run_id))])
        bus.flush()
    logger.close()
    if bus.refused:          # D69: refused messages (forged sender, direct delivery, malformed), outside the summary
        with open(out_dir / variant / "refused.json", "w", encoding="utf-8") as f:
            json.dump(bus.refused, f, indent=1)
    margin_log = [{"agent": ag.id, **e} for ag in agents for e in ag.margin_log]
    if margin_log:
        with open(out_dir / variant / "agent_log.jsonl", "w", encoding="utf-8") as f:
            f.writelines(json.dumps(e) + "\n" for e in margin_log)
    edge = band_edge(ex, window_start_utc) if ex is not None and nominations else None
    return {"variant": variant, "headroom_mw": headroom_mw, "C": C, "D": D, "X": X, "SOC": SOC, "fleet": fleet,
            "band_edge": edge,
            "log": bus.log, "refused": bus.refused, "agents": agents, "fc_op": fc_op, "operator": op, "rounds": rounds,
            "nominations": nominations and ex is not None, "header": header, "m2a": m2a, "v2": v2, "premium": premium,
            "daily_clipping": clipping, "referee_failures": failures, "referee_on": referee,
            "price_log": ex.price_log if (ex and fl["price_mode"]) else None,
            "peak_settled": dict(ex.peak_settled) if (ex and fl["price_mode"]) else None,
            "settled_prices": settled_prices(ex) if (ex and fl["price_mode"]) else None,
            "settle_paths": dict(ex.settle_paths) if (ex and getattr(ex, "settle_paths", None)) else None,
            "price_round_secs": round_secs, "price_round_wall_utc": round_wall, "margin_log": margin_log,
            "margin_usd_mwh": margin if fl["price_mode"] else None,
            "margin_compare_secs": float(sum(ag.margin_compare_secs for ag in agents)),
            "runtime_s": time.time() - tic,
            "wall_utc": {"start": wall_start, "end": datetime.now(timezone.utc).isoformat(timespec="seconds")}}


def bus_refusals(refused) -> dict:
    """D69: count of messages the bus refused, by reason (summary field; refused messages are never delivered)."""
    refused = refused or []
    return {"total": len(refused), "by_reason": dict(sorted(Counter(x["reason"] for x in refused).items()))}


def evaluate(cfg, s: Slice, part, r: dict, rec_net, m0, selfish_run=None) -> dict:
    a = s.agent
    clock = Clock(s.ts_utc, TZ)
    K = clock.dt_h / 1000.0
    pv, curt, load, bess = np.nan_to_num(a["pv"][part]), a["curt"][part], a["load"][part], a["bess"][part]
    C, D, X = r["C"], r["D"], r["X"]
    net = rec_net + (pv - curt + bess).sum(0) - (pv - curt - X + D - C).sum(0)
    rt = s.system["price_rt"]
    energy = (rt * K * (pv - curt - X + D - C - load)).sum(1)
    wear = cfg["wear_usd_mwh"] * K * D.sum(1)
    f = r["fleet"]
    out = {**peak_info(net, clock), "share_of_m0_ceiling": share(net.max(), m0),
           "energy_revenue_usd": float(energy.sum()), "wear_usd": float(wear.sum()),
           "extra_curtail_kwh": float(X.sum() * clock.dt_h),
           "soc_within_10_90": bool(((r["SOC"] >= f.s_min[:, None] - 1e-6) & (r["SOC"] <= f.s_max[:, None] + 1e-6)).all()),
           "runtime_s": round(r["runtime_s"], 1), "wall_utc": r.get("wall_utc")}
    out["_net"], out["_agent_value"] = net, energy - wear
    if r.get("v2"):
        out["value_share_of_m0_ceiling"] = value_share(cfg, s, part, energy - wear, m0)
    if r["headroom_mw"] is None:
        return out

    log = r["log"]
    needs = {m["msg_id"]: m["payload"] for m in log if m["msg_type"] == "post_need" and m["sender"] == cfg["operator_id"]}
    awards = [m for m in log if m["msg_type"] == "award"]
    settles = [m["payload"] for m in log if m["msg_type"] == "settle"]
    iso_pos = {t: i for i, t in enumerate(clock.iso)}
    T = len(clock.iso)

    def mask(w):
        m = np.zeros(T, bool)
        m[iso_pos[w["start_utc"]]:iso_pos.get(w["end_utc"], T)] = True
        return m

    in_window = np.zeros(T, bool)
    need_kwh = awarded_kwh = unfilled_kwh = 0.0
    awarded_by_need = Counter()
    for m in awards:
        awarded_by_need[m["payload"]["need_id"]] += m["payload"]["accredited_kw"]
    for nid, p in needs.items():
        w = mask(p["window"])
        in_window |= w
        h = w.sum() * clock.dt_h
        need_kwh += p["kw_needed"] * h
        awarded_kwh += awarded_by_need[nid] * h
        unfilled_kwh += max(p["kw_needed"] - awarded_by_need[nid], 0.0) * h
    outside = np.where(in_window, -np.inf, net)
    ko = int(outside.argmax())
    credits = Counter()
    charges = Counter()
    for m in log:
        if m["msg_type"] == "settle":
            credits[m["to"][0]] += m["payload"]["credit_usd"]
            charges[m["to"][0]] += m["payload"]["charge_usd"]
    ids = [ag.id for ag in r["agents"]]
    total_value = out["_agent_value"] + np.array([credits[i] - charges[i] for i in ids])
    out["_agent_value"] = total_value
    prices = [m["payload"]["price_usd_mwh"] for m in awards]
    local_day = lambda t: str(pd.Timestamp(t).tz_convert(TZ).date())
    per_day = Counter((local_day(m["t_utc"]), m["msg_type"]) for m in log)
    out.update({
        "needs": len(needs), "need_kwh": need_kwh, "awarded_kwh": awarded_kwh, "unfilled_need_kwh": unfilled_kwh,
        "awards": len(awards),
        "clearing_price_usd_mwh": {"min": min(prices), "max": max(prices), "mean": float(np.mean(prices))} if prices else None,
        "delivered_kwh": float(sum(p["delivered_kw"] * mask(needs[p["need_id"]]["window"]).sum() * clock.dt_h for p in settles)),
        "credits_usd": float(sum(credits.values())), "charges_usd": float(sum(charges.values())),
        "rebound": {"peak_outside_windows_mw": float(outside[ko]) / 1e3 if np.isfinite(outside[ko]) else None,
                    "at_utc": clock.iso[ko], "at_central": str(clock.local[ko]),
                    "peak_moved_outside_windows": bool(not in_window[int(net.argmax())])},
        "rejects_by_code": dict(Counter(m["payload"]["code"] for m in log if m["msg_type"] == "reject")),
        "messages_per_day": {d: {t: c for (dd, t), c in sorted(per_day.items()) if dd == d}
                             for d in sorted({d for d, _ in per_day})},
        "messages_total": len(log),
        # D69: messages the bus refused (forged sender, direct delivery, malformed), by reason. Not delivered, so
        # never in the message counts above.
        "bus_refusals": bus_refusals(r.get("refused")),
    })
    out["_in_window"] = in_window
    if r["nominations"]:
        out.update(nomination_metrics(r, log, needs, awards, net, ids, clock, mask))
        imb = out.pop("_imb_by_agent")
        out["_agent_value"] = out["_agent_value"] - np.array([imb.get(i, 0.0) for i in ids])
    if r.get("v2"):
        reb = Counter()
        for m in log:
            if m["msg_type"] == "rebound":
                reb[m["to"]] += m["payload"]["charge_usd"]
        out["_agent_value"] = out["_agent_value"] - np.array([reb.get(i, 0.0) for i in ids])
        peak = Counter()
        for m in log:
            if m["msg_type"] == "peak_settle":
                peak[m["to"]] += m["payload"]["net_usd"]
        out["_agent_value"] = out["_agent_value"] + np.array([peak.get(i, 0.0) for i in ids])
        out.update(m2a_metrics(cfg, s, part, r, selfish_run, log, net, in_window, ids, clock, credits, charges, reb,
                               peak))
        if r.get("price_log") is not None:
            out["price_mode"] = price_summary(cfg, r, log)
        acc = {m["to"]: m["payload"] for m in log if m["msg_type"] == "guarantee_accrual"}
        if acc:          # D62 (report only here; the test's verdict is bench/d62_guarantee.py)
            out["guarantee"] = {"by_agent": acc, "shortfall_usd_total": float(sum(a["shortfall_usd"] for a in acc.values())),
                                "positive_shortfall_usd": float(sum(max(a["shortfall_usd"], 0.0) for a in acc.values()))}
    return out


def value_share(cfg, s: Slice, part, part_value, m0):
    """Fleet value (RT energy revenue net of wear, all 80 agents; non-participants at recorded dispatch) as a share
    of M0's value gain: (run - recorded) / (ceiling - recorded)."""
    if m0 is None:
        return None
    a = s.agent
    K = s.dt_h / 1000.0
    others = np.setdiff1d(np.arange(len(s.ids)), part)
    rec_val = lambda idx: float((s.system["price_rt"] * K * a["pnet"][idx]).sum()
                                - cfg["wear_usd_mwh"] * K * np.maximum(a["bess"][idx], 0).sum())
    run_val = float(np.sum(part_value)) + (rec_val(others) if len(others) else 0.0)
    base, ceil = m0["baseline_recorded"]["value_usd"], m0["ceiling_15min"]["value_usd"]
    return (run_val - base) / (ceil - base)


def margin_summary(r) -> dict:
    """D58 test: the agents' margin and how often it bound (entries in <variant>/agent_log.jsonl)."""
    ml = r.get("margin_log") or []
    by_gate = {}                     # each gate's final round: the plan that is nominated (and settled if binding)
    for e in ml:
        if e.get("final_round"):
            g = by_gate.setdefault(e["t_utc"], {"agents_bound": 0, "declined_mwh": 0.0})
            g["agents_bound"] += 1
            g["declined_mwh"] += e["declined_mwh"]
    return {"peak_response_margin_usd_mwh": r.get("margin_usd_mwh"), "log_entries": len(ml),
            "agents_bound": len({e["agent"] for e in ml}), "final_round_by_gate": by_gate,
            "compare_solve_secs": r.get("margin_compare_secs", 0.0),
            "note": "declined = shifted MWh (sum |site net - selfish anchor| x dt, both legs) of the m = 0 plan minus "
                    "the margin plan's at the same re-plan; per gate, only the final round's plans count"}


def settled_prices(ex) -> dict[str, float]:
    """Per settled step, the price peak settlement paid at ($/MWh). Today's rule: the last binding call's. Under
    settlement_rule max_first_call (D76): the highest price among the binding calls that priced the step (the price
    first-call relief is paid at), over every step any of them priced."""
    if getattr(ex, "settlement_rule", "today") == "max_first_call":
        return {t: max(ex.call_price[c][t] for c in cids) for t, cids in ex.settle_chain.items()}
    return {t: p for t, (p, _) in ex.peak_settled.items()}


def price_summary(cfg, r, log) -> dict:
    """D53: price rounds per gate, the settled price, and the operator's net payout (refunds - charges)."""
    rows = r["price_log"]
    gates = []
    for cid in dict.fromkeys(x["call_id"] for x in rows):
        g = [x for x in rows if x["call_id"] == cid]
        bc = [x for x in g if not x.get("closing")]
        close = next((x for x in g if x.get("closing")), None)
        gates.append({"call_id": cid, "binding": g[0]["binding"], "t_utc": g[0]["t_utc"], "rounds": len(bc),
                      "converged": any(x.get("converged") for x in bc),
                      "rho_start": bc[0]["rho"] if bc else None, "rho_end": bc[-1]["rho_next"] if bc else None,
                      "max_pi_usd_mwh": max((x["max_pi_usd_mwh"] for x in bc), default=0.0),
                      "rounds_at_cap": sum(1 for x in bc if x["at_cap_steps"] > 0),
                      "final_max_excess_mw": close["max_excess_kw"] / 1e3 if close else None,
                      "final_dual_res_usd_mwh": close["dual_res_usd_mwh"] if close else None})
    sp = r.get("settled_prices")
    if sp is None:          # results built before settled_prices existed
        sp = {t: p for t, (p, _) in (r["peak_settled"] or {}).items()}
    settled = [p for p in sp.values() if p > 1e-9]
    ps = [m["payload"] for m in log if m["msg_type"] == "peak_settle"]
    charges, refunds = sum(p["charge_usd"] for p in ps), sum(p["refund_usd"] for p in ps)
    secs = r.get("price_round_secs") or []
    return {"rounds_per_gate_cap": int(cfg.get("price_rounds_per_gate", 20)),
            "round_secs": {"mean": float(np.mean(secs)) if secs else None, "total": float(np.sum(secs))},
            "rounds_basis": cfg.get("price_rounds_basis"),
            "round_wall_utc": r.get("price_round_wall_utc") or [],
            "margin": margin_summary(r),
            "gates": gates, "rounds_total": sum(g["rounds"] for g in gates),
            "rounds_at_cap": sum(g["rounds_at_cap"] for g in gates),
            "max_peak_price_usd_mwh": max((g["max_pi_usd_mwh"] for g in gates), default=0.0),
            "settled": {"priced_steps": len(settled), "max_usd_mwh": max(settled, default=0.0),
                        "mean_usd_mwh": float(np.mean(settled)) if settled else 0.0,
                        **({"basis": "highest price among the binding calls that priced each step (D76 max rule)",
                            "agent_steps_by_path": r.get("settle_paths")}
                           if cfg.get("settlement_rule") == "max_first_call" else {})},
            "charges_usd": charges, "refunds_usd": refunds, "operator_net_payout_usd": refunds - charges,
            "log": rows}


def m2a_metrics(cfg, s, part, r, sf, log, net, in_window, ids, clock, credits, charges, reb, peak=None) -> dict:
    """Net harm by component, skipped offers and price effect, and the target checks."""
    a = s.agent
    K, dt = clock.dt_h / 1000.0, clock.dt_h
    rt, da = s.system["price_rt"], s.system["price_da"]
    deg = cfg["wear_usd_mwh"]
    out = {}
    imb = Counter()
    for m in log:
        if m["msg_type"] == "imbalance":
            imb[m["to"]] += m["payload"]["charge_usd"]
    end_price = float(da.mean()) / 1000.0           # $/kWh for energy left in storage at the end of the slice
    if sf is not None:
        rows = []
        for j, aid in enumerate(ids):
            dn = (r["D"][j] - r["C"][j]) - (sf["D"][j] - sf["C"][j])           # battery net change, kW
            dx = r["X"][j] - sf["X"][j]                                         # extra curtailment, kW
            dwear = deg * K * (r["D"][j].sum() - sf["D"][j].sum())
            end_kwh = sf["SOC"][j, -1] - r["SOC"][j, -1]                       # energy left behind vs selfish
            # warm-up: energy each arm brought into the window, valued like the energy left at the end
            start = ({"start_energy": float(r["SOC_start"][j] - sf["SOC_start"][j]) * end_price}
                     if r.get("SOC_start") is not None else {})
            comp = {"lost_arbitrage_net_of_credits": -(float(da @ dn) * K - dwear) - credits[aid],
                    "da_rt_gap": -float((rt - da) @ dn) * K,
                    "extra_curtailment": float(rt @ dx) * K,
                    "shortfall_charges": charges[aid], "imbalance_charges": imb[aid], "rebound_charges": reb[aid],
                    "peak_price_net": -(peak or {}).get(aid, 0.0), "end_energy": end_kwh * end_price, **start}
            rows.append({"agent": aid, **comp, "total": sum(comp.values()), "end_kwh": end_kwh})
        out["_components"] = rows
    if r.get("referee_on"):
        fails = r.get("referee_failures")
        short_needs = {(m["to"][0], m["payload"]["need_id"]) for m in log
                       if m["msg_type"] == "settle" and m["payload"]["charge_usd"] > 0.005}
        typed = []
        for f in fails or []:
            kind = ("short_at_settlement" if any((f["agent"], nid) in short_needs for nid in f["failing_needs"])
                    else "referee_only")
            typed.append({**f, "type": kind})
        out["referee"] = {"mode": "collect" if fails is not None else "abort",
                          "failures": len(typed), "by_type": dict(Counter(f["type"] for f in typed)),
                          "agents": sorted({f["agent"] for f in typed}), "detail": typed[:50]}
    needs_all = {m["msg_id"]: m["payload"] for m in log if m["msg_type"] == "post_need" and m["sender"] == cfg["operator_id"]}
    hours = lambda w: (pd.Timestamp(w["end_utc"]) - pd.Timestamp(w["start_utc"])).total_seconds() / 3600
    prep_kwh, deliv_kwh = Counter(), Counter()
    for m in log:
        if m["msg_type"] == "award" and m["payload"].get("prep"):
            prep_kwh[m["to"][0]] += sum(m["payload"]["prep"]["kw"]) * dt
        elif m["msg_type"] == "settle" and m["payload"]["need_id"] in needs_all:
            deliv_kwh[m["to"][0]] += m["payload"]["delivered_kw"] * hours(needs_all[m["payload"]["need_id"]]["window"])
    ratios = {aid: prep_kwh[aid] / deliv_kwh[aid] for aid in deliv_kwh if deliv_kwh[aid] > 1e-6}
    vals = np.array(list(ratios.values())) if ratios else np.array([np.nan])
    out["band_edge"] = r.get("band_edge")
    out["prep_vs_delivered"] = {
        "total_prep_kwh": round(float(sum(prep_kwh.values())), 1), "total_delivered_kwh": round(float(sum(deliv_kwh.values())), 1),
        "per_agent_ratio": {"median": round(float(np.nanmedian(vals)), 2), "p90": round(float(np.nanpercentile(vals, 90)), 2),
                            "max": round(float(np.nanmax(vals)), 2), "agents": len(ratios)},
        "by_agent": {a: round(v, 2) for a, v in sorted(ratios.items())}}
    reports = [m["payload"] for m in log if m["msg_type"] == "clearing_report"]
    skipped = Counter(reason for rp in reports for _, reason in rp["skipped"])
    diffs = [rp["price_usd_mwh"] - rp["price_without_prep_check"] for rp in reports
             if rp["price_usd_mwh"] is not None and rp["price_without_prep_check"] is not None]
    out["prep_clearing"] = {
        "needs_cleared": len(reports), "offers_skipped": sum(skipped.values()), "skipped_by_reason": dict(skipped),
        "offers_accepted_in_part": sum(len(rp.get("partial", [])) for rp in reports),
        "needs_price_changed": sum(1 for d in diffs if abs(d) > 1e-9),
        "price_effect_usd_mwh": {"mean": float(np.mean(diffs)), "max": float(np.max(diffs))} if diffs else None,
        "needs_unfilled_only_because_of_skips": sum(1 for rp in reports if rp["price_usd_mwh"] is None
                                                    and rp["price_without_prep_check"] is not None),
    }
    sys_t = r["operator"].system_target() if r["operator"] else None
    if sys_t is not None:
        first_gate = r["operator"].days[0]["gates"][0]
        steps = np.arange(len(net))
        check = (steps >= first_gate) & ~in_window & ~np.isnan(sys_t)
        excess = np.where(check, net - sys_t, -np.inf)
        k_ = int(excess.argmax())
        pre = steps < first_gate
        day1_t = r["operator"].days[0]["target"]
        f = r["fleet"]
        agg_band = float(np.maximum(cfg.get("imbalance_band_pct", 0.05) * f.P,
                                    cfg.get("imbalance_band_min_kw", 50.0)).sum())
        out["target_check_metered"] = {
            "rule": "metered system net load at or below the target at every step outside need windows, from the "
                    "first gate on; the pass check (D51) ignores exceedance within the participants' aggregate free "
                    "band (sum of max(band_pct x P, band_min_kw)); the strict count is steps_over",
            "steps_checked": int(check.sum()), "steps_over": over_counts(excess, agg_band)[0],
            "aggregate_band_mw": agg_band / 1e3, "steps_over_beyond_band": over_counts(excess, agg_band)[1],
            "max_excess_mw": float(max(excess[k_], 0.0)) / 1e3 if np.isfinite(excess[k_]) else 0.0,
            "at_central": str(clock.local[k_]),
            "pre_gate": {"steps": int(pre.sum()),
                         "max_excess_vs_day1_target_mw": float(max((net[pre] - day1_t).max(), 0.0)) / 1e3
                         if day1_t is not None and pre.any() else None,
                         "note": "before the first gate no need window can exist; reported, not checked"},
        }
        binding = [m["payload"] for m in log if m["msg_type"] == "nomination_aggregate" and m["payload"].get("binding")]
        nom_ex = []
        for b in binding:
            refc = r["operator"].reforecast(r["fc_op"], b)
            pos = {t: i for i, t in enumerate(clock.iso)}
            idx = np.arange(pos[b["window"]["start_utc"]], pos[b["window"]["start_utc"]] + len(b["kw"]))
            ok = ~in_window[idx] & ~np.isnan(sys_t[idx])
            if ok.any():
                nom_ex.append(float((refc[idx][ok] - sys_t[idx][ok]).max()))
        out["target_check_nominated"] = {"max_excess_mw": max(max(nom_ex), 0.0) / 1e3 if nom_ex else None,
                                         "note": "binding aggregate re-forecast vs target outside need windows"}
    return out


def nomination_metrics(r, log, needs, awards, net, ids, clock, mask) -> dict:
    """Rounds, imbalance, and how well the binding nominations forecast the real net load."""
    rnd = {nid: p.get("round", 1) for nid, p in needs.items()}
    awarded_by_round = Counter()
    for m in awards:
        p = m["payload"]
        awarded_by_round[rnd[p["need_id"]]] += p["accredited_kw"] * mask(p["window"]).sum() * clock.dt_h
    imb = [m for m in log if m["msg_type"] == "imbalance"]
    honest = {ag.id for ag in r["agents"] if getattr(ag, "honest", True)}
    by_agent = Counter()
    for m in imb:
        by_agent[m["to"]] += m["payload"]["charge_usd"]

    def total(rows):
        return {"deviation_beyond_band_kwh": float(sum(m["payload"]["deviation_kwh"] for m in rows)),
                "raw_deviation_kwh": float(sum(m["payload"]["raw_deviation_kwh"] for m in rows)),
                "charge_usd": float(sum(m["payload"]["charge_usd"] for m in rows)),
                "agents_charged": len({m["to"] for m in rows if m["payload"]["charge_usd"] > 0.005})}

    aggs = [m["payload"] for m in log if m["msg_type"] == "nomination_aggregate"]
    binding = [a for a in aggs if a.get("binding")]
    fc_pairs = []
    for a in binding:
        refc = r["operator"].reforecast(r["fc_op"], a)
        w = mask(a["window"])
        fc_pairs.append((refc[w], net[w]))
    gap = None
    if fc_pairs:
        f = np.concatenate([x for x, _ in fc_pairs])
        y = np.concatenate([y for _, y in fc_pairs])
        gap = {"mean_abs_mw": float(np.abs(f - y).mean()) / 1e3, "max_abs_mw": float(np.abs(f - y).max()) / 1e3,
               "forecast_peak_mw": float(f.max()) / 1e3, "actual_peak_mw": float(y.max()) / 1e3,
               "peak_gap_mw": float(f.max() - y.max()) / 1e3, "steps": int(len(f))}
    return {
        "rounds": r["rounds"],
        "needs_by_round": dict(sorted(Counter(rnd.values()).items())),
        "awards_by_round": dict(sorted(Counter(rnd[m["payload"]["need_id"]] for m in awards).items())),
        "awarded_kwh_by_round": {k: float(v) for k, v in sorted(awarded_by_round.items())},
        "imbalance_all": total(imb),
        "imbalance_honest": total([m for m in imb if m["to"] in honest]),
        "imbalance_dishonest": total([m for m in imb if m["to"] not in honest]),
        "agents_nominating_min": min((a["n_agents"] for a in aggs), default=None),
        "binding_forecast_vs_actual": gap,
        "_imb_by_agent": dict(by_agent),
    }


SIDE_BY_SIDE = ["peak_mw", "peak_vs_selfish_mw", "share_of_m0_ceiling", "unfilled_need_kwh", "need_kwh",
                "awarded_kwh", "awards", "credits_usd", "charges_usd", "agents_worse_off_than_selfish", "pass",
                ("net_harm", "agents_harmed"), ("net_harm", "total_net_harm_usd"),
                ("rebound", "peak_outside_windows_mw"), ("rebound", "peak_moved_outside_windows")]


def net_harm(alone: np.ndarray, with_ps: np.ndarray, ids: list[str], roster: list[str], bystanders: int) -> dict:
    """No one is net harmed (PLAN.md section 8). alone: each agent's result acting alone (the selfish run);
    with_ps: its total result with Pearl Street (energy - wear + credits - charges - imbalance). An agent is harmed
    when it falls short by more than max(1% of |alone|, $10). The roster comes from the run header, and the check
    fails if any agent on it is missing (CLAUDE.md)."""
    short = np.asarray(alone, float) - np.asarray(with_ps, float)
    tol = np.maximum(NET_HARM_REL * np.abs(alone), NET_HARM_ABS)
    harmed = short > tol
    missing = sorted(set(roster) - set(ids))
    return {
        "rule": "with Pearl Street >= acting alone - max(1% of acting alone, $10), every honest agent",
        "roster_size": len(roster), "agents_checked": len(ids), "missing_from_check": missing,
        "agents_harmed": int(harmed.sum()), "total_net_harm_usd": float(short[harmed].sum()),
        "by_agent_usd": {i: round(float(max(x, 0.0)), 2) for i, x in zip(ids, short)},
        "bystanders": {"count": bystanders, "bill_increase_usd": 0.0,
                       "note": "non-participants keep recorded dispatch, and prices do not react to agents"},
        "harm_from_attackers": None,
        "pass": not missing and not bool(harmed.any()),
    }


def side_by_side(m1: dict, m11: dict, labels=("m1", "m1_1")) -> dict:
    def get(d, key):
        return (d.get(key[0]) or {}).get(key[1]) if isinstance(key, tuple) else d.get(key)
    a, b = labels
    out = {"selfish_peak_mw": {a: m1["selfish"]["peak_mw"], b: m11["selfish"]["peak_mw"]}}
    for h, ev in m11["exchange_by_headroom_mw"].items():
        old = m1["exchange_by_headroom_mw"].get(h)
        if old is None:
            continue
        out[h] = {(k if isinstance(k, str) else ".".join(k)): {a: get(old, k), b: get(ev, k)} for k in SIDE_BY_SIDE}
    return out


def over_counts(excess_kw, agg_band_kw: float) -> tuple[int, int]:
    """(strict, D51) step counts over target: any excess, and excess beyond the participants' aggregate free band."""
    e = np.asarray(excess_kw, float)
    return int((e > 1e-3).sum()), int((e > agg_band_kw + 1e-3).sum())


def m0_dir(cfg) -> Path:
    """Folder of the M0 ceiling for this config's window (config m0_dir, default runs/m0)."""
    return REPO / cfg.get("m0_dir", "runs/m0")


def series(table: pd.DataFrame, clock: Clock, m0_folder: Path = REPO / "runs" / "m0") -> dict:
    """The net-load table (and the M0 ceiling's net load, if M0 has run) as lists, so the run viewer can plot it
    from summary.json alone. kW, rounded to 0.1."""
    out = {"t_utc": clock.iso, "t_central": list(table.t_central)}
    for c in table.columns[2:]:
        v = table[c]
        out[c] = [bool(x) for x in v] if v.dtype == bool else [round(float(x), 1) for x in v]
    m0_csv = m0_folder / "dual_15min.csv"
    if m0_csv.exists():
        d = pd.read_csv(m0_csv)
        if list(d.t_utc) == clock.iso:
            out["m0_ceiling_kw"] = [round(float(x), 1) for x in d.net_load_peakmin_kw]
    return out


def previous_day(cfg) -> Slice | None:
    """The local day before the slice, for demonstrated limits (metered history). None if the data has no such day."""
    day = pd.Timestamp(cfg["start"]) - pd.Timedelta(days=1)
    try:
        return load_slice(cfg["data_dir"], day.date(), day.date(), cfg["slice_tz"])
    except ValueError:
        return None


COMPONENTS = ("lost_arbitrage_net_of_credits", "da_rt_gap", "extra_curtailment", "shortfall_charges",
              "imbalance_charges", "rebound_charges", "peak_price_net", "end_energy")


def harm_components(rows, nh, alone) -> dict:
    """Net harm by component for harmed agents and for all agents; the parts add up to each total exactly."""
    harmed_ids = {i for i, v in nh["by_agent_usd"].items() if v > 0}
    tol = np.maximum(NET_HARM_REL * np.abs(alone), NET_HARM_ABS)
    harmed = [r for r, t in zip(rows, tol) if r["total"] > t]
    for r, v in ((r, nh["by_agent_usd"][r["agent"]]) for r in rows):
        assert abs(max(r["total"], 0.0) - v) < 0.02, (r["agent"], r["total"], v)
    comps = COMPONENTS + (("start_energy",) if rows and "start_energy" in rows[0] else ())     # warm-up only
    tot = lambda rs: {c: round(float(sum(r[c] for r in rs)), 2) for c in comps + ("total",)}
    return {"harmed_agents": tot(harmed), "all_agents": tot(rows), "sum_check": "components sum to each agent's total",
            "end_kwh_vs_selfish_total": round(float(sum(r["end_kwh"] for r in rows)), 1),
            "largest_harmed": sorted(({k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()}
                                      for r in harmed), key=lambda r: -r["total"])[:5],
            "agents_with_harm_above_zero": len(harmed_ids)}


def ablation_row(ev) -> dict:
    nh = ev["net_harm"]
    comp = (nh.get("components") or {}).get("harmed_agents", {})
    return {"peak_mw": ev["peak_mw"], "peak_vs_selfish_mw": ev["peak_vs_selfish_mw"],
            "share_of_m0_ceiling": ev.get("share_of_m0_ceiling"),
            "share_of_selfish_to_ceiling_gap": ev.get("share_of_selfish_to_ceiling_gap"),
            "unfilled_need_kwh": ev["unfilled_need_kwh"], "agents_harmed": nh["agents_harmed"],
            "total_net_harm_usd": nh["total_net_harm_usd"], "harm_by_component": comp,
            "steps_over_target": (ev.get("target_check_metered") or {}).get("steps_over"),
            "offers_skipped": (ev.get("prep_clearing") or {}).get("offers_skipped"),
            "offers_accepted_in_part": (ev.get("prep_clearing") or {}).get("offers_accepted_in_part"),
            "pass": ev.get("m2a_pass", {}).get("pass")}


def merge_ablation(cfg, h="150"):
    """ablation/<flag>/row.json files plus the main run's 150 MW result (all on) into summary.json."""
    out_dir = REPO / "runs" / cfg["run_name"]
    summary = json.loads((out_dir / "summary.json").read_text())
    rows = {"all_on": ablation_row(summary["exchange_by_headroom_mw"][h])}
    for f in M2A_FLAGS:
        path = out_dir / "ablation" / f / "row.json"
        if path.exists():
            rows[f"without_{f}"] = json.loads(path.read_text())
    summary["ablation_150"] = {"headroom_mw": int(h), "rows": rows,
                               "note": "all_on is the main run at 150 MW; each other row turns one flag off"}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary["ablation_150"], indent=2))


def peak_info(net, clock: Clock) -> dict:
    k = int(net.argmax())
    return {"peak_mw": float(net[k]) / 1e3, "peak_utc": clock.iso[k], "peak_central": str(clock.local[k])}


def gap_share(selfish_mw, peak_mw, m0):
    """Share of the selfish-to-ceiling gap closed: (selfish peak - exchange peak) / (selfish peak - M0 ceiling peak)."""
    if m0 is None:
        return None
    return (selfish_mw - peak_mw) / (selfish_mw - m0["ceiling_15min"]["peak_min_mw"])


def share(peak_kw, m0):
    if m0 is None:
        return None
    rec, ceil = m0["baseline_recorded"]["peak_mw"], m0["ceiling_15min"]["peak_min_mw"]
    return (rec - peak_kw / 1e3) / (rec - ceil)


def main(cfg_path):
    cfg = load_config(cfg_path)
    nomination_span(cfg)          # refuse a bad nomination_span before any work
    n_warm = warmup_days(cfg)     # and a bad or refused warmup_days
    controllable = peak_steps(cfg) == "controllable"     # and a bad peak_steps (D57)
    if "--ablation-merge" in sys.argv:
        return merge_ablation(cfg)
    s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
    part = participants(s, cfg["agents"])
    out_dir = REPO / "runs" / cfg["run_name"]
    ablation_flag = sys.argv[sys.argv.index("--ablation-one") + 1] if "--ablation-one" in sys.argv else None
    if ablation_flag:
        if ablation_flag not in M2A_FLAGS:
            raise ValueError(f"--ablation-one takes one of {M2A_FLAGS}")
        out_dir = out_dir / "ablation" / ablation_flag
    m0_path = m0_dir(cfg) / "ceiling.json"
    m0 = json.loads(m0_path.read_text()) if m0_path.exists() else None
    if m0 is not None and m0["slice"]["start_utc"] != str(s.ts_utc[0]):
        raise ValueError(f"{m0_path} is for a slice starting {m0['slice']['start_utc']}, not {s.ts_utc[0]}: "
                         "set m0_dir to this window's M0 run")
    clock = Clock(s.ts_utc, TZ)
    a = s.agent
    rec_net = s.system["system_load"] * 1000.0 - (np.nan_to_num(a["pv"]) - a["curt"] + a["bess"]).sum(0)
    pf, m0c = 0, None
    if controllable:      # D57: the controllable M0 ceiling must exist and start where this run's controllable steps do
        if ablation_flag:
            raise ValueError("peak_steps: controllable is not built for --ablation-one")
        pf = controllable_from(cfg, s)
        mc_path = m0_dir(cfg) / "ceiling_controllable.json"
        if pf <= 0:
            raise ValueError("peak_steps: controllable needs a binding call (nominations on)")
        if not mc_path.exists():
            raise ValueError(f"peak_steps: controllable needs {mc_path} (python -m bench.m0 <config> --controllable)")
        m0c = json.loads(mc_path.read_text())
        if m0c["controllable"]["peak_from_utc"] != clock.iso[pf] or m0c["slice"]["start_utc"] != str(s.ts_utc[0]):
            raise ValueError(f"{mc_path} starts its controllable steps at {m0c['controllable']['peak_from_utc']}, "
                             f"this run at {clock.iso[pf]}: rerun M0 --controllable for this config")
    # warm-up (owner, Nov 6): the simulation runs on s_run, which starts n_warm local days earlier; s stays the
    # reported window, and every metric below is computed on the window view of each run (window_result)
    s_run, start_utc, view = s, None, (lambda r: r)
    if n_warm:
        if ablation_flag:
            raise ValueError("warmup_days is not built for --ablation-one")
        day0 = (pd.Timestamp(cfg["start"]) - pd.Timedelta(days=n_warm)).date()
        s_run = load_slice(cfg["data_dir"], day0, cfg["end"], cfg["slice_tz"])   # raises if the data starts later
        w0 = len(s_run.ts_utc) - len(s.ts_utc)
        same = (w0 > 0 and s_run.ts_utc[w0:].equals(s.ts_utc) and np.array_equal(s_run.ids, s.ids)
                and all(np.array_equal(s_run.agent[k][:, w0:], v, equal_nan=True) for k, v in s.agent.items())
                and all(np.array_equal(s_run.system[k][w0:], v, equal_nan=True) for k, v in s.system.items()))
        if not same:
            raise ValueError("warm-up slice does not end with the window's own slice")
        iso_run = Clock(s_run.ts_utc, TZ).iso
        start_utc = iso_run[w0]
        view = lambda r: window_result(r, w0, iso_run)      # noqa: E731

    headrooms = cfg["headroom_mw"] if isinstance(cfg["headroom_mw"], list) else [cfg["headroom_mw"]]
    nominations, rounds = settings(cfg)
    fl = m2a_flags(cfg)
    m2a = any(fl[k] for k in M2A_FLAGS)
    v2 = m2a or bool(cfg.get("harm_components", False))
    prev = previous_day(cfg) if fl["daily_energy"] else None
    print(f"{len(part)} agents, {len(s.ts_utc)} steps" + (f" after {n_warm} warm-up day(s)" if n_warm else "")
          + "; selfish ...", flush=True)
    selfish_run = view(run_variant(cfg, s_run, part, out_dir, prev=prev, window_start_utc=start_utc))
    selfish = evaluate(cfg, s, part, selfish_run, rec_net, m0, selfish_run if v2 else None)
    print(f"  selfish peak {selfish['peak_mw']:.1f} MW ({selfish['runtime_s']} s)", flush=True)
    if pf:
        selfish["controllable"] = controllable_block(selfish["_net"], None, clock, pf, m0c)
    table = pd.DataFrame({"t_utc": clock.iso, "t_central": clock.local.strftime("%Y-%m-%d %H:%M"),
                          "recorded_kw": rec_net, "operator_forecast_kw": selfish_run["fc_op"],
                          "selfish_kw": selfish["_net"]})
    def exchange_run(h, rnds, off=(), suffix=""):
        print(f"exchange, headroom {h} MW" + (f", {rnds} rounds" if nominations else "") + suffix + " ...", flush=True)
        r = view(run_variant(cfg, s_run, part, out_dir, h, rnds, off=off, prev=prev, suffix=suffix,
                             window_start_utc=start_utc))
        ev = evaluate(cfg, s, part, r, rec_net, m0, selfish_run if r["v2"] else None)
        ev["pass"] = bool(ev["peak_mw"] < selfish["peak_mw"] - 1e-3)
        ev["pass_peak_inside_windows"] = bool(ev["pass"] and not ev["rebound"]["peak_moved_outside_windows"])
        ev["peak_vs_selfish_mw"] = ev["peak_mw"] - selfish["peak_mw"]
        ev["share_of_selfish_to_ceiling_gap"] = gap_share(selfish["peak_mw"], ev["peak_mw"], m0)
        if pf:
            ev["controllable"] = controllable_block(ev["_net"], selfish["_net"], clock, pf, m0c)
        if "price_mode" in ev:
            cut = selfish["peak_mw"] - ev["peak_mw"]
            ev["price_mode"]["payout_per_mw_cut_usd"] = (ev["price_mode"]["operator_net_payout_usd"] / cut
                                                         if cut > 1e-6 else None)
        ev["_daily_clipping"] = r["daily_clipping"]
        ev["agents_worse_off_than_selfish"] = int((ev["_agent_value"] < selfish["_agent_value"] - 1e-6).sum())
        alone, with_ps = selfish["_agent_value"], ev["_agent_value"]
        if r["v2"]:           # stored energy left at the end of the slice counts, valued at the slice's mean DA price
            p_end = float(s.system["price_da"].mean()) / 1000.0
            alone = alone + selfish_run["SOC"][:, -1] * p_end
            with_ps = with_ps + r["SOC"][:, -1] * p_end
            if n_warm:     # warm-up: the energy each arm brought into the window, valued the same way
                alone = alone - selfish_run["SOC_start"] * p_end
                with_ps = with_ps - r["SOC_start"] * p_end
        ev["net_harm"] = net_harm(alone, with_ps, [ag.id for ag in r["agents"]], r["header"]["roster"],
                                  len(s.ids) - len(part))
        # D61 (M3 step 7): each agent's value with Pearl Street, the same as net_harm's, for the twin comparison
        ev["agent_value_usd"] = {ag.id: float(v) for ag, v in zip(r["agents"], with_ps)}
        if r["v2"]:
            ev["net_harm"]["components"] = harm_components(ev.pop("_components"), ev["net_harm"], alone)
        if r["m2a"]:
            tc = ev.get("target_check_metered") or {}
            ev["m2a_pass"] = {"no_net_harm": ev["net_harm"]["pass"], "peak_below_selfish": ev["pass"],
                              "no_step_outside_windows_over_target": tc.get("steps_over_beyond_band", 1) == 0,
                              "roster_complete": not ev["net_harm"]["missing_from_check"]}
            ev["m2a_pass"]["pass"] = all(ev["m2a_pass"].values())
        table[f"{r['variant']}_kw"] = ev["_net"]
        table[f"{r['variant']}_in_window"] = ev["_in_window"]
        print(f"  peak {ev['peak_mw']:.1f} MW, pass {ev['pass']}, unfilled {ev['unfilled_need_kwh']:.0f} kWh, "
              f"rebound {ev['rebound']['peak_outside_windows_mw']}, {ev['runtime_s']} s", flush=True)
        return ev

    if ablation_flag:
        ev = exchange_run(150, rounds, off=(ablation_flag,), suffix=f"_no_{ablation_flag}")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "row.json").write_text(json.dumps(ablation_row(ev), indent=2))
        return None
    exch = {f"{h:g}": exchange_run(h, rounds) for h in headrooms}
    sweep = None
    if nominations and cfg.get("rounds_sweep"):
        sw = cfg["rounds_sweep"]
        sweep = {"headroom_mw": sw["headroom_mw"], "by_rounds": {}}
        for rr in sw["rounds"]:
            same = rr == rounds and f"{sw['headroom_mw']:g}" in exch
            ev = exch[f"{sw['headroom_mw']:g}"] if same else exchange_run(sw["headroom_mw"], rr)
            g = ev.get("binding_forecast_vs_actual") or {}
            sweep["by_rounds"][str(rr)] = {"peak_mw": ev["peak_mw"], "peak_vs_selfish_mw": ev["peak_vs_selfish_mw"],
                                           "unfilled_need_kwh": ev["unfilled_need_kwh"],
                                           "binding_forecast_gap": g or None,
                                           "peak_moved_outside_windows": ev["rebound"]["peak_moved_outside_windows"],
                                           "agents_harmed": ev["net_harm"]["agents_harmed"],
                                           "total_net_harm_usd": ev["net_harm"]["total_net_harm_usd"]}

    strip = lambda d: {k: v for k, v in d.items() if not k.startswith("_")}
    summary = {
        "header": summary_header(cfg, [str(s.ids[i]) for i in part], selfish_run["header"]),
        "slice": {"start_utc": clock.iso[0], "end_utc": clock.end_iso(len(clock.iso) - 1), "steps": len(clock.iso)},
        "agents": len(part),
        "m0": None if m0 is None else {"recorded_peak_mw": m0["baseline_recorded"]["peak_mw"],
                                       "ceiling_peak_mw": m0["ceiling_15min"]["peak_min_mw"]},
        "recorded": {**peak_info(rec_net, clock), "share_of_m0_ceiling": share(rec_net.max(), m0)},
        "selfish": strip(selfish),
        "exchange_by_headroom_mw": {h: strip(v) for h, v in exch.items()},
        "pass_all_headrooms": all(v["pass"] for v in exch.values()),
    }
    if pf:
        summary["peak_steps"] = {"rule": "controllable (D57 option 1): headline peak, share of the M0 ceiling and gap "
                                         "share over steps from the first binding call's window on; the full-slice "
                                         "fields are the footnote",
                                 "from_utc": clock.iso[pf], "from_step": pf,
                                 "m0_controllable": {"recorded_peak_mw": m0c["baseline_recorded"]["peak_mw"],
                                                     "ceiling_peak_mw": m0c["ceiling_15min"]["peak_min_mw"],
                                                     "pre_from_cap_mw": m0c["controllable"]["pre_from_cap_mw"]}}
    if n_warm:
        summary["warmup"] = {
            "warmup_days": n_warm, "run_start_utc": str(s_run.ts_utc[0]), "window_start_utc": start_utc,
            "share_of_m0_ceiling_provisional": True,
            "note": "the simulation ran from run_start_utc; every metric here covers the window only (slice); money "
                    "settles from window_start_utc (exchange settle_from_utc); stored energy at the window start is "
                    "valued in harm like the energy at the end (start_energy). The M0 ceiling is the window's own, "
                    "from a cold start, so share of ceiling is provisional"}
    if nominations:
        summary["rounds"] = rounds
        summary["rounds_sweep"] = sweep
        tested = [h for h in ("100", "150") if h in exch]
        summary["m1_1_pass_test"] = {"rule": "at 100 and 150 MW: exchange peak below selfish and the overall peak "
                                             "inside need windows (PLAN.md section 7)",
                                     "by_headroom": {h: exch[h]["pass_peak_inside_windows"] for h in tested},
                                     "pass": bool(tested) and all(exch[h]["pass_peak_inside_windows"] for h in tested)}
        prev_path = REPO / "runs" / cfg.get("compare_to", "m1") / "summary.json"
        if prev_path.exists() and not m2a:
            summary["side_by_side_with_m1"] = side_by_side(json.loads(prev_path.read_text()), summary)
        elif prev_path.exists():
            summary[f"side_by_side_with_{cfg['compare_to']}"] = side_by_side(
                json.loads(prev_path.read_text()), summary, (cfg["compare_to"], cfg["run_name"]))
    if m2a:
        summary["m2a"] = {**{k: fl[k] for k in M2A_FLAGS},
                          "risk_premium_usd_mwh": risk_premium(fl["premium_setting"], s),
                          "risk_premium_note": "auto = mean |RT - DA| over this slice (in-sample)",
                          "referee": cfg.get("referee", "abort")}
        if fl["sequential_clearing"]:
            summary["m2a"]["clearing_order"] = ("D42: needs sharing a gate clear one at a time by operator priority: "
                                                "highest forecast exceedance first (max over the window of forecast "
                                                "or re-forecast minus the day target), ties to the earlier window "
                                                "start, then post order")
        if fl["daily_energy"]:
            clip = next(iter(exch.values()))["_daily_clipping"] if exch else {}
            summary["daily_energy_clipping"] = {
                "agents_clipped": len(clip), "total_cut_kwh": round(sum(v for c in clip.values() for v in c.values()), 1),
                "by_agent_kwh": clip,
                "note": "declared daily_kwh = usable storage + 0.5 x own PV forecast, clipped at usable storage + "
                        "demonstrated PV; demonstrated limits come from one day of metered history (Jan 1, 2023)"}
        summary["m2a_pass_test"] = {
            "rule": "at each headroom: no one net harmed, peak below selfish, no step outside need windows above "
                    "target (metered, from the first gate); unfilled need reported, not gated",
            "by_headroom": {h: exch[h]["m2a_pass"] for h in exch},
            "pass": all(exch[h]["m2a_pass"]["pass"] for h in exch)}
        old = REPO / "runs" / cfg["run_name"] / "summary.json"
        if old.exists():
            prior = json.loads(old.read_text())
            if "ablation_150" in prior:
                summary["ablation_150"] = prior["ablation_150"]
    summary["series"] = series(table, clock, m0_dir(cfg))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    table.to_csv(out_dir / "netload.csv", index=False)
    print(json.dumps({"selfish_peak_mw": selfish["peak_mw"], "pass_all_headrooms": summary["pass_all_headrooms"],
                      "m1_1_pass_test": summary.get("m1_1_pass_test", {}).get("pass")}))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else REPO / "configs" / "m1.yaml")
