"""bench.d69_regress on synthetic runs (owner, Nov 2): only the two declared exclusions are excused, each only when its
guard holds; anything else still fails."""
import json
import shutil

from bench import d69_check as C
from bench import d69_regress as G
from tests.test_d69_check import _folder, _runs


def _pre_d69_twin(tmp_path, new):
    base = tmp_path / "base"
    shutil.copytree(new, base)
    for p in base.glob("*/trace.jsonl"):
        lines = [json.loads(l) for l in p.read_text().splitlines()]
        lines[0].get("exchange", {}).pop("attested_owners", None)
        keep = [lines[0]] + [l for l in lines[1:] if not C.is_attestation(l)]
        p.write_text("\n".join(json.dumps(l, separators=(",", ":"), sort_keys=True) for l in keep) + "\n")
    s = json.loads((base / "summary.json").read_text())
    h = s["exchange_by_headroom_mw"]["0.03"]
    h["messages_total"] -= 2
    del h["messages_per_day"]["2023-01-02"]["asset_limits"]
    del h["bus_refusals"]
    (base / "summary.json").write_text(json.dumps(s))
    return base


def _set_header(run, key, value):
    p = next(run.glob("exchange_*/trace.jsonl"))
    lines = p.read_text().splitlines()
    h = json.loads(lines[0])
    h["exchange"][key] = value
    lines[0] = json.dumps(h, separators=(",", ":"), sort_keys=True)
    p.write_text("\n".join(lines) + "\n")


def test_exact_twin_is_match_and_nothing_is_excused(tmp_path):
    _, cfg_fn, _ = _runs(tmp_path)
    new, commit = _folder(tmp_path, "new", cfg_fn())
    base = _pre_d69_twin(tmp_path, new)
    res = G.regress(base, new, commit)
    assert res["verdict"] == "MATCH" and res["excused"] == []


def test_cap_type_and_commit_source_are_excused_only_under_their_guards(tmp_path):
    _, cfg_fn, _ = _runs(tmp_path)
    new, commit = _folder(tmp_path, "new", cfg_fn())
    base = _pre_d69_twin(tmp_path, new)
    _set_header(base, "price_cap_usd_mwh", 500)
    _set_header(new, "price_cap_usd_mwh", 500.0)
    s = json.loads((base / "summary.json").read_text())
    s["header"]["git_commit_from"] = "trace header"
    (base / "summary.json").write_text(json.dumps(s))
    res = G.regress(base, new, commit)
    assert res["verdict"] == "MATCH_DECLARED", res["diffs"]
    assert {e["exclusion"] for e in res["excused"]} == {"header price cap type", "commit-source note"}
    _set_header(new, "price_cap_usd_mwh", 100.0)                   # a real cap change is not excused
    assert G.regress(base, new, commit)["verdict"] == "FAIL"
    _set_header(new, "price_cap_usd_mwh", 500.0)
    s["header"]["git_commit_from"] = "git"                         # another commit-source value is not excused
    (base / "summary.json").write_text(json.dumps(s))
    assert G.regress(base, new, commit)["verdict"] == "FAIL"


def test_an_undeclared_extra_header_key_fails(tmp_path):
    """E.g. jan12 and jan16, whose comparators predate D58's margin code: an extra header key is not excused."""
    _, cfg_fn, _ = _runs(tmp_path)
    new, commit = _folder(tmp_path, "new", cfg_fn())
    base = _pre_d69_twin(tmp_path, new)
    _set_header(new, "peak_response_margin_usd_mwh", 0.0)
    assert G.regress(base, new, commit)["verdict"] == "FAIL"
