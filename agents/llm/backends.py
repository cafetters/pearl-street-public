"""LLM backends behind one interface: complete(Request) -> Response. No live calls happen anywhere in tests or in
tonight's runs; real backends read their key from the environment at call time only and never log it."""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass, field

CHARS_PER_TOKEN = 3.5
TOKEN_SAFETY = 1.3          # tonight's estimate for Haiku 5.5's tokenizer (owner, Nov 12); the pilot uses token counting


def est_tokens(text: str) -> int:
    return int(math.ceil(len(text) / CHARS_PER_TOKEN * TOKEN_SAFETY))


@dataclass(frozen=True)
class Request:
    provider: str
    model: str
    system: str
    user: str
    schema: dict | None = None
    effort: str = "low"
    max_tokens: int = 2048
    prompt_version: str = ""
    menu_version: str = ""
    kind: str = "posture"

    def canonical(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def key(self) -> str:
        return hashlib.sha256(self.canonical().encode()).hexdigest()

    @property
    def model_id(self) -> str:
        return f"{self.provider}/{self.model}"

    def est_input_tokens(self) -> int:
        return est_tokens(self.system) + est_tokens(self.user) + (est_tokens(json.dumps(self.schema))
                                                                   if self.schema else 0)


@dataclass
class Response:
    text: str | None
    input_tokens: int
    output_tokens: int
    stop_reason: str                      # end_turn | refusal | max_tokens | error
    error: str | None = None
    provider: str = ""
    model: str = ""
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Response":
        return cls(**{k: d.get(k) for k in ("text", "input_tokens", "output_tokens", "stop_reason", "error",
                                              "provider", "model")}, meta=d.get("meta") or {})


class FakeBackend:
    """Deterministic stand-in. With `script`, returns the scripted texts in order. Otherwise picks a posture by a hash
    of the prompt (or at random with nondeterministic=True, to test that the shared cache, not the backend, makes twin
    answers identical). Token counts come from the text lengths, so cost logging is exercised."""
    name = "fake"

    def __init__(self, script=None, labels=("normal", "cautious"), nondeterministic: bool = False, seed: int = 0,
                 output_tokens: int = 60):
        self.script = list(script) if script is not None else None
        self.labels = tuple(labels)
        self.rng = random.Random(seed) if nondeterministic else None
        self.output_tokens = output_tokens
        self.calls = 0

    def complete(self, req: Request) -> Response:
        self.calls += 1
        if self.script is not None:
            text = self.script.pop(0) if self.script else json.dumps({"tool": "done", "args": {}})
        else:
            if self.rng is not None:
                lab = self.rng.choice(self.labels)
            else:
                lab = self.labels[int(hashlib.sha256(req.user.encode()).hexdigest(), 16) % len(self.labels)]
            text = json.dumps({"posture": lab, "rationale": "fake backend"})
        return Response(text=text, input_tokens=req.est_input_tokens(), output_tokens=self.output_tokens,
                        stop_reason="end_turn", provider=req.provider, model=req.model)


class FakeAdversarial:
    """Returns the bad answers a real model or network can produce, to test that the chooser fails closed."""
    name = "fake_adversarial"
    MODES = ("bad_json", "out_of_menu", "number", "refusal", "error", "exception", "bold")

    def __init__(self, mode: str):
        if mode not in self.MODES:
            raise ValueError(f"unknown adversarial mode {mode!r}")
        self.mode = mode
        self.calls = 0

    def complete(self, req: Request) -> Response:
        self.calls += 1
        n = req.est_input_tokens()
        m = self.mode
        if m == "exception":
            raise RuntimeError("backend blew up")
        if m == "error":
            return Response(text=None, input_tokens=0, output_tokens=0, stop_reason="error", error="timeout",
                            provider=req.provider, model=req.model)
        if m == "refusal":
            return Response(text="", input_tokens=n, output_tokens=5, stop_reason="refusal", provider=req.provider,
                            model=req.model)
        text = {"bad_json": "{posture: normal", "out_of_menu": json.dumps({"posture": "aggressive"}),
                "number": json.dumps({"posture": 7.5}), "bold": json.dumps({"posture": "bold"})}[m]
        return Response(text=text, input_tokens=n, output_tokens=20, stop_reason="end_turn", provider=req.provider,
                        model=req.model)
