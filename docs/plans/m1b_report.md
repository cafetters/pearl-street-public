# M1b report: run viewer

Branch `viewer`. Not merged into `main` and not pushed. Decisions and open questions are in `m1b_questions.md`.

## What was built
- **`exchange/trace/viewer.py`:** `python -m exchange.trace.viewer runs/<run>` writes `runs/<run>/viewer.html`.
  - **Inputs:** only `runs/<run>/summary.json` and `runs/<run>/*/trace.jsonl`.
  - **Output:** one file with no network requests, using inline CSS, vanilla JS and SVG, light and dark.
  - **Variant picker:** selfish and every exchange variant, including the rounds-sweep runs.
  - **Summary tiles:** shown straight from `summary.json`, with the path to each block.
  - **Net-load chart:** recorded, selfish, exchange and the M0 ceiling, with need windows shaded, a hover readout and click to jump.
  - **Scrubbable timeline:**
    - lanes for round-1 needs, later-round needs, offers, revisions, awards, settlements, nomination calls, nominations, zone aggregates, imbalance charges and rejects;
    - a slider plus play, step and next-event buttons;
    - the event list for the current step.
  - **Agent table, sortable:** offers, awards, awarded and delivered kWh, credits, charges, nominations, imbalance kWh and $, and rejects.
- **`sim/run_m1.py`:** writes the net-load series into `summary.json` (`series`), because the viewer may not read `netload.csv` (see question 1).
- **`tests/test_viewer.py`, 5 tests:**
  - self-contained output;
  - counts and agent rollups match the trace;
  - summary blocks are identical to `summary.json`;
  - M1.1 round and sweep mapping;
  - a missing series gets a notice;
  - the inline JS passes `node --check`;
  - the source doesn't read anything but trace and summary files.

## Results
- **Tests:** 100 pass (95 earlier plus 5 viewer tests).
- **Regenerated runs:** M1 and M1.1 were rerun to add `series`.
  - **Full runs:** for `m1` and `m1_1`, every existing summary value is identical. Only `series` was added.
  - **Smoke runs:** `m1_smoke` and `m1_1_smoke` differ, but from fixes already on `main` (M1's offer rounding, and the imbalance "agents charged" threshold), not from M1b.
- **Viewers generated (git-ignored):**
  - `runs/m1/viewer.html` (356 kB, 5 variants);
  - `runs/m1_1/viewer.html` (1.0 MB, 7 variants);
  - both smoke runs.
- **Matches `summary.json`:** for all four, every variant's embedded summary block equals its `summary.json` block exactly, and the series match.
- **Opens offline:** in headless Chrome with the network blocked, both full viewers render 80 agent rows, the chart and the timeline, with no console errors. The tiles show the same peak and unfilled need as `summary.json`.

## Pass test (PLAN.md section 7)
**"The M1 run produces a viewer that opens offline in a browser and matches summary.json":** met, with the browser check done headlessly. Open the viewers yourself to confirm (question 2).

## Not done
- **Nothing merged, nothing deleted, no Decided items in PLAN.md touched.**
- **`docs/PLAN.md` not updated:** there's no change-log line for M1b. The plan's M1b entry already describes this work.
