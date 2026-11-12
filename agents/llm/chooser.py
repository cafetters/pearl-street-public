"""The LLM chooser (M4) and its run-level side: backend, shared cache (D95), spend guard (D94), model draws (D93) and
the call log. The LLM picks a posture label; code validates it (agents/strategy.py) and maps it to numbers (D12)."""
from __future__ import annotations

import json
from collections import defaultdict

from agents.llm import assign as AS
from agents.llm import prompt_builder as PB
from agents.llm import spend as SP
from agents.llm.backends import FakeAdversarial, FakeBackend, Request
from agents.llm.respcache import CacheMiss, ResponseCache, redact
from agents.strategy import MENU_VERSION, Decision

DEFAULT_EFFORT, DEFAULT_MAX_TOKENS = "low", 2048
RATIONALE_MAX = 300


def make_backend(name: str):
    if name == "fake":
        return FakeBackend()
    if name.startswith("fake_random:"):        # nondeterministic on purpose (tests the shared cache, D95)
        return FakeBackend(nondeterministic=True, seed=int(name.split(":", 1)[1]))
    if name.startswith("fake_adversarial:"):
        return FakeAdversarial(name.split(":", 1)[1])
    if name == "live":
        from agents.llm.live import LiveBackend
        return LiveBackend()
    raise ValueError(f"llm_strategy.backend: unknown backend {name!r} (fake, fake_adversarial:<mode>, live)")


class LLMRun:
    def __init__(self, spec: dict, *, roster, attackers, seed, run_id, power_kw):
        self.spec, self.roster, self.run_id = spec, list(roster), run_id
        self.backend_name = str(spec.get("backend", "fake"))
        self.backend = make_backend(self.backend_name)
        self.live = self.backend_name == "live"
        self.honest = spec.get("honest_models") or AS.DEFAULT_HONEST
        self.attacker = spec.get("attacker_models") or AS.DEFAULT_ATTACKER
        self.draws = AS.assign(self.roster, seed, set(attackers), self.honest, self.attacker)
        c = spec.get("cache") or {}
        self.cache = ResponseCache(c.get("path"), str(c.get("mode", "off")))
        if "cap_usd_run" not in spec:
            raise ValueError("llm_strategy: an LLM chooser needs cap_usd_run (D94, fail closed)")
        self.spend = SP.SpendGuard(float(spec["cap_usd_run"]), live=self.live)
        self.effort = str(spec.get("effort", DEFAULT_EFFORT))
        self.max_tokens = int(spec.get("max_tokens", DEFAULT_MAX_TOKENS))
        self.calls: list[dict] = []

    def cap_hit(self) -> bool:
        return self.spend.cap_hit

    def chooser_for(self, agent_id: str) -> "LLMChooser":
        d = self.draws[agent_id]
        return LLMChooser(self, agent_id, f"{d['provider']}/{d['model']}")

    def header(self) -> dict:
        return {"models": AS.header_block(self.honest, self.attacker, self.draws), "backend": self.backend_name,
                "cache": {"path": None if self.cache.path is None else str(self.cache.path), "mode": self.cache.mode,
                          "sha256_at_open": self.cache.sha256_at_open},
                "cap_usd_run": self.spend.cap, "phase": self.spec.get("phase"),
                "cap_usd_phase": self.spec.get("cap_usd_phase"), "price_table_version": SP.PRICE_TABLE_VERSION,
                "prices": {k: {kk: vv for kk, vv in v.items()} for k, v in SP.PRICES.items()},
                "prompt_version": PB.PROMPT_VERSION, "effort": self.effort, "max_tokens": self.max_tokens}

    def call_records(self) -> list[dict]:
        return list(self.calls)

    def summary(self, local_dates) -> dict:
        days = list(dict.fromkeys(local_dates))
        by_day, by_model = defaultdict(float), defaultdict(lambda: {"calls": 0, "tokens_in": 0, "tokens_out": 0,
                                                                     "usd": 0.0, "unpriced_calls": 0})
        errors, reasons, n_calls = defaultdict(int), defaultdict(int), 0
        tin = tout = 0
        total = 0.0
        for r in self.calls:
            if r.get("sent"):
                n_calls += 1
            if r.get("error"):
                errors[r["error"]] += 1
            reasons[r["reason"].split(":")[0]] += 1
            m = by_model[r["model"]]
            m["calls"] += 1 if r.get("sent") else 0
            m["tokens_in"] += r.get("tokens_in") or 0
            m["tokens_out"] += r.get("tokens_out") or 0
            tin += r.get("tokens_in") or 0
            tout += r.get("tokens_out") or 0
            if r.get("usd") is None and r.get("sent") and not r.get("cache_hit"):
                m["unpriced_calls"] += 1
            usd = r.get("usd") or 0.0
            m["usd"] += usd
            by_day[r["decision_day"]] += usd
            total += usd
        n_days = max(len(days), 1)
        return {"llm_calls": n_calls, "tokens_in": tin, "tokens_out": tout, "usd_total": total,
                "simulated_days": len(days), "usd_per_simulated_day": total / n_days,
                "usd_by_day": dict(by_day), "by_model": {k: dict(v) for k, v in by_model.items()},
                "by_model_note": "report only: model is mixed with site (D93)",
                "errors": dict(errors), "reasons": dict(reasons), "llm_cap_hit": self.spend.cap_hit,
                "cache": {"mode": self.cache.mode, "hits": self.cache.hits, "misses": self.cache.misses,
                          "sha256_end": self.cache.file_sha256()}}


class LLMChooser:
    kind = "llm"

    def __init__(self, run: LLMRun, agent_id: str, model_id: str):
        self.run, self.agent_id, self.model_id = run, agent_id, model_id

    def decide(self, view, reason: str, model_override=None) -> Decision:
        run = self.run
        model_id = model_override or self.model_id
        provider, _, model = model_id.partition("/")
        system, user = PB.render(view)
        req = Request(provider=provider, model=model, system=system, user=user, schema=PB.schema(view),
                      effort=run.effort, max_tokens=run.max_tokens, prompt_version=PB.PROMPT_VERSION,
                      menu_version=MENU_VERSION)
        rec = {"event": "call", "agent": self.agent_id, "decision_day": view.decision_day, "reason": reason,
               "model": model_id, "request_sha256": req.key(), "est_tokens_in": req.est_input_tokens(),
               "sent": False, "cache_hit": False, "tokens_in": 0, "tokens_out": 0, "usd": 0.0, "stop": None,
               "error": None, "label": None, "rationale": None}

        def done(d: Decision) -> Decision:
            rec.update(error=d.error, label=d.label)
            run.calls.append(redact(rec))
            return d

        try:
            run.spend.authorize(req)
        except SP.CapReached:
            rec["cap"] = "reached"
            return done(Decision(label="normal", source="cap_reached", reason=reason, error=None,
                                 meta={"model": model_id}))
        except SP.LiveRefused:
            return done(Decision(label=None, source="llm", reason=reason, error="live_refused"))
        except SP.PriceMissing:
            return done(Decision(label=None, source="llm", reason=reason, error="price_missing"))
        try:
            resp, hit = run.cache.get(req, run.backend)
        except CacheMiss:
            return done(Decision(label=None, source="llm", reason=reason, error="cache_miss"))
        except Exception as e:                  # noqa: BLE001  fail closed: keep the previous label
            code = getattr(e, "code", None)
            return done(Decision(label=None, source="llm", reason=reason,
                                 error=code or f"backend_exception:{type(e).__name__}"))
        usd = run.spend.record(req, resp, hit)
        rec.update(sent=not hit, cache_hit=hit, tokens_in=0 if hit else resp.input_tokens,
                   tokens_out=0 if hit else resp.output_tokens, usd=usd, stop=resp.stop_reason)
        if resp.stop_reason == "refusal":
            return done(Decision(label=None, source="llm", reason=reason, error="refusal"))
        if resp.stop_reason == "error":
            return done(Decision(label=None, source="llm", reason=reason, error=f"backend_error:{resp.error}"))
        try:
            obj = json.loads(resp.text or "")
        except (ValueError, TypeError):
            return done(Decision(label=None, source="llm", reason=reason, error="bad_json"))
        if not isinstance(obj, dict) or not isinstance(obj.get("posture"), str):
            return done(Decision(label=None, source="llm", reason=reason, error="not_a_label"))
        rat = obj.get("rationale")
        rec["rationale"] = rat[:RATIONALE_MAX] if isinstance(rat, str) else None
        return done(Decision(label=obj["posture"], source="llm", reason=reason, rationale=rec["rationale"],
                             meta={"model": model_id, "cache_hit": hit, "usd": usd}))
