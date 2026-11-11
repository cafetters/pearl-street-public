"""bench/d61v2_cloud.py (gap 3): from generated configs, their M0 folders and a commit, build bench.cloud_queue's
arguments (PLAN section 6), launch or print them (--print-only), and judge the downloads with bench.d61v2_judge.
No test launches anything: the real-config test runs --print-only on the rehearsal's configs."""
import json
from pathlib import Path

import pytest

from bench import d61v2_cloud as K
from bench import d61v2_make_configs as G

SHA = "d471a72"


def _setup(tmp_path, windows=(("jan12", "2023-01-12", "2023-01-13"),), controllable=True, smoke=None):
    cfgs = tmp_path / "configs"
    cfgs.mkdir()
    for n, t in G.build(windows=list(windows), smoke=smoke).items():
        (cfgs / n).write_text(t, encoding="utf-8")
    root = tmp_path / "m0root"
    for tag, *_ in [*windows, *([smoke] if smoke else [])]:
        d = root / "runs" / f"m0_{tag}"
        d.mkdir(parents=True)
        (d / "ceiling.json").write_text("{}")
        if controllable:
            (d / "ceiling_controllable.json").write_text("{}")
    return cfgs, root


def test_the_plan_orders_twins_first_and_dollar5_before_m0_and_leaves_reruns_out(tmp_path):
    cfgs, root = _setup(tmp_path, windows=(("jan12", "2023-01-12", "2023-01-13"), ("jan06", "2023-01-06", "2023-01-07")))
    items = K.select(cfgs)
    names = [c["run_name"] for _, c in items]
    assert len(names) == 2 * (11 + 4) and not any(n.endswith("_max") for n in names)
    # windows: the v2 windows in the draft's order, any other by start date (jan06 before jan12)
    assert names[0] == "h2h_c_d61v2_jan06_twin_m5" and names[11] == "h2h_c_d61v2_jan12_twin_m5"
    assert names[22] == "h2h_c_d61v2_jan06_twin_m0" and names[26] == "h2h_c_d61v2_jan12_twin_m0"
    seen_twins = set()
    for n in names:
        if "_twin_" in n:
            seen_twins.add(n)
        else:
            assert K.twin_for(n) in seen_twins, n
    pairs = K.plan(items, root)
    assert all(Path(m0) == root / "runs" / f"m0_{c['run_name'].split('_')[3]}" for (_, c), (_, m0) in zip(items, pairs))


def test_include_max_adds_the_reruns_last_and_names_select(tmp_path):
    cfgs, _ = _setup(tmp_path)
    items = K.select(cfgs, include_max=True)
    names = [c["run_name"] for _, c in items]
    assert len(names) == 30 and all(n.endswith("_max") for n in names[15:])
    sel = K.select(cfgs, names=["h2h_c_d61v2_jan12_silent_m5", "h2h_c_d61v2_jan12_twin_m5"])
    assert [c["run_name"] for _, c in sel] == ["h2h_c_d61v2_jan12_twin_m5", "h2h_c_d61v2_jan12_silent_m5"]
    with pytest.raises(K.Refused, match="not found"):
        K.select(cfgs, names=["h2h_c_d61v2_jan12_nope_m5"])


def test_the_queue_args(tmp_path):
    cfgs, root = _setup(tmp_path)
    items = K.select(cfgs, names=["h2h_c_d61v2_jan12_twin_m5", "h2h_c_d61v2_jan12_silent_m5"])
    args = K.queue_args(K.plan(items, root), commit=SHA, concurrency=2, budget=3, runtime_s=1000, max_awake=10,
                        queue_id="q1")
    assert args.count("--run") == 2
    i = args.index("--commit")
    assert args[i + 1] == SHA and args[args.index("--concurrency") + 1] == "2"
    assert args[args.index("--budget") + 1] == "3" and args[args.index("--queue-id") + 1] == "q1"
    assert args[args.index("--measured-runtime-s") + 1] == "1000" and args[args.index("--max-awake") + 1] == "10"


@pytest.mark.parametrize("kw, why", [({"concurrency": 11}, "concurrency"), ({"budget": 1}, "budget"),
                                     ({"concurrency": 6, "max_awake": 4}, "max-awake")])
def test_the_cloud_rules_are_refused_before_launch(tmp_path, kw, why):
    cfgs, root = _setup(tmp_path)
    items = K.select(cfgs, names=["h2h_c_d61v2_jan12_twin_m5", "h2h_c_d61v2_jan12_silent_m5"])
    base = dict(commit=SHA, concurrency=2, budget=3, runtime_s=1000, max_awake=10, queue_id="q1")
    with pytest.raises(K.Refused, match=why):
        K.queue_args(K.plan(items, root), **{**base, **kw})


def test_a_config_resolving_d62_guarantee_true_is_refused(tmp_path):
    cfgs, root = _setup(tmp_path)
    p = cfgs / "h2h_c_d61v2_jan12_silent_m5.yaml"
    p.write_text(p.read_text().replace("d62_guarantee: false", "d62_guarantee: true"))
    with pytest.raises(K.Refused, match="d62_guarantee"):
        K.plan(K.select(cfgs), root)


def test_a_missing_controllable_ceiling_is_refused(tmp_path):
    cfgs, root = _setup(tmp_path, controllable=False)
    with pytest.raises(K.Refused, match="ceiling_controllable"):
        K.plan(K.select(cfgs), root)


def test_judge_pairs_are_the_attack_cells_with_a_twin_in_the_set(tmp_path):
    cfgs, _ = _setup(tmp_path, smoke=G.SMOKE)
    items = K.select(cfgs, names=["h2h_c_d61v2_jan12_twin_m5", "h2h_c_d61v2_jan12_silent_m5",
                                  "h2h_c_d61v2_jan12_renege_day_m0",
                                  "h2h_c_d61v2smoke_sep05_all_m5", "h2h_c_d61v2smoke_sep05_twin_m5"])
    pairs = K.judge_pairs(items)
    assert ("h2h_c_d61v2_jan12_silent_m5", "h2h_c_d61v2_jan12_twin_m5") in pairs
    assert ("h2h_c_d61v2smoke_sep05_all_m5", "h2h_c_d61v2smoke_sep05_twin_m5") in pairs
    assert not any(c == "h2h_c_d61v2_jan12_renege_day_m0" for c, _ in pairs)        # its twin isn't in the set


def test_print_only_launches_nothing(tmp_path, capsys, monkeypatch):
    cfgs, root = _setup(tmp_path)
    monkeypatch.setattr(K, "launch", lambda *a, **k: pytest.fail("launched"))
    monkeypatch.setattr(K, "judge", lambda *a, **k: pytest.fail("judged"))
    rc = K.main(["--configs", str(cfgs), "--m0-root", str(root), "--commit", SHA, "--queue-id", "q1",
                 "--concurrency", "2", "--budget", "16", "--measured-runtime-s", "1000", "--print-only"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "bench.cloud_queue" in out and out.count("--run ") == 15


# ---- real configs: --print-only on the rehearsal's (runs/nov07/v2r/configs in the main checkout) ------------------

MAIN = Path(__file__).resolve().parents[2] / "pearl-street"
REH = MAIN / "runs" / "nov07" / "v2r" / "configs"


@pytest.mark.skipif(not REH.exists() or not (MAIN / "runs" / "m0_jan12").exists(), reason="needs the rehearsal")
def test_print_only_on_the_rehearsal_configs_gives_its_queue(capsys):
    rc = K.main(["--configs", str(REH), "--m0-root", str(MAIN), "--commit", SHA, "--queue-id", "v2r-print",
                 "--concurrency", "10", "--budget", "64", "--measured-runtime-s", "1000", "--print-only",
                 "--glob", "h2h_c_v2r_*"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("--run ") == 60
    main_args = (MAIN / "runs" / "nov07" / "v2r" / "main_args.txt").read_text().split()
    staged = {Path(main_args[i + 1]).name for i, a in enumerate(main_args) if a == "--run"}
    printed = {Path(t).name for t in out.split() if t.endswith(".yaml")}
    assert printed == staged
