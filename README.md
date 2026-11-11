# flex-exchange

Snapshot of a private development repo at 4988a6b5564b9dd438a2bacd6df2cd02f564cec8. Published for MIT AI Studio Fall 2026 HW8 review. License TBD.

docs/PLAN.md and some internal notes are referenced in comments but kept in the private repo.

Python 3.12 project for simulating a flexibility exchange on Engie data.

## Layout

| Folder      | Purpose                                                   |
|-------------|-----------------------------------------------------------|
| `exchange/` | Exchange / market mechanism (pure logic, never reads data files) |
| `sim/`      | Simulation driver that loads data and runs the exchange   |
| `agents/`   | Market participant agents                                 |
| `attacks/`  | Adversarial / manipulation scenarios                      |
| `bench/`    | Benchmarks and evaluation                                 |
| `configs/`  | Run configurations (YAML)                                 |
| `runs/`     | Run outputs (git-ignored except `runs/samples/`)          |
| `docs/`     | Project documentation                                     |

## Data

Engie data lives in `../data` (sibling of this repo) and is never copied here.
Configs reference it via `data_dir: ../data`. All timestamps are UTC.

## Setup

```sh
python -m venv .venv
.venv/Scripts/activate      # Windows; use .venv/bin/activate elsewhere
pip install -e .
```
