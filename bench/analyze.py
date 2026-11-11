"""Score all scenarios on the same metrics; write results.json + CSVs."""
import numpy as np, pandas as pd, json
import bench as b

d, meta = b.d, b.meta
N, T = b.N, b.T
ts = pd.to_datetime(d['ts'], utc=True).tz_convert('America/Chicago')
price = b.price
PV, LOAD = b.PV, b.LOAD


def scen_from_lp(f):
    r = np.load(f)
    return dict(bess=r['D'] - r['C'], curt=r['U'], D=r['D'], C=r['C'])

# no batteries: curtail only what the export cap forces
curt0 = np.clip(PV - LOAD - b.POI[:, None], 0, None)
S = {
    'No batteries':        dict(bess=np.zeros((N, T)), curt=curt0),
    'Dataset baseline':    dict(bess=d['bess'], curt=d['curt']),
    'Selfish optimum':     scen_from_lp('res_selfish.npz'),
    'Peak-only (ignores price)': scen_from_lp('res_peak.npz'),
    'Co-optimized':        scen_from_lp('res_coopt.npz'),
}

rows, per_agent, net_curves = [], {}, {}
for name, s in S.items():
    pnet = PV - s['curt'] + s['bess'] - LOAD                          # kW per agent
    net = b.SL - (PV - s['curt'] + s['bess']).sum(0)                  # kW system
    net_mw = net / 1000
    hourly = pd.Series(net_mw, index=ts).resample('h').mean()
    # battery value = change in agent wholesale revenue vs the no-battery case, $ per agent
    val = (price * b.K * (s['bess'] - s['curt'])).sum(1) - (price * b.K * (-curt0)).sum(1)
    dis = np.clip(s['bess'], 0, None)
    wear = (b.DEG * b.K * dis).sum(1)
    cycles = (dis.sum(1) * b.DT) / b.CAP
    bill = -(price * b.K * pnet).sum(1)                                # $ paid (neg = earned)
    per_agent[name] = dict(value=val, value_net=val - wear, bill=bill, cycles=cycles)
    rows.append({
        'scenario': name,
        'peak_mw': net_mw.max(),
        'peak_time': str(ts[net_mw.argmax()]),
        'top100_mean_mw': np.sort(net_mw)[-100:].mean(),
        'hours_above_2500': int((hourly > 2500).sum()),
        'battery_value_musd': val.sum() / 1e6,
        'battery_value_net_wear_musd': (val - wear).sum() / 1e6,
        'avg_cycles_per_yr': cycles.mean(),
        'all_agent_bills_musd': bill.sum() / 1e6,
        'community_dc_bills_musd': bill[(meta.entity_type != 'pv_bess').values].sum() / 1e6,
    })
    net_curves[name] = net_mw

res = pd.DataFrame(rows)
base = per_agent['Dataset baseline']['value_net']
for name in S:
    v = per_agent[name]['value_net']
    res.loc[res.scenario == name, 'agents_worse_than_baseline'] = int((v < base - 1).sum())
    res.loc[res.scenario == name, 'side_payment_needed_usd'] = float(np.clip(base - v, 0, None).sum())
print(res.round(2).to_string())
res.to_csv('results_summary.csv', index=False)

pa = meta[['entity_id', 'entity_type', 'bess_power_kw', 'bess_energy_kwh']].copy()
for name in S:
    k = name.lower().replace(' ', '_').replace('-', '_')
    pa[f'value_net_{k}'] = per_agent[name]['value_net']
    pa[f'cycles_{k}'] = per_agent[name]['cycles']
pa.to_csv('results_per_agent.csv', index=False)

# peak day profile (day of the dataset-baseline annual peak) + coordinated peak day
out = {'summary': res.to_dict(orient='records')}
for tag, ref in [('baseline_peak_day', 'Dataset baseline'), ('raw_peak_day', 'No batteries')]:
    i = int(np.argmax(net_curves[ref])); day = ts[i].normalize()
    m = (ts >= day) & (ts < day + pd.Timedelta(days=1))
    out[tag] = {'date': str(day.date()), 'hours': [round(h + mi / 60, 2) for h, mi in zip(ts[m].hour, ts[m].minute)],
                'series': {n: np.round(net_curves[n][m], 1).tolist() for n in S},
                'price': np.round(price[m], 2).tolist()}
# load duration curve, top 500 hours
out['duration'] = {n: np.round(np.sort(pd.Series(net_curves[n], index=ts).resample('h').mean().values)[::-1][:500], 1).tolist() for n in S}
# weekly peaks
out['weekly'] = {n: np.round(pd.Series(net_curves[n], index=ts).resample('W').max().values, 1).tolist() for n in S}
out['by_type'] = pa.groupby('entity_type')[[c for c in pa.columns if c.startswith('value_net_')]].sum().div(1e6).round(3).reset_index().to_dict(orient='records')
json.dump(out, open('results.json', 'w'))
print(pa.groupby('entity_type')[[c for c in pa.columns if c.startswith('value_net_')]].sum().div(1e6).round(2).to_string())
