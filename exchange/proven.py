"""Proven capacity (owner, Nov 19; revises D65's proven capacity, formerly "credit line"; Leaning). Pure rules: the
exchange calls them under its `proven_capacity` setting; nothing here reads a file or the clock.

Words (CLAUDE.md): the delivery record is the history of promised vs metered delivery; proven capacity is what the
exchange counts on from an asset; unproven capacity is the rest. All kW and kWh.

- An asset with meter history (the metering service attested demonstrated limits for it) starts proven at its
  demonstrated power, counted at rate 1 until its first miss (owner, Nov 20, option a); its strong, high prior
  (cfg.existing_prior) updates all along and sets its rate from the first miss on. Only an asset with no history starts at the
  starter cap with the skeptical prior (cfg.new_prior), and only it is on probation until n0 clean calls.
- Starter cap (kW, never a fraction of the claim): min(claim, attested nameplate if any, the median demonstrated power
  of its asset type among assets with history; cfg.starter_fallback_kw when the type has none). The type comes from
  the metering attestation, never from the agent's register.
- Delivery rate: the Beta prior's mean, updated once per binding call by the delivered share of nominated relief.
- Relief is against the agent's 1p reference; the cap is on the battery's own contribution (nominated site net minus
  the battery-idle site net), since relief against the reference can reach about twice the power (a battery charging
  in the reference, discharging in the nomination). The caller turns "battery contribution <= proven + step" into a
  relief ceiling (`ceilings`). Counted relief = rate x min(relief, ceiling); added load (relief < 0) counts in full.
  Firm relief = rate x min(relief, the ceiling at proven), and 0 while on probation: unproven capacity is never firm.
- After each binding call: clean (metered relief energy >= (1 - clean_tol) x nominated; default tolerance 10%, honest
  synthetic agents deliver 94-96%) grows proven by up to grow x step, but never past the battery's metered peak export
  in that call: proven capacity grows only from metered delivery (slow).
- A miss never cuts what the asset has been seen to deliver (owner, Nov 20): proven capacity is a capability, and a
  miss is a reliability fact, so it lowers the delivery rate (the reputation) instead. Only a newcomer's untested
  starter amount (above `seen_kw`, the most metered so far) is dropped on a miss, down to what it has shown. `cut` is
  no longer used (kept in Config so recorded configs still load).
- Portfolio cap per call: expected shortfall from unproven capacity, sum (counted - firm) x (1 - rate), at most
  budget_kw; when it binds, every asset's unproven part is scaled by one factor (pro rata).
- Contingency cap: counted relief at most reserve_kw per asset, and per legal entity (pro rata inside it).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from statistics import median


@dataclass(frozen=True)
class Config:
    step_kw: float
    grow: float
    cut: float
    clean_tol: float
    n0: int
    new_prior: tuple[float, float]
    existing_prior: tuple[float, float]
    starter_fallback_kw: float
    budget_kw: float
    reserve_kw: float

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        return cls(step_kw=float(d["step_kw"]), grow=float(d["grow"]), cut=float(d["cut"]),
                   clean_tol=float(d.get("clean_tol", 0.10)), n0=int(d.get("n0", 3)),
                   new_prior=tuple(float(x) for x in d["new_prior"]),
                   existing_prior=tuple(float(x) for x in d.get("existing_prior", (19.0, 1.0))),
                   starter_fallback_kw=float(d["starter_fallback_kw"]), budget_kw=float(d["budget_kw"]),
                   reserve_kw=float(d["reserve_kw"]))


@dataclass(frozen=True)
class State:
    proven_kw: float
    alpha: float
    beta: float
    new: bool
    clean_calls: int
    trusted: bool = False      # meter history and no miss yet: rate 1 (owner, Nov 20, option a)
    seen_kw: float = 0.0       # the most the battery has been seen to deliver (meter history, then metered peaks)


def starter_kw(claim_kw: float, nameplate_kw: float | None, type_median: float | None, fallback_kw: float) -> float:
    vals = [float(claim_kw), float(type_median) if type_median is not None else float(fallback_kw)]
    if nameplate_kw is not None:
        vals.append(float(nameplate_kw))
    return max(0.0, min(vals))


def type_median_kw(history: dict, asset_type: str) -> float | None:
    """history: asset -> (attested type, demonstrated power kW), assets with meter history only."""
    v = [float(kw) for t, kw in history.values() if t == asset_type]
    return float(median(v)) if v else None


def initial(has_history: bool, demonstrated_kw: float, claim_kw: float, nameplate_kw: float | None,
            type_median_kw: float | None, cfg: Config) -> State:
    if has_history:
        a, b = cfg.existing_prior
        return State(float(demonstrated_kw), a, b, False, 0, trusted=True, seen_kw=float(demonstrated_kw))
    a, b = cfg.new_prior
    return State(starter_kw(claim_kw, nameplate_kw, type_median_kw, cfg.starter_fallback_kw), a, b, True, 0)


def rate(s: State) -> float:
    """The delivery rate: 1 for a trusted asset (meter history, no miss yet), else the Beta posterior's mean. A trusted
    asset's posterior keeps updating, so after its first miss the rate is the strong prior's posterior over every call."""
    return 1.0 if s.trusted else s.alpha / (s.alpha + s.beta)


def ceilings(ref_kw: float, idle_kw: float, s: State, cfg: Config) -> tuple[float, float]:
    """Relief ceilings (counted, firm) from the battery-contribution caps proven + step and proven:
    relief = nominated - ref <= idle + cap - ref."""
    return (max(0.0, idle_kw + s.proven_kw + cfg.step_kw - ref_kw), max(0.0, idle_kw + s.proven_kw - ref_kw))


def counted(relief_kw: float, max_relief_kw: float, s: State) -> float:
    if relief_kw <= 0.0:
        return float(relief_kw)
    return rate(s) * min(float(relief_kw), float(max_relief_kw))


def counted_net(v_kw: float, ref_kw: float, counted_relief_kw: float) -> float:
    """The counted site net for a nomination v against reference ref: v minus the discounted part of its relief.
    Exactly v when nothing is discounted (ref + (v - ref) is not v in floating point, and the price loop amplifies
    such differences: Nov 20, the 80-agent check)."""
    return float(v_kw) - ((float(v_kw) - float(ref_kw)) - float(counted_relief_kw))


def firm(relief_kw: float, max_firm_kw: float, s: State) -> float:
    if relief_kw <= 0.0 or s.new:
        return 0.0
    return rate(s) * min(float(relief_kw), float(max_firm_kw))


def update(s: State, nominated_kwh: float, metered_kwh: float, metered_peak_kw: float, cfg: Config) -> State:
    """One binding call's outcome (relief energy nominated and metered over its window, and the metered peak relief)."""
    if nominated_kwh <= 0.0:
        return s
    share = max(0.0, min(1.0, float(metered_kwh) / float(nominated_kwh)))
    clean = share >= 1.0 - cfg.clean_tol
    n = s.clean_calls + (1 if clean else 0)
    seen = max(s.seen_kw, float(metered_peak_kw))
    if clean:
        proven = max(s.proven_kw, min(s.proven_kw + cfg.grow * cfg.step_kw, float(metered_peak_kw)))
    else:                       # never below what was seen; only an untested starter amount above it is dropped
        proven = min(s.proven_kw, seen)
    return replace(s, proven_kw=proven, alpha=s.alpha + share, beta=s.beta + (1.0 - share), clean_calls=n,
                   new=s.new and n < cfg.n0, trusted=s.trusted and clean, seen_kw=seen)


def portfolio_scale(unproven_kw: dict, rates: dict, budget_kw: float) -> float:
    """One factor for every asset's unproven counted relief, so that sum unproven x (1 - rate) <= budget."""
    short = sum(max(0.0, u) * (1.0 - rates[a]) for a, u in unproven_kw.items())
    return 1.0 if short <= budget_kw or short <= 0.0 else float(budget_kw) / short


def contingency_scale(counted_kw: dict, entity_of: dict, reserve_kw: float) -> dict:
    """Per asset, the factor on its counted relief: at most reserve_kw per asset, then per legal entity (pro rata)."""
    f = {a: (1.0 if v <= reserve_kw or v <= 0.0 else float(reserve_kw) / v) for a, v in counted_kw.items()}
    by_ent: dict = {}
    for a, v in counted_kw.items():
        by_ent.setdefault(entity_of[a], []).append(a)
    for ent, assets in by_ent.items():
        tot = sum(max(0.0, counted_kw[a]) * f[a] for a in assets)
        if tot > reserve_kw:
            k = float(reserve_kw) / tot
            for a in assets:
                f[a] *= k
    return f
