"""Run a red-team phase from a config (fake backend only, M5).

    python -m redteam.launch --phase configs/redteam/phase_fake.yaml [--out runs/redteam/<name>]
    python -m redteam.launch <launcher config.yaml>     # the cloud launcher's form (--module redteam.launch)

The launcher form reads a run config with run_name and an inline `phase` block, writes runs/<run_name>/ with every
world's trace and summary.json (header: config, seed, git commit; the phase report), as bench.cloud_queue expects.

A phase config names `cap_usd_phase` (D94), the catalog directory, and an ordered list of round config files; each
round config (configs/redteam/<round>.yaml) names its type, attackers, D93 attacker list, round cap, flags, gateway
limits, world and `fake_playbooks` ({agent: playbook}). Rounds run in order; each round's confirmed exploits join the
catalog before the next round's regression gate (the catalog fed forward). A backend other than fake is refused:
real-model rounds need their own pre-registration (docs/plans/redteam_round1.md) and keys (D96).
Writes <out>/<round>/round_report.json (header: config, seed, git commit) and <out>/phase_report.json.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from agents.llm.spend import CapReached, PhaseAllocator
from redteam.fake import FakeAttacker
from redteam.rounds import run_round

REPO = Path(__file__).resolve().parents[1]


def load(p) -> dict:
    return yaml.safe_load(Path(p).read_text(encoding="utf-8"))


def backend_factory(rc: dict):
    if str(rc.get("backend", "fake")) != "fake":
        raise ValueError("only the fake backend runs here (no provider calls without a pre-registration, D61/D96)")
    pb = rc.get("fake_playbooks") or {}

    def make(agent_id, draw):
        return FakeAttacker(str(pb.get(agent_id, "honest_like")),
                            channel_tool={"collusion_shared": "message_partner",
                                          "collusion_planted": "post_bulletin"}.get(rc["type"]))
    return make


def run_phase(phase_cfg: dict, out: Path, root: Path = REPO) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    phase = PhaseAllocator(float(phase_cfg["cap_usd_phase"]))
    cat = root / phase_cfg.get("catalog_dir", "attacks/catalog")
    rows = []
    for rpath in phase_cfg["rounds"]:
        rc = load(root / rpath)
        try:
            rep = run_round(rc, out / rc["name"], backend_factory(rc), phase=phase, catalog_dir=cat,
                            write_catalog=bool(phase_cfg.get("write_catalog", False)))
        except CapReached as e:
            rows.append({"round": rc["name"], "refused": f"phase cap: {e}"})
            continue
        rows.append({"round": rc["name"], "type": rc["type"], "candidate": rep["triage"].get("candidate"),
                     "reasons": rep["triage"].get("reasons"), "confirmed": [e["id"] for e in rep["confirmed"]],
                     "classes": {e["id"]: e.get("class") for e in rep["confirmed"]},
                     "gate_blocked": {g["id"]: g["blocked"] for g in rep["regression_gate"]},
                     "refusal_rate_by_model": rep["refusal_rate_by_model"], "spent_usd": rep["spend"]["spent_usd"],
                     "channel": rep.get("channel"), "injection_pass": (rep.get("injection") or {}).get("pass")})
    res = {"phase": phase_cfg.get("name"), "cap_usd_phase": phase.cap, "spent_usd": phase.spent, "rounds": rows,
           "label": "[sim, synthetic] fake backend"}
    (out / "phase_report.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("config", nargs="?", help="launcher form: a run config with run_name and a phase block")
    ap.add_argument("--phase")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.config:
        from bench.runinfo import run_header
        cfg = load(a.config)
        out = REPO / "runs" / str(cfg["run_name"])
        r = run_phase(cfg["phase"], out)
        summary = {"header": run_header("redteam/launch.py", cfg, cfg.get("seed", 1)), **r}
        (out / "summary.json").write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
    elif a.phase:
        pc = load(a.phase)
        r = run_phase(pc, Path(a.out) if a.out else REPO / "runs" / "redteam" / str(pc.get("name", "phase")))
    else:
        ap.error("give a launcher config or --phase")
    print(json.dumps(r, indent=1, default=str))
