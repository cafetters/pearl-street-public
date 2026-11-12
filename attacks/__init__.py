"""Attacks: wrappers around honest agents (D61). A run config lists them under `attackers`; the header records them."""


def _local_dates(agent, cfg):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cfg.get("slice_tz") or "America/Chicago")     # run_m1's local time (TZ)
    return [datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(tz).date().isoformat() for t in agent.iso]


def wrap_attacker(spec: dict, agent, cfg: dict):
    """Wrap an honest agent in the attack `spec` names."""
    kind = spec.get("type")
    tz = cfg.get("slice_tz") or "America/Chicago"
    if kind == "impersonate":
        from attacks.impersonate import Impersonator
        return Impersonator(agent, victim=str(spec["victim"]), operator_id=cfg["operator_id"])
    if kind == "renege":
        from attacks.renege import Reneger
        mode = str(spec.get("mode", "full"))
        if mode == "zero_b":                # owner, Nov 5: nominate at A, withdraw at a zero-price B
            from attacks.renege import ZeroPriceBReneger
            return ZeroPriceBReneger(agent)
        local = _local_dates(agent, cfg) if mode == "day" else None
        return Reneger(agent, mode=mode, day=None if spec.get("day") is None else str(spec["day"]), local_dates=local)
    if kind == "silent":                    # M3 step 6: silent, and "silent, then deviate"
        from attacks.silent import Silent
        day = None if spec.get("day") is None else str(spec["day"])
        return Silent(agent, mode=str(spec.get("mode", "selfish")), day=day,
                      local_dates=_local_dates(agent, cfg) if day else None, drop=str(spec.get("drop", "nominations")))
    if kind == "outage":                    # M3 step 6: late or false outage, (a) stop and (b) run
        from attacks.declare import LateOutage
        return LateOutage(agent, mode=str(spec["mode"]), day=str(spec["day"]), tz=tz)
    if kind == "misdeclare":                # M3 step 6: less power or more reserve, then runs fully
        from attacks.declare import Misdeclare
        return Misdeclare(agent, mode=str(spec["mode"]), tz=tz,
                          publish_hour=int(cfg.get("price_publish_hour_local", 12)), frac=spec.get("frac"))
    if kind == "infeasible_declare":        # owner, Nov 4: declarations that would leave 1p infeasible
        from attacks.declare import InfeasibleDeclare
        return InfeasibleDeclare(agent, tz=tz, order=str(spec.get("order", "outage_first")))
    if kind == "round_liar":                # M4 REP comparison (D24): misreports in non-binding price rounds only
        from attacks.round_liar import RoundLiar
        return RoundLiar(agent, mode=str(spec.get("mode", "overstate")))
    raise ValueError(f"unknown attack type {kind!r}")
