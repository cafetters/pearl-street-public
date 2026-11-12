"""Call cadence (D97, proposed; tested under D28, whose trigger is the M4 ablation).

Between calls an agent keeps its last labels. All schedule and trigger logic is code, evaluated once per local day
(when the next day's DA prices publish) from AgentView only, and a call is effective from the next gate. Every call
carries its reason: start, scheduled, or trigger:<names>.

- schedule_days: N or none. Presets: daily (1), every_3, weekly (7), every_10, monthly (30), triggers_only (none),
  backoff (interval starts at 1 day, doubles after each call with no trigger since the previous call, max 30 days;
  any trigger resets it to 1 day).
- triggers (each on/off; thresholds from the owner's spec, measured rates in runs/m4/trigger_rates_*.json):
  T1 own peak_settle loss beyond max(1% x refund, $10); T2 a reject from the exchange; T3 own imbalance charges over
  $10 since the last call; T4 the decision day's DA spread above the 75th percentile of the trailing 30 days of
  published DA prices (fleet-wide); T5 own site net load off its forecast by more than 20% yesterday; T6 a binding call
  announced (measured at 1.0 per day on the design windows, so off in every preset except daily).
- max_calls_per_day: default 2, hard ceiling 3, counted by the local day the call is made.
- start call: one per agent at run start in every preset.
- escalate (report only): trigger calls use Sonnet 5.5; routine calls use the agent's drawn model.
- size_gate: the LLM chooser only for agents at or above the median battery power (run-level, agents/strategy.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agents.llm.agent_view import AgentView

PRESETS = {"daily": 1, "every_3": 3, "weekly": 7, "every_10": 10, "monthly": 30, "triggers_only": None,
           "backoff": "backoff"}
TRIGGERS = ("T1", "T2", "T3", "T4", "T5", "T6")
MAX_CALLS_CEILING = 3
BACKOFF_MAX_DAYS = 30
ESCALATE_MODEL = "anthropic/claude-sonnet-5-5"
SPEC_KEYS = {"preset", "triggers", "max_calls_per_day", "escalate", "size_gate"}
T1_PCT, T1_FLOOR_USD, T3_USD, T5_FRAC = 0.01, 10.0, 10.0, 0.20


def default_triggers(preset: str) -> dict:
    return {t: (t != "T6" or preset == "daily") for t in TRIGGERS}


def fired(view: AgentView, on: dict) -> list[str]:
    """The enabled triggers that fire on this view. Reads the AgentView only."""
    if not isinstance(view, AgentView):
        raise TypeError("triggers read only an AgentView")
    out = []
    if on.get("T1") and view.peak_settle_net_usd is not None:
        tol = max(T1_PCT * abs(view.peak_settle_refund_usd or 0.0), T1_FLOOR_USD)
        if view.peak_settle_net_usd < -tol:
            out.append("T1")
    if on.get("T2") and view.rejects_since_eval > 0:
        out.append("T2")
    if on.get("T3") and view.imbalance_usd_since_last_call > T3_USD:
        out.append("T3")
    if on.get("T4") and view.da_spread_threshold is not None and view.da_spread > view.da_spread_threshold:
        out.append("T4")
    if (on.get("T5") and view.site_net_forecast_kwh_yesterday is not None
            and view.site_net_metered_kwh_yesterday is not None):
        f, m = view.site_net_forecast_kwh_yesterday, view.site_net_metered_kwh_yesterday
        if abs(m - f) > T5_FRAC * max(abs(f), 1e-9):
            out.append("T5")
    if on.get("T6") and view.binding_calls_since_eval > 0:
        out.append("T6")
    return out


@dataclass
class Cadence:
    preset: str = "daily"
    schedule_days: int | None = 1
    max_calls_per_day: int = 2
    triggers: dict = field(default_factory=lambda: default_triggers("daily"))
    backoff: bool = False
    escalate: bool = False
    size_gate: bool = False
    interval: int = 1
    _last_call_day: int | None = None
    _trigger_since_call: bool = False
    _calls_by_call_day: dict = field(default_factory=dict)

    def start(self, day_index: int, call_day: int | None = None) -> tuple[bool, str]:
        self._note_call(day_index, day_index if call_day is None else call_day)
        return True, "start"

    def evaluate(self, day_index: int, view: AgentView, call_day: int | None = None) -> tuple[bool, str]:
        cd = day_index if call_day is None else call_day
        hits = fired(view, self.triggers)
        if hits:
            reason = "trigger:" + ",".join(hits)
        else:
            gap = None if self._last_call_day is None else day_index - self._last_call_day
            due = self.interval if self.backoff else self.schedule_days
            if due is None or (gap is not None and gap < due):
                return False, "none"
            reason = "scheduled"
        if self._calls_by_call_day.get(cd, 0) >= self.max_calls_per_day:
            return False, "day_cap"
        if self.backoff:
            self.interval = 1 if hits else min(self.interval * 2, BACKOFF_MAX_DAYS)
        self._note_call(day_index, cd)
        return True, reason

    def model_for(self, reason: str):
        return ESCALATE_MODEL if (self.escalate and reason.startswith("trigger:")) else None

    def _note_call(self, day_index: int, call_day: int) -> None:
        self._last_call_day = day_index
        self._calls_by_call_day[call_day] = self._calls_by_call_day.get(call_day, 0) + 1


def from_spec(spec, agent_id: str | None = None, escalate: bool = False) -> Cadence:
    if isinstance(spec, str) or spec is None:
        spec = {"preset": spec or "daily"}
    if not isinstance(spec, dict):
        raise ValueError("llm_strategy.cadence: a preset name or a mapping")
    bad = sorted(set(spec) - SPEC_KEYS)
    if bad:
        raise ValueError(f"llm_strategy.cadence: unknown keys {bad}")
    name = spec.get("preset", "daily")
    if name not in PRESETS:
        raise ValueError(f"llm_strategy.cadence: unknown preset {name!r} (one of {sorted(PRESETS)})")
    trig = default_triggers(name)
    for t, v in (spec.get("triggers") or {}).items():
        if t not in TRIGGERS:
            raise ValueError(f"llm_strategy.cadence: unknown trigger {t!r}")
        trig[t] = bool(v)
    mcd = int(spec.get("max_calls_per_day", 2))
    if not 1 <= mcd <= MAX_CALLS_CEILING:
        raise ValueError(f"max_calls_per_day must be 1..{MAX_CALLS_CEILING} (a choice only acts at the next gate)")
    days = PRESETS[name]
    return Cadence(preset=name, schedule_days=None if days in (None, "backoff") else days, max_calls_per_day=mcd,
                   triggers=trig, backoff=(days == "backoff"), escalate=bool(spec.get("escalate", escalate)),
                   size_gate=bool(spec.get("size_gate", False)))


def daily_cadence() -> Cadence:
    return from_spec("daily")


def describe(spec) -> dict:
    c = from_spec(spec)
    return {"preset": c.preset, "schedule_days": c.schedule_days, "backoff": c.backoff,
            "max_calls_per_day": c.max_calls_per_day, "triggers": c.triggers, "escalate": c.escalate,
            "size_gate": c.size_gate, "start_call": True, "evaluated": "once per local day at DA publication"}
