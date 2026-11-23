"""A synthetic world for the red-team harness's tests and smoke rounds [sim, synthetic]. One Central day, zero site
load and PV, an evening system peak, cheap afternoons and evenings (DA 20 $/MWh) and dear nights (30 $/MWh), the shape
the D61 tests use (tests/test_replay.synthetic_slice), with a configurable fleet. Nothing here is Engie data."""
from __future__ import annotations

import numpy as np
import pandas as pd

from sim.data import META_COLS, Slice

# arm C, rule 1p, the D61 test settings (tests/test_d58_margin._cfg) plus D61's delivery_record
BASE_CFG = {"run_name": "redteam", "zone": "SYS", "operator_id": "GRID_OP", "metering_id": "metering",
            "clock_id": "clock", "layer_mw": 0.05, "price_cap_usd_mwh": 500.0, "post_hour_local": 9, "gate_lead_h": 1,
            "price_publish_hour_local": 12, "forecast_noise_sigma": 0.0, "seed": 1, "min_offer_kw": 5.0,
            "wear_usd_mwh": 10.0, "nominations": True, "rounds": "info", "nomination_lead_h": 1, "price_mode": True,
            "plan_to_nomination": True, "release_superseded_lock": True, "price_rounds_per_gate": 10,
            "settlement_reference": "selfish_1p", "peak_response_margin_usd_mwh": 5.0, "delivery_record": True,
            # the synthetic base is the exchange as it was before D-a (owner, Nov 19): both M5 defenses off unless a
            # round, a defense or a catalog entry turns them on (runs otherwise default them on, sim/metering_cap.py)
            "register_power_check": False, "reference_power_cap": False}
HEADROOM_MW = 0.5
FLEET = (("A", 2000.0, 8000.0), ("B", 100.0, 400.0), ("C", 60.0, 240.0))      # id, power kW, energy kWh


PRICES = ("d61", "cheap_peak")


def synthetic_slice(fleet=FLEET, prices: str = "cheap_peak", days: int = 1) -> Slice:
    """prices: "d61", the D61 tests' shape (20 $/MWh, 30 at night); "cheap_peak" (default), dear nights (60) and the
    cheapest energy exactly at the evening system peak (15 at 19:00 local), so an acting-alone battery recharges into
    the peak and its rule 1p reference is power-limited there. Both synthetic. days: the same day repeated (2 gives
    one day boundary, e.g. for D70's memory sub-round)."""
    if prices not in PRICES:
        raise ValueError(f"prices must be one of {PRICES}")
    T = 96 * int(days)
    ts = pd.date_range("2023-01-02 06:00", periods=T, freq="15min", tz="UTC")
    hour = np.asarray(ts.tz_convert("America/Chicago").hour)
    n = len(fleet)
    sys_load = 1000 + 300 * np.exp(-((hour - 19) ** 2) / 4)
    if prices == "d61":
        price = 20 + 10 * (hour < 6)
    else:
        price = 30.0 + 30.0 * (hour < 6) - 15.0 * np.exp(-((hour - 19) ** 2) / 2.0)
    z = np.zeros((n, T))
    meta = pd.DataFrame({"entity_id": [f[0] for f in fleet], "entity_type": "pv_bess",
                         "bess_power_kw": [f[1] for f in fleet], "bess_energy_kwh": [f[2] for f in fleet],
                         "eta_inverter": 0.97, "eta_battery_rt": 0.9, "soc0": 0.5, "poi_limit_kw": 1e6,
                         "self_discharge_per_h": 0.0, "soh0": 100.0, "peak_kw": np.nan, "bus": list(range(1, n + 1))})
    assert set(META_COLS) <= set(meta.columns)
    return Slice(ids=np.array([f[0] for f in fleet]), ts_utc=ts, dt_h=0.25,
                 agent={"load": z.copy(), "pv": z.copy(), "curt": z.copy(), "bess": z.copy(),
                        "soc": np.full((n, T), 50.0), "soh": np.full((n, T), 100.0), "pnet": z.copy()},
                 system={"price_rt": price.astype(float), "price_da": price.astype(float), "system_load": sys_load,
                         "net_disp": sys_load, "bg_load_scale": np.ones(T)},
                 meta=meta)
