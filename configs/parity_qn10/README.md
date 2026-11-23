# Q-N10 parity configs: `nomination_power_check` on (owner, Nov 19, proven-capacity change 3)

Byte-for-byte copies of `configs/parity/*.yaml`, each with one line appended:

    nomination_power_check: true

Compared with `../pearl-street-cloud/runs/cloud/_par_main`, as the M5 flag parity was.

**Declared before the run (Nov 19):**
- **Expected trace differences:** the `idle_reference` lines the flag adds (one per asset per call, from the metering
  service). Parity is judged on the trace with those lines removed: every other line text-identical, the same count.
- **Expected header differences:**
  - `nomination_power_check` (new);
  - `register_power_check` and `reference_power_cap` (on by default since D102, recorded in every header);
  - `nomination_span` (judged Nov 6).
- **Pass:** no `over_power_nomination` reject, and the stripped traces are identical in all 11. If identical, a decision
  entry proposing default on is staged (like D102); the owner decides.

Not a pre-registration.
