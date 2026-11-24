# Single-LLM-agent runs: pre-registration (owner, Nov 24, 2026)

**Status: judged PASS (Nov 24, 05:15 ET).** Integrity, run health and determinism hold in all nine runs (`runs/s10_judge.json`; integrity diff against 93924f0 empty). Results and disclosed deviations below. Pre-registered, before any run on the window below. The commit that adds this file is the pre-registration
commit, and every run below runs at it. The verdict is recorded at the top of this file, as is, once judged.

## What this is

Agreed LLM order (owner, Nov 23): first, one honest LLM agent among rule agents, 10 days, three models, paired with the
same agent on the Rule chooser. Then a free-hand honest agent and a single attacker (separate pre-registrations).

These runs are measurements:
- whether a real LLM chooser works end to end over 10 days;
- what it costs per day, in tokens and dollars;
- what it chooses against the Rule.

**What the LLM controls (owner's question, Nov 24):** once a day, agent X's posture label: `normal` ($5/MWh
peak-response margin), `cautious` ($10) or `bold` ($0) (D92). Code turns the label into the margin. The LLM writes no
number, bid or message (D12). Its prompt holds only allow-listed fields of X's own data (D59/D70). An invalid answer is
refused, and X keeps its previous label.

## Owner decisions (Nov 24)

1. The effect on everyone else is **reported, not gated**, beside three fixed-label controls. A single agent's legal
   label moves the others through prices: on Jan 12-13 one `cautious` day moved about $90 from 79 agents to it. So a
   zero-tolerance gate would judge the market, not the LLM.
2. **Window:** D61 v2's rule extended to 10 days over the whole year (below). Nov 16-25 is kept for the red-team
   round.
3. **A model refusal is not a run failure.** It is counted and reported, and the agent keeps its previous label.
4. **One shared Rule twin**, with a determinism copy.
5. **`bold` is allowed** for X: three labels, not two.
6. X's own result against acting alone is reported, not gated (D63: an agent's own risk).

**Defaults chosen here (not set by the owner):**
- X = the lower-median battery power agent, ties by id (D97 `size_gate`'s convention).
- Run cap $0.50, phase cap $2.00.
- Cadence `daily`.
- The retry rule below.

## Window

`bench/window_pick10.py` (pinned below) applies the rule:
- **The rule:** the clean 10-day stretch of 2023 with the highest recorded system peak. Ties go to the higher
  first-day peak, then the earlier start.
- **Clean** means no day in any earlier test, holdout, warm-up, span, D61 v2 window, red-team window, or any committed
  config, and the day before and the day after exist.
- **Fail-closed check:** no YAML config on disk in any of the 38 worktrees overlaps the pick.
- **Result: Jul 17-26, 2023, peak 2,326.7 MW.** Next were Jul 16-25 (the same peak, lower first day) and Aug 7-16
  (2,324.0 MW). The best fall stretch, Nov 1-10, was not used: it contains the clock change (Nov 5), which no run has
  crossed.

## Agent X

**PVB_33**, 2,495.8 kW, the 40th of 80 by battery power. Its neighbours are PVB_37 at 2,434.7 kW and PVB_15 at
2,841.5 kW.

## Runs (all at this commit, on Maritime; configs in `configs/single_llm/`)

**Shared setup:** each config is a copy of the staged pilot config (`runs/nov12/pilot/m4pilot_jan12_daily.yaml`), with
`start`/`end` set to Jul 17-26, `m0_dir: runs/m0_jul17`, `harm_unrounded: true` (D98), and `llm_strategy` with
`chooser: rule`, `bold_allowed: true` and `cadence: daily` in every run.

| Run | X's chooser | Notes |
|---|---|---|
| `s10_jul17_twin` | Rule | the shared twin; the launcher's `--pilot`, which measures the runtime |
| `s10_jul17_twin_copy` | Rule | determinism copy |
| `s10_jul17_llm_haiku` / `_luna` / `_sonnet` | LLM, course route | `honest_models` pinned to one model; cap $0.50 each; cache recorded |
| `s10_jul17_ctrl_normal` / `_cautious` / `_bold` | Fixed | `fixed_label` as named |
| `s10_jul17_replay_haiku` | LLM, cache only | replays `s10_jul17_llm_haiku`'s cache; no model call; a second queue |

**M0:** `python -m bench.m0 configs/single_llm/m0_jul17.yaml`, then with `--controllable`, built on the laptop after
this commit is pushed.

## Gates (verdict order; PASS needs all three in every run)

`bench/single_llm_judge.py` (pinned) with `--x PVB_33 --commit <this commit>`.
1. **Integrity:**
   - every trace header has this commit, not dirty;
   - start, end and seed are equal across runs;
   - configs differ from the twin only in `run_name` and these `llm_strategy` keys: `per_agent`, `honest_models`,
     `route`, `backend`, `cache`, `fixed_label`, `cap_usd_run`, `cap_usd_phase`, `phase`;
   - rosters and D101 statuses are equal;
   - choosers are Rule everywhere except X (LLM in an LLM run, Fixed in a control);
   - `t4_threshold_usd_mwh` is equal.
2. **Run health:**
   - `bench/course_route_check.py` PASS on each LLM run: cost reconciled on every call within $0.000002 + 1%, no
     backend error, no missing cost header, no blocked term or key in any file;
   - the cap was never hit;
   - every label X applied is on the menu;
   - no Rule agent applied `bold`;
   - each control's X kept its label every day.
3. **Determinism:**
   - the twin matches its copy, on the summary and the trace body, ignoring volatile keys and run names (as
     `bench/d58_margin.py`);
   - an LLM run whose X labels equal the twin's matches it on the exchange values;
   - the replay matches its source on the exchange values and every agent's labels.

## Reported, not gated

- **For each LLM run and each control, against the twin:**
  - `bench/bystander_check.py` on the unrounded basis, with X as the treated agent: others newly below acting alone,
    further below beyond section 8's tolerance and at zero tolerance, and the twin-relative loss;
  - label differences for every agent.
- **X:** its value minus the twin's, in total and per day, and its shortfall against acting alone. PLAN claim 2:
  three runs are too few to judge "LLM earns its keep"; if they tie, we say so.
- **LLM:** calls, prompt and output tokens per day, cache writes and reads, refusals, the reason for each call, and
  reported dollars per simulated day.

## Run order, retries and stopping

1. Push this commit, then run a full pytest on Maritime at this commit (0 failed). The M0 build follows on the laptop
   (0.85 GB rule).
2. Queue 1 is the twin as `--pilot`. Queue 2 is the other seven runs, with the measured runtime. Queue 3 is the replay,
   with `--runs-from` pointing at the downloaded Haiku run.
3. **Retry:** a run lost to a gateway, sandbox or launcher failure (not to a gate) is rerun once at the same commit and
   disclosed. No code or config may change after the first run counts. A crash in the code stops the round, and the
   fix goes through a new pre-registration.
4. **Judge:** `git diff <this commit> -- agents/ sim/ exchange/ bench/ configs/single_llm/` must be empty at verdict
   time. The verdict is committed as is.

## What was seen before this was written

- **Nov 23, Jan 12-13 single-agent smoke runs** (`configs/course_route/`, PVB_01, two days):
  - all three models were valid, and cost reconciled;
  - Haiku and Sonnet stayed `normal`, and luna picked `cautious` on day 2, as the Rule agents did;
  - the reviewer's cents reading of Haiku against luna: one label-day of difference moved X by +$91.69 and the others
    by −$90.50 net; 9 went further below acting alone (the largest $8.09 against a $10 tolerance), and none went newly
    below.
- **The Jan 12-13 judge validation set** (`configs/single_llm/validation/`, X = PVB_33): twin, copy, a Haiku LLM run,
  three controls and a replay, at commit a12167d. PASS; recorded below before any run on the window.
- **The gateway** caches prompts by itself: a write costs 2× input and a read 0.1×. The daily prompt changes, so
  every call is a write.

## Disclosures

- The window rule is new: an extension of D61 v2's 2-day monthly rule, written for this test.
- Jul 17-26 is the first 10-day window run with LLM agents. The 7-day v2 pilot (Feb 10-16) is the longest earlier run.
- `price_rounds_per_gate: 30` was justified on 2-day timing, and is kept unchanged.
- `bench/bystander_check.py` was pinned by D61 v2 (a finished verdict). It has changed since: the D101 guard, and here
  an optional treated-agent argument whose default path is unchanged.
- Temperature can't be pinned (D95), so each LLM run is one draw. The replay covers the LLM path's determinism, not the
  model's.

## Pins (sha256 at this commit)

Content sha256 with line endings normalized to LF (the same on Windows and on Maritime).

| File | sha256 |
|---|---|
| `bench/single_llm_judge.py` | `83837c722deda74aa0ac032779d39579d14c0f22bcfb447befd10b8202006c64` |
| `bench/course_route_check.py` | `8c42098717a348930735d1b67c748bbfa72c7628c06ab4dfc88c20556ba79bbd` |
| `bench/bystander_check.py` | `853d34e98951cdefd432814ff70fec2c0655cfe158b9639ee4c40c62d67e33c4` |
| `bench/roster_cover.py` | `fddfacc6a6625cc9b7ee1af589816db8e34b0d6437ecffce228794d89d86e7c2` |
| `bench/window_pick10.py` | `b62cc5f9276da79538444bae5610d09060aff017a5335e45c68c4211d47bf82d` |
| `agents/llm/course_route.py` | `f8551e0b10155b88e745a9d07449f7d42a7ac7b9cdedfe52392cf0362294d304` |
| `agents/llm/spend.py` | `9ae2d98213bb75c430380c4f4002c2eb71bfc8877db85dca4bb947c5a8964619` |
| `agents/llm/chooser.py` | `565a2c7d7821978c1711aecc0e1d0f90f9f9c913f044475cf410a6dadb90a09c` |
| `configs/single_llm/m0_jul17.yaml` | `baed4c6af0f86873f5af84991fde8460a8a5ca9e2b851ff8e05103a9a9aa40ae` |
| `configs/single_llm/s10_jul17_ctrl_bold.yaml` | `24bbf214969cc909443e7ef3967b03d6c40527746bf3cbca8dec2c28166ea8b9` |
| `configs/single_llm/s10_jul17_ctrl_cautious.yaml` | `ea57a5c06dfa2dddae97203386b00c86fb5191603b5fe89cf37f2702686fcf4c` |
| `configs/single_llm/s10_jul17_ctrl_normal.yaml` | `983f39c24ed77b529eb3f54041743f56a8aa8e2312ed8e90a8ee154823ee4e04` |
| `configs/single_llm/s10_jul17_llm_haiku.yaml` | `a936a23c8df2d564535f6e01f338957c461102f1ba057d65d8395f0977162d9b` |
| `configs/single_llm/s10_jul17_llm_luna.yaml` | `aac4b5b3f3913f847c6a86e4ac3ec8103ccca1c73b38c842295f30bc7718b164` |
| `configs/single_llm/s10_jul17_llm_sonnet.yaml` | `e6ef846f68cea27bdd63c274de86731776d4bbb2b66d79d15d2f387186f5b0a0` |
| `configs/single_llm/s10_jul17_replay_haiku.yaml` | `5c18b30ca521f92d64a8fc0f64738dd6ca4db1732bd6a891b1c27267fc4b147f` |
| `configs/single_llm/s10_jul17_twin.yaml` | `3bc33c6638c26e6cc405eabba575de7eed47c972ecc3704e42b8e5208d05bb87` |
| `configs/single_llm/s10_jul17_twin_copy.yaml` | `df0431c315ec9faec17669e7b9415bcaf59cf8877fe02154da9522a6261ca4f7` |

## Judge validation (Jan 12-13, recorded before any run on the window)

**PASS at commit a12167d** (`runs/vjan12_judge.json`, judged 00:49 ET Nov 24): integrity, run health and determinism all pass across the twin, its copy, the Haiku LLM run, the three controls and the cache-only replay (the replay matched its source on every exchange value and every agent's label). The judge fails closed on real data with a wrong X (integrity, health and determinism all fail) and with a wrong commit (integrity fails).

Reported (Jan 12-13, a design window; nothing here is judged):

| Run | X's labels | X minus twin ($) | others newly below acting alone | further below beyond tolerance | twin-relative loss |
|---|---|---|---|---|---|
| vjan12_llm_haiku | {'2023-01-12': 'normal', '2023-01-13': 'normal'} | +32.72 | 0 | 0 (6 at zero tolerance) | 47 agents, -60.98 |
| vjan12_ctrl_normal | {'2023-01-12': 'normal', '2023-01-13': 'normal'} | +32.72 | 0 | 0 (6 at zero tolerance) | 47 agents, -60.98 |
| vjan12_ctrl_cautious | {'2023-01-12': 'cautious', '2023-01-13': 'cautious'} | -0.02 | 0 | 0 (3 at zero tolerance) | 36 agents, -15.24 |
| vjan12_ctrl_bold | {'2023-01-12': 'bold', '2023-01-13': 'bold'} | +11.60 | 0 | 0 (3 at zero tolerance) | 30 agents, -37.31 |

The Rule twin put X on `cautious` on day 2; Haiku kept `normal` both days, so its run equals the fixed-`normal` control to the cent. One label-day of difference moved X by +$32.72 and 47 others by -$60.98 in total, with none pushed below acting alone.

## Verdict and results (Nov 24, 2026; recorded as judged)

**PASS.** `bench/single_llm_judge.py --x PVB_33 --commit 93924f0` on the twin, its copy, the three LLM runs, the three controls and the cache-only replay: integrity ok, run health ok (`course_route_check` PASS on each LLM run), determinism ok (twin = copy; the replay matched Haiku's run on every exchange value and every agent's label).

Reported, not gated (Jul 17-26, against the all-Rule twin; X's shortfall: negative = above acting alone):

| Run | X's labels (days) | X minus twin ($) | X's shortfall vs acting alone ($) | others newly below acting alone | others further below beyond tolerance | others below their twin (agents, $) | LLM |
|---|---|---|---|---|---|---|---|
| llm_haiku | 10 normal | +257.49 | -578.36 | 0 | 0 (0 at zero tol.) | 49, -1468.43 | 10 calls, $0.0036 |
| llm_luna | 2 normal, 8 cautious | -71.71 | -249.16 | 0 | 0 (0 at zero tol.) | 33, -334.36 | 10 calls, $0.0022 |
| llm_sonnet | 10 normal | +257.49 | -578.36 | 0 | 0 (0 at zero tol.) | 49, -1468.43 | 10 calls, $0.0475 |
| ctrl_normal | 10 normal | +257.49 | -578.36 | 0 | 0 (0 at zero tol.) | 49, -1468.43 | - |
| ctrl_cautious | 10 cautious | -75.59 | -245.28 | 0 | 0 (0 at zero tol.) | 29, -706.25 | - |
| ctrl_bold | 10 bold | +505.13 | -826.00 | 0 | 0 (0 at zero tol.) | 47, -1192.90 | - |

Reading:
- **Labels:** Haiku and Sonnet chose `normal` on all 10 days, so their runs equal the fixed-`normal` control to the cent. luna chose `normal` twice, then `cautious` for eight days. No model ever chose `bold`, though it was allowed. The Rule chooser put X on `cautious` on 5 of 10 days.
- **Claim 2 (LLM against the Rule), reported:** on this window `normal` beat the Rule's mix for X by $257, and `cautious` lost $76. So Haiku and Sonnet did better for X than the Rule, and luna did slightly worse ($72). With one window and three draws, this is not evidence that the LLM earns its keep; the choice amounts to "stay `normal`".
- **Everyone else:** no other agent was pushed below acting alone in any run. Twin-relative losses are a transfer through prices, the same size as the fixed-label controls': a legal label moves others' money; the LLM did nothing a Rule agent couldn't.
- **Cost:** 10 calls per run (one start call and nine trigger calls). Haiku $0.0036, luna $0.0022, Sonnet $0.0475. Every Claude call was a prompt-cache write.

Disclosed deviations from the run order (operational; no code, config or commit changed):
1. **The other seven runs were queued before the twin pilot finished,** with a runtime estimated from its partial progress (July is slow: the twin's first simulated day took about 40 min). They ran in two queues (three LLM runs, four code-only), because an LLM queue allows at most 5 sandboxes. The first two queues (`s10-llm`, `s10-code`) were stopped by me before any run started or any model was called, because their time limit (15 h) looked too short. They were relaunched as `s10-llm2` and `s10-code2` with a 23.9 h limit.
2. **All launchers were reaped for low memory at 07:20Z.** `runs/m5/rescue/rescue.py`, started as a hidden process within 4 minutes, kept every sandbox awake and downloaded every run with its sha256 checked. Every queue's cleanup was then confirmed.
3. **Runtimes:** the twin's exchange variant took 5,558 s alone; the replay took 7,093 s.
