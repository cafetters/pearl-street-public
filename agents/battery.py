"""
Code-only battery agent. Sees only its own battery, its own site's PV and load, and the price forecast the
simulator hands it. Talks to the exchange only through JSON messages.

Bids: for each need, offer the most discharge it can hold over the window (given its awards and earlier
offers), at a floor equal to its opportunity cost: forecast revenue lost per MWh committed (D27).
Bids are recomputed whenever inputs change (new prices, an award, SOC off plan) and revised if they move (D28).

Nominations (D32): when the operator calls for them, it sends its planned 15-min site net for the call's window,
and sends it again if the plan moves before the deadline. Once a binding call closes, that schedule is locked and
the agent plans with the imbalance price on any deviation beyond the free band.

M2a (all off by default):
  firm_offers : size each offer from the energy reachable by the window start, with preparation starting at gate
                closure (until then it follows its current plan), not at bid time.
  send_prep   : declare that preparation in the offer: the extra net load per step outside the window, from the
                gate to window end + prep_hours (recharge after the window counts too).
  no_rebound  : after its first award, plan under the D34 cap (pre-award nomination plus awarded prep), with
                rebound_priority "grid" (miss an award rather than break the cap) or "delivery" (the reverse).
  daily_budget: kWh per local date it may commit (declared in its mandate); offers stay within what is left.
  premium     : $/MWh added to every floor for the day-ahead vs. real-time gap.
  independent_offers (D42, with sequential clearing): every offer is sized from known awards only, never assuming
                its other live offers win. Between clearings at a gate, after a new award, it resizes its remaining
                offers (kW down only at the gate, prep and floor exact for its full award set).
  release_superseded_lock (D46): a binding call announced but not yet closed takes over the steps it covers, so
                the agent plans those steps without the earlier binding nomination's lock (price-informed re-plan).
  cap_rebase  (D47, with no_rebound): the exchange re-bases the D34 cap at each binding deadline, so the agent
                also plans the open binding call's steps without the old cap, then adopts the cap_rebase it gets.
  plan_to_nomination (D48): plan to the nomination and the cap exactly; the free band is for forecast error.
  use_room    (D50): when sizing an offer, keep its preparation within the published zone room (allowed load plus
                room at each prep step outside the window), so prep lands where the zone can take it.
  price_mode  (D53, arm C): no needs or offers. It plans with the latest peak price on its net load. In each price
                round (between_price_rounds) it re-plans against the new price plus a prox term centred on its last
                nominated net load minus the exchange's centre shift, and nominates the call every round.
  peak_response_margin_usd_mwh (D58 test, price mode): m. Each plan made against a peak price also pays (m/2) per MWh
                of distance from its own selfish plan (same step, SOC, forecasts and locks; no peak price, no prox),
                so it moves energy only where the peak price beats its lost day-ahead arbitrage by about m. When the
                margin binds it logs the MWh it declined to shift against the m = 0 plan (margin_log). m = 0 is
                today's behaviour; a non-participant never uses it.
"""
import hashlib
import math
import time

import numpy as np

from agents.planner import CAP_PRIORITY_USD_MWH, Battery, solve
from exchange import messages as M

REVISE_REL = 0.01
MARGIN_BIND_KW = 1e-3         # D58: a plan this close to its selfish anchor at every step pays no margin
RENOMINATE_KW = 1.0


class BatteryAgent:
    def __init__(self, agent_id: str, battery: Battery, pv, curt, load, clock_iso: list[str], end_iso: str,
                 dt_h: float, run_id: str, zone: str, participate: bool, wear: float = 10.0,
                 min_offer_kw: float = 50.0, firm_offers: bool = False, send_prep: bool = False,
                 no_rebound: bool = False, daily_budget: dict | None = None, local_dates: list[str] | None = None,
                 premium: float = 0.0, prep_hours: float = 24.0, rebound_priority: str = "grid",
                 independent_offers: bool = False, release_superseded_lock: bool = False,
                 cap_rebase: bool = False, plan_to_nomination: bool = False, use_room: bool = False,
                 price_mode: bool = False, prox_eps_usd_mwh: float = 0.05,
                 peak_response_margin_usd_mwh: float = 0.0):
        self.id, self.b = agent_id, battery
        self.avail = np.maximum(np.nan_to_num(pv) - curt, 0.0)
        self.base_export = np.nan_to_num(pv) - curt - load
        self.iso, self.end_iso, self.dt_h = clock_iso, end_iso, dt_h
        self.idx = {t: i for i, t in enumerate(clock_iso)}
        self.T = len(clock_iso)
        self.run_id, self.zone, self.participate = run_id, zone, participate
        self.wear, self.min_offer_kw = wear, min_offer_kw
        self.asset_id = f"{agent_id}/bess"
        self.legal_entity = f"{agent_id} Owner LLC (simulated)"
        self.inbox: list[str] = []
        self.needs: dict[str, dict] = {}      # open needs, in arrival order
        self.offers: dict[str, dict] = {}     # need_id -> {offer_id, kw, floor}
        self.commit = np.zeros(self.T)        # awarded kW per step
        self.plan = None
        self.price_version = None
        self.dirty = True
        self.credits = self.charges = 0.0
        self.rejects: list[dict] = []
        self.calls: dict[str, dict] = {}      # call_id -> {mask, deadline, binding, sent}
        self.nom = np.full(self.T, np.nan)    # locked (binding) nomination, site net kW
        self.band = 0.0
        self.imb_price = 0.0
        self.imbalance_usd = self.imbalance_kwh = 0.0
        self.n_solves = 0
        self._n = 0
        self.firm_offers, self.send_prep, self.no_rebound = firm_offers, send_prep, no_rebound
        self.daily_budget, self.local_dates, self.premium = daily_budget, local_dates, premium
        self.prep_steps = round(prep_hours / dt_h)
        self.cap_price = CAP_PRIORITY_USD_MWH[rebound_priority]
        self.independent_offers = independent_offers
        self.release_superseded_lock, self.cap_rebase = release_superseded_lock, cap_rebase
        self.plan_to_nomination = plan_to_nomination
        self.use_room = use_room
        self.room = np.full(self.T, np.nan)           # latest published zone room per step (D50)
        self._new_award = False
        self.gate_prices: dict[str, float | None] = {}
        self.latest_sent = np.full(self.T, np.nan)    # latest nominated site net per step, as the exchange holds it
        self.cap = None                               # D34 rebound cap, after the first award
        self.load_cap = None
        self.price_mode = price_mode
        self.peak = np.zeros(self.T)                  # latest peak price per step, $/MWh (D53)
        self.round = None                             # the price round to answer: call, rho, centre shift
        self.prox_eps = prox_eps_usd_mwh * dt_h / 1000.0
        self.peak_net_usd = 0.0
        self.margin = float(peak_response_margin_usd_mwh)
        self.posture = "cautious" if self.margin > 0 else "normal"     # label only; the number is the config's m
        self._anchors: dict[bytes, np.ndarray] = {}   # selfish anchor per solve inputs (one LP per gate, not round)
        self.margin_log: list[dict] = []
        self._final_round = False
        self.margin_compare_secs = 0.0               # time in the m = 0 comparison solves (logging cost only)

    # ---- messaging ----
    def receive(self, raw: str):
        self.inbox.append(raw)

    def _msg(self, msg_type, t, payload):
        self._n += 1
        return M.encode(M.make(msg_type, self.id, "exchange", t, payload, f"{self.id}:{self._n}", self.run_id))

    def _mask(self, window):
        m = np.zeros(self.T, bool)
        m[self.idx[window["start_utc"]]:self.idx.get(window["end_utc"], self.T)] = True
        return m

    def start(self, k: int) -> list[str]:
        if not self.participate:
            return []
        t = self.iso[k]
        return [self._msg("register", t, {
                    "agent_id": self.id, "legal_entity": self.legal_entity,
                    "assets": [{"asset_id": self.asset_id, "zone": self.zone, "type": "bess"}],
                    "mandate": self._mandate()}),
                self._msg("subscribe", t, {"signals": ["post_need", "call_nominations"]
                                           + (["peak_price"] if self.price_mode else []), "zones": [self.zone]})]

    def _mandate(self):
        m = {"max_kw": float(self.b.P), "price_floor": 0.0, "workload_limits": None}
        if self.daily_budget is not None:
            m["daily_kwh"] = {d: float(v) for d, v in self.daily_budget.items()}     # unrounded (JSON keeps it)
        return m

    # ---- one step ----
    def act(self, k: int, soc: float, price_version: int, price: np.ndarray):
        """Handle the inbox, update bids, and return (messages, (charge kW, discharge kW, extra curtail kW))."""
        t = self.iso[k]
        out = []
        new_needs, rebid = self._read_inbox()
        for nid in [n for n, v in self.needs.items() if v["gate_utc"] <= t]:
            del self.needs[nid]
            self.offers.pop(nid, None)
        for c in self.calls.values():
            if c["binding"] and not c["locked"] and c["deadline_utc"] <= t:
                c["locked"] = True
                if c["sent"] is not None:
                    self.nom[c["mask"]] = c["sent"]
                    self.dirty = rebid = True
        if price_version != self.price_version:
            self.price_version, self.dirty, rebid = price_version, True, True
        if self.plan is not None and not self.dirty:
            expect = self.plan.s[k - self.plan.k0 - 1] if k > self.plan.k0 else self.plan_s0
            if abs(soc - expect) > 1.0:
                self.dirty = rebid = True

        # Plan, then nominate, then bid: prep is measured against the nomination the exchange would freeze at an
        # award, so that nomination has to be sent first. A re-nomination marks bids for recomputation (D28).
        if self.dirty or self.plan is None:
            self.plan = self._solve(k, soc, price, self.commit)
            self.plan_s0, self.dirty = soc, False
        if self.participate:
            noms = self._nominate(k)
            if noms and self.send_prep and self.offers:
                rebid = True
            out += noms
        if self.participate and (new_needs or rebid):
            out += self._bid(k, soc, price, None if rebid else new_needs)
        j = k - self.plan.k0
        return out, (self.plan.c[j], self.plan.d[j], self.plan.x[j])

    def _read_inbox(self):
        """Handle everything received since the last read. Returns (new need ids, whether bids need recomputing)."""
        new_needs, rebid = [], False
        for raw in self.inbox:
            m = M.decode(raw)
            if m.get("sender") != "exchange":
                continue        # D69 rule 2: act only on what the exchange sends (a planted channel may deliver more)
            p = m["payload"]
            typ = m["msg_type"]
            if typ == "post_need":
                self.needs[p["need_id"]] = {**p, "mask": self._mask(p["window"])}
                new_needs.append(p["need_id"])
            elif typ == "award":
                self.commit += p["accredited_kw"] * self._mask(p["window"])
                self.dirty = rebid = True
                self._new_award = True
                if self.no_rebound:
                    self._add_to_cap(m["t_utc"], p)
            elif typ == "need_cleared":
                self.needs.pop(p["need_id"], None)
                self.offers.pop(p["need_id"], None)
            elif typ == "gate_prices":
                self.gate_prices.update(p["prices"])
            elif typ == "settle":
                self.credits += p["credit_usd"]
                self.charges += p["charge_usd"]
            elif typ == "call_nominations":
                self.calls[p["call_id"]] = {**p, "mask": self._mask(p["window"]), "sent": None, "locked": False}
                if p["binding"] and self.release_superseded_lock:
                    self.dirty = rebid = True          # re-plan the steps this call takes over
                if p.get("imbalance_usd_mwh") is not None:
                    self.imb_price = p["imbalance_usd_mwh"]
                    self.band = max(p["band_pct"] * self.b.P, p["band_min_kw"])
            elif typ == "imbalance":
                self.imbalance_usd += p["charge_usd"]
                self.imbalance_kwh += p["deviation_kwh"]
            elif typ == "rebound":
                self.imbalance_usd += p["charge_usd"]
            elif typ == "peak_price":
                mask = self._mask(p["window"])
                self.peak[mask] = np.asarray(p["price_usd_mwh"], float)[:mask.sum()]
                shift = np.full(self.T, np.nan)
                shift[mask] = np.asarray(p["shift_kw"], float)[:mask.sum()]
                self.round = {"call_id": p["call_id"], "rho": p["rho"], "shift": shift, "final": p["final"]}
                self.dirty = True
            elif typ == "peak_settle":
                self.peak_net_usd += p["net_usd"]
            elif typ == "zone_room":
                pos = np.flatnonzero(self._mask(p["window"]))
                vals = np.array([np.nan if v is None else v for v in p["room_kw"]], float)[:len(pos)]
                self.room[pos[:len(vals)]] = vals
            elif typ == "cap_rebase":
                m_ = self._mask(p["window"])
                pos = np.flatnonzero(m_)
                base = np.array([np.nan if v is None else v for v in p["base_kw"]], float)[:len(pos)]
                if self.cap is not None:
                    ok = ~np.isnan(base)
                    self.cap["base"][pos[ok]] = base[ok]
                    self._rebuild_load_cap()
                nom = np.array([np.nan if v is None else v for v in p["nomination_kw"]], float)[:len(pos)]
                c = self.calls.get(p["call_id"])
                if c is not None:
                    c["sent"] = np.where(np.isnan(nom), c["sent"] if c["sent"] is not None else np.nan, nom)
                    if c["locked"]:
                        self.nom[c["mask"]] = c["sent"]
                ok = ~np.isnan(nom)
                self.latest_sent[pos[ok]] = nom[ok]
                self.dirty = rebid = True
            elif typ == "reject":
                self.rejects.append(p)
                for nid, o in list(self.offers.items()):
                    if o["offer_id"] == p["ref_msg_id"]:
                        del self.offers[nid]
        self.inbox.clear()
        return new_needs, rebid

    def between_clearings(self, k: int, soc: float, price_version: int, price: np.ndarray) -> list[str]:
        """D42: between clearings at a gate. Reads awards and need_cleared; after a new award, resizes the remaining
        live offers (at the gate kW may only go down; prep and floor are recomputed for the full award set). No
        dispatch and no nominations."""
        self._new_award = False
        self._read_inbox()
        if not (self.participate and self._new_award and self.offers):
            return []
        if self.dirty or self.plan is None:
            self.plan = self._solve(k, soc, price, self.commit)
            self.plan_s0, self.dirty = soc, False
        return self._bid(k, soc, price, None)

    def between_price_rounds(self, k: int, soc: float, price_version: int, price: np.ndarray) -> list[str]:
        """D53: answer a price round. Step k is already dispatched, so it re-plans from k + 1 (from the SOC its step-k
        action reaches) against the new peak price plus (rho/2)(net load - centre)^2, centre = its last nominated net
        load minus the exchange's shift, and nominates the round's call whatever the change."""
        self._read_inbox()
        rnd, self.round = self.round, None
        if not (self.participate and rnd) or k + 1 >= self.T or self.plan is None:
            return []
        c = self.calls.get(rnd["call_id"])
        if c is None or c["deadline_utc"] <= self.iso[k]:
            return []
        # after an earlier round the plan already starts at k + 1, from the SOC it saved
        s_next = self.plan_s0 if self.plan.k0 == k + 1 else float(self.plan.s[k - self.plan.k0])
        centre = np.where(c["mask"] & ~np.isnan(rnd["shift"]) & ~np.isnan(self.latest_sent),
                          self.base_export - self.latest_sent - rnd["shift"], np.nan)
        self._final_round = bool(rnd.get("final"))       # D58 log: the final round's plan is the one nominated
        self.plan = self._solve(k + 1, s_next, price, self.commit, prox=(rnd["rho"], centre[k + 1:], self.prox_eps))
        self._final_round = False
        self.plan_s0, self.dirty = s_next, False
        p = self.plan
        net = np.full(self.T, np.nan)
        net[p.k0:] = self.base_export[p.k0:] + p.d - p.c - p.x
        sched = np.round(net[c["mask"]], 3)
        c["sent"] = sched
        self.latest_sent[c["mask"]] = sched
        return [self._msg("nominate", self.iso[k], {"call_id": rnd["call_id"], "zone": c["zone"],
                                                    "window": c["window"], "kw": [float(v) for v in sched]})]

    def _nominate(self, k):
        """Send (or resend, if the plan moved) the planned site net for every call still open."""
        t, out = self.iso[k], []
        p = self.plan
        net = np.full(self.T, np.nan)
        net[p.k0:] = self.base_export[p.k0:] + p.d - p.c - p.x
        for cid, c in self.calls.items():
            if c["deadline_utc"] <= t:
                continue
            sched = np.round(net[c["mask"]], 3)
            if c["sent"] is None or np.abs(sched - c["sent"]).max() > RENOMINATE_KW:
                out.append(self._msg("nominate", t, {"call_id": cid, "zone": c["zone"], "window": c["window"],
                                                     "kw": [float(v) for v in sched]}))
                c["sent"] = sched
                self.latest_sent[c["mask"]] = sched
        return out

    def _superseded(self) -> np.ndarray:
        """Steps an announced, still-open binding call will take over (D46)."""
        m = np.zeros(self.T, bool)
        if self.release_superseded_lock:
            for c in self.calls.values():
                if c["binding"] and not c["locked"]:
                    m |= c["mask"]
        return m

    def _solve(self, k, soc, price, commit, q_mask=None, ref_net=None, use_cap=True, extra_cap=None, prox=None):
        self.n_solves += 1
        free = self._superseded()
        nom_all = self.nom
        if free.any():
            nom_all = nom_all.copy()
            nom_all[free] = np.nan
        nom = nom_all[k:] if not np.isnan(nom_all[k:]).all() else None
        cap = None
        if use_cap and self.load_cap is not None:
            cap_all = self.load_cap
            if self.cap_rebase and free.any():
                cap_all = cap_all.copy()
                cap_all[free] = np.nan
            if not np.isnan(cap_all[k:]).all():
                cap = cap_all[k:]
        if extra_cap is not None and not np.isnan(extra_cap[k:]).all():      # min of both caps (NaN = none)
            cap = extra_cap[k:] if cap is None else np.fmin(cap, extra_cap[k:])
        band = 0.0 if self.plan_to_nomination else self.band
        args = (self.b, k, soc, price[k:], self.avail[k:], self.base_export[k:], commit[k:], self.dt_h, self.wear,
                None if q_mask is None else q_mask[k:], nom, band, self.imb_price,
                None if ref_net is None else ref_net[k:], cap, band, self.cap_price)
        peak = self.peak[k:] if self.price_mode else None
        if not (self.margin > 0 and self.participate and q_mask is None and peak is not None and peak.any()):
            return solve(*args, peak=peak, prox=prox)
        anchor = self._anchor(args)
        plan = solve(*args, peak=peak, prox=prox, anchor=(anchor, self.margin))
        self._log_margin(k, args, peak, prox, anchor, plan)
        return plan

    def _anchor(self, args) -> np.ndarray:
        """D58: site net of the agent's own selfish plan for these inputs (no peak price, no prox), cached on them."""
        h = hashlib.sha1(repr((args[1], args[2], args[11], args[12])).encode())
        for a in (args[3], args[6], args[9], args[10], args[13], args[14]):
            h.update(b"-" if a is None else np.ascontiguousarray(a, float).tobytes())
        key = h.digest()
        if key not in self._anchors:
            if len(self._anchors) > 64:
                self._anchors.clear()
            p = solve(*args)
            self._anchors[key] = self.base_export[p.k0:] + p.d - p.c - p.x
        return self._anchors[key]

    def _log_margin(self, k, args, peak, prox, anchor, plan):
        """Log when the margin binds: the plan sits off its selfish anchor (the margin term costs something), or the
        margin held back a shift the m = 0 plan at the same step would make. Declined MWh is against that m = 0 plan;
        shifted MWh = sum |site net - anchor| x dt (both legs, as bench/d58_sites.py counts it)."""
        kmwh = self.dt_h / 1000.0
        dev = np.abs(self.base_export[plan.k0:] + plan.d - plan.c - plan.x - anchor)
        t0 = time.perf_counter()
        p0 = solve(*args, peak=peak, prox=prox)
        self.margin_compare_secs += time.perf_counter() - t0
        shifted, shifted0 = float(dev.sum() * kmwh), float(np.abs(
            self.base_export[p0.k0:] + p0.d - p0.c - p0.x - anchor).sum() * kmwh)
        if dev.max(initial=0.0) <= MARGIN_BIND_KW and shifted0 - shifted <= MARGIN_BIND_KW * kmwh:
            return
        self.margin_log.append({
            "k": k, "t_utc": self.iso[k], "posture": self.posture, "m_usd_mwh": self.margin,
            "shifted_mwh": shifted, "shifted_m0_mwh": shifted0, "declined_mwh": shifted0 - shifted,
            "margin_cost_usd": self.margin / 2 * shifted, "in_price_round": prox is not None,
            "final_round": self._final_round,
            "reason": f"posture {self.posture}: shifts only where the peak price beats lost day-ahead arbitrage by "
                      f"about {self.margin:g} $/MWh (forecast-risk margin, D58)"})

    def _allowed_net(self) -> np.ndarray:
        """The site net the exchange will hold this agent to outside award windows, per step (NaN = no limit): before
        any award, its latest nomination (what a first award would freeze); after, the frozen nomination plus
        awarded prep (prep is extra load, so it lowers the allowed net)."""
        if self.cap is None:
            return self.latest_sent.copy()
        return self.cap["base"] - self.cap["prep"]

    def _add_to_cap(self, t_award, award):
        """D34 (revised), mirroring the exchange: the first award freezes the latest nominated schedule; each award
        adds its window and prep, and extends the capped span to its window end + prep_hours."""
        win = self._mask(award["window"])
        if self.cap is None:
            self.cap = {"base": self.latest_sent.copy(), "from": self.idx[t_award], "until": self.idx[t_award],
                        "prep": np.zeros(self.T), "windows": np.zeros(self.T, bool)}
        c = self.cap
        c["windows"] |= win
        pr = award.get("prep")
        if pr:
            a = self.idx[pr["start_utc"]]
            vals = np.asarray(pr["kw"], float)[:max(self.T - a, 0)]
            c["prep"][a:a + len(vals)] += vals
        end = int(np.flatnonzero(win)[-1]) + 1
        c["until"] = max(c["until"], min(end + self.prep_steps, self.T))
        self._rebuild_load_cap()

    def _rebuild_load_cap(self):
        c = self.cap
        cap = np.full(self.T, np.nan)
        span = np.zeros(self.T, bool)
        span[c["from"]:c["until"]] = True
        ok = span & ~c["windows"] & ~np.isnan(c["base"])
        cap[ok] = -c["base"][ok] + c["prep"][ok]
        self.load_cap = cap

    def _bid(self, k, soc, price, only):
        """Offers for open needs in arrival order, each assuming the earlier ones win. `only` limits which needs
        get new offers; the others still count as commitments."""
        t, out = self.iso[k], []
        existing = self.commit.copy()
        for nid, need in self.needs.items():
            o = self.offers.get(nid)
            gated = need["gate_utc"] <= t          # between clearings at its gate: no new offer, kW only down
            if gated and o is None:
                continue
            if only is not None and nid not in only:
                if o and not self.independent_offers:
                    existing += o["kw"] * need["mask"]
                continue
            kw_cap = min(need["kw_needed"], self._budget_kw(need["mask"], existing))
            q, floor, prep = self._price_commitment(k, soc, price, need, existing, kw_cap)
            if gated and q > o["kw"]:
                q, floor, prep = self._price_commitment(k, soc, price, need, existing, o["kw"])
            prep_msg = self._prep_payload(prep) if self.send_prep else None
            if o is None and q >= self.min_offer_kw:
                payload = {"need_id": nid, "mode": "scheduled", "zone": need["zone"], "window": need["window"],
                           "kw": q, "floor_usd_mwh": floor, "standing": False}
                if prep_msg:
                    payload["prep"] = prep_msg
                raw = self._msg("offer", t, payload)
                self.offers[nid] = {"offer_id": M.decode(raw)["msg_id"], "kw": q, "floor": floor, "prep": prep_msg}
                out.append(raw)
            elif o is not None and q < self.min_offer_kw:
                out.append(self._msg("revise", t, {"offer_id": o["offer_id"], "withdraw": True}))
                del self.offers[nid]
            elif o is not None and (abs(q - o["kw"]) > REVISE_REL * o["kw"]
                                    or abs(floor - o["floor"]) > max(REVISE_REL * o["floor"], 0.01)
                                    or prep_msg != o.get("prep")):
                rev = {"offer_id": o["offer_id"], "kw": q, "floor_usd_mwh": floor}
                if self.send_prep:
                    rev["prep"] = prep_msg if prep_msg else {"start_utc": need["gate_utc"], "kw": [0.0]}
                out.append(self._msg("revise", t, rev))
                o.update(kw=q, floor=floor, prep=prep_msg)
            if nid in self.offers and not self.independent_offers:
                existing += self.offers[nid]["kw"] * need["mask"]
        return out

    def _budget_kw(self, mask, existing):
        """Most kW a new offer over `mask` can hold within the daily energy budget left (awards and other live
        offers already count). Each step counts on its own local date."""
        if self.daily_budget is None:
            return float("inf")
        used = existing             # awards + earlier live offers, kW per step (later offers assume this one wins)
        dates = np.asarray(self.local_dates)
        best = float("inf")
        for d in set(dates[mask]):
            on_d = dates == d
            left = self.daily_budget.get(d, 0.0) - used[on_d].sum() * self.dt_h
            best = min(best, max(left, 0.0) / ((mask & on_d).sum() * self.dt_h))
        return best

    def _prep_payload(self, prep: dict):
        if not prep:
            return None
        a, b = min(prep), max(prep)
        # rounded up to 0.1 kW: rounding down would leave the agent short of the charging it needs
        return {"start_utc": self.iso[a], "kw": [math.ceil(round(prep.get(j, 0.0) * 10, 6)) / 10 for j in range(a, b + 1)]}

    def _price_commitment(self, k, soc, price, need, existing, kw_needed):
        """Largest whole kW it can hold over the window (no more than the need asks for), its opportunity cost in
        $/MWh plus the premium, and the preparation it needs ({step: extra kW}). Capping at the need leaves
        capacity for the other layers. With firm_offers, everything is planned from the gate, from the SOC its
        current plan reaches there."""
        mask = need["mask"]
        g = max(self.idx[need["gate_utc"]], k) if self.firm_offers else k
        if g >= self.T or not mask[g:].any() or kw_needed < self.min_offer_kw:
            return 0.0, 0.0, {}
        s_g = soc
        if g > k:
            if self.plan is None or self.dirty:
                self.plan = self._solve(k, soc, price, self.commit)
                self.plan_s0, self.dirty = soc, False
            s_g = float(self.plan.s[g - self.plan.k0 - 1])
        qp = self._solve(g, s_g, price, existing, q_mask=mask)
        if qp is None:
            return 0.0, 0.0, {}
        q = float(math.floor(round(min(qp.q, kw_needed), 6)))
        if q < self.min_offer_kw:
            return q, 0.0, {}
        end = int(np.flatnonzero(mask)[-1]) + 1
        span = np.zeros(self.T, bool)
        span[g:min(end + self.prep_steps, self.T)] = True
        span &= ~mask
        ref, use_cap = None, True
        if self.send_prep:
            # Prep is measured against exactly what the exchange will enforce (the latest nomination, or the cap after
            # an award), and both plans are solved against that same reference instead of the cap, so the declared prep
            # is the load this offer adds beyond what is already allowed.
            allowed = self._allowed_net()
            ref = np.where(span, allowed, np.nan)
            use_cap = False
        room_cap = None
        if self.use_room and self.send_prep:        # D50: the offer's prep stays within the published room
            room_cap = np.where(span & ~np.isnan(self.room), -allowed + np.maximum(self.room, 0.0), np.nan)
        free = self._solve(g, s_g, price, existing, ref_net=ref, use_cap=use_cap)
        held = self._solve(g, s_g, price, existing + q * mask, ref_net=ref, use_cap=use_cap, extra_cap=room_cap)
        prep = {}
        if self.send_prep:
            held_net = self.base_export[g:] + held.d - held.c - held.x
            add = np.maximum(ref[g:] - held_net, 0.0)
            prep = {g + j: float(v) for j, v in enumerate(add) if span[g + j] and np.isfinite(v) and v >= 1.0}
        mwh = q * mask[g:].sum() * self.dt_h / 1000.0
        return q, round(max(held.cost - free.cost, 0.0) / mwh + self.premium, 4), prep
