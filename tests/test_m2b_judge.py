"""M2b judge (bench/m2b_judge.py; pass test in docs/plans/m2b_dc_plan.md): the D98 bystander line between a deferral-on
run and its deferral-off twin on unrounded shortfalls, and fail-closed input checks. Synthetic summaries only."""
import json

import pytest

from bench import m2b_judge as J

ROSTER = ["A", "B", "DC_01"]


def _write(d, short, value, commit="abc123", dc=True, extra=None):
    d.mkdir(parents=True)
    ev = {"net_harm": {"by_agent_usd": {k: round(max(v, 0.0), 2) for k, v in short.items()}, "shortfall_usd": short,
                       "pass": True, "missing_from_check": []},
          "agent_value_usd": value, **(extra or {})}
    (d / "summary.json").write_text(json.dumps({"exchange_by_headroom_mw": {"100": ev},
                                                "header": {"config": {"dc_deferral": str(dc)}}}))
    (d / "exchange_h100_rinfo").mkdir()
    (d / "exchange_h100_rinfo" / "trace.jsonl").write_text(json.dumps({"git_commit": commit, "roster": ROSTER,
                                                                       "attackers": []}) + "\n")
    return d


def test_line_passes_when_nobody_is_newly_or_further_below(tmp_path):
    on = _write(tmp_path / "on", {"A": -5.0, "B": 2.0, "DC_01": -1.0}, {"A": 100.0, "B": 50.0, "DC_01": 10.0})
    off = _write(tmp_path / "off", {"A": -4.0, "B": 3.0, "DC_01": -0.5}, {"A": 99.0, "B": 49.0, "DC_01": 9.5})
    out = J.bystander_line(on, off)
    assert out["verdict"] == "PASS" and not out["newly_below_acting_alone"]


def test_a_millionth_above_the_guard_is_newly_below(tmp_path):
    on = _write(tmp_path / "on", {"A": 2e-6, "B": -1.0, "DC_01": -1.0}, {"A": 1.0, "B": 1.0, "DC_01": 1.0})
    off = _write(tmp_path / "off", {"A": 0.0, "B": -1.0, "DC_01": -1.0}, {"A": 1.0, "B": 1.0, "DC_01": 1.0})
    out = J.bystander_line(on, off)
    assert out["verdict"] == "FAIL" and list(out["newly_below_acting_alone"]) == ["A"]


def test_further_below_fails_only_beyond_section_8(tmp_path):
    on = _write(tmp_path / "on", {"A": 9.0, "B": 30.0, "DC_01": 0.0}, {"A": 100.0, "B": 100.0, "DC_01": 1.0})
    off = _write(tmp_path / "off", {"A": 1.0, "B": 1.0, "DC_01": 0.0}, {"A": 108.0, "B": 129.0, "DC_01": 1.0})
    out = J.bystander_line(on, off)
    assert set(out["further_below_acting_alone"]) == {"A", "B"}
    assert list(out["further_below_judged"]) == ["B"] and out["verdict"] == "FAIL"


def test_fails_closed_on_different_commits_or_missing_unrounded_field(tmp_path):
    on = _write(tmp_path / "on", {"A": 0.0, "B": 0.0, "DC_01": 0.0}, {"A": 1.0, "B": 1.0, "DC_01": 1.0})
    off = _write(tmp_path / "off", {"A": 0.0, "B": 0.0, "DC_01": 0.0}, {"A": 1.0, "B": 1.0, "DC_01": 1.0}, commit="zzz")
    with pytest.raises(J.CheckFailed):
        J.bystander_line(on, off)
    s = json.loads((off / "summary.json").read_text())
    del s["exchange_by_headroom_mw"]["100"]["net_harm"]["shortfall_usd"]
    (off / "summary.json").write_text(json.dumps(s))
    with pytest.raises(J.CheckFailed):
        J.bystander_line(on, off)


# ---- round 2 as revised (docs/plans/m2b_round2b_prereg.md; owner, Nov 20) ----

def _run(d, *, short, value, peak, peak_c, alone, alone_c, nh_pass=True, harmed=0, harm=0.0, dc_pass=True,
         commit="abc123", imb=0.0):
    ev = {"peak_mw": peak, "controllable": {"peak_mw": peak_c, "pre_from_rebound": False},
          "dc_deferral": {"exchange": {"pass": dc_pass, "agents": {"DC_01": {}}, "violations_total": 0,
                                       "missing_from_check": []},
                          "acting_alone": {"pass": True, "violations_total": 0}, "share_of_relief": {"dc_share": 0.5},
                          "declared_tighter": []},
          "imbalance_all": {"charge_usd": imb, "agents_charged": int(imb > 0)}}
    _write(d, short, value, commit=commit, extra=ev)
    s = json.loads((d / "summary.json").read_text())
    s["selfish"] = {"peak_mw": alone, "controllable": {"peak_mw": alone_c}}
    s["exchange_by_headroom_mw"]["100"]["net_harm"].update({"pass": nh_pass, "agents_harmed": harmed,
                                                           "total_net_harm_usd": harm})
    (d / "summary.json").write_text(json.dumps(s))
    from tests.test_m2b_round2b import _netload
    import numpy as np
    _netload(d, np.full(192, 2100.0))                     # the operator's target: 2,100 - 100 = 2,000 MW each day
    with open(d / "exchange_h100_rinfo" / "trace.jsonl", "a") as f:
        f.write(json.dumps({"msg_type": "peak_settle", "to": "DC_01",
                            "payload": {"net_usd": 1.0, "refund_usd": 2.0, "charge_usd": 1.0}}) + "\n")
    return d


def _round2(tmp_path, monkeypatch, on_peak_c=2000.5, off_nh_pass=True, don=1990.0, doff=1995.0, gap=10.0):
    from tests.test_m2b_round2b import CFG
    import yaml
    monkeypatch.setattr(J, "WINDOWS_R2", ("w1", "w2"))
    monkeypatch.setattr(J, "N_CEILINGS", 2)
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    for w in ("w1", "w2"):
        c = tmp_path / f"m2b2_ceil_{w}"
        c.mkdir()
        (c / "DONE").write_text("ok")
        row = {"battery_only_solved": True, "with_deferral_solved": True, "battery_only_peak_mw": 1900.0,
               "with_deferral_peak_mw": 1900.0 - gap, "recorded_peak_mw": 2500.0, "dc_share_of_ceiling_relief": 0.1}
        (c / "summary.json").write_text(json.dumps({"variants": {"cold": row, "controllable": row}}))
        (cfgs / f"m2b2_{w}_off.yaml").write_text(yaml.safe_dump(CFG))
    z = {"A": -1.0, "B": -1.0, "DC_01": -1.0}
    v = {"A": 1.0, "B": 1.0, "DC_01": 1.0}
    for w in ("w1", "w2"):
        _run(tmp_path / f"m2b2_{w}_on", short=z, value=v, peak=2600.0, peak_c=on_peak_c, alone=2700.0, alone_c=2650.0)
        _run(tmp_path / f"m2b2_{w}_off", short=z, value=v, peak=2100.0, peak_c=2000.0, alone=2300.0, alone_c=2250.0,
             nh_pass=off_nh_pass, harmed=0 if off_nh_pass else 13, harm=0.0 if off_nh_pass else 6818.0)
        _run(tmp_path / f"m2b2_{w}_don", short=z, value=v, peak=don, peak_c=don, alone=2700.0, alone_c=2650.0)
        _run(tmp_path / f"m2b2_{w}_doff", short=z, value=v, peak=doff, peak_c=doff, alone=2300.0, alone_c=2250.0)
    par = tmp_path / "parity.json"
    par.write_text(json.dumps({f"c{i}": "PASS" for i in range(J.N_PARITY)}))
    return J.judge(tmp_path, par, rnd=2, configs=cfgs)


def test_round2_passes_no_harm_and_depth(tmp_path, monkeypatch):
    out = _round2(tmp_path, monkeypatch)
    assert out["items"]["f_no_harm_vs_target"] is True and out["items"]["g_depth_lowers_the_peak"] is True
    assert out["verdict"] == "PASS" and out["depth_windows_judged"] == 2
    row = out["windows"]["w1"]
    assert row["day_targets_mw"] == {"2023-01-12": 2000.0, "2023-01-13": 2000.0}
    assert row["f_no_harm"]["over_reference_mw"] == pytest.approx(0.5)
    assert row["superseded_1mw_check_report_only"]["on"]["exchange_full_mw"] == 2600.0     # full peak beside it
    assert row["payout_split_report_only"]["on"]["data_centers"]["net_usd"] == 1.0


def test_round2_f_fails_beyond_the_band(tmp_path, monkeypatch):
    out = _round2(tmp_path, monkeypatch, on_peak_c=2006.0000001)
    assert out["items"]["f_no_harm_vs_target"] is False and out["verdict"] == "FAIL"


def test_round2_g_fails_when_deferral_does_not_lower_the_deep_cut_peak(tmp_path, monkeypatch):
    out = _round2(tmp_path, monkeypatch, don=1995.0, doff=1995.0)
    assert out["items"]["g_depth_lowers_the_peak"] is False and out["verdict"] == "FAIL"


def test_round2_g_needs_at_least_one_judged_window(tmp_path, monkeypatch):
    out = _round2(tmp_path, monkeypatch, gap=2.0)
    assert out["depth_windows_judged"] == 0 and out["items"]["g_depth_lowers_the_peak"] is False


def test_round2_reports_a_failing_off_twin_as_context_only(tmp_path, monkeypatch):
    out = _round2(tmp_path, monkeypatch, off_nh_pass=False)
    ctx = out["windows"]["w1"]["off_twin_section8"]
    assert ctx["pass"] is False and ctx["agents_harmed"] == 13
    assert out["items"]["b_no_net_harm_and_bystander_line"] is True                 # (b) is judged on the on run


def test_round2_fails_closed_on_a_missing_window_or_configs(tmp_path, monkeypatch):
    _round2(tmp_path, monkeypatch)
    monkeypatch.setattr(J, "WINDOWS_R2", ("w1", "w2", "w3"))
    out = J.judge(tmp_path, tmp_path / "parity.json", rnd=2, configs=tmp_path / "configs")
    assert out["verdict"] == "FAIL" and "error" in out["windows"]["w3"]
    monkeypatch.setattr(J, "WINDOWS_R2", ("w1",))
    out = J.judge(tmp_path, tmp_path / "parity.json", rnd=2)
    assert out["verdict"] == "FAIL" and "configs" in out["windows"]["w1"]["error"]
