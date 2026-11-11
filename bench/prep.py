"""Extract per-agent arrays from the Engie dataset into a compact npz."""
import duckdb, pandas as pd, numpy as np, sys
TS, META, SYS = sys.argv[1], sys.argv[2], sys.argv[3]
con = duckdb.connect()
df = con.sql(f"""select timestamp, entity_id, coalesce(load_kw,0) as "load", pv_ac_kw pv, pv_curtailed_kw curt,
   bess_power_true_kw bess, bess_soc_true_percent soc, bess_soh_percent soh, p_net_kw pnet
   from '{TS}' order by entity_id, timestamp""").df()
m = pd.read_parquet(META).set_index('entity_id')
ids = sorted(df.entity_id.unique()); n = len(ids); T = 35040
arr = {c: df[c].to_numpy(np.float64).reshape(n, T) for c in ['load','pv','curt','bess','soc','soh','pnet']}
s = pd.read_parquet(SYS)
ts = df.timestamp.iloc[:T].to_numpy()
assert (pd.DatetimeIndex(s.index) == pd.DatetimeIndex(df.timestamp.iloc[:T])).all()
cols = ['entity_type','bess_power_kw','bess_energy_kwh','eta_inverter','eta_battery_rt','soc0','poi_limit_kw','self_discharge_per_h','soh0','peak_kw']
meta = m.loc[ids, cols].reset_index()
meta.to_csv('meta.csv', index=False)
np.savez_compressed('data.npz', ids=np.array(ids), ts=ts.astype('datetime64[ns]').astype(np.int64),
    price_rt=s.price_usd_mwh.to_numpy(np.float64), price_da=s.price_da_usd_mwh.to_numpy(np.float64),
    system_load=s.system_load_mw.to_numpy(np.float64), net_disp=s.net_load_dispatched_mw.to_numpy(np.float64), **arr)
# sanity: pnet formula
res = arr['pv'] - arr['curt'] + arr['bess'] - arr['load'] - arr['pnet']
print('pnet residual abs max per type:', pd.Series(np.abs(res).max(1), index=meta.entity_type).groupby(level=0).max().to_dict())
print('poi export check (pnet - poi) max:', (arr['pnet'].max(1) - meta.poi_limit_kw.values).max())
