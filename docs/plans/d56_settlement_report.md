# D56: arm C settlement reference and no-harm floor (offline re-settle)

**Pre-registration commit:** `fd9926a` (step 1: this file's pre-registration sections, `bench/d56_resettle.py`, `tests/test_d56_resettle.py`).
**Tool:** `python -m bench.d56_resettle` (offline re-settle of existing arm C traces; no simulation runs).
**Status:** pre-registration only. This file holds the scope, cells, definitions, rules and selection procedure. Results are added below them later, and the selection procedure section is not edited after the step 1 commit.

## Scope
- **The question:** which settlement rule fixes the settlement harm shown on Jan 16-17 at 100 MW (infeasible target)? There, D53's recorded-schedule refund harmed all 80 agents ($136.5k), and agents paid the operator $133.5k on net.
- **Not in scope:**
  - D56 is not expected to make arm C pass M2a in the feasible cells. Most feasible-cell harm is the DA vs. RT gap, which no refund rule touches. The gap is a separate open item, not part of D56.
  - The report gives, per window, how much of the harm left after the selected rule is gap.
- **Planning diagnostic** (`h2h_c_jan16_h100`, print-only, Oct 27):
  - The whole $133,465 shortfall comes from the battery. Base load and curtailment cancel exactly.
  - On the Jan 17 evening steps (Jan 18 UTC; 24 steps at a mean of 482 $/MWh), the recorded batteries discharge 107.5 MWh, while the run's batteries net-charge 25.8 MWh.
  - Over the slice, the recorded schedule drains 404 MWh of storage.
  - Against the recorded reference, the agents' own selfish plans would also have lost $141k. Against the selfish plan, the run gained $7.8k.
  - So the recorded schedule is not each agent's counterfactual, and high prices make that mismatch expensive. Target feasibility is not the cause.

## Cells
Every cell is an arm C run under `runs/` (git-ignored). Each has `exchange_h<MW>_rinfo/trace.jsonl`, `selfish/trace.jsonl` and `summary.json`. Feasibility comes from `runs/h2h/feasibility.json` (central cap LP).

| Set | Cells | Use |
|---|---|---|
| Selection (6) | `h2h_c_jan02_h100` (feasible), `h2h_c_jan02_h150` (infeasible), `h2h_c_jan12_h100` (feasible), `h2h_c_jan12_h150` (infeasible), `h2h_c_jan16_h100` (infeasible), `h2h_c_jan16_h150` (infeasible) | eligibility and ranking |
| Held out (8) | `h2h_c_feb12_h100`, `h2h_c_jun19_h100`, `h2h_c_jun19_h150`, `h2h_c_sep21_h100`, `h2h_c_jul01_h100`, `h2h_c_aug28_h100`, `h2h_c_jul14_h100`, `h2h_c_nov28_h100` (all feasible; `nov28` is the D57 start-of-slice artifact) | held-out check, computed only after the verdict commit |
| Sensitivity (4) | `h2h_c_jan02_h150_cap100`, `h2h_c_jan02_h150_cap250`, `h2h_c_jan12_h150_cap100`, `h2h_c_jan12_h150_cap250` | D54 price-cap sensitivity, and validation of the clip-at-100 approximation |

**Excluded:**
- `h2h_c_jan02_h100_rerun`, which is identical to `jan02_h100`;
- the `_r20` and `_r40` round-cap runs;
- smoke and timing.

**Already seen before this pre-registration** (from `runs/overnight_diag/` and the overnight notes), so the split is partly post hoc:
- rules 1 and 1s on all six selection cells;
- rules 1 and 1s on held-out `jun19_h100`, `jun19_h150` and `nov28_h100`;
- rule 1 harm and payout on every cell (run summaries).

No other rule has been computed on any cell.

## Definitions
**Notation:**
- Steps are 15 minutes, h = 0.25 h.
- Money per step is p [$/MWh] × kW × h / 1000.
- Site net is in kW, export positive.

**Settled price p_t:** at each binding call, the last `peak_price` broadcast for that call, over the call's window. A later binding call replaces an earlier one, as in `exchange/core.py` `_close_price_gate`.

**Priced steps:** p_t > 1e-9.

**Per agent i, per step t:**
- N: metered site net (exchange trace telemetry);
- R: recorded reference (the `reference_net` message: dataset site net);
- B: recorded battery (dataset `bess`);
- S: selfish-run site net (selfish trace telemetry);
- Q: rule 1p's reference (below);
- C_i = Σ_t p_t (−N_i,t) h/1000, the peak charge.

**Acting alone (section 8):** alone_i = selfish run's RT energy value − wear + its end-of-slice SOC × the mean DA price. This is `sim/run_m1.py`'s definition.

**Pre-settlement shortfall e_i:** alone_i − (run RT energy value − wear − imbalance + end SOC × mean DA). It splits into the `m2a_metrics` components:
- lost arbitrage net of credits (credits are 0 in arm C);
- DA vs. RT gap;
- extra curtailment;
- imbalance;
- end energy.

**Under a rule:** shortfall_i = e_i − net_i − topup_i.
- Harmed: shortfall_i > tol_i, where tol_i = max(1% × |alone_i|, $10).
- Net harm is Σ shortfall_i over harmed agents.

**Settlement-caused harm:** agents with shortfall_i − gap_i > tol_i (harm with the DA vs. RT gap component removed), plus their $ (shortfall − gap).

**Operator net payout:** Σ_i net_i + Σ_i topup_i, positive when the operator pays.

**MW cut:** selfish peak − run peak (from the summary).

**Breakeven avoided cost:**
- payout / MW cut, in $/MW cut;
- payout / relief MWh, in $/MWh. Relief MWh = Σ over priced steps of max(0, selfish participant load − run participant load) × h / 1000. This uses the selfish run, so it is sim-only.

**Zeroed margin:** an agent-step on which the rule makes the agent's settlement insensitive to its own metered load at a priced step.

## Rules
All are computed from the as-run settled price.

| # | Rule | net_i (paid to agent) | Zeroed margin |
|---|---|---|---|
| 1 | Recorded-schedule refund (D53 as run) | Σ_t p_t (N_i,t − R_i,t) h/1000 | none |
| 1s | Selfish-run refund (oracle bound; **not in selection**) | rule 1 with R → S | none |
| 1p | Exchange-computed selfish reference (deployable 1s; **replaces 1s in selection**) | rule 1 with R → Q | none |
| 2 | Rule 1 on feasible cells only | rule 1 if the cell is feasible, else 0 | every priced step of every agent in an infeasible cell |
| 3e | Charges returned in equal shares | −C_i + (1/n) Σ_j C_j | none (own margin × (1 − 1/n)) |
| 3c | Charges returned by registered MW | −C_i + w_i Σ_j C_j, with w_i = max_kw_i / Σ max_kw | none (own margin × (1 − w_i)) |
| 3d | Rule 1 made budget-balanced | s_i − w_i Σ_j s_j, with s = rule 1 net | none (own margin × (1 − w_i)) |
| 4f/b, 4c/b | Base b ∈ {1, 3d, 3c} plus a top-up from the operator | base net. Full (4f): topup_i = shortfall_i^b if shortfall_i^b > tol_i, else 0. Continuous (4c): topup_i = max(0, shortfall_i^b − tol_i) | every priced step of every topped-up agent |
| 5 | Per-agent floor: charge' = min(charge, refund) | max(0, rule 1 net_i) | every priced step of every agent whose rule 1 net is < 0 |
| 6 | Idle-battery reference | rule 1 with R → R − B (recorded site net with the battery idle) | none |

**Rule 1p's reference Q.** The exchange computes a selfish battery schedule from what it holds at each binding gate:
- **Power:** the registered `mandate.max_kw` (register messages in the trace; no grant changed any of them).
- **Energy limit:** `usable_kwh` as the metering service's `asset_limits` reports it (0.8 × capacity, `sim/run_m1.py`). It was not sent in arm C runs because `daily_energy` is off. The SOC window is 10-90% of usable / 0.8, the planner's convention.
- **SOC at slice start:** metered, from the dataset `soc` at step 0, as `physics.Fleet.S0`. It is also the soft end-of-horizon target, as agents use it.
- **Day-ahead prices:** those published by the gate's time (`sim.forecasts.PriceForecast.at`; unpublished days repeat the last published day).
- **Site net excluding the battery:** recorded pv − recorded curtailment − load. Extra curtailment is agent-controlled during the run, so the dataset's recorded values are used.
- **Not registered, so fixed constants** (set before any result, not tuned):
  - one-way efficiency 0.95 (the fleet median is 0.949, from metadata);
  - self-discharge 0;
  - no export limit.
- **Wear:** the market config's `wear_usd_mwh`.
- **Planner:** `agents/planner.solve` (the agents' own LP) with no awards, nominations, peak price or prox.
- **Not used:** agent forecasts and the selfish-run trace.
- For each binding gate, the plan runs from slice start, and Q takes the plan's site net on the steps that gate settles.
- **M3 gaming paths:** declared `max_kw`; `usable_kwh` from metered history; SOC at slice start; any planner constant that later becomes a registration field.

## Selection procedure (pre-registered)
Applied mechanically to the six selection cells, at the as-run settled price. The candidates are 1, 1p, 2, 3e, 3c, 3d, 4f/1, 4c/1, 4f/3d, 4c/3d, 4f/3c, 4c/3c, 5 and 6. 1s is reported as the oracle bound only.

1. **Eligibility.** A rule is eligible only if it passes all three:
   - **E1 (margin):** zero zeroed-margin agent-steps in every selection cell. The owner's strict gate: a rule that zeroes any margin can be adopted only after a run where agents re-plan knowing the rule.
   - **E2 (reference):** its reference, lump sum and top-up use nothing the agent sends during the run. By construction every candidate passes as computed here. 1p's registration inputs are listed as M3 gaming paths. Rule 4's top-up and 1s use the selfish run, which only the simulator has.
   - **E3 (net payment):** the operator's net payout is ≥ −$1 in every selection cell, judged per cell and not summed.
2. **Ranking.** Eligible rules are ranked on:
   - (a) agent-cells harmed beyond tolerance (section 8, gap included), summed over the six cells;
   - (b) then total $ net harm;
   - (c) then total operator net payout.

   Harm ranks first, so no rule wins on lower payout with higher harm.
3. **Winner.** The top-ranked eligible rule is the proposed D56 rule. If no rule is eligible, there is no winner, and the report says so.
4. **Held-out check.** Computed only after the verdict is committed. The winner passes if both hold:
   - it meets E1 to E3 in every held-out cell;
   - its settlement-caused harm $, summed over the held-out cells, is no higher than rule 1's.
5. **Robustness (report only; it does not change the verdict).**
   - the winner with each one of the six selection cells left out;
   - the winner at tolerance 0;
   - the winner at twice max(1%, $10).

## Checks (fail closed, per cell)
If any check fails, that cell stops, the failure is recorded here, and the work moves on. No tolerance is widened and no rule definition is changed to make a check pass.
- **Roster:** the per-agent tables cover the run header's roster exactly (CLAUDE.md rule 10).
- **Rebuild:**
  - rule 1 net equals each agent's `peak_settle` `net_usd` within $0.0001;
  - max(shortfall under rule 1, 0) equals the summary's `by_agent_usd` within $0.05;
  - components sum to e_i within $0.01;
  - summed over rule 1's harmed agents, the components match the summary's `components.harmed_agents` within $0.05.
- **Accounting:** for every rule, agent settlements plus top-ups equal the operator's net payout, computed independently from fleet aggregates, within $0.01.
- **Determinism:** two runs of the tool on `h2h_c_jan16_h100` give identical outputs apart from the run timestamp.
- **Integrity:** before the verdict is written, the "Selection procedure" section must have no diff since the step 1 commit.

## Also reported (not used for selection)
- **D54 clip:** every rule with the settled price clipped at 100 $/MWh, for cells run at 500. Behavior is held fixed. The approximation is validated against the real `_cap100` runs on Jan 2-3 and Jan 12-13 at 150 MW.
- **Price pumping (first-order, one round):** for each rule and agent, the gain from raising one priced step's price by one penultimate-round MW of phantom nomination, summed over steps where it gains. Δp_t = min(α × 1000 / K, cap − p_t), where α = the gate's last rho_next / n and K = h / 1000. This is linked to M3 price pumping.
- **Rule 1p vs. 1s:** the per-agent and per-cell difference.
- **Remaining harm per window under the winner,** and the share of it that is DA vs. RT gap.

## Proposed confirmation test (new runs, only after owner approval)
**Build first:**
- The winning rule goes in `exchange/` behind a config key.
- Its reference arrives as a metering message, as `reference_net` does now.
- It is code-enforced, with a test.

**Runs:** under D54's 100 $/MWh cap, `jan02_h100`, `jan12_h100` and `jan16_h100`. Add one fresh January window at 100 MW, chosen by the breadth rule (the next-highest recorded peak not yet used), as the fresh failure check. Runs go one at a time.

**Pass if all hold:**
1. Settlement-caused harm is 0 on Jan 16-17 and on the fresh window. In the feasible cells it is no higher than under rule 1.
2. The operator's net payout is ≥ 0 in every cell.
3. The in-run `peak_settle` equals this tool's figure on the same trace within $0.0001.
4. Rosters are complete.

M2a pass and gap harm are reported but are not pass criteria. A margin-zeroing rule (2, 4, 5) would first need agent code that plans knowing the rule; the test then also requires the peak to be within 1 MW of the behavior-held-fixed result.

---

## Verdict on the selection cells (Oct 27; written after the integrity check)
**How this was run:**
- **Integrity:** the "Selection procedure" section is byte-identical (CR stripped) to commit `fd9926a`: sha256 `b67384d1…` on both sides. The tool and tests were unchanged from `fd9926a` when the six selection cells and the first selection ran.
- **Cells:** all six selection cells passed every fail-closed check (roster, rebuild, components, accounting). Logs are in `runs/d56_<cell>.log` and outputs in `runs/d56/<run>.json`.
  - The worst rule 1 vs. `peak_settle` error was $3.1e-6. The worst rebuild error against the summary was $0.005.
- **Determinism:** two runs on `h2h_c_jan16_h100` are identical apart from `run_at_utc` (`runs/d56/determinism/`).
- **One code fix after the first selection (`bench/d56_resettle.py` `EPS_USD`):**
  - Under the continuous top-up (4c), an agent's shortfall lands exactly on its tolerance, which by definition is not harmed. In floating point it sometimes landed about 1e-12 above, so up to 22 agents were counted as harmed.
  - "Harmed" now means shortfall > tolerance + $0.000001, with a test.
  - Only the 4c rows changed. Winner, ranking and every robustness verdict are identical: `runs/d56/selection_before_eps_fix.json` vs. `runs/d56/selection.json`.
  - No fail-closed check tolerance was changed.

**Selection table** (six cells, as-run price; `runs/d56/selection.json`):

| Rule | E1 margin | E3 payout ≥ −$1 every cell | Eligible | Agent-cells harmed | Harm $ | Operator net payout $ (sum) |
|---|---|---|---|---|---|---|
| **1p** | yes | yes | **yes, rank 1** | **39** | **8,733** | 1,131,205 |
| 6 | yes | yes | yes, rank 2 | 59 | 10,510 | 880,191 |
| 3e | yes | yes | yes, rank 3 | 98 | 6,965,239 | 0 |
| 3c | yes | yes | yes, rank 4 | 185 | 7,013,593 | 0 |
| 3d | yes | yes | yes, rank 5 | 247 | 186,863 | 0 |
| 1 (D53 as run) | yes | **no** (Jan 16-17 h100: −$133,465) | no | 119 | 143,864 | 1,210,138 |
| 2 | **no** (all 80 agents on every infeasible cell) | yes | no | 220 | 38,542 | 18,697 |
| 4f/1 | **no** | yes | no | 0 | 0 | 1,354,002 |
| 4c/1 | **no** | **no** (Jan 16-17 h100: −$3,051) | no | 0 | 0 | 1,346,370 |
| 4f/3d, 4c/3d | **no** | yes | no | 0, 0 | 0, 0 | 186,863; 172,949 |
| 4f/3c, 4c/3c | **no** | yes | no | 0, 0 | 0, 0 | 7,013,593; 6,979,336 |
| 5 | **no** (all 80 agents on Jan 16-17 h100) | yes | no | 78 | 9,252 | 1,343,603 |

**Winner: rule 1p.** The settlement reference is a selfish battery schedule the exchange computes itself, from:
- registered power and metered usable energy;
- SOC at slice start;
- the day-ahead prices published by each binding gate;
- recorded site net without the battery.

**Robustness** (report only; it does not change the verdict):
- 1p still wins with any one selection cell left out, **except Jan 16-17 at 100 MW**. Without that cell, rule 1 wins: 39 agent-cells and $7,356, against 1p's 39 and $8,733. So 1p's advantage is the Jan 16-17 fix, and elsewhere it is slightly worse than rule 1 on Jan 12-13 at 100 MW ($7,902 vs. $6,521), with the same agents harmed.
- 1p also wins at tolerance 0 and at twice max(1%, $10).

**1p per selection cell:**

| Cell | Target | Harmed (section 8) | Settlement-caused harmed | Gap $ among harmed | Operator net payout | Per MW cut | Per relief MWh | Rule 1 for comparison: harmed / payout |
|---|---|---|---|---|---|---|---|---|
| Jan 2-3 h100 | feasible | 26, $831 | 0 | $965 | $4,155 | $29.8 | $5.1 | 26, $836 / $4,467 |
| Jan 2-3 h150 | infeasible | 0 | 0 | $0 | $531,693 | $4,051 | $391 | 0 / $476,791 |
| Jan 12-13 h100 | feasible | 13, $7,902 | 0 | $8,185 | $6,970 | $38.7 | $8.8 | 13, $6,521 / $14,231 |
| Jan 12-13 h150 | infeasible | 0 | 0 | $0 | $324,305 | $3,237 | $350 | 0 / $585,279 |
| **Jan 16-17 h100** | infeasible | **0** | **0** | $0 | **$6,855** | $129 | $9.3 | **80, $136,507 / −$133,465** |
| Jan 16-17 h150 | infeasible | 0 | 0 | $0 | $257,226 | $16,107 | $276 | 0 / $262,835 |

- **What is left after 1p is gap.** Settlement-caused harm (harm with the DA vs. RT gap removed) is 0 in every selection cell. In both feasible cells, the gap among harmed agents ($965 and $8,185) is larger than their total harm ($831 and $7,902). Remove the gap and no agent is beyond tolerance.
- **Components of 1p's harm, over harmed agents:**
  - Jan 2-3 h100: lost arbitrage $1,165, gap $965, settlement −$1,298.
  - Jan 12-13 h100: lost arbitrage $1,730, gap $8,185, settlement −$2,013.
- The gap is the separate open item; it is not part of D56.
- **Payout:** 1p does not lower payout on the infeasible h150 cells at 500 $/MWh ($257k to $532k). The infeasible Jan 16-17 h100 cell pays $6,855. Lowering payout is D54's job (price capped at avoided cost); see the clip results below. *[Corrected after review: the range first omitted Jan 16-17 h100.]*

**1p vs. the oracle 1s:**

| Cell | 1s harmed / payout | 1p − 1s, sum over agents | Largest single-agent difference |
|---|---|---|---|
| Jan 2-3 h100 | 26, $836 / $4,143 | +$12 | $6.1 |
| Jan 2-3 h150 | 0 / $532,644 | −$951 | $1,316.5 |
| Jan 12-13 h100 | 13, $7,870 / $7,191 | −$221 | $25.8 |
| Jan 12-13 h150 | 0 / $345,928 | −$21,623 | $2,701.5 |
| Jan 16-17 h100 | 0 / $7,812 | −$957 | $129.7 |
| Jan 16-17 h150 | 0 / $254,621 | +$2,605 | $1,819.0 |

- At low settled prices (the feasible cells) 1p is within $25.8 per agent of the oracle.
- At the 500 $/MWh cap, the planner's stand-in constants (one-way efficiency 0.95, no self-discharge, no export limit) and day-1's repeated prices cost up to $2.7k per agent. No agent was harmed either way.

**Price pumping** (first-order bound: largest single-agent gain from 1 MW of phantom penultimate-round nomination on every priced step where it gains):

| Rule | Jan 2-3 h100 | Jan 12-13 h100 | Jan 16-17 h100 | Infeasible h150 cells |
|---|---|---|---|---|
| 1p | $4.71 | $4.59 | $16.14 | $16.72 to $29.13 |
| 1 | $5.61 | $8.09 | $30.15 | $17.29 to $27.07 |
| 6 | $3.36 | $5.03 | $14.25 | $18.68 to $22.56 |
| 3d | $2.69 | $2.63 | $11.02 | $10.15 to $17.14 |
| 3c | $26.53 | $23.62 | $116.20 | $81.92 to $124.03 |

- 1p's exposure is no larger than rule 1's in the feasible selection cells. In the held-out cells it is slightly larger in four: Jun 19-20 h100 ($12.84 vs. $11.08), Jun 19-20 h150 ($8.81 vs. $7.96), Sep 21-22 ($2.90 vs. $2.47) and Jul 14-15 ($3.39 vs. $3.30). Every price-based rule leaves some exposure: the M3 price-pumping attack. *[Held-out comparison added after review.]*

**1p's gaming paths (M3 items):**
- declared `mandate.max_kw` (bounded by D38's demonstrated grant once that is built);
- `usable_kwh` from metered history (demonstrated-limit farming before the run);
- SOC at slice start (arriving full to inflate the reference's discharge at peak steps);
- any of the planner's stand-in constants (efficiency, self-discharge, export limit) that later becomes a registration field.

Nothing the agent sends during the run enters the reference.

**Next, in the pre-registered order:** the held-out cells (8) and the held-out check, then the sensitivity cells and the D54 clip. These are added below, and are not part of the verdict commit.

---

## Held-out cells (computed after the verdict commit `64afdd3`; not committed)
All eight held-out cells passed every fail-closed check.

**Held-out check (pre-registered): PASS** (`runs/d56/heldout.json`). Both rules have $0 settlement-caused harm here, so the harm leg holds trivially and the pass rests on E1 and E3.
- 1p meets E1 (no zeroed margin), E2 and E3 (payout ≥ −$1) in every held-out cell.
- Its settlement-caused harm summed over the held-out cells is $0, the same as rule 1's ($0), so it is not higher.
- Already seen before pre-registration: rules 1 and 1s on `jun19` (h100, h150) and `nov28`.

| Cell | 1p harmed | Settlement-caused | Gap $ among harmed | Operator net payout | Per MW cut | Per relief MWh | Rule 1 harmed / payout | 1s harmed | 1p − 1s (sum; largest agent) |
|---|---|---|---|---|---|---|---|---|---|
| Feb 12-13 h100 | 0 | 0 | $0 | $5,223 | $21.2 | $8.6 | 0 / $6,487 | 0 | −$311; $54.9 |
| Jun 19-20 h100 | 1, $46 | 0 | $52 | $3,025 | $33.6 | $12.5 | 2, $61 / $2,533 | 4, $152 | +$686; $107.6 |
| Jun 19-20 h150 | 3, $160 | 0 | $196 | $5,817 | $41.6 | $11.8 | 4, $217 / $5,319 | 4, $222 | +$726; $114.4 |
| Sep 21-22 h100 | 0 | 0 | $0 | $838 | $8.8 | $3.2 | 0 / $746 | 0 | +$81; $20.3 |
| Jul 1-2 h100 | 0 | 0 | $0 | $2,976 | $31.7 | $9.7 | 0 / $2,695 | 0 | +$365; $112.4 |
| Aug 28-29 h100 | 7, $1,240 | 0 | $1,294 | $739 | $8.6 | $4.5 | 7, $1,269 / $665 | 7, $1,281 | +$101; $19.1 |
| Jul 14-15 h100 | 1, $51 | 0 | $83 | $1,964 | $24.2 | $10.4 | 1, $56 / $1,845 | 1, $72 | +$287; $85.6 |
| Nov 28-29 h100 (D57 artifact) | 53, $10,074 | 0 | $10,440 | $5,915 | n/a (no cut) | $6.9 | 46, $8,498 / $7,122 | 52, $9,733 | −$25; $3.3 |

**Remaining harm after 1p, per window, and the share that is gap** (your scope numbers):
- In every window, the gap among harmed agents is at least as large as their total harm.
- With the gap removed, no agent in any of the 14 primary cells is beyond tolerance.

| Window | Harm left after 1p | Gap among those agents | Settlement-caused harm |
|---|---|---|---|
| Jan 2-3 (h100) | 26 agents, $831 | $965 (116%) | 0 |
| Jan 12-13 (h100) | 13 agents, $7,902 | $8,185 (104%) | 0 |
| Jan 16-17 (h100, h150), Jan 2-3 h150, Jan 12-13 h150 | none | | 0 |
| Feb 12-13, Sep 21-22, Jul 1-2 | none | | 0 |
| Jun 19-20 (h100 / h150) | 1, $46 / 3, $160 | 114% / 123% | 0 |
| Aug 28-29 | 7, $1,240 | $1,294 (104%) | 0 |
| Jul 14-15 | 1, $51 | $83 (164%) | 0 |
| Nov 28-29 (D57) | 53, $10,074 | $10,440 (104%) | 0 |

A gap share above 100% means the agent's other components net out in its favour (its settlement more than covers its lost arbitrage), so the gap alone puts it over tolerance.

## D54 price-cap sensitivity (report only)
**Clip at 100 $/MWh, behavior held fixed, validated against the real cap100 runs** (Jan 2-3 and Jan 12-13 at 150 MW):

| Cell | Rule | Clipped 500 run: payout | Real cap100 run: payout | Error | Harmed (both) |
|---|---|---|---|---|---|
| Jan 2-3 h150 | 1 | $96,638 | $94,215 | +2.6% | 0 |
| Jan 2-3 h150 | 1p | $111,969 | $103,415 | +8.3% | 0 |
| Jan 2-3 h150 | 6 | $54,503 | $54,157 | +0.6% | 0 |
| Jan 12-13 h150 | 1 | $117,628 | $115,553 | +1.8% | 0 |
| Jan 12-13 h150 | 1p | $66,370 | $63,404 | +4.7% | 0 |
| Jan 12-13 h150 | 6 | $62,968 | $62,085 | +1.4% | 0 |

- The clip overstates payout by 0.6% to 8.3% on these cells. Both agree that no one is harmed.
- With the clip, 1p's payout per MW cut on the infeasible h150 cells falls from $3,237 to $16,107 (as run at 500) to $662 to $3,279. On Jan 16-17 h100 it falls from $129 to $60.

**Jan 16-17 at 100 MW with the clip: a risk for the confirmation test.**
- Rule 1 goes from 80 harmed to 66 harmed ($25,424), still all settlement.
- 1p goes from 0 harmed to **7 harmed ($515), with 10 settlement-caused ($798)**. Even the oracle 1s shows 4.
- **Cause:** behavior is held fixed. Agents moved energy against prices up to 500 $/MWh (lost arbitrage $736 among 1p's harmed agents), and here they are re-settled at 100.
- A real cap100 run would have agents re-plan at the lower price, so the clip can't say whether 1p passes there. No cap100 run of this cell exists. The confirmation test runs it and decides.

**Real cap100 and cap250 runs (as run):** 0 harmed under rules 1, 1p and 6 in all four cells.

| Cell | 1p payout | Rule 1 payout |
|---|---|---|
| Jan 2-3 h150, cap 100 | $103,415 | $94,215 |
| Jan 2-3 h150, cap 250 | $262,931 | $238,073 |
| Jan 12-13 h150, cap 100 | $63,404 | $115,553 |
| Jan 12-13 h150, cap 250 | $161,207 | $291,769 |

## What depends on Q-N1 and on the harm baseline
**Q-N1 (avoided cost):**
- D54's cap sets the infeasible-cell payout under any rule. Under 1p, the infeasible h150 cells pay $257k to $532k at 500 $/MWh and $63k to $103k at 100 (real cap100 runs). The infeasible Jan 16-17 h100 cell pays $6,855 at 500.
- **Breakeven per window (1p, as run): the Q-N1 question for Engie.**
  - Feasible cells: $8.6 to $41.6 per MW cut, and $3.2 to $12.5 per relief MWh. Nov 28-29 has no cut.
  - Infeasible h150 cells at the 500 cap: $3,237 to $16,107 per MW cut, and $276 to $391 per relief MWh. Jan 16-17 h100 (infeasible, 500 cap): $129 per MW cut, $9.3 per relief MWh.
  - Relief MWh is measured against the selfish run, so it is sim-only.
- The ranking itself doesn't depend on Q-N1, because harm ranks first.

**Harm baseline:**
- Section 8 measures against the selfish run, settled at RT prices, plus end SOC at the mean DA price.
- Under that baseline, 1p leaves only gap-driven harm. Measured ex ante (gap removed), no agent in any primary cell is beyond tolerance under 1p, at the as-run price.
- 1s and every rule 4 variant use the selfish run itself (sim-only). 1p does not.
- D3 as specified is a scalar reference peak, so it can't serve as a schedule reference. The recorded schedule was its stand-in, and the planning diagnostic shows it is not the agents' counterfactual.

## Proposed D56 entry (for the owner; PLAN.md not edited)
> | D56 | Arm C settlement reference (revises D53's settlement clause): the peak price is charged on metered site net and refunded at the same price on a **selfish battery schedule the exchange computes itself** at each binding gate. Inputs: registered power (`mandate.max_kw`), metered usable energy (`asset_limits.usable_kwh`), SOC at slice start, the day-ahead prices published by that gate, and recorded site net without the battery (recorded curtailment, since curtailment is agent-controlled). Unregistered planner constants: one-way efficiency 0.95, no self-discharge, no export limit. Nothing the agent sends during the run enters it. Rule names (2, 4, 5) refer to the D56 offline report. Floors, top-ups and no-settlement on infeasible targets are not adopted: each zeroes some agents' marginal price. They are eligible only after a run where agents re-plan knowing the rule (owner's strict margin gate). Selection rule: E1 no zeroed margin; E3 operator net payout ≥ −$1 in every cell; then rank on agent-cells harmed (section 8), $ harm, payout. | Leaning, pending the confirmation test | Offline re-settle of 14 arm C cells (`docs/plans/d56_settlement_report.md`; pre-registered at fd9926a, verdict at 64afdd3, chosen on 6 cells). On Jan 16-17 at 100 MW: 0 agents harmed (recorded-schedule refund: 80 agents, $136.5k, and agents paid the operator $133.5k). Settlement-caused harm is 0 in all 14 cells; what harm is left is DA vs. RT gap. It passed the 8-cell held-out check. 1p is within $26 per agent of the selfish-run oracle in the feasible selection cells, and within $115 across all 10 feasible cells (up to $2.7k at the 500 cap). The recorded-schedule refund fails E3 on Jan 16-17. The recorded schedule is not each agent's counterfactual: it drains 404 MWh over the slice and discharges into the 482 $/MWh evening steps | Oct 27 |

**Also proposed, as separate items:**
- **A new open item for the DA vs. RT gap** (not D56). In every primary cell, what harm is left after 1p is gap-driven: agents plan on day-ahead prices and settle at real-time ones. Options to plan later:
  - a risk premium in agents' response;
  - an ex ante harm measure (a section 8 change);
  - a top-up, which needs the rerun path.
- **M3 additions (1p gaming paths):**
  - declared `max_kw`;
  - demonstrated `usable_kwh` farming;
  - SOC at slice start;
  - any 1p planner constant that becomes a registration field.
  - Price pumping applies to every price-based rule. 1p's first-order bound is $2.69 to $29.13 per agent per MW of phantom nomination, about the same as rule 1's.

## Confirmation test (pre-registered above; not run)
- **Unchanged:** build 1p in `exchange/` behind a config key, with its reference sent by the metering service.
- **Runs:** under D54's cap of 100, `jan02_h100`, `jan12_h100`, `jan16_h100`, and one fresh January window at 100 MW (breadth rule). Only after owner approval.
- **Known risk (from the clip above):** on Jan 16-17 under the 100 cap, criterion 1 depends on agents re-planning at the lower price.

## Review (reviewer agent, Oct 27) and the data-level 1p check
- **Stage 1: pass.** The code matches the pre-registered definitions, rules and selection procedure, and every reported number matches `runs/d56`.
- **Independent cross-checks run by the reviewer (all 18 cells):**
  - summed components and totals over all agents match each summary's `components.all_agents` within $0.005;
  - priced-step counts, max and mean settled price match `price_mode.settled`;
  - rule 1 matches `peak_settle` within $9e-6.
- **Three Low findings, all addressed in the text:**
  - the infeasible-cell payout ranges omitted Jan 16-17 h100 (corrected above);
  - the oracle-gap and pumping statements were selection-only (held-out figures added);
  - **the "accounting" check (and the component-sum check) are implementation-consistency checks, not independent evidence.** For every rule, the operator side is the same bilinear sum as the agents' side, in a different order, so a wrong reference or price would pass both. The checks section above keeps its pre-registered wording. Read it as consistency only.
- **Real data anchors:**
  - rule 1 against the exchange's `peak_settle` and the run summaries;
  - **for 1p, a data-level check run after review** (read-only, scratchpad `check_1p.py`): in every gate plan of all 18 cells, the reference battery stays within `max_kw`, and its implied SOC path stays within 10-90% of usable / 0.8. The worst excursion is −0.000 kWh, which is float noise.

---

## Confirmation test: owner approval and the fresh window (Oct 27, before any run)
**Owner decisions (Oct 27):**
- D56 stays Leaning with rule 1p, and becomes Decided only if this test passes. New D58 (Leaning): the DA vs. RT gap. Both are in PLAN.md (commit `5920603`).
- The test is approved as pre-registered above, plus the fresh window.
- Build 1p into `exchange/` behind a config key, with the reference delivered as a metering message, code-enforced with a test.
- Runs at D54's 100 $/MWh cap, one at a time, in this order: `jan02_h100`, `jan12_h100`, `jan16_h100`, then the fresh window. Nothing else heavy runs alongside.
- If a run fails, the queue still finishes and the failure is reported. Neither 1p nor any pass criterion changes.
- The fresh window is reported separately from the three tuning cells.

**Fresh window: Jan 31 to Feb 1, 2023** (peak day Jan 31, recorded max 2,301.1 MW).
- **Rule:** the overnight breadth rule as written (`docs/overnight/2026-10-26.md`, "Window rule"), restricted to January for this test:
  - rank local Central days by maximum recorded system net load (system load minus the agents' PV − curtailment + battery, as M0 defines it);
  - a window is the peak day and the following day;
  - both days lie outside Jan 1 to 14, and the day before must exist;
  - the window starts at least 7 days from every window already taken (the eight breadth windows).
- **Check:** the script reproduces M0's recorded peaks (Jan 2: 2,745.5 MW; Jan 16: 2,666.0 MW; Jan 12-13 window: 2,441.2 MW).
- **January ranking:** every day above Jan 31 is excluded.
  - Jan 2, 6, 3, 7, 4, 5, 13 and 12 are inside Jan 1 to 14.
  - Jan 16, 18, 17, 14 and 15 are within 7 days of the Jan 16-17 window.
  - Jan 31 (2,301.1 MW) is the highest remaining day. Next by the same rule are Jan 30 (2,279.4 MW) and Jan 25 (2,241.5 MW).
- **Record:** `runs/d56/fresh_window.json` (git-ignored), computed from recorded data only.
- **Feasibility at 100 MW:** not yet known. It is computed with the central cap LP before the run and reported with the result.
- Jan 31 is the lowest-peak January window used so far, so its target may well be feasible. It was picked by the rule and is not replaced if so.

## Confirmation test: the build (Oct 27, before any run)
**In the run:**
- **Config key:** `settlement_reference`. The default `recorded` is D53 as run. `selfish_1p` turns on rule 1p.
- **Metering service** (`sim/run_m1.py`):
  - just before each binding call's first price round, it computes each asset's selfish site net with `sim/reference.py` `selfish_site_net`;
  - it sends that as a new metering message, `selfish_reference` (`asset_id`, `call_id`, `window`, `net_kw`);
  - inputs: the agent's registered `max_kw`, usable energy 0.8 × capacity, SOC at slice start, the day-ahead prices published at that tick, recorded pv − curtailment − load, and the config's wear;
  - the recorded `reference_net` is still sent, so rule 1 stays computable on the same trace.
- **Exchange** (`exchange/core.py`), all code-enforced with tests in `tests/test_price_mode.py`:
  - only the metering service may send `selfish_reference`, and only in `selfish_1p` mode;
  - it must name a binding call and cover that call's window, with finite values;
  - it must arrive before the call's deadline (`after_gate_closure` otherwise);
  - it is never revised;
  - `peak_settle` refunds each priced step on the reference of the binding call that settled it;
  - a missing reference at a priced step raises, so settlement fails closed;
  - the simulator stops the run if any `selfish_reference` is rejected.

**Offline tool changes** (`bench/d56_resettle.py`). These are check selection, not widened tolerances:
- `ref_1p` now calls the shared `sim/reference.py` function, the same one the run uses.
  - **Proof it changed nothing:** I reran the six selection cells into `runs/d56/refactor_check/`. Every output body is identical to `runs/d56/<cell>.json`; only the header commit, timestamp and tool hash differ.
- **The rebuild checks use the rule the run settled under,** read from the trace header (`exchange.settlement_reference`):
  - for 1p runs, `peak_settle` is checked against rule 1p within $0.0001 (criterion 3);
  - the summary's harm is checked against rule 1p within $0.05.
  - Recorded-reference runs are checked against rule 1 as before.
- **New report-only check:** the largest difference between the in-run `selfish_reference` and the tool's own Q at settled steps.
- **Feasibility for the new cells** comes from `runs/d56/feasibility_confirm.json`, written by `python -m bench.h2h_feasibility --out …` (new `--out` option).
- **New command, `confirm`,** reports the pre-registered criteria per cell, with the tuning cells and the fresh window kept apart. Criterion 1 for Jan 2-3 and Jan 12-13 compares settlement-caused harm $ with rule 1 on the same trace, per cell (the held-out check summed over cells; the pre-registered wording here is per cell, which is stricter).

**Configs** (`configs/h2h/`):
- `h2h_c_{jan02,jan12,jan16}_h100_1p` are copies of the arm C 100 MW configs with only these changed: run name, `peak_price_cap_usd_mwh: 100` and `settlement_reference: selfish_1p`.
- `h2h_c_jan31_h100_1p` is a copy of the Jan 16-17 config with the dates and M0 folder changed as well.
- `m0_jan31` is new.
- The imbalance price stays 500, as in the cap100 sensitivity runs.

**End-to-end check before the queue** (smoke only, not a result; `runs/d56/smoke/`):
- 4 agents on Jan 16-17, 3 rounds per gate, 1p settlement.
- In-run `peak_settle` vs. the tool: $1.4e-13.
- In-run reference vs. the tool's Q: 0.0 kW.
- Summary harm rebuild: $0.00.

**Tests:** full suite passed, 311 tests.

**Review of the build (reviewer agent, before the queue): clean.**
- The in-run reference and the tool's Q use the same binding call, price version and inputs.
- In all four windows, both binding calls get price rounds (round steps 15 and 59), so no settled step relies on a stale price.
- **Notes:**
  - criterion 1 is applied per cell (wording corrected above);
  - `confirm` reads existing `runs/d56/<run>.json` files (these cells are new);
  - the simulator's stop-on-reject guard has no test.
- **Feasibility at 100 MW** (`runs/d56/feasibility_confirm.json`): Jan 2-3, Jan 12-13 and Jan 31 to Feb 1 are feasible; Jan 16-17 is infeasible. These match the earlier verdicts for the three tuning windows.
- **M0 for Jan 31 to Feb 1:** recorded peak 2,301.1 MW, 15-min ceiling 1,961.3 MW.

## Criterion 1's rule 1 comparison, and a conditional cell (owner, Oct 28; committed while the queue was on its first run)
**Criterion 1, rule 1 side:** in Jan 2-3, Jan 12-13 and the fresh window, rule 1's settlement-caused harm is computed by re-settling each new cap-100 trace offline under rule 1. Those traces are the ones settled in-run on 1p. The earlier cap-500 runs are not used. This is what `confirm_check` already does: `cell_metrics(cell, "1")` on the same cell's re-settled rule nets. Rule 1 is therefore behavior-held-fixed on 1p behavior.

**Conditional cell: `h2h_c_jan31_h150_1p`** (config committed here; a copy of `h2h_c_jan31_h100_1p` with only the run name and headroom changed).
- **Disclosure:** this cell was added after the 100 MW Jan 31 cell was found feasible, and before any 1p result on Jan 31 existed. At commit time the queue was still on its first run, Jan 2-3.
- **Rule:**
  1. After the queue finishes, run the central cap LP (`bench.h2h_feasibility`) for Jan 31 to Feb 1 at 150 MW.
  2. **If it is feasible:** don't run the cell, and record why here.
  3. **If it is infeasible:** run arm C with 1p at the 100 $/MWh cap as a fresh infeasible-target check.
- **Pass criterion: settlement-caused harm is zero.** Payout, the `peak_settle` match and the roster are reported as well.
- It is reported separately from the tuning cells and from the 100 MW fresh window.
- **Mechanics:**
  - the feasibility LP is rerun for all five `_1p` configs into `runs/d56/feasibility_confirm.json`, so the existing four verdicts are recomputed alongside it;
  - the cell is evaluated with `bench.d56_resettle cell`, and the zero-harm check uses the same `confirm_check` with this cell as a zero-required cell;
  - `bench/d56_resettle.py` is not edited while the queue runs.

## Confirmation test: results (Oct 28)
**Verdict: PASS on all four cells. The tuning cells and the fresh window pass separately** (`runs/d56/confirm.json`: `tuning.pass`, `fresh.pass`, `pass` all true). Neither 1p nor any criterion changed. Whether D56 becomes Decided is the owner's call; PLAN.md is not edited here.

**How the queue ran:**
- One process at a time, in the approved order, at the 100 $/MWh peak price cap with `settlement_reference: selfish_1p`. Every run and every tool step exited 0 (`runs/d56/confirm_queue.log`).
- Trace header commits: `79b32a8` for Jan 2-3, `39b1723` for the rest. The two differ only in docs and the new 150 MW config, with no change under `exchange/`, `sim/`, `agents/` or `bench/`.
- Claude Code's low-memory guard stopped the two background task wrappers while the fresh-window run was going. The scripts underneath kept running and finished; nothing was restarted.

**All four cells:**
- the in-run reference equals the tool's 1p Q exactly (0.0 kW);
- the summary harm rebuild is within $0.005;
- components sum within $1e-10;
- rosters are complete (80 of 80).

### Tuning cells
| Cell | Feasible | 1p settlement-caused harm | Rule 1, same trace | 1p operator payout (per MW cut; per relief MWh) | Rule 1 payout | `peak_settle` vs. tool | Pass |
|---|---|---|---|---|---|---|---|
| Jan 2-3 h100 | yes | 0, $0 | 0, $0 | $4,155 ($29.8; $5.14) | $4,467 | $2.4e-6 | yes |
| Jan 12-13 h100 | yes | 0, $0 | 0, $0 | $6,970 ($38.7; $8.79) | $14,231 | $4.3e-6 | yes |
| Jan 16-17 h100 (zero required) | no | **0, $0** | 65 agents, $24,396 | $4,726 ($112.8; $6.38) | **−$21,700** | $2.3e-6 | yes |

### Fresh window
| Cell | Feasible | 1p settlement-caused harm | Rule 1, same trace | 1p operator payout (per relief MWh) | Rule 1 payout | `peak_settle` vs. tool | Pass |
|---|---|---|---|---|---|---|---|
| Jan 31 to Feb 1 h100 (zero required) | yes | **0, $0** | 2 agents, $38 | $5,803 ($5.98) | $6,810 | $3.2e-6 | yes |

**Rule 1 numbers:** every rule 1 figure comes from re-settling the same cap-100 trace offline, so it is behavior-held-fixed on 1p behavior.
- **On Jan 16-17,** the recorded-schedule refund would have harmed 65 agents through settlement, and agents would have paid the operator on net, so rule 1 fails criteria 1 and 2 on this trace.
- **On the fresh window,** rule 1 harms 2 agents through settlement, and 1p harms none.

**Jan 31 peak, and its per-MW figure:**
- **The summary's peak is not a result of the price.** It reports a run peak of 2,145.0 MW against 2,150.7 MW for the selfish run, a cut of only 5.7 MW. That peak falls at 06:45 UTC, step 3, before any gate. The run and the selfish plan are identical there (the D57 pattern, as on Nov 28-29).
- **From step 4 on:**
  - the run peaks at 2,047.9 MW, against the Jan 31 target of 2,047.5 MW;
  - the selfish run peaks at 2,150.7 MW;
  - a cut of 102.8 MW.
- **So the per-MW-cut payout from the tool, $1,023, is an artifact and is left out of the table.** The per-relief-MWh figure ($5.98) is unaffected.

**Harm left under 1p, and the share that is gap** (section 8, all four cells):

| Cell | Harm left | Gap among those agents | Settlement-caused |
|---|---|---|---|
| Jan 2-3 h100 | 26 agents, $831 | $965 (116%) | 0 |
| Jan 12-13 h100 | 13 agents, $7,902 | $8,185 (104%) | 0 |
| Jan 16-17 h100 | 1 agent, $77 | $110 (142%) | 0 |
| Jan 31 to Feb 1 h100 | 6 agents, $563 | $577 (102%) | 0 |

- **All remaining harm is gap (D58).** M2a's no-net-harm check therefore still fails in all four cells; that is outside D56's scope.
- **Jan 12-13 (caveat (b) of the D56 entry):** 1p again shows more section 8 harm than rule 1 on the same trace, $7,902 vs. $6,521, with the same 13 agents. Settlement-caused harm is $0 under both rules.
- **Jan 16-17:** 12 steps outside the need windows exceed the target beyond the aggregate band, by at most 73.9 MW. That cell's target is infeasible (LP max short 99 MW).

### Conditional cell (Jan 31 to Feb 1 at 150 MW)
**Central cap LP** (after the queue finished, 05:11 UTC): **infeasible**, max short 119.3 MW, slack 1,218,340 kW-steps (`runs/d56/feasibility_confirm.json`).
- The four other verdicts reproduced unchanged.
- By the committed rule, `h2h_c_jan31_h150_1p` was started at 05:11:50 UTC.
- **Its result is not in yet.** It will be added here with its pass criterion (settlement-caused harm zero), and reported separately.

## D58 horizon test (pre-registered Oct 28, before computing it in this form)
**Owner decision (Oct 28):**
- Harm that comes only from imperfect forecasting is acceptable. A perfect forecast is a yardstick, not a target.
- What matters:
  1. people outside Pearl Street are not made worse off;
  2. agents who opt in are not worse off than acting alone.
- This adopts a D58 metric rule (Leaning). PLAN.md is not edited until the owner has seen the results.
- **This is a pass-rule change made after a failure.** M2a's per-window no-net-harm check fails in every arm C cell, and only because of the DA vs. RT gap.

**Baseline unchanged:** each agent against acting alone (section 8), settled on rule 1p.

**The test** (`bench/d58_horizon.py`; tests in `tests/test_d58_horizon.py`):
- **Horizon gate, per agent:** the agent's result with Pearl Street minus acting alone, summed over the 11 windows below, is at least −max(1% × |summed acting-alone result|, $10). That is one $10 floor over the horizon, not $10 per window.
- **Per-window gate:** 1p settlement-caused harm is 0 in every window. It is the same quantity as D56's criterion 1 (`cell_metrics`), so only the gap can even out across windows.
- **The test passes only if both gates hold for every agent and every window.**
- **Roster:** taken from each cell's header. It must be identical in all 11 cells and complete, or the test fails closed.

**Cells: one per window, all at 100 MW, 11 in all.**
- **Settled in-run on 1p at the 100 $/MWh cap:**
  - `h2h_c_jan02_h100_1p`, `h2h_c_jan12_h100_1p`, `h2h_c_jan16_h100_1p` and `h2h_c_jan31_h100_1p`;
  - each must show settled rule 1p and cap 100, or the test fails closed.
- **Re-settled offline under 1p** (run at the 500 cap with the recorded reference):
  - `h2h_c_feb12_h100`, `h2h_c_jun19_h100`, `h2h_c_sep21_h100`, `h2h_c_jul01_h100`, `h2h_c_aug28_h100`, `h2h_c_jul14_h100` and `h2h_c_nov28_h100`.
- **Cap-100 equivalence check for the re-settled cells** (fail closed): the highest price in any round of any gate (summary `price_mode.max_peak_price_usd_mwh`) must be at most 100 $/MWh. Then the cap never bound, and since the settlement reference does not change what agents do, behavior is what a cap-100 run gives. Jan 2-3 and Jan 12-13 support this: their cap-500 and cap-100 1p runs give identical figures.
- **Not used:**
  - the cap-500 `h2h_c_jan16_h100`, whose price reached the cap;
  - the 150 MW cells, which repeat windows and, at the 500 cap, give agents $250-515k each. They are reported separately, as is the pending `h2h_c_jan31_h150_1p`.
- **Nov 28-29** (D57 start-of-slice artifact) is in the pass set. The result without it is reported as a sensitivity.

**Reported, not gated:**
- each agent's worst single window, in $ and which window (risk exposure);
- gap sign per window (cost or gain, totals) and per agent (windows with a gap cost or a gain), to see whether the gap is mean-zero or one-signed;
- a breakdown by agent type (ID prefix);
- **bystander items, per window:**
  - **(a) Operator payout and the value of the cut:** payout per relief MWh, and per MW cut both from the summary and from the first gate. The breakeven stands until Q-N1 is answered.
  - **(b) Peak:** the peak against the selfish run, from the summary and from the first gate (flagged when the summary peak is before the first gate), plus steps above target outside need windows, those beyond the band, and the largest excess.
  - **(c) Price impact:** not modeled, because prices are recorded.
  - These replace the hard-coded bystander $0 (`sim/run_m1.py`, `bill_increase_usd: 0.0`) in this report only. The run code is unchanged.

**Disclosures:**
- **Not fully blind.** Before this rule was written, a looser version was computed in the owner conversation: $10 per window summed, 10 windows with the cap-500 Jan 16-17 cell and without Jan 31. It gave:
  - 3 agents failing over 10 windows;
  - 0 failing without Nov 28;
  - 8 failing over the four January confirmation cells alone;
  - the window gap totals change sign from window to window;
  - PVB_39 and PVB_13 were the worst agents.
- **A sample, not a season.** 11 two-day windows, chosen for high peaks.
- **Nothing is adjusted after computing.** If a check fails, the failure is reported.

## D58 horizon test: results (Oct 28)
- **Integrity:** computed at commit `922d7fd` (tool sha256 `df8b48e8c172ac63`). The definition, tool and tests have no diff since that commit. Output: `runs/d58/horizon.json`.
- **Checks:** all fail-closed checks pass.
  - Rosters: 80 agents, identical in all 11 cells.
  - In-run cells: all settled on 1p at the 100 cap.
  - Highest round price in the re-settled cells: 22.6-51.1 $/MWh, under 100, so they count as cap-100 equivalent.

**Verdict: FAIL.** 2 of 80 agents end below acting alone by more than their horizon tolerance, $174 beyond tolerance in total. The per-window settlement gate holds in all 11 windows: 1p settlement-caused harm is 0 everywhere.

| Agent | Result vs. acting alone, 11 windows | Tolerance | Beyond | Worst window | Gap, 11 windows | Windows with gap cost / gain |
|---|---|---|---|---|---|---|
| PVB_39 | −$865 | $718 | $148 | Jan 12-13: −$1,668 | +$1,028 | 5 / 6 |
| PVB_22 | −$130 | $103 | $26 | Jan 12-13: −$239 | +$174 | 6 / 5 |

- **Near the line but passing:** PVB_13 (−$280 against $302), DC_07 (−$229 against $3,069) and PVB_25 (−$141 against $719).
- **Across all agents:** 8 of 80 are below zero over the horizon, and the median agent is +$183.

**Sensitivity without Nov 28-29 (D57 artifact): PASS.** 0 agents fail. The worst agent, PVB_39, is −$401 against a tolerance of $672. 5 agents are below zero, and the median is +$373.

**Is the gap mean-zero or one-signed?**
- **Across windows it is not one-signed.** The fleet's gap was a cost in 5 windows (Jan 2, Jan 12, Jun 19, Jul 14, Nov 28) and a gain in 6. Summed over all 11, participants gained $19,650 from it (mean −$1,786 per window).
- **Within a window it is nearly one-signed,** because agents shift at the same times. For example, Nov 28 was a cost for 80 of 80 agents, Sep 21 a gain for 80 of 80.
- **Each agent's worst window:**
  - Nov 28: 53 agents;
  - Jan 12-13: 11;
  - Jan 2-3: 7;
  - Aug 28: 5;
  - Jun 19 and Jan 16: 2 each.

**By agent type:**

| Type | Agents | Failing | Summed result | Lowest agent | Summed gap |
|---|---|---|---|---|---|
| COM | 20 | 0 | +$1,735 | −$6 | −$1,226 (a gain) |
| DC | 10 | 0 | +$3,379 | −$229 | −$2,454 (a gain) |
| PVB | 50 | 2 | +$22,218 | −$865 | −$15,969 (a gain) |

- **This is not a bias against one type.** PVB sites as a group gained most, from the gap and overall. The failures are individual PVB sites whose losing windows outweigh their winning ones. PVB_39 has 5 cost windows and 6 gain windows, but the costs are larger.
- **A site-level look is the next diagnostic, not part of this test.** For example: does PVB_39's shift fall in hours where real-time prices beat day-ahead more often than chance?

**Bystander items** (report only; payout is the operator's net 1p payout, and its funding is Q-N1):

| Window | Operator payout | Per relief MWh | Per MW cut from first gate | Cut from first gate vs. selfish | Steps over target outside windows (beyond band; max excess) |
|---|---|---|---|---|---|
| Jan 2-3 | $4,155 | $5.14 | $29.8 | 139.3 MW | 3 (0; 1.8 MW) |
| Jan 12-13 | $6,970 | $8.79 | $38.7 | 180.3 MW | 28 (0; 1.0 MW) |
| Jan 16-17 (target infeasible) | $4,726 | $6.38 | $112.7 | 41.9 MW | 27 (**12**; **73.9 MW**) |
| Jan 31 to Feb 1 | $5,803 | $5.98 | $56.4 | 102.8 MW* | 54 (0; 0.5 MW) |
| Feb 12-13 | $5,223 | $8.58 | $21.2 | 246.2 MW | 29 (0; 0.2 MW) |
| Jun 19-20 | $3,025 | $12.52 | $33.6 | 90.0 MW | 7 (0; 0.1 MW) |
| Sep 21-22 | $838 | $3.21 | $8.8 | 94.8 MW | 8 (0; 0.1 MW) |
| Jul 1-2 | $2,976 | $9.73 | $31.7 | 93.8 MW | 17 (0; 7.0 MW) |
| Aug 28-29 | $739 | $4.52 | $8.6 | 85.6 MW | 5 (0; 0.7 MW) |
| Jul 14-15 | $1,964 | $10.42 | $24.2 | 81.3 MW | 14 (0; 8.7 MW) |
| Nov 28-29 | $5,915 | $6.86 | $51.4 | 115.0 MW* | 46 (0; 0.3 MW) |

\* The summary peak falls before the first gate, where the run and the selfish plan are identical (D57 pattern). The summary cuts are 5.7 MW for Jan 31 and 0 MW for Nov 28.

- **(a) Payout:** $42,334 in total over the 11 windows. Non-participants come out ahead only if the avoided cost is above $3-13 per relief MWh, or $9-113 per MW cut, per window. Q-N1 stands.
- **(b) Peak:**
  - From the first gate, every window's peak is 42-246 MW below the selfish run.
  - The only steps above target beyond the band are on Jan 16-17, whose target is infeasible (LP max short 99 MW).
- **(c) Price impact:** not modeled.
- **PLAN.md is not edited.** The owner decides next.

**How to read the sensitivity:** the result without Nov 28 is reported, not grounds to discount the verdict.
- D57 concerns where the summary peak falls. Nothing here shows that it causes Nov 28's harm.
- From the first gate, that window cut 115 MW, and the gap was a cost for all 80 agents. That is the after-the-fact loss this test is meant to catch.
- The pre-registered verdict stands: FAIL.

## Conditional cell result: Jan 31 to Feb 1 at 150 MW (Oct 28; reported separately)
**Pass criterion (committed at `39b1723`): settlement-caused harm is zero. PASS.**
- Rule 1p settlement-caused harm: 0 agents, $0.
- Total section 8 harm: 0 agents. The run's own M2a `no_net_harm` is true.
- **Also reported:**
  - **Payout:** operator net payout under 1p is $62,385, not negative ($47.2 per relief MWh).
  - **Settlement match:** in-run `peak_settle` equals the tool within $2.2e-6, and the in-run reference equals the tool's Q exactly (0.0 kW).
  - **Roster:** complete, 80 of 80.
- **Evaluation:** `bench.d56_resettle.confirm_check`, unchanged, with this cell as zero-required (scratch script; output `runs/d56/conditional_h150_eval.json`).

**How it ran:**
- **Feasibility:** the central cap LP found the target infeasible (max short 119.3 MW), so the cell ran as the rule requires.
- **Commits:** the trace header records `39b1723`. The summary header records `a2b2bce`, since the summary is written at the end. The two differ only in docs and bench files, with nothing under `exchange/`, `sim/`, `agents/` or `configs/`.
- **Wall time:** 12 hours. The first gate took 11.4 hours, likely because the machine slept.
- **The price was at the cap for most of the run:** 113 of 120 rounds were at the cap, and the settled price reached the 100 cap (mean 36.8 $/MWh over 87 priced steps).

**Rule 1 on the same trace, for comparison:** 0 settlement-caused harm, 1 agent harmed ($208, gap), payout $33,040.
- Unlike Jan 16-17, the recorded schedule did not go against agents here.
- So this cell supports 1p's zero-harm property on a second infeasible target. It does not separate 1p from rule 1.

**Peak and bystander items (report only):**
- **Peak:** the summary peak is 2,145.0 MW, at the pre-gate step 3, as at 100 MW. From the first gate, the run peaks at 1,998.4 MW against the selfish run's 2,150.7 MW, a 152.3 MW cut.
- **Payout per MW cut:** $410 from the first gate. The summary's $10,996 rests on the pre-gate 5.7 MW.
- **Target:** 71 steps outside need windows exceed it, 17 of them beyond the band, by at most 112.4 MW (Feb 1, 20:15 Central). The target is infeasible.

## D58 site-level diagnostic (Oct 28; report only, not a pass test)
- **Tool:** `bench/d58_sites.py`, committed at `a2b2bce` before it ran; computed at `d484f09`. Output: `runs/d58/sites.json`.
- **Checks:** the rebuilt gap per agent matches `runs/d56/<cell>.json` within $0.01 in all 11 cells, and the rosters are complete.
- **Agents:** the two failing agents and the three nearest the line.
- **Question:** are their gap losses about *when* they shift, or *how much* they shift against what they earn?

**Results:**

| Agent | Gap over the horizon | MWh shifted (rank of 80, typical) | Gap per MWh shifted | Margin with the gap removed |
|---|---|---|---|---|
| PVB_39 | +$1,028 | 450 (top 3-13) | +$2.28 | +$163 ($0.36/MWh) |
| PVB_22 | +$174 | 90 (50-64) | +$1.93 | +$45 ($0.50/MWh) |
| PVB_13 | +$367 | 148 | +$2.48 | +$86 |
| DC_07 | +$283 | 115 | +$2.46 | +$53 |
| PVB_25 | +$565 | 366 (top 4-26) | +$1.54 | +$424 |
| Fleet | −$19,650 | | −$1.35 | |

1. **One window does most of it.** On Jan 12-13 the fleet's gap was +$2.90 per shifted MWh, and the median agent's was −$2.04, a gain. Four of these five agents lost $27-33 per MWh that window.
   - For PVB_39 that one window cost $1,717, more than its whole horizon gap.
   - In the other windows these agents track the fleet in sign and roughly in size. The cosine between each agent's shift and the fleet's is 0.5-0.9.
   - So their timing is mostly the fleet's. On Jan 12-13 it differed in a way that ran into real-time prices well above day-ahead.
   - Without Jan 12-13, PVB_39 would be +$803 over the horizon (its Jan 12-13 result was −$1,668). That is a post hoc observation, not a reason to drop the window.
2. **The margin for forecast risk is tiny.** With the gap removed, these agents earn $0.36-0.50 per MWh shifted.
   - The gap swings by −$50 to +$33 per MWh from window to window.
   - Arm C agents re-plan until the peak price just covers their lost day-ahead arbitrage, so no margin is left to absorb forecast error.
   - A large shifter like PVB_39 carries the most real-time risk with almost no cushion. The tolerance (1% of acting alone) is the only buffer.
3. **This is not a bias against a site type.** PVB as a group gained $15,969 from the gap. These are individual sites, mostly those that shift a lot.

**For the owner, not built:** this points at D58's existing note that a lever would sit in the agent's re-plan, for example a risk premium. An agent would move energy only if the peak price beat its lost arbitrage by a margin sized to the gap's spread. The trade-off is less peak cut. Choosing and testing it is the owner's call, with a new pre-registered test.
