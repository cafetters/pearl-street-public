"""Rule 10 with D101: which roster agents a check covers, and how an agent out of the market counts as covered.

A run summary's header lists every agent in `roster` and (from D101 on) each agent's status in `roster_status`:
"active", "excluded: registration rejected", "excluded: never registered" or "excluded: data centers off (setting)"
(D111). A saved run without `roster_status` predates D101: every roster agent was active. An excluded agent counts as
covered only if the check reports it as excluded, so:

  coverage(header, ids)     the check covers ids; returns the excluded agents to report and the roster agents missing
                            from both (the check fails if any)
  require_all_active(...)   for a check not built for exclusions: fails closed when the header lists any
  guard(header, tool, path) the same for a summary or trace header (a trace header lists setting exclusions in
                            `data_centers_excluded`; registration exclusions are only in the run summary, so with a
                            path the run folder's summary.json is checked too)
"""
from __future__ import annotations

import json
from pathlib import Path

ACTIVE = "active"
STATUSES = ("active", "excluded: registration rejected", "excluded: never registered",
            "excluded: data centers off (setting)")


class RosterError(ValueError):
    pass


def statuses(header: dict) -> dict[str, str]:
    """Every roster agent's status; all active for a run that predates D101. Fails on an unknown status, a status for
    an agent not on the roster, or a roster agent without one."""
    roster = [str(a) for a in (header.get("roster") or [])]
    if not roster:
        raise RosterError("no roster in the header")
    st = header.get("roster_status")
    if st is None:
        return {a: ACTIVE for a in roster}
    bad = {a: s for a, s in st.items() if s not in STATUSES}
    if bad:
        raise RosterError(f"unknown roster_status values: {bad}")
    extra, lacking = sorted(set(st) - set(roster)), sorted(set(roster) - set(st))
    if extra or lacking:
        raise RosterError(f"roster_status does not match the roster: extra {extra}, missing {lacking}")
    return {a: st[a] for a in roster}


def excluded(header: dict) -> dict[str, str]:
    return {a: s for a, s in statuses(header).items() if s != ACTIVE}


def coverage(header: dict, ids) -> dict:
    """ids: the agents the check covered. missing: roster agents neither covered nor reported excluded."""
    exc = excluded(header)
    ids = {str(x) for x in ids}
    return {"excluded": exc, "missing": sorted(set(statuses(header)) - ids - set(exc))}


def require_all_active(header: dict, tool: str) -> None:
    exc = excluded(header)
    if exc:
        raise RosterError(f"{tool} is not built for agents out of the market (D101); excluded here: {exc}")


def guard(header: dict | None, tool: str, path=None) -> None:
    """For a check not built for exclusions: fails closed on any agent out of the market, from a summary or trace
    header and, given a run folder or a path inside one (a trace), from that run's summary.json (the run folder is the
    path itself or one of its two nearest parents). A header or summary without roster_status predates D101 and
    passes."""
    h = header or {}
    if h.get("data_centers_excluded"):
        raise RosterError(f"{tool} is not built for agents out of the market (D101); data centers off (setting): "
                          f"{h['data_centers_excluded']}")
    if h.get("roster_status") is not None:
        require_all_active(h, tool)
    if path is not None:
        p = Path(path)
        for d in [p, *p.parents][:3]:
            f = d / "summary.json"
            if f.is_file():
                sh = (json.loads(f.read_text(encoding="utf-8")) or {}).get("header") or {}
                if sh.get("roster_status") is not None:
                    require_all_active(sh, tool)
                break
