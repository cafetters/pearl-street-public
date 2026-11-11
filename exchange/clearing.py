"""Uniform-price clearing on plain kW (M1), with an optional fit check on each offer (M2a). Accreditation and
pivotal mitigation arrive in M2b."""


def clear(kw_needed: float, price_cap: float, offers: list[dict]) -> tuple[list[tuple[dict, float]], float | None]:
    """Accept the cheapest offers until kw_needed is covered; the last one may be filled in part.

    offers: dicts with kw, floor_usd_mwh and seq (arrival order, breaks ties).
    Returns ([(offer, accepted_kw), ...], uniform price) where the price is the floor of the last accepted
    offer, paid to every winner. Offers priced above the cap are never accepted.
    """
    accepted, price, _ = clear_with(kw_needed, price_cap, offers)
    return accepted, price


def clear_with(kw_needed: float, price_cap: float, offers: list[dict], fits=None):
    """clear(), walking the merit order with an optional fits(offer, q) -> (kw that fits, reason). 0 skips the
    offer and the walk continues, so a cheaper offer can lose to a dearer one (as with block offers); less than q
    accepts that part. fits must record what it accepts, since later offers are checked against it. The uniform
    price comes from accepted offers only. Returns (accepted, price, skipped) with skipped = [(offer, reason)];
    an offer accepted in part also appears in skipped with its reason."""
    eligible = sorted((o for o in offers if o["kw"] > 0 and o["floor_usd_mwh"] <= price_cap),
                      key=lambda o: (o["floor_usd_mwh"], o["seq"]))
    accepted, skipped, left = [], [], kw_needed
    for o in eligible:
        if left <= 1e-9:
            break
        q = min(o["kw"], left)
        if fits is not None:
            q_fit, reason = fits(o, q)
            if q_fit <= 0:
                skipped.append((o, reason))
                continue
            if q_fit < q - 1e-9:
                skipped.append((o, reason))
                q = q_fit
        accepted.append((o, q))
        left -= q
    return accepted, (accepted[-1][0]["floor_usd_mwh"] if accepted else None), skipped
