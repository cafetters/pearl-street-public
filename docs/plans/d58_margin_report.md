# D58 risk-margin test

**Pre-registered Oct 29, 2026, before any run with a margin.** The commit that adds this file is the pre-registration commit. Its hash is recorded in the results section when results are added.

**Integrity:** when each verdict is written, `git diff <pre-registration commit> -- agents/ sim/ exchange/ bench/d58_margin.py bench/d58_queue.py bench/d56_resettle.py bench/d58_horizon.py configs/d58m/` must be empty, and so must the diff of this file's sections "The test" and "Run order". Every run header must show the pre-registration commit without `-dirty`.

**Owner decisions (Oct 29):**
- m ∈ {$1, $3, $10} per MWh.
- The relief floor is 75% of today's cut, in every window.
- Run order is bisected, with $3 first and at most two margins.
- Runs are staged: a margin stops at its first relief failure.
- **A PASS is report only.** D58, M2a and PLAN.md do not change until the owner decides after seeing results.

## Why
D58 is the open item behind M2a's no-net-harm failure. Under D56's rule 1p, settlement harms no one. The harm left is the day-ahead vs. real-time gap: arm C agents plan on DA prices and shift energy until the peak price only just covers their lost DA arbitrage, then are settled at RT.

The D58 horizon test (pre-registered at 922d7fd) failed on 2 of 80 agents: PVB_39 and PVB_22, $174 beyond tolerance in total. The site diagnostic showed those agents earn only $0.36-0.50 per MWh shifted. The gap swings by tens of $/MWh between windows.

**The lever:** a forecast-risk margin m in the agent's re-plan. An agent moves energy only where the peak price beats its lost DA arbitrage by about m.
- It is the "cautious" risk posture of the strategy design (`docs/plans/agent_strategy_design.md`). `normal` is m = 0, today's behavior.
- Here every agent uses the same m. That tests whether any uniform margin passes.

## What was seen before this was written
- Every m = 0 result in this test's windows: the D56 confirmation cells, the horizon test, the site diagnostic and the bystander table.
- The failing agents' figures in per-MWh-cut terms. With "MWh shifted" counting both legs, $0.36-0.50 per shifted MWh is about $0.7-1.0 per MWh of peak cut, and their horizon gap of about $1.9-2.3 per shifted MWh is about $4-4.6 per MWh cut.
- So $1 sits at the margin they earned, $3 below their horizon gap, and $10 well above it. The values are not blind.
- **The fresh window** (Sep 5-6) was picked by rule from recorded peaks only. Its M0 ceiling and target feasibility (feasible at 100 MW) were computed before this commit. No run on it exists.
- **No run with m > 0 exists** on real data. The build was tested on synthetic data only.

## The lever as built
- **Planner** (`agents/planner.py`, `solve(anchor=...)`): adds (m/2) × |site net − anchor| per MWh at every planned step. A shift moves energy on two legs: the peak-step discharge, and about 1/η² as much extra charging elsewhere. It therefore pays about m per MWh of peak cut.
  - The toy test checks the exact threshold: lost arbitrage + m/2 × (1 + 1/η²).
  - The margin term is not in `Plan.cost`.
  - With m = 0 the LP is exactly today's (tested).
- **The anchor** (`agents/battery.py`): the agent's own selfish plan at the same step, SOC, forecasts, commitments and binding locks, with no peak price and no prox term. It is cached per input set.
- **When the margin applies:** only for a participant, and only when a peak price is in force. The selfish (acting-alone) run never uses it. The exchange, the 1p reference (`sim/reference.py`) and settlement are unchanged.
- **Margin log:** whenever the margin binds, the agent writes an entry to `agent_log.jsonl` in the run folder. It binds when either:
  - the plan sits off its anchor, or
  - the margin held back a shift that the m = 0 plan would make.

  Each entry records posture, m, MWh shifted, MWh declined against the m = 0 plan at the same re-plan, whether it came from the gate's final round, and a reason. The purpose is to tell honest caution apart from capacity withholding in M3's red-team checks.
- **Config key** `peak_response_margin_usd_mwh` (default 0). It is recorded in the trace header. Each price round and each run variant also records a wall-clock timestamp.

## The test
**Cells:** the 11 windows of the horizon test at 100 MW, for each margin run. Every cell settles on 1p in-run at D54's 100 $/MWh cap. The configs are `configs/d58m/h2h_c_<window>_h100_m<m>.yaml`. Each is a copy of the original cell's config with only these keys set: `run_name`, `peak_price_cap_usd_mwh: 100`, `settlement_reference: selfish_1p` and `peak_response_margin_usd_mwh`.

**PASS for a margin m requires all three gates:**
1. **Horizon gate**, unchanged from 922d7fd (`bench.d58_horizon.horizon_check`): every agent's result vs. acting alone, summed over the 11 windows, is ≥ −max(1% × |summed acting alone|, $10).
2. **Settlement gate:** 1p settlement-caused harm is 0 in every window.
3. **Relief floor:** in every window, the peak cut from the first gate against the selfish run is ≥ 75% of the m = 0 cut in that window.

**The m = 0 cuts** (MW, from `runs/d58/horizon.json` at 922d7fd; exact values in `bench/d58_margin.py`):

| Jan 12-13 | Nov 28-29 | Jan 2-3 | Jan 16-17 | Jan 31-Feb 1 | Feb 12-13 | Jun 19-20 | Sep 21-22 | Jul 1-2 | Aug 28-29 | Jul 14-15 |
|---|---|---|---|---|---|---|---|---|---|---|
| 180.3 | 115.0 | 139.3 | 41.9 | 102.8 | 246.2 | 90.0 | 94.8 | 93.8 | 85.6 | 81.3 |

**Verdict order:**
1. A window below the relief floor fails the margin (FAIL_relief). This is judged on the windows run.
2. Otherwise the harm gates need all 11 windows; if any is missing the margin is INCOMPLETE.
3. The settlement gate is checked next (FAIL_settlement).
4. The horizon gate is checked last (FAIL_horizon or PASS).

**Fail-closed checks** (`bench/d58_margin.py`):
- each cell settled on 1p in-run at the 100 cap;
- the run header's margin equals the cell's margin;
- the roster comes from each run header, is complete in every cell, and is identical across cells (the horizon tool's check);
- the cell checks of `bench.d56_resettle`: accounting, settlement match and roster.

A failed check stops that cell and is recorded. No tolerance, gate or margin changes after any run.

## m = 0 baseline check (before any margin run)
The m = 0 comparators ran on older commits, and seven of them under the recorded reference at the 500 cap. Two m = 0 reruns on this build come first:
- `h2h_c_jan12_h100_m0` against `h2h_c_jan12_h100_1p`;
- `h2h_c_nov28_h100_m0` against `h2h_c_nov28_h100`. Nov 28-29 is the re-settled cell with the highest max round price (51.09 $/MWh; the others are Feb 12 22.6, Jun 19 32.7, Sep 21 49.5, Jul 1 42.9, Aug 28 24.2 and Jul 14 40.2). It also tests directly the claim that a cap-100 run gives the same result.

**Match** means the peak, the cut from the first gate, and every agent's result vs. acting alone all agree within 1e-6 ($ and MW; float noise only).

**Mismatch rule:** if either window mismatches, or either rerun fails, the other nine m = 0 cells are re-run on this build. Those reruns become the comparator for the relief floor and for the reported m = 0 figures, before any margin is judged.

## Run order
1. **Bisection:**
   - Run m = $3 first.
   - $3 PASS → run $1.
   - $3 FAIL on horizon → run $10.
   - $3 FAIL on relief → run $1.
   - Stop after the second margin, so at most two margins run.
   - If $3 ends FAIL_settlement, INCOMPLETE or a failed check, no second margin runs, and the owner decides.
   - Configs exist for all three margins. Only those the rule reaches are run.
2. **Within a margin:** Jan 12-13, then Nov 28-29, then Jan 2-3, Jan 16-17, Jan 31-Feb 1, Feb 12-13, Jun 19-20, Sep 21-22, Jul 1-2, Aug 28-29 and Jul 14-15. After each run, the window's relief is checked. A margin stops at its first window below the floor.
3. **Determinism:** right after the first margin cell (Jan 12-13 at $3), it is re-run as `h2h_c_jan12_h100_m3_det`. Its summary and cell result must match exactly, except for the fields listed below. A mismatch is recorded and reported, and the queue continues.
4. **Crashes:** a crashed run is recorded and the queue continues. A margin with a missing window cannot pass the harm gates (INCOMPLETE).
5. **Fresh window (confirmation, reported separately):** Sep 5-6, 2023.
   - **How it was picked:** the breadth rule in `docs/overnight/2026-10-26.md` applied to all of 2023 after the 11 windows (`runs/d58m/fresh_window.json`; peak day 2,386.1 MW). The check reproduces M0's recorded peaks.
   - **Runs:** m = 0 runs once for its own baseline cut, then each margin that passed.
   - **Confirmation passes if:** 1p settlement-caused harm is 0, and the cut from the first gate is ≥ 75% of its own m = 0 cut.
   - Per-agent results vs. acting alone are reported, not gated, because the horizon gate can't judge one window.
   - If no margin passes, the fresh window is not run.

**Rules the owner did not set (defaults chosen here; owner to confirm):**
- A failed baseline rerun counts as a mismatch.
- If $3 ends FAIL_settlement, INCOMPLETE or a failed check, no second margin runs.
- The verdict order: relief, then completeness, then settlement, then horizon.
- The fresh window is not run if no margin passes.
- The queue keeps the display on as well as the system (ES_DISPLAY_REQUIRED), because this laptop uses S0 Modern Standby.
- Shifts that the margin stops entirely are logged too. The owner's rule named only the case where the margin term has a cost.

**Fields excluded from exact-match checks** (tested before this commit on the overnight double run of Jan 2-3, `h2h_c_jan02_h100` vs. `h2h_c_jan02_h100_rerun`, which matches exactly under this list; NaN on both sides counts as a match): `header` (config, commit, run time, roster: the rosters are checked separately), `runtime_s`, `wall_utc`, `round_secs`, `round_wall_utc`, `compare_solve_secs`, `run_at_utc`, `git_commit`, `tool_sha256_16`, and the run names themselves. The m = 0 baseline check compares only the named quantities, so the fields this build added don't enter it.

**Queue** (`bench/d58_queue.py`, run on the owner's Windows laptop):
- one process at a time, from the repo root;
- Windows is asked to stay awake for the whole queue;
- any gap over 5 minutes between a run's consecutive wall-clock marks is flagged as a possible sleep stall;
- `runs/d58m/queue_status.json` is written after every run.

## Reported, not gated
Each figure is reported per margin, next to m = 0, and next to the run's share of the M0 ceiling. The report's summary table leads with share of the ceiling.
- Share of the M0 ceiling for peak and for value, and the gap share.
- Cut from the first gate and the relief ratio.
- Agents harmed and $ harm, and settlement-caused harm.
- Operator net payout, the extra payout vs. m = 0, payout per relief MWh and per MW cut.
- Maximum round price, settled price, and rounds at the cap.
- How often the margin bound, and MWh declined at each gate's final round.
- Agents below zero, the median agent, each agent's worst window, gap sign per window and per agent, and a breakdown by type.
- The result without Nov 28-29 (sensitivity only).
- The lowest passing margin.

## Disclosures
- A fleet-wide m tests whether any uniform margin passes. It does not settle the deployed design, which is expected to be a per-agent posture chosen by the strategy layer (D12).
- An operator top-up to acting-alone would remove the $174 harm directly but invites over-shifting: agents keep the upside while the downside is covered. The margin addresses the risk on the agent side instead.
- **Not blind:** see "What was seen before this was written".
- **A sample, not a season:** 11 high-peak two-day windows, plus one fresh one.
- **Payout figures** rest on D54's placeholder cap (Q-N1). A margin is expected to show up partly as a higher payout rather than a smaller cut, because the exchange raises its price to get relief back.
- **A PASS changes nothing by itself** (owner: report only).

## Results (Oct 30, 2026)

**Pre-registration commit:** `6156964`. Every run in this test (27 runs) carries that commit in its header without `-dirty`. When the queue finished, `git diff 6156964 -- agents/ sim/ exchange/ bench/d58_margin.py bench/d58_queue.py bench/d56_resettle.py bench/d58_horizon.py configs/d58m/ docs/plans/d58_margin_report.md` was empty. The tool outputs are in `runs/d58m/` (`verdict_m3.json`, `verdict_m10.json`, `confirm_m10.json`, `report.md`, `queue_status.json`).

**Verdicts:**
- **$3: FAIL_horizon.**
- **$10: PASS.** Its fresh-window confirmation also passed.
- **$1 was not run.** The bisection rule ($3 FAIL on horizon → $10; at most two margins) never reaches it.
- **The lowest passing margin among those run is $10.** Nothing between $3 and $10 was tested.

**Checks before any margin was judged:**
- **Baseline: MATCH.** `h2h_c_jan12_h100_m0` and `h2h_c_nov28_h100_m0` reproduce the original m = 0 cells exactly (largest per-agent difference $0.00). The original cells therefore stayed the comparator. The Nov 28-29 rerun, at cap 100 with 1p in-run, matches the re-settled cell.
- **Determinism: MATCH.** `h2h_c_jan12_h100_m3_det` reproduces `h2h_c_jan12_h100_m3` outside the excluded fields.
- **Fail-closed cell checks** passed in every cell: 1p in-run at cap 100, the header margin, a complete and identical roster of 80 agents, accounting and the settlement match.

### Summary over the 11 windows

| | m = 0 | $3 | $10 |
|---|---|---|---|
| Peak cut from the first gate, sum of windows (MW) | 1,271.0 | 1,265.4 | 1,271.9 |
| Share of M0 ceiling, gap share | per window, below | within 1 point of m = 0 | within 1.6 points of m = 0 |
| Relief, lowest window | — | 98.4% (Jan 16) | 98.7% (Jan 2) |
| Settlement-caused harm | $0 | $0 | $0 |
| Horizon gate: agents beyond tolerance | 2 ($174 beyond) | 1 (PVB_22, $37 beyond) | **0** |
| Agents below zero over 11 windows | 8 | 6 | **0** |
| Median agent over 11 windows | — | $433 | $1,100 |
| Harmed agent-windows; $ harm (forecast error only) | 108; $20,783 | 54; $13,233 | 36; $8,549 |
| **Operator net payout** | **$42,335** | **$61,501** | **$106,691** |

- **Per-agent results:** PVB_39 is −$865 at m = 0, −$376 at $3 and +$573 at $10. PVB_22 is −$130, −$140 and +$345.
- **At $10, harm is left in only two windows:** Jan 12-13 (11 agents, $5,607) and Nov 28-29 (25 agents, $2,942).
- **By type at $10, over the 11 windows:**
  - COM: +$7,224 (lowest agent +$14)
  - DC: +$15,549 (lowest +$379)
  - PVB: +$71,641 (lowest +$199)
  
  At m = 0 the totals were $1,735, $3,379 and $22,218.

**What the margin does.** The cut barely moves, because the exchange raises its price until it gets the relief back.
- **Payout per relief MWh** rises from $5.7-16.6 at $3 to $13.4-26.2 at $10.
- **Payout rises roughly linearly:** about $6.4k over the 11 windows for each $1/MWh of m (both steps give the same slope).
- **Harm falls by less each time:** about $2.5 of extra payout per $1 of harm removed from m = 0 to $3, and about $9.6 per $1 from $3 to $10.
- **The margin bound for all 80 agents in every window**, at both margins. "Declined MWh" at the final round of each gate, summed over gates (both legs, against the m = 0 plan at the same price): $3: 91-643 per window; $10: 466-1,433 per window.
- **The highest round price reached the 100 cap only on Jan 16-17** (102 rounds at the cap at both margins). Jan 16-17 is the infeasible-target window. Every other window's highest round price stayed at or below 57.3 $/MWh.
- **The gap's sign is unchanged:** it is a cost in 5 windows and a gain in 6 at every margin.

### Per window, m = $3 (FAIL_horizon)

| Window | Share of M0 ceiling (m=0) | Value share (m=0) | Gap share (m=0) | Cut MW (m=0) | Relief | Harmed (m=0) | Harm $ (m=0) | Settlement harm | Payout $ (m=0) | Extra payout $ | Max round price |
|---|---|---|---|---|---|---|---|---|---|---|---|
| jan12 | 94.5% (95.4%) | -17.8% (-17.9%) | 96.3% (96.8%) | 179.3 (180.3) | 99.4% | 13 (13) | 7,243 (7,902) | 0 | 9,430 (6,970) | 2,460 | 27.4 |
| nov28 | 49.2% (49.2%) | 7.1% (4.7%) | 0.0% (0.0%) | 115.1 (115.0) | 100.1% | 33 (53) | 5,768 (10,074) | 0 | 8,175 (5,915) | 2,259 | 50.8 |
| jan02 | 83.7% (83.8%) | 60.0% (58.5%) | 73.4% (73.6%) | 139.0 (139.3) | 99.8% | 5 (26) | 110 (831) | 0 | 6,492 (4,155) | 2,337 | 22.2 |
| jan16 | 78.3% (78.5%) | -38.4% (-38.8%) | 43.0% (43.7%) | 41.3 (41.9) | 98.4% | 1 (1) | 43 (77) | 0 | 7,278 (4,726) | 2,553 | 100.0 |
| jan31 | 45.9% (45.9%) | 14.2% (11.6%) | 3.0% (3.0%) | 102.8 (102.8) | 100.0% | 0 (6) | 0 (563) | 0 | 9,044 (5,803) | 3,241 | 55.0 |
| feb12 | 85.1% (85.8%) | 70.2% (76.8%) | 84.0% (84.8%) | 244.0 (246.2) | 99.1% | 0 (0) | 0 (0) | 0 | 6,826 (5,223) | 1,603 | 25.5 |
| jun19 | 50.0% (50.0%) | 74.4% (74.5%) | 49.2% (49.2%) | 90.0 (90.0) | 100.0% | 1 (1) | 28 (46) | 0 | 4,017 (3,025) | 991 | 36.2 |
| sep21 | 44.5% (45.2%) | -10.8% (-10.7%) | 44.1% (44.7%) | 93.5 (94.8) | 98.6% | 0 (0) | 0 (0) | 0 | 2,173 (838) | 1,335 | 49.5 |
| jul01 | 53.6% (53.6%) | 42.7% (43.1%) | 52.2% (52.2%) | 93.8 (93.8) | 100.1% | 0 (0) | 0 (0) | 0 | 4,144 (2,976) | 1,168 | 44.2 |
| aug28 | 43.7% (43.3%) | 41.0% (40.4%) | 42.5% (42.2%) | 86.3 (85.6) | 100.8% | 0 (7) | 0 (1,240) | 0 | 1,280 (739) | 541 | 13.9 |
| jul14 | 46.6% (47.1%) | -27.7% (-27.0%) | 44.3% (44.9%) | 80.3 (81.3) | 98.8% | 1 (1) | 41 (51) | 0 | 2,643 (1,964) | 678 | 41.9 |

### Per window, m = $10 (PASS)

| Window | Share of M0 ceiling (m=0) | Value share (m=0) | Gap share (m=0) | Cut MW (m=0) | Relief | Harmed (m=0) | Harm $ (m=0) | Settlement harm | Payout $ (m=0) | Extra payout $ | Max round price |
|---|---|---|---|---|---|---|---|---|---|---|---|
| jan12 | 95.4% (95.4%) | -17.5% (-17.9%) | 96.8% (96.8%) | 180.3 (180.3) | 100.0% | 11 (13) | 5,607 (7,902) | 0 | 15,080 (6,970) | 8,109 | 37.6 |
| nov28 | 49.2% (49.2%) | 6.6% (4.7%) | 0.0% (0.0%) | 115.2 (115.0) | 100.2% | 25 (53) | 2,942 (10,074) | 0 | 14,116 (5,915) | 8,201 | 50.7 |
| jan02 | 83.2% (83.8%) | 58.6% (58.5%) | 72.6% (73.6%) | 137.5 (139.3) | 98.7% | 0 (26) | 0 (831) | 0 | 12,100 (4,155) | 7,946 | 44.3 |
| jan16 | 80.0% (78.5%) | -38.5% (-38.8%) | 47.5% (43.7%) | 45.6 (41.9) | 108.8% | 0 (1) | 0 (77) | 0 | 12,631 (4,726) | 7,906 | 100.0 |
| jan31 | 45.9% (45.9%) | 15.2% (11.6%) | 3.0% (3.0%) | 102.8 (102.8) | 100.0% | 0 (6) | 0 (563) | 0 | 16,214 (5,803) | 10,411 | 57.3 |
| feb12 | 85.4% (85.8%) | 70.0% (76.8%) | 84.5% (84.8%) | 245.2 (246.2) | 99.6% | 0 (0) | 0 (0) | 0 | 10,899 (5,223) | 5,675 | 31.8 |
| jun19 | 50.0% (50.0%) | 74.2% (74.5%) | 49.2% (49.2%) | 90.0 (90.0) | 100.0% | 0 (1) | 0 (46) | 0 | 6,344 (3,025) | 3,318 | 43.4 |
| sep21 | 45.2% (45.2%) | -10.7% (-10.7%) | 44.7% (44.7%) | 94.8 (94.8) | 100.0% | 0 (0) | 0 (0) | 0 | 5,115 (838) | 4,277 | 49.5 |
| jul01 | 53.5% (53.6%) | 42.4% (43.1%) | 52.2% (52.2%) | 93.7 (93.8) | 99.9% | 0 (0) | 0 (0) | 0 | 6,773 (2,976) | 3,796 | 47.5 |
| aug28 | 43.3% (43.3%) | 40.5% (40.4%) | 42.1% (42.2%) | 85.5 (85.6) | 99.9% | 0 (7) | 0 (1,240) | 0 | 3,194 (739) | 2,455 | 33.9 |
| jul14 | 47.2% (47.1%) | -27.5% (-27.0%) | 44.9% (44.9%) | 81.4 (81.3) | 100.1% | 0 (1) | 0 (51) | 0 | 4,226 (1,964) | 2,261 | 48.8 |

### Fresh window, Sep 5-6 at $10: confirmation PASS
- **Gates:** settlement-caused harm $0; the cut from the first gate is 82.6 MW against 82.9 MW at m = 0 (99.6%).
- **Shares:** share of M0 ceiling 44.8% (m = 0: 45.0%); gap share 43.9% (44.0%).
- **Harm and payout:** 0 agents harmed beyond tolerance (m = 0: 2, $51). Operator payout is $4,850 against $2,477 (×2.0).
- **Prices:** the highest round price was 57.5 $/MWh; no rounds hit the cap.
- **Reported, not gated:** 9 agents end slightly below acting alone, all inside the $10 floor of the tolerance. The lowest is PVB_08 at −$8.17. The median agent is +$6.09.

### Disclosures for these results
- **Smoke test:** before the queue, one 2-round run at m = 3 on Jan 12-13 (`d58m_smoke_jan12_m3`) checked the margin path for crashes. It is not a test cell.
- **Queue stopped and restarted:**
  - At 22:22 UTC on Oct 29, Claude Code ended the queue's shell because system memory ran low. It was not a crash.
  - The run in progress (Sep 21-22 at $3) had no result. On restart it ran again from scratch on the same commit and clean tree.
  - From then on the queue ran as a detached process.
- **Sleep stall:** Feb 12-13 at $10 stalled for 2 h 5 min (08:41-10:46 UTC, Oct 30) after a power-button standby. The run resumed on its own. The simulation doesn't use wall-clock time.
- **A slow run:** Jun 19-20 at $10 took 90 min instead of about 44, with no gap over 5 min.
- **Owner-to-confirm defaults:** the six listed under "Run order" are still unconfirmed.
  - Three never triggered: the baseline matched, both margins got a PASS/FAIL verdict, and a margin passed.
  - The other three were in force as written: the verdict order, the display keep-awake flag, and logging of fully stopped shifts.
- **The comparison solve** behind "declined MWh" took 19-24% of round time in every run except Feb 12-13 at $10 (7%, because the stall counts as round time).

### What follows (owner, Oct 30; not part of the pre-registered test)
The $10 margin fixes a $174 horizon shortfall by raising operator payout ×2.5. The owner proposed an exchange-side guarantee instead (proposed D62, Leaning): the exchange covers the shortfall of a modeled m = 0 response against acting alone, paid quarterly with an annual true-up.

A first cost estimate on these 11 windows (report only, from the m = 0 results) puts it at $1.7k net, so about $44.0k in total with m = 0, against $106.7k at $10. D58 stays Leaning and open, and M2a's pass test is unchanged.
