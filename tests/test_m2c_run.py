"""M2c step 4c: run plumbing for the zone product (D99, Leaning). run_variant(grid_zones=...) runs arm C with zones on
the synthetic grid: the trace replays (its header carries the full grid_zones), zone_settle joins each agent's
value, the summary reports rounds to converge per gate with zones on and any round-limit gate, and the zone product
lowers the worst overload against acting alone."""
import json

import numpy as np
import pytest

from sim import synth_grid as S
from sim.run_m1 import evaluate, run_variant
from tests.test_replay import CFG_M11

CFG = {**CFG_M11, "rounds": "info", "price_mode": True, "plan_to_nomination": True, "release_superseded_lock": True,
       "price_rounds_per_gate": 12, "settlement_reference": "selfish_1p"}


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("m2c")
    s = S.run_slice(days=1)
    part = np.arange(len(s.ids))
    sf = run_variant(CFG, s, part, tmp)
    r = run_variant(CFG, s, part, tmp, headroom_mw=0.5, grid_zones=S.grid_zones(s))
    return s, part, sf, r, tmp


def test_zone_run_replays_and_its_header_carries_the_zones(runs):
    from exchange.trace.replay import replay
    s, part, sf, r, tmp = runs
    path = next(tmp.glob("exchange_h0.5*/trace.jsonl"))
    head = json.loads(path.read_text().splitlines()[0])
    gz = head["exchange"]["grid_zones"]
    assert [z["key"] for z in gz] == S.MONITORED and len(gz[0]["base_mw"]) == len(s.ts_utc)
    assert replay(path)["match"]
    types = {m["msg_type"] for m in r["log"]}
    assert {"zone_rows", "zone_price", "zone_settle"} <= types


def test_zone_settle_joins_agent_value_and_rounds_are_reported(runs):
    s, part, sf, r, tmp = runs
    rec_net = s.system["system_load"] * 1000.0
    ev = evaluate(CFG, s, part, r, rec_net, None, sf)
    zs = {m["to"]: m["payload"]["net_usd"] for m in r["log"] if m["msg_type"] == "zone_settle"}
    assert zs and ev["zones"]["zone_settle_usd_by_agent"] == pytest.approx(zs)
    gates = ev["zones"]["gates"]
    assert gates and all({"call_id", "rounds", "converged", "round_limit"} <= g.keys() for g in gates)
    assert all(g["converged"] or g["round_limit"] for g in gates)                 # no unhandled non-convergence


def test_zone_product_lowers_the_worst_overload_against_acting_alone(runs):
    s, part, sf, r, tmp = runs
    rating = S.branch_table().set_index("key").rating_mva

    def worst(run):
        f = S.flows_mw(S.injections_from_run(run, s.ids)).abs()
        return {k: float((f[k] - 0.9 * rating[k]).max()) for k in S.MONITORED}

    alone, zoned = worst(sf), worst(r)
    assert all(zoned[k] <= alone[k] + 1e-9 for k in S.MONITORED)
    assert sum(zoned.values()) < sum(alone.values())


def test_no_grid_zones_means_no_zone_message(runs):
    s, part, sf, r, tmp = runs
    plain = run_variant(CFG, s, part, tmp / "plain", headroom_mw=0.5)
    assert not [m for m in plain["log"] if m["msg_type"] in ("zone_rows", "zone_price", "zone_settle")]
