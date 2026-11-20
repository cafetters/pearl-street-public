"""A backstop, not a sandbox (owner, Nov 18, item 7). The security boundary is the tool allow-list: an attacker returns
JSON tool calls and the harness runs only the tools it was given. While an attacker's tool call is dispatched, this
audit hook refuses file opens, sockets and process creation in that thread, so a tool that would touch the file system
or the network by mistake fails loudly instead of handing an attacker a capability.

sys.addaudithook can't be removed, so the hook is installed once per process and checks a thread-local flag; outside
`guarded()` it does nothing."""
from __future__ import annotations

import contextlib
import sys
import threading

_STATE = threading.local()
_INSTALLED = False
BLOCKED_PREFIXES = ("open", "socket.", "subprocess.", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
                    "os.startfile", "os.remove", "os.rename", "os.mkdir", "shutil.", "ctypes.", "urllib.", "http.")


class SandboxViolation(PermissionError):
    pass


def _hook(event: str, args):
    if getattr(_STATE, "on", False) and event.startswith(BLOCKED_PREFIXES):
        raise SandboxViolation(f"blocked during attacker tool dispatch: {event}")


def install():
    global _INSTALLED
    if not _INSTALLED:
        sys.addaudithook(_hook)
        _INSTALLED = True


@contextlib.contextmanager
def guarded():
    install()
    prev = getattr(_STATE, "on", False)
    _STATE.on = True
    try:
        yield
    finally:
        _STATE.on = prev
