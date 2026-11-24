"""Course-route live check (owner, Nov 23; plan step 2): one strategy decision per model through the course's LLM gateway,
with a fixed synthetic agent view (no simulation). Writes `runs/course_route_smoke/llm_calls.jsonl` with a header
(config, seed, git commit, the strategy header) and the call records, and `trace.jsonl` with the run header and roster,
so bench/course_route_check.py can judge it. About $0.001 at list prices; the run cap is $0.05.

usage: python -m bench.course_route_smoke [--out runs/course_route_smoke]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agents.llm.agent_view import AgentView
from agents.llm.chooser import LLMRun
from bench.runinfo import run_header

MODELS = [("anthropic", "claude-haiku-5-5"), ("openai", "gpt-6-luna"), ("anthropic", "claude-sonnet-5-5")]
CAP_USD = 0.05


def view() -> AgentView:
    return AgentView(decision_day="2023-01-13", reason="start", power_kw=1000.0, energy_kwh=2000.0,
                     bold_allowed=False, current_label="normal", label_history=(),
                     da_prices_hourly=tuple([30.0] * 16 + [90.0] * 4 + [30.0] * 4), da_spread=60.0,
                     da_spread_threshold=20.0, net_load_forecast_hourly=tuple([1.0] * 24), peak_price_max=None,
                     peak_price_steps=0, imbalance_usd_since_last_call=0.0, rejects_since_eval=0, reject_codes=(),
                     binding_calls_since_eval=0, peak_settle_net_usd=None, peak_settle_refund_usd=None,
                     site_net_forecast_kwh_yesterday=None, site_net_metered_kwh_yesterday=None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="runs/course_route_smoke")
    a = ap.parse_args(argv)
    out = Path(a.out)
    if out.exists():
        print(f"{out} exists: move it aside first (nothing is overwritten)")
        return 2
    roster = [f"SMOKE_{p}_{m}".replace("-", "_") for p, m in MODELS]
    cfg = {"script": "course_route_smoke", "models": [f"{p}/{m}" for p, m in MODELS], "cap_usd_run": CAP_USD}
    head = {**run_header("bench/course_route_smoke.py", cfg, 0), "event": "header", "run_id": "course_route_smoke",
            "roster": roster}
    records, headers = [], []
    for agent, (p, m) in zip(roster, MODELS):
        spec = {"chooser": "llm", "backend": "live", "route": "course", "cap_usd_run": CAP_USD,
                "honest_models": [{"provider": p, "model": m, "weight": 1.0}]}
        run = LLMRun(spec, roster=[agent], attackers=set(), seed=0, run_id="course_route_smoke",
                     power_kw={agent: 1000.0})
        d = run.chooser_for(agent).decide(view(), "start")
        records += run.call_records()
        headers.append(run.header())
        r = run.call_records()[-1]
        print(f"{p}/{m}: label {d.label!r} error {d.error!r} tokens {r['tokens_in']}/{r['tokens_out']} "
              f"reported ${r['usd_reported']} table ${r['usd_table']}")
    out.mkdir(parents=True)
    strat = {**headers[0], "models": [h["models"] for h in headers], "chooser_by_agent": {a: "llm" for a in roster}}
    side = {"event": "header", "run_id": head["run_id"], "config": head["config"], "seed": head["seed"],
            "git_commit": head["git_commit"], "llm_strategy": strat}
    (out / "trace.jsonl").write_text(json.dumps(head) + "\n", encoding="utf-8")
    with open(out / "llm_calls.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(side) + "\n")
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")
    print(f"wrote {out}; judge it with: python -m bench.course_route_check {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
