# HW9 cold start: pre-registered success checks

Written and committed **before** the cold-start run. The commit time in git proves the order. All simulation dollar figures are **synthetic**.

## The run

| Item | Value |
|---|---|
| Config | `configs/hw9/hw9_cold_jan12.yaml` (arm C, rule 1p; Jan 12–13, 2023; 80 batteries; 100 MW headroom; seed 1) |
| Fallback | `configs/hw9/hw9_cold_jan12_1day.yaml` (Jan 12 only), used at the first sign of memory pressure |
| Outside agent's battery | **PVB_15** (2,842 kW, 6,967 kWh; a mid-sized battery in the 80) |
| Other batteries | 79 project reference agents |

**Client.** A fresh Claude Code session, run headless with `claude -p` and stream-json output, in an otherwise empty folder `%USERPROFILE%\AI-Studio\hw9-coldstart\`. The folder holds only:

- `SKILL.md`, a copy of `docs/agents/SKILL.md` at this commit;
- `.mcp.json`, which starts `python -m iface.mcp_stdio --config <hw9 worktree>/configs/hw9/hw9_cold_jan12.yaml`.

**Settings that isolate the client:**

| Flag | Effect |
|---|---|
| `--strict-mcp-config` | no other MCP servers |
| `--setting-sources project` | no user settings |
| `--allowedTools "mcp__pearl-street,Read,Write,Glob"` | the only tools allowed |
| `--disallowedTools Bash,Grep,WebFetch,WebSearch,Edit,Task` | explicitly blocked |
| `--max-turns 250` | turn cap |
| `MCP_TOOL_TIMEOUT=600000` | MCP tool timeout |

**Task, verbatim:** "You operate battery PVB_15. Participate in Pearl Street for this 2-day window and report whether participating beat acting alone, with numbers."

The client gets no call sequence and no expected answer. The only pointer to SKILL.md is the server's `initialize` instructions and the file in the folder.

**Self-run disclosure.** The owner set this up and the client is a fresh session of the same model family. No human is in the loop during the run, and there is no manual help. Any read by the client outside its folder will be found in the session log and disclosed. The run will not be repeated to hide one.

## Checks

The checks are run by `scripts/hw9_check.py --run runs/hw9_cold_jan12 --answer <final answer, verbatim>`.

**Inputs and roster.** The roster comes from the trace header. The outside agent is the header's `attackers` entry with type `external`, and it must be in the roster. Every roster agent must have a register message with no reject.

| # | Check | Passes when |
|---|---|---|
| 1 | **Registered** | PVB_15's `register` is in the trace with no `reject` whose `ref_msg_id` points to it |
| 2 | **At least one accepted binding nomination** | A `nominate` from PVB_15 exists on a call whose exchange `call_nominations` has `binding: true`, with `t_utc` before that call's `deadline_utc`, and no `reject` points to it |
| 3 | **Settlement retrieved** | The MCP log has a `get_settlement` response with `run_status: "finished"` |
| 4 | **The final numbers match the trace** | One dollar figure in the agent's final answer equals \|S\| within max($0.50, 1% of \|S\|) |
| 5 | **The conclusion matches the sign** | The agent's yes/no on "beat acting alone" equals YES if S > 0 and NO if S ≤ 0 |

**How S is defined (check 4):** S = Σ `peak_settle.net_usd` − Σ `imbalance.charge_usd` for PVB_15. The `peak_settle.net_usd` term is itself refund − charge: the settled peak price times (metered site net − the metering service's acting-alone reference net). So S is the exchange's settlement of participating against the acting-alone reference.

- **Sign:** it is judged by check 5. A figure written as a positive number with the word "lost" is still read as a match by check 4.
- **Other figures:** every other dollar figure in the answer is listed next to its nearest trace sum for review, and any mismatch is reported in the write-up.

**Verdict extraction (check 5):** `verdict()` in the script sets YES or NO from the answer's wording. If the wording is ambiguous, the script prints UNDETERMINED. The verdict is then read by hand and the write-up says so. **Correct numbers with a wrong conclusion fail.**

**What S leaves out.** Any energy-cost difference between the agent's dispatch and acting alone outside the settled terms (for example, buying or selling energy at other hours because the state of charge differs) is not part of S. An agent that includes it may reach a different conclusion. In that case check 5 still compares the agent's verdict against the sign of S, as pre-registered, and the write-up discusses the difference.

**Pass:** checks 1–5 all pass, with the roster complete. A failed check is reported as a failure. Each failure is attributed either to the interface or project, or to the test setup.

## Proof on a real trace (CLAUDE.md rule)

Before this file was committed, `scripts/hw9_check.py` was run on the real smoke trace `runs/hw9_smoke_jan12` (5 agents, 1 day, scripted client), with two answers written by hand:

| Answer | Result |
|---|---|
| Correct total and "Yes" | PASS (S = $284.03, synthetic) |
| Wrong total and "No" | FAIL on checks 4 and 5 |

It is re-run on a smoke trace at this commit before the cold run.

## Boundary test (owner, Nov 18)

**Design.** The same window (5 agents, 1 day; `configs/hw9/hw9_boundary_{idle,overlimit}.yaml`) is run twice through `scripts/hw9_probe.py --transport inproc`, which runs sim/run_m1.py's main: a selfish run, then the exchange run. Both runs use the same scripted outside agent on PVB_15:

| Run | PVB_15's nomination for every binding call |
|---|---|
| `idle` | its battery-idle site net |
| `overlimit` | battery-idle net + 10 × registered max_kw at every step |

**Questions asked:**

- Was the over-limit nomination accepted?
- What did PVB_15 pay?
- Did any **honest** agent's settlement change?
- Did any honest agent end up below acting alone? This is D88's rule with **zero tolerance**: run_m1's `agents_worse_off_than_selfish`, a 1e-6 comparison against the selfish run.

**Order.** These probes ran before this file was committed:

| Commit | What ran |
|---|---|
| `059b976` | the probes before the improvement |
| `506766f` | the improvement (an `exceeds_asset_power` warning on `nominate`), its regression test in `tests/test_hw9_iface.py`, and the over-limit probe rerun |

The results are reported in the write-up, not here.

## Cold run 2 (added after cold run 1, before cold run 2 starts)

**Cold run 1 outcome**, as pre-registered (`runs/hw9_cold_jan12`, commit `0033874`):

| Check | Result |
|---|---|
| 1, 3, 4, 5 | PASS |
| **2** | **FAIL**: no binding nomination |

The client waited for a nonzero price before nominating. But the exchange computes zone load only where every registered agent has nominated (`exchange/core.py` `_zone_load`; D78 `silent_reference` is off in this config). So every peak price stayed $0 for all 80 agents, and the rounds "converged" at round 4.

**Control.** The same config with PVB_15 as an ordinary reference agent priced the first gate at up to $18/MWh from round 1. The control was stopped after 8 rounds to free memory. Its partial trace is `runs/hw9_control_jan12`.

**Improvement 2** (this commit):

- SKILL.md 0.2 states the rule under "Prices need your nomination".
- `advance` and the status now list `open_calls_you_have_not_nominated` with an `attention` note.
- Regression test: `tests/test_hw9_iface.py::test_regression_advance_flags_open_calls_not_nominated`.

**Cold run 2 setup.** Identical to run 1 except:

| Item | Run 2 |
|---|---|
| Config | `configs/hw9/hw9_cold2_jan12.yaml` (run_name only differs) |
| Folder | a new empty folder, `%USERPROFILE%\AI-Studio\hw9-coldstart2\`, with SKILL.md 0.2 and `.mcp.json` pointing at the new config |
| Session | fresh |

The task text, flags, battery (PVB_15) and checks 1–5 with their tolerance are unchanged.

**Reporting.** Both runs are reported. Run 1 is not replaced.
