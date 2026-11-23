# Proven capacity, 80-agent existing-fleet check (owner, Nov 19, change 1; report only)

Two runs of D61 v2's dec11 twin config (a used window, 80 agents, D78 on): `pc80_dec11_off` (as the config) and
`pc80_dec11_on` (plus `proven_capacity`, no newcomers, existing assets' prior Beta(99,1)), at the same commit.

**Declared before the run:** the change-1 red test passes if every final peak price is within 1% and every agent's 1p
peak settlement within 5% of the off run, or $1, whichever is larger (the synthetic test's tolerance, stated after the
synthetic smoke and disclosed as such). The M2a $5 and D61 v2 checks remain the gate; they are not run here.
