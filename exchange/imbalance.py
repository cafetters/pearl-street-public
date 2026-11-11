"""Imbalance against a binding nomination (M1.1, D32): deviation beyond a free band, priced at the zone's scarcity price."""


def band_kw(max_kw: float, pct: float, min_kw: float) -> float:
    return max(pct * max_kw, min_kw)


def imbalance(nominated: dict[str, float], metered: dict[str, float], steps: list[str], step_h: float,
              band: float, price_usd_mwh: float) -> dict:
    """Symmetric: |metered net - nominated| per step, charged only beyond the band. A missing reading counts as 0 kW."""
    raw = excess = 0.0
    for t in steps:
        if t not in nominated:
            continue
        dev = abs(metered.get(t, 0.0) - nominated[t])
        raw += dev * step_h
        excess += max(dev - band, 0.0) * step_h
    return {"raw_deviation_kwh": raw, "deviation_kwh": excess, "charge_usd": price_usd_mwh * excess / 1000.0}
