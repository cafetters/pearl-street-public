"""The course route (owner, Nov 23): LLM calls through the course's LLM gateway over the standard library's http.client
(no SDK, no redirects, no proxies), selected by `llm_strategy: {backend: live, route: course}`.

Identifiers. None of the gateway's identifiers (address, key variable, header names) is in the repo, a config, a run file
or a log. They are read at runtime, in this order:
  1. the four neutral environment variables in NEUTRAL_ENV (all four together; the cloud launcher passes them to a
     Maritime run stage as secret environment values);
  2. the local file `%LOCALAPPDATA%/pearl-street/llm_gateway.json` (outside the repo) with keys base_url, key_env,
     cost_header and request_id_header; the key itself stays in the environment variable `key_env` names.
CONFIG_PATH_ENV overrides the file's path (tests). The key is read at call time only and never stored, returned or
logged; every message here names only the neutral variables. scrub_terms() lists every identifier string so that
writers (respcache.redact, the cloud launcher) can remove them.

Wire. Exactly the headers built in _headers() and the body keys in BODY_KEYS are sent: no retrieval header or parameter
can be added (the D59/D70 prompt firewall). Never streamed. A redirect is an error, never followed.

Cost (D94). Every attempt is logged in Response.meta["attempts"]: its outcome, whether it may have been billed, and the
cost the gateway reported for it. The route never raises once a request may have left; it returns an error Response,
so the spend guard always sees the attempt. Only an attempt that is certainly unbilled is retried (the connection
failed before anything was sent, or a 429 that reports no cost), so at most one billed attempt follows each
SpendGuard.authorize().
"""
from __future__ import annotations

import http.client
import json
import math
import os
import re
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from agents.llm.backends import Response
from agents.llm.live import anthropic_body, openai_body

ROUTE = "course"
NEUTRAL_ENV = {"base_url": "PS_COURSE_LLM_URL", "key": "PS_COURSE_LLM_KEY",
               "cost_header": "PS_COURSE_LLM_COST_HEADER", "request_id_header": "PS_COURSE_LLM_ID_HEADER"}
CONFIG_PATH_ENV = "PS_COURSE_LLM_CONFIG"
FILE_KEYS = ("base_url", "key_env", "cost_header", "request_id_header")
PATHS = {"anthropic": "/v1/messages", "openai": "/v1/chat/completions"}
BODY_KEYS = {"anthropic": frozenset({"model", "max_tokens", "system", "messages", "output_config"}),
             "openai": frozenset({"model", "max_completion_tokens", "messages", "response_format"})}
ANTHROPIC_VERSION = "2023-06-01"
TIMEOUT_S = 60.0
RETRY_WAITS_S = (2.0, 4.0)       # at most two retries, and only of certainly unbilled attempts
LOOPBACK = "127.0.0.1"           # plain http is allowed only to this host (tests)
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


class RouteConfigMissing(Exception):
    """No usable course-route config or key. Messages name only the neutral variables, never an identifier."""
    code = "route_config_missing"


@dataclass(frozen=True, repr=False)
class RouteConfig:
    base_url: str
    cost_header: str
    request_id_header: str
    key_var: str             # the environment variable that holds the key (never the key itself)
    source: str              # "env" or "file"

    def __repr__(self) -> str:                # never prints an identifier
        return f"RouteConfig(source={self.source!r})"

    __str__ = __repr__

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""

    def key(self) -> str:
        k = os.environ.get(self.key_var)
        if not k:
            raise RouteConfigMissing("the course route's key is not set in this environment (see "
                                     "agents/llm/course_route.py)")
        return k


def config_path() -> Path | None:
    p = os.environ.get(CONFIG_PATH_ENV)
    if p:
        return Path(p)
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "pearl-street" / "llm_gateway.json" if base else None


def _validate(cfg: RouteConfig) -> RouteConfig:
    u = urlsplit(cfg.base_url)
    if (not u.hostname or u.path not in ("", "/") or u.query or u.fragment or u.username or u.password
            or not (u.scheme == "https" or (u.scheme == "http" and u.hostname == LOOPBACK))):
        raise RouteConfigMissing("the course route's address must be a bare https address")
    for h in (cfg.cost_header, cfg.request_id_header):
        if not _HEADER_NAME.match(h):
            raise RouteConfigMissing("the course route's header names must be plain header names")
    return cfg


def load_config() -> RouteConfig:
    """The neutral environment variables if any is set (then all four must be), else the local file. Fails closed."""
    env = {k: os.environ.get(v) for k, v in NEUTRAL_ENV.items()}
    if any(env.values()):
        if not all(env.values()):
            raise RouteConfigMissing(f"set all of {sorted(NEUTRAL_ENV.values())} or none of them")
        return _validate(RouteConfig(env["base_url"].rstrip("/"), env["cost_header"], env["request_id_header"],
                                     NEUTRAL_ENV["key"], "env"))
    p = config_path()
    if p is None or not p.is_file():
        raise RouteConfigMissing(f"no course-route config: neither {sorted(NEUTRAL_ENV.values())} nor the local "
                                 "config file (see agents/llm/course_route.py)")
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RouteConfigMissing("the local course-route config file is unreadable or not JSON") from None
    if not isinstance(d, dict) or any(not isinstance(d.get(k), str) or not d.get(k) for k in FILE_KEYS):
        raise RouteConfigMissing(f"the local course-route config file needs the keys {list(FILE_KEYS)}")
    return _validate(RouteConfig(d["base_url"].rstrip("/"), d["cost_header"], d["request_id_header"], d["key_env"],
                                 "file"))


def scrub_terms() -> list[str]:
    """Every identifier string a writer must remove: the key value, the address, its host, both header names and the
    key variable's name (when it isn't a neutral one). Empty when no config resolves (then there is nothing to hide)."""
    try:
        cfg = load_config()
    except RouteConfigMissing:
        return []
    out = [cfg.base_url, cfg.host, cfg.cost_header, cfg.request_id_header]
    if cfg.key_var not in NEUTRAL_ENV.values():
        out.append(cfg.key_var)
    k = os.environ.get(cfg.key_var)
    if k:
        out.append(k)
    return [t for t in dict.fromkeys(out) if t and len(t) >= 4]


def launcher_env() -> tuple[dict, list]:
    """For the cloud launcher: the four neutral variables with their values (read from the local config and key), and
    the scrub terms. Fails closed with a neutral message."""
    cfg = load_config()
    env = {NEUTRAL_ENV["base_url"]: cfg.base_url, NEUTRAL_ENV["key"]: cfg.key(),
           NEUTRAL_ENV["cost_header"]: cfg.cost_header, NEUTRAL_ENV["request_id_header"]: cfg.request_id_header}
    return env, scrub_terms()


def _usd(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x >= 0 else None


def _request_id(v) -> str | None:
    return v if isinstance(v, str) and _REQUEST_ID.match(v) else None


class CourseRoute:
    """complete(Request) -> Response over the course's LLM gateway. Built once per run; the config resolves here, so a
    run with no config or key stops before the simulation starts."""
    name = "live"
    route = ROUTE

    def __init__(self, config: RouteConfig | None = None, sleep=time.sleep, timeout_s: float = TIMEOUT_S):
        self.config = config or load_config()
        self.config.key()                         # fail closed now, not on the first call mid-run
        self.sleep, self.timeout_s = sleep, float(timeout_s)

    def _headers(self, provider: str, key: str, n: int) -> dict:
        h = {"Host": self.config.host, "Content-Type": "application/json", "Content-Length": str(n),
             "Accept-Encoding": "identity"}
        if provider == "anthropic":
            h["x-api-key"] = key
            h["anthropic-version"] = ANTHROPIC_VERSION
        else:
            h["Authorization"] = "Bearer " + key
        return h

    def complete(self, req) -> Response:
        if req.provider not in PATHS:
            raise ValueError(f"unknown provider {req.provider!r}")        # nothing sent
        body = (anthropic_body if req.provider == "anthropic" else openai_body)(req)
        if set(body) - BODY_KEYS[req.provider]:
            raise ValueError(f"body keys outside the allowlist: {sorted(set(body) - BODY_KEYS[req.provider])}")
        key = self.config.key()                                            # may raise; nothing sent
        data = json.dumps(body).encode()
        headers = self._headers(req.provider, key, len(data))
        attempts, parsed = [], None
        for i in range(len(RETRY_WAITS_S) + 1):
            att, parsed = self._attempt(req.provider, data, headers)
            retry = att.pop("_retry", False)
            attempts.append(att)
            if not retry or i == len(RETRY_WAITS_S):
                break
            self.sleep(RETRY_WAITS_S[i])
        return self._response(req, attempts, parsed)

    def _attempt(self, provider: str, data: bytes, headers: dict):
        u = urlsplit(self.config.base_url)
        att = {"outcome": None, "status": None, "billed": "no", "reported_usd": None, "request_id": None}
        if u.scheme == "https":
            conn = http.client.HTTPSConnection(u.hostname, u.port, timeout=self.timeout_s,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(u.hostname, u.port, timeout=self.timeout_s)
        try:
            try:
                conn.connect()
            except (OSError, http.client.HTTPException):
                att.update(outcome="connect_failed", _retry=True)          # nothing left this machine's socket
                return att, None
            try:
                conn.request("POST", PATHS[provider], body=data, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
            except TimeoutError:
                att.update(outcome="timeout", billed="maybe")
                return att, None
            except (OSError, http.client.HTTPException):
                att.update(outcome="bad_response", billed="maybe")
                return att, None
        finally:
            conn.close()
        rh = {k.lower(): v for k, v in resp.getheaders()}
        st = int(resp.status)
        att.update(status=st, reported_usd=_usd(rh.get(self.config.cost_header.lower())),
                   request_id=_request_id(rh.get(self.config.request_id_header.lower())))
        if st == 200:
            try:
                parsed = json.loads(raw)
            except ValueError:
                att.update(outcome="bad_response", billed="maybe")
                return att, None
            att.update(outcome="ok", billed="yes")
            return att, parsed
        if st == 429 and not att["reported_usd"]:
            att.update(outcome="http_429", _retry=True)                   # refused before any model ran
        elif 300 <= st < 400 or st >= 500:
            att.update(outcome=f"http_{st}", billed="maybe")               # a redirect is never followed
        else:
            att.update(outcome=f"http_{st}")                               # other 4xx: refused, not billed
        return att, None

    def _response(self, req, attempts: list, parsed) -> Response:
        last = attempts[-1]
        reported = [a["reported_usd"] for a in attempts if a["reported_usd"] is not None]
        meta = {"route": ROUTE,
                "attempts": [{k: a[k] for k in ("outcome", "status", "billed", "reported_usd")} for a in attempts],
                "reported_usd": sum(reported) if reported else None, "request_id": last["request_id"],
                "cost_unknown": any(a["billed"] == "maybe" and a["reported_usd"] is None for a in attempts),
                "cost_header_missing": last["outcome"] == "ok" and last["reported_usd"] is None}

        def err(code: str) -> Response:
            return Response(text=None, input_tokens=0, output_tokens=0, stop_reason="error", error=code,
                            provider=req.provider, model=req.model, meta=meta)

        if last["outcome"] != "ok":
            return err(last["outcome"])
        try:
            if req.provider == "anthropic":
                usage = parsed.get("usage") or {}
                tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
                cw = int(usage.get("cache_creation_input_tokens") or 0)
                cr = int(usage.get("cache_read_input_tokens") or 0)
                stop = parsed.get("stop_reason")
                if stop == "refusal":
                    text, stop = "", "refusal"
                else:
                    text = next((b.get("text") for b in parsed.get("content") or []
                                 if isinstance(b, dict) and b.get("type") == "text"), "") or ""
                    stop = "max_tokens" if stop == "max_tokens" else "end_turn"
            else:
                usage = parsed.get("usage") or {}
                tin, tout = int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)
                cw = cr = 0          # OpenAI style: cached tokens sit inside prompt_tokens (none seen on luna yet)
                ch = (parsed.get("choices") or [None])[0]
                msg = ch.get("message") or {}
                fr = ch.get("finish_reason")
                if msg.get("refusal") or fr == "content_filter":
                    text, stop = "", "refusal"
                else:
                    text, stop = msg.get("content") or "", ("max_tokens" if fr == "length" else "end_turn")
        except (AttributeError, TypeError, ValueError, IndexError):
            if meta["reported_usd"] is None:
                meta["cost_unknown"] = True                                # billed, cost unknown: worst case
            return err("bad_response")
        if not usage and meta["reported_usd"] is None:
            meta["cost_unknown"] = True                                    # no usage and no reported cost
        if not isinstance(text, str):
            if meta["reported_usd"] is None:
                meta["cost_unknown"] = True
            return err("bad_response")
        return Response(text=text, input_tokens=tin, output_tokens=tout, stop_reason=stop, provider=req.provider,
                        model=req.model, meta=meta, cache_write_tokens=cw, cache_read_tokens=cr)
