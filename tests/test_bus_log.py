"""Fix 1 (memory, Nov 5): bus.log keeps `nominate` and `telemetry` as envelope only. The run summary only counts them,
so it must not change, and the trace must keep every message in full."""
import json

import numpy as np
import pytest

from exchange import messages as M
from sim.bus import Bus


class _Logger:
    def __init__(self):
        self.lines = []

    def log(self, raw, event):
        self.lines.append((event, raw))

    def refuse(self, *a):
        raise AssertionError("nothing is refused here")


class _Sink:
    def receive(self, raw):
        pass


def _bus(slim=()):
    logger = _Logger()
    bus = Bus(logger, None, slim=slim)
    bus.attach("exchange", _Sink())
    return bus, logger


def _msgs():
    t = "2023-01-02T06:00:00Z"
    return [M.encode(M.make("telemetry", "meter", "exchange", t,
                            {"asset_id": "A/bess", "soc_kwh": 5.0, "meter_kw": 1.0, "net_kw": -2.0}, "meter:1", "run")),
            M.encode(M.make("tick", "meter", "exchange", t, {}, "meter:2", "run"))]


def test_default_keeps_every_message_in_full():
    bus, _ = _bus()
    bus.send(_msgs(), "meter")
    bus.flush()
    assert [m["payload"] for m in bus.log] == [M.decode(r)["payload"] for r in _msgs()]


def test_slim_types_keep_the_envelope_only_and_the_trace_keeps_everything():
    bus, logger = _bus(slim={"telemetry"})
    bus.send(_msgs(), "meter")
    bus.flush()
    slim, full = bus.log
    assert slim == {k: v for k, v in M.decode(_msgs()[0]).items() if k != "payload"}
    with pytest.raises(KeyError):          # a reader that needs the payload fails loudly
        slim["payload"]
    assert full == M.decode(_msgs()[1])
    assert [raw for _, raw in logger.lines] == _msgs()


def _price_cfg(**kw):
    from tests.test_d58_margin import _cfg
    return _cfg(**kw)


def _arm_a_cfg():
    from tests.test_replay import CFG_M11
    return {**CFG_M11, "rounds": "info", "nomination_span": "horizon", "firm_offers": True, "prep_clearing": True, "no_rebound": True,
            "daily_energy": True, "risk_premium_usd_mwh": 2.0, "rebound_hours": 24}


TIMING = {"runtime_s", "wall_utc", "round_secs", "round_wall_utc", "compare_solve_secs"}


def _strip(x):
    if isinstance(x, dict):
        return {k: _strip(v) for k, v in x.items() if k not in TIMING and not str(k).startswith("_")}
    if isinstance(x, list):
        return [_strip(v) for v in x]
    return x


@pytest.mark.parametrize("arm", ["price_mode", "arm_a"])
def test_summary_and_trace_are_unchanged_by_the_slim_log(tmp_path, monkeypatch, arm):
    import sim.run_m1 as R
    from tests.test_replay import synthetic_slice
    cfg, h = (_price_cfg(), 0.03) if arm == "price_mode" else (_arm_a_cfg(), 0.1)
    s = synthetic_slice()
    rec = s.system["system_load"] * 1000.0
    sf = R.run_variant(cfg, s, np.arange(2), tmp_path / "slim")
    slim = R.run_variant(cfg, s, np.arange(2), tmp_path / "slim", headroom_mw=h)
    monkeypatch.setattr(R, "BUS_LOG_SLIM", frozenset())
    full = R.run_variant(cfg, s, np.arange(2), tmp_path / "full", headroom_mw=h)
    types = {m["msg_type"] for m in full["log"]}
    assert {"nominate", "telemetry"} <= types               # the run exercises both slim types
    assert len(slim["log"]) == len(full["log"])
    assert [m["msg_type"] for m in slim["log"]] == [m["msg_type"] for m in full["log"]]
    ev_slim = R.evaluate(cfg, s, np.arange(2), slim, rec, None, sf)
    ev_full = R.evaluate(cfg, s, np.arange(2), full, rec, None, sf)
    dump = lambda ev: json.dumps(_strip(ev), sort_keys=True, default=str)     # noqa: E731  (NaN == NaN here)
    assert dump(ev_slim) == dump(ev_full)
    for key in ("_agent_value", "_net"):
        assert np.array_equal(ev_slim[key], ev_full[key])
    variant = slim["variant"]
    body = lambda d: (d / variant / "trace.jsonl").read_text().splitlines()[1:]    # noqa: E731
    assert body(tmp_path / "slim") == body(tmp_path / "full")
    # the trace has every message the full log had, payload included: readers that need a payload read the trace
    from exchange.trace.logger import line_to_msg
    assert [line_to_msg(json.loads(x)) for x in body(tmp_path / "slim")] == full["log"]
    noms = [m for m in slim["log"] if m["msg_type"] == "nominate"]
    tele = [m for m in slim["log"] if m["msg_type"] == "telemetry"]
    assert tele and all("payload" not in m for m in tele)
    if arm == "price_mode":
        assert noms and all("payload" not in m for m in noms)
    else:                                                     # the D34 referee reads pre-award nominations in full
        assert slim["referee_on"] and noms and all("kw" in m["payload"] for m in noms)
