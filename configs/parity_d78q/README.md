# Both proposed defaults together: D78 (`silent_reference`) and Q-N10's check (`nomination_power_check`)

Owner, Nov 21: "run any tests that are no regret" (while offline). D109 (D78 default on) and D107 (Q-N10's check
default on) are proposed together (`docs/private/m5_proven_plan_updates.md`). Each passed its own parity, 11 of 11,
with the other flag off (`configs/parity_d78/`, `configs/parity_qn10/`). This runs them together, as they would ship.
Test only; nothing changes a default.

Byte-for-byte copies of `configs/parity/*.yaml`, each with two lines appended:

    silent_reference: true
    nomination_power_check: true

**Declared before the run (Nov 21, 02:15 ET), judged by `bench/d78_parity.py --idle-root runs/cloud/_qn10_parity`:**
- **Compared with:** `../pearl-street-cloud/runs/cloud/_par_main` (flag off), as both earlier parities were.
- **Set aside, as in the D78 parity:**
  - D79's `selfish_reference` lines for non-binding priced calls, with the exact count (assets x non-binding priced
    calls; 160 in each of the 11);
  - the metering ids `metering:<digits>`, which shift by D79's lines;
  - the header line and the `run_id` field.
- **Set aside, as in the Q-N10 parity:** the `idle_reference` lines. They must equal, line for line (without
  run_id), the lines of the Q-N10 parity run at `e1fda32` for the same config (`runs/cloud/_qn10_parity/<name>`; 560
  in jan16). Their ids have their own sequence (`metering:idleN`), so they shift nothing.
- **No `over_power_nomination` reject** may appear.
- **Pass:** all of the above in all 11 runs, for every exchange and selfish trace.
- **What a FAIL would mean:** the two flags interact. For example, a nomination the check refuses leaves the agent
  silent, and D78 then counts it at its reference. Any difference is reported with its steps and agents, not set
  aside, and D107/D109 would not be proposed together as they stand.

**The judge on real saved traces (before the run):** on jan16, the Q-N10 parity run judged as if it were the combined
run FAILs on the D79 count (0 vs 160) with its idle lines identical (560); the D78 parity run FAILs on the idle lines
(0 vs 560) with everything else identical. Unit tests: `tests/test_d78_parity.py` (5 passed).

Not a pre-registration.

Run: `python -m bench.cloud_queue --run configs/parity_d78q/<name>.yaml runs/m0_<w> [...] --commit <commit>
--concurrency 6 --budget 13 --measured-runtime-s 2400 --queue-id d78q`.

## Result (Nov 21, 02:47 ET): PASS, 11 of 11 [sim, in-run]

- **Timing correction:** the line above says "declared before the run (Nov 21, 02:15 ET)". The declaration is commit
  `7a23b57` at 02:05:53 ET, and the queue launched at 02:06:45 ET, so it was declared before the run; "02:15" was a
  typo.
- **Runs:** all 11 at `7a23b57` with `silent_reference` and `nomination_power_check` true in every exchange header.
  Queue `d78q` finished normally (no reaper), $0.07. Outputs were moved to `runs/cloud/_d78q_parity/`.
- **Judge:** `bench/d78_parity.py --idle-root runs/cloud/_qn10_parity`, against `_par_main`. In every run:
  - every exchange and selfish trace is identical once the declared differences are set aside;
  - D79 lines: exactly 160 (80 x 2);
  - `idle_reference` lines: 560, identical to the Q-N10 parity run's;
  - `over_power_nomination` rejects: 0.

  Judge output: `runs/m5/d78q_judge.txt` (git-ignored).
- **What this shows:** in these 11 windows the two flags don't interact; together they change nothing but their
  declared lines. As in each single-flag parity, the check refused nothing here, and whether D78's silent path fired
  was not counted. Their interaction where an agent is refused is **not covered**: no run here refuses anyone, and no
  test sets both flags (`tests/test_q_n10_power_check.py` leaves `silent_reference` off). A test of a refused agent
  counting at its reference under D78 is a gap to close before D107 and D109 are adopted together.
- **Gap closed (Nov 21, 03:00 ET):** `tests/test_q_n10_power_check.py::test_a_refused_agent_counts_at_its_reference_only_with_d78`
  (synthetic world, the over-nominator refused every time). With the check on and D78 off, no price forms: every
  call's final peak price is 0. With D78 on, every call's final price is above 0, and the refused agent is charged
  imbalance against its reference. It pins today's behaviour; no code changed.
