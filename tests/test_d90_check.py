"""bench/d90_check.py: D90's infeasible_declare verdict (docs/plans/d90_check_plan.md, approved by the owner Nov 7).

Synthetic tests use hand-built headers and traces with an injected planner (no data). The real-trace tests re-run
the check on D61's four infeasible_declare traces (at 74b1ae0, from ../pearl-street-d61run) and on the rehearsal's
four (at d471a72, from a checkout of that commit); they skip when those folders are absent."""
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from bench import d90_check as D

SHA = "b" * 40
ISO = ["2023-01-12T06:00:00Z", "2023-01-12T12:00:00Z", "2023-01-12T18:00:00Z", "2023-01-13T00:00:00Z",
       "2023-01-13T06:00:00Z", "2023-01-13T12:00:00Z", "2023-01-13T18:00:00Z", "2023-01-14T00:00:00Z"]
LOC = ["2023-01-12"] * 4 + ["2023-01-13"] * 4
ATK = [{"agent": "DC_08", "type": "infeasible_declare"}]


def _header(attackers=ATK, roster=("DC_08", "PVB_01"), commit=SHA):
    h = {"event": "header", "git_commit": commit, "roster": list(roster),
         "config": {"start": "2023-01-12", "end": "2023-01-13", "slice_tz": "America/Chicago", "wear_usd_mwh": "10"},
         "exchange": {"step_minutes": 15}}
    if attackers is not None:
        h["attackers"] = attackers
    return h


def _msg(typ, frm, to, payload, t="2023-01-12T06:00:00Z", mid=None):
    return {"msg_type": typ, "sender": frm, "to": to, "t_utc": t, "msg_id": mid or f"{frm}:{typ}", "payload": payload}


OUTAGE = _msg("declare_outage", "DC_08", "exchange",
              {"asset_id": "DC_08/bess", "window": {"start_utc": ISO[0], "end_utc": ISO[4]}}, mid="DC_08:4")


def _avail(reserve):
    return _msg("declare_availability", "DC_08", "exchange",
                {"asset_id": "DC_08/bess", "date": "2023-01-13", "planned_outage": False, "power_kw": 0.0,
                 "reserve_kwh": reserve}, mid="DC_08:5")


def _reject(ref="DC_08:5", t="2023-01-12T06:00:00Z", code="infeasible_declaration"):
    return _msg("reject", "exchange", "DC_08", {"code": code, "ref_msg_id": ref}, t=t, mid="ex:2")


TELEMETRY = [_msg("telemetry", "metering", "exchange", {"asset_id": "DC_08/bess", "meter_kw": 0.0}, t=t, mid=f"m:{i}")
             for i, t in enumerate(ISO)]


def _plan(aid, pmax, floor):
    """Feasible unless some step's SOC floor is above 100 kWh."""
    return not np.any(np.nan_to_num(floor, nan=0.0) > 100.0)


def _judge(log, header=None):
    return D.judge(header or _header(), [*TELEMETRY, *log], ISO, LOC, _plan)


def test_a_filed_set_that_leaves_1p_feasible_is_not_exercised():
    res = _judge([OUTAGE, _avail(50.0)])
    a = res["attackers"]["DC_08"]
    assert res["verdict"] == a["verdict"] == "NOT_EXERCISED"
    assert a["infeasible_at_filing"] is False and a["made_infeasible_by"] is None
    assert a["feasible_after"] == [["DC_08:4", True], ["DC_08:5", True]]


def test_an_infeasible_filed_set_with_d82_fired_at_filing_passes():
    res = _judge([OUTAGE, _avail(500.0), _reject()])
    a = res["attackers"]["DC_08"]
    assert res["verdict"] == "PASS"
    assert a["infeasible_at_filing"] is True and a["made_infeasible_by"] == "DC_08:5"
    assert a["d82_fired"] is True and a["d82_at_filing"] is True and a["d82_later_drop"] is False


def test_a_later_d82_drop_counts_as_fired():
    res = _judge([OUTAGE, _avail(500.0), _reject(ref="DC_08:5", t="2023-01-12T18:00:00Z")])
    a = res["attackers"]["DC_08"]
    assert res["verdict"] == "PASS" and a["d82_at_filing"] is False and a["d82_later_drop"] is True


def test_an_infeasible_filed_set_without_d82_fails():
    assert _judge([OUTAGE, _avail(500.0)])["verdict"] == "FAIL"


def test_d82_fired_but_the_replay_finds_the_set_feasible_disagrees():
    assert _judge([OUTAGE, _avail(50.0), _reject()])["verdict"] == "DISAGREE"


def test_only_a_reject_to_the_attacker_with_the_exact_code_counts():
    rec = _msg("delivery_record", "exchange", "DC_08", {"record": {"infeasible_declaration_calls": 0}}, mid="ex:9")
    other = _reject(code="malformed")
    to_other = {**_reject(), "to": "PVB_01"}
    assert _judge([OUTAGE, _avail(500.0), rec, other, to_other])["verdict"] == "FAIL"


def test_limits_accumulate_in_filing_order():
    sets = D.limits([OUTAGE, _avail(500.0)], ISO, LOC)
    (m1, p1, f1), (m2, p2, f2) = sets
    assert m1 == "DC_08:4" and m2 == "DC_08:5"
    assert list(p1[:4]) == [0.0] * 4 and np.isnan(p1[4:]).all() and np.isnan(f1).all()
    assert list(p2[:4]) == [0.0] * 4 and list(p2[4:]) == [0.0] * 4
    assert np.isnan(f2[:4]).all() and list(f2[4:]) == [500.0] * 4


@pytest.mark.parametrize("header, log, why", [
    (_header(attackers=[{"agent": "DC_08", "type": "silent"}]), [OUTAGE, _avail(5.0)], "no infeasible_declare"),
    (_header(attackers=None), [OUTAGE, _avail(5.0)], "no infeasible_declare"),
    (_header(), [OUTAGE], "one outage plus one availability"),
    (_header(), [OUTAGE, _avail(5.0), {**_avail(6.0), "msg_id": "DC_08:6"}], "one outage plus one availability"),
    (_header(roster=("PVB_01",)), [OUTAGE, _avail(5.0)], "roster"),
    (_header(commit=SHA + "-dirty"), [OUTAGE, _avail(5.0)], "commit"),
    (_header(commit=""), [OUTAGE, _avail(5.0)], "commit"),
])
def test_the_check_fails_closed(header, log, why):
    with pytest.raises(D.Refused, match=why):
        _judge(log, header)


def test_a_data_window_that_is_not_the_traces_clock_fails_closed():
    with pytest.raises(D.Refused, match="clock"):
        D.judge(_header(), [*TELEMETRY[:-1], OUTAGE, _avail(5.0)], ISO, LOC, _plan)
    with pytest.raises(D.Refused, match="clock"):
        D.judge(_header(), [*TELEMETRY, OUTAGE, _avail(5.0)], ISO[1:], LOC[1:], _plan)


def test_the_commit_guard_refuses_a_different_code_commit():
    D.guard_commit(_header(), SHA)
    with pytest.raises(D.Refused, match="commit"):
        D.guard_commit(_header(), "c" * 40)
    with pytest.raises(D.Refused, match="commit"):
        D.guard_commit(_header(), None)


def test_the_cli_exits_2_and_writes_the_refusal(tmp_path):
    run = tmp_path / "run"
    (run / "exchange_h100_rinfo").mkdir(parents=True)
    lines = [json.dumps(_header(attackers=None))] + [json.dumps({**m, "from": m["sender"]}) for m in TELEMETRY]
    (run / "exchange_h100_rinfo" / "trace.jsonl").write_text("\n".join(lines) + "\n")
    out = tmp_path / "out.json"
    assert D.main([str(run), "--out", str(out)]) == 2
    res = json.loads(out.read_text())
    assert res["verdict"] == "REFUSED" and "no infeasible_declare" in res["reason"]


# ---- real traces (report only; CLAUDE.md: a check runs on real saved traces before it enters a pre-registration) ---

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT.parent / "data"
D61RUN = ROOT.parent / "pearl-street-d61run"
REHEARSAL_RUNS = ROOT.parent / "pearl-street" / "runs" / "cloud"
REHEARSAL_CODE = ROOT.parent / "pearl-street-d471"          # a clean checkout of d471a72 (the rehearsal's commit)
EXPECTED = {"jan12": "NOT_EXERCISED", "feb02": "NOT_EXERCISED", "jan06": "PASS", "jun29": "PASS"}


def _cli(run_dir: Path, code_root: Path, tmp_path) -> dict:
    out = tmp_path / f"{run_dir.name}.json"
    r = subprocess.run([sys.executable, "-m", "bench.d90_check", str(run_dir), "--code-root", str(code_root),
                        "--data-dir", str(DATA), "--out", str(out)], cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": ""})
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(out.read_text())


@pytest.mark.skipif(not (D61RUN / "runs").exists() or not DATA.exists(), reason="needs ../pearl-street-d61run")
@pytest.mark.parametrize("w", sorted(EXPECTED))
def test_real_trace_d61_infeasible_declare(w, tmp_path):
    res = _cli(D61RUN / "runs" / f"h2h_c_d61_{w}_infeasible_declare_m5", D61RUN, tmp_path)
    assert res["verdict"] == EXPECTED[w], res


REHEARSAL_RECHECK = ROOT.parent / "pearl-street" / "runs" / "nov07" / "v2r" / "d90_recheck.json"


@pytest.mark.skipif(not REHEARSAL_CODE.exists() or not DATA.exists() or not REHEARSAL_RECHECK.exists(),
                    reason="needs a d471a72 checkout and the rehearsal's offline recheck")
@pytest.mark.parametrize("w", sorted(EXPECTED))
def test_real_trace_rehearsal_infeasible_declare_matches_the_offline_recheck(w, tmp_path):
    name = f"h2h_c_v2r_{w}_infeasible_declare_m5"
    run = REHEARSAL_RUNS / name
    old = json.loads(REHEARSAL_RECHECK.read_text()).get(name)
    if not (run / "DONE").exists() or not old:
        pytest.skip(f"{name} not downloaded or not rechecked")
    a = _cli(run, REHEARSAL_CODE, tmp_path)["attackers"]["DC_08"]
    assert a["verdict"] == old["d90_verdict"]
    assert a["infeasible_at_filing"] == old["infeasible_at_filing"]
    assert a["feasible_after"][0][1] == old["feasible_outage_only"]
    assert a["feasible_after"][1][1] == old["feasible_outage_plus_reserve"]
    assert a["d82_fired"] == old["d82_fired_in_run"]
