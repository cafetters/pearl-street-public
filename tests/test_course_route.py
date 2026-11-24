"""Course route (owner, Nov 23): LLM calls through the course's LLM gateway. A loopback HTTP server stands in for the
gateway, so no test leaves this machine. Every identifier here is fake and built at runtime; the autouse fixture keeps
the real local config out of these tests."""
import http.server
import json
import socket
import threading
import time
import types

import pytest

from agents.llm import course_route as CR
from agents.llm import spend as SP
from agents.llm.backends import Request
from agents.llm.chooser import LLMRun
from agents.llm.live import anthropic_body, openai_body
from agents.llm.respcache import redact

COST_H = "x-" + "tst" + "-cost"
ID_H = "x-" + "tst" + "-rid"
KEY = "sk-" + "zz" + "-v1-" + "Qq" * 20
KEY_VAR = "FAKE_COURSE_" + "KEY_VAR"
SCHEMA = {"type": "object", "properties": {"posture": {"type": "string", "enum": ["normal", "cautious"]},
                                           "rationale": {"type": "string"}},
          "required": ["posture", "rationale"], "additionalProperties": False}
TABLE_USD = 100 * 0.10 / 1e6 + 50 * 0.50 / 1e6          # Haiku and luna: 100 in, 50 out
POSTURE = json.dumps({"posture": "normal", "rationale": "test"})
A_OK = {"content": [{"type": "thinking", "thinking": "hm"}, {"type": "text", "text": POSTURE}],
        "stop_reason": "end_turn", "usage": {"input_tokens": 100, "output_tokens": 50}}
O_OK = {"choices": [{"message": {"role": "assistant", "content": POSTURE}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50}}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    for v in CR.NEUTRAL_ENV.values():
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv(CR.CONFIG_PATH_ENV, str(tmp_path / "no_config.json"))
    monkeypatch.delenv(KEY_VAR, raising=False)


class Gateway:
    """Scripted loopback server: each POST pops (status, headers, body, delay_s) and records what was sent."""

    def __init__(self, script):
        self.script, self.seen = list(script), []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.seen.append({"path": self.path, "headers": [k.lower() for k in self.headers.keys()],
                                   "body": json.loads(self.rfile.read(n) or b"{}")})
                st, hd, body, delay = outer.script.pop(0)
                if delay:
                    time.sleep(delay)
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(st)
                    for k, v in hd.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def gw():
    made = []

    def make(*script):
        g = Gateway(script)
        made.append(g)
        return g
    yield make
    for g in made:
        g.close()


def _file_config(monkeypatch, tmp_path, url):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"base_url": url, "key_env": KEY_VAR, "cost_header": COST_H, "request_id_header": ID_H}))
    monkeypatch.setenv(CR.CONFIG_PATH_ENV, str(p))
    monkeypatch.setenv(KEY_VAR, KEY)


def _route(monkeypatch, tmp_path, g, **kw):
    _file_config(monkeypatch, tmp_path, g.url)
    sleeps = []
    r = CR.CourseRoute(sleep=sleeps.append, **kw)
    r.sleeps = sleeps
    return r


def _req(provider="anthropic", model="claude-haiku-5-5", max_tokens=512):
    return Request(provider=provider, model=model, system="SYS", user="USER", schema=SCHEMA, effort="low",
                   max_tokens=max_tokens, prompt_version="p", menu_version="m")


def _ok(reported=TABLE_USD, provider="anthropic", rid="req-1"):
    hd = {ID_H: rid}
    if reported is not None:
        hd[COST_H] = repr(reported)
    return (200, hd, A_OK if provider == "anthropic" else O_OK, 0)


# ---- 1. the wire: exact headers and body keys, no proxy, no redirect ----

@pytest.mark.parametrize("provider,model,headers", [
    ("anthropic", "claude-haiku-5-5", {"host", "content-type", "content-length", "accept-encoding", "x-api-key",
                                       "anthropic-version"}),
    ("openai", "gpt-6-luna", {"host", "content-type", "content-length", "accept-encoding", "authorization"})])
def test_sends_exactly_the_allowed_headers_and_body_keys(monkeypatch, tmp_path, gw, provider, model, headers):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")       # a proxy would break the call: none is used
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    g = gw(_ok(provider=provider))
    resp = _route(monkeypatch, tmp_path, g).complete(_req(provider, model))
    assert resp.stop_reason == "end_turn" and json.loads(resp.text)["posture"] == "normal"
    (seen,) = g.seen
    assert set(seen["headers"]) == headers and len(seen["headers"]) == len(headers)
    assert set(seen["body"]) == CR.BODY_KEYS[provider]
    assert seen["path"] == CR.PATHS[provider]
    assert seen["body"] == json.loads(json.dumps((anthropic_body if provider == "anthropic" else openai_body)(
        _req(provider, model))))


def test_a_redirect_is_never_followed_and_is_charged_as_maybe_billed(monkeypatch, tmp_path, gw):
    g = gw((302, {"Location": "http://127.0.0.1:9/elsewhere"}, b"", 0))
    r = _route(monkeypatch, tmp_path, g)
    resp = r.complete(_req())
    assert resp.stop_reason == "error" and resp.error == "http_302" and len(g.seen) == 1
    assert resp.meta["cost_unknown"] is True and r.sleeps == []


def test_an_unknown_provider_is_refused_before_anything_is_sent(monkeypatch, tmp_path, gw):
    g = gw()
    with pytest.raises(ValueError):
        _route(monkeypatch, tmp_path, g).complete(_req(provider="other"))
    assert g.seen == []


# ---- 2. config resolution, fail closed, no identifier in any message ----

def test_neutral_environment_variables_win_over_the_file(monkeypatch, tmp_path, gw):
    g = gw(_ok())
    _file_config(monkeypatch, tmp_path, "https://unused.invalid")
    for k, v in {"base_url": g.url, "key": KEY, "cost_header": COST_H, "request_id_header": ID_H}.items():
        monkeypatch.setenv(CR.NEUTRAL_ENV[k], v)
    cfg = CR.load_config()
    assert cfg.source == "env" and cfg.base_url == g.url and cfg.key_var == CR.NEUTRAL_ENV["key"]
    assert CR.CourseRoute().complete(_req()).stop_reason == "end_turn"


def test_partial_neutral_variables_are_refused(monkeypatch):
    monkeypatch.setenv(CR.NEUTRAL_ENV["base_url"], "https://x.invalid")
    with pytest.raises(CR.RouteConfigMissing):
        CR.load_config()


@pytest.mark.parametrize("url", ["http://example.invalid", "https://h.invalid/path", "https://u:p@h.invalid"])
def test_only_a_bare_https_address_is_accepted(monkeypatch, tmp_path, url):
    _file_config(monkeypatch, tmp_path, url)
    with pytest.raises(CR.RouteConfigMissing):
        CR.load_config()


def test_no_config_or_no_key_stops_the_run_before_it_starts_and_names_nothing(monkeypatch, tmp_path):
    spec = {"chooser": "llm", "backend": "live", "route": "course", "cap_usd_run": 1.0,
            "honest_models": [{"provider": "anthropic", "model": "claude-haiku-5-5", "weight": 1.0}]}
    with pytest.raises(CR.RouteConfigMissing) as e1:
        LLMRun(spec, roster=["A"], attackers=set(), seed=1, run_id="r", power_kw={"A": 1.0})
    _file_config(monkeypatch, tmp_path, "https://h.invalid")
    monkeypatch.delenv(KEY_VAR)
    with pytest.raises(CR.RouteConfigMissing) as e2:
        LLMRun(spec, roster=["A"], attackers=set(), seed=1, run_id="r", power_kw={"A": 1.0})
    for e in (e1, e2):
        msg = str(e.value)
        assert KEY_VAR not in msg and "h.invalid" not in msg and KEY not in msg
    assert repr(CR.load_config()) == "RouteConfig(source='file')"


# ---- 3. cost per attempt ----

def _charge(r, req, g=None):
    guard = SP.SpendGuard(1.0, live=True, route="course")
    guard.authorize(req)
    resp = r.complete(req)
    return guard, resp, guard.record(req, resp, False)


def test_the_reported_cost_is_charged_and_logged(monkeypatch, tmp_path, gw):
    g = gw(_ok(reported=TABLE_USD))
    guard, resp, usd = _charge(_route(monkeypatch, tmp_path, g), _req())
    assert usd == pytest.approx(TABLE_USD) and resp.meta["reported_usd"] == pytest.approx(TABLE_USD)
    assert resp.meta["request_id"] == "req-1" and resp.meta["cost_header_missing"] is False
    assert guard.cap_hit is False and guard.over_table is False


def test_a_200_without_the_cost_header_is_charged_the_table_cost_and_flagged(monkeypatch, tmp_path, gw):
    g = gw(_ok(reported=None))
    _, resp, usd = _charge(_route(monkeypatch, tmp_path, g), _req())
    assert usd == pytest.approx(TABLE_USD) and resp.meta["cost_header_missing"] is True


def test_a_200_with_neither_usage_nor_cost_is_charged_the_worst_case(monkeypatch, tmp_path, gw):
    body = {"content": [{"type": "text", "text": POSTURE}], "stop_reason": "end_turn"}
    g = gw((200, {}, body, 0))
    req = _req()
    _, resp, usd = _charge(_route(monkeypatch, tmp_path, g), req)
    assert resp.meta["cost_unknown"] is True and usd == pytest.approx(SP.worst_case_usd(req, "course"))


@pytest.mark.parametrize("script,code", [((500, {}, b"x", 0), "http_500"), ((200, {}, b"not json", 0), "bad_response")])
def test_a_5xx_or_unreadable_200_is_charged_the_worst_case_and_not_retried(monkeypatch, tmp_path, gw, script, code):
    g = gw(script)
    r = _route(monkeypatch, tmp_path, g)
    req = _req()
    _, resp, usd = _charge(r, req)
    assert resp.error == code and len(g.seen) == 1 and r.sleeps == []
    assert usd == pytest.approx(SP.worst_case_usd(req, "course"))


def test_a_timeout_after_sending_is_charged_the_worst_case_and_not_retried(monkeypatch, tmp_path, gw):
    g = gw((200, {}, A_OK, 1.5))
    r = _route(monkeypatch, tmp_path, g, timeout_s=0.3)
    req = _req()
    _, resp, usd = _charge(r, req)
    assert resp.error == "timeout" and len(g.seen) == 1 and r.sleeps == []
    assert usd == pytest.approx(SP.worst_case_usd(req, "course"))


def test_a_failed_connection_is_retried_twice_and_charged_nothing(monkeypatch, tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                    # nothing listens there now
    _file_config(monkeypatch, tmp_path, f"http://127.0.0.1:{port}")
    sleeps = []
    r = CR.CourseRoute(sleep=sleeps.append, timeout_s=0.5)
    _, resp, usd = _charge(r, _req())
    assert resp.error == "connect_failed" and usd == 0.0 and sleeps == list(CR.RETRY_WAITS_S)
    assert [a["outcome"] for a in resp.meta["attempts"]] == ["connect_failed"] * 3


def test_a_4xx_is_charged_nothing_and_not_retried(monkeypatch, tmp_path, gw):
    g = gw((400, {}, {"error": {"message": "echo of the request"}}, 0))
    r = _route(monkeypatch, tmp_path, g)
    _, resp, usd = _charge(r, _req())
    assert resp.error == "http_400" and usd == 0.0 and len(g.seen) == 1 and r.sleeps == []
    assert "echo" not in json.dumps(resp.to_dict())              # an error body is never kept


def test_a_reported_cost_above_the_table_marks_the_cap_hit(monkeypatch, tmp_path, gw):
    g = gw(_ok(reported=TABLE_USD * 1.5))
    guard, _, usd = _charge(_route(monkeypatch, tmp_path, g), _req())
    assert usd == pytest.approx(TABLE_USD * 1.5) and guard.over_table and guard.cap_hit
    with pytest.raises(SP.CapReached):
        guard.authorize(_req())


# ---- 4. retries ----

def test_a_429_then_200_makes_two_attempts_and_charges_once(monkeypatch, tmp_path, gw):
    g = gw((429, {}, b"", 0), _ok())
    r = _route(monkeypatch, tmp_path, g)
    _, resp, usd = _charge(r, _req())
    assert resp.stop_reason == "end_turn" and len(g.seen) == 2 and r.sleeps == [CR.RETRY_WAITS_S[0]]
    assert [a["outcome"] for a in resp.meta["attempts"]] == ["http_429", "ok"] and usd == pytest.approx(TABLE_USD)


def test_a_429_that_reports_a_cost_is_not_retried(monkeypatch, tmp_path, gw):
    g = gw((429, {COST_H: "0.000010"}, b"", 0))
    r = _route(monkeypatch, tmp_path, g)
    _, resp, usd = _charge(r, _req())
    assert resp.error == "http_429" and len(g.seen) == 1 and usd == pytest.approx(0.00001)


# ---- 5. cache hits; 7. nothing identifying is written ----

def test_a_cache_hit_has_no_reported_cost_or_request_id_and_nothing_written_names_the_gateway(monkeypatch, tmp_path,
                                                                                             gw):
    from tests.test_m4_strategy import _view
    g = gw(_ok())
    _file_config(monkeypatch, tmp_path, g.url)
    cache = tmp_path / "llm_cache.jsonl"
    run = LLMRun({"chooser": "llm", "backend": "live", "route": "course", "cap_usd_run": 1.0,
                  "cache": {"mode": "record", "path": str(cache)},
                  "honest_models": [{"provider": "anthropic", "model": "claude-haiku-5-5", "weight": 1.0}]},
                 roster=["A"], attackers=set(), seed=1, run_id="r", power_kw={"A": 1.0})
    ch = run.chooser_for("A")
    assert ch.decide(_view(), "start").label == "normal"
    assert ch.decide(_view(), "scheduled").label == "normal"     # the same prompt: a cache hit
    first, second = run.call_records()
    assert first["usd_reported"] == pytest.approx(TABLE_USD) and first["request_id"] == "req-1"
    assert second["cache_hit"] and second["usd_reported"] == 0.0 and second["request_id"] is None
    s = run.summary(["d0"])
    assert s["route"] == "course" and s["usd_reported_total"] == pytest.approx(TABLE_USD)
    assert s["cost_header_missing"] == 0 and s["max_cost_gap_usd"] < 1e-9 and len(g.seen) == 1
    blob = (json.dumps([run.header(), run.call_records(), s], default=str) + cache.read_text()).lower()
    for term in (KEY, KEY_VAR, COST_H, ID_H, g.url):
        assert term.lower() not in blob


def test_redaction_catches_every_identifier_in_any_case_and_the_key_by_its_shape(monkeypatch, tmp_path):
    _file_config(monkeypatch, tmp_path, "https://gw.example.invalid")
    text = f"{KEY} at HTTPS://GW.EXAMPLE.INVALID, host gw.example.invalid, {COST_H.upper()} {ID_H} {KEY_VAR.lower()}"
    out = redact({"x": text})["x"]
    for term in (KEY, "gw.example.invalid", COST_H, ID_H, KEY_VAR):
        assert term.lower() not in out.lower()
    monkeypatch.delenv(KEY_VAR)
    monkeypatch.setenv(CR.CONFIG_PATH_ENV, str(tmp_path / "none.json"))
    assert KEY not in redact({"x": f"key {KEY}"})["x"]          # with no config, the key's shape alone


# ---- 6. response mapping ----

@pytest.mark.parametrize("body,stop,text", [
    ({**A_OK, "stop_reason": "refusal"}, "refusal", ""),
    ({**A_OK, "stop_reason": "max_tokens"}, "max_tokens", POSTURE),
    (A_OK, "end_turn", POSTURE)])
def test_anthropic_style_mapping(monkeypatch, tmp_path, gw, body, stop, text):
    g = gw((200, {COST_H: f"{TABLE_USD:.6f}"}, body, 0))
    resp = _route(monkeypatch, tmp_path, g).complete(_req())
    assert (resp.stop_reason, resp.text) == (stop, text)
    assert (resp.input_tokens, resp.output_tokens) == (100, 50)


@pytest.mark.parametrize("choice,stop,text", [
    ({"message": {"content": POSTURE}, "finish_reason": "content_filter"}, "refusal", ""),
    ({"message": {"content": None, "refusal": "no"}, "finish_reason": "stop"}, "refusal", ""),
    ({"message": {"content": POSTURE}, "finish_reason": "length"}, "max_tokens", POSTURE)])
def test_openai_style_mapping(monkeypatch, tmp_path, gw, choice, stop, text):
    g = gw((200, {COST_H: f"{TABLE_USD:.6f}"}, {**O_OK, "choices": [choice]}, 0))
    resp = _route(monkeypatch, tmp_path, g).complete(_req("openai", "gpt-6-luna"))
    assert (resp.stop_reason, resp.text) == (stop, text)


# ---- 8. the course route counts as live everywhere; luna only on it ----

def test_the_course_route_is_live_and_allows_luna_only_there(monkeypatch, tmp_path, gw):
    g = gw()
    _file_config(monkeypatch, tmp_path, g.url)
    run = LLMRun({"chooser": "llm", "backend": "live", "route": "course", "cap_usd_run": 1.0},
                 roster=["A"], attackers=set(), seed=1, run_id="r", power_kw={"A": 1.0})
    assert run.live and run.spend.route == "course" and run.header()["route"] == "course"
    luna = _req("openai", "gpt-6-luna")
    SP.SpendGuard(1.0, live=True, route="course").authorize(luna)
    with pytest.raises(SP.LiveRefused):
        SP.SpendGuard(1.0, live=True).authorize(luna)
    with pytest.raises(ValueError):
        SP.SpendGuard(1.0, live=True, route="elsewhere")
    with pytest.raises(ValueError):
        LLMRun({"chooser": "llm", "backend": "fake", "route": "elsewhere", "cap_usd_run": 1.0},
               roster=["A"], attackers=set(), seed=1, run_id="r", power_kw={"A": 1.0})


def test_the_cost_guard_and_the_ablation_treat_the_course_route_as_live():
    from bench import llm_cost_estimate as E
    from bench.m4_ablation import _synthetic
    cfg = {"start": "2023-01-12", "end": "2023-01-13", "llm_strategy": {
        "chooser": "llm", "backend": "live", "route": "course", "cap_usd_run": 5.0,
        "honest_models": [{"provider": "openai", "model": "gpt-6-luna", "weight": 1.0}]}}
    assert E.check_config(cfg, n_agents=2, prompt_tokens=500)["ok"] is True
    cfg["llm_strategy"]["route"] = "direct"
    r = E.check_config(cfg, n_agents=2, prompt_tokens=500)
    assert r["ok"] is False and r["refused_models"] == ["openai/gpt-6-luna"]
    assert _synthetic([{"backend": "live"}]) is False


# ---- 9. the launcher ----

def test_the_launcher_passes_neutral_names_and_scrubs_every_identifier(monkeypatch, tmp_path):
    from bench import cloud_queue as CQ
    _file_config(monkeypatch, tmp_path, "https://gw.example.invalid")
    runs = [{"name": "a", "cfg": {"llm_strategy": {"route": "course"}}}, {"name": "b", "cfg": {}}]
    try:
        env = CQ.course_route_env(runs)
        assert set(env) == set(CR.NEUTRAL_ENV.values()) and env[CR.NEUTRAL_ENV["key"]] == KEY
        line = CQ.redact(f"TLS error to gw.example.invalid ({COST_H.upper()}, {ID_H}, {KEY_VAR}, {KEY})")
        for term in ("gw.example.invalid", COST_H, ID_H, KEY_VAR, KEY):
            assert term.lower() not in line.lower()
    finally:
        CQ.forget_secret_values()
    assert CQ.course_route_env([{"name": "b", "cfg": {}}]) == {}
    monkeypatch.delenv(KEY_VAR)
    with pytest.raises(CQ.QueueError) as e:
        CQ.course_route_env(runs)
    assert KEY_VAR not in str(e.value) and "gw.example" not in str(e.value)
    CQ.forget_secret_values()


# ---- 10. the SDK adapters send the same bodies (runs where the SDKs are installed, e.g. the Maritime pytest) ----

def test_the_sdk_adapters_send_the_shared_bodies(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    openai = pytest.importorskip("openai")
    from agents.llm.live import LiveBackend
    sent = {}
    usage = types.SimpleNamespace(input_tokens=1, output_tokens=1, prompt_tokens=1, completion_tokens=1)

    class AClient:
        def __init__(self, **kw):
            self.messages = types.SimpleNamespace(create=lambda **b: sent.setdefault("anthropic", b) and None or
                                                  types.SimpleNamespace(usage=usage, stop_reason="end_turn",
                                                                        content=[]))

    class OClient:
        def __init__(self, **kw):
            msg = types.SimpleNamespace(content="", refusal=None)
            ch = types.SimpleNamespace(message=msg, finish_reason="stop")
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(
                create=lambda **b: sent.setdefault("openai", b) and None or
                types.SimpleNamespace(usage=usage, choices=[ch])))
    monkeypatch.setattr(anthropic, "Anthropic", AClient)
    monkeypatch.setattr(openai, "OpenAI", OClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x" * 20)
    monkeypatch.setenv("OPENAI_API_KEY", "x" * 20)
    LiveBackend().complete(_req())
    LiveBackend().complete(_req("openai", "gpt-6-luna"))
    assert sent["anthropic"] == anthropic_body(_req()) and sent["openai"] == openai_body(_req("openai", "gpt-6-luna"))


# ---- 11. the check module ----

def _call(agent="A", reported=TABLE_USD, **kw):
    return {"event": "call", "agent": agent, "decision_day": "2023-01-12", "model": "anthropic/claude-haiku-5-5",
            "sent": True, "cache_hit": False, "tokens_in": 100, "tokens_out": 50, "usd_table": TABLE_USD,
            "usd_reported": reported, "error": None, "request_sha256": "ab" * 32, **kw}


def _mk_run(tmp_path, roster=("A", "B"), cba=None, calls=None, status=None):
    d = tmp_path / "run" / "exchange_x"
    d.mkdir(parents=True)
    th = {"event": "header", "config": {"a": 1}, "seed": 1, "git_commit": "abc", "roster": list(roster)}
    (d / "trace.jsonl").write_text(json.dumps(th) + "\n")
    if status:              # D101: roster_status lives in the run summary's header, not the trace header
        (tmp_path / "run" / "summary.json").write_text(json.dumps({"header": {**th, "roster_status": status}}))
    head = {"event": "header", "run_id": "r", "config": {"a": 1}, "seed": 1, "git_commit": "abc",
            "llm_strategy": {"backend": "live", "route": "course",
                             "chooser_by_agent": cba if cba is not None else {"A": "llm", "B": "rule"}}}
    rows = [head] + (calls if calls is not None else [_call()])
    (d / "llm_calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return tmp_path / "run"


def _patterns(tmp_path):
    from bench.course_route_check import load_patterns
    t = tmp_path / "terms.txt"
    t.write_text("# comment\n" + "Zq" + "blockedterm\n")
    return load_patterns(t, [KEY])


def test_check_passes_a_clean_run_and_reports_tokens(tmp_path):
    from bench.course_route_check import check
    rep = check(_mk_run(tmp_path), _patterns(tmp_path))
    assert rep["verdict"] == "PASS", rep["fails"]
    v = rep["variants"][0]
    assert v["llm_agents"] == ["A"] and v["non_llm_agents"] == 1
    assert v["by_model"]["anthropic/claude-haiku-5-5"]["tokens_out_per_call"] == 50


def test_check_counts_an_excluded_agent_only_when_the_header_says_so(tmp_path):
    from bench.course_route_check import check
    pats = _patterns(tmp_path)
    assert check(_mk_run(tmp_path / "1", roster=("A", "B", "C")), pats)["verdict"] == "FAIL"
    rep = check(_mk_run(tmp_path / "2", roster=("A", "B", "C"),
                        status={"A": "active", "B": "active", "C": "excluded: never registered"}), pats)
    assert rep["verdict"] == "PASS" and rep["variants"][0]["excluded"] == ["C"]


@pytest.mark.parametrize("calls,why", [
    ([_call(reported=TABLE_USD * 1.5)], "reported cost off"),
    ([_call(reported=None)], "no reported cost"),
    ([_call(), _call(agent="B")], "calls by non-LLM agents"),
    ([_call(error="backend_error:timeout")], "calls with errors"),
    ([_call(error="cache_miss")], "calls with errors"),
    ([], "no call was sent")])
def test_check_fails_on_cost_calls_and_errors(tmp_path, calls, why):
    from bench.course_route_check import check
    rep = check(_mk_run(tmp_path, calls=calls), _patterns(tmp_path))
    assert rep["verdict"] == "FAIL" and any(why in f for f in rep["fails"]), rep["fails"]


def test_check_finds_a_leaked_term_in_any_case_and_never_prints_it(tmp_path):
    from bench.course_route_check import check
    run = _mk_run(tmp_path)
    (run / "exchange_x" / "log.txt").write_text("oops " + ("Zq" + "blockedterm").upper())
    rep = check(run, _patterns(tmp_path))
    assert rep["verdict"] == "FAIL" and "leak: exchange_x/log.txt: term #1" in rep["fails"]
    assert "blockedterm" not in json.dumps(rep).lower()
    assert check(_mk_run(tmp_path / "k"), [])["verdict"] == "FAIL"        # no terms: the scan proves nothing


# ---- prompt caching on the course route (found on the first real calls, Nov 23) ----

def test_cache_writes_and_reads_are_priced_and_reconcile(monkeypatch, tmp_path, gw):
    write = {**A_OK, "usage": {"input_tokens": 4, "output_tokens": 180, "cache_creation_input_tokens": 853,
                               "cache_read_input_tokens": 0}}
    read = {**A_OK, "usage": {"input_tokens": 4, "output_tokens": 207, "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 853}}
    # the gateway's reported costs for these two usages on Haiku, Nov 23
    g = gw((200, {COST_H: "0.000261"}, write, 0), (200, {COST_H: "0.000112"}, read, 0))
    r = _route(monkeypatch, tmp_path, g)
    for want in (0.000261, 0.000112):
        guard, resp, usd = _charge(r, _req())
        assert usd == pytest.approx(want, abs=1e-6) and not guard.over_table
        assert resp.cache_write_tokens + resp.cache_read_tokens == 853


def test_the_course_route_worst_case_counts_every_input_token_as_a_cache_write():
    req = _req()
    assert SP.worst_case_usd(req, "course") - SP.worst_case_usd(req) == pytest.approx(
        req.est_input_tokens() * 0.10 / 1e6 * (SP.CACHE_WRITE_MULT - 1))
    assert SP.worst_case_usd(req) == pytest.approx(SP.cost_usd(req.model_id, req.est_input_tokens(), req.max_tokens))


def test_a_cached_response_keeps_its_cache_token_counts():
    from agents.llm.backends import Response
    r = Response(text="x", input_tokens=4, output_tokens=1, stop_reason="end_turn", cache_write_tokens=7,
                 cache_read_tokens=9)
    back = Response.from_dict(json.loads(json.dumps(r.to_dict())))
    assert (back.cache_write_tokens, back.cache_read_tokens) == (7, 9)
    assert Response.from_dict({"text": "x", "input_tokens": 1, "output_tokens": 1, "stop_reason": "end_turn"}
                              ).cache_read_tokens == 0           # a cache file written before this change


def test_check_counts_a_refusal_without_failing_the_run(tmp_path):
    from bench.course_route_check import check
    rep = check(_mk_run(tmp_path, calls=[_call(), _call(error="refusal")]), _patterns(tmp_path))
    assert rep["verdict"] == "PASS", rep["fails"]
    assert rep["variants"][0]["refusals"] == 1

