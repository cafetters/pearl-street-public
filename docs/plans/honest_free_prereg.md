# Free-hand honest LLM agent: pre-registration (owner, Nov 24, 2026; exploratory, report only)

**Status:** run Nov 24 (attempt 2, `hf-jan12b`, commit 1003f0b): **soundness checks PASS**; results below. Pre-registered before the run. The commit that adds this file is the run's commit. This is exploratory:
there is no pass or fail on behaviour, only on whether the run itself is sound.

## Why

The owner (Nov 24): real-world LLM agents will not be limited to a posture label; anyone can point a model at the
exchange's public tools. Before an LLM attacker, test what an **honest** model does with the same freedom. "At least one
test of an honest LLM agent with full freedom", at the end of the night's plan.

## Setup

- **Agent:** PVB_33 (the single-LLM-agent runs' X), operated by **Haiku 5.5** through the exchange's public tools.
  - **Tools:** `my_asset`, `register`, `subscribe`, `read_inbox`, `read_public`, `nominate`, `declare_availability`,
    `declare_outage`.
  - **The loop:** the red-team agent loop (`redteam/attacker.py`), with the honest instruction `HONEST_SYSTEM`: earn as
    much as you can for the owner by taking part honestly, follow the rules, keep your promises, never claim more than
    the battery can do, and doing nothing is allowed.
  - **Not told:** known exploits.
  - **Per pause:** up to 30 replies (the turn cap).
- **The other 79 agents** are code agents. **The twin** is the same world with PVB_33 run by code too; the round runs
  it.
- **Window:** Jan 12-13, 2023, a design window (`configs/single_llm/validation/vjan12_twin.yaml`'s world, with no
  strategy layer).
- **Exchange settings:** as shipped. Both M5 defenses are on (`register_power_check`, `reference_power_cap`), D78 is on
  for missed pauses, and the gateway limits are at their defaults.
- **Route and caps:** the course route (D112). Round cap $2.00, phase cap $2.00. Expected spend is about $0.10-0.30.
- **No regression gate:** an empty catalog. **No exploit confirmation or ablation replays:** the thresholds are set out
  of reach, and this is not an attack.
- **Configs:** `configs/honest_free/hf_jan12_round.yaml` and `configs/honest_free/cloud_hf_jan12.yaml`.
- **Command:** `python -m bench.cloud_queue --llm --phase-cap-usd 2.0 --module redteam.launch --pilot --budget 1
  --commit <this commit> --queue-id hf-jan12 --run configs/honest_free/cloud_hf_jan12.yaml <m0_jan12>` (`--pilot`: no
  measured runtime exists for a live round, whose pace is set by the gateway, so it gets the 24 h limit).

## Soundness checks (the only gates)

1. The run finishes, and the round report and both worlds' traces download.
2. `bench/course_route_check.py` on the whole downloaded run folder (it finds the one call log, beside the attack world's
   trace; the leak scan covers every file, round and phase reports included):
   - roster coverage, with PVB_33 the only LLM agent and the others marked as code;
   - at least one call sent;
   - no backend error, and the cap not hit;
   - cost reconciled on every call;
   - no missing cost header;
   - no blocked term or key in any file of the run.

## Reported (no verdict on behaviour)

- **What it did:** tool calls by name, refusals by the exchange, by code (the gateway log), pauses missed, model
  refusals, unparseable replies, and whether it stopped (`done`).
- **PVB_33's result against its twin** (the round's triage gain).
- **The effect on everyone else**, twin-relative, by agent.
- **Calls, tokens per day, and reported dollars per simulated day.**
- **A plain-language reading** of its choices (from the call log and tool calls): did it register honestly, nominate,
  deliver, or sit out?

## Retry and stopping

- A run lost to a gateway, sandbox or launcher failure is rerun once at the same commit and disclosed.
- A code crash stops the test, and the fix goes through a new commit and a note here.

## What was seen before

- The red-team harness on a real window, with the fake backend (rt_real_dec11, Nov 19).
- The single-LLM-agent judge validation on the same Jan 12-13 window: Haiku as a posture chooser kept `normal` both
  days.
- **The honest agent has not been run before.**

## Disclosures

- The agent's freedom is the public interface's freedom: its battery follows it only during binding calls it
  nominated, and otherwise runs its acting-alone plan (`iface/external.py`).
- The full previous tool result goes into each prompt untruncated, so prompts are large (about 6k tokens).
- A reply wrapped in a markdown code fence is now read as the JSON inside it (`redteam/attacker.py strip_fence`), for
  attackers too.

## Notes from the pre-run review (Opus 5.5, Nov 24)

- A cap hit is written to the call log (`cap: reached`), so the cap gate can see it; each world's agent statuses
  (D101) are written beside it, so a rejected registration is reported as an excluded agent, not hidden.
- The cloud launcher's pre-queue LLM estimate reads only a chooser config and shows $0 here: it does not apply to
  a red-team round. The caps that bind are the round's spend guard and phase cap in the sandbox and the launcher's
  phase reservation of `llm_cap_usd_run`.
- A live round requires its pre-registration to be tracked and unchanged at HEAD, not merely present.

## Attempt 1 (Nov 24, 01:55 ET): crashed at triage; fixed and rerun once

Both worlds ran (the model's calls included), then triage crashed: the config's `1.0e9` thresholds were read by
YAML as text, and `float > str` raised. Nothing but the logs was kept, so nothing was seen of the result. Fix (this
commit): the thresholds are written out as numbers, triage converts thresholds to numbers, and a round now refuses
non-numeric thresholds before any world runs or any model is called. Rerun once at this commit, as the retry rule
allows. Attempt 1's model spend is lost (estimated $0.1-0.3, inside the cap; not reconciled per call).

## Results (attempt 2, Nov 24, 02:00-02:48 ET; recorded as found)

**Soundness checks:**
1. The run finished, and the launcher was reaped for low memory: `runs/m5/rescue/rescue.py` kept the sandbox awake and
   downloaded the run with its sha256 checked.
2. `bench/course_route_check.py` PASS on the whole run folder: 161 calls, costs reconciled, no backend error, no
   missing cost header, no leak (31 terms).
3. Model spend was $0.162. About 7.1k prompt tokens a call, 47% read from the gateway's cache.

**What it did (reported, no verdict):**

| | PVB_33 by code (twin) | PVB_33 by the honest LLM |
|---|---|---|
| Messages sent | register, subscribe, **116 nominations** | register, subscribe, **0 nominations** |
| Imbalance charged | $0.00 | **$743.31** |
| Peak settlement (net) | +$109.00 | +$0.31 |
| Value against its own twin | (reference) | **-$828.96** |

- **The model's turns:** 161 calls in all.
  - 65 replies were unparseable (40%), and there were no model refusals.
  - It made 83 tool calls.
  - It missed 4 pauses: the 30-reply turn cap was spent during price rounds for the call GRID_OP:8, and the queued
    sends at those pauses were discarded (`pause_deadline_missed`).
- **No nomination got through.** As a silent registered agent, it was counted at its 1p reference (D78, on). Its
  battery ran its acting-alone plan, and the gap was charged as imbalance.
- **Everyone else, twin-relative:** +$63.46 in total. The largest single loss was $13.81, and the largest gain $8.16.

**Reading:**
- The honest model with free hands did much worse than the code agent. The loss came from the interface's friction,
  not from dishonest or risky choices:
  - unparseable replies;
  - the turn cap at long price-round pauses;
  - D78 charging a silent agent against a reference it never chose.
- Two follow-ups:
  - The harness: structured replies or a schema, and a larger or per-call turn budget for honest agents.
  - The platform question: should a quiet, honest agent be charged imbalance against its 1p reference? D110 covers how
    quiet agents count; the charge is the owner's call.
- The call log does not record which tool each reply chose, so the 83 tool calls can't be broken down by tool. Fixing
  that is part of the harness follow-up.
