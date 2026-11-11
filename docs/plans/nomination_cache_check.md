# Nomination cache check (pre-registered, Oct 31, 2026)

**Change.** `exchange/core.py` keeps one merged dict of each agent's latest nominated kW (`_latest_cache`), updated in
place by `_add_nom` whenever a nomination or a D47 re-base clip is stored. Before, `_latest` re-merged every stored
nomination on every call, and price rounds call it once per agent per priced step (PLAN M2a deferred item: 11 to
14 s per round). `dict.update` keeps first-seen key order, so the cached dict has the same values and key order as
the full merge. No summation order changes. Test: `tests/test_nomination_cache.py`.

**Pass test (set by the owner, Oct 31).** Rerun Jan 12-13 and Nov 28-29 at m = 0 with the D62 flag on, using the
unchanged configs `configs/d62/h2h_c_jan12_h100_g.yaml` and `configs/d62/h2h_c_nov28_h100_g.yaml`. Each must match the
D62 v2 in-run run (`runs/h2h_c_{jan12,nov28}_h100_g` in the main checkout, trace commit 4dc9a29; no code outside
docs/ and bench/d62_diag.py changed between 4dc9a29 and 4460374) exactly. Full pytest green. Any difference,
including floating point: stop and report. Report the speedup.

**What "exactly" means** (`bench/run_diff.py`, committed with this doc, tests in `tests/test_run_diff.py`):
- Both traces (selfish and exchange): same line count, every message line identical as text. Header lines equal
  after dropping `git_commit` and `run_at_utc`. The same config means the same `run_name`, so `run_id` (in every
  message) must match too.
- `summary.json` equal at every path, floats bit-identical (NaN equals NaN), after dropping these keys wherever they
  appear: `runtime_s`, `round_secs`, `run_at_utc`, `run_name`, `git_commit` (the owner's list) and `wall_utc`,
  `round_wall_utc`, `compare_solve_secs` (wall-clock fields the owner's "wall-clock and identity fields" covers;
  added without the owner, logged in `docs/overnight/2026-11-01.md`).
- `netload.csv` identical as text; any `agent_log.jsonl` identical as text.
- Integrity: every header in the new run carries exactly this commit (no `-dirty`); rosters equal; every roster agent
  sends at least one message in the exchange trace. Before each run, `exchange.core.__file__` is printed from the run
  worktree to show the cached code is the one loaded (the shared venv has main installed in editable mode).

**Procedure.**
1. Wait until the overnight queue's summary commit is on origin/main and its process has exited.
2. Pinned worktree `../pearl-street-m3-run`, detached at branch `nom-cache` (this commit). Copy (never move)
   `runs/m0_jan12` and `runs/m0_nov28` from the main checkout into its `runs/`.
3. Full pytest there. Then, one at a time with nothing else heavy running:
   `python -u -m sim.run_m1 configs/d62/h2h_c_jan12_h100_g.yaml > runs/cache_check_jan12.log`, then nov28.
4. `python -m bench.run_diff <main>/runs/h2h_c_jan12_h100_g runs/h2h_c_jan12_h100_g --commit <sha>` (and nov28).
5. Speedup: `runtime_s` and mean `round_secs` against v2 (Jan 12-13: 3,436 s, 29.4 s; Nov 28-29: 1,823 s, 14.7 s;
   machine load during v2 not controlled). Supplementary, exchange only: `python -m bench.replay_timing <v2 trace>
   4460374` times a replay of each v2 trace through the old and new exchange; both must reproduce the trace.

**Outcome rule (owner, Oct 31).** Both match and pytest green: merge `nom-cache` into main and push. Otherwise it
stays on the branch, and the overnight runs use main as it is.
