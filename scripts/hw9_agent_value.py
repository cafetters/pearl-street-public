"""HW9: one agent's full value with Pearl Street vs acting alone, by sim/run_m1.py evaluate()'s definition (synthetic $):
energy at real-time prices on metered site net, minus wear on discharge, plus peak settlement, minus imbalance, plus
end-of-window state of charge at the window's mean day-ahead price. "Acting alone" is the selfish run of the same
config (every battery alone, no exchange), whose trace sits in the run folder (scripts/hw9_viewer_summary.py writes it).
Usage: python scripts/hw9_agent_value.py configs/hw9/<cfg>.yaml runs/<run> PVB_15"""
import json, sys
from pathlib import Path

import numpy as np

from sim.clock import Clock
from sim import run_m1 as R
from sim.data import load_config, load_slice

cfg_path, run, me = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
cfg = load_config(cfg_path)
s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
iso = Clock(s.ts_utc, R.TZ).iso
idx = {t: i for i, t in enumerate(iso)}
K = 0.25 / 1000.0
rt = np.asarray(s.system["price_rt"], float)
p_end = float(np.mean(s.system["price_da"])) / 1000.0
wear = float(cfg["wear_usd_mwh"])


def value(trace: Path) -> dict:
    net, meter, soc = np.zeros(len(iso)), np.zeros(len(iso)), np.zeros(len(iso))
    peak = imb = 0.0
    with open(trace, encoding="utf-8") as f:
        f.readline()
        for line in f:
            if me not in line:
                continue
            d = json.loads(line)
            p = d.get("payload") or {}
            if d.get("msg_type") == "telemetry" and p.get("asset_id") == f"{me}/bess":
                i = idx[d["t_utc"]]
                net[i], meter[i], soc[i] = p["net_kw"], p["meter_kw"], p["soc_kwh"]
            elif d.get("msg_type") == "peak_settle" and d.get("to") == me:
                peak += p["net_usd"]
            elif d.get("msg_type") == "imbalance" and d.get("to") == me:
                imb += p["charge_usd"]
    energy = float((rt * K * net).sum())
    w = wear * K * float(np.maximum(meter, 0.0).sum())
    end = float(soc[-1]) * p_end
    return {"energy_rt_usd": energy, "wear_usd": w, "peak_settle_usd": peak, "imbalance_usd": imb,
            "end_soc_kwh": float(soc[-1]), "end_soc_value_usd": end, "total_usd": energy - w + peak - imb + end}


alone = value(run / "selfish" / "trace.jsonl")
withps = value(next(run.glob("exchange*/trace.jsonl")))
out = {"agent": me, "acting_alone": alone, "with_pearl_street": withps,
       "difference_usd": withps["total_usd"] - alone["total_usd"],
       "note": "run_m1 evaluate() definition; synthetic. The settlement-only figure is peak_settle - imbalance."}
(run / f"agent_value_{me}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
print(json.dumps(out, indent=1))
