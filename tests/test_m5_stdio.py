"""The stdio MCP transport end to end (M5), in a subprocess on the synthetic world [sim, synthetic]: initialize,
tools/list, a raw line over the cap refused before parsing, the credential required by the config (refused without
it, bound with it), registration with the grant, and the credential never written to mcp_log.jsonl or server.log.
The token is made at runtime (no token-shaped literal in the repo)."""
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import yaml

from iface.credentials import sha256_hex
from redteam.synth import BASE_CFG, HEADROOM_MW

REPO = Path(__file__).resolve().parents[1]


def _config(tmp_path, run_name, token):
    cfg = {**BASE_CFG, "run_name": run_name, "data_dir": "../data", "headroom_mw": HEADROOM_MW,
           "synthetic_world": True, "attackers": [{"agent": "A", "type": "external"}],
           "gateway": {"credential_sha256": {sha256_hex(token): "A Owner LLC (simulated)"},
                       "limits": {"max_line_bytes": 4096, "pause_wall_s": 5.0}}}
    p = tmp_path / f"{run_name}.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return p


def _session(cfg_path, env_token, lines):
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    env.pop("PEARL_STREET_CREDENTIAL", None)
    if env_token is not None:
        env["PEARL_STREET_CREDENTIAL"] = env_token
    p = subprocess.Popen([sys.executable, "-m", "iface.mcp_stdio", "--config", str(cfg_path)], cwd=REPO, env=env,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = []
    for ln in lines:
        p.stdin.write((ln if isinstance(ln, str) else json.dumps(ln)).encode() + b"\n")
        p.stdin.flush()
        out.append(json.loads(p.stdout.readline()))
    p.stdin.close()
    p.wait(timeout=300)
    return out


def _call(i, name, args=None):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}


def _res(r):
    return json.loads(r["result"]["content"][0]["text"]), r["result"]["isError"]


def test_stdio_with_credential(tmp_path):
    tok = secrets.token_urlsafe(24)
    run = f"test_stdio_cred_{os.getpid()}"
    cfg = _config(tmp_path, run, tok)
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    out = _session(cfg, tok, [init, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                              '{"jsonrpc": "2.0", "id": 3, "method": "ping", "pad": "' + "z" * 5000 + '"}',
                              _call(4, "register", {"asset_id": "A/bess", "max_kw": 9000.0}),
                              _call(5, "subscribe", {"signals": ["call_nominations"], "zones": ["SYS"]})])
    assert out[0]["result"]["serverInfo"]["version"] == "0.3-m5"
    assert {t["name"] for t in out[1]["result"]["tools"]} >= {"register", "advance", "nominate"}
    assert out[2]["error"]["message"] == "message_too_large" and out[2]["error"]["data"]["max_bytes"] == 4096
    r, err = _res(out[3])
    assert not err and r["claimed_kw"] == 9000.0 and r["granted_kw"] == 2000.0
    assert not _res(out[4])[1]
    run_dir = REPO / "runs" / run
    for f in ("mcp_log.jsonl", "server.log"):
        assert tok not in (run_dir / f).read_text(encoding="utf-8")
    assert "in_oversize" in (run_dir / "mcp_log.jsonl").read_text(encoding="utf-8")


def test_stdio_without_the_required_credential_is_refused(tmp_path):
    tok = secrets.token_urlsafe(24)
    cfg = _config(tmp_path, f"test_stdio_nocred_{os.getpid()}", tok)
    out = _session(cfg, None, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                               _call(2, "my_asset"), _call(3, "register", {"asset_id": "A/bess", "max_kw": 1.0})])
    for r in out[1:]:
        res, err = _res(r)
        assert err and res == {"ok": False, "code": "no_credential"}
    out = _session(cfg, tok + "x", [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                                    _call(2, "my_asset")])
    res, err = _res(out[1])
    assert err and res == {"ok": False, "code": "bad_credential"}
