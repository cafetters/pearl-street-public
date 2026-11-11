"""bench.parity_check: the trace folder is the default exchange_h100_rinfo unless --trace-dir names another."""
import json

import pytest

from bench import parity_check as P

LINES = [{"event": "send", "msg_type": "telemetry", "t_utc": "2023-01-02T06:00:00Z",
          "payload": {"asset_id": "A/bess", "net_kw": -1.5}},
         {"event": "send", "msg_type": "peak_settle", "t_utc": "2023-01-04T06:00:00Z", "payload": {"net_usd": 12.5}}]


def _run(d, folder, commit="abc1234deadbeef", lines=LINES):
    (d / folder).mkdir(parents=True)
    hdr = {"event": "header", "git_commit": commit}
    (d / folder / "trace.jsonl").write_text("\n".join(json.dumps(x) for x in [hdr, *lines]) + "\n", encoding="utf-8")
    (d / "summary.json").write_text(json.dumps({"x": 1, "runtime_s": 3.0, "header": {"git_commit": commit}}),
                                    encoding="utf-8")
    return d


def test_default_folder_is_unchanged(tmp_path):
    c, r = _run(tmp_path / "c", "exchange_h100_rinfo"), _run(tmp_path / "r", "exchange_h100_rinfo", "0000000")
    assert P.main(["--cloud", str(c), "--reference", str(r), "--commit", "abc1234"]) == 0


def test_trace_dir_option_reads_another_folder_and_still_fails_on_a_difference(tmp_path):
    c, r = _run(tmp_path / "c", "exchange_h150_rinfo"), _run(tmp_path / "r", "exchange_h150_rinfo", "0000000")
    args = ["--cloud", str(c), "--reference", str(r), "--commit", "abc1234"]
    assert P.main(args + ["--trace-dir", "exchange_h150_rinfo"]) == 0
    with pytest.raises(FileNotFoundError):              # the default folder isn't there
        P.main(args)
    bad = [LINES[0], {**LINES[1], "payload": {"net_usd": 13.5}}]
    c2 = _run(tmp_path / "c2", "exchange_h150_rinfo", lines=bad)
    assert P.main(["--cloud", str(c2), "--reference", str(r), "--commit", "abc1234",
                   "--trace-dir", "exchange_h150_rinfo"]) == 1          # payout and trace text differ


def test_summary_commit_must_match_the_cloud_trace_not_the_reference(tmp_path):
    c, r = _run(tmp_path / "c", "exchange_h100_rinfo"), _run(tmp_path / "r", "exchange_h100_rinfo", "0000000")
    args = ["--cloud", str(c), "--reference", str(r), "--commit", "abc1234"]
    assert P.main(args) == 0                      # summary commits differ from the reference's: not a failure
    s = json.loads((c / "summary.json").read_text())
    s["header"]["git_commit"] = "fff0000"          # but the summary must name the commit its trace ran
    (c / "summary.json").write_text(json.dumps(s))
    assert P.main(args) == 1
    s["header"]["git_commit"], s["x"] = "abc1234deadbeef", 2       # any other summary difference still fails
    (c / "summary.json").write_text(json.dumps(s))
    assert P.main(args) == 1
