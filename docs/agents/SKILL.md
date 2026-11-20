# Pearl Street: operating one battery through MCP

**For:** an agent that has never seen this project. **Interface version:** 0.3-m5 (Nov 19, 2026).

- **0.3** adds a credential per legal entity, registration with a grant (you claim, the platform grants what your battery has demonstrated), a cool-down after a refused registration, size and rate limits with readable refusals, and a pause deadline.
- **0.2** added the "Prices need your nomination" rule and the `open_calls_you_have_not_nominated` field, both found in the first cold-start run.

**All dollar figures are synthetic.** This is a research simulator. Its dollars are simulation outputs, not real money and not market results.

## What Pearl Street offers

Pearl Street is a **flexibility exchange for agents**. Many battery owners, each with its own agent, share one grid zone. The grid operator wants the zone's peak load lower. Pearl Street lets each agent sell that relief without giving up control of its battery.

The run uses 80 simulated battery sites (solar plus a battery, behind a meter) over a 15-minute grid. **You operate exactly one of them.** The other 79 are reference agents written by the project.

**What you can do through this interface:**

- register your battery with the exchange;
- see the operator's calls for nominations and the exchange's peak prices;
- send your planned site net schedule (a *nomination*);
- declare availability or an outage;
- step the simulation forward;
- read your settlement at the end.

The exchange only knows what agents send it and what meters report.

**How money flows ("arm C, rule 1p"):**

1. **Calls.** The operator sends `call_nominations` for a window of steps. A call is either non-binding (information only) or `binding: true`, and each has a `deadline_utc`.
2. **Price rounds.** Before a call's deadline, the exchange runs price rounds. Each round broadcasts `peak_price`: a $/MWh price per step of the window. Prices are high where the zone is over its target and zero elsewhere. Agents may re-nominate between rounds. The last round has `final: true`.

   **Prices need your nomination.** The exchange measures the zone's load from everyone's latest nominations, and only at steps where **every** registered agent in the zone has nominated. While you haven't nominated an open call, its peak price stays at 0, for you and for every other agent. Don't wait for a nonzero price before nominating. Nominate your best plan when the call arrives, even if it's just your battery-idle net plus what you intend to do with the battery, then refine it in later rounds before the deadline. `advance` lists any open calls you haven't nominated under `open_calls_you_have_not_nominated`.
3. **Settlement of the priced steps.** At a binding call's deadline, the price in force is settled for those steps. At the end of the run each agent gets `peak_settle`:
   - `charge_usd` = settled price × your **metered** net load
   - `refund_usd` = settled price × your **acting-alone reference** net load
   - `net_usd` = refund − charge

   The **acting-alone reference** is a battery schedule the metering service computes for your site before the binding call's first price round. It comes from your registration and public prices only, and is what your battery would do with no exchange. So `net_usd` > 0 means you drew less from the grid (or exported more) than acting alone, at the steps that were priced.
4. **Imbalance.** At each step of a binding call's window, your metered site net is compared with your binding nomination. Deviation beyond a free band of max(`band_pct` × power, `band_min_kw`) pays `imbalance_usd_mwh`, which is $500/MWh in this run. It arrives as an `imbalance` message.

Day-ahead energy prices (`read_public`) are public. Day D's prices are published at 12:00 local time (US Central) on day D−1.

## Setup

**Requirements:** Python 3.12 and a checkout of the repository, with the project's data folder next to it at `../data`. The simulation data is not distributed with the repository.

The server is a stdio MCP server with no dependencies beyond the project's own. Add it to your MCP client's config, for example `.mcp.json`:

```json
{
  "mcpServers": {
    "pearl-street": {
      "command": "<repo>/.venv/Scripts/python.exe",
      "args": ["-m", "iface.mcp_stdio", "--config", "<repo>/configs/hw9/hw9_cold_jan12.yaml"],
      "env": {"PYTHONPATH": "<repo>", "PEARL_STREET_CREDENTIAL": "<your credential>"}
    }
  }
}
```

**Your credential.** The platform issues one credential per legal entity, when the entity enrolls. It identifies your legal entity, not a message. The server reads `PEARL_STREET_CREDENTIAL` once at start, keeps only its hash, and never writes it to a log. With it, you can act only for the battery agent that the metering service attests to your entity.

- A config with a `gateway.credential_sha256` block requires the credential. Without it, every tool call is refused with `no_credential` or `bad_credential`.
- A config without a `gateway` block (the HW9 configs) keeps local-process access with no credential.
- The run's operator makes a credential with `python -m iface.credentials --entity "<entity>"`. That prints the token for you and the hash for the config.

**What the config sets:**

- **Window:** Jan 12–13, 2023, US Central (192 steps of 15 minutes).
- **Your battery:** the battery named under `attackers: [{agent: <id>, type: external}]`. That key is the simulator's only hook for a per-agent wrapper. An outside agent is **untrusted by design, not presumed malicious**.

The server answers `initialize` at once and starts the simulation in the background. Its log goes to `runs/<run_name>/server.log`. Every request and response is recorded in `runs/<run_name>/mcp_log.jsonl`. The full message trace goes to `runs/<run_name>/<variant>/trace.jsonl`, and its first line holds the config, seed and git commit.

## How a session runs: pause and advance

The simulation **pauses** and waits for you at these points:

- **Registration** (step 0). You must call `register` and should call `subscribe`, then `advance`.
- A step where the exchange sent you something new: a call, a reject, an imbalance charge, or a settlement.
- **Price round 1** and the **final price round** of each call. In between, your latest nomination stands.

While paused, your sends are **queued**. They reach the exchange when you call `advance`, stamped with the current simulated time, and the exchange's replies (rejects, for example) appear in `read_inbox` at the next pause.

`advance` waits at most 60 s. If it returns `"status": "running"`, call it again. It never skips a pause. When it returns `"status": "finished"`, the run is over and `get_settlement` has everything.

A 2-day run has roughly 10–25 pauses. The 79 reference agents make each `advance` take seconds to about a minute of wall time.

**The pause deadline.** While the simulation is paused for you, every other agent waits. So each pause has a deadline:

- a cap on tool calls within one pause (`max_calls_per_pause`, 60 by default);
- a wall-clock backstop (`pause_wall_s`, 1,800 s by default).

If you miss the deadline:

- the pause ends without you: `pause_deadline_missed`, with `discarded_sends`;
- whatever you queued during that pause is discarded;
- you count as silent for that gate (where the run sets D78's silent reference, as at your acting-alone reference);
- the simulation resumes.

If the missed pause was the registration pause, you're registered at your demonstrated power and stay inert. `advance` reports `pauses_missed`.

## Tools

Each send tool maps one-to-one onto an exchange message. The rest only read.

| Tool | Inputs | Output |
|---|---|---|
| `my_asset` | none | `agent_id`, `asset_id`, `legal_entity`, `zone`, `power_kw`, `capacity_kwh`, `soc_min_kwh`, `soc_max_kwh`, `soc_now_kwh`, `step_minutes`, `window`, `now_utc`, `battery_idle_site_net_kw` (one value per step of the whole window: site net kW with the battery idle; + export, − import) |
| `register` | `asset_id` (from `my_asset`), `max_kw` (your claim, > 0); optional `legal_entity` and `zone`, which must match | `{"ok": true, "msg_id", "status": "queued", "claimed_kw", "granted_kw"}`: the platform grants min(claim, demonstrated `power_kw`) and sends that. Or a refusal (nothing sent; see below) |
| `subscribe` | `signals` (use `["call_nominations", "peak_price"]`), `zones` (`[your zone]`) | queued. **Without it you see no calls or prices.** |
| `read_inbox` | none | `messages`: the exchange's messages since your last read, each `{msg_id, sender, to, t_utc, msg_type, payload}` |
| `read_public` | none | published day-ahead prices: `[{t_utc, usd_mwh}]` |
| `nominate` | `call_id`, `zone`, `window` (copied exactly from the call), `kw`: one number per 15-minute step of the window, your planned **site net** kW (+ export, − import) | queued; may carry `warnings` (below) |
| `declare_availability` | `asset_id`, `date` (YYYY-MM-DD, local), `power_kw`, `reserve_kwh`, `planned_outage` | queued (optional) |
| `declare_outage` | `asset_id`, `window` | queued (optional) |
| `advance` | none | `status` (`paused`, `running`, `finished`, `error`), `step`, `t_utc`, `reason`, `unread_messages`, and when relevant `open_calls_you_have_not_nominated` with an `attention` note |
| `get_settlement` | none | your `peak_settle`, `imbalance` (and any `settle`, `delivery_record`) messages verbatim; after the run, also `acting_alone_reference` (per binding call: `call_id`, `window`, `net_kw`) and `metered_site_net_kw` (per step) |

Refusals come back as `{"ok": false, "code": ...}` with a constant code and, where useful, structured numbers. They never repeat your input back.

| Code | Meaning | What to do |
|---|---|---|
| `no_credential`, `bad_credential` | no credential, or one the platform didn't issue | set `PEARL_STREET_CREDENTIAL` |
| `not_your_agent`, `not_your_asset`, `not_your_entity`, `not_your_zone` | the agent, asset, entity or zone isn't the one attested to your credential | use the values from `my_asset` |
| `already_registered` | you already sent a registration that wasn't rejected | nothing; it stands |
| `registration_cooldown` | a refused or rejected registration started a cool-down; `retry_after_utc` (simulated time) says when you may retry | wait, then re-apply (a lower claim is fine) |
| `message_too_large` | the arguments are over `max_bytes` (64 KB); a raw JSON-RPC line over 256 KB gets a JSON-RPC error with the same message | send less |
| `rate_limited` | more than `max` sends (8) in one simulated step (`limit: sends_per_step`) | `advance` |
| `pause_deadline_missed` | the pause's tool-call cap was reached (see above); refused calls count too | `advance` sooner |
| `unknown_tool`, `bad_arguments` | not a tool, or a missing or extra argument (no tool takes a sender) | check the tool list |
| `bad_message` | fails the exchange's message schema | check ids, times and numbers |
| `not_registered_yet`, `sim_running`, `run_over` | as before | `register`; `advance`; `get_settlement` |

### How your battery is dispatched

No message carries battery set-points, so the rule is fixed:

- **In binding windows:** during the window of a **binding** call that you nominated (before the deadline, not rejected), your battery follows your nomination. Battery kW = nominated site net − battery-idle site net, clipped to ±`power_kw`. Physics also clips state of charge to its limits.
- **Everywhere else:** your battery runs the acting-alone plan, which is the project's reference planner at day-ahead prices.

So whatever you nominate for a binding window is what your battery tries to deliver. Anything it can't deliver beyond the free band pays imbalance.

## Limits and permissions

- **One agent per server.** The server is bound to one battery. Every message it sends carries that agent's id as sender, and the message bus refuses any message whose sender doesn't match its origin. You can't speak for another agent, and no tool takes a sender argument.
- **Credential:** one per legal entity (above). Access is still one client per server process. A hosted transport is planned, not built.
- **Registration:**
  - Your legal entity comes from your credential. The asset must be the one the metering service attests to your entity and agent.
  - **Demonstrated, not claimed:** the interface grants at most your battery's demonstrated power and tells you (`claimed_kw`, `granted_kw`). The exchange can also reject a claim above demonstrated power (`over_demonstrated_limit`, under its `register_power_check` setting). The acting-alone reference can use the lower of the two (`reference_power_cap`). So over-claiming earns nothing.
  - A refused attempt starts a cool-down in simulated time (`registration_cooldown_h`, 24 h by default). You may then re-apply, with a lower claim if you like.
  - One rejected registration still stops the whole simulated run. That's a known limitation of the simulator harness, not an exchange rule, and a fix is proposed. The interface's checks are there so it doesn't happen.
  - `advance` is refused until you've called `register`.
- **Nominations:**
  - They're closed at the call's `deadline_utc`. A later one gets `reject` with `after_gate_closure`.
  - `call_id`, `zone` and `window` must match the call, and `kw` must have exactly one finite number per step.
  - **The exchange does not check a nomination against your battery's power,** because it has no baseline for your site. The interface does: if nominated − battery-idle net exceeds `power_kw` at any step, `nominate` still sends the message but returns `warnings: [{code: "exceeds_asset_power", steps, worst_step_index, worst_implied_battery_kw, power_kw, detail}]`. Heed it, or you'll pay imbalance.
- **Reject codes** (in `read_inbox`, `payload.code` and `payload.detail`):

  | Code | Meaning |
  |---|---|
  | `outside_mandate` | outside your registered mandate |
  | `over_demonstrated_limit` | above the battery's demonstrated power |
  | `after_gate_closure` | sent after the call's deadline |
  | `not_registered` | not registered |
  | `unauthorized_sender` | wrong agent or owner |
  | `bad_message` | wrong call, zone, window or step count |

- **Not supported here:** deregistering, more than one battery, sending to other agents, and anything outside the tool list.
- **Concurrency:** the platform serializes every tool call, so two clients of one entity can't commit the same kW twice (tested with racing threads).

## A small example

This is the start of a session. It is not a strategy.

```
my_asset         -> {"asset_id": "X/bess", "legal_entity": "X Owner LLC (simulated)", "zone": "SYS",
                     "power_kw": 1000.0, "battery_idle_site_net_kw": [...], ...}
register         {"asset_id": "X/bess", "max_kw": 1200.0}
                 -> {"ok": true, "msg_id": "X:t1", "status": "queued", "claimed_kw": 1200.0, "granted_kw": 1000.0}
subscribe        {"signals": ["call_nominations", "peak_price"], "zones": ["SYS"]}  -> {"ok": true, ...}
advance          -> {"status": "paused", "t_utc": "2023-01-12T06:00:00Z",
                     "reason": "new exchange messages: call_nominations"}
read_inbox       -> {"messages": [{"msg_type": "call_nominations", "payload": {"call_id": "GRID_OP:1",
                     "binding": false, "window": {"start_utc": "...", "end_utc": "..."},
                     "deadline_utc": "...", "zone": "SYS"}}]}
nominate         {"call_id": "GRID_OP:1", "zone": "SYS", "window": {...copied...}, "kw": [ ...one per step... ]}
advance          -> ...
```

To work out the index of a step inside `battery_idle_site_net_kw`, take its offset from `window.start_utc` in `my_asset` in 15-minute steps.
