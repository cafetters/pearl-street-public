"""Live provider backends (Anthropic, OpenAI). Keys are read from the environment at call time only, passed to the SDK
client and never stored, returned or logged; an API error is reported by its class name only (its message can echo
request data). No live call happens in tests or tonight's runs: tests inject a mock-transport http client."""
from __future__ import annotations

import os

from agents.llm.backends import Response

KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}
TIMEOUT_S = 60.0


class MissingKey(Exception):
    code = "missing_key"


def _key(provider: str) -> str:
    env = KEY_ENV.get(provider)
    if env is None:
        raise ValueError(f"unknown provider {provider!r}")
    k = os.environ.get(env)
    if not k:
        raise MissingKey(env)
    return k


def _anthropic(req, key, http_client):
    import anthropic
    kw = {"api_key": key, "max_retries": 2, "timeout": TIMEOUT_S}
    if http_client is not None:
        kw["http_client"] = http_client
    client = anthropic.Anthropic(**kw)
    try:
        out_cfg = {"effort": req.effort}
        if req.schema:
            out_cfg["format"] = {"type": "json_schema", "schema": req.schema}
        msg = client.messages.create(model=req.model, max_tokens=req.max_tokens, system=req.system,
                                     messages=[{"role": "user", "content": req.user}], output_config=out_cfg)
    except anthropic.APIError as e:
        return Response(text=None, input_tokens=0, output_tokens=0, stop_reason="error", error=type(e).__name__,
                        provider=req.provider, model=req.model)
    usage = getattr(msg, "usage", None)
    tin, tout = int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0)
    if msg.stop_reason == "refusal":
        return Response(text="", input_tokens=tin, output_tokens=tout, stop_reason="refusal",
                        provider=req.provider, model=req.model)
    text = next((b.text for b in msg.content if getattr(b, "type", None) == "text"), "")
    stop = "max_tokens" if msg.stop_reason == "max_tokens" else "end_turn"
    return Response(text=text, input_tokens=tin, output_tokens=tout, stop_reason=stop, provider=req.provider,
                    model=req.model)


def _openai(req, key, http_client):
    import openai
    kw = {"api_key": key, "max_retries": 2, "timeout": TIMEOUT_S}
    if http_client is not None:
        kw["http_client"] = http_client
    client = openai.OpenAI(**kw)
    try:
        args = {"model": req.model, "max_completion_tokens": req.max_tokens,
                "messages": [{"role": "system", "content": req.system}, {"role": "user", "content": req.user}]}
        if req.schema:
            args["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "posture", "schema": req.schema, "strict": True}}
        r = client.chat.completions.create(**args)
    except openai.APIError as e:
        return Response(text=None, input_tokens=0, output_tokens=0, stop_reason="error", error=type(e).__name__,
                        provider=req.provider, model=req.model)
    usage = getattr(r, "usage", None)
    tin, tout = int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0)
    m = r.choices[0].message
    if getattr(m, "refusal", None):
        return Response(text="", input_tokens=tin, output_tokens=tout, stop_reason="refusal",
                        provider=req.provider, model=req.model)
    stop = "max_tokens" if r.choices[0].finish_reason == "length" else "end_turn"
    return Response(text=m.content or "", input_tokens=tin, output_tokens=tout, stop_reason=stop,
                    provider=req.provider, model=req.model)


class LiveBackend:
    name = "live"
    _ADAPTERS = {"anthropic": _anthropic, "openai": _openai}

    def __init__(self, http_clients: dict | None = None):
        self.http_clients = http_clients or {}

    def complete(self, req) -> Response:
        if req.provider not in self._ADAPTERS:
            raise ValueError(f"unknown provider {req.provider!r}")
        key = _key(req.provider)              # fails closed before any request is built
        return self._ADAPTERS[req.provider](req, key, self.http_clients.get(req.provider))
