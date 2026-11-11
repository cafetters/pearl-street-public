"""Trace logger: one JSON line per message, replayable. Line 1 is the header (config, seed, git commit)."""
import json
from pathlib import Path

from exchange import messages as M


class TraceLogger:
    def __init__(self, path: str | Path, header: dict):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("w", encoding="utf-8")
        self._write({"event": "header", **header})

    def _write(self, obj):
        self._f.write(json.dumps(obj, separators=(",", ":"), sort_keys=True) + "\n")

    def log(self, raw: str, event: str):
        """event: send | broadcast | dropped (D21 vocabulary)."""
        m = M.decode(raw)
        self._write({"run_id": m["run_id"], "t_utc": m["t_utc"], "event": event, "from": m["sender"], "to": m["to"],
                     "msg_type": m["msg_type"], "msg_id": m["msg_id"], "payload": m["payload"]})

    def refuse(self, raw: str, msg: dict | None, origin: str, reason: str):
        """D69: a message the bus refused (sender_mismatch, direct_delivery, malformed). Logged as `dropped` with the
        reason and the true origin; replay never feeds dropped lines to the exchange."""
        line = {"event": "dropped", "reason": reason, "origin": origin}
        if isinstance(msg, dict):
            line.update({"run_id": msg.get("run_id"), "t_utc": msg.get("t_utc"), "from": msg.get("sender"),
                         "to": msg.get("to"), "msg_type": msg.get("msg_type"), "msg_id": msg.get("msg_id"),
                         "payload": msg.get("payload")})
            try:
                json.dumps(line)
            except (ValueError, TypeError, RecursionError):       # hostile payload: keep the refusal, drop the body
                line.update({"payload": None, "msg_type": None, "to": None, "unloggable": True})
        else:
            line.update({"run_id": None, "t_utc": None, "from": None, "to": None, "msg_type": None, "msg_id": None,
                         "payload": None, "raw_len": len(raw) if isinstance(raw, str) else None})
        self._write(line)

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def line_to_msg(line: dict) -> dict:
    """Rebuild the message a trace line recorded."""
    return M.make(line["msg_type"], line["from"], line["to"], line["t_utc"], line["payload"], line["msg_id"],
                  line["run_id"])
