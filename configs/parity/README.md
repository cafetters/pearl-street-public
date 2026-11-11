# Parity reference configs (owner, Nov 6)

The 11 configs every cloud parity check runs from now on. They are byte-identical copies of `runs/nov06/span_par/`.
Those are the `fix1-par2` parity set (`../upload/h2h_c_jan12_h100_m0_max.yaml` and
`../pearl-street-cloud/runs/cloudtest/s4/h2h_c_<w>_h100_m5.yaml`) with one line added:
`nomination_span: horizon`.

- **Why the line.** After D86's default flip, a config without it runs `operating_day`. Eight of these set
  `d62_guarantee: true`, and D62 refuses any span but `horizon`. The other three (jan12 m0 max, jan16, nov28) would
  silently change span.
- **Reference outputs.** `../pearl-street-cloud/runs/cloud/_par_main` (main `74b1ae0`, setting absent, so `horizon`).
  At `34f7315` these configs reproduced them exactly (telemetry, trace text, payout, commit) [sim, in-run, Nov 6,
  queue `span-par`].
- **The expected summary difference.** `bench/parity_check.py` compares `summary.json`, whose header records the
  config. So it shows one gated difference against `_par_main`: `/header/config/nomination_span` (`'horizon'` vs
  missing). The owner judged that PASS on Nov 6.
  - A reference set rerun from these configs removes the difference.
  - Any other gated difference is a FAIL.
- **M0 folders.** Each config's `m0_dir` is `runs/m0_<w>`, which is git-ignored. Pass the local folder as the
  launcher's `M0_FOLDER`.
- **Not a pre-registration.** Some files keep a header comment from their source saying "Pre-registered". That
  describes the source run, not this folder. Edit these only through a decision-log entry, because they are the
  parity baseline.

Run (from the repo root): `python -m bench.cloud_queue --run configs/parity/<name>.yaml runs/m0_<w> [...] --commit
<commit> --concurrency 10 --budget 13 --measured-runtime-s 1625 --max-awake 10`, then `python -m bench.parity_check
--cloud runs/cloud/<name> --reference ../pearl-street-cloud/runs/cloud/_par_main/<name> --commit <commit>`.
