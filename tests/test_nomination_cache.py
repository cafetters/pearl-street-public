"""The exchange's latest-nominations cache equals a from-scratch merge of every stored nomination (values and key
order), through nominations and D47 cap re-base clips, so caching changes no output (PLAN M2a deferred item)."""
import random

import pytest

from exchange.core import Exchange, fmt_t, parse_t

T0 = parse_t("2023-01-02T06:00:00Z")


def full_merge(ex, aid):
    out = {}
    for n in ex.noms.get(aid, []):
        out.update(n["values"])
    return out


def test_cache_matches_full_merge_values_and_order():
    rng = random.Random(7)
    ex = Exchange("r", "OP", "meter", "clock", ["SYS"])
    steps = [fmt_t(T0 + i * ex.step) for i in range(40)]
    agents = ["A", "B", "C"]
    for i in range(300):
        aid = rng.choice(agents)
        a = rng.randrange(0, 39)
        b = rng.randrange(a + 1, 41)
        vals = {t: rng.uniform(-100, 100) for t in steps[a:b]}
        if rng.random() < 0.2:          # a re-base clip: a sparse subset, as _rebase_caps stores
            vals = {t: v for t, v in vals.items() if rng.random() < 0.3}
        ex._add_nom(aid, f"c{i}", vals)
        for x in agents:
            got, want = ex._latest(x), full_merge(ex, x)
            assert got == want and list(got) == list(want)
    assert ex._latest("nobody") == {}


def test_nominate_and_rebase_paths_both_feed_the_cache():
    ex = Exchange("r", "OP", "meter", "clock", ["SYS"])
    ex._add_nom("A", "c1", {"t1": 1.0, "t2": 2.0})
    ex._add_nom("A", "c2", {"t2": 5.0, "t3": 3.0})
    assert list(ex._latest("A").items()) == [("t1", 1.0), ("t2", 5.0), ("t3", 3.0)]
    assert [n["call_id"] for n in ex.noms["A"]] == ["c1", "c2"]


def test_latest_is_a_read_only_view():
    """W4 (owner, Nov 2): _latest hands out a read-only view of the cache, so no caller can corrupt it."""
    ex = Exchange("run", "OP", "meter", "clock", ["SYS"])
    ex._add_nom("A", "c1", {"t1": 1.0})
    view = ex._latest("A")
    with pytest.raises(TypeError):
        view["t1"] = 9.0
    with pytest.raises(TypeError):
        ex._latest("nobody")["t1"] = 9.0
    assert dict(view) == {"t1": 1.0}
    ex._add_nom("A", "c2", {"t1": 2.0, "t2": 3.0})
    assert list(ex._latest("A").items()) == [("t1", 2.0), ("t2", 3.0)]      # the view follows the cache
    snap = dict(ex._latest("A"))
    ex._add_nom("A", "c3", {"t1": 4.0})
    assert snap == {"t1": 2.0, "t2": 3.0}                                   # a dict() copy is a snapshot
