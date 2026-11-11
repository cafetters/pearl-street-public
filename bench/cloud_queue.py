"""
Cloud queue: run simulation configs on Maritime sandboxes started from the pearl-base snapshot (D84; PLAN.md
section 6, "Cloud run rules (Maritime)").

One sandbox per run, killed when the run ends. For each run:
  1. wait until the account has fewer than --max-awake sandboxes awake, then sandboxes create --from pearl-base,
     Small, idle timeout 10 min (on idle: kill), time limit from the rules below; the name
     ps-<queue>-<run>-a<attempt>-<hash> and the id go to runs/cloud/_queues/<queue_id>/created.jsonl at once
  2. upload a fresh git bundle (made once per queue), the config, its M0 folder, any file it reads from other runs
     (--runs-from: runs/<compare_to>/summary.json, an earlier summary with ablation_150), and the job scripts
  3. in the sandbox: fetch the bundle, check out the full commit, pip install -e . --no-deps, then confirm HEAD is
     the commit and `git status --porcelain` is empty; the pip freeze sha256 is recorded
  4. start the run in the background and poll it every --poll-s seconds (default 60) with a short attached exec;
     the polls are what keep the sandbox from idling out, since background commands do not count as activity
  5. download runs/<run_name>/ as a tarball (sha256 checked), check it holds summary.json and a trace, write DONE,
     and rename it into runs/cloud/<run_name>/ in one step (with the sandbox logs and metrics in cloud/)
  6. sandboxes kill, confirmed by info (state ended), then read costSoFarUsd and runningSeconds
The queue ends with `sandboxes list` (every page, live): any of its sandboxes still live is a queue failure.
The report goes to runs/cloud/_queues/<queue_id>/ (report.json, cost_log.csv, queue.log, created.jsonl, failed/).

Stopping: Ctrl+C, or create runs/cloud/_queues/<queue_id>/STOP. No new sandbox and no retry after it; running
sandboxes are killed within about a minute. A queue stopped this way, with no run failed before the stop and every
sandbox confirmed ended, reports status "stopped" and exits 2 (ok stays false); any failure, including a run that
failed before the stop, reports "failed" and exits 1; a finished queue "ok", 0. After a crash or a killed launcher: --cleanup <queue_id> kills every
live sandbox in the journal or named ps-<queue_id>-... with queue=<queue_id> metadata, and confirms with a listing.

Rules enforced before anything is created (PLAN.md section 6):
  one launcher a machine-wide OS lock, held for the whole queue, so two launchers can't both pass these checks
  concurrency  at most 40 sandboxes awake for code-only queues, 5 with --llm (a config with a truthy top-level key
               containing "llm" needs --llm); --max-awake (default and max 40) caps every awake sandbox on the
               account, counted before each create
  run budget   --budget is the queue's sandbox count, declared up front: the launcher never creates more. A create
               that makes no sandbox spends no slot. Only infrastructure failures are retried, once; a nonzero run
               exit, wrong HEAD, dirty tree, failed setup or incomplete output is not
  lifetimes    time limit = max(1 h, 2 x --measured-runtime-s), in whole minutes; without a measured runtime the
               queue must be a single --pilot run at 24 h. 2x over 24 h is refused (needs chunking), never capped
  spend        period spend read before starting (`maritime usage get` over the billing period and `billing status`
               usedCents, cross-checked as compute minutes x the Small rate; the largest counts). Hard cap = the
               lower of the account cap (`billing status` capCents) and $400 (PLAN; owner, Nov 5): spend + the
               queue's worst case (budget x time limit x rate) must stay under it. Pace = $400 x days elapsed /
               days in period; once spend is over $50, above 1.5x pace is reported and above 2x pace refuses
               without --owner-approved-over-pace
  data         each config's data_dir must be ../data (the snapshot holds the data at /home/user/data); its M0
               folder's window must be the config's start to end
Rate limits: HTTP 429/502/503/504 are waited out (the server's retry hint, else backoff) for up to 15 min per call;
a run fails only after 15 min without a good status poll.

Polls (owner, Nov 6; PLAN open items): the launcher's polls go straight to the Maritime API over HTTPS, not through
maritime.cmd: the status poll of a running stage (POST <dataPlaneUrl>/commands with the sandbox secret in X-Maritime-Token, an attached
command that also keeps the sandbox awake), the usage read every fifth poll (GET /v1/sandboxes/<id>/usage) and the state read after a failed
poll (GET /v1/sandboxes/<id>/info). The account token is read at runtime, from MARITIME_TOKEN or else the Maritime
CLI's entry in the Windows credential store (maritime:token), kept in memory only, and never logged or written: every
error text passes redact() and request headers are never logged. Creates, uploads, downloads, kills, listings and
the spend reads stay on the CLI. --cli-polls puts the polls back on the CLI (the behavior before Nov 6).

Every other maritime call goes through maritime.cmd, so no argument may contain a cmd.exe metacharacter; all shell work
happens in scripts uploaded to /home/user/job/. Local files are copied with cwd set to their folder and a relative
name, since `sandbox cp` reads "<sandbox>:/path" and a drive letter's colon would be taken for a sandbox name.
Responses are never logged whole: sandbox info and create carry the sandbox secret.

Run:  python -m bench.cloud_queue --run <config.yaml> <m0 folder> [--run ...] --commit 9bcc0d1 --concurrency 1
          --budget 1 --measured-runtime-s 292.6 [--max-awake 10] [--runs-from <runs folder>]
      --dry-run checks the rules and prints the plan without creating anything; it writes nothing under
          runs/cloud/_queues/, so its queue id stays free. A queue id already used is refused (dry run or not).
      --cleanup <queue_id> kills what a crashed launcher left.
"""
import argparse
import csv
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from bench.runinfo import run_header
from sim.data import REPO

SNAPSHOT = "pearl-base"
API_BASE = "https://api.maritime.sh"     # the CLI's own base URL (its --dry-run prints these requests)
CRED_TARGET = "maritime:token"           # where the Maritime CLI keeps the account token (Windows credential store)
API_TIMEOUT_S = 90                       # one HTTP poll; an attached status command is cut at EXEC_WAIT_S
SIZE = "small"
RATE_USD_H = 0.022            # Small, 2 vCPU 4 GB (pricing page; Nov 4 sandbox: $0.005273 for 862 s)
IDLE = "10m"
CAP_CODE, CAP_LLM = 40, 5
MIN_LIMIT_S, PILOT_LIMIT_S, CHUNK_S = 3600, 86400, 20 * 3600
HARD_STOP_USD, PACE_FROM_USD = 400.0, 50.0
PERIOD = ("2026-11-04", "2026-12-05")   # first billing period (PLAN.md); pass --period-* once it renews
EXEC_WAIT_S = 60                         # an attached exec waits at most this long
STOP_SWEEP_S = 60                        # after a stop, workers get this long to kill their own sandboxes before the
FIND_WAITS_S = (0, 10, 30)               # listings after a failed create, looking for a sandbox made anyway
STOP_GRACE_S = 330                      # main thread kills them; it waits this long (over the create timeout) in all
# Ctrl+C reaches every process on the console; maritime.cmd children get their own process group (and no stdin, so
# cmd.exe can't stop at "Terminate batch job (Y/N)?") and only the launcher's handler sees it.
CHILD_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
HOME = "/home/user"
JOB = f"{HOME}/job"
CODE = f"{HOME}/pearl-street"
# one launcher at a time on this machine (all worktrees): an OS lock on a held handle, released when the process
# ends however it ends, so a killed launcher never leaves a stale lock
LOCK_PATH = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "pearl-street" / "cloud_queue.lock"
LOCK_WAIT_S = 10
AWAKE_WAIT_S, AWAKE_GIVE_UP_S = 60, 1800  # account at --max-awake: re-check every 60 s, give the slot back after 30 min
# Maritime errors read "... returned HTTP <code>: {json}". These are worth waiting out; anything else fails at once.
RETRYABLE = re.compile(r"HTTP (?:429|502|503|504)\b|\"code\":\s*\"(?:rate_limit\w*|too_many_requests|awake_limit\w*|"
                       r"no_ready_machine|capacity\w*|unavailable|service_unavailable|overloaded)\"", re.I)
RETRY_HINT = re.compile(r"retry[-_ ]?after(?:[-_ ]?(ms|seconds|s))?\"?\s*[:=]?\s*\"?(\d+(?:\.\d+)?)", re.I)
SECRETS = [(re.compile(r"\b(mst|mut|sk|pk)_[A-Za-z0-9_-]{8,}"), r"\1_***"),
           (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "JWT***"),
           (re.compile(r"(?i)(bearer\s+)[^\s\"',}]+"), r"\1***"),
           (re.compile(r"(?i)(x-maritime-token\"?\s*[:=]\s*\"?)[^\s\"',}]+"), r"\1***"),
           (re.compile(r'"(secret|urlToken|token|apiKey|api_key|password)"\s*:\s*"[^"]*"', re.I), r'"\1": "***"')]
POLL_FAIL_S = 900                        # a run fails only after 15 min without one good status poll
KILL_RETRY_WAITS_S = (5, 15, 30)         # a kill not confirmed by info is retried after these waits
REQUIRED_OUTPUTS = ("summary.json",)    # plus at least one trace.jsonl anywhere in the run folder
QUEUE_ID_MAX = 24                        # leaves room in the 63-character sandbox name for the run and a hash
CMD_META = re.compile(r'[&|<>^%"\r\n]')
NAME_OK = re.compile(r"^[A-Za-z0-9_.-]+$")
COMMIT_OK = re.compile(r"^[0-9a-f]{7,40}$")

# --- scripts run inside the sandbox -------------------------------------------------------------------------
STAGE_SH = """#!/bin/bash
# stage.sh <name>: run <name>.sh, log to <name>.log, exit code to <name>.exit (written last)
cd /home/user/job
bash "$1.sh" > "$1.log" 2>&1
echo $? > "$1.exit.tmp" && mv "$1.exit.tmp" "$1.exit"
"""
STATUS_SH = """#!/bin/bash
# status.sh <name>: one line, always exit 0
cd /home/user/job
if [ -f "$1.exit" ]; then echo "EXIT $(cat "$1.exit")"; else echo "RUNNING $(tail -n 1 "$1.log" 2>/dev/null | cut -c1-160)"; fi
"""
SETUP_SH = """set -eu
rm -rf {code}
git init -q {code}
cd {code}
git fetch -q {job}/repo.bundle '+refs/heads/*:refs/remotes/bundle/*'
git -c advice.detachedHead=false checkout -q --detach {commit}
mkdir -p runs/_cloud {m0_dir}
cp {job}/config.yaml runs/_cloud/{run_name}.yaml
if ls {job}/m0/* >/dev/null 2>&1; then cp {job}/m0/* {m0_dir}/; fi
{context_cmds}
{home}/venv/bin/python -m pip install --no-input -q -e . --no-deps
echo "FREEZE_SHA256 $({home}/venv/bin/python -m pip freeze --all --exclude-editable | sha256sum | cut -d' ' -f1)"
echo "HEAD $(git rev-parse HEAD)"
echo "PORCELAIN_BEGIN"
git status --porcelain
echo "PORCELAIN_END"
"""
RUN_SH = """set -u
cd {code}
{home}/venv/bin/python -u {job}/measure.py {job}/metrics.json -- {home}/venv/bin/python -u -m {module} runs/_cloud/{run_name}.yaml
rc=$?
if [ -d runs/{run_name} ]; then
  tar czf {job}/out.tgz -C runs {run_name} && sha256sum {job}/out.tgz | cut -d' ' -f1 > {job}/out.sha256
fi
exit $rc
"""
MEASURE_PY = """import json, resource, subprocess, sys, time
out, cmd = sys.argv[1], sys.argv[sys.argv.index("--") + 1:]
t = time.time()
rc = subprocess.call(cmd)
m = {"elapsed_s": round(time.time() - t, 1), "max_rss_mb": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024,
     "exit": rc}
open(out, "w").write(json.dumps(m))
print("elapsed_s", m["elapsed_s"], "max_rss_mb", m["max_rss_mb"])
sys.exit(rc)
"""


class QueueError(Exception):
    pass


def redact(text: str) -> str:
    for pat, sub in SECRETS:
        text = pat.sub(sub, text)
    return text


def retry_hint_s(text: str):
    m = RETRY_HINT.search(text)
    if not m:
        return None
    v = float(m.group(2))
    return v / 1000.0 if (m.group(1) or "").lower() == "ms" else v


class MaritimeError(QueueError):
    def __init__(self, msg, retryable=False):
        super().__init__(msg)
        self.retryable = retryable


class CreateRefused(QueueError):
    """A create that made no sandbox: it spent no budget slot."""


class RunError(QueueError):
    """A failure that would repeat on a second sandbox (nonzero run exit, wrong HEAD, dirty tree, failed setup,
    incomplete output): recorded, never retried."""


class OutputIncomplete(RunError):
    """The run exited 0 but its output folder lacks summary.json or every trace."""


class Stopped(QueueError):
    """The queue was stopped (Ctrl+C or the STOP file): no new sandbox, no retry."""


# --- maritime CLI ---------------------------------------------------------------------------------------------
class Maritime:
    def __init__(self, log):
        self.exe = shutil.which("maritime.cmd")
        if not self.exe:
            raise QueueError("maritime.cmd not found on PATH")
        self.log = log

    def _run(self, argv, cwd, timeout):
        """The one place a maritime.cmd process starts (tests replace this)."""
        return subprocess.run([self.exe, *argv], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, creationflags=CHILD_FLAGS)

    backoff_cap_s = 120        # longest computed wait between tries of a rate-limited or unavailable call
    hint_max_s = 300           # longest server retry hint honoured
    backoff_total_s = 900      # after this long the call gives up

    def call(self, *args, cwd=None, timeout=300, check=True):
        """One CLI call. HTTP 429/502/503/504 (rate limit, awake limit, no capacity) is waited out: the server's
        retry hint if it gives one, else 5 s doubling up to backoff_cap_s, for at most backoff_total_s. One call at
        a time per caller, so a refusal never piles up requests."""
        for a in args:
            if CMD_META.search(str(a)):
                raise QueueError(f"argument has a cmd.exe metacharacter: {a!r}")
        t0, k = time.time(), 0
        while True:
            p = self._run([str(a) for a in args], cwd, timeout)
            text = f"{p.stderr or ''}\n{p.stdout or ''}"
            retryable = p.returncode != 0 and bool(RETRYABLE.search(text))
            if not retryable or time.time() - t0 > self.backoff_total_s:
                break
            hint = retry_hint_s(text)
            wait = min(hint, self.hint_max_s) if hint is not None else min(self.backoff_cap_s, 5.0 * 2 ** k)
            k += 1
            self.log(f"maritime {' '.join(map(str, args[:2]))}: {RETRYABLE.search(text).group(0)}; "
                     f"waiting {wait:.0f} s ({'server hint' if hint is not None else 'backoff'}, try {k + 1})")
            time.sleep(wait)
        if check and p.returncode != 0:
            raise MaritimeError(f"maritime {' '.join(map(str, args[:3]))} exit {p.returncode}: "
                                f"{redact((p.stderr or p.stdout).strip())[:500]}", retryable=retryable)
        return p

    def json(self, *args, **kw):
        out = self.call(*args, "--json", **kw).stdout
        return json.loads(out[out.index("{"):])

    def cp_up(self, sbx, local: Path, remote: str):
        self.call("sandbox", "cp", local.name, f"{sbx}:{remote}", cwd=local.parent, timeout=900)

    def cp_down(self, sbx, remote: str, local: Path):
        local.parent.mkdir(parents=True, exist_ok=True)
        self.call("sandbox", "cp", f"{sbx}:{remote}", local.name, cwd=local.parent, timeout=900)

    def exec(self, sbx, *cmd, check=True):
        """Attached exec, at most EXEC_WAIT_S."""
        return self.call("sandbox", "exec", sbx, "--timeout-seconds", EXEC_WAIT_S, "--", *cmd,
                         timeout=EXEC_WAIT_S + 60, check=check)

    def exec_bg(self, sbx, *cmd):
        return self.call("sandbox", "exec", sbx, "--background", "--", *cmd).stdout.strip()

    def live_sandboxes(self):
        """Every live sandbox on the account, every page, read from the API (not the CLI's local store or cache)."""
        out, cursor = [], None
        while True:
            r = self.json("sandboxes", "list", "--data-source", "live", "--no-cache",
                          *(["--cursor", cursor] if cursor else []))
            out += r.get("sandboxes") or []
            cursor = r.get("nextCursor")
            if not cursor:
                return out

    def metadata(self, s):
        if "metadata" in s:
            return s.get("metadata") or {}
        return self.json("sandbox", "info", s["id"], "--data-source", "live", "--no-cache",
                         "--select", "id,metadata").get("metadata") or {}


# --- polls over the Maritime API (owner, Nov 6) ----------------------------------------------------------------
def read_token() -> str:
    """The account token, read when first needed: MARITIME_TOKEN, else the CLI's Windows credential store entry.
    Returned to the caller only; never logged, printed or written anywhere."""
    t = (os.environ.get("MARITIME_TOKEN") or "").strip()
    if t:
        return t
    if os.name != "nt":
        raise QueueError("no API token: set MARITIME_TOKEN (the credential store is read on Windows only)")
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    adv.CredReadW.restype = wintypes.BOOL
    pcred = ctypes.POINTER(CREDENTIAL)()
    if not adv.CredReadW(CRED_TARGET, 1, 0, ctypes.byref(pcred)):          # 1 = CRED_TYPE_GENERIC
        raise QueueError(f"no API token: credential {CRED_TARGET} not found (log in with the Maritime CLI)")
    try:
        c = pcred.contents
        blob = bytes(c.CredentialBlob[:c.CredentialBlobSize])
    finally:
        adv.CredFree(pcred)
    for enc in ("utf-8", "utf-16-le"):
        try:
            tok = blob.decode(enc).strip().strip(chr(0)).strip()
        except UnicodeDecodeError:
            continue
        if tok and tok.isprintable():
            return tok
    raise QueueError(f"no API token: credential {CRED_TARGET} could not be read as text")


class ApiPolls:
    """The launcher's polls as direct HTTPS calls. One call per poll, no retry loop (wait_stage already tolerates
    POLL_FAIL_S of failed polls). The token is read on first use and kept on this object only."""

    def __init__(self, log, base=API_BASE, token_fn=read_token):
        self.log, self.base, self._token_fn, self._token = log, base.rstrip("/"), token_fn, None
        self._dp = {}                      # sandbox id -> (dataPlaneUrl, sandbox secret); never logged
        self.calls = 0

    def __repr__(self):                    # never shows the token or a secret
        return f"ApiPolls(base={self.base!r}, calls={self.calls})"

    def _http(self, method, url, body=None, headers=None, timeout=API_TIMEOUT_S):
        """The one place a request leaves this process (tests replace it). Returns (status, text)."""
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Accept": "application/json", **({"Content-Type": "application/json"} if data else {}), **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def _headers(self, auth, sbx):
        if self._token is None:
            self._token = self._token_fn()
        if auth == "account":
            return {"Authorization": f"Bearer {self._token}"}
        return {"X-Maritime-Token": self._dp[sbx][1]}     # the sandbox's data plane: its secret (live probe, Nov 6)

    def _call(self, method, url, body=None, auth="account", sbx=None):
        where = f"{method} {url.split('?')[0]}"
        self.calls += 1
        try:
            code, text = self._http(method, url, body, self._headers(auth, sbx))
            if code == 401 and auth == "account":   # expired: read the token again once (the CLI may have refreshed it)
                self._token = None
                code, text = self._http(method, url, body, self._headers(auth, sbx))
        except QueueError:
            raise
        except Exception as e:  # noqa: BLE001 (network errors: reported redacted, never with headers)
            raise MaritimeError(f"{where}: {redact(type(e).__name__ + ': ' + str(e))[:300]}", retryable=True) from None
        if not 200 <= code < 300:
            raise MaritimeError(f"{where} returned HTTP {code}: {redact(text)[:300]}",
                                retryable=bool(RETRYABLE.search(f"HTTP {code}")))
        try:
            return json.loads(text) if text.strip() else {}
        except ValueError:
            raise MaritimeError(f"{where}: not JSON: {redact(text)[:200]}") from None

    def _data_plane(self, sbx):
        if sbx not in self._dp:
            sb = self._call("GET", f"{self.base}/v1/sandboxes/{sbx}")
            url, secret = sb.get("dataPlaneUrl"), sb.get("secret")
            if not url or not secret:
                raise MaritimeError(f"sandbox {sbx}: no dataPlaneUrl or secret in GET /v1/sandboxes/{sbx}")
            self._dp[sbx] = (url.rstrip("/"), secret)
        return self._dp[sbx][0]

    def status(self, sbx, stage):
        """status.sh <stage> through the data plane, as a CompletedProcess (returncode, stdout, stderr)."""
        try:
            url = self._data_plane(sbx)
            r = self._call("POST", f"{url}/commands", {"cmd": ["bash", f"{JOB}/status.sh", stage],
                                                       "timeoutSeconds": EXEC_WAIT_S}, auth="sandbox", sbx=sbx)
        except QueueError as e:
            return subprocess.CompletedProcess([], 1, "", str(e))
        rc = r.get("exitCode", r.get("exit_code"))
        out = r.get("stdout")
        if out is None and isinstance(r.get("output"), str):
            out = r["output"]
        if rc is None or out is None:
            return subprocess.CompletedProcess([], 1, "", f"command response without exitCode/stdout: keys {sorted(r)}")
        return subprocess.CompletedProcess([], int(rc), out, r.get("stderr") or "")

    def usage(self, sbx):
        return self._call("GET", f"{self.base}/v1/sandboxes/{sbx}/usage")

    def state(self, sbx):
        return self._call("GET", f"{self.base}/v1/sandboxes/{sbx}/info").get("state")


class CliPolls:
    """--cli-polls: the polls through maritime.cmd, as before Nov 6."""

    def __init__(self, mar):
        self.mar, self.calls = mar, 0

    def status(self, sbx, stage):
        self.calls += 1
        return self.mar.exec(sbx, "bash", f"{JOB}/status.sh", stage, check=False)

    def usage(self, sbx):
        self.calls += 1
        return self.mar.json("sandbox", "usage", sbx)

    def state(self, sbx):
        self.calls += 1
        return sandbox_info(self.mar, sbx)["state"]


# --- created-sandbox journal and cleanup ----------------------------------------------------------------------
def name_prefix(queue_id):
    """Every sandbox of this queue is named <prefix><run>...; the trailing hyphen keeps queue a1 off a10's names."""
    return re.sub(r"[^a-z0-9-]+", "-", f"ps-{queue_id}-".lower())


def sandbox_name(queue_id, run, attempt):
    """ps-<queue>-<run, shortened>-a<attempt>-<hash>. Maritime names are 1 to 63 lowercase letters, digits and
    hyphens starting with a letter, and a create with a live sandbox's name returns that sandbox, so two runs must
    never share one: the hash of (queue, exact run name, attempt) keeps a_b, a.b, A-b and long names that differ
    only at the end apart."""
    pre = name_prefix(queue_id)
    tail = f"-a{attempt}-" + hashlib.sha256(f"{queue_id}\0{run}\0{attempt}".encode()).hexdigest()[:8]
    body = re.sub(r"[^a-z0-9-]+", "-", run.lower()).strip("-")[: 63 - len(pre) - len(tail)].rstrip("-") or "run"
    return pre + body + tail


class Journal:
    """runs/cloud/_queues/<id>/created.jsonl: every create intent and every sandbox id, written (and fsynced) the
    moment it is known, so a crashed launcher's sandboxes can be found by --cleanup. Ids and names only: never the
    create response (it carries the sandbox secret)."""

    def __init__(self, path: Path):
        self.path, self.lock = path, threading.Lock()

    def add(self, event, **kw):
        line = json.dumps({"event": event, "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), **kw})
        with self.lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def ids(self):
        if not self.path.exists():
            return []
        out = []
        for ln in self.path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(ln)
            except ValueError:
                continue                  # a line cut short by a crash
            if e.get("event") == "created" and e.get("id") not in out:
                out.append(e["id"])
        return out


def ours(mar, queue_id, live, ids):
    """Live sandboxes of this queue: in the journal, or named with its exact prefix AND carrying queue=<id>."""
    pre = name_prefix(queue_id)
    hit = []
    for s in live:
        if s.get("id") in ids:
            hit.append(s)
        elif str(s.get("name", "")).startswith(pre) and mar.metadata(s).get("queue") == queue_id:
            hit.append(s)
    return hit


def sandbox_info(mar, sbx):
    """State and cost of one sandbox (selected fields only: the full info carries the sandbox secret)."""
    i = mar.json("sandbox", "info", sbx, "--data-source", "live", "--no-cache",
                 "--select", "id,state,endedReason,runningSeconds,costSoFarUsd")
    return {"sandbox": sbx, "state": i.get("state"), "ended_reason": i.get("endedReason"),
            "running_s": i.get("runningSeconds"), "cost_usd": float(i.get("costSoFarUsd") or 0)}


def kill_sandbox(mar, sbx, log, journal=None):
    """Kill, then confirm with info that the sandbox ended; retry a few times. info['confirmed'] says whether it
    did."""
    info = {"sandbox": sbx}
    for i, wait in enumerate((0, *KILL_RETRY_WAITS_S)):
        time.sleep(wait)
        try:
            mar.json("sandboxes", "kill", sbx)
        except Exception as e:  # noqa: BLE001 (the info read decides whether it ended)
            log(f"kill {sbx} failed (try {i + 1}): {e}")
        try:
            info = sandbox_info(mar, sbx)
        except Exception as e:  # noqa: BLE001
            info = {"sandbox": sbx, "info_error": str(e)}
            log(f"info {sbx} failed (try {i + 1}): {e}")
            continue
        if info["state"] == "ended":
            break
    info["confirmed"] = info.get("state") == "ended"
    log(f"{'killed' if info['confirmed'] else 'KILL NOT CONFIRMED'} {sbx}: {info.get('state')}, "
        f"{info.get('running_s')} s, ${info.get('cost_usd') or 0:.6f}")
    if journal is not None:
        journal.add("killed" if info["confirmed"] else "kill_unconfirmed", id=sbx, state=info.get("state"))
    return info


def cleanup_queue(mar, queue_id, journal, log, kill):
    """Kill every live sandbox of the queue, then list again. Returns the record; 'left' empty means confirmed."""
    ids = journal.ids()
    rec = {"journal_ids": ids}
    targets = ours(mar, queue_id, mar.live_sandboxes(), ids)
    rec["killed"] = [s["id"] for s in targets]
    for s in targets:
        log(f"cleanup: killing {s['id']} ({s.get('name')})")
        kill(s["id"])
    live = mar.live_sandboxes()
    left = ours(mar, queue_id, live, ids)
    rec.update(left=[s["id"] for s in left], others_live=[s.get("id") for s in live if s not in left])
    return rec


# --- checks before anything is created -----------------------------------------------------------------------
def limit_seconds(args):
    if args.no_m0 and not args.pilot:
        raise QueueError("--no-m0 is for a --pilot only (a run without its M0 ceiling is not a result)")
    if args.pilot:
        if len(args.run) != 1:
            raise QueueError("a pilot queue runs exactly one config (PLAN: one pilot sandbox at the 24 h maximum)")
        return PILOT_LIMIT_S
    if args.measured_runtime_s is None:
        raise QueueError("no measured runtime: give --measured-runtime-s, or run one config with --pilot first")
    if args.measured_runtime_s > CHUNK_S:
        raise QueueError(f"measured runtime {args.measured_runtime_s:.0f} s is over 20 h: split the run into "
                         "chunks that save state (not supported by this launcher)")
    if args.measured_runtime_s <= 0:
        raise QueueError("--measured-runtime-s must be positive")
    s = int(math.ceil(max(MIN_LIMIT_S, 2 * args.measured_runtime_s) / 60.0)) * 60
    if s > PILOT_LIMIT_S:
        # PLAN: time limit = 2x the measured runtime. Over 12 h measured, 2x is past Maritime's 24 h sandbox limit;
        # capping it would quietly give the run less than 2x, so the queue is refused instead.
        raise QueueError(f"measured runtime {args.measured_runtime_s / 3600:.2f} h: 2x is {s / 3600:.2f} h, over "
                         f"Maritime's 24 h sandbox limit; the PLAN time limit can't be met, so split the run into "
                         "chunks that save state (not supported by this launcher)")
    return s


def m0_window(cfg, m0_path: Path) -> dict:
    """R2: the M0 folder's ceiling.json slice must be the config's window: the first step at local midnight of
    `start` and the last step one step before local midnight after `end` (slice_tz), as bench/m0.py writes it."""
    from zoneinfo import ZoneInfo
    try:
        sl = json.loads((m0_path / "ceiling.json").read_text(encoding="utf-8"))["slice"]
        s0, s1 = datetime.fromisoformat(str(sl["start_utc"])), datetime.fromisoformat(str(sl["end_utc"]))
        steps = int(sl["steps"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise QueueError(f"{m0_path}: ceiling.json has no readable slice start_utc/end_utc/steps ({e!r})")
    try:
        tz = ZoneInfo(str(cfg["slice_tz"]))
        d0, d1 = date.fromisoformat(str(cfg["start"])), date.fromisoformat(str(cfg["end"]))
    except (KeyError, ValueError) as e:
        raise QueueError(f"config has no readable start/end/slice_tz ({e!r})")
    want0 = datetime(d0.year, d0.month, d0.day, tzinfo=tz).astimezone(timezone.utc)
    nxt = d1 + timedelta(days=1)
    want_end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz).astimezone(timezone.utc)
    step = (s1 - s0) / (steps - 1) if steps > 1 else None
    got = {"m0_start_utc": s0.isoformat(), "m0_end_utc": s1.isoformat(), "m0_steps": steps,
           "config_start_utc": want0.isoformat(), "config_end_utc_exclusive": want_end.isoformat()}
    if s0 != want0 or step is None or s1 + step != want_end:
        raise QueueError(f"{m0_path}: M0 window {s0.isoformat()} to {s1.isoformat()} ({steps} steps) is not the "
                         f"config's {cfg['start']} to {cfg['end']} {cfg['slice_tz']} ({want0.isoformat()} to "
                         f"{want_end.isoformat()} exclusive)")
    return got


def run_context(cfg, name, cfg_path, runs_from: Path):
    """R1: files from other run folders that sim/run_m1.py reads when they exist, so a cloud run without them would
    silently differ from the laptop: runs/<compare_to>/summary.json (compare_to defaults to m1; 'none' needs
    nothing) and this run's earlier runs/<run>/summary.json when it carries a merged ablation_150 (or the run has an
    ablation/ folder). Returns [(local file, path under runs/)]; refuses when a needed file can't be found."""
    out = []
    compare = str(cfg.get("compare_to", "m1"))
    if compare.lower() != "none":
        if not NAME_OK.match(compare):
            raise QueueError(f"{cfg_path}: compare_to {compare!r} must match {NAME_OK.pattern}")
        src = runs_from / compare / "summary.json"
        if not src.is_file():
            raise QueueError(f"{cfg_path}: compare_to {compare} needs {src}, which isn't there (pass --runs-from with "
                             "the runs folder that holds it, or set compare_to: none)")
        out.append((src, f"{compare}/summary.json"))
    prior = runs_from / name / "summary.json"
    has_ablation = False
    if prior.is_file():
        try:
            has_ablation = "ablation_150" in json.loads(prior.read_text(encoding="utf-8"))
        except ValueError as e:
            raise QueueError(f"{prior}: unreadable earlier summary ({e})")
    if has_ablation:
        out.append((prior, f"{name}/summary.json"))
    elif (runs_from / name / "ablation").is_dir():
        raise QueueError(f"{cfg_path}: {runs_from / name / 'ablation'} exists, so the run carries ablation_150 over "
                         f"from an earlier summary.json, but {prior} has none")
    return out


def read_runs(args):
    runs, names = [], set()
    for cfg_path, m0_path in args.run:
        cfg_path, m0_path = Path(cfg_path).resolve(), Path(m0_path).resolve()
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        name = str(cfg.get("run_name", ""))
        if not NAME_OK.match(name):
            raise QueueError(f"{cfg_path}: run_name {name!r} must match {NAME_OK.pattern}")
        if name in names:
            raise QueueError(f"run_name {name} appears twice in the queue")
        names.add(name)
        if str(cfg.get("data_dir")).rstrip("/\\") != "../data":
            raise QueueError(f"{cfg_path}: data_dir must be ../data (cloud data lives at /home/user/data)")
        m0_dir = str(cfg.get("m0_dir", "runs/m0")).replace("\\", "/").rstrip("/")
        if not m0_dir.startswith("runs/") or ".." in m0_dir or not NAME_OK.match(m0_dir.replace("/", "_")):
            raise QueueError(f"{cfg_path}: m0_dir {m0_dir!r} must be a plain folder under runs/ (git-ignored)")
        if args.no_m0:
            # pilot only: measures runtime and idle behaviour, not a result (run_m1 runs without a ceiling)
            m0_files, m0_check = [], {"no_m0": "pilot without an M0 folder: not a result"}
        else:
            m0_files = sorted(p for p in m0_path.iterdir() if p.is_file()) if m0_path.is_dir() else []
            if not any(p.name == "ceiling.json" for p in m0_files):
                raise QueueError(f"{m0_path}: M0 folder has no ceiling.json")
            m0_check = m0_window(cfg, m0_path)
        context = run_context(cfg, name, cfg_path, Path(args.runs_from).resolve())
        for p in m0_files:
            if not NAME_OK.match(p.name):
                raise QueueError(f"{p}: file name not allowed")
        llm = sorted(k for k, v in cfg.items() if "llm" in str(k).lower() and v not in (None, False, 0, "", "none"))
        if llm and not args.llm:
            raise QueueError(f"{cfg_path}: has LLM settings {llm}: declare the queue with --llm (cap {CAP_LLM})")
        dest = REPO / "runs" / "cloud" / name
        if (dest / "DONE").exists():
            raise QueueError(f"{dest} is a finished cloud run (DONE): move it before re-running {name}")
        if dest.exists():
            raise QueueError(f"{dest} exists without DONE: an unfinished or pre-DONE download, not a result; move it "
                             f"aside before re-running {name} (the launcher never deletes or overwrites it)")
        runs.append({"name": name, "config": cfg_path, "m0": m0_path, "m0_files": m0_files, "m0_dir": m0_dir,
                     "config_sha256": hashlib.sha256(cfg_path.read_bytes()).hexdigest(), "dest": dest,
                     "m0_window": m0_check, "context": context})
    return runs


def check_caps(args, runs):
    cap = CAP_LLM if args.llm else CAP_CODE
    if not 1 <= args.concurrency <= cap:
        raise QueueError(f"concurrency {args.concurrency} outside 1..{cap} ({'LLM' if args.llm else 'code-only'} cap)")
    if not 1 <= args.max_awake <= CAP_CODE:
        raise QueueError(f"--max-awake {args.max_awake} outside 1..{CAP_CODE} (PLAN: never more than {CAP_CODE} awake)")
    if args.concurrency > args.max_awake:
        raise QueueError(f"concurrency {args.concurrency} is over --max-awake {args.max_awake}")
    if args.budget < len(runs):
        raise QueueError(f"budget {args.budget} sandboxes is less than the {len(runs)} runs")
    if args.budget > 2 * len(runs):
        raise QueueError(f"budget {args.budget} is more than runs plus one retry each ({2 * len(runs)})")


def check_spend(args, mar, limit_s, log):
    today = datetime.now(timezone.utc)
    p0 = datetime.fromisoformat(args.period_start).replace(tzinfo=timezone.utc)
    p1 = datetime.fromisoformat(args.period_end).replace(tzinfo=timezone.utc)
    if not p0 <= today < p1:
        raise QueueError(f"today is outside the billing period {args.period_start} to {args.period_end}: "
                         "pass --period-start/--period-end from the Maritime Billing page")
    u = mar.json("usage", "get", "--from", args.period_start,
                 "--to", (today.date() + timedelta(days=1)).isoformat())["results"]
    # hard cap = the lower of the account's cap (billing status capCents) and the PLAN.md cap (owner, Nov 5)
    b = mar.json("billing", "status")["results"]
    if b.get("capCents") is None:
        raise QueueError("billing status has no capCents: cannot confirm the account cap")
    account_cap = float(b["capCents"]) / 100.0
    cap = min(account_cap, HARD_STOP_USD)
    used = float(b.get("usedCents") or 0) / 100.0
    reported = float(u.get("totalCostCents") or 0) / 100.0
    minutes = float(u.get("computeMinutes") or 0)
    by_minutes = minutes / 60.0 * RATE_USD_H
    spend = max(reported, by_minutes, used)
    days_in = (p1 - p0).total_seconds() / 86400.0
    elapsed = max((today - p0).total_seconds() / 86400.0, 1.0)
    pace = HARD_STOP_USD * elapsed / days_in
    worst = args.budget * limit_s / 3600.0 * RATE_USD_H
    s = {"period": [args.period_start, args.period_end], "reported_usd": round(reported, 4),
         "compute_minutes": minutes, "by_minutes_usd": round(by_minutes, 4), "spend_usd": round(spend, 4),
         "pace_usd": round(pace, 2), "days_elapsed": round(elapsed, 2), "days_in_period": round(days_in, 2),
         "queue_worst_case_usd": round(worst, 4), "ratio_to_pace": round(spend / pace, 3) if pace else None,
         "billing_used_usd": round(used, 4), "account_cap_usd": account_cap, "plan_cap_usd": HARD_STOP_USD,
         "hard_cap_usd": cap}
    log(f"spend: period {args.period_start} to {args.period_end}, reported ${reported:.4f}, billing used "
        f"${used:.4f}, {minutes:.1f} compute min x ${RATE_USD_H}/h = ${by_minutes:.4f}; counted ${spend:.4f}; "
        f"pace ${pace:.2f} ({elapsed:.1f} of {days_in:.0f} days); queue worst case ${worst:.4f}; "
        f"hard cap ${cap:.2f} (account ${account_cap:.2f}, PLAN ${HARD_STOP_USD:.0f})")
    if spend + worst >= cap:
        raise QueueError(f"hard stop: spend ${spend:.2f} + queue worst case ${worst:.2f} reaches the hard cap "
                         f"${cap:.2f} (lower of account ${account_cap:.2f} and PLAN ${HARD_STOP_USD:.0f})")
    if spend > PACE_FROM_USD and spend > 1.5 * pace:
        s["report"] = f"spend ${spend:.2f} is over 1.5x pace (${1.5 * pace:.2f})"
        log("REPORT TO OWNER: " + s["report"])
    if spend > PACE_FROM_USD and spend > 2 * pace and not args.owner_approved_over_pace:
        raise QueueError(f"spend ${spend:.2f} is over 2x pace (${2 * pace:.2f}): needs the owner's approval "
                         "(--owner-approved-over-pace)")
    return s


class LauncherLock:
    """Held for the whole queue, from before the spend and cap checks until the process ends."""

    def __init__(self, path: Path = None):
        self.path = path or LOCK_PATH
        self.f = None

    def acquire(self, what, wait_s=LOCK_WAIT_S):
        """Windows frees a dead process's byte locks asynchronously, so a launcher killed a moment ago may still
        hold it: retry for wait_s before refusing."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+")
        t0 = time.time()
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() - t0 < wait_s:
                    time.sleep(0.5)
                    continue
            self.f.close()
            self.f = None
            raise QueueError(f"another cloud launcher holds {self.path}: one queue at a time, so two can't both "
                             "pass the spend and awake-cap checks")

    def release(self):
        if self.f is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    self.f.seek(0)
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self.f.close()
            self.f = None


# --- one run --------------------------------------------------------------------------------------------------
class Queue:
    def __init__(self, args, runs, limit_s, qdir, mar, log):
        self.args, self.runs, self.limit_s, self.qdir, self.mar, self.log = args, runs, limit_s, qdir, mar, log
        self.polls = CliPolls(mar) if getattr(args, "cli_polls", False) else ApiPolls(log)
        self.lock = threading.Lock()
        self.created = []          # every sandbox id this queue created, for cleanup
        self.journal = Journal(qdir / "created.jsonl")
        self.create_lock = threading.Lock()  # count of awake sandboxes and the create that follows, one at a time
        self.sandboxes = []        # cost log rows
        self.left = args.budget
        self.stop = threading.Event()   # set by Ctrl+C or the STOP file: no new sandbox, no retry, polls end
        self.stop_reason = None         # "ctrl_c", "stop_file" (the owner stopped it) or "kill_unconfirmed"
        self.hurry = threading.Event()  # second Ctrl+C: skip the rest of the stop grace

    def check_stop(self, what):
        if self.stop.is_set():
            raise Stopped(f"queue stopped before {what}")

    def take_slot(self):
        with self.lock:
            if self.stop.is_set() or self.left <= 0:
                return False
            self.left -= 1
            return True

    def run(self, r):
        for attempt in (1, 2):
            if self.stop.is_set():
                r["status"] = "stopped"
                r.setdefault("errors", []).append(f"attempt {attempt}: not started, queue stopped")
                return r
            if not self.take_slot():
                r.setdefault("errors", []).append(f"attempt {attempt}: no sandbox left in the budget")
                break
            try:
                self.attempt(r, attempt)
                r["status"] = "ok"
                return r
            except CreateRefused as e:
                r.setdefault("errors", []).append(f"attempt {attempt}: {e}")
                self.log(f"[{r['name']}] attempt {attempt} REFUSED: {e}")
                continue
            except Stopped as e:
                r["status"] = "stopped"
                r.setdefault("errors", []).append(f"attempt {attempt}: {e}")
                self.log(f"[{r['name']}] attempt {attempt} STOPPED: {e}")
                return r
            except RunError as e:
                r.setdefault("errors", []).append(f"attempt {attempt}: {e}")
                r["failure"] = "run"
                self.log(f"[{r['name']}] attempt {attempt} FAILED (not retried: it would fail the same way): {e}")
                break
            except QueueError as e:
                r.setdefault("errors", []).append(f"attempt {attempt}: {e}")
                r["failure"] = "infrastructure"
                self.log(f"[{r['name']}] attempt {attempt} FAILED (infrastructure, retried once): {e}")
            except Exception as e:  # noqa: BLE001 (unexpected: recorded with where it happened, retried once)
                tb = traceback.format_exc()
                r.setdefault("errors", []).append(f"attempt {attempt}: {e!r} at {tb.strip().splitlines()[-3:]}")
                r["failure"] = "unexpected"
                self.log(f"[{r['name']}] attempt {attempt} FAILED (unexpected): {e!r}")
        r["status"] = "failed"
        return r

    def attempt(self, r, attempt):
        name, mar = r["name"], self.mar
        r["attempt_now"] = attempt
        self.check_stop("create")
        sname = sandbox_name(self.args.queue_id, name, attempt)
        with self.create_lock:
            try:
                awake = self.wait_awake_slot(name)
            except Exception:
                self.refund()          # no sandbox was made: the budget slot goes back
                raise
            self.check_stop("create")
            sb = self.create(r, attempt, sname, awake)
        sbx = sb["id"]
        self.journal.add("created", id=sbx, run=name, attempt=attempt, name=sname,
                         recovered=bool(sb.get("recovered")))
        with self.lock:
            self.created.append(sbx)
        self.after_create(r, attempt, sb)

    def create(self, r, attempt, sname, awake):
        name, mar = r["name"], self.mar
        self.journal.add("intent", run=name, attempt=attempt, name=sname, awake_before=awake)
        try:
            sb = mar.json("sandboxes", "create", "--name", sname, "--from", SNAPSHOT, "--size", SIZE,
                          "--idle", IDLE, "--on-idle", "kill", "--time-limit", f"{self.limit_s // 60}m",
                          "--metadata", f"queue={self.args.queue_id}", "--metadata", f"run={name}")
            sb = {k: sb.get(k) for k in ("id", "name", "createdAt", "expiresAt", "from")}
            if not sb["id"]:
                raise QueueError("create returned no sandbox id")
        except Exception as e:  # noqa: BLE001 (the sandbox may exist even though the call failed)
            found = self.find_made_anyway(sname)
            if not found:
                self.refund()          # nothing was made: the create doesn't spend a budget slot
                self.journal.add("refused", run=name, attempt=attempt, name=sname, error=redact(str(e))[:300])
                raise CreateRefused(f"create made no sandbox (budget slot given back): {e}") from e
            self.log(f"[{name}] create call failed ({e}) but {found['id']} exists with its name: using it")
            sb = {"id": found["id"], "name": sname, "createdAt": None, "expiresAt": None, "recovered": True}
        return sb

    def after_create(self, r, attempt, sb):
        name, sbx = r["name"], sb["id"]
        row = {"run": name, "attempt": attempt, "sandbox": sbx, "created_utc": sb.get("createdAt"),
               "time_limit_s": self.limit_s, "peak_mem_mib": None}
        self.sandboxes.append(row)
        self.log(f"[{name}] sandbox {sbx} (attempt {attempt}, time limit {self.limit_s // 60} min, "
                 f"idle {IDLE}, expires {sb.get('expiresAt')})")
        try:
            self.upload(r, sbx)
            self.setup(r, sbx)
            self.start_and_poll(r, sbx, row)
            self.download(r, sbx)
        except Stopped:
            raise
        except BaseException:
            self.save_logs(r, sbx, attempt)
            raise
        finally:
            self.kill(sbx, row)

    def save_logs(self, r, sbx, attempt):
        """Before a failed attempt's sandbox is killed: its full setup.log, run.log and metrics.json (whatever
        exists) to runs/cloud/_queues/<id>/failed/<run>-a<attempt>/."""
        d = self.qdir / "failed" / f"{r['name']}-a{attempt}"
        got = []
        for f in ("setup.log", "run.log", "metrics.json"):
            try:
                self.mar.cp_down(sbx, f"{JOB}/{f}", d / f)
                got.append(f)
            except Exception:  # noqa: BLE001 (a stage that never started has no log)
                pass
        r.setdefault("failed_logs", []).append({"attempt": attempt, "dir": str(d.relative_to(REPO)), "files": got})
        self.log(f"[{r['name']}] saved {got or 'no logs'} to {d.relative_to(REPO)}")

    def sandbox_state(self, sbx):
        try:
            return self.polls.state(sbx)
        except Exception:  # noqa: BLE001
            return None

    def refund(self):
        with self.lock:
            self.left += 1

    def wait_awake_slot(self, name):
        """Hold until the whole account has fewer than --max-awake sandboxes awake (ours, other queues', the hosted
        exchange's). Called with create_lock held, so this launcher's own workers can't both take the last one."""
        t0, said = time.time(), False
        while True:
            awake = [s for s in self.mar.live_sandboxes() if s.get("state") not in ("ended", "paused")]
            if len(awake) < self.args.max_awake:
                return len(awake)
            if not said:
                self.log(f"[{name}] {len(awake)} sandboxes awake on the account (max {self.args.max_awake}): waiting")
                said = True
            if time.time() - t0 > AWAKE_GIVE_UP_S:
                raise QueueError(f"account stayed at {len(awake)} awake sandboxes for {AWAKE_GIVE_UP_S // 60} min")
            if self.stop.wait(AWAKE_WAIT_S):
                raise Stopped("queue stopped while waiting for an awake slot")

    def find_made_anyway(self, sname):
        """After a create error or timeout: the live sandbox with this exact name and this queue's metadata, if any.
        Retries the listing a few times, since the create may still be finishing."""
        for wait in FIND_WAITS_S:
            time.sleep(wait)              # even when stopped: a sandbox made anyway must be found to be killed
            try:
                hit = [s for s in self.mar.live_sandboxes() if s.get("name") == sname
                       and self.mar.metadata(s).get("queue") == self.args.queue_id]
            except Exception as e:  # noqa: BLE001
                self.log(f"listing after a failed create failed: {e}")
                continue
            if hit:
                return hit[0]
        return None

    def upload(self, r, sbx):
        mar, t0 = self.mar, time.time()
        jobdir = Path(tempfile.mkdtemp(prefix=f"cq_{r['name']}_", dir=self.qdir))
        ctx = "\n".join(f"mkdir -p runs/{rel.rsplit('/', 1)[0]} && cp {JOB}/ctx/{i}.json runs/{rel}"
                        for i, (_, rel) in enumerate(r["context"]))
        fmt = {"code": CODE, "job": JOB, "home": HOME, "commit": self.full_commit, "run_name": r["name"],
               "m0_dir": r["m0_dir"], "module": self.args.module, "context_cmds": ctx}
        for fname, text in (("stage.sh", STAGE_SH), ("status.sh", STATUS_SH), ("setup.sh", SETUP_SH.format(**fmt)),
                            ("run.sh", RUN_SH.format(**fmt)), ("measure.py", MEASURE_PY)):
            (jobdir / fname).write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
        shutil.copyfile(r["config"], jobdir / "config.yaml")
        for f in ("stage.sh", "status.sh", "setup.sh", "run.sh", "measure.py", "config.yaml"):
            self.check_stop("upload")
            mar.cp_up(sbx, jobdir / f, f"{JOB}/{f}")
        self.check_stop("upload")
        mar.cp_up(sbx, self.bundle, f"{JOB}/repo.bundle")
        for p in r["m0_files"]:
            self.check_stop("upload")
            mar.cp_up(sbx, p, f"{JOB}/m0/{p.name}")
        for i, (src, _) in enumerate(r["context"]):
            self.check_stop("upload")
            mar.cp_up(sbx, src, f"{JOB}/ctx/{i}.json")
        shutil.rmtree(jobdir, ignore_errors=True)
        self.log(f"[{r['name']}] uploaded bundle, config, {len(r['m0_files'])} M0 files, job scripts "
                 f"({time.time() - t0:.0f} s)")

    def wait_stage(self, sbx, stage, limit_s, row=None, name=""):
        t0, last, last_ok, n, missing = time.time(), "", time.time(), 0, False
        while True:
            p = self.polls.status(sbx, stage)
            line = (p.stdout or "").strip().splitlines()[-1:] or [""]
            line = line[0]
            if p.returncode != 0 or not line:
                err = redact((p.stderr or "").strip())[:300]
                if "sandbox_not_found" in err or self.sandbox_state(sbx) == "ended":
                    raise QueueError(f"{stage}: the sandbox ended while the launcher was polling it: {err}")
                if time.time() - last_ok > POLL_FAIL_S:
                    raise QueueError(f"{stage}: no good status poll for {POLL_FAIL_S // 60} min: {err}")
                if not missing:
                    self.log(f"[{name or sbx}] {stage}: status poll failed ({err[:120]}); retrying, the run fails "
                             f"after {POLL_FAIL_S // 60} min without a good poll")
                    missing = True
            else:
                if missing:
                    self.log(f"[{name or sbx}] {stage}: status poll good again")
                last_ok, missing = time.time(), False
                if line.startswith("EXIT "):
                    return int(line.split()[1]), time.time() - t0
                if line != last:
                    last = line
            n += 1
            if row is not None and n % 5 == 0:
                try:
                    u = self.polls.usage(sbx)
                    mem = u.get("memoryUsedMib")
                    if mem is not None:
                        row["peak_mem_mib"] = max(row["peak_mem_mib"] or 0, mem)
                    self.log(f"[{name}] {stage} {time.time() - t0:.0f} s, cpu {u.get('cpuPercent')}%, "
                             f"mem {mem} MiB, idle {u.get('idleSeconds')} s: {last[:100]}")
                except Exception as e:  # noqa: BLE001 (usage is information only)
                    self.log(f"[{name}] usage read failed: {e}")
            if time.time() - t0 > limit_s:
                raise QueueError(f"{stage}: still running after {limit_s} s")
            if self.stop.wait(self.args.poll_s):
                raise Stopped(f"queue stopped during {stage}")

    def setup(self, r, sbx):
        self.check_stop("setup")
        self.mar.exec_bg(sbx, "bash", f"{JOB}/stage.sh", "setup")
        rc, secs = self.wait_stage(sbx, "setup", 1200)
        log_text = self.mar.exec(sbx, "cat", f"{JOB}/setup.log").stdout
        r["setup_log"] = log_text
        if rc != 0:
            raise RunError(f"setup exit {rc}: {log_text.strip()[-500:]}")
        head = re.search(r"^HEAD ([0-9a-f]{40})$", log_text, re.M)
        porcelain = re.search(r"^PORCELAIN_BEGIN\n(.*?)^PORCELAIN_END$", log_text, re.M | re.S)
        if not head or head.group(1) != self.full_commit:
            raise RunError(f"sandbox HEAD {head.group(1) if head else '?'} is not {self.full_commit}")
        if porcelain is None or porcelain.group(1).strip():
            raise RunError(f"tree not clean after install: {porcelain.group(1).strip() if porcelain else '?'}")
        r["sandbox_head"] = head.group(1)
        freeze = re.search(r"^FREEZE_SHA256 ([0-9a-f]{64})$", log_text, re.M)
        r["pip_freeze_sha256"] = freeze.group(1) if freeze else None   # packages other than the repo itself
        r["snapshot"] = SNAPSHOT
        self.log(f"[{r['name']}] setup ok ({secs:.0f} s): HEAD {head.group(1)[:12]}, clean tree")

    def start_and_poll(self, r, sbx, row):
        self.check_stop("run")
        self.mar.exec_bg(sbx, "bash", f"{JOB}/stage.sh", "run")
        self.log(f"[{r['name']}] run started")
        rc, secs = self.wait_stage(sbx, "run", self.limit_s, row=row, name=r["name"])
        r["run_exit"], r["run_poll_s"] = rc, round(secs, 1)
        if rc != 0:
            tail = self.mar.exec(sbx, "tail", "-n", "30", f"{JOB}/run.log", check=False).stdout
            r["run_log_tail"] = tail
            raise RunError(f"run exit {rc}: {tail.strip()[-500:]}")
        self.log(f"[{r['name']}] run finished ({secs:.0f} s)")

    def download(self, r, sbx):
        mar, name = self.mar, r["name"]
        self.check_stop("download")
        stage = self.qdir / "dl" / name
        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)
        for f in ("out.sha256", "metrics.json", "run.log", "setup.log"):
            mar.cp_down(sbx, f"{JOB}/{f}", stage / f)
        want = (stage / "out.sha256").read_text().strip()
        for i in range(3):                      # a bad copy is downloaded again, not re-run
            mar.cp_down(sbx, f"{JOB}/out.tgz", stage / "out.tgz")
            got = hashlib.sha256((stage / "out.tgz").read_bytes()).hexdigest()
            if want == got:
                break
            self.log(f"[{name}] download sha256 mismatch (try {i + 1}): sandbox {want[:12]}, local {got[:12]}")
        else:
            raise QueueError(f"download sha256 mismatch 3 times: sandbox {want}, local {got}")
        # R3: build the whole folder under a temporary name, check it, mark it DONE, then one rename puts it in
        # place. runs/cloud/<run> either doesn't exist or is complete with DONE; a folder without DONE is unfinished.
        dest_root = REPO / "runs" / "cloud"
        dest_root.mkdir(parents=True, exist_ok=True)
        part = dest_root / f".partial-{name}-{uuid.uuid4().hex[:6]}"
        part.mkdir()
        try:
            with tarfile.open(stage / "out.tgz") as t:
                names = t.getnames()
                if any(n != name and not n.startswith(name + "/") for n in names):
                    raise QueueError(f"tarball has paths outside {name}/")
                t.extractall(part, filter="data")
            out = part / name
            missing = [f for f in REQUIRED_OUTPUTS if not (out / f).exists()]
            traces = sorted(str(t.relative_to(out)).replace("\\", "/") for t in out.rglob("trace.jsonl"))
            if missing or not traces:
                raise OutputIncomplete(f"run exited 0 but its output lacks {missing + ([] if traces else ['any trace.jsonl'])}")
            cdir = out / "cloud"
            cdir.mkdir(parents=True, exist_ok=True)
            for f in ("metrics.json", "run.log", "setup.log", "out.sha256"):
                shutil.copyfile(stage / f, cdir / f)
            metrics = json.loads((stage / "metrics.json").read_text())
            done = {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "queue": self.args.queue_id,
                    "sandbox": sbx, "attempt": r.get("attempt_now"), "commit": self.full_commit,
                    "out_sha256": got, "entries": len(names), "traces": traces, "metrics": metrics}
            (out / "DONE").write_text(json.dumps(done, indent=2), encoding="utf-8")
            if r["dest"].exists():
                raise QueueError(f"{r['dest']} appeared during the run: not overwritten")
            os.rename(out, r["dest"])
        finally:
            shutil.rmtree(part, ignore_errors=True)   # the launcher's own temporary folder, never a result
        r["metrics"], r["out_sha256"], r["traces"] = metrics, got, traces
        shutil.rmtree(stage, ignore_errors=True)
        self.log(f"[{name}] downloaded to {r['dest'].relative_to(REPO)} ({len(names)} entries, {len(traces)} traces, "
                 f"summary.json, sha256 ok, DONE written; elapsed {metrics.get('elapsed_s')} s, max RSS "
                 f"{metrics.get('max_rss_mb')} MB)")

    def kill(self, sbx, row):
        info = kill_sandbox(self.mar, sbx, self.log, self.journal)
        row.update({k: v for k, v in info.items() if k != "sandbox"})
        if not info["confirmed"] and not self.stop.is_set():
            self.log(f"STOP: kill of {sbx} not confirmed: no further sandbox is created (the awake count can't be "
                     "trusted); the final cleanup tries it again")
            self.stop_reason = self.stop_reason or "kill_unconfirmed"
            self.stop.set()
        return info

    def sweep(self, why):
        """Kill every sandbox this queue created (a kill of an ended sandbox returns it unchanged)."""
        with self.lock:
            ids = list(self.created)
        for sbx in ids:
            self.log(f"{why}: killing {sbx}")
            self.kill(sbx, next((row for row in self.sandboxes if row["sandbox"] == sbx), {"sandbox": sbx}))

    def run_all(self, stop_file: Path):
        """Run every config on at most --concurrency worker threads; a slot frees, the next config starts. Ctrl+C or
        the STOP file stops the queue: no new sandbox, no retry; workers kill their own sandboxes, and after
        STOP_SWEEP_S the main thread kills whatever is left. Workers are daemon threads, so a stuck CLI call can't
        hold the launcher open past STOP_GRACE_S."""
        pending = list(self.runs)

        def worker():
            while True:
                with self.lock:
                    if not pending or self.stop.is_set():
                        return
                    r = pending.pop(0)
                try:
                    self.run(r)
                except Exception as e:  # noqa: BLE001 (run() records its own failures; this is a last resort)
                    r["status"] = "failed"
                    r.setdefault("errors", []).append(f"worker: {e!r}")

        def on_sigint(signum, frame):
            if not self.stop.is_set():
                self.log("STOP: Ctrl+C: no new sandbox or retry; running sandboxes are being killed "
                         "(Ctrl+C again to skip the grace wait)")
                self.stop_reason = self.stop_reason or "ctrl_c"
                self.stop.set()
            else:
                self.log("second Ctrl+C: skipping the rest of the grace wait")
                self.hurry.set()

        signal.signal(signal.SIGINT, on_sigint)   # stays installed through cleanup; main() restores it
        threads = [threading.Thread(target=worker, name=f"cq-{i}", daemon=True)
                   for i in range(min(self.args.concurrency, len(self.runs)))]
        for t in threads:
            t.start()
        stop_at, swept = None, False
        while any(t.is_alive() for t in threads):
            if not self.stop.is_set() and stop_file.exists():
                self.log(f"STOP: {stop_file.name} found: no new sandbox or retry; running sandboxes are being killed")
                self.stop_reason = self.stop_reason or "stop_file"
                self.stop.set()
            if self.stop.is_set():
                stop_at = stop_at or time.time()
                waited = time.time() - stop_at
                if not swept and (waited > STOP_SWEEP_S or self.hurry.is_set()):
                    self.sweep("stop sweep")
                    swept = True
                if waited > STOP_GRACE_S or self.hurry.is_set():
                    alive = [t.name for t in threads if t.is_alive()]
                    self.log(f"stop grace over; workers still in a CLI call: {alive}; final cleanup follows")
                    break
            time.sleep(1)
        for r in self.runs:
            if not r.get("status"):
                r["status"] = "stopped" if self.stop.is_set() else "failed"
                r.setdefault("errors", []).append("not started" if self.stop.is_set() else "worker ended early")
        return self.runs


# --- queue ----------------------------------------------------------------------------------------------------
def make_bundle(commit, qdir):
    def git(*a):
        return subprocess.run(["git", *a], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()

    full = git("rev-parse", "--verify", f"{commit}^{{commit}}")
    if not git("branch", "--contains", full):
        raise QueueError(f"commit {commit} is on no local branch, so a bundle of the branches would not carry it")
    path = qdir / "repo.bundle"
    subprocess.run(["git", "bundle", "create", str(path), "--branches"], cwd=REPO, capture_output=True, check=True)
    git("bundle", "verify", str(path))
    return path, full


def eastern(ts_utc: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        return ts_utc.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:  # noqa: BLE001
        return ts_utc.strftime("%Y-%m-%d %H:%M UTC")


def cleanup_main(queue_id):
    """--cleanup <queue_id>: for a launcher that crashed or was killed. Never touches another queue's sandboxes."""
    if not NAME_OK.match(queue_id):
        raise SystemExit(f"queue id must match {NAME_OK.pattern}")
    qdir = REPO / "runs" / "cloud" / "_queues" / queue_id
    qdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with open(qdir / "cleanup.log", "a", encoding="utf-8") as lf:
        def log(msg):
            line = f"{datetime.now(timezone.utc).strftime('%H:%M:%SZ')} {msg}"
            print(line, flush=True)
            lf.write(line + "\n")
            lf.flush()

        journal = Journal(qdir / "created.jsonl")
        log(f"cleanup {queue_id}: journal {'has ' + str(len(journal.ids())) + ' ids' if journal.path.exists() else 'missing'}"
            f"; also matching name prefix {name_prefix(queue_id)} with queue={queue_id} metadata")
        rec = {"queue_id": queue_id, "utc": stamp}
        try:
            mar = Maritime(log)
            costs = []

            def kill(sbx):
                costs.append(kill_sandbox(mar, sbx, log, journal))
            rec.update(cleanup_queue(mar, queue_id, journal, log, kill))
            rec["killed_info"] = costs
            for sbx in rec["journal_ids"]:      # cost of every sandbox in the journal, ended or not
                if sbx not in rec["killed"]:
                    try:
                        rec.setdefault("ended_info", []).append(sandbox_info(mar, sbx))
                    except Exception as e:  # noqa: BLE001
                        rec.setdefault("ended_info", []).append({"sandbox": sbx, "info_error": str(e)})
        except Exception as e:  # noqa: BLE001
            rec["error"] = str(e)
            log(f"CLEANUP UNCONFIRMED: {e}")
        (qdir / f"cleanup_{stamp}.json").write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
        ok = "error" not in rec and not rec.get("left")
        log(f"cleanup {'CONFIRMED' if ok else 'NOT CONFIRMED'}: killed {rec.get('killed')}, left {rec.get('left')}; "
            f"record {(qdir / f'cleanup_{stamp}.json').relative_to(REPO)}")
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run configs on Maritime sandboxes (PLAN.md cloud run rules).")
    ap.add_argument("--run", nargs=2, action="append", default=None, metavar=("CONFIG", "M0_FOLDER"),
                    help="a config YAML and the local folder holding its M0 files (repeat per run)")
    ap.add_argument("--commit", default=None, help="commit to check out in the sandbox (must be on a local branch)")
    ap.add_argument("--concurrency", type=int, default=None, help=f"sandboxes awake at once ({CAP_CODE} max, "
                    f"{CAP_LLM} with --llm)")
    ap.add_argument("--budget", type=int, default=None, help="sandboxes this queue may create, retries included")
    ap.add_argument("--measured-runtime-s", type=float, default=None,
                    help="measured runtime of this run type on a sandbox; time limit = max(1 h, 2x)")
    ap.add_argument("--pilot", action="store_true", help="one run at the 24 h limit, for a run type not yet timed")
    ap.add_argument("--max-awake", type=int, default=CAP_CODE,
                    help=f"most sandboxes awake on the whole account before a create waits (default and max {CAP_CODE})")
    ap.add_argument("--no-m0", action="store_true",
                    help="pilot only: run without an M0 folder (give any path as M0_FOLDER); measures runtime, not a "
                         "result")
    ap.add_argument("--llm", action="store_true", help="the runs call an LLM (concurrency cap 5)")
    ap.add_argument("--runs-from", default=None,
                    help="local runs folder holding what a config reads from other runs (compare_to summary, an "
                         "earlier summary with ablation_150); default this checkout's runs/")
    ap.add_argument("--module", default="sim.run_m1", help="python -m entry point (default sim.run_m1)")
    ap.add_argument("--poll-s", type=float, default=60.0, help="seconds between status polls (default 60)")
    ap.add_argument("--cli-polls", action="store_true",
                    help="poll through maritime.cmd as before Nov 6 (default: direct Maritime API calls)")
    ap.add_argument("--period-start", default=PERIOD[0], help="billing period start (Maritime Billing page)")
    ap.add_argument("--period-end", default=PERIOD[1], help="billing period end, the next renewal date")
    ap.add_argument("--owner-approved-over-pace", action="store_true",
                    help="the owner approved starting this queue above 2x pace")
    ap.add_argument("--queue-id", default=None, help="name for this queue (default: UTC time plus a short id)")
    ap.add_argument("--dry-run", action="store_true", help="check the rules and print the plan; create nothing")
    ap.add_argument("--cleanup", metavar="QUEUE_ID", default=None,
                    help="kill every live sandbox of that queue (its created.jsonl journal, and its ps-<queue_id>- "
                         "name prefix with queue=<queue_id> metadata), then list again to confirm; nothing else runs")
    args = ap.parse_args(argv)

    if args.cleanup:
        return cleanup_main(args.cleanup)
    args.runs_from = args.runs_from or str(REPO / "runs")
    for flag in ("run", "commit", "concurrency", "budget"):
        if getattr(args, flag) is None:
            ap.error(f"--{flag} is required (except with --cleanup)")
    started = datetime.now(timezone.utc)
    args.queue_id = args.queue_id or started.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:4]
    if not NAME_OK.match(args.queue_id) or len(args.queue_id) > QUEUE_ID_MAX:
        raise SystemExit(f"--queue-id must match {NAME_OK.pattern} and be at most {QUEUE_ID_MAX} characters")
    if not COMMIT_OK.match(args.commit):
        raise SystemExit("--commit must be a hex commit hash")
    if not 5 <= args.poll_s <= 120:
        raise SystemExit("--poll-s must be 5 to 120 (the idle timeout is 10 min)")
    qdir = REPO / "runs" / "cloud" / "_queues" / args.queue_id
    used = (f"queue id {args.queue_id} is already used ({qdir.relative_to(REPO)} exists): give a new --queue-id; "
            f"if that queue may have live sandboxes, run --cleanup {args.queue_id}")
    if qdir.exists():
        raise SystemExit(used)
    if args.dry_run:   # nothing kept: the queue id stays free for the real run
        qdir = Path(tempfile.mkdtemp(prefix=f"cq-dry-{args.queue_id}-"))
    else:
        try:
            qdir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            raise SystemExit(used) from None
    log_lock, log_file = threading.Lock(), open(qdir / "queue.log", "a", encoding="utf-8")

    def log(msg):
        line = f"{datetime.now(timezone.utc).strftime('%H:%M:%SZ')} {msg}"
        with log_lock:
            print(line, flush=True)
            log_file.write(line + "\n")
            log_file.flush()

    report = {"header": run_header("bench/cloud_queue.py", vars(args)), "queue_id": args.queue_id,
              "started_utc": started.isoformat(timespec="seconds")}
    ok = stopped = False
    q = None
    prev_sigint = signal.getsignal(signal.SIGINT)
    lock = None
    try:
        mar = Maritime(log)
        if not args.dry_run:
            lock = LauncherLock().acquire(args.queue_id)
        runs = read_runs(args)
        check_caps(args, runs)
        limit_s = limit_seconds(args)
        report["time_limit_s"], report["idle"] = limit_s, IDLE
        report["spend_check"] = check_spend(args, mar, limit_s, log)
        snaps = [s.get("name") for s in mar.json("sandbox", "snapshots", "list").get("data", [])]
        if SNAPSHOT not in snaps:
            raise QueueError(f"snapshot {SNAPSHOT} not found")
        pre = mar.live_sandboxes()
        if pre:
            log(f"note: {len(pre)} sandboxes already live before this queue: {[s.get('id') for s in pre]}")
        bundle, full = make_bundle(args.commit, qdir)
        report["commit_full"] = full
        report["runs"] = [{"name": r["name"], "config": str(r["config"]), "config_sha256": r["config_sha256"],
                           "m0": str(r["m0"]), "m0_dir": r["m0_dir"], "m0_window": r["m0_window"],
                           "context_uploaded": [{"from": str(a), "to": f"runs/{b}",
                                                 "sha256": hashlib.sha256(a.read_bytes()).hexdigest()}
                                                for a, b in r["context"]]} for r in runs]
        log(f"queue {args.queue_id}: {len(runs)} runs at commit {full[:12]}, concurrency {args.concurrency}, "
            f"budget {args.budget} sandboxes, time limit {limit_s // 60} min, idle {IDLE}, "
            f"{'LLM' if args.llm else 'code-only'}")
        if args.dry_run:
            log("dry run: nothing created")
            ok = True
            return 0
        q = Queue(args, runs, limit_s, qdir, mar, log)
        q.bundle, q.full_commit = bundle, full
        log(f"polls: {'maritime.cmd (--cli-polls)' if args.cli_polls else 'Maritime API (' + API_BASE + ')'}")
        log(f"stop: Ctrl+C, or create {(qdir / 'STOP').relative_to(REPO)}")
        results = q.run_all(qdir / "STOP")
        for r, row in zip(results, report["runs"]):
            row.update({k: r.get(k) for k in ("status", "failure", "errors", "failed_logs", "sandbox_head",
                                              "run_exit", "metrics", "out_sha256", "traces", "snapshot",
                                              "pip_freeze_sha256")})
            row["dest"] = str(r["dest"].relative_to(REPO))
        ok = all(r.get("status") == "ok" for r in results)
    except QueueError as e:
        log(f"REFUSED/FAILED: {e}")
        report["error"] = str(e)
    except Exception as e:  # noqa: BLE001 (record it; cleanup and the report still run)
        log(f"FAILED (unexpected): {e!r}")
        report["error"] = repr(e)
        report["traceback"] = traceback.format_exc()[-4000:]
    finally:
        def finalize_error(what, e):
            report.setdefault("finalize_errors", []).append(f"{what}: {e!r}")
            log(f"FINALIZE: {what} failed: {e!r}")

        if q is not None:
            # kill anything of ours still live (normally none: each run kills its own sandbox): by the journal, and
            # by name prefix plus queue metadata for one whose create failed after the sandbox was made
            try:
                def kill(sbx):
                    q.kill(sbx, next((row for row in q.sandboxes if row["sandbox"] == sbx), {"sandbox": sbx}))
                rec = cleanup_queue(q.mar, args.queue_id, q.journal, log, kill)
                report["cleanup"] = rec
                missing = [i for i in q.created if i not in rec["journal_ids"]]
                if missing:
                    log(f"note: {missing} created but not in the journal")
                if rec["left"]:
                    ok = False
                    log(f"CLEANUP FAILED: still live: {rec['left']}")
                else:
                    log(f"cleanup confirmed: sandboxes list shows none of this queue's {len(q.created)} sandboxes"
                        + (f" ({len(rec['others_live'])} others live: {rec['others_live']})"
                           if rec["others_live"] else " (list is empty)"))
            except Exception as e:  # noqa: BLE001
                ok = False
                report["cleanup_error"] = str(e)
                log(f"CLEANUP UNCONFIRMED: {e}; run --cleanup {args.queue_id}")
            try:
                total = sum(row.get("cost_usd") or 0 for row in q.sandboxes)
                report["cost_log"] = {"sandboxes": q.sandboxes, "total_usd": round(total, 6)}
                for row in q.sandboxes:
                    log(f"cost: {row['run']} attempt {row['attempt']} {row['sandbox']}: {row.get('running_s')} s, "
                        f"${row.get('cost_usd', 0) or 0:.6f}")
                log(f"cost: queue total ${total:.6f} over {len(q.sandboxes)} sandboxes")
            except Exception as e:  # noqa: BLE001
                finalize_error("cost totals", e)
            try:
                with open(qdir / "cost_log.csv", "w", newline="", encoding="utf-8") as f:
                    cols = ["run", "attempt", "sandbox", "created_utc", "time_limit_s", "state", "ended_reason",
                            "running_s", "peak_mem_mib", "cost_usd"]
                    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
                    w.writeheader()
                    w.writerows(q.sandboxes)
                    w.writerow({"run": "TOTAL", "cost_usd": round(sum(r.get("cost_usd") or 0 for r in q.sandboxes), 6)})
            except Exception as e:  # noqa: BLE001
                finalize_error("cost_log.csv", e)
        for what, fn in (("bundle removal", lambda: (qdir / "repo.bundle").unlink(missing_ok=True)),
                         ("job folders", lambda: [shutil.rmtree(d) for d in qdir.glob("cq_*")]),
                         ("download staging", lambda: shutil.rmtree(qdir / "dl") if (qdir / "dl").exists() else None)):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                finalize_error(what, e)
        ended = datetime.now(timezone.utc)
        report["ended_utc"] = ended.isoformat(timespec="seconds")
        if report.get("finalize_errors"):
            ok = False
        # the owner stopped it (Ctrl+C or STOP), no run had failed before that, and every sandbox is confirmed
        # ended: "stopped", not "failed"
        stopped = (not ok and q is not None and q.stop_reason in ("ctrl_c", "stop_file")
                   and not any(r.get("status") == "failed" for r in q.runs)
                   and "error" not in report and "cleanup_error" not in report
                   and not (report.get("cleanup") or {}).get("left") and not report.get("finalize_errors"))
        report["ok"] = ok
        report["status"] = "ok" if ok else "stopped" if stopped else "failed"
        if q is not None:
            report["stop_reason"] = q.stop_reason
        try:
            (qdir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log(f"REPORT NOT WRITTEN: {e!r}")
            ok = False
        try:
            where = ("dry run, nothing kept" if args.dry_run
                     else f"report {(qdir / 'report.json').relative_to(REPO)}")
            log(f"queue {report['status'].upper()}; {where}; ended {eastern(ended)}")
            log_file.close()
        except Exception:  # noqa: BLE001
            pass
        if args.dry_run:
            shutil.rmtree(qdir, ignore_errors=True)
        signal.signal(signal.SIGINT, prev_sigint)
        if lock is not None:
            lock.release()
    return 0 if ok else 2 if stopped else 1


if __name__ == "__main__":
    sys.exit(main())
