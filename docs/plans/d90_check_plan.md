# D90 check tool: plan for the owner (DRAFT, Nov 7 overnight; not built)

> **For approval before any code (CLAUDE.md: plan mode first).** This is D61 v2 prerequisite 2
> (`docs/plans/d61v2_attacks_check.md`). Nothing under `bench/` or `tests/` has been written for it.
> The overnight rehearsal used an offline script, `runs/nov07/v2r/d90_recheck.py` (git-ignored). It already
> reproduces the in-run D82 decision on D61's four `infeasible_declare` traces. The tool below is that script made
> into a pinned tool with tests.

## What it decides (D90, as decided Nov 6)

An `infeasible_declare` cell is **judged only where the filed set is infeasible for 1p when filed**. Then D82's
`infeasible_declaration` must fire. Otherwise the cell is **not exercised**.

## Design: `bench/d90_check.py`

```
python -m bench.d90_check <attack_run_dir> [--out file.json]
```

**Inputs.** Everything is read from the run folder: the trace header's config and roster, the attackers, the
exchange trace. Then one data load, the run's own slice through `sim.data.load_slice`. `exchange/` reads no data;
this is a bench tool.

**Steps.** For each header attacker of type `infeasible_declare`:
1. Collect its filings in trace order: `declare_outage` and `declare_availability`, with `asset_limits` and
   `register`.
2. **Replay in filing order**, re-solving the selfish 1p plan (`sim.reference.selfish_plan`, the same function the
   metering service uses) with each set of filings in turn. That means after the first filing, then after the first
   two. Each set's power limits and SOC floors are built exactly as `runs/nov06/infeasible/d82_recheck.py` builds
   them.
3. Report `infeasible_at_filing` (the full filed set has no plan) and which filing made it so.
4. Read the in-run D82 result: a `reject` to the attacker with `code: infeasible_declaration`, either
   - directly after the filing (refused at filing), or
   - later, as a D82 drop (`_void_for_outage`).
5. **Verdict:**
   - NOT_EXERCISED if the filed set was feasible at filing;
   - PASS if it was infeasible and D82 fired;
   - FAIL if it was infeasible and D82 did not fire.
   - Also **DISAGREE** (fails closed) if D82 fired but the replay finds the set feasible: the offline re-solve and
     the in-run check disagree.

**Fails closed (exit 2):**
- no attacker of this type in the header;
- the filings aren't one outage plus one availability;
- an attacker missing from the header roster;
- a dirty or missing commit in the header;
- the config's data window isn't the trace's clock.

**Commit guard.** The tool must run at the run's commit. It refuses if the run header's commit differs from
`git rev-parse HEAD`, as `d82_recheck.py` does with `PYTHONPATH=.`.

**Name.** `d90_check` is not a stdlib module name; it will be checked against `sys.stdlib_module_names` before the
file is created.

## Tests (red first), `tests/test_d90_check.py`

**Synthetic:**
- a filed set that leaves 1p feasible gives NOT_EXERCISED;
- one that doesn't gives PASS with a D82 reject in the trace;
- the same without the reject gives FAIL;
- a reject with a feasible replay gives DISAGREE;
- each fail-closed case above.

**Real traces (CLAUDE.md: before it enters the pre-registration), report only, kept in a test marked to skip
without `../pearl-street-d61run`:**
- D61's four `infeasible_declare` traces, at `74b1ae0`, run from that worktree: feasible on jan12 and feb02
  (NOT_EXERCISED), infeasible with D82 fired on jan06 and jun29 (PASS). The overnight script already gives exactly
  this (`runs/nov07/v2r/d90_recheck_d61.json`).
- The four rehearsal `infeasible_declare` traces (`runs/cloud/h2h_c_v2r_*_infeasible_declare_m5`, at `d471a72`).

## Open question for the owner

Should the D90 verdict fold into a single v2 check tool, together with d61_check's gain, roster and defenses,
bystander_check and D87's `voided_calls` (see the overnight report's prerequisite gaps)? Or stay a separate tool the
pre-registration names? **Recommended:** a separate tool, called by one thin v2 judge that is pinned with the
pre-registration. Each piece then keeps its own tests.
