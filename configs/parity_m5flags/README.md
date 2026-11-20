# M5 parity configs: both power flags on (owner, Nov 18, item 1)

Byte-for-byte copies of `configs/parity/*.yaml`, each with two lines appended:

    register_power_check: true
    reference_power_cap: true

These are compared with `../pearl-street-cloud/runs/cloud/_par_main` by `bench/parity_check.py`.

**Expected gated summary differences:**
- `/header/config/register_power_check` and `/header/config/reference_power_cap`. These are the new keys. Judging
  them PASS is the owner's call, not pre-judged.
- `/header/config/nomination_span`, pre-judged PASS since Nov 6.

`parity_check` itself exits FAIL on these.

**Result (Nov 19, Maritime, `13544e4`):** in all 11, telemetry, trace text, payout and commit are ok, and the summary
differs only in these three keys.

**Why the runs can't move:** in all 11 references, every register's `max_kw` equals its `asset_limits` `power_kw`
(880 of 880).

Not a pre-registration.
