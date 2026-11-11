# D62 guarantee test

**Pre-registered Oct 30, 2026, before any modeled response P\* is computed on real data.** The commit that adds this file is the pre-registration commit. A commit can't hold its own hash, so a docs-only follow-up commit (the "hash-record commit") writes it here and in PLAN.md's change log. That commit changes only this header line and that change-log line. **Pre-registration commit (v1): `67ca965`.**

**Amended (v2), Oct 30, before any gate number was computed:** see "Amendment v2" below. The v2 commit fixes one checker function, adds its test, and changes this file. From v2 on, "the pre-registration commit" below means the v2 commit, and the in-run runs are redone on it. **Pre-registration commit (v2): `eea5a91`.**

**Integrity:**
- When each verdict is written, `git diff <pre-registration commit> -- agents/ sim/ exchange/ bench/d62_guarantee.py bench/d56_resettle.py bench/d58_margin.py bench/d58_horizon.py configs/d62/` must be empty, and so must the diff of this file's sections "Definitions", "Gates" and "Run order".
- Every in-run header must show the pre-registration commit, or the hash-record commit that follows it, without `-dirty`.
- `git diff <pre-registration commit> <hash-record commit>` must touch only this file's header and PLAN.md's change log.

**Owner decisions (Oct 30):**
- **Run scope:** offline from saved m = 0 traces, plus two in-run checks (Jan 12-13, Nov 28-29). If either check mismatches, stop: the offline numbers are not used, and the test goes all in-run. Sep 5-6 already has an m = 0 trace on 6156964, so it is computed offline.
- **Cost gate on gross guarantee payments**, before any clawback. Clawbacks are recovered only by netting, and an agent that exits owing may never pay.
- **Missed response minimum:** gains count, losses don't.
- **True-up over longer runs:** report only, and a named open item.
- **Spans:** each gate is valued only over its own span. P\* is one continuous path, carrying its own SOC.
- **Acting-alone comparator:** Q\*, one continuous path. 1p's Q_g stays the settlement reference, and the mismatch is reported.
- **Threshold:** the midpoint rule. Agent-quarters with no modeled shift (0/0, under 1 kWh) are eligible and left out of separation.
- **Sep 5-6 confirmation** includes coverage.
- **D64:** a structural check now, and the behavioural test with D64's build.
- **Stage-1 identity:** in the owner's exact form (below).
- **A PASS is report only.** D62, D58, D59 and PLAN.md's decisions do not change until the owner decides after seeing the results.

## Why
D58 is the open item behind M2a's no-net-harm failure. Under D56's rule 1p, settlement harms no one. The harm left is the gap between day-ahead (DA) and real-time (RT) prices.
- The D58 margin test (`docs/plans/d58_margin_report.md`) found that a uniform $10/MWh margin passes the horizon gate.
- But it raised operator payout from $42.3k to $106.7k over the 11 windows (×2.5), to fix a $174 shortfall on 2 agents.

D62 is the exchange-side alternative. The exchange pays the shortfall of a **modeled** m = 0 response P\* against a modeled acting-alone path Q\*.
- It pays quarterly, with an annual true-up and clawback by netting.
- It pays only to agents whose metered response shows they delivered the modeled shift (D65).
- It never pays on the agent's actual result, so no agent's marginal price is zeroed.

The first cost estimate ($1.7k net) used the fleet's actual m = 0 results in place of P\*. That stand-in is the assumption this test checks.

## What was seen before this was written
- **Every result of the D58 tests** on these windows: m = 0, $3 and $10, per agent and per window. That includes the $174 shortfall, the payouts and the per-window harm.
- **The stand-in cost estimate** (`runs/d58m/guarantee_cost.json`):
  - per window $25.1k, per quarter $19.5k, per year $1.7k (8 agents);
  - quarterly payout with annual true-up: $7.8k paid, $6.1k clawed back, $1.7k net, $6.6k maximum outstanding;
  - its shape: Q1 pays, Q3 claws most of it back.
- **Sep 5-6:** the m = 0 and $10 results from the D58 confirmation (m = 0: 2 agents harmed, $51; cut 82.9 MW).
- **A loader dry run** on four real traces (`h2h_c_jan12_h100_1p`, `h2h_c_nov28_h100`, `h2h_c_sep05_h100_m0`, `h2h_c_jul14_h100_m10`). It read inputs only:
  - binding-call deadlines and gate times;
  - DA price versions (1 and 2);
  - priced-step counts (53, 63, 10 and 16).

  No P\*, Q\*, shortfall or share was computed.
- **Two checks on already-seen D58 data, before this commit:**
  - the exact payouts ($42,334.81 at m = 0, $106,691.44 at $10);
  - a priced-step check on all 25 traces this test reads (the 11 comparators, the 11 m10 runs, Sep 5-6 and the two m = 0 reruns). The exchange marks a step priced when the unrounded settled price is above 1e-9, and the tool marks it priced when the broadcast price (rounded to 6 decimals) is. The two counts agree in every trace (`peak_settle` steps), and no settled price lies between 0 and 1e-6 $/MWh, so the in-run and offline response measures count the same steps.
- **The v1 in-run check on Jan 12-13** (see "Amendment v2"): only maximum differences and match flags.
- **No code drift:** `git diff 6156964 HEAD -- agents/ sim/ exchange/ bench/` was empty before the D62 build, so the in-run runs differ from the m = 0 reruns only by the D62 code.
- **The build was tested on synthetic data only** (`tests/test_d62_guarantee.py`).
- **Not blind:** the threshold, coverage and cost gates are judged on windows whose m = 0 results are known. The stand-in suggests coverage and cost pass if P\* tracks our agents. Whether it does is unknown.

## Amendment v2 (Oct 30)
**What happened.** The v1 in-run check on Jan 12-13 (`h2h_c_jan12_h100_g`, run on a0f79d3) returned MISMATCH, and the queue stopped before Nov 28-29, as the stop rule says.
- **Its only failing item:** "paths", a maximum in-run vs. tool difference of 10,618.9 kW.
- **Everything else passed:**
  - the reproduction was exact (exchange trace, selfish trace, summary);
  - accruals agreed within $0.0000052, and response kWh exactly;
  - the tool on the new trace equalled the tool on `h2h_c_jan12_h100_m0` and on `h2h_c_jan12_h100_1p` exactly;
  - the gate-0 checks inside the step passed.

**Cause: a checker bug.** `inrun_vs_tool` compared each stage's message over its whole tail, from d_g to the slice end, against the final P\* and Q\*.
- By the definitions above, the next stage re-plans a stage's steps after the next deadline, so they always differ there and never enter an accrual.
- A diagnostic on the v1 trace, with no code change, found the in-run and tool paths identical on each stage's own span (0.0 kW). The 10,618.9 kW lies entirely on stage-1 steps after d2 (DC_08, `p_net_kw`).
- The synthetic tests had one binding call, so they never reached this case.

**Owner's decision (option 1):**
- Fix the checker to compare each stage over its own span only.
- Add a two-gate synthetic test (`test_inrun_check_compares_each_stage_over_its_own_span_with_two_gates`). It fails under the v1 comparison and still catches a 0.01 kW change inside a stage's own span.
- Re-register as v2, and rerun both in-run runs on the v2 commit, so the integrity rule holds without exceptions.

**What changes, and what doesn't:**
- Nothing else in the code, the definitions, the gates, the tolerances or the run order changes.
- The v1 run's outputs are kept, not used: `runs/d62/v1_h2h_c_jan12_h100_g`, `v1_inrun_jan12.json`, `v1_logs/` and `v1_queue_status.json`.
- **What v1 showed:** only the maximum differences listed above. No gate number was computed: no cell, share, ledger or verdict. The v1 run's summary `guarantee` block was not opened.

## The build
- **`sim/reference.py`**
  - `ref_battery` is the single input bundle for 1p's Q and D62's P\* and Q\*. D64's declarations will enter it.
  - `selfish_plan` returns 1p's Plan; `selfish_site_net` is unchanged (tested).
  - `ModeledPaths` builds P\* and Q\*.
- **`exchange/guarantee.py`:** pure arithmetic, shared by the exchange and the tool: spans, span shortfall, response measure, share, separation and ledger.
- **`exchange/core.py`**, behind `guarantee` (default off; its keys enter the config and the trace header only when on). It accepts:
  - `modeled_response` from the metering service only, for a binding call, after that call's final price, before its deadline, once;
  - `rt_price`: the RT prices relayed by the metering service, once. The exchange never reads data files.

  At the closing tick, after `peak_settle`, it sends each agent `guarantee_accrual`: its shortfall by span, modeled shift and delivered kWh. A missing input fails closed.
- **`sim/run_m1.py`**, behind config key `d62_guarantee`:
  - after each binding call's price rounds, the metering service sends each asset's stage;
  - before the closing tick, it relays RT prices;
  - the summary gets a `guarantee` block.
- **`bench/d62_guarantee.py`:** the offline tool; one window per process.
- **`configs/d62/`:** copies of `configs/d58m/h2h_c_{jan12,nov28}_h100_m0.yaml` with only `run_name` and `d62_guarantee: true` set.
- **What doesn't change:** agents never see the D62 messages, so behaviour is unchanged (tested: with the flag on, the run is identical outside the new message types and msg_ids). `bench/d56_resettle._read` already ignores message types it doesn't know, so it is not changed.

## Definitions
**Binding calls.** Each window has two.
- Call 1 has deadline d1 (D1 10:00Z in winter, 09:00Z in summer) and settles [d1, d2).
- Call 2 has deadline d2 (D1 21:00Z / 20:00Z) and settles [d2, slice end).
- Span g = [d_g, d_{g+1}); the last runs to the slice end. A span is exactly the steps call g settles. The tool checks this per run.

**Inputs.** 1p's only:
- registered max_kw;
- metered usable kWh;
- start SOC, which is also the soft end target;
- recorded base export and available PV (recorded curtailment);
- wear (10 $/MWh);
- ETA_REF = 0.95 and SD_REF = 0;
- the DA price version published by gate g (`PriceForecast.at` at the gate's first price round).

**The paths** (`sim/reference.py`, `ModeledPaths`):
- **Q_g (1p, unchanged):** the selfish plan from slice start with version v_g. It is the settlement reference on the steps call g settles.
- **P\*, one continuous path:**
  - before d1, P\* = Q_1;
  - at d1, it re-plans from Q_1's SOC at d1 with v1 plus call 1's final peak price array;
  - at d2, it re-plans from **P\*'s own SOC** at d2 with v2 plus call 2's final peak price array;
  - no margin, no prox; steps before each re-plan are held.
- **Q\*, one continuous acting-alone path:**
  - Q\* = Q_1 until d2;
  - at d2, it re-plans from Q\*'s own SOC with v2 and no peak price.
  - Stitching Q_1 and Q_2 would jump SOC at d2.
- The final peak price is the call's last `peak_price` broadcast (`final: true`), the array P\*'s LP sees.

**Shortfall** (`exchange/guarantee.py`, `span_shortfall`), per agent and span. Here K = 0.25/1000, and E(x) = Σ RT·x·K − wear·K·Σ discharge over the span:

shortfall_g = E(Q\*) − [E(P\*) + Σ_priced p·(P\* − Q_g)·K]

- p is the settled peak price. Only priced steps (p > 0) carry settlement.
- The last span adds (SOC_end(Q\*) − SOC_end(P\*)) × p_end, with p_end = the slice's mean DA price / 1000, as in section 8.
- Window shortfall = Σ_g shortfall_g. Day 2 lies in span 2 only.

**Example** (Jan 12-13; d1 = 12th 10:00Z, d2 = 12th 21:00Z, end = 14th 06:00Z):

| Steps | P\* | Q\* | Refund reference | Counted in |
|---|---|---|---|---|
| before 10:00Z | = Q_1 | = Q_1 | none | nothing (0) |
| 10:00–21:00Z | re-plan from Q_1's SOC, v1 + p₁ | Q_1 | Q_1 | span 1 |
| 21:00Z–end | re-plan from P\*'s own SOC, v2 + p₂ | re-plan from Q_1's SOC, v2 | Q_2 | span 2 |

If P\* sells 100 kWh more than Q\* in span 1 and buys back 105 kWh in span 2, span 1 carries the extra revenue plus p₁ × 100 kWh, and span 2 carries the 105 kWh rebuy.

**Response measure (D65)** on priced steps against 1p's Q_g, summed over an agent-quarter:
- Δ_t = max(P\*_t − Q_g,t, 0);
- delivered_t = min(max(N_t − Q_g,t, 0), Δ_t), where N is metered site net;
- share = Σ delivered / Σ Δ, which is at most 1;
- under 1 kWh of Σ Δ, the measure is 0/0: no modeled shift, eligible, and left out of separation.

**Acting-alone (non-responder) sample:** the same measure with N replaced by the metered site net of each window's own selfish run (`runs/<run>/selfish`), against the same P\* and Q_g.

**Quarters:** by window start. Q1 = Jan 2, Jan 12, Jan 16, Jan 31, Feb 12; Q2 = Jun 19; Q3 = Jul 1, Jul 14, Aug 28, Sep 21; Q4 = Nov 28. All are in 2023. Sep 5-6 (Q3) is not in the 11-window ledger.

**Ledger** (`exchange/guarantee.py`, `ledger`):
- An eligible quarter adds its shortfall. An ineligible one adds only min(shortfall, 0): gains count, losses don't.
- At each quarter end, balance = max(0, year-to-date sum). An increase is paid, and a decrease is clawed back by netting, never more than was paid that year. The balance resets each January 1.
- Eligible means share ≥ threshold, or 0/0.

**Stage-1 identity** (owner's exact form), on [d1, end), per agent:

[v1 energy cost + wear + terminal term](P) − p₁·(net_P − net_Q1)·K ≤ [v1 energy cost + wear + terminal term](Q_1) + $0.01

- The bracket is the planner's objective without any peak price:
  - v1 · (c − d + x) · K;
  - plus wear · K · Σd;
  - plus TERMINAL_USD_KWH · max(0, s_term − s_end).
- **The peak price enters once, as the settlement payment.** It is not also inside P's energy cost.
- p₁ is call 1's final price array, the same one P's stage-1 LP used.
- It must hold because P's stage-1 LP minimises bracket − p₁·(net − base)·K, and Q_1's tail is feasible for it (same SOC at d1).

## Gates
PASS needs all of gates 0-4, in this order.

**Gate 0. Fail-closed checks.** A failure stops that window and is recorded. The verdict is then FAIL_check, because gates 2-4 need all 11 windows. No gate, tolerance or definition changes after any number is seen.

**FAIL_check means the test is invalid, D62 stays Leaning, and it is not evidence against D62** (owner, Oct 30).
- **Rosters** come from each run header, are complete in every trace (exchange telemetry, selfish telemetry, register) and are identical across windows. A missing agent fails.
- **Stage-1 identity:** at most $0.01 per agent.
- **Tool double run:** each window is computed twice in one process, and the results must be identical.
- **1p reference:** where 1p ran in-run, the tool's Q_g on settled priced steps equals the in-run `selfish_reference` within 1e-6 kW.
- **1p settlement-caused harm is 0** in every window (`bench.d56_resettle.cell_metrics`), along with the D56 cell checks those cells already passed: accounting and the settlement match.
- **Payout figures:** the m = 0 payout ($42,334.81) and the $10 payout ($106,691.44), recomputed from the D58 cell results, each match within $0.01.
- **Accounting:** each agent's paid minus clawed-back equals its final balance, and the operator's outlay (gross, clawed back, net) equals the sum over agents, within $0.01. In each in-run run, the summary's `guarantee` total equals the sum of the agents' `guarantee_accrual` shortfalls within $0.01.

**Gate 1. In-run confirmation:** Jan 12-13 and Nov 28-29 at m = 0 with `d62_guarantee: true` (`h2h_c_jan12_h100_g`, `h2h_c_nov28_h100_g`).
- **Reproduction** against `h2h_c_jan12_h100_m0` / `h2h_c_nov28_h100_m0` (same settings, built at 6156964; both matched the originals exactly in the D58 baseline):
  - in the exchange and selfish traces, with `modeled_response`, `rt_price` and `guarantee_accrual` removed, msg_ids dropped and the header line skipped, every message must match exactly (run names swapped): type, sender, recipient, time and payload, which covers nominations, meter readings, peak prices and settlements;
  - the summaries must match outside the ignored fields listed under "Run order".
- **In-run vs tool, on the new trace:**
  - each stage's `modeled_response` (P\* and Q\* net and discharge), over its own span [d_g, d_{g+1}) only, against the tool's final P\* and Q\*, within 1e-6 kW. A stage's steps after the next deadline are re-planned by the next stage, so they are not compared (v2);
  - accruals (total and by span) within $0.0001;
  - modeled shift and delivered kWh within 1e-6 kWh.
- **Tool vs tool:** the tool's per-agent shortfall, shift and delivered kWh (run and acting alone) on the new trace equal those on `h2h_c_<w>_h100_m0` and on the original comparator (`h2h_c_jan12_h100_1p`, `h2h_c_nov28_h100`), within $0.0001 and kWh.
- **Any mismatch: report the difference and stop.** Offline numbers are not used, and the test goes all in-run.
- **The tool labels the outcome:**
  - MATCH;
  - MISMATCH: behaviour drift; stop and go all in-run;
  - FAIL_check: a gate-0 check failed inside the in-run step, such as the stage-1 identity or accounting. That is a wiring fault: stop, and the owner decides. The code is frozen.

**Gate 2. Separation.**
- Every m = 0 agent-quarter's share must exceed every acting-alone agent-quarter's share, with 0/0 left out of both groups.
- **PASS:** threshold = (highest acting-alone + lowest m = 0) / 2.
- **Otherwise FAIL_separation:** no threshold is set, D62 stays Leaning, and the overlapping agent-quarters are listed.
- The report gives how many agent-quarters were left out of each group.

**Gate 3. Coverage.** For each agent:

Σ over the 11 windows of (its m = 0 result vs acting alone under 1p) + its D62 year-end net payment ≥ −max(1% × |Σ acting alone|, $10)

- The result vs acting alone is the D58 comparator cell's, as in `bench.d58_horizon`.
- The net payment is the ledger with the threshold applied.
- This is the unchanged horizon tolerance, with the same float guard.

**Gate 4. Cost.** m = 0 peak payout over the 11 windows + D62 **gross** payments (eligible only, quarterly, before any clawback) < the $10 margin's payout over the same windows.
- Reported next to it: net after the true-up, clawbacks, maximum outstanding (fleet), and the stand-in estimate.
- Both sides rest on D54's placeholder cap (Q-N1), so the comparison is like for like.
- Cost vs. the value of relief is report-only until Q-N1 is answered.

**Verdict order:** STOPPED_inrun (gate 1) → FAIL_check (gate 0) → FAIL_separation → FAIL_coverage → FAIL_cost → PASS.

**Gate 5. Fresh window: Sep 5-6, held out.** Computed only after the verdict commit, and only if a threshold exists. All three must hold, or the result is CONFIRM_FAIL:
- **Separation at the threshold:** every m = 0 agent's share on that window ≥ threshold, and every acting-alone share < threshold, with 0/0 left out.
- 1p settlement-caused harm is 0.
- **Coverage on that window:** each agent's m = 0 result vs acting alone + that window's D62 payment (max(0, shortfall) if eligible on the window's own share) ≥ −max(1% × |acting alone|, $10).

## Reported, not gated
- **Scope of separation and the threshold:** they are validated on our reference agents only. Our m = 0 agents plan like P\* (D63's known gap), so the threshold is not yet evidence about outside agents.
- **Per-agent shortfall components:**
  - the shortfall at the final DA prices instead of RT (the remainder is the RT-vs-DA gap);
  - the reference mismatch Σ_priced p·(Q\* − Q_g)·K, which is non-zero on span 2 only.
- **The ledger by quarter:** paid, clawed back, outstanding, agents owed, and ineligible agents.
- **Per window:** shortfall total, positive shortfall and peak payout.
- **$10-posture shares**, from the m10 traces, each with its own P\*: how many would clear the threshold (cautious-agent eligibility).
- **The ledger without Nov 28-29** (sensitivity only).
- **The true-up over runs longer than 2-day windows:** an open item, answered by D61's month experiment with D66's warm-up.

## Run order
1. **The pre-registration commit.**
2. **In-run runs, after the owner's go-ahead.** One at a time, detached, from the repo root, on the clean commit (about 26-29 min each):
   - `.venv\Scripts\python -u -m sim.run_m1 configs/d62/h2h_c_jan12_h100_g.yaml`
   - then the Nov 28-29 config.
3. **The in-run checks:** `python -m bench.d62_guarantee inrun jan12`, then `inrun nov28`. A MISMATCH stops the test here.
4. **Offline cells**, one per process: `python -m bench.d62_guarantee cell <run>`, for the 11 comparators (`bench.d58_margin.M0_RUN`) and, for the report, the 11 m10 runs.
5. **The verdict:** `python -m bench.d62_guarantee verdict`, then the integrity diff, then **the verdict commit**.
6. **The fresh window:** `cell h2h_c_sep05_h100_m0`, then `fresh`, then `report`. The confirmation is added to this file.

**Summary fields ignored in the reproduction:**
- D58's list: `header`, `runtime_s`, `wall_utc`, `round_secs`, `round_wall_utc`, `compare_solve_secs`, `run_at_utc`, `git_commit`, `tool_sha256_16`;
- plus `messages_total`, `messages_per_day` and `guarantee`;
- plus the run names themselves.

**Trace comparison:** the header line is skipped, D62's three message types are removed, and msg_ids are dropped. New messages shift the metering service's sequential msg_ids. Everything else must be equal.

**Tolerances:** stage-1 identity $0.01 per agent; paths 1e-6 kW; accruals $0.0001; response 1e-6 kWh; payout recomputation $0.01; ledger accounting $0.01. None changes after any run.

## Defaults chosen here (all accepted by the owner, Oct 30)
1. At exactly the threshold, an agent counts as eligible (share ≥ threshold). The midpoint makes a tie unlikely.
2. On Sep 5-6, the window's D62 payment is max(0, shortfall) if the agent is eligible on that window's own share.
3. Sep 5-6 counts as Q3 but stays out of the 11-window ledger, so gates 2-4 compare like for like with the $10 margin.
4. A gate-0 failure in any window makes the verdict FAIL_check, because gates 2-4 need all 11 windows.
5. The m10 shares (report only) use each m10 run's own P\*, computed from its own settled prices.

## Disclosures
- **D63's known gap:** P\* and Q\* use our planner. For our agents P\* is close to how they plan; for outside agents it is a model. Gate 3 checks our agents only.
- **The guarantee changes no behaviour in this test.** Agents never see D62's messages, and the accrual comes at the closing tick. An agent that knew the threshold could respond differently; that is not modeled.
- **Reference mismatch:** on span 2, Q\* differs from 1p's Q_2. The shortfall includes p₂·(Q\* − Q_2) on priced steps, and it is reported per agent.
- **D64:** availability declarations are not built yet, and no test window has any. P\* and Q\* share 1p's single input bundle (tested). The behavioural test is written with D64's build: a declared outage over priced steps gives Δ = 0, so 0/0 and eligible.
- **Gaming paths:** these are 1p's inputs (declared capacity, usable energy, SOC at slice start), plus any influence an agent has on the settled price that P\* responds to. They are M3 items.
- **A sample, not a year:** 11 high-peak two-day windows, not whole quarters. Q2 and Q4 have one window each.
- **Payout figures** rest on D54's placeholder cap (Q-N1).
- **A PASS changes nothing by itself** (owner: report only).

## Results (Oct 30, 2026)

**Verdict: FAIL_separation.** Under the pre-registered rules, no threshold is set and D62 stays Leaning. Gates 3 (coverage) and 4 (cost) are not evaluated. Gate 5 (Sep 5-6) is not run, because it needs a threshold.

**Integrity:**
- Pre-registration v2 is `eea5a91`, with hash-record commit `4dc9a29`.
- Both in-run headers show `4dc9a29` without `-dirty`.
- At the verdict, `git diff eea5a91 -- agents/ sim/ exchange/ bench/d62_guarantee.py bench/d56_resettle.py bench/d58_margin.py bench/d58_horizon.py configs/d62/` was empty, and so was this file's diff in "Definitions", "Gates" and "Run order".
- Tool outputs are in `runs/d62/` (`inrun_jan12.json`, `inrun_nov28.json`, `cells/`, `verdict.json`, `report.md`).

**Gate 1 (in-run): MATCH on both windows.**

| | Jan 12-13 | Nov 28-29 |
|---|---|---|
| Run time | 57 min | 30 min |
| Reproduction (exchange trace, selfish trace, summary) | exact (0 differences) | exact (0 differences) |
| P\*/Q\* in-run vs tool, own spans | 0.0 kW | 0.0 kW |
| Accruals in-run vs tool | $0.0000052 | $0.0000014 |
| Response kWh in-run vs tool | 0.0 | 0.0 |
| Tool on new trace vs m = 0 rerun and vs original | 0.0 | 0.0 |

**Gate 0 (fail-closed checks): passed as far as the verdict reached.**
- All 22 cells (11 m = 0 comparators, 11 m10) computed. Each passed:
  - the tool double run;
  - complete rosters (80);
  - the 1p reference vs in-run `selfish_reference` where 1p ran in-run.
- Stage-1 identity: worst slack 1.1e-13 $ (tolerance $0.01).
- 1p settlement-caused harm: 0 agents in all 11 windows.
- Not reached, because they need a threshold: the ledger accounting check and the payout recomputation, which sit on the coverage and cost path.

**Gate 2 (separation): FAIL.** 320 agent-quarters in each group (80 agents × 4 quarters), none 0/0.
- The lowest m = 0 share is 0.021, below the highest acting-alone share of 0.714.
- **Overlapping agent-quarters:**
  - 315 of the 320 m = 0 agent-quarters are at or below 0.714. Only 5 are above it.
  - 123 of the 320 acting-alone agent-quarters are at or above 0.021.
- The full list is in `runs/d62/verdict.json` (`gate2_separation.overlap`).

**Reported, not gated (descriptive; post hoc):**
- **Share quantiles** (0, 10, 25, 50, 75, 90, 100%):
  - m = 0: 0.021, 0.172, 0.318, 0.421, 0.519, 0.590, 0.804;
  - acting alone: 0.000, 0.000, 0.000, 0.008, 0.044, 0.147, 0.714.
- The two groups differ strongly in the middle but overlap in the tails, so the strict pre-registered rule fails.
- **Our m = 0 agents deliver less than half of P\*'s modeled shift** at the median (0.42). P\* overstates how far our own agents respond, at least as measured against 1p's Q_g. Some acting-alone agent-quarters show large "delivered" shares because their actual selfish schedule departs from 1p's modeled Q_g on priced steps.
- **Scope:** as pre-registered, this is about our reference agents only (D63).

**What this does and doesn't say:**
- It says D65's response measure, as defined (delivered share of P\*'s modeled shift against 1p's Q_g), cannot serve as a strict eligibility rule. It doesn't tell our m = 0 agents apart from acting-alone agents in every agent-quarter.
- It says nothing about whether the guarantee's coverage or cost would pass. Neither was computed.
- Choosing a threshold after seeing these distributions would be selection on seen data. The pre-registration forbids it.

### Post-hoc diagnostic (Oct 30, owner-requested; exploratory)
**This section is post hoc. It sets no threshold and decides nothing.**
- The 11 windows are now the design set for a v3, and nothing was run on new windows.
- The tool is `bench/d62_diag.py` (its own file; the pre-registered tool is unchanged). Outputs are in `runs/d62/diag/` (`summary.json` plus one file per window).

**Gate-0 checks the verdict never reached: both pass.**
- **Ledger accounting:** largest difference $1.1e-13 (tolerance $0.01).
- **Payout recomputation:** m = 0 $42,334.81 and $10 $106,691.44, both exactly the pre-registered values.

**1. Coverage and cost with every agent-quarter eligible.**
- **Coverage: fails.** 2 agents are beyond tolerance, and they are exactly the two that failed the D58 horizon test without D62:

  | Agent | Result vs acting alone | Tolerance | D62 net payment |
  |---|---|---|---|
  | PVB_39 | −$865 | $718 | $0 |
  | PVB_22 | −$129 | $103 | $0 |

  - D62 pays them nothing, because their modeled shortfall over the year is negative (PVB_39 −$26, PVB_22 −$148).
  - Window by window, the model moves with the actual result in some windows but not in size. PVB_39 on Jan 12-13: actual −$1,668, modeled shortfall +$519. On Sep 21-22 it reverses: actual +$352, modeled −$613. Over the year the model nets out while the actual loss doesn't.
  - Only 4 agents get a net payment.
- **Cost, as an upper bound:** D62 gross $2,603 + m = 0 payout $42,335 = $44,938, against $106,691 for the $10 margin.
  - Net after the true-up: $1,001.
  - Clawed back: $1,602.
  - Maximum outstanding: $2,032.

**2. The m = 0 median share of 0.42, decomposed.** Share quantiles over 320 agent-quarters per group (0, 10, 25, 50, 75, 90, 100%):

| Measure | m = 0 | Acting alone | Strict separation |
|---|---|---|---|
| (pre-registered) per-step clipped, P\* | 0.021, 0.172, 0.318, **0.421**, 0.519, 0.590, 0.804 | 0, 0, 0, 0.008, 0.044, 0.147, 0.714 | fails (315 / 123 overlap) |
| (a) netted per span, P\* | 0.367, 0.673, 0.806, **0.907**, 1, 1, 1 | 0, 0, 0, 0.004, 0.107, 0.386, 1.000 | fails (320 / 33 overlap) |
| (b) per-step, P\* with final-round prox | 0.841, 0.978, 0.989, **0.998**, 1, 1, 1 | 0, 0, 0, 0.020, 0.066, 0.196, 0.691 | separates (0.841 > 0.691) |
| (a)+(b) netted, P\* with prox | 0.907, 0.973, 0.989, **0.999**, 1, 1, 1 | 0, 0, 0, 0.007, 0.111, 0.299, 0.882 | separates (0.907 > 0.882) |

- **(a) Per-step clipping** accounts for about half the gap: the median rises from 0.42 to 0.91. But one acting-alone agent-quarter reaches 1.0, so a netted measure still overlaps.
- **(b) The prox term** accounts for nearly all of the rest. Our agents' responses are damped by the price loop's prox. P\* re-solved with each agent's final-round prox is matched almost exactly (median 0.998).
  - **Caution:** that prox's centre is built from the agent's own last nomination, which D62 rules out of P ("nothing the agent sends enters P or Q"). So (b) explains the gap but is not a usable design as it stands.
  - A v3 that wants this effect would need an exchange-side damping term built from public inputs only.
- **(c) Non-convergence does not explain it.** Gates whose price rounds converged have a *lower* median span share than those that didn't: 0.354 (80 spans) against 0.468 (762 spans). Only 4 of 22 binding gates converged.

**3. Under the netted measure,** the m = 0 and acting-alone distributions are far apart in the middle (medians 0.907 vs 0.004) but still overlap at the extremes: the acting-alone maximum is 1.0, against an m = 0 minimum of 0.367.

**What this suggests for a v3 (design input only, not a decision):**
- A delivered-share eligibility measure needs (a) netting and (b) a P that includes the price loop's damping. Only that combination separated the two groups on these windows.
- Separately, **the guarantee as modeled would not cover the two agents D58 found harmed.** P\*'s modeled shortfall moves with their actual result in some windows but not in size, and over the year it nets out while the actual loss doesn't.
- That is a coverage problem independent of eligibility. Even with everyone eligible, gate 3 fails.
