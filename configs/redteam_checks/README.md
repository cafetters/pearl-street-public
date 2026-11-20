# Red-team harness checks (not red-team rounds)

Configs here exercise the harness itself, report only. They are not covered by D-e (every `configs/redteam/*.yaml`
turns both M5 defenses and `silent_reference` on), because they run today's exchange on purpose: the planted exploit
must be found so that confirmation, the ablation control, D-d's class and each defense layer run.

- `real_dec11_exploit.yaml`: the harness on a real-data world (D61 v2's dec11 twin config, a used window), fake
  backend (owner, Nov 19, item 4: one real-data harness run on Maritime, report only).
- `cloud_real_dec11.yaml`: its launcher config (`python -m bench.cloud_queue --run <this> <m0 folder> --module
  redteam.launch ...`).
