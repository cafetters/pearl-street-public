"""Offline tests for bench/cloud_queue.py with a fake Maritime CLI (no sandbox, no network, no spend).

FakeCloud stands in for the Maritime API at the one place the launcher starts a maritime.cmd process
(Maritime._run), so the launcher's own retry, journal, stop and cleanup code runs unchanged. The real Maritime._run is
replaced by a guard that raises, so a test can never reach the real CLI. The git bundle is faked too.

Runs under pytest, or without it: python tests/test_cloud_queue.py
"""
import hashlib
import io
import json
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

import bench.cloud_queue as cq

FULL = "9bcc0d11d892d8c63e224be0d5575b799f407286"
TOKEN = "tok-ACCOUNT-0123456789abcdef"      # not shaped like any redacted secret: the test that it never leaks is real
DP_SECRET = "mst_SANDBOXSECRET0123"


class P:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class FakeCloud:
    """Sandboxes in memory. Knobs (all optional):
      run_s[name]            seconds a run stays RUNNING (default 0)
      run_rc[name]           the run's exit code (default 0)
      setup_head[name]       HEAD the setup log reports (default FULL)
      dirty[name]            porcelain text (default clean)
      no_summary[name]       the run's tarball lacks summary.json
      create_timeout_after   names whose create times out AFTER the sandbox exists (once each)
      create_429             how many creates are refused with HTTP 429 first (no sandbox made)
      kill_fail              how many kill calls fail first
      poll_fail_s            status polls fail for this long after a run starts
      foreign                extra live sandboxes owned by someone else: list of (name, metadata)
      run_log_extra          bytes appended to every run.log (M4: a secret value echoed by a run)
    """

    def __init__(self, **kw):
        self.lock = threading.Lock()
        self.sbx, self.n, self.calls = {}, 0, []
        self.run_s, self.run_rc, self.setup_head, self.dirty, self.no_summary = {}, {}, {}, {}, set()
        self.create_timeout_after, self.create_429, self.kill_fail, self.poll_fail_s = set(), 0, 0, 0
        self.billing = {"capCents": 2000, "usedCents": 0}
        self.run_log_extra = b""
        self.http_env = []        # (cmd, env, background) of every data-plane command sent with an env map
        for k, v in kw.items():
            setattr(self, k, v)
        for name, meta in kw.get("foreign", []):
            self._new(name, meta)

    def _new(self, name, meta):
        self.n += 1
        sid = f"sbx-fake{self.n:04d}"
        self.sbx[sid] = {"id": sid, "name": name, "metadata": meta, "state": "running", "stage": {}, "files": {},
                         "created": time.time()}
        return sid

    def live(self):
        return [s for s in list(self.sbx.values()) if s["state"] != "ended"]   # list(): workers insert

    def run_of(self, s):
        return (s["metadata"] or {}).get("run", "")

    def __call__(self, argv, cwd, timeout):
        with self.lock:
            self.calls.append(list(argv))
        a = [x for x in argv if x not in ("--json", "--no-input", "--no-cache")]
        while "--data-source" in a:
            i = a.index("--data-source")
            del a[i:i + 2]
        if "--select" in a:
            i = a.index("--select")
            del a[i:i + 2]
        head = tuple(a[:2])
        if head == ("sandboxes", "create"):
            return self.create(a)
        if head == ("sandboxes", "list"):
            if getattr(self, "list_fail_after_kill", False) and any(s["state"] == "ended" for s in list(self.sbx.values())):
                return P(1, "", "Error: GET /v1/sandboxes returned HTTP 500: {\"code\":\"internal\"}")
            return P(0, json.dumps({"sandboxes": [{"id": s["id"], "name": s["name"], "state": s["state"],
                                                   "metadata": s["metadata"]} for s in self.live()],
                                    "nextCursor": None}))
        if head == ("sandboxes", "kill"):
            with self.lock:
                if self.kill_fail > 0:
                    self.kill_fail -= 1
                    return P(1, "", "Error: POST /v1/sandboxes/x/kill returned HTTP 500: {\"code\":\"internal\"}")
            s = self.sbx.get(a[2])
            if not s:
                return P(1, "", "Error: returned HTTP 404: {\"code\":\"sandbox_not_found\"}")
            s["state"] = "ended"
            return P(0, json.dumps({"id": s["id"], "state": "ended"}))
        if head == ("sandbox", "info"):
            s = self.sbx.get(a[2])
            if not s:
                return P(1, "", "Error: returned HTTP 404: {\"code\":\"sandbox_not_found\"}")
            return P(0, json.dumps({"id": s["id"], "name": s["name"], "state": s["state"], "metadata": s["metadata"],
                                    "endedReason": "killed_by_user" if s["state"] == "ended" else None,
                                    "costSoFarUsd": "0.001", "runningSeconds": 30, "from": cq.SNAPSHOT}))
        if head == ("sandbox", "usage"):
            return P(0, json.dumps({"memoryUsedMib": 500, "cpuPercent": 90}))
        if head == ("usage", "get"):
            return P(0, json.dumps({"results": {"totalCostCents": 0, "computeMinutes": 0}}))
        if head == ("billing", "status"):
            return P(0, json.dumps({"results": self.billing}))
        if head == ("sandbox", "snapshots"):
            return P(0, json.dumps({"data": [{"name": cq.SNAPSHOT}]}))
        if head == ("sandbox", "cp"):
            return self.cp(a[2], a[3], cwd)
        if head == ("sandbox", "exec"):
            return self.exec(a)
        return P(2, "", f"fake: unknown command {argv}")

    def create(self, a):
        name = a[a.index("--name") + 1]
        meta = dict(m.split("=", 1) for i, m in enumerate(a) if i and a[i - 1] == "--metadata")
        with self.lock:
            if self.create_429 > 0:
                self.create_429 -= 1
                return P(1, "", 'Error: POST /v1/sandboxes returned HTTP 429: {"code":"awake_limit","retryAfter":1}')
            for s in self.live():
                if s["name"] == name:      # Maritime: a create with a live sandbox's name returns that sandbox
                    return P(0, json.dumps({"id": s["id"], "name": name, "createdAt": "t", "expiresAt": "t",
                                            "secret": "mst_SECRET", "urlToken": "mut_SECRET"}))
            sid = self._new(name, meta)
            self.peak_awake = max(getattr(self, "peak_awake", 0), len(self.live()))
            if name in self.create_timeout_after:
                self.create_timeout_after.discard(name)
                raise subprocess.TimeoutExpired(["maritime.cmd", "sandboxes", "create"], 300)
        return P(0, json.dumps({"id": sid, "name": name, "createdAt": "t", "expiresAt": "t", "from": cq.SNAPSHOT,
                                "secret": "mst_SECRET", "urlToken": "mut_SECRET"}))

    def cp(self, src, dst, cwd):
        if src.startswith("sbx-"):
            sid, path = src.split(":", 1)
            s = self.sbx.get(sid)
            if not s or s["state"] == "ended":
                return P(1, "", "Error: returned HTTP 404: {\"code\":\"sandbox_not_found\"}")
            data = self.file_in(s, path.rsplit("/", 1)[1])
            if data is None:
                return P(1, "", f"Error: {path}: no such file")
            (Path(cwd) / dst).write_bytes(data)
            return P(0)
        sid = dst.split(":", 1)[0]
        s = self.sbx.get(sid)
        if not s or s["state"] == "ended":
            return P(1, "", "Error: returned HTTP 404: {\"code\":\"sandbox_not_found\"}")
        s["files"][dst.split(":", 1)[1]] = (Path(cwd) / src).read_bytes()
        return P(0)

    def finished(self, s, stage):
        t = s["stage"].get(stage)
        if t is None:
            return False
        dur = 0 if stage == "setup" else self.run_s.get(self.run_of(s), 0)
        return time.time() - t >= dur

    def file_in(self, s, fname):
        run = self.run_of(s)
        done = self.finished(s, "run")
        rc = self.run_rc.get(run, 0)
        if fname == "setup.log":
            return self.setup_log(s).encode()
        if fname == "run.log":
            return b"fake run log\nline 2\n" + self.run_log_extra
        if not done:
            return None
        if fname == "metrics.json":
            return json.dumps({"elapsed_s": 1.0, "max_rss_mb": 400, "exit": rc}).encode()
        if fname in ("out.tgz", "out.sha256"):
            if rc != 0:
                return None
            tgz = self.tarball(run)
            return tgz if fname == "out.tgz" else hashlib.sha256(tgz).hexdigest().encode() + b"\n"
        return None

    def tarball(self, run):
        """Deterministic bytes (gzip mtime 0), like a file that sits in the sandbox between the two copies."""
        import gzip
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as t:
            files = {f"{run}/selfish/trace.jsonl": b'{"header": 1}\n'}
            if run not in self.no_summary:
                files[f"{run}/summary.json"] = b'{"ok": true}'
            for n, data in files.items():
                ti = tarfile.TarInfo(n)
                ti.size, ti.mtime = len(data), 0
                t.addfile(ti, io.BytesIO(data))
        return buf.getvalue()

    def setup_log(self, s):
        run = self.run_of(s)
        return (f"HEAD {self.setup_head.get(run, FULL)}\nFREEZE_SHA256 {'ab' * 32}\nPORCELAIN_BEGIN\n"
                f"{self.dirty.get(run, '')}PORCELAIN_END\n")

    def http(self, method, url, body, headers):
        """The Maritime API, for the launcher's polls (ApiPolls._http). Checks the bearer on every call."""
        import re as _re
        with self.lock:
            self.calls.append(["HTTP", method, url])
        auth = (headers or {}).get("Authorization", "")
        m = _re.match(r"https://dp\.fake/(sbx-[a-z0-9]+)/commands$", url)
        if m:
            if (headers or {}).get("X-Maritime-Token") != DP_SECRET or auth:
                return 401, '{"code":"unauthorized"}'
            if body.get("env"):
                with self.lock:
                    self.http_env.append((list(body["cmd"]), dict(body["env"]), bool(body.get("background"))))
            bg = ["--background"] if body.get("background") else []
            p = self.exec(["sandbox", "exec", m.group(1), *bg, "--", *body["cmd"]])
            code = _re.search(r"HTTP (\d{3})", p.stderr or "")
            if p.returncode != 0 and code:
                return int(code.group(1)), p.stderr
            return 200, json.dumps({"exitCode": p.returncode, "stdout": p.stdout, "stderr": p.stderr})
        if auth != f"Bearer {TOKEN}":
            return 401, '{"code":"unauthorized"}'
        m = _re.match(r"https://api\.maritime\.sh/v1/sandboxes/(sbx-[a-z0-9]+)(/usage|/info)?$", url)
        if not m or method != "GET":
            return 404, '{"code":"not_found"}'
        sid, tail = m.group(1), m.group(2)
        s = self.sbx.get(sid)
        if not s or (s["state"] == "ended" and tail != "/info"):
            return 404, '{"code":"sandbox_not_found"}'
        if tail == "/usage":
            return 200, json.dumps({"memoryUsedMib": 500, "cpuPercent": 90})
        if tail == "/info":
            return 200, json.dumps({"id": sid, "state": s["state"]})
        return 200, json.dumps({"id": sid, "dataPlaneUrl": f"https://dp.fake/{sid}", "secret": DP_SECRET})

    def exec(self, a):
        sid = a[2]
        s = self.sbx.get(sid)
        if not s or s["state"] == "ended":
            return P(1, "", "Error: returned HTTP 404: {\"code\":\"sandbox_not_found\"}")
        cmd = a[a.index("--") + 1:]
        if "--background" in a:
            s["stage"][cmd[-1]] = time.time()
            return P(0, "started")
        if cmd[:2] == ["bash", f"{cq.JOB}/status.sh"]:
            stage = cmd[2]
            if stage == "run" and self.poll_fail_s and time.time() - s["stage"].get("run", 0) < self.poll_fail_s:
                return P(1, "", "Error: returned HTTP 503: {\"code\":\"unavailable\"}")
            if self.finished(s, stage):
                rc = 0 if stage == "setup" else self.run_rc.get(self.run_of(s), 0)
                return P(0, f"EXIT {rc}\n")
            return P(0, "RUNNING step 3 of 9\n")
        if cmd[0] == "cat":
            data = self.file_in(s, cmd[1].rsplit("/", 1)[1])
            return P(0 if data is not None else 1, (data or b"").decode())
        if cmd[0] == "tail":
            return P(0, "fake run log tail\n")
        return P(0, "")


# --- harness --------------------------------------------------------------------------------------------------
class Env:
    """A temp REPO with configs and M0 folders, the fake cloud wired in, and the real CLI guarded off."""

    def __init__(self, cloud=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="cq_test_"))
        self.cloud = cloud or FakeCloud()
        self.saved = {}

    def __enter__(self):
        def guard(*a, **k):
            raise AssertionError("test reached the real maritime CLI")

        def fake_bundle(commit, qdir):
            p = qdir / "repo.bundle"
            p.write_bytes(b"bundle")
            return p, FULL

        for name, val in (("REPO", self.tmp), ("make_bundle", fake_bundle), ("EXEC_WAIT_S", 1),
                          ("STOP_SWEEP_S", 3), ("STOP_GRACE_S", 8),
                          ("LOCK_PATH", self.tmp / "launcher.lock"), ("AWAKE_WAIT_S", 0.2),
                          ("KILL_RETRY_WAITS_S", (0, 0, 0)), ("FIND_WAITS_S", (0,))):
            self.saved[name] = getattr(cq, name, None)
            setattr(cq, name, val)
        self.saved["_run"], self.saved["exe"] = cq.Maritime._run, None
        cloud = self.cloud
        cq.Maritime._run = lambda self_, argv, cwd, timeout: cloud(argv, cwd, timeout)
        self.saved["init"] = cq.Maritime.__init__
        self.saved["_http"] = cq.ApiPolls._http
        cq.ApiPolls._http = lambda self_, method, url, body=None, headers=None, timeout=None: \
            cloud.http(method, url, body, headers)
        self.saved["token_env"] = cq.os.environ.get("MARITIME_TOKEN")
        cq.os.environ["MARITIME_TOKEN"] = TOKEN

        def init(self_, log):
            self_.exe, self_.log = "fake-maritime", log
            self_.backoff_cap_s, self_.hint_max_s, self_.backoff_total_s = 0.2, 0.2, 3
        cq.Maritime.__init__ = init
        self.guard = guard
        (self.tmp / "runs").mkdir()
        return self

    def __exit__(self, *exc):
        cq.ApiPolls._http = self.saved["_http"]
        if self.saved["token_env"] is None:
            cq.os.environ.pop("MARITIME_TOKEN", None)
        else:
            cq.os.environ["MARITIME_TOKEN"] = self.saved["token_env"]
        for name, val in self.saved.items():
            if name in ("_run", "exe", "init", "_http", "token_env"):
                continue
            setattr(cq, name, val)
        cq.Maritime._run = self.saved["_run"]
        cq.Maritime.__init__ = self.saved["init"]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, name, start="2023-01-12", end="2023-01-13", **extra):
        cfg = self.tmp / "cfg" / f"{name}.yaml"
        cfg.parent.mkdir(exist_ok=True)
        lines = [f"run_name: {name}", "data_dir: ../data", f"start: {start}", f"end: {end}",
                 "slice_tz: America/Chicago", "m0_dir: runs/m0_jan12", "compare_to: none"]
        lines += [f"{k}: {v}" for k, v in extra.items()]
        cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return cfg

    def m0(self, tag="jan12", start_utc="2023-01-12 06:00:00+00:00", end_utc="2023-01-14 05:45:00+00:00", steps=192):
        d = self.tmp / "m0src" / tag
        d.mkdir(parents=True, exist_ok=True)
        (d / "ceiling.json").write_text(json.dumps({"slice": {"start_utc": start_utc, "end_utc": end_utc,
                                                              "steps": steps}}))
        (d / "run.json").write_text("{}")
        return d

    def main(self, *argv):
        return cq.main([*map(str, argv)])

    def qdir(self, qid):
        return self.tmp / "runs" / "cloud" / "_queues" / qid

    def report(self, qid):
        return json.loads((self.qdir(qid) / "report.json").read_text())


BASE = ["--commit", "9bcc0d1", "--measured-runtime-s", "100", "--poll-s", "5"]


def runs_args(env, names, m0=None):
    m0 = m0 or env.m0()
    out = []
    for n in names:
        out += ["--run", env.config(n), m0]
    return out


# --- tests ----------------------------------------------------------------------------------------------------
def test_happy_path_three_runs():
    with Env() as env:
        rc = env.main(*runs_args(env, ["r1", "r2", "r3"]), *BASE, "--concurrency", "3", "--budget", "3",
                      "--queue-id", "q-ok")
        rep = env.report("q-ok")
        assert rc == 0, rep
        assert [r["status"] for r in rep["runs"]] == ["ok"] * 3
        assert not env.cloud.live()
        for n in ("r1", "r2", "r3"):
            assert (env.tmp / "runs" / "cloud" / n / "summary.json").exists()
        assert {r["pip_freeze_sha256"] for r in rep["runs"]} == {"ab" * 32}
        assert {r["snapshot"] for r in rep["runs"]} == {cq.SNAPSHOT}
        setup = env.cloud.sbx["sbx-fake0001"]["files"][f"{cq.JOB}/setup.sh"].decode()
        assert f"checkout -q --detach {FULL}" in setup


def test_stop_file_no_new_sandbox_and_all_killed():
    cloud = FakeCloud(run_s={"s1": 60, "s2": 60, "s3": 60, "s4": 60})
    with Env(cloud) as env:
        qid = "q-stop"

        def drop_stop():
            while sum(1 for s in cloud.sbx.values() if "run" in s["stage"]) < 2:
                time.sleep(0.2)
            (env.qdir(qid) / "STOP").write_text("stop")
        threading.Thread(target=drop_stop, daemon=True).start()
        t0 = time.time()
        rc = env.main(*runs_args(env, ["s1", "s2", "s3", "s4"]), *BASE, "--concurrency", "2", "--budget", "8",
                      "--queue-id", qid)
        rep = env.report(qid)
        assert rc == 2, "stopped by the owner: exit 2, not 1"
        assert (rep["status"], rep["stop_reason"], rep["ok"]) == ("stopped", "stop_file", False)
        assert "queue STOPPED" in (env.qdir(qid) / "queue.log").read_text(encoding="utf-8")
        assert time.time() - t0 < 30
        assert len(cloud.sbx) == 2, "no sandbox after STOP"
        assert not cloud.live()
        assert {r["status"] for r in rep["runs"]} == {"stopped"}


def test_ctrl_c_reports_stopped_not_failed():
    cloud = FakeCloud(run_s={"c1": 60, "c2": 60, "c3": 60})
    with Env(cloud) as env:
        def press_ctrl_c():
            while sum(1 for s in cloud.sbx.values() if "run" in s["stage"]) < 2:
                time.sleep(0.2)
            signal.raise_signal(signal.SIGINT)
        threading.Thread(target=press_ctrl_c, daemon=True).start()
        rc = env.main(*runs_args(env, ["c1", "c2", "c3"]), *BASE, "--concurrency", "2", "--budget", "6",
                      "--queue-id", "q-cc")
        rep = env.report("q-cc")
        assert rc == 2, rep
        assert (rep["status"], rep["stop_reason"], rep["ok"]) == ("stopped", "ctrl_c", False)
        assert "queue STOPPED" in (env.qdir("q-cc") / "queue.log").read_text(encoding="utf-8")
        assert len(cloud.sbx) == 2, "the third run never starts"
        assert not cloud.live()
        assert [r["status"] for r in rep["runs"]] == ["stopped"] * 3


def test_a_run_failed_before_the_stop_keeps_the_queue_failed():
    cloud = FakeCloud(run_rc={"bad": 1}, run_s={"bad": 0, "slow": 60})
    with Env(cloud) as env:
        def stop_after_failure():
            while not (env.qdir("q-fs") / "failed" / "bad-a1").exists():
                time.sleep(0.2)
            (env.qdir("q-fs") / "STOP").write_text("stop")
        threading.Thread(target=stop_after_failure, daemon=True).start()
        rc = env.main(*runs_args(env, ["bad", "slow"]), *BASE, "--concurrency", "2", "--budget", "4",
                      "--queue-id", "q-fs")
        rep = env.report("q-fs")
        assert rc == 1, rep
        assert (rep["status"], rep["stop_reason"]) == ("failed", "stop_file")
        assert {r["name"]: r["status"] for r in rep["runs"]} == {"bad": "failed", "slow": "stopped"}
        assert not cloud.live()


def test_dry_run_leaves_the_queue_id_free():
    with Env() as env:
        args = [*runs_args(env, ["d1"]), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-dry"]
        left_before = set(Path(tempfile.gettempdir()).glob("cq-dry-q-dry-*"))
        assert env.main(*args, "--dry-run") == 0
        assert not env.qdir("q-dry").exists(), "a dry run writes nothing under _queues/"
        assert set(Path(tempfile.gettempdir()).glob("cq-dry-q-dry-*")) == left_before, "dry-run folder removed"
        assert not env.cloud.sbx, "a dry run creates nothing"
        assert env.main(*args) == 0, "the real run takes the id the dry run checked"
        assert env.report("q-dry")["status"] == "ok"
        for extra in ([], ["--dry-run"]):
            try:
                env.main(*args, *extra)
            except SystemExit as e:
                assert "already used" in str(e) and "--cleanup q-dry" in str(e), e
            else:
                raise AssertionError(f"a used queue id must be refused ({extra or 'real run'})")
        assert len(env.cloud.sbx) == 1


def test_create_timeout_after_sandbox_exists_is_found_journaled_and_killed():
    cloud = FakeCloud()
    with Env(cloud) as env:
        saved, cq.FIND_WAITS_S = cq.FIND_WAITS_S, (0,)
        try:
            # the name is only known inside the launcher: time out the first create, whatever its name
            orig = cloud.create

            def create_once(a, _done=[]):
                if not _done:
                    _done.append(1)
                    cloud.create_timeout_after.add(a[a.index("--name") + 1])
                return orig(a)
            cloud.create = create_once
            rc = env.main(*runs_args(env, ["t1"]), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-to")
        finally:
            cq.FIND_WAITS_S = saved
        rep = env.report("q-to")
        assert rc == 0, rep
        assert len(cloud.sbx) == 1 and not cloud.live()
        j = (env.qdir("q-to") / "created.jsonl").read_text()
        assert '"recovered": true' in j and "SECRET" not in j


def test_cleanup_kills_journal_and_prefix_but_not_lookalike_queue():
    with Env() as env:
        cloud = env.cloud
        qid = "ct-a1"
        mine_j = cloud._new("ps-ct-a1-r1-x", {"queue": qid, "run": "r1"})
        mine_p = cloud._new("ps-ct-a1-r2-y", {"queue": qid, "run": "r2"})        # not in the journal
        other = cloud._new("ps-ct-a10-r1-z", {"queue": "ct-a10", "run": "r1"})   # look-alike queue id
        stranger = cloud._new("ps-ct-a1-r9-w", {"queue": "someone-else"})        # right prefix, wrong metadata
        exch = cloud._new("hosted-exchange", {})
        qd = env.qdir(qid)
        qd.mkdir(parents=True)
        cq.Journal(qd / "created.jsonl").add("created", id=mine_j, run="r1", attempt=1, name="ps-ct-a1-r1-x")
        rc = env.main("--cleanup", qid)
        assert rc == 0
        live = {s["id"] for s in cloud.live()}
        assert mine_j not in live and mine_p not in live
        assert {other, stranger, exch} <= live


def test_final_listing_failure_still_writes_report_and_cost_log():
    cloud = FakeCloud(list_fail_after_kill=True)
    with Env(cloud) as env:
        rc = env.main(*runs_args(env, ["f1"]), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-lf")
        qd = env.qdir("q-lf")
        rep = env.report("q-lf")
        assert rc == 1 and rep["ok"] is False
        assert "cleanup_error" in rep
        assert (qd / "cost_log.csv").exists() and "f1" in (qd / "cost_log.csv").read_text()
        assert not (qd / "repo.bundle").exists()


def test_max_awake_counts_the_whole_account():
    cloud = FakeCloud(foreign=[("hosted-exchange-1", {}), ("hosted-exchange-2", {})],
                      run_s={"w1": 1, "w2": 1, "w3": 1, "w4": 1})
    with Env(cloud) as env:
        rc = env.main(*runs_args(env, ["w1", "w2", "w3", "w4"]), *BASE, "--concurrency", "3", "--budget", "4",
                      "--max-awake", "3", "--queue-id", "q-aw")
        assert rc == 0, env.report("q-aw")
        assert cloud.peak_awake <= 3, cloud.peak_awake


def test_second_launcher_is_refused_while_the_lock_is_held():
    with Env() as env:
        held = cq.LauncherLock().acquire("other")
        try:
            rc = env.main(*runs_args(env, ["l1"]), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-l1")
            assert rc == 1
            assert "another cloud launcher" in env.report("q-l1")["error"]
            assert not env.cloud.sbx
        finally:
            held.release()
        rc = env.main(*runs_args(env, ["l2"]), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-l2")
        assert rc == 0


def test_429_is_waited_out_without_spending_a_budget_slot():
    cloud = FakeCloud(create_429=2)
    with Env(cloud) as env:
        rc = env.main(*runs_args(env, ["b1"]), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-429")
        assert rc == 0, env.report("q-429")
        assert len(cloud.sbx) == 1


def test_refused_create_gives_the_slot_back_and_is_recorded():
    cloud = FakeCloud(create_429=10 ** 6)
    with Env(cloud) as env:
        rc = env.main(*runs_args(env, ["b2"]), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-ref")
        rep = env.report("q-ref")
        assert rc == 1 and not cloud.sbx
        errs = rep["runs"][0]["errors"]
        assert len(errs) == 2 and all("budget slot given back" in e for e in errs), errs
        n_create = sum(1 for c in cloud.calls if c[:2] == ["sandboxes", "create"])
        assert n_create < 40, n_create      # waited between tries, didn't pile up


def test_failing_polls_do_not_fail_a_run_within_the_window():
    cloud = FakeCloud(run_s={"p1": 4}, poll_fail_s=3)
    with Env(cloud) as env:
        rc = env.main(*runs_args(env, ["p1"]), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-pf")
        assert rc == 0, env.report("q-pf")


def test_unconfirmed_kill_stops_new_creates():
    cloud = FakeCloud(kill_fail=10 ** 6, run_s={"k1": 0, "k2": 0})
    with Env(cloud) as env:
        rc = env.main(*runs_args(env, ["k1", "k2"]), *BASE, "--concurrency", "1", "--budget", "2",
                      "--queue-id", "q-kf")
        rep = env.report("q-kf")
        assert rc == 1
        assert (rep["status"], rep["stop_reason"]) == ("failed", "kill_unconfirmed"), "not the owner's stop"
        assert len(cloud.sbx) == 1, "no create after an unconfirmed kill"
        assert rep["cleanup"]["left"], "the report says the sandbox is still live"


def test_errors_never_carry_the_sandbox_secret():
    assert "SECRETxyz123" not in cq.redact('{"secret":"mst_SECRETxyz123","urlToken":"mut_abcdefgh12"}')
    assert "abcdefgh12" not in cq.redact("token mut_abcdefgh12 leaked")


def test_missing_summary_leaves_no_result_folder():
    cloud = FakeCloud(no_summary={"n1"})
    with Env(cloud) as env:
        rc = env.main(*runs_args(env, ["n1"]), *BASE, "--concurrency", "1", "--budget", "2", "--queue-id", "q-ns")
        assert rc == 1
        root = env.tmp / "runs" / "cloud"
        assert not (root / "n1").exists()
        assert not [d for d in root.iterdir() if d.name.startswith(".partial")]
        assert "summary.json" in " ".join(env.report("q-ns")["runs"][0]["errors"])


def test_finished_run_has_done_and_a_folder_without_done_is_refused():
    with Env() as env:
        rc = env.main(*runs_args(env, ["d1"]), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-d1")
        assert rc == 0
        done = json.loads((env.tmp / "runs" / "cloud" / "d1" / "DONE").read_text())
        assert done["commit"] == FULL and done["traces"] == ["selfish/trace.jsonl"]
        (env.tmp / "runs" / "cloud" / "d2").mkdir()
        rc = env.main(*runs_args(env, ["d2"]), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-d2")
        assert rc == 1 and "without DONE" in env.report("q-d2")["error"]
        rc = env.main(*runs_args(env, ["d1"]), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-d3")
        assert rc == 1 and "finished cloud run" in env.report("q-d3")["error"]


def test_m0_window_must_match_the_config():
    with Env() as env:
        jan02 = env.m0("jan02", "2023-01-02 06:00:00+00:00", "2023-01-04 05:45:00+00:00", 192)
        rc = env.main("--run", env.config("w1"), jan02, *BASE, "--concurrency", "1", "--budget", "1",
                      "--queue-id", "q-m0")
        assert rc == 1 and "M0 window" in env.report("q-m0")["error"]
        assert not env.cloud.sbx
        one_day = env.m0("jan12d1", "2023-01-12 06:00:00+00:00", "2023-01-13 05:45:00+00:00", 96)
        rc = env.main("--run", env.config("w2"), one_day, *BASE, "--concurrency", "1", "--budget", "1",
                      "--queue-id", "q-m0b")
        assert rc == 1 and "M0 window" in env.report("q-m0b")["error"]
        # the real jan12 folder's window (Nov 4 parity run) passes
        assert cq.m0_window({"start": "2023-01-12", "end": "2023-01-13", "slice_tz": "America/Chicago"}, env.m0())


def test_compare_to_summary_is_uploaded_or_the_run_refused():
    with Env() as env:
        cfg = env.config("c1")
        cfg.write_text(cfg.read_text().replace("compare_to: none", "compare_to: m1_1"))
        rc = env.main("--run", cfg, env.m0(), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-c1")
        assert rc == 1 and "compare_to m1_1" in env.report("q-c1")["error"] and not env.cloud.sbx
        other = env.tmp / "otherruns"
        (other / "m1_1").mkdir(parents=True)
        (other / "m1_1" / "summary.json").write_text('{"m1_1": 1}')
        rc = env.main("--run", cfg, env.m0(), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-c2",
                      "--runs-from", other)
        assert rc == 0, env.report("q-c2")
        files = env.cloud.sbx["sbx-fake0001"]["files"]
        assert files[f"{cq.JOB}/ctx/0.json"] == b'{"m1_1": 1}'
        assert f"cp {cq.JOB}/ctx/0.json runs/m1_1/summary.json" in files[f"{cq.JOB}/setup.sh"].decode()


def test_missing_compare_to_key_means_m1():
    with Env() as env:
        cfg = env.config("c3")
        cfg.write_text(cfg.read_text().replace("compare_to: none\n", ""))
        rc = env.main("--run", cfg, env.m0(), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-c3")
        assert rc == 1 and "compare_to m1 " in env.report("q-c3")["error"]


def test_broken_config_uses_one_sandbox_others_finish_logs_saved():
    cloud = FakeCloud(run_rc={"bad": 1})
    with Env(cloud) as env:
        rc = env.main(*runs_args(env, ["ok1", "ok2", "bad"]), *BASE, "--concurrency", "3", "--budget", "4",
                      "--queue-id", "q-bad")
        rep = env.report("q-bad")
        assert rc == 1 and rep["ok"] is False
        st = {r["name"]: r for r in rep["runs"]}
        assert st["ok1"]["status"] == st["ok2"]["status"] == "ok"
        assert st["bad"]["status"] == "failed" and st["bad"]["failure"] == "run"
        assert len(st["bad"]["errors"]) == 1 and "run exit 1" in st["bad"]["errors"][0]
        assert sum(1 for s_ in cloud.sbx.values() if s_["metadata"]["run"] == "bad") == 1
        assert (env.qdir("q-bad") / "failed" / "bad-a1" / "run.log").exists()
        assert not (env.tmp / "runs" / "cloud" / "bad").exists()
        assert not rep["cleanup"]["left"]


def test_wrong_head_and_dirty_tree_are_not_retried():
    cloud = FakeCloud(setup_head={"h1": "f" * 40}, dirty={"h2": " M sim/run_m1.py\n"})
    with Env(cloud) as env:
        rc = env.main(*runs_args(env, ["h1", "h2"]), *BASE, "--concurrency", "2", "--budget", "4",
                      "--queue-id", "q-head")
        rep = env.report("q-head")
        assert rc == 1 and len(cloud.sbx) == 2
        errs = {r["name"]: r["errors"] for r in rep["runs"]}
        assert "HEAD" in errs["h1"][0] and "not clean" in errs["h2"][0]


def test_sandbox_names_never_collide_and_fit():
    import re as _re
    qid = "20261105T120000Z-ab12"
    long = "h2h_c_jan12_h100_m5_settlement_rule_max_first_call_d62_guarantee_on"
    names = [cq.sandbox_name(qid, r, a) for r in ("a_b", "a.b", "A_b", long, long + "x", long + "y")
             for a in (1, 2)]
    assert len(set(names)) == len(names)
    for n in names:
        assert len(n) <= 63 and _re.match(r"^[a-z][a-z0-9-]*$", n) and n.startswith(cq.name_prefix(qid)), n


def test_time_limit_is_2x_or_refused_never_capped():
    class A:
        pilot, run, no_m0 = False, [1], False
    a = A()
    a.measured_runtime_s = 292.6
    assert cq.limit_seconds(a) == 3600
    a.measured_runtime_s = 5 * 3600
    assert cq.limit_seconds(a) == 10 * 3600
    a.measured_runtime_s = 12 * 3600
    assert cq.limit_seconds(a) == 24 * 3600
    for bad in (12 * 3600 + 60, 15 * 3600, 21 * 3600):
        a.measured_runtime_s = bad
        try:
            cq.limit_seconds(a)
        except cq.QueueError as e:
            assert "24 h" in str(e) or "over 20 h" in str(e), str(e)
        else:
            raise AssertionError(f"{bad} s accepted")


def test_a_killed_launcher_leaves_no_stale_lock():
    tmp = Path(tempfile.mkdtemp(prefix="cq_lock_"))
    lock = tmp / "launcher.lock"
    code = ("import sys, time, bench.cloud_queue as cq; from pathlib import Path; "
            "held = cq.LauncherLock(Path(sys.argv[1])).acquire('x'); print('locked', flush=True); time.sleep(60)")
    child = subprocess.Popen([sys.executable, "-c", code, str(lock), "nov05-locktest"], stdout=subprocess.PIPE,
                             text=True, cwd=str(Path(__file__).resolve().parents[1]))
    try:
        assert child.stdout.readline().strip() == "locked"
        try:
            cq.LauncherLock(lock).acquire("y", wait_s=0).release()
        except cq.QueueError:
            pass
        else:
            raise AssertionError("lock not held by the child")
        child.kill()                 # our own child only (Popen handle), like a hard-killed launcher
        child.wait(10)
        cq.LauncherLock(lock).acquire("z").release()
    finally:
        if child.poll() is None:
            child.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_m0_only_for_a_pilot():
    with Env() as env:
        rc = env.main("--run", env.config("nm1"), env.tmp / "none", *BASE, "--concurrency", "1", "--budget", "1",
                      "--no-m0", "--queue-id", "q-nm1")
        assert rc == 1 and "--pilot only" in env.report("q-nm1")["error"]
        rc = env.main("--run", env.config("nm2"), env.tmp / "none", "--commit", "9bcc0d1", "--poll-s", "5",
                      "--pilot", "--no-m0", "--concurrency", "1", "--budget", "1", "--queue-id", "q-nm2")
        assert rc == 0, env.report("q-nm2")
        setup = env.cloud.sbx["sbx-fake0001"]["files"][f"{cq.JOB}/setup.sh"].decode()
        assert "if ls /home/user/job/m0/*" in setup


# --- polls over the Maritime API (owner, Nov 6) ---------------------------------------------------------------
def _status_cli_calls(cloud):
    return [c for c in cloud.calls if c[:2] == ["sandbox", "exec"] and "status.sh" in " ".join(c)]


def test_polls_go_to_the_api_not_the_cli_and_the_token_never_leaves_memory():
    cloud = FakeCloud(run_s={"p1": 2, "p2": 2})
    with Env(cloud) as env:
        rc = env.main(*runs_args(env, ["p1", "p2"]), *BASE, "--concurrency", "2", "--budget", "2", "--queue-id", "q-api")
        rep = env.report("q-api")
        assert rc == 0, rep
        http = [c for c in cloud.calls if c[0] == "HTTP"]
        assert any(c[1] == "POST" and c[2].endswith("/commands") for c in http)
        assert not _status_cli_calls(cloud)                        # no status poll through maritime.cmd
        assert not [c for c in cloud.calls if c[:2] == ["sandbox", "usage"]]
        qd = env.qdir("q-api")
        for f in qd.rglob("*"):
            if f.is_file():
                text = f.read_bytes().decode("utf-8", "replace")
                assert TOKEN not in text and DP_SECRET not in text, f


def test_cli_polls_flag_keeps_the_old_path():
    cloud = FakeCloud(run_s={"c1": 1})
    with Env(cloud) as env:
        rc = env.main(*runs_args(env, ["c1"]), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-cli",
                      "--cli-polls")
        assert rc == 0
        assert _status_cli_calls(cloud) and not [c for c in cloud.calls if c[0] == "HTTP"]


def test_a_bad_token_fails_polls_redacted_and_the_run_fails_closed_after_the_window():
    cloud = FakeCloud(run_s={"b1": 60})
    with Env(cloud) as env:
        cq.os.environ["MARITIME_TOKEN"] = "tok-WRONG-0123456789abcdef"
        saved = cq.POLL_FAIL_S
        cq.POLL_FAIL_S = 3
        try:
            rc = env.main(*runs_args(env, ["b1"]), *BASE, "--concurrency", "1", "--budget", "1", "--queue-id", "q-bad")
        finally:
            cq.POLL_FAIL_S = saved
        rep = env.report("q-bad")
        assert rc == 1 and rep["runs"][0]["status"] == "failed"
        assert "no good status poll" in json.dumps(rep["runs"][0]["errors"])
        assert not cloud.live()                                     # killed all the same
        log = (env.qdir("q-bad") / "queue.log").read_text(encoding="utf-8")
        assert "tok-WRONG-0123456789abcdef" not in log and "tok-WRONG" not in json.dumps(rep)


def test_the_data_plane_token_header_is_redacted():
    assert "s3cr3tvalue" not in cq.redact('X-Maritime-Token: s3cr3tvalue {"X-Maritime-Token": "s3cr3tvalue"}')


def test_api_errors_are_redacted_and_carry_no_headers():
    seen = []
    polls = cq.ApiPolls(seen.append, token_fn=lambda: TOKEN)
    polls._http = lambda method, url, body=None, headers=None, timeout=None: (
        500, '{"error":"boom","echo":"Authorization: Bearer ' + TOKEN + '","secret":"' + DP_SECRET + '"}')
    try:
        polls.usage("sbx-x1")
        raise AssertionError("expected a MaritimeError")
    except cq.MaritimeError as e:
        assert TOKEN not in str(e) and DP_SECRET not in str(e) and "HTTP 500" in str(e)
    assert TOKEN not in repr(polls)


def test_the_token_is_read_at_first_use_not_at_construction():
    n = []
    polls = cq.ApiPolls(print, token_fn=lambda: n.append(1) or TOKEN)
    assert n == []
    polls._http = lambda method, url, body=None, headers=None, timeout=None: (200, '{"state": "running"}')
    assert polls.state("sbx-x1") == "running" and n == [1]
    polls.state("sbx-x1")
    assert n == [1]                                                 # kept in memory, read once


def test_a_401_reads_the_token_again_once():
    toks = iter(["tok-old-0123456789abcdef", TOKEN])
    polls = cq.ApiPolls(print, token_fn=lambda: next(toks))
    polls._http = lambda method, url, body=None, headers=None, timeout=None: (
        (200, '{"state": "running"}') if headers["Authorization"] == f"Bearer {TOKEN}" else (401, "{}"))
    assert polls.state("sbx-x1") == "running"


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    only = sys.argv[1:]
    failed = 0
    for t in TESTS:
        if only and t.__name__ not in only:
            continue
        t0 = time.time()
        try:
            t()
            print(f"PASS {t.__name__} ({time.time() - t0:.1f} s)", flush=True)
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {t.__name__}: {e!r}", flush=True)
    sys.exit(1 if failed else 0)
