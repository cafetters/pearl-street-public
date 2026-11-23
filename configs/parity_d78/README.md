# D78 default-on parity: `silent_reference` on (owner, Nov 20: "Good to run these when ready")

Byte-for-byte copies of `configs/parity/*.yaml`, each with one line appended:

    silent_reference: true

Nothing else changes; `nomination_power_check` stays off. Compared with `../pearl-street-cloud/runs/cloud/_par_main`,
as the M5 flag and Q-N10 parity runs were. All 11 configs use price mode and rule 1p, which D78 requires; the
exchange has no guard against D78 with the D62 guarantee (8 configs) or the max rule (jan12).

**Declared before the run (Nov 20, evening), judged by `bench/d78_parity.py`:**
- **Expected trace differences:**
  - D79's `selfish_reference` lines for non-binding calls that run price rounds, once per asset per call. Their
    count must be exactly (assets with a binding-call reference) x (non-binding priced calls). In jan16's reference
    run that is 80 x 2 = 160.
  - The metering service's numbered message ids (`metering:<n>`). D79's lines share its counter, so every later id
    shifts by the lines before it. The judge replaces each `"metering:<digits>"` with `"metering:N"`.
  - The header line and the `run_id` field.
- **Expected header and summary difference:** `silent_reference` in the config. Not judged.
- **Pass:** in all 11, every exchange and selfish trace line is identical once the above are set aside, and the count
  of D79 lines is exact.
- **What a FAIL would mean:** D78 changes the market only where an agent has no nomination for a step of a priced
  call. Flag off, the zone price there freezes; flag on, the agent counts at its 1p reference, so prices, imbalance
  bases and the delivery record can change. Any such difference is reported as a finding, with the steps and agents,
  and not set aside.
- **If all 11 pass:** a decision entry proposing D78 (`silent_reference`) default on is staged, and with it Q-N10's
  check default on (D107); the owner decides.

**The judge on a real saved trace (before the run):** on jan16's reference trace with the D79 lines simulated (160
lines inserted, later metering ids shifted), PASS; with one nomination changed by 0.001 kW, FAIL; with no D79 lines,
FAIL on the count.

Not a pre-registration.

Run: `python -m bench.cloud_queue --run configs/parity_d78/<name>.yaml runs/m0_<w> [...] --commit <commit>
--concurrency 6 --budget 13 --measured-runtime-s 2400 --queue-id d78par`, then `python -m bench.d78_parity runs/cloud
../pearl-street-cloud/runs/cloud/_par_main <name> [...]`.

## Result (Nov 21, 01:52 ET): PASS, 11 of 11 [sim, in-run]

All 11 ran at `7fb355b` with `silent_reference: true` in every exchange header, judged by `bench/d78_parity.py` against
`_par_main`. In every run the exchange and selfish traces are identical once the declared differences are set aside,
and the exchange trace's D79 lines are exactly 160 = 80 assets x 2 non-binding priced calls (selfish traces: 0 of 0).
Judge output: `runs/m5/d78_parity_judge.txt` (git-ignored).

- **What this shows:** turning D78 on changes nothing else in these 11 windows: wherever D78's silent path could act,
  it left every price, base and settlement as it was (whether it fired at all was not counted). The parity is a
  no-regression check; D78's effect where an agent is silent is covered by the tests and HW9 item 5, not by these
  runs.
- **Launch:** queue `d78par`, concurrency 6. The low-memory reaper killed the launcher at about 23:42 ET (Nov 20)
  with 7 runs downloaded. The other 4 (dec07, mar08, nov28, oct04) finished on their sandboxes and were fetched by
  `runs/m5/rescue/rescue.py` (sha256 checked; DONE marked "rescued"). No relaunch.
- **Next:** the staged proposal (D109, `docs/private/m5_proven_plan_updates.md`). The owner decides.
- **Outputs moved (Nov 21):** to `runs/cloud/_d78_parity/` (for the combined run's names); rerun the judge with
  `runs/cloud/_d78_parity` as `<new_root>`.
