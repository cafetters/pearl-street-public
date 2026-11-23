# Q-N10 parity and the 80-agent proven-capacity check (Nov 20, 2026; report only)

**Run:** Maritime, queue `qn10-pc80`, commit `e1fda32` (`m5-proven`), 13 runs, $0.18, cleanup confirmed. Both
expected results were declared before the run in `configs/parity_qn10/README.md` and `configs/proven_check/README.md`.

## Q-N10 parity (`nomination_power_check` on): PASS, 11 of 11 [sim, in-run]

- `bench/qn10_parity.py` against `../pearl-street-cloud/runs/cloud/_par_main`.
- **Result:** every exchange and selfish trace is identical line for line once the flag's `idle_reference` lines (560
  in jan06) and the header are set aside, with no `over_power_nomination` refusal.
- **The flag was live:** the header records it, and the metering service sent the references.
- **So, as declared:** D107 (default on) can be proposed. The owner decides.

## Proven capacity with the existing fleet only (owner change 1): the price rule FAILS on one call [sim, in-run]

D61 v2's dec11 twin config (80 agents, D78 on), run off and on (`proven_capacity`, no newcomers, existing prior
Beta(99, 1)) at the same commit.

| Measure | Off | On | Declared rule | Verdict |
|---|---|---|---|---|
| Final peak price, GRID_OP:2 / 3 / 4 / 8 ($/MWh) | 23.718 / 23.614 / 23.415 / 23.498 | 23.738 / 23.613 / 23.440 / **25.321** | each within 1% | **FAIL**: call 8 +7.75%; the other three within 0.11% |
| 1p peak settlement per agent | | | each within max(5%, $1) | PASS: 80 of 80; worst PVB_48 $589.38 → $616.79 (+4.7%) |
| Fleet 1p settlement, total | $13,625.02 | $13,950.08 | (report only) | +$325.06 (+2.4%), honest agents paid more |
| Metered peak | 2,168.50 MW | 2,168.50 MW | (report only) | identical |
| Proven capacity | | 160 updates, 0 misses, 0 cuts, max uncovered 0 kW | (report only) | |

**What it shows.**
- Counting existing assets at rate 0.99 (Beta(99, 1)) lowers counted relief by 1%, so the price loop settles a little
  higher.
- On call 8 the difference is 7.75%: the loop's path is not linear in the counted total.
- Nothing physical changed: the same peak, no misses, no uncovered shortfall.
- The money moves to the honest agents (+2.4% in total).

**The owner's options** (no change made):
1. Count assets with meter history at rate 1 until their first miss. The rate then starts from the strong prior.
2. A stronger prior, e.g. Beta(999, 1).
3. Accept the price move and judge the change at the gate.

The gate is still the M2a $5 and D61 v2 checks, which are not run here.

**Owner decision (Nov 20): option 1.** Assets with meter history count at rate 1 until their first miss (`bd3e3f5`).
The synthetic existing-fleet test now requires an exact match with flag-off. The 80-agent check is rerun at the new
commit under the same declared rule.

## Rerun at `81cb416` (Nov 20, 21:43 ET): PASS, identical to flag-off [sim, in-run]

Queue `pc80-v7`, 2 runs, $0.01, cleanup confirmed. Same configs and the same declared rule.
- **Final peak prices:** GRID_OP:2 / 3 / 4 / 8 identical to flag-off (23.717577 / 23.613942 / 23.414674 /
  23.498459 $/MWh).
- **1p peak settlement:** 80 of 80 agents identical; fleet $13,625.02 both.
- **Exchange trace:** all 26,935 messages identical once the flag's 560 `idle_reference` lines, the `asset_type`
  field and the header are set aside.

**The path there:**
1. Rate 1 until the first miss (`bd3e3f5`) removed the 7.75%.
2. Rescaling only when a factor is below 1 (`9b29f90`) removed one float difference, but call 8 still moved +0.34%
   at `919284e`.
3. The rest came from summation: the proven path added the counted zone totals with Python 3.12's `sum()`, which
   compensates rounding, while flag-off adds with a plain `+=`. Fixed in `c437323`, with a red-then-green test.

## Full pytest on the trial merge (Nov 20, 23:32 ET): 1,225 passed, 0 failed [Maritime]

- **Merge:** local branch `m5-trialmerge`, merge commit `ffc56b8` = `m5-proven` at `aa5ffd9` + `origin/main` at
  `3685ac3`, no conflicts. Not pushed.
- **Result:** 1,225 passed, 13 skipped, 1 xfailed, 0 failed (21 min 42 s, queue `pt-m5merge-ffc56b8`). The skips
  need saved runs or `../data` that the sandbox doesn't have.
- **Since then:**
  - `m5-proven` added only docs, the D78 parity configs, `bench/d78_parity.py` and its tests (3 passed locally).
  - `origin/main` moved to `c9cbe3e`, which adds one report file (`docs/overnight/2026-11-21-m2b-diagnosis.md`).
  - The real merge still needs a fetch and merge, the targeted tests for the touched files, and the owner's go.

## Full pytest on the merge into main (Nov 23, 18:01 ET): 1,228 passed, 0 failed [Maritime]

- **Commit:** `78ee916` on main = merge `a439972` (`m5-proven` at `6c239b5` + `origin/main` at `c9cbe3e`, no conflicts)
  plus the PLAN entries D106 to D110.
- **Result:** 1,228 passed, 13 skipped, 1 xfailed, 0 failed (9 min 48 s, queue `pt-main-78ee916b`, $0.004). The skips
  need saved runs or `../data` that the sandbox doesn't have. A first launch (`pt-main-78ee916`) ran the simulator
  instead of pytest (the `--module bench.cloud_pytest` flag was missing); it failed at config load, cost $0.001, and
  tested nothing.
- **After it:** docs only (D110's scope line for D101-excluded agents, the review notes).
