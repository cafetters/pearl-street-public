"""M4 step 8: provider adapters against a mock transport, no network (plan test 10). Skipped if an SDK is missing."""
import json

import pytest

from agents.llm.backends import Request
from agents.llm.live import LiveBackend, MissingKey

httpx2 = pytest.importorskip("httpx2")
anthropic = pytest.importorskip("anthropic")
openai = pytest.importorskip("openai")

FAKE_KEY = "sk-" + "ant-" + "api03-" + "Mm" * 20 + "AA"
FAKE_OAI = "sk-" + "proj-" + "Nn" * 22
SCHEMA = {"type": "object", "properties": {"posture": {"type": "string", "enum": ["normal", "cautious"]},
                                           "rationale": {"type": "string"}},
          "required": ["posture", "rationale"], "additionalProperties": False}


def _req(provider="anthropic", model="claude-haiku-5-5"):
    return Request(provider=provider, model=model, system="SYS", user="USER", schema=SCHEMA, effort="low",
                   max_tokens=512, prompt_version="p", menu_version="m")


def _client(handler, seen):
    def h(request):
        seen.append(request)
        return handler(request)
    return httpx2.Client(transport=httpx2.MockTransport(h))


def _anth_ok(request):
    return httpx2.Response(200, json={
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": json.dumps({"posture": "cautious", "rationale": "r"})}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 700, "output_tokens": 90}})


def test_anthropic_request_shape_and_parse(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    seen = []
    be = LiveBackend(http_clients={"anthropic": _client(_anth_ok, seen)})
    r = be.complete(_req())
    assert r.stop_reason == "end_turn" and json.loads(r.text)["posture"] == "cautious"
    assert (r.input_tokens, r.output_tokens) == (700, 90)
    body = json.loads(seen[0].content)
    assert body["model"] == "claude-haiku-5-5" and body["max_tokens"] == 512 and body["system"] == "SYS"
    assert body["messages"] == [{"role": "user", "content": "USER"}]
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert body["output_config"]["effort"] == "low"
    assert "temperature" not in body
    hd = seen[0].headers                                         # sent to the provider (SDK 2.x: authorization)
    assert FAKE_KEY in (hd.get("x-api-key") or "") + (hd.get("authorization") or "")
    assert FAKE_KEY not in json.dumps(r.to_dict())


def test_anthropic_refusal_and_http_errors_map_to_the_fallback_path(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)

    def refuse(request):
        return httpx2.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": "x",
                                          "content": [], "stop_reason": "refusal", "stop_sequence": None,
                                          "usage": {"input_tokens": 10, "output_tokens": 0}})
    be = LiveBackend(http_clients={"anthropic": _client(refuse, [])})
    assert be.complete(_req()).stop_reason == "refusal"

    def boom(request):
        return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                     "message": f"bad key {FAKE_KEY}"}})
    be = LiveBackend(http_clients={"anthropic": _client(boom, [])})
    r = be.complete(_req())
    assert r.stop_reason == "error" and r.error == "BadRequestError" and FAKE_KEY not in json.dumps(r.to_dict())


def _oai_ok(request):
    return httpx2.Response(200, json={
        "id": "c1", "object": "chat.completion", "created": 0, "model": "gpt-6-luna",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": json.dumps({"posture": "normal", "rationale": "r"}),
                                 "refusal": None}}],
        "usage": {"prompt_tokens": 650, "completion_tokens": 40, "total_tokens": 690}})


def test_openai_request_shape_and_parse(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OAI)
    seen = []
    be = LiveBackend(http_clients={"openai": _client(_oai_ok, seen)})
    r = be.complete(_req("openai", "gpt-6-luna"))
    assert r.stop_reason == "end_turn" and json.loads(r.text)["posture"] == "normal"
    assert (r.input_tokens, r.output_tokens) == (650, 40)
    body = json.loads(seen[0].content)
    assert body["model"] == "gpt-6-luna" and body["max_completion_tokens"] == 512
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert seen[0].headers.get("authorization") == "Bearer " + FAKE_OAI


def test_openai_refusal(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OAI)

    def refuse(request):
        return httpx2.Response(200, json={
            "id": "c", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": None, "refusal": "no"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}})
    assert LiveBackend(http_clients={"openai": _client(refuse, [])}).complete(
        _req("openai", "gpt-6-luna")).stop_reason == "refusal"


def test_missing_key_fails_before_any_request(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    seen = []
    with pytest.raises(MissingKey):
        LiveBackend(http_clients={"anthropic": _client(_anth_ok, seen)}).complete(_req())
    assert seen == []
