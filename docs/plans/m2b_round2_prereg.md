# M2b round 2 pre-registration (owner, Nov 19)

**Status: pre-registered.** The owner approved round 2 on Nov 19, only with the data-center agent change (item 4)
included. Their answers to the item 4 plan are recorded below. This file is committed and pushed before any round 2
run. It supersedes `m2b_round2_prereg_DRAFT.md` (`d8975c4`), which is kept for the record.

**If round 2 fails, M2b is cut** and the herding finding is reported as a result. Deadline Nov 24.

## Why a round 2

- Round 1 (`docs/overnight/2026-11-19-m2b.md`, judged at `18c25eb`) is recorded as FAIL as is.
- With deferral on, the exchange's peak was 280 to 382 MW above its deferral-off twin in all four design windows.
  Two build defects were fixed after the verdict:
  - `6b9d65a`: data centers hold their workload on bound steps;
  - `97c4b4c`: the power ceiling includes site room.
- The cause of the herd: each data center planned its deferral in a separate LP, linear in price plus peak price, with
  no prox term. So every price round moved its whole workload to the cheapest steps.

## The agent change (item 4, owner, Nov 19)

- **In the exchange (`dc_joint: true`, price mode only), deferral is planned jointly with the battery** in the agent's
  own LP (`agents/planner.solve(dc=...)`, `sim/dc_defer.rows_from`).
- Served work is a variable inside the agent's site net, so it is nominated, priced at the peak price, settled under 1p,
  and under the same lock, imbalance, POI limit, prox term and D58 anchor as battery relief.
- **The no-rebound rule** (owner, answer 1): served workload is part of site net, so the lock, imbalance, POI limit,
  prox term, D58 anchor and D57's report all cover it. There is no D34 cap, and no config turns `no_rebound` on.
- **Acting alone and the 1p reference keep selfish deferral** (`dc_defer.schedule`, herding included; D100
  unchanged).
- **Built red first** on `m2b-dc`:
  - `6d8bd20`: red tests;
  - `26cb589`: the joint planner;
  - `26f4ec5`: `bench/m2b_peak_check.py`;
  - `fe16038`: the round 2 judge.
- **Synthetic herd** (`tests/test_dc_joint.py`), the participants' peak:
  - off twin 30.5 kW;
  - separate deferral 454.1 kW;
  - joint 26.4 kW.

## Gates before any round 2 run (in this order, on Maritime)

1. **Full pytest** at the code commit, in a sandbox: 0 failed.
2. **Item 4's red test on round 1's windows.** jan12, jan06, feb02 and jun29 are rerun at the code commit, deferral
   on with `dc_joint: true` (`m2bj_<w>_on`), each with its deferral-off twin (`m2bj_<w>_off`).
   - Each config is round 1's with only `run_name` and `dc_joint` changed.
   - Check: `bench.m2b_peak_check --measure full`, exchange `peak_mw` on at most off + 1 MW in all 4 windows.
   - On the saved `18c25eb` runs, the same check fails all four windows (+381.848, +381.464, +378.206 and
     +279.567 MW). That is its real-trace check.
   - **If any window fails, round 2 does not run.** The finding goes to the owner.
3. **Flag-off parity:** the 11 `configs/parity/` configs at the code commit, against
   `../pearl-street-cloud/runs/cloud/_par_main` (`bench/parity_check.py`, all five checks). This is (d) for round 2.
   - The parity reference is pinned to main as last merged into `m2b-dc`: `4fc7f22`, merge `88552f1`.
   - Main is not merged into `m2b-dc` again before round 2 ends. Main now has M5's default-on flags.

The gate results are recorded in a follow-up commit to this file, before the first round 2 run.

## Commit

- **Runs execute at the commit that records the gate results.** That commit changes only `docs/` on top of the code
  commit, `fe16038`, so the gates' pytest and parity cover its code.
- **No code change after the first round 2 run counts.** A crash fix restarts the round.

## Windows

The 19 M2a $5 windows not used in round 1:
- apr16, aug26, aug28, dec07, feb12;
- jan02, jan16, jan31;
- jul01, jul05, jul14, jun19;
- mar08, may29, nov14, nov28;
- oct04, sep18, sep21.

**Holdout check, by date:**
- For each window, every day from the config's `start` to its `end` was checked against every day of the 24 windows in
  `docs/plans/holdout_windows.md` (48 days, parsed from its table).
- No day is shared.
- None of the 19 is a round 1 window.

## Runs

**Configs** (git-ignored, `runs/m2b/q/`):
- `m2b2_<w>_{on,off,att}.yaml` copy `configs/nov01/h2h_c_<w>_h100_m5.yaml`. Only `run_name` changes, and these keys are
  added:
  - all three: `nomination_span: horizon`, `harm_unrounded: true`, `peak_steps: controllable`;
  - on: `dc_deferral: true`, `dc_joint: true`;
  - off: `dc_deferral: false`;
  - att: as on, plus `dc_declare_tighter: [<attacker>]`.
- **Attacker by rule:** the data center with the most flexible energy in the window (ties to the first id). That is
  DC_08 in all 19 windows (`runs/m2b/q/m2b2_attackers.json`).
- **Ceilings:** `m2b2_ceil_<w>.yaml`, for the 23 windows: the 19 above plus round 1's four. They are copies of round 1's
  ceiling configs with only `run_name` changed, run with `bench.m0_dc` at the run commit, so the site-room ceiling of
  `97c4b4c` applies.

**Queue:** one launcher, `--snapshot pearl-base-full`, concurrency 10:
- 57 runs (about $0.55 at Maritime's measured rate);
- 23 ceilings (about $0.05).

## Pass test (unrounded; rosters from run headers; any missing agent, field or run fails closed)

The judge is `python -m bench.m2b_judge --round 2 --cloud runs/cloud --parity <parity.json> --pytest <pytest run>`.

- **(a) Zero deferral-rule violations** for every data center, in every window's deferral-on run, exchange and acting
  alone:
  - no session late;
  - window energy unchanged;
  - served work never above the Scope ceiling: the lower of the session's `charge_power_kw_max` and the site room
    `dc_capacity_kw - (load_kw - dc_flexible_kw)` (1e-6 guards).
- **(b) Section 8 and the bystander line.** In every window's deferral-on run:
  - section 8's net-harm check passes with a complete roster;
  - D98's bystander line against the deferral-off twin passes (newly below at a $1e-6 guard; further below judged
    beyond section 8's tolerance, D91).
  - **As written** (owner): a window whose off twin also fails section 8 still fails (b). The off twin's section 8
    result is reported beside it as context.
- **(c) The ceiling with deferral** solves on all 23 windows (both variants), with its peak at or below the
  battery-only ceiling's + 1e-3 MW. Shares and gap shares are reported against both ceilings.
- **(d) Flag-off parity** is identical on the 11 configs (gate 3).
- **(e) The data centers' share of relief** is reported (runs and ceilings).
- **(f) The deferral-on run doesn't raise the peak** (owner, answer 2). In every window, the exchange's
  controllable-step peak (D57, `peak_steps: controllable`) with deferral on is at most its deferral-off twin's + 1 MW.
  The full-slice peak is reported beside it.

**Verdict:** PASS only if (a) to (f) all hold.

## Report only

- **Acting alone with deferral against acting alone without, per window** (owner, answer 3): the herding finding, full
  slice and controllable.
- **Binding-price coverage:** of the exchange's 12 highest-load controllable steps, how many carry a nonzero settled
  peak price, on and off. From each run's trace and summary (`price_mode.settled`).
- **Imbalance charges, on and off;** data centers' share.
- **`pre_from_rebound`** (D57).
- **The D100 (c) attack:**
  - the attacker's gain against its honest twin;
  - the 1p references compared;
  - the bystander line for everyone else.

## After round 2 (owner, answer 4)

- If round 2 passes and `m2b-dc` merges into main, main will include M5's default-on flags.
- Round 2's verdict stands for its pinned commit.
- After the merge, the 11 parity configs and one round 2 window are rerun on the merge commit, and any differences are
  reported.

## Not in this round

- Any spreading cost for the acting-alone data center's herd: acting alone keeps the price-taker's herd, which is the
  finding.

## Gate results (Nov 20, 00:38 ET): gate 2 FAILS, so round 2 does not run

All gates ran at `c28af16` on Maritime.

1. **Full pytest:** 1,174 passed, 0 failed, 14 skipped (queue `pt-m2b-c28af16`, $0.0031).
2. **Item 4's red test on round 1's windows: FAIL in all four** (`bench.m2b_peak_check --measure full --prefix
   m2bj_`). Exchange `peak_mw`, deferral on with the joint planner minus its off twin, against the 1 MW band:

   | Window | On (MW) | Off (MW) | On - off (MW) | Round 1 on - off (MW) |
   |---|---|---|---|---|
   | jan12 | 2,322.715 | 2,321.353 | +1.362 | +381.848 |
   | jan06 | 2,469.987 | 2,320.847 | +149.140 | +381.464 |
   | feb02 | 2,179.804 | 2,174.909 | +4.894 | +378.206 |
   | jun29 | 2,325.039 | 2,323.532 | +1.507 | +279.567 |

   - The off twins are identical to round 1's off runs (same peaks), as flag-off code should be.
   - Zero deferral-rule violations in all four on runs, exchange and acting alone.
3. **Flag-off parity:** 11 of 11 PASS against `_par_main` (`runs/m2b/q/parity_all.py c28af16`).

**Diagnostic, not a pre-registered measure** (`runs/m2b/q/gate_diag.py`, from each run's `netload.csv`). Exchange on
minus off:

| Window | Before the first binding call (MW) | Controllable steps, D57 (MW) |
|---|---|---|
| jan12 | +145.5 | +1.4 |
| jan06 | +159.6 | +3.1 |
| feb02 | +68.5 | +4.9 |
| jun29 | +125.6 | +1.5 |

- Acting alone, controllable, with deferral against without: +41.1 to +159.7 MW.
- So the joint planner removes almost all of the herd on the priced steps. What is left:
  - deferred work served before the first binding call (00:00 to 04:00 local on day 0), where no peak price reaches
    it. In jan06 that step is the whole-slice peak;
  - +1.4 to +4.9 MW on the controllable steps, at 21:45 to 02:00 local on day 2, where deadlines are clipped to the
    window's end.

Gates queue `gates19-m2b-c28af16` (19 at once, owner-approved restart): $0.1963; the stopped first attempt at 10 cost
$0.0726. Under this pre-registration, round 2 does not run. The owner decides what follows.
