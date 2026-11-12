"""M4 step 3: D93 model assignment (plan test 4; owner additions 1 and 4)."""
import pytest

from agents.llm import assign as A

HONEST = [{"provider": "anthropic", "model": "claude-haiku-5-5", "weight": 0.6},
          {"provider": "openai", "model": "gpt-6-luna", "weight": 0.2},
          {"provider": "anthropic", "model": "claude-sonnet-5-5", "weight": 0.2}]
ATTACK = [{"provider": "anthropic", "model": "claude-sonnet-5-5", "weight": 0.9},
          {"provider": "anthropic", "model": "claude-opus-5-5", "weight": 0.1}]
ROSTER = [f"PVB_{i:02d}" for i in range(1, 51)] + [f"DC_{i:02d}" for i in range(1, 11)]


def test_owner_default_weights():
    assert A.DEFAULT_HONEST == HONEST and A.DEFAULT_ATTACKER == ATTACK


def test_same_seed_same_draw_with_or_without_an_attacker():
    a = A.assign(ROSTER, seed=1, attackers=set(), honest=HONEST, attacker=ATTACK)
    b = A.assign(ROSTER, seed=1, attackers={"DC_08"}, honest=HONEST, attacker=ATTACK)
    for ag in ROSTER:
        if ag != "DC_08":
            assert a[ag] == b[ag]
    assert b["DC_08"]["role"] == "attacker" and b["DC_08"]["model"] in {"claude-sonnet-5-5", "claude-opus-5-5"}
    assert a["DC_08"]["role"] == "honest"        # the twin draws from the honest list
    assert a["DC_08"]["u"] == b["DC_08"]["u"]     # same u = hash(seed, agent_id), read against the role's list


def test_draw_is_keyed_on_seed_and_agent_id_only():
    assert A.u_of(1, "PVB_01") == A.u_of(1, "PVB_01")
    assert A.u_of(1, "PVB_01") != A.u_of(2, "PVB_01")
    assert 0.0 <= A.u_of(7, "X") < 1.0
    # adding agents never moves anyone else's draw
    small = A.assign(ROSTER[:5], seed=3, attackers=set(), honest=HONEST, attacker=ATTACK)
    big = A.assign(ROSTER, seed=3, attackers=set(), honest=HONEST, attacker=ATTACK)
    assert all(small[a] == big[a] for a in ROSTER[:5])


def test_frequencies_follow_the_weights_and_zero_is_never_drawn():
    ids = [f"A{i}" for i in range(10_000)]
    w = [{"provider": "p", "model": "x", "weight": 0.6}, {"provider": "p", "model": "y", "weight": 0.4},
         {"provider": "p", "model": "z", "weight": 0.0}]
    got = A.assign(ids, seed=9, attackers=set(), honest=w, attacker=ATTACK)
    n = {m: sum(1 for v in got.values() if v["model"] == m) for m in "xyz"}
    assert abs(n["x"] / 10_000 - 0.6) < 0.02 and abs(n["y"] / 10_000 - 0.4) < 0.02 and n["z"] == 0


@pytest.mark.parametrize("bad", [[], [{"provider": "p", "model": "x", "weight": -1}],
                                 [{"provider": "p", "model": "x", "weight": 0}],
                                 [{"provider": "p", "weight": 1}]])
def test_bad_lists_are_refused(bad):
    with pytest.raises(ValueError):
        A.assign(ROSTER, seed=1, attackers=set(), honest=bad, attacker=ATTACK)


def test_header_block_hashes_the_weights():
    h = A.header_block(HONEST, ATTACK, A.assign(ROSTER, 1, set(), HONEST, ATTACK))
    assert len(h["weights_sha256"]) == 64 and h["honest_models"] == HONEST and h["attacker_models"] == ATTACK
    assert set(h["draws"]) == set(ROSTER)
