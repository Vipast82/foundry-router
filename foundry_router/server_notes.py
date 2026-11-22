"""A tool server's own usage notes reach the model (v0.181.249).

An MCP server can send ``instructions`` when a session opens: its notes on how its
tools are meant to be used. Media_Gen's say every generate tool queues a job,
the model reports the job id and stops, checks once when asked, never in a loop,
and that 'game' is the library for Animal Uprising assets. Foundry never passed
them on, so a chat model saw only the tool descriptions.

``MCPManager`` keeps each server's instructions from its last initialize
(``svc.mcp.instructions``). For a server set to pass them
(``mcp_servers[].pass_instructions``, the MCP connection form), they are added to
the system prompt of a chat request whose tool set includes that server's tools:
the persona's granted tools, or in direct mode the tools actually offered (or the
tool finder, which reaches all granted ones). The text is the same on every turn,
so a model's prompt cache keeps it.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

MAX_CHARS = 8000                 # per server


def servers_in_toolset(svc, persona: Optional[dict], client_tools, direct: bool) -> set:
    """The MCP servers whose tools this request can call."""
    try:
        granted = svc.tool_registry.mcp_tools_for_persona(persona)
    except Exception:                                             # noqa: BLE001
        return set()
    names = {t.server for t in granted if t.server}
    if not direct or not names:
        return names
    from .facade.ollama_api import _direct_mcp_split
    from .tool_finder import FINDER_SERVER
    try:
        offered, _, _ = _direct_mcp_split(svc, persona, client_tools)
    except Exception:                                             # noqa: BLE001
        return set()
    servers = {td.server for td in offered.values()}
    return names if FINDER_SERVER in servers else (names & servers)


def block(svc, servers: set) -> str:
    """The text added to the system prompt for these servers ("" = none)."""
    parts = []
    known = getattr(svc.mcp, "instructions", None) or {}
    for name in sorted(servers):
        cfg = (getattr(svc.mcp, "servers", {}) or {}).get(name)
        if cfg is None or not getattr(cfg, "pass_instructions", False):
            continue
        text = str(known.get(name) or "").strip()[:MAX_CHARS]
        if text:
            parts.append(f"[Notes from the {name} tool server on using its tools]\n{text}")
    return "\n\n".join(parts)


def apply(svc, persona: Optional[dict], messages: list, client_tools, direct: bool) -> list:
    """The messages to send: the same list when nothing applies."""
    if not persona:
        return messages
    try:
        text = block(svc, servers_in_toolset(svc, persona, client_tools, direct))
    except Exception:                                             # noqa: BLE001
        log.exception("server instructions skipped")
        return messages
    if not text:
        return messages
    out = list(messages)
    first = out[0] if out else None
    if isinstance(first, dict) and first.get("role") == "system" \
            and isinstance(first.get("content"), str):
        if text in first["content"]:
            return messages
        out[0] = {**first, "content": f"{first['content']}\n\n{text}"}
    else:
        out.insert(0, {"role": "system", "content": text})
    return out
