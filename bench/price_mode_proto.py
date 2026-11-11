"""
Price-mode clearing prototype on the dev slice. Standalone: no exchange messages, nothing in exchange/, agents/
or sim/ changes.

Round 0 is selfish: each agent solves its own bench.build LP on RT prices with perfect foresight. Then, each round:
  1. aggregate = system net load from every agent's last plan (the same quantity as the M0 peak)
  2. peak price, projected damped dual ascent:  pi_t <- max(0, pi_t + alpha (agg_t - target_t))
  3. every agent re-solves its own build() problem against RT price + pi, plus a proximal penalty
     (rho/2) sum_t (x_t - x_prev_t)^2 on its battery-driven net load x = c - d + u (Jacobi: all at once)
Targets follow agents/operator.py: selfish Central-day peak - headroom.

Each agent's model is the build() LP captured unchanged, plus the proximal term as separable piecewise-linear secants
(AgentProx), solved as an LP by the HiGHS that ships inside scipy (private API scipy.optimize._highspy._core) with
hot starts between rounds. The secants overstate the square by at most ~4.3% and leave a kink at zero change of at
most prox_deadzone_usd_mwh. Not an exact QP: HiGHS 1.12's QP solver (same module) ran 106k iterations in 5 s on one
agent at rho = 2e-6 without finishing. The run stops if, at rho = 0 and pi = 0, any agent's objective differs from
its build() objective.

Central reference per headroom: build(all agents, cap_kw=target per step) gives feasibility (slack), the best fleet
value at that target and the per-step dual pi*. Under convexity a converged price-mode run should match all three.

Units: pi is held in $/kW-step like the m0 dual ($/MWh = pi / K). rho is $/kW^2-step, alpha $/kW-step per kW.

Oracle-free variant (kind "oracle-free", v2): rho starts from public data only (mean daily day-ahead spread over the
slice / the median battery's registered power), the price step is tied to it as in exchange ADMM (alpha = rho / N),
and rho adapts by residual balancing (Boyd et al. 2011, sec. 3.4.1): x tau when the primal residual (projected excess
over target) is more than mu x the change in plans, / tau in the opposite case. The agent update is unchanged: prox
on the change from the agent's own last plan (the textbook exchange ADMM also shifts that center by the mean
residual; v2 does not).

v3 switches, all off by default so v1 and v2 behave as before:
  prox_centre: textbook   prox centred on x_prev - shift, shift = (pi_new - pi_old) / (alpha N): the mean projected
                          residual behind the price step (Boyd sec. 7.3 sharing ADMM, g = indicator of agg <= target,
                          pi = rho u; exact when alpha = rho / N). The PWL range grows by shift_margin to cover it.
  rb_dual: scaled         residual balancing compares the primal residual with mu x s / rho0, s = rho x |plan change|
                          the dual residual, rho0 the setting's starting rho (s in kW); rho capped at
                          rb_rho_max_factor x rho0.
  conv_rule: target+dual  converged = max excess <= tol_excess_mw and s <= tol_dual_usd_mwh for `streak` rounds; the
                          v2 target-only rule (excess, plan movement, slackness) is reported next to it.

Run:  python -m bench.price_mode_proto configs/price_proto.yaml [--smoke] [--only label,label]
      -> runs/price_proto/   (per-setting files in settings/, then table and plot over every setting on disk)
      A config with out_tag (e.g. v2) writes settings_<tag>/ and table_<tag>.md etc. instead, leaving v1's files.
"""
import argparse, json, time
from pathlib import Path

import numpy as np
import pandas as pd
import scipy
import scipy.sparse as sp
from scipy.optimize._highspy import _core as hc

from bench.m0 import dispatch, load_bench, net_load, solve, use_slice, value
from bench.runinfo import git_commit, run_header
from sim.data import REPO, load_config, load_slice

OUT = REPO / "runs" / "price_proto"
REG_RTOL = 1e-5          # agent model at rho = 0, pi = 0 vs. build() objective. Scaled x1e3 (OBJ_SCALE), HiGHS
                         # finds up to 2.9e-6 rel better than build()'s unscaled solve (cents); unscaled, equal to 1e-15
OBJ_SCALE = 1e3          # objective scale inside HiGHS: keeps the smallest secant slopes far above its tolerances
SQRT2 = np.sqrt(2.0)
K_MWH = 0.25 / 1000.0    # 15-min step: $/kW-step -> $/MWh (dt_h / 1000); the slice is fixed at 15 min (D11)
M0_TOL_MW = 0.1
SLACK_TOL = 1.0          # kW-steps of cap slack still counted as feasible
PRICED = 1e-6            # $/kW-step: a price above this counts as positive
SMOKE_ROUNDS = 5         # runtime estimate uses the warm rounds 2..5


class AgentProx:
    """One agent's build() LP as a persistent HiGHS model, plus the proximal term as separable piecewise-linear
    secants of (rho/2) delta^2 on delta = x - x_prev, x = c - d + u. Columns: c, d, s, u (L each, as in build()),
    then segment columns p_k and n_k (L each) with delta = sum p_k - sum n_k, then x_prev as L columns fixed by their
    bounds, one equality row per step: x - sum p + sum n - x_prev = 0.
    Secant breakpoints run from b1 up to R = the largest possible |delta| (2P + max PV) in steps of sqrt 2, so the
    PWL term overstates the square by at most ~4.3% past b1; b1 = min(R/256, 2 eps / rho) keeps the kink at
    delta = 0 (slope rho b1 / 2) at or below eps. Per round only the c, d, u costs (price) and the x_prev column
    bounds change, so HiGHS hot-starts. Objectives are scaled by OBJ_SCALE inside HiGHS."""

    def __init__(self, cost, A_ub, b_ub, A_eq, b_eq, bounds, L):
        self.L = L
        self.cost0 = np.asarray(cost, dtype=float)
        self.A = sp.vstack([A_ub, A_eq], format="csr")
        self.rlo = np.r_[np.full(A_ub.shape[0], -hc.kHighsInf), b_eq]
        self.rhi = np.r_[b_ub, b_eq]
        self.bounds = np.asarray(bounds, dtype=float)
        ub = self.bounds[:, 1]
        self.R = float(ub[:L].max() + ub[L:2 * L].max() + ub[3 * L:4 * L].max()) + 1.0
        self.extra_kw = 0.0      # added to R when the prox centre is shifted off x_prev (textbook centre)
        self.idx4 = np.arange(4 * L, dtype=np.int32)
        self.h = hc._Highs()
        self.h.setOptionValue("output_flag", False)
        self.set_rho(0.0)

    @staticmethod
    def _ok(status, what):
        if status != hc.HighsStatus.kOk:
            raise RuntimeError(f"HiGHS {what}: {status}")

    def set_rho(self, rho, eps=0.0, reset_counts=False):
        """Rebuild the model for one rho. eps: largest kink slope at delta = 0, $/kW-step."""
        L, n0 = self.L, len(self.cost0)
        self.rho = rho
        if not hasattr(self, "warnings") or reset_counts:
            self.warnings, self.cold = 0, 0
        if rho > 0:
            R = self.R + self.extra_kw
            b1 = min(R / 256, 2 * eps / rho) if eps > 0 else R / 256
            b = [0.0, b1]
            while b[-1] * SQRT2 < R:
                b.append(b[-1] * SQRT2)
            b.append(R)
            self.b = np.array(b)
        else:
            self.b = np.array([0.0])
        m = len(self.b) - 1
        w = np.diff(self.b)
        slope = rho / 2 * (self.b[:-1] + self.b[1:])
        self.deadzone = float(slope[0]) if m else 0.0
        seg_cost = np.repeat(slope, L)
        nprev = L if m else 0
        cost = np.r_[self.cost0, seg_cost, seg_cost, np.zeros(nprev)]
        lo = np.r_[self.bounds[:, 0], np.zeros(2 * m * L), np.zeros(nprev)]
        hi = np.r_[self.bounds[:, 1], np.repeat(w, L), np.repeat(w, L), np.zeros(nprev)]
        A, rlo, rhi = self.A, self.rlo, self.rhi
        if m:
            I = sp.identity(L, format="csr")
            Z = sp.csr_matrix((L, L))
            A = sp.bmat([[A, None, None, None],
                         [sp.hstack([I, -I, Z, I]), sp.hstack([-I] * m), sp.hstack([I] * m), -I]], format="csc")
            rlo, rhi = np.r_[rlo, np.zeros(L)], np.r_[rhi, np.zeros(L)]
        else:
            A = A.tocsc()
        A.sort_indices()
        lp = hc.HighsLp()
        lp.num_col_, lp.num_row_ = len(cost), A.shape[0]
        lp.col_cost_, lp.col_lower_, lp.col_upper_ = OBJ_SCALE * cost, lo, hi
        lp.row_lower_, lp.row_upper_ = rlo, rhi
        mat = lp.a_matrix_
        mat.format_ = hc.MatrixFormat.kColwise
        mat.start_, mat.index_, mat.value_ = A.indptr, A.indices, A.data
        lp.a_matrix_ = mat
        self.h.clearModel()
        self._ok(self.h.passModel(lp), "passModel")
        self.n, self.m, self.seg_cost = len(cost), m, np.r_[seg_cost, seg_cost]
        self.prev_cols = np.arange(self.n - L, self.n, dtype=np.int32) if m else None

    def solve(self, g, x_prev=None):
        """Minimize cost0 v + g'x + PWL (rho/2)|x - x_prev|^2. g per step, $/kW-step.
        Returns (v = [c, d, s, u] columns, objective $, PWL prox term $)."""
        L = self.L
        cost = self.cost0[:4 * L].copy()
        cost[:L] += g; cost[L:2 * L] -= g; cost[3 * L:4 * L] += g
        self._ok(self.h.changeColsCost(4 * L, self.idx4, OBJ_SCALE * cost), "changeColsCost")
        if self.m:
            xp = np.asarray(x_prev, dtype=float)
            self._ok(self.h.changeColsBounds(L, self.prev_cols, xp, xp), "changeColsBounds")
        run = self.h.run()
        if run != hc.HighsStatus.kOk or self.h.getModelStatus() != hc.HighsModelStatus.kOptimal:
            # a hot start can end with a warning; keep it only if optimal, else re-solve cold. Counted in the row.
            self.warnings += 1
            if self.h.getModelStatus() != hc.HighsModelStatus.kOptimal:
                self.cold += 1
                self.h.clearSolver()
                run = self.h.run()
            st = self.h.getModelStatus()
            if run == hc.HighsStatus.kError or st != hc.HighsModelStatus.kOptimal:
                raise RuntimeError(f"agent LP not optimal: {run}, {self.h.modelStatusToString(st)}")
        v = np.array(self.h.getSolution().col_value)
        prox = float(self.seg_cost @ v[4 * L:4 * L + len(self.seg_cost)]) if self.m else 0.0
        return v[:4 * L], float(self.h.getInfo().objective_function_value) / OBJ_SCALE, prox


def pwl_square(delta, b, rho):
    """The secant interpolant of (rho/2) delta^2 on breakpoints b (what AgentProx charges once segments fill in
    order)."""
    return np.interp(np.abs(delta), b, rho / 2 * b ** 2)


def net_x(v, L):
    """Battery-driven net load x = c - d + u from a build() variable vector [c, d, s, u]."""
    return v[:L] - v[L:2 * L] + v[3 * L:4 * L]


def capture_agents(B):
    """Each agent's selfish plan and its LP, captured from bench.build unchanged (the m0.Capture trick)."""
    orig, rec = B.linprog, {}

    def grab(c, **kw):
        r = orig(c, **kw)
        rec.update(c=c, fun=float(r.fun), **kw)
        return r

    qps, fun = [], np.empty(B.N)
    C, D, S, U = (np.empty((B.N, B.T)) for _ in range(4))
    B.linprog = grab
    try:
        for i in range(B.N):
            x, _ = B.build([i], 0, B.T, [B.S0[i]], [B.S0[i]])
            C[i], D[i], S[i], U[i] = x[0]
            fun[i] = rec["fun"]
            qps.append(AgentProx(rec["c"], rec["A_ub"], rec["b_ub"], rec["A_eq"], rec["b_eq"], rec["bounds"], B.T))
    finally:
        B.linprog = orig
    return qps, {"c": C, "d": D, "s": S, "u": U}, fun


def agent_values(B, c, d, u):
    """Per-agent RT energy revenue net of wear, $ (m0.value summed per agent)."""
    return (B.price * B.K * (B.PV - B.LOAD + d - c - u)).sum(1) - B.DEG * B.K * d.sum(1)


def price_update(pi, agg, target, alpha):
    """Projected damped dual ascent: rises where the aggregate is over target, falls where under, never < 0."""
    return np.maximum(0.0, pi + alpha * (agg - target))


def residual(agg, target, pi):
    """Projected primal residual per step: the excess where unpriced (only over-target counts), the full gap where
    priced (a priced step below target is also off the fixed point)."""
    gap = agg - target
    return np.where(pi > PRICED, gap, np.maximum(gap, 0.0))


def rb_update(rho, r, d, mu, tau, lo, hi):
    """Residual balancing: raise rho when the primal residual r is more than mu x the plan change d, lower it in the
    opposite case, within [lo, hi]."""
    if r > mu * d:
        rho *= tau
    elif d > mu * r:
        rho /= tau
    return float(min(max(rho, lo), hi))


def rb_dual_kw(rho, rho0, d, scaled):
    """What residual balancing weighs the primal residual against, kW. Raw (v2): the plan change d. Scaled (v3): the
    dual residual s = rho d over the fixed starting rho0, so a raise of rho also raises this side."""
    return rho / rho0 * d if scaled else d


def centre_shift(pi_new, pi_old, alpha, N):
    """Textbook exchange-ADMM shift of each agent's prox centre, kW per step: the mean projected residual behind the
    price step, (pi_new - pi_old) / (alpha N). The full gap / N where the step stays priced, 0 where unpriced and
    under target, clipped where the price falls to 0."""
    return (pi_new - pi_old) / (alpha * N)


def first_streak(ok, streak):
    """First round (rounds counted from 1) of the first run of `streak` consecutive True flags, else None."""
    run = 0
    for k, f in enumerate(ok, start=1):
        run = run + 1 if f else 0
        if run >= streak:
            return k - streak + 1
    return None


D49_PRICE_REL = 0.10     # D49 arm C entry: final max price within 10% of the central dual's max


def d49_entry(row):
    """D49 arm C entry test for one oracle-free row (PLAN.md, amended Oct 26): settles within 5 MW of target, final
    max price within 10% of the central dual's max, no agent harmed after refund (column c). Each condition, and
    all three."""
    rel = row["max_pi_usd_mwh"] / row["max_pi_star_usd_mwh"] - 1.0
    out = {"settled_5mw": row.get("settled_within_5mw") is not None and not pd.isna(row.get("settled_within_5mw")),
           "price_10pct": bool(abs(rel) <= D49_PRICE_REL), "no_harm_c": int(row["harmed_c_n"]) == 0,
           "price_rel": float(rel)}
    out["pass"] = out["settled_5mw"] and out["price_10pct"] and out["no_harm_c"]
    return out


def rounds_within(excess_mw, tol_mw):
    """(first round with max excess <= tol, first round from which every later round stays <= tol), rounds counted
    from 1 (index 0 is the selfish round 0). None where it never happens."""
    e = np.asarray(excess_mw, dtype=float)[1:]
    ok = e <= tol_mw
    first = int(np.argmax(ok)) + 1 if ok.any() else None
    if not ok[-1]:
        return first, None
    bad = np.where(~ok)[0]
    return first, int(bad[-1]) + 2 if len(bad) else 1


def da_spread(s, tz):
    """Mean over local days of (max - min) day-ahead price, $/MWh. Public: day-ahead prices publish before the day."""
    day = s.ts_utc.tz_convert(tz).date
    da = s.system["price_da"]
    return float(np.mean([np.ptp(da[day == d]) for d in np.unique(day)]))


def classify(aggs_mw, excess_mw, sum_pi, feasible, window=30):
    """Label a run that did not converge, from its per-round history (round 0 first).
    diverging: target infeasible and the total price still climbing. oscillating: a 2-cycle (each plan close to
    the one two rounds back but far from the last) or the max-excess series flipping direction over and over.
    drifting: anything else. Returns (label, swing MW = median of the last 10 rounds' max |change in aggregate|)."""
    A = np.asarray(aggs_mw)[-window:]
    move = np.abs(np.diff(A, axis=0)).max(1)
    swing = float(np.median(move[-10:]))
    if not feasible and np.mean(np.diff(np.asarray(sum_pi)[-window:]) > 0) >= 0.9:
        return "diverging", swing
    two_back = np.abs(A[2:] - A[:-2]).max(1)
    if swing > 10.0 and np.median(two_back[-10:]) < 0.25 * swing:
        return "oscillating", swing
    de = np.diff(np.asarray(excess_mw)[-window:])
    de = de[np.abs(de) > 1e-9]
    flips = int((np.sign(de[1:]) != np.sign(de[:-1])).sum())
    if flips >= window // 3:
        return "oscillating", swing
    return "drifting", swing


def central_ref(B, target):
    """Fleet cap LP at the target per step: feasibility, best value, peak and the per-step dual."""
    r = solve(B, cap_kw=target)
    slack = r["z"]
    x = dispatch(r["x"])
    return {"feasible": bool(slack.sum() <= SLACK_TOL), "slack_kw_steps": float(slack.sum()),
            "slack_steps": int((slack > 1e-3).sum()), "value_usd": value(B, **x),
            "peak_kw": float(net_load(B, **x).max()), "dual": r["dual"], "secs": r["secs"]}


def run_setting(B, qps, X0, base, target, st, cfg, max_rounds, log_every=10):
    """Run one setting's price loop. Returns the final plans, price, history and status."""
    rho, alpha = st["rho"], st["alpha"]
    rho0 = rho
    ad = st.get("adaptive")
    textbook = cfg.get("prox_centre", "own") == "textbook"
    scaled = cfg.get("rb_dual", "raw") == "scaled"
    dual_conv = cfg.get("conv_rule", "target") == "target+dual"
    margin = st.get("shift_margin_kw", 0.0) if textbook else 0.0
    eps = cfg["prox_deadzone_usd_mwh"] * B.K
    for q in qps:
        q.extra_kw = margin
        q.set_rho(rho, eps, reset_counts=True)
    prox = {"max_deadzone_usd_mwh": max(q.deadzone for q in qps) / B.K,
            "max_segments_per_side": max(q.m for q in qps)}
    N, T = X0.shape
    X, agg, pi = X0.copy(), base + X0.sum(0), np.zeros(T)
    aggs, rows = [agg / 1e3], []

    def record(k, agg, move, secs=float("nan"), r=float("nan"), d=float("nan"), shift=float("nan"), ok=False):
        rows.append({"setting": st["label"], "round": k, "secs": secs, "rho": rho, "alpha_usd_mwh_per_mw":
                     alpha / B.K * 1e3, "primal_res_mw": r / 1e3, "plan_change_mw": d / 1e3,
                     "max_excess_mw": float((agg - target).max()) / 1e3,
                     "peak_mw": float(agg.max()) / 1e3, "max_move_mw": move,
                     "sum_pi_usd_mwh": float(pi.sum() / B.K), "priced_steps": int((pi > PRICED).sum()),
                     "dual_res_usd_mwh": rho * d / B.K, "max_shift_kw": shift, "target_only_ok": ok})

    record(0, agg, float("nan"))
    tic, streak, conv, sols, changes = time.time(), 0, None, None, 0
    for k in range(1, max_rounds + 1):
        tk = time.time()
        pi_old = pi
        pi = price_update(pi, agg, target, alpha)
        shift = centre_shift(pi, pi_old, alpha, N) if textbook else np.zeros(T)
        max_shift = float(np.abs(shift).max())
        if max_shift > margin + 1e-9 and textbook:
            raise SystemExit(f"{st['label']} round {k}: centre shift {max_shift:.0f} kW > PWL margin {margin:.0f} kW")
        Xn, sols = np.empty_like(X), []
        for i, q in enumerate(qps):
            v, _, _ = q.solve(pi, X[i] - shift)
            Xn[i] = net_x(v, T)
            sols.append(v)
        agg_n = base + Xn.sum(0)
        move = float(np.abs(agg_n - agg).max()) / 1e3
        r_k = float(np.linalg.norm(residual(agg_n, target, pi)))
        d_k = float(np.linalg.norm(Xn - X))
        s_k = rho * d_k / B.K                                # dual residual, $/MWh, at the rho this round used
        X, agg = Xn, agg_n
        aggs.append(agg / 1e3)
        excess = float((agg - target).max()) / 1e3
        ok_t = bool(excess <= cfg["tol_excess_mw"] and move <= cfg["tol_move_mw"]
                    and not ((pi > PRICED) & (agg < target - cfg["tol_slack_mw"] * 1e3)).any())
        record(k, agg, move, time.time() - tk, r_k, d_k, max_shift, ok_t)
        if ad:
            new = rb_update(rho, r_k, rb_dual_kw(rho, rho0, d_k, scaled), ad["mu"], ad["tau"], ad["lo"], ad["hi"])
            if new != rho:
                rho, changes = new, changes + 1
                for q in qps:
                    q.set_rho(rho, eps)
            alpha = rho / N                                   # ADMM-tied price step
        ok = (excess <= cfg["tol_excess_mw"] and s_k <= cfg["tol_dual_usd_mwh"]) if dual_conv else ok_t
        streak = streak + 1 if ok else 0
        if k % log_every == 0 or streak >= cfg["streak"]:
            print(f"  {st['label']} round {k}: excess {excess:8.2f} MW, move {move:8.2f} MW, s {s_k:8.1f} $/MWh,"
                  f" rho x{rho / rho0:.3g}, peak {rows[-1]['peak_mw']:8.1f} MW  ({time.time() - tic:.0f} s)",
                  flush=True)
        if streak >= cfg["streak"]:
            conv = k - cfg["streak"] + 1
            break
    if conv is not None:
        status, swing = "converged", rows[-1]["max_move_mw"]
    else:
        status, swing = classify(aggs, [r["max_excess_mw"] for r in rows], [r["sum_pi_usd_mwh"] for r in rows],
                                 st["feasible"])
    target_only = first_streak([r["target_only_ok"] for r in rows[1:]], cfg["streak"])
    V = np.array(sols)
    prox["highs_warnings"] = sum(q.warnings for q in qps)
    prox["highs_cold_resolves"] = sum(q.cold for q in qps)
    prox["rho_final"], prox["alpha_final"], prox["rho_changes"] = rho, alpha, changes
    prox["target_only_conv_round"], prox["shift_margin_kw"] = target_only, margin
    return {"X": X, "V": V, "pi": pi, "agg": agg, "rows": rows, "status": status, "conv_round": conv,
            "rounds_run": len(rows) - 1, "swing_mw": swing, "secs": time.time() - tic, **prox}


def check_plans(B, V, tol=1e-3):
    """SOC within 10-90 % and terminal energy at least S0, every agent (the agent model keeps build()'s constraints)."""
    T = B.T
    s = V[:, 2 * T:3 * T]
    lo, hi = 0.1 * B.CAP[:, None], 0.9 * B.CAP[:, None]
    return {"soc_bound_violations": int(((s < lo - tol) | (s > hi + tol)).sum()),
            "terminal_shortfall_kwh": float(np.maximum(B.S0 - s[:, -1], 0).sum())}


def evaluate(B, cfg, st, res, sf, ref, m0, roster):
    """Summary row and per-agent table for one setting."""
    T = B.T
    V = res["V"]
    c, d, u = V[:, :T], V[:, T:2 * T], V[:, 3 * T:4 * T]
    val = agent_values(B, c, d, u)
    pi = res["pi"]
    settle = (pi * res["X"]).sum(1)           # agent pays pi on its battery-driven net load ($/kW-step x kW)
    refund = (pi * sf["X"]).sum(1)            # pi_final on its own selfish plan (stand-in for a D3 reference)
    tol = np.maximum(cfg["harm_rel"] * np.abs(sf["val"]), cfg["harm_abs_usd"])
    loss = {"a": sf["val"] - val, "b": sf["val"] - (val - settle), "c": sf["val"] - (val - settle + refund)}
    agents = pd.DataFrame({"agent_id": roster, "power_kw": B.P, "selfish_value_usd": sf["val"], "value_usd": val,
                           "peak_settlement_usd": settle, "refund_usd": refund, "tolerance_usd": tol,
                           **{f"loss_{k}_usd": v for k, v in loss.items()},
                           **{f"harmed_{k}": v > tol for k, v in loss.items()}})
    if set(agents.agent_id) != set(roster) or len(agents) != len(roster):
        raise SystemExit(f"{st['label']}: per-agent table does not cover the roster")
    peak = float(res["agg"].max())
    rec, ceil = m0["recorded_mw"], m0["ceiling_mw"]
    fleet = float(val.sum())
    row = {
        "setting": st["label"], "kind": st["kind"], "headroom_mw": st["headroom"], "target_feasible": st["feasible"],
        "rho_usd_per_kw2_step": st["rho"], "alpha_usd_mwh_per_mw": st["alpha"] / B.K * 1e3,
        "status": res["status"], "rounds_to_converge": res["conv_round"], "rounds_run": res["rounds_run"],
        "final_max_excess_mw": res["rows"][-1]["max_excess_mw"], "final_move_mw": res["rows"][-1]["max_move_mw"],
        "swing_mw": res["swing_mw"],
        "peak_mw": peak / 1e3, "peak_vs_selfish_mw": (peak - sf["peak_kw"]) / 1e3,
        "share_of_m0_ceiling": (rec - peak / 1e3) / (rec - ceil), "central_peak_mw": ref["peak_kw"] / 1e3,
        "value_usd": fleet, "value_vs_selfish_usd": fleet - float(sf["val"].sum()),
        "value_share_of_m0_ceiling": (fleet - m0["recorded_value"]) / (m0["ceiling_value"] - m0["recorded_value"]),
        "central_value_usd": ref["value_usd"], "value_vs_central_usd": fleet - ref["value_usd"],
        "max_pi_usd_mwh": float(pi.max() / B.K), "max_pi_star_usd_mwh": float(ref["dual"].max() / B.K),
        "pi_l1_gap_usd_mwh_steps": float(np.abs(pi - ref["dual"]).sum() / B.K),
        **{k2: v2 for k in "abc" for k2, v2 in ((f"harmed_{k}_n", int(agents[f"harmed_{k}"].sum())),
                                                 (f"harmed_{k}_usd", float(agents.loc[agents[f"harmed_{k}"],
                                                                                      f"loss_{k}_usd"].sum())))},
        "charges_usd": float(settle.sum()), "refunds_usd": float(refund.sum()),
        "revenue_adequacy_usd": float(settle.sum() - refund.sum()),
        **check_plans(B, V), "secs": res["secs"],
        "prox_max_deadzone_usd_mwh": res["max_deadzone_usd_mwh"],
        "prox_max_segments_per_side": res["max_segments_per_side"],
        "highs_warnings": res["highs_warnings"], "highs_cold_resolves": res["highs_cold_resolves"],
        "rho_final": res["rho_final"], "alpha_final_usd_mwh_per_mw": res["alpha_final"] / B.K * 1e3,
        "rho_changes": res["rho_changes"],
        "target_only_conv_round": res["target_only_conv_round"], "shift_margin_kw": res["shift_margin_kw"],
        "rho_cap": st["adaptive"]["hi"] if st.get("adaptive") else None,
        "final_dual_res_usd_mwh": res["rows"][-1]["dual_res_usd_mwh"],
        "share_of_selfish_gap": (sf["peak_kw"] / 1e3 - peak / 1e3) / (sf["peak_kw"] / 1e3 - ceil),
        "central_share_of_selfish_gap": (sf["peak_kw"] - ref["peak_kw"]) / 1e3 / (sf["peak_kw"] / 1e3 - ceil),
    }
    excess = [r["max_excess_mw"] for r in res["rows"]]
    for tol_mw in cfg.get("within_mw", []):
        first, settled = rounds_within(excess, tol_mw)
        row[f"first_within_{tol_mw:g}mw"], row[f"settled_within_{tol_mw:g}mw"] = first, settled
    return row, agents


def curvature_check(B, qps, X0, pi, rho, eps, agents=3):
    """On the largest batteries, solve with the central dual as price and the selfish plan as x_prev. The PWL term
    must equal the secant interpolant of the move and lie at or above the exact square."""
    out = []
    for i in np.argsort(B.P)[::-1][:agents]:
        q = qps[i]
        q.set_rho(rho, eps)
        v, _, prox = q.solve(pi, X0[i])
        dlt = net_x(v, B.T) - X0[i]
        exact, interp = float(rho / 2 * (dlt ** 2).sum()), float(pwl_square(dlt, q.b, rho).sum())
        ok = exact - 1e-6 <= prox and abs(prox - interp) <= 1e-6 + 1e-6 * interp
        out.append({"agent": int(i), "max_move_kw": float(np.abs(dlt).max()), "exact_usd": exact,
                    "pwl_usd": prox, "interp_usd": interp, "ok": bool(ok)})
        if not ok:
            raise SystemExit(f"curvature check failed: {out[-1]}")
    for q in qps:
        q.set_rho(0.0)
    return out


# ---------- plot ----------

SLOTS_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]   # dataviz reference palette, slots 1-4
SLOTS_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500"]


def series_label(k):
    if k == "no-oracle":
        return "no-oracle (ρ_mid, price step from imbalance price)"
    if k == "free-rb":
        return "oracle-free (ρ from DA spread, α = ρ/N, residual balancing)"
    return f"ρ = {k[3:-1]} × ρ_mid"


def plot_svg(rounds, table, path, log=False):
    """Max excess over target vs. round, one panel per headroom, one line per setting (fixed slot per setting key).
    Native <title> hover on each line; the numbers are in rounds.csv."""
    heads = sorted(table.headroom_mw.unique())
    keys = sorted(set(table.setting.str.split("_", n=1).str[1]),
                  key=lambda k: (not k.startswith("rho"), float(k[3:-1]) if k.startswith("rho") else 0.0, k))
    slot = {k: i for i, k in enumerate(keys)}
    W, H, PL, PR, PT, PB, GAP = 1000, 452, 64, 16, 96, 48, 40
    pw = (W - PL - PR - GAP * (len(heads) - 1)) / len(heads)
    ph = H - PT - PB
    xmax = max(int(rounds["round"].max()), 1)
    FLOOR = 0.5                                                        # log axis: excess at or below this sits on it
    if log:
        ylo, yhi = np.log10(FLOOR), np.log10(float(rounds.max_excess_mw.max()) * 1.2)
        yticks = [v for v in (0.5, 1, 2, 5, 10, 20, 50, 100, 200) if np.log10(v) <= yhi]
        yv = lambda v: np.log10(np.maximum(v, FLOOR))
    else:
        ylo = min(0.0, float(rounds.max_excess_mw.min()))
        yhi = float(rounds.max_excess_mw.max()) * 1.05
        yticks = np.linspace(ylo, yhi, 6) if ylo < 0 else np.arange(0, yhi + 1e-9, 25 if yhi <= 200 else 50)
        yv = lambda v: v
    css = ("text{font:12px system-ui,sans-serif;fill:var(--t2)} .t1{fill:var(--t1);font-size:14px;font-weight:600}"
           ".grid{stroke:var(--grid);stroke-width:1} .zero{stroke:var(--t2);stroke-width:1}"
           " polyline{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}"
           ":root{--bg:#fcfcfb;--t1:#0b0b0b;--t2:#52514e;--grid:#e6e5e0;"
           + "".join(f"--s{i}:{c};" for i, c in enumerate(SLOTS_LIGHT)) + "}"
           "@media (prefers-color-scheme: dark){:root{--bg:#1a1a19;--t1:#f0efec;--t2:#c3c2b7;--grid:#33332f;"
           + "".join(f"--s{i}:{c};" for i, c in enumerate(SLOTS_DARK)) + "}}")
    g = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" role="img" '
         f'aria-label="Max net load over target by round, per headroom and setting"><style>{css}</style>',
         f'<rect width="{W}" height="{H}" fill="var(--bg)"/>',
         f'<text class="t1" x="{PL}" y="22">Price mode: max system net load over target, by round (MW'
         + (", log scale" if log else "") + ')</text>']
    lx = PL
    for k in keys:                                                     # legend row
        lab = series_label(k)
        g.append(f'<line x1="{lx}" y1="40" x2="{lx + 18}" y2="40" stroke="var(--s{slot[k]})" stroke-width="2"/>'
                 f'<text x="{lx + 24}" y="44">{lab}</text>')
        lx += 34 + 7 * len(lab)
    for p, h in enumerate(heads):
        x0 = PL + p * (pw + GAP)
        sx = lambda r: x0 + r / xmax * pw
        sy = lambda v: PT + (yhi - yv(v)) / (yhi - ylo) * ph
        for yt in yticks:
            g.append(f'<line class="grid" x1="{x0}" x2="{x0 + pw}" y1="{sy(yt):.1f}" y2="{sy(yt):.1f}"/>')
            if p == 0:
                lab = (f"\u2264{yt:g}" if yt == FLOOR else f"{yt:g}") if log else f"{yt:.0f}"
                g.append(f'<text x="{PL - 8}" y="{sy(yt) + 4:.1f}" text-anchor="end">{lab}</text>')
        if log:
            for lv in (1, 5):                                     # the 1 and 5 MW bands; the axis ticks label them
                g.append(f'<line class="zero" x1="{x0}" x2="{x0 + pw}" y1="{sy(lv):.1f}" y2="{sy(lv):.1f}"/>')
        else:
            g.append(f'<line class="zero" x1="{x0}" x2="{x0 + pw}" y1="{sy(0):.1f}" y2="{sy(0):.1f}"/>'
                     f'<text x="{x0 + 6}" y="{sy(0) - 6:.1f}">target</text>')
        for xt in np.linspace(0, xmax, 6):
            g.append(f'<text x="{sx(xt):.1f}" y="{PT + ph + 18}" text-anchor="middle">{xt:.0f}</text>')
        g.append(f'<text x="{x0 + pw / 2}" y="{H - 8}" text-anchor="middle">round</text>'
                 f'<text class="t1" x="{x0}" y="{PT - 14}">Headroom {h:g} MW</text>')
        for _, row in table[table.headroom_mw == h].iterrows():
            k = row.setting.split("_", 1)[1]
            r = rounds[rounds.setting == row.setting]
            pts = " ".join(f"{sx(a):.1f},{sy(b):.1f}" for a, b in zip(r["round"], r.max_excess_mw))
            tip = f"{row.setting}: {row.status}" + (f" at round {int(row.rounds_to_converge)}"
                                                    if row.status == "converged" else f" after {row.rounds_run}")
            g.append(f'<polyline points="{pts}" stroke="var(--s{slot[k]})"><title>{tip}</title></polyline>')
    g.append("</svg>")
    Path(path).write_text("\n".join(g), encoding="utf-8")


# ---------- table ----------

MD_COLS = [("setting", "Setting"), ("status", "Status"), ("rounds_to_converge", "Rounds"),
           ("swing_mw", "Swing MW"), ("peak_mw", "Peak MW"), ("peak_vs_selfish_mw", "vs selfish MW"),
           ("share_of_m0_ceiling", "Share of M0 ceiling"), ("value_vs_selfish_usd", "Value vs selfish $"),
           ("value_vs_central_usd", "Value vs central $"), ("harmed_a_n", "(a) n"), ("harmed_a_usd", "(a) $"),
           ("harmed_b_n", "(b) n"), ("harmed_b_usd", "(b) $"), ("harmed_c_n", "(c) n"), ("harmed_c_usd", "(c) $"),
           ("revenue_adequacy_usd", "Charges - refunds $")]


def fmt(v, col):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "-"
    if col in ("share_of_m0_ceiling", "share_of_selfish_gap"):
        return f"{v:.1%}"
    if col.startswith("alpha"):
        return f"{v:.3g}"
    if col == "rounds_to_converge" or col.endswith("_n") or "_within_" in col:
        return f"{int(v)}"
    if isinstance(v, (bool, np.bool_)):
        return "yes" if v else "no"
    if isinstance(v, (int, np.integer)):
        return f"{v:d}"
    if isinstance(v, float):
        return f"{v:,.1f}" if abs(v) < 1000 else f"{v:,.0f}"
    return str(v)


TAIL_COLS = [("setting", "Setting"), ("excess_last30_mw", "Excess MW, last 30 rounds"),
             ("best_excess_mw", "Best excess MW (round)"), ("peak_last10_mw", "Peak MW, last 10 rounds"),
             ("share_last10", "Share of M0 ceiling, last 10"), ("central_share", "Central LP share"),
             ("pi_vs_star", "Final max pi vs pi* $/MWh")]


def tail_stats(table, rounds, m0):
    """Per setting, what the round-150 snapshot hides: the late excess range, the best round, the late peak range
    and its share of the M0 ceiling, the central LP's share at the same target, and the final price vs. the dual."""
    rec, ceil = m0["recorded_mw"], m0["ceiling_mw"]
    share = lambda mw: (rec - mw) / (rec - ceil)
    out = []
    for _, r in table.iterrows():
        g = rounds[(rounds.setting == r.setting) & (rounds["round"] >= 1)]          # round 0 is selfish
        e30, p10 = g.max_excess_mw.iloc[-30:], g.peak_mw.iloc[-10:]
        best = g.loc[g.max_excess_mw.idxmin()]
        out.append({"setting": r.setting, "excess_last30_mw": f"{e30.min():.1f} to {e30.max():.1f}",
                    "best_excess_mw": f"{best.max_excess_mw:.1f} ({int(best['round'])})",
                    "peak_last10_mw": f"{p10.min():,.1f} to {p10.max():,.1f}",
                    "share_last10": f"{share(p10.max()):.1%} to {share(p10.min()):.1%}",
                    "central_share": f"{share(r.central_peak_mw):.1%}",
                    "pi_vs_star": f"{r.max_pi_usd_mwh:.1f} vs {r.max_pi_star_usd_mwh:.1f}"})
        if "rho_changes" in table:
            out[-1]["central_gap_share"] = f"{r.central_share_of_selfish_gap:.1%}"
            out[-1]["rho"] = f"{r.rho_usd_per_kw2_step:.3g} -> {r.rho_final:.3g} ({int(r.rho_changes)})"
            last = g.iloc[-1]                     # Boyd's dual residual s = rho x |plan change|, last round, $/MWh
            out[-1]["dual_res"] = f"{last.rho * last.plan_change_mw * 1e3 / K_MWH:,.0f}"
        if "target_only_conv_round" in table:
            s = g.rho * g.plan_change_mw * 1e3 / K_MWH
            out[-1]["rho_traj"] = rho_trajectory(g["round"].to_numpy(), g.rho.to_numpy())
            cap = r.get("rho_cap")
            out[-1]["at_cap"] = ("-" if cap is None or pd.isna(cap)
                                 else f"{int((g.rho >= cap * (1 - 1e-9)).sum())}")
            at = dict(zip(g["round"], s))
            out[-1]["s_checks"] = " / ".join(f"{at[c]:,.0f}" if c in at else "-" for c in S_CHECKS) \
                + f" / {s.iloc[-1]:,.1f}"
            out[-1]["s_min"] = f"{s.min():,.1f} ({int(g['round'].iloc[int(np.argmin(s.to_numpy()))])})"
    return out


S_CHECKS = (10, 50, 100, 200)


def rho_trajectory(rounds, rho, keep=8):
    """rho as multiples of its round-1 value at each change: 'x1@1, x2@3, ...'; long ones keep the first and last
    few changes."""
    pts = [(int(rounds[0]), rho[0])] + [(int(k), v) for k, v, p in zip(rounds[1:], rho[1:], rho[:-1]) if v != p]
    items = [f"x{v / rho[0]:.3g}@{k}" for k, v in pts]
    if len(items) > keep:
        items = items[:keep - 3] + [f"... {len(items) - keep + 1} more ..."] + items[-2:]
    return ", ".join(items)


V2_COLS = [("first_within_1mw", "First <= 1 MW"), ("settled_within_1mw", "Settled <= 1 MW"),
           ("first_within_5mw", "First <= 5 MW"), ("settled_within_5mw", "Settled <= 5 MW"),
           ("share_of_selfish_gap", "Share of selfish-ceiling gap")]


def write_tables(table, header, rounds, tag=""):
    sfx = f"_{tag}" if tag else ""
    cfg = header["config"]
    v3 = "target_only_conv_round" in table and (cfg.get("prox_centre", "own") != "own"
                                                or cfg.get("rb_dual", "raw") != "raw"
                                                or cfg.get("conv_rule", "target") != "target")
    if v3:
        table = table.copy()
        table["target_only_status"] = [f"converged @{int(k)}" if k is not None and not pd.isna(k) else "not converged"
                                       for k in table.target_only_conv_round]
        d49 = {r.setting: d49_entry(r) for _, r in table.iterrows() if r.kind == "oracle-free"}
        for col in ("settled_5mw", "price_10pct", "no_harm_c", "price_rel", "pass"):
            table[f"d49_{col}"] = [d49[s][col] if s in d49 else None for s in table.setting]
    table.to_csv(OUT / f"table{sfx}.csv", index=False)
    cols = MD_COLS[:3] + [c for c in V2_COLS if c[0] in table] + MD_COLS[3:]
    if v3:
        cols = (cols[:2] + [("target_only_status", "Target-only status")] + cols[2:3]
                + [("alpha_usd_mwh_per_mw", "alpha $/MWh per MW")] + cols[3:])
    lines = ["# Price-mode prototype, Jan 2 to 3 slice, 80 agents", "",
             f"Commit {header['git_commit']}. M0 recorded {header['m0']['recorded_mw']:.1f} MW, "
             f"ceiling {header['m0']['ceiling_mw']:.1f} MW. Selfish peak {header['selfish_peak_mw']:.1f} MW.", "",
             f"Final round ({header['config']['max_rounds']} unless converged):", "",
             *([f"Table regenerated from saved data at commit {header['table_regenerated_commit']} (no rerun).", ""]
               if header.get("table_regenerated_commit") else []),
             "| " + " | ".join(h for _, h in cols) + " |", "|" + "---|" * len(cols)]
    for _, r in table.iterrows():
        lines.append("| " + " | ".join(fmt(r[c], c) for c, _ in cols) + " |")
    lines += ["", "Late rounds and references:", "",
              "| " + " | ".join(h for _, h in TAIL_COLS) + " |", "|" + "---|" * len(TAIL_COLS)]
    tail = tail_stats(table, rounds, header["m0"])
    tcols = TAIL_COLS + ([("central_gap_share", "Central LP share of selfish gap"),
                          ("rho", "rho start -> final (changes)"),
                          ("dual_res", "Dual residual s, last round $/MWh")] if "rho_changes" in table else [])
    if v3:
        tcols = [c for c in tcols if c[0] not in ("rho", "dual_res")] + [
            ("rho_traj", "rho trajectory (x start @ round)"), ("at_cap", "Rounds at rho cap"),
            ("s_checks", "s at rounds " + "/".join(map(str, S_CHECKS)) + "/last $/MWh"),
            ("s_min", "Min s $/MWh (round)")]
    lines[lines.index("Late rounds and references:") + 2:lines.index("Late rounds and references:") + 4] = [
        "| " + " | ".join(h for _, h in tcols) + " |", "|" + "---|" * len(tcols)]
    for r in tail:
        lines.append("| " + " | ".join(r[c] for c, _ in tcols) + " |")
    if v3 and d49:
        rc = header.get("roster_check")
        if rc:
            lines += ["", f"Roster check: every setting's per-agent table covers the run header's {len(header['roster'])} "
                      "agents, and its harmed counts (a), (b), (c) match a recount from that table (check_roster)."]
        pf = lambda b: "PASS" if b else "FAIL"
        lines += ["", "**D49 arm C entry test** (PLAN.md D49, amended Oct 26; oracle-free rows only):", ""]
        for _, r in table[table.kind == "oracle-free"].iterrows():
            t = d49[r.setting]
            st5 = r.settled_within_5mw
            lines.append(
                f"- {r.setting}: settles within 5 MW {pf(t['settled_5mw'])} (settled from round "
                f"{fmt(st5, 'settled_within_5mw')}); final price within 10% of the central dual "
                f"{pf(t['price_10pct'])} ({r.max_pi_usd_mwh:.1f} vs {r.max_pi_star_usd_mwh:.1f} $/MWh, "
                f"{t['price_rel']:+.1%}); zero harmed after refund (c) {pf(t['no_harm_c'])} "
                f"({int(r.harmed_c_n)} agents). **{pf(t['pass'])}**")
        heads = sorted(table[table.kind == "oracle-free"].headroom_mw.unique())
        both = all(d49[s]["pass"] for s in d49)
        lines.append(f"- Arm C enters the head-to-head only if every oracle-free headroom passes "
                     f"({', '.join(f'{h:g}' for h in heads)} MW here): **{pf(both)}**.")
    lines += ["", "**Notes**",
              "- Status labels are heuristic (classify(): 2-cycles and direction flips in the max excess). Read them "
              "with the late-round ranges and the plot: a setting still improving at round 150 can be labeled "
              "oscillating, and a slow long-period cycle drifting.",
              "- Peak, value and per-agent figures in the first table are the final round's snapshot. Where the "
              "swing is large, the late-round ranges in the second table are the fairer read.",
              "- Central LP share: the best share of the M0 ceiling reachable at that target (the target, not the "
              "ceiling, caps it).",
              "- Rounds: first round of the closing streak (converged), else `-`. Swing: median max |change in "
              "aggregate| over the last 10 rounds.",
              "- Value: fleet RT energy revenue net of wear. Central: the fleet cap LP at the same target.",
              "- Per-agent loss vs. selfish, counted over max(1% of |selfish value|, $10); $ is the total loss of "
              "those agents. (a) energy value only. (b) energy value minus the peak-price settlement "
              "sum(pi x K x (c - d + u)). (c) (b) plus a refund of the final peak price on the agent's own selfish "
              "plan.",
              "- The selfish-plan refund in (c) is a prototype stand-in for the D3 enrollment reference. In a real "
              "system it is farmable: an agent could inflate its declared selfish plan to collect a larger refund.",
              "- Charges - refunds: fleet revenue adequacy of (c); negative means the operator pays out.",
              "- Settlement base: pi is charged on battery-driven net load (c - d + u) only. Agents discharge at "
              "priced steps, so gross charges come out negative (payments to agents). (c) and charges - refunds "
              "do not depend on the base (a fixed load or PV term cancels against the refund); (b) and gross "
              "charges and refunds would change if pi were charged on full site net.",
              "- Price step: alpha = theta x price_ref / selfish max excess. Oracle rows use the central dual for "
              "price_ref; the no-oracle rows use the imbalance price instead. rho_mid = max central dual / median "
              "battery power in every row, so the no-oracle rows still take rho from central knowledge.",
              "- Proximal term: (rho/2) x squared change, as piecewise-linear secants solved as an LP, not an exact "
              "QP (scipy's bundled HiGHS 1.12 QP solver did not finish one agent in 5 s and 106k iterations). The "
              "secants overstate the square by at most ~4.3% and leave a kink at zero change of at most "
              f"{header['config']['prox_deadzone_usd_mwh']} $/MWh."]
    if "rho_changes" in table:
        lines += ["- Within X MW: max excess over target <= X. First: the first round it happens. Settled: the round "
                  "from which every later round stays within (to the last round run); `-` if never.",
                  "- Share of selfish-ceiling gap: (selfish peak - peak) / (selfish peak - M0 ceiling), with this "
                  f"prototype's own selfish peak ({header['selfish_peak_mw']:,.1f} MW). The central LP's share on the "
                  "same basis is in the late-round table.",
                  "- Oracle-free rows: rho0 = mean daily day-ahead spread x K / median registered battery power "
                  f"({header.get('da_spread_usd_mwh', float('nan')):.1f} $/MWh, {header['median_power_kw']:,.0f} kW); "
                  "price step alpha = rho / N, re-tied every round; residual balancing (mu = "
                  f"{cfg.get('rb_mu')}, tau = {cfg.get('rb_tau')}) on the primal residual "
                  "(L2 of the projected excess over target) vs. "
                  + ("mu x s / rho0 (s the dual residual below, rho0 the starting rho; kW), rho within rho0 / "
                     f"{cfg.get('rb_bounds_factor')} to {cfg.get('rb_rho_max_factor')} x rho0. "
                     if v3 and cfg.get("rb_dual") == "scaled" else
                     "the plan change (L2 of all agents' plan changes), both kW. ")
                  + "No central dual is used, but the target itself is the operator's (selfish day peak - "
                  "headroom)."]
        if v3:
            lines += ["- Agent update: " + (
                          "textbook exchange ADMM. Each agent's prox is centred on its own last plan minus the mean "
                          "projected residual, shift = (pi_new - pi_old) / (alpha N) per step: the gap / N where the "
                          "step stays priced, 0 where unpriced and under target. With alpha = rho / N (oracle-free) "
                          "this is Boyd's sharing ADMM with pi = rho u. The oracle row keeps v2's alpha, so there it "
                          "is the same shift with its own alpha. The PWL range grows by 2 x selfish max excess / N to "
                          "cover the shift; the run stops if any shift exceeds that."
                          if cfg.get("prox_centre") == "textbook" else "prox on the change from the agent's last plan."),
                      "- Dual residual s = rho x L2 norm of all agents' plan change in a round (kW-step prices shown "
                      "in $/MWh), at the rho that round used. Not Boyd's exact exchange form, which uses |r| / sqrt N "
                      "and the change in x_i - mean x.",
                      "- `converged` (Status, Rounds): " + (
                          f"max excess over target <= {cfg['tol_excess_mw']} MW and s <= "
                          f"{cfg.get('tol_dual_usd_mwh')} $/MWh for {cfg['streak']} rounds in a row; the run stops "
                          "there. The s threshold was set before running, just above v2's oracle rows' final s (5.8 "
                          "and 4.0 $/MWh)." if cfg.get("conv_rule") == "target+dual" else "the target-only rule.")
                      + " Target-only status: v2's rule (excess, plan movement, slackness, "
                      f"{cfg['streak']} rounds) applied to the same rounds, reported but not used to stop.",
                      "- rho trajectory: rho at each change as a multiple of its starting value, with the round it was "
                      "first used. Rounds at rho cap: rounds run at the upper bound."]
        else:
            lines += ["- `converged` is the primal stopping rule only (excess, plan movement, slackness for 3 rounds). "
                      "Dual residual s = rho x L2 norm of all agents' plan change in the last round (kW-step prices "
                      "shown in $/MWh): how far plans are from best responses to the price. A large s with a small "
                      "plan change means a large rho froze the plans, not that they reached the equilibrium.",
                      "- Residual balancing here compares the primal residual with the raw plan change (both kW), as "
                      "specified. Boyd et al. compare it with the rho-scaled dual residual. With the raw change, "
                      "raising rho shrinks the change and invites a further raise; the oracle-free rows show that "
                      "runaway (rho up about 500x in under 70 rounds)."]
    (OUT / f"table{sfx}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- main ----------

def no_oracle_price(cfg, s):
    p = REPO / cfg["no_oracle_price_from"]
    other = load_config(p) if p.exists() else {}
    if "price_cap_usd_mwh" in other:
        return float(other["price_cap_usd_mwh"]), f"{cfg['no_oracle_price_from']}: price_cap_usd_mwh"
    return float(s.system["price_rt"].max()), "slice max RT price"


def main(cfg_path, smoke=False, only=None):
    cfg = load_config(cfg_path)
    s = load_slice(REPO / cfg["data_dir"], cfg["start"], cfg["end"], cfg["slice_tz"])
    B = load_bench(s)
    use_slice(B, s)
    roster = [str(x) for x in s.ids]
    T = B.T

    # M0, recomputed in-process and checked against main's runs/m0
    print("M0 ...", flush=True)
    bess = s.agent["bess"]
    rec = {"c": np.maximum(-bess, 0), "d": np.maximum(bess, 0), "u": s.agent["curt"]}
    pk = solve(B, peak=True)
    vl = solve(B, cap_kw=float(pk["z"][0]) * 1.01)
    m0 = {"recorded_mw": float(net_load(B, **rec).max()) / 1e3, "recorded_value": value(B, **rec),
          "ceiling_mw": float(net_load(B, **dispatch(pk["x"])).max()) / 1e3,
          "ceiling_value": value(B, **dispatch(vl["x"]))}
    for k in ("recorded", "ceiling"):
        if abs(m0[f"{k}_mw"] - cfg["m0_expect_mw"][k]) > M0_TOL_MW:
            raise SystemExit(f"M0 {k} peak {m0[f'{k}_mw']:.2f} MW != expected {cfg['m0_expect_mw'][k]}")
    print(f"  recorded {m0['recorded_mw']:.2f} MW, ceiling {m0['ceiling_mw']:.2f} MW", flush=True)

    # Selfish (round 0) and each agent's model; regression at rho = 0, pi = 0
    print("selfish and agent models ...", flush=True)
    tic = time.time()
    qps, sfx, fun = capture_agents(B)
    X0 = sfx["c"] - sfx["d"] + sfx["u"]
    base = B.SL - B.PV.sum(0)
    agg0 = base + X0.sum(0)
    sf = {"X": X0, "val": agent_values(B, sfx["c"], sfx["d"], sfx["u"]), "peak_kw": float(agg0.max())}
    assert np.allclose(agg0, net_load(B, sfx["c"], sfx["d"], sfx["u"]))
    tq = time.time()
    reg = np.array([abs(q.solve(np.zeros(T))[1] - f) / max(1.0, abs(f)) for q, f in zip(qps, fun)])
    lp_secs = (tq - tic) / B.N
    if (reg > REG_RTOL).any():
        raise SystemExit(f"regression failed for {[roster[i] for i in np.where(reg > REG_RTOL)[0]]}")
    print(f"  selfish peak {sf['peak_kw'] / 1e3:.2f} MW; regression max rel diff {reg.max():.2e}", flush=True)

    # Targets and central references
    day = s.ts_utc.tz_convert(cfg["slice_tz"]).date
    pmed = float(np.median(B.P))
    imb, imb_src = no_oracle_price(cfg, s)
    refs, settings = {}, []
    pairs = cfg.get("oracle_pairs") or [[h, m] for h in cfg["headroom_mw"] for m in cfg["rho_mults"]]
    free_h = cfg.get("free_headroom_mw", [])
    spread = da_spread(s, cfg["slice_tz"])
    rho0 = spread * B.K / pmed
    for h in sorted({p[0] for p in pairs} | set(cfg["no_oracle_headroom_mw"]) | set(free_h)):
        target = np.empty(T)
        for dd in np.unique(day):
            target[day == dd] = agg0[day == dd].max() - h * 1e3
        ref = central_ref(B, target)
        dual = ref["dual"]
        free = dual < 1 - 1e-9                        # steps not at the $1/kW-step slack penalty
        pi_ref = float(dual[free].max())
        e0 = float((agg0 - target).max())
        rho_mid = pi_ref / pmed
        refs[h] = {"target": target, "ref": ref, "pi_ref": pi_ref, "e0": e0, "rho_mid": rho_mid}
        print(f"  headroom {h} MW: targets {sorted({round(float(v) / 1e3, 1) for v in target})} MW, central "
              f"{'feasible' if ref['feasible'] else 'INFEASIBLE'} (slack {ref['slack_kw_steps']:.0f} kW-steps on "
              f"{ref['slack_steps']} steps), central peak {ref['peak_kw'] / 1e3:.1f} MW, max dual "
              f"{pi_ref / B.K:.1f} $/MWh, rho_mid {rho_mid:.3g}", flush=True)
        for m in [pm for ph, pm in pairs if ph == h]:
            settings.append({"label": f"h{h}_rho{m:g}x", "kind": "oracle", "headroom": h, "rho": m * rho_mid,
                             "alpha": cfg["theta"] * pi_ref / e0, "feasible": ref["feasible"]})
        if h in cfg["no_oracle_headroom_mw"]:
            settings.append({"label": f"h{h}_no-oracle", "kind": "no-oracle", "headroom": h, "rho": rho_mid,
                             "alpha": cfg["theta"] * imb * B.K / e0, "feasible": ref["feasible"]})
        if h in free_h:
            f = cfg["rb_bounds_factor"]
            fhi = cfg.get("rb_rho_max_factor", f)
            settings.append({"label": f"h{h}_free-rb", "kind": "oracle-free", "headroom": h, "rho": rho0,
                             "alpha": rho0 / B.N, "feasible": ref["feasible"],
                             "adaptive": {"mu": cfg["rb_mu"], "tau": cfg["rb_tau"], "lo": rho0 / f, "hi": rho0 * fhi}})
        for st in settings:
            if st["headroom"] == h:                  # textbook centre: PWL reach for |shift| <= 2 x selfish excess / N
                st["shift_margin_kw"] = 2 * e0 / B.N

    header = {**run_header("bench/price_mode_proto.py", cfg, seed=cfg.get("seed")), "roster": roster,
              "m0": m0, "selfish_peak_mw": sf["peak_kw"] / 1e3, "median_power_kw": pmed,
              "no_oracle_price_usd_mwh": imb, "no_oracle_price_source": imb_src,
              "da_spread_usd_mwh": spread, "rho0_public": rho0,
              "agent_backend": f"scipy {scipy.__version__} bundled HiGHS {hc.HIGHS_VERSION_MAJOR}."
                               f"{hc.HIGHS_VERSION_MINOR}.{hc.HIGHS_VERSION_PATCH} LP (private "
                               f"scipy.optimize._highspy._core); prox as PWL secants, ratio sqrt 2, "
                               f"kink <= {cfg['prox_deadzone_usd_mwh']} $/MWh",
              "regression_max_rel_diff": float(reg.max()),
              "headrooms": {str(h): {"targets_mw": sorted({round(float(v) / 1e3, 3) for v in r["target"]}),
                                     "central_feasible": r["ref"]["feasible"],
                                     "central_slack_kw_steps": r["ref"]["slack_kw_steps"],
                                     "central_peak_mw": r["ref"]["peak_kw"] / 1e3,
                                     "central_value_usd": r["ref"]["value_usd"],
                                     "pi_ref_usd_mwh": r["pi_ref"] / B.K, "selfish_max_excess_mw": r["e0"] / 1e3,
                                     "rho_mid": r["rho_mid"]} for h, r in refs.items()},
              "settings": [{k: v for k, v in st.items()} for st in settings]}
    OUT.mkdir(parents=True, exist_ok=True)
    tag = cfg.get("out_tag", "")
    sfx = f"_{tag}" if tag else ""

    if smoke:
        st = next(x for x in settings if x["label"].endswith("_rho1x"))
        r = refs[st["headroom"]]
        curv = curvature_check(B, qps, X0, r["ref"]["dual"], st["rho"], cfg["prox_deadzone_usd_mwh"] * B.K)
        print(f"  curvature check at rho_mid: {curv}", flush=True)
        res = run_setting(B, qps, X0, base, r["target"], st, cfg, SMOKE_ROUNDS, log_every=1)
        per_round = float(np.mean([row["secs"] for row in res["rows"][2:]]))      # warm rounds 2..
        est = per_round * cfg["max_rounds"] * len(settings) / 60
        smoke_out = {**header, "lp_secs_per_agent": lp_secs, "curvature_check": curv,
                     "secs_per_warm_round": per_round, "worst_case_sweep_min": est, "n_settings": len(settings),
                     "max_deadzone_usd_mwh": res["max_deadzone_usd_mwh"],
                     "max_segments_per_side": res["max_segments_per_side"], "rounds": res["rows"]}
        # v1 and v2 wrote smoke.json; a v3+ config tags it so earlier smoke output stays
        name = f"smoke{sfx}.json" if tag not in ("", "v2") else "smoke.json"
        (OUT / name).write_text(json.dumps(smoke_out, indent=2, default=str))
        print(f"smoke: {per_round:.2f} s per warm round (80 agents); worst case {len(settings)} settings x "
              f"{cfg['max_rounds']} rounds = {est:.0f} min", flush=True)
        return

    sdir = OUT / f"settings{sfx}"
    sdir.mkdir(exist_ok=True)
    for st in settings:
        if only and st["label"] not in only:
            continue
        print(f"setting {st['label']} (rho {st['rho']:.3g}, alpha {st['alpha'] / B.K * 1e3:.3g} $/MWh per MW)",
              flush=True)
        r = refs[st["headroom"]]
        res = run_setting(B, qps, X0, base, r["target"], st, cfg, cfg["max_rounds"])
        row, agents = evaluate(B, cfg, st, res, sf, r["ref"], m0, roster)
        row["git_commit"] = header["git_commit"]
        print(f"  -> {row['status']}, peak {row['peak_mw']:.1f} MW ({row['share_of_m0_ceiling']:.1%} of ceiling)",
              flush=True)
        (sdir / f"{st['label']}.json").write_text(json.dumps(row, indent=2, default=float))
        agents.to_csv(sdir / f"agents_{st['label']}.csv", index=False)
        pd.DataFrame(res["rows"]).to_csv(sdir / f"rounds_{st['label']}.csv", index=False)
        pd.DataFrame({"t_utc": s.ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "target_kw": r["target"],
                      "selfish_kw": agg0, "final_kw": res["agg"], "pi_usd_mwh": res["pi"] / B.K,
                      "pi_star_usd_mwh": r["ref"]["dual"] / B.K}).to_csv(
            sdir / f"steps_{st['label']}.csv", index=False)

    # Table and plot over every setting on disk, in config order
    order = [st["label"] for st in settings]
    done = [l for l in order if (sdir / f"{l}.json").exists()]
    header["settings_in_table"] = done
    write_outputs(header, sdir, tag, bool(cfg.get("plot_log")))


def check_roster(sdir, labels, roster):
    """Rule 10 across processes: each setting's per-agent table must cover exactly the run header's roster, and its
    harmed counts (a), (b), (c), which the summary and the D49 test read, must match a recount from that table.
    Stops on any mismatch. Returns {label: {"agents": n, "harmed_c_n": n}}."""
    want = sorted(str(x) for x in roster)
    out = {}
    for l in labels:
        a = pd.read_csv(sdir / f"agents_{l}.csv", dtype={"agent_id": str})
        got = sorted(a.agent_id)
        if got != want:
            missing, extra = sorted(set(want) - set(got)), sorted(set(got) - set(want))
            raise SystemExit(f"{l}: per-agent table does not match the run header's roster "
                             f"(missing {missing}, extra {extra}, {len(got)} rows vs {len(want)} ids)")
        row = json.loads((sdir / f"{l}.json").read_text())
        for k in "abc":
            n = int((a[f"loss_{k}_usd"] > a["tolerance_usd"]).sum())
            if n != int(row[f"harmed_{k}_n"]):
                raise SystemExit(f"{l}: harmed ({k}) recount {n} from the per-agent table != summary "
                                 f"{row[f'harmed_{k}_n']}")
        out[l] = {"agents": len(got), "harmed_c_n": n}
    return out


def write_outputs(header, sdir, tag, plot_log):
    """Roster check, then run header, rounds, table and plot over header['settings_in_table'] from sdir."""
    sfx = f"_{tag}" if tag else ""
    done = header["settings_in_table"]
    header["roster_check"] = check_roster(sdir, done, header["roster"])
    table = pd.DataFrame([json.loads((sdir / f"{l}.json").read_text()) for l in done])
    rounds = pd.concat([pd.read_csv(sdir / f"rounds_{l}.csv") for l in done])
    (OUT / f"run{sfx}.json").write_text(json.dumps(header, indent=2, default=str))
    rounds.to_csv(OUT / f"rounds{sfx}.csv", index=False)
    write_tables(table, header, rounds, tag)
    plot_svg(rounds, table, OUT / f"convergence{sfx}.svg", log=plot_log)
    print((OUT / f"table{sfx}.md").read_text(encoding="utf-8"))


def table_only(cfg_path):
    """Regenerate the table and plot from saved per-setting files and run_<tag>.json, no rerun. The header keeps the
    run's commit; the commit that regenerated the table is added."""
    cfg = load_config(cfg_path)
    tag = cfg.get("out_tag", "")
    sfx = f"_{tag}" if tag else ""
    header = json.loads((OUT / f"run{sfx}.json").read_text())
    header["table_regenerated_commit"] = git_commit()
    write_outputs(header, OUT / f"settings{sfx}", tag, bool(cfg.get("plot_log")))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("config", nargs="?", default=REPO / "configs" / "price_proto.yaml")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--only", default=None, help="comma-separated setting labels")
    ap.add_argument("--table-only", action="store_true", help="regenerate table and plot from saved files, no rerun")
    a = ap.parse_args()
    if a.table_only:
        table_only(a.config)
    else:
        main(a.config, smoke=a.smoke, only=set(a.only.split(",")) if a.only else None)
