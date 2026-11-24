# M2b plan: data-center workload deferral (owner, Nov 18 evening)

The owner did not stop for plan approval. M2b is built on branch `m2b-dc`, red first, and not merged. Its runs are
judged by the pass test below, which includes all of the owner's criteria (a) to (e) (pre-approved, Nov 18
overnight). Deadline Nov 24, or M2b is cut.

## Scope

**In:**
1. **Data-center workload deferral for the 10 data-center agents.**
   - Each agent may postpone flexible workload, up to `dc_flexible_kw` at each step (the load that could be postponed
     right now).
   - It may serve postponed work later, within its power ceiling: `dc_capacity_kw` minus the load it isn't
     postponing.
   - It must finish each local day's flexible energy by that day's `dc_sessions_2023` deadline.
   - So each day's data-center energy is unchanged. Work is never served before it arrives: cumulative served is at
     most cumulative generated.
   - The agent plans the deferral in its own LP, alongside its battery. It changes only the agent's site net.
     Nominations, 1p references and settlement stay as they are, applied to the new site net.
   - Behind a config flag, `dc_deferral` (off by default).
2. **The LP ceiling rerun with the same load flexibility** (`bench/bench.py build`, `bench/m0.py`): the same deferral
   variables and constraints for the same 10 agents.
   - The battery-only and battery-plus-load ceilings are reported side by side.
   - Share of ceiling is computed against the matching ceiling, so it stays at or below 100%.

**Cut (owner, Nov 18 evening):** item (a), D67's value as arm C's peak-price cap. Arm C keeps D54's 100 $/MWh
placeholder, disclosed in every report.

**Deferred, unchanged:** D2, D3, D4, D6 (arm A designs), D30 (EV smart charging, first in the cut order), and grid
zones on Engie's grid (D35; M2c).

## Pass test (owner's criteria, Nov 18 overnight; committed before any run; judged on unrounded values)

The owner pre-approved a pass test that includes all of (a) to (e). The earlier draft lacked (b)'s bystander line,
(c)'s "solves on every window" and (e), so they are added here, in the owner's terms. Every check reads the roster
from the run header and fails on a missing agent.

**(a) Zero deferral-rule violations,** unrounded, for every data center in every design-window run (exchange and
acting alone), checked against the simulator's session data (`sim.dc_defer.check`; summary field
`dc_deferral.*.violations_total`, with 1e-6 kWh and 1e-6 kW guards):
- no session late: each session's work served by its deadline (`dc_sessions_2023`, local midnight to 04:30 the next
  day);
- total energy unchanged: everything generated in the window is served by the window's end (deadlines past the end
  are clipped to it, disclosed per agent);
- power ceilings held: no step serves more than the session's `charge_power_kw_max`;
- and work is never served before it is generated.

**(b) No one is net harmed:**
- every participant at section 8's tolerance, max(1% of |acting alone|, $10), against acting alone with the same
  deferral (D100);
- **and the bystander line at zero tolerance (D98):** each design-window run with deferral on against its twin with
  deferral off (same config, `dc_deferral: false`), both with `harm_unrounded: true`, on `net_harm.shortfall_usd`
  with the $1e-6 float guard:
  - no agent newly below acting alone (shortfall above $1e-6 with deferral on, at most $1e-6 off);
  - no agent further below beyond section 8's tolerance (D91).

**(c) The ceiling with deferral** (`bench/m0.py --dc`, `bench/m0_dc.py` on Maritime):
- solves on every window it is run on: the 23 M2a $5 windows, which include the four design windows;
- its peak is at or below the battery-only ceiling's on every window (1e-3 MW LP tolerance);
- the design-window runs report share of ceiling and gap share against both ceilings.

**(d) Flag-off parity:** with `dc_deferral` off, the 11 `configs/parity/` runs are identical to main on Maritime
(`bench/parity_check.py`, all five checks; the expected `/header/config/nomination_span` key only).

**(e) Report the data centers' share of relief:**
- in each design-window run, at the acting-alone peak step: the data centers' part of the relief (their effective
  load acting alone minus with the exchange) over the whole relief (`dc_deferral.share_of_relief`);
- in each ceiling, (battery-only peak - with-deferral peak) / (recorded peak - with-deferral peak).
- Report only, no threshold.

**Report only, not judged (D100 (c)):** a data center declares deadlines tighter than its sessions allow
(`dc_declare_tighter`: its sessions end with its own local day instead of 04:30 the next day). One attacker per
design window, chosen by rule: the data center with the most flexible energy in the window. Reported: its gain
against its honest twin (the same window with deferral on, no declaration), the 1p reference against the honest
twin's, and the D98 bystander numbers.

**Runs (pre-registered here; all on Maritime, snapshot `pearl-base-full`, code-only):**
- **Design windows:** jan12, jan06, feb02 and jun29, 2 days each. Configs: `configs/nov01/h2h_c_<w>_h100_m5.yaml`
  (M2a's $5 configs) with only `run_name`, `nomination_span: horizon` (as in `configs/parity/`), `harm_unrounded:
  true` and the M2b keys set.
  - Three runs per window: deferral on, deferral off (the twin for (b)), and the tighter-declaration attack.
- **Ceilings:** `bench.m0_dc` on the 23 M2a $5 windows.
- **Parity:** the 11 `configs/parity/` configs at the branch head.
- **Tests:** full pytest in a sandbox at the branch head.
- **Judge:** `bench/m2b_judge.py` reads the downloaded summaries and writes the verdict per item. No rerun after the
  verdict.

## Build order (red first; pytest and every run on Maritime: the laptop is shared tonight)

1. A data-center deferral constraint module (pure numpy): it builds the per-agent deferral rows and checks a
   schedule against the energy and ceiling rules. Tests on a synthetic day.
2. LP ceiling rows (`bench/bench.py`, `bench/m0.py --dc`). Tests: battery-plus-load at or below battery-only, energy
   kept per day, and flag-off identical.
3. The agent planner (`agents/planner.py`) gets optional deferral variables; the battery agent wiring and
   `sim/run_m1.py` load `dc_flexible_kw`, `dc_capacity_kw` and the sessions behind `dc_deferral`.
4. Maritime: full pytest, parity (flag off), then the four design windows with deferral on. Report only.

## Cost

Code-only. Full pytest about $0.003 per run; parity about $0.13; the four design windows about $0.03; LP ceilings
under $0.01. Total under $0.25.
