"""bench.run_diff: exact run comparison used by the nomination-cache check and the D69 honest-behavior check."""
import json

from bench.run_diff import diff_runs, first_diff


def make_run(root, commit="abc", lines=None, summary=None, netload="t,kw\n1,2\n", roster=("A", "B")):
    root.mkdir(parents=True)
    hdr = {"event": "header", "git_commit": commit, "run_at_utc": "now", "roster": list(roster), "exchange": {"x": 1}}
    msgs = lines if lines is not None else [{"from": a, "to": "exchange", "msg_type": "register", "payload": {"v": 1.5}}
                                            for a in roster]
    for v in ("selfish", "exchange_h100"):
        (root / v).mkdir()
        h = dict(hdr) if v != "selfish" else {k: x for k, x in hdr.items() if k != "exchange"}
        body = [json.dumps(h, sort_keys=True)] + [json.dumps(m, sort_keys=True) for m in msgs]
        (root / v / "trace.jsonl").write_text("\n".join(body) + "\n")
    s = summary or {"header": {"git_commit": commit, "config": {"run_name": "r"}}, "x": {"runtime_s": 1.0, "peak": 2.0,
                                                                                         "nan": float("nan")}}
    (root / "summary.json").write_text(json.dumps(s))
    (root / "netload.csv").write_text(netload)
    return root


def test_identical_runs_match_except_wall_clock_and_identity(tmp_path):
    a = make_run(tmp_path / "a", commit="old")
    s = {"header": {"git_commit": "new", "config": {"run_name": "other"}},
         "x": {"runtime_s": 99.0, "peak": 2.0, "nan": float("nan")}}
    b = make_run(tmp_path / "b", commit="new", summary=s)
    assert diff_runs(a, b, expect_commit="new")["match"]
    assert not diff_runs(a, b, expect_commit="other")["match"]          # wrong code loaded: fails


def test_any_float_difference_in_a_message_fails(tmp_path):
    a = make_run(tmp_path / "a")
    m = [{"from": "A", "to": "exchange", "msg_type": "register", "payload": {"v": 1.5}},
         {"from": "B", "to": "exchange", "msg_type": "register", "payload": {"v": 1.5000000000000002}}]
    b = make_run(tmp_path / "b", lines=m)
    r = diff_runs(a, b)
    assert not r["match"] and any("line 2" in d for d in r["diffs"])


def test_summary_difference_netload_and_roster_gaps_fail(tmp_path):
    a = make_run(tmp_path / "a")
    s = {"header": {"git_commit": "abc", "config": {"run_name": "r"}}, "x": {"runtime_s": 1.0, "peak": 2.1,
                                                                             "nan": float("nan")}}
    assert not diff_runs(a, make_run(tmp_path / "b", summary=s))["match"]
    assert not diff_runs(a, make_run(tmp_path / "c", netload="t,kw\n1,3\n"))["match"]
    only_a = [{"from": "A", "to": "exchange", "msg_type": "register", "payload": {"v": 1.5}}]
    r = diff_runs(make_run(tmp_path / "d", lines=only_a), make_run(tmp_path / "e", lines=only_a))
    assert not r["match"] and any("roster" in d for d in r["diffs"])


def test_ignore_line_drops_a_preregistered_message_class(tmp_path):
    a = make_run(tmp_path / "a")
    extra = [{"from": "metering", "to": "exchange", "msg_type": "asset_limits", "payload": {}}]
    base = [{"from": x, "to": "exchange", "msg_type": "register", "payload": {"v": 1.5}} for x in ("A", "B")]
    b = make_run(tmp_path / "b", lines=extra + base)
    assert not diff_runs(a, b)["match"]
    assert diff_runs(a, b, ignore_line=lambda l: l["msg_type"] == "asset_limits")["match"]


def test_first_diff_types_and_nan():
    assert first_diff({"a": [1.0, float("nan")]}, {"a": [1.0, float("nan")]}) is None
    assert first_diff(1, 1.0) is not None
    assert first_diff(-0.0, 0.0) is not None
