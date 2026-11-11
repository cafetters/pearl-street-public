# Flexibility benchmark (Engie 80-agent dataset)

Perfect-foresight ceiling for what the 80 batteries can do on the Engie 2023 dataset.
Open `flex_benchmark_report.html` in a browser for the results.

## Files
| File | What it is |
|---|---|
| `flex_benchmark_report.html` | Results: headline numbers, peak-day chart, top-500-hour curve, scenario table |
| `results_summary.csv` | One row per scenario: peak, top-100 average, battery value, cycles, agents worse off |
| `results_per_agent.csv` | One row per agent: battery value (net of wear) and cycles under each scenario |
| `prep.py` | Pulls the needed columns out of the parquet files into `data.npz` |
| `bench.py` | The LPs (selfish, peak-min, co-optimized) |
| `analyze.py` | Scores every scenario on the same metrics |
| `dr_check.py` | Tests the dataset's own reference DR policies against the system peak |
| `price_mode_proto.py` | Price-mode clearing prototype (peak price + proximal re-solve, 80 agents, dev slice). Report: `docs/plans/price_proto_report.md` |

## Rerun
```bash
pip install duckdb pyarrow pandas numpy scipy highspy
python prep.py <agents_timeseries.parquet> <agents_metadata.parquet> <system_prices_and_load.parquet>
python bench.py selfish
python bench.py peak
python bench.py coopt <peak_cap_MW>     # cap = peak-min annual peak x 1.01
python analyze.py
```
Selfish takes ~4 min and each fleet run ~5-10 min. Peak-min found 2,437.9 MW, so the cap used was 2,462.27 MW. `DEG` in `bench.py` sets the battery wear cost ($10/MWh discharged).
