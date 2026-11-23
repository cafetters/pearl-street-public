# Proven capacity, step (a): does an over-nominator move arm C's price? (Nov 19, report only)

**Label:** [sim, synthetic].
- **World:** one day (`redteam/synth.py` `synthetic_slice`, cheap_peak prices), arm C, rule 1p, the $5 margin,
  `delivery_record`, and D78's `silent_reference` on.
- **Fleet:** L 100 kW / 400 kWh, H1 1,000 kW / 4,000 kWh, H2 500 kW / 2,000 kWh.
- **The attack:** L is `attacks/over_nominate.py` with factor 3. Every nomination L sends adds 200 kW of export
  (relief) beyond its honest plan, while its battery runs the honest plan.
- **Reproduce:** `runs/m5/proven/verify.py` (git-ignored); outputs `runs/m5/proven/verify_d78.json` and
  `verify_check.json`.
- **Measures:** `bench/firmness.py`, read from the traces.

## Result

| Run | Final peak price per call ($/MWh) | 1p peak settlement L / H1 / H2 | Counted minus delivered, max |
|---|---|---|---|
| Honest twin | 32.56 / 33.90 / 34.79 | $6.06 / $44.93 / $23.03 | 0 kW |
| L over-nominates (today) | 28.53 / 24.96 / 32.18 | $3.93 / $38.29 / $19.64 | 200 kW |
| L over-nominates, `nomination_power_check` on (Q-N10) | 34.38 / 32.59 / 33.09 | $4.02 / $43.51 / $24.25 | 0 kW |

**The mechanism exists.**
- The over-nominator's promised relief lowers the zone total the price loop sees. So the final peak price falls, by
  12%, 26% and 8% on the three calls.
- The honest agents are paid less for the same response: H1 gets $6.64 less (−15%) and H2 $3.39 less.
- The operator counts 200 kW of relief that never arrives (the whole lie).
- In the same run without D78 (recorded earlier the same evening), the metered fleet stayed above the operator's
  target on 4 steps, against 0 in the twin.
- **Crowd-out of the honest response itself is small here.** The honest agents' window energy moved by under 2%, so
  in this world the harm shows up mainly as **lower pay for the same response** and **uncounted reliance**.

**With the Q-N10 check on:**
- every inflated nomination is refused (`over_power_nomination`), and no inflated relief is counted;
- L counts at its 1p reference (D78), so the prices are close to the twin's but not equal: up to $1.84/MWh away;
- H1 is $1.42 below its twin and H2 $1.22 above.
- **The residue:** the check refuses L's whole nomination, so L's honest relief isn't counted either. The proven-capacity
  counting (plan steps 2 to 4) bounds what the check can't see: a claim the battery could physically give but won't.
- **Without D78, a refused agent freezes the price loop** (no price, no settlement), as HW9 item 5 found. Under
  `nomination_power_check`, D78 should be on.

**Limitation (j).** The simulation's external grid absorbs any gap. This reports the gap the operator would have to
cover, not whether the grid keeps up. One synthetic day; not shown at scale.
