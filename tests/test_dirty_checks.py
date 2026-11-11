"""The pinned dirty checks (owner, Nov 11, decision 2; Nov 8 report, decision 5): `d61v2_judge.tool_header` and
`d90_check.code_commit` mark a commit `-dirty` when bench/, sim/, agents/, exchange/ or attacks/ has a modified
tracked file OR an untracked file (git-ignored files such as __pycache__ don't count), and when `git status` itself
fails (fail closed). Each test builds its own throwaway repo under tmp_path; nothing is written into this checkout."""
import subprocess
from pathlib import Path

import pytest

from bench import d61v2_judge as J
from bench import d90_check as D

DIRS = ("bench", "sim", "agents", "exchange", "attacks")


def _git(root, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(root), *a],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / ".gitignore").write_text("__pycache__/\n*.py[cod]\n")
    for d in (*DIRS, "docs", "tests"):
        (r / d).mkdir()
        (r / d / "a.py").write_text("x = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    return r


def _judge_commit(monkeypatch, root):
    monkeypatch.setattr(J, "ROOT", root)
    return J.tool_header()["git_commit"]


def test_both_checks_cover_the_same_five_folders():
    assert tuple(J.DIRTY_PATHS) == DIRS and tuple(D.DIRTY_PATHS) == DIRS


def test_a_clean_repo_is_clean(repo, monkeypatch):
    sha = _git(repo, "rev-parse", "HEAD")
    assert D.code_commit(repo) == sha
    assert _judge_commit(monkeypatch, repo) == sha


@pytest.mark.parametrize("d", DIRS)
def test_an_untracked_file_in_a_checked_folder_is_dirty(repo, monkeypatch, d):
    (repo / d / "new_module.py").write_text("y = 2\n")
    assert D.code_commit(repo).endswith("-dirty")
    assert _judge_commit(monkeypatch, repo).endswith("-dirty")


@pytest.mark.parametrize("d", DIRS)
def test_a_modified_tracked_file_in_a_checked_folder_is_dirty(repo, monkeypatch, d):
    (repo / d / "a.py").write_text("x = 3\n")
    assert D.code_commit(repo).endswith("-dirty")
    assert _judge_commit(monkeypatch, repo).endswith("-dirty")


def test_an_untracked_file_in_a_nested_folder_is_dirty(repo, monkeypatch):
    (repo / "sim" / "sub").mkdir()
    (repo / "sim" / "sub" / "deep.py").write_text("z = 1\n")
    assert D.code_commit(repo).endswith("-dirty")
    assert _judge_commit(monkeypatch, repo).endswith("-dirty")


def test_ignored_files_and_other_folders_stay_clean(repo, monkeypatch):
    sha = _git(repo, "rev-parse", "HEAD")
    for d in DIRS:
        (repo / d / "__pycache__").mkdir()
        (repo / d / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\0")
    (repo / "docs" / "notes.md").write_text("untracked doc\n")
    (repo / "tests" / "a.py").write_text("x = 9\n")
    assert D.code_commit(repo) == sha
    assert _judge_commit(monkeypatch, repo) == sha


def test_a_failing_git_status_is_dirty(repo, monkeypatch):
    real = subprocess.run

    def fake(cmd, *a, **k):
        if "status" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: simulated")
        return real(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", fake)
    assert D.code_commit(repo).endswith("-dirty")
    assert _judge_commit(monkeypatch, repo).endswith("-dirty")


def test_the_commit_guard_refuses_an_untracked_file(repo):
    sha = _git(repo, "rev-parse", "HEAD")
    header = {"git_commit": sha}
    D.guard_commit(header, D.code_commit(repo))
    (repo / "agents" / "stray.py").write_text("q = 1\n")
    with pytest.raises(D.Refused, match="commit"):
        D.guard_commit(header, D.code_commit(repo))
