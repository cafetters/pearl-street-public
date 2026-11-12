"""bench.cloud_pytest: full pytest as a launcher run (owner decision 4, Nov 12). The run folder gets the counts, the
pytest log, a junit file and a trace whose header has the config and the git commit; the exit code is pytest's."""
import json
import textwrap

from bench import cloud_pytest as CP


def _suite(tmp_path):
    d = tmp_path / "suite"
    d.mkdir()
    (d / "test_x.py").write_text(textwrap.dedent("""
        import pytest
        def test_ok():
            assert True
        def test_bad():
            assert 1 == 2
        @pytest.mark.skip(reason="sdk not installed")
        def test_skip():
            pass
    """))
    return d


def test_counts_log_trace_and_exit_code(tmp_path, monkeypatch):
    suite = _suite(tmp_path)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"run_name: pt_x\npytest_args: [{suite.as_posix()}, -p, no:cacheprovider]\n")
    monkeypatch.chdir(tmp_path)
    rc = CP.main([str(cfg)])
    out = tmp_path / "runs" / "pt_x"
    s = json.loads((out / "summary.json").read_text())
    assert rc == 1 and s["exit"] == 1
    assert (s["passed"], s["failed"], s["skipped"], s["errors"]) == (1, 1, 1, 0)
    assert "test_bad" in " ".join(s["failed_tests"]) and "sdk not installed" in " ".join(s["skip_reasons"])
    head = json.loads((out / "trace.jsonl").read_text().splitlines()[0])["header"]
    assert head["config"]["run_name"] == "pt_x" and "git_commit" in head
    assert (out / "pytest.log").read_text() and (out / "junit.xml").exists()


def test_all_green_exits_zero(tmp_path, monkeypatch):
    d = tmp_path / "s2"
    d.mkdir()
    (d / "test_y.py").write_text("def test_ok():\n    assert True\n")
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"run_name: pt_y\npytest_args: [{d.as_posix()}, -p, no:cacheprovider]\n")
    monkeypatch.chdir(tmp_path)
    assert CP.main([str(cfg)]) == 0
    assert json.loads((tmp_path / "runs" / "pt_y" / "summary.json").read_text())["passed"] == 1


def test_failures_are_printed_to_stdout_because_a_failed_run_keeps_only_its_log(tmp_path, monkeypatch, capsys):
    suite = _suite(tmp_path)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(f"run_name: pt_z\npytest_args: [{suite.as_posix()}, -q, -rs, -p, no:cacheprovider]\n")
    monkeypatch.chdir(tmp_path)
    assert CP.main([str(cfg)]) == 1
    out = capsys.readouterr().out
    assert "FAILED_TEST" in out and "test_bad" in out          # names, not only counts
    assert "SKIP" in out and "sdk not installed" in out
    assert "short test summary info" in out                    # the tail of pytest's own log
