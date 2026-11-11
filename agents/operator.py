"""
Grid-operator agent. Turns its load forecast into scheduled needs, posted the day before.

Round 1 (M1): for each local day D it targets (forecast peak - headroom) and posts layered needs, a staircase
that follows the forecast: each layer starts at target + j x layer and covers the contiguous steps where the
forecast is above that level. Needs for day D go up at `post_hour` local on D-1 (the first day's at the first
step), and their gate closes `gate_lead_steps` later. Windows never start before the gate. The forecast treats
participating batteries as idle.

Rounds 2.. (M1.1, D32): after each round clears, it calls for nominations. At the deadline it gets only the zone
total of the agents' planned site net, re-forecasts with it, and posts residual needs wherever the re-forecast is
still above the same target. With rounds="info", rounds are scheduled by information, not count: residual rounds
continue until one closes after the agents' price-informed nominations (day-ahead prices for D publish at
`info_hour` on D-1; the first day's are known at the start), with at least one residual round. After the final
awards, one more call is binding and covers the rest of the horizon, so it includes every award made so far and
takes over from earlier binding nominations for the steps it covers.

M2a (prep_clearing): every call covers the rest of the horizon, so prep can be checked wherever it lands, and the
operator sends the exchange its target as zone_target: the most net load the zone's participants may draw in
total at each step (the system target minus its own base forecast: idle-battery forecast plus the participants'
idle site net). It sends targets for the full horizon with round 1 and again after each re-forecast.

nomination_span (owner, Nov 5; changes the full-horizon rule above, which came in with D37 and D32): how far a
full call's window reaches. "horizon" (the default; every existing config runs unchanged): to the end of the slice.
"operating_day": to the end of the local operating day the call's needs are for. A number: that many hours from the
deadline. Windows are clipped to the slice. A span shorter than the time between binding calls leaves steps that no
binding nomination covers (D33: no imbalance there). Calls that were already day-only (M1.1 without prep_clearing or
price_mode) are unchanged.
Arm A (prep_clearing or no_rebound) refuses a span other than "horizon" (owner, Nov 5; sim/run_m1.py): D37's prep
and D34's cap reach a need's window end + 24 h and need a pre-award nomination there.

D42 (priorities): every need carries a priority (1 = cleared first at its gate): highest forecast exceedance first,
where exceedance = max over the window of (forecast or re-forecast - day target); ties go to the earlier window
start, then post order.
"""
import math

import numpy as np
import pandas as pd

from exchange import messages as M

NOMINATION_SPANS = ("horizon", "operating_day")


def parse_nomination_span(value) -> tuple[str, float | None]:
    """nomination_span -> ("horizon", None), ("operating_day", None) or ("hours", h). Anything else is refused."""
    if isinstance(value, str) and value in NOMINATION_SPANS:
        return value, None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return "hours", float(value)
    raise ValueError(f"nomination_span must be 'horizon', 'operating_day' or a positive number of hours, not {value!r}")


def layers_above(fc: np.ndarray, steps: np.ndarray, target: float, layer_kw: float) -> list[tuple[int, int, float]]:
    """(first step, last step + 1, kW) for each need covering the forecast above `target` over the given steps."""
    if len(steps) == 0:
        return []
    peak = fc[steps].max()
    out, j = [], 0
    while target + j * layer_kw < peak - 1e-9:
        edge = target + j * layer_kw
        kw = min(layer_kw, peak - edge)
        above = steps[fc[steps] > edge]
        runs = np.split(above, np.flatnonzero(np.diff(above) != 1) + 1)
        out += [(int(r[0]), int(r[-1]) + 1, float(kw)) for r in runs if len(r)]
        j += 1
    return out


def layers(fc: np.ndarray, steps: np.ndarray, headroom_kw: float, layer_kw: float) -> list[tuple[int, int, float]]:
    """Round-1 layers: target = forecast peak over the steps - headroom."""
    if len(steps) == 0:
        return []
    return layers_above(fc, steps, fc[steps].max() - headroom_kw, layer_kw)


class GridOperator:
    def __init__(self, op_id: str, clock_iso: list[str], end_iso: str, ts_local: pd.DatetimeIndex, run_id: str,
                 zone: str, headroom_kw: float, layer_kw: float, price_cap: float, post_hour: int = 9,
                 gate_lead_steps: int = 4, rounds: int | str = 1, nominations: bool = False, nom_lead_steps: int = 4,
                 idle_net=None, info_hour: int = 12, prep_clearing: bool = False, priorities: bool = False,
                 price_mode: bool = False, nomination_span: str | float = "horizon"):
        """idle_net: the participants' total site net (kW per step) with batteries idle, part of the operator's own
        forecast. Needed to turn the nominated aggregate into a re-forecast.
        rounds: a count, or "info" to schedule residual rounds by when price-informed nominations exist.
        price_mode (D53, arm C): no needs. The zone target goes to the exchange at the first post, calls cover the
        rest of the horizon, and price_round_steps() says where the simulator runs price rounds."""
        if not nominations and rounds != 1:
            raise ValueError("more than one round needs nominations")
        if rounds != "info" and not (isinstance(rounds, int) and rounds >= 1):
            raise ValueError("rounds must be a positive count or 'info'")
        self.id, self.iso, self.end_iso, self.run_id, self.zone = op_id, clock_iso, end_iso, run_id, zone
        self.headroom_kw, self.layer_kw, self.price_cap = headroom_kw, layer_kw, price_cap
        self.nominations, self.idle_net = nominations, idle_net
        self.prep_clearing, self.priorities = prep_clearing, priorities
        self.price_mode = price_mode
        self.span_kind, self.span_hours = parse_nomination_span(nomination_span)
        self.step_h = (ts_local[1] - ts_local[0]).total_seconds() / 3600.0 if len(ts_local) > 1 else 0.25
        T = len(clock_iso)
        days = ts_local.normalize()
        self.days = []
        for j, day in enumerate(days.unique()):
            steps = np.flatnonzero(days == day)
            post_at = day - pd.Timedelta(days=1) + pd.Timedelta(hours=post_hour)
            later = np.flatnonzero(ts_local >= post_at)
            post = 0 if j == 0 or post_at < ts_local[0] or not len(later) else int(later[0])
            gates = [post + gate_lead_steps]
            calls = [(post, gates[0], False)] if nominations else []     # (sent at, deadline, binding)
            if rounds == "info":
                pub_at = day - pd.Timedelta(days=1) + pd.Timedelta(hours=info_hour)
                pub = np.flatnonzero(ts_local >= pub_at)
                info = 0 if j == 0 or pub_at < ts_local[0] or not len(pub) else int(pub[0])
                while True:
                    deadline = gates[-1] + nom_lead_steps
                    calls.append((gates[-1], deadline, False))
                    gates.append(deadline + gate_lead_steps)
                    if deadline > info or deadline >= T:      # nominations made after the prices published
                        break
            else:
                for _ in range(1, rounds):
                    deadline = gates[-1] + nom_lead_steps
                    calls.append((gates[-1], deadline, False))
                    gates.append(deadline + gate_lead_steps)
            if nominations:
                calls.append((gates[-1], gates[-1] + nom_lead_steps, True))
            self.days.append({"post": post, "gates": gates, "calls": calls, "steps": steps, "target": None,
                              "info": info if rounds == "info" else None})
        self.T = T
        self.aggregates: dict[str, dict] = {}     # call msg_id -> aggregate payload
        self._call_ids: dict[tuple[int, int], str] = {}
        self.inbox: list[str] = []
        self._n = 0

    def price_round_steps(self) -> dict[int, tuple[int, bool]]:
        """D53: step -> (deadline, binding) for each call that gets price rounds; the rounds run in the step before
        its deadline. A call qualifies if it closes after its day's day-ahead prices publish and its window is not
        still under an earlier binding lock: before the first binding call any call, after it only binding calls
        (D46 frees the steps a binding call takes over once it is announced; a non-binding call's are still locked)."""
        out, bound = {}, False
        for d in self.days:
            for _, deadline, binding in d["calls"]:
                after_info = d["info"] is None or deadline > d["info"]
                if after_info and (binding or not bound) and 1 <= deadline < self.T:
                    out[deadline - 1] = (deadline, binding)
                bound = bound or binding
        return out

    # kept for M1 callers and tests
    @property
    def schedule(self):
        return [(d["post"], d["gates"][0], d["steps"]) for d in self.days]

    def receive(self, raw: str):
        self.inbox.append(raw)

    def _send(self, msg_type, k, payload):
        self._n += 1
        raw = M.encode(M.make(msg_type, self.id, "exchange", self.iso[k], payload, f"{self.id}:{self._n}", self.run_id))
        return raw, M.decode(raw)["msg_id"]

    def _window(self, a, b):
        return {"start_utc": self.iso[a], "end_utc": self.iso[b] if b < self.T else self.end_iso}

    def _needs(self, k, gate, needs, rnd, fc=None, target=None):
        out = []
        prio = {}
        if self.priorities and needs and fc is not None and target is not None:
            exceed = [float((fc[a:b] - target).max()) for a, b, _ in needs]
            order = sorted(range(len(needs)), key=lambda i: (-exceed[i], needs[i][0], i))
            prio = {i: r + 1 for r, i in enumerate(order)}
        for i, (a, b, kw) in enumerate(needs):
            payload = {"zone": self.zone, "window": self._window(a, b), "kw_needed": kw, "mode": "scheduled",
                       "price_cap": self.price_cap, "gate_utc": self.iso[gate], "round": rnd}
            if i in prio:
                payload["priority"] = prio[i]
            out.append(self._send("post_need", k, payload)[0])
        return out

    def system_target(self) -> np.ndarray:
        """System net-load target per step (kW): each day's target from its first gate on; NaN before that."""
        out = np.full(self.T, np.nan)
        for d in self.days:
            if d["target"] is not None:
                st = d["steps"][d["steps"] >= d["gates"][0]]
                out[st] = d["target"]
        return out

    def _zone_target(self, k, forecast):
        """zone_target from the first day's gate to the end of the horizon."""
        for d in self.days:
            if d["target"] is None and d["gates"][0] < self.T:
                eligible = d["steps"][d["steps"] >= d["gates"][0]]
                if len(eligible):
                    d["target"] = forecast[eligible].max() - self.headroom_kw
        sys_t = self.system_target()
        start = self.days[0]["gates"][0]
        if start >= self.T:
            return []
        steps = np.arange(start, self.T)
        base = forecast[steps] + self.idle_net[steps]
        tk = sys_t[steps] - base
        tk = np.where(np.isnan(tk), np.inf, tk)          # no day target yet: unconstrained (not sent as NaN)
        if np.isinf(tk).any():
            steps, tk = steps[~np.isinf(tk)], tk[~np.isinf(tk)]
        if not len(steps):
            return []
        payload = {"zone": self.zone, "window": self._window(int(steps[0]), int(steps[-1]) + 1),
                   "target_kw": [round(float(v), 3) for v in tk]}
        return [self._send("zone_target", k, payload)[0]]

    def act(self, k: int, forecast: np.ndarray) -> list[str]:
        for raw in self.inbox:
            m = M.decode(raw)
            if m.get("sender") == "exchange" and m["msg_type"] == "nomination_aggregate":     # D69 rule 2
                self.aggregates[m["payload"]["call_id"]] = m["payload"]
        self.inbox.clear()       # awards and settlements are summarized by the simulator from the trace
        out = []
        for j, day in enumerate(self.days):
            gates, steps = day["gates"], day["steps"]
            if day["post"] == k and gates[0] < self.T:
                eligible = steps[steps >= gates[0]]
                if len(eligible):
                    day["target"] = forecast[eligible].max() - self.headroom_kw
                if (self.prep_clearing or self.price_mode) and j == 0:
                    out += self._zone_target(k, forecast)
                if not self.price_mode:
                    out += self._needs(k, gates[0], layers(forecast, eligible, self.headroom_kw, self.layer_kw), 1,
                                       forecast, day["target"])
            for r, (sent_at, deadline, binding) in enumerate(day["calls"]):
                if sent_at == k and deadline < self.T:
                    # binding: the rest of the horizon, so it includes next-day awards and preparation for them
                    full = binding or self.prep_clearing or self.price_mode
                    win_steps = (np.arange(deadline, self._span_end(deadline, steps)) if full
                                 else steps[steps >= deadline])
                    if len(win_steps):
                        raw, cid = self._send("call_nominations", k, {
                            "zone": self.zone, "window": self._window(win_steps[0], win_steps[-1] + 1),
                            "deadline_utc": self.iso[deadline], "binding": binding, "round": r + 1})
                        self._call_ids[(j, r)] = cid
                        out.append(raw)
                # rounds 2..: residual needs once this round's call closes
                if deadline == k and not binding and r >= 1 and r < len(gates) and gates[r] < self.T \
                        and not self.price_mode:
                    agg = self.aggregates.get(self._call_ids.get((j, r)))
                    if agg is None or day["target"] is None:
                        continue
                    refc = self.reforecast(forecast, agg)
                    eligible = steps[steps >= gates[r]]
                    if self.prep_clearing:
                        out += self._zone_target(k, forecast)
                    out += self._needs(k, gates[r], layers_above(refc, eligible, day["target"], self.layer_kw), r + 1,
                                       refc, day["target"])
        return out

    def _span_end(self, deadline: int, day_steps: np.ndarray) -> int:
        """End step (exclusive) of a full call's window under nomination_span; day_steps are the call's operating
        day's steps."""
        if self.span_kind == "horizon":
            return self.T
        if self.span_kind == "operating_day":
            end = int(day_steps[-1]) + 1
        else:
            end = deadline + max(1, int(np.ceil(self.span_hours / self.step_h - 1e-9)))
        return min(end, self.T)

    def reforecast(self, forecast: np.ndarray, agg: dict) -> np.ndarray:
        """Idle-battery forecast, corrected by how far the nominated site net departs from idle."""
        pos = {t: i for i, t in enumerate(self.iso)}
        a = pos[agg["window"]["start_utc"]]
        out = forecast.copy()
        idx = np.arange(a, a + len(agg["kw"]))
        out[idx] = forecast[idx] - (np.asarray(agg["kw"]) - self.idle_net[idx])
        return out
