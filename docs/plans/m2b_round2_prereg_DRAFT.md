# M2b round 2 pre-registration: DRAFT for the owner (Nov 19, overnight)

**Status: DRAFT. Nothing here runs until the owner approves it.** Round 1 (`docs/overnight/2026-11-19-m2b.md`, judged
at `18c25eb`) did not pass and found two build defects. Both are fixed on `m2b-dc`, red first, but not run on real
data:
- `6b9d65a`: data centers keep their workload on bound steps.
- `97c4b4c`: the power ceiling includes site capacity.

Round 1's four design windows (jan12, jan06, feb02, jun29) are seen, so this round judges on windows not yet run with
deferral.

## Commit

The `m2b-dc` head at approval, at or after `97c4b4c`, with full pytest green in a sandbox first. No code change after
the first run counts; a crash fix restarts the round.

## Windows

The 19 M2a $5 windows not used in round 1:
apr16, aug26, aug28, dec07, feb12, jan02, jan16, jan31, jul01, jul05, jul14, jun19, mar08, may29, nov14, nov28, oct04,
sep18 and sep21.

- Same configs as round 1: `configs/nov01/h2h_c_<w>_h100_m5.yaml` with only `run_name`, `nomination_span: horizon`,
  `harm_unrounded: true` and the M2b keys set.
- Three runs per window (deferral on, the off twin, the DC_08-rule attack): 57 runs, about $0.55 at Maritime's measured
  rate.
- Ceilings on all 23 windows: about $0.05.
- A smaller set is an option for the owner: one window per month, 12 windows.

## Pass test

Round 1's (a) to (e) carry over, with two changes for the owner to accept or strike:

1. **(a)'s ceiling clause becomes the plan's Scope ceiling:** served work never exceeds the lower of the session's
   `charge_power_kw_max` and the site room `dc_capacity_kw - (load_kw - dc_flexible_kw)` (1e-6 kW guard).
2. **(b) on a window whose deferral-off twin already fails section 8:** the owner chooses one.
   - (i) Keep (b) as written: the window fails.
   - (ii) Judge only the D98 line against the twin there, with section 8's failure reported. jan12 was such a window
     in round 1; whether any of the 19 is one is not known in advance.

**Added as report only:**
- Exchange peak with deferral against its deferral-off twin, per window. It is the system result round 1 found worse
  by 280 to 382 MW.
- Binding-price coverage of the 12 highest-load steps, on and off.
- The data centers' imbalance charges.

**Judge:** `bench/m2b_judge.py`, with the window list and (a)'s ceiling updated before any run, red first, and
committed with this file.

## Not in this round

- The joint battery-plus-deferral planner with a prox term.
- Any spreading cost for the reference data-center agent's herd.

Both stay owner questions (round 1 report, items 4 and 5).
