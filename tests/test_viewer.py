"""Run viewer (exchange/trace/viewer.py): one self-contained HTML file from summary.json and trace.jsonl only."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from exchange.trace import viewer
from sim.run_m1 import run_variant
from tests.test_replay import CFG, CFG_M11, synthetic_slice


def embedded(html: str) -> dict:
    return json.loads(re.search(r"const DATA = (.*?);\nconst S", html, re.S).group(1))


def trace_events(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines()[1:]]


def make_run(tmp_path, cfg, variants, summary_extra):
    """A run directory holding only trace files and a summary.json, like runs/<run>/."""
    run = tmp_path / "run"
    s = synthetic_slice()
    for h, rounds in variants:
        run_variant(cfg, s, np.arange(2), run, headroom_mw=h, rounds=rounds)
    T = len(s.ts_utc)
    series = {"t_utc": list(s.ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ")), "t_central": [f"c{i}" for i in range(T)],
              "recorded_kw": [1000.0] * T, "selfish_kw": [1001.0] * T}
    summary = {"header": {"git_commit": "abc1234", "seed": 1, "run_at_utc": "now"}, "series": series,
               "selfish": {"peak_mw": 1.001, "energy_revenue_usd": -5.0}, **summary_extra}
    (run / "summary.json").write_text(json.dumps(summary))
    return run, summary


def test_m1_viewer_is_self_contained_and_matches_trace_and_summary(tmp_path):
    block = {"peak_mw": 0.99, "pass": True, "unfilled_need_kwh": 12.5, "rebound": {"peak_outside_windows_mw": 0.9,
             "peak_moved_outside_windows": False}}
    run, summary = make_run(tmp_path, CFG, [(None, None), (0.1, None)], {"exchange_by_headroom_mw": {"0.1": block}})
    html = viewer.build(run).read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)\s*=\s*["']?https?://""", html) and "<link" not in html
    d = embedded(html)
    assert d["order"] == ["selfish", "exchange_h0.1"]
    ex = d["variants"]["exchange_h0.1"]
    assert ex["summary_path"] == "exchange_by_headroom_mw.0.1" and ex["summary"] == block
    assert d["variants"]["selfish"]["summary"] == summary["selfish"]
    assert d["series"] == summary["series"]

    events = trace_events(run / "exchange_h0.1" / "trace.jsonl")
    n = lambda t, **kw: sum(1 for e in events if e["msg_type"] == t and all(e[k] == v for k, v in kw.items()))
    assert ex["counts"]["post_need"] == n("post_need", **{"from": "GRID_OP"}) > 0
    assert ex["counts"]["offer"] == n("offer", to="exchange") > 0
    assert ex["counts"]["award"] == n("award") > 0 and ex["counts"]["settle"] == n("settle")
    credits = sum(e["payload"]["credit_usd"] for e in events if e["msg_type"] == "settle")
    assert sum(a["credits_usd"] for a in ex["agents"]) == pytest.approx(credits, abs=0.02)
    assert sorted(a["id"] for a in ex["agents"]) == ["A", "B"]
    assert sum(a["awards"] for a in ex["agents"]) == n("award")


def test_m1_1_viewer_maps_rounds_and_rolls_up_nominations_and_imbalance(tmp_path):
    main = {"peak_mw": 0.98, "imbalance_honest": {"charge_usd": 0.0, "deviation_beyond_band_kwh": 0.0}}
    sweep3 = {"peak_mw": 0.97, "unfilled_need_kwh": 1.0}
    run, _ = make_run(tmp_path, CFG_M11, [(None, None), (0.1, 2), (0.1, 3)],
                      {"rounds": 2, "exchange_by_headroom_mw": {"0.1": main},
                       "rounds_sweep": {"headroom_mw": 0.1, "by_rounds": {"2": {"peak_mw": 0.98}, "3": sweep3}}})
    d = embedded(viewer.build(run).read_text(encoding="utf-8"))
    assert d["order"] == ["selfish", "exchange_h0.1_r2", "exchange_h0.1_r3"]
    r2, r3 = d["variants"]["exchange_h0.1_r2"], d["variants"]["exchange_h0.1_r3"]
    assert (r2["summary_path"], r2["summary"]) == ("exchange_by_headroom_mw.0.1", main)
    assert (r3["summary_path"], r3["summary"]) == ("rounds_sweep.by_rounds.3", sweep3)
    events = trace_events(run / "exchange_h0.1_r2" / "trace.jsonl")
    noms = sum(1 for e in events if e["msg_type"] == "nominate")
    assert noms > 0 and r2["counts"]["nominate"] == noms
    assert sum(a["nominations"] for a in r2["agents"]) == noms
    assert r2["counts"]["call_nominations"] == 3 and r2["counts"]["nomination_aggregate"] == 3
    imb = sum(e["payload"]["charge_usd"] for e in events if e["msg_type"] == "imbalance")
    assert sum(a["imbalance_usd"] for a in r2["agents"]) == pytest.approx(imb, abs=0.02)
    assert r2["counts"]["imbalance"] == sum(1 for e in events if e["msg_type"] == "imbalance") > 0


def test_missing_series_still_builds_with_a_notice(tmp_path):
    run, summary = make_run(tmp_path, CFG, [(0.1, None)], {"exchange_by_headroom_mw": {}})
    del summary["series"]
    (run / "summary.json").write_text(json.dumps(summary))
    html = viewer.build(run).read_text(encoding="utf-8")
    assert embedded(html)["series"] is None
    assert "has no net-load series" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_inline_script_is_valid_javascript(tmp_path):
    run, _ = make_run(tmp_path, CFG_M11, [(None, None), (0.1, 2)], {"rounds": 2, "exchange_by_headroom_mw": {}})
    html = viewer.build(run).read_text(encoding="utf-8")
    js = tmp_path / "viewer.js"
    js.write_text(re.search(r"<script>(.*)</script>", html, re.S).group(1), encoding="utf-8")
    r = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_viewer_reads_only_trace_and_summary():
    src = Path(viewer.__file__).read_text(encoding="utf-8")
    code = src.split("TEMPLATE =")[0]
    assert not re.search(r"^\s*(import sim|from sim)", code, re.M)
    for banned in ("read_parquet", "netload.csv", "np.load", "dual_15min"):
        assert banned not in code
    assert re.findall(r"\.glob\(([^)]*)\)", code) == ['"*/trace.jsonl"']


def test_net_harm_is_shown_from_summary(tmp_path):
    harm = {"agents_harmed": 1, "total_net_harm_usd": 51.0, "by_agent_usd": {"A": 51.0, "B": 0.0}, "pass": False}
    block = {"peak_mw": 0.99, "net_harm": harm}
    run, _ = make_run(tmp_path, CFG, [(0.1, None)], {"exchange_by_headroom_mw": {"0.1": block}})
    html = viewer.build(run).read_text(encoding="utf-8")
    assert embedded(html)["variants"]["exchange_h0.1"]["summary"]["net_harm"] == harm
    assert "Net harm (vs acting alone)" in html and '"net_harm_usd", "Net harm $"' in html


def test_traces_not_in_the_summary_are_skipped_and_listed(tmp_path):
    run, summary = make_run(tmp_path, CFG_M11, [(None, None), (0.1, "info"), (0.1, 2)],
                            {"rounds": "info", "exchange_by_headroom_mw": {"0.1": {"peak_mw": 0.9}}})
    d = embedded(viewer.build(run).read_text(encoding="utf-8"))
    assert d["order"] == ["selfish", "exchange_h0.1_rinfo"]          # mixed str/int rounds sort fine
    assert d["skipped_traces"] == ["exchange_h0.1_r2"]


def test_price_mode_viewer_shows_price_rounds_and_peak_settlement(tmp_path):
    cfg = {**CFG_M11, "rounds": "info", "price_mode": True, "plan_to_nomination": True,
           "release_superseded_lock": True, "price_rounds_per_gate": 10}
    run, _ = make_run(tmp_path, cfg, [(None, None), (0.03, "info")],
                      {"rounds": "info", "exchange_by_headroom_mw": {"0.03": {"peak_mw": 0.99}}})
    d = embedded(viewer.build(run).read_text(encoding="utf-8"))
    ex = d["variants"]["exchange_h0.03_rinfo"]
    events = trace_events(run / "exchange_h0.03_rinfo" / "trace.jsonl")
    prices = [e["payload"] for e in events if e["msg_type"] == "peak_price"]
    settles = [e["payload"] for e in events if e["msg_type"] == "peak_settle"]
    assert ex["counts"]["peak_price"] == len(prices) > 0 and ex["counts"]["peak_settle"] == len(settles) > 0
    assert sum(e["final"] for e in ex["events"]["peak_price"]) == sum(p["final"] for p in prices)
    assert all("price_usd_mwh" not in e for e in ex["events"]["peak_price"])      # per-round arrays not embedded
    assert sum(e["net"] for e in ex["events"]["peak_settle"]) == pytest.approx(sum(p["net_usd"] for p in settles),
                                                                              abs=0.01 * len(settles))
