"""Prompt builder for the LLM chooser (D59 firewall, D70). It renders only an AgentView: never the agent object, the
bus, the exchange, the config or raw JSON from a message. Numbers are formatted by code; the LLM answers with a label
from the allowed menu (structured output), never a number that code uses (D12)."""
from __future__ import annotations

from agents.llm.agent_view import AgentView
from agents.strategy import MENU_VERSION, POSTURE_MARGIN_USD_MWH, allowed_labels

PROMPT_VERSION = "posture-p1"

_MEANING = {
    "normal": "the default; shift toward the peak price only where it beats lost day-ahead arbitrage by about $5/MWh",
    "cautious": "respond less; shift only where the peak price beats lost arbitrage by about $10/MWh",
    "bold": "respond fully to the peak price, with no forecast-risk margin ($0/MWh)",
}


def _f(x, nd=1) -> str:
    return "none" if x is None else f"{float(x):.{nd}f}"


def _series(vals, nd=1) -> str:
    return " ".join(f"{float(v):.{nd}f}" for v in vals)


def schema(view: AgentView) -> dict:
    if not isinstance(view, AgentView):
        raise TypeError("schema takes an AgentView")
    return {"type": "object",
            "properties": {"posture": {"type": "string", "enum": list(allowed_labels(view.bold_allowed))},
                           "rationale": {"type": "string"}},
            "required": ["posture", "rationale"], "additionalProperties": False}


def render(view: AgentView) -> tuple[str, str]:
    if not isinstance(view, AgentView):
        raise TypeError("the prompt builder renders only an AgentView")
    labels = allowed_labels(view.bold_allowed)
    menu = "\n".join(f"- {lab}: {_MEANING[lab]}" for lab in labels)
    assert all(POSTURE_MARGIN_USD_MWH[lab] >= 0 for lab in labels)
    system = (
        "You choose a risk posture for one battery owner's agent on a flexibility exchange. The exchange posts a "
        "peak price; your agent may shift its battery away from its own day-ahead plan to answer it, and is paid on "
        "metered delivery against its own selfish schedule. Forecasts can be wrong, so answering the peak price can "
        "cost money. Your posture sets how much of a margin the agent needs before it shifts.\n"
        f"Allowed postures (menu {MENU_VERSION}):\n{menu}\n"
        "Reply with JSON only: {\"posture\": <one allowed posture>, \"rationale\": <one short sentence>}.")
    lines = [
        f"Decision for local day: {view.decision_day} (reason: {view.reason})",
        f"Battery: {_f(view.power_kw, 0)} kW, {_f(view.energy_kwh, 0)} kWh",
        f"Current posture: {view.current_label}; previous postures: {' '.join(view.label_history) or 'none'}",
        f"Published day-ahead prices for that day, hourly, $/MWh: {_series(view.da_prices_hourly)}",
        f"Day-ahead spread that day: {_f(view.da_spread)} $/MWh; 75th percentile of the trailing 30 days: "
        f"{_f(view.da_spread_threshold)} $/MWh",
        f"Your site's net load forecast for that day, hourly, kW: {_series(view.net_load_forecast_hourly)}",
        f"Last peak price broadcast: max {_f(view.peak_price_max)} $/MWh over {int(view.peak_price_steps)} steps",
        f"Your imbalance charges since your last decision: ${_f(view.imbalance_usd_since_last_call, 2)}",
        f"Rejects from the exchange since the last check: {int(view.rejects_since_eval)}"
        + (f" (codes: {' '.join(view.reject_codes)})" if view.reject_codes else ""),
        f"Binding calls announced since the last check: {int(view.binding_calls_since_eval)}",
        f"Peak settlement statement: net ${_f(view.peak_settle_net_usd, 2)} on a refund of "
        f"${_f(view.peak_settle_refund_usd, 2)}",
        f"Yesterday's site net load: forecast {_f(view.site_net_forecast_kwh_yesterday)} kWh, metered "
        f"{_f(view.site_net_metered_kwh_yesterday)} kWh",
    ]
    return system, "\n".join(lines)
