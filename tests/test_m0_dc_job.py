"""M2b: bench/m0_dc.py, the launcher job for the ceiling with deferral (python -m bench.m0_dc <config>): it writes
runs/<run_name>/summary.json (the ceiling_dc output) and trace.jsonl (one header line: config, seed, git commit)."""
import json
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[1].parent / "data"


@pytest.mark.skipif(not (DATA / "dc_sessions_2023.parquet").exists(), reason="needs ../data with the session table")
def test_job_writes_summary_and_trace(tmp_path, monkeypatch):
    import yaml

    from bench import m0_dc
    cfg = {"data_dir": str(DATA), "start": "2023-01-12", "end": "2023-01-13", "slice_tz": "America/Chicago",
           "run_name": "m0dc_test", "m0_dir": str(tmp_path / "m0"), "agents": "all"}
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(m0_dc, "RUNS", tmp_path / "runs")
    assert m0_dc.main([str(p)]) == 0
    out = tmp_path / "runs" / "m0dc_test"
    s = json.loads((out / "summary.json").read_text())
    assert s["variants"]["cold"]["with_deferral_le_battery_only"]
    h = json.loads((out / "trace.jsonl").read_text().splitlines()[0])["header"]
    assert "git_commit" in h and h["config"]["run_name"] == "m0dc_test"
