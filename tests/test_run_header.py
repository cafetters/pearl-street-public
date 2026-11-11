"""Summary provenance (sim/run_m1.py summary_header): the commit falls back to the run's trace header when the git
call fails at summary time."""
import bench.runinfo
from sim.run_m1 import summary_header

CFG = {"seed": 1, "run_name": "x"}
TRACE = {"git_commit": "abc1234"}


def test_summary_header_keeps_its_own_commit_when_git_works(monkeypatch):
    monkeypatch.setattr(bench.runinfo, "git_commit", lambda: "def5678")
    h = summary_header(CFG, ["A", "B"], TRACE)
    assert h["git_commit"] == "def5678" and "git_commit_from" not in h and h["roster"] == ["A", "B"]


def test_summary_header_falls_back_to_the_trace_commit_when_git_fails(monkeypatch):
    for failed in ("", "-dirty"):           # rev-parse failed; status alone may still report dirty
        monkeypatch.setattr(bench.runinfo, "git_commit", lambda: failed)
        h = summary_header(CFG, ["A"], TRACE)
        assert h["git_commit"] == "abc1234" and h["git_commit_from"] == "trace header"
