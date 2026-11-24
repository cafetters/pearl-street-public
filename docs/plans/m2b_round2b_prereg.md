# M2b round 2, revised (owner, Nov 20): pre-registration

**Status: pre-registered; judged FAIL (Nov 20, 21:20 ET). M2b is cut.**
- (b) fails in 11 of 19 windows and (f) in 4 (apr16, aug26, dec07, mar08). (a), (c), (d), (e) and (g) pass.
- Report: `docs/overnight/2026-11-20-m2b-round2.md` on main. Verdict file `runs/m2b/verdict_r2b.json` (git-ignored).
- This note is docs-only and changes nothing below.

- On Nov 20 the owner approved the three tests proposed in `docs/overnight/2026-11-20-m2b-analysis.md` (on main).
- This file revises `m2b_round2_prereg.md` (`c28af16`):
  - its gate 2 (the 1 MW test on round 1's windows) failed and is recorded there;
  - its item (f) is replaced by (f) and (g) below.
- It is committed and pushed before any run of this round, the ceilings included.
- **If this round fails, M2b is cut** and the herding finding is reported as a result. Deadline Nov 24.

## Why the revision

The analysis (report only, 16 runs at `c28af16`) found three things:
- **At a 100 MW cut, deferral has nothing to do.** Batteries alone reach the operator's target, and arm C prices only
  load above it. So deferral on and off both land on the target.
  - Without deferral, the price rounds miss their target by up to 5.97 MW (26 run-days where batteries can reach it).
  - A 1 MW test between two runs measured that precision, not flexibility.
- **At a cut only batteries plus deferral can reach,** the controllable peak with deferral was 12.9 to 99.2 MW lower
  in 5 of 8 runs.
- **The operator pays data centers more with deferral,** mostly through the D100 acting-alone baseline.

## Commit and gates

- Code is at `483f0bc` (the revised judge). Runs execute at the commit that adds this file, which changes only
  `docs/`.
- **Full pytest** at that commit runs in a sandbox with the ceilings: 0 failed, or the round stops.
- **No code change after the first run counts.** A crash fix restarts the round.
- Main is not merged into `m2b-dc` before the verdict.

## Windows

The 19 windows of `m2b_round2_prereg.md`, unchanged, with the same holdout check by date (no day shared with
`docs/plans/holdout_windows.md`):
- apr16, aug26, aug28, dec07, feb12;
- jan02, jan16, jan31;
- jul01, jul05, jul14, jun19;
- mar08, may29, nov14, nov28;
- oct04, sep18, sep21.

## Runs

All at the run commit, `--snapshot pearl-base-full`, through main's launcher (`runs/m2b/q/launch_main.sh`).

1. **Ceilings, 23** (`m2b2_ceil_<w>`, `bench.m0_dc`): the 19 windows plus round 1's four. Each gives battery-only and
   with-deferral ceilings, cold and controllable.
2. **100 MW cut, 57 runs**, `m2b2_<w>_{on,off,att}`, configs as in `m2b_round2_prereg.md`:
   - `peak_steps: controllable`;
   - on: `dc_joint: true`;
   - att: also `dc_declare_tighter: [DC_08]`.
3. **Depth cut, 38 runs**, `m2b2_<w>_{don,doff}`: copies of the on and off configs with only `run_name` and
   `headroom_mw` changed. The headroom is set mechanically after the ceilings:
   - h_d = (the operator's highest daily forecast peak) - (the with-deferral **controllable** ceiling), rounded to
     0.1 MW;
   - the forecast peak is `sim.forecasts.operator_forecast` over each day's steps from its first gate
     (`sim.run_m1.operator_schedule`);
   - so the highest daily target equals the with-deferral ceiling.
   - The depths are recorded in `runs/m2b/q/m2b2_depths.json` and in the report.
4. **Flag-off parity:** the 11 `configs/parity/` configs at the run commit, against `_par_main`.

About 106 runs plus 23 ceilings, about $1.0, as many at once as the account's 60 allow.

## Pass test (unrounded; rosters from run headers; any missing run, agent or field fails closed)

The judge is `python -m bench.m2b_judge --round 2 --cloud <dir> --configs runs/m2b/q --parity <parity.json> --pytest
<pytest run>`.

- **(a) Zero deferral-rule violations** for every data center, in every deferral-on run (100 MW and depth), exchange and
  acting alone:
  - no session late;
  - window energy unchanged;
  - served work never above the lower of the session's `charge_power_kw_max` and the site room (1e-6 guards).
- **(b) At 100 MW: section 8 and the bystander line.** Section 8's net-harm check passes with a complete roster, and
  D98's bystander line against the deferral-off twin passes. As written: a window whose off twin also fails section 8
  still fails (b); the off twin's result is reported beside it.
- **(c) The ceilings** solve on all 23 windows, with deferral at or below battery-only + 1e-3 MW.
- **(d) Flag-off parity** is identical on the 11 configs.
- **(e) The data centers' share of relief** is reported.
- **(f) Do no harm at 100 MW, every window.** The controllable-step peak (D57) with deferral on is at most the larger
  of:
  - the operator's highest daily target (its forecast's maximum over the day's steps from its first gate, minus
    100 MW: `bench.m2b_judge.day_targets`);
  - the off twin's controllable peak;
  plus **6.0 MW**, the market's measured precision (worst no-deferral miss 5.97 MW over the 26 reachable run-days of
  the Nov 20 gate runs, `runs/m2b/target_miss_exact.json`).
- **(g) Depth: deferral lowers the peak.** At the depth cut, in every window whose controllable ceilings differ by more
  than 6.0 MW, the controllable-step peak with deferral on is strictly below the off twin's.
  - Windows with a gap of 6.0 MW or less are reported, not judged.
  - At least one window must be judged, else (g) fails.

**Verdict:** PASS only if (a) to (g) all hold.

## Report only

- **The payout split per window** at 100 MW, on and off (`peak_settle`): data centers and everyone else, refunds and
  charges. This is the D100 baseline finding. Changing D100 is a separate owner decision.
- **The superseded test** (100 MW, on within 1 MW of off) and the full-slice peaks.
- **Acting alone with deferral against without** (the herding finding).
- **Imbalance charges, `pre_from_rebound`.**
- **The D100 (c) attack:** gain against the honest twin, the 1p references, and the bystander line.
- **The depth runs:** peaks, gains and net harm.

## After the verdict (owner, Nov 19 answer 4)

- If this round passes and `m2b-dc` merges into main (which then has M5's default-on flags), the verdict stands for
  its pinned commit.
- After the merge, the 11 parity configs and one round 2 window are rerun on the merge commit, and any differences are
  reported.
