# Proven capacity: the parameter experiment (pre-registration, Nov 19, 2026)

> **Pre-registered.** This file, the shard configs `configs/proven_sweep/` and the code they run are committed before
> any sweep run. Nothing in the grid has been run. The owner picks the operating point from the trade-off curve; no
> point is chosen here. **All numbers are synthetic** [sim, synthetic].

## Question

New assets start with little proven capacity and earn more by delivering. The experiment asks where the operating
point should sit: how fast honest newcomers reach full counting, against how much relief the operator counts that
never arrives. The parameters are the portfolio budget B, the contingency reserve R and the strength of the skeptical
prior.

## Worlds (redteam/proven_scenarios.py, MW scale)

- **Shape:** `redteam/synth.py` `synthetic_slice`, 14 days (the same synthetic day repeated, cheap_peak prices).
- **Market:** arm C, rule 1p, the $5 margin, `delivery_record`, D78's `silent_reference`. Headroom 0.5 MW, as in the
  red-team base.
- **Existing fleet:** 10 batteries of 1 MW / 4 MWh, all with meter history (proven at demonstrated power).
- **Scenarios:**
  - **no_newcomers:** the existing fleet only (control).
  - **honest_flood:** plus 10 honest newcomers of 1.5 MW. Their paper equals their battery. They start at the type
    median, 1 MW, so growth is measured.
  - **giant_liar:** plus one newcomer with 30 MW on paper and a 1 MW battery. It nominates its paper (relief 29 MW
    beyond its battery); its battery runs the honest plan.
  - **sybil:** plus 40 newcomers, each its own legal entity, with 1 MW on paper (at the starter cap) and a 0.1 MW
    battery. Each nominates its paper.
- **Honest twins:** for the two liar scenarios, a twin per grid point where the same agents nominate honestly (same
  paper). Attacker gain is measured against the twin.

## Grid (27 points per scenario; 162 runs with the 54 twins)

| Parameter | Values |
|---|---|
| Portfolio budget B (expected shortfall from unproven capacity, per step) | 1, 5, 20 MW |
| Contingency reserve R (per asset and per legal entity) | 5, 20, 50 MW |
| Skeptical prior strength α0 + β0 (mean 0.5) | 2, 6, 20 |

**Fixed (owner-approved defaults and the build's settings):**
- grow g = 0.5 and cut c = 0.5;
- step s = 100 kW (10% of the type median);
- probation n0 = 3 clean calls;
- clean tolerance 10% of relief energy;
- existing assets' prior Beta(99, 1);
- starter fallback 1 MW.

The grid's values are the plan's starting points (owner, Nov 19); they get replaced by the curve, not chosen.

## Measures (reported per point; none is a pass test)

- **Peak miss:** the max over steps of uncovered shortfall, max(counted relief − delivered relief, 0), in MW, compared
  with R. Also the number of steps with any uncovered shortfall.
- **Max expected shortfall from unproven capacity:** in kW, against B.
- **Firm-when-called rate:** delivered firm relief / called firm relief, over the run. This is M5's pass-line measure
  here; with and without attackers comes from the scenario pairs.
- **Honest newcomers' time to full proven capacity:** binding calls with a nomination until proven ≥ their battery.
- **Attacker gain against the honest twin:** the 1p peak settlement minus imbalance charges, in $.
- **Detection:** two times per miss: steps from the first missed step until the meter shows it (0 here: the
  simulator meters every step in its own step; real meter latency is not modeled), and until the exchange's record
  updates (the call's imbalance tick).
- **Share of the ceiling:** a synthetic slice has no M0, so this is reported as the metered fleet peak cut against
  acting alone. Disclosed as a proxy.

## Output

- **Trade-off curve:** one point per (B, R, strength).
  - x: honest newcomers' median calls to full proven capacity (honest_flood).
  - y: peak uncovered shortfall, the max over the giant_liar and sybil scenarios.
  - Labelled with the attacker gain and the firm-when-called rate.
- **Report:** `docs/plans/proven_capacity_experiment_report.md`, written after the run. **The owner picks the point.**

## How it runs

- On Maritime: `python -m bench.cloud_queue --run configs/proven_sweep/pcsweep_NN.yaml runs/m0_jan12 ...
  --module redteam.proven_sweep`, 12 shards. The M0 files are uploaded only for the launcher's checks; nothing reads
  them.
- The launcher never runs detached. One queue at a time, shared with the main session (handover by message).

**Pins (sha256, first 16 hex digits, at the commit that adds this file):** listed in
`configs/proven_sweep/PINS.txt`, written by the same commit.

## Disclosures

- **The simulated grid.** The simulation's external grid absorbs any gap. We report the size of the gap the operator
  would have to cover, not whether the grid keeps up.
- **Synthetic worlds, not measured.**
  - Each 14-day world repeats one synthetic day.
  - The liars' "paper" and the honest newcomers' sizes are set here.
- **Changes after looking at smoke runs.** These were set after looking at the build's smoke runs (Nov 19):
  - the clean tolerance (10%), since honest synthetic agents deliver 94–96% of nominated relief;
  - the existing-fleet prior Beta(99, 1).
- **Tolerance set after the result.** The change-1 tolerance (prices within 1%, settlements within 5% of flag-off)
  was stated after the synthetic smoke.

**Amendment before any run (Nov 19):** `sim/run_m1.py` changed after the pre-registration commit only to give the
`idle_reference` messages their own id sequence (`aa070a9`); the pins in `PINS.txt` were rewritten then. No sweep
run existed.

**Second amendment, before any sweep result (Nov 20).**
- A first launch (queue `pcsweep`, `e1fda32`) was stopped at 01:41 ET after about 62 minutes for a planned loss of
  internet. No shard had finished, and no result exists or was looked at.
- The owner then chose option (a) for the existing fleet: assets with meter history count at rate 1 until their first
  miss (`docs/plans/proven_capacity_checks.md`).
- `exchange/proven.py` changed accordingly, and `PINS.txt` was rewritten. The grid, scenarios, measures and every
  other setting are unchanged.

**Third amendment, before any sweep result (Nov 20).**
- Two more launches were stopped with no shard finished and no result looked at:
  - `pcsweep3` and `pcsweep3c` (`0ed3250`), stopped at 07:55 ET for the owner's rule change;
  - `pcsweep3b`, stopped after upload failures.
- The owner's rule: a miss never cuts what the asset was seen to deliver. It lowers the delivery rate instead, and
  only a newcomer's untested starter amount is dropped on a miss. The fixed `cut = 0.5` is therefore no longer
  used.
- The counted net is now exactly the nomination when nothing is discounted.
- `PINS.txt` was rewritten. The grid, scenarios and measures are unchanged.

**Fourth amendment, before any sweep result (Nov 20).**
- Launch `pcsweep4` and `pcsweep4c` (`e93d056`) was lost at 10:36 ET: the low-memory reaper killed its launchers on
  the laptop.
- Shard 10 had just finished. Its output was destroyed by an immediate cleanup before it was fetched, and it was never
  read.
- **Correction (Nov 20, evening):** two shards of that launch, 08 and 06, finished and were downloaded before the
  reaper (10:17 and 10:27 ET, at `e93d056`). Their outputs were moved unread to `runs/cloud/_pcsweep_e93d056_unread/`
  and are not used: the sweep reruns all 12 shards at the commit with the fix. No result has been looked at.
- Since then, one fix: the price loop's per-agent loads keep their flag-off values where no counted value exists
  (`a8b438f`). `PINS.txt` was rewritten. The grid, scenarios and measures are unchanged.

**Fifth amendment, before any sweep result (Nov 20, evening).**
- Launch `pcsweep5` (`26ae768`) was lost at about 19:49 ET: the low-memory reaper killed its launcher on the laptop.
  No shard had finished (checked before cleanup), cleanup was confirmed, and no result exists or was looked at.
- Since then, one fix: counted relief is rescaled only when the portfolio or contingency factor is below 1
  (`9b29f90`). Without it, watt-rounded nominations a little above an asset's power gave float differences
  (f + 1.0 x (c - f) != c) that the price loop amplified. `PINS.txt` was rewritten. The grid, scenarios and
  measures are unchanged. All 12 shards rerun at the commit with the fix.

**Sixth amendment, before any sweep result (Nov 20, evening).**
- Launch `pcsweep6` (`919284e`) was stopped at 21:26 ET, about 15 minutes in, with no shard finished and no result
  looked at; cleanup was confirmed.
- The 80-agent check at `919284e` still differed from flag-off (call 8 +0.34%). The cause: the proven path added the
  counted zone totals with Python's `sum()`, which compensates rounding, while flag-off adds with a plain `+=`. Both
  now add the same way (the next commit after `919284e`). `PINS.txt` was rewritten. The grid, scenarios and measures
  are unchanged. All 12 shards rerun at the commit with the fix.
