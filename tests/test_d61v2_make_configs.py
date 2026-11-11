"""bench/d61v2_make_configs.py (gap 4): the D61 v2 config generator, from the rehearsal copy runs/nov07/v2r/make_configs.py.

Every attack cell maps through twin_of() to a twin that differs only in run_name and attackers; every config sets the
v2 settings and D61's flags. The same tests run on the committed configs/d61v2/ when it exists (item 6)."""
from pathlib import Path

import pytest
import yaml

from bench import d61v2_make_configs as G
from bench.d61v2_run_queue import twin_of

ROOT = Path(__file__).resolve().parents[1]
COMMITTED = ROOT / "configs" / "d61v2"
REQUIRED = {"nomination_span": "operating_day", "peak_steps": "controllable", "d62_guarantee": False,
            "delivery_record": True, "silent_reference": True, "declared_reference": True,
            "forecast_noise_sigma": 0.0, "settlement_reference": "selfish_1p", "price_mode": True,
            "peak_price_cap_usd_mwh": 100, "headroom_mw": [100]}
V2 = {"dec11": ("2023-12-11", "2023-12-12"), "jan09": ("2023-01-09", "2023-01-10"),
      "feb26": ("2023-02-26", "2023-02-27"), "jun02": ("2023-06-02", "2023-06-03")}


def _sets():
    out = [("generated", {n: yaml.safe_load(t) for n, t in G.build().items()})]
    if COMMITTED.exists():
        out.append(("committed", {p.name: yaml.safe_load(p.read_text(encoding="utf-8"))
                                  for p in sorted(COMMITTED.glob("*.yaml"))}))
    return out


SETS = _sets()


def _runs(cfgs):
    return {n: c for n, c in cfgs.items() if not n.startswith("m0_")}


@pytest.mark.parametrize("label, cfgs", SETS, ids=[s[0] for s in SETS])
def test_the_set_is_the_drafts_runs(label, cfgs):
    runs = _runs(cfgs)
    cells = [n for n in runs if "_twin_" not in n and "smoke" not in n]
    twins = [n for n in runs if "_twin_" in n and "smoke" not in n]
    # per window: 10 attacks at $5 + 3 reneging at m = 0, each with its _max rerun; twins at $5 and m = 0, with _max
    assert len(cells) == 4 * 13 * 2 and len(twins) == 4 * 2 * 2
    assert sorted(n for n in cfgs if n.startswith("m0_")) == sorted(f"m0_{w}.yaml" for w in [*V2, "sep05"])
    assert {n for n in runs if "smoke" in n} == {"h2h_c_d61v2smoke_sep05_twin_m5.yaml",
                                                  "h2h_c_d61v2smoke_sep05_all_m5.yaml"}
    for n, c in runs.items():
        assert c["run_name"] == n[:-5]
        w = n.split("_")[3]
        if w in V2:
            assert (str(c["start"]), str(c["end"])) == V2[w] and c["m0_dir"] == f"runs/m0_{w}"


@pytest.mark.parametrize("label, cfgs", SETS, ids=[s[0] for s in SETS])
def test_every_cell_maps_through_twin_of_to_a_twin_differing_only_in_run_name_and_attackers(label, cfgs):
    runs = _runs(cfgs)
    for n, c in runs.items():
        if "_twin_" in n:
            assert "attackers" not in c, n
            continue
        tw = (n.replace("_all_", "_twin_") if "smoke" in n else twin_of(n[:-5]) + ".yaml")
        assert tw in runs, (n, tw)
        a = {k: v for k, v in c.items() if k not in ("run_name", "attackers")}
        t = {k: v for k, v in runs[tw].items() if k not in ("run_name", "attackers")}
        assert a == t, (n, tw, sorted(k for k in set(a) | set(t) if a.get(k) != t.get(k)))
        assert c["attackers"], n


@pytest.mark.parametrize("label, cfgs", SETS, ids=[s[0] for s in SETS])
def test_every_config_sets_the_v2_settings_and_the_d61_flags(label, cfgs):
    for n, c in _runs(cfgs).items():
        for k, v in REQUIRED.items():
            assert c.get(k) == v, (n, k, c.get(k))
        assert "warmup_days" not in c, n
        m = c["peak_response_margin_usd_mwh"]
        assert m in (5.0, 0.0), n
        assert c.get("settlement_rule") == ("max_first_call" if n.endswith("_max.yaml") else None), n


@pytest.mark.parametrize("label, cfgs", SETS, ids=[s[0] for s in SETS])
def test_the_attacker_is_dc08_with_one_attack_per_cell(label, cfgs):
    for n, c in _runs(cfgs).items():
        if "_twin_" in n or "smoke" in n:
            continue
        (a,) = c["attackers"]
        assert a["agent"] == "DC_08", n
    smoke = _runs(cfgs)["h2h_c_d61v2smoke_sep05_all_m5.yaml"]["attackers"]
    assert [a["agent"] for a in smoke] == G.SMOKE_ATTACKERS and len({a["agent"] for a in smoke}) == 10


def test_only_template_keys_and_the_named_settings_are_added():
    tmpl = {k: v for k, v in yaml.safe_load(G.SRC.read_text(encoding="utf-8")).items()}
    allowed = {"run_name", "start", "end", "m0_dir", "peak_response_margin_usd_mwh", "attackers", "settlement_rule",
               *G.FLAGS}
    for n, c in _runs(dict(SETS[0][1])).items():
        changed = {k for k in c if k not in tmpl or c[k] != tmpl[k]}
        assert changed <= allowed, (n, changed - allowed)


def test_a_window_can_be_substituted_for_the_smoke(tmp_path):
    files = G.build(windows=[("sep05", "2023-09-05", "2023-09-06")], smoke=None)
    assert "h2h_c_d61v2_sep05_twin_m5.yaml" in files and not any("dec11" in n for n in files)
    assert G.main(["--out", str(tmp_path), "--smoke-only"]) == 0
    assert sorted(p.name for p in tmp_path.glob("*.yaml")) == ["h2h_c_d61v2smoke_sep05_all_m5.yaml",
                                                               "h2h_c_d61v2smoke_sep05_twin_m5.yaml",
                                                               "m0_sep05.yaml"]


def test_the_generator_reads_only_the_template(monkeypatch):
    import builtins
    import pathlib
    seen = []
    real_read, real_open = pathlib.Path.read_text, builtins.open

    def read(self, *a, **kw):
        seen.append(Path(self).resolve())
        return real_read(self, *a, **kw)

    def opener(path, *a, **kw):
        seen.append(Path(str(path)).resolve())
        return real_open(path, *a, **kw)
    monkeypatch.setattr(pathlib.Path, "read_text", read)
    monkeypatch.setattr(builtins, "open", opener)
    G.build()
    assert seen and set(seen) == {G.SRC.resolve()}


# ---- real configs: with the rehearsal's windows and prefix the generator gives exactly the configs that ran ---------

REH = ROOT.parent / "pearl-street" / "runs" / "nov07" / "v2r" / "configs"
D61_WINDOWS = [("jan12", "2023-01-12", "2023-01-13"), ("jan06", "2023-01-06", "2023-01-07"),
               ("feb02", "2023-02-02", "2023-02-03"), ("jun29", "2023-06-29", "2023-06-30")]


@pytest.mark.skipif(not REH.exists(), reason="needs the rehearsal's configs (runs/nov07/v2r/configs)")
def test_the_rehearsal_configs_are_reproduced_except_the_comment_line():
    files = {n: t for n, t in G.build(windows=D61_WINDOWS, prefix="h2h_c_v2r").items() if not n.startswith("m0_")}
    ran = {p.name: p.read_text(encoding="utf-8") for p in REH.glob("*.yaml")}
    assert set(files) == set(ran) and len(ran) == 122
    for n, t in files.items():
        assert t.splitlines()[1:] == ran[n].splitlines()[1:], n
