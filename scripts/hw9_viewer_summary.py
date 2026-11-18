"""HW9: write a summary.json for a cold-run folder so exchange/trace/viewer.py can draw it (runs/ only, never
committed). Series follow run_m1's definitions (system net load, kW): recorded = system_load - sum(pv - curt + bess)
(the dataset's recorded dispatch); selfish = a selfish run_variant of the same config (every battery acting alone);
exchange = system_load - sum(site_net + load) from the cold run's metered telemetry. Synthetic simulation outputs.
Usage: python scripts/hw9_viewer_summary.py configs/hw9/<cfg>.yaml runs/<run>  [--no-selfish]"""
import json, sys
from collections import defaultdict
from pathlib import Path

import numpy as np

from sim import run_m1 as R
from sim.clock import Clock
from sim.data import load_config, load_slice

cfg_path, run = sys.argv[1], Path(sys.argv[2])
cfg = load_config(cfg_path)
cfg.pop("attackers", None)
s = load_slice(cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
part = R.participants(s, cfg["agents"])
clock = Clock(s.ts_utc, R.TZ)
a = s.agent
pv, curt, load = np.nan_to_num(a["pv"][part]), a["curt"][part], a["load"][part]
sys_kw = s.system["system_load"] * 1000.0
recorded = sys_kw - (np.nan_to_num(a["pv"]) - a["curt"] + a["bess"]).sum(0)

tp = next(run.glob("exchange*/trace.jsonl"))
site = defaultdict(float)
binding = []
with open(tp, encoding="utf-8") as f:
    header = json.loads(f.readline())
    for line in f:
        if '"telemetry"' in line or '"call_nominations"' in line:
            d = json.loads(line)
            if d.get("msg_type") == "telemetry":
                site[d["t_utc"]] += float(d["payload"].get("net_kw", 0.0))
            elif d.get("msg_type") == "call_nominations" and d.get("from") == "exchange" and d["payload"]["binding"]:
                binding.append(d["payload"]["window"])
iso = clock.iso
exch = sys_kw - np.array([site[t] for t in iso]) - load.sum(0)
var = header["variant"]
series = {"t_utc": iso, "t_central": [str(x)[:16] for x in clock.local],
          "recorded_kw": [float(v) for v in recorded], f"{var}_kw": [float(v) for v in exch],
          f"{var}_in_window": [any(w["start_utc"] <= t < w["end_utc"] for w in binding) for t in iso]}
summary = {"header": {k: header.get(k) for k in ("git_commit", "run_at_utc", "seed")},
           "note": "HW9 cold run viewer data (not a run_m1 evaluation). Synthetic.",
           "exchange_by_headroom_mw": {"100": {"peak_mw": float(exch.max()) / 1000}}}
if "--no-selfish" not in sys.argv:
    r = R.run_variant(cfg, s, part, run, None)
    net = sys_kw - (pv - curt - r["X"] + r["D"] - r["C"]).sum(0)
    series["selfish_kw"] = [float(v) for v in net]
    summary["selfish"] = {"peak_mw": float(net.max()) / 1000}
    summary["exchange_by_headroom_mw"]["100"]["peak_vs_selfish_mw"] = float(exch.max() - net.max()) / 1000
else:
    series["selfish_kw"] = series["recorded_kw"]
summary["series"] = series
(run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
print("steps", len(iso), "binding windows", len(binding), {k: v for k, v in summary.items() if k in ("selfish",)},
      summary["exchange_by_headroom_mw"])
