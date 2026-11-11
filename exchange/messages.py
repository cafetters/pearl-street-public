"""Message schema (PLAN.md section 5). Every agent-exchange interaction is one of these, sent as JSON.

Envelope: msg_id, run_id, sender, to, t_utc, msg_type, payload.
`to` is "exchange", an agent id, or a list of agent ids (a broadcast).
Times are ISO strings in UTC, e.g. "2023-01-02T06:00:00Z". Windows are {"start_utc", "end_utc"}, end exclusive.

D69 hygiene (rule 4): every message the exchange accepts has a per-type allow-list of payload keys and their types
(SPEC); unknown keys are refused. IDs (msg_id, sender, recipients, and payload ids that may come from a msg_id) match
ID_RE, times match TIME_RE, numbers are finite and never bools. validate() returns a constant problem text and a
reference to our own field name, never the sender's text (rule 5).
"""
import json
import math
import re
from datetime import datetime

ENVELOPE = ("msg_id", "run_id", "sender", "to", "t_utc", "msg_type", "payload")

REQUIRED = {
    "register": ("agent_id", "legal_entity", "assets", "mandate"),
    "subscribe": ("signals", "zones"),
    "post_need": ("zone", "window", "kw_needed", "mode", "price_cap", "gate_utc"),
    "offer": ("need_id", "mode", "zone", "window", "kw", "floor_usd_mwh", "standing"),
    "revise": ("offer_id",),
    "award": ("offer_id", "need_id", "window", "accredited_kw", "price_usd_mwh", "cap_kw"),
    "telemetry": ("asset_id", "soc_kwh", "meter_kw"),
    "suspend": ("offer_id", "reason"),
    "settle": ("award_id", "delivered_kw", "credit_usd", "charge_usd", "new_delivery_record"),
    "directive": ("action",),
    "tick": (),
    "reject": ("ref_msg_id", "code", "detail", "refs"),
    "call_nominations": ("zone", "window", "deadline_utc", "binding"),
    "nominate": ("call_id", "zone", "window", "kw"),
    "nomination_aggregate": ("call_id", "zone", "window", "kw", "n_agents"),
    "imbalance": ("call_id", "window", "deviation_kwh", "charge_usd"),
    # M2a (D37)
    "zone_target": ("zone", "window", "target_kw"),
    "asset_limits": ("asset_id", "usable_kwh", "power_kw", "pv_kwh_per_day"),
    "clearing_report": ("need_id", "price_usd_mwh", "accepted", "skipped"),
    "rebound": ("window", "excess_kwh", "charge_usd"),
    # D42: sequential clearing within a gate
    "need_cleared": ("need_id",),
    "gate_prices": ("gate_utc", "prices"),
    # D47: the D34 cap re-based at a binding call's deadline
    "cap_rebase": ("call_id", "window", "base_kw", "nomination_kw"),
    # D50: published per-step zone room (target minus zone load)
    "zone_room": ("zone", "window", "room_kw"),
    # D53: price mode (arm C)
    "peak_price": ("zone", "call_id", "window", "price_usd_mwh", "rho", "shift_kw", "round", "final"),
    "reference_net": ("asset_id", "window", "net_kw"),
    "peak_settle": ("window", "steps", "charge_usd", "refund_usd", "net_usd"),
    # D56: rule 1p's refund reference for one binding call (metering service only)
    "selfish_reference": ("asset_id", "call_id", "window", "net_kw"),
    # D62 (Leaning): the modeled response and acting-alone paths for one binding call's stage, the relayed real-time
    # prices, and the per-agent accrual the exchange sends at the closing tick
    "modeled_response": ("asset_id", "call_id", "window", "p_net_kw", "p_discharge_kw", "p_soc_end_kwh", "q_net_kw",
                         "q_discharge_kw", "q_soc_end_kwh"),
    "rt_price": ("window", "rt_usd_mwh", "end_energy_usd_kwh"),
    "guarantee_accrual": ("window", "calls", "shortfall_usd_by_span", "shortfall_usd", "modeled_shift_kwh",
                          "delivered_kwh"),
    # D64 enrollment and availability (agent to exchange) and the D65 delivery record (exchange to agent)
    "deregister": ("agent_id", "effective_date"),
    "declare_availability": ("asset_id", "date", "power_kw", "reserve_kwh", "planned_outage"),
    "declare_outage": ("asset_id", "window"),
    "delivery_record": ("assets", "this_run", "record"),
}

# Sent only by the exchange
EXCHANGE_TYPES = {"award", "suspend", "settle", "reject", "nomination_aggregate", "imbalance", "clearing_report",
                  "rebound", "need_cleared", "gate_prices", "cap_rebase", "zone_room", "peak_price",
                  "peak_settle", "guarantee_accrual", "delivery_record"}

REJECT_CODES = ("outside_mandate", "over_demonstrated_limit", "already_sold", "after_gate_closure",
                "not_registered", "unauthorized_sender", "bad_message",
                "sender_mismatch", "direct_delivery", "malformed",       # these three: bus refusals (refusal_notice)
                "infeasible_declaration")                                # D82, under declared_reference only

WINDOW_KEYS = ("start_utc", "end_utc")


def make(msg_type: str, sender: str, to, t_utc: str, payload: dict, msg_id: str, run_id: str) -> dict:
    return {"msg_id": msg_id, "run_id": run_id, "sender": sender, "to": to, "t_utc": t_utc,
            "msg_type": msg_type, "payload": payload}


def encode(msg: dict) -> str:
    return json.dumps(msg, separators=(",", ":"), sort_keys=True)


def decode(raw: str) -> dict:
    return json.loads(raw)


ID_RE = re.compile(r"[A-Za-z0-9_.:/-]{1,64}", re.ASCII)
TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.ASCII)
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
TEXT_MAX = 128                     # free text (legal_entity): printable ASCII, at most this long
LIST_MAX = 100_000                 # longest list accepted (a year of 15-minute steps is 35,040)


def is_id(v) -> bool:
    return isinstance(v, str) and ID_RE.fullmatch(v) is not None


def is_time(v) -> bool:
    """A real UTC time in the one accepted format (2023-02-30T00:00:00Z fails)."""
    if not (isinstance(v, str) and TIME_RE.fullmatch(v) is not None):
        return False
    try:
        datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def is_num(v) -> bool:
    """A finite real number, never a bool; huge JSON integers (beyond float range) fail instead of raising."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:
        return False


def _text(v) -> bool:
    return isinstance(v, str) and 0 < len(v) <= TEXT_MAX and all(32 <= ord(c) < 127 for c in v)


def _nums(v, nullable=False) -> bool:
    return isinstance(v, list) and len(v) <= LIST_MAX and all(is_num(x) or (nullable and x is None) for x in v)


def _ids(v) -> bool:
    return isinstance(v, list) and len(v) <= 1000 and all(is_id(x) for x in v)


def _window(v) -> bool:
    return isinstance(v, dict) and set(v) == set(WINDOW_KEYS) and all(is_time(v[k]) for k in WINDOW_KEYS)


def _prep(v) -> bool:
    return v is None or (isinstance(v, dict) and set(v) == {"start_utc", "kw"} and is_time(v["start_utc"])
                         and _nums(v["kw"]))


def _asset(v) -> bool:
    return isinstance(v, dict) and set(v) == {"asset_id", "zone", "type"} and all(is_id(v[k]) for k in v)


def _assets(v) -> bool:
    return isinstance(v, list) and len(v) <= 100 and all(_asset(a) for a in v)


def _mandate(v) -> bool:
    if not (isinstance(v, dict) and {"max_kw", "price_floor"} <= set(v)
            and set(v) <= {"max_kw", "price_floor", "workload_limits", "daily_kwh"}):
        return False
    if not (is_num(v["max_kw"]) and is_num(v["price_floor"]) and v.get("workload_limits") is None):
        return False
    d = v.get("daily_kwh")
    return d is None or (isinstance(d, dict) and len(d) <= 400
                         and all(isinstance(k, str) and DATE_RE.fullmatch(k) and is_num(x) for k, x in d.items()))


def _date(v) -> bool:
    if not (isinstance(v, str) and DATE_RE.fullmatch(v) is not None):
        return False
    try:
        datetime.strptime(v, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _bool(v) -> bool:
    return isinstance(v, bool)


def _num_or_none(v) -> bool:
    return v is None or is_num(v)


# Per-type allow-list for messages sent to the exchange: (required {key: check}, optional {key: check}).
_W, _I, _T, _N, _L = _window, is_id, is_time, is_num, _nums
SPEC = {
    "register": ({"agent_id": _I, "legal_entity": _text, "assets": _assets, "mandate": _mandate}, {}),
    "subscribe": ({"signals": _ids, "zones": _ids}, {}),
    "post_need": ({"zone": _I, "window": _W, "kw_needed": _N, "mode": _I, "price_cap": _N, "gate_utc": _T},
                  {"round": _int, "priority": _int}),
    "offer": ({"need_id": _I, "mode": _I, "zone": _I, "window": _W, "kw": _N, "floor_usd_mwh": _N, "standing": _bool},
              {"prep": _prep}),
    "revise": ({"offer_id": _I}, {"withdraw": _bool, "kw": _N, "floor_usd_mwh": _N, "prep": _prep}),
    "telemetry": ({"asset_id": _I, "soc_kwh": _N, "meter_kw": _N}, {"net_kw": _N}),
    "tick": ({}, {"substep": _int, "price_round": _int, "final": _bool}),
    "call_nominations": ({"zone": _I, "window": _W, "deadline_utc": _T, "binding": _bool}, {"round": _int}),
    "nominate": ({"call_id": _I, "zone": _I, "window": _W, "kw": _L}, {}),
    "zone_target": ({"zone": _I, "window": _W, "target_kw": _L}, {}),
    "asset_limits": ({"asset_id": _I, "usable_kwh": _N, "power_kw": _N, "pv_kwh_per_day": _N},
                     {"owner": _text, "agent_id": _I}),
    "reference_net": ({"asset_id": _I, "window": _W, "net_kw": _L}, {}),
    "selfish_reference": ({"asset_id": _I, "call_id": _I, "window": _W, "net_kw": _L},
                          {"net_kw_registered": _L,            # D81, under declared_reference only
                           "declarations_void": _bool}),        # D82's backstop, under declared_reference only
    "modeled_response": ({"asset_id": _I, "call_id": _I, "window": _W, "p_net_kw": _L, "p_discharge_kw": _L,
                          "p_soc_end_kwh": _N, "q_net_kw": _L, "q_discharge_kw": _L, "q_soc_end_kwh": _N}, {}),
    "rt_price": ({"window": _W, "rt_usd_mwh": _L, "end_energy_usd_kwh": _N}, {}),
    "deregister": ({"agent_id": _I, "effective_date": _date}, {}),
    "declare_availability": ({"asset_id": _I, "date": _date, "power_kw": _N, "reserve_kwh": _N,
                              "planned_outage": _bool}, {}),
    "declare_outage": ({"asset_id": _I, "window": _W}, {}),
}


def problem(msg) -> tuple[str, dict] | None:
    """None if the message is well formed, else (constant problem text, refs). refs name only our own fields."""
    if not isinstance(msg, dict):
        return "message is not a JSON object", {}
    if set(msg) != set(ENVELOPE):
        return "envelope keys must be exactly " + ", ".join(ENVELOPE), {}
    if not is_id(msg["msg_id"]):
        return "msg_id must be an id (letters, digits, _ . : / -, at most 64)", {"field": "msg_id"}
    if not (is_id(msg["sender"]) and is_id(msg["run_id"])):
        return "sender and run_id must be ids", {"field": "sender"}
    if not is_time(msg["t_utc"]):
        return "t_utc must look like 2023-01-02T06:00:00Z", {"field": "t_utc"}
    to = msg["to"]
    if not (is_id(to) or (isinstance(to, list) and to and len(to) <= 10_000 and all(is_id(r) for r in to))):
        return "to must be an id or a non-empty list of ids", {"field": "to"}
    if not isinstance(msg["msg_type"], str) or msg["msg_type"] not in REQUIRED:
        return "unknown msg_type", {"field": "msg_type"}
    p = msg["payload"]
    if not isinstance(p, dict):
        return "payload is not an object", {"field": "payload"}
    spec = SPEC.get(msg["msg_type"])
    if spec is None:                      # types only the exchange sends: shape only; the exchange refuses them
        if any(k not in p for k in REQUIRED[msg["msg_type"]]):
            return "payload missing required keys", {"msg_type": msg["msg_type"]}
        return None
    req, opt = spec
    for k, check in req.items():
        if k not in p:
            return "payload missing a required key", {"field": f"payload.{k}"}
        if not check(p[k]):
            return "payload key has the wrong type or format", {"field": f"payload.{k}"}
    for k, v in p.items():
        if k in req:
            continue
        if k not in opt:
            return "payload has a key outside the allow-list", {"msg_type": msg["msg_type"]}
        if not opt[k](v):
            return "payload key has the wrong type or format", {"field": f"payload.{k}"}
    return None


def validate(msg) -> str | None:
    """None if the message is well formed, else what is wrong (constant text; see problem())."""
    pr = problem(msg)
    return None if pr is None else pr[0]
