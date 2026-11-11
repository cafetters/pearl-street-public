"""D58 margin test tools (bench/d58_margin.py, bench/d58_queue.py): the bisection, the gates' edges, the fail-closed
checks, the exact-match rules, the configs, the fresh-window rule and the queue's stall flags. Synthetic inputs only."""
import json

import pytest
import yaml

from bench import d58_margin as G
from bench import d58_queue as Q
from bench.d56_resettle import CheckFailed


# ---- the bisection (pre-registered) ----
@pytest.mark.parametrize("verdicts, expected", [
    ({}, 3.0),
    ({3.0: "PASS"}, 1.0),
    ({3.0: "FAIL_horizon"}, 10.0),
    ({3.0: "FAIL_relief"}, 1.0),
    ({3.0: "FAIL_settlement"}, None),
    ({3.0: "INCOMPLETE"}, None),
    ({3.0: "CHECK_FAILED"}, None),
    ({3.0: "PASS", 1.0: "FAIL_relief"}, None),
    ({3.0: "FAIL_horizon", 10.0: "PASS"}, None),
])
def test_bisection_runs_3_first_then_at_most_one_more(verdicts, expected):
    assert G.next_margin(verdicts) == expected


@pytest.mark.parametrize("args, expected", [
    ((True, False, None, None), "FAIL_relief"),
    ((True, True, True, True), "FAIL_relief"),
    ((False, False, None, None), "INCOMPLETE"),
    ((False, True, False, True), "FAIL_settlement"),
    ((False, True, False, False), "FAIL_settlement"),
    ((False, True, True, False), "FAIL_horizon"),
    ((False, True, True, True), "PASS"),
])
def test_verdict_order_relief_then_complete_then_settlement_then_horizon(args, expected):
    assert G.classify(*args) == expected


def test_window_order_is_jan12_nov28_then_the_horizon_cells_order():
    from bench.d58_horizon import CELLS
    rest = [c.split("_")[2] for c in CELLS if c.split("_")[2] not in ("jan12", "nov28")]
    assert G.WINDOWS == ["jan12", "nov28", *rest] and len(G.WINDOWS) == 11


def test_m0_cuts_are_the_horizon_bystander_values():
    assert set(G.M0_CUT_MW) == set(G.WINDOWS)
    f = G.REPO / "runs" / "d58" / "horizon.json"
    if not f.exists():
        pytest.skip("runs/d58/horizon.json is git-ignored")
    by = json.loads(f.read_text())["bystander"]
    assert {w: by[G.M0_RUN[w]]["cut_from_first_gate_mw"] for w in G.WINDOWS} == G.M0_CUT_MW


# ---- the relief floor ----
def test_relief_floor_passes_at_exactly_75_percent_and_fails_just_below(monkeypatch):
    for cut, ok in ((75.0, True), (75.0 - 1e-6, False), (120.0, True), (0.0, False)):
        monkeypatch.setattr(G, "cut_mw", lambda c, s, v=cut: v)
        r = G.relief({}, {}, 100.0)
        assert r["pass"] is ok and r["m0_cut_mw"] == 100.0


# ---- fail-closed checks ----
def _cell(run="r", rule="1p", cap=100.0, agents=("A", "B")):
    return {"run": run, "settled_rule": rule, "price_cap_usd_mwh": cap, "agents": [{"agent": a} for a in agents]}


def _summ(m="3.0", roster=("A", "B")):
    return {"header": {"config": {"peak_response_margin_usd_mwh": m}, "roster": list(roster)}}


def test_margin_cell_checks_fail_closed():
    G.check_margin_cell(_cell(), _summ(), 3.0, ["A", "B"])
    with pytest.raises(CheckFailed, match="header margin"):
        G.check_margin_cell(_cell(), _summ("1.0"), 3.0, ["A", "B"])
    with pytest.raises(CheckFailed, match="not settled"):
        G.check_margin_cell(_cell(rule="1"), _summ(), 3.0, ["A", "B"])
    with pytest.raises(CheckFailed, match="not settled"):
        G.check_margin_cell(_cell(cap=500.0), _summ(), 3.0, ["A", "B"])
    with pytest.raises(CheckFailed):                                     # an agent missing from the cell
        G.check_margin_cell(_cell(agents=("A",)), _summ(), 3.0, ["A", "B"])
    with pytest.raises(CheckFailed):                                     # an agent missing from the run header
        G.check_margin_cell(_cell(), _summ(roster=("A",)), 3.0, ["A", "B"])
    G.check_margin_cell(_cell(), {"header": {"config": {}, "roster": ["A", "B"]}}, 0.0, ["A", "B"])    # original m=0


# ---- exact-match rules ----
def test_diff_ignores_wall_clock_timing_header_and_run_names_but_nothing_else():
    a = {"header": {"git_commit": "x"}, "runtime_s": 1, "wall_utc": {"start": "t"},
         "ex": {"price_mode": {"round_secs": [1], "round_wall_utc": ["t1"], "rounds_total": 4,
                               "margin": {"compare_solve_secs": 3.0, "log_entries": 7}}},
         "series": {"x": [1.0, 2.0]}, "id": "h2h_c_jan12_h100_m3-exchange"}
    b = json.loads(json.dumps(a))
    b["header"]["git_commit"], b["runtime_s"], b["wall_utc"]["start"] = "y", 9, "u"
    b["ex"]["price_mode"]["round_secs"], b["ex"]["price_mode"]["round_wall_utc"] = [2], ["t2"]
    b["ex"]["price_mode"]["margin"]["compare_solve_secs"] = 4.0
    b["id"] = "h2h_c_jan12_h100_m3_det-exchange"
    names = ("h2h_c_jan12_h100_m3", "h2h_c_jan12_h100_m3_det")
    assert G.diff(a, b, "", names) == []
    assert G.diff({"r": float("nan")}, {"r": float("nan")}) == [] and G.diff({"r": float("nan")}, {"r": 1.0}) == ["/r"]
    b["series"]["x"][1] = 2.0000001
    b["ex"]["price_mode"]["margin"]["log_entries"] = 8
    assert G.diff(a, b, "", names) == ["/ex/price_mode/margin/log_entries", "/series/x[1]"]


def _bcell(run, gains):
    return {"run": run, "agents": [{"agent": a, "e": 0.0, "alone": 0.0, "components": {"da_rt_gap": 0.0}}
                                   for a in gains], "rules": {"1p": {"net": [-g for g in gains.values()]}}}


def _bsumm(peak, roster):
    return {"header": {"roster": roster}, "exchange_by_headroom_mw": {"100": {"peak_mw": peak}}}


def test_baseline_compare_matches_within_float_noise_only(monkeypatch):
    monkeypatch.setattr(G, "cut_mw", lambda c, s: s["exchange_by_headroom_mw"]["100"]["peak_mw"] / 10)
    o = (_bcell("o", {"A": 1.0, "B": -2.0}), _bsumm(2000.0, ["A", "B"]))
    assert G.baseline_compare(o, (_bcell("n", {"A": 1.0 + 1e-9, "B": -2.0}), _bsumm(2000.0, ["A", "B"])))["match"]
    assert not G.baseline_compare(o, (_bcell("n", {"A": 1.0 + 1e-5, "B": -2.0}), _bsumm(2000.0, ["A", "B"])))["match"]
    assert not G.baseline_compare(o, (_bcell("n", {"A": 1.0, "B": -2.0}), _bsumm(2000.1, ["A", "B"])))["match"]
    with pytest.raises(CheckFailed):
        G.baseline_compare(o, (_bcell("n", {"A": 1.0}), _bsumm(2000.0, ["A"])))


# ---- configs ----
def test_configs_change_only_the_listed_keys():
    texts = G.all_configs(G.FRESH_WINDOW)
    tag = G.fresh_tag()
    assert len(texts) == 11 * 4 + 1 + 4 + 1
    for w in G.WINDOWS:
        src = yaml.safe_load(G.source_config(w).read_text())
        for m in (0.0, *G.MARGINS):
            cfg = yaml.safe_load(texts[G.run_name(w, m)])
            changed = {k for k in set(src) | set(cfg) if src.get(k) != cfg.get(k)}
            assert changed <= {"run_name", "peak_price_cap_usd_mwh", "settlement_reference",
                               "peak_response_margin_usd_mwh"}
            assert cfg["run_name"] == G.run_name(w, m) and cfg["peak_price_cap_usd_mwh"] == 100
            assert cfg["settlement_reference"] == "selfish_1p" and cfg["peak_response_margin_usd_mwh"] == m
    det, m3 = yaml.safe_load(texts[G.DETERMINISM[1]]), yaml.safe_load(texts[G.DETERMINISM[0]])
    assert {k for k in det if det[k] != m3.get(k)} == {"run_name"}
    f3 = yaml.safe_load(texts[G.run_name(tag, 3.0)])
    assert str(f3["start"]) == G.FRESH_WINDOW and f3["m0_dir"] == f"runs/m0_{tag}"
    assert "Jan" not in texts[G.run_name(tag, 3.0)].split("\n", 2)[2]       # no comments about another window


# ---- the fresh window (breadth rule) ----
def test_fresh_window_rule_skips_jan_1_14_near_windows_and_missing_neighbours():
    import datetime as dt
    days = [str(dt.date(2023, 1, 1) + dt.timedelta(days=i)) for i in range(365)]
    peaks = {d: 1000.0 for d in days}
    peaks.update({"2023-01-05": 3000.0, "2023-02-03": 2900.0, "2023-03-01": 2800.0, "2023-12-31": 2950.0,
                  "2023-04-10": 2700.0})
    pick, ranked = G.pick_fresh(peaks, ["2023-01-30", "2023-03-05"])
    assert pick == "2023-04-10"
    why = {r["day"]: r["excluded"] for r in ranked}
    assert "inside Jan 1-14" in why["2023-01-05"][0]
    assert why["2023-12-31"] == ["day before or after missing"]
    assert why["2023-02-03"] == ["within 7 days of 2023-01-30"] and why["2023-03-01"] == ["within 7 days of 2023-03-05"]


# ---- report ----
def test_report_table_leads_with_share_of_ceiling_and_pairs_money_with_m0():
    row = {"share_of_m0_ceiling": 0.9, "value_share_of_m0_ceiling": 0.5, "gap_share": 0.8,
           "m0": {"share_of_m0_ceiling": 0.95, "value_share_of_m0_ceiling": 0.6, "gap_share": 0.9, "harmed": 3,
                  "harm_usd": 100.0, "operator_payout_usd": 5000.0},
           "relief": {"cut_mw": 150.0, "m0_cut_mw": 180.0, "ratio": 150 / 180}, "harmed": 1, "harm_usd": 20.0,
           "settlement_harmed": 0, "operator_payout_usd": 6000.0, "extra_payout_vs_m0_usd": 1000.0,
           "max_round_price_usd_mwh": 40.0}
    md = G.report_md({3.0: {"verdict": "PASS", "windows": {"jan12": row}}})
    head = next(ln for ln in md.splitlines() if ln.startswith("| Window"))
    assert head.split("|")[2].strip().startswith("Share of M0 ceiling")
    assert "90.0% (95.0%)" in md and "6,000 (5,000)" in md and "20 (100)" in md


# ---- the queue ----
def test_stall_flags_gaps_over_five_minutes_between_consecutive_marks():
    marks = [("selfish start", "2026-10-29T10:00:00+00:00"), ("selfish end", "2026-10-29T10:01:00+00:00"),
             ("round 1", "2026-10-29T10:05:59+00:00"), ("round 2", "2026-10-29T10:11:00+00:00")]
    gaps = Q.stall_gaps(marks)
    assert [(g["from"], g["to"]) for g in gaps] == [("round 1", "round 2")] and gaps[0]["seconds"] == 301.0


def test_wall_marks_cover_selfish_exchange_and_every_round():
    s = {"selfish": {"wall_utc": {"start": "a", "end": "b"}},
         "exchange_by_headroom_mw": {"100": {"wall_utc": {"start": "c", "end": "f"},
                                             "price_mode": {"round_wall_utc": ["d", "e"]}}}}
    assert [t for _, t in Q.wall_marks(s)] == ["a", "b", "c", "d", "e", "f"]


def test_keep_awake_can_be_set_and_cleared():
    import sys
    ok = Q.keep_awake(True)
    try:
        assert ok == (sys.platform == "win32")
    finally:
        Q.keep_awake(False)
