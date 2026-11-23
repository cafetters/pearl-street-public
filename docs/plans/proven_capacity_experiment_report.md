# Proven capacity: the parameter experiment, report (Nov 21, 2026)

> Judged against the pre-registration `docs/plans/proven_capacity_experiment.md` (six amendments, all before any
> result). **All numbers are synthetic** [sim, synthetic]. **No operating point is chosen here; the owner picks.**
> Tables: `python -m bench.proven_report runs/cloud --commit 81cb416` (`bench/proven_report.py`, written after the
> run). Section B's measures were chosen after seeing the results.

## The run

- **Runs:** 162 of 162 points (12 shards, queue `pcsweep7`), every trace header at `81cb416`, the commit the sixth
  amendment pins. Each shard's output was sha256-checked.
- **The reaper:** the low-memory reaper killed the laptop launcher at about 23:42 ET (Nov 20) with no shard finished.
  `runs/m5/rescue/rescue.py` started within about 3 minutes and kept every sandbox alive by polling it. It fetched
  each shard as it finished, between 00:27 and 01:51 ET (Nov 21), sha256-checked it, and marked its DONE file
  "rescued". No relaunch, no rerun, and no shard was lost. The shards ran unchanged on their sandboxes; only the
  download path differs from a normal launch.
- **Cost:** about $0.65 (29.6 sandbox-hours, 1.8 to 3.0 hours per shard, from creation to download, at the queue log's
  $0.022 per hour).

## Correction: the sybils started at 100 kW each, not at the 1 MW starter cap

The pre-registration describes each sybil as "1 MW on paper (at the starter cap)". The 1 MW paper was attested
(nameplate and power), but each sybil's reference agent registered its real battery, 100 kW, as its mandate claim. The
starter is min(claim, attested nameplate, type median) = min(100 kW, 1 MW, 1 MW) = **100 kW**. So the 40 sybils
started with 4 MW of untested capacity in total, not 40 MW. The giant is not affected: it claimed its real 1 MW, which
equals the type median.

What this changes:
- **The sybil numbers describe a weaker attack** than the one pre-registered: 40 liars at 100 kW each, not 40 at 1 MW
  each.
- **"B = 5 and 20 MW give the same y"** holds for this scenario only. At 1 MW each, day 2 would count up to
  40 x 0.5 x 900 kW (each sybil's nominated relief, now under its ceiling) = 18 MW, an expected 9 MW. So B would bind
  at 5 MW and not at 20 MW. Not run.
- **The mechanisms** (detection lag, realized = expected / (1 - rate), the charging swing, R per entity) are the same.

Not rerun: whether to rerun the sybil scenario with the claim at the paper (1 MW) is the owner's call.

## Headline

1. **The trade-off curve is degenerate: 2 distinct points out of 27.**
   - x (honest newcomers' median calls to full proven capacity) is 10 at every point. The fixed growth (g x s =
     50 kW per clean call) sets it: (1.5 MW - 1 MW starter) / 50 kW = 10. B, R and strength cannot move it.
   - y (peak uncovered shortfall, the max over the two liar scenarios) is 2.0 MW at B = 1 MW and 6.0 MW at
     B = 5 or 20 MW, whatever R and strength are.
2. **R does nothing in these worlds.** No measure changes with R. Every asset's counted relief stays far below
   5 MW, and every sybil is its own legal entity.
   - At B = 5 and 20 MW, the sybil peak (6.0 MW) is **above R = 5 MW**. The per-entity cap cannot see 40 entities
     together. (The sybils ran at 100 kW each; see the correction.)
3. **B bounds the expected shortfall, not the realized one.** At B = 1 MW the realized sybil peak is exactly 2 x B
   (2.0 MW against 1.0 MW expected).
   - The expectation uses the prior mean rate 0.5, while the liars' unproven part delivers nothing. So the realized
     peak is the expected shortfall / (1 - rate).
4. **The peak comes from detection lag, on the second call (day 2).**
   - A call's record updates at 06:00 UTC the day after its window. That is 80 to 132 steps (20 to 33 hours) after
     the first missed step.
   - The next daily call's counting is fixed at its aggregate, before then. So every liar is counted at its untested
     starter amount for two calls, not one.
5. **The counting ceiling includes the reference's charging.** A battery's counted relief is capped at
   idle + proven + step - reference. Where the 1p reference charges, that is up to proven + step + its charging
   power: a 1 MW battery about to charge at 1 MW can swing 2 MW.
   - That doubles the giant's day-2 count (0.5 x 2,100 kW = 1,050 kW). It also sets the sybils' 6.0 MW peak
     (40 x 0.5 x 300 kW).
   - It also keeps a liar with proven capacity 0 counted at rate x (step + charging), which explains the tail below.
6. **Lying loses money.** Attacker gain against the honest twin is about -$4.81M (giant) and -$5.64M (sybil, all
   40 together) at every point. The size comes from the imbalance charge: the liars' deviations are charged at
   $500.00/MWh, the scarcity price, on about 29 MW (giant) or 0.9 MW each (sybil) of undelivered relief on every
   call step.
   - Proven capacity is not what makes lying lose. By design, the imbalance base stays the unweighted nomination,
     so the flag-off run should charge the same. That follows from the design; it was not run here.
7. **Firm-when-called is 1.000 everywhere, and here that is by construction.** Only assets past probation (n0 = 3
   clean calls) have firm relief. The liars never pass probation, and the trusted fleet always delivers within its
   band. The measure can fall below 1 only when a firm asset misses beyond its band, which no world here contains.
   So it is no evidence for M5's pass line yet.

## Pre-registered measures

All 27 points are in the table from `bench/proven_report.py` (section A). Summary:

| Measure | Result |
|---|---|
| Peak uncovered shortfall (MW), against R | giant 1.050 at every point; sybil 2.000 (B = 1 MW) or 6.000 (B = 5, 20 MW). Above R = 5 MW at 6 of 9 R = 5 points. Honest twins (54) and honest_flood: 0 |
| Steps with any uncovered shortfall (of 1,328 counted steps) | giant 1,116-1,117; sybil 1,113-1,127. Most are small: see section B |
| Max expected shortfall from unproven capacity (kW), against B | giant 525; sybil 1,000 (B = 1 MW, binding) or 3,000 (not binding); honest_flood 1,000 / 5,000 / 5,933 for B = 1 / 5 / 20 MW (binding at 1 and 5) |
| Firm-when-called | 1.000 in all 162 runs (see headline 7) |
| Honest newcomers' calls to full proven capacity | median 10, max 10, all 10 newcomers reach it, at every point |
| Attacker gain vs honest twin ($) | giant -4,805,630 to -4,805,685; sybil -5,643,640 to -5,644,107 |
| Detection, meter | 0 steps (the simulator meters each step in that step; real meter latency is not modeled) |
| Detection, exchange record | 80 to 132 steps (one record update per call, at the next day's 06:00 UTC) |
| Share of the ceiling (proxy: metered fleet peak cut vs acting alone) | **Not delivered.** The sweep code returns no such field, and the shards have no acting-alone run. Computing it now would need a definition written after the result |

**Trade-off curve:** x = 10 calls at every point; y = 2.0 MW (9 points: B = 1 MW, any R and strength) or 6.0 MW
(18 points: B = 5 or 20 MW). The attacker gain is negative and firm-when-called is 1.000 at every point.

## B. Chosen after seeing the results (not pre-registered)

These separate the points that the pre-registered measures cannot tell apart.

| Strength (prior a0 + b0) | 2 | 6 | 20 |
|---|---|---|---|
| Uncovered energy over 14 days, giant (MWh) | 25.9 | 31.8 | 38.1 |
| Uncovered energy, sybil, B = 1 MW (MWh) | 215.4 | 324.2 | 437.5 |
| Uncovered energy, sybil, B = 5 or 20 MW (MWh) | 291.6 | 415.7 | 549.8 |
| Last call's peak uncovered, sybil, B = 5 or 20 MW (kW) | 620 | 1,371 | 2,521 |
| Liars' final delivery rate | 0.064-0.069 | 0.151-0.155 | 0.295-0.297 |
| Honest newcomers' final delivery rate (median) | 0.933 | 0.842 | 0.697 |

- **Strength is a real trade-off, but not on the pre-registered x axis.** A weak prior (2) drops a liar's rate
  fastest, so it has the least uncovered energy and the shortest tail. It also lets honest newcomers' rate rise
  fastest. A strong prior (20) is worse on both sides: liars stay counted longer, and honest newcomers' relief is
  discounted more (counted = rate x relief). Within this grid, a weak prior is better for both.
- **The tail:** the giant's uncovered shortfall falls from 1,050 kW (day 2) to 80 kW on the last call (strength 2).
  Its proven capacity is 0 from call 1, so what remains is rate x (step + the reference's charging), shrinking as its
  rate falls.
  - The sybils' last call rises again (457 to 620 kW at strength 2). That call's window runs 9 hours longer
    (132 steps). Its peak step falls in those extra hours, where each sybil's 1p reference charges at its full 100 kW.
    At the previous call's peak step it charged at about 47 kW. The ceiling is wider by the difference (an
    end-of-run effect of the reference plan; checked at these two steps only).
- **Liars end at proven capacity 0, below their real batteries.**
  - The giant's battery exports 1,000 kW on 143 steps, from call 2 on.
  - But on call 1 it exported nothing, so the miss dropped its starter to 0 ("never below what was seen", seen 0
    then).
  - Proven capacity grows only on clean calls, and an asset that keeps over-nominating never has one.
  - This is consistent with the owner's rule, but the owner's test said an over-claimer "converges to its real
    capacity". Here it converges to 0 for as long as it lies. It would grow back once it nominates honestly (not run
    here).

## Disclosures

- **The simulated grid.** The external grid absorbs any gap. We report the size of the gap the operator would have
  to cover, not whether the grid keeps up.
- **Synthetic worlds:** 14 days of one repeated synthetic day; the liars' paper and the newcomers' sizes are set
  here.
- **The price bound.** In sybil and its honest twin the final peak price sits at its cap (100.0 in the trace) on 11
  to 14 of 16 calls; in honest_flood on up to 7 of 16; in no_newcomers and giant_liar never. Both sybil and its twin
  hit it, so the cap comes from the newcomers' presence, not from the lie, but prices there say nothing past the cap.
- **The liars' attack is crude:** they over-nominate on every step of every call. A liar that over-nominates only at
  the peak would pay less imbalance; this grid does not test that.
- **Section B and `bench/proven_report.py` were written after the result.** The pre-registered measures come from
  `redteam/proven_scenarios.run` at `81cb416`, unchanged.
- **Rescued downloads:** see "The run".

## For the owner (nothing here is run or changed)

1. **Pick the point.** What the grid can and can't separate:
   - x never moves. R and strength don't move y, and B moves y only between 1 MW and 5 MW (sybil, as built).
   - B = 1 MW gives the lowest peak, but B binds on honest_flood at 1 and 5 MW. There the honest newcomers' counted
     relief is scaled down, and the pre-registered measures don't capture that cost.
   - A weak prior (2) gives the least uncovered energy and the highest honest rates here. But no world has an honest
     asset that misses, so the cost of a weak prior to an honest asset after a miss is untested.
2. **Proposed next grid (not launched):** sweep the growth g and step s, which set x; drop R (inert here) or add an
   aggregate cap across legal entities, since 40 sybils exceed R = 5 MW together.
3. **Proposed design questions (no code changed):**
   - **Detection lag:** count the next call with the previous call's misses, for example an in-window update at the
     first missed metered step, so a liar is counted at its starter for one call, not two.
   - **The expected-vs-realized gap:** B could bound the realized shortfall under a worst-case rate (rate 0 for
     unproven capacity), which would make B a hard cap, as R is.
   - **The charging swing:** whether proven capacity should cap relief against the reference (proven + step) or
     the battery's output above idle (today).
   - **What "seen" means:** after a miss, should proven capacity rise to later metered output, or only on clean calls
     (today)? This is part of the miss-impact discussion (Nov 20).
4. **Firm-when-called** needs a world where a firm asset misses before it can show anything about M5's pass line.
