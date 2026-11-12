"""One-way mirror of this private repo to github.com/cafetters/pearl-street-public.

    python scripts/publish_public.py --check              # drift report only; changes nothing
    python scripts/publish_public.py --dry-run            # every step except commit and push
    python scripts/publish_public.py --publish            # commit "Snapshot of private <sha>", push, recheck

The source is always a pushed ref (default origin/main): a ref whose commit is
not on origin is refused. The snapshot is `git archive` of that commit with:
- the paths in .publicignore left out;
- local absolute paths (C:\\Users\\<user>\\AI-Studio\\Engie\\<repo>) rewritten as
  relative ones ("." for this repo, "../<name>" for a sibling);
- the README header regenerated with the source commit's SHA.

Publishing is refused on any hit from the blocked-terms file
(%LOCALAPPDATA%\\pearl-street\\public_blocked_terms.txt, kept outside any repo)
and on any gitleaks finding other than the known test placeholders. After a
push, --check must report zero drift or the script fails.

Nothing here writes to this repo or to a local checkout of the public repo:
the public side is a fresh clone in a temporary directory.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PUBLIC_URL = "https://github.com/cafetters/pearl-street-public.git"
PUBLIC_BRANCH = "main"
AUTHOR_NAME = "cafetters"
AUTHOR_EMAIL = "91074782+cafetters@users.noreply.github.com"
LOCAL = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local"))
TERMS_FILE = LOCAL / "pearl-street" / "public_blocked_terms.txt"
GITLEAKS_FALLBACK = LOCAL / "pearl-street" / "bin" / "gitleaks.exe"

HEADER = ("Snapshot of a private development repo at {sha}. Published for MIT AI Studio Fall 2026 HW8 "
          "review. License TBD.\n\n"
          "docs/PLAN.md and some internal notes are referenced in comments but kept in the private repo.\n")

# gitleaks findings allowed through: fake secrets in a redaction test, identified by file and by the
# sha256 of the matched value (so the values themselves are not repeated here).
KNOWN_PLACEHOLDERS = {
    ("tests/test_cloud_queue.py", "ef875d00ff3890efcb158fec94eadac5e7a88ea77fdf4edab96de7ccd11e43e0"),
    ("tests/test_cloud_queue.py", "87f1a06a4c2c96312ac440004aeeb4c0099d1bfc2a98f58f901eaf75af39df92"),
}

# C:\Users\<user>\AI-Studio\Engie\<repo>, with single or JSON-escaped (doubled) backslashes.
LOCAL_PATH = re.compile(r"[A-Za-z]:(\\\\|\\)Users\1[^\\/\"'\s]+\1AI-Studio\1Engie\1([A-Za-z0-9._-]+)")


class Refused(Exception):
    pass


def git(*args: str, cwd: Path = REPO, binary: bool = False):
    out = subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd,
                         capture_output=True, check=False)
    if out.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {out.stderr.decode(errors='replace').strip()}")
    return out.stdout if binary else out.stdout.decode().strip()


# ---- source --------------------------------------------------------------

def resolve_pushed(ref: str, fetch: bool = True) -> str:
    """The commit for ref, refused unless origin has it."""
    if fetch:
        git("fetch", "--quiet", "origin")
    sha = git("rev-parse", "--verify", f"{ref}^{{commit}}")
    remotes = [r.strip() for r in git("branch", "-r", "--contains", sha).splitlines()]
    if not any(r.startswith("origin/") for r in remotes):
        raise Refused(f"{ref} ({sha[:7]}) is not on origin; push it first")
    return sha


def load_ignore(path: Path = REPO / ".publicignore") -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def ignored(rel: str, patterns: list[str]) -> bool:
    for p in patterns:
        if p.endswith("/"):
            if rel.startswith(p):
                return True
        elif rel == p or fnmatch.fnmatchcase(rel, p):
            return True
    return False


def relativize(text: str) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(2)
        return "." if name == "pearl-street" else f"../{name}"
    return LOCAL_PATH.sub(repl, text)


def with_header(readme: str, sha: str) -> str:
    """README with the snapshot header right under its first (H1) line."""
    first, _, rest = readme.partition("\n")
    return f"{first}\n\n{HEADER.format(sha=sha)}\n{rest.lstrip(chr(10))}"


def build_tree(sha: str, out: Path, patterns: list[str]) -> list[str]:
    """Write the filtered snapshot of sha into out; return its relative file paths."""
    data = git("archive", "--format=tar", sha, binary=True)
    tar_path = out.parent / "src.tar"
    tar_path.write_bytes(data)
    files = []
    with tarfile.open(tar_path) as tar:
        for member in tar.getmembers():
            if not member.isfile() or ignored(member.name, patterns):
                continue
            raw = tar.extractfile(member).read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = None
            if text is not None:
                new = relativize(text)
                if member.name == "README.md":
                    new = with_header(new, sha)
                raw = new.encode("utf-8")
            dest = out / member.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            files.append(member.name)
    tar_path.unlink()
    return sorted(files)


# ---- scans ---------------------------------------------------------------

def load_terms(path: Path = TERMS_FILE) -> list[str]:
    if not path.exists():
        raise Refused(f"blocked-terms file missing: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    terms = [ln.rstrip("\n") for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    if not terms:
        raise Refused(f"blocked-terms file is empty: {path}")
    return terms


def _matcher(term: str) -> re.Pattern:
    """A blocked term: a case-sensitive literal, or a regular expression when
    the line starts with `re:` (e.g. a username anchored so it can't match
    inside a longer word)."""
    return re.compile(term[3:] if term.startswith("re:") else re.escape(term))


def blocked_hits(tree: Path, files: list[str], terms: list[str]) -> list[str]:
    hits = []
    patterns = [_matcher(t) for t in terms]
    for rel in files:
        body = (tree / rel).read_bytes().decode("utf-8", errors="replace")
        for pat in patterns:
            if pat.search(rel):
                hits.append(f"{rel}: path contains a blocked term")
            found = list(pat.finditer(body))
            if found:
                line = body[:found[0].start()].count("\n") + 1
                hits.append(f"{rel}: {len(found)} hit(s) of a blocked term (line {line})")
    return hits


def find_gitleaks(explicit: str | None) -> str:
    for cand in (explicit, os.environ.get("GITLEAKS"), shutil.which("gitleaks"), str(GITLEAKS_FALLBACK)):
        if cand and Path(cand).exists():
            return cand
    raise Refused("gitleaks not found: pass --gitleaks, set GITLEAKS, or install it on PATH")


def gitleaks_hits(tree: Path, binary: str) -> tuple[list[str], int]:
    """Unexpected findings, and how many known placeholders were allowed."""
    report = tree.parent / "gitleaks.json"
    out = subprocess.run([binary, "dir", str(tree), "--no-banner", "--log-level", "error",
                          "-f", "json", "-r", str(report)], capture_output=True, check=False)
    if not report.exists():
        raise Refused("gitleaks wrote no report")
    text = report.read_text(encoding="utf-8")
    report.unlink()                       # it can hold matched values; never keep it
    try:
        findings = json.loads(text)
    except json.JSONDecodeError:
        raise Refused("gitleaks report is unreadable") from None
    # Fail closed: exit 0 means no findings and exit 1 means findings; anything else, or an exit
    # code that disagrees with the report, is a gitleaks error, not a clean scan.
    if not isinstance(findings, list) or out.returncode not in (0, 1) or (out.returncode == 1) != bool(findings):
        raise Refused(f"gitleaks failed (exit {out.returncode}, {len(findings) if isinstance(findings, list) else '?'} finding(s))")
    bad, allowed = [], 0
    for f in findings:
        rel = Path(f["File"]).resolve().relative_to(tree.resolve()).as_posix()
        digest = hashlib.sha256(f.get("Secret", "").encode()).hexdigest()
        if (rel, digest) in KNOWN_PLACEHOLDERS:
            allowed += 1
        else:
            bad.append(f"{rel}:{f.get('StartLine')} {f.get('RuleID')}")
    return bad, allowed


# ---- public side ---------------------------------------------------------

def clone_public(dest: Path, depth: int | None = 1) -> str:
    args = ["clone", "--quiet", "--branch", PUBLIC_BRANCH]
    if depth:
        args += ["--depth", str(depth)]
    git(*args, PUBLIC_URL, str(dest), cwd=dest.parent)
    return git("rev-parse", "HEAD", cwd=dest)


def tree_files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and ".git" not in p.relative_to(root).parts)


def drift(candidate: Path, cand_files: list[str], public: Path) -> dict[str, list[str]]:
    pub_files = tree_files(public)
    c, p = set(cand_files), set(pub_files)
    changed = [f for f in sorted(c & p) if (candidate / f).read_bytes() != (public / f).read_bytes()]
    return {"added": sorted(c - p), "removed": sorted(p - c), "changed": changed}


def header_sha(readme: Path) -> str | None:
    m = re.search(r"Snapshot of a private development repo at ([0-9a-f]{40})", readme.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def report(d: dict[str, list[str]], limit: int = 40) -> str:
    lines = [f"{len(d['added'])} added, {len(d['removed'])} removed, {len(d['changed'])} changed"]
    for kind, mark in (("added", "A"), ("removed", "D"), ("changed", "M")):
        for f in d[kind][:limit]:
            lines.append(f"  {mark} {f}")
        if len(d[kind]) > limit:
            lines.append(f"  ... {len(d[kind]) - limit} more {kind}")
    return "\n".join(lines)


# ---- modes ---------------------------------------------------------------

def run(mode: str, ref: str, gitleaks: str | None, fetch: bool) -> int:
    sha = resolve_pushed(ref, fetch=fetch)
    with tempfile.TemporaryDirectory(prefix="ps-publish-") as tmp:
        tmp = Path(tmp)
        (tmp / "candidate").mkdir()
        files = build_tree(sha, tmp / "candidate", load_ignore())
        print(f"source: {ref} = {sha} ({len(files)} files after .publicignore)")

        public = tmp / "public"
        public_head = clone_public(public, depth=None if mode == "publish" else 1)
        print(f"public: {PUBLIC_URL} {PUBLIC_BRANCH} = {public_head}"
              f" (snapshot of private {(header_sha(public / 'README.md') or '?')[:12]})")
        d = drift(tmp / "candidate", files, public)
        print("drift: " + report(d))
        if mode == "check":
            return 0 if not any(d.values()) else 1

        hits = blocked_hits(tmp / "candidate", files, load_terms())
        leaks, allowed = gitleaks_hits(tmp / "candidate", find_gitleaks(gitleaks))
        print(f"blocked terms: {len(hits)} hit(s); gitleaks: {len(leaks)} unexpected,"
              f" {allowed} known placeholder(s) allowed")
        for h in hits + leaks:
            print(f"  REFUSED {h}")
        if hits or leaks:
            raise Refused("snapshot has blocked terms or secrets; nothing was committed")
        if not any(d.values()):
            print("nothing to publish: the public repo already matches")
            return 0

        for f in tree_files(public):                      # stage: public tree := candidate tree
            (public / f).unlink()
        for f in files:
            dest = public / f
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tmp / "candidate" / f, dest)
        git("add", "-A", cwd=public)
        print("staged:\n" + git("diff", "--cached", "--stat", cwd=public))
        if mode == "dry-run":
            print("dry run: not committed, not pushed")
            return 0

        git("-c", f"user.name={AUTHOR_NAME}", "-c", f"user.email={AUTHOR_EMAIL}",
            "commit", "--quiet", "-m", f"Snapshot of private {sha}", cwd=public)
        git("push", "--quiet", "origin", PUBLIC_BRANCH, cwd=public)
        print(f"pushed {git('rev-parse', 'HEAD', cwd=public)}")

    print("rechecking ...")
    if run("check", sha, gitleaks, fetch=False) != 0:
        raise Refused("drift remains after publishing")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_const", dest="mode", const="check")
    mode.add_argument("--dry-run", action="store_const", dest="mode", const="dry-run")
    mode.add_argument("--publish", action="store_const", dest="mode", const="publish")
    ap.add_argument("--ref", default="origin/main", help="pushed ref to publish (default origin/main)")
    ap.add_argument("--gitleaks", help="path to the gitleaks binary")
    ap.add_argument("--no-fetch", action="store_true", help="don't fetch origin first")
    args = ap.parse_args(argv)
    try:
        return run(args.mode, args.ref, args.gitleaks, fetch=not args.no_fetch)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:              # fail closed: any check error is a refusal
        print(f"refused: unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
