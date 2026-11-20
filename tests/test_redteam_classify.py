"""D-d (owner, Nov 19): the legitimate-play classification is a code rule (redteam.rounds.classify), never an LLM
judgment, and it fails closed; flagged gains are reported whatever the class [sim, synthetic: hand-built inputs]."""
import pytest

from redteam.rounds import CLASSES, classify

TRI = {"gain_usd": {"A": 125.65}, "harm_usd": {"B": 0.0, "C": 0.0}}
CLEAN = {"gateway_log": [], "rejects": {"A": 0, "B": 0, "C": 0}, "imbalance": {"A": 0.0}}
ZERO = ({"A": 0.0}, {"B": 0.0, "C": 0.0})


def test_attributable_gain_or_harm_is_an_exploit():
    assert classify(TRI, {"A": 400.0}, {"B": 0.0}, CLEAN, ["A"])[0] == "exploit"
    assert classify(TRI, {"A": 0.0}, {"B": 3.0}, CLEAN, ["A"])[0] == "exploit"


def test_clean_delivered_gain_with_nothing_attributable_is_legitimate_play():
    assert classify(TRI, *ZERO, CLEAN, ["A"])[0] == "legitimate_play"
    # no claim above demonstrated power: the clamp is the identity, so no control run is needed
    assert classify(TRI, None, None, CLEAN, ["A"], nothing_to_clamp=True)[0] == "legitimate_play"


@pytest.mark.parametrize("why,args,world", [
    ("control failed", (None, None), CLEAN),
    ("bystander harm", ZERO, CLEAN),
    ("gateway entry", ZERO, {**CLEAN, "gateway_log": [{"agent_id": "A", "code": "granted_below_claim"}]}),
    ("exchange reject", ZERO, {**CLEAN, "rejects": {"A": 1}}),
    ("undelivered", ZERO, {**CLEAN, "imbalance": {"A": 12.5}}),
])
def test_anything_else_fails_closed_to_unclassified(why, args, world):
    tri = {**TRI, "harm_usd": {"B": 0.5}} if why == "bystander harm" else TRI
    cls, reason = classify(tri, *args, world, ["A"])
    assert cls == "unclassified" and reason


def test_classes_are_the_three_named():
    assert CLASSES == ("exploit", "legitimate_play", "unclassified")
