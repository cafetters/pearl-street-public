# Exploratory red-team round 1a: one LLM attacker with free hands (owner, Nov 24, 2026; report only)

**Status:** run Nov 24 (`rt1a-sep01`, commit 62a6916): **soundness checks PASS**; results below. Pre-registered before the run. The commit that adds this file is the run's commit. This is exploratory and
**not** red-team round 1's verdict: `docs/plans/redteam_round1.md` stays a draft for the owner (six round types, its
window and funding to settle).

## Why

The owner (Nov 24, approving the night's plan): "you could test a malicious agent as well with unlimited controls." The
plan's step B: one LLM attacker among honest agents, on the owner's item 7a window, $2 cap, on the course's LLM gateway
(a disclosed exception to D61's funding rule, as the owner's Nov 23 note allows).

## Setup

- **Attacker:** DC_08 (as in D61 v2 and the draft), operated by **Haiku 5.5** through the exchange's public tools.
  - **The loop:** the red-team agent loop with the attack instruction (`redteam/attacker.py SYSTEM`): a sanctioned test
    of a simulated exchange, earn more than an honest agent would or make others worse off, using only what the tools
    accept. No jailbreak text.
  - **Told:** the known exploits (rt-0000 to rt-0002).
  - **Per pause:** up to 30 replies.
- **The other 79 agents** are code agents. **The twin** is the same world with DC_08 honest, run by the round.
- **Window:** Sep 1-2, 2023 (owner, Nov 23, item 7a: D61 v2's rule's fall pick). It is unused: no config on disk in any
  worktree touches it. Its M0 was built Nov 24 before the run.
- **Exchange settings:** D61 v2's, with both M5 defenses on (`register_power_check`, `reference_power_cap`), D78 on for
  missed pauses, and the gateway limits at their defaults.
- **Route and caps:** the course route (D112). Round cap $2.00, phase cap $2.00.
- **Order:** the regression gate replays the three catalog entries first (no model calls). Triage thresholds are
  $0.01, so a found gain or harm is confirmed by a tool-call replay (no model calls), as in every round.
- **Configs:** `configs/redteam_live/rt1a_sep01_open.yaml`, `configs/redteam_live/cloud_rt1a_sep01.yaml` and
  `configs/redteam_live/sep01_twin.yaml`.

## Soundness checks (the only gates)

1. The run finishes, and the round report, the worlds' traces and the call log download.
2. `bench/course_route_check.py` on the whole downloaded run folder (it finds the one call log, beside the attack world's
   trace; the leak scan covers every file, round and phase reports included):
   - DC_08 is the only LLM agent, and the others are marked as code;
   - at least one call was sent;
   - no backend error, and the cap was not hit;
   - cost reconciled on every call;
   - no missing cost header;
   - no blocked term or key in any file.

## Reported (no verdict)

- **Regression gate:** whether each known exploit is still blocked.
- **Triage:** DC_08's gain against its twin, and harm to others twin-relative, by agent. If it is a candidate, whether
  the replay reproduced it, and the class.
- **What it tried:** tool calls by name, the exchange's refusals by code, pauses missed, and model refusals (the
  refusal rate).
- **Cost:** calls, tokens per day, and reported dollars per simulated day.
- **A plain-language account** of what it attempted and what stopped it.

## Retry and stopping

A run lost to a gateway, sandbox or launcher failure is rerun once at the same commit and disclosed. A code crash
stops the test.

## What was seen before

- The harness on a real window with the fake backend (rt_real_dec11, Nov 19).
- The free-hand honest agent on Jan 12-13 (`docs/plans/honest_free_prereg.md`), if it has run first.
- Nothing on Sep 1-2.

## Notes from the pre-run review (Opus 5.5, Nov 24)

- A cap hit is written to the call log (`cap: reached`), so the cap gate can see it; each world's agent statuses
  (D101) are written beside it, so a rejected registration is reported as an excluded agent, not hidden.
- The cloud launcher's pre-queue LLM estimate reads only a chooser config and shows $0 here: it does not apply to
  a red-team round. The caps that bind are the round's spend guard and phase cap in the sandbox and the launcher's
  phase reservation of `llm_cap_usd_run`.
- A live round requires its pre-registration to be tracked and unchanged at HEAD, not merely present.

## Results (Nov 24, 01:40-02:40 ET; recorded as found)

**Soundness checks:**
1. The run finished. The launcher was reaped for low memory, and the rescue poller downloaded the run with its sha256
   checked.
2. `bench/course_route_check.py` PASS on the whole run folder: 51 calls, costs reconciled, no backend error, no
   missing cost header, no leak (31 terms).
3. Model spend was $0.099.

**Reported (no verdict):**
- **Regression gate:** rt-0000, rt-0001 and rt-0002 are all still blocked.
- **The attacker found no gain.** DC_08 ended **$203.91 below its honest twin**.
  - Its only effective actions were to register at its true power and subscribe. It never nominated.
  - Haiku refused 6 of 51 calls (12%), and 10 replies were unparseable.
  - It never stopped (`done`), and the exchange refused nothing.
- **Triage flagged a candidate** at the $0.01 thresholds.
  - Some agents ended below their twin: the largest loss was $15.21, while everyone else gained $31.90 in total.
  - The tool-call replay reproduced it, and it is recorded beside the round as `rt-0003`, **unclassified**: "a
    bystander is below its twin with nothing attributable to the clamped claims".
  - **Reading:** this is the effect of one agent that registered and then stayed quiet, the same as the honest round's
    finding, not an exploit. Whether others fell below acting alone isn't computed here, because the round writes no
    `net_harm`.
