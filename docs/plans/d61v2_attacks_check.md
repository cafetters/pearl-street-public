# D61 v2: the attack check on fresh windows

> **DRAFT for the owner (Nov 6 evening, item 12; updated Nov 8 on branch `v2-prereqs`, item 6 of the Nov 7-8
> unattended block; re-pinned Nov 11 to the build commit `e33e9b9`, owner decisions 1-4). Still not a
> pre-registration.** It becomes one only when the owner has
> read it and it is committed with a message saying so. Nothing has run on the v2 windows, M0 included, and nothing
> runs on them before that commit. D61's pre-registered verdict (FAIL, `2d61911`) stands; v2 is a new test on new
> windows, not a rerun of D61.

## The test

**Question:** with D64's defenses on and the fixes since D61 (D87 to D90), does each scripted attack gain nothing,
push no honest agent below acting alone, and trip its named defense, in every window, at the $5 margin?

**Windows** (owner, Nov 6; 133 excluded days confirmed; `runs/nov06/v2_windows/`):

| Window | Days (local) | Recorded window peak, MW |
|---|---|---|
| dec11 | 2023-12-11 to 12 | 2,239.8 |
| jan09 | 2023-01-09 to 10 | 2,203.8 |
| feb26 | 2023-02-26 to 27 | 2,110.1 |
| jun02 | 2023-06-02 to 03 | 2,331.3 |

Run in that order.

**Settings, every run.** The configs are `configs/d61v2/` (127 files, generated Nov 8 by
`bench/d61v2_make_configs.py`; sha256s below), generated from the D61 template with the same rule:
- `configs/d58m/h2h_c_sep05_h100_m0.yaml` without comments;
- only `run_name`, `start`, `end`, `m0_dir`, `peak_response_margin_usd_mwh`, the flags below, and `attackers` in
  attack cells (`settlement_rule` in reruns) set.

The flags and settings:
- 100 MW headroom, arm C, `price_mode: true`, `settlement_reference: selfish_1p` (today's 1p),
  `peak_price_cap_usd_mwh: 100`, and imbalance on.
- `delivery_record: true`, `silent_reference: true`, `declared_reference: true`. Under this flag D87's widened void is
  in the code (merged `d68f4a6`).
- **`nomination_span: operating_day`** (D86), written out.
- **`peak_steps: controllable`** (D57), written out. The headline peak, share of the M0 ceiling and feasibility come
  from the `controllable` block; the full-slice values are reported as the footnote.
- **`d62_guarantee: false`**, written out. Before every run the queue checks that no config resolves it to true.
- `forecast_noise_sigma: 0.0`, so runs are deterministic (D85).
- **No `warmup_days`** (owner, Nov 6). It refuses `delivery_record`, and attack against twin cancels the cold-start
  edge.

**The attacker:** `DC_08` in every cell, as in D61: the largest battery power (10,619 kW), with its band far below its
full response.

**The attacks:** D61's ten wrappers, unchanged (`attacks/`). The named defenses:

| Cell | Named defense (must fire) |
|---|---|
| `renege_full` | imbalance beyond the band at the scarcity price, or no 1p pay for relief the meter didn't show |
| `renege_day` | the same |
| `renege_zero_b` | the attacker's peak settlement is no more than the twin's |
| `silent_deviate` | D78: imbalance against its 1p reference on bound steps; D65 counts the calls missed |
| `silent` | D78/D79; D65 counts every skipped binding call missed |
| `outage_stop` | D80: imbalance on bound steps never excused; D65 counts the outage late |
| `outage_run` | D81/D87: activity during the outage is a breach; the void fires |
| `misdeclare_power` | **D81 as widened by D87:** output above declared power is a breach; every reference computed with the breached declaration is replaced whole (`voided_calls` non-empty, required by `bench/d61v2_judge.py` where a breach occurred) |
| `misdeclare_reserve` | the same, for SOC below declared reserve |
| `infeasible_declare` | **D90:** judged only where the filed set is infeasible for 1p when filed (`bench/d90_check.py`, the D82 check replayed offline on the filed set); then D82's `infeasible_declaration` must fire. Otherwise the cell is **not exercised**. A replay that finds the set feasible while D82 fired (DISAGREE) fails |

**Not exercised** (not judged):
- a `renege_zero_b` cell with no opportunity (D61's rule);
- an `infeasible_declare` cell whose filed set was feasible when filed (D90).

An attack that never acted is **inert**, and its defense counts as not firing.

**Margins:** $5 for every attack (D75). Reneging also at m = 0, reported, not judged.

**Runs:** 60 main runs, as D61:
- 40 attack cells and 4 twins at $5;
- 12 reneging cells and 4 twins at m = 0;
- per window, the cold M0, the controllable M0 (`bench.m0 --controllable`) and feasibility with `--controllable`.

**Honest twins:** as in D61. Each twin is its cells' config minus `attackers`, run at the pre-registration commit.
Headers may differ only in `run_name`, `run_id`, `run_at_utc` and `attackers`, plus `config.data_dir` when both values
are absolute paths (D89, laptop against Maritime).

## Pass rule (judged on today's rule at $5 only)

For every judged $5 cell, against its twin:
1. **Gain:** the attacker's value minus its twin value is at most `GAIN_GUARD_USD` ($1e-6), from `agent_value_usd`
   as in D61.
2. **No bystander harm (D88 as revised by D91, replacing D61's flat $10 line):** no honest agent is newly below
   acting alone, and none is further below beyond section 8's tolerance.
   - The basis is the summary's `net_harm.by_agent_usd`.
   - **Newly below** (harm $0 in the twin, above $0 in the attack cell) fails at zero tolerance.
   - **Further below** (harm above $0 in the twin and higher in the attack cell) fails only when the increase is above
     max(1% of |acting-alone value|, $10) for that agent; the acting-alone value is the attack cell's
     `agent_value_usd + by_agent_usd`.
   - The zero-tolerance further-below count and $ (D88 as first built) are reported next to the line in every row.
   - Checked by `bench/bystander_check.py` with `further_below_tol="section8"`, called by `bench/d61v2_judge.py`.
   - **Disclosure (D91):** set Nov 7, after D61's FAIL and after seeing the jan12 rehearsal (a design window), where
     zero tolerance failed 5 of 6 cells on agents already below acting alone moving at most $0.21 further.
3. **Defense:** the named defense fires (table above).
4. **Roster:** every header-roster agent has a value in both summaries; the rosters are equal; the attackers come from
   the header.

**D61 v2 passes if every judged $5 cell passes.** Applied by `bench/d61v2_judge.py` (pinned below): each attacker
is judged on its own (a multi-attacker cell is NOT_EXERCISED only when every attacker is; the bystander line is per
cell and can't be split by attacker).

**Headline table, every cell, never dropped (owner, Nov 6):**
- the **twin-relative loss**: honest agents below their twin value, with the count and the sum, at zero tolerance;
- the **zero-tolerance further-below** count and $ (D91);
- the controllable-steps peak against the twin's, plus `pre_from_rebound`;
- operator payout against the twin.

**Reported per cell, zero or not:**
- every D61 counter;
- D61's $10 twin-harm count, now reported, not judged;
- `voided_days` and `voided_calls`;
- the D90 feasibility-at-filing result;
- the full-slice peak (the footnote).

**Conditional max-rule reruns** (report only), as D61: any cell with an attacker's gain above $1e-6, and its twin
once. The cell-to-twin mapping is `twin_of()` (`bench/d61v2_run_queue.py`, D90), which covers every attack name.

## Where it runs

- **Maritime**, through `bench/d61v2_cloud.py`, which checks the configs and M0 folders, orders the runs, calls
  `bench/cloud_queue.py` (PLAN section 6) in its own foreground and then runs `bench/d61v2_judge.py` on the downloads.
  - Concurrency 10 or less; Small sandboxes; a declared budget; time limit 2× the concurrency-10 runtime.
  - An empty sandbox list after every queue; cost logged.
  - M0, controllable M0 and feasibility run on the laptop, one window at a time (LP only).
- **Order:** M0 and feasibility per window; then each window's $5 twin and its cells, with each cell's check after
  its pair; then the m = 0 twins and cells; then the conditional reruns.
- **Estimate (Nov 8, from the rehearsal on D61's windows at `d471a72` with these settings) [sim, in-run]:**
  - Runtime: a $5 run took a median of about 800 s at concurrency 10 (`v2r-main`, 7 runs) and 666 s at concurrency 6
    (`v2r-main2`, 37 runs); an m = 0 run 229 s (16 runs). Time limit 1 h (`--measured-runtime-s 1000`).
  - Cost: `v2r-main2` cost $0.199979 for 53 sandboxes ($0.0046 per $5 run, $0.0020 per m = 0 run, median). Main runs
    (44 at $5, 16 at m = 0): about $0.25 to $0.30, about 1.5 h at concurrency 10. Worst-case reruns (60 more): about
    the same again.

## Must be done before the pre-registration commit (each with a real trace, CLAUDE.md)

**Status Nov 8 (branch `v2-prereqs`, pushed, not merged):**
- 1. Done on the branch: `silent-harm` merged (`10fd56e`), with D91 (`7a3c862`). Real traces: the rehearsal's jan12
  cells.
- 2. Done: `bench/d90_check.py` (`5ebc354`). Real traces: D61's four (jan12 and feb02 NOT_EXERCISED, jan06 and jun29
  PASS) and the rehearsal's four (match the offline recheck).
- 3. Smoke at the build commit `e33e9b9` (Nov 11, owner decision 3, option (b)): Maritime, queues `v2p-smoke2` and `v2p-smoke3`, cost
  $0.020087 in total, sandbox lists empty after each; judged by `bench/d61v2_judge.py` at `e33e9b9` (header clean)
  [sim, in-run, sep05; design-window evidence, report only].
  - Gate (simulation failure only, as D61): passes. One run stopped at registration, 4 s in, because of my config
    naming error, not the code under test: the first single-attacker config, `h2h_c_d61v2smoke1_sep05_misdeclare_reserve_m5`,
    gave a `run_id` of 65 characters (run name plus `-exchange_h100_rinfo`), above the exchange's 64-character id
    limit, so every registration was rejected. It was renamed `h2h_c_d61v2smk1_...` and rerun. Every v2 config's
    `run_id` is at most 63 characters.
  - **`DC_03` misdeclare_reserve alone** (`h2h_c_d61v2smk1_sep05_misdeclare_reserve_m5` against
    `h2h_c_d61v2smoke1_sep05_twin_m5`): **PASS**, gain exactly $0.00, defense fired, `voided_calls` GRID_OP:2, 3, 4, 8;
    bystander PASS (0 newly, 0 further below; 0 at zero tolerance); twin-relative loss 0; controllable peak and payout
    equal to the twin's.
  - **Ten-attacker cell** against its twin: agent values identical to the `55c0897` smoke (largest difference
    $0.00), so the same per-attacker result: `DC_03` FAIL at +$5.88 and every other attacker PASS, with `PVB_39`
    NOT_EXERCISED; bystander PASS. Alone, `DC_03` gains nothing, so the +$5.88 comes from the other nine attackers'
    price moves (the twin is the counterfactual for the cell, not for one attacker among ten). v2 has one attacker
    per cell.
  - The two sep05 twins (`smoke` and `smoke1`) are identical (largest agent-value difference $0.00).
- 4. Pins: below, at the build commit `e33e9b9` (Nov 11). Only `bench/d61v2_judge.py` and `bench/d90_check.py`
  changed since `55c0897` (the wider dirty check below); the generator is unchanged, so the configs were not
  regenerated and the manifest's sha256 is unchanged. The queue is `bench/d61v2_cloud.py` (Maritime), not `bench/d61v2_run_queue.py`'s local WMI queue;
  `twin_of()` still comes from `bench/d61v2_run_queue.py`.
- Also built: `bench/d61v2_judge.py` (`3eb6715`, gaps 1, 2, 6), `bench/d61v2_make_configs.py` (`31d5653`, gap 4),
  `bench/d61v2_cloud.py` (`55c0897`, gap 3).
- Nov 11 (owner decision 2): the dirty checks of `bench/d61v2_judge.py` (`tool_header`) and `bench/d90_check.py`
  (`code_commit`) mark a commit `-dirty` on any modified **or untracked** file under `bench/`, `sim/`, `agents/`,
  `exchange/` or `attacks/` (git-ignored files excluded), and when `git status` fails. D90's commit guard refuses a
  dirty code root (`e33e9b9`, 16 tests, red first).

1. **Merge `silent-harm`** (`bench/bystander_check.py`, 8 tests). It has run on the 8 D61 silent cells (a real
   trace). Merge only after your OK.
2. **Build the D90 check as a tool**, from `runs/nov06/infeasible/d82_recheck.py`, with tests. It must reproduce the
   in-run D82 decision on D61's four `infeasible_declare` traces: feasible on jan12 and feb02, infeasible on jan06 and
   jun29.
3. **A smoke pair on sep05 at the build commit** with the v2 settings (operating_day, controllable, D87): the honest
   twin plus the ten-attacker cell. Report only; it gates on simulation failure as in D61. sep05 is already excluded.
4. **Pin** the config generator, the queue and the tools by sha256 in this file.

## Disclosures

- **D88 was set after D61's FAIL** (`2d61911`). It is a different harm line from D61's, and it can't re-judge D61.
- **The fixes were designed on D61's four windows** (D87 on its misdeclare cells; D88 and D90 on its silent and
  infeasible cells). v2's windows are fresh for exactly that reason.
- **Reneging at m = 0 is a known open gain**, reported, not judged.
- Under the max rule, feb02's m = 0 `renege_zero_b` gained $3.37 (cloud rerun, report only).
- **One attacker.** Smaller batteries, several attackers and collusion are not tested.
- **D57's controllable headline** is new in v2. In the re-score of the design windows, `pre_from_rebound` was set in
  7 of 33 runs (jan02 +83.9 MW) [sim, offline]. v2 reports it per cell.
- **Exclusions:** January has one window left (jan09). Every new test spends scarce winter days.

## What was seen before this was written

- Every D61 result.
- The offline analyses of Nov 6 (T3, T2, D82 recheck, D57 re-score).
- The sep05 T3-c check runs (`t3c-par`).
- **For the v2 windows, only their recorded daily peaks.** No M0, no run, no price.

## Pins (Nov 11, DRAFT; LF sha256 = `git show <commit>:<path> | sha256sum`, this checkout has core.autocrlf true)

Tools at the build commit `e33e9b9` (Nov 11; the two changed since `55c0897` are marked):

| File | sha256 |
|---|---|
| `bench/d61v2_judge.py` (changed Nov 11) | `e5273942d9f5ad66dba635420278e879c89638736bf3f0768d9ae331fc4f294f` |
| `bench/d90_check.py` (changed Nov 11) | `0aff77cd8ac83a0f04d23c7c162fffc29ac020f455ecbe8491842af8ce03c07c` |
| `bench/bystander_check.py` | `ccaeeb6af7b6e9c052c492f9d39b0bf5ebb225255ccd2b3b497330ec53075532` |
| `bench/d61_check.py` (unchanged from D61) | `e185d2709721031f8c44a0bba8401f21913032571580bbeaa24f98650d78a360` |
| `bench/d61v2_cloud.py` | `66c4c31fae8385abe9e1e1ecd6fe1bb993f9545b614c423ab7d0014d5c2acf2d` |
| `bench/d61v2_make_configs.py` | `d701e90d4bf4dcafea89e8c8190d4ae14df42ea619942005cd9d1ff22d0f9e93` |
| `bench/d61v2_run_queue.py` (`twin_of`) | `34eed1f848663336ae58aa91af7a17b6d6a63d2c1090238d39369076911bf468` |
| `bench/tolerances.py` | `0dce6da982bcf4817b083ba3451bb6d4ba3e7c3fab1556e060e643580b857d04` |
| `bench/cloud_queue.py` | `848572c6e16105a62e6e21cd0c12ab8a9ea07c502d1f2e752453703937c21ea8` |
| `configs/d58m/h2h_c_sep05_h100_m0.yaml` (template) | `b827d66b86964701ea3779d0979e6b399208703f3cfd78496c7c2205daa9da56` |

Configs: `configs/d61v2/` (127 files), one LF sha256 per file in `docs/plans/d61v2_configs.sha256`; that manifest's
own sha256 is `2aafd98b4d8acfa22054ce012fb48ab8ed704393a61cbed865f2ec5b541fce2e`.

## What remains for the owner before the pre-registration commit (Nov 11)

Done Nov 11: D91 approved as staged and applied to PLAN.md on main (`cb5b0b1`); the dirty-check fix (`e33e9b9`, the
build commit); the smoke at the build commit (prerequisite 3); these pins; `v2-prereqs` merged into main (the merge commit follows this edit).

1. The owner's go for the pre-registration commit: this file without "DRAFT", committed on main with a message
   saying so. The pins above name blobs; any later change to a pinned file needs new pins. Twins and cells run at
   that commit; the judge's D90 step needs a clean checkout of it (`--d90-code-root`).
2. Then, at that commit: M0, controllable M0 and feasibility for the four v2 windows (nothing has touched those
   dates), then the queue.
