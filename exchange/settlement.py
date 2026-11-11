"""Meter-verified settlement of plain-kW awards (M1)."""


def settle_award(award: dict, asset_awards: list[dict], meter: dict[str, float], steps: list[str],
                 step_h: float, scarcity_usd_mwh: float) -> dict:
    """Delivered kW, credit and charge for one award.

    At each step of the window the asset's discharge (meter_kw, + = discharging) is split pro rata across all
    of that asset's awards active at that step. A missing reading counts as zero delivery.
    Credit = price x delivered kW x hours. Shortfall is charged at the scarcity price.
    """
    kw = award["accredited_kw"]
    delivered = []
    for t in steps:
        total = sum(a["accredited_kw"] for a in asset_awards if t in a["_steps"])
        m = max(meter.get(t, 0.0), 0.0)
        delivered.append(kw * min(1.0, m / total) if total > 0 else 0.0)
    delivered_kw = sum(delivered) / len(steps)
    hours = len(steps) * step_h
    return {
        "delivered_kw": delivered_kw,
        "delivered_kwh": delivered_kw * hours,
        "awarded_kwh": kw * hours,
        "credit_usd": award["price_usd_mwh"] * delivered_kw * hours / 1000.0,
        "charge_usd": scarcity_usd_mwh * max(kw - delivered_kw, 0.0) * hours / 1000.0,
    }
