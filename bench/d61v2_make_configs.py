"""D61 v2 configs (gap 4; owner, Nov 7, D4). The rehearsal's generator (runs/nov07/v2r/make_configs.py, itself a copy of
the pinned runs/d61/make_configs.py, never edited) made into a committed, tested tool. It keeps the pinned generator's
rule (docs/plans/d61v2_attacks_check.md): a copy of configs/d58m/h2h_c_sep05_h100_m0.yaml without comment lines, with
only run_name, start, end, m0_dir, peak_response_margin_usd_mwh, the flags below and `attackers` (attack cells) set,
plus `settlement_rule: max_first_call` in the conditional reruns.

Flags, every run: D61's (delivery_record, silent_reference, declared_reference: true; d62_guarantee: false) and the
v2 draft's (nomination_span: operating_day, D86; peak_steps: controllable, D57), written out. No warmup_days.

Writes (default configs/d61v2/):
  h2h_c_d61v2_<w>_twin_m<m>.yaml        honest twin: flags on, no attackers
  h2h_c_d61v2_<w>_<attack>_m<m>.yaml    attack cell: the twin plus `attackers` and its run_name, nothing else
  ..._max.yaml                          the conditional max-rule rerun of each
  h2h_c_d61v2smoke_sep05_{twin,all}_m5.yaml   the smoke pair (ten attackers, one per type; prerequisite 3)
  m0_<w>.yaml                           each window's M0 config, as the pinned generator writes them
Pure templating: reads only the committed template; reads no data, runs nothing.

    python -m bench.d61v2_make_configs [--out DIR] [--window TAG START END ...] [--smoke-only]
"""
import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "configs" / "d61v2"
SRC = REPO / "configs" / "d58m" / "h2h_c_sep05_h100_m0.yaml"
DOC = "docs/plans/d61v2_attacks_check.md"
PREFIX = "h2h_c_d61v2"
# the v2 windows (owner, Nov 6; runs/nov06/v2_windows/), in run order
WINDOWS = [("dec11", "2023-12-11", "2023-12-12"), ("jan09", "2023-01-09", "2023-01-10"),
           ("feb26", "2023-02-26", "2023-02-27"), ("jun02", "2023-06-02", "2023-06-03")]
SMOKE = ("sep05", "2023-09-05", "2023-09-06")
ATTACKER = "DC_08"                       # largest battery power (10,619 kW), as D61
SMOKE_ATTACKERS = ["DC_08", "PVB_48", "DC_09", "PVB_35", "PVB_12", "PVB_36", "PVB_50", "DC_03", "DC_05",
                   "PVB_39"]             # top 10 by battery power, one per attack type (PVB_39: zero_b), as D61
FLAGS = {"delivery_record": "true", "silent_reference": "true", "declared_reference": "true",
         "d62_guarantee": "false", "nomination_span": "operating_day", "peak_steps": "controllable"}
M0_ATTACKS = ("renege_full", "renege_day", "renege_zero_b")


def attacks(day1: str) -> dict[str, dict]:
    """D61's ten $5 attacks, unchanged."""
    return {
        "renege_full": {"type": "renege", "mode": "full"},
        "renege_day": {"type": "renege", "mode": "day", "day": day1},
        "renege_zero_b": {"type": "renege", "mode": "zero_b"},
        "silent_deviate": {"type": "silent", "mode": "deviate"},
        "silent": {"type": "silent"},
        "outage_stop": {"type": "outage", "mode": "stop", "day": day1},
        "outage_run": {"type": "outage", "mode": "run", "day": day1},
        "misdeclare_power": {"type": "misdeclare", "mode": "power"},
        "misdeclare_reserve": {"type": "misdeclare", "mode": "reserve"},
        "infeasible_declare": {"type": "infeasible_declare"},
    }


def flow(spec: dict) -> str:
    return "{" + ", ".join(f"{k}: '{v}'" if k == "day" else f"{k}: {v}" for k, v in spec.items()) + "}"


def cell(keys: dict, template: str) -> str:
    out = [f"# D61 v2 ({DOC}). Copy of {SRC.relative_to(REPO).as_posix()} without comments, "
           f"with only {', '.join(keys)} set."]
    done = set()
    for ln in template.splitlines():
        if ln.lstrip().startswith("#") or not ln.strip():
            continue
        k = ln.split(":", 1)[0].strip() if ":" in ln else None
        if k in keys:
            out.append(f"{k}: {keys[k]}")
            done.add(k)
        else:
            out.append(ln)
    out += [f"{k}: {v}" for k, v in keys.items() if k not in done]
    return "\n".join(out) + "\n"


def _m0(tag, d0, d1) -> str:
    return (f"# D61 v2 ({DOC}): M0 ceiling for {d0} to {d1} -> runs/m0_{tag}/.\ndata_dir: ../data\nstart: {d0}\n"
            f"end: {d1}\nslice_tz: America/Chicago\nm0_dir: runs/m0_{tag}\n")


def build(windows=WINDOWS, smoke=SMOKE, prefix=PREFIX) -> dict[str, str]:
    template = SRC.read_text(encoding="utf-8")
    files: dict[str, str] = {}

    def write(name, keys):
        files[f"{name}.yaml"] = cell({"run_name": name, **keys}, template)
        files[f"{name}_max.yaml"] = cell({"run_name": f"{name}_max", **keys, "settlement_rule": "max_first_call"},
                                         template)

    for tag, d0, d1 in windows:
        base = {"start": d0, "end": d1, "m0_dir": f"runs/m0_{tag}", **FLAGS}
        for m, mk in (("5.0", "m5"), ("0.0", "m0")):
            keys = {**base, "peak_response_margin_usd_mwh": m}
            write(f"{prefix}_{tag}_twin_{mk}", keys)
            for name, spec in attacks(d0).items():
                if mk == "m0" and name not in M0_ATTACKS:
                    continue
                write(f"{prefix}_{tag}_{name}_{mk}", {**keys, "attackers": f"[{flow({'agent': ATTACKER, **spec})}]"})
        files[f"m0_{tag}.yaml"] = _m0(tag, d0, d1)
    if smoke:
        tag, d0, d1 = smoke
        keys = {"start": d0, "end": d1, "m0_dir": f"runs/m0_{tag}", **FLAGS, "peak_response_margin_usd_mwh": "5.0"}
        name = f"{prefix}smoke_{tag}"
        files[f"{name}_twin_m5.yaml"] = cell({"run_name": f"{name}_twin_m5", **keys}, template)
        at = attacks(d0)
        order = [k for k in at if k != "renege_zero_b"] + ["renege_zero_b"]
        every = [flow({"agent": a, **at[k]}) for a, k in zip(SMOKE_ATTACKERS, order, strict=True)]
        files[f"{name}_all_m5.yaml"] = cell({"run_name": f"{name}_all_m5", **keys,
                                             "attackers": "[" + ", ".join(every) + "]"}, template)
        files[f"m0_{tag}.yaml"] = _m0(tag, d0, d1)
    return files


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--window", nargs=3, action="append", metavar=("TAG", "START", "END"),
                    help="replace the v2 windows (e.g. a design window for a rehearsal); repeat")
    ap.add_argument("--smoke-only", action="store_true", help="only the sep05 smoke pair and its M0 config")
    a = ap.parse_args(argv)
    windows = [] if a.smoke_only else ([tuple(w) for w in a.window] if a.window else WINDOWS)
    files = build(windows=windows)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8", newline="\n")
    print(len(files), "configs in", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
