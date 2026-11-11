"""How much do the dataset's own reference DR policies (DC workload shift, EV smart charging) move the system peak?"""
import duckdb, pandas as pd, numpy as np, sys
TS, SYS = sys.argv[1], sys.argv[2]
c = duckdb.connect()
x = c.sql(f"""select timestamp,
  sum(case when entity_type='datacenter' then dc_load_dr_ref_kw - load_kw else 0 end)/1000 dc_delta,
  sum(case when entity_type='community' then ev_load_dr_ref_kw - ev_load_kw else 0 end)/1000 ev_delta,
  sum(case when entity_type='datacenter' then dc_flexible_kw else 0 end)/1000 dc_flex
  from '{TS}' group by 1 order by 1""").df().set_index('timestamp')
s = pd.read_parquet(SYS)
net = s.net_load_dispatched_mw
for nm, n in [('dataset baseline', net), ('+ DC ref DR', net + x.dc_delta), ('+ EV ref smart charging', net + x.ev_delta), ('+ both', net + x.dc_delta + x.ev_delta)]:
    print(f'{nm:26s} peak {n.max():8.1f} MW at {n.idxmax().tz_convert("America/Chicago")}')
print('DC flexible MW: mean', round(x.dc_flex.mean(),1), ' max', round(x.dc_flex.max(),1))
print('energy conserved? DC', round(x.dc_delta.sum(),2), ' EV', round(x.ev_delta.sum(),2))
