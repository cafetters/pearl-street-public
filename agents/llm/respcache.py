"""Shared response cache (D95, owner addition 2) and record/replay fixtures with redaction (owner addition 5).

Keyed on sha256 of the canonical request (provider, model, system, prompt, schema, effort, max_tokens, prompt and menu
versions); no auth field is ever part of a request. Twin pairs and ablation arms on one window share one cache file:
the honest twin runs first in `record` mode (a new file), every other run in `read_through` mode (a hit is reused, a
miss is called and appended), and `replay` reads only (a miss is an error). Temperature can't be pinned on current
models, so identical prompts get identical answers only through this cache.

Everything written passes redact() first: auth-like keys are dropped, key-shaped strings and the current values of
the provider key variables are replaced.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from agents.llm.backends import Request, Response

MODES = ("record", "read_through", "replay", "off")
KEY_ENV = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN")
_AUTH_KEYS = {"authorization", "x-api-key", "api-key", "api_key", "apikey", "openai-api-key", "proxy-authorization"}
_KEYLIKE = re.compile(r"(sk-(?:ant-|proj-)?[A-Za-z0-9_\-]{16,})|(Bearer\s+\S+)")
REDACTED = "[REDACTED]"


class CacheMiss(Exception):
    pass


def _secrets() -> list[str]:
    return [v for v in (os.environ.get(k) for k in KEY_ENV) if v and len(v) >= 8]


def redact(obj, _secrets_list=None):
    secrets = _secrets() if _secrets_list is None else _secrets_list
    if isinstance(obj, dict):
        return {k: redact(v, secrets) for k, v in obj.items() if str(k).lower() not in _AUTH_KEYS}
    if isinstance(obj, (list, tuple)):
        return [redact(v, secrets) for v in obj]
    if isinstance(obj, str):
        s = obj
        for sec in secrets:
            s = s.replace(sec, REDACTED)
        if _KEYLIKE.fullmatch(s.strip()):
            return REDACTED
        return _KEYLIKE.sub(REDACTED, s)
    return obj


def write_fixture(path, request: dict, response: dict) -> None:
    """Append one record/replay fixture line; headers and bodies are redacted before anything touches disk."""
    line = json.dumps({"request": redact(request), "response": redact(response)}, sort_keys=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    if path.exists():
        h.update(path.read_bytes())
    return h.hexdigest()


class ResponseCache:
    def __init__(self, path, mode: str):
        if mode not in MODES:
            raise ValueError(f"cache mode {mode!r} not in {MODES}")
        self.path, self.mode = (Path(path) if path else None), mode
        self.index: dict[str, dict] = {}
        self.hits = self.misses = 0
        if mode != "off":
            if self.path is None:
                raise ValueError(f"cache mode {mode} needs a path")
            if mode == "record" and self.path.exists() and self.path.stat().st_size > 0:
                raise ValueError(f"record mode needs a new cache file; {self.path} exists (use read_through)")
            if mode == "replay" and not self.path.exists():
                raise ValueError(f"replay mode: {self.path} does not exist")
            if self.path.exists():
                for ln in self.path.read_text(encoding="utf-8").splitlines():
                    if ln.strip():
                        r = json.loads(ln)
                        self.index.setdefault(r["key"], r["response"])
        self.sha256_at_open = _sha(self.path) if self.path else None

    def file_sha256(self) -> str | None:
        return _sha(self.path) if self.path else None

    def get(self, req: Request, backend) -> tuple[Response, bool]:
        k = req.key()
        if self.mode != "off" and k in self.index:
            self.hits += 1
            return Response.from_dict(self.index[k]), True
        if self.mode == "replay":
            raise CacheMiss(f"replay: no cached response for request {k[:12]}")
        resp = backend.complete(req)
        self.misses += 1
        if self.mode != "off" and resp.stop_reason != "error":      # errors are never cached (a retry may succeed)
            d = redact(resp.to_dict())
            self.index[k] = d
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"key": k, "request": redact(json.loads(req.canonical())), "response": d},
                                   sort_keys=True) + "\n")
        return resp, False
