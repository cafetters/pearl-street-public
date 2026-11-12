"""scripts/publish_public.py: the filters that decide what reaches the public repo (no network, no git)."""
import importlib.util
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "publish_public", Path(__file__).resolve().parents[1] / "scripts" / "publish_public.py")
P = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(P)


def test_publicignore_excludes_the_private_notes():
    pats = P.load_ignore()
    for rel in ("docs/PLAN.md", "docs/overnight/2026-11-05-cloud.md", "docs/plans/m1b.md",
                "docs/plans/m1b_questions.md", "CLAUDE.md", ".claude/state.md", "tests/test_d61_check.py"):
        assert P.ignored(rel, pats), rel
    for rel in ("exchange/core.py", "docs/plans/m1b_report.md", "docs/PLAN.md.bak", "README.md"):
        assert not P.ignored(rel, pats), rel


@pytest.mark.parametrize("raw,want", [
    (r'"repo": "."', '"repo": "."'),
    (r'"holdout": "../pearl-street-holdout"', '"holdout": "../pearl-street-holdout"'),
    (r"data_dir: ../data", "data_dir: ../data"),
    ("no path here", "no path here"),
])
def test_local_paths_become_relative(raw, want):
    assert P.relativize(raw) == want


def test_readme_header_carries_the_source_sha():
    out = P.with_header("# flex-exchange\n\nPython 3.12 project.\n", "a" * 40)
    assert out.startswith("# flex-exchange\n\nSnapshot of a private development repo at " + "a" * 40)
    assert out.endswith("kept in the private repo.\n\nPython 3.12 project.\n")


def test_blocked_terms_are_found_in_paths_and_contents(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "notes.md").write_text("line one\nmet Secretname today\n", encoding="utf-8")
    (tmp_path / "Secretname.txt").write_text("clean", encoding="utf-8")
    hits = P.blocked_hits(tmp_path, ["docs/notes.md", "Secretname.txt"], ["Secretname"])
    assert hits == ["docs/notes.md: 1 hit(s) of a blocked term (line 2)",
                    "Secretname.txt: path contains a blocked term"]
    assert P.blocked_hits(tmp_path, ["docs/notes.md"], ["secretname"]) == []     # case-sensitive


def test_missing_or_empty_terms_file_refuses(tmp_path):
    with pytest.raises(P.Refused):
        P.load_terms(tmp_path / "absent.txt")
    (tmp_path / "t.txt").write_text("# only a comment\n", encoding="utf-8")
    with pytest.raises(P.Refused):
        P.load_terms(tmp_path / "t.txt")


@pytest.mark.parametrize("text,hit", [
    (r"C:\Users\someone\AI-Studio", True),
    (r"D:\users\someone\x", True),
    (r"c:\Users\someone\x", True),
    ("/home/someone/x", True),
    ("/someone/", True),
    ("someone", True),
    ("someone's", True),
    ("Someone", True),               # the term carries (?i)
    ("someoneelse", False),
    ("cosomeone", False),
    ("someone2", False),
])
def test_a_regex_term_catches_a_username_in_paths_but_not_inside_words(tmp_path, text, hit):
    (tmp_path / "f.txt").write_text(text, encoding="utf-8")
    term = "re:(?i)(?<![A-Za-z0-9])someone(?![A-Za-z0-9])"
    assert bool(P.blocked_hits(tmp_path, ["f.txt"], [term])) is hit


def test_a_literal_term_is_not_a_regex(tmp_path):
    (tmp_path / "f.txt").write_text("costs $7 lid and a.b", encoding="utf-8")
    assert P.blocked_hits(tmp_path, ["f.txt"], ["$7 lid"]) == ["f.txt: 1 hit(s) of a blocked term (line 1)"]
    assert P.blocked_hits(tmp_path, ["f.txt"], ["a*b"]) == []


# ---- fail closed (owner, Nov 11): a missing tool or any check error refuses; nothing is published ----

def test_missing_gitleaks_refuses(monkeypatch, tmp_path):
    monkeypatch.delenv("GITLEAKS", raising=False)
    monkeypatch.setattr(P.shutil, "which", lambda name: None)
    monkeypatch.setattr(P, "GITLEAKS_FALLBACK", tmp_path / "absent.exe")
    with pytest.raises(P.Refused):
        P.find_gitleaks(None)


def _fake_gitleaks(monkeypatch, returncode, report_text):
    """Stand in for the gitleaks binary: write report_text (None = no report) and exit with returncode."""
    def fake_run(cmd, **kw):
        if report_text is not None:
            Path(cmd[cmd.index("-r") + 1]).write_text(report_text, encoding="utf-8")
        class R:
            pass
        r = R()
        r.returncode, r.stdout, r.stderr = returncode, b"", b"fatal"
        return r
    monkeypatch.setattr(P.subprocess, "run", fake_run)


@pytest.mark.parametrize("rc,text", [
    (2, "[]"),          # an error exit code, even with an empty report
    (1, "[]"),          # "leaks found" exit with no findings: gitleaks failed, not clean
    (1, ""),            # empty report on a failing exit
    (0, "{not json"),   # unreadable report
    (0, None),          # no report at all
])
def test_a_gitleaks_error_refuses(monkeypatch, tmp_path, rc, text):
    tree = tmp_path / "candidate"
    tree.mkdir()
    _fake_gitleaks(monkeypatch, rc, text)
    with pytest.raises(P.Refused):
        P.gitleaks_hits(tree, "gitleaks")


def test_a_clean_gitleaks_run_passes(monkeypatch, tmp_path):
    tree = tmp_path / "candidate"
    tree.mkdir()
    _fake_gitleaks(monkeypatch, 0, "[]")
    assert P.gitleaks_hits(tree, "gitleaks") == ([], 0)


def test_any_unexpected_error_is_a_refusal_not_a_publish(monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("check crashed")
    monkeypatch.setattr(P, "run", boom)
    assert P.main(["--publish"]) == 2
    assert "refused" in capsys.readouterr().err
