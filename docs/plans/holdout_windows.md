# Sealed holdout windows (Nov 2, 2026)

**Status: used (owner, Nov 5).** The 24 windows were spent on D76's in-run holdout check (pre-registered `20b77d0`,
run Nov 4, 144 of 144 steps, integrity OK). Its pre-registered verdict, FAIL, stands as recorded: the $5 margin stays
the interim rule (D75), the max rule stays Leaning behind its flag (D76), and the nov11 gate stands as pre-registered
(it isolates the exchange's settlement from forecast luck). These windows are no longer sealed and count as used for
every later pick.

**Purpose (owner, Nov 2, step 7).** These windows are reserved for a single confirmation run of a final settlement
rule. Until then nothing is computed on them: no M0, no feasibility, no run, no cell and no offline tool. Any later
design work uses the 24 windows already used, not these.

**Rule.**
- For each calendar month of 2023, take the two 2-day windows (d, d+1), with d in that month, that have the highest
  recorded system peak.
- A window may share no day with any window used in an earlier test, nor with a window already picked here. Picks go
  month by month, the higher window first.
- The day before and the day after the window must exist.
- **Ties:** the higher first-day peak wins, then the earlier window.
- **Recorded peak:** computed as in the Oct 31 overnight pick (`bench.d58_margin.daily_peaks`, checked against M0's
  recorded peaks for Jan 2 and Jan 16).

**Windows excluded as used (24):**
- the 12 earlier windows: Jan 2-3, Jan 12-13, Jan 16-17, Jan 31–Feb 1, Feb 12-13, Jun 19-20, Jul 1-2, Jul 14-15,
  Aug 28-29, Sep 5-6, Sep 21-22 and Nov 28-29;
- the 12 overnight Oct 31 windows.

Step 8 ran no window (its condition was not met; see `docs/overnight/2026-11-02.md`).

**What was read to pick them:** only the recorded daily system peaks of 2023, minus recorded PV and battery. The script
is `runs/nov02/holdout_pick.py` (git-ignored), sha256 `3ebe28dfc066fb57` (first 16 hex digits). Its output is
`runs/nov02/holdout.json`.

| Month | Rank | Window | Tag | Window peak MW | First-day peak MW |
|---|---|---|---|---|---|
| Jan | 2 | 2023-01-04 to 2023-01-05 | `jan04` | 2,491.2 | 2,491.2 |
| Jan | 3 (replaces `jan18`, Nov 3) | 2023-01-14 to 2023-01-15 | `jan14` | 2,465.8 | 2,465.8 |
| Feb | 1 | 2023-02-08 to 2023-02-09 | `feb08` | 2,359.1 | 2,359.1 |
| Feb | 2 | 2023-02-06 to 2023-02-07 | `feb06` | 2,348.7 | 2,348.7 |
| Mar | 1 | 2023-03-12 to 2023-03-13 | `mar12` | 2,157.9 | 2,157.9 |
| Mar | 2 | 2023-03-06 to 2023-03-07 | `mar06` | 2,142.2 | 2,020.4 |
| Apr | 1 | 2023-04-08 to 2023-04-09 | `apr08` | 2,094.4 | 2,094.4 |
| Apr | 2 | 2023-04-04 to 2023-04-05 | `apr04` | 2,091.9 | 2,091.9 |
| May | 1 | 2023-05-27 to 2023-05-28 | `may27` | 2,316.5 | 2,316.5 |
| May | 2 | 2023-05-31 to 2023-06-01 | `may31` | 2,295.9 | 2,295.9 |
| Jun | 1 | 2023-06-16 to 2023-06-17 | `jun16` | 2,407.9 | 2,407.9 |
| Jun | 2 | 2023-06-08 to 2023-06-09 | `jun08` | 2,338.7 | 2,338.7 |
| Jul | 1 | 2023-07-11 to 2023-07-12 | `jul11` | 2,380.3 | 2,380.3 |
| Jul | 2 | 2023-07-09 to 2023-07-10 | `jul09` | 2,345.8 | 2,198.3 |
| Aug | 1 | 2023-08-05 to 2023-08-06 | `aug05` | 2,349.6 | 2,349.6 |
| Aug | 2 | 2023-08-03 to 2023-08-04 | `aug03` | 2,330.4 | 2,193.7 |
| Sep | 1 | 2023-09-15 to 2023-09-16 | `sep15` | 2,348.9 | 2,348.9 |
| Sep | 2 | 2023-09-03 to 2023-09-04 | `sep03` | 2,328.2 | 2,328.2 |
| Oct | 1 | 2023-10-02 to 2023-10-03 | `oct02` | 2,134.7 | 2,110.2 |
| Oct | 2 | 2023-10-09 to 2023-10-10 | `oct09` | 2,130.5 | 2,130.5 |
| Nov | 1 | 2023-11-26 to 2023-11-27 | `nov26` | 2,219.6 | 2,131.2 |
| Nov | 2 | 2023-11-11 to 2023-11-12 | `nov11` | 2,209.2 | 2,209.2 |
| Dec | 1 | 2023-12-05 to 2023-12-06 | `dec05` | 2,300.2 | 2,135.0 |
| Dec | 2 | 2023-12-09 to 2023-12-10 | `dec09` | 2,252.0 | 2,252.0 |

**Swap (owner, Nov 3): `jan18` replaced by `jan14`.**
- **Why:** Jan 18-19 lies inside D66's forward warm-up span (Jan 18 to 31), and a warm-up run would break the seal.
  The owner chose to swap it for the next January window by the same rule.
- **How:** the same rule, re-run with these excluded:
  - the 24 used windows;
  - the 23 other sealed picks;
  - Jan 18-19 itself;
  - every day of the warm-up span.
- **Result:** Jan 14-15, window peak 2,465.8 MW. The rule picks the same window without the warm-up exclusion. The
  next candidates were Jan 9-10 and Jan 8-9 (2,203.8 MW).
- **Checks:** the script checks that M0's recorded peaks and all 24 old picks reproduce exactly.
- **What was read:** only recorded daily peaks.
- **Script:** `runs/nov02/holdout_swap.py` (git-ignored), sha256 `677018e14df587e5` (first 16 hex digits). Output:
  `runs/nov02/holdout_v2.json`.
- **Timing:** committed before anything else was computed on Nov 3.
- Jan 18-19 is no longer sealed. Nothing has been computed on it, and it may now be used by the warm-up.
- The table keeps 24 windows. January's are now `jan04` and `jan14`.
