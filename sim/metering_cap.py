"""The metering service's power for rule 1p's acting-alone reference (M5, owner, Nov 18).

Before D-a the reference planner used the mandate's registered max_kw. Under `reference_power_cap` (on by default since
D-a, owner, Nov 19; a config may set it false) it uses
min(registered max_kw, demonstrated power_kw), the power the metering service itself attested in asset_limits, so an
over-registration can't widen an agent's own reference even where the exchange let the registration through (D61:
defenses live in the exchange or the metering, not the interface)."""
from __future__ import annotations

# D-a (owner, Nov 19): both M5 defenses are on by default in every run. run_m1 applies these before it writes the
# header, so the header records the effective values even when a config omits them. The Exchange class keeps its own
# kwarg default off, for direct construction in unit tests; runs always pass the run value.
M5_DEFAULTS = {"register_power_check": True, "reference_power_cap": True}


def with_m5_defaults(cfg: dict) -> dict:
    """A copy of cfg with D-a's defaults filled in where the config doesn't set them."""
    return {**M5_DEFAULTS, **cfg}


def reference_kw(log, cfg: dict, demonstrated_kw: dict) -> dict[str, float]:
    """agent id -> kW for the 1p reference. `log` holds the bus's routed messages (register payloads in full);
    demonstrated_kw maps agent id -> the power_kw the metering service attested for its battery."""
    reg = {m["sender"]: float(m["payload"]["mandate"]["max_kw"]) for m in log if m["msg_type"] == "register"}
    if not cfg.get("reference_power_cap", M5_DEFAULTS["reference_power_cap"]):
        return reg
    return {a: min(kw, float(demonstrated_kw[a])) if a in demonstrated_kw else kw for a, kw in reg.items()}
