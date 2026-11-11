"""D65 delivery record ("credit score") for arm C: pure functions over messages and meters only (D1).

The record is kept per asset and its liable legal entity (the register message's legal_entity), not per agent
software, so a new agent that registers the same asset for the same entity continues the record. It limits upside
and never charges (charges stay with imbalance); the credit line that would use it, and D65's response measure, are
not built (the response measure is invalid as defined after D62's FAIL_separation; RESPONSE_HOOK marks its place).
"""
from collections import defaultdict

RESPONSE_HOOK = None        # D65 response measure: left out until D62 v3 defines it (owner, Oct 31)
ACTIVITY_TOL_KW = 1.0       # battery meter |kW| above this during a declared outage is activity


def season(local_date: str) -> str:
    m = int(local_date[5:7])
    return {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM", 6: "JJA", 7: "JJA", 8: "JJA"}.get(m, "SON")


def nomination_delivery(in_force: dict[str, float], metered: dict[str, float], band: float, step_h: float) -> dict:
    """Binding nominations delivered within the D33 band. Each step weighs max(|nominated kW|, band) x step, in kWh,
    where band is the D51 free band max(5% x registered power, 50 kW): a step near zero still counts as a band's worth
    of energy, so one unit covers every step and no step-count fallback is needed. A missing reading counts as 0 kW,
    as for imbalance."""
    n = k = 0
    w = wk = 0.0
    for t, nom in in_force.items():
        ok = abs(metered.get(t, 0.0) - nom) <= band + 1e-9
        e = max(abs(nom), band) * step_h
        n += 1
        k += ok
        w += e
        wk += e if ok else 0.0
    return {"binding_steps": n, "steps_within_band": k, "weight_kwh": w, "weight_kwh_within_band": wk,
            "share_within_band": (wk / w) if w > 0.0 else None}


class Ledger:
    """Accumulates each (asset key, legal entity) record across the run (and, with D60 snapshots, across runs)."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = defaultdict(lambda: {
            "binding_steps": 0, "steps_within_band": 0, "weight_kwh": 0.0, "weight_kwh_within_band": 0.0,
            "binding_calls_missed": 0, "days_declared": 0, "declared_power_share_sum": 0.0, "planned_outage_days": 0,
            "forced_outages": 0, "late_outages": 0, "outage_activity_steps": 0, "below_reserve_steps": 0})
        self.refusals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))   # entity -> reason -> n

    def add_refusal(self, entity: str, reason: str):
        """A message the bus refused, counted against the legal entity of the agent it came from (refusal_notice)."""
        self.refusals[entity][reason] += 1

    def refusals_view(self, entity: str) -> dict:
        return dict(sorted(self.refusals[entity].items()))

    def add(self, key: tuple[str, str], **vals):
        r = self.rows[key]
        for k, v in vals.items():
            r[k] = r.get(k, 0) + v          # D83's counts exist only in records kept under declared_reference

    def view(self, key: tuple[str, str]) -> dict:
        r = dict(self.rows[key])
        r["share_within_band"] = r["weight_kwh_within_band"] / r["weight_kwh"] if r["weight_kwh"] > 0.0 else None
        r["mean_declared_power_share"] = (r.pop("declared_power_share_sum") / r["days_declared"]
                                          if r["days_declared"] else None)
        r["response"] = RESPONSE_HOOK
        return r
