"""
The exchange: registry, order book, clearing and settlement.

One entry point, handle(raw JSON) -> list of raw JSON replies, so an MCP tool can wrap it later.
It never reads data files and never reads a clock: time comes only from the messages it receives
(the simulator's `tick` closes gates and triggers settlement).
"""
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from zoneinfo import ZoneInfo

from exchange import messages as M
from exchange.clearing import clear, clear_with
from exchange.imbalance import band_kw, imbalance
from exchange import guarantee as G
from exchange import price_mode as PM
from exchange import records as REC
from exchange.settlement import settle_award

ISO = "%Y-%m-%dT%H:%M:%SZ"
ENERGY_TOL_KWH = 0.01      # daily_kwh checks: floating-point slack, far below anything that matters physically


def parse_t(s: str) -> datetime:
    return datetime.strptime(s, ISO).replace(tzinfo=timezone.utc)


def fmt_t(t: datetime) -> str:
    return t.strftime(ISO)


# constant text for the bus-refusal notices (refusal_notice); never built from a message's strings (D69 rule 5)
REFUSAL_DETAIL = {
    "sender_mismatch": "the sender field does not match the endpoint the message came from; the refusal is recorded",
    "direct_delivery": "only the exchange delivers to other endpoints; the refusal is recorded",
    "malformed": "the message is not a valid envelope; the refusal is recorded",
}


class Reject(Exception):
    """A refusal. detail is constant text (never built from a message's strings, D69 rule 5); refs carry structured
    references: our own field names, exchange-held numbers, and ids or times that passed validation."""
    def __init__(self, code: str, detail: str, **refs):
        super().__init__(detail)
        self.code, self.detail, self.refs = code, detail, refs


def _from_ok(ex, t: str) -> bool:
    """Warm-up (settle_from_utc): money counts at step t. Read with getattr so offline tools that call the settlement
    methods on a stand-in object (bench/d56_resettle's tests) keep today's behaviour."""
    sf = getattr(ex, "settle_from", None)
    return sf is None or t >= sf


def _safe_ref(v) -> bool:
    if v is None or isinstance(v, bool) or M.is_num(v) or M.is_id(v) or M.is_time(v):
        return True
    return isinstance(v, list) and len(v) <= 100 and all(_safe_ref(x) and not isinstance(x, list) for x in v)


_EMPTY_VIEW = MappingProxyType({})     # _latest for an agent that has not nominated


# D76 max rule: agent-steps by settlement path (Exchange.settle_paths). max_rule: the agent's full chain;
# max_rule_after_d77_drop / today_via_d77: D77 dropped calls the agent wasn't registered for, leaving two or more calls /
# the settling call alone (today's 1p); today_partly_excused: the settling call partly excused (today's D77 treatment);
# not_settled_d77: the settling call fully excused; of_which_settling_price_zero: settled steps whose settling call
# priced 0 (paid only through an earlier call's price; owner review item 2c).
SETTLE_PATHS = ("max_rule", "max_rule_after_d77_drop", "today_via_d77", "today_partly_excused", "not_settled_d77",
                "of_which_settling_price_zero")


class Exchange:
    def __init__(self, run_id: str, operator_id: str, metering_id: str, clock_id: str, zones: list[str],
                 step_minutes: int = 15, require_nominations: bool = False, scarcity_usd_mwh: float | None = None,
                 band_pct: float = 0.05, band_min_kw: float = 50.0, prep_clearing: bool = False,
                 no_rebound: bool = False, rebound_hours: float = 24.0, daily_energy: bool = False,
                 tz: str = "America/Chicago", partial_prep: bool = False, min_partial_kw: float = 50.0,
                 sequential_clearing: bool = False, cap_rebase: bool = False, publish_room: bool = False,
                 price_mode: bool = False, price_rounds: int = 20, price_spread_usd_mwh: float | None = None,
                 price_cap_usd_mwh: float | None = None, price_rb_mu: float = 10.0, price_rb_tau: float = 2.0,
                 price_rho_floor: float = 1000.0, price_rho_cap: float = 10.0, price_tol_excess_kw: float = 1000.0,
                 price_tol_dual_usd_mwh: float = 6.0, price_streak: int = 3,
                 settlement_reference: str = "recorded", guarantee: bool = False,
                 guarantee_wear_usd_mwh: float = 0.0, attested_owners: bool = True, delivery_record: bool = False,
                 da_publish_hour_local: int = 12, max_outage_days_per_season: int | None = None,
                 refusal_notice: bool = False, settlement_rule: str = "today", silent_reference: bool = False,
                 declared_reference: bool = False, settle_from_utc: str | None = None):
        """require_nominations: offers clear only for agents whose nominations cover the need's window (D32).
        scarcity_usd_mwh: the zone's scarcity price for imbalance; None turns imbalance off (M1).
        M2a (D34, D37), all off by default: prep_clearing (an offer clears only if its declared preparation keeps
        the zone at or below the operator's target), no_rebound (awarded agents' load capped at their pre-award
        nomination plus awarded prep, from the first award to each window end + rebound_hours), daily_energy
        (offers plus awards per local day within the mandate's daily_kwh; registration checks it against the
        metering service's demonstrated limits). tz only names local days. partial_prep (D41): an offer whose
        prep doesn't fit is accepted for the largest part whose pro-rata prep does (at least min_partial_kw).
        sequential_clearing (D42): needs sharing a gate clear one per tick in the operator's priority order. Between
        clearings only agents with a new award since the gate may revise (lower kW, prep, floor); need_cleared carries
        the need id only, and each need's uniform price is published in one gate_prices message after the last one.
        Offers are alternatives, so mandate and daily-energy checks count awards plus this offer, re-checked at
        clearing.
        cap_rebase (D47): at each binding call's deadline a capped agent's D34 cap is raised to its new binding
        nomination where that adds load, only within zone room (target minus zone load), pro rata across agents
        when short. A re-base never lowers a cap: a cap limits added load and must not turn planned discharge into
        an obligation. Where added load is refused, the binding nomination is clipped to the cap, so the agent owes
        no imbalance for it. Each agent gets a cap_rebase.
        publish_room (D50): the exchange publishes zone_room (target minus zone load from latest nominations and
        awarded prep, per step to the end of the horizon) to the zone's subscribers with each post_need broadcast
        when it moved by more than 1 kW, and after every clearing. The target itself stays private.
        price_mode (D53, arm C): at a clock tick carrying price_round r, the exchange runs round r at the open
        nomination call: from the zone's latest nominations vs. its target it measures the last round (excess, dual
        residual), stops when converged, else adapts rho (residual balancing on the scaled dual residual, within
        rho0 / price_rho_floor to price_rho_cap x rho0) and updates the per-step peak price (alpha = rho / N, capped at
        price_cap_usd_mwh), and broadcasts peak_price with the price, rho and the textbook centre shift. rho0 comes from
        public data: price_spread_usd_mwh (mean daily day-ahead spread) x K / median registered power. At most
        price_rounds rounds per call; the last broadcast is `final`. At a binding call's deadline the price in force is
        settled for the steps it binds; at the closing tick each agent gets peak_settle: the settled price on metered
        site net load, refunded on its reference_net (recorded schedule, from the metering service).
        settlement_reference (D56): "recorded" (D53 as run: the refund reference is reference_net) or "selfish_1p"
        (rule 1p: each priced step is refunded on the selfish_reference the metering service sent for the binding
        call that settled that step, a selfish battery schedule computed from registration and public data only).
        A selfish_reference must name a binding call, cover the call's window, arrive before the call's deadline,
        and is never revised. A priced step without one for every asset makes peak settlement fail.
        settlement_rule (D76, Leaning; only with price mode and selfish_1p): "today" (the default: each priced step is
        settled at the last binding call's price on metered site net less that call's reference) or "max_first_call"
        (the max rule, as bench/item7_resettle.max_first_call_steps): with c_1 .. c_k the binding calls that price a
        step, in closing order, rel_i = N_i - Q_i (binding nomination less reference, site net), each increment
        rel_i - rel_{i-1} (rel_1 for the first) is paid at the highest settled price among c_i and the later calls,
        and the metered deviation from the last nomination, N_metered - N_k, at c_k's price. With one call it is
        today's 1p. Relief cap (owner, Nov 4: the max rule never pays for relief the meter didn't show): the step is
        today's 1p at c_k, p_k (N_metered - Q_k), plus a premium for relief priced higher at earlier calls; where an
        earlier call priced the step above c_k (P1 > p_k, P1 the highest price), the premium is
        sum_i (L_i - L_{i+1}) x max(0, min(rel_i, N_metered - Q_j)) (L_{k+1} = p_k), Q_j the reference of the last
        call that priced the step above 0: each call's relief at its own price spread, never above the metered relief. Two calls with p_B < p_A: (p_A - p_B) x max(0, min(N_A - Q_A, N_metered - Q_j)). So relief nominated
        at an earlier call and withdrawn at a later, lower-priced call is paid only as far as the meter delivered it. A step with no binding nomination from the agent counts as nominating its reference (N_i = Q_i).
        A call whose reference an asset lacks because it had no holder at that call's gate (D77) drops out of the
        agent's chain (the agent wasn't registered at that gate, so it has no nomination for that call either). If
        it is the settling call, the step is settled as today: not at all when every asset is excused, else on the
        other assets' reference with the excused assets' metered load left out. peak_settle keeps its fields:
        charge_usd is the last call's price on metered load, refund_usd = net_usd + charge_usd. The setting enters the
        config (and so the trace header) only when it is not "today".
        guarantee (D62, Leaning; needs price mode and selfish_1p): after each binding call's final price the metering
        service sends each asset's modeled_response (D62's modeled m = 0 response P* and acting-alone path Q* from the
        call's deadline to the slice end), and before the closing tick the real-time prices (rt_price). At the closing
        tick each agent gets guarantee_accrual: the shortfall of P* against Q* per span (exchange/guarantee.py, with
        guarantee_wear_usd_mwh, 1p's planner constant), and D65's response measure. A binding call that settled priced
        steps without a modeled_response for every asset, or no rt_price, makes the accrual fail closed. Both
        settings enter the config (and so the trace header) only when guarantee is on.
        attested_owners (D69 rule 6, on by default): the metering service attests each asset's owner in asset_limits:
        the liable legal entity (`owner`) and the agent that operates the asset for it (`agent_id`). register refuses
        an asset unless the sender is the attested agent and its legal_entity is the attested owner. Re-attesting an
        asset to another agent of the same entity (a software swap) is allowed and keeps the D65 record, which is kept
        per asset and legal entity; re-attesting it to a different entity is refused. Replay of traces written before
        D69 (no such key in the header) turns it off, since those runs sent no attestation.
        delivery_record (D64 enrollment and availability, D65 record; off by default, Leaning): agents may deregister
        (from a later local day, filed before that day's day-ahead prices publish at da_publish_hour_local on the day
        before), declare availability per local day before the same deadline (power available, reserve energy, planned
        outage; planned outage days per season capped at max_outage_days_per_season) and declare forced outages at any
        time (late if after the deadline for the outage's day). At the closing tick each agent gets delivery_record:
        per asset and legal entity, binding nominations delivered within the D33 band (each step weighs max(|kW|, band)), binding calls
        missed, declarations, late outages, and battery activity during declared outages or below declared reserve.
        The record never charges; D65's response measure is a hook only. These settings enter the config (and so the
        trace header) only when delivery_record is on."""
        self.config = {"run_id": run_id, "operator_id": operator_id, "metering_id": metering_id,
                       "clock_id": clock_id, "zones": list(zones), "step_minutes": step_minutes,
                       "require_nominations": require_nominations, "scarcity_usd_mwh": scarcity_usd_mwh,
                       "band_pct": band_pct, "band_min_kw": band_min_kw, "prep_clearing": prep_clearing,
                       "no_rebound": no_rebound, "rebound_hours": rebound_hours, "daily_energy": daily_energy,
                       "tz": tz, "partial_prep": partial_prep, "min_partial_kw": min_partial_kw,
                       "sequential_clearing": sequential_clearing, "cap_rebase": cap_rebase,
                       "publish_room": publish_room, "price_mode": price_mode, "price_rounds": price_rounds,
                       "price_spread_usd_mwh": price_spread_usd_mwh, "price_cap_usd_mwh": price_cap_usd_mwh,
                       "price_rb_mu": price_rb_mu, "price_rb_tau": price_rb_tau, "price_rho_floor": price_rho_floor,
                       "price_rho_cap": price_rho_cap, "price_tol_excess_kw": price_tol_excess_kw,
                       "price_tol_dual_usd_mwh": price_tol_dual_usd_mwh, "price_streak": price_streak,
                       "settlement_reference": settlement_reference, "attested_owners": attested_owners}
        if settlement_reference not in ("recorded", "selfish_1p"):
            raise ValueError(f"settlement_reference must be 'recorded' or 'selfish_1p', not {settlement_reference!r}")
        if settlement_rule not in ("today", "max_first_call"):
            raise ValueError(f"settlement_rule must be 'today' or 'max_first_call', not {settlement_rule!r}")
        if settlement_rule != "today":
            if not (price_mode and settlement_reference == "selfish_1p"):
                raise ValueError("settlement_rule max_first_call (D76) needs price_mode and settlement_reference "
                                 "selfish_1p")
            self.config["settlement_rule"] = settlement_rule
        self.settlement_rule = settlement_rule
        # D78/D79 (owner, Nov 3; off by default): a step with no nomination from an active agent counts as the agent
        # having nominated its 1p reference there (N = Q), in the price loop, the binding aggregate and the imbalance
        # base; the metering service sends references for every call that runs price rounds
        if silent_reference:
            if not (price_mode and settlement_reference == "selfish_1p"):
                raise ValueError("silent_reference (D78) needs price_mode and settlement_reference selfish_1p")
            self.config["silent_reference"] = True
        self.silent_reference = bool(silent_reference)
        self.ref_base: dict[str, set[str]] = {}            # agent -> bound steps whose imbalance base is its reference
        # D80/D81 (owner, Nov 3; off by default): one flag for both halves. Accepted declarations enter 1p's reference
        # (the metering service reads them through declared_limits), and on a local day with a breach the day's
        # declaration is void: settlement re-reads the reference from the registered limits at the closing tick
        if declared_reference:
            if not (price_mode and settlement_reference == "selfish_1p" and delivery_record):
                raise ValueError("declared_reference (D80) needs price_mode, settlement_reference selfish_1p and "
                                 "delivery_record")
            if guarantee:
                raise ValueError("declared_reference (D80) with the D62 guarantee: P doesn't get declarations in "
                                 "this build (owner, Nov 3, Q-R2)")
            self.config["declared_reference"] = True
        self.declared_reference = bool(declared_reference)
        # warm-up (owner, Nov 6; off by default): the run starts before the window it reports on. Prices, nominations,
        # locks and references run on every step as usual; peak settlement and imbalance count only steps at or after
        # settle_from_utc, so the money is the window's. Agents only add up money messages, never plan on them, so
        # behaviour doesn't change. Arm C only (price_mode), without the D62 guarantee or the delivery record
        if settle_from_utc is not None:
            parse_t(settle_from_utc)
            if not price_mode or guarantee or delivery_record:
                raise ValueError("settle_from_utc (warm-up) needs price_mode, without the D62 guarantee or the "
                                 "delivery record")
            self.config["settle_from_utc"] = settle_from_utc
        self.settle_from = settle_from_utc
        self.call_reference_reg: dict[str, dict[str, dict[str, float]]] = {}   # D81: the registered-limits reference
        self.voided: dict[str, list[str]] = {}                  # D81: asset -> local dates whose declaration was void
        self.call_ref_days: dict[str, dict[str, frozenset]] = {}   # D87: call -> asset -> days in force at filing
        self.voided_calls: dict[str, list[str]] = {}            # D87: asset -> calls whose whole reference was void
        # D82: the metering service's 1p feasibility check, check(asset_id, as_of) -> bool, read with every accepted
        # declaration of the asset as of as_of (the one being filed included). Installed by sim/run_m1.py; under the
        # flag, with none installed every declaration is rejected (fails closed). The exchange reads no data.
        self.declaration_check = None
        self.infeasible_calls: dict[str, set[str]] = {}         # D82 backstop: asset -> calls whose declarations were void
        self.declarations_voided: dict[str, list[dict]] = {}    # D82 amendment: asset -> voluntary declarations an outage voided
        self._decl_seq = 0                                      # D82: filing order of voluntary declarations
        self.void_done = False
        self.settle_chain: dict[str, list[str]] = {}       # t_utc -> binding calls that settled it, closing order (D76)
        self.call_price: dict[str, dict[str, float]] = {}  # call_id -> t_utc -> settled $/MWh (D76)
        self.call_nom: dict[str, dict[str, dict[str, float]]] = {}   # call_id -> agent -> t_utc -> binding kW (D76)
        # D76 (owner, Nov 3): agent-steps by settlement path at the closing tick, for the run summary
        self.settle_paths: dict[str, int] = dict.fromkeys(SETTLE_PATHS, 0) if settlement_rule != "today" else {}
        if guarantee:
            if not (price_mode and settlement_reference == "selfish_1p"):
                raise ValueError("guarantee (D62) needs price_mode and settlement_reference selfish_1p")
            self.config.update({"guarantee": True, "guarantee_wear_usd_mwh": float(guarantee_wear_usd_mwh)})
        self.guarantee, self.guarantee_wear = guarantee, float(guarantee_wear_usd_mwh)
        self.final_calls: set[str] = set()                  # calls whose final peak_price went out (D62)
        self.modeled: dict[str, dict[str, dict]] = {}       # call_id -> asset_id -> modeled_response payload (D62)
        self.rt: dict | None = None                         # rt_price payload (D62)
        self.guarantee_done = False
        self.delivery_record, self.da_hour, self.max_outage_days = delivery_record, da_publish_hour_local, \
            max_outage_days_per_season
        if delivery_record:
            self.config.update({"delivery_record": True, "da_publish_hour_local": da_publish_hour_local,
                                "max_outage_days_per_season": max_outage_days_per_season})
        self.declared: dict[str, dict[str, dict]] = {}     # asset -> local date -> availability declaration (D64)
        self.outages: dict[str, list[dict]] = {}           # asset -> forced outage declarations (D64)
        self.dereg: dict[str, str] = {}                    # agent -> local date its deregistration takes effect
        self.inactive: set[str] = set()                    # agents whose deregistration has taken effect
        self.tenure_from: dict[str, str] = {}               # agent -> D64 tenure start (never a sender's time)
        self.released: dict[str, str] = {}                  # asset -> when its last holder's deregistration began
        # D56 1p (owner, Nov 3): per asset, the binding calls whose gate closed, by the exchange's clock, while the asset
        # had no holder (before its first registration in the run, or between a holder's release and the next
        # registration). The metering service can't send a 1p reference for an unheld asset, so these are the only
        # (asset, call) pairs that may lack one
        self.unheld_at_gate: dict[str, frozenset[str]] = {}
        self._closed_at_release: dict[str, frozenset[str]] = {}
        self.last_tick = ""                                 # the clock's latest tick: the exchange's own time
        self.soc: dict[str, dict[str, float]] = {}         # asset -> t_utc -> SOC kWh (kept only with the record)
        self.ledger = REC.Ledger()
        self.record_done = False
        # D69 rule 1 amendment (owner, Nov 2; off by default): the bus hands each refusal to the exchange, which
        # answers the true origin with a coded reject (at most one per origin, reason and minute of its own clock)
        # and counts it against the origin's legal entity in the D65 record
        self.refusal_notice = refusal_notice
        if refusal_notice:
            self.config["refusal_notice"] = True
        self.refusal_counts: dict[str, dict[str, int]] = {}  # origin endpoint -> reason -> refusals this run
        self._notice_sent: set[tuple[str, str, str]] = set()
        self._n_notice = 0
        self.partial_prep, self.min_partial_kw = partial_prep, min_partial_kw
        self.sequential = sequential_clearing
        self.cap_rebase = cap_rebase
        self.publish_room = publish_room
        self.last_room: dict[str, list] = {}               # zone -> room_kw last published (D50)
        self.price_mode, self.price_rounds = price_mode, price_rounds
        self.price_spread = price_spread_usd_mwh
        self.price_cap = price_cap_usd_mwh if price_cap_usd_mwh is not None else scarcity_usd_mwh
        self.rb = (price_rb_mu, price_rb_tau, price_rho_floor, price_rho_cap)
        self.price_tol = (price_tol_excess_kw, price_tol_dual_usd_mwh, price_streak)
        self.pi: dict[str, float] = {}                     # t_utc -> peak price in force, $/kW-step (D53)
        self.rho = self.rho0 = self.alpha = None
        self.gate: dict | None = None                      # the call whose price rounds are running
        self.price_log: list[dict] = []                    # one row per measured or broadcast round
        self.peak_settled: dict[str, tuple[float, str]] = {}   # t_utc -> (settled $/MWh, binding call_id)
        self.reference: dict[str, dict[str, float]] = {}   # asset_id -> t_utc -> reference site net kW
        self.settlement_reference = settlement_reference
        self.call_reference: dict[str, dict[str, dict[str, float]]] = {}   # call_id -> asset_id -> t_utc -> kW (D56)
        self.peak_done = False
        self.gate_winners: dict[str, set] = {}             # gate_utc -> agents with an award from that gate
        self.gate_session: list[tuple[str, str, float | None]] = []   # (gate_utc, need_id, price) not yet published
        self.prep_clearing, self.no_rebound, self.daily_energy = prep_clearing, no_rebound, daily_energy
        self.rebound_span = timedelta(hours=rebound_hours)
        self.tz = ZoneInfo(tz)
        self.targets: dict[str, dict[str, float]] = {z: {} for z in zones}   # zone -> t_utc -> max zone load kW
        self.limits: dict[str, dict] = {}                  # asset_id -> demonstrated limits (from metering)
        self.attested_owners = attested_owners
        self.attested: dict[str, dict] = {}                # asset_id -> {owner: legal entity, agent_id} (D69)
        self.pending_prep: dict[str, dict[str, float]] = {}  # agent -> t -> awarded prep not yet in a nomination
        self.caps: dict[str, dict] = {}                    # agent -> D34 rebound cap
        self.rebound_done = False
        self.require_nominations, self.scarcity_usd_mwh = require_nominations, scarcity_usd_mwh
        self.band_pct, self.band_min_kw = band_pct, band_min_kw
        self.calls: dict[str, dict] = {}
        self.noms: dict[str, list[dict]] = {}              # agent_id -> nominations in arrival order
        # agent_id -> the merge of self.noms[agent_id] in arrival order (later values win), kept up to date by
        # _add_nom so price rounds don't re-merge every stored nomination. Same values and key order as a full merge.
        self._latest_cache: dict[str, dict[str, float]] = {}
        self._latest_view: dict[str, MappingProxyType] = {}   # read-only views of _latest_cache, one per agent
        # The binding nomination in force for each step: agent_id -> t_utc -> (kW, binding call_id). A later binding
        # call (after later awards) replaces earlier values for the steps it covers.
        self.baseline: dict[str, dict[str, tuple[float, str]]] = {}
        self.net_meter: dict[str, dict[str, float]] = {}   # asset_id -> t_utc -> site net kW
        self.run_id, self.operator_id, self.metering_id, self.clock_id = run_id, operator_id, metering_id, clock_id
        self.step = timedelta(minutes=step_minutes)
        self.step_h = step_minutes / 60.0
        self.agents: dict[str, dict] = {}
        self.asset_owner: dict[str, str] = {}
        self.subs: dict[str, list[str]] = {z: [] for z in zones}
        self.needs: dict[str, dict] = {}
        self.offers: dict[str, dict] = {}
        self.awards: dict[str, dict] = {}
        self.meter: dict[str, dict[str, float]] = {}     # asset_id -> t_utc -> meter_kw
        self.record: dict[str, list[float]] = {}         # agent_id -> [delivered kWh, awarded kWh]
        self._n = 0
        self._seq = 0

    # ---- entry point ----
    def handle(self, raw: str) -> list[str]:
        try:
            msg = M.decode(raw)
        except (ValueError, RecursionError):
            return [M.encode(self._reject({}, "bad_message", "not JSON"))]
        pr = M.problem(msg)
        if pr is None and msg["to"] != "exchange":
            pr = ("message is not addressed to the exchange", {"field": "to"})
        if pr:
            return [M.encode(self._reject(msg if isinstance(msg, dict) else {}, "bad_message", pr[0], pr[1]))]
        try:
            if msg["msg_type"] in M.EXCHANGE_TYPES:
                raise Reject("unauthorized_sender", "only the exchange sends this message type",
                             msg_type=msg["msg_type"])
            handler = getattr(self, "_on_" + msg["msg_type"], None)
            if handler is None:
                raise Reject("bad_message", "message type is not handled yet", msg_type=msg["msg_type"])
            out = handler(msg)
        except Reject as r:
            out = [self._reject(msg, r.code, r.detail, r.refs)]
        return [M.encode(m) for m in out]

    # ---- helpers ----
    def _msg(self, msg_type, to, t_utc, payload):
        self._n += 1
        return M.make(msg_type, "exchange", to, t_utc, payload, f"ex:{self._n}", self.run_id)

    def _reject(self, msg, code, detail, refs=None):
        """To the sender only; nothing from the message is echoed except its validated msg_id, sender and time."""
        sender = msg.get("sender") if M.is_id(msg.get("sender")) else "unknown"
        t = msg.get("t_utc") if M.is_time(msg.get("t_utc")) else ""
        ref = msg.get("msg_id") if M.is_id(msg.get("msg_id")) else None
        refs = {k: (v if _safe_ref(v) else None) for k, v in (refs or {}).items()}
        return self._msg("reject", sender, t, {"ref_msg_id": ref, "code": code, "detail": detail, "refs": refs})

    def bus_refusal(self, origin: str, reason: str, msg) -> list[str]:
        """refusal_notice: the bus refused a message from endpoint `origin` (sender_mismatch, direct_delivery or
        malformed). Count it, against the origin's legal entity if it is a registered agent, and answer the origin
        (never the claimed sender) with a coded reject: constant text, nothing echoed but a valid msg_id, and at most
        one per origin and reason per minute of the exchange's clock (its last tick). The notice has its own msg_id
        sequence, so every other exchange message keeps its id."""
        if not self.refusal_notice or reason not in REFUSAL_DETAIL:
            return []
        counts = self.refusal_counts.setdefault(origin, {})
        counts[reason] = counts.get(reason, 0) + 1
        if origin in self.agents:
            self.ledger.add_refusal(self.agents[origin]["legal_entity"], reason)
        bucket = (origin, reason, self.last_tick[:16])
        if bucket in self._notice_sent or not M.is_id(origin):
            return []
        self._notice_sent.add(bucket)
        self._n_notice += 1
        ref = msg.get("msg_id") if isinstance(msg, dict) and M.is_id(msg.get("msg_id")) else None
        notice = M.make("reject", "exchange", origin, self.last_tick,
                        {"ref_msg_id": ref, "code": reason, "detail": REFUSAL_DETAIL[reason],
                         "refs": {"recorded": True}}, f"ex:refusal{self._n_notice}", self.run_id)
        return [M.encode(notice)]

    def _steps(self, window) -> list[str]:
        try:
            t0, t1 = parse_t(window["start_utc"]), parse_t(window["end_utc"])
        except ValueError:
            raise Reject("bad_message", "window times must look like 2023-01-02T06:00:00Z", field="window")
        if t1 <= t0 or (t1 - t0) % self.step:
            raise Reject("bad_message", "window must be a positive whole number of steps", field="window")
        out, t = [], t0
        while t < t1:
            out.append(fmt_t(t))
            t += self.step
        return out

    def _registered(self, msg) -> str:
        if msg["sender"] not in self.agents or msg["sender"] in self.inactive:
            raise Reject("not_registered", "sender is not registered")
        return msg["sender"]

    def _only(self, msg, sender_id, what):
        if msg["sender"] != sender_id:
            raise Reject("unauthorized_sender", "this sender may not send this message type", allowed=sender_id)

    def _check_mandate(self, agent_id, steps, kw, floor, exclude=None):
        """Floor at or above the mandate's price floor, and the agent's live offers plus awards at any step
        stay within its max_kw."""
        mandate = self.agents[agent_id]["mandate"]
        if floor < mandate["price_floor"]:
            raise Reject("outside_mandate", "floor is below the mandate's price_floor", floor=floor,
                         price_floor=mandate["price_floor"])
        commits = [] if self.sequential else [o for oid, o in self.offers.items()
                                              if o["agent_id"] == agent_id and o["live"] and oid != exclude]
        commits += [a for a in self.awards.values() if a["agent_id"] == agent_id]
        for t in steps:
            load = sum(c.get("kw", c.get("accredited_kw", 0.0)) for c in commits if t in c["_steps"])
            if load + kw > mandate["max_kw"] + 1e-9:
                raise Reject("outside_mandate", "kW exceeds the mandate's max_kw", kw=load + kw, t_utc=t,
                             max_kw=mandate["max_kw"])

    def _local_date(self, t: str) -> str:
        return parse_t(t).astimezone(self.tz).date().isoformat()

    def _offer_energy(self, steps, kw) -> dict[str, float]:
        """kWh per local date for kw held over the steps; a window crossing midnight splits by date."""
        out: dict[str, float] = {}
        for t in steps:
            d = self._local_date(t)
            out[d] = out.get(d, 0.0) + kw * self.step_h
        return out

    def _check_daily(self, agent_id, steps, kw, exclude=None):
        mandate = self.agents[agent_id]["mandate"]
        budget = mandate.get("daily_kwh")
        if not self.daily_energy or budget is None:
            return
        used: dict[str, float] = {}
        commits = [] if self.sequential else [o for oid, o in self.offers.items()
                                              if o["agent_id"] == agent_id and o["live"] and oid != exclude]
        for c in commits:
            for d, e in self._offer_energy(sorted(c["_steps"]), c["kw"]).items():
                used[d] = used.get(d, 0.0) + e
        for a in self.awards.values():
            if a["agent_id"] == agent_id:
                for d, e in self._offer_energy(sorted(a["_steps"]), a["accredited_kw"]).items():
                    used[d] = used.get(d, 0.0) + e
        for d, e in self._offer_energy(steps, kw).items():
            if used.get(d, 0.0) + e > budget.get(d, 0.0) + ENERGY_TOL_KWH:
                raise Reject("outside_mandate", "kWh on a local date exceeds daily_kwh", kwh=used.get(d, 0.0) + e,
                             date=d, daily_kwh=budget.get(d, 0.0))

    def _parse_prep(self, prep, need):
        """{start_utc, kw[]} -> {t: kW > 0}. Extra net load the agent needs outside the window to deliver, from the
        gate on; zero inside the window."""
        if prep is None:
            return {}
        if not (isinstance(prep, dict) and isinstance(prep.get("start_utc"), str) and isinstance(prep.get("kw"), list)):
            raise Reject("bad_message", "prep must be {start_utc, kw[]}", field="prep")
        kw = prep["kw"]
        if not kw or not all(isinstance(v, (int, float)) and v == v and 0 <= v < float("inf") for v in kw):
            raise Reject("bad_message", "prep kw must be finite and >= 0", field="prep")
        if prep["start_utc"] < need["gate_utc"]:
            raise Reject("bad_message", "prep cannot start before the gate", field="prep")
        try:
            t0 = parse_t(prep["start_utc"])
        except ValueError:
            raise Reject("bad_message", "prep start_utc must look like 2023-01-02T06:00:00Z", field="prep")
        out = {}
        for i, v in enumerate(kw):
            t = fmt_t(t0 + i * self.step)
            if v > 0:
                if t in need["_steps"]:
                    raise Reject("bad_message", "prep must be zero inside the offer's window", field="prep")
                out[t] = float(v)
        return out

    def _ref_net(self, aid, call_id, t) -> float | None:
        """D78: the agent's 1p reference site net at t (kW, + = export, the nomination's convention) for the call,
        summed over its assets; None if the flag is off or any asset has no reference there."""
        if not self.silent_reference or call_id is None:
            return None
        refs = self.call_reference.get(call_id, {})
        total = 0.0
        for x in self.agents[aid]["assets"]:
            r = refs.get(x, {}).get(t)
            if r is None:
                return None
            total += r
        return total

    def _nom_or_ref(self, aid, call_id, t) -> float | None:
        """The agent's latest nomination at t, or under D78 its 1p reference for the call where it has none."""
        v = self._latest(aid).get(t)
        return self._ref_net(aid, call_id, t) if v is None else v

    def _zone_load(self, zone, t, call_id=None) -> float | None:
        """Zone participants' total net load at t from their latest nominations plus awarded prep not yet in a
        nomination. None if any registered agent in the zone has no nomination there (under D78's silent_reference,
        given the call, an agent with no nomination counts at its 1p reference for that call; None if that is missing
        too)."""
        total = 0.0
        for aid, a in self.agents.items():
            if zone not in a["zones"].values() or aid in self.inactive:
                continue
            v = self._nom_or_ref(aid, call_id, t)
            if v is None:
                return None
            total += -v + self.pending_prep.get(aid, {}).get(t, 0.0)
        return total

    def _gate_open(self, need, msg):
        if need["status"] != "open" or msg["t_utc"] >= need["gate_utc"]:
            raise Reject("after_gate_closure", "the need's gate is closed", need_id=need["need_id"], gate_utc=need["gate_utc"])

    # ---- handlers ----
    def _on_register(self, msg):
        p = msg["payload"]
        aid = p["agent_id"]
        if msg["sender"] != aid:
            raise Reject("unauthorized_sender", "agent_id must match the sender", field="agent_id")
        if aid in self.agents or aid in (self.operator_id, self.metering_id, self.clock_id, "exchange"):
            raise Reject("bad_message", "agent_id is already registered or reserved", field="agent_id")
        mandate = p["mandate"]
        if not (isinstance(mandate, dict) and isinstance(mandate.get("max_kw"), (int, float))
                and isinstance(mandate.get("price_floor"), (int, float))):
            raise Reject("bad_message", "mandate needs numeric max_kw and price_floor", field="mandate")
        assets = p["assets"]
        if not isinstance(assets, list) or not assets:
            raise Reject("bad_message", "assets must be a non-empty list", field="assets")
        for a in assets:
            if not isinstance(a, dict) or not {"asset_id", "zone", "type"} <= a.keys():
                raise Reject("bad_message", "each asset needs asset_id, zone and type", field="assets")
            if a["zone"] not in self.subs:
                raise Reject("bad_message", "unknown zone", field="assets")
            if a["asset_id"] in self.asset_owner:
                raise Reject("bad_message", "asset is already registered", asset_id=a["asset_id"])
            att = self.attested.get(a["asset_id"])
            if self.attested_owners and (att is None or att["agent_id"] != aid):
                raise Reject("unauthorized_sender", "the metering service has not attested this sender as the asset's "
                                                    "operator", asset_id=a["asset_id"])
            if self.attested_owners and att["owner"] != p["legal_entity"]:
                raise Reject("unauthorized_sender", "legal_entity is not the asset's attested owner",
                             asset_id=a["asset_id"], field="legal_entity")
        budget = mandate.get("daily_kwh")
        if budget is not None:
            if not (isinstance(budget, dict) and all(isinstance(v, (int, float)) and v >= 0 for v in budget.values())):
                raise Reject("bad_message", "daily_kwh must map local dates to kWh >= 0", field="mandate.daily_kwh")
            if any(a["asset_id"] not in self.limits for a in assets):
                raise Reject("over_demonstrated_limit", "no demonstrated limits on record for these assets")
            demo = sum(self.limits[a["asset_id"]]["usable_kwh"] + self.limits[a["asset_id"]]["pv_kwh_per_day"]
                       for a in assets)
            over = {d: v for d, v in budget.items() if v > demo + ENERGY_TOL_KWH}
            if over:
                raise Reject("over_demonstrated_limit", "daily_kwh above demonstrated limits", dates=sorted(over)[:100],
                             demonstrated_kwh=demo)
        # D64 tenure (used only with delivery_record): it starts at the later of the exchange's own clock (the last
        # tick) and where the asset's previous holder's tenure ended, never at the message's t_utc, which the sender
        # writes. A future-dated register can't escape billing, a backdated one can't overlap, and an asset
        # registered for the first time mid-run isn't billed for steps before it registered
        self.tenure_from[aid] = max([self.last_tick, *(self.released.get(a["asset_id"], "") for a in assets)])
        self.agents[aid] = {"legal_entity": p["legal_entity"], "assets": [a["asset_id"] for a in assets],
                            "zones": {a["asset_id"]: a["zone"] for a in assets}, "mandate": dict(mandate)}
        closed = self._closed_binding_calls()
        for a in assets:
            x = a["asset_id"]
            self.asset_owner[x] = aid
            self.unheld_at_gate[x] = (self.unheld_at_gate.get(x, frozenset())
                                      | (closed - self._closed_at_release.get(x, frozenset())))
        return []

    def _on_subscribe(self, msg):
        aid = self._registered(msg)
        zones = msg["payload"]["zones"]
        unknown = [z for z in zones if z not in self.subs]
        if unknown:
            raise Reject("bad_message", "unknown zones", field="zones")
        for z in zones:
            if aid not in self.subs[z]:
                self.subs[z].append(aid)
        return []

    def _on_post_need(self, msg):
        self._only(msg, self.operator_id, "post_need")
        p = msg["payload"]
        if p["zone"] not in self.subs:
            raise Reject("bad_message", "unknown zone", field="zone")
        if p["mode"] != "scheduled":
            raise Reject("bad_message", "only scheduled mode exists until M5", field="mode")
        if not p["kw_needed"] > 0:
            raise Reject("bad_message", "kw_needed must be positive", field="kw_needed")
        steps = self._steps(p["window"])
        if not msg["t_utc"] < p["gate_utc"] <= p["window"]["start_utc"]:
            raise Reject("bad_message", "need t_utc < gate_utc <= window start", field="gate_utc")
        if "priority" in p and not isinstance(p["priority"], int):
            raise Reject("bad_message", "priority must be an integer", field="priority")
        self._seq += 1
        need = {**p, "need_id": msg["msg_id"], "status": "open", "_steps": set(steps), "_step_list": steps,
                "_seq": self._seq}
        self.needs[msg["msg_id"]] = need
        subs = self.subs[p["zone"]]
        if not subs:
            return []
        out = [self._msg("post_need", list(subs), msg["t_utc"], {**p, "need_id": msg["msg_id"]})]
        if self.publish_room:
            out += self._zone_room(p["zone"], msg["t_utc"], force=False)
        return out

    def _on_offer(self, msg):
        aid = self._registered(msg)
        p = msg["payload"]
        need = self.needs.get(p["need_id"])
        if need is None:
            raise Reject("bad_message", "unknown need", need_id=p["need_id"])
        if p["zone"] != need["zone"] or p["window"] != need["window"] or p["mode"] != need["mode"]:
            raise Reject("bad_message", "zone, window and mode must match the need", need_id=need["need_id"])
        if p["standing"]:
            raise Reject("bad_message", "standing offers arrive with urgent mode in M5", field="standing")
        self._gate_open(need, msg)
        kw, floor = p["kw"], p["floor_usd_mwh"]
        if not (isinstance(kw, (int, float)) and kw > 0 and isinstance(floor, (int, float))):
            raise Reject("bad_message", "kw must be positive and floor_usd_mwh numeric")
        self._check_mandate(aid, need["_step_list"], kw, floor)
        self._check_daily(aid, need["_step_list"], kw)
        prep = self._parse_prep(p.get("prep"), need)
        self._seq += 1
        self.offers[msg["msg_id"]] = {"offer_id": msg["msg_id"], "agent_id": aid, "need_id": need["need_id"],
                                      "kw": float(kw), "floor_usd_mwh": float(floor), "seq": self._seq,
                                      "live": True, "_steps": need["_steps"], "_prep": prep}
        return []

    def _on_revise(self, msg):
        p = msg["payload"]
        offer = self.offers.get(p["offer_id"])
        if offer is None:
            raise Reject("bad_message", "unknown offer", offer_id=p["offer_id"])
        if offer["agent_id"] != msg["sender"]:
            raise Reject("unauthorized_sender", "only the offer's owner may revise it", offer_id=offer["offer_id"])
        need = self.needs[offer["need_id"]]
        between = (self.sequential and need["status"] == "open" and msg["t_utc"] >= need["gate_utc"])
        if between:      # D42: only an agent with a new award since this gate may revise, and only downward in kW
            if offer["agent_id"] not in self.gate_winners.get(need["gate_utc"], set()):
                raise Reject("after_gate_closure", "between clearings only agents with a new award since the gate "
                                                   "may revise", offer_id=offer["offer_id"])
            if not p.get("withdraw") and p.get("kw", offer["kw"]) > offer["kw"] + 1e-9:
                raise Reject("after_gate_closure", "between clearings kW may only go down", offer_id=offer["offer_id"])
        else:
            self._gate_open(need, msg)
        if not offer["live"]:
            raise Reject("bad_message", "offer was already withdrawn", offer_id=offer["offer_id"])
        if p.get("withdraw"):
            offer["live"] = False
            return []
        kw, floor = p.get("kw", offer["kw"]), p.get("floor_usd_mwh", offer["floor_usd_mwh"])
        if not (isinstance(kw, (int, float)) and kw > 0 and isinstance(floor, (int, float))):
            raise Reject("bad_message", "kw must be positive and floor_usd_mwh numeric")
        self._check_mandate(offer["agent_id"], need["_step_list"], kw, floor, exclude=offer["offer_id"])
        self._check_daily(offer["agent_id"], need["_step_list"], kw, exclude=offer["offer_id"])
        if "prep" in p:
            offer["_prep"] = self._parse_prep(p["prep"], need)
        offer["kw"], offer["floor_usd_mwh"] = float(kw), float(floor)
        return []

    def _on_telemetry(self, msg):
        self._only(msg, self.metering_id, "telemetry")
        p = msg["payload"]
        if p["asset_id"] not in self.asset_owner:
            raise Reject("bad_message", "unknown asset", field="asset_id")
        self.meter.setdefault(p["asset_id"], {})[msg["t_utc"]] = float(p["meter_kw"])
        if self.delivery_record:
            self.soc.setdefault(p["asset_id"], {})[msg["t_utc"]] = float(p["soc_kwh"])
        if "net_kw" in p:
            self.net_meter.setdefault(p["asset_id"], {})[msg["t_utc"]] = float(p["net_kw"])
        return []

    def _on_asset_limits(self, msg):
        """Demonstrated limits for an asset, from metered history. Only the metering service may send these."""
        self._only(msg, self.metering_id, "asset_limits")
        p = msg["payload"]
        vals = [p["usable_kwh"], p["power_kw"], p["pv_kwh_per_day"]]
        if not all(isinstance(v, (int, float)) and v >= 0 for v in vals):
            raise Reject("bad_message", "limits must be numbers >= 0", asset_id=p["asset_id"])
        owner, agent = p.get("owner"), p.get("agent_id")
        if self.attested_owners:
            if owner is None or agent is None:
                raise Reject("bad_message", "asset_limits must attest the owner and the operating agent",
                             asset_id=p["asset_id"])
            prev = self.attested.get(p["asset_id"])
            if prev is not None and prev["owner"] != owner:
                raise Reject("bad_message", "an asset's attested owner cannot change", asset_id=p["asset_id"])
            self.attested[p["asset_id"]] = {"owner": owner, "agent_id": agent}
        self.limits[p["asset_id"]] = {"usable_kwh": float(p["usable_kwh"]), "power_kw": float(p["power_kw"]),
                                      "pv_kwh_per_day": float(p["pv_kwh_per_day"])}
        return []

    def _on_reference_net(self, msg):
        """D53: the refund reference for an asset (recorded site net per step), from the metering service only."""
        self._only(msg, self.metering_id, "reference_net")
        p = msg["payload"]
        steps = self._steps(p["window"])
        kw = p["net_kw"]
        if not (isinstance(kw, list) and len(kw) == len(steps)
                and all(isinstance(v, (int, float)) and v == v and abs(v) != float("inf") for v in kw)):
            raise Reject("bad_message", "net_kw must be finite numbers, one per step", steps=len(steps))
        self.reference[p["asset_id"]] = dict(zip(steps, map(float, kw)))
        return []

    def _on_selfish_reference(self, msg):
        """D56 rule 1p: the refund reference for an asset on the steps one binding call settles, from the metering
        service only, before that call's deadline, once."""
        self._only(msg, self.metering_id, "selfish_reference")
        if self.settlement_reference != "selfish_1p":
            raise Reject("bad_message", "selfish_reference is used only with settlement_reference selfish_1p")
        p = msg["payload"]
        if p["asset_id"] not in self.asset_owner:
            raise Reject("bad_message", "unknown asset", field="asset_id")
        call = self.calls.get(p["call_id"])
        if call is None or not (call["binding"] or self.silent_reference):   # D79: any priced call under the flag
            raise Reject("bad_message", "not a binding call", call_id=p["call_id"])
        if call["aggregated"] or msg["t_utc"] >= call["deadline_utc"]:
            raise Reject("after_gate_closure", "the call is closed", call_id=p["call_id"], deadline_utc=call["deadline_utc"])
        if p["asset_id"] in self.call_reference.get(p["call_id"], {}):
            raise Reject("bad_message", "a reference is never revised", asset_id=p["asset_id"], call_id=p["call_id"])
        if p["window"] != call["window"]:
            raise Reject("bad_message", "window must be the call's window", call_id=p["call_id"])
        kw = p["net_kw"]
        if not (isinstance(kw, list) and len(kw) == len(call["_steps"])
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) and v == v
                        and abs(v) != float("inf") for v in kw)):
            raise Reject("bad_message", "net_kw must be finite numbers, one per step", steps=len(call["_steps"]))
        reg = p.get("net_kw_registered")
        if self.declared_reference:      # D81: the registered-limits reference, for the void rule at the closing tick
            if not (isinstance(reg, list) and len(reg) == len(call["_steps"])
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool) and v == v
                            and abs(v) != float("inf") for v in reg)):
                raise Reject("bad_message", "net_kw_registered must be finite numbers, one per step",
                             steps=len(call["_steps"]))
            if p.get("declarations_void"):   # D82 backstop: 1p had no plan within the declarations; registered limits
                if [float(v) for v in kw] != [float(v) for v in reg]:
                    raise Reject("bad_message", "declarations_void needs net_kw equal to net_kw_registered",
                                 call_id=p["call_id"])
                self.infeasible_calls.setdefault(p["asset_id"], set()).add(p["call_id"])
            self.call_reference_reg.setdefault(p["call_id"], {})[p["asset_id"]] = dict(
                zip(call["_steps"], map(float, reg)))
            # D87: the days whose declarations this reference was computed with (the metering service plans as of the
            # message's time, from the exchange's accepted declarations, with nothing delivered in between)
            self.call_ref_days.setdefault(p["call_id"], {})[p["asset_id"]] = self.days_in_force(p["asset_id"],
                                                                                               msg["t_utc"])
        elif reg is not None:
            raise Reject("bad_message", "net_kw_registered is used only with declared_reference")
        elif "declarations_void" in p:
            raise Reject("bad_message", "declarations_void is used only with declared_reference")
        self.call_reference.setdefault(p["call_id"], {})[p["asset_id"]] = dict(zip(call["_steps"], map(float, kw)))
        return []

    def _in_force(self, asset: str, as_of: str) -> tuple[dict, set]:
        """The asset's accepted declarations (local date -> latest filing) and forced-outage steps in force at as_of:
        filed at or before it. One definition for declared_limits (what 1p plans with) and the D87 record of which days
        each reference was computed with, so the two can't drift apart."""
        decl = {d: x for d, x in self.declared.get(asset, {}).items() if x["t_utc"] <= as_of}
        down = set()
        for o in self.outages.get(asset, []):
            if o["t_utc"] <= as_of:
                down |= o["steps"]
        return decl, down

    def days_in_force(self, asset: str, as_of: str) -> frozenset:
        """D87: the local dates whose declaration or forced outage was in force at as_of, so entered a 1p reference
        computed then (1p plans the whole run, so a declaration for any day enters every later reference)."""
        decl, down = self._in_force(asset, as_of)
        return frozenset(decl) | frozenset(self._local_date(t) for t in down)

    def declared_limits(self, asset: str, steps: list[str], as_of: str) -> tuple[list[float], list[float]]:
        """D80: the per-step power cap (kW) and SOC floor (kWh) an asset's accepted declarations set, NaN where none;
        only declarations accepted by as_of. A planned outage day or a forced outage step caps power at 0 (and has no
        floor). For the metering service's 1p planner; flag off: all NaN."""
        nan = float("nan")
        if not self.declared_reference:
            return [nan] * len(steps), [nan] * len(steps)
        decl, down = self._in_force(asset, as_of)
        pmax, floor = [], []
        for t in steps:
            x = decl.get(self._local_date(t))
            if t in down or (x is not None and x["planned_outage"]):
                pmax.append(0.0)
                floor.append(nan)
            elif x is not None:
                pmax.append(x["power_kw"])
                floor.append(x["reserve_kwh"])
            else:
                pmax.append(nan)
                floor.append(nan)
        return pmax, floor

    def _breach_steps(self, asset: str) -> dict[str, set[str]]:
        """D64/D81 breach detectors, per asset (one definition for D65's counts and the void rule): battery-meter
        activity during a declared outage, SOC below the declared reserve, and (under declared_reference) battery
        output above declared power. The site-net-meter check is not built: it needs the battery-free site net, which
        the exchange doesn't have."""
        decl = self.declared.get(asset, {})
        outs = self.outages.get(asset, [])
        down = set().union(*(o["steps"] for o in outs)) if outs else set()
        res = {"outage_activity": set(), "below_reserve": set(), "above_power": set()}
        for t, kw in self.meter.get(asset, {}).items():
            x = decl.get(self._local_date(t))
            if (t in down or (x is not None and x["planned_outage"])) and abs(kw) > REC.ACTIVITY_TOL_KW:
                res["outage_activity"].add(t)
            elif self.declared_reference and x is not None and abs(kw) > x["power_kw"] + REC.ACTIVITY_TOL_KW:
                res["above_power"].add(t)
        for t, soc in self.soc.get(asset, {}).items():
            x = decl.get(self._local_date(t))
            if x is not None and soc < x["reserve_kwh"] - ENERGY_TOL_KWH:
                res["below_reserve"].add(t)
        return res

    def _void_declarations(self):
        """D81 at the closing tick, before either settlement rule, widened by D87: for an asset with a breach on local
        days B, every stored reference that was computed with a declaration or forced outage for a day in B (the days
        in force when it was filed, call_ref_days) is replaced on all its steps by its registered-limits reference;
        every other reference has its B-day steps replaced, as before. Only peak settlement reads the result: prices
        already formed are never recomputed, and imbalance bases were fixed at each binding aggregate (and imbalance
        charged) before this runs, so a silent agent's (D78) imbalance base stays the reference as bound."""
        self.void_done = True
        # every asset with a registered-limits reference, including one whose agent has since deregistered (its
        # steps are still settled; review finding, Nov 4)
        for asset in sorted({x for by_asset in self.call_reference_reg.values() for x in by_asset}):
            days = {self._local_date(t) for ts in self._breach_steps(asset).values() for t in ts}
            if not days:
                continue
            self.voided[asset] = sorted(days)
            for cid, by_asset in self.call_reference_reg.items():
                reg = by_asset.get(asset)
                if reg is None:
                    continue
                cur = self.call_reference[cid][asset]
                if self.call_ref_days.get(cid, {}).get(asset, frozenset()) & days:     # D87: the whole reference
                    cur.update(reg)
                    self.voided_calls.setdefault(asset, []).append(cid)
                    continue
                for t, v in reg.items():
                    if self._local_date(t) in days:
                        cur[t] = v

    @staticmethod
    def _finite(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float("inf")

    def _on_modeled_response(self, msg):
        """D62: one asset's modeled response P* and acting-alone path Q* for a binding call's stage, from the call's
        deadline to the slice end. Metering service only, after the call's final price, before its deadline, once."""
        self._only(msg, self.metering_id, "modeled_response")
        if not self.guarantee:
            raise Reject("bad_message", "modeled_response is used only with the D62 guarantee")
        p = msg["payload"]
        if p["asset_id"] not in self.asset_owner:
            raise Reject("bad_message", "unknown asset", field="asset_id")
        call = self.calls.get(p["call_id"])
        if call is None or not call["binding"]:
            raise Reject("bad_message", "not a binding call", call_id=p["call_id"])
        if p["call_id"] not in self.final_calls:
            raise Reject("bad_message", "the call has no final price yet", call_id=p["call_id"])
        if call["aggregated"] or msg["t_utc"] >= call["deadline_utc"]:
            raise Reject("after_gate_closure", "the call is closed", call_id=p["call_id"], deadline_utc=call["deadline_utc"])
        if p["asset_id"] in self.modeled.get(p["call_id"], {}):
            raise Reject("bad_message", "a modeled response is never revised", asset_id=p["asset_id"], call_id=p["call_id"])
        if p["window"] != {"start_utc": call["deadline_utc"], "end_utc": call["window"]["end_utc"]}:
            raise Reject("bad_message", "window must run from the call's deadline to its window's end", call_id=p["call_id"])
        n = len(self._steps(p["window"]))
        for k in ("p_net_kw", "p_discharge_kw", "q_net_kw", "q_discharge_kw"):
            if not (isinstance(p[k], list) and len(p[k]) == n and all(self._finite(v) for v in p[k])):
                raise Reject("bad_message", "paths must be finite numbers, one per step", field=k, steps=n)
        if not (self._finite(p["p_soc_end_kwh"]) and self._finite(p["q_soc_end_kwh"])):
            raise Reject("bad_message", "end SOC must be finite numbers")
        self.modeled.setdefault(p["call_id"], {})[p["asset_id"]] = p
        return []

    def _on_rt_price(self, msg):
        """D62: the real-time prices the guarantee is valued at, relayed by the metering service, once."""
        self._only(msg, self.metering_id, "rt_price")
        if not self.guarantee:
            raise Reject("bad_message", "rt_price is used only with the D62 guarantee")
        if self.rt is not None:
            raise Reject("bad_message", "rt_price is sent once")
        p = msg["payload"]
        n = len(self._steps(p["window"]))
        if not (isinstance(p["rt_usd_mwh"], list) and len(p["rt_usd_mwh"]) == n
                and all(self._finite(v) for v in p["rt_usd_mwh"]) and self._finite(p["end_energy_usd_kwh"])):
            raise Reject("bad_message", "rt_usd_mwh must be finite numbers, one per step, and end_energy_usd_kwh finite",
                         steps=n)
        self.rt = p
        return []

    def _guarantee_accrual(self, now):
        """D62 at the closing tick: per agent, the shortfall of P* against Q* per span (summed over its assets) and
        D65's response measure on priced steps against 1p's reference. Fails closed on a missing input."""
        self.guarantee_done = True
        if self.rt is None:
            raise RuntimeError("D62: no rt_price before the closing tick")
        steps = self._steps(self.rt["window"])
        pos = {t: i for i, t in enumerate(steps)}
        T = len(steps)
        settled = {t: (p, cid) for t, (p, cid) in self.peak_settled.items() if p > 1e-9}
        calls = sorted((c for c in self.calls.values() if c["binding"] and c["call_id"] in self.modeled),
                       key=lambda c: c["deadline_utc"])
        missing = {cid for _, cid in settled.values()} - {c["call_id"] for c in calls}
        if missing or not calls:
            raise RuntimeError(f"D62: no modeled_response for binding calls {sorted(missing)}")
        sp = G.spans([pos[c["deadline_utc"]] for c in calls], T)
        K = self.step_h / 1000.0
        rt = [float(v) for v in self.rt["rt_usd_mwh"]]
        price = [0.0] * T
        for t, (p, _) in settled.items():
            price[pos[t]] = p
        window = dict(self.rt["window"])
        out = []
        for aid, a in self.agents.items():
            by_span, shift, got = [0.0] * len(sp), 0.0, 0.0
            for asset in a["assets"]:
                P_net, P_dis, Q_net, Q_dis = ([0.0] * T for _ in range(4))
                for c, (a0, a1) in zip(calls, sp):
                    m = self.modeled[c["call_id"]].get(asset)
                    if m is None:
                        raise RuntimeError(f"D62: no modeled_response for {asset} on {c['call_id']}")
                    for t in range(a0, a1):
                        P_net[t], P_dis[t] = m["p_net_kw"][t - a0], m["p_discharge_kw"][t - a0]
                        Q_net[t], Q_dis[t] = m["q_net_kw"][t - a0], m["q_discharge_kw"][t - a0]
                last = self.modeled[calls[-1]["call_id"]][asset]
                q_ref, meter = [0.0] * T, [0.0] * T
                for t, (_, cid) in settled.items():
                    r = self.call_reference.get(cid, {}).get(asset, {}).get(t)
                    if r is None:
                        raise RuntimeError(f"D62: no selfish_reference for {asset} on {cid} at {t}")
                    q_ref[pos[t]] = r
                for t, v in self.net_meter.get(asset, {}).items():
                    if t in pos:
                        meter[pos[t]] = v
                s = G.span_shortfall(rt, price, q_ref, P_net, P_dis, Q_net, Q_dis, last["p_soc_end_kwh"],
                                     last["q_soc_end_kwh"], self.guarantee_wear, self.rt["end_energy_usd_kwh"], sp, K)
                by_span = [x + y for x, y in zip(by_span, s)]
                d_kwh, g_kwh = G.response_kwh(price, q_ref, P_net, meter, self.step_h)
                shift, got = shift + d_kwh, got + g_kwh
            out.append(self._msg("guarantee_accrual", aid, now, {
                "window": window, "calls": [c["call_id"] for c in calls], "shortfall_usd_by_span": by_span,
                "shortfall_usd": sum(by_span), "modeled_shift_kwh": shift, "delivered_kwh": got}))
        return out

    def _on_zone_target(self, msg):
        """The operator's limit on the zone's total net load per step (operator to exchange only)."""
        self._only(msg, self.operator_id, "zone_target")
        p = msg["payload"]
        if p["zone"] not in self.subs:
            raise Reject("bad_message", "unknown zone", field="zone")
        steps = self._steps(p["window"])
        tk = p["target_kw"]
        if not (isinstance(tk, list) and len(tk) == len(steps)
                and all(isinstance(v, (int, float)) and v == v and abs(v) != float("inf") for v in tk)):
            raise Reject("bad_message", "target_kw must be finite numbers, one per step", steps=len(steps))
        self.targets[p["zone"]].update(zip(steps, map(float, tk)))
        return []

    def _on_call_nominations(self, msg):
        self._only(msg, self.operator_id, "call_nominations")
        p = msg["payload"]
        if p["zone"] not in self.subs:
            raise Reject("bad_message", "unknown zone", field="zone")
        steps = self._steps(p["window"])
        if not msg["t_utc"] < p["deadline_utc"] <= p["window"]["start_utc"]:
            raise Reject("bad_message", "call needs t_utc < deadline_utc <= window start", field="deadline_utc")
        self.calls[msg["msg_id"]] = {**p, "call_id": msg["msg_id"], "_steps": steps, "aggregated": False,
                                     "imbalance_done": False}
        subs = self.subs[p["zone"]]
        rules = {"imbalance_usd_mwh": self.scarcity_usd_mwh, "band_pct": self.band_pct, "band_min_kw": self.band_min_kw}
        payload = {**p, "call_id": msg["msg_id"], **rules}
        return [self._msg("call_nominations", list(subs), msg["t_utc"], payload)] if subs else []

    def _on_nominate(self, msg):
        aid = self._registered(msg)
        p = msg["payload"]
        call = self.calls.get(p["call_id"])
        if call is None:
            raise Reject("bad_message", "unknown call", call_id=p["call_id"])
        if p["zone"] != call["zone"] or p["window"] != call["window"]:
            raise Reject("bad_message", "zone and window must match the call", call_id=p["call_id"])
        if call["zone"] not in self.agents[aid]["zones"].values():
            raise Reject("bad_message", "sender has no asset in the call's zone", call_id=p["call_id"])
        if call["aggregated"] or msg["t_utc"] >= call["deadline_utc"]:
            raise Reject("after_gate_closure", "nominations for the call are closed", call_id=call["call_id"],
                         deadline_utc=call["deadline_utc"])
        kw = p["kw"]
        if not (isinstance(kw, list) and len(kw) == len(call["_steps"])
                and all(isinstance(v, (int, float)) and v == v and abs(v) != float("inf") for v in kw)):
            raise Reject("bad_message", "kw must be finite numbers, one per step", steps=len(call["_steps"]))
        self._add_nom(aid, call["call_id"], dict(zip(call["_steps"], map(float, kw))))
        pend = self.pending_prep.get(aid)
        if pend:            # the new nomination already includes the agent's awarded prep for these steps
            for t in call["_steps"]:
                pend.pop(t, None)
        return []

    def _add_nom(self, aid, call_id, values: dict[str, float]):
        """Store a nomination (or a clipped binding schedule) and fold it into the agent's latest-values cache."""
        self.noms.setdefault(aid, []).append({"call_id": call_id, "values": values})
        if aid not in self._latest_cache:
            self._latest_cache[aid] = {}
            self._latest_view[aid] = MappingProxyType(self._latest_cache[aid])
        self._latest_cache[aid].update(values)

    def _latest(self, aid) -> MappingProxyType:
        """An agent's most recent nominated kW for every step it has nominated: a read-only view of the cache, so
        no caller can change it (copy with dict() to keep a snapshot, as _award_cap does)."""
        return self._latest_view.get(aid, _EMPTY_VIEW)

    def _aggregate(self, call, now):
        """Zone total of the latest nominations. Only the total goes to the operator."""
        total = [0.0] * len(call["_steps"])
        n = 0
        for aid, a in self.agents.items():
            if call["zone"] not in a["zones"].values() or aid in self.inactive:
                continue
            latest = self._latest(aid)
            vals = [latest.get(t) for t in call["_steps"]]
            if any(v is not None for v in vals):
                n += 1
            # D78: a step with no nomination counts at the agent's 1p reference for this call (flag off: None)
            refs = [self._ref_net(aid, call["call_id"], t) if v is None else None for t, v in zip(call["_steps"], vals)]
            total = [s + (v if v is not None else (r or 0.0)) for s, v, r in zip(total, vals, refs)]
            if call["binding"]:
                force = self.baseline.setdefault(aid, {})
                for t, v, r in zip(call["_steps"], vals, refs):
                    if v is not None:
                        force[t] = (v, call["call_id"])
                        self.ref_base.get(aid, set()).discard(t)
                    elif r is not None and self._holds(aid, t):     # D78: the silent agent's imbalance base
                        force[t] = (r, call["call_id"])
                        self.ref_base.setdefault(aid, set()).add(t)     # not a promise: D65 leaves it out
                if self.settlement_rule == "max_first_call":
                    self.call_nom.setdefault(call["call_id"], {})[aid] = {
                        t: v for t, v in zip(call["_steps"], vals) if v is not None}
        call["aggregated"] = True
        if self.price_mode:
            self._close_price_gate(call, now)
        payload = {"call_id": call["call_id"], "zone": call["zone"], "window": call["window"], "kw": total,
                   "n_agents": n, "binding": call["binding"]}
        return self._msg("nomination_aggregate", self.operator_id, now, payload)

    def _settles(self, t: str) -> bool:
        """Warm-up: money counts only from settle_from_utc on (all steps when it is unset)."""
        return _from_ok(self, t)

    def _imbalance(self, call, now, rebound=None):
        """Charge each step against the binding nomination in force at delivery: steps a later binding call took
        over are settled under that call instead, so every step is charged exactly once. With the rebound cap on,
        a step over both is charged once, for the larger excess: here if imbalance is larger, else as rebound."""
        out = []
        rebound = rebound or {}
        for aid, force in self.baseline.items():
            base = {t: v for t, (v, cid) in force.items()
                    if cid == call["call_id"] and self._holds(aid, t) and _from_ok(self, t)}
            if not base:
                continue        # never nominated, every step taken over by a later binding call, or outside tenure
            metered = self._metered(aid)
            band = band_kw(self.agents[aid]["mandate"]["max_kw"], self.band_pct, self.band_min_kw)
            reb = rebound.get(aid, {})
            if reb:
                for t in list(base):
                    dev = max(abs(metered.get(t, 0.0) - base[t]) - band, 0.0)
                    if reb.get(t, 0.0) > dev:
                        del base[t]            # charged as rebound instead
                    else:
                        reb.pop(t, None)       # charged here; not again as rebound
            res = imbalance(base, metered, call["_steps"], self.step_h, band, self.scarcity_usd_mwh)
            out.append(self._msg("imbalance", aid, now, {"call_id": call["call_id"], "window": call["window"],
                                                         "band_kw": band, **res}))
        call["imbalance_done"] = True
        return out

    def _on_tick(self, msg):
        self._only(msg, self.clock_id, "tick")
        now = msg["t_utc"]
        self.last_tick = max(self.last_tick, now)
        if self.dereg:
            self._apply_deregistrations(now)
        out = []
        substep = msg["payload"].get("substep", 0)
        if not isinstance(substep, int) or substep < 0:
            raise Reject("bad_message", "substep must be an integer >= 0", field="substep")
        if substep and not self.sequential:
            raise Reject("bad_message", "substeps exist only with sequential clearing", field="substep")
        price_round = msg["payload"].get("price_round", 0)
        if not isinstance(price_round, int) or price_round < 0:
            raise Reject("bad_message", "price_round must be an integer >= 0", field="price_round")
        if price_round:
            if not self.price_mode:
                raise Reject("bad_message", "price rounds exist only in price mode", field="price_round")
            return self._price_round(now, price_round)
        if substep == 0:
            for call in self.calls.values():
                if not call["aggregated"] and call["deadline_utc"] <= now:
                    if call["binding"] and self.cap_rebase:
                        out += self._rebase_caps(call, now)
                    out.append(self._aggregate(call, now))
        due = [n for n in self.needs.values() if n["status"] == "open" and n["gate_utc"] <= now]
        if self.sequential:
            due.sort(key=lambda n: (n["priority"] if isinstance(n.get("priority"), int) else float("inf"),
                                    n["window"]["start_utc"], n["_seq"]))
            if due:
                out += self._clear(due[0], now)
                still = [n for n in due[1:]]
                if not still:
                    out += self._publish_gate_prices(now)
        else:
            for need in due:
                out += self._clear(need, now)
        if substep:
            return out
        for award in self.awards.values():
            if not award["settled"] and award["window"]["end_utc"] <= now:
                out.append(self._settle(award, now))
        final = bool(msg["payload"].get("final"))
        if self.scarcity_usd_mwh is not None:
            reb = self._rebound_excess() if (self.no_rebound and final and not self.rebound_done) else {}
            for call in self.calls.values():
                if (call["binding"] and call["aggregated"] and not call["imbalance_done"]
                        and call["window"]["end_utc"] <= now and (final or not self.no_rebound)):
                    out += self._imbalance(call, now, reb)
            if reb or (self.no_rebound and final and not self.rebound_done):
                out += self._rebound(now, reb)
        if self.declared_reference and final and not self.void_done:
            self._void_declarations()                    # D81: before either settlement rule reads the references
        if self.price_mode and final and not self.peak_done:
            out += self._peak_settle_max(now) if self.settlement_rule == "max_first_call" else self._peak_settle(now)
        if self.delivery_record and final and not self.record_done:
            out += self._delivery_record(now)
        if self.guarantee and final and not self.guarantee_done:
            out += self._guarantee_accrual(now)
        return out

    # ---- enrollment, availability and the delivery record (D64, D65) ----
    def _da_deadline(self, local_date: str) -> str:
        """When day-ahead prices for a local date publish: da_publish_hour_local on the day before, in UTC."""
        d = datetime.fromisoformat(local_date).replace(tzinfo=self.tz) - timedelta(days=1)
        return fmt_t(d.replace(hour=self.da_hour).astimezone(timezone.utc))

    def _record_only(self):
        if not self.delivery_record:
            raise Reject("bad_message", "enrollment and availability messages are used only with delivery_record")

    def _owned_asset(self, aid, asset):
        if self.asset_owner.get(asset) != aid:
            raise Reject("unauthorized_sender", "the asset is not registered to the sender", asset_id=asset)

    def _holds(self, aid, t) -> bool:
        """D64 tenure: an agent answers for its assets' steps from its register to its deregistration's start. An
        asset that changes hands mid-run is billed to each holder for its own steps only, so never twice. Only with
        delivery_record (the only way an asset can change hands); with it off every step is the agent's, as before
        D64, so honest runs can't change."""
        if not self.delivery_record:
            return True
        if t < self.tenure_from.get(aid, ""):
            return False
        return aid not in self.inactive or t < self._dereg_start(aid)

    def _metered(self, aid) -> dict[str, float]:
        """Metered site net (kW) of an agent's assets per step, over its tenure only."""
        metered: dict[str, float] = {}
        for asset in self.agents[aid]["assets"]:
            for t, v in self.net_meter.get(asset, {}).items():
                if self._holds(aid, t):
                    metered[t] = metered.get(t, 0.0) + v
        return metered

    def _now(self, msg) -> str:
        """D64 deadlines use the later of the message's t_utc and the clock's last tick: a sender can't backdate a
        filing past a deadline the clock has already passed (forward-dating only makes it later)."""
        return max(msg["t_utc"], self.last_tick)

    def _on_deregister(self, msg):
        """D64: registration lasts until the agent deregisters, from a later local day, filed before that day's
        day-ahead prices publish. No daily opt-out."""
        self._record_only()
        aid = self._registered(msg)
        p = msg["payload"]
        if p["agent_id"] != aid:
            raise Reject("unauthorized_sender", "agent_id must match the sender", field="agent_id")
        d = p["effective_date"]
        if not d > self._local_date(self._now(msg)):
            raise Reject("bad_message", "deregistration takes effect from a later day", field="effective_date")
        if self._now(msg) >= self._da_deadline(d):
            raise Reject("after_gate_closure", "deregistration must be filed before that day's day-ahead prices "
                                               "publish", effective_date=d, deadline_utc=self._da_deadline(d))
        self.dereg[aid] = d
        return []

    def _dereg_start(self, aid) -> str:
        return fmt_t(datetime.fromisoformat(self.dereg[aid]).replace(tzinfo=self.tz).astimezone(timezone.utc))

    def _closed_binding_calls(self) -> frozenset[str]:
        """Binding calls whose gate has closed by the exchange's clock (aggregated, or deadline at or before the last
        tick), never by a time a sender writes."""
        return frozenset(c["call_id"] for c in self.calls.values()
                         if c["binding"] and (c.get("aggregated") or c["deadline_utc"] <= self.last_tick))

    def _apply_deregistrations(self, now):
        """At the first tick on or after the effective day's local midnight: the agent stops being a participant
        (its messages get not_registered, its subscriptions end) and its assets are free to register again; their
        record stays with the asset and legal entity."""
        for aid in list(self.dereg):
            if now >= self._dereg_start(aid) and aid not in self.inactive:
                self.inactive.add(aid)
                for subs in self.subs.values():
                    if aid in subs:
                        subs.remove(aid)
                for asset in self.agents[aid]["assets"]:
                    self.asset_owner.pop(asset, None)
                    self.released[asset] = self._dereg_start(aid)
                    self._closed_at_release[asset] = self._closed_binding_calls()      # D56 1p: see unheld_at_gate

    def _on_declare_availability(self, msg):
        """D64: per asset and local day, before that day's day-ahead prices publish (the latest declaration wins).
        Power available within registered power; reserve within demonstrated usable energy when known."""
        self._record_only()
        aid = self._registered(msg)
        p = msg["payload"]
        self._owned_asset(aid, p["asset_id"])
        d = p["date"]
        if self._now(msg) >= self._da_deadline(d):
            raise Reject("after_gate_closure", "availability must be declared before the day's day-ahead prices "
                                               "publish", date=d, deadline_utc=self._da_deadline(d))
        mandate = self.agents[aid]["mandate"]
        if not 0.0 <= p["power_kw"] <= mandate["max_kw"] + 1e-9:
            raise Reject("outside_mandate", "declared power must be between 0 and registered power",
                         max_kw=mandate["max_kw"])
        usable = self.limits.get(p["asset_id"], {}).get("usable_kwh")
        if p["reserve_kwh"] < 0 or (usable is not None and p["reserve_kwh"] > usable + ENERGY_TOL_KWH):
            raise Reject("over_demonstrated_limit", "reserve must be between 0 and demonstrated usable energy",
                         usable_kwh=usable)
        if p["planned_outage"] and self.max_outage_days is not None:
            sea = REC.season(d)
            used = sum(1 for dd, x in self.declared.get(p["asset_id"], {}).items()
                       if x["planned_outage"] and REC.season(dd) == sea and dd != d)
            if used + 1 > self.max_outage_days:
                raise Reject("outside_mandate", "planned outage days per season are capped", season=sea,
                             cap=self.max_outage_days)
        mine = self.declared.setdefault(p["asset_id"], {})
        prev = mine.get(d)
        mine[d] = {"power_kw": float(p["power_kw"]), "reserve_kwh": float(p["reserve_kwh"]),
                   "planned_outage": bool(p["planned_outage"]), "t_utc": self._now(msg)}
        if self.declared_reference:     # D82: filing order and the message, for an outage that later voids it
            self._decl_seq += 1
            mine[d].update(seq=self._decl_seq, msg_id=msg["msg_id"])
        if not self._declaration_feasible(p["asset_id"], self._now(msg)):
            if prev is None:
                del mine[d]
                if not mine:
                    del self.declared[p["asset_id"]]
            else:
                mine[d] = prev
            raise Reject("infeasible_declaration", "with this asset's accepted declarations 1p would have no "
                                                   "feasible plan", asset_id=p["asset_id"], date=d)
        return []

    def _declaration_feasible(self, asset: str, now: str) -> bool:
        """D82: under declared_reference, whether 1p still has a feasible plan with every accepted declaration of the
        asset plus the one being filed (already stored, so the check reads it). Flag off: always True, never asked."""
        if not self.declared_reference:
            return True
        return self.declaration_check is not None and bool(self.declaration_check(asset, now))

    def _on_declare_outage(self, msg):
        """D64: a forced outage, declared as soon as it is known; late if after the deadline for its first day."""
        self._record_only()
        aid = self._registered(msg)
        p = msg["payload"]
        self._owned_asset(aid, p["asset_id"])
        steps = self._steps(p["window"])
        late = self._now(msg) >= self._da_deadline(self._local_date(steps[0]))
        outs = self.outages.setdefault(p["asset_id"], [])
        outs.append({"steps": set(steps), "t_utc": self._now(msg), "late": late})
        if not self._declaration_feasible(p["asset_id"], self._now(msg)):
            return self._void_for_outage(aid, p["asset_id"], self._now(msg))      # D82: an outage is never rejected
        return []

    def _void_for_outage(self, aid: str, asset: str, now: str) -> list[dict]:
        """D82 amendment: the outage stands; the asset's voluntary declarations are rebuilt in a fixed order (every
        forced outage is already in, then the voluntary declarations in filing order, each kept only if 1p stays
        feasible). Each one dropped is void from now on, and the agent gets infeasible_declaration for it. A voluntary
        declaration filed into an infeasible set is rejected at filing, so either filing order gives this set."""
        mine = self.declared.pop(asset, {})
        kept = self.declared.setdefault(asset, {})
        dropped = []
        for d, x in sorted(mine.items(), key=lambda kv: kv[1]["seq"]):
            kept[d] = x
            if not self._declaration_feasible(asset, now):
                del kept[d]
                dropped.append((d, x))
        if not kept:
            del self.declared[asset]
        out = []
        for d, x in dropped:
            self.declarations_voided.setdefault(asset, []).append({"date": d, "msg_id": x["msg_id"], "t_utc": now})
            out.append(self._msg("reject", aid, now, {
                "ref_msg_id": x["msg_id"], "code": "infeasible_declaration",
                "detail": "a forced outage left 1p with no feasible plan; this declaration is void from now on",
                "refs": {"asset_id": asset, "date": d}}))
        return out

    def _delivery_record(self, now):
        """D65 at the closing tick: add this run to each (asset, legal entity) record and send each agent its rows."""
        self.record_done = True
        out = []
        for aid, a in self.agents.items():
            key = ("+".join(sorted(a["assets"])), a["legal_entity"])
            ref_base = self.ref_base.get(aid, set())     # D78: a reference base is not a nomination it kept
            force = {t: v for t, (v, _) in self.baseline.get(aid, {}).items()
                     if self._holds(aid, t) and t not in ref_base}
            metered = self._metered(aid)
            band = band_kw(a["mandate"]["max_kw"], self.band_pct, self.band_min_kw)
            nd = REC.nomination_delivery(force, metered, band, self.step_h)
            zones = set(a["zones"].values())
            left = self._dereg_start(aid) if aid in self.inactive else None
            missed = sum(1 for c in self.calls.values()
                         if c["binding"] and c["aggregated"] and c["zone"] in zones
                         and (left is None or c["deadline_utc"] < left)
                         and c["deadline_utc"] >= self.tenure_from.get(aid, "")
                         and not any(n["call_id"] == c["call_id"] for n in self.noms.get(aid, [])))
            days = power_share = planned = forced = late = activity = low = above = infeasible = 0
            for asset in a["assets"]:
                if self.asset_owner.get(asset, aid) != aid:
                    continue        # re-registered by another agent: its per-asset counts go there, once
                decl = self.declared.get(asset, {})
                days += len(decl)
                power_share += sum(x["power_kw"] / a["mandate"]["max_kw"] if a["mandate"]["max_kw"] > 0 else 0.0
                                   for x in decl.values())
                planned += sum(1 for x in decl.values() if x["planned_outage"])
                outs = self.outages.get(asset, [])
                forced += len(outs)
                late += sum(1 for o in outs if o["late"])
                br = self._breach_steps(asset)
                activity += len(br["outage_activity"])
                low += len(br["below_reserve"])
                above += len(br["above_power"])
                infeasible += len(self.infeasible_calls.get(asset, ()))
            self.ledger.add(key, binding_steps=nd["binding_steps"], steps_within_band=nd["steps_within_band"],
                            weight_kwh=nd["weight_kwh"], weight_kwh_within_band=nd["weight_kwh_within_band"],
                            binding_calls_missed=missed, days_declared=days, declared_power_share_sum=power_share,
                            planned_outage_days=planned, forced_outages=forced, late_outages=late,
                            outage_activity_steps=activity, below_reserve_steps=low,
                            **({"above_power_steps": above, "infeasible_declaration_calls": infeasible}
                               if self.declared_reference else {}))         # D83: in the record under the flag only
            row = {"assets": sorted(a["assets"]), "this_run": {**nd, "binding_calls_missed": missed},
                   "record": self.ledger.view(key)}
            if self.declared_reference:         # D80/D81: the power-breach count and the days whose declaration was void
                row["this_run"]["above_power_steps"] = above
                row["this_run"]["infeasible_declaration_calls"] = infeasible
                row["this_run"]["voided_days"] = sorted({d for x in a["assets"] for d in self.voided.get(x, [])})
                row["this_run"]["voided_calls"] = sorted({c for x in a["assets"] for c in self.voided_calls.get(x, [])},
                                                         key=lambda c: (len(c), c))     # D87
            if self.refusal_notice:              # refusals by reason, per legal entity (this run's and the record's)
                mine = {}
                for o, c in self.refusal_counts.items():
                    if o in self.agents and self.agents[o]["legal_entity"] == a["legal_entity"]:
                        for r_, n_ in c.items():
                            mine[r_] = mine.get(r_, 0) + n_
                row["this_run"]["refusals_by_reason"] = dict(sorted(mine.items()))
                row["record"]["refusals_by_reason"] = self.ledger.refusals_view(a["legal_entity"])
            if aid not in self.inactive:
                out.append(self._msg("delivery_record", aid, now, row))
        return out

    # ---- price mode (D53) ----
    def _zone_agents(self, zone) -> list[str]:
        return [aid for aid, a in self.agents.items() if zone in a["zones"].values() and aid not in self.inactive]

    def _price_state(self, call):
        """Priced steps of the call (those with a zone target), the zone load there from the latest nominations
        (None where any agent has not nominated), the target, and each agent's nominated load (-site net) per step."""
        zone = call["zone"]
        tgt = self.targets.get(zone, {})
        steps = [t for t in call["_steps"] if t in tgt]
        agg = [self._zone_load(zone, t, call["call_id"]) for t in steps]
        loads = {aid: [-(self._nom_or_ref(aid, call["call_id"], t) or 0.0) for t in steps]
                 for aid in self._zone_agents(zone)}
        return steps, agg, [tgt[t] for t in steps], loads

    def _measure(self, steps, agg, target, loads, prev):
        """Max excess over target (kW), primal residual and plan change (L2, kW), and the dual residual s ($/MWh)
        at the rho the agents just responded to."""
        K = self.step_h / 1000.0
        ok = [i for i, a in enumerate(agg) if a is not None]
        a_, t_, p_ = [agg[i] for i in ok], [target[i] for i in ok], [self.pi.get(steps[i], 0.0) for i in ok]
        r = PM.l2(PM.residual(a_, t_, p_))
        d = PM.l2([x - y for aid in loads for x, y in zip(loads[aid], prev.get(aid, loads[aid]))])
        return {"max_excess_kw": max((a - t for a, t in zip(a_, t_)), default=0.0), "primal_res_kw": r,
                "plan_change_kw": d, "dual_res_usd_mwh": self.rho * d / K}

    def _price_round(self, now, r):
        open_calls = [c for c in self.calls.values() if not c["aggregated"] and c["deadline_utc"] > now]
        if not open_calls:
            raise Reject("bad_message", "no open nomination call for a price round")
        call = min(open_calls, key=lambda c: c["deadline_utc"])
        g = self.gate
        if g is None or g["call_id"] != call["call_id"]:
            if r != 1:
                raise Reject("bad_message", "price rounds for a call start at 1", call_id=call["call_id"])
            g = self.gate = {"call_id": call["call_id"], "binding": call["binding"], "round": 0, "prev": None,
                             "hist": [], "done": False}
        if g["done"]:
            raise Reject("bad_message", "price rounds for the call are finished", call_id=call["call_id"])
        if r != g["round"] + 1:
            raise Reject("bad_message", "unexpected price round", expected=g["round"] + 1, got=r)
        K = self.step_h / 1000.0
        steps, agg, target, loads = self._price_state(call)
        n = len(loads)
        if not n or not steps:
            raise Reject("bad_message", "the call has no agents or no target to price", call_id=call["call_id"])
        if self.rho is None:
            powers = sorted(self.agents[aid]["mandate"]["max_kw"] for aid in loads)
            med = (powers[(n - 1) // 2] + powers[n // 2]) / 2
            self.rho = self.rho0 = PM.rho0_public(self.price_spread, self.step_h, med)
            self.alpha = self.rho / n
        mu, tau, floor_f, cap_f = self.rb
        tol_e, tol_s, streak = self.price_tol
        row = {"call_id": call["call_id"], "binding": call["binding"], "round": r, "t_utc": now,
               "rho": self.rho, "alpha": self.alpha}
        final = r >= self.price_rounds
        if g["prev"] is not None:                       # measure the agents' response to the last broadcast
            m = self._measure(steps, agg, target, loads, g["prev"])
            row.update(m)
            g["hist"].append((m["max_excess_kw"], m["dual_res_usd_mwh"]))
            if PM.converged(g["hist"], tol_e, tol_s, streak):
                final = True
                row["converged"] = True
            else:
                self.rho = PM.rb_update(self.rho, m["primal_res_kw"],
                                        PM.rb_dual_kw(self.rho, self.rho0, m["plan_change_kw"]),
                                        mu, tau, self.rho0 / floor_f, self.rho0 * cap_f)
                self.alpha = self.rho / n
        old = [self.pi.get(t, 0.0) for t in steps]
        cap = self.price_cap * K if self.price_cap is not None else float("inf")
        if row.get("converged"):
            new, shift = old, [0.0] * len(steps)       # the agents already answered this price: no step
        else:
            known = [a is not None for a in agg]
            stepped = PM.price_update(old, [a if a is not None else 0.0 for a in agg], target, self.alpha, cap)
            new = [v if k_ else o for v, o, k_ in zip(stepped, old, known)]
            shift = PM.centre_shift(new, old, self.alpha, n)
        for t, v in zip(steps, new):
            self.pi[t] = v
        g["prev"], g["round"], g["done"] = loads, r, final
        if final:
            self.final_calls.add(call["call_id"])
        row.update({"rho_next": self.rho, "final": final, "max_pi_usd_mwh": max(new, default=0.0) / K,
                    "priced_steps": sum(1 for v in new if v > PM.PRICED),
                    "at_cap_steps": sum(1 for v in new if v >= cap - 1e-12)})
        self.price_log.append(row)
        pos = {t: i for i, t in enumerate(steps)}
        price = [round(new[pos[t]] / K, 6) if t in pos else 0.0 for t in call["_steps"]]
        shift_all = [round(shift[pos[t]], 6) if t in pos else 0.0 for t in call["_steps"]]
        subs = self.subs[call["zone"]]
        payload = {"zone": call["zone"], "call_id": call["call_id"], "window": call["window"], "price_usd_mwh": price,
                   "rho": self.rho, "shift_kw": shift_all, "round": r, "final": final}
        return [self._msg("peak_price", list(subs), now, payload)] if subs else []

    def _close_price_gate(self, call, now):
        """At a call's deadline: log the agents' final response to its price rounds; at a binding call, settle the
        price in force for every step it binds (a later binding call replaces earlier values)."""
        g = self.gate
        if g is not None and g["call_id"] == call["call_id"] and g["prev"] is not None:
            steps, agg, target, loads = self._price_state(call)
            m = self._measure(steps, agg, target, loads, g["prev"])
            self.price_log.append({"call_id": call["call_id"], "binding": call["binding"], "round": g["round"],
                                   "t_utc": now, "closing": True, **m})
        if call["binding"]:
            K = self.step_h / 1000.0
            for t in call["_steps"]:
                self.peak_settled[t] = (self.pi.get(t, 0.0) / K, call["call_id"])
            if self.settlement_rule == "max_first_call":
                self.call_price[call["call_id"]] = {t: self.pi.get(t, 0.0) / K for t in call["_steps"]}
                for t in call["_steps"]:
                    self.settle_chain.setdefault(t, []).append(call["call_id"])

    def _peak_settle(self, now):
        """Closing tick: per agent, the settled price on metered site net load at priced steps, refunded on its
        reference. Load = -site net; a missing reading or reference counts as 0, as for imbalance. With
        settlement_reference selfish_1p (D56) the reference at each step is the settling call's selfish_reference,
        and a missing one raises: settlement fails closed."""
        self.peak_done = True
        priced = {t: p for t, (p, _) in self.peak_settled.items() if p > 1e-9 and _from_ok(self, t)}
        by_call = {t: cid for t, (p, cid) in self.peak_settled.items() if p > 1e-9 and _from_ok(self, t)}
        if not priced:
            return []
        h = self.step_h / 1000.0
        ts = sorted(priced)
        window = {"start_utc": ts[0], "end_utc": fmt_t(parse_t(ts[-1]) + self.step)}
        out = []
        for aid, a in self.agents.items():
            mine = {t: p for t, p in priced.items() if self._holds(aid, t)}      # D64 tenure: each step billed once
            metered, ref = self._metered(aid), {}
            unsettled: dict[str, float] = {}         # t -> metered kW of this agent's assets left out at t (below)
            for asset in a["assets"]:
                if self.settlement_reference == "selfish_1p":
                    for t in mine:
                        r = self.call_reference.get(by_call[t], {}).get(asset, {}).get(t)
                        if r is None:
                            # only an asset that had no holder at this call's gate (by the exchange's clock) may
                            # lack its reference: it is not settled on the call's steps. Any other gap fails closed
                            if by_call[t] not in self.unheld_at_gate.get(asset, frozenset()):
                                raise RuntimeError(f"D56: no selfish_reference for {asset} on {by_call[t]} at {t}")
                            unsettled[t] = unsettled.get(t, 0.0) + self.net_meter.get(asset, {}).get(t, 0.0)
                            continue
                        ref[t] = ref.get(t, 0.0) + r
                else:
                    for t, v in self.reference.get(asset, {}).items():
                        ref[t] = ref.get(t, 0.0) + v
            if unsettled:
                left_out = {t for t in unsettled
                            if all(by_call[t] in self.unheld_at_gate.get(x, frozenset())
                                   and self.call_reference.get(by_call[t], {}).get(x, {}).get(t) is None
                                   for x in a["assets"])}
                mine = {t: p for t, p in mine.items() if t not in left_out}
                metered = {t: v - unsettled.get(t, 0.0) for t, v in metered.items()}
            charge = sum(p * -metered.get(t, 0.0) * h for t, p in mine.items())
            refund = sum(p * -ref.get(t, 0.0) * h for t, p in mine.items())
            out.append(self._msg("peak_settle", aid, now, {"window": window, "steps": len(mine),
                                                           "charge_usd": charge, "refund_usd": refund,
                                                           "net_usd": refund - charge}))
        return out

    def _peak_settle_max(self, now):
        """Closing tick under settlement_rule max_first_call (D76's max rule; see __init__). Settles every step that any
        binding call priced (settled price above 1e-9), over each agent's tenure. A missing reference fails closed,
        except D77's (an asset with no holder at that call's gate), which drops the call from the asset's chain."""
        self.peak_done = True
        chain = self.settle_chain
        priced = sorted(t for t, cids in chain.items()
                        if _from_ok(self, t) and any(self.call_price[c][t] > 1e-9 for c in cids))
        if not priced:
            return []
        h = self.step_h / 1000.0
        window = {"start_utc": priced[0], "end_utc": fmt_t(parse_t(priced[-1]) + self.step)}
        out = []
        for aid, a in self.agents.items():
            assets = a["assets"]
            metered = self._metered(aid)
            net = charge = 0.0
            n = 0
            for t in priced:
                if not self._holds(aid, t):                      # D64 tenure: each step billed once
                    continue
                cids = chain[t]
                calls = []                                       # (call_id, Q summed over the agent's assets)
                partial = None                                   # the settling call, some assets excused (D77)
                for c in cids:
                    refs = [self.call_reference.get(c, {}).get(x, {}).get(t) for x in assets]
                    missing = [x for x, r in zip(assets, refs) if r is None]
                    for x in missing:
                        if c not in self.unheld_at_gate.get(x, frozenset()):
                            raise RuntimeError(f"D56: no selfish_reference for {x} on {c} at {t}")
                    if not missing:
                        calls.append((c, sum(refs)))
                    elif c == cids[-1] and len(missing) < len(assets):
                        partial = (c, sum(r for r in refs if r is not None), missing)
                    # Otherwise D77: an asset had no holder at c's gate, so this agent (which registers all its assets
                    # at once) wasn't registered then and has no nomination for c; c drops out of its chain, which
                    # is the same as nominating its reference there. A fully excused settling call: not settled.
                if partial is not None:
                    # today's D77 treatment: the excused assets' metered load is left out and the step is settled
                    # on the others' reference at the settling call's price. Earlier calls in the chain may have every
                    # reference (sent while previous holders held the assets), but they closed before this agent
                    # registered, so it has no nomination there: N = Q, they add nothing. A binding nomination from
                    # this agent on one of them would be a state the rule doesn't define, and fails closed.
                    if any(self.call_nom.get(c, {}).get(aid) for c, _ in calls):
                        raise RuntimeError(f"D76: max rule undefined for {aid} at {t}: a partly excused settling call "
                                           "after a call it nominated for")
                    c_k, q_k, missing = partial
                    p_k = self.call_price[c_k][t]
                    if p_k <= 1e-9:
                        continue
                    m_t = metered.get(t, 0.0) - sum(self.net_meter.get(x, {}).get(t, 0.0) for x in missing)
                    net += p_k * (m_t - q_k) * h
                    charge += p_k * -m_t * h
                    n += 1
                    self.settle_paths["today_partly_excused"] += 1
                    continue
                if not calls or calls[-1][0] != cids[-1]:
                    self.settle_paths["not_settled_d77"] += 1
                    continue                                     # the settling call is excused (D77): not settled
                noms = [self.call_nom.get(c, {}).get(aid, {}).get(t) for c, _ in calls]
                terms, rels, prev = [], [], None
                for (c, q), nv in zip(calls, noms):
                    rel = (q if nv is None else nv) - q          # no binding nomination: it nominated its reference
                    terms.append((self.call_price[c][t], rel if prev is None else rel - prev))
                    rels.append(rel)
                    prev = rel
                later, pay = float("-inf"), 0.0
                for p, inc in reversed(terms):
                    later = max(later, p)
                    pay += later * inc
                c_k, q_k = calls[-1]
                p_k = self.call_price[c_k][t]
                n_k = q_k if noms[-1] is None else noms[-1]
                m_t = metered.get(t, 0.0)
                pay += p_k * (m_t - n_k)
                p_1 = max(p for p, _ in terms)
                if p_1 > p_k + 1e-9:
                    # relief cap (owner, Nov 4, D76: the max rule never pays for relief the meter didn't show). The
                    # step is today's 1p at the settling call plus a premium for relief priced higher at earlier
                    # calls; each call's relief is paid at its own price spread, no more than the metered relief
                    # against the reference of the last call that priced the step above 0, never below 0
                    # Per call: the premium is sum_i (L_i - L_{i+1}) rel_i (L_{k+1} = p_k), and each call's relief is
                    # limited to max(0, min(rel_i, metered relief against Q_j))
                    q_j = next((q for c, q in reversed(calls) if self.call_price[c][t] > 1e-9), calls[0][1])
                    met = m_t - q_j
                    prem, nxt, lvl = 0.0, p_k, float("-inf")
                    for (p, _), rel in zip(reversed(terms), reversed(rels)):
                        lvl = max(lvl, p)
                        prem += (lvl - nxt) * max(0.0, min(rel, met))
                        nxt = lvl
                    pay = p_k * (m_t - q_k) + prem
                net += pay * h
                charge += p_k * -m_t * h
                n += 1
                if len(calls) == len(cids):
                    path = "max_rule"
                else:
                    path = "today_via_d77" if len(calls) == 1 else "max_rule_after_d77_drop"
                self.settle_paths[path] += 1
                if p_k <= 1e-9:
                    self.settle_paths["of_which_settling_price_zero"] += 1
            out.append(self._msg("peak_settle", aid, now, {"window": window, "steps": n, "charge_usd": charge,
                                                           "refund_usd": net + charge, "net_usd": net}))
        return out

    def _rebound_excess(self) -> dict[str, dict[str, float]]:
        """agent -> t -> kW of metered net load above its D34 cap, beyond the band."""
        out = {}
        for aid, cap in self.caps.items():
            metered = self._metered(aid)
            band = band_kw(self.agents[aid]["mandate"]["max_kw"], self.band_pct, self.band_min_kw)
            ex = {}
            for t, base in cap["base"].items():
                if not (cap["from"] <= t < cap["until"]) or t in cap["windows"] or t not in metered:
                    continue
                load_cap = -base + cap["prep"].get(t, 0.0)
                e = -metered[t] - load_cap - band
                if e > 1e-9:
                    ex[t] = e
            out[aid] = ex
        return out

    def _rebound(self, now, excess):
        self.rebound_done = True
        out = []
        for aid, ex in excess.items():
            cap = self.caps[aid]
            kwh = sum(ex.values()) * self.step_h
            band = band_kw(self.agents[aid]["mandate"]["max_kw"], self.band_pct, self.band_min_kw)
            out.append(self._msg("rebound", aid, now, {
                "window": {"start_utc": cap["from"], "end_utc": cap["until"]}, "excess_kwh": kwh,
                "charge_usd": self.scarcity_usd_mwh * kwh / 1000.0, "band_kw": band}))
        return out

    def _zone_room(self, zone, now, force):
        """D50: room per step from now to the end of the targets: target minus zone load (latest nominations plus
        awarded prep not yet nominated); null where the zone load isn't known. Sent to the zone's subscribers when
        forced (after a clearing) or when any step moved by more than 1 kW since the last one."""
        subs = self.subs.get(zone)
        steps = sorted(t for t in self.targets.get(zone, {}) if t >= now)
        if not subs or not steps:
            return []
        room = []
        for t in steps:
            load = self._zone_load(zone, t)
            room.append(None if load is None else round(self.targets[zone][t] - load, 3))
        last = self.last_room.get(zone)
        if not force and last is not None and len(last) >= len(room):
            prev = last[len(last) - len(room):]
            if all((a is None and b is None) or (a is not None and b is not None and abs(a - b) <= 1.0)
                   for a, b in zip(room, prev)):
                return []
        self.last_room[zone] = room
        end = fmt_t(parse_t(steps[-1]) + self.step)
        return [self._msg("zone_room", list(subs), now, {"zone": zone, "window": {"start_utc": steps[0], "end_utc": end},
                                                         "room_kw": room})]

    def _rebase_caps(self, call, now):
        """D47: raise each capped agent's D34 cap toward its binding nomination for this call's steps (inside the
        capped span, outside award windows) where the nomination adds load over the cap. Nominations already include
        awarded prep, so the prep-free base is the nominated net plus awarded prep. Added load is accepted only within
        zone room, scaled pro rata when the zone is over target; refused load is clipped from the binding nomination.
        Less load than the cap leaves the cap where it is (it never becomes a discharge obligation)."""
        steps, zone = call["_steps"], call["zone"]
        targets = self.targets.get(zone, {})
        want: dict[str, dict[str, tuple[float, float]]] = {}       # agent -> t -> (old cap load, nominated load)
        for aid in sorted(self.caps):
            cap = self.caps[aid]
            if zone not in self.agents[aid]["zones"].values():
                continue
            latest = self._latest(aid)
            for t in steps:
                if cap["from"] <= t < cap["until"] and t not in cap["windows"] and t in latest and t in cap["base"]:
                    old = -cap["base"][t] + cap["prep"].get(t, 0.0)
                    want.setdefault(aid, {})[t] = (old, -latest[t])
        scale = {}
        for t in steps:
            added = sum(max(w[t][1] - w[t][0], 0.0) for w in want.values() if t in w)
            if added <= 1e-9:
                continue
            load = self._zone_load(zone, t)
            if t not in targets or load is None:
                scale[t] = 0.0                       # no target or no full picture: keep the old cap
            else:
                over = max(load - targets[t], 0.0)
                scale[t] = max(added - over, 0.0) / added
        out = []
        for aid, w in want.items():
            cap = self.caps[aid]
            clipped = {}
            for t, (old, new) in w.items():
                if new <= old:
                    continue                         # never lower a cap
                load = old + (new - old) * scale.get(t, 1.0)    # negligible added load: as nominated
                cap["base"][t] = cap["prep"].get(t, 0.0) - load
                if load < new - 1e-9:
                    clipped[t] = -load
            if clipped:      # the binding schedule in force: the nomination with refused load taken out
                self._add_nom(aid, call["call_id"], clipped)
            latest = self._latest(aid)
            out.append(self._msg("cap_rebase", aid, now, {
                "call_id": call["call_id"], "window": call["window"],
                "base_kw": [cap["base"].get(t) if t in w else None for t in steps],
                "nomination_kw": [latest.get(t) for t in steps]}))
        return out

    def _award_cap(self, aid, need, prep, now):
        """D34 (revised): the first award freezes the agent's nominated schedule; every award adds its prep and
        window, and extends the capped span to its window end + rebound_hours."""
        cap = self.caps.get(aid)
        if cap is None:
            cap = self.caps[aid] = {"base": dict(self._latest(aid)), "from": now, "until": now, "prep": {},
                                    "windows": set()}
        cap["windows"] |= need["_steps"]
        for t, v in prep.items():
            cap["prep"][t] = cap["prep"].get(t, 0.0) + v
        cap["until"] = max(cap["until"], fmt_t(parse_t(need["window"]["end_utc"]) + self.rebound_span))

    def _publish_gate_prices(self, now):
        """After the last need at a gate clears: each need's uniform price, in one message (D42)."""
        if not self.gate_session:
            return []
        prices = {nid: price for _, nid, price in self.gate_session}
        gates = sorted({g for g, _, _ in self.gate_session})
        to = sorted({a for z in self.subs.values() for a in z} | {self.operator_id})
        self.gate_session = []
        return [self._msg("gate_prices", to, now, {"gate_utc": gates[-1], "prices": prices})]

    def _mandate_room(self, o, q):
        """Sequential mode: awards plus q within max_kw at every step and within daily_kwh (re-checked at clearing,
        since offers are alternatives). Returns the kW that fits."""
        aid = o["agent_id"]
        mandate = self.agents[aid]["mandate"]
        mine = [a for a in self.awards.values() if a["agent_id"] == aid]
        room = q
        for t in o["_steps"]:
            load = sum(a["accredited_kw"] for a in mine if t in a["_steps"])
            room = min(room, mandate["max_kw"] - load)
        budget = mandate.get("daily_kwh")
        if self.daily_energy and budget is not None:
            used: dict[str, float] = {}
            for a in mine:
                for d, e in self._offer_energy(sorted(a["_steps"]), a["accredited_kw"]).items():
                    used[d] = used.get(d, 0.0) + e
            per_kw = self._offer_energy(sorted(o["_steps"]), 1.0)
            for d, e in per_kw.items():
                room = min(room, (budget.get(d, 0.0) + ENERGY_TOL_KWH - used.get(d, 0.0)) / e)
        return max(room, 0.0)

    def _clear(self, need, now):
        offers = [o for o in self.offers.values() if o["need_id"] == need["need_id"] and o["live"]]
        eligible = offers
        if self.require_nominations:     # D32: no nomination covering the window, no award
            eligible = [o for o in offers if need["_steps"] <= self._latest(o["agent_id"]).keys()]
        skipped = []
        if self.prep_clearing or self.sequential:
            targets = self.targets[need["zone"]]
            taken: dict[str, float] = {}

            def fits(o, q):
                """kW of this offer that fits: (sequential) within mandate room after this agent's awards, then q if its
                pro-rata prep stays at or below target, else (with partial_prep) the largest part that does, else 0."""
                if self.sequential:
                    room = self._mandate_room(o, q)
                    if room < q - 1e-9:
                        if not self.partial_prep or room < self.min_partial_kw:
                            return 0.0, "mandate"
                        q = room
                if not self.prep_clearing:
                    return q, None
                frac, reason = q / o["kw"], None
                for t, v in o["_prep"].items():
                    if t not in targets:
                        return 0.0, "missing_target"
                    load = self._zone_load(need["zone"], t)
                    if load is None:
                        return 0.0, "missing_nominations"
                    room = targets[t] - load - taken.get(t, 0.0)
                    if v * frac > room + 1e-6:
                        frac, reason = max(room, 0.0) / v, "over_target"
                q_fit = frac * o["kw"]
                if reason and (not self.partial_prep or q_fit < self.min_partial_kw):
                    return 0.0, reason
                for t, v in o["_prep"].items():
                    taken[t] = taken.get(t, 0.0) + v * frac
                return min(q_fit, q), ("partial_over_target" if reason else None)

            accepted, price, skipped = clear_with(need["kw_needed"], need["price_cap"], eligible, fits)
        else:
            accepted, price = clear(need["kw_needed"], need["price_cap"], eligible)
        for o in offers:
            o["live"] = False
        need["status"] = "cleared"
        out = []
        if self.sequential:
            self.gate_session.append((need["gate_utc"], need["need_id"], price))
            winners = self.gate_winners.setdefault(need["gate_utc"], set())
            winners |= {o["agent_id"] for o, _ in accepted}
        for o, q in accepted:
            payload = {"offer_id": o["offer_id"], "need_id": need["need_id"], "window": need["window"],
                       "accredited_kw": q, "price_usd_mwh": price, "cap_kw": None}
            prep = {t: v * q / o["kw"] for t, v in o.get("_prep", {}).items()}
            if self.prep_clearing and prep:
                ts = sorted(prep)
                t0 = parse_t(ts[0])
                n = int((parse_t(ts[-1]) - t0) / self.step) + 1
                payload["prep"] = {"start_utc": ts[0],
                                   "kw": [prep.get(fmt_t(t0 + i * self.step), 0.0) for i in range(n)]}
                pend = self.pending_prep.setdefault(o["agent_id"], {})
                for t, v in prep.items():
                    pend[t] = pend.get(t, 0.0) + v
            m = self._msg("award", [o["agent_id"], self.operator_id], now, payload)
            self.awards[m["msg_id"]] = {**payload, "award_id": m["msg_id"], "agent_id": o["agent_id"],
                                        "settled": False, "_steps": need["_steps"]}
            if self.no_rebound:
                self._award_cap(o["agent_id"], need, prep, now)
            out.append(m)
        if self.sequential:          # the need id only, while needs at this gate are still clearing (D42)
            subs = self.subs[need["zone"]]
            if subs:
                out.append(self._msg("need_cleared", list(subs), now, {"need_id": need["need_id"]}))
        if self.publish_room:
            out += self._zone_room(need["zone"], now, force=True)
        if self.prep_clearing:
            _, price_free = clear(need["kw_needed"], need["price_cap"], eligible)
            out.append(self._msg("clearing_report", self.operator_id, now, {
                "need_id": need["need_id"], "price_usd_mwh": price, "price_without_prep_check": price_free,
                "accepted": [[o["offer_id"], q] for o, q in accepted],
                "skipped": [[o["offer_id"], reason] for o, reason in skipped if reason != "partial_over_target"],
                "partial": [[o["offer_id"], q] for o, q in accepted
                            if any(x is o and r == "partial_over_target" for x, r in skipped)]}))
        return out

    def _settle(self, award, now):
        aid = award["agent_id"]
        meter: dict[str, float] = {}
        for asset in self.agents[aid]["assets"]:
            for t, v in self.meter.get(asset, {}).items():
                if self._holds(aid, t):                    # D64 tenure: another holder's delivery isn't credited
                    meter[t] = meter.get(t, 0.0) + v
        need = self.needs[award["need_id"]]
        mine = [a for a in self.awards.values() if a["agent_id"] == aid]
        res = settle_award(award, mine, meter, need["_step_list"], self.step_h, need["price_cap"])
        rec = self.record.setdefault(aid, [0.0, 0.0])
        rec[0] += res["delivered_kwh"]
        rec[1] += res["awarded_kwh"]
        award["settled"] = True
        payload = {"award_id": award["award_id"], "need_id": award["need_id"], "delivered_kw": res["delivered_kw"],
                   "credit_usd": res["credit_usd"], "charge_usd": res["charge_usd"],
                   "new_delivery_record": rec[0] / rec[1] if rec[1] else 1.0}
        return self._msg("settle", [aid, self.operator_id], now, payload)
