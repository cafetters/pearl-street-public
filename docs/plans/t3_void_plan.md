# T3-c build plan: widen the D81 void to every reference a breached declaration touched

**Status (Nov 6 evening): approved (D87) and built on branch `t3c`, with the rule corrected below and the owner's four
changes.** The text after this box is the earlier draft, kept as written.

> **Correction (found in plan mode, Nov 6).** The draft rule required "the call's window holds a step of day d". That
> condition is wrong.
> - The metering service solves 1p once over the whole run from the slice start, with every declaration in force, and
>   cuts out the call's window (`sim/run_m1.py`).
> - So a declaration for any day enters every reference computed after it. Under `operating_day` a day-1-only call is
>   still moved by a day-2 declaration.
> - As built: replace a stored reference on all its steps when a declaration or forced outage for a breached day was
>   in force when the reference was filed (`Exchange.days_in_force`, recorded per reference as `call_ref_days`). No
>   window condition.
> - T3-a re-run with the corrected rule: identical in all 11 D61 cells (horizon traces).
>
> **Owner's changes, as built:**
> 1. A consistency test: the recorded days equal what `declared_limits` gave the metering service, in harness cases
>    and in two full synthetic runs (one with a D82-rejected filing, which counts on neither side, and a shared
>    `t_utc`).
> 2. D87 restaged with the corrected rule.
> 3. Silent agents: the closing-tick replacement does not change a D78 imbalance base (fixed at the binding
>    aggregate and charged before the void), and prices are never recomputed. Pinned by a test.
> 4. sep05 checks (a) and (c) gate the merge, with (b) report only. (a) is a consistency check between the in-run code
>    and `bench/t3_void.py`, not independent evidence.

Plan only (Nov 6, unattended day block, item 3). Nothing here is built. It needs the owner's approval of the staged
D81-revision entry (`runs/nov06/plan_updates.md`, proposed D87) and plan mode before code (CLAUDE.md).

## Why

- **Evidence** [sim, offline]: `bench/t3_void.py` on D61's traces (`74b1ae0`).
  - It reproduces every in-run `peak_settle` within $0.0000049 per agent.
  - In all 6 gaining misdeclare cells (3 at $5 under today's rule and their 3 max-rule reruns; jan12 power and reserve,
    jan06 reserve), the attacker's whole gain is peak settlement on the day that was **not** voided.
  - Gain by day, attack minus twin: jan12 power $87.91 on Jan 12, $0 on Jan 13 (voided).
- **The mechanism.** The calls run to the end of the run window (`nomination_span: horizon`), so one 1p plan spans both
  days. A declaration for day 2 changes that plan's day 1 too. `_void_declarations` (exchange/core.py:786) re-reads only
  the breached day's steps.
- **Suspect (b) is ruled out.** Peak settlement is paid once, at the closing tick, after the void. 0 `peak_settle`
  messages were sent before the final tick in any of the 17 runs read (11 attack cells, 6 twins), so nothing is paid before the void.
- **T3-a** [sim, offline]: with every reference computed with a breached declaration replaced on all of its steps:
  - the attacker's gain is at most $6.3e-12 in all 11 cells;
  - every honest agent is unchanged ($0.0 exactly);
  - every twin is unchanged ($0.0).

## The rule (proposed wording, in D87)

At the closing tick, for each asset with a breach on local day d:
- Every stored reference for that asset is replaced on **all** of its steps by its `net_kw_registered` when both hold:
  - the call's window contains a step on day d;
  - a declaration for day d had been accepted at or before the time the reference was filed.
- Today's day-d replacement is kept for every other reference, unchanged.
- The rule is the same under today's 1p and under `max_first_call`, before either settlement rule reads the references.

## Code (behind `declared_reference`; the flag stays the only key)

1. **`exchange/core.py`**
   - `_on_selfish_reference`: under the flag, keep the reference's filing time per (call, asset):
     `self.call_reference_t[cid][asset] = msg["t_utc"]`. No new message field, since the time is the message's.
   - Accepted declarations keep `t_utc` as today. A filing that D82 rejects is never stored, so it can't enter any
     reference and isn't counted. A declaration accepted and later voided by an outage (D82's amendment) still
     counts from its filing time: it was in force when earlier references were computed.
   - `_void_declarations`: after the per-day loop, for each breached asset, for each call with a registered reference,
     replace all steps when the window holds a breached day d whose declaration (any filing for d,
     `self.declared[asset][d]` plus the D82 voided list) was filed at or before `call_reference_t`. Record
     `self.voided_calls[asset] = sorted(cids)`.
   - `delivery_record`: add `voided_calls` under the flag only (same place as `voided_days`, `exchange/core.py:1312`).
2. **`max_first_call` path.** No separate code. `_peak_settle_max` reads `self.call_reference` after the void, as
   today, so every call in a step's chain sees the replaced reference. Covered by test 4 below.
3. **Nothing in `sim/` or `agents/`.** The metering service already sends `net_kw_registered` on every reference
   (D81).

## Tests (red first)

1. `tests/test_t3_void.py::test_after_a_breach_the_exchange_settles_the_attacker_like_its_twin` is today's strict
   xfail. It goes green and the marker is removed.
2. The existing D81 tests in `tests/test_d64_declared_reference.py` stay green unchanged. A one-day window can't
   tell the two rules apart.
3. **New.** A reference filed **before** the breached day's declaration is not replaced, because it wasn't computed
   with it. The same holds for a call whose window doesn't hold the breached day.
4. **New, max rule.** Two binding calls, both spanning days 1 and 2.
   - The attacker's day-1 settlement equals the twin's under `max_first_call`.
   - It is also checked against `bench.item7_resettle.max_first_call_steps`, as the existing max-rule D81 test is.
5. **New.** Honest agents' settlement is byte-identical with and without the widening in a run with a breach.
6. **Flag off.** The existing `test_flag_off_is_byte_identical...` test plus parity: the 11 parity configs (no
   `declared_reference`) must match `_par_main` exactly on Maritime.
7. **Real trace.** `bench/t3_void.py` must give, from a new in-run trace, the same attacker settlement as the
   exchange (the check stops being "offline only").

## Checks before any pre-registration (CLAUDE.md)

- Re-run D61's three FAIL cells and the three max-rule reruns in-run, on **new, unused windows only** (never D61's
  four). The D61 v2 draft (item 7) carries them as `misdeclare_power` and `misdeclare_reserve` cells, judged as in
  D61.
- `bench/t3_void.py` on those new traces: the in-run widened void must equal the offline T3-a figure within $0.001.

## Risks

- **Over-reach.** A truthful declaration for day d that is then breached by accident (forecast error) now voids
  references on other days too. The agent was already voided on day d; the widening only removes the cross-day
  benefit its declaration bought. Honest agents without declarations are untouched (T3-a).
- **The site-net-meter check** (D80/D81) is still not built. A breach that only the site meter would show isn't
  detected, so it isn't voided under either rule.
