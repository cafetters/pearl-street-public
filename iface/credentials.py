"""Credentials for the agent interface (D69's MCP credential; Q-N9 identity), one per legal entity.

The platform issues a credential to a legal entity at enrollment. Only its sha256 is stored (in memory, or in a run
config as `gateway.credential_sha256: {<sha256>: <legal entity>}`); the token itself lives with the client, e.g. in the
`PEARL_STREET_CREDENTIAL` environment variable an MCP client passes to the stdio server. A session bound to an entity may
act only for agents the metering service attests to that entity (asset_limits owner and agent_id, D69 rule 6).

Never logged: the stdio server redacts the environment value from mcp_log.jsonl, and nothing here writes a trace.
Tests build tokens at runtime (no token-shaped literals in the repo).

    python -m iface.credentials --entity "X Owner LLC (simulated)"     # prints a new token and its sha256
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import secrets as _secrets

ENV_VAR = "PEARL_STREET_CREDENTIAL"
REDACTED = "[REDACTED:credential]"


def sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class CredentialStore:
    def __init__(self, hashes: dict[str, str] | None = None):
        self._by_hash: dict[str, str] = dict(hashes or {})        # sha256 -> legal entity

    def issue(self, legal_entity: str) -> str:
        """A new credential for `legal_entity`. Only its hash is kept here; the caller hands the token to the client."""
        tok = _secrets.token_urlsafe(32)
        self._by_hash[sha256_hex(tok)] = str(legal_entity)
        return tok

    def entity(self, token) -> str | None:
        """The legal entity a token was issued to, or None. Compares hashes in constant time."""
        if not isinstance(token, str) or not token or len(token) > 512:
            return None
        h = sha256_hex(token)
        found = None
        for k, ent in self._by_hash.items():
            if hmac.compare_digest(k, h):
                found = ent
        return found

    def entities(self) -> set[str]:
        return set(self._by_hash.values())


def redact_text(text: str, tokens=()) -> str:
    """The text with every given token (and the environment's credential, if set) replaced."""
    for t in [*tokens, os.environ.get(ENV_VAR, "")]:
        if t and len(t) >= 8:
            text = text.replace(t, REDACTED)
    return text


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity", required=True)
    a = ap.parse_args()
    st = CredentialStore()
    tok = st.issue(a.entity)
    print(f"token (give to the client, never commit): {tok}")
    print(f"config: gateway: {{credential_sha256: {{{sha256_hex(tok)}: {a.entity!r}}}}}")
