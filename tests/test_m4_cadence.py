"""M4 step 6: D97 call cadence (proposed; tested under D28). Presets, triggers T1-T6, max_calls_per_day, backoff,
escalate, size_gate, the start call, labels persisting between calls, and triggers reading only AgentView."""
import dataclasses

import numpy as np
import pytest

from agents import strategy as S
from agents.llm import cadence as CD
from tests.test_m4_strategy import _view


def _schedule(preset, days=30, fire=()):
    """Decision days (1..days) a preset calls on with no trigger, or with triggers on the days in `fire`."""
    c = CD.from_spec(preset)
    assert c.start(0) == (True, "start")
    out = []
    for d in range(1, days + 1):
        v = _view(da_spread=99.0 if d in fire else 1.0)
        call, reason = c.evaluate(d, v, call_day=d)
        if call:
            out.append((d, reason))
    return out


@pytest.mark.parametrize("preset,days", [
    ("daily", list(range(1, 31))), ("every_3", [3, 6, 9, 12, 15, 18, 21, 24, 27, 30]), ("weekly", [7, 14, 21, 28]),
    ("every_10", [10, 20, 30]), ("monthly", [30]), ("triggers_only", []), ("backoff", [1, 3, 7, 15])])
def test_each_preset_schedule_on_a_30_day_run(preset, days):
    got = _schedule(preset)
    assert [d for d, _ in got] == days
    assert all(r == "scheduled" for _, r in got)


def test_presets_are_the_owner_list():
    assert set(CD.PRESETS) == {"daily", "every_3", "weekly", "every_10", "monthly", "triggers_only", "backoff"}


def test_triggers_call_off_schedule_and_name_themselves():
    got = dict(_schedule("weekly", fire={4}))
    # a trigger call is a call: the schedule counts from it (decision Nov 12, the cheaper reading of schedule_days)
    assert got == {4: "trigger:T4", 11: "scheduled", 18: "scheduled", 25: "scheduled"}


def test_t6_is_off_except_daily():
    for p in CD.PRESETS:
        assert CD.from_spec(p).triggers["T6"] is (p == "daily")


def test_backoff_doubles_and_any_trigger_resets_it():
    assert [d for d, _ in _schedule("backoff", fire={5})] == [1, 3, 5, 6, 8, 12, 20]


def test_max_calls_per_day_and_the_hard_ceiling():
    c = CD.from_spec({"preset": "daily", "max_calls_per_day": 1})
    c.start(0)
    # the first publication is evaluated on local day 0, the start call's day: over the day cap
    assert c.evaluate(1, _view(), call_day=0) == (False, "day_cap")
    assert c.evaluate(2, _view(), call_day=1)[0] is True
    assert CD.from_spec("daily").max_calls_per_day == 2
    with pytest.raises(ValueError):
        CD.from_spec({"preset": "daily", "max_calls_per_day": 4})


@pytest.mark.parametrize("kw,fires", [
    ({"peak_settle_net_usd": -10.01, "peak_settle_refund_usd": 100.0}, "T1"),
    ({"peak_settle_net_usd": -30.0, "peak_settle_refund_usd": 5000.0}, None),     # within 1% x refund = $50
    ({"rejects_since_eval": 1}, "T2"),
    ({"imbalance_usd_since_last_call": 10.5}, "T3"),
    ({"da_spread": 20.5}, "T4"),
    ({"site_net_forecast_kwh_yesterday": 100.0, "site_net_metered_kwh_yesterday": 121.0}, "T5"),
    ({"site_net_forecast_kwh_yesterday": 100.0, "site_net_metered_kwh_yesterday": 119.0}, None),
    ({"binding_calls_since_eval": 1}, "T6"),
])
def test_each_trigger(kw, fires):
    on = {t: True for t in CD.TRIGGERS}
    got = CD.fired(_view(**kw), on)
    assert got == ([fires] if fires else [])


def test_triggers_read_only_an_agent_view():
    with pytest.raises(TypeError):
        CD.fired({"rejects_since_eval": 1}, {"T2": True})


def test_escalate_uses_sonnet_on_trigger_calls_only():
    c = CD.from_spec({"preset": "weekly", "escalate": True})
    assert c.model_for("trigger:T4") == "anthropic/claude-sonnet-5-5"
    assert c.model_for("scheduled") is None and c.model_for("start") is None
    assert CD.from_spec("weekly").model_for("trigger:T4") is None


def test_unknown_cadence_keys_and_presets_are_refused():
    with pytest.raises(ValueError):
        CD.from_spec("hourly")
    with pytest.raises(ValueError):
        CD.from_spec({"preset": "weekly", "triggers": {"T9": True}})
    with pytest.raises(ValueError):
        CD.from_spec({"preset": "weekly", "colour": "red"})


# ---- the wrapper over a scripted 30-day run ----

class Inner:
    def __init__(self, T):
        self.id, self.margin, self.posture = "PVB_01", 5.0, "normal"
        self.iso = [f"t{k}" for k in range(T)]
        self.base_export = -np.ones(T)
        self.b = type("B", (), {"P": 1000.0, "CAP": 2000.0})()

    def receive(self, raw):
        pass

    def act(self, k, soc, v, price):
        return [], (0.0, 0.0, 0.0)


class Counting:
    kind = "counting"

    def __init__(self):
        self.n = 0

    def decide(self, view, reason, model_override=None):
        self.n += 1
        return S.Decision(label="cautious" if self.n % 2 else "normal", source="counting", reason=reason)


def _run(preset, days=30):
    T = 96 * days
    local = [f"d{d:02d}" for d in range(days) for _ in range(96)]
    inner, ch = Inner(T), Counting()
    ag = S.StrategyAgent(inner, ch, CD.from_spec(preset), S.RunContext(local_dates=local, da_spread_threshold=None))
    price = np.full(T, 30.0)
    labels = []
    for k in range(T):
        v = min(1 + max(0, (k - 48)) // 96 + (1 if k >= 48 else 0), days)   # publication at 12:00 local
        ag.act(k, 0.0, v, price)
        labels.append(inner.posture)
    return ag, labels


def test_wrapper_weekly_calls_start_then_every_7_days_and_labels_persist():
    ag, labels = _run("weekly")
    reasons = [d["reason"] for d in ag.decisions]
    assert reasons == ["start", "scheduled", "scheduled", "scheduled", "scheduled"]
    assert [d["decision_day"] for d in ag.decisions] == ["d00", "d07", "d14", "d21", "d28"]
    first_change = next(i for i, lab in enumerate(labels) if lab != labels[0])
    assert all(lab == "cautious" for lab in labels[:first_change])     # start label kept until the day-7 call


def test_wrapper_daily_calls_every_day():
    ag, _ = _run("daily")
    assert len(ag.decisions) == 30


def test_size_gate_gives_llm_only_to_agents_at_or_above_the_median_power():
    run = S.StrategyRun({"chooser": "rule", "per_agent": {}, "cadence": {"preset": "weekly", "size_gate": True},
                         "t4_threshold_usd_mwh": 1.0},
                        roster=["A", "B", "C", "D"], power_kw={"A": 1.0, "B": 3.0, "C": 2.0, "D": 2.0},
                        attackers=set(), local_dates=["x"], seed=1, run_id="r", config_margin=5.0,
                        t4_threshold=1.0)
    assert run.chooser_by_agent == {"A": "rule", "B": "rule", "C": "rule", "D": "rule"}
    run = S.StrategyRun({"chooser": "llm", "backend": "fake", "cap_usd_run": 1.0,
                         "cadence": {"preset": "weekly", "size_gate": True}},
                        roster=["A", "B", "C", "D"], power_kw={"A": 1.0, "B": 3.0, "C": 2.0, "D": 2.0},
                        attackers=set(), local_dates=["x"], seed=1, run_id="r", config_margin=5.0,
                        t4_threshold=1.0)
    # sorted by (power, id): A 1, C 2, D 2, B 3 -> lower median is C (2.0); C, D, B are at or above it
    assert run.chooser_by_agent == {"A": "rule", "B": "llm", "C": "llm", "D": "llm"}
