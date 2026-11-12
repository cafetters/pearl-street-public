# flex-exchange

Snapshot of a private development repo at 2b872c70afda5e98111407c5a52e222b669b9b2a. Published for MIT AI Studio Fall 2026 HW8 review. License TBD.

docs/PLAN.md and some internal notes are referenced in comments but kept in the private repo.

Python 3.12 project for simulating a flexibility exchange on Engie data.

## Layout

| Folder      | Purpose                                                   |
|-------------|-----------------------------------------------------------|
| `exchange/` | Exchange / market mechanism (pure logic, never reads data files) |
| `sim/`      | Simulation driver that loads data and runs the exchange   |
| `agents/`   | Market participant agents                                 |
| `attacks/`  | Adversarial / manipulation scenarios                      |
| `bench/`    | Benchmarks and evaluation                                 |
| `configs/`  | Run configurations (YAML)                                 |
| `runs/`     | Run outputs (git-ignored except `runs/samples/`)          |
| `docs/`     | Project documentation                                     |

## Data

Engie data lives in `../data` (sibling of this repo) and is never copied here.
Configs reference it via `data_dir: ../data`. All timestamps are UTC.

## Setup

```sh
python -m venv .venv
.venv/Scripts/activate      # Windows; use .venv/bin/activate elsewhere
pip install -e .
```

## Publishing

`cafetters/pearl-street-public` is a one-way, filtered mirror of this repo. It
is only ever written by `scripts/publish_public.py`, never edited by hand.

```sh
python scripts/publish_public.py --check      # drift between origin/main and the public repo; changes nothing
python scripts/publish_public.py --dry-run    # build, scan and stage in a temp clone; no commit, no push
python scripts/publish_public.py --publish    # commit "Snapshot of private <sha>", push, recheck for zero drift
```

- The source is a pushed ref (`--ref`, default `origin/main`); a commit not on origin is refused.
- `.publicignore` lists the paths left out. Local absolute paths are rewritten as relative ones, and the public
  README header is regenerated with the source SHA.
- Publishing is refused on any hit from `%LOCALAPPDATA%\pearl-street\public_blocked_terms.txt` (kept outside any
  repo; one term per line, or a regular expression on a line starting with `re:`) and on any gitleaks
  finding other than the known test placeholders. gitleaks is found via `--gitleaks`,
  `GITLEAKS`, `PATH`, or `%LOCALAPPDATA%\pearl-street\bin\gitleaks.exe`.
- Fail closed: a missing or empty blocked-terms file, a missing gitleaks, a gitleaks error (an exit code other than 0 or 1,
  or one that disagrees with its report) or any other error during the checks is a refusal; nothing is published.
- `tests/test_publish_public.py` covers the filters and the fail-closed cases.
