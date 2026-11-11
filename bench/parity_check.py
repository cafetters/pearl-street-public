"""
Parity check: a cloud run against the laptop reference run of the same config (Nov 4 parity gate; report only).

Reads <run>/<trace dir>/trace.jsonl (--trace-dir, default exchange_h100_rinfo) and <run>/summary.json in both run
dirs. Line 1 of each trace (the
header) is left out of the trace checks. Every check below must pass for PASS:

  telemetry  (t_utc, payload.asset_id, payload.net_kw) for every msg_type == "telemetry": same count and same keys,
             no duplicate keys, every net_kw within --telemetry-tol kW of the reference (exact matches and the max
             absolute difference are reported)
  trace text every line after the header identical as text, with line endings normalized (CRLF == LF)
  payout     sum of payload.net_usd over msg_type == "peak_settle": the cloud sum within --payout-tol USD of the
             reference sum and, when --payout is given, both sums within --payout-tol USD of it (owner, Nov 4)
  commit     the cloud header's git_commit starts with --commit and has no -dirty anywhere in the header
  summary    summary.json equal at every path, leaving out run_at_utc, data_dir and the wall-time fields
             (SUMMARY_SKIP; also any key containing "wall"); NaN equals NaN. /header/git_commit is not compared
             with the reference: it must equal the cloud trace header's git_commit (checked under commit). Differences in the left-out fields are
             printed for information only.

Exit 0 on PASS, 1 on FAIL.

Run:  python -m bench.parity_check --cloud <run dir> --reference <run dir> --payout 7418.1914 --commit 9bcc0d1
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

TRACE_DIR = "exchange_h100_rinfo"      # default; --trace-dir names another variant folder (e.g. exchange_h150_rinfo)
# wall-time and machine-specific fields, left out of the summary comparison wherever they appear
SUMMARY_SKIP = {"run_at_utc", "data_dir", "runtime_s", "round_secs", "wall_utc", "round_wall_utc",
                "compare_solve_secs"}


def _skipped(key):
    return key in SUMMARY_SKIP or "wall" in str(key).lower()


def read_trace(run_dir, trace_dir=TRACE_DIR):
    path = Path(run_dir) / trace_dir / "trace.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise SystemExit(f"empty trace: {path}")
    header = json.loads(lines[0])
    if header.get("event") != "header":
        raise SystemExit(f"line 1 of {path} is not a header")
    return header, lines[1:]


def scan(body):
    """Telemetry by (t_utc, asset_id), duplicate key count, peak_settle payout sum and message count."""
    tel, dup, pay, n_settle = {}, 0, 0.0, 0
    for line in body:
        o = json.loads(line)
        mt = o.get("msg_type")
        p = o.get("payload") or {}
        if mt == "telemetry":
            k = (o.get("t_utc"), p.get("asset_id"))
            if k in tel:
                dup += 1
            tel.setdefault(k, []).append(p.get("net_kw"))
        elif mt == "peak_settle":
            pay += p.get("net_usd", 0.0)
            n_settle += 1
    return tel, dup, pay, n_settle


def _same(a, b):
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


def summary_diffs(a, b, path=""):
    """(gated, excluded): lists of (path, cloud, reference) differences."""
    gated, excluded = [], []

    def walk(x, y, p, skip):
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(set(x) | set(y), key=str):
                q = f"{p}/{k}"
                s = skip or _skipped(k)
                if k not in x or k not in y:
                    (excluded if s else gated).append((q, x.get(k, "<missing>"), y.get(k, "<missing>")))
                else:
                    walk(x[k], y[k], q, s)
        elif isinstance(x, list) and isinstance(y, list) and len(x) == len(y):
            for i, (u, v) in enumerate(zip(x, y)):
                walk(u, v, f"{p}[{i}]", skip)
        elif not _same(x, y):
            (excluded if skip else gated).append((p, x, y))

    walk(a, b, path, False)
    return gated, excluded


def _short(v, n=120):
    r = repr(v)
    return r if len(r) <= n else r[:n] + "..."


def main(argv=None):
    ap = argparse.ArgumentParser(description="Compare a cloud run against the laptop reference run.")
    ap.add_argument("--cloud", required=True, help="cloud run dir")
    ap.add_argument("--reference", required=True, help="laptop reference run dir")
    ap.add_argument("--payout", type=float, default=None,
                    help="expected payout, USD (optional; both sums must be within --payout-tol of it)")
    ap.add_argument("--payout-tol", type=float, default=0.01, help="payout tolerance, USD (default 0.01)")
    ap.add_argument("--telemetry-tol", type=float, default=0.001, help="telemetry tolerance, kW (default 0.001)")
    ap.add_argument("--commit", required=True, help="expected short git hash in the cloud header")
    ap.add_argument("--trace-dir", default=TRACE_DIR,
                    help=f"variant folder holding trace.jsonl in both run dirs (default {TRACE_DIR})")
    args = ap.parse_args(argv)

    hc, bc = read_trace(args.cloud, args.trace_dir)
    hr, br = read_trace(args.reference, args.trace_dir)
    fails = []
    print(f"cloud:     {args.cloud}")
    print(f"reference: {args.reference}")
    print(f"trace:     {args.trace_dir}/trace.jsonl")

    # 1. telemetry
    tc, dc, pc, nc = scan(bc)
    tr, dr, pr, nr = scan(br)
    cnt_c = sum(len(v) for v in tc.values())
    cnt_r = sum(len(v) for v in tr.values())
    only_c, only_r = set(tc) - set(tr), set(tr) - set(tc)
    exact = over = 0
    maxd, worst = 0.0, None
    for k in set(tc) & set(tr):
        if len(tc[k]) != len(tr[k]):
            over += 1
        for a, b in zip(tc[k], tr[k]):
            if a == b:
                exact += 1
                continue
            if a is None or b is None:
                over += 1
                continue
            d = abs(a - b)
            if d > args.telemetry_tol:
                over += 1
            if d > maxd:
                maxd, worst = d, (k, a, b)
    tel_ok = cnt_c == cnt_r and not only_c and not only_r and dc == 0 and dr == 0 and over == 0
    print("\n[telemetry]")
    print(f"  count cloud {cnt_c}, reference {cnt_r} ({'match' if cnt_c == cnt_r else 'MISMATCH'})")
    print(f"  keys only in cloud {len(only_c)}, only in reference {len(only_r)}; duplicate keys {dc}, {dr}")
    print(f"  exact matches {exact} of {cnt_r}")
    print(f"  max abs difference {maxd} kW" + (f" at {worst[0]}: {worst[1]} vs {worst[2]}" if worst else ""))
    print(f"  over {args.telemetry_tol} kW: {over}")
    print(f"  -> {'ok' if tel_ok else 'FAIL'}")
    if not tel_ok:
        fails.append("telemetry")

    # 2. full trace text, line endings normalized (splitlines), header left out
    ndiff = sum(1 for a, b in zip(bc, br) if a != b) + abs(len(bc) - len(br))
    first = next((i for i, (a, b) in enumerate(zip(bc, br)) if a != b), None)
    text_ok = ndiff == 0
    print("\n[trace text]")
    print(f"  lines after header: cloud {len(bc)}, reference {len(br)}; differing {ndiff}")
    if first is not None:
        print(f"  first difference at line {first + 2}:")
        print(f"    cloud     {bc[first][:300]}")
        print(f"    reference {br[first][:300]}")
    print(f"  -> {'ok' if text_ok else 'FAIL'}")
    if not text_ok:
        fails.append("trace text")

    # 3. payout
    tol = args.payout_tol
    vs_ref = abs(pc - pr) <= tol
    print("\n[payout]")
    print(f"  cloud     {pc:.6f} USD ({nc} peak_settle)")
    print(f"  reference {pr:.6f} USD ({nr} peak_settle)")
    print(f"  cloud minus reference {pc - pr:+.6f} (tol {tol}): {'ok' if vs_ref else 'FAIL'}")
    pay_ok = vs_ref
    if args.payout is not None:
        c_exp, r_exp = abs(pc - args.payout) <= tol, abs(pr - args.payout) <= tol
        print(f"  expected {args.payout}: cloud minus expected {pc - args.payout:+.6f} ({'ok' if c_exp else 'FAIL'}), "
              f"reference minus expected {pr - args.payout:+.6f} ({'ok' if r_exp else 'FAIL'})")
        pay_ok = pay_ok and c_exp and r_exp
    else:
        print("  expected: not given")
    print(f"  -> {'ok' if pay_ok else 'FAIL'}")
    if not pay_ok:
        fails.append("payout")

    # 4. commit
    gc = str(hc.get("git_commit", ""))
    dirty = "dirty" in json.dumps(hc).lower()
    commit_ok = bool(args.commit) and gc.startswith(args.commit) and not dirty
    print("\n[commit]")
    print(f"  cloud header git_commit {gc} (reference {hr.get('git_commit')})")
    print(f"  expected prefix {args.commit}: {'yes' if gc.startswith(args.commit) else 'NO'}; "
          f"-dirty in header: {'YES' if dirty else 'no'}")
    hdiff = [k for k in sorted(set(hc) | set(hr)) if not _same(hc.get(k), hr.get(k))]
    for k in hdiff:
        if isinstance(hc.get(k), dict) and isinstance(hr.get(k), dict):
            for kk in sorted(set(hc[k]) | set(hr[k])):
                if not _same(hc[k].get(kk), hr[k].get(kk)):
                    print(f"  header differs (info): {k}.{kk}: {_short(hc[k].get(kk))} | {_short(hr[k].get(kk))}")
        else:
            print(f"  header differs (info): {k}: {_short(hc.get(k))} | {_short(hr.get(k))}")
    print(f"  -> {'ok' if commit_ok else 'FAIL'}")
    if not commit_ok:
        fails.append("commit")

    # 5. summary.json
    sc = json.loads((Path(args.cloud) / "summary.json").read_text(encoding="utf-8"))
    sr = json.loads((Path(args.reference) / "summary.json").read_text(encoding="utf-8"))
    gated, excluded = summary_diffs(sc, sr)
    # The summary's header names the commit that ran it, which differs from the reference's by design whenever a
    # change is checked against main. It isn't compared with the reference: it must equal the cloud trace header's
    # commit instead, which [commit] above checks against --commit (owner, Nov 6: every check must PASS)
    s_commit = (sc.get("header") or {}).get("git_commit")
    commit_same = s_commit == hc.get("git_commit")
    gated = [g for g in gated if not (g[0] == "/header/git_commit" and commit_same)]
    if not commit_same:
        gated.append(("/header/git_commit (vs the cloud trace header)", s_commit, hc.get("git_commit")))
    sum_ok = not gated
    print("\n[summary.json]")
    print(f"  summary header git_commit equals the cloud trace header's: {'yes' if commit_same else 'NO'}")
    print(f"  differing fields (gated): {len(gated)}")
    for p, a, b in gated:
        print(f"    {p}: {_short(a)} | {_short(b)}")
    # left-out fields, list items folded into one line per path ([*]) so per-round wall clocks stay short
    folded = {}
    for p, a, b in excluded:
        folded.setdefault(re.sub(r"\[\d+\]", "[*]", p), []).append((a, b))
    print(f"  differing left-out fields (info): {len(excluded)} values at {len(folded)} paths")
    for p, vals in folded.items():
        a, b = vals[0]
        more = f" ({len(vals)} values, first shown)" if len(vals) > 1 else ""
        print(f"    {p}: {_short(a)} | {_short(b)}{more}")
    for name, s in (("cloud", sc), ("reference", sr)):
        t = s.get("m1_1_pass_test")
        if isinstance(t, dict):
            print(f"  m1_1_pass_test ({name}): pass {t.get('pass')}, by_headroom {t.get('by_headroom')}")
    t = sc.get("m1_1_pass_test")
    if isinstance(t, dict) and t.get("rule"):
        print(f"  m1_1_pass_test rule: {t['rule']}")
    print(f"  -> {'ok' if sum_ok else 'FAIL'}")
    if not sum_ok:
        fails.append("summary.json")

    print()
    if fails:
        print(f"FAIL ({', '.join(fails)})")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
