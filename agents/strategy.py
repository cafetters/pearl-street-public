"""Strategy menu and choosers for our reference agents (D12, D59, D92; design doc 2.2-2.5).

A chooser picks a label from a fixed menu; code maps each label to numbers here, in one table. The LLM never outputs a
number that code uses. The menu binds only our reference agents (D63); attackers are wrapped outside it (D61).

StrategyAgent wraps an honest BatteryAgent the way attacks/ wraps one: before the inner agent acts on a new day-ahead
publication (and once at the start), the cadence (D97) decides whether to ask the chooser; the chosen label sets the
inner agent's peak-response margin (D58) and posture label, effective from the next gate. Between calls the agent keeps
its last label. Without an `llm_strategy` block in the config no wrapper is built, so runs are unchanged.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from agents.llm.agent_view import AgentView, InboxDigest, hourly
from agents.llm.cadence import Cadence, daily_cadence  # noqa: F401  (re-exported for configs and tests)

MENU = ("normal", "cautious", "bold")
MENU_VERSION = "menu-d92-1"
# D92 (owner, Nov 12): normal = the interim $5 margin (D75/D76), cautious = $10 (D58 horizon pass), bold = m = 0.
POSTURE_MARGIN_USD_MWH = {"normal": 5.0, "cautious": 10.0, "bold": 0.0}
DEFAULT_LABEL = "normal"
T3_USD = 10.0                       # section 8's tolerance floor


def label_table_sha256() -> str:
    return hashlib.sha256(json.dumps(POSTURE_MARGIN_USD_MWH, sort_keys=True).encode()).hexdigest()


def margin_for(label: str) -> float:
    if label not in POSTURE_MARGIN_USD_MWH:
        raise ValueError(f"unknown posture {label!r}")
    return POSTURE_MARGIN_USD_MWH[label]


def allowed_labels(bold_allowed: bool) -> tuple:
    return MENU if bold_allowed else tuple(m for m in MENU if m != "bold")


def label_ok(label, bold_allowed: bool) -> bool:
    return isinstance(label, str) and label in allowed_labels(bold_allowed)


@dataclass
class Decision:
    label: str | None
    source: str
    reason: str
    rationale: str | None = None          # logged, never parsed, never rendered into a later prompt
    error: str | None = None
    meta: dict = field(default_factory=dict)


class FixedChooser:
    kind = "fixed"

    def __init__(self, label: str = DEFAULT_LABEL):
        margin_for(label)
        self.label = label

    def decide(self, view: AgentView, reason: str, model_override=None) -> Decision:
        return Decision(label=self.label, source="fixed", reason=reason)


class RuleChooser:
    """M4's ablation baseline (design 2.4): a short deterministic rule on the inputs the LLM sees.
    cautious for the next gate if, since its last decision, the agent's own imbalance charges exceed $10 (T3) or it got
    a reject (T2), or (owner, Nov 12: the fallback, since T2 and T3 measured 0 per agent-day on the saved design-window
    twins) tomorrow's DA spread is in the top quartile of the trailing 30 days of published DA prices (T4; fleet-wide,
    DA prices are shared). Otherwise normal. Never bold."""
    kind = "rule"

    def decide(self, view: AgentView, reason: str, model_override=None) -> Decision:
        why = []
        if view.imbalance_usd_since_last_call > T3_USD:
            why.append("T3")
        if view.rejects_since_eval > 0:
            why.append("T2")
        if view.da_spread_threshold is not None and view.da_spread > view.da_spread_threshold:
            why.append("T4")
        return Decision(label="cautious" if why else "normal", source="rule", reason=reason,
                        meta={"rule_fired": why})


@dataclass
class RunContext:
    local_dates: list                      # local date per step (the slice's time zone)
    da_spread_threshold: float | None      # T4, from public DA prices before the window start (sim computes it)
    bold_allowed: bool = False
    dt_h: float = 0.25
    cap_hit: object = None                 # callable -> bool: the run's LLM spend cap was reached (D94)

    def __post_init__(self):
        self.days = list(dict.fromkeys(self.local_dates))


class StrategyAgent:
    def __init__(self, inner, chooser, cadence: Cadence, ctx: RunContext):
        self._inner, self.chooser, self.cadence, self.ctx = inner, chooser, cadence, ctx
        self.digest = InboxDigest()
        self.label = DEFAULT_LABEL
        self.history: list[str] = []
        self.decisions: list[dict] = []
        self._version = None
        self._apply(DEFAULT_LABEL)

    def __getattr__(self, name):                 # everything else is the honest agent's
        return getattr(self._inner, name)

    def receive(self, raw: str):
        self.digest.observe(raw)
        self._inner.receive(raw)

    # ---- the hook ----
    def act(self, k: int, soc: float, price_version: int, price):
        if self._version is None:
            self._decide(k, 0, price, *self.cadence.start(0))
        elif price_version != self._version:
            day_i = min(price_version - 1, len(self.ctx.days) - 1)
            view = self._view(k, day_i, price, "pending")
            call, reason = self.cadence.evaluate(day_i, view, call_day=self.ctx.days.index(self.ctx.local_dates[k]))
            if self.ctx.cap_hit is not None and self.ctx.cap_hit() and self.label != DEFAULT_LABEL:
                self._record(k, day_i, "cap_reached", Decision(DEFAULT_LABEL, "cap_reached", "cap_reached"), True)
                self._apply(DEFAULT_LABEL)
            elif call:
                self._decide(k, day_i, price, True, reason)
            self.digest.reset_eval()
        self._version = price_version
        return self._inner.act(k, soc, price_version, price)

    def _decide(self, k, day_i, price, call: bool, reason: str):
        view = self._view(k, day_i, price, reason)
        try:
            d = self.chooser.decide(view, reason, model_override=getattr(self.cadence, "model_for", lambda r: None)(reason))
        except Exception as e:                                   # fail closed: keep the previous label
            d = Decision(label=None, source=getattr(self.chooser, "kind", "?"), reason=reason,
                         error=f"chooser_exception:{type(e).__name__}")
        ok = d.label is not None and label_ok(d.label, self.ctx.bold_allowed)
        if not ok and d.error is None:
            d.error = "label_not_allowed" if isinstance(d.label, str) else "no_label"
        self._record(k, day_i, reason, d, ok)
        if ok:
            self._apply(d.label)
        self.digest.reset_call()

    def _apply(self, label: str):
        self.label = label
        self._inner.margin = margin_for(label)
        self._inner.posture = label

    def _record(self, k, day_i, reason, d: Decision, ok: bool):
        prev = self.history[-1] if self.history else None
        if ok:
            self.history.append(d.label)
        self.decisions.append({"k": k, "t_utc": self._inner.iso[k], "call_day": self.ctx.local_dates[k],
                               "decision_day": self.ctx.days[day_i], "reason": reason, "label": d.label,
                               "prev_label": prev, "source": d.source, "valid": ok, "error": d.error,
                               **({"meta": d.meta} if d.meta else {})})

    # ---- AgentView: own fields and the digest only ----
    def _view(self, k: int, day_i: int, price, reason: str) -> AgentView:
        ctx, inner, dg = self.ctx, self._inner, self.digest
        day = ctx.days[day_i]
        steps = [i for i, d in enumerate(ctx.local_dates) if d == day]
        da = [float(price[i]) for i in steps]
        net = [-float(inner.base_export[i]) for i in steps]          # own load - usable PV, kW
        today = ctx.local_dates[k]
        yi = ctx.days.index(today) - 1
        if yi >= 0:
            ysteps = [i for i, d in enumerate(ctx.local_dates) if d == ctx.days[yi]]
            net_y = -sum(float(inner.base_export[i]) for i in ysteps) * ctx.dt_h
            ly = (net_y, net_y)            # agents plan on the recorded series: forecast == metered (T5 = 0)
        else:
            ly = (None, None)
        return AgentView(
            decision_day=day, reason=reason, power_kw=float(inner.b.P), energy_kwh=float(inner.b.CAP),
            bold_allowed=bool(ctx.bold_allowed), current_label=self.label, label_history=tuple(self.history[-7:]),
            da_prices_hourly=hourly(da), da_spread=(max(da) - min(da)) if da else 0.0,
            da_spread_threshold=ctx.da_spread_threshold, net_load_forecast_hourly=hourly(net),
            peak_price_max=dg.peak_price_max, peak_price_steps=dg.peak_price_steps,
            imbalance_usd_since_last_call=dg.imbalance_usd_since_last_call, rejects_since_eval=dg.rejects_since_eval,
            reject_codes=tuple(dg.reject_codes), binding_calls_since_eval=dg.binding_calls_since_eval,
            peak_settle_net_usd=dg.peak_settle_net_usd, peak_settle_refund_usd=dg.peak_settle_refund_usd,
            site_net_forecast_kwh_yesterday=ly[0], site_net_metered_kwh_yesterday=ly[1])


# ---- run setup (sim/run_m1.py calls this only when the config has an llm_strategy block) ----

CHOOSERS = ("fixed", "rule", "llm")
SPEC_KEYS = {"chooser", "per_agent", "cadence", "bold_allowed", "honest_models", "attacker_models", "backend",
             "cache", "cap_usd_run", "phase", "cap_usd_phase", "size_gate", "escalate", "t4_threshold_usd_mwh",
             "effort", "max_tokens", "reset", "fixed_label"}


class StrategyRun:
    """One run's strategy layer: builds each agent's chooser and cadence, and collects the header, the sidecar call log
    (llm_calls.jsonl) and the summary block."""

    def __init__(self, spec: dict, *, roster: list, power_kw: dict, attackers: set, local_dates: list, seed,
                 run_id: str, config_margin: float, t4_threshold: float | None):
        spec = dict(spec or {})
        bad = sorted(set(spec) - SPEC_KEYS)
        if bad:
            raise ValueError(f"llm_strategy: unknown keys {bad}")
        if float(config_margin) != POSTURE_MARGIN_USD_MWH[DEFAULT_LABEL]:
            raise ValueError(f"llm_strategy: peak_response_margin_usd_mwh {config_margin} differs from the normal "
                             f"posture's ${POSTURE_MARGIN_USD_MWH[DEFAULT_LABEL]} (D92); labels set the margin")
        self.spec, self.roster, self.power_kw, self.attackers = spec, list(roster), dict(power_kw), set(attackers)
        fleet = str(spec.get("chooser", "fixed"))
        per = {str(k): str(v) for k, v in (spec.get("per_agent") or {}).items()}
        for c in [fleet, *per.values()]:
            if c not in CHOOSERS:
                raise ValueError(f"llm_strategy: unknown chooser {c!r} (one of {CHOOSERS})")
        unknown = sorted(set(per) - set(roster))
        if unknown:
            raise ValueError(f"llm_strategy.per_agent names agents not in the roster: {unknown}")
        self.local_dates, self.seed, self.run_id = list(local_dates), seed, run_id
        self.bold_allowed = bool(spec.get("bold_allowed", False))
        self.t4_threshold = t4_threshold
        self.chooser_by_agent = {a: per.get(a, fleet) for a in self.roster}
        self.cadence_spec = spec.get("cadence", "daily")
        from agents.llm import cadence as CD
        cad = CD.from_spec(self.cadence_spec)            # refuse a bad cadence before the run
        if cad.size_gate:     # D97 size_gate: the LLM only at or above the lower-median battery power (ties by id)
            ranked = sorted(self.roster, key=lambda a: (self.power_kw[a], a))
            med = self.power_kw[ranked[(len(ranked) - 1) // 2]]
            self.chooser_by_agent = {a: ("rule" if c == "llm" and self.power_kw[a] < med else c)
                                     for a, c in self.chooser_by_agent.items()}
        self.agents: dict[str, StrategyAgent] = {}
        self.llm = None                                  # the LLM side (backend, cache, spend), built on demand
        if "llm" in self.chooser_by_agent.values():
            from agents.llm.chooser import LLMRun
            self.llm = LLMRun(spec, roster=self.roster, attackers=self.attackers, seed=seed, run_id=run_id,
                              power_kw=self.power_kw)

    def _ctx(self) -> RunContext:
        return RunContext(local_dates=self.local_dates, da_spread_threshold=self.t4_threshold,
                          bold_allowed=self.bold_allowed,
                          cap_hit=(self.llm.cap_hit if self.llm is not None else None))

    def chooser_for(self, agent_id: str):
        kind = self.chooser_by_agent[agent_id]
        if kind == "fixed":
            return FixedChooser(str(self.spec.get("fixed_label", DEFAULT_LABEL)))
        if kind == "rule":
            return RuleChooser()
        return self.llm.chooser_for(agent_id)

    def wrap(self, agent):
        from agents.llm import cadence as CD
        sa = StrategyAgent(agent, self.chooser_for(agent.id), CD.from_spec(self.cadence_spec, agent_id=agent.id,
                                                                            escalate=bool(self.spec.get("escalate"))),
                           self._ctx())
        self.agents[agent.id] = sa
        return sa

    def header(self) -> dict:
        from agents.llm import cadence as CD
        h = {"chooser_by_agent": self.chooser_by_agent, "cadence": CD.describe(self.cadence_spec),
             "menu": list(MENU), "menu_version": MENU_VERSION, "posture_margin_usd_mwh": POSTURE_MARGIN_USD_MWH,
             "label_table_sha256": label_table_sha256(), "bold_allowed": self.bold_allowed,
             "t4_threshold_usd_mwh": self.t4_threshold, "reset": str(self.spec.get("reset", "agents_fresh")),
             "backend": str(self.spec.get("backend", "none" if self.llm is None else "fake"))}
        if self.llm is not None:
            h.update(self.llm.header())
        return h

    def records(self) -> list[dict]:
        out = []
        for a in self.roster:
            sa = self.agents.get(a)
            for d in (sa.decisions if sa else []):
                out.append({"event": "decision", "agent": a, **d})
        if self.llm is not None:
            out += self.llm.call_records()
        return out

    def write_sidecar(self, path, run_header: dict) -> None:
        head = {"event": "header", "run_id": run_header.get("run_id"), "config": run_header.get("config"),
                "seed": run_header.get("seed"), "git_commit": run_header.get("git_commit"),
                "llm_strategy": self.header()}
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(head) + "\n")
            for r in self.records():
                f.write(json.dumps(r, default=str) + "\n")

    def summary(self) -> dict:
        labels, counts, errors = {}, {}, 0
        for a in self.roster:
            sa = self.agents.get(a)
            labels[a] = {}
            for d in (sa.decisions if sa else []):
                if d["valid"]:
                    labels[a][d["decision_day"]] = d["label"]
                    counts.setdefault(d["decision_day"], {}).setdefault(d["label"], 0)
                    counts[d["decision_day"]][d["label"]] += 1
                else:
                    errors += 1
        n = sum(len(sa.decisions) for sa in self.agents.values())
        out = {"decisions": n, "invalid_decisions": errors, "labels_by_agent": labels, "label_counts_by_day": counts,
               "missing_from_strategy": sorted(set(self.roster) - set(self.agents))}
        if self.llm is not None:
            out.update(self.llm.summary(self.local_dates))
        return out
