# Agent strategy, memory and reset, and bad actors (design)

**Oct 28, 2026. Design only: no code, no runs.** It plans the M3 and M4 pieces early because they change how agents play, and so what the M2a-era rules are tested against. The proposed decision entries below were Leaning. **Oct 30:** the owner answered section 8, and PLAN.md now carries D59-D61 and D63-D66. Where this doc and PLAN.md differ, PLAN.md wins.

**Gate (CLAUDE.md):** M2a must pass before M3 or M4 code is built. The one exception, below, is the risk-margin setting. It is D58's lever, so it is M2a work.

---

## 1. Where the agents are today

- **Every agent runs the same fixed code** (`agents/battery.py`). Each step it solves the same LP (`agents/planner.solve`). In arm C it re-plans against each peak price round (`between_price_rounds`).
- **Every rule so far (D53, D56, D58) has been tested against that one behavior.** If an LLM picks strategy, behavior changes. A rule that holds for one behavior may fail for another.
- **The owner's levers are config flags shared by the whole fleet.** `risk_premium_usd_mwh` already exists, but it adds to offer floors (arm A). Arm C has no offers, so it does nothing there (D58).
- **Each run starts from zero.** Agents are built fresh from the config and seed. Nothing carries over between runs. D20's warm-up (Leaning) would be the first exception.

## 2. Strategy: what the LLM may choose

### 2.1 The rule that keeps the harm metric meaningful
**A strategy choice may change only how the agent responds to Pearl Street signals. It never changes the agent's selfish plan.**

Why this rule:
- **The no-net-harm baseline is a separate acting-alone run** (section 8 of PLAN.md). An LLM's choices depend on what it saw inside Pearl Street, so they can't be replayed in that run. If a choice moved the selfish plan, harm would measure strategy quality, not Pearl Street.
- **1p settlement needs it.** The exchange's reference Q (D56) is computed with the default planner. If a strategy changed the selfish plan, Q would stop being the agent's counterfactual, and settlement-caused harm could come back.
- **The acting-alone run stays LLM-free.** That makes it cheaper, and the run is deterministic.

### 2.2 How a choice becomes numbers (D12)
- **The LLM picks a label from a fixed menu.** For example, risk posture: `cautious`, `normal` or `bold`.
- **Code maps each label to numbers**, in one table in a new module (`agents/strategy.py`). A test covers the table.
- **The LLM never outputs a number that code uses.** Letting it set a $/MWh value directly would change D12 (Decided) and need a new log entry. This design doesn't do that.
- **The exchange still enforces the mandate (D13).** The menu and its mapping are agent-side. They are not a safety boundary (section 4).

### 2.3 Candidate menu items, checked against the rule

| Item | Labels | What code does | Changes the selfish plan? | Status |
|---|---|---|---|---|
| **Risk posture** (D58's lever) | cautious, normal, bold | A margin m on peak response, in $/MWh. The agent shifts energy only where the peak price beats its lost day-ahead arbitrage by more than m. The leading form is a linear penalty of m per MWh on the distance from the agent's own selfish plan at that gate. A dead band on the price, max(peak − m, 0), is the alternative. The D58 pre-registration picks one | No, it only damps the response | **Proposed now.** `normal` = today's behavior (m = 0) until D58's test adopts a value |
| **Response cap** | full, half | Caps the battery power used for peak response at 100% or 50% of `max_kw` | No | Candidate |
| **Participation today** | in, out | When out, the agent does not nominate in that day's price rounds and plans selfishly | No | **Owner's call:** it changes exchange semantics (registration and settlement on days out), so it needs a decision entry |
| Terminal SOC target, wear assumption, forecast choice, arbitrage aggressiveness | — | — | **Yes** | Rejected under 2.1 |
| Arm A items (offer premium, rebound priority) | — | — | — | Out of scope: arm A is the comparison, not the clearing (D53) |

- **The new knob gets a new config name** (for example `peak_response_margin`). It does not reuse `risk_premium_usd_mwh`, so old configs keep their meaning.
- **It acts in the arm C re-plan** (`between_price_rounds` and the plan the binding nomination comes from), not in offer floors.

### 2.4 Who chooses: three choosers on one interface
1. **Fixed** (the default): every agent is `normal` on every item. All results so far are this chooser.
2. **Rule:** a short deterministic rule on the same inputs the LLM sees (for example, `cautious` after a window where the agent's own gap was a loss). This is M4's ablation baseline, so headline claim 2 is LLM vs. rule.
3. **LLM:** the M4 build.

The chooser is set in the config, per agent or for the fleet.

### 2.5 When it chooses (cadence, D28)
- **Never per price round.** Up to 30 rounds run per gate. A strategy is fixed for the whole gate.
- **Daily:** right after the day-ahead prices publish (12:00 local on D−1, `price_publish_hour_local`). That is before the day's binding call and its price rounds.
- **Key events:** checked only between gates, and effective from the next gate. Candidates:
  - a settlement statement (`peak_settle`) showing a loss beyond tolerance;
  - a reject from the exchange;
  - a binding call announced.
- **The base load is 80 agents × one call per day.** That is 160 calls for a 2-day window, plus key events.
- **D28 stays Leaning.** Its trigger is the M4 ablation.

### 2.6 What the LLM sees (information firewall)
**In:** only what the agent already holds.
- its own site's forecasts (load and PV, as the simulator hands them over), its battery and its mandate;
- the published day-ahead prices;
- broadcasts it is subscribed to (`peak_price` and the centre shift, `call_nominations`);
- its own settlements (`peak_settle`, `imbalance`) and rejects;
- its own memory (section 3).

**Never:**
- real-time prices before settlement;
- any other agent's data;
- `zone_target` (D37);
- recorded data beyond what the agent is handed;
- the acting-alone run.

**Enforced in code, with a test.** The prompt builder takes only the agent object's own fields and its inbox. A test fails if any other input reaches it. One leak would void headline claim 2.

**Output:** JSON with one label per menu item, plus a short rationale in text.
- Code validates the labels against the menu.
- An invalid or missing answer keeps the previous strategy, and the event is logged.
- The rationale is logged and never parsed.

### 2.7 Reproducibility
- **Response cache.** LLM responses are cached, keyed on a hash of (model, prompt). Replay mode reads only from the cache, and a cache miss is an error. A run can then be replayed exactly, as the trace rule requires.
- **Trace header additions:**
  - chooser type;
  - model ID;
  - prompt and menu version;
  - cache file hash;
  - memory snapshot hash (section 3).
- **Logged per call:** the prompt hash, the labels chosen, tokens and dollars. The dollar logging is M4's pass test.

## 3. Memory and reset

### 3.1 State lives on two sides

| Side | What | Who holds it |
|---|---|---|
| Agent | Strategy history, its own settlement history, LLM notes (text) | The agent |
| Exchange | Registry, granted limits (D38, D40), delivery records (D4) | `exchange/` |
| Metering | Demonstrated PV, metered `usable_kwh` history (an input to 1p, and a gaming path) | The simulator's metering service |

- **LLM notes are text for the LLM only.** Code never reads a number from them (D12).

### 3.2 Snapshots
- **A snapshot holds all three sides at a point in time.** It is a file under `runs/<run>/snapshots/`.
- **The simulator loads it and passes it in.** The exchange never reads files (CLAUDE.md).
- **Its hash goes in the trace header.**

### 3.3 Reset levels

| Level | Agent memory | Exchange and metering records | Use |
|---|---|---|---|
| **Fresh** | empty | empty | Today's runs; any clean comparison |
| **Agents fresh** | empty | from a snapshot | New LLM agents entering a mature market |
| **Named snapshot** | from snapshot | from snapshot | D20 warm-up (Jan 4-17 into Jan 2-3), and "start back from day N" |

- **D20's warm-up is the first user.** It needs the delivery records carried forward, and so does M3's baseline-farming attack.
- **Fresh stays the default**, so every existing result keeps its meaning.

## 4. Bad actors

### 4.1 Structure
- **Attacks are wrappers around an honest agent, in `attacks/`.** This is the folder PLAN.md section 3 already names. A wrapper changes what the agent sends (its nominations, registration or meter claims) or how it plans. The honest agent code is unchanged.
- **An attacker is not bound by the strategy menu.** The menu protects honest agents only. Every defense has to sit in the exchange or the metering. A defense that relies on agent-side code is not a defense. Each M3 attack is a test of that.
- **Config:** per agent, for example `attackers: {PVB_39: {attack: price_pumping, params: ...}}`.
- **Roster:** the attacker set goes in the run header next to the roster. Validators and pass tests read honest and attacker sets from there and fail if any agent is missing (CLAUDE.md).
- **Attacker gain is measured against the same agent run honestly,** with the same seed and window. So every attack cell needs a paired honest run, which doubles the run count.

### 4.2 Which M3 attacks apply under arm C
The M3 list was written for arm A (needs, offers, awards). Arm C is now the clearing (D53), so the list needs sorting:

| Attack | Arm C | Note |
|---|---|---|
| Price pumping | **Yes** | Phantom load in non-binding price rounds |
| Reference gaming (1p) | **Yes** | `max_kw`, farmed `usable_kwh`, SOC at slice start (D56's M3 items) |
| Baseline farming | **Yes** | Through the metered history 1p uses; needs snapshots |
| Coordinated phantom capacity | **Yes** | Registration; D38 grants only demonstrated limits |
| Collusion | **Yes** | Coordinated nominations or withheld response |
| Band riding | **Yes** | The imbalance band (D33) applies in arm C |
| Faulty sensor | **Yes** | Dataset faults as an accidental liar |
| Withholding (arm C analogue of pivotal bidding) | **Yes** | An agent with market power holds back its response to raise the price. New item |
| Nomination gaming | As written, no (it needs offers) | Its arm C form is price pumping |
| Pivotal bidding, bait-and-withdraw, prep inflation, price chasing, double-sell (D16) | No | Arm A only |
| Room squatting | No | Arm B (parked) |

- **Forecast-noise robustness** is not an attack. It applies to both arms and stays in M3.

### 4.3 An LLM red team (owner's call; not in PLAN.md)
- **What it is:** an attacker whose chooser is an LLM told to find a profitable exploit within what the exchange accepts. It picks attacks and their settings from an attack menu, and code builds the messages, so D12 holds.
- **Why it might be worth it:** it may find attacks we didn't list, and it reuses the M4 chooser and the attack wrappers.
- **The risk:** results are harder to reproduce. The response cache covers replay, not variety.

## 5. NANDA Town (M5)
- **The plan is unchanged:** an adapter around `exchange/`, a trace converter, a delivery-record trust plugin and an `energy_flex` scenario with two validators (`docs/research/nanda_town.md` on `nanda-spike`).
- **The spike's adapter predates the arm C message set** (`peak_price`, `selfish_reference`, `peak_settle`). It needs a recheck before M5.
- **The spike's validator lesson is already a project rule:** take the roster from a trusted source and fail on a missing agent (CLAUDE.md).
- **Our LLM chooser lives in our agent, not in NEST.** NEST's `brain: llm` needs `nest-shell` and is not used.
- **M3's attackers are the agents** behind the planned demo contrast (NEST's reputation missing a cheater that only cheats other cheaters).

## 6. What gets built when

| When | What | Gate |
|---|---|---|
| **Now, under M2a** | Risk posture only: `agents/strategy.py` with the label-to-number table, the Fixed chooser, the `peak_response_margin` knob in the arm C re-plan, tests. Built in this design's final shape so M4 does not re-plumb it | Only after the owner approves the D58 risk-premium pre-registration, which sets the margin values and the pass test |
| **M3** (after M2a passes) | `attacks/` wrappers and the arm C attack set; the attacker roster in the header; snapshots and reset (D20 first); the multi-seed runner; the Rule chooser | M2a pass |
| **M4** | The LLM chooser, prompt builder with the firewall test, response cache, cost logging, ablation of LLM vs. Rule vs. Fixed | M3 pass |
| **M5** | NANDA Town adapter recheck and build | M4 pass |

**What "holds for every menu option" would cost.** A check that no one is net harmed under each risk posture on the 11 D58 windows comes to 3 postures × 11 windows = 33 runs. At 15-38 minutes each, one at a time, that is about 8-21 hours. Response cap or participation multiply it further. The scope is the owner's call (for example, 3 postures on 4 windows first).

## 7. Proposed decision entries (as of Oct 28; superseded by PLAN.md D59-D61 and D63-D66, Oct 30)
- **D59 Strategy menu.**
  - The LLM picks labels from a fixed menu, and code maps them to numbers.
  - Menu items may change only the response to Pearl Street signals, never the selfish plan.
  - Three choosers (Fixed, Rule, LLM) share one interface.
  - The strategy is fixed per gate. It refreshes daily after day-ahead prices publish, and on key events between gates.
  - Prompt inputs are limited to what the agent holds, with a test.
  - LLM responses are cached, and the trace header carries chooser, model, prompt version and cache hash.
  - Trigger: the M4 ablation.
- **D60 Memory, snapshots and reset.**
  - State lives on three sides: agent, exchange and metering.
  - A snapshot holds all three. The simulator loads it and passes it in, and its hash goes in the header.
  - There are three reset levels, and Fresh is the default.
  - Trigger: M3 (D20 warm-up).
- **D61 Attacks.**
  - Attacks are wrappers in `attacks/` and are not bound by the menu.
  - Defenses live in the exchange or metering only.
  - The attacker set goes in the header roster.
  - Gain is measured against a paired honest run.
  - The M3 list is re-sorted for arm C as in 4.2.
  - Trigger: M3.

## 8. Owner decisions
1. **Menu contents.** Is it risk posture only to start, or also the response cap?
2. **Participation by day.** In or out? If in, it needs its own entry, since it changes registration and settlement semantics.
3. **Reset scope.** Are the three levels in 3.3 right? Should "Agents fresh" be in M3 or wait for M4?
4. **LLM red team.** In (M3 or M4) or out?
5. **Model and budget for M4.** PLAN.md says "a small model". The model and a dollar cap per run are open, and per-call cost is logged either way.
6. **Robustness scope.** Which postures and windows get the "holds for every option" check (section 6)?

### Owner answers (Oct 30)
**Framing first (D63).** Pearl Street is the exchange, and our agents are reference agents. Every rule must hold if someone else wrote the agent. Guarantees sit in the exchange or metering. Agent-side choices are evidence, not guarantees. Agents own their risk-taking.

1. **Menu (D59):**
   - Risk posture only; no response cap.
   - `normal` (default; the only posture with a stated no-harm result), `cautious` (higher margin) and `bold` (lower margin). `bold` is allowed only by the owner's mandate.
   - Values as settled: `normal` = $10 (passed the D58 horizon test), `cautious` higher, `bold` = m = 0. If D62 is adopted, proposed instead: `normal` = m = 0 under D62, `cautious` = $10, `bold` = a negative margin (to confirm).
   - A battery reserve is a mandate field, not a menu item (D64).
   - External agents aren't bound by the menu.
   - Follow-up: test an agent at m < 0 for its effect on others.
2. **Participation (D64):**
   - No daily opt-out. Enrollment lasts until deregistration, which is filed before day-ahead prices publish.
   - Availability declarations (reserve, planned or forced outage, derate) enter the 1p reference and are checked against the site meter.
   - A silent agent's load is its 1p reference. Today a silent agent freezes its zone's price.
   - Added by the owner: the delivery record and credit line (D65).
3. **Reset (D60, D66):**
   - The three levels stand. There are four sides of state (physical SOC added). SOC carries over only between contiguous runs.
   - Snapshots hold records only. Credit lines are off in Fresh by default.
   - "Agents fresh" is built in M4.
   - January windows stay Fresh. History tests start Feb 1 or later, after a forward warm-up from Jan 18 to 31.
   - Long runs are hosted.
4. **Bad actors (D61):**
   - Scripted misbehavior attacks in M3.
   - Then an open-ended LLM red team, the priority, in attack-fix-attack rounds with collusion (a given channel, an emergent one and a planted one) and a white-box round, in a sandbox.
   - An open-weights model if refusals persist; no jailbreaks.
   - Then the profit-by-any-means month experiment after M4.
5. **Models and budget:** Leaning; the owner will revisit cost (PLAN.md open question 9).
6. **Robustness (D59):**
   - In order: recorded-schedule agents (tests 1p for agents unlike ours), mixed postures (Jan 12-13 and Nov 28-29, 2 seeds), then an equivalence check after the build.
   - `cautious` runs only when a chooser first uses it.
   - Nothing starts without the owner's go-ahead.
