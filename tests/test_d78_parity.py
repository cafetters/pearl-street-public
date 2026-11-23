"""The D78 parity judge (bench/d78_parity.py) sets aside only the declared differences (configs/parity_d78/README.md)."""
import json

from bench.d78_parity import judge


def _write(path, msgs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([json.dumps({"header": 1})] + [json.dumps(m) for m in msgs]) + "\n", encoding="utf-8")


def _base():
    return [
        {"msg_type": "call_nominations", "payload": {"call_id": "C1", "binding": False}, "msg_id": "ex:1"},
        {"msg_type": "call_nominations", "payload": {"call_id": "C2", "binding": True}, "msg_id": "ex:2"},
        {"msg_type": "peak_price", "payload": {"call_id": "C1"}, "msg_id": "ex:3"},
        {"msg_type": "selfish_reference", "payload": {"call_id": "C2", "asset_id": "A/b"}, "msg_id": "metering:1"},
        {"msg_type": "peak_price", "payload": {"call_id": "C2"}, "msg_id": "ex:4"},
        {"msg_type": "telemetry", "payload": {"kw": 1.0}, "msg_id": "metering:2", "run_id": "ref"},
    ]


def _flag_on(base, kw=1.0):
    d79 = {"msg_type": "selfish_reference", "payload": {"call_id": "C1", "asset_id": "A/b"}, "msg_id": "metering:1"}
    out = base[:2] + [d79] + base[2:]
    out[4] = {**out[4], "msg_id": "metering:2"}
    out[6] = {**out[6], "msg_id": "metering:3", "run_id": "new", "payload": {"kw": kw}}
    return out


def test_only_the_declared_differences_are_set_aside(tmp_path):
    _write(tmp_path / "ref/x/trace.jsonl", _base())
    _write(tmp_path / "new/x/trace.jsonl", _flag_on(_base()))
    r = judge(tmp_path / "new", tmp_path / "ref")
    assert r["pass"] and list(r["d79_lines"].values()) == [{"removed": 1, "expected": 1}], r


def test_any_other_difference_fails(tmp_path):
    _write(tmp_path / "ref/x/trace.jsonl", _base())
    _write(tmp_path / "new/x/trace.jsonl", _flag_on(_base(), kw=1.001))
    assert not judge(tmp_path / "new", tmp_path / "ref")["pass"]


def test_missing_d79_lines_fail(tmp_path):
    _write(tmp_path / "ref/x/trace.jsonl", _base())
    _write(tmp_path / "new/x/trace.jsonl", _base())
    assert not judge(tmp_path / "new", tmp_path / "ref")["pass"]


# Both proposed defaults together (D109 with D107; configs/parity_d78q/README.md): the idle_reference lines Q-N10's
# check adds must equal those of the Q-N10 parity run, and no over_power_nomination reject may appear.
def _idle(kw=-1.0):
    return {"msg_type": "idle_reference", "payload": {"call_id": "C2", "asset_id": "A/b", "net_kw": [kw]},
            "msg_id": "metering:idle1"}


def test_both_flags_idle_lines_must_match_the_qn10_run(tmp_path):
    _write(tmp_path / "ref/x/trace.jsonl", _base())
    _write(tmp_path / "qn10/x/trace.jsonl", [_idle()] + _base())
    _write(tmp_path / "new/x/trace.jsonl", [{**_idle(), "run_id": "new"}] + _flag_on(_base()))
    r = judge(tmp_path / "new", tmp_path / "ref", idle_root=tmp_path / "qn10")
    assert r["pass"] and list(r["idle_lines"].values()) == ["identical (1)"], r
    _write(tmp_path / "new/x/trace.jsonl", [_idle(kw=-1.5)] + _flag_on(_base()))
    assert not judge(tmp_path / "new", tmp_path / "ref", idle_root=tmp_path / "qn10")["pass"]


def test_both_flags_an_over_power_reject_fails(tmp_path):
    _write(tmp_path / "ref/x/trace.jsonl", _base())
    _write(tmp_path / "qn10/x/trace.jsonl", [_idle()] + _base())
    rej = {"msg_type": "reject", "payload": {"code": "over_power_nomination"}, "msg_id": "ex:9"}
    _write(tmp_path / "new/x/trace.jsonl", [_idle()] + _flag_on(_base()) + [rej])
    r = judge(tmp_path / "new", tmp_path / "ref", idle_root=tmp_path / "qn10")
    assert not r["pass"] and r["over_power_rejects"] == 1, r
