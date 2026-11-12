"""LLM cost estimator (owner, Nov 12). Expected and worst-case $ per run for each D97 cadence preset, per model and in
total, for 2, 7, 30 and 365 simulated days, plus the twin-pair multiplier and, when the runtime table has an entry,
runtime and peak memory.

Inputs: cadence preset, weighted model list, number of LLM agents, days; prompt tokens measured from the prompt builder
(tonight: chars / 3.5 x 1.3; at the pilot: each provider's token-count call); expected and maximum output tokens;
the trigger rate per agent-day (measured from saved traces, or 0.1 labeled "assumed"); the price table.

Calls per agent: one start call, then one evaluation per published DA day (days - 1). The expected count is exact for
a per-day trigger probability r (a dynamic program over days since the last call, and the backoff interval). The
worst case is a call at every evaluation (a trigger every day): `days` calls. Unpriced models are reported, not summed.

    python -m bench.llm_cost_estimate [--rates runs/m4/trigger_rates_v2r_twins_m5.json] [--agents 80] [--out f.json]

The launcher (bench.cloud_queue) calls check_config before every --llm queue and refuses a run whose worst case
exceeds its run cap.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

from agents.llm import assign as AS
from agents.llm import cadence as CD
from agents.llm import spend as SP

REPO = Path(__file__).resolve().parents[1]
HORIZONS = (2, 7, 30, 365)
DEFAULT_RATE = 0.1
DEFAULT_OUT_TOKENS = 800          # assumed until the pilot (adaptive thinking at low effort counts as output)
DEFAULT_MAX_TOKENS = 2048
RUNTIME_TABLE = REPO / "configs" / "runtime_table.json"


def _due(preset: str):
    d = CD.PRESETS[preset]
    return None if d is None else d


def expected_calls(preset: str, days: int, rate: float, split: bool = False):
    """Expected calls per agent over `days` simulated days (start call included); with split=True also the expected
    number of trigger calls."""
    if preset not in CD.PRESETS:
        raise ValueError(f"unknown preset {preset!r}")
    due = _due(preset)
    r = min(max(float(rate), 0.0), 1.0)
    # state: (days since last call, backoff interval) -> probability
    dist = {(1, 1): 1.0}
    calls, trig = 1.0, 0.0
    for _ in range(1, days):
        nxt = defaultdict(float)
        for (g, iv), p in dist.items():
            if p == 0.0:
                continue
            # trigger: call, interval resets to 1
            calls += p * r
            trig += p * r
            nxt[(1, 1)] += p * r
            q = p * (1.0 - r)
            if due == "backoff":
                if g >= iv:
                    calls += q
                    nxt[(1, min(iv * 2, CD.BACKOFF_MAX_DAYS))] += q
                else:
                    nxt[(g + 1, iv)] += q
            elif due is not None and g >= due:
                calls += q
                nxt[(1, iv)] += q
            else:
                nxt[(min(g + 1, 400), iv)] += q
        dist = nxt
    return (calls, trig) if split else calls


def worst_calls(preset: str, days: int) -> int:
    return int(days)


def _per_call(model_id: str, tin: int, tout: int):
    try:
        return SP.cost_usd(model_id, tin, tout)
    except SP.PriceMissing:
        return None


def estimate(preset: str, days: int, n_agents: float, honest, prompt_tokens: int, out_tokens: int, max_tokens: int,
             rate: float, escalate: bool = False) -> dict:
    models = AS.check_list(honest)
    total_w = sum(m["weight"] for m in models)
    exp_calls, exp_trig = expected_calls(preset, days, rate, split=True)
    wc = worst_calls(preset, days)
    by_model, unpriced = {}, []
    exp_sum = worst_sum = 0.0
    esc = "anthropic/claude-sonnet-5-5"
    for m in models:
        if m["weight"] <= 0:
            continue
        mid = f"{m['provider']}/{m['model']}"
        agents = n_agents * m["weight"] / total_w
        routine = exp_calls - (exp_trig if escalate else 0.0)
        pc_exp = _per_call(mid, prompt_tokens, out_tokens)
        pc_worst = _per_call(mid, prompt_tokens, max_tokens)
        if pc_exp is None:
            unpriced.append(mid)
            e = w = None
        else:
            e = agents * routine * pc_exp
            w = agents * (0 if escalate else wc) * pc_worst
            if escalate:      # trigger calls on Sonnet; worst case: every call is a trigger call
                e += agents * exp_trig * _per_call(esc, prompt_tokens, out_tokens)
                w += agents * wc * _per_call(esc, prompt_tokens, max_tokens)
            exp_sum += e
            worst_sum += w
        by_model[mid] = {"agents": agents, "expected_usd": e, "worst_usd": w}
    return {"preset": preset, "days": days, "calls_per_agent_expected": exp_calls,
            "trigger_calls_per_agent_expected": exp_trig, "calls_per_agent_worst": wc,
            "expected_usd": exp_sum, "worst_usd": worst_sum, "by_model": by_model, "unpriced_models": unpriced}


def twin_multiplier(n_agents: int) -> dict:
    """An attack run beside its honest twin, sharing the twin's cache (D95): at best only the attacker's own prompts
    miss (1 + 1/n); at worst nothing hits (2x)."""
    return {"best": 1.0 + 1.0 / n_agents, "worst": 2.0,
            "note": "per twin pair, relative to the twin alone; honest agents with identical inputs reuse the twin's "
                    "answers through the shared cache"}


def runtime_for(days: int, agents: int, path=RUNTIME_TABLE):
    p = Path(path)
    if not p.exists():
        return None
    for e in json.loads(p.read_text(encoding="utf-8")).get("entries", []):
        if int(e["days"]) == int(days) and int(e["agents"]) == int(agents):
            return e
    return None


def measured_prompt_tokens() -> int:
    """Input tokens of a typical rendered prompt (24 hourly prices and loads, full history), chars / 3.5 x 1.3."""
    from agents.llm import prompt_builder as PB
    from agents.llm.agent_view import AgentView
    from agents.llm.backends import Request
    v = AgentView(decision_day="2023-01-13", reason="trigger:T4", power_kw=10619.0, energy_kwh=42476.0,
                  bold_allowed=False, current_label="normal", label_history=("normal", "cautious") * 3 + ("normal",),
                  da_prices_hourly=tuple(23.456 + i for i in range(24)), da_spread=36.3, da_spread_threshold=24.1,
                  net_load_forecast_hourly=tuple(-1234.5 + 10 * i for i in range(24)), peak_price_max=87.5,
                  peak_price_steps=12, imbalance_usd_since_last_call=12.34, rejects_since_eval=1,
                  reject_codes=("after_gate_closure",), binding_calls_since_eval=1, peak_settle_net_usd=-15.2,
                  peak_settle_refund_usd=120.4, site_net_forecast_kwh_yesterday=-23456.7,
                  site_net_metered_kwh_yesterday=-23456.7)
    s, u = PB.render(v)
    return Request(provider="anthropic", model="claude-haiku-5-5", system=s, user=u, schema=PB.schema(v)) \
        .est_input_tokens()


def table(n_agents: int, honest, prompt_tokens: int, out_tokens: int, max_tokens: int, rate: float,
          rate_label: str, escalate: bool = False) -> dict:
    rows = [estimate(p, d, n_agents, honest, prompt_tokens, out_tokens, max_tokens, rate, escalate)
            for p in CD.PRESETS for d in HORIZONS]
    models = sorted({m for r in rows for m in r["by_model"]})
    head = ("| preset | days | calls/agent expected | calls/agent worst | expected $ | worst $ | "
            + " | ".join(f"{m} expected $" for m in models) + " |")
    sep = "|" + "---|" * (6 + len(models))

    def f(x, nd=2):
        return "unpriced" if x is None else f"{x:,.{nd}f}"
    lines = [head, sep]
    for r in rows:
        lines.append(f"| {r['preset']} | {r['days']} | {r['calls_per_agent_expected']:.2f} | "
                     f"{r['calls_per_agent_worst']} | {f(r['expected_usd'])} | {f(r['worst_usd'])} | "
                     + " | ".join(f(r["by_model"].get(m, {}).get("expected_usd")) for m in models) + " |")
    return {"rows": rows, "agents": n_agents, "models": AS.check_list(honest), "prompt_tokens": prompt_tokens,
            "out_tokens_expected": out_tokens, "max_tokens": max_tokens,
            "trigger_rate": {"value": rate, "label": rate_label}, "escalate": escalate,
            "twin_multiplier": twin_multiplier(n_agents), "price_table_version": SP.PRICE_TABLE_VERSION,
            "runtime": {str(d): runtime_for(d, n_agents) for d in HORIZONS},
            "label": "synthetic estimate (fake backend; no real call yet)", "markdown": "\n".join(lines)}


def _days(cfg) -> int:
    import datetime as dt
    return (dt.date.fromisoformat(str(cfg["end"])) - dt.date.fromisoformat(str(cfg["start"]))).days + 1


def check_config(cfg: dict, n_agents: int = 80, prompt_tokens: int | None = None, rate: float = 1.0) -> dict:
    """The launcher's guard: worst-case $ for this config's LLM agents against its run cap. Worst case = a call at every
    evaluation, max_tokens of output, each LLM agent on the most expensive priced model with weight > 0 in its list."""
    spec = cfg.get("llm_strategy") or {}
    fleet = str(spec.get("chooser", "fixed"))
    per = spec.get("per_agent") or {}
    n_llm = sum(1 for c in per.values() if c == "llm") + (max(n_agents - len(per), 0) if fleet == "llm" else 0)
    if n_llm == 0:
        return {"ok": True, "llm_agents": 0, "worst_usd": 0.0}
    cad = CD.from_spec(spec.get("cadence", "daily"))
    if cad.size_gate:
        n_llm = n_llm - (n_llm - 1) // 2
    days = _days(cfg)
    tin = prompt_tokens if prompt_tokens is not None else measured_prompt_tokens()
    mt = int(spec.get("max_tokens", DEFAULT_MAX_TOKENS))
    worst_pc, unpriced = 0.0, []
    lists = [spec.get("honest_models") or AS.DEFAULT_HONEST]
    if cfg.get("attackers"):
        lists.append(spec.get("attacker_models") or AS.DEFAULT_ATTACKER)
    for lst in lists:
        for m in AS.check_list(lst):
            if m["weight"] <= 0:
                continue
            pc = _per_call(f"{m['provider']}/{m['model']}", tin, mt)
            if pc is None:
                unpriced.append(f"{m['provider']}/{m['model']}")
            else:
                worst_pc = max(worst_pc, pc)
    if cad.escalate:
        worst_pc = max(worst_pc, _per_call(CD.ESCALATE_MODEL, tin, mt))
    worst = n_llm * worst_calls(cad.preset, days) * worst_pc
    cap = spec.get("cap_usd_run")
    live = str(spec.get("backend", "fake")) == "live"
    ok = cap is not None and worst <= float(cap) + 1e-12 and not (live and unpriced)
    return {"ok": ok, "llm_agents": n_llm, "days": days, "preset": cad.preset, "prompt_tokens": tin,
            "max_tokens": mt, "worst_usd": worst, "cap_usd_run": cap, "unpriced_models": sorted(set(unpriced)),
            "why": None if ok else ("no cap_usd_run" if cap is None else
                                    "live run with an unpriced model" if (live and unpriced) else
                                    f"worst case ${worst:.4f} above the run cap ${float(cap):.4f}")}


def _measured_rate(path) -> tuple[float, str]:
    r = json.loads(Path(path).read_text(encoding="utf-8"))["pooled_per_agent_day"]
    return float(sum(r.get(t, 0.0) for t in ("T1", "T2", "T3", "T4", "T5"))), f"measured ({Path(path).name})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rates", help="measured trigger rates (bench.m4_trigger_rates output)")
    ap.add_argument("--agents", type=int, default=80)
    ap.add_argument("--out-tokens", type=int, default=DEFAULT_OUT_TOKENS)
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--escalate", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    tin = measured_prompt_tokens()
    tables = {"assumed": table(a.agents, AS.DEFAULT_HONEST, tin, a.out_tokens, a.max_tokens, DEFAULT_RATE,
                               "assumed", a.escalate)}
    if a.rates:
        rate, lab = _measured_rate(a.rates)
        tables["measured"] = table(a.agents, AS.DEFAULT_HONEST, tin, a.out_tokens, a.max_tokens, rate, lab,
                                   a.escalate)
    for k, t in tables.items():
        print(f"\n## trigger rate {t['trigger_rate']['value']:.3f} per agent-day ({t['trigger_rate']['label']}); "
              f"{a.agents} agents; prompt {tin} tokens in, {a.out_tokens} out expected, {a.max_tokens} max\n")
        print(t["markdown"])
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(tables, indent=1, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
