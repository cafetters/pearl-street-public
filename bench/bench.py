"""
Perfect-foresight battery benchmark for the Engie 80-agent flexibility dataset.

Scenarios (all use the dataset's own load, PV, prices and battery specs; only battery
dispatch and PV curtailment are re-decided):
  baseline   : dispatch as recorded in the dataset (rule-based cheap/dear quantile policy)
  selfish    : each agent maximizes its own RT-price revenue, full-year perfect foresight
  peakmin    : fleet jointly minimizes system net-load peak (weekly windows + 1 day lookahead)
  coopt      : fleet maximizes total revenue subject to system net load <= annual min peak cap

Battery model (verified against the dataset's true SOC): usable capacity = E * SOH,
one-way efficiency = eta_inverter * sqrt(eta_battery_rt), SOC 10-90 %, AC power <= rating.
Revenue = sum(RT price * p_net * 0.25 h). Peak = system_load - sum(PV_ac - curtail + BESS).
"""
import numpy as np, pandas as pd, scipy.sparse as sp, time, sys, json
from scipy.optimize import linprog

DT = 0.25
d = np.load('data.npz'); meta = pd.read_csv('meta.csv')
N, T = d['pv'].shape
price = d['price_rt']; SL = d['system_load'] * 1000.0          # kW
PV = np.nan_to_num(d['pv']); LOAD = d['load']
P = meta.bess_power_kw.values
CAP = meta.bess_energy_kwh.values * d['soh'].mean(1) / 100.0
ETA = meta.eta_inverter.values * np.sqrt(meta.eta_battery_rt.values)
SD = meta.self_discharge_per_h.values
POI = meta.poi_limit_kw.values
S0 = d['soc'][:, 0] / 100.0 * CAP
K = DT / 1000.0                                                 # kW*step -> MWh
DEG = 10.0   # $/MWh discharged: battery wear cost, keeps perfect foresight from over-cycling


def build(agents, t0, t1, s_init, s_term, cap_kw=None, peak=False, peak_from=0, pre_cap_kw=None):
    """LP over agents x [t0,t1). Vars per agent: c,d,s,u (each L). Extra: Z (peak) or slack[L - peak_from] (cap).
    peak_from (D57, default 0 = the LP as before): the peak row (peak mode) or the cap rows and their slack (cap mode)
    exist only for steps peak_from..L-1 of the window; earlier steps keep every physical constraint.
    pre_cap_kw (D57, default None = no rows): on each step before peak_from, system net load <= pre_cap_kw, a hard row
    with no slack, so the uncounted hours can't absorb shifted load. Placed before the coupling rows, which stay last."""
    L = t1 - t0; na = len(agents); nv = 4 * L
    pf = int(peak_from)
    if not 0 <= pf < L:
        raise ValueError(f"peak_from must be in [0, {L}), not {peak_from}")
    if pre_cap_kw is not None and pf == 0:
        raise ValueError("pre_cap_kw needs peak_from > 0")
    nc = L - pf                                                                # coupling rows
    pr = price[t0:t1]
    rows_eq, rows_ub = [], []
    beq, bub = [], []
    lb, ub, cost = [], [], []
    I = sp.identity(L, format='csr')
    Dm = sp.diags([np.ones(L), -np.ones(L - 1)], [0, -1], format='csr')   # s_t - s_{t-1}
    Z = sp.csr_matrix((L, L))
    for j, i in enumerate(agents):
        keep = 1 - SD[i] * DT
        Dk = sp.diags([np.ones(L), -keep * np.ones(L - 1)], [0, -1], format='csr')
        blk = [Z] * (4 * na)
        # dynamics: s_t - keep*s_{t-1} - eta*dt*c_t + dt/eta*d_t = (t==0) keep*s_init
        dyn = sp.hstack([-ETA[i] * DT * I, DT / ETA[i] * I, Dk, Z])
        b = np.zeros(L); b[0] = keep * s_init[j]
        rows_eq.append((j, dyn)); beq.append(b)
        # export: -c + d - u <= POI - pv + load
        ex = sp.hstack([-I, I, Z, -I]); rows_ub.append((j, ex)); bub.append(POI[i] - PV[i, t0:t1] + LOAD[i, t0:t1])
        # c + d <= P
        cd = sp.hstack([I, I, Z, Z]); rows_ub.append((j, cd)); bub.append(np.full(L, P[i]))
        # terminal energy >= s_term
        tr = sp.csr_matrix(([-1.0], ([0], [2 * L + L - 1])), shape=(1, nv)); rows_ub.append((j, tr)); bub.append(np.array([-s_term[j]]))
        lb += [np.zeros(L), np.zeros(L), np.full(L, 0.1 * CAP[i]), np.zeros(L)]
        ub += [np.full(L, P[i]), np.full(L, P[i]), np.full(L, 0.9 * CAP[i]), PV[i, t0:t1].copy()]
        # minimize -(revenue) => cost on (c - d + u)
        cost += [pr * K, (DEG - pr) * K, np.zeros(L), pr * K]
    nx = 4 * L * na
    extra = 0
    if peak: extra = 1
    elif cap_kw is not None: extra = nc
    def stack(rows, nrows_list):
        mats = []
        for j, M in rows:
            left = sp.csr_matrix((M.shape[0], 4 * L * j)); right = sp.csr_matrix((M.shape[0], nx - 4 * L * (j + 1) + extra))
            mats.append(sp.hstack([left, M, right]))
        return sp.vstack(mats, format='csr')
    Aeq = stack(rows_eq, None); Aub = stack(rows_ub, None)
    beq = np.concatenate(beq); bub = np.concatenate(bub)
    if pre_cap_kw is not None:
        # D57: sum_i (c - d + u) <= sum_i pv - SL + pre_cap on steps before peak_from (net load <= pre_cap)
        pre = sp.hstack([sp.hstack([I[:pf], -I[:pf], Z[:pf], I[:pf]])] * na + [sp.csr_matrix((pf, extra))])
        rhs_pre = (PV[agents, t0:t1].sum(0) - SL[t0:t1] + pre_cap_kw)[:pf]
        Aub = sp.vstack([Aub, pre], format='csr'); bub = np.concatenate([bub, rhs_pre])
    if peak or cap_kw is not None:
        # sum_i (c - d + u) - Z/slack <= sum_i pv - SL (+cap), on steps peak_from.. only
        Ic, Zc = (I, Z) if pf == 0 else (I[pf:], Z[pf:])
        base = sp.hstack([Ic, -Ic, Zc, Ic])
        cpl = sp.hstack([base] * na)
        pvsum = PV[agents, t0:t1].sum(0); sl = SL[t0:t1]
        if peak:
            cpl = sp.hstack([cpl, -np.ones((nc, 1))]); rhs = pvsum - sl
        else:
            cpl = sp.hstack([cpl, -(I if pf == 0 else sp.identity(nc, format='csr'))]); rhs = pvsum - sl + cap_kw
        if pf:
            rhs = rhs[pf:]
        Aub = sp.vstack([Aub, cpl], format='csr'); bub = np.concatenate([bub, rhs])
    lb = np.concatenate(lb); ub = np.concatenate(ub); cost = np.concatenate(cost)
    if peak:
        cost = np.concatenate([cost * 1e-6, [1.0]]); lb = np.append(lb, 0); ub = np.append(ub, np.inf)
    elif cap_kw is not None:
        cost = np.concatenate([cost, np.full(nc, 1.0)]); lb = np.concatenate([lb, np.zeros(nc)]); ub = np.concatenate([ub, np.full(nc, np.inf)])  # $1/kW-step slack penalty (>> price*K)
    r = linprog(cost, A_ub=Aub, b_ub=bub, A_eq=Aeq, b_eq=beq, bounds=np.c_[lb, ub], method='highs')
    if r.status != 0: raise RuntimeError(r.message)
    x = r.x[:nx].reshape(na, 4, L)
    return x, r.x[nx:]


def selfish():
    C = np.zeros((N, T)); D = np.zeros((N, T)); U = np.zeros((N, T)); S = np.zeros((N, T))
    for i in range(N):
        x, _ = build([i], 0, T, [S0[i]], [S0[i]])
        C[i], D[i], S[i], U[i] = x[0]
    return C, D, U, S


def fleet(mode, cap_kw=None, win=672, look=96):
    agents = list(range(N))
    C = np.zeros((N, T)); D = np.zeros((N, T)); U = np.zeros((N, T)); S = np.zeros((N, T))
    s = S0.copy(); weekly = []; slack_tot = 0.0
    t0 = 0
    while t0 < T:
        t1 = min(t0 + win, T); te = min(t1 + look, T)
        tic = time.time()
        if mode == 'peak':
            x, z = build(agents, t0, te, s, S0, peak=True)
        else:
            x, z = build(agents, t0, te, s, S0, cap_kw=cap_kw)
            slack_tot += z[:t1 - t0].sum()
        k = t1 - t0
        C[:, t0:t1] = x[:, 0, :k]; D[:, t0:t1] = x[:, 1, :k]; S[:, t0:t1] = x[:, 2, :k]; U[:, t0:t1] = x[:, 3, :k]
        s = x[:, 2, k - 1].copy()
        net = SL[t0:t1] - (PV[:, t0:t1] - U[:, t0:t1] + D[:, t0:t1] - C[:, t0:t1]).sum(0)
        weekly.append(net.max() / 1000)
        print(f'{mode} window {t0//win:2d} peak {net.max()/1000:8.1f} MW  {time.time()-tic:5.1f}s', flush=True)
        t0 = t1
    return C, D, U, S, weekly, slack_tot


if __name__ == '__main__':
    which = sys.argv[1]
    if which == 'selfish':
        C, D, U, S = selfish(); np.savez_compressed('res_selfish.npz', C=C, D=D, U=U, S=S)
    elif which == 'peak':
        C, D, U, S, wk, _ = fleet('peak'); np.savez_compressed('res_peak.npz', C=C, D=D, U=U, S=S, weekly=wk)
    elif which == 'coopt':
        cap = float(sys.argv[2]) * 1000
        C, D, U, S, wk, sl = fleet('cap', cap_kw=cap); np.savez_compressed('res_coopt.npz', C=C, D=D, U=U, S=S, weekly=wk, slack=sl, cap=cap)
        print('total slack kW-steps', sl)
