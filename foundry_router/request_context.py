"""Per-request client context that backend adapters may forward.

A client's own session / conversation identifiers (OpenCode's
x-opencode-session, a generic x-session-affinity, …) are the only reliable key
a stateful backend such as Meridian has for resuming the same Claude session —
which is what keeps its prompt cache warm and gives the model its own previous
turns. The facade records them here (a ContextVar, so concurrent requests never
see each other's) and the Anthropic adapter forwards the allow-listed ones.
"""

from __future__ import annotations

import contextvars
from typing import Optional

# Client headers worth forwarding to a session-aware backend, lower-case.
SESSION_HEADERS = ("x-session-affinity", "x-opencode-session", "x-session-id",
                   "x-litellm-session-id", "x-opencode-agent-mode",
                   "x-opencode-agent-name")

_client_headers: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "foundry_client_headers", default={})
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "foundry_request_id", default="")


def capture(headers) -> None:
    """Record the forwardable client headers + a request id for this request."""
    import uuid
    picked = {}
    try:
        for k in SESSION_HEADERS:
            v = headers.get(k)
            if v:
                picked[k] = str(v)[:200]
        rid = headers.get("x-request-id") or ""
    except Exception:
        rid = ""
    _client_headers.set(picked)
    _request_id.set(str(rid)[:100] or uuid.uuid4().hex)


def client_headers() -> dict:
    return dict(_client_headers.get())


def request_id() -> Optional[str]:
    return _request_id.get() or None


# -- MCP call attribution --------------------------------------------------------
# Who is making an MCP tool call right now — set by each caller (brain, worker
# loop, direct mode, aggregator, research, gateway) so the single MCP funnel
# (MCPManager.call_tool) can log source / caller / client per call.
_mcp_attr: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "foundry_mcp_attr", default={})


def set_mcp_attribution(source: str, caller: str = "", client: str = "") -> contextvars.Token:
    return _mcp_attr.set({"source": source, "caller": caller, "client": client})


def reset_mcp_attribution(token) -> None:
    try:
        _mcp_attr.reset(token)
    except Exception:
        pass


def mcp_attribution() -> dict:
    return dict(_mcp_attr.get())


async def mcp_attributed(coro, source: str, caller: str = "", client: str = ""):
    """Await an MCP call with its attribution set (works whether the caller
    awaits it directly or wraps it in a task)."""
    tok = set_mcp_attribution(source, caller, client)
    try:
        return await coro
    finally:
        reset_mcp_attribution(tok)
