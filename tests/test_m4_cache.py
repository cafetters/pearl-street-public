"""M4 step 4: the shared response cache (D95, owner addition 2; plan test 5), record/replay fixtures and redaction
(owner addition 5; plan test 9), and the adversarial fake backend."""
import json
import os

import pytest

from agents.llm.backends import FakeAdversarial, FakeBackend, Request
from agents.llm.respcache import CacheMiss, ResponseCache, redact, write_fixture

# built at runtime so no literal key-shaped string sits in the repo (gitleaks)
FAKE_KEY = "sk-" + "ant-" + "api03-" + "Zq" * 20 + "AA"
FAKE_OAI = "sk-" + "proj-" + "Xy" * 22


def _req(user="day 2023-01-13, spread 22.1", model="claude-haiku-5-5"):
    return Request(provider="anthropic", model=model, system="sys", user=user, schema={"type": "object"},
                   prompt_version="p1", menu_version="m1")


def test_twin_and_attack_runs_share_answers_through_the_cache(tmp_path):
    path = tmp_path / "jan12.jsonl"
    twin = ResponseCache(path, "record")
    be = FakeBackend(nondeterministic=True, seed=1)
    first = [twin.get(_req(f"day {i}"), be)[0].text for i in range(20)]
    atk = ResponseCache(path, "read_through")
    be2 = FakeBackend(nondeterministic=True, seed=999)          # a different random stream
    again = [atk.get(_req(f"day {i}"), be2) for i in range(20)]
    assert [r.text for r, hit in again] == first
    assert all(hit for _, hit in again) and be2.calls == 0
    r, hit = atk.get(_req("a prompt the twin never saw"), be2)
    assert not hit and be2.calls == 1


def test_replay_mode_fails_on_a_miss(tmp_path):
    path = tmp_path / "c.jsonl"
    ResponseCache(path, "record").get(_req(), FakeBackend())
    rp = ResponseCache(path, "replay")
    assert rp.get(_req(), None)[1] is True
    with pytest.raises(CacheMiss):
        rp.get(_req("unseen"), None)


def test_record_refuses_an_existing_cache(tmp_path):
    path = tmp_path / "c.jsonl"
    ResponseCache(path, "record").get(_req(), FakeBackend())
    with pytest.raises(ValueError):
        ResponseCache(path, "record")


def test_cache_key_changes_with_model_prompt_schema_or_effort():
    base = _req()
    keys = {base.key(), _req(model="claude-sonnet-5-5").key(), _req(user="other").key(),
            Request(**{**base.__dict__, "schema": {"type": "array"}}).key(),
            Request(**{**base.__dict__, "effort": "high"}).key()}
    assert len(keys) == 5


def test_cache_hashes_are_recorded(tmp_path):
    c = ResponseCache(tmp_path / "c.jsonl", "record")
    h0 = c.sha256_at_open
    c.get(_req(), FakeBackend())
    assert c.file_sha256() != h0 and len(c.file_sha256()) == 64


def test_no_key_reaches_any_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OAI)
    c = ResponseCache(tmp_path / "c.jsonl", "record")
    c.get(_req(user=f"leak {FAKE_KEY} and {FAKE_OAI}"), FakeBackend())
    write_fixture(tmp_path / "fx.jsonl", {"user": f"x {FAKE_KEY}", "headers": {"x-api-key": FAKE_KEY,
                                                                               "Authorization": "Bearer " + FAKE_OAI,
                                                                               "anthropic-version": "2026-10-21"}},
                  {"text": "ok " + FAKE_OAI})
    for f in tmp_path.iterdir():
        txt = f.read_text()
        assert FAKE_KEY not in txt and FAKE_OAI not in txt and "Bearer " not in txt
    fx = json.loads((tmp_path / "fx.jsonl").read_text().splitlines()[0])
    assert "x-api-key" not in json.dumps(fx).lower() and "authorization" not in json.dumps(fx).lower()
    assert fx["request"]["headers"]["anthropic-version"] == "2026-10-21"


def test_redact_handles_key_shapes_without_env():
    s = redact({"a": "sk-" + "ant-" + "a" * 40, "b": ["Bearer " + "q" * 30], "api_key": "x", "ok": "normal text"})
    assert s == {"a": "[REDACTED]", "b": ["[REDACTED]"], "ok": "normal text"}


@pytest.mark.parametrize("mode,stop,err", [("bad_json", "end_turn", None), ("out_of_menu", "end_turn", None),
                                           ("number", "end_turn", None), ("refusal", "refusal", None),
                                           ("error", "error", "timeout")])
def test_adversarial_backend_modes(mode, stop, err):
    r = FakeAdversarial(mode).complete(_req())
    assert r.stop_reason == stop and r.error == err
    if mode == "out_of_menu":
        assert json.loads(r.text)["posture"] == "aggressive"
    if mode == "number":
        assert json.loads(r.text)["posture"] == 7.5


def test_adversarial_exception_mode_raises():
    with pytest.raises(RuntimeError):
        FakeAdversarial("exception").complete(_req())
