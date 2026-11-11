"""warmup_days (owner, Nov 6): the run starts N local days early; metrics and money cover only the reported window.

Synthetic: the two-day slice from test_nomination_span, with day 1 as the "window" and day 0 as the warm-up. The
real-trace check is the warm-up runs themselves (docs/overnight/2026-11-06-cloud.md)."""
import json

import numpy as np
import pytest

from tests.test_nomination_span import DAY0_END, ISO, two_day_slice

W0 = DAY0_END                      # first window step: Jan 3 00:00 Central
START = ISO[W0]
K = 0.25 / 1000.0                  # MWh per kW-step at 15 minutes


def _cfg(**kw):
    from tests.test_replay import CFG_M11
    return {**CFG_M11, "rounds": "info", "price_mode": True, "plan_to_nomination": True,
            "release_superseded_lock": True, "price_rounds_per_gate": 5, "settlement_reference": "selfish_1p",
            "nomination_span": "operating_day", **kw}


def test_warmup_days_values_and_refusals():
    from sim.run_m1 import warmup_days
    assert warmup_days({}) == 0 and warmup_days({"warmup_days": 0}) == 0
    assert warmup_days(_cfg(warmup_days=1)) == 1
    for bad in (-1, 1.5, "1", True, None):
        with pytest.raises(ValueError):
            warmup_days(_cfg(warmup_days=bad))
    for refused in ({"price_mode": False}, {"prep_clearing": True}, {"no_rebound": True}, {"daily_energy": True},
                    {"d62_guarantee": True}, {"delivery_record": True}):
        with pytest.raises(ValueError):
            warmup_days(_cfg(warmup_days=1, **refused))
        assert warmup_days(_cfg(**refused)) == 0                    # without warm-up, nothing is refused


def test_exchange_settle_from_is_off_by_default_and_arm_c_only():
    from exchange.core import Exchange
    base = dict(run_id="r", operator_id="OP", metering_id="m", clock_id="c", zones=["SYS"])
    assert "settle_from_utc" not in Exchange(**base, price_mode=True).config
    ex = Exchange(**base, price_mode=True, settle_from_utc=START)
    assert ex.config["settle_from_utc"] == START and ex._settles(START) and not ex._settles(ISO[W0 - 1])
    with pytest.raises(ValueError):
        Exchange(**base, settle_from_utc=START)                       # not price mode
    with pytest.raises(ValueError):
        Exchange(**base, price_mode=True, settle_from_utc="tomorrow")


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    from sim.run_m1 import run_variant
    d = tmp_path_factory.mktemp("warm")
    s = two_day_slice()
    out = {"s": s, "dir": d}
    for rule in ("today", "max_first_call"):
        cfg = _cfg(run_name="w", settlement_rule=rule, warmup_days=1)
        out[rule] = {
            "cfg": cfg,
            "plain": run_variant(cfg, s, np.arange(2), d / f"{rule}_plain", headroom_mw=0.03),
            "warm": run_variant(cfg, s, np.arange(2), d / f"{rule}_warm", headroom_mw=0.03, window_start_utc=START),
            "sf": run_variant(cfg, s, np.arange(2), d / f"{rule}_warm", window_start_utc=START)}
    return out


def _trace(d, variant="exchange_h0.03_rinfo"):
    return [json.loads(x) for x in (d / variant / "trace.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("rule", ["today", "max_first_call"])
def test_settling_from_the_window_changes_money_only(runs, rule):
    r = runs[rule]
    for f in ("C", "D", "X", "SOC"):                 # agents never plan on money: same physics
        assert np.array_equal(r["plain"][f], r["warm"][f])
    prices = lambda x: [(m["t_utc"], m["payload"]["call_id"], m["payload"]["price_usd_mwh"])    # noqa: E731
                        for m in x["log"] if m["msg_type"] == "peak_price"]
    assert prices(r["plain"]) == prices(r["warm"])
    hdr = _trace(runs["dir"] / f"{rule}_warm")[0]
    assert hdr["exchange"]["settle_from_utc"] == START and hdr["warmup"] == {"warmup_days": 1,
                                                                             "window_start_utc": START}
    plain_hdr = _trace(runs["dir"] / f"{rule}_plain")[0]
    assert "settle_from_utc" not in plain_hdr["exchange"] and "warmup" not in plain_hdr
    ps = {m["to"]: m["payload"] for m in r["warm"]["log"] if m["msg_type"] == "peak_settle"}
    assert ps and all(p["window"]["start_utc"] >= START for p in ps.values())
    if rule == "today":       # 1p: each agent's charge is the settled price on its metered load, window steps only
        settled = {t: p for t, (p, _) in r["warm"]["peak_settled"].items() if p > 1e-9 and t >= START}
        assert settled
        net = {}            # from the trace: bus.log may keep telemetry without its payload (fix 1)
        for m in _trace(runs["dir"] / f"{rule}_warm")[1:]:
            if m["msg_type"] == "telemetry" and "net_kw" in m["payload"]:
                net.setdefault(m["payload"]["asset_id"].split("/")[0], {})[m["t_utc"]] = m["payload"]["net_kw"]
        for aid, p in ps.items():
            want = sum(pr * -net[aid].get(t, 0.0) * K for t, pr in settled.items())
            assert p["charge_usd"] == pytest.approx(want, abs=1e-9) and p["steps"] == len(settled)
    imb = lambda x: [m for m in x["log"] if m["msg_type"] == "imbalance"]       # noqa: E731
    assert sum(m["payload"]["deviation_kwh"] for m in imb(r["warm"])) <= sum(
        m["payload"]["deviation_kwh"] for m in imb(r["plain"])) + 1e-9


@pytest.mark.parametrize("rule", ["today", "max_first_call"])
def test_window_view_reports_the_window_only(runs, rule):
    from sim.run_m1 import evaluate, harm_components, net_harm, window_result, window_slice
    r, s = runs[rule], runs["s"]
    cfg = r["cfg"]
    sw = window_slice(s, W0)
    a = sw.agent
    rec = sw.system["system_load"] * 1000.0 - (np.nan_to_num(a["pv"]) - a["curt"] + a["bess"]).sum(0)
    ex, sf = window_result(r["warm"], W0, ISO), window_result(r["sf"], W0, ISO)
    assert ex["C"].shape[1] == len(ISO) - W0 and np.array_equal(ex["SOC_start"], r["warm"]["SOC"][:, W0 - 1])
    assert all(m["t_utc"] >= START for m in ex["log"]) and all(t >= START for t in ex["peak_settled"])
    sev = evaluate(cfg, sw, np.arange(2), sf, rec, None, sf)
    ev = evaluate(cfg, sw, np.arange(2), ex, rec, None, sf)
    full = r["warm"]
    net_full = (s.system["system_load"] * 1000.0 - (np.nan_to_num(s.agent["pv"]) - s.agent["curt"]
                + s.agent["bess"]).sum(0)) + (np.nan_to_num(s.agent["pv"]) - s.agent["curt"] + s.agent["bess"]
                                              ).sum(0) - (np.nan_to_num(s.agent["pv"]) - s.agent["curt"]
                                                          - full["X"] + full["D"] - full["C"]).sum(0)
    assert ev["peak_mw"] == pytest.approx(net_full[W0:].max() / 1e3) and ev["peak_utc"] >= START
    pay = sum(m["payload"]["refund_usd"] - m["payload"]["charge_usd"] for m in ex["log"]
              if m["msg_type"] == "peak_settle")
    assert ev["price_mode"]["operator_net_payout_usd"] == pytest.approx(pay)
    assert all(g["call_id"] for g in ev["price_mode"]["gates"])
    # harm: stored energy at the window start is valued like the energy at the end, and the parts add up
    p_end = float(sw.system["price_da"].mean()) / 1000.0
    alone = sev["_agent_value"] + (sf["SOC"][:, -1] - sf["SOC_start"]) * p_end
    with_ps = ev["_agent_value"] + (ex["SOC"][:, -1] - ex["SOC_start"]) * p_end
    ids = [ag.id for ag in ex["agents"]]
    nh = net_harm(alone, with_ps, ids, ex["header"]["roster"], 0)
    comp = harm_components(ev["_components"], nh, alone)          # asserts each agent's parts sum to its total
    assert "start_energy" in comp["all_agents"]
    for row, al, wp in zip(ev["_components"], alone, with_ps):
        assert row["total"] == pytest.approx(al - wp, abs=0.02)      # harm_components' own tolerance
