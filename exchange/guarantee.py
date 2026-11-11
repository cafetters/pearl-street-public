"""
D62 forecast-risk guarantee (Leaning; docs/plans/d62_guarantee_report.md): the arithmetic, shared by the exchange
(core.py, in-run) and the offline tool (bench/d62_guarantee.py). Pure functions on per-step lists; no data files.

The exchange pays the shortfall of a modeled m = 0 price response P* against a modeled acting-alone path Q*, each one
continuous path built from 1p's inputs only (sim/reference.py, ModeledPaths). It never pays on the agent's actual
result, so no agent's marginal price is zeroed. Span g of a window runs from binding call g's deadline to the next
binding deadline (the last to the slice end), which are exactly the steps call g settles.
"""
MIN_SHIFT_KWH = 1.0           # modeled shift below this in an agent-quarter: the response measure is 0/0 (eligible)


def spans(deadlines: list[int], T: int) -> list[tuple[int, int]]:
    """[d_g, d_{g+1}) per binding call, the last to T. deadlines: step indices, strictly increasing."""
    if any(b <= a for a, b in zip(deadlines, deadlines[1:])) or not deadlines or deadlines[-1] >= T:
        raise ValueError(f"deadlines must be increasing step indices below {T}: {deadlines}")
    return list(zip(deadlines, list(deadlines[1:]) + [T]))


def span_shortfall(rt, p, q_ref, p_net, p_dis, q_net, q_dis, p_soc_end: float, q_soc_end: float, wear: float,
                   end_usd_kwh: float, sp: list[tuple[int, int]], K: float) -> list[float]:
    """Per span: E(Q*) - [E(P*) + p (P* - Q_g) K], with E(x) = RT x K - wear K discharge, summed over the span's steps.
    The settlement term counts only priced steps (p > 0); q_ref there is 1p's reference of the call settling that step.
    The last span adds (soc_end(Q*) - soc_end(P*)) x end_usd_kwh. Site nets in kW (export positive), $/MWh prices."""
    out = []
    for g, (a, b) in enumerate(sp):
        energy = sum(rt[t] * (q_net[t] - p_net[t]) for t in range(a, b)) * K
        wear_d = wear * K * sum(q_dis[t] - p_dis[t] for t in range(a, b))
        settle = sum(p[t] * (p_net[t] - q_ref[t]) for t in range(a, b) if p[t] > 0) * K
        v = energy - wear_d - settle
        if g == len(sp) - 1:
            v += (q_soc_end - p_soc_end) * end_usd_kwh
        out.append(v)
    return out


def response_kwh(p, q_ref, p_net, net, step_h: float) -> tuple[float, float]:
    """D65's response measure on priced steps: modeled shift sum max(P* - Q_g, 0) and the part of it the meter shows
    delivered, sum min(max(N - Q_g, 0), max(P* - Q_g, 0)), both in kWh."""
    shift = got = 0.0
    for t in range(len(p)):
        if p[t] > 0:
            d = max(p_net[t] - q_ref[t], 0.0)
            shift += d
            got += min(max(net[t] - q_ref[t], 0.0), d)
    return shift * step_h, got * step_h


def share(shift_kwh: float, delivered_kwh: float, min_kwh: float = MIN_SHIFT_KWH) -> float | None:
    """Delivered share of the modeled shift, or None (0/0: no modeled shift, eligible) below min_kwh."""
    return None if shift_kwh < min_kwh else delivered_kwh / shift_kwh


def separation(responders: dict, non_responders: dict) -> dict:
    """The pre-registered threshold rule. Each dict maps an agent-quarter key to its share or None (0/0, left out of
    both groups). PASS when every responder share exceeds every non-responder share; the threshold is the midpoint of
    (highest non-responder, lowest responder). Otherwise FAIL_separation with the overlapping agent-quarters."""
    r = {k: v for k, v in responders.items() if v is not None}
    n = {k: v for k, v in non_responders.items() if v is not None}
    out = {"excluded_0_0": {"responders": len(responders) - len(r), "non_responders": len(non_responders) - len(n)},
           "counted": {"responders": len(r), "non_responders": len(n)}}
    if not r or not n:
        return {**out, "pass": False, "threshold": None, "reason": "an empty group", "overlap": {}}
    lo, hi = min(r.values()), max(n.values())
    out.update({"lowest_responder": lo, "highest_non_responder": hi})
    if lo > hi:
        return {**out, "pass": True, "threshold": (lo + hi) / 2, "overlap": {}}
    return {**out, "pass": False, "threshold": None,
            "overlap": {"responders": {str(k): v for k, v in r.items() if v <= hi},
                        "non_responders": {str(k): v for k, v in n.items() if v >= lo}}}


def eligible(s: float | None, threshold: float) -> bool:
    return s is None or s >= threshold


def ledger(quarters: list[tuple[int, int, float, bool]]) -> dict:
    """Quarterly payout with an annual true-up and clawback by netting. quarters: (year, quarter, shortfall $,
    eligible), in any order. An eligible quarter adds its shortfall; an ineligible one adds only a gain (min(s, 0)):
    gains count, losses don't. At each quarter end the balance is max(0, year-to-date sum): an increase is paid, a
    decrease clawed back, never more than was paid that year. Balances reset each January 1."""
    rows, gross, clawed, peak = [], 0.0, 0.0, 0.0
    ytd = balance = 0.0
    year = None
    for y, q, s, ok in sorted(quarters, key=lambda r: (r[0], r[1])):
        if y != year:
            year, ytd, balance = y, 0.0, 0.0
        ytd += s if ok else min(s, 0.0)
        target = max(0.0, ytd)
        paid, back = max(target - balance, 0.0), max(balance - target, 0.0)
        gross, clawed, balance = gross + paid, clawed + back, target
        peak = max(peak, balance)
        rows.append({"year": y, "quarter": q, "shortfall_usd": s, "eligible": ok, "paid_usd": paid,
                     "clawed_back_usd": back, "balance_usd": balance})
    return {"gross_paid_usd": gross, "clawed_back_usd": clawed, "net_usd": gross - clawed,
            "max_outstanding_usd": peak, "by_quarter": rows}
