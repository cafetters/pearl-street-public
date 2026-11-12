"""D93 model assignment (owner, Nov 11-12): each agent draws its model once per run.

u = sha256(f"{seed}:{agent_id}") / 2^256, read against the cumulative weights of the list for the agent's role. There
is no sequential RNG stream, so adding or removing an attacker (or any agent) never moves another agent's draw; an
attacker and its honest twin share u and read it against different lists. Per-model results from this draw are report
only (model is mixed with site); Q-N7 uses paired runs (all honest agents on X vs all on Y).
"""
from __future__ import annotations

import hashlib
import json

# Owner, Nov 12 (decision 3). gpt-6-luna is priced (OpenAI pricing page, checked 2026-11-12) but refused for live calls
# until the owner confirms the OpenAI key route (D96); the fake backend covers it.
DEFAULT_HONEST = [{"provider": "anthropic", "model": "claude-haiku-5-5", "weight": 0.6},
                  {"provider": "openai", "model": "gpt-6-luna", "weight": 0.2},
                  {"provider": "anthropic", "model": "claude-sonnet-5-5", "weight": 0.2}]
DEFAULT_ATTACKER = [{"provider": "anthropic", "model": "claude-sonnet-5-5", "weight": 0.9},
                    {"provider": "anthropic", "model": "claude-opus-5-5", "weight": 0.1}]


def u_of(seed, agent_id: str) -> float:
    h = hashlib.sha256(f"{seed}:{agent_id}".encode()).digest()
    return int.from_bytes(h, "big") / float(1 << 256)


def check_list(models) -> list[dict]:
    if not isinstance(models, list) or not models:
        raise ValueError("model list must be a non-empty list")
    out = []
    for m in models:
        if not isinstance(m, dict) or not all(k in m for k in ("provider", "model", "weight")):
            raise ValueError(f"model entry needs provider, model and weight: {m!r}")
        w = float(m["weight"])
        if not w >= 0.0:
            raise ValueError(f"negative or NaN weight: {m!r}")
        out.append({"provider": str(m["provider"]), "model": str(m["model"]), "weight": w})
    if sum(m["weight"] for m in out) <= 0.0:
        raise ValueError("model weights sum to zero")
    return out


def pick(models: list[dict], u: float) -> dict:
    total = sum(m["weight"] for m in models)
    acc = 0.0
    for m in models:
        if m["weight"] <= 0.0:
            continue
        acc += m["weight"] / total
        if u < acc:
            return m
    return [m for m in models if m["weight"] > 0.0][-1]


def assign(roster, seed, attackers, honest, attacker) -> dict:
    honest, attacker = check_list(honest), check_list(attacker)
    out = {}
    for a in roster:
        role = "attacker" if a in attackers else "honest"
        u = u_of(seed, a)
        m = pick(attacker if role == "attacker" else honest, u)
        out[a] = {"role": role, "provider": m["provider"], "model": m["model"], "u": u}
    return out


def header_block(honest, attacker, draws) -> dict:
    lists = {"honest_models": check_list(honest), "attacker_models": check_list(attacker)}
    sha = hashlib.sha256(json.dumps(lists, sort_keys=True).encode()).hexdigest()
    return {**lists, "weights_sha256": sha, "draw_rule": "u = sha256(seed:agent_id)/2^256 vs the role's weights",
            "draws": {a: {k: v for k, v in d.items()} for a, d in draws.items()}}
