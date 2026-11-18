# Pearl Street: HW9 agent-usability evidence

**Team:** Cooper Fetters (solo). I did all of the work: the interface, the documentation, the tests, the runs and this write-up. It was built with Claude Code as a coding assistant.

**Course:** MAS.665, Fall 2026, HW9.

**All dollar figures from the simulation are synthetic.** They are simulator outputs on a historical dataset, not real money and not market results.

## 0. Numbers for slides

| What | Result (synthetic $) |
|---|---|
| Cold start run 1 (docs 0.1) | The client registered and subscribed, then waited for a nonzero price before nominating. Prices need every agent's nomination, so all 80 agents saw $0. Verdict "tie, $0.00" (correct). **Check 2 failed** (no binding nomination). |
| Improvement 2 | SKILL.md states that prices need your nomination; `advance` flags calls you haven't nominated. Regression test added. |
| Cold start run 2 (docs 0.2) | **All 5 pre-registered checks pass.** Participating beat acting alone by **+$92.42** (peak settlement, $0 imbalance). The client's figures match the trace to the cent. |
| Silent-agent finding | One agent that doesn't nominate freezes zone pricing at $0 for all 80 agents (exchange rule with D78 off). A control with the reference agent priced up to $18/MWh. |
| Fleet peak, Jan 12–13 (80 batteries) | Acting alone (selfish run): **2,500.3 MW**. M0 LP ceiling: **2,314.1 MW**. **Cold run 2: 2,320.0 MW**, a cut of 180.3 MW, which is **96.8% of the acting-alone-to-ceiling gap** and 95.4% of the M0 ceiling's cut from recorded. **Cold run 1: 2,500.3 MW**, a 0.0 MW cut and 0% of the gap: the silent agent erased the whole market's effect. |
| Boundary: one agent nominates 10× its battery's power | Accepted by the exchange; that agent paid **$281,453** imbalance (its value fell $281,485). |
| Did that hurt the other agents? | No honest agent fell below acting alone (D88 rule, zero tolerance: 0 of 4). Their values moved by −$0.43 to +$0.27, and their peak settlements by less than $0.01. |
| What improved (boundary) | Before: the over-limit nomination was accepted with `ok: true` and the sender learned of it only at settlement. Now: `nominate` returns an `exceeds_asset_power` warning at send time. A regression test covers it. |
| What remains unproven | Hosted, authenticated access (local stdio only). A cold start by another builder's agent (both runs were self-run, same model family). Repeatability: one run per docs version, one window. Whether participating beats acting alone once the energy market is counted. Over-limit and silent-agent effects at scale. The exchange-side fixes (only interface and docs changed). |

## 1. The interface

**Links.**

- **SKILL.md:** written for an agent that has never seen the repository. https://github.com/cafetters/pearl-street-public/blob/main/docs/agents/SKILL.md
- **Pre-registered checks:** https://github.com/cafetters/pearl-street-public/blob/main/docs/hw9/success_checks.md
- **Evidence files**, with every MCP request and response, the final answers verbatim, the check outputs and trace excerpts: https://github.com/cafetters/pearl-street-public/tree/main/runs/samples/hw9
- **Code:**
  - `iface/mcp_stdio.py`: the MCP server, stdio, no dependencies.
  - `iface/external.py`: one battery operated by an outside agent inside the simulator.
  - `agents/tool_iface.py`: the M4 tool layer, reused unchanged.

**What Pearl Street offers to an agent:** a flexibility exchange. The agent operates one of 80 simulated solar-plus-battery sites in one grid zone. It can:

- register its battery;
- see the operator's calls for nominations and the exchange's per-step peak prices;
- send a site net schedule (a nomination);
- declare availability or an outage;
- step the simulation;
- read its settlement.

**Settlement** (rule 1p) pays the settled peak price on (metered site net − an acting-alone reference). The metering service computes that reference from the agent's registration and public prices. Deviation from a binding nomination beyond a free band pays imbalance at $500/MWh.

**How the tools are built.** Each send tool maps one-to-one onto an exchange message. Every send is checked against the message schema (`exchange.messages.validate`) and goes through the message bus, which binds the sender to the endpoint it came from (D69). There is no sender argument, so an agent cannot speak for another. Read tools: `my_asset`, `read_inbox`, `read_public`, `get_settlement`. Control: `advance`.

**How a session runs.** The simulation pauses at each gate:

- registration;
- a new call, reject or settlement;
- the first and final price round of each call.

It waits there until the agent calls `advance`. The other 79 batteries are the project's reference agents.

**Limits and permissions** (all in SKILL.md):

- one agent per server process;
- no credential: access is the local process;
- registration is checked against the owner's attested facts;
- nominations close at the call's deadline;
- the reject codes are listed;
- deregistering and multiple assets are not supported.

The run config lists the outside agent under the simulator's `attackers` hook with type `external`. **An external agent is untrusted by design (D63), not malicious.** That hook is simply the simulator's only per-agent wrapper.

## 2. Cold-start integration

**Setup.**

- **Client:** a fresh Claude Code session (claude-opus-5-5), run headless with `claude -p` and stream-json output. Two runs: run 1 with SKILL.md 0.1, run 2 with 0.2 after improvement 2.
- **Folder:** otherwise empty, holding only `SKILL.md` and `.mcp.json`.
- **Task, verbatim:** "You operate battery PVB_15. Participate in Pearl Street for this 2-day window and report whether participating beat acting alone, with numbers."
- **What it was not given:** no call sequence and no expected answer.
- **Window:** Jan 12–13, 2023, 80 batteries, arm C (price mode), rule 1p, seed 1.

**Independence.** **Self-run**: I set it up, and the client is a fresh session of a model from the same family as my build assistant. No human was in the loop during the run, and I gave no manual help. There were two runs, each a fresh session with no memory of the other. Each client's only file reads were SKILL.md in its own folder and, in run 2, its own saved tool outputs. **No repository file was read.**

**Pre-registered checks.** These were committed before the run in `docs/hw9/success_checks.md` (commit `0033874`). The checker, `scripts/hw9_check.py`, was proved first on a real smoke trace. Check 5 (the conclusion matches the sign) was added at the owner's request: correct numbers with a wrong conclusion fail.

### Cold run 1 (SKILL.md 0.1; commit `0033874`)

**Run facts.** 42 turns, 6.8 min wall time, client usage about $1.06. The run was cheap and quick because no price rounds formed (see below).

**What the client did.**

- **Its own calls:** 16 `advance`, 14 `read_inbox`, 3 `my_asset`, 2 `read_public`, 1 each of `register`, `subscribe` and `get_settlement`, 0 `nominate`.
- **Startup:** it found SKILL.md with `Glob **/SKILL.md` in its folder and read it. It then registered at full power and subscribed, unprompted.
- **Calls:** it stepped through 7 operator calls, 2 of them binding (GRID_OP:4 and GRID_OP:8).
- **Its stated reasoning at the binding calls:** "Round 1 prices for the binding call are all zero. Nominating with no priced steps only adds imbalance risk, so I'll check the final round before deciding." The final rounds were also $0 everywhere, so it never nominated.
- **Result use:** it called `get_settlement` and used the acting-alone reference and the metered net to explain the small meter differences. It then answered.

**Final answer (verbatim excerpt; full text in the evidence files):**

> "I took part in the full 2-day run, and **participating did not beat acting alone. It tied at exactly $0.00.** … For both binding calls, all 4 price rounds, including the final one, priced every step at **$0/MWh** … **I didn't send a nomination for either binding call.** With a $0 price, a nomination can't earn anything … It could only cost money: any miss beyond the free band of max(5% of power, 50 kW) ≈ 142 kW is charged $500/MWh."

**Pre-registered checks** (`scripts/hw9_check.py`; roster 80 of 80 registered):

| Check | Result |
|---|---|
| 1. Registered | PASS |
| 2. At least one accepted binding nomination | **FAIL** (0) |
| 3. Settlement retrieved after the run | PASS |
| 4. Final numbers match the trace (S = $0.00; every $ figure in the answer is $0 apart from the $500/MWh imbalance rate) | PASS |
| 5. Verdict matches sign of S (S = 0 → NO; the agent said "did not beat") | PASS |
| **Overall** | **FAIL** (check 2) |

**Why every price was $0. A project limitation, found by this run.**

- The exchange computes the zone's load only at steps where **every** registered agent has nominated (`exchange/core.py`, `_zone_load`; D78's `silent_reference`, which would count a silent agent at its reference, is off in this config).
- The outside agent waited for a nonzero price before nominating, and the price waited for its nomination. The price update saw no measurable excess, so the rounds "converged" at $0 in round 4.
- That zeroed pricing for **all 80 agents** for the whole window.
- **Control:** the same config with PVB_15 as an ordinary reference agent priced the first gate at up to $18/MWh from round 1 (`runs/hw9_control_jan12`; stopped after 8 rounds to free memory).
- **Who is at fault:** the client behaved sensibly given SKILL.md 0.1, which never said that prices need its nomination. So this is an **interface and documentation gap on top of a project limitation** (one silent agent stops price formation). It is not a test-setup problem.
- **Boundary finding:** it is a stronger form of the section 3 question. A silent agent, not a malicious one, disabled the market for everyone.

**Improvement 2 (commit `1fc6922`, before cold run 2).**

- SKILL.md 0.2 states the rule ("Prices need your nomination").
- While an open call lacks the agent's nomination, every `advance` returns `open_calls_you_have_not_nominated` with an `attention` note giving the reason.
- Regression test: `test_regression_advance_flags_open_calls_not_nominated`.
- The exchange-side fix (D78's silent reference, or counting a silent agent at its reference) is a plan item, not changed tonight.

### Run 1 diagnosis and what changed before run 2

**Classification.** This is a **project limitation**, compounded by an **interface and documentation gap**. It is **not a test-setup problem**. It is the silent-agent price freeze that D78 and D79's `silent_reference` exists to prevent. That flag is off in this config: neither `silent_reference` nor `declared_reference` appears in `configs/hw9/hw9_cold_jan12.yaml`.

**Evidence from the trace** (`runs/samples/hw9/cold1_trace_excerpt_PVB_15.jsonl` holds the PVB_15 lines and every final price):

| | Run 1 | Run 2 |
|---|---|---|
| `nomination_aggregate` at GRID_OP:4 and GRID_OP:8: `n_agents` | **79** of 80 | 80 of 80 |
| Final `peak_price`, GRID_OP:4 | round 4, max $0.00/MWh | round 16, max $13.43/MWh |
| Final `peak_price`, GRID_OP:8 | round 4, max $0.00/MWh | round 30, max $18.65/MWh |
| `nominate` calls by the client (MCP log) | **0** | 8 |

**Mechanism.** With one agent missing, `_zone_load` returns no zone load at any step. The price update then sees no excess, and the convergence test passes on its 3-round streak, so round 4 is "final" at $0. The control with PVB_15 as a reference agent priced up to $18/MWh from round 1.

**What changed between run 1 and run 2** (everything else is identical):

| Item | Change | Visible to the client? |
|---|---|---|
| Server code | `advance` and the paused status add `open_calls_you_have_not_nominated` and an `attention` note (`iface/external.py` +7 lines, `iface/mcp_stdio.py` +17 lines); `serverInfo.version` goes from 0.1-hw9 to 0.2-hw9 | **Yes** |
| SKILL.md | 0.1 → 0.2: a version line, a "Prices need your nomination" paragraph, and the `advance` row in the tools table mentions the new field | **Yes** |
| Config | `run_name` only (`hw9_cold_jan12` → `hw9_cold2_jan12`) | No |
| `.mcp.json` | the config path only | The path is visible; behavior is unchanged |
| Prompt, flags, model, battery, window, seed | identical | — |
| Folder and session | a new empty folder and a fresh session, with no memory of run 1 | — |

**Both runs saw the boundary-test improvement.** The `exceeds_asset_power` warning on `nominate` and its SKILL.md text were added in `9b8c6df` (originally `506766f`), before run 1. Neither client triggered it, because both nominated within power. So run 2 is the fresh cold run with the improved docs and interface, and it ran all five checks. No third run was needed.

**Commit names.** The SHAs cited in the trace headers and the pre-registration are the original branch commits (`0033874` for run 1, `1fc6922` for run 2). They are preserved on the private repo's `hw9-usability` branch. Main carries the same content, rebased: `35a83f6` and `70a73ce`.

**Billing.** Both clients ran on the Claude Code subscription login, not an API key. The stream-json init shows `apiKeySource: "none"`, and no `ANTHROPIC_API_KEY` was set. The "$1.06" and "$2.82" are Claude Code's usage estimates, not API charges.

### Cold run 2 (SKILL.md 0.2; commit `1fc6922`; pre-registered in the same commit)

**Setup.** Same task, flags and checks as run 1, in a fresh session and a new empty folder.

**Run facts.** 65 turns, 15.7 min wall time, client usage about $2.82. Price rounds formed this time: 30, 7, 16 and 30 rounds at the four gates.

**What the client did.**

- **Its own calls:** 24 `advance`, 16 `read_inbox`, 8 `nominate`, 7 `my_asset`, 1 each of `register`, `subscribe`, `read_public` and `get_settlement`.
- **Calls:** it nominated every call, binding or not, when it arrived, as the `attention` note asked.
- **Battery plan:**
  - It stored midday solar (about 2,135 kWh at 450 kW on Jan 12) and discharged 1,800 kW, then 1,200 kW, from 03:00 to 06:00 UTC (9 pm to midnight Central) on each night.
  - It checked its state of charge against its own projection: 898 kWh actual vs 903 kWh planned.
- **Dispatch:** the battery followed its two accepted binding nominations for 176 steps, with 0 kWh deviation.
- **Result use:** it called `get_settlement` and built the answer from the exchange's own settlement lines.

**Final answer (verbatim excerpt; full text in the evidence files):**

> "Participating beat acting alone by **+$92.42** (synthetic simulator dollars), with no imbalance penalties.
>
> | Refund (the exchange's acting-alone reference, at the settled peak price) | +$0.91 |
> | Charge on my metered site net (negative because I exported, so I was paid) | −$91.51 |
> | **Peak settlement net** | **+$92.42** |
> | Imbalance … | $0.00 |
>
> … **Energy market not included.** The settlement covers only the peak exchange and imbalance … This doesn't show whether the battery did better overall. **I left money on the table on night 2 by choice.** … I could have moved the discharge into those frozen high prices … That would have quietly removed relief the market had counted on at 04:00–06:00, so I kept the schedule it cleared on."

**Pre-registered checks** (roster 80 of 80 registered):

| Check | Result |
|---|---|
| 1. Registered | PASS |
| 2. At least one accepted binding nomination | PASS (2: GRID_OP:4 and GRID_OP:8) |
| 3. Settlement retrieved after the run | PASS |
| 4. Final numbers match the trace: S = $92.42 (Σ `peak_settle.net_usd` $92.4192 − Σ imbalance $0) | PASS. Every settlement figure matched within $0.005: refund $0.91 vs $0.9138; charge −$91.51 vs −$91.5054. Its $83 and $29 are its own counterfactual estimates, not settlement figures. |
| 5. Verdict matches the sign of S (YES) | PASS |
| **Overall** | **PASS** |

**Fleet outcome (system net load, run_m1 definitions; computed by `scripts/hw9_viewer_summary.py` from each trace plus one selfish run of the same config; not a run_m1 evaluation):**

| | Peak (MW) | Cut vs acting alone | Share of acting-alone-to-ceiling gap | Share of the M0 ceiling's cut from recorded |
|---|---|---|---|---|
| Acting alone (selfish run) | 2,500.3 | — | — | — |
| M0 LP ceiling (`runs/m0_jan12`) | 2,314.1 | 186.2 | 100% | 100% |
| **Cold run 2** (outside agent with docs 0.2, plus 79 reference agents) | **2,320.0** | **180.3** | **96.8%** | 95.4% |
| Cold run 1 (outside agent silent, plus 79 reference agents) | 2,500.3 | 0.0 | 0% | −46.6% (above recorded) |

Trace viewers: `python -m exchange.trace.viewer runs/hw9_cold2_jan12` (and `runs/hw9_cold_jan12`), after `scripts/hw9_viewer_summary.py` writes each folder's `summary.json`.

**Friction the client hit in run 2** (seen in the log; not fixed tonight):

- **Step-count reject.** Its first nomination (a non-binding call) had 91 values for a 92-step window. The reject said `"kw must be finite numbers, one per step"`, with `refs: {"steps": 92}`. The client guessed the cause was integer zeros, but it also recounted and resent 92 values, which were accepted.
  - **Possible fix:** the interface could give each call's step count with the call, or pre-check the length before sending.
- **Oversized `read_inbox`.** With 30 price rounds, one `read_inbox` returned 68k–89k characters (every round's price vector). The client's harness saved these to files, which the client then read.
  - **Possible fix:** a "latest round only" option.
- **What its verdict covers.** It correctly noted that its verdict covers the exchange settlement only, not the day-ahead energy market. That is the same boundary the pre-registration states.

**Note on file access.** Beyond SKILL.md, run 2's client read two files: Claude Code's own saved copies of its oversized `read_inbox` results, under its session folder in `~/.claude/projects/`. These are its own tool outputs, not repository files. In neither run did the client read any repository file.

## 3. Boundary test, improvement, regression test

**The case: a nomination over the battery's registered limit.** I chose it because the exchange's own nomination check (`exchange/core.py`, `_on_nominate`) covers the call, zone, window, deadline and finite numbers, but not power. A nomination is site net (solar − load ± battery), so the exchange has no baseline to bound it by.

**Design (owner).** The same window was run twice, with 5 batteries over Jan 12 and a scripted (non-LLM) outside agent on PVB_15:

| Run | PVB_15's nomination for every binding call |
|---|---|
| `idle` | its battery-idle site net |
| `overlimit` | battery-idle net + 10 × registered max_kw at every step |

Each run is sim/run_m1.py's full pipeline: a selfish run, then the exchange run, then the harm evaluation.

**The question:** does one bad agent hurt the others? Did any honest agent's settlement change, or fall below acting alone? That is D88's rule with zero tolerance.

**Results** (before the improvement; commit `059b976`; synthetic $):

| Agent | Peak settlement, idle → overlimit | Imbalance paid | Value with Pearl Street, change |
|---|---|---|---|
| PVB_15 (over-limit) | $284.03 → $284.03 | $0 → **$281,452.91** | **−$281,484.79** |
| PVB_33 | $504.3960 → $504.3949 | $0 → $0 | −$0.43 |
| COM_03 | $543.1362 → $543.1374 | $0 → $0 | +$0.27 |
| DC_06 | $477.4425 → $477.4410 | $0 → $0 | −$0.40 |
| PVB_41 | $649.3225 → $649.3225 | $0 → $0 | +$0.03 |

- **Below acting alone, zero tolerance:**
  - idle run: 0 of 5 agents;
  - overlimit run: 1 of 5, the over-limit agent itself.
  - So 0 of 4 honest agents fell below acting alone.
- **Harm rule** (D88 as coded: max(1%, $10) tolerance): only PVB_15 is harmed, and its harm is self-inflicted.
- **Peak:** the zone peak moved from 2,338.17 MW to 2,337.94 MW.

**What happened.**

- The exchange accepted the impossible nomination. The interface answered `{"ok": true, "status": "queued"}`, and the agent learned of the problem only at the end, from a $281k imbalance charge.
- The bad nomination entered the zone total that drives the price rounds. That moved the honest agents' outcomes slightly, in both directions: values by −$0.43 to +$0.27, and peak settlements by under $0.01.
- None of them fell below acting alone.
- The cost landed on the agent that lied.

**Improvement (interface, commit `506766f`).** `nominate` now returns a warning at send time:

```json
{"code": "exceeds_asset_power", "steps": 80, "worst_implied_battery_kw": 28415.4, "power_kw": 2841.5, "detail": "..."}
```

It fires when the implied battery kW (nominated − battery-idle site net) is beyond the battery's power. It is a warning, not a refusal: the message is still sent, and the exchange's rules are unchanged. SKILL.md documents it.

**Regression test.** `tests/test_hw9_iface.py::test_regression_overlimit_nomination_warns_but_is_sent` covers it, alongside six interface tests: sender binding, bad arguments, the register pre-check, `advance` refused before register, and dispatch dropping a rejected nomination. Targeted run: `pytest tests/test_hw9_iface.py tests/test_m4_tool_iface.py`, 14 passed.

**Rerun** of the over-limit probe at `506766f`: the `nominate` reply now carries the warning. Settlement and harm are identical, because the warning changes information, not rules.

**What improved:** an agent that mis-sizes a nomination learns at send time, with the step and kW, instead of from a settlement charge.

**What did not:**

- The exchange still accepts the nomination.
- The honest agents' small exposure through the zone total is unchanged.

**What remains unproven:**

- that an LLM agent heeds the warning;
- the size of the effect on others at 80 agents, or with many over-limit agents.

A proposed exchange rule (bound |nomination − acting-alone reference| by max_kw under rule 1p) is written up for the project plan, not built.

## 4. Interface limitations vs. test-setup problems

**Interface or project limitations.** These are true of Pearl Street as built.

1. **No credential, local stdio only.** Access is whoever can start the process. The hosted, authenticated server (D69's registration credential) is M5 work.
2. **No exchange-side bound on nominations.** The bound is site net, and the exchange has no site baseline (section 3). Tonight the only guard is the interface warning.
3. **One rejected registration stops the whole simulated run.** That is the simulator harness (`sim/run_m1.py:628`), not an exchange rule. An agent that never registers crashes the first settlement gate (seen in development, when a scripted driver failed before registering). Mitigations:
   - the interface pre-checks registration against the owner's attested facts;
   - `advance` is refused until register is sent.

   A hosted exchange must not let one bad registration stop the market. This is written up as a proposed plan change.
4. **The dispatch rule is fixed.** In binding windows the battery follows the agent's nomination, and elsewhere it runs the reference acting-alone plan. No message carries battery set-points, so the agent controls the battery only through binding nominations.
5. **The acting-alone reference arrives only at the end.** It reaches the agent in `get_settlement` after the run. During the run it can't see the reference it will be settled against, only the prices.
6. **Pauses are coarse.** Intermediate price rounds don't pause. The agent sees round 1 and the final round of each call, and its latest nomination stands in between.

**Test-setup problems.** These are true of this test, not of the interface.

1. **Self-run.** The client is a fresh session, but the owner chose the model, the folder and the flags. Another builder's agent was not available tonight.
2. **The client could see more of its own harness than a real outside agent would.** Built-in Claude Code tools (Workflow, SendMessage, Cron, and others) stayed loaded in the client. Bash, Grep, web access and editing were blocked.
3. **The MCP config reveals the repository path.** It holds the server's command line, so a client could have read the repository with Read or Glob. The session logs show that neither client did.
4. **Synthetic data.** Results are from one 2-day window, seed 1, with 79 project reference agents as counterparties.
5. **Slow `advance`.** With 80 agents, a price round takes about 16 s, so `advance` often returned `running`. That is a laptop limitation, not a protocol limit.

## 5. How to repeat

**Requirements:** the repository (private; the public mirror has the same code) and the dataset folder at `../data`. The dataset is not distributed. Python 3.12 and the project's venv.

```bash
# interface tests (no data needed)
python -m pytest tests/test_hw9_iface.py tests/test_m4_tool_iface.py

# scripted smoke over real MCP stdio (5 agents, 1 day, ~30 s)
python scripts/hw9_probe.py --config configs/hw9/hw9_smoke_jan12.yaml --policy idle --transport stdio

# boundary test: same window with and without the 10x over-limit nomination (~30 s each)
python scripts/hw9_probe.py --config configs/hw9/hw9_boundary_idle.yaml --policy idle --transport inproc
python scripts/hw9_probe.py --config configs/hw9/hw9_boundary_overlimit.yaml --policy overlimit --transport inproc
#   compare runs/hw9_boundary_*/summary.json: exchange_by_headroom_mw.100.agents_worse_off_than_selfish, agent_value_usd

# cold start: an empty folder with SKILL.md and .mcp.json (server: python -m iface.mcp_stdio --config
# configs/hw9/hw9_cold_jan12.yaml, PYTHONPATH=<repo>), then:
MCP_TOOL_TIMEOUT=600000 claude -p "You operate battery PVB_15. Participate in Pearl Street for this 2-day window and report whether participating beat acting alone, with numbers." \
  --mcp-config .mcp.json --strict-mcp-config --setting-sources project --output-format stream-json --verbose \
  --allowedTools "mcp__pearl-street" Read Write Glob --disallowedTools Bash Grep WebFetch WebSearch Edit Task --max-turns 250 > session.jsonl

# run 2: same command in a second empty folder whose .mcp.json points at configs/hw9/hw9_cold2_jan12.yaml
# checks 1-5 (the final answer is the stream's "result" text, saved verbatim)
python scripts/hw9_check.py --run runs/hw9_cold_jan12 --answer runs/hw9_cold_client/final_answer.txt
python scripts/hw9_check.py --run runs/hw9_cold2_jan12 --answer runs/hw9_cold2_client/final_answer.txt
```

**Evidence files** (`runs/samples/hw9/` in the repository):

- the MCP log, with every request and response;
- the client's final answer, verbatim;
- the check output;
- a trace excerpt, with its header (config, seed, git commit) and every message to or from PVB_15.

The full trace is regenerated by the cold-start command above. The run is deterministic apart from the client's choices.
