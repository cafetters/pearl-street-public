"""bench.d69_breadth on synthetic runs: leaves only the new run has pass only if a run made at the branch base also has
them; a changed or missing old leaf, or a changed message line, fails."""
import json
import shutil

from bench import d69_breadth as B
from bench import d69_check as C
from tests.test_d69_check import _folder, _runs


def _twin(new, base):
    shutil.copytree(new, base)
    for p in base.glob("*/trace.jsonl"):           # pre-D69 and older: no attestations, fewer exchange header keys
        lines = [json.loads(l) for l in p.read_text().splitlines()]
        ex = lines[0].get("exchange", {})
        ex.pop("attested_owners", None)
        ex.pop("guarantee", None)
        keep = [lines[0]] + [l for l in lines[1:] if not C.is_attestation(l)]
        p.write_text("\n".join(json.dumps(l, separators=(",", ":"), sort_keys=True) for l in keep) + "\n")
    s = json.loads((base / "summary.json").read_text())
    h = s["exchange_by_headroom_mw"]["0.03"]
    h["messages_total"] -= 2
    del h["messages_per_day"]["2023-01-02"]["asset_limits"]
    del h["bus_refusals"]
    (base / "summary.json").write_text(json.dumps(s))


def _set(run, key, value):
    s = json.loads((run / "summary.json").read_text())
    s["exchange_by_headroom_mw"]["0.03"][key] = value
    (run / "summary.json").write_text(json.dumps(s))


def test_breadth_classifier(tmp_path, monkeypatch):
    _, cfg_fn, _ = _runs(tmp_path)
    new, commit = _folder(tmp_path, "new", cfg_fn())
    _twin(new, tmp_path / "old")
    old = tmp_path / "old"
    ref = tmp_path / "ref"
    shutil.copytree(new, ref)
    monkeypatch.setattr(B, "BASE_COMMIT", commit)
    _set(new, "added_later", 1.5)
    _set(ref, "added_later", 9.0)                   # only the path of a base-commit run is used, not its value
    r = B.classify(old, new, commit, [ref])
    assert r["verdict"] == "MATCH_EXCEPT_PRE_D69_ADDITIONS", r
    assert "/exchange_by_headroom_mw/0.03/added_later" in r["added_by_pre_d69_code"]["summary"]
    _set(new, "unexplained", 1)                     # not in any base-commit run
    assert B.classify(old, new, commit, [ref])["verdict"] == "FAIL"
    s = json.loads((new / "summary.json").read_text())
    del s["exchange_by_headroom_mw"]["0.03"]["unexplained"]
    s["exchange_by_headroom_mw"]["0.03"]["messages_per_day"]["2023-01-02"]["tick"] = 6     # an old leaf changed
    (new / "summary.json").write_text(json.dumps(s))
    assert B.classify(old, new, commit, [ref])["verdict"] == "FAIL"
    monkeypatch.setattr(B, "BASE_COMMIT", "0" * 40)  # a ref not made at the base commit does not count
    s["exchange_by_headroom_mw"]["0.03"]["messages_per_day"]["2023-01-02"]["tick"] = 5
    (new / "summary.json").write_text(json.dumps(s))
    assert B.classify(old, new, commit, [ref])["verdict"] == "FAIL"


def test_identical_runs_are_an_exact_match(tmp_path):
    _, cfg_fn, _ = _runs(tmp_path)
    new, commit = _folder(tmp_path, "new", cfg_fn())
    old = tmp_path / "old"
    shutil.copytree(new, old)
    for p in old.glob("*/trace.jsonl"):
        lines = [json.loads(l) for l in p.read_text().splitlines()]
        lines[0].get("exchange", {}).pop("attested_owners", None)
        keep = [lines[0]] + [l for l in lines[1:] if not C.is_attestation(l)]
        p.write_text("\n".join(json.dumps(l, separators=(",", ":"), sort_keys=True) for l in keep) + "\n")
    s = json.loads((old / "summary.json").read_text())
    s["exchange_by_headroom_mw"]["0.03"]["messages_total"] -= 2
    del s["exchange_by_headroom_mw"]["0.03"]["messages_per_day"]["2023-01-02"]["asset_limits"]
    del s["exchange_by_headroom_mw"]["0.03"]["bus_refusals"]
    (old / "summary.json").write_text(json.dumps(s))
    assert B.classify(old, new, commit, [])["verdict"] == "MATCH"
