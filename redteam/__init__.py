"""D61 part 2 / D70: the LLM red-team harness (M5). Fake backend only until a pre-registration says otherwise.

Attackers act only through the agent interface's gateway (iface/gateway.py) and any planted channels; the security
boundary is the tool allow-list (an attacker returns JSON tool calls, nothing else). The audit hook in
redteam/sandbox.py is a backstop, not a sandbox. Every number here is computed in code; the LLM never writes one."""
